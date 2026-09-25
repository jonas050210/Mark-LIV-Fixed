"""Live action execution records shared by voice, dashboard, and plugins.

The assistant used to return a string from each dispatch branch, which made a
running operation impossible to identify or cancel from the control panel. This
small runtime keeps only bounded, in-memory records and exposes cooperative
cancellation events to handlers. It deliberately does not execute code: the
existing action registry remains the policy boundary.
"""
from __future__ import annotations

import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|credential|"
    r"(?:^|[_-])(?:text|content|message|query|instruction|clipboard|value)(?:$|[_-]))",
    re.IGNORECASE,
)


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        output = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                break
            clean_key = str(key)[:100]
            output[clean_key] = (
                "[redacted]" if _SENSITIVE_KEY.search(clean_key)
                else _safe_value(item, depth + 1)
            )
        return output
    return f"<{type(value).__name__}>"


def _safe_parameters(parameters: dict | None) -> dict[str, Any]:
    safe = _safe_value(parameters or {})
    return safe if isinstance(safe, dict) else {}


@dataclass
class ActionRun:
    action_id: str
    action: str
    parameters: dict[str, Any] = field(default_factory=dict)
    source: str = "voice"
    status: str = "queued"
    progress: int = 0
    message: str = "Queued"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action": self.action,
            "parameters": _safe_parameters(self.parameters),
            "source": self.source,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancel_requested": self.cancel_event.is_set(),
        }


class ActionRuntime:
    MAX_RUNS = 100

    def __init__(self) -> None:
        self._runs: dict[str, ActionRun] = {}
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict], None]] = []

    def subscribe(self, listener: Callable[[dict], None]) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)
                if len(self._listeners) > 100:
                    del self._listeners[: len(self._listeners) - 100]

    def _emit(self, run: ActionRun) -> None:
        event = run.as_dict()
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def start(self, action: str, parameters: dict | None = None,
              source: str = "voice", action_id: str | None = None) -> str:
        run = ActionRun(
            action_id=action_id or f"act-{uuid.uuid4().hex[:12]}",
            action=str(action)[:100],
            parameters=_safe_parameters(parameters),
            source=str(source)[:40],
        )
        with self._lock:
            self._runs[run.action_id] = run
            while len(self._runs) > self.MAX_RUNS:
                oldest = next(iter(self._runs))
                del self._runs[oldest]
            self._emit(run)
        return run.action_id

    def get(self, action_id: str) -> ActionRun | None:
        """Return a detached view so callers cannot mutate runtime state."""
        with self._lock:
            run = self._runs.get(str(action_id))
            if run is None:
                return None
            cancel_event = threading.Event()
            if run.cancel_event.is_set():
                cancel_event.set()
            return ActionRun(
                action_id=run.action_id,
                action=run.action,
                parameters=_safe_parameters(run.parameters),
                source=run.source,
                status=run.status,
                progress=run.progress,
                message=run.message,
                started_at=run.started_at,
                finished_at=run.finished_at,
                cancel_event=cancel_event,
            )

    def cancellation_event(self, action_id: str | None) -> threading.Event | None:
        if not action_id:
            return None
        with self._lock:
            run = self._runs.get(str(action_id))
            return run.cancel_event if run else None

    def update(self, action_id: str, *, status: str | None = None,
               progress: int | None = None, message: str | None = None) -> None:
        with self._lock:
            run = self._runs.get(str(action_id))
            if not run:
                return
            if status:
                run.status = status
            if progress is not None:
                run.progress = max(0, min(100, int(progress)))
            if message is not None:
                run.message = str(message)[:500]
            self._emit(run)

    def finish(self, action_id: str, *, ok: bool, message: str = "",
               status: str | None = None) -> None:
        with self._lock:
            run = self._runs.get(str(action_id))
            if not run:
                return
            cancelled = run.cancel_event.is_set()
            normalized = str(status or "").strip().casefold()
            if normalized:
                run.status = normalized[:64]
            else:
                run.status = "cancelled" if cancelled else ("succeeded" if ok else "failed")
            run.progress = 100 if ok and not cancelled else run.progress
            run.message = str(message or run.message)[:500]
            run.finished_at = time.time()
            self._emit(run)

    def cancel(self, action_id: str) -> bool:
        with self._lock:
            run = self._runs.get(str(action_id))
            if not run or run.finished_at is not None:
                return False
            run.cancel_event.set()
            run.status = "cancelling"
            run.message = "Cancellation requested"
            self._emit(run)
            return True

    def snapshots(self, include_finished: bool = True) -> list[dict]:
        with self._lock:
            values = list(self._runs.values())
            if not include_finished:
                values = [r for r in values if r.finished_at is None]
            return [r.as_dict() for r in reversed(values)]


runtime = ActionRuntime()

__all__ = ["ActionRun", "ActionRuntime", "runtime"]
