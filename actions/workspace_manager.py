"""Saved app workspaces: a named, repeatable version of an app sequence.

A window layout remembers where already-open windows sit.  A workspace remembers
which applications belong to an activity (school, coding, gaming) and their
requested monitor/state, then opens/verifies them one by one through the same
safe app-sequence pipeline used for a spoken multi-app command.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from actions.app_sequence import app_sequence
from core.json_store import JsonStore, JsonStoreCorruptError
from core.undo import push_undo

BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACES_FILE = BASE_DIR / "memory" / "workspaces.json"

MAX_WORKSPACES = 15
MAX_STEPS = 8
MAX_NAME_CHARS = 40
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
_ALLOWED_STATES = {
    "normal", "maximized", "fullscreen", "minimized",
    "left", "right", "top", "bottom",
}


class WorkspaceError(ValueError):
    """A user-correctable workspace validation failure."""


def _clean_name(value) -> str:
    name = " ".join(str(value or "").strip().split())[:MAX_NAME_CHARS]
    if not name or not _NAME_RE.fullmatch(name):
        raise WorkspaceError(
            "Workspace names may contain letters, numbers, spaces, dots, hyphens, and underscores."
        )
    return name


def _clean_steps(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise WorkspaceError("Provide at least one application step for the workspace.")
    if len(raw) > MAX_STEPS:
        raise WorkspaceError(f"A workspace can contain at most {MAX_STEPS} applications.")
    cleaned = []
    for number, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise WorkspaceError(f"Workspace step {number} must be an object.")
        name = str(item.get("app_name") or "").strip()
        if not name or len(name) > 160 or any(ord(char) < 32 for char in name):
            raise WorkspaceError(f"Workspace step {number} needs a valid app_name.")
        row = {"app_name": name}
        monitor = item.get("monitor")
        if monitor not in (None, ""):
            if isinstance(monitor, bool) or not isinstance(monitor, (int, str)):
                raise WorkspaceError(f"Workspace step {number} has an invalid monitor.")
            if isinstance(monitor, str) and (len(monitor) > 40 or any(ord(char) < 32 for char in monitor)):
                raise WorkspaceError(f"Workspace step {number} has an invalid monitor.")
            row["monitor"] = monitor
        state = str(item.get("state") or "").strip().casefold()
        if state in {"fullscreen", "fulscreen", "full_screen", "full screen", "full"}:
            state = "maximized"
        if state:
            if state not in _ALLOWED_STATES:
                raise WorkspaceError(f"Workspace step {number} has an invalid window state.")
            row["state"] = state
        if item.get("foreground") is not None:
            if not isinstance(item["foreground"], bool):
                raise WorkspaceError(f"Workspace step {number} foreground must be true or false.")
            row["foreground"] = item["foreground"]
        cleaned.append(row)
    return cleaned


def _valid_store(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    workspaces = value.get("workspaces")
    if not isinstance(workspaces, dict) or len(workspaces) > MAX_WORKSPACES:
        return False
    try:
        for name, workspace in workspaces.items():
            if _clean_name(name) != name or not isinstance(workspace, dict):
                return False
            if not isinstance(workspace.get("saved_at"), str) or len(workspace["saved_at"]) > 64:
                return False
            if _clean_steps(workspace.get("steps")) != workspace.get("steps"):
                return False
    except (TypeError, ValueError, WorkspaceError):
        return False
    return True


def _store() -> JsonStore[dict]:
    return JsonStore(WORKSPACES_FILE, dict, validator=_valid_store, private=True)


def _read() -> dict:
    if not WORKSPACES_FILE.exists():
        return {"version": 1, "workspaces": {}}
    try:
        value = _store().read()
    except (JsonStoreCorruptError, OSError, ValueError):
        return {"version": 1, "workspaces": {}}
    return value if isinstance(value.get("workspaces"), dict) else {"version": 1, "workspaces": {}}


def _write(value: dict) -> None:
    _store().write(value)


def _list(workspaces: dict) -> str:
    if not workspaces:
        return "No workspaces are saved. Say, for example, 'save a school workspace with Chrome and Word'."
    lines = []
    for name, item in sorted(workspaces.items()):
        steps = item.get("steps", [])
        labels = ", ".join(str(step.get("app_name", "")) for step in steps[:4])
        extra = f" +{len(steps) - 4}" if len(steps) > 4 else ""
        lines.append(f"- {name}: {labels}{extra}")
    return "Saved workspaces:\n" + "\n".join(lines)


def workspace_manager(parameters: dict | None = None, player=None, report_progress=None,
                      cancel_event=None) -> dict | str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list").strip().casefold().replace(" ", "_")
    data = _read()
    workspaces = data.get("workspaces", {})

    try:
        if action in {"list", "show"}:
            return _list(workspaces)

        name = _clean_name(p.get("name") or p.get("workspace") or "")
        if action in {"save", "create", "update"}:
            steps = _clean_steps(p.get("steps"))
            if name not in workspaces and len(workspaces) >= MAX_WORKSPACES:
                return f"You already have {MAX_WORKSPACES} workspaces. Delete one first."
            previous = workspaces.get(name)
            workspaces[name] = {
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "steps": steps,
            }
            data["workspaces"] = workspaces
            _write(data)

            def _undo_save() -> str:
                current = _read()
                saved = current.setdefault("workspaces", {})
                if previous is None:
                    saved.pop(name, None)
                else:
                    saved[name] = previous
                _write(current)
                return f"Removed workspace '{name}'." if previous is None else f"Restored workspace '{name}'."

            push_undo(f"saving workspace '{name}'", _undo_save)
            return f"{'Updated' if previous else 'Saved'} workspace '{name}' with {len(steps)} application(s)."

        if action in {"delete", "remove", "forget"}:
            previous = workspaces.pop(name, None)
            if previous is None:
                return f"I have no workspace called '{name}'."
            data["workspaces"] = workspaces
            _write(data)

            def _undo_delete() -> str:
                current = _read()
                current.setdefault("workspaces", {})[name] = previous
                _write(current)
                return f"Restored workspace '{name}'."

            push_undo(f"deleting workspace '{name}'", _undo_delete)
            return f"Deleted workspace '{name}'."

        if action in {"run", "open", "start"}:
            workspace = workspaces.get(name)
            if workspace is None:
                available = ", ".join(sorted(workspaces)) or "none"
                return f"I have no workspace called '{name}'. Saved workspaces: {available}."
            result = app_sequence(
                {"steps": workspace["steps"]}, player=player,
                report_progress=report_progress, cancel_event=cancel_event,
            )
            if isinstance(result, dict):
                message = str(result.get("message") or "Workspace completed.")
                return {
                    "ok": bool(result.get("ok")),
                    "status": str(result.get("status") or "failed"),
                    "message": f"Workspace '{name}': {message}",
                    "data": {"workspace": name, **(result.get("data") if isinstance(result.get("data"), dict) else {})},
                }
            return f"Workspace '{name}': {result}"
    except WorkspaceError as exc:
        return str(exc)
    except (OSError, ValueError) as exc:
        return f"The workspace request could not be saved ({type(exc).__name__})."
    return "Use list, save, run, or delete for workspaces."


TOOL = {
    "name": "workspace_manager",
    "description": (
        "Saves, lists, opens, and deletes named application workspaces such as school, coding, "
        "or gaming. A workspace remembers up to eight apps plus optional monitor/window-state choices. "
        "When run, each app is launched and verified one at a time through app_sequence; failures are "
        "reported honestly and later steps still run. Use layout_manager too when the user wants to save "
        "the precise arrangement of windows that are already open."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["list", "save", "run", "delete"],
                "description": "list | save | run | delete",
            },
            "name": {
                "type": "STRING",
                "maxLength": MAX_NAME_CHARS,
                "description": "Workspace name such as school, coding, or gaming.",
            },
            "steps": {
                "type": "ARRAY",
                "maxItems": MAX_STEPS,
                "description": "Ordered application steps, required when saving.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "app_name": {"type": "STRING", "maxLength": 160},
                        "monitor": {"type": "STRING", "maxLength": 40},
                        "state": {"type": "STRING", "enum": sorted(_ALLOWED_STATES)},
                        "foreground": {"type": "BOOLEAN"},
                    },
                    "required": ["app_name"],
                },
            },
        },
        "required": ["action"],
    },
    "handler": workspace_manager,
    "category": "desktop",
    "undoable": True,
    "timeout_seconds": 480.0,
}
