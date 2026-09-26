"""Named window and multi-monitor control for MARK LIV."""
from __future__ import annotations

import time

from core import window_events
from core.text_match import partial_ratio
from core.undo import push_undo, refuse
from core.window_manager import (
    backend_name,
    describe_monitors,
    describe_windows,
    find_window,
    find_windows,
    focus_window,
    last_launched_window,
    list_windows,
    monitor_for,
    move_to_monitor,
    operate,
    place_window,
    recent_launch_window_for,
    refresh_window,
    snap_window,
    window_on_monitor,
)

# How often a settling-title retry re-reads the desktop, and how long it keeps
# trying. A browser tab that is still "New Tab" while YouTube loads typically
# lands its real title well inside two seconds.
_SETTLE_POLL_SECONDS = 0.25
_SETTLE_TIMEOUT_SECONDS = 2.5


def _strict_matches(target: str, *, timeout: float | None = None) -> list:
    """Strict (min_score=80) matches, re-read briefly while a title settles.

    Uses this module's ``find_windows`` so behaviour stays identical whether it
    is the real desktop or a test double answering.
    """
    budget = _SETTLE_TIMEOUT_SECONDS if timeout is None else max(0.0, float(timeout))
    deadline = time.monotonic() + budget
    while True:
        matches = find_windows(target, min_score=80)
        if matches or time.monotonic() >= deadline:
            return matches
        # Wake as soon as the desktop says a window changed (a title landing
        # is exactly such an event) instead of sleeping the whole interval.
        window_events.wait_for_change(_SETTLE_POLL_SECONDS)


def _normalised(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in str(value or "")).strip()


def _site_token(url: str) -> str:
    """The name a user would use for a site URL, e.g. 'youtube' for youtube.com."""
    try:
        from urllib.parse import urlsplit

        host = urlsplit(str(url or "")).hostname or ""
    except Exception:
        return ""
    parts = [part for part in host.casefold().split(".") if part not in ("www", "com", "de", "net", "org")]
    return parts[0] if parts else ""


def _not_found_message(target: str) -> str:
    """Tell the model what IS open, so it can correct itself in one turn.

    "Could not find a window matching 'YouTube'" used to end the exchange; the
    user said "it is open, look again" and the assistant had nothing new to
    look at. Listing the windows that were actually seen turns the same
    failure into a self-service answer: the model can pick the right title and
    retry, or report honestly what the desktop shows.
    """
    label = target or "the active window"
    try:
        windows = list_windows()
    except Exception:
        windows = []
    if not windows:
        return (
            f"I could not find a visible window matching '{label}', and no open "
            f"windows could be read at all (window backend: '{backend_name()}'). "
            "If windows are open, this desktop session may not expose them to me."
        )
    rows = []
    for i, window in enumerate(windows[:12], 1):
        process = f" [{window.process}]" if window.process else ""
        rows.append(f"{i}. {window.title}{process}")
    more = "" if len(windows) <= 12 else f" …and {len(windows) - 12} more"
    return (
        f"I could not find a visible window matching '{label}'. "
        f"These windows are currently visible: " + "; ".join(rows) + more +
        ". Retry with the exact title or process of the window the user means."
    )


def _named_window_candidates(target: str, *, wait: float | None = None,
                             launch_fallback: bool = True) -> list:
    """Resolve a spoken target to windows, surviving the two common races.

    1. A just-opened page has not settled its title yet ("New Tab" while
       YouTube loads) — answered by re-scoring for a few seconds.
    2. The page lives in a tab whose title never contains the spoken name —
       answered by the memory of the window this assistant launched (when the
       remembered name agrees with the target), or by the last URL handed to
       the browser when its site name agrees with the target.

    Strictness is preserved: the launch-memory fallback only fires when the
    target clearly names what was launched, and the URL fallback only when a
    single browser window is in play or the remembered launch was a browser.
    """
    matches = find_windows(target, min_score=80)
    if matches:
        return matches
    budget = _SETTLE_TIMEOUT_SECONDS if wait is None else wait
    if budget > 0:
        matches = _strict_matches(target, timeout=budget)
        if matches:
            return matches
    if launch_fallback:
        remembered = recent_launch_window_for(target)
        if remembered is not None:
            live = refresh_window(remembered)
            if live is not None:
                return [live]
        # The site was just opened in the browser (handoff record), but its
        # tab title does not carry the name the user said.
        try:
            from core import browser_handoff

            last_url = browser_handoff.peek()
        except Exception:
            last_url = ""
        site = _site_token(last_url)
        if site and site in _normalised(target):
            candidates = [w for w in find_windows("browser", min_score=80)]
            if remembered is not None and any(
                int(w.handle) == int(remembered.handle) for w in candidates
            ):
                live = refresh_window(remembered)
                if live is not None:
                    return [live]
            if len(candidates) == 1:
                return candidates
    return []


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
        # Wait for a settling title, but never guess: close/minimize_all act on
        # every match, so only a confirmed title/process match is acceptable.
        matches = _strict_matches(target)
        if not matches:
            return _not_found_message(target)
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
        keep = _strict_matches(target)
        if not keep:
            return _not_found_message(target)
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
    # _named_window_candidates adds only two tightly-scoped escapes to that
    # bar: a settling title gets a few seconds to load, and the window this
    # assistant itself just launched can stand in when the user names it.
    # With no target, retain the intentional "active window" behaviour.
    if target:
        named = _named_window_candidates(target)
        window = named[0] if named else None
    else:
        window = find_window()
    if window is None:
        return _not_found_message(target)

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
        "The target may be an application name or part of a window title; the generic "
        "word 'browser' matches any browser window (Chrome, Edge, Firefox, …). A window "
        "the user just asked to open is remembered, so a following 'make it fullscreen' "
        "finds it even while its title is still loading. If no window matches, the "
        "currently visible windows are listed back — retry with the exact title. "
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
                    "display), 'left'/'right' (by physical position), or 'monitor 2'. "
                    "German is accepted too: 'ersten'/'zweiten'/'dritten' (or any "
                    "case form), 'Hauptmonitor', 'links'/'rechts', 'Bildschirm 2'."
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
