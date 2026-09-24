"""Voice-accessible audio device selection."""
from __future__ import annotations

from core import audio_devices
from memory.config_manager import (
    get_input_device,
    get_output_device,
    save_input_device,
    save_output_device,
)


def audio_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "list").casefold().strip().replace(" ", "_")
    value = str(p.get("device") or p.get("value") or "").strip()
    if action in {"list", "list_devices", "devices"}:
        try:
            inputs = audio_devices.list_devices("input")
            outputs = audio_devices.list_devices("output")
            return "Microphones:\n- " + "\n- ".join(inputs) + "\n\nSpeakers:\n- " + "\n- ".join(outputs)
        except Exception as exc:
            return f"Could not list audio devices: {exc}"
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

    if player is not None:
        callback = getattr(player, "on_audio_device_change", None)
        if callback:
            try:
                callback()
            except Exception as exc:
                return f"Saved {selected}, but reconnect failed: {exc}"
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
            "action": {"type": "STRING", "description": "list | set_input | set_output | default"},
            "device": {"type": "STRING", "description": "Exact device name from the list."},
        },
        "required": ["action"],
    },
    "handler": audio_manager,
    "category": "audio",
    "undoable": True,
}
