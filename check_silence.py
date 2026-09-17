#!/usr/bin/env python3
"""
check_silence.py — explain the common "JARVIS is silent" causes without
starting the app.

It reads the same config keys, resolves the same audio device names, records a
short microphone sample, plays a short speaker tone, checks dashboard ports, and
prints which intentional gate is closed. No Qt, no Gemini session, no network.
"""
from __future__ import annotations

import math
import socket
import sys
import time
from pathlib import Path

for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import audio_devices  # noqa: E402
from memory.config_manager import (  # noqa: E402
    get_dashboard_lan_enabled,
    get_input_device,
    get_output_device,
    get_plugin_config,
    get_push_to_talk_enabled,
    get_wake_word_enabled,
    load_api_keys,
)

SEND_SAMPLE_RATE = 16000
RECEIVE_SAMPLE_RATE = 24000


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)), flush=True)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _audio_probe() -> None:
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:
        print(f"Audio probe skipped: sounddevice/numpy unavailable ({e})")
        return

    audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)
    input_name = get_input_device()
    output_name = get_output_device()
    input_dev = audio_devices.resolve(input_name, "input")
    output_dev = audio_devices.resolve(output_name, "output")
    print(f"Configured input:  {input_name or 'System default'} -> {input_dev}")
    print(f"Configured output: {output_name or 'System default'} -> {output_dev}")

    try:
        print("Recording 1.5 seconds from the resolved input device...")
        rec = sd.rec(int(1.5 * SEND_SAMPLE_RATE), samplerate=SEND_SAMPLE_RATE,
                     channels=1, dtype="float32", device=input_dev)
        sd.wait()
        peak = float(np.max(np.abs(rec))) if rec.size else 0.0
        rms = float(math.sqrt(np.mean(np.square(rec)))) if rec.size else 0.0
        print(f"Mic level: peak={peak:.5f} rms={rms:.5f}")
        if peak < 0.005:
            print("Gate hint: the selected microphone is extremely quiet or muted.")
    except Exception as e:
        print(f"Microphone probe failed: {e}")

    try:
        print("Playing a 0.7 second 660 Hz tone on the resolved output device...")
        t = np.linspace(0, 0.7, int(RECEIVE_SAMPLE_RATE * 0.7), endpoint=False)
        tone = (0.18 * np.sin(2 * np.pi * 660 * t)).astype("float32")
        sd.play(tone, samplerate=RECEIVE_SAMPLE_RATE, device=output_dev)
        sd.wait()
        print("Did you hear the tone? If not, the selected output path is silent.")
    except Exception as e:
        print(f"Speaker probe failed: {e}")


def main() -> int:
    print("=" * 72)
    print(" MARK LIV — silence / gate / audio probe")
    print("=" * 72)

    cfg = load_api_keys()
    section("Relevant config")
    keys = [
        "wake_word_enabled", "push_to_talk_enabled", "input_device",
        "output_device", "dashboard_lan_enabled", "plugins_enabled",
        "plugin_config",
    ]
    for key in keys:
        print(f"{key}: {cfg.get(key)!r}")
    print(f"telegram_remote config: {get_plugin_config('telegram_remote')!r}")
    print(f"chat_takeover config:  {get_plugin_config('chat_takeover')!r}")

    section("Intentional gates")
    wake = bool(get_wake_word_enabled())
    ptt = bool(get_push_to_talk_enabled())
    if wake:
        print("Wake gate: CLOSED at startup — microphone audio stays local until 'Hey Jarvis'.")
        print("Typed chat and remote commands should now wake JARVIS automatically.")
    else:
        print("Wake gate: open — JARVIS listens continuously unless another gate is active.")
    if ptt:
        print("Push-to-talk gate: CLOSED unless the configured hotkey is held.")
    else:
        print("Push-to-talk gate: open.")
    print(f"Dashboard LAN exposure: {'on' if get_dashboard_lan_enabled() else 'off'}")

    section("Dashboard ports")
    for port in (8000, 8001):
        print(f"127.0.0.1:{port} is {'free' if _port_free(port) else 'already in use'}")

    section("Audio")
    _audio_probe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
