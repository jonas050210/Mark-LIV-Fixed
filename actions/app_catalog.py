"""Installed application catalogue actions.

This is deliberately separate from ``open_app`` so the action loader exposes a
small, explicit maintenance surface instead of hiding an unreachable helper in
the launcher module.
"""
from __future__ import annotations

from core.app_index import build_index, load_index


def app_catalog(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list").strip().casefold().replace(" ", "_")

    if action in {"refresh", "rescan", "rebuild"}:
        entries = build_index()
        if not entries:
            return "I could not build an application index on this system."
        return f"Rescanned installed applications: {len(entries)} launchable entries found."

    if action in {"list", "list_apps", "installed"}:
        entries = load_index()
        if not entries:
            return "The installed application index is empty."
        try:
            limit = max(1, min(int(p.get("limit", 30)), 100))
        except (TypeError, ValueError):
            limit = 30
        names = [entry.name for entry in entries[:limit]]
        suffix = f" (+{len(entries) - limit} more)" if len(entries) > limit else ""
        return f"Installed applications ({len(entries)}): {', '.join(names)}{suffix}."

    return "Unknown application catalogue action. Use list or refresh."


TOOL = {
    "name": "app_catalog",
    "description": (
        "Lists applications in MARK LIV's installed-app index or rescans the computer "
        "after an application, game, or browser web app was installed or updated."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["list", "refresh"],
                "description": "list | refresh",
            },
            "limit": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum names returned by list; default 30.",
            },
        },
        "required": ["action"],
    },
    "handler": app_catalog,
}
