"""Verification layer: decide whether a tool call *actually* worked.

A tool returning text is not the same as a tool succeeding — launchers report
"started" while the process died a second later, writers report paths that do
not exist, searches report failure one sentence in. The multi-step agent
(:mod:`core.planner`) asks :func:`verify` after every step and stops the plan
— honestly — on the first unverified one, instead of building later steps on
a success that never happened.

Two verification strengths:

- *independent re-check* (strong): ask the world again — is the process
  running, does the file exist, is the setting really set — via a second tool
  or a read-only probe. Used where a cheap ground truth exists.
- *result-text analysis* (weak): the tool's own words matched against
  failure phrasing. Better than nothing, but never trusted alone for
  state-changing tools when a strong check exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Verification:
    """Outcome of verifying one tool call."""

    ok: bool
    detail: str = ""
    method: str = "none"  # "recheck" | "text" | "none"
    evidence: dict[str, Any] = field(default_factory=dict)


# ── generic result-text analysis ─────────────────────────────────────────────


#: Phrases (lowercased substring match) by which tools report their own
#: failure. Sources: the actual error strings used across actions/*.py.
_FAILURE_PHRASES: tuple[str, ...] = (
    "failed",
    "failure",
    "error",
    "could not",
    "couldn't",
    "canceled",
    "cancelled",
    "timed out",
    "timeout",
    "not found",
    "no such",
    "does not exist",
    "doesn't exist",
    "unable to",
    "refused",
    "denied",
    "permission",
    "unauthorized",
    "forbidden",
    "unknown tool",
    "unknown action",
    "unsupported",
    "not supported",
    "not available",
    "no results",
    "nothing found",
    "i don't know",
    "i do not know",
)

#: Phrases that REDEEM a result even when a failure phrase matched — e.g. a
#: search that found nothing for one spelling but something for another, or an
#: honest "permission denied, did not change anything" (a correct refusal is a
#: successful no-op, not a failure to build on — but the plan must still stop
#: before acting on the refused thing; callers treat ok=False + refused=True).
_REFUSAL_PHRASES: tuple[str, ...] = (
    "did you mean",
    "showing instead",
    "results for",
    "found ",
)


def is_failure_text(result: str) -> bool:
    """Heuristic: does the tool's own reply read like a failure?"""
    low = (result or "").lower()
    if not low.strip():
        return True
    if any(p in low for p in _FAILURE_PHRASES):
        if any(p in low for p in _REFUSAL_PHRASES):
            return False
        return True
    return False


# ── per-tool strong re-checks ────────────────────────────────────────────────


def _recheck_open_app(tool: str, params: dict, dispatcher, result: str) -> Verification | None:
    from core import app_controller as _ac

    name = str(params.get("app_name") or params.get("name") or "").strip()
    if not name:
        return None
    try:
        running = _ac.is_running(name)
    except Exception:
        return None
    if running is None:
        return None  # no process access: unverifiable, degrade to text
    if running:
        return Verification(True, f"'{name}' is running.", method="recheck",
                            evidence={"running": True})
    # One grace wait: freshly launched processes can take a moment to appear.
    import time as _t

    _t.sleep(1.5)
    try:
        running = _ac.is_running(name)
    except Exception:
        return None
    if running is None:
        return None
    if running:
        return Verification(True, f"'{name}' is running.", method="recheck",
                            evidence={"running": True, "after_grace": True})
    return Verification(False, f"'{name}' is not running after launch.",
                        method="recheck", evidence={"running": False})


def _recheck_close_app(tool: str, params: dict, dispatcher, result: str) -> Verification | None:
    from core import app_controller as _ac

    name = str(params.get("app_name") or params.get("name") or "").strip()
    if not name:
        return None
    try:
        running = _ac.is_running(name)
    except Exception:
        return None
    if running is None:
        return None  # no process access: unverifiable, degrade to text
    if not running:
        return Verification(True, f"'{name}' is no longer running.",
                            method="recheck", evidence={"running": False})
    return Verification(False, f"'{name}' is still running.", method="recheck",
                        evidence={"running": True})


def _recheck_file_op(tool: str, params: dict, dispatcher, result: str) -> Verification | None:
    import os as _os

    path = str(params.get("path") or params.get("file_path") or params.get("destination") or "").strip()
    if not path:
        return None
    exists = _os.path.exists(path)
    action = str(params.get("action", "")).lower()
    if tool in ("file_processor",):
        # read/analyse tools: the source must exist (it was read).
        if exists:
            return Verification(True, f"'{path}' exists.", method="recheck")
        return Verification(False, f"'{path}' does not exist.", method="recheck")
    # file_controller: verify by action direction.
    if action in ("delete", "remove"):
        ok = not exists
        return Verification(ok, f"'{path}' {'is gone' if ok else 'still exists'}.",
                            method="recheck")
    if action in ("create", "write", "copy", "move", "rename", "mkdir"):
        target = str(params.get("destination") or params.get("to") or path)
        ok = _os.path.exists(target)
        return Verification(ok, f"'{target}' {'exists' if ok else 'is missing'}.",
                            method="recheck")
    return None


def _recheck_timer(tool: str, params: dict, dispatcher, result: str) -> Verification | None:
    if str(params.get("action", "start")).lower() != "start":
        return None
    low = (result or "").lower()
    if "timer set" in low or "timer '" in low or "timer'" in low:
        return Verification(True, "Timer acknowledged.", method="text")
    return None


def _recheck_uninstall_app(tool: str, params: dict, dispatcher,
                           result: str) -> Verification | None:
    """Removal is verified the only way it can be: the resolver no longer knows
    the app. An uninstaller's exit code proves nothing here."""
    from core import app_controller as _ac
    from core import app_finder as _af

    name = str(params.get("app_name") or params.get("name") or "").strip()
    if not name:
        return None
    try:
        if _af.resolve(name) is None:
            return Verification(True, f"'{name}' is no longer installed.",
                                method="recheck", evidence={"installed": False})
    except Exception:
        return None
    running = None
    try:
        running = _ac.is_running(name)
    except Exception:
        running = None
    if running is True:
        detail = f"'{name}' is still installed and still running."
    else:
        detail = f"'{name}' is still installed."
    return Verification(False, detail, method="recheck",
                        evidence={"installed": True, "running": running})


def _recheck_restart_app(tool: str, params: dict, dispatcher,
                         result: str) -> Verification | None:
    from core import app_controller as _ac

    name = str(params.get("app_name") or params.get("name") or "").strip()
    if not name:
        return None
    try:
        running = _ac.is_running(name)
    except Exception:
        return None
    if running is None:
        return None
    return Verification(running, f"'{name}' is {'running' if running else 'not running'}.",
                        method="recheck", evidence={"running": running})


def _recheck_window_control(tool: str, params: dict, dispatcher,
                            result: str) -> Verification | None:
    """Only `list` has a cheap ground truth; the rest degrade to text."""
    if str(params.get("action") or "focus").strip().lower() not in ("list", "show"):
        return None
    from core import app_controller as _ac

    try:
        titles, _why = _ac.list_windows(limit=40)
    except Exception:
        return None
    if titles is None:
        return None
    if titles:
        return Verification(True, f"{len(titles)} titled window(s) are open.",
                            method="recheck", evidence={"windows": len(titles)})
    return Verification(False, "no titled windows are open.", method="recheck",
                        evidence={"windows": 0})


#: tool name → strong re-check. Missing tools fall back to text analysis.
_RECHECKERS = {
    "open_app": _recheck_open_app,
    "close_app": _recheck_close_app,
    "uninstall_app": _recheck_uninstall_app,
    "restart_app": _recheck_restart_app,
    "window_control": _recheck_window_control,
    "file_controller": _recheck_file_op,
    "file_processor": _recheck_file_op,
    "timer": _recheck_timer,
}


# ── entry point ──────────────────────────────────────────────────────────────


def verify(
    tool: str,
    params: dict | None,
    result: str,
    dispatcher=None,
) -> Verification:
    """Verify one finished tool call.

    Runs the strong re-check when one exists for `tool`, then falls back to
    result-text analysis. A strong re-check that cannot run (missing ground
    truth, probe error) degrades to text analysis rather than failing — an
    unverifiable success is reported as success-with-weak-evidence, never as
    a false failure.
    """
    params = params or {}
    result = result if isinstance(result, str) else str(result)

    checker = _RECHECKERS.get((tool or "").strip())
    if checker is not None:
        try:
            strong = checker(tool, params, dispatcher, result)
        except Exception as e:  # noqa: BLE001 — probes must never break plans
            strong = None
        if strong is not None:
            return strong

    if is_failure_text(result):
        return Verification(False, "Tool reported failure.", method="text")
    detail = (result.strip().splitlines() or [""])[0][:200]
    return Verification(True, detail or "ok", method="text")
