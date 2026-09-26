"""Named window and multi-monitor control for MARK LIV."""
from __future__ import annotations

import time

from core.undo import push_undo, refuse
from core.window_manager import (
    describe_monitors,
    describe_windows,
    find_window,
    find_windows,
    focus_window,
    list_windows,
    monitor_for,
    move_to_monitor,
    operate,
    place_window,
    refresh_window,
    snap_window,
    window_on_monitor,
)


def _target_label(window) -> str:
    return window.title or window.process or "the selected window"


def _window_state(window) -> tuple[int, int, int, int, bool, bool]:
    return (
        int(window.left), int(window.top), int(window.width), int(window.height),
        bool(window.minimized), bool(window.maximized),
    )


def _restore_window_state(window, state) -> str:
    left, top, width, height, was_minimized, was_maximized = state
    operate(window, "restore")
    operate(window, "move", left, top, max(1, width), max(1, height))
    if was_maximized:
        operate(window, "maximize")
    elif was_minimized:
        operate(window, "minimize")
    else:
        operate(window, "focus")
    return "The previous window position and state were restored."


def _remember_window(window, label: str, state) -> None:
    push_undo(f"window layout of {label}", lambda: _restore_window_state(window, state))


def _restore_minimized_windows(snapshots) -> str:
    """Undo a tidy-desktop action without overwriting later user changes.

    A minimised window could have been restored, moved, or closed after MARK
    LIV tidied the desktop. Check every affected handle first, so an undo never
    partially restores old geometry over a window the user has since changed.
    """
    live_windows = []
    for original, state in snapshots:
        live = refresh_window(original)
        if live is None:
            refuse(f"'{_target_label(original)}' is no longer open")
        if not live.minimized:
            refuse(f"'{_target_label(live)}' was changed after the tidy action")
        live_windows.append((live, state))

    restored = 0
    for window, state in live_windows:
        left, top, width, height, was_minimized, was_maximized = state
        try:
            operate(window, "restore")
            operate(window, "move", left, top, max(1, width), max(1, height))
            if was_maximized:
                operate(window, "maximize")
            elif was_minimized:
                operate(window, "minimize")
            restored += 1
        except Exception:
            continue
    return f"Restored {restored} window(s) that were minimised by the tidy action."


def _wait_for_closed(handles: set[int], timeout: float = 3.0) -> set[int]:
    """Return handles still visible after a bounded graceful-close wait."""
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = set(handles)
    while remaining and time.monotonic() < deadline:
        try:
            visible = {int(window.handle) for window in list_windows()}
        except Exception:
            break
        remaining &= visible
        if remaining:
            time.sleep(0.1)
    return remaining


def window_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list_windows")[:32].strip().casefold().replace(" ", "_")
    target = str(p.get("target") or p.get("app") or "")[:200].strip()

    if action in {"list", "list_windows", "windows", "open_apps"}:
        return describe_windows()
    if action in {"monitors", "list_monitors", "displays", "display_info"}:
        return describe_monitors()
    if action in {"list_app_windows", "app_windows", "close_all", "minimize_all"}:
        if not target:
            return f"A target application is required for {action}."
        matches = find_windows(target, min_score=80)
        if not matches:
            return f"I could not find a visible window matching '{target}'."
        if action in {"list_app_windows", "app_windows"}:
            rows = [
                f"{index}. {window.title} [{window.process}] HWND {window.handle}"
                for index, window in enumerate(matches[:40], 1)
            ]
            return f"Windows matching '{target}':\n" + "\n".join(rows)
        operation = "close" if action == "close_all" else "minimize"
        requested = []
        for item in matches:
            try:
                operate(item, operation)
                requested.append(item)
            except Exception:
                continue
        if operation == "close":
            remaining = _wait_for_closed({int(item.handle) for item in requested})
            closed = len(requested) - len(remaining)
            if remaining:
                return (
                    f"Closed {closed} of {len(matches)} window(s) matching '{target}'; "
                    f"{len(remaining)} still open, possibly because the app is showing a save prompt."
                )
            return f"Closed {closed} of {len(matches)} window(s) matching '{target}'."
        return f"Minimized {len(requested)} of {len(matches)} window(s) matching '{target}'."

    if action in {"minimize_others", "tidy_desktop", "clear_desktop"}:
        if not target:
            return "Name the application or window to keep visible when tidying the desktop."
        keep = find_windows(target, min_score=80)
        if not keep:
            return f"I could not find a visible window matching '{target}' to keep open."
        keep_handles = {int(window.handle) for window in keep}
        snapshots = []
        failed = 0
        for item in list_windows():
            if int(item.handle) in keep_handles or item.minimized:
                continue
            before = _window_state(item)
            try:
                operate(item, "minimize")
                snapshots.append((item, before))
            except Exception:
                failed += 1
        label = _target_label(keep[0])
        if not snapshots:
            return f"{label} is already the only non-minimized matching desktop window."
        push_undo(
            f"tidied desktop around {label}",
            lambda rows=tuple(snapshots): _restore_minimized_windows(rows),
        )
        try:
            operate(keep[0], "focus")
        except Exception:
            pass
        result = (
            f"Kept {label} visible and minimized {len(snapshots)} other window(s). "
            "Say undo to restore them."
        )
        if failed:
            result += f" I could not minimize {failed} window(s)."
        return result

    # Any named window operation must clear the same strict match bar as
    # close/quit. A weak fuzzy match is not an acceptable target for moving,
    # minimising, or focusing somebody's windows either: acting on Visual
    # Studio Code because the user said Chrome is still the wrong operation.
    # With no target, retain the intentional "active window" behaviour.
    if target:
        strict_matches = find_windows(target, min_score=80)
        window = strict_matches[0] if strict_matches else None
    else:
        window = find_window()
    if window is None:
        return f"I could not find a visible window matching '{target or 'the active window'}'."

    label = _target_label(window)
    before = _window_state(window)
    if action in {"minimize", "minimise"}:
        operate(window, "minimize")
        _remember_window(window, label, before)
        return f"Minimized {label}."
    if action in {"maximize", "maximise"}:
        operate(window, "maximize")
        _remember_window(window, label, before)
        return f"Maximized {label}."
    if action in {"restore", "unminimize", "unminimise"}:
        operate(window, "restore")
        _remember_window(window, label, before)
        return f"Restored {label}."
    if action in {"focus", "switch", "activate"}:
        if not focus_window(window):
            return (
                f"Could not verify focus for {_target_label(window)} after asking it to switch."
            )
        return f"Switched to {_target_label(window)}."
    if action in {"close", "quit"}:
        # Closing a named window is deterministic and intentionally immediate.
        # The application may still show its own native save prompt when it
        # genuinely owns unsaved work; MARK LIV does not add another prompt.
        operate(window, "close")
        if _wait_for_closed({int(window.handle)}):
            return (
                f"I asked {_target_label(window)} to close, but its window is still open. "
                "The application may be showing its own save prompt."
            )
        return f"Closed {_target_label(window)}."
    if action in {"fullscreen", "fulscreen", "full_screen", "full", "maximize", "maximise"}:
        # Spoken "fullscreen" deliberately means the native maximise button:
        # it fills the usable desktop but keeps the Windows taskbar visible.
        # It never sends F11 or a focus-dependent hotkey.
        monitor = monitor_for(p.get("monitor")) if p.get("monitor") else None
        placed = place_window(window, monitor, "maximized")
        _remember_window(window, label, before)
        if monitor is not None and not window_on_monitor(placed, monitor):
            return f"I maximized {label}, but could not verify monitor {monitor.index}."
        where = f" on monitor {monitor.index}" if monitor is not None else ""
        return f"{label} is now maximized{where}; the taskbar remains available."
    if action in {"move_to_monitor", "move_monitor", "send_to_monitor"}:
        monitor = monitor_for(p.get("monitor", 1))
        move_to_monitor(window, monitor)
        _remember_window(window, label, before)
        # Report the verified result rather than assuming the move landed.
        from core.window_manager import refresh_window
        moved = refresh_window(window) or window
        if not window_on_monitor(moved, monitor):
            return f"I asked to move {label} to monitor {monitor.index}, but could not verify it."
        return f"Moved {label} to monitor {monitor.index}."
    if action in {"snap", "tile"}:
        monitor = monitor_for(p.get("monitor", 1))
        side = str(p.get("side") or "left")
        snap_window(window, monitor, side)
        _remember_window(window, label, before)
        return f"Snapped {label} {side} on monitor {monitor.index}."
    if action in {"move", "resize", "position"}:
        monitor = monitor_for(p.get("monitor", 1)) if p.get("monitor") else None
        left = int(p.get("x", monitor.work_left if monitor else window.left))
        top = int(p.get("y", monitor.work_top if monitor else window.top))
        width = int(p.get("width", window.width))
        height = int(p.get("height", window.height))
        operate(window, "restore")
        operate(window, "move", left, top, max(200, width), max(150, height))
        operate(window, "focus")
        _remember_window(window, label, before)
        return f"Moved {label} to {left},{top} ({width}x{height})."

    return (
        f"Unknown window action '{action}'. Use list_windows, list_monitors, focus, "
        "minimize, minimize_others, maximize, restore, close, move_to_monitor, snap, or move."
    )


def _close_confirmed(window, label: str) -> str:
    operate(window, "close")
    return f"Closed {label}."


TOOL = {
    "name": "window_manager",
    "description": (
        "Controls a named desktop window and the user's monitors. Use this instead "
        "of a blind hotkey when the user names an app: list open windows, focus, "
        "list all windows of one app, minimize or close one/all, keep a named app visible "
        "while minimizing other windows (reversible with undo), maximize, or fullscreen "
        "(which deliberately means native maximize with the taskbar still visible, never F11), "
        "restore, move an app to a "
        "monitor, snap it left/right/top/bottom, or move and resize it. It can also "
        "report monitor resolution, position, primary status, and refresh rate. "
        "If no target is supplied, use the currently active window."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["list_windows", "list_monitors", "list_app_windows", "focus", "minimize", "minimize_all", "minimize_others", "maximize", "fullscreen", "restore", "close", "close_all", "move_to_monitor", "snap", "move"],
                "maxLength": 32,
                "description": (
                    "list_windows | list_monitors | list_app_windows | focus | minimize | "
                    "minimize_all | minimize_others (requires target) | maximize | fullscreen | "
                    "restore | close | close_all | move_to_monitor | snap | move"
                ),
            },
            "target": {
                "type": "STRING",
                "maxLength": 500,
                "description": "Application name or part of the window title, such as Chrome or Discord. Required for minimize_others so MARK LIV knows what to keep visible.",
            },
            "monitor": {
                "type": "STRING",
                "maxLength": 40,
                "description": (
                    "Which monitor: a 1-based number ('1', '2'), 'primary'/'main' "
                    "(the Windows primary display), 'secondary'/'second' (the other "
                    "display), 'left'/'right' (by physical position), or 'monitor 2'."
                ),
            },
            "side": {
                "type": "STRING",
                "enum": ["left", "right", "top", "bottom", "full"],
                "maxLength": 10,
                "description": "For snap: left | right | top | bottom | full.",
            },
            "x": {"type": "INTEGER", "minimum": -100000, "maximum": 100000, "description": "Left desktop coordinate for move."},
            "y": {"type": "INTEGER", "minimum": -100000, "maximum": 100000, "description": "Top desktop coordinate for move."},
            "width": {"type": "INTEGER", "minimum": 200, "maximum": 100000, "description": "Width in pixels for move."},
            "height": {"type": "INTEGER", "minimum": 150, "maximum": 100000, "description": "Height in pixels for move."},
        },
        "required": ["action"],
    },
    "handler": window_manager,
    "risk": "medium",
    "undoable": True,
}
