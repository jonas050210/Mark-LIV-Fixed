"""Regression coverage for OneDrive-aware user folders and desktop app scans."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import user_paths
from core import app_index


class UserPathTests(unittest.TestCase):
    def test_windows_known_folder_is_used_even_when_the_desktop_is_redirected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "jonas"
            redirected = home / "OneDrive" / "Desktop"
            redirected.mkdir(parents=True)
            with patch.object(user_paths.Path, "home", return_value=home), \
                 patch.object(user_paths, "_windows_known_folder", return_value=redirected):
                found = user_paths.locations()
        self.assertEqual(found["desktop"], redirected)
        self.assertEqual(found["documents"], redirected)

    def test_onedrive_environment_fallback_is_used_if_shell_lookup_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "jonas"
            desktop = home / "OneDrive" / "Desktop"
            desktop.mkdir(parents=True)
            with patch.object(user_paths.Path, "home", return_value=home), \
                 patch.object(user_paths, "_windows_known_folder", return_value=None), \
                 patch.dict(user_paths.os.environ, {"OneDrive": str(home / "OneDrive")}, clear=False):
                found = user_paths.locations()
        self.assertEqual(found["desktop"], desktop)

    def test_desktop_candidates_keep_redirected_and_legacy_desktops_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            redirected = root / "OneDrive" / "Desktop"
            legacy = root / "Desktop"
            redirected.mkdir(parents=True)
            legacy.mkdir()
            with patch.object(user_paths.Path, "home", return_value=root), \
                 patch.object(user_paths, "location", return_value=redirected), \
                 patch.object(user_paths, "_onedrive_candidates", return_value=[redirected]):
                candidates = user_paths.desktop_candidates()
        self.assertEqual(candidates, [redirected, legacy])


class DesktopShortcutIndexTests(unittest.TestCase):
    def test_personal_onedrive_desktop_shortcuts_are_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            desktop = Path(directory) / "OneDrive" / "Desktop"
            desktop.mkdir(parents=True)
            editor_link = desktop / "My Editor.lnk"
            pwa_link = desktop / "My Video App.lnk"
            editor_link.write_bytes(b"shortcut")
            pwa_link.write_bytes(b"shortcut")
            executable = Path(directory) / "editor.exe"
            executable.write_bytes(b"binary")

            def parse(path: str):
                if Path(path).name == editor_link.name:
                    return SimpleNamespace(path=str(executable), arguments="")
                return SimpleNamespace(
                    path=str(executable), arguments="--app-id=abcdefghijklmnop"
                )

            fake_module = SimpleNamespace(parse=parse)
            with patch.object(app_index, "desktop_candidates", return_value=[desktop]), \
                 patch.dict(sys.modules, {"pylnk3": fake_module}):
                entries = app_index._windows_desktop_entries()

        found = {entry.name: entry for entry in entries}
        self.assertEqual(found["My Editor"].kind, "exec")
        self.assertEqual(found["My Editor"].source, "desktop")
        self.assertEqual(Path(found["My Editor"].target), executable)
        self.assertEqual(found["My Video App"].kind, "lnk")
        self.assertEqual(found["My Video App"].source, "webapp")


if __name__ == "__main__":
    unittest.main()
