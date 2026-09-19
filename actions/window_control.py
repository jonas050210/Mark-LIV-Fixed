"""
Window control by name: list, focus, minimize, maximize, restore.

Windows are matched by app name or by title, through
``core/app_controller.window_action`` — the same implementation the
``computer_control`` focus action uses, so there is one answer to "which window
does 'Chrome' mean?".

What it deliberately does *not* do: close windows. Everything here is
reversible and nothing sends a keystroke to whatever happens to be focused
(Alt+F4 into an unsaved document is exactly the accident this avoids) — apps are
closed by name with ``close_app``/``restart_app`` instead.

When a system cannot report window state (no pygetwindow, no wmctrl/xdotool),
the answer says which piece is missing instead of pretending the window moved.
"""
from __future__ import annotations


def window_control(parameters=None, response=None, player=None,
                   session_memory=None) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "focus").strip().lower()
    app_name = str(params.get("app_name") or params.get("name") or "").strip()
    title = str(params.get("title") or "").strip()
    hint = str(params.get("query") or "").strip()
    if hint and not title and not app_name:
        title = hint
    try:
        limit = int(params.get("limit", 40))
    except (TypeError, ValueError):
        limit = 40

    if player:
        try:
            player.write_log(f"[window_control] {action} {title or app_name}".strip())
        except Exception:
            pass
    print(f"[window_control] {action} '{title or app_name}'")

    try:
        from core import app_controller as _ac
    except Exception as e:
        print(f"[window_control] controller unavailable: {e}")
        return "Window control is unavailable (app_controller failed to load)."

    try:
        geometry = {key: params[key] for key in ("monitor", "width", "height", "topmost") if key in params}
        ok, message = _ac.window_action(action, name=app_name, title=title,
                                        limit=limit, **geometry)
    except Exception as e:
        return f"Window control failed ({type(e).__name__}: {e})."
    return message


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "window_control",
    "description": (
        "Lists, focuses, minimizes, maximizes or restores windows by app name "
        "or window title ('bring Chrome to the front', 'minimize Spotify', "
        "'which windows are open?'). Works on named windows only — never "
        "keystrokes into whatever happens to be focused — and never closes "
        "anything: use close_app for that. Says honestly when the system cannot "
        "report window state."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list | focus | minimize | maximize | restore | snap_left | snap_right | move_monitor | resize | always_on_top"
            },
            "app_name": {
                "type": "STRING",
                "description": "App whose window is meant ('Chrome', 'Spotify')"
            },
            "title": {
                "type": "STRING",
                "description": "Exact/partial window title, when it is not just the app name"
            },
            "monitor": {"type": "INTEGER", "description": "Target monitor, starting at 1"},
            "width": {"type": "INTEGER", "description": "Requested width for resize"},
            "height": {"type": "INTEGER", "description": "Requested height for resize"},
            "topmost": {"type": "BOOLEAN", "description": "Enable always-on-top; false removes it"},
            "limit": {
                "type": "INTEGER",
                "description": "Maximum titles to list for action=list (default 40)"
            }
        },
        "required": []
    },
    "handler": window_control,
}
