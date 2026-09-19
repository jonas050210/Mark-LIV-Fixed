"""Decoupled voice output and optional local AI fallback.

Covers core/speech.py (engine routing live -> local -> log) and
core/local_ai.py (passive availability, intent/plan helpers that degrade to
None). No audio hardware, no server, no network needed. Runnable with pytest
or directly (`python tests/test_voice_local.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import local_ai as lai  # noqa: E402
from core.speech import (  # noqa: E402
    SpeechRouter,
    VoiceProvider,
    _CommandProvider,
    default_local_provider,
)


class LoudProvider(VoiceProvider):
    name = "test-loud"

    def __init__(self):
        self.said = []

    def speak(self, text: str) -> None:
        self.said.append(text)


class BrokenProvider(VoiceProvider):
    name = "test-broken"

    def speak(self, text: str) -> None:
        raise OSError("no speaker")


# ── speech router ────────────────────────────────────────────────────────────


def test_announce_prefers_live_when_connected():
    r = SpeechRouter()
    said = []
    r.set_live_provider(is_connected=lambda: True, speak=said.append)
    r.set_log(lambda _m: None)
    with patch.object(SpeechRouter, "mode", return_value="auto"):
        assert r.announce("hi", background=False) == "live"
    assert said == ["hi"]


def test_announce_falls_back_to_local_then_log():
    r = SpeechRouter()
    logged = []
    r.set_live_provider(is_connected=lambda: False, speak=lambda _t: None)
    r.set_log(logged.append)
    loud = LoudProvider()
    r.set_local_provider(loud)
    with patch.object(SpeechRouter, "mode", return_value="auto"):
        assert r.announce("timer up", background=False) == "local:test-loud"
    assert loud.said == ["timer up"]
    # Broken local voice degrades to the log, never raises.
    r2 = SpeechRouter()
    r2.set_live_provider(is_connected=lambda: False, speak=lambda _t: None)
    r2.set_log(logged.append)
    r2.set_local_provider(BrokenProvider())
    with patch.object(SpeechRouter, "mode", return_value="auto"):
        assert r2.announce("timer up", background=False) == "log"
    assert any("timer up" in m for m in logged)


def test_announce_modes_pin_engines():
    r = SpeechRouter()
    said = []
    r.set_live_provider(is_connected=lambda: False, speak=said.append)
    r.set_log(lambda _m: None)
    r.set_local_provider(LoudProvider())
    with patch.object(SpeechRouter, "mode", return_value="live"):
        # Live pinned but down: log only, no local voice.
        assert r.announce("x", background=False) == "log"
    with patch.object(SpeechRouter, "mode", return_value="silent"):
        assert r.announce("x", background=False) == "log"
    with patch.object(SpeechRouter, "mode", return_value="local"):
        assert r.announce("x", background=False).startswith("local:")


def test_announce_never_raises_and_skips_blanks():
    r = SpeechRouter()
    with patch.object(SpeechRouter, "mode", side_effect=RuntimeError("cfg")):
        assert r.announce("", background=False) == "log"
    assert r.announce("   ", background=False) == "log"


def test_background_announce_queues_and_speaks():
    import time as _t

    r = SpeechRouter()
    said = []
    r.set_live_provider(is_connected=lambda: True, speak=said.append)
    r.set_log(lambda _m: None)
    with patch.object(SpeechRouter, "mode", return_value="auto"):
        assert r.announce("hello", background=True) == "queued"
        deadline = _t.monotonic() + 5
        while not said and _t.monotonic() < deadline:
            _t.sleep(0.02)
    assert said == ["hello"]


def test_command_provider_resolves_and_argv_is_safe():
    import subprocess as _sp

    prov = _CommandProvider("unit", ["definitely-not-a-binary-xyz"],
                            feed_stdin=True)
    try:
        prov.speak("hi")
    except OSError as e:
        assert "not installed" in str(e)
    else:
        raise AssertionError("expected OSError")
    # With stdin feeding, the text never touches argv (no injection shape).
    seen = {}

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")

        class _R:
            returncode = 0

        return _R()

    prov2 = _CommandProvider("unit", ["echo"], feed_stdin=True)
    with patch.object(_sp, "run", side_effect=_fake_run), \
         patch("shutil.which", return_value="/bin/echo"):
        prov2.speak("hello; rm -rf /")
    assert seen["argv"] == ["/bin/echo"]
    assert seen["input"] == "hello; rm -rf /".encode()


def test_default_local_provider_never_raises():
    prov = default_local_provider()
    assert isinstance(prov, VoiceProvider)
    assert prov.name


# ── local_ai ─────────────────────────────────────────────────────────────────


def test_local_ai_disabled_by_config_short_circuits():
    with patch.object(lai, "is_config_enabled", return_value=False):
        assert lai.is_available() is False
        assert lai.classify_intent("open x", ["open_app"]) is None
        assert lai.plan_steps("do x", ["open_app"]) is None


def test_local_ai_is_available_cached_and_never_raises():
    lai._avail_cache["at"] = 0.0
    with patch.object(lai, "is_config_enabled", return_value=True), \
         patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert lai.is_available() is False
        # Second call uses the cache (no second probe).
        with patch("urllib.request.urlopen",
                   side_effect=AssertionError("probed twice")):
            assert lai.is_available() is False
    lai._avail_cache["at"] = 0.0


def test_classify_intent_grounds_or_discards():
    tools = ["open_app", "web_search"]
    with patch.object(lai, "is_available", return_value=True), \
         patch.object(lai, "_generate", return_value="open_app"):
        assert lai.classify_intent("open Spotify", tools) == "open_app"
    with patch.object(lai, "is_available", return_value=True), \
         patch.object(lai, "_generate", return_value="none"):
        assert lai.classify_intent("open Spotify", tools) is None
    with patch.object(lai, "is_available", return_value=True), \
         patch.object(lai, "_generate", return_value="rm -rf"):
        assert lai.classify_intent("open Spotify", tools) is None
    with patch.object(lai, "is_available", return_value=False):
        assert lai.classify_intent("open Spotify", tools) is None


def test_plan_steps_requires_grounded_json():
    tools = ["open_app", "web_search"]
    good = '[{"tool": "open_app", "params": {"app_name": "Spotify"}}]'
    with patch.object(lai, "is_available", return_value=True), \
         patch.object(lai, "_generate", return_value=good):
        steps = lai.plan_steps("open Spotify", tools)
        assert steps == [{"tool": "open_app",
                          "params": {"app_name": "Spotify"}}]
    for bad in ["not json", '{"tool": 1}', "[]",
                '[{"tool": "evil_tool", "params": {}}]',
                '[{"tool": "open_app", "params": "x"}]']:
        with patch.object(lai, "is_available", return_value=True), \
             patch.object(lai, "_generate", return_value=bad):
            assert lai.plan_steps("goal", tools) is None, bad


def test_generate_never_raises_without_backend():
    # llm_client missing/unreachable -> None, and the availability cache is
    # invalidated so the next probe is real.
    with patch.dict(sys.modules, {"core.llm_client": None}):
        assert lai._generate("p", system="s", timeout=1) is None
    lai._avail_cache["at"] = 0.0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {e!r}")
        else:
            print(f"  [PASS] {fn.__name__}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
