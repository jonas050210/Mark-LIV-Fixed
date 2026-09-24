"""Named window and multi-monitor control for MARK LIV."""
from __future__ import annotations

from core.window_manager import (
    describe_monitors,
    describe_windows,
    find_window,
    list_monitors,
    list_windows,
    monitor_for,
    move_to_monitor,
    operate,
    snap_window,
)


def _target_label(window) -> str:
    return window.title or window.process or "the selected window"


def window_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "list_windows").strip().casefold().replace(" ", "_")
    target = str(p.get("target") or p.get("app") or "").strip()

    if action in {"list", "list_windows", "windows", "open_apps"}:
        return describe_windows()
    if action in {"monitors", "list_monitors", "displays", "display_info"}:
        return describe_monitors()

    window = find_window(target)
    if window is None:
        return f"I could not find a visible window matching '{target or 'the active window'}'."

    if action in {"minimize", "minimise"}:
        operate(window, "minimize")
        return f"Minimized {_target_label(window)}."
    if action in {"maximize", "maximise"}:
        operate(window, "maximize")
        return f"Maximized {_target_label(window)}."
    if action in {"restore", "unminimize", "unminimise"}:
        operate(window, "restore")
        return f"Restored {_target_label(window)}."
    if action in {"focus", "switch", "activate"}:
        operate(window, "focus")
        return f"Switched to {_target_label(window)}."
    if action in {"close", "quit"}:
        # The action registry parks this operation behind the shared human
        # confirmation gate.  Keeping the actual close here makes the policy
        # impossible to bypass through a second caller.
        operate(window, "close")
        return f"Closed {_target_label(window)}."
    if action in {"move_to_monitor", "move_monitor", "send_to_monitor"}:
        monitor = monitor_for(p.get("monitor", 1))
        move_to_monitor(window, monitor)
        return f"Moved {_target_label(window)} to monitor {monitor.index}."
    if action in {"snap", "tile"}:
        monitor = monitor_for(p.get("monitor", 1))
        side = str(p.get("side") or "left")
        snap_window(window, monitor, side)
        return f"Snapped {_target_label(window)} {side} on monitor {monitor.index}."
    if action in {"move", "resize", "position"}:
        monitor = monitor_for(p.get("monitor", 1)) if p.get("monitor") else None
        left = int(p.get("x", monitor.work_left if monitor else window.left))
        top = int(p.get("y", monitor.work_top if monitor else window.top))
        width = int(p.get("width", window.width))
        height = int(p.get("height", window.height))
        operate(window, "restore")
        operate(window, "move", left, top, max(200, width), max(150, height))
        operate(window, "focus")
        return f"Moved {_target_label(window)} to {left},{top} ({width}x{height})."

    return (
        f"Unknown window action '{action}'. Use list_windows, list_monitors, "
        "focus, minimize, maximize, restore, close, move_to_monitor, snap, or move."
    )


def _close_confirmed(window, label: str) -> str:
    operate(window, "close")
    return f"Closed {label}."


TOOL = {
    "name": "window_manager",
    "description": (
        "Controls a named desktop window and the user's monitors. Use this instead "
        "of a blind hotkey when the user names an app: list open windows, focus, "
        "minimize, maximize, restore, close with confirmation, move an app to a "
        "monitor, snap it left/right/top/bottom, or move and resize it. It can also "
        "report monitor resolution, position, primary status, and refresh rate. "
        "If no target is supplied, use the currently active window."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "list_windows | list_monitors | focus | minimize | maximize | "
                    "restore | close | move_to_monitor | snap | move"
                ),
            },
            "target": {
                "type": "STRING",
                "description": "Application name or part of the window title, such as Chrome or Discord.",
            },
            "monitor": {
                "type": "INTEGER",
                "description": "1-based monitor number.",
            },
            "side": {
                "type": "STRING",
                "description": "For snap: left | right | top | bottom | full.",
            },
            "x": {"type": "INTEGER", "description": "Left desktop coordinate for move."},
            "y": {"type": "INTEGER", "description": "Top desktop coordinate for move."},
            "width": {"type": "INTEGER", "description": "Width in pixels for move."},
            "height": {"type": "INTEGER", "description": "Height in pixels for move."},
        },
        "required": ["action"],
    },
    "handler": window_manager,
    "risk": "close requires confirmation; other window operations are reversible",
    "confirmation_actions": ["close"],
    "undoable": True,
}
