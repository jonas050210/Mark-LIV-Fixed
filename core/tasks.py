"""
One central activity/task registry for everything that takes time.

Downloads, timers, installs and multi-step agent runs all used to report their
progress in their own way — a log line here, a spoken sentence there, and for
the user often nothing at all until the result arrived. This module is the one
place they publish to instead: a small, thread-safe, in-memory registry of what
is running right now, and the UI renders *that* (``ui.py`` polls
:func:`snapshot` a few times a second and repaints only when :func:`revision`
changes).

Design rules, in order of importance:

- Never block the caller. Every operation is a short dict update under a lock;
  no I/O, no callbacks into other subsystems, no threads of its own.
- Never lie. A task's state is exactly what its owner reported: a task is only
  ``done`` when the owner verified it, and an owner that cannot verify says so
  in ``detail`` rather than flipping the state. :func:`fail` exists for that.
- Never grow. The registry keeps the most recent tasks and the finished ones
  only briefly (:data:`FINISHED_TTL`), so a long session cannot leak memory
  through here.

Owners use the module-level helpers (``start`` / ``update`` / ``finish`` /
``fail``) or keep the returned :class:`Task` and call its methods; both write
through the same lock. Long-running loops that support cancellation poll
:attr:`Task.cancel_requested`.
"""

from __future__ import annotations

import copy
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: Task kinds the UI special-cases for its label. Anything else renders as a
#: generic task, so a new owner does not have to be added here to work.
KIND_DOWNLOAD = "download"
KIND_TIMER = "timer"
KIND_AGENT = "agent"
KIND_INSTALL = "install"
KIND_UPDATE = "update"
KIND_UNINSTALL = "uninstall"
KIND_TASK = "task"

#: Queued/paused work remains active. Only owners acknowledge pause and completion.
RUNNING = "running"
QUEUED = "queued"
PAUSED = "paused"
ACTIVE_STATES = (RUNNING, QUEUED, PAUSED)
TERMINAL_STATES = ("done", "failed", "cancelled")
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

#: How long a finished task stays in :func:`snapshot` results (seconds), and
#: how many tasks are kept at all. Bounded on purpose: this is a live view of
#: the machine, not a history log.
FINISHED_TTL = 90.0
MAX_TASKS = 32

_LABELS = {
    "download": "↓",
    "upload": "↑",
    "install": "⊕",
    "update": "↻",
    "uninstall": "⊖",
    "timer": "⏱",
    "agent": "❯",
    "task": "•",
}


def format_bytes(n: Optional[float]) -> str:
    """Human byte count (``1.4 GB``), or ``''`` when the size is unknown."""
    try:
        value = float(n)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if value < 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            # Whole bytes are counts; from KB up a rounded-off "2 KB" would hide
            # the difference between 1.5 KB and 2.4 KB.
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return ""


def format_speed(bps: Optional[float]) -> str:
    """Transfer rate (``3.2 MB/s``), or ``''`` when unknown."""
    if not bps or bps <= 0:
        return ""
    return format_bytes(bps) + "/s"


def format_duration(seconds: Optional[float]) -> str:
    """``1h 04m`` / ``12m 05s`` / ``07s`` — compact, language-neutral."""
    try:
        total = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs:02d}s"


@dataclass
class Task:
    """One unit of long-running work, as its owner describes it."""

    id: str
    kind: str
    title: str
    state: str = RUNNING
    detail: str = ""
    progress: Optional[float] = None       # 0.0 … 1.0, None = indeterminate
    total_bytes: Optional[int] = None
    done_bytes: Optional[int] = None
    speed_bps: Optional[float] = None
    eta_seconds: Optional[float] = None
    error: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    meta: dict = field(default_factory=dict)
    cancel_requested: bool = False
    _on_cancel: Optional[Callable[[], None]] = None
    #: True while ``progress`` was computed from bytes rather than reported by
    #: the owner. Such a value is recomputed on the next byte update; a value the
    #: owner set itself always wins.
    _derived_progress: bool = False
    pausable: bool = False
    pause_requested: bool = False

    # ── owner API ────────────────────────────────────────────────────────────

    def update(self, **fields: Any) -> "Task":
        """Merge ``fields`` into the task and recompute derived values.

        Unknown keys are ignored rather than raising: a caller reporting an
        extra field must never be the thing that breaks a running download.
        Byte totals and speed are enough to derive progress and ETA, so an
        owner only reports the numbers it actually has.
        """
        with _LOCK:
            if self.state in TERMINAL_STATES:
                return self
            for key, value in fields.items():
                if key in ("id", "kind", "state") or key.startswith("_"):
                    continue
                if hasattr(self, key):
                    setattr(self, key, value)
                    if key == "progress" and value is not None:
                        self._derived_progress = False
            self._derive()
            self.updated_at = time.time()
            _touch()
        return self

    def finish(self, detail: str = "", **fields: Any) -> "Task":
        """Mark the task done. Only call this once the work is *verified*."""
        return self._close(DONE, detail=detail, **fields)

    def fail(self, error: str = "", detail: str = "", **fields: Any) -> "Task":
        """Mark the task failed, with the reason the world gave."""
        return self._close(FAILED, detail=detail, error=error, **fields)

    def cancel(self, detail: str = "Cancelled.") -> "Task":
        """Mark a task cancelled (the owner stopped it, or the user did)."""
        return self._close(CANCELLED, detail=detail)

    def request_cancel(self) -> None:
        """Ask the owner to stop. The owner's loop polls :attr:`cancel_requested`."""
        with _LOCK:
            if self.state in TERMINAL_STATES:
                return
            self.cancel_requested = True
            self.updated_at = time.time()
            _touch()
        cb = self._on_cancel
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def patch_meta(self, **fields) -> None:
        """Merge owner metadata atomically, preserving concurrent attachment updates."""
        with _LOCK:
            if self.state in TERMINAL_STATES:
                return
            self.meta = {**self.meta, **copy.deepcopy(fields)}
            self.updated_at = time.time()
            _touch()

    def append_meta(self, key, value, limit=32) -> None:
        with _LOCK:
            if self.state in TERMINAL_STATES:
                return
            entries = list(self.meta.get(key, []))
            entries.append(copy.deepcopy(value))
            self.meta = {**self.meta, key: entries[-max(1, limit):]}
            self.updated_at = time.time()
            _touch()

    def discard_meta_item(self, key, ident) -> None:
        """Remove revoked attachment context without overwriting concurrent updates."""
        with _LOCK:
            if self.state in TERMINAL_STATES:
                return
            entries = self.meta.get(key, [])
            self.meta = {**self.meta, key: [entry for entry in entries
                         if not isinstance(entry, dict) or entry.get("id") != ident]}
            self.updated_at = time.time()
            _touch()

    def request_pause(self) -> bool:
        """Cooperative: PAUSED is published only when the owner reaches a checkpoint."""
        with _LOCK:
            if not self.pausable or self.state not in ACTIVE_STATES:
                return False
            self.pause_requested = True
            _touch()
            return True

    def resume(self) -> bool:
        with _LOCK:
            if self.state not in ACTIVE_STATES:
                return False
            self.pause_requested = False
            if self.state == PAUSED:
                self.state = RUNNING
            _touch()
            return True

    def checkpoint(self) -> bool:
        """Owner-thread only. Wait without holding the registry lock; cancel wakes promptly."""
        while True:
            with _LOCK:
                if self.cancel_requested or self.state in TERMINAL_STATES:
                    return False
                if not self.pause_requested:
                    if self.state in (QUEUED, PAUSED):
                        self.state = RUNNING
                        _touch()
                    return True
                if self.state != PAUSED:
                    self.state = PAUSED
                    _touch()
            time.sleep(0.05)

    def _close(self, state: str, detail: str = "", error: str = "",
               **fields: Any) -> "Task":
        with _LOCK:
            if self.state in TERMINAL_STATES:
                return self
            for key, value in fields.items():
                if hasattr(self, key) and not key.startswith("_"):
                    setattr(self, key, value)
            self.state = state
            if detail:
                self.detail = detail
            if error:
                self.error = error
            if state == DONE and self.progress is not None and not self.error:
                # Finished work counts as complete even if the last byte count
                # the owner reported was a little behind.
                self.progress = 1.0
                self._derived_progress = False
            self.speed_bps = None
            self.eta_seconds = None
            self.pause_requested = False
            self.finished_at = time.time()
            self.updated_at = self.finished_at
            self._derive()
            _touch()
        return self

    # ── reporting ────────────────────────────────────────────────────────────

    def _derive(self) -> None:
        """Fill progress/ETA from bytes when the owner gave numbers only."""
        try:
            if (self.done_bytes is not None and self.total_bytes
                    and self.total_bytes > 0
                    and (self.progress is None or self._derived_progress)):
                # A download that reports real bytes must move its own bar: the
                # value derived from the previous pair of numbers is stale.
                self.progress = max(0.0, min(1.0, self.done_bytes / self.total_bytes))
                self._derived_progress = True
            if self.progress is not None:
                self.progress = max(0.0, min(1.0, float(self.progress)))
        except (TypeError, ValueError):
            self.progress = None
        try:
            if (self.speed_bps and self.speed_bps > 0
                    and self.done_bytes is not None and self.total_bytes
                    and self.total_bytes > self.done_bytes):
                self.eta_seconds = (self.total_bytes - self.done_bytes) / self.speed_bps
        except (TypeError, ValueError):
            self.eta_seconds = None

    def snapshot(self) -> dict:
        """Plain-dict view for the UI/reporting layer (never the live object)."""
        with _LOCK:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        age = (self.finished_at or time.time()) - self.started_at
        return {
            "id": self.id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "meta": copy.deepcopy(self.meta),
            "kind": self.kind,
            "label": _LABELS.get(self.kind, _LABELS["task"]),
            "title": self.title,
            "state": self.state,
            "detail": self.detail,
            "error": self.error,
            "progress": self.progress,
            "percent": (None if self.progress is None
                        else int(round(self.progress * 100))),
            "total_bytes": self.total_bytes,
            "done_bytes": self.done_bytes,
            "total_human": format_bytes(self.total_bytes),
            "done_human": format_bytes(self.done_bytes),
            "speed_bps": self.speed_bps,
            "speed_human": format_speed(self.speed_bps),
            "eta_seconds": self.eta_seconds,
            "eta_human": format_duration(self.eta_seconds),
            "elapsed_seconds": max(0.0, age),
            "elapsed_human": format_duration(age),
            "cancel_requested": self.cancel_requested,
            "pausable": self.pausable,
            "pause_requested": self.pause_requested,
            "finished": self.state in TERMINAL_STATES,
        }

    # ── convenience ──────────────────────────────────────────────────────────

    def __enter__(self) -> "Task":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Only an *exception* closes the task; a normal exit means the owner
        # reported the outcome itself (finish/fail), which is the honest path.
        if exc_type is not None:
            self.fail(error=f"{exc_type.__name__}: {exc}")
        return False


# ── registry ─────────────────────────────────────────────────────────────────

_LOCK = threading.RLock()
_TASKS: dict[str, Task] = {}
_ORDER: list[str] = []
_COUNTER = itertools.count(1)
_REVISION = 0
_LISTENERS: list[Callable[[dict], None]] = []


def _touch() -> None:
    global _REVISION
    _REVISION += 1
    for entry in list(_LISTENERS):
        try:
            entry({"revision": _REVISION})
        except Exception:
            pass


def _prune() -> None:
    """Drop finished tasks that aged out and cap the total (call with _LOCK)."""
    now = time.time()
    for tid in list(_ORDER):
        task = _TASKS.get(tid)
        if task is None:
            _ORDER.remove(tid)
            continue
        if (task.state not in ACTIVE_STATES and task.finished_at is not None
                and now - task.finished_at > FINISHED_TTL):
            _TASKS.pop(tid, None)
            _ORDER.remove(tid)
    # A long-lived first task must not pin every finished task behind it.
    finished = [tid for tid in _ORDER if _TASKS[tid].state in TERMINAL_STATES]
    while len(_ORDER) > MAX_TASKS and finished:
        oldest = finished.pop(0)
        _TASKS.pop(oldest, None)
        _ORDER.remove(oldest)


def start(kind: str, title: str, *, detail: str = "",
          total_bytes: Optional[int] = None,
          done_bytes: Optional[int] = None,
          progress: Optional[float] = None,
          on_cancel: Optional[Callable[[], None]] = None,
          pausable: bool = False, queued: bool = False,
          **meta: Any) -> Task:
    """Register a new running task and return it."""
    with _LOCK:
        task = Task(
            id=f"t{next(_COUNTER)}",
            kind=str(kind or KIND_TASK),
            title=str(title or "Task"),
            detail=str(detail or ""),
            total_bytes=total_bytes,
            done_bytes=done_bytes,
            progress=progress,
            meta=copy.deepcopy(meta),
            pausable=pausable,
            state=QUEUED if queued else RUNNING,
        )
        task._on_cancel = on_cancel
        task._derive()
        _TASKS[task.id] = task
        _ORDER.append(task.id)
        _prune()
        _touch()
        return task


def get(task_id: str) -> Optional[Task]:
    with _LOCK:
        return _TASKS.get(str(task_id or ""))


def update(task_id: str, **fields: Any) -> Optional[Task]:
    """Update a task by id. Missing ids are a no-op, never an error."""
    task = get(task_id)
    return task.update(**fields) if task is not None else None


def finish(task_id: str, detail: str = "", **fields: Any) -> Optional[Task]:
    task = get(task_id)
    return task.finish(detail=detail, **fields) if task is not None else None


def fail(task_id: str, error: str = "", detail: str = "",
         **fields: Any) -> Optional[Task]:
    task = get(task_id)
    return task.fail(error=error, detail=detail, **fields) if task is not None else None


def cancel(task_id: str, detail: str = "Cancelled.") -> Optional[Task]:
    task = get(task_id)
    return task.cancel(detail=detail) if task is not None else None


def request_cancel(task_id: str) -> bool:
    """Ask an owner to stop (what the HUD's ✕ button calls).

    Returns False when there is no such task. The owner's loop polls
    :attr:`Task.cancel_requested`; nothing is force-stopped from here.
    """
    task = get(task_id)
    if task is None:
        return False
    task.request_cancel()
    return True


def snapshot(include_finished_for: float = FINISHED_TTL,
             limit: int = 12) -> list[dict]:
    """The tasks worth showing, newest last.

    Running tasks always come first so an active download cannot be pushed off
    the panel by a burst of finished ones.
    """
    now = time.time()
    with _LOCK:
        _prune()
        rows: list[tuple[float, dict]] = []
        for tid in _ORDER:
            task = _TASKS.get(tid)
            if task is None:
                continue
            if (task.state not in ACTIVE_STATES and task.finished_at is not None
                    and now - task.finished_at > include_finished_for):
                continue
            rows.append((task.started_at, task.snapshot()))
    running = [r for r in rows if r[1]["state"] in ACTIVE_STATES]
    finished = [r for r in rows if r[1]["state"] not in ACTIVE_STATES]
    remaining = max(0, limit - len(running))
    ordered = running + (finished[-remaining:] if remaining else [])
    return [row for _ts, row in ordered[:max(1, limit)]]


def revision() -> int:
    """Monotonic counter; the UI repaints only when it changes."""
    with _LOCK:
        return _REVISION


def active() -> list[Task]:
    """Live :class:`Task` objects still running (owners/tests inspect these)."""
    with _LOCK:
        _prune()
        return [_TASKS[t] for t in _ORDER
                if _TASKS.get(t) is not None and _TASKS[t].state in ACTIVE_STATES]


def clear_finished() -> None:
    with _LOCK:
        for tid in list(_ORDER):
            task = _TASKS.get(tid)
            if task is not None and task.state not in ACTIVE_STATES:
                _TASKS.pop(tid, None)
                _ORDER.remove(tid)
        _touch()


def add_listener(fn: Callable[[dict], None]) -> None:
    """Optional push notification. The UI polls instead; this is for tests."""
    with _LOCK:
        if fn not in _LISTENERS:
            _LISTENERS.append(fn)


def remove_listener(fn: Callable[[dict], None]) -> None:
    with _LOCK:
        if fn in _LISTENERS:
            _LISTENERS.remove(fn)


def reset() -> None:
    """Test helper: forget everything and start ids/revisions over."""
    global _REVISION, _COUNTER
    with _LOCK:
        _TASKS.clear()
        _ORDER.clear()
        _COUNTER = itertools.count(1)
        _REVISION = 0
        _LISTENERS.clear()


__all__ = [
    "Task", "RUNNING", "QUEUED", "PAUSED", "ACTIVE_STATES", "TERMINAL_STATES", "DONE", "FAILED", "CANCELLED",
    "KIND_DOWNLOAD", "KIND_TIMER", "KIND_AGENT", "KIND_INSTALL", "KIND_UPDATE",
    "KIND_UNINSTALL", "KIND_TASK",
    "format_bytes", "format_speed", "format_duration",
    "start", "get", "update", "finish", "fail", "cancel", "request_cancel",
    "snapshot", "revision", "active", "clear_finished",
    "add_listener", "remove_listener", "reset",
]
