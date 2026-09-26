"""Installed application catalogue actions.

This is deliberately separate from ``open_app`` so the action loader exposes a
small, explicit maintenance surface instead of hiding an unreachable helper in
the launcher module.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from core import shortcut_store
from core.app_index import (
    INDEX_FILE,
    build_index,
    load_index,
    resolve,
)
from core.user_paths import desktop_candidates, locations


def _source_summary(entries) -> str:
    counts = Counter(str(getattr(entry, "source", "") or "unknown") for entry in entries)
    return ", ".join(f"{source}: {count}" for source, count in sorted(counts.items())) or "none"


def _diagnose(query: str) -> str:
    """Explain exactly what the launcher currently knows, without guessing."""
    entries = load_index()
    known = locations()
    desktops = desktop_candidates()
    aliases = shortcut_store.all_shortcuts()
    source_rows = _source_summary(entries)
    cache = "present" if INDEX_FILE.exists() else "not built yet"

    lines = [
        "Application discovery diagnostic",
        f"Indexed launch targets: {len(entries)} ({source_rows})",
        f"Index cache: {cache}",
        "Desktop folders checked: " + (
            ", ".join(str(path) for path in desktops) if desktops else "none available"
        ),
        f"Canonical Desktop: {known['desktop']}",
        f"Saved aliases: {len(aliases)}",
    ]
    if "onedrive" in str(known["desktop"]).casefold():
        lines.append("OneDrive Desktop: detected and included in application discovery.")

    clean = str(query or "").strip()[:160]
    if not clean:
        lines.append(
            "For one missing app, ask me to diagnose it by name; I will show matching "
            "entries, aliases, and the next useful fix."
        )
        return "\n".join(lines)

    resolved_alias = shortcut_store.resolve(clean)
    if resolved_alias != clean:
        lines.append(f"Alias '{clean}' resolves to: {resolved_alias}")
    matches = resolve(resolved_alias, entries=entries) or resolve(clean, entries=entries)
    if matches:
        lines.append(f"Matches for '{clean}':")
        for entry in matches[:5]:
            target = Path(entry.target).name if entry.kind in {"exec", "lnk", "bundle"} else entry.target
            lines.append(f"- {entry.name} [{entry.kind}/{entry.source}] → {target}")
    else:
        lines.append(f"No indexed match for '{clean}'.")
        lines.append(
            "Next steps: rescan applications; ensure its Start-menu or OneDrive Desktop "
            "shortcut exists; or save a custom alias pointing at its .lnk/.exe."
        )
    return "\n".join(lines)


def app_catalog(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list").strip().casefold().replace(" ", "_")

    if action in {"refresh", "rescan", "rebuild"}:
        entries = build_index()
        if not entries:
            return "I could not build an application index on this system."
        return (
            f"Rescanned installed applications: {len(entries)} launchable entries found "
            f"({_source_summary(entries)})."
        )

    if action in {"diagnose", "diagnostics", "why_missing"}:
        return _diagnose(str(p.get("app_name") or p.get("query") or ""))

    if action in {"list", "list_apps", "installed"}:
        entries = load_index()
        if not entries:
            return "The installed application index is empty. Try refreshing it."
        try:
            limit = max(1, min(int(p.get("limit", 30)), 100))
        except (TypeError, ValueError):
            limit = 30
        names = [entry.name for entry in entries[:limit]]
        suffix = f" (+{len(entries) - limit} more)" if len(entries) > limit else ""
        return f"Installed applications ({len(entries)}): {', '.join(names)}{suffix}."

    return "Unknown application catalogue action. Use list, refresh, or diagnose."


TOOL = {
    "name": "app_catalog",
    "description": (
        "Lists applications in MARK LIV's installed-app index, rescans the computer "
        "after an application, game, or browser web app was installed or updated, or "
        "diagnoses why a named application was not found. The diagnosis reports actual "
        "scan sources, OneDrive Desktop discovery, aliases, and matching launch entries."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["list", "refresh", "diagnose"],
                "description": "list | refresh | diagnose",
            },
            "limit": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 100,
                "description": "Maximum names returned by list; default 30.",
            },
            "app_name": {
                "type": "STRING",
                "maxLength": 160,
                "description": "Application or saved alias to diagnose when action is diagnose.",
            },
        },
        "required": ["action"],
    },
    "handler": app_catalog,
}
