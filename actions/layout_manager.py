"""Named window layouts: capture the current arrangement and restore it later.

The placement primitives already existed — ``place_window`` can put any window
on any monitor in any state — but nothing remembered an arrangement. This turns
"put everything back the way I had it for work" into one command instead of six.

A layout stores matching hints (process name and a title fragment) plus the
geometry, never a window handle, because handles do not survive a restart.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from core.json_store import JsonStore, JsonStoreCorruptError
from core.undo import push_undo
from core.window_manager import (
    WindowInfo,
    find_window,
    list_monitors,
    list_windows,
    monitor_for,
    monitor_of,
    operate,
    place_window,
)

BASE_DIR = Path(__file__).resolve().parent.parent
LAYOUT_FILE = BASE_DIR / "memory" / "layouts.json"

MAX_LAYOUTS = 20
MAX_WINDOWS_PER_LAYOUT = 30
MAX_NAME_CHARS = 40
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")

# Windows that belong to the desktop shell rather than to the user's workspace.
# Capturing them produces a layout that fights the operating system on restore.
_SKIP_PROCESSES = {
    "explorer.exe", "searchhost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "textinputhost.exe", "applicationframehost.exe",
    "systemsettings.exe", "lockapp.exe", "dwm.exe",
}


class LayoutError(RuntimeError):
    """Raised when a layout request is invalid."""


def _clean_name(value: str) -> str:
    name = " ".join(str(value or "").strip().split())[:MAX_NAME_CHARS]
    if not name or not _NAME_RE.fullmatch(name):
        raise LayoutError(
            "Layout names may contain letters, numbers, spaces, dots, hyphens, "
            "and underscores, up to 40 characters."
        )
    return name


def _valid_store(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    layouts = value.get("layouts")
    if not isinstance(layouts, dict) or len(layouts) > MAX_LAYOUTS:
        return False
    for name, layout in layouts.items():
        if not isinstance(name, str) or len(name) > MAX_NAME_CHARS:
            return False
        if not isinstance(layout, dict):
            return False
        windows = layout.get("windows")
        if not isinstance(windows, list) or len(windows) > MAX_WINDOWS_PER_LAYOUT:
            return False
        for item in windows:
            if not isinstance(item, dict):
                return False
            for key in ("process", "title"):
                if not isinstance(item.get(key, ""), str) or len(item.get(key, "")) > 200:
                    return False
            for key in ("monitor", "left", "top", "width", "height"):
                number = item.get(key, 0)
                if isinstance(number, bool) or not isinstance(number, int):
                    return False
                if not -200_000 <= number <= 200_000:
                    return False
            if item.get("state") not in {"normal", "maximized", "fullscreen", "minimized"}:
                return False
    return True


def _store() -> JsonStore[dict]:
    return JsonStore(LAYOUT_FILE, dict, validator=_valid_store)


def _read() -> dict:
    if not LAYOUT_FILE.exists():
        return {"version": 1, "layouts": {}}
    try:
        return _store().read()
    except (JsonStoreCorruptError, OSError, ValueError):
        return {"version": 1, "layouts": {}}


def _write(data: dict) -> None:
    _store().write(data)


def _window_state(window: WindowInfo) -> str:
    if window.minimized:
        return "minimized"
    if window.maximized:
        return "maximized"
    return "normal"


def _capture() -> list[dict]:
    monitors = list_monitors()
    rows = []
    for window in list_windows():
        process = Path(str(window.process or "")).name
        if process.casefold() in _SKIP_PROCESSES:
            continue
        if not str(window.title or "").strip():
            continue
        monitor = monitor_of(window)
        rows.append({
            "process": process[:200],
            "title": str(window.title or "")[:200],
            "monitor": int(monitor.index) if monitor else (monitors[0].index if monitors else 1),
            "left": int(window.left),
            "top": int(window.top),
            "width": int(window.width),
            "height": int(window.height),
            "state": _window_state(window),
        })
        if len(rows) >= MAX_WINDOWS_PER_LAYOUT:
            break
    return rows


def _match_window(row: dict):
    """Find the live window a saved row refers to, preferring the process."""
    for candidate in (row.get("process", ""), row.get("title", "")):
        target = str(candidate or "").strip()
        if not target:
            continue
        window = find_window(target)
        if window is not None:
            return window
    return None


def _apply(rows: list[dict]) -> tuple[int, list[str]]:
    applied = 0
    missing = []
    monitor_count = len(list_monitors())
    for row in rows:
        window = _match_window(row)
        if window is None:
            missing.append(str(row.get("process") or row.get("title") or "unknown"))
            continue
        state = str(row.get("state") or "normal")
        try:
            if state == "minimized":
                operate(window, "minimize")
            else:
                index = int(row.get("monitor") or 1)
                monitor = monitor_for(index) if 1 <= index <= monitor_count else None
                if state in {"maximized", "fullscreen"}:
                    place_window(window, monitor, state, focus=False)
                else:
                    operate(window, "restore")
                    operate(window, "move",
                            int(row["left"]), int(row["top"]),
                            max(200, int(row["width"])), max(150, int(row["height"])))
            applied += 1
        except Exception as exc:
            missing.append(f"{row.get('process') or row.get('title')} ({type(exc).__name__})")
    return applied, missing


def layout_manager(parameters: dict | None = None, player=None) -> str:
    try:
        return _layout_manager(parameters)
    except LayoutError as exc:
        return str(exc)
    except Exception as exc:
        return f"The layout request failed ({type(exc).__name__})."


def _layout_manager(parameters: dict | None = None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list")[:32].strip().casefold().replace(" ", "_")
    data = _read()
    layouts = data.get("layouts", {})

    if action in {"list", "list_layouts", "layouts"}:
        if not layouts:
            return "No window layouts are saved yet. Say 'save this layout as work'."
        lines = []
        for name, layout in sorted(layouts.items()):
            count = len(layout.get("windows", []))
            saved = str(layout.get("saved_at", ""))[:10]
            lines.append(f"- {name}: {count} windows, saved {saved}")
        return "Saved layouts:\n" + "\n".join(lines)

    if action in {"save", "capture", "store"}:
        name = _clean_name(p.get("name") or p.get("layout") or "")
        rows = _capture()
        if not rows:
            return "I could not see any ordinary application windows to save."
        if name not in layouts and len(layouts) >= MAX_LAYOUTS:
            return f"You already have {MAX_LAYOUTS} layouts saved. Delete one first."
        replaced = name in layouts
        previous = layouts.get(name)
        layouts[name] = {
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "windows": rows,
        }
        data["layouts"] = layouts
        _write(data)

        def _undo_save() -> str:
            current = _read()
            if previous is None:
                current.get("layouts", {}).pop(name, None)
            else:
                current.setdefault("layouts", {})[name] = previous
            _write(current)
            return f"Removed the saved layout '{name}'." if previous is None else \
                   f"Restored the previous version of layout '{name}'."

        push_undo(f"saving window layout '{name}'", _undo_save)
        verb = "Updated" if replaced else "Saved"
        return f"{verb} layout '{name}' with {len(rows)} windows."

    if action in {"apply", "restore", "load", "use"}:
        name = _clean_name(p.get("name") or p.get("layout") or "")
        layout = layouts.get(name)
        if layout is None:
            available = ", ".join(sorted(layouts)) or "none"
            return f"I have no layout called '{name}'. Saved layouts: {available}."
        before = _capture()
        applied, missing = _apply(layout.get("windows", []))
        if applied:
            push_undo(f"applying window layout '{name}'", lambda: _restore_snapshot(before))
        if not applied:
            return f"None of the windows in layout '{name}' are open right now."
        if missing:
            return (
                f"Applied layout '{name}' to {applied} windows. "
                f"Not open: {', '.join(missing[:6])}."
            )
        return f"Applied layout '{name}' to {applied} windows."

    if action in {"delete", "remove", "forget"}:
        name = _clean_name(p.get("name") or p.get("layout") or "")
        removed = layouts.pop(name, None)
        if removed is None:
            return f"I have no layout called '{name}'."
        data["layouts"] = layouts
        _write(data)

        def _undo_delete() -> str:
            current = _read()
            current.setdefault("layouts", {})[name] = removed
            _write(current)
            return f"Restored the layout '{name}'."

        push_undo(f"deleting window layout '{name}'", _undo_delete)
        return f"Deleted the layout '{name}'."

    return "Use list, save, apply, or delete for window layouts."


def _restore_snapshot(rows: list[dict]) -> str:
    applied, _ = _apply(rows)
    return f"Restored the previous window arrangement ({applied} windows)."


TOOL = {
    "name": "layout_manager",
    "description": (
        "Saves and restores named desktop window layouts. 'Save this as work' captures "
        "where every open application window currently sits, on which monitor, and "
        "whether it is maximised or fullscreen. 'Set up work' puts the open windows back "
        "into that arrangement. Windows that are not running are reported rather than "
        "launched. Also lists and deletes saved layouts."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["list", "save", "apply", "delete"],
                "maxLength": 16,
                "description": "list | save | apply | delete",
            },
            "name": {
                "type": "STRING",
                "maxLength": 40,
                "description": "Layout name, such as 'work' or 'gaming'.",
            },
        },
        "required": ["action"],
    },
    "handler": layout_manager,
    "category": "desktop",
    "undoable": True,
}
