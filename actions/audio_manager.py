"""Voice-accessible audio device selection."""
from __future__ import annotations

from core import audio_devices
from core import system_audio
from core import undo as undo_stack
from memory.config_manager import (
    get_input_device,
    get_output_device,
    save_input_device,
    save_output_device,
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
        save_input_device(value)
        selected = "microphone"
    elif action in {"output", "speakers", "set_output", "set_speakers"}:
        if not value:
            return "Tell me the speaker name, or say list audio devices."
        save_output_device(value)
        selected = "speakers"
    elif action in {"default", "system_default"}:
        save_input_device("")
        save_output_device("")
        selected = "audio devices"
        value = "System default"
    else:
        return "Use list, set_input, set_output, default, list_system_outputs, or set_system_output."

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
        "Lists and selects the microphone and speakers JARVIS uses, and can "
        "also switch the whole system's default playback device. Use "
        "set_input/set_output for JARVIS's own microphone and speakers (e.g. "
        "a headset such as JBL Quantum 400); use set_system_output when the "
        "user means 'switch what my whole PC plays sound through', which "
        "affects every application, not just JARVIS. list_system_outputs "
        "shows the exact device names set_system_output accepts."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["list", "set_input", "set_output", "default", "list_system_outputs", "set_system_output"],
                "maxLength": 24,
                "description": "list | set_input | set_output | default | list_system_outputs | set_system_output",
            },
            "device": {"type": "STRING", "maxLength": 300, "description": "Exact device name from the list."},
        },
        "required": ["action"],
    },
    "handler": audio_manager,
    "category": "audio",
    "undoable": True,
}
