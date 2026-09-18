"""
audio_device.py — list and choose the microphone / speaker by voice.

core/audio_devices.py already enumerates and probes the host audio endpoints,
but until now it was reachable only through the settings picker in ui.py. This
action exposes the same information to the model: "which mics do I have",
"take the headset".

Deliberate limitation
─────────────────────
The choice is *persisted*, exactly like the settings picker does, but the action
does not tear down and rebuild the running Gemini Live audio session. Restarting
a session from a tool callback would cut off whatever the user is hearing — the
same reason the API Keys overlay refreshes caches instead of reconnecting. The
reply therefore says plainly when the new device takes effect.

Every switch is registered with core.undo, so the existing `undo` tool puts the
previous device straight back.
"""
from __future__ import annotations

_CORE_KINDS = ("input", "output")
_ALIASES = {
    "input": "input", "inputs": "input", "mic": "input", "microphone": "input",
    "recording": "input", "in": "input",
    "output": "output", "outputs": "output", "speaker": "output",
    "speakers": "output", "playback": "output", "out": "output",
}


def _kind(raw, default: str = "input") -> str:
    key = str(raw or "").strip().lower()
    return _ALIASES.get(key, default if default in _CORE_KINDS else "input")


def _current() -> dict[str, str]:
    try:
        from memory.config_manager import get_input_device, get_output_device
        return {"input": get_input_device() or "", "output": get_output_device() or ""}
    except Exception:
        return {"input": "", "output": ""}


def _persist(kind: str, name: str) -> bool:
    try:
        from memory.config_manager import save_input_device, save_output_device
        saver = save_input_device if kind == "input" else save_output_device
        return bool(saver(name))
    except Exception:
        return False


def _match(name: str, devices: list[str]):
    """Pick the single best device for a spoken name, or None.

    Spoken device names are approximate ("the headset", "blue yeti"), so match
    case-insensitively on the whole name first and then on a substring. An
    ambiguous substring is refused rather than guessed — picking the wrong
    microphone silently is worse than asking.
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for device in devices:
        if str(device).strip().lower() == wanted:
            return device
    hits = [d for d in devices if wanted in str(d).strip().lower()]
    return hits[0] if len(hits) == 1 else None


def _register_undo(kind: str, previous: str, new: str) -> None:
    """Make the switch reversible through the existing `undo` tool."""
    try:
        from core import undo

        def restore() -> str:
            ok = _persist(kind, previous)
            return (f"Audio {kind} restored to {previous or 'system default'}."
                    if ok else f"Could not restore the audio {kind} device.")

        undo.push_undo(f"audio {kind} → {new or 'system default'}", restore)
    except Exception:
        pass


def audio_device_action(parameters: dict, player=None) -> str:
    """
    List audio endpoints or choose the active microphone / speaker.

    parameters:
        action : "list" (default) | "set_input" | "set_output"
        kind   : "input" | "output" — alternative to set_input / set_output
        device : spoken name of the device to select
    """
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "list").strip().lower()

    try:
        from core import audio_devices
    except Exception as exc:
        return f"The audio device list is unavailable right now: {type(exc).__name__}."

    kind = _kind(p.get("kind"), "input")
    if action in ("set_input", "set_mic", "set_microphone"):
        action, kind = "set", "input"
    elif action in ("set_output", "set_speaker", "set_speakers"):
        action, kind = "set", "output"
    elif action in ("set", "switch", "use", "select"):
        action = "set"
    elif action not in ("list",):
        action = "list"

    current = _current()

    if action == "list":
        lines = []
        for one in _CORE_KINDS:
            try:
                devices = audio_devices.list_devices(one, refresh=True)
            except Exception:
                devices = []
            marker = current.get(one, "")
            shown = ", ".join(
                f"{d}{' (active)' if d == marker else ''}" for d in devices[:12]
            ) or "none found"
            lines.append(f"{one.title()}: {shown}")
        if player is not None:
            try:
                player.write_log("AUDIO: device list requested")
            except Exception:
                pass
        return " | ".join(lines)

    wanted = str(p.get("device") or "").strip()
    if not wanted:
        return f"Which device should I use for {kind}? Say 'list audio devices' to hear the options."

    try:
        devices = audio_devices.list_devices(kind, refresh=True)
    except Exception as exc:
        return f"I could not read the {kind} devices: {type(exc).__name__}."

    # "system default" is a real choice, not a failure: audio_devices.resolve()
    # returns None for it, which is also the fallback when a device disappears.
    default_label = getattr(audio_devices, "DEFAULT_LABEL", "System default")
    if wanted.lower() in ("default", "system", "system default", default_label.lower()):
        chosen, label = "", default_label
    else:
        chosen = _match(wanted, devices)
        if chosen is None:
            near = [d for d in devices if str(wanted).lower() in str(d).lower()]
            if len(near) > 1:
                return (f"'{wanted}' matches several {kind} devices: "
                        + ", ".join(str(d) for d in near[:5]) + ". Which one?")
            return (f"I could not find a {kind} device called '{wanted}'. "
                    f"Available: {', '.join(str(d) for d in devices[:8]) or 'none'}.")
        label = chosen

    previous = current.get(kind, "")
    if chosen == previous:
        return f"{label} is already the active {kind} device."

    if not _persist(kind, chosen):
        return f"I could not save the {kind} device choice — the configuration was not changed."

    _register_undo(kind, previous, label)
    if player is not None:
        try:
            player.write_log(f"AUDIO: {kind} device set to {label or default_label}")
        except Exception:
            pass

    return (f"{kind.title()} device set to {label}. It takes effect as soon as the "
            f"audio session is rebuilt — say 'undo' to switch back to "
            f"{previous or default_label}.")


TOOL = {
    "name": "audio_device",
    "description": (
        "Lists the available microphones and speakers, or selects which one the "
        "assistant uses. Use it when the user asks what audio devices exist, wants "
        "to switch to a headset or Bluetooth speaker, or says the wrong microphone "
        "was picked. Selections are saved and can be reverted with the undo tool; "
        "the change applies when the audio session is next rebuilt."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: list (default), set_input, set_output.",
            },
            "kind": {
                "type": "STRING",
                "description": "Optional alternative to set_input/set_output: "
                               "'input' (microphone) or 'output' (speaker).",
            },
            "device": {
                "type": "STRING",
                "description": "Name of the device to select, as spoken by the "
                               "user, or 'system default'. Required when setting.",
            },
        },
        "required": [],
    },
    "handler": audio_device_action,
}
