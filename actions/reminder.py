import json
import subprocess
import sys
import secrets
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from core.json_store import JsonStore, JsonStoreCorruptError
from core.undo import push_undo
from core.path_policy import atomic_create_text, resolve_user_path

_CNW: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _scripts_dir() -> Path:
    d = resolve_user_path(
        Path.home() / ".jarvis" / "reminders",
        allow_missing=True,
        allow_protected=True,
        reject_symlinks=True,
    )
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if d.is_symlink() or not d.is_dir():
        raise OSError("The reminder storage path is not a safe directory.")
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _sanitise(text: str, max_len: int = 200) -> str:
    return " ".join(
        "".join(char for char in str(text or "") if ord(char) >= 32 and char != "\x7f").split()
    )[:max_len]

def _write_notify_script(task_name: str, message: str) -> Path:
    script_path = _scripts_dir() / f"{task_name}.py"
    msg_literal = json.dumps(message)
    script_body = f"""# Auto-generated MARK LIV Windows reminder
import pathlib
message = {msg_literal}
notified = False
try:
    from win10toast import ToastNotifier
    ToastNotifier().show_toast("MARK LIV Reminder", message, duration=15, threaded=False)
    notified = True
except Exception:
    pass
if not notified:
    try:
        import subprocess
        subprocess.run(["msg", "*", "/TIME:30", message], check=False, timeout=10)
    except Exception:
        pass
try:
    import winsound
    for frequency in (800, 1000, 1200):
        winsound.Beep(frequency, 180)
except Exception:
    pass
try:
    pathlib.Path(__file__).unlink(missing_ok=True)
except Exception:
    pass
"""
    atomic_create_text(script_path, script_body)
    return script_path


def _schedule_windows(target_dt: datetime, task_name: str,
                      script_path: Path, message: str) -> tuple[str, str]:
    python_exe = Path(sys.executable)
    pythonw = python_exe.parent / "pythonw.exe"
    if pythonw.exists():
        python_exe = pythonw

    xml_path = _scripts_dir() / f"{task_name}.xml"
    command_xml = xml_escape(str(python_exe))
    script_xml = xml_escape(str(script_path), {'"': "&quot;"})
    xml_content = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <RegistrationInfo><Description>J.A.R.V.I.S Reminder</Description></RegistrationInfo>\n'
        '  <Triggers><TimeTrigger>\n'
        f'    <StartBoundary>{target_dt.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>\n'
        '    <Enabled>true</Enabled>\n'
        '  </TimeTrigger></Triggers>\n'
        '  <Actions><Exec>\n'
        f'    <Command>{command_xml}</Command>\n'
        f'    <Arguments>&quot;{script_xml}&quot;</Arguments>\n'
        '  </Exec></Actions>\n'
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>\n'
        '    <Enabled>true</Enabled>\n'
        '  </Settings>\n'
        '  <Principals><Principal>\n'
        '    <LogonType>InteractiveToken</LogonType>\n'
        '    <RunLevel>LeastPrivilege</RunLevel>\n'
        '  </Principal></Principals>\n'
        '</Task>'
    )

    atomic_create_text(xml_path, xml_content, encoding="utf-16")

    try:
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
            capture_output=True, text=True, timeout=20, **_CNW,
        )
    finally:
        try:
            xml_path.unlink(missing_ok=True)
        except OSError:
            pass

    if result.returncode != 0:
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ schtasks failed with exit code {result.returncode}.")
        return "", ""

    return task_name, "schtasks"


# ── Registry of scheduled reminders ──────────────────────────────────────────
#
# Scheduling a reminder hands the job to the operating system, which is what
# makes it survive a restart of MARK LIV. The price is that the assistant then
# has no idea what it scheduled: without a record, a reminder can only be
# removed by opening Task Scheduler, launchctl or systemctl by hand. This
# registry is that record — it stores what is needed to cancel a job and
# nothing else.

BASE_DIR = Path(__file__).resolve().parent.parent
REMINDER_FILE = BASE_DIR / "memory" / "reminders.json"
MAX_REMINDERS = 100
_BACKENDS = {"schtasks"}


def _valid_registry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    items = value.get("reminders", [])
    if not isinstance(items, list) or len(items) > MAX_REMINDERS:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        for field in ("id", "when", "message", "backend"):
            if not isinstance(item.get(field), str) or len(item[field]) > 300:
                return False
        if item["backend"] not in _BACKENDS:
            return False
        if not isinstance(item.get("script", ""), str) or len(item.get("script", "")) > 1000:
            return False
        if not isinstance(item.get("handle", ""), str) or len(item.get("handle", "")) > 300:
            return False
    return True


def _registry_store() -> JsonStore[dict]:
    return JsonStore(REMINDER_FILE, dict, validator=_valid_registry)


def _read_registry() -> list[dict]:
    if not REMINDER_FILE.exists():
        return []
    try:
        data = _registry_store().read()
    except (JsonStoreCorruptError, OSError, ValueError):
        return []
    items = data.get("reminders")
    return list(items)[:MAX_REMINDERS] if isinstance(items, list) else []


def _write_registry(items: list[dict]) -> None:
    try:
        _registry_store().write({"reminders": items[:MAX_REMINDERS]})
    except Exception as exc:
        print(f"[Reminder] could not persist the reminder registry ({type(exc).__name__}).")


def _record_reminder(entry: dict) -> None:
    items = [item for item in _read_registry() if item.get("id") != entry.get("id")]
    items.append(entry)
    items.sort(key=lambda item: item.get("when", ""))
    _write_registry(items)


def _parse_when(value: str) -> datetime | None:
    try:
        return datetime.strptime(str(value or "")[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _prune_registry(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split the registry into still-pending and already-fired reminders.

    A reminder is dropped a full minute after its time rather than exactly on
    it, so a listing made while the notification is being delivered does not
    claim the reminder never existed.
    """
    now = datetime.now()
    pending, expired = [], []
    for item in items:
        when = _parse_when(item.get("when", ""))
        if when is None or (now - when).total_seconds() > 60:
            expired.append(item)
        else:
            pending.append(item)
    return pending, expired


def _delete_script(path_text: str) -> None:
    if not path_text:
        return
    try:
        candidate = Path(path_text)
        if candidate.parent == _scripts_dir() and candidate.suffix == ".py":
            candidate.unlink(missing_ok=True)
    except OSError:
        pass


def _run(argv: list[str], **kwargs) -> tuple[bool, str]:
    """Run a scheduler command, reporting failure honestly."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=20, **kwargs
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        return False, type(exc).__name__
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return False, (detail[-1][:120] if detail else f"exit code {result.returncode}")
    return True, ""


def _cancel_job(item: dict) -> tuple[bool, str]:
    """Remove one scheduled job from the operating system's scheduler."""
    backend = item.get("backend", "")
    handle = item.get("handle") or item.get("id", "")
    if not handle:
        return False, "the reminder has no scheduler handle"

    if backend != "schtasks":
        return False, f"unknown Windows scheduler '{backend}'"
    return _run(["schtasks", "/Delete", "/TN", handle, "/F"], **_CNW)


def reminder(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    raw_action = params.get("action", "set")
    action = raw_action.strip().casefold()[:16] if isinstance(raw_action, str) else "set"

    if action in {"list", "show", "pending"}:
        return _list_reminders()
    if action in {"cancel", "delete", "remove"}:
        return _cancel_reminder(params)
    return _set_reminder(params, player)


def _list_reminders() -> str:
    pending, expired = _prune_registry(_read_registry())
    if expired:
        for item in expired:
            _delete_script(item.get("script", ""))
        _write_registry(pending)
    if not pending:
        return "You have no reminders scheduled."
    lines = [f"{len(pending)} reminder{'s' if len(pending) != 1 else ''} scheduled:"]
    for index, item in enumerate(pending, start=1):
        when = _parse_when(item.get("when", ""))
        stamp = when.strftime("%B %d at %H:%M") if when else item.get("when", "unknown time")
        lines.append(f"{index}. {stamp} — {item.get('message', 'Reminder')}")
    return "\n".join(lines)


def _select_reminder(pending: list[dict], params: dict) -> dict | None:
    """Pick the reminder a cancel request refers to, by number or by text."""
    raw_index = params.get("index")
    if isinstance(raw_index, bool):
        raw_index = None
    if isinstance(raw_index, (int, float, str)):
        try:
            number = int(str(raw_index).strip())
        except (TypeError, ValueError):
            number = 0
        if 1 <= number <= len(pending):
            return pending[number - 1]
    wanted = str(params.get("message") or "").strip().casefold()
    if wanted:
        for item in pending:
            if item.get("message", "").casefold() == wanted:
                return item
        matches = [item for item in pending if wanted in item.get("message", "").casefold()]
        if len(matches) == 1:
            return matches[0]
    return None


def _cancel_reminder(params: dict) -> str:
    pending, expired = _prune_registry(_read_registry())
    if expired:
        for item in expired:
            _delete_script(item.get("script", ""))
        _write_registry(pending)
    if not pending:
        return "You have no reminders scheduled, so there is nothing to cancel."

    if len(pending) == 1 and not params.get("index") and not params.get("message"):
        target = pending[0]
    else:
        target = _select_reminder(pending, params)
    if target is None:
        return (
            "I could not tell which reminder you mean. "
            + _list_reminders()
            + "\nSay the number or the exact text."
        )

    removed, detail = _cancel_job(target)
    if not removed:
        # The job is still registered with the operating system, so the
        # reminder will still fire. Saying otherwise would be a lie.
        return (
            f"I could not cancel the reminder '{target.get('message', '')}' "
            f"({detail}). It is still scheduled."
        )
    _delete_script(target.get("script", ""))
    _write_registry([item for item in pending if item.get("id") != target.get("id")])
    when = _parse_when(target.get("when", ""))
    stamp = when.strftime("%B %d at %H:%M") if when else target.get("when", "")

    # Cancelling is otherwise unrecoverable: the scheduler job is gone and the
    # registry entry with it, so "no, not that one" would mean dictating the
    # date, time and text again. Re-scheduling is the exact inverse.
    if when is not None:
        push_undo(
            f"cancelled reminder '{target.get('message', '')}'",
            lambda item=dict(target), moment=when: _restore_reminder(item, moment),
        )
    return f"Cancelled the reminder for {stamp} — {target.get('message', 'Reminder')}."


def _undo_set(task_name: str) -> str:
    """Take back a reminder that was just scheduled."""
    for item in _read_registry():
        if item.get("id") == task_name:
            removed, detail = _cancel_job(item)
            if not removed:
                return f"I could not remove that reminder again ({detail})."
            _delete_script(item.get("script", ""))
            _write_registry([
                other for other in _read_registry() if other.get("id") != task_name
            ])
            return "Removed the reminder again."
    return "That reminder is no longer scheduled."


def _restore_reminder(item: dict, when: datetime) -> str:
    """Put a cancelled reminder back, if its time has not already passed."""
    if when <= datetime.now():
        return "That reminder's time has already passed, so I cannot restore it."
    result = _set_reminder({
        "date": when.strftime("%Y-%m-%d"),
        "time": when.strftime("%H:%M"),
        "message": item.get("message", "Reminder"),
    })
    if result.startswith("Reminder set"):
        return f"Restored the reminder for {when.strftime('%B %d at %H:%M')}."
    return f"I could not restore that reminder: {result}"


def _set_reminder(params: dict, player=None) -> str:
    raw_date = params.get("date", "")
    raw_time = params.get("time", "")
    raw_message = params.get("message", "Reminder")
    date_str = raw_date.strip()[:10] if isinstance(raw_date, str) else ""
    time_str = raw_time.strip()[:5] if isinstance(raw_time, str) else ""
    message = raw_message.strip()[:200] if isinstance(raw_message, str) else "Reminder"

    if not date_str or not time_str:
        return "I need both a date and a time to set a reminder."

    try:
        target_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "I couldn't parse that date or time. Please use YYYY-MM-DD and HH:MM."

    if target_dt <= datetime.now():
        return "That time has already passed — I can't set a reminder in the past."

    pending, _expired = _prune_registry(_read_registry())
    if len(pending) >= MAX_REMINDERS:
        return f"You already have {MAX_REMINDERS} reminders scheduled; cancel one first."

    safe_msg = _sanitise(message)
    task_name = (
        f"JARVISReminder_{target_dt.strftime('%Y%m%d_%H%M%S')}_"
        f"{secrets.token_hex(3)}"
    )

    try:
        script_path = _write_notify_script(task_name, safe_msg)
    except Exception as e:
        print(f"[Reminder] ❌ Script preparation failed ({type(e).__name__}).")
        return "Could not prepare the reminder script."

    try:
        handle, backend = _schedule_windows(target_dt, task_name, script_path, safe_msg)
    except Exception as e:
        script_path.unlink(missing_ok=True)
        print(f"[Reminder] ❌ Scheduling failed ({type(e).__name__}).")
        return "Something went wrong while scheduling the reminder."

    if not backend:
        return "I couldn't register the reminder with the system scheduler."

    _record_reminder({
        "id": task_name,
        "when": target_dt.strftime("%Y-%m-%d %H:%M"),
        "message": safe_msg,
        "backend": backend,
        "handle": handle,
        "script": str(script_path),
    })

    if player:
        player.write_log(f"[Reminder] ✅ {date_str} {time_str}")

    push_undo(
        f"reminder for {target_dt.strftime('%Y-%m-%d %H:%M')}",
        lambda task=task_name: _undo_set(task),
    )

    friendly_time = target_dt.strftime("%B %d at %I:%M %p")
    return f"Reminder set for {friendly_time}."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "reminder",
    "description": (
        "Set, list, or cancel timed reminders. The reminder is registered with "
        "the operating system's scheduler, so it fires even when MARK LIV is "
        "not running. Use action 'set' with date, time and message; 'list' to "
        "see what is pending; 'cancel' with the number from that list or the "
        "exact message text."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["set", "list", "cancel"],
                "description": "set | list | cancel (default: set)"
            },
            "index": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 100,
                "description": "Which reminder to cancel, as numbered by the list action"
            },
            "date": {
                "type": "STRING",
                "minLength": 10,
                "maxLength": 10,
                "description": "Date in YYYY-MM-DD format"
            },
            "time": {
                "type": "STRING",
                "minLength": 5,
                "maxLength": 5,
                "description": "Time in HH:MM format (24h)"
            },
            "message": {
                "type": "STRING",
                "minLength": 1,
                "maxLength": 200,
                "description": "Reminder message text"
            }
        },
        "required": ["action"]
    },
    "handler": reminder,
}
