"""Tests for user-facing recovery tools: app diagnosis, aliases and camera setup."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import app_catalog, camera_manager
from core import shortcut_store
from core.app_index import AppEntry


class ApplicationDiagnosisTests(unittest.TestCase):
    def test_diagnosis_explains_onedrive_sources_and_a_matching_entry(self) -> None:
        desktop = Path("C:/Users/jonas/OneDrive/Desktop")
        entry = AppEntry("Spotify", "aumid", "SpotifyAB.Spotify!Spotify", "appsfolder")
        with patch.object(app_catalog, "load_index", return_value=[entry]), \
             patch.object(app_catalog, "locations", return_value={"desktop": desktop}), \
             patch.object(app_catalog, "desktop_candidates", return_value=[desktop]), \
             patch.object(app_catalog.shortcut_store, "all_shortcuts", return_value={}):
            result = app_catalog.app_catalog({"action": "diagnose", "app_name": "Spotify"})
        self.assertIn("OneDrive Desktop: detected", result)
        self.assertIn("appsfolder: 1", result)
        self.assertIn("Spotify [aumid/appsfolder]", result)

    def test_diagnosis_of_a_missing_app_gives_actionable_next_steps(self) -> None:
        desktop = Path("C:/Users/jonas/OneDrive/Desktop")
        with patch.object(app_catalog, "load_index", return_value=[]), \
             patch.object(app_catalog, "locations", return_value={"desktop": desktop}), \
             patch.object(app_catalog, "desktop_candidates", return_value=[desktop]), \
             patch.object(app_catalog.shortcut_store, "all_shortcuts", return_value={}):
            result = app_catalog.app_catalog({"action": "diagnose", "app_name": "My Game"})
        self.assertIn("No indexed match", result)
        self.assertIn("save a custom alias", result)


class ShortcutStoreFriendlinessTests(unittest.TestCase):
    def test_multiword_alias_and_long_onedrive_target_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shortcuts.json"
            long_target = "C:/Users/jonas/OneDrive - School/Desktop/" + "nested/" * 50 + "Code.lnk"
            with patch.object(shortcut_store, "SHORTCUTS_FILE", path):
                shortcut_store.save("  School   Code  ", long_target)
                self.assertEqual(shortcut_store.resolve("school code"), long_target)
                self.assertEqual(shortcut_store.all_shortcuts(), {"school code": long_target})

    def test_aliases_still_refuse_path_like_or_punctuation_input(self) -> None:
        for alias in ("../bad", "open app; shutdown", ""):
            with self.assertRaises(ValueError):
                shortcut_store.save(alias, "Chrome")


class CameraManagerTests(unittest.TestCase):
    def test_status_reports_the_configured_camera_without_opening_a_device(self) -> None:
        with patch.object(camera_manager.screen_processor, "_CV2", True), \
             patch.object(camera_manager.screen_processor, "_NUMPY", True), \
             patch.object(camera_manager, "load_api_keys", return_value={"camera_index": 2}), \
             patch.object(camera_manager.screen_processor, "_probe_camera") as probe:
            result = camera_manager.camera_manager({"action": "status"})
        self.assertIn("Configured camera index: 2", result)
        probe.assert_not_called()

    def test_set_only_persists_a_camera_after_it_produces_a_real_frame(self) -> None:
        with patch.object(camera_manager.screen_processor, "_CV2", True), \
             patch.object(camera_manager.screen_processor, "_NUMPY", True), \
             patch.object(camera_manager, "load_api_keys", return_value={"camera_index": 0}), \
             patch.object(camera_manager.screen_processor, "_cv2_backend", return_value=7), \
             patch.object(camera_manager.screen_processor, "_probe_camera", return_value=True) as probe, \
             patch.object(camera_manager, "patch_config") as save:
            result = camera_manager.camera_manager({"action": "set", "index": 1})
        self.assertIn("now selected", result)
        probe.assert_called_once_with(1, 7, warmup=6)
        save.assert_called_once_with(camera_index=1)

    def test_broken_camera_does_not_replace_a_known_good_selection(self) -> None:
        with patch.object(camera_manager.screen_processor, "_CV2", True), \
             patch.object(camera_manager.screen_processor, "_NUMPY", True), \
             patch.object(camera_manager, "load_api_keys", return_value={"camera_index": 2}), \
             patch.object(camera_manager.screen_processor, "_cv2_backend", return_value=7), \
             patch.object(camera_manager.screen_processor, "_probe_camera", return_value=False), \
             patch.object(camera_manager, "patch_config") as save:
            result = camera_manager.camera_manager({"action": "set", "index": 1})
        self.assertIn("left camera 2 selected", result)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
