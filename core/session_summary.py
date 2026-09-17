"""
core/session_summary.py — get the conversation into long_term.json before the
process ends.

WHY THIS EXISTS
    A session summary is not a file write, it is an LLM round trip: the last 40
    turns go to the model, and what comes back is the one or two sentences the
    morning briefing later pops ("yesterday we talked about …"). That takes a few
    seconds, and this app has several ways to end in fewer than a few seconds —
    `shutdown_jarvis` calls os._exit(), closing the window kills the daemon
    thread the loop lives on, and a reconnect tears the task group down.

    The old code cleared the turn list *first* and then made the call:

        log = self._session_log
        self._session_log = []          # the conversation now exists only here
        summary = await to_thread(gemini.text, ...)   # up to 30 s
        if summary: save_session_summary(summary, lang)

    so every failure mode deleted the session instead of losing a summary:
    the call raised (offline, quota, timeout) → turns gone, nothing written;
    the call returned "" → same; the process ended mid-call → same. And the
    end-of-session call site was a bare `asyncio.create_task(...)` that nobody
    held a reference to, which is exactly the task os._exit() never runs.

    This module keeps the turns until they are ON DISK. The caller supplies the
    part that needs the app (the LLM call and the memory write) and gets the
    bookkeeping — hand-over, retry, single-flight, tracked task, bounded flush —
    from here, where it can be tested without PyQt6, sounddevice or a network.

WHY THE CALLER OWNS THE LIVE LOG
    main.py appends turns to `self._session_log` from the receive loop and reads
    the last few of them for proactive context. Taking that list by reference and
    emptying it in place keeps one list, so nothing that reads it has to change.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

# A conversation shorter than this is not worth an LLM call or a memory entry —
# "hi" / "hello" / "thanks" is not a session.
DEFAULT_MIN_TURNS = 3
# Ceiling on how much conversation is carried in RAM while waiting for a summary
# to land. Keeping the turns after a failure is the whole point, so the retry
# buffer needs a bound; 200 turns is far more than one sitting produces.
DEFAULT_MAX_PENDING = 200


class SessionSummary:
    """Holds conversation turns until they are safely written to memory."""

    def __init__(self,
                 log: list,
                 write: Callable[[list], Awaitable[bool]],
                 logger: Callable[[str], None] = print,
                 min_turns: int = DEFAULT_MIN_TURNS,
                 max_pending: int = DEFAULT_MAX_PENDING):
        #: The caller's live turn list. Emptied in place when a save starts.
        self.log = log
        #: Taken out of `log`, not yet on disk. Survives a failed attempt.
        self.pending: list = []
        self._write = write
        self._logger = logger
        self._min_turns = int(min_turns)
        self._max_pending = int(max_pending)
        self._running = False
        self._task: "asyncio.Task | None" = None

    # ── state ───────────────────────────────────────────────────────────────

    @property
    def waiting(self) -> int:
        """Turns that still owe the user a memory entry (live + not yet saved)."""
        return len(self.log) + len(self.pending)

    @property
    def dirty(self) -> bool:
        """True when there is enough unwritten conversation to be worth saving."""
        return self.waiting >= self._min_turns

    @property
    def task(self):
        """The in-flight save task, if any — exit paths await this."""
        return self._task

    # ── saving ──────────────────────────────────────────────────────────────

    async def save_now(self) -> bool:
        """One attempt: take the turns, hand them to the writer, keep them if it
        fails. Returns True only when the summary is on disk.

        Single-flight: two call sites can land here at once (the run loop's
        finally and a spoken shutdown), and summarising the same conversation
        twice would put two entries in long_term.json for one sitting.
        """
        self._collect()
        if len(self.pending) < self._min_turns:
            return False
        if self._running:
            return False
        self._running = True
        batch = list(self.pending)
        try:
            try:
                ok = bool(await self._write(batch))
            except asyncio.CancelledError:
                # Cut off mid-call (the loop is going down). The turns are still
                # in `pending`, so whoever gets there next can finish the job.
                raise
            except Exception as e:
                self._logger(f"[Memory] ⚠️ Session summary failed: {e}")
                ok = False
            if ok:
                # Only drop what was actually written — turns that arrived while
                # the model was thinking belong to the next summary, not to none.
                del self.pending[:len(batch)]
            else:
                self._logger(f"[Memory] ⚠️ Summary not written — keeping "
                             f"{len(self.pending)} turns for the next attempt.")
            return ok
        finally:
            self._running = False

    def _collect(self) -> None:
        """Move the live turns into the retry buffer (in place, capped)."""
        if self.log:
            self.pending.extend(self.log)
            del self.log[:]
        if len(self.pending) > self._max_pending:
            del self.pending[:len(self.pending) - self._max_pending]

    def start_task(self) -> bool:
        """Kick off a save in the background and KEEP THE REFERENCE.

        An untracked `asyncio.create_task()` is what used to be here, and a task
        nobody holds is a task os._exit() never runs. Returns False when there is
        nothing worth saving or a save is already in flight.
        """
        self._collect()
        if len(self.pending) < self._min_turns:
            return False
        t = self._task
        if t is not None and not t.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False                      # no loop here (called after teardown)
        self._task = loop.create_task(self.save_now())
        return True

    async def flush(self, timeout: float = 20.0) -> bool:
        """Wait for the summary to reach disk, with a hard budget.

        This is the last thing every exit path does. The budget matters: the
        write is an LLM call, and an assistant that refuses to quit because the
        network is down is worse than one that says it could not save. Returns
        True when nothing is left waiting.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))

        t = self._task
        if t is not None and not t.done():
            left = deadline - time.monotonic()
            if left > 0:
                try:
                    # shield: a timeout here must not cancel the write halfway and
                    # leave the caller believing it can still be resumed.
                    await asyncio.wait_for(asyncio.shield(t), left)
                except asyncio.TimeoutError:
                    self._logger("[Memory] ⚠️ Session summary timed out.")
                    # Give up on it PROPERLY: cancel and reap. The attempt holds
                    # the single-flight guard, so leaving it hung would block
                    # every later save until the process dies — and a later exit
                    # path (the window closing after a failed reconnect) is the
                    # one chance these turns have left.
                    t.cancel()
                    try:
                        await t
                    except BaseException:
                        pass
                except Exception as e:
                    self._logger(f"[Memory] ⚠️ Session summary task failed: {e}")

        left = deadline - time.monotonic()
        if left > 0 and self.dirty and not self._running:
            try:
                await asyncio.wait_for(self.save_now(), left)
            except asyncio.TimeoutError:
                self._logger("[Memory] ⚠️ Session summary timed out.")
            except Exception as e:
                self._logger(f"[Memory] ⚠️ Session summary failed: {e}")

        if self.waiting >= self._min_turns:
            self._logger(f"[Memory] ⚠️ {self.waiting} conversation turns could not "
                         f"be saved — they are gone with this process.")
            return False
        return True
