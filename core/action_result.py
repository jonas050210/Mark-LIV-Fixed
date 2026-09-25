"""Common result contract for every action invocation.

Handlers in the original project pre-date the action registry and return plain
strings.  Keeping that compatibility at the edge is useful, but internally the
runtime needs to distinguish a successful result from a rejected, timed-out, or
confirmation-pending operation.  This small value object is the single boundary
for that distinction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import re
from typing import Any

_STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ActionResult:
    """A serialisable result returned by the action runtime."""

    action: str
    ok: bool
    status: str
    message: str
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_text(self) -> str:
        """The backwards-compatible text sent to Gemini and the UI."""
        return self.message or ("Done." if self.ok else "The action failed.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "duration_ms": self.duration_ms,
            "data": copy.deepcopy(self.data),
            "error": self.error,
        }

    @classmethod
    def success(cls, action: str, message: str = "Done.", **data: Any) -> "ActionResult":
        return cls(action, True, "succeeded", str(message or "Done."), data=data)

    @classmethod
    def failure(cls, action: str, message: str, *, error: str = "", status: str = "failed") -> "ActionResult":
        return cls(action, False, status, str(message), error=error or str(message))

    @classmethod
    def from_handler(cls, action: str, value: Any) -> "ActionResult":
        if isinstance(value, cls):
            value = value.as_dict()
        if isinstance(value, dict):
            ok = value.get("ok")
            message = value.get("message")
            status = value.get("status", "succeeded" if ok else "failed")
            data = value.get("data", {})
            error = value.get("error", "")
            duration = value.get("duration_ms", 0)
            valid = (
                isinstance(ok, bool)
                and isinstance(message, str)
                and isinstance(status, str)
                and bool(_STATUS_RE.fullmatch(status))
                and isinstance(data, dict)
                and isinstance(error, str)
                and isinstance(duration, int)
                and not isinstance(duration, bool)
                and 0 <= duration <= 86_400_000
            )
            if valid:
                try:
                    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
                    valid = len(encoded.encode("utf-8")) <= 1_000_000
                except (TypeError, ValueError):
                    valid = False
            if not valid:
                return cls.failure(
                    action,
                    "Action returned an invalid result contract.",
                    status="invalid_result",
                )
            return cls(
                action=action,
                ok=ok,
                status=status,
                message=message[:100_000],
                duration_ms=duration,
                data=copy.deepcopy(data),
                error=error[:10_000],
            )
        if value is not None and not isinstance(value, str):
            return cls.failure(
                action,
                "Action returned an unsupported result type.",
                status="invalid_result",
            )
        message = str(value or "Done.")[:100_000]
        # Legacy handlers report errors in-band. Preserve their exact text but
        # classify common rejection forms until each action returns ActionResult.
        low = message.lstrip().casefold()
        failure_prefixes = (
            "failed", "could not", "error:", "execution error", "access denied",
            "permission denied", "not available", "not found", "timed out",
            "action failed", "tool '", "unsupported", "not installed", "refused",
            "cannot ", "can't ", "invalid ", "unknown ", "no file path",
            "no path", "no source", "no destination", "archive rejected",
            "office document rejected", "processing failed", "extract failed",
            "convert failed", "trim failed", "compress failed", "transcription failed",
        )
        if low.startswith(failure_prefixes):
            return cls.failure(action, message)
        return cls.success(action, message)


__all__ = ["ActionResult"]
