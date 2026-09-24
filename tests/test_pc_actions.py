from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import shortcut_store
from actions.media_control import media_control


class PcActionTests(unittest.TestCase):
    def test_shortcuts_are_deterministic_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shortcuts.json"
            with patch.object(shortcut_store, "SHORTCUTS_FILE", path):
                self.assertEqual(shortcut_store.resolve("gd"), "gd")
                self.assertEqual(shortcut_store.save("gd", "Geometry Dash"), "Geometry Dash")
                self.assertEqual(shortcut_store.resolve("GD"), "Geometry Dash")
                self.assertEqual(shortcut_store.all_shortcuts(), {"gd": "Geometry Dash"})
                self.assertTrue(shortcut_store.remove("gd"))
                self.assertEqual(shortcut_store.resolve("gd"), "gd")

    def test_shortcut_rejects_command_injection_characters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(shortcut_store, "SHORTCUTS_FILE", Path(directory) / "shortcuts.json"):
                with self.assertRaises(ValueError):
                    shortcut_store.save("open app; shutdown", "not safe")

    def test_media_does_not_foreground_or_launch_spotify_without_auth(self) -> None:
        with patch("actions.media_control._load_token", return_value={}), \
             patch("actions.media_control._client_id", return_value=""), \
             patch("actions.media_control.webbrowser.open") as open_browser:
            result = media_control({"action": "play", "query": "Numb"})
            self.assertIn("not connected", result)
            open_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
