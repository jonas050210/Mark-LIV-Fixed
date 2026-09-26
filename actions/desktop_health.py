"""Read-only desktop diagnostics: what MARK LIV can currently see and control.

When a multi-step desktop command keeps failing, guessing at another retry is
worse than pausing to check whether the environment itself is the problem —
no window backend, an empty application index, or a monitor that was
unplugged. This action answers exactly that question, without moving,
opening, or closing anything, so it is always safe to call first.
"""
from __future__ import annotations

from collections import Counter

from core import shortcut_store
from core.user_paths import desktop_candidates, locations
from core.window_manager import backend_name, list_monitors, list_windows
from memory.config_manager import get_input_device, get_output_device, load_api_keys


def _monitor_line(monitor) -> str:
    primary = ", primary" if monitor.primary else ""
    hz = f", {monitor.refresh_hz} Hz" if monitor.refresh_hz else ""
    return (
        f"  {monitor.index}. {monitor.name} — {monitor.width}x{monitor.height} "
        f"at {monitor.left},{monitor.top}{primary}{hz}"
    )


def desktop_health(parameters: dict | None = None, player=None) -> dict:
    reasons: list[str] = []

    try:
        monitors = list_monitors()
    except Exception as exc:
        monitors = []
        reasons.append(f"monitor detection failed ({type(exc).__name__})")

    try:
        backend = backend_name()
    except Exception as exc:
        backend = "unknown"
        reasons.append(f"window backend lookup failed ({type(exc).__name__})")
    if backend in {"none", "unknown", ""}:
        reasons.append("no window backend is available, so windows cannot be found, moved, or verified")

    try:
        windows = list_windows()
    except Exception as exc:
        windows = []
        reasons.append(f"window enumeration failed ({type(exc).__name__})")

    try:
        from core.app_index import load_index
        app_entries = load_index()
    except Exception as exc:
        app_entries = []
        reasons.append(f"the installed-application index failed to load ({type(exc).__name__})")
    if not app_entries:
        reasons.append("the installed-application index is empty; open_app will not find anything until it is rescanned")

    try:
        shortcuts = shortcut_store.all_shortcuts()
    except Exception as exc:
        shortcuts = {}
        reasons.append(f"saved shortcuts could not be read ({type(exc).__name__})")

    primary = next((m for m in monitors if m.primary), None)
    user_folders = locations()
    desktops = desktop_candidates()
    app_sources = Counter(str(getattr(entry, "source", "") or "unknown") for entry in app_entries)
    configured_input = get_input_device() or "System default"
    configured_output = get_output_device() or "System default"
    config = load_api_keys()
    camera_index = config.get("camera_index", 0)
    camera_ready = False
    try:
        from actions import screen_processor
        camera_ready = bool(screen_processor._CV2 and screen_processor._NUMPY)
    except Exception:
        camera_ready = False

    status = "ready" if not reasons else "degraded"
    desktop_path = user_folders.get("desktop")
    onedrive = "onedrive" in str(desktop_path or "").casefold()

    lines = [
        f"Status: {status}",
        f"Window backend: {backend}",
        f"Monitors: {len(monitors)}" + (f" (primary: monitor {primary.index})" if primary else ""),
    ]
    lines.extend(_monitor_line(m) for m in monitors)
    lines.append(f"Visible windows: {len(windows)}")
    lines.append(
        f"Installed applications indexed: {len(app_entries)}"
        + (" (" + ", ".join(f"{name}: {count}" for name, count in sorted(app_sources.items())) + ")"
           if app_sources else "")
    )
    lines.append(f"Saved shortcuts: {len(shortcuts)}")
    lines.append(f"Desktop path: {desktop_path or 'unknown'}" + (" (OneDrive)" if onedrive else ""))
    lines.append("Desktop scan folders: " + (", ".join(str(path) for path in desktops) or "none"))
    lines.append(f"Audio selection: mic={configured_input}; speakers={configured_output}")
    lines.append(
        f"Camera: {'ready' if camera_ready else 'unavailable'} "
        f"(configured index: {camera_index if isinstance(camera_index, int) else 'auto'})"
    )
    if reasons:
        lines.append("Issues found:")
        lines.extend(f"  - {reason}" for reason in reasons)

    return {
        "ok": True,
        "status": "succeeded",
        "message": "\n".join(lines),
        "data": {
            "health": status,
            "backend": backend,
            "monitor_count": len(monitors),
            "primary_monitor": primary.index if primary else None,
            "visible_window_count": len(windows),
            "indexed_application_count": len(app_entries),
            "application_sources": dict(sorted(app_sources.items())),
            "saved_shortcut_count": len(shortcuts),
            "user_folders": {name: str(path) for name, path in user_folders.items()},
            "desktop_scan_folders": [str(path) for path in desktops],
            "onedrive_desktop": onedrive,
            "audio": {"input": configured_input, "output": configured_output},
            "camera": {"ready": camera_ready, "configured_index": camera_index},
            "issues": reasons,
        },
    }


TOOL = {
    "name": "desktop_health",
    "description": (
        "Read-only diagnostic snapshot of the desktop environment: detected monitors "
        "and which one is primary, the window backend in use, visible windows, indexed "
        "application sources, saved aliases, the real user folders (including OneDrive "
        "Desktop), selected audio devices, and camera readiness. Never moves, opens, "
        "closes, or captures anything. Call this before retrying a desktop command that "
        "keeps failing, or when the user asks what MARK LIV can currently see, so a real "
        "cause can be reported instead of another blind retry."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": desktop_health,
    "category": "desktop",
}
