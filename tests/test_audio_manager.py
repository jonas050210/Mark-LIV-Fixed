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
                       "list_system_outputs", "set_system_output"):
            self.assertIn(action, result)


if __name__ == "__main__":
    unittest.main()
