"""Regression tests for `audio_manager`, in particular the system-wide
default output device actions layered on top of `core.system_audio`.

These are dispatch-level tests: `core.system_audio` has its own test suite
covering the Windows COM fallback and per-platform subprocess behaviour, so
here `audio_manager.system_audio` is mocked directly to verify that the
action routes correctly, reports the underlying result honestly, and wires
undo without silently swallowing a restore failure.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import audio_manager
from core import undo


class ListSystemOutputsTests(unittest.TestCase):
    def test_lists_devices_with_the_current_default_highlighted(self) -> None:
        with patch.object(audio_manager.system_audio, "list_playback_devices",
                           return_value=["JBL Quantum 400", "Realtek Speakers"]), \
             patch.object(audio_manager.system_audio, "get_default_playback_device",
                           return_value="Realtek Speakers"):
            result = audio_manager.audio_manager({"action": "list_system_outputs"})
        self.assertIn("Realtek Speakers", result)
        self.assertIn("JBL Quantum 400", result)

    def test_no_devices_gives_an_explanatory_message_not_an_empty_list(self) -> None:
        with patch.object(audio_manager.system_audio, "list_playback_devices", return_value=[]):
            result = audio_manager.audio_manager({"action": "list_system_outputs"})
        self.assertIn("No system playback devices", result)


class SetSystemOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        undo.clear()

    def tearDown(self) -> None:
        undo.clear()

    def test_missing_device_name_is_refused_before_touching_anything(self) -> None:
        result = audio_manager.audio_manager({"action": "set_system_output"})
        self.assertIn("Tell me", result)

    def test_a_successful_switch_is_reported_and_registers_undo(self) -> None:
        with patch.object(audio_manager.system_audio, "get_default_playback_device",
                           return_value="Realtek Speakers"), \
             patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(True, "System audio output switched to JBL Quantum 400.")):
            result = audio_manager.audio_manager(
                {"action": "set_system_output", "device": "jbl"}
            )
        self.assertIn("JBL Quantum 400", result)
        self.assertIn("system audio output device", undo.peek())

    def test_a_failed_switch_is_reported_honestly_and_registers_no_undo(self) -> None:
        with patch.object(audio_manager.system_audio, "get_default_playback_device",
                           return_value="Realtek Speakers"), \
             patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(False, "Windows refused the default-device change.")):
            result = audio_manager.audio_manager(
                {"action": "set_system_output", "device": "nonexistent"}
            )
        self.assertIn("refused", result)
        self.assertEqual(undo.peek(), "")

    def test_undo_restores_the_previous_system_output(self) -> None:
        with patch.object(audio_manager.system_audio, "get_default_playback_device",
                           return_value="Realtek Speakers"), \
             patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(True, "System audio output switched to JBL Quantum 400.")):
            audio_manager.audio_manager({"action": "set_system_output", "device": "jbl"})

        with patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(True, "System audio output switched to Realtek Speakers.")) as restore:
            outcome = undo.undo_last()
        restore.assert_called_once_with("Realtek Speakers")
        self.assertIn("Realtek Speakers", outcome)

    def test_undo_reports_honestly_when_restoring_fails(self) -> None:
        with patch.object(audio_manager.system_audio, "get_default_playback_device",
                           return_value="Realtek Speakers"), \
             patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(True, "System audio output switched to JBL Quantum 400.")):
            audio_manager.audio_manager({"action": "set_system_output", "device": "jbl"})

        with patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(False, "Device disconnected.")):
            outcome = undo.undo_last()
        self.assertIn("Could not restore", outcome)

    def test_no_previous_default_means_no_undo_is_registered(self) -> None:
        with patch.object(audio_manager.system_audio, "get_default_playback_device",
                           return_value=None), \
             patch.object(audio_manager.system_audio, "set_default_playback_device",
                           return_value=(True, "System audio output switched to JBL Quantum 400.")):
            audio_manager.audio_manager({"action": "set_system_output", "device": "jbl"})
        self.assertEqual(undo.peek(), "")


class MarkLivDeviceSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        undo.clear()

    def tearDown(self) -> None:
        undo.clear()

    def test_unique_partial_name_resolves_to_the_listed_microphone(self) -> None:
        with patch.object(audio_manager, "get_input_device", return_value="Old Mic"), \
             patch.object(audio_manager, "get_output_device", return_value="Old Speakers"), \
             patch.object(audio_manager.audio_devices, "list_devices", return_value=["JBL Quantum 400 Microphone"]), \
             patch.object(audio_manager, "save_input_device") as save:
            result = audio_manager.audio_manager(
                {"action": "set_input", "device": "jbl quantum"}
            )
        save.assert_called_once_with("JBL Quantum 400 Microphone")
        self.assertIn("JBL Quantum 400 Microphone", result)

    def test_unknown_device_is_not_saved_or_reconnected(self) -> None:
        with patch.object(audio_manager, "get_input_device", return_value="Old Mic"), \
             patch.object(audio_manager, "get_output_device", return_value="Old Speakers"), \
             patch.object(audio_manager.audio_devices, "list_devices", return_value=["JBL Quantum 400 Microphone"]), \
             patch.object(audio_manager, "save_input_device") as save:
            result = audio_manager.audio_manager(
                {"action": "set_input", "device": "nonexistent microphone"}
            )
        self.assertIn("could not find a selectable microphone", result.casefold())
        save.assert_not_called()
        self.assertEqual(undo.peek(), "")

    def test_ambiguous_device_name_requires_an_exact_choice(self) -> None:
        with patch.object(audio_manager, "get_input_device", return_value="Old Mic"), \
             patch.object(audio_manager, "get_output_device", return_value="Old Speakers"), \
             patch.object(
                 audio_manager.audio_devices,
                 "list_devices",
                 return_value=["JBL Quantum 400 Microphone", "JBL Webcam Microphone"],
             ), patch.object(audio_manager, "save_input_device") as save:
            result = audio_manager.audio_manager({"action": "set_input", "device": "jbl"})
        self.assertIn("matches more than one microphone", result)
        save.assert_not_called()


class UnknownActionTests(unittest.TestCase):
    def test_an_unrecognised_action_lists_every_valid_one(self) -> None:
        result = audio_manager.audio_manager({"action": "does_not_exist"})
        for action in ("list", "set_input", "set_output", "default",
                       "list_system_outputs", "set_system_output",
                       "list_app_volumes", "set_app_volume"):
            self.assertIn(action, result)


# ── Per-app volume (Windows volume mixer) ────────────────────────────────────

class _FakeVolume:
    def __init__(self, level: float = 0.8, muted: bool = False):
        self.level = float(level)
        self.muted = bool(muted)
        self.calls: list[tuple] = []

    def GetMasterVolume(self) -> float:
        return self.level

    def SetMasterVolume(self, level, ctx=None) -> None:
        self.level = float(level)
        self.calls.append(("set", float(level)))

    def GetMute(self) -> bool:
        return self.muted

    def SetMute(self, muted, ctx=None) -> None:
        self.muted = bool(muted)
        self.calls.append(("mute", bool(muted)))


class _FakeProcess:
    def __init__(self, name: str, pid: int):
        self._name = name
        self.pid = pid

    def name(self) -> str:
        return self._name


class _FakeSession:
    def __init__(self, name: str, pid: int, level: float = 0.8, muted: bool = False):
        self.Process = _FakeProcess(name, pid)
        self.SimpleAudioVolume = _FakeVolume(level, muted)


def _session_dict(session: _FakeSession) -> dict:
    """The dict audio_manager builds from a raw pycaw session, for tests that
    patch _app_sessions directly instead of going through the fake module."""
    control = session.SimpleAudioVolume
    name = session.Process._name
    return {
        "name": name,
        "stem": audio_manager._norm_name(name.rsplit(".", 1)[0]),
        "pid": session.Process.pid,
        "level": control.level,
        "muted": control.muted,
        "control": control,
    }


class SessionReadingTests(unittest.TestCase):
    def test_sessions_are_read_with_name_level_and_mute(self) -> None:
        import sys
        import types

        raw = [
            _FakeSession("spotify.exe", 101, level=0.8),
            _FakeSession("chrome.exe", 102, level=0.4, muted=True),
        ]
        utilities = types.SimpleNamespace(GetAllSessions=lambda: raw)
        fake_inner = types.ModuleType("pycaw.pycaw")
        fake_inner.AudioUtilities = utilities
        fake_outer = types.ModuleType("pycaw")
        fake_outer.pycaw = fake_inner

        with patch.dict(sys.modules, {"pycaw": fake_outer, "pycaw.pycaw": fake_inner}):
            sessions = audio_manager._app_sessions()

        self.assertIsNotNone(sessions)
        self.assertEqual([s["stem"] for s in sessions], ["spotify", "chrome"])
        self.assertEqual(sessions[0]["level"], 0.8)
        self.assertTrue(sessions[1]["muted"])
        self.assertEqual(sessions[0]["control"].level, 0.8)

    def test_missing_pycaw_reports_unavailable_not_empty(self) -> None:
        import sys

        with patch.dict(sys.modules, {"pycaw": None, "pycaw.pycaw": None}):
            self.assertIsNone(audio_manager._app_sessions())
        result = audio_manager.audio_manager({"action": "list_app_volumes"})
        self.assertIn("unavailable", result)
        self.assertIn("pycaw", result)


class SetAppVolumeTests(unittest.TestCase):
    def setUp(self) -> None:
        undo.clear()
        self.spotify = _FakeSession("spotify.exe", 101, level=0.8)

    def tearDown(self) -> None:
        undo.clear()

    def _run(self, parameters: dict, sessions=None):
        with patch.object(audio_manager, "_app_sessions",
                          return_value=sessions if sessions is not None
                          else [_session_dict(self.spotify)]):
            return audio_manager.audio_manager(parameters)

    def test_a_percentage_sets_that_level_and_registers_undo(self) -> None:
        result = self._run({"action": "set_app_volume", "app": "Spotify", "value": "50"})
        self.assertIn("50%", result)
        self.assertAlmostEqual(self.spotify.SimpleAudioVolume.level, 0.5)
        self.assertEqual(undo.history(), ["changed Spotify's volume"])

    def test_undo_restores_the_previous_level(self) -> None:
        self._run({"action": "set_app_volume", "app": "Spotify", "value": "20"})
        self.assertAlmostEqual(self.spotify.SimpleAudioVolume.level, 0.2)
        with patch.object(audio_manager, "_app_sessions",
                          return_value=[_session_dict(self.spotify)]):
            undo.undo_last()
        self.assertAlmostEqual(self.spotify.SimpleAudioVolume.level, 0.8)

    def test_up_and_down_clamp_at_the_ends(self) -> None:
        loud = _FakeSession("spotify.exe", 101, level=0.95)
        self._run({"action": "set_app_volume", "app": "Spotify", "value": "up"},
                  sessions=[_session_dict(loud)])
        self.assertAlmostEqual(loud.SimpleAudioVolume.level, 1.0)

        quiet = _FakeSession("spotify.exe", 102, level=0.05)
        self._run({"action": "set_app_volume", "app": "Spotify", "value": "down"},
                  sessions=[_session_dict(quiet)])
        self.assertAlmostEqual(quiet.SimpleAudioVolume.level, 0.0)

    def test_mute_and_unmute(self) -> None:
        result = self._run({"action": "set_app_volume", "app": "Spotify", "value": "mute"})
        self.assertIn("Muted Spotify", result)
        self.assertTrue(self.spotify.SimpleAudioVolume.muted)
        result = self._run({"action": "set_app_volume", "app": "Spotify", "value": "unmute"})
        self.assertIn("Unmuted Spotify", result)
        self.assertFalse(self.spotify.SimpleAudioVolume.muted)

    def test_every_session_of_one_app_is_adjusted(self) -> None:
        first = _FakeSession("chrome.exe", 201, level=0.3)
        second = _FakeSession("chrome.exe", 202, level=0.6)
        result = self._run(
            {"action": "set_app_volume", "app": "Chrome", "value": "40"},
            sessions=[_session_dict(first), _session_dict(second)],
        )
        self.assertIn("2 sessions", result)
        self.assertAlmostEqual(first.SimpleAudioVolume.level, 0.4)
        self.assertAlmostEqual(second.SimpleAudioVolume.level, 0.4)

    def test_unknown_app_lists_what_is_playing(self) -> None:
        result = self._run({"action": "set_app_volume", "app": "Discord", "value": "50"})
        self.assertIn("No audio session matches 'Discord'", result)
        self.assertIn("spotify", result)

    def test_vague_directive_is_rejected_without_touching_anything(self) -> None:
        result = self._run({"action": "set_app_volume", "app": "Spotify", "value": "banana"})
        self.assertIn("Tell me the volume", result)
        self.assertAlmostEqual(self.spotify.SimpleAudioVolume.level, 0.8)
        self.assertEqual(undo.history(), [])

    def test_no_app_named_is_rejected(self) -> None:
        result = self._run({"action": "set_app_volume", "value": "50"})
        self.assertIn("Tell me which application", result)

    def test_nothing_playing_is_distinguished_from_unavailable(self) -> None:
        result = self._run({"action": "set_app_volume", "app": "Spotify", "value": "50"},
                           sessions=[])
        self.assertIn("No applications currently have an audio session", result)

    def test_list_groups_repeated_stems(self) -> None:
        first = _FakeSession("chrome.exe", 201, level=0.3)
        second = _FakeSession("chrome.exe", 202, level=0.6, muted=True)
        result = self._run(
            {"action": "list_app_volumes"},
            sessions=[_session_dict(first), _session_dict(second)],
        )
        self.assertIn("chrome (x2)", result)
        self.assertIn("30%", result)
        self.assertIn("muted", result)


if __name__ == "__main__":
    unittest.main()
