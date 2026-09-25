"""Voice-accessible audio device selection."""
from __future__ import annotations

from core import audio_devices
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
        return "Use list, set_input, set_output, or default."

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
        "Lists and selects the microphone and speakers JARVIS uses. Use this for "
        "headsets such as JBL Quantum 400: list devices first, then set_input or "
        "set_output by the exact displayed name. The audio session reconnects "
        "without losing the conversation."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["list", "set_input", "set_output", "default"], "maxLength": 16, "description": "list | set_input | set_output | default"},
            "device": {"type": "STRING", "maxLength": 300, "description": "Exact device name from the list."},
        },
        "required": ["action"],
    },
    "handler": audio_manager,
    "category": "audio",
    "undoable": True,
}
