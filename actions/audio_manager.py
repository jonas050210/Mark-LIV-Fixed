"""Voice-accessible audio device selection."""
from __future__ import annotations

import re

from core import audio_devices
from core import system_audio
from core import undo as undo_stack
from memory.config_manager import (
    get_input_device,
    get_output_device,
    save_input_device,
    save_output_device,
)


def _norm_name(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in str(value or "")).strip()


def _selected_device_name(value: str, kind: str) -> tuple[str | None, str]:
    """Map one spoken device name to a currently selectable endpoint.

    Saving a misspelled or disconnected microphone used to look successful, but
    only failed later when MARK LIV reopened its audio stream and silently fell
    back to the system default.  Resolve exact names first and tolerate one
    unambiguous partial name; never guess between several headsets or webcams.
    """
    label = "microphone" if kind == "input" else "speaker"
    try:
        devices = audio_devices.list_devices(kind)
    except Exception as exc:
        return None, f"Could not check available {label}s ({type(exc).__name__})."
    if not devices:
        return None, f"No selectable {label}s are available right now."
    wanted = " ".join(str(value or "").casefold().split())
    exact = [item for item in devices if " ".join(item.casefold().split()) == wanted]
    if exact:
        return exact[0], ""
    candidates = [
        item for item in devices
        if wanted and wanted in " ".join(item.casefold().split())
    ]
    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return (
            None,
            f"'{value}' matches more than one {label}: {', '.join(candidates[:4])}. "
            "Use the exact name from list audio devices.",
        )
    return (
        None,
        f"I could not find a selectable {label} called '{value}'. "
        "Say list audio devices to check the exact name.",
    )


def _app_sessions():
    """Live per-app audio sessions, or None when the mixer cannot be read.

    ``None`` means per-app volume is unavailable here (pycaw missing, or a
    desktop without the Windows audio mixer) — deliberately different from
    ``[]``, which means the mixer is readable but no app currently owns an
    audio session. Telling those two apart is what keeps the failure message
    honest instead of "nothing is playing" on a machine that simply cannot
    answer.
    """
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return None
    try:
        raw = AudioUtilities.GetAllSessions()
    except Exception:
        return None
    out = []
    for session in raw or []:
        try:
            process = getattr(session, "Process", None)
            name = "System sounds" if process is None else str(process.name() or "unknown")
            stem = _norm_name(str(name).rsplit(".", 1)[0])
            pid = int(getattr(process, "pid", 0) or 0)
            control = getattr(session, "SimpleAudioVolume", None)
            if control is None:
                continue
            out.append({
                "name": name,
                "stem": stem,
                "pid": pid,
                "level": float(control.GetMasterVolume() or 0.0),
                "muted": bool(control.GetMute()),
                "control": control,
            })
        except Exception:
            continue
    return out


def _sessions_for(sessions: list, app: str) -> list:
    """Sessions of one application, matched the way its name is spoken.

    Exact process stem first ('spotify' for spotify.exe), then a containment
    either way, then a bounded fuzzy match. Anything looser than that is not
    allowed to move somebody's volume.
    """
    wanted = _norm_name(app)
    if not wanted:
        return []
    exact = [s for s in sessions if s["stem"] == wanted]
    if exact:
        return exact
    contained = [s for s in sessions if wanted in s["stem"] or s["stem"] in wanted]
    if contained:
        return contained
    from core.text_match import partial_ratio

    return [s for s in sessions if partial_ratio(wanted, s["stem"]) >= 0.75]


def _parse_volume_directive(value) -> tuple[str, float | None]:
    """('set'|'delta'|'mute'|'unmute', amount) from a spoken or typed value."""
    text = _norm_name(str(value or ""))
    if not text:
        return ("", None)
    if text in {"mute", "muted", "silent", "stumm", "ton aus"}:
        return ("mute", None)
    if text in {"unmute", "unmuted", "sound on", "ton an", "laut schalten"}:
        return ("unmute", None)
    if text in {"up", "louder", "lauter", "raise", "increase", "mehr"}:
        return ("delta", 10.0)
    if text in {"down", "quieter", "leiser", "lower", "decrease", "reduce", "weniger"}:
        return ("delta", -10.0)
    if text in {"max", "full", "maximum", "voll"}:
        return ("set", 100.0)
    if text in {"half", "halbe", "halfway"}:
        return ("set", 50.0)
    digits = re.findall(r"\d+", text)
    if digits:
        return ("set", min(100.0, max(0.0, float(digits[0]))))
    return ("", None)


def _list_app_volumes() -> str:
    sessions = _app_sessions()
    if sessions is None:
        return (
            "Per-app volume is unavailable on this system; on Windows it needs "
            "the pycaw package (pip install pycaw)."
        )
    if not sessions:
        return (
            "No applications currently have an audio session. Start playback "
            "first, then ask again."
        )
    grouped: dict[str, list] = {}
    for session in sessions:
        grouped.setdefault(session["stem"] or "system sounds", []).append(session)
    rows = []
    for stem, items in grouped.items():
        states = ", ".join(
            ("muted" if item["muted"] else f"{round(item['level'] * 100)}%")
            for item in items
        )
        count = f" (x{len(items)})" if len(items) > 1 else ""
        rows.append(f"- {stem}{count}: {states}")
    return "Per-app volumes right now:\n" + "\n".join(rows)


def _set_app_volume(parameters: dict) -> str:
    app = str(parameters.get("app") or parameters.get("device") or "")[:160].strip()
    if not app:
        return "Tell me which application, for example 'Spotify 50', or say list app volumes."
    sessions = _app_sessions()
    if sessions is None:
        return (
            "Per-app volume is unavailable on this system; on Windows it needs "
            "the pycaw package (pip install pycaw)."
        )
    if not sessions:
        return (
            "No applications currently have an audio session. Start playback "
            "first, then ask again."
        )
    matches = _sessions_for(sessions, app)
    if not matches:
        known = ", ".join(sorted({s["stem"] for s in sessions})[:12])
        return (
            f"No audio session matches '{app}'. Applications with audio right "
            f"now: {known}."
        )
    kind, amount = _parse_volume_directive(
        parameters.get("value")
        if parameters.get("value") is not None
        else parameters.get("level")
    )
    if kind == "":
        return "Tell me the volume as a number 0-100, or up, down, mute, or unmute."

    previous = [(item["pid"], item["level"], item["muted"]) for item in matches]
    changed = 0
    for item in matches:
        try:
            control = item["control"]
            if kind == "mute":
                control.SetMute(1, None)
            elif kind == "unmute":
                control.SetMute(0, None)
            elif kind == "set":
                control.SetMasterVolume(float(amount) / 100.0, None)
            else:   # delta
                new_level = min(1.0, max(0.0, item["level"] + amount / 100.0))
                control.SetMasterVolume(new_level, None)
            changed += 1
        except Exception:
            continue
    if not changed:
        return f"Windows refused the volume change for {app}. Nothing was changed."

    def _undo_app_volume():
        restored = 0
        current = _app_sessions() or []
        by_pid = {session["pid"]: session for session in current}
        for pid, level, muted in previous:
            target = by_pid.get(pid)
            if target is None:
                continue   # that session has ended; nothing to restore there
            try:
                target["control"].SetMasterVolume(level, None)
                target["control"].SetMute(1 if muted else 0, None)
                restored += 1
            except Exception:
                continue
        if not restored:
            undo_stack.refuse(
                "the audio session is gone, so there is nothing to restore."
            )
        return f"Restored the previous volume for {restored} session(s) of {app}."

    undo_stack.push_undo(f"changed {app}'s volume", _undo_app_volume)

    count_note = f" across {changed} sessions" if changed > 1 else ""
    if kind == "mute":
        return f"Muted {app}{count_note}."
    if kind == "unmute":
        return f"Unmuted {app}{count_note}."
    if kind == "set":
        return f"Set {app}'s volume to {round(amount)}%{count_note}."
    return (
        f"{'Raised' if amount and amount > 0 else 'Lowered'} {app}'s volume by "
        f"{round(abs(amount or 0.0))} points{count_note}. Say undo to put it back."
    )


def audio_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list")[:32].casefold().strip().replace(" ", "_")
    value = str(p.get("device") or p.get("value") or "")[:300].strip()
    previous_input = get_input_device()
    previous_output = get_output_device()
    if action in {"list", "list_devices", "devices"}:
        try:
            inputs = audio_devices.list_devices("input")
            outputs = audio_devices.list_devices("output")
            status = audio_devices.diagnostics(get_input_device(), get_output_device())
            inp = status.get("input", {})
            out = status.get("output", {})
            health = (
                f"Current microphone: {inp.get('selected', 'System default')} "
                f"({'connected' if inp.get('connected') else 'fallback'})\n"
                f"Current speakers: {out.get('selected', 'System default')} "
                f"({'connected' if out.get('connected') else 'fallback'})"
            )
            return (health + "\n\nMicrophones:\n- " + "\n- ".join(inputs or ['No tested microphones'])
                    + "\n\nSpeakers:\n- " + "\n- ".join(outputs or ['No tested speakers']))
        except Exception as exc:
            return f"Could not list audio devices: {type(exc).__name__}"

    if action in {"list_system_outputs", "system_devices", "list_system_devices"}:
        devices = system_audio.list_playback_devices()
        current = system_audio.get_default_playback_device()
        if not devices:
            return (
                "No system playback devices could be listed on this platform. "
                "On Windows this needs pycaw and comtypes; on Linux it needs "
                "pactl; on macOS it needs SwitchAudioSource."
            )
        header = f"System default output: {current or 'unknown'}"
        return header + "\n\nAll playback devices:\n- " + "\n- ".join(devices)

    if action in {"list_app_volumes", "app_volumes", "playing_apps"}:
        return _list_app_volumes()

    if action in {"set_app_volume", "app_volume", "volume_app"}:
        return _set_app_volume(p)

    if action in {"set_system_output", "system_output", "output_device", "set_output_device"}:
        if not value:
            return "Tell me the speaker or headset name, or say list system outputs."
        previous_system_output = system_audio.get_default_playback_device()
        ok, message = system_audio.set_default_playback_device(value)
        if not ok:
            return message or f"Could not switch the system output to '{value}'."
        if previous_system_output:
            def _undo_system_output(previous=previous_system_output):
                restored, detail = system_audio.set_default_playback_device(previous)
                return detail if restored else (
                    f"Could not restore the previous output device ({detail})."
                )
            undo_stack.push_undo("changed the system audio output device", _undo_system_output)
        return message

    if action in {"input", "microphone", "set_input", "set_microphone"}:

        if not value:
            return "Tell me the microphone name, or say list audio devices."
        resolved, error = _selected_device_name(value, "input")
        if resolved is None:
            return error
        value = resolved
        save_input_device(value)
        selected = "microphone"
    elif action in {"output", "speakers", "set_output", "set_speakers"}:
        if not value:
            return "Tell me the speaker name, or say list audio devices."
        resolved, error = _selected_device_name(value, "output")
        if resolved is None:
            return error
        value = resolved
        save_output_device(value)
        selected = "speakers"
    elif action in {"default", "system_default"}:
        save_input_device("")
        save_output_device("")
        selected = "audio devices"
        value = "System default"
    else:
        return (
            "Use list, set_input, set_output, default, list_system_outputs, "
            "set_system_output, list_app_volumes, or set_app_volume."
        )

    def undo_audio_selection():
        save_input_device(previous_input)
        save_output_device(previous_output)
        if player is not None:
            reconnect = getattr(player, "on_audio_device_change", None)
            if reconnect:
                reconnect()
        return "Restored the previous microphone and speaker selection."

    undo_stack.push_undo("changed audio device selection", undo_audio_selection)

    if player is not None:
        callback = getattr(player, "on_audio_device_change", None)
        if callback:
            try:
                callback()
            except Exception as exc:
                return f"Saved {selected}, but reconnect failed: {type(exc).__name__}"
    return f"{selected.capitalize()} set to {value}. Reconnecting the audio session."


TOOL = {
    "name": "audio_manager",
    "description": (
        "Lists and selects the microphone and speakers JARVIS uses, can switch "
        "the whole system's default playback device, and changes the volume of "
        "a single application. Use set_input/set_output for JARVIS's own "
        "microphone and speakers (e.g. a headset such as JBL Quantum 400); use "
        "set_system_output when the user means 'switch what my whole PC plays "
        "sound through', which affects every application, not just JARVIS. "
        "list_system_outputs shows the exact device names set_system_output "
        "accepts. set_app_volume changes ONE application's volume in the "
        "Windows mixer without touching anything else ('Spotify 50', 'Spotify "
        "down', 'mute Discord'); list_app_volumes shows what is playing and at "
        "what level. Per-app volume works by application process name — "
        "browser tabs share their browser's sessions, so say 'Chrome', not a "
        "page name. Volume changes can be reversed with undo."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": [
                    "list", "set_input", "set_output", "default",
                    "list_system_outputs", "set_system_output",
                    "list_app_volumes", "set_app_volume",
                ],
                "maxLength": 24,
                "description": (
                    "list | set_input | set_output | default | list_system_outputs | "
                    "set_system_output | list_app_volumes | set_app_volume"
                ),
            },
            "device": {"type": "STRING", "maxLength": 300, "description": "Exact device name from the list."},
            "app": {
                "type": "STRING",
                "maxLength": 160,
                "description": "Application for set_app_volume, e.g. 'Spotify', 'Discord', 'Chrome'.",
            },
            "value": {
                "type": "STRING",
                "maxLength": 40,
                "description": "Volume for set_app_volume: a number 0-100, or 'up'/'down'/'mute'/'unmute'.",
            },
        },
        "required": ["action"],
    },
    "handler": audio_manager,
    "category": "audio",
    "undoable": True,
}
