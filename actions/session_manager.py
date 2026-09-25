"""Save, list, resume, and delete bounded local conversation snapshots."""
from __future__ import annotations

from core.action_result import ActionResult
from memory import session_store


def _identifier(parameters: dict) -> str:
    return str(
        parameters.get("session_id")
        or parameters.get("title")
        or parameters.get("name")
        or ""
    ).strip()


def session_manager(
    parameters: dict | None = None,
    player=None,
    session_memory=None,
    **_context,
) -> str | ActionResult:
    values = parameters if isinstance(parameters, dict) else {}
    action = str(values.get("action") or "list").strip().casefold().replace(" ", "_")

    try:
        if action in {"save", "bookmark", "checkpoint"}:
            transcript = session_memory if isinstance(session_memory, (list, tuple)) else []
            snapshot, created = session_store.save_session(
                transcript,
                title=str(values.get("title") or ""),
                summary=str(values.get("summary") or ""),
            )
            verb = "Saved" if created else "Updated"
            return (
                f"{verb} session '{snapshot['title']}' "
                f"(ID {snapshot['id'][:8]}, {len(snapshot['turns'])} turns)."
            )

        if action in {"list", "show", "list_sessions"}:
            sessions = session_store.list_sessions()
            if not sessions:
                return "No saved sessions are available."
            lines = ["Saved sessions:"]
            for item in sessions:
                summary = f" — {item['summary']}" if item["summary"] else ""
                lines.append(
                    f"- {item['id'][:8]} | {item['title']} | "
                    f"{item['turn_count']} turns | {item['updated_at'][:10]}{summary}"
                )
            return "\n".join(lines)

        if action in {"resume", "load", "continue"}:
            identifier = _identifier(values)
            if not identifier:
                return "Invalid session request: provide the saved session name or ID to resume."
            snapshot = session_store.load_session(identifier)
            return ActionResult.success(
                "session_manager",
                f"Loaded saved session '{snapshot['title']}' for a fresh Live conversation.",
                resume_context=session_store.format_resume_context(snapshot),
                session_id=snapshot["id"],
                title=snapshot["title"],
            )

        if action in {"delete", "remove", "forget"}:
            identifier = _identifier(values)
            if not identifier:
                return "Invalid session request: provide the saved session name or ID to delete."
            snapshot = session_store.delete_session(identifier)
            return f"Deleted saved session '{snapshot['title']}'."
    except session_store.SessionStoreError as exc:
        return str(exc)
    except (OSError, RuntimeError, ValueError) as exc:
        return f"Could not update saved sessions ({type(exc).__name__})."

    return "Use save, list, resume, or delete for saved sessions."


TOOL = {
    "name": "session_manager",
    "description": (
        "Manages private local conversation bookmarks. Use save only when the user "
        "explicitly asks to save, bookmark, or checkpoint the current conversation; "
        "supply a short title and optional factual summary. Use list to show saved "
        "sessions, resume to continue one by title or ID, and delete only when the "
        "user explicitly asks to remove one. This is separate from automatic long-term facts."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["save", "list", "resume", "delete"],
                "maxLength": 12,
                "description": "save | list | resume | delete",
            },
            "title": {
                "type": "STRING",
                "maxLength": 80,
                "description": "Short session title; also accepted as the session selector.",
            },
            "session_id": {
                "type": "STRING",
                "maxLength": 16,
                "description": "Full or displayed ID from list; preferred when titles are similar.",
            },
            "summary": {
                "type": "STRING",
                "maxLength": 1000,
                "description": "Optional 1-2 sentence factual summary supplied when saving.",
            },
        },
        "required": ["action"],
    },
    "handler": session_manager,
    "category": "memory",
    "risk": "medium",
    "confirmation_actions": ["delete"],
    "timeout_seconds": 10,
}
