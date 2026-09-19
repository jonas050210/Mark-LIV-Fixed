"""
Close applications by natural name — "close Spotify" just works.

The counterpart to open_app, through the SAME unified controller
(core/app_controller.py): the app is resolved with the same resolver open
uses, its own processes are terminated (terminate → wait → kill), and a
post-check confirms it is really gone.

What this action NEVER does, by design:

- no Alt+F4 / Ctrl+W / Cmd+Q keystrokes (those hit whatever window happens to
  be focused — the wrong app, a dialog mid-save, anything);
- no global shortcuts or window-manager guessing;
- never closes JARVIS itself, its sibling processes, or system/shell
  processes — those refusals cannot be overridden by phrasing.

An app that is not running gets an honest "not running" instead of a faked
success.
"""
from __future__ import annotations


def close_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = str((parameters or {}).get("app_name") or "").strip()
    if not app_name:
        return "No application name provided."

    if player:
        try:
            player.write_log(f"[close_app] {app_name}")
        except Exception:
            pass

    print(f"[close_app] Closing: '{app_name}'")

    try:
        from core import app_controller as _ac
    except Exception as e:
        print(f"[close_app] controller unavailable: {e}")
        return "Closing apps is unavailable (app_controller failed to load)."

    try:
        ok, message = _ac.close(app_name)
    except Exception as e:
        print(f"[close_app] close failed: {e}")
        return f"Failed to close {app_name}: {type(e).__name__}."
    print(f"[close_app] → ok={ok}")
    return message


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "close_app",
    "description": (
        "Closes an application or game by its natural name ('close Spotify', "
        "'quit Chrome', 'beende Discord'). Uses the app's own processes — "
        "never keystrokes like Alt+F4 and never global shortcuts — and "
        "verifies the app is really gone. Says so honestly when the app is "
        "not running. Will not close JARVIS itself or any system process."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Natural name or alias of the app/game to close ('Spotify', 'Chrome', 'GD')"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": close_app,
}
