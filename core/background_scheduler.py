"""One loop for the recurring background jobs.

The topic monitor and the proactive check-in each ran their own ``while True``
loop, and each re-implemented the same three questions before doing anything:
is there a live session, is MARK LIV mid-sentence, did the user just speak. The
two answers drifted — the monitor treated 30 seconds of silence as enough, the
check-in used its own gate — and a third job would have meant a third copy.

This module keeps the timing and the gate in one place and leaves the work
itself where it belongs. Jobs are plain callables with an interval; the loop
decides when they may run.

The reminder action deliberately stays outside: it hands its jobs to the
operating system's scheduler so they fire while MARK LIV is closed, which an
in-process loop cannot do.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

# The gate every job passes through. Returning False means "not now"; the job
# is simply retried at the next tick.
GateFn = Callable[[], bool]
JobFn = Callable[[], Awaitable[None]]

TICK_SECONDS = 20


@dataclass
class Job:
    """One recurring background job."""

    name: str
    interval: float
    run: JobFn
    initial_delay: float = 0.0
    requires_silence: bool = True
    # None until the job is first considered: the clock the scheduler is run
    # against is decided by the caller, so the job must not bake in one of its
    # own at construction time.
    _next_run: float | None = field(default=None, repr=False)
    _failures: int = field(default=0, repr=False)

    def schedule_first(self, now: float) -> None:
        self._next_run = now + self.initial_delay

    def is_due(self, now: float) -> bool:
        if self._next_run is None:
            self.schedule_first(now)
        return now >= self._next_run

    def mark_ran(self, now: float) -> None:
        self._next_run = now + self.interval

    def back_off(self, now: float) -> float:
        """Delay the next attempt after a failure, up to eight intervals.

        A job that fails usually keeps failing — no network, no session — and
        retrying it on the normal interval turns one broken dependency into a
        stream of identical log lines.
        """
        self._failures = min(self._failures + 1, 3)
        delay = self.interval * (2 ** self._failures)
        self._next_run = now + delay
        return delay

    def mark_succeeded(self) -> None:
        self._failures = 0


class BackgroundScheduler:
    """Runs registered jobs on their own intervals behind a shared gate."""

    def __init__(self, gate: GateFn | None = None, *, tick: float = TICK_SECONDS) -> None:
        self._jobs: list[Job] = []
        self._gate = gate or (lambda: True)
        self._tick = max(1.0, float(tick))

    def add(
        self,
        name: str,
        interval: float,
        run: JobFn,
        *,
        initial_delay: float = 0.0,
        requires_silence: bool = True,
    ) -> Job:
        job = Job(
            name=name,
            interval=max(1.0, float(interval)),
            run=run,
            initial_delay=max(0.0, float(initial_delay)),
            requires_silence=requires_silence,
        )
        self._jobs.append(job)
        return job

    @property
    def jobs(self) -> list[Job]:
        return list(self._jobs)

    def due_jobs(self, now: float | None = None, *, gated: bool = True) -> list[Job]:
        """Jobs whose time has come and whose gate is open."""
        moment = time.monotonic() if now is None else now
        open_gate = self._gate() if gated else True
        return [
            job for job in self._jobs
            if job.is_due(moment) and (open_gate or not job.requires_silence)
        ]

    async def run_once(self, now: float | None = None) -> list[str]:
        """Run every due job once. Returns the names that ran."""
        moment = time.monotonic() if now is None else now
        ran = []
        for job in self.due_jobs(moment):
            try:
                await job.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = job.back_off(moment)
                print(
                    f"[Scheduler] '{job.name}' failed ({type(exc).__name__}); "
                    f"next attempt in {int(delay)}s."
                )
                continue
            job.mark_succeeded()
            job.mark_ran(moment)
            ran.append(job.name)
        return ran

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[Scheduler] tick failed ({type(exc).__name__}).")
