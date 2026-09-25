"""Tests for the parts of the application index that decide what gets started.

The index is the component with the most authority in the project: whatever it
returns is what gets executed. These tests cover the desktop-entry parser, the
cache lifecycle, and every branch of ``launch`` — in particular the refusals,
because a launch that quietly drops an argument or starts the wrong binary is
worse than one that fails.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core import app_index
from core.app_index import AppEntry, LaunchError


def _entry(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


class DesktopEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        # A real file, addressed absolutely: resolving through PATH would find
        # a different binary on every platform (on Windows "sh" resolves to
        # Git's sh.EXE), which says nothing about the parser.
        self.binary = self.root / "editor-bin"
        self.binary.write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_normal_entry_resolves_to_its_binary(self) -> None:
        path = _entry(self.root / "editor.desktop",
                      f"[Desktop Entry]\nType=Application\nName=Editor\n"
                      f"Exec={self.binary} %U\n")
        entry = app_index._parse_desktop_entry(path)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "Editor")
        self.assertEqual(entry.kind, "exec")
        self.assertEqual(Path(entry.target), self.binary)

    def test_field_codes_are_not_treated_as_arguments(self) -> None:
        path = _entry(self.root / "a.desktop",
                      f"[Desktop Entry]\nType=Application\nName=A\n"
                      f"Exec={self.binary} %f %U %i\n")
        self.assertEqual(Path(app_index._parse_desktop_entry(path).target), self.binary)

    def test_a_hidden_entry_is_skipped(self) -> None:
        path = _entry(self.root / "h.desktop",
                      f"[Desktop Entry]\nType=Application\nName=H\n"
                      f"Exec={self.binary}\nNoDisplay=true\n")
        self.assertIsNone(app_index._parse_desktop_entry(path))

    def test_a_non_application_entry_is_skipped(self) -> None:
        path = _entry(self.root / "l.desktop",
                      f"[Desktop Entry]\nType=Link\nName=L\nExec={self.binary}\n")
        self.assertIsNone(app_index._parse_desktop_entry(path))

    def test_an_entry_whose_binary_does_not_exist_is_skipped(self) -> None:
        path = _entry(self.root / "g.desktop",
                      "[Desktop Entry]\nType=Application\nName=Ghost\nExec=definitely-not-here\n")
        self.assertIsNone(app_index._parse_desktop_entry(path))

    def test_keys_outside_the_desktop_entry_group_are_ignored(self) -> None:
        path = _entry(self.root / "s.desktop",
                      f"[Desktop Action New]\nName=Wrong\nExec={self.binary}\n"
                      f"[Desktop Entry]\nType=Application\nName=Right\nExec={self.binary}\n")
        self.assertEqual(app_index._parse_desktop_entry(path).name, "Right")

    def test_an_absurdly_large_file_is_refused(self) -> None:
        path = _entry(self.root / "big.desktop", "[Desktop Entry]\n" + "#" * 70_000)
        self.assertIsNone(app_index._parse_desktop_entry(path))

    def test_an_unreadable_file_returns_nothing_rather_than_raising(self) -> None:
        self.assertIsNone(app_index._parse_desktop_entry(self.root / "missing.desktop"))

    @unittest.skipIf(os.name == "nt", "XDG_DATA_DIRS is colon-separated; a Windows "
                                      "path contains a colon and this scanner never "
                                      "runs there")
    def test_the_linux_scan_reads_the_xdg_directories(self) -> None:
        apps = self.root / "applications"
        apps.mkdir()
        _entry(apps / "one.desktop",
               f"[Desktop Entry]\nType=Application\nName=One\nExec={self.binary}\n")
        _entry(apps / "two.desktop",
               f"[Desktop Entry]\nType=Application\nName=Two\nExec={self.binary}\n")
        with patch.dict(os.environ, {"XDG_DATA_DIRS": str(self.root)}):
            found = app_index._scan_linux()
        self.assertEqual(sorted(item.name for item in found), ["One", "Two"])


class CacheLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.previous = app_index.INDEX_FILE
        app_index.INDEX_FILE = Path(self.directory.name) / "index.json"
        self.entries = [AppEntry("Editor", "exec", "/usr/bin/editor", "desktop")]

    def tearDown(self) -> None:
        app_index.INDEX_FILE = self.previous
        self.directory.cleanup()

    def test_a_missing_cache_triggers_a_scan(self) -> None:
        with patch.object(app_index, "build_index", return_value=self.entries) as build:
            self.assertEqual(app_index.load_index(), self.entries)
        build.assert_called_once()

    def test_a_fresh_cache_is_used_without_scanning(self) -> None:
        app_index._write_cache(self.entries)
        with patch.object(app_index, "build_index", return_value=[]) as build:
            loaded = app_index.load_index()
        build.assert_not_called()
        self.assertEqual([item.name for item in loaded], ["Editor"])

    def test_refresh_ignores_a_fresh_cache(self) -> None:
        app_index._write_cache(self.entries)
        with patch.object(app_index, "build_index", return_value=[]) as build:
            app_index.load_index(refresh=True)
        build.assert_called_once()

    def test_an_expired_cache_triggers_a_scan(self) -> None:
        app_index._write_cache(self.entries)
        old = time.time() - app_index.CACHE_TTL_SECONDS - 60
        store = app_index._store()
        data = store.read()
        data["built_at"] = old
        store.write(data)
        with patch.object(app_index, "build_index", return_value=self.entries) as build:
            app_index.load_index()
        build.assert_called_once()

    def test_a_changed_source_signature_triggers_a_scan(self) -> None:
        """A newly installed application must show up without waiting a day."""
        app_index._write_cache(self.entries)
        with patch.object(app_index, "_source_signature", return_value=12345.0), \
             patch.object(app_index, "build_index", return_value=self.entries) as build:
            app_index.load_index()
        build.assert_called_once()

    def test_a_cache_from_another_operating_system_is_discarded(self) -> None:
        app_index._write_cache(self.entries)
        store = app_index._store()
        data = store.read()
        data["system"] = "Haiku"
        store.write(data)
        with patch.object(app_index, "build_index", return_value=self.entries) as build:
            app_index.load_index()
        build.assert_called_once()

    def test_an_entry_with_an_unknown_kind_cannot_even_be_written(self) -> None:
        """The validator is the first line of defence: a cache claiming a launch
        kind the launcher does not implement never reaches the disk."""
        from core.json_store import JsonStoreCorruptError

        app_index._write_cache(self.entries)
        store = app_index._store()
        data = store.read()
        data["entries"].append({"name": "Odd", "kind": "telepathy", "target": "x", "source": "y"})
        with self.assertRaises(JsonStoreCorruptError):
            store.write(data)

    def test_a_corrupt_cache_file_is_rebuilt_rather_than_trusted(self) -> None:
        app_index.INDEX_FILE.write_text("{not json at all", encoding="utf-8")
        with patch.object(app_index, "build_index", return_value=self.entries) as build:
            self.assertEqual(app_index.load_index(), self.entries)
        build.assert_called_once()

    def test_an_unsupported_platform_yields_no_index(self) -> None:
        with patch.object(app_index, "_OS", "Haiku"):
            self.assertEqual(app_index.build_index(), [])


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.binary = Path(self.directory.name) / "app"
        self.binary.write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_an_executable_is_spawned_with_its_arguments(self) -> None:
        entry = AppEntry("App", "exec", str(self.binary), "desktop")
        with patch.object(app_index, "_spawn", return_value=4321) as spawn:
            self.assertEqual(app_index.launch(entry, ["report.pdf"]), 4321)
        spawn.assert_called_once_with([str(self.binary), "report.pdf"])

    def test_a_missing_executable_raises_rather_than_reporting_success(self) -> None:
        entry = AppEntry("Gone", "exec", str(self.binary) + ".missing", "desktop")
        with self.assertRaises(LaunchError) as caught:
            app_index.launch(entry)
        self.assertIn("no longer installed", str(caught.exception))

    def test_a_shortcut_refuses_arguments_instead_of_dropping_them(self) -> None:
        entry = AppEntry("Shortcut", "lnk", str(self.binary), "startmenu")
        with self.assertRaises(LaunchError) as caught:
            app_index.launch(entry, ["report.pdf"])
        self.assertIn("cannot", str(caught.exception))

    def test_a_store_application_refuses_arguments(self) -> None:
        entry = AppEntry("Store", "aumid", "Publisher.App_8wek!App", "appsfolder")
        with self.assertRaises(LaunchError):
            app_index.launch(entry, ["report.pdf"])

    def test_a_malformed_application_id_is_refused(self) -> None:
        entry = AppEntry("Store", "aumid", "bad id; rm -rf /", "appsfolder")
        with self.assertRaises(LaunchError) as caught:
            app_index.launch(entry)
        self.assertIn("unusable application id", str(caught.exception))

    def test_a_uri_is_opened_through_the_platform_handler(self) -> None:
        entry = AppEntry("Docs", "uri", "https://example.com", "builtin")
        with patch.object(app_index, "_OS", "Linux"), \
             patch.object(app_index.shutil, "which", return_value="/usr/bin/xdg-open"), \
             patch.object(app_index, "_spawn", return_value=99) as spawn:
            self.assertEqual(app_index.launch(entry), 99)
        spawn.assert_called_once_with(["xdg-open", "https://example.com"])

    def test_a_uri_without_a_handler_raises(self) -> None:
        entry = AppEntry("Docs", "uri", "https://example.com", "builtin")
        with patch.object(app_index, "_OS", "Linux"), \
             patch.object(app_index.shutil, "which", return_value=None):
            with self.assertRaises(LaunchError):
                app_index.launch(entry)

    def test_a_bundle_that_macos_refuses_raises(self) -> None:
        import subprocess

        entry = AppEntry("Safari", "bundle", "/Applications/Safari.app", "bundle")
        failed = subprocess.CompletedProcess(["open"], 1, b"", b"")
        with patch.object(app_index.subprocess, "run", return_value=failed):
            with self.assertRaises(LaunchError):
                app_index.launch(entry)

    def test_an_unknown_kind_raises(self) -> None:
        with self.assertRaises(LaunchError):
            app_index.launch(AppEntry("Odd", "telepathy", "x", "y"))

    def test_launch_uri_wraps_a_bare_address(self) -> None:
        with patch.object(app_index, "launch", return_value=7) as launch:
            self.assertEqual(app_index.launch_uri("ms-settings:display"), 7)
        self.assertEqual(launch.call_args[0][0].kind, "uri")


class DescribeAndResolveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = [
            AppEntry("Visual Studio Code", "exec", "/usr/bin/code", "desktop"),
            AppEntry("Blender", "exec", "/usr/bin/blender", "desktop"),
            AppEntry("Chromium", "exec", "/usr/bin/chromium", "desktop"),
        ]

    def test_resolve_ranks_the_obvious_match_first(self) -> None:
        found = app_index.resolve("blender", entries=self.pool)
        self.assertEqual(found[0].name, "Blender")

    def test_resolve_ignores_filler_words(self) -> None:
        found = app_index.resolve("please open the blender app", entries=self.pool)
        self.assertTrue(found)
        self.assertEqual(found[0].name, "Blender")

    def test_resolve_returns_nothing_for_an_unrelated_request(self) -> None:
        self.assertEqual(app_index.resolve("quantum harmonica", entries=self.pool), [])

    def test_describe_lists_names_for_the_user(self) -> None:
        text = app_index.describe(self.pool, limit=2)
        self.assertIn("Visual Studio Code", text)
        self.assertIn("Blender", text)

    def test_describe_handles_an_empty_pool(self) -> None:
        self.assertIsInstance(app_index.describe([]), str)

    def test_is_uri_accepts_schemes_and_rejects_bare_names(self) -> None:
        self.assertTrue(app_index.is_uri("ms-settings:display"))
        self.assertTrue(app_index.is_uri("https://example.com"))
        self.assertFalse(app_index.is_uri("chrome"))
        self.assertFalse(app_index.is_uri(""))


if __name__ == "__main__":
    unittest.main()
