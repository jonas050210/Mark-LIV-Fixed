"""
Multi-step agent — "open Spotify and then search for jazz" as one command.

Thin voice-facing wrapper over core/planner.py: the goal is decomposed into
tool steps (rules first, optional local model as fallback), each step runs
through the shared tool dispatcher, and each result is verified before the
next step runs. The first unverified step stops the plan with an honest
message — later steps are never built on a success that never happened.

Single-step goals run directly with no planning ceremony, exactly as if the
underlying tool had been called.
"""
from __future__ import annotations


def agent_task(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
    dispatcher=None,
) -> str:
    params = parameters or {}
    goal = str(params.get("goal") or params.get("task") or params.get("command") or "").strip()
    if not goal:
        return "No goal provided."

    if player:
        try:
            player.write_log(f"[agent_task] {goal[:100]}")
        except Exception:
            pass

    print(f"[agent_task] Goal: '{goal[:120]}'")

    if dispatcher is None:
        try:
            from core.dispatcher import get_dispatcher

            dispatcher = get_dispatcher()
        except Exception:
            dispatcher = None
    if dispatcher is None or not dispatcher.is_bound:
        return ("The action planner is not connected right now — "
                "try the steps as single commands instead.")

    try:
        from core import planner as _planner
    except Exception as e:
        print(f"[agent_task] planner unavailable: {e}")
        return "Multi-step planning is unavailable (planner failed to load)."

    ctx = {}
    if player is not None:
        ctx["player"] = player
    if session_memory is not None:
        ctx["session_memory"] = session_memory
    # The dispatcher is passed through so nested agent calls keep working.
    ctx["dispatcher"] = dispatcher

    try:
        return _planner.run_plan(goal, dispatcher, ctx)
    except Exception as e:
        print(f"[agent_task] plan failed: {e}")
        return f"The multi-step task failed ({type(e).__name__})."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "agent_task",
    "description": (
        "Runs a MULTI-STEP goal as one command ('open Spotify and search for "
        "jazz', 'schließe Chrome und öffne dann Discord'). Each step is "
        "executed in order and verified before the next runs; the plan stops "
        "at the first failed step and says so honestly. Prefer a direct tool "
        "whenever one can do the job — use this only for genuine sequences."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The multi-step goal in the user's own words"
            }
        },
        "required": [
            "goal"
        ]
    },
    "handler": agent_task,
}
