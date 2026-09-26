"""Small cooperative resource scheduler for desktop actions.

A worker limit prevents unbounded threads, but it cannot stop two otherwise
valid actions from racing over the same desktop, browser session, or filesystem.
This module provides shared/exclusive leases for the action registry:

* independent read-only claims may run together;
* an exclusive claim waits for every reader/writer of that resource;
* multi-resource claims are admitted atomically, so they cannot deadlock;
* a queued request observes its cancellation event and timeout.

It deliberately schedules only in-process action handlers.  It never attempts
to lock arbitrary files or Windows handles, which would be unreliable and could
block normal user interaction outside MARK LIV.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass


_MODE_SHARED = "shared"
_MODE_EXCLUSIVE = "exclusive"
_VALID_MODES = {_MODE_SHARED, _MODE_EXCLUSIVE}


def _normalise_claims(claims: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Return bounded, deterministic claims; invalid inputs fail closed."""
    merged: dict[str, str] = {}
    for raw_name, raw_mode in (claims or {}).items():
        name = str(raw_name).strip().casefold()
        mode = str(raw_mode).strip().casefold()
        if not name or len(name) > 64 or mode not in _VALID_MODES:
            raise ValueError("invalid action resource claim")
        # If callers accidentally name a resource twice, exclusive is safest.
        previous = merged.get(name)
        merged[name] = _MODE_EXCLUSIVE if _MODE_EXCLUSIVE in {previous, mode} else mode
    return tuple(sorted(merged.items()))


@dataclass
class ResourceLease:
    """An idempotently releasable group of action-resource claims."""

    _scheduler: "ActionResourceScheduler"
    claims: tuple[tuple[str, str], ...]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._scheduler._release(self.claims)

    def __enter__(self) -> "ResourceLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class ActionResourceScheduler:
    """Thread-safe shared/exclusive scheduler with bounded cooperative waits."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._shared: dict[str, int] = {}
        self._exclusive: set[str] = set()
        # Once a writer is queued, do not admit a stream of new readers ahead
        # of it forever. This is writer preference, not a global FIFO: it keeps
        # the implementation small while preventing desktop-write starvation.
        self._waiting_exclusive: dict[str, int] = {}

    def _available(self, claims: tuple[tuple[str, str], ...]) -> bool:
        for name, mode in claims:
            if mode == _MODE_SHARED:
                if name in self._exclusive or self._waiting_exclusive.get(name, 0):
                    return False
            elif name in self._exclusive or self._shared.get(name, 0):
                return False
        return True

    def _unregister_waiting_exclusive(self, names: tuple[str, ...]) -> None:
        for name in names:
            count = self._waiting_exclusive.get(name, 0) - 1
            if count > 0:
                self._waiting_exclusive[name] = count
            else:
                self._waiting_exclusive.pop(name, None)

    def acquire(
        self,
        claims: Mapping[str, str] | None,
        *,
        cancel_event=None,
        timeout_seconds: float | None = None,
        on_wait: Callable[[], None] | None = None,
    ) -> ResourceLease | None:
        """Wait for all claims atomically, returning ``None`` on cancel/timeout.

        ``on_wait`` runs once while the scheduler condition is held, so callers
        must keep it short and non-blocking. The registry uses it only for a
        bounded in-memory progress update.
        """
        normalised = _normalise_claims(claims)
        if not normalised:
            return ResourceLease(self, normalised)
        try:
            timeout = None if timeout_seconds is None else max(0.0, float(timeout_seconds))
        except (TypeError, ValueError):
            timeout = 0.0
        deadline = None if timeout is None else time.monotonic() + timeout
        waiting_notified = False
        writer_names = tuple(name for name, mode in normalised if mode == _MODE_EXCLUSIVE)
        writer_registered = False

        with self._condition:
            while not self._available(normalised):
                if cancel_event is not None and cancel_event.is_set():
                    if writer_registered:
                        self._unregister_waiting_exclusive(writer_names)
                        self._condition.notify_all()
                    return None
                if not writer_registered and writer_names:
                    for name in writer_names:
                        self._waiting_exclusive[name] = self._waiting_exclusive.get(name, 0) + 1
                    writer_registered = True
                if not waiting_notified:
                    waiting_notified = True
                    if on_wait is not None:
                        try:
                            on_wait()
                        except Exception:
                            pass
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    if writer_registered:
                        self._unregister_waiting_exclusive(writer_names)
                        self._condition.notify_all()
                    return None
                # Cancellation has no callback, so keep waits short without
                # spinning. This also avoids a stale queued live-action card.
                self._condition.wait(0.10 if remaining is None else min(0.10, remaining))

            if cancel_event is not None and cancel_event.is_set():
                if writer_registered:
                    self._unregister_waiting_exclusive(writer_names)
                    self._condition.notify_all()
                return None
            if writer_registered:
                self._unregister_waiting_exclusive(writer_names)
            for name, mode in normalised:
                if mode == _MODE_SHARED:
                    self._shared[name] = self._shared.get(name, 0) + 1
                else:
                    self._exclusive.add(name)
            # Removing a queued writer can unblock waiting readers. Notify even
            # when an exclusive lease was just granted; those readers must then
            # re-check the active writer before proceeding.
            self._condition.notify_all()
            return ResourceLease(self, normalised)

    def _release(self, claims: tuple[tuple[str, str], ...]) -> None:
        with self._condition:
            for name, mode in claims:
                if mode == _MODE_SHARED:
                    count = self._shared.get(name, 0) - 1
                    if count > 0:
                        self._shared[name] = count
                    else:
                        self._shared.pop(name, None)
                else:
                    self._exclusive.discard(name)
            self._condition.notify_all()

    def snapshot(self) -> dict[str, dict[str, int | bool]]:
        """A detached diagnostics view, mainly useful to deterministic tests."""
        with self._condition:
            names = sorted(set(self._shared) | self._exclusive | set(self._waiting_exclusive))
            return {
                name: {
                    "shared": self._shared.get(name, 0),
                    "exclusive": name in self._exclusive,
                    "waiting_exclusive": self._waiting_exclusive.get(name, 0),
                }
                for name in names
            }


__all__ = ["ActionResourceScheduler", "ResourceLease"]
