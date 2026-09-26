"""Tracked, authenticated dispatch for dashboard-originated registry actions."""
from __future__ import annotations

from typing import Any

from core.action_result import ActionResult
from core.action_runtime import runtime as action_runtime


def run_dashboard_action(
    registry,
    name: str,
    parameters: dict | None,
    *,
    player=None,
    speak=None,
    session_memory=None,
) -> tuple[str, ActionResult]:
    """Execute one already-authenticated dashboard action with live tracking.

    The dashboard server authenticates every control route before its callback
    reaches this function.  Give that trusted local-control fact to the action
    registry through context — never through model/user parameters — and use the
    same action-runtime lifecycle as a voice call. Confirmation-pending actions
    remain open for ``core.confirm`` to finish after the user chooses.
    """
    safe_parameters = parameters if isinstance(parameters, dict) else {}
    action_id = action_runtime.start(name, safe_parameters, source="dashboard")
    context: dict[str, Any] = {
        "player": player,
        "speak": speak,
        "session_memory": session_memory,
        "action_id": action_id,
        "trusted": True,
    }
    try:
        result = registry.execute(name, parameters, context)
    except Exception as exc:
        message = f"Dashboard action failed ({type(exc).__name__})."
        action_runtime.finish(action_id, ok=False, message=message)
        return action_id, ActionResult.failure(name, message, error=type(exc).__name__)

    if result.status != "confirmation_pending":
        run = action_runtime.get(action_id)
        if run is not None and run.finished_at is None:
            action_runtime.finish(
                action_id,
                ok=result.ok,
                message=result.as_text(),
                status=result.status,
            )
    return action_id, result


__all__ = ["run_dashboard_action"]
