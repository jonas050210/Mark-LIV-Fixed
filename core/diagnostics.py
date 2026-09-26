"""Bounded, redacted runtime diagnostics safe to show or export locally."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import re
import threading
import time

_MAX_EVENTS = 500
_MAX_MESSAGE = 1_000
_LOCK = threading.RLock()
_EVENTS: deque["DiagnosticEvent"] = deque(maxlen=_MAX_EVENTS)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)(api[_ -]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)


@dataclass(frozen=True)
class DiagnosticEvent:
    timestamp: float
    subsystem: str
    level: str
    message: str
    exception: str = ""


def redact(value: object) -> str:
    """Return bounded text with common credential forms removed."""
    text = str(value or "").replace("\x00", "")[:_MAX_MESSAGE]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + "=[REDACTED]" if match.lastindex else "[REDACTED]", text)
    return text


def record(subsystem: str, message: object, *, level: str = "warning", exception: BaseException | None = None) -> None:
    event = DiagnosticEvent(
        timestamp=time.time(),
        subsystem=redact(subsystem)[:80] or "application",
        level=level if level in {"debug", "info", "warning", "error"} else "warning",
        message=redact(message),
        exception=type(exception).__name__ if exception is not None else "",
    )
    with _LOCK:
        _EVENTS.append(event)


def snapshot() -> list[dict]:
    """Return a detached oldest-first copy of current diagnostic events."""
    with _LOCK:
        return [asdict(event) for event in _EVENTS]


def export(path: Path) -> Path:
    """Atomically create a redacted report without replacing an existing file."""
    from core.path_policy import atomic_create_text

    destination = Path(path).expanduser()
    atomic_create_text(
        destination,
        json.dumps({"events": snapshot()}, indent=2),
        encoding="utf-8",
    )
    return destination


def clear() -> None:
    with _LOCK:
        _EVENTS.clear()
