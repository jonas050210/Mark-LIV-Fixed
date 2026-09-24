"""User-defined deterministic application and URL shortcuts."""
from __future__ import annotations

from core import shortcut_store
from core import undo as undo_stack


def shortcut_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "list").casefold().strip().replace(" ", "_")
    alias = str(p.get("alias") or p.get("name") or "").strip()
    target = str(p.get("target") or p.get("app") or p.get("value") or "").strip()

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
        return f"I could not save that shortcut: {exc}."
    except OSError as exc:
        return f"I could not update the shortcut file: {exc}."
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
            "action": {"type": "STRING", "description": "set | remove | resolve | list"},
            "alias": {"type": "STRING", "description": "Short name such as gd, roblox, or school."},
            "target": {"type": "STRING", "description": "Application name, path, folder, or URL to open."},
        },
        "required": ["action"],
    },
    "handler": shortcut_manager,
    "category": "computer",
    "undoable": True,
}
