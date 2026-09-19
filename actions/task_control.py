"""Voice/chat controls for the same activities displayed in the HUD."""
import json
from core import tasks


def task_control(parameters=None, **_context):
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action", "list")).lower()
    if action == "list":
        return json.dumps(tasks.snapshot(limit=32), ensure_ascii=False)
    ident = str(params.get("task_id", ""))
    task = tasks.get(ident)
    if task is None:
        return "Task not found. Use list to get the current task IDs."
    if action == "inspect":
        return json.dumps(task.snapshot(), ensure_ascii=False)
    if task.state not in tasks.ACTIVE_STATES:
        return f"That task has already {task.state}; nothing was changed."
    if action == "pause":
        return ("Pause requested; the owner will pause at its next safe checkpoint."
                if task.request_pause() else "This task cannot be safely paused by JARVIS.")
    if action == "resume":
        task.resume()
        return "Resume requested."
    if action == "cancel":
        task.request_cancel()
        return "Cancellation requested. Completion is not claimed until the owner stops."
    return "Unknown task action. Use list, inspect, pause, resume or cancel."


TOOL = {
    "name": "task_control",
    "description": "List and inspect activities, or request safe pause/resume/cancel by task ID. Never force-kills processes.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | inspect | pause | resume | cancel"},
            "task_id": {"type": "STRING", "description": "Exact ID from list; required except for list"},
        },
        "required": ["action"],
    },
    "handler": task_control,
}
