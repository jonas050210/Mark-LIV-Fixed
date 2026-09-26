"""User-defined deterministic application and URL shortcuts."""
from __future__ import annotations

from core import shortcut_store
from core import undo as undo_stack


def shortcut_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list")[:24].casefold().strip().replace(" ", "_")
    alias = str(p.get("alias") or p.get("name") or "")[:40].strip()
    target = str(p.get("target") or p.get("app") or p.get("value") or "")[:1024].strip()

    try:
        if action in {"set", "save", "remember", "add"}:
            if not alias or not target:
                return "Provide both a shortcut name and its application or URL target."
            previous = shortcut_store.all_shortcuts().get(alias.casefold())
            saved = shortcut_store.save(alias, target)
            if previous is None:
                undo_stack.push_undo(
                    f"shortcut '{alias.casefold()}'", lambda: shortcut_store.remove(alias)
                )
            else:
                undo_stack.push_undo(
                    f"shortcut '{alias.casefold()}'",
                    lambda: shortcut_store.save(alias, previous),
                )
            return f"Remembered '{alias}' as '{saved}'."
        if action in {"remove", "delete", "forget"}:
            if not alias:
                return "Tell me which shortcut to forget."
            previous = shortcut_store.all_shortcuts().get(alias.casefold())
            if previous is None:
                return f"I do not have a shortcut named '{alias}'."
            shortcut_store.remove(alias)
            undo_stack.push_undo(
                f"removed shortcut '{alias.casefold()}'",
                lambda: shortcut_store.save(alias, previous),
            )
            return f"Forgot shortcut '{alias}'."
        if action in {"resolve", "get", "lookup"}:
            if not alias:
                return "Tell me which shortcut to look up."
            resolved = shortcut_store.resolve(alias)
            return f"'{alias}' opens '{resolved}'." if resolved != alias else f"I do not have a shortcut named '{alias}'."
        if action in {"list", "list_shortcuts", "show"}:
            values = shortcut_store.all_shortcuts()
            if not values:
                return "No custom shortcuts are saved."
            return "Saved shortcuts:\n" + "\n".join(
                f"- {name} → {value}" for name, value in values.items()
            )
    except ValueError as exc:
        return f"I could not save that shortcut: {type(exc).__name__}."
    except OSError as exc:
        return f"I could not update the shortcut file: {type(exc).__name__}."
    return "Use set, remove, resolve, or list for shortcuts."


TOOL = {
    "name": "shortcut_manager",
    "description": (
        "Creates deterministic shortcuts for applications, games, folders, or URLs. "
        "Use this when the user says remember that an abbreviation means something, "
        "for example remember gd means Geometry Dash. Later open_app resolves the "
        "shortcut before launching, so 'open gd' is reliable."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["set", "remove", "resolve", "list"], "maxLength": 16, "description": "set | remove | resolve | list"},
            "alias": {"type": "STRING", "maxLength": 40, "description": "Name such as gd, school mode, or my code."},
            "target": {"type": "STRING", "maxLength": 1024, "description": "Application name, OneDrive path, folder, or URL to open."},
        },
        "required": ["action"],
    },
    "handler": shortcut_manager,
    "category": "computer",
    "undoable": True,
}
