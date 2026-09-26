"""Privacy-safe runtime diagnostic report export."""
from __future__ import annotations

from datetime import datetime, timezone

from core.diagnostics import export, snapshot
from core.user_paths import locations


def diagnostics(parameters: dict | None = None, player=None) -> dict:
    events = snapshot()
    requested = str((parameters or {}).get("action") or "summary").strip().lower()
    if requested == "summary":
        failures = sum(event["level"] == "error" for event in events)
        warnings = sum(event["level"] == "warning" for event in events)
        return {
            "ok": True,
            "status": "succeeded",
            "message": f"Runtime diagnostics: {len(events)} events, {warnings} warnings, {failures} errors.",
            "data": {"event_count": len(events), "warnings": warnings, "errors": failures},
        }

    downloads = locations()["downloads"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    destination = downloads / f"MARK-LIV-diagnostics-{stamp}.json"
    try:
        export(destination)
    except OSError as exc:
        return {
            "ok": False,
            "status": "failed",
            "message": f"Could not export diagnostics ({type(exc).__name__}).",
        }
    return {
        "ok": True,
        "status": "succeeded",
        "message": f"Redacted diagnostics exported to {destination}.",
        "data": {"path": str(destination), "event_count": len(events)},
    }


TOOL = {
    "name": "diagnostics",
    "description": (
        "Summarize recent MARK LIV runtime warnings or export a privacy-redacted "
        "diagnostic JSON report to Downloads. The report never contains API keys, "
        "authorization tokens, passwords, file contents, or conversation text."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["summary", "export"],
                "description": "Summarize diagnostics or export the redacted report.",
            }
        },
        "required": ["action"],
    },
    "handler": diagnostics,
    "category": "system",
    "timeout": 10,
}
