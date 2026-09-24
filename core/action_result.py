"""Common result contract for every action invocation.

Handlers in the original project pre-date the action registry and return plain
strings.  Keeping that compatibility at the edge is useful, but internally the
runtime needs to distinguish a successful result from a rejected, timed-out, or
confirmation-pending operation.  This small value object is the single boundary
for that distinction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
            "data": dict(self.data),
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
            return value
        message = str(value or "Done.")
        # Legacy handlers report errors in-band.  Preserve their exact text but
        # give callers a useful status instead of labelling every red error
        # sentence as a successful operation.
        low = message.lstrip().casefold()
        failure_prefixes = (
            "failed", "could not", "error:", "execution error", "access denied",
            "permission denied", "not available", "not found:", "timed out",
            "action failed", "tool '", "unsupported operating system", "not installed",
            "refused", "cannot ",
        )
        if low.startswith(failure_prefixes):
            return cls.failure(action, message)
        return cls.success(action, message)


__all__ = ["ActionResult"]
