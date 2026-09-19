"""
Restart an application: close it, then start it again — both halves verified.

Reuses the unified controller for both steps (``core/app_controller.close`` and
``open``), so an app that cannot be closed is never reported as restarted, and
an app that closes but will not come back is reported exactly like that.

An app that was not running at all is *started* instead: "restart Discord" when
Discord is closed is a request for Discord to be running, and refusing it with
"it is not running" would be pedantry. The answer still says which of the two
things actually happened.
"""
from __future__ import annotations

from core import tasks


def restart_app(parameters=None, response=None, player=None,
                session_memory=None, speak=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    app_name = str(params.get("app_name") or params.get("name") or "").strip()
    if not app_name:
        return "Which app should I restart?"

    if player:
        try:
            player.write_log(f"[restart_app] {app_name}")
        except Exception:
            pass
    print(f"[restart_app] '{app_name}'")

    try:
        from core import app_controller as _ac
    except Exception as e:
        print(f"[restart_app] controller unavailable: {e}")
        return "Restarting apps is unavailable (app_controller failed to load)."

    if _ac.is_self_name(app_name):
        return "I will not restart myself — say 'shutdown' if you want me to quit."

    task = tasks.start(tasks.KIND_TASK, f"Restart {app_name}",
                       detail="checking whether it is running")

    # The state decides the order, and a *refusal* from close() stops the
    # restart: only "it was not running" is allowed to skip the close half (in
    # that case starting it is the whole job, and the answer says so).
    running = _ac.is_running(app_name)
    if running is False:
        task.update(detail="it was not running — starting it")
        opened, open_msg = _ac.open(app_name)
        if not opened:
            task.fail(error=open_msg[:160], detail="would not start")
            return (f"{app_name} was not running, and it did not start: "
                    f"{open_msg}")
        task.finish(detail="started (it was not running)")
        return f"{app_name} was not running, so I started it. {open_msg}"

    task.update(detail="closing it first")
    closed, close_msg = _ac.close(app_name)
    if not closed:
        task.fail(error=close_msg[:160], detail="could not close it")
        return f"{close_msg} I did not start a second copy."

    task.update(detail="starting it again")
    opened, open_msg = _ac.open(app_name)
    if not opened:
        task.fail(error=open_msg[:160], detail="closed, but would not start again")
        return f"{close_msg} But it did not come back: {open_msg}"
    task.finish(detail="closed and running again")
    return f"{close_msg} {open_msg}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "restart_app",
    "description": (
        "Restarts an application or game: closes it through the app's own "
        "processes, then starts it again, and verifies both halves. Use for "
        "'restart Discord', 'starte Chrome neu'. If the app was not running it "
        "is started instead, and the answer says so. Never claims a restart "
        "that did not happen."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Natural name of the app to restart ('Discord', 'Chrome')"
            }
        },
        "required": ["app_name"]
    },
    "handler": restart_app,
}
