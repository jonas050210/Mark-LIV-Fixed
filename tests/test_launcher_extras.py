"""Tests for pins/recents, launch arguments, icons, and window layouts."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import app_icons, app_index
from core.app_index import AppEntry
from core.window_manager import MonitorInfo, WindowInfo
from actions import layout_manager, open_app


def _entry(name: str, target: str = "/usr/bin/app", kind: str = "exec") -> AppEntry:
    return AppEntry(name=name, kind=kind, target=target, source="registry")


class ArgumentSafetyTests(unittest.TestCase):
    """Arguments let 'open Chrome with youtube.com' work without a shell."""

    def test_a_url_is_accepted(self) -> None:
        self.assertEqual(
            app_index.sanitise_arguments("https://youtube.com"),
            ["https://youtube.com"],
        )

    def test_switches_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            app_index.sanitise_arguments(["--remote-debugging-port=9222"])

    def test_control_characters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            app_index.sanitise_arguments(["file\x00name"])

    def test_the_argument_count_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            app_index.sanitise_arguments([f"file{index}" for index in range(20)])

    def test_open_app_reports_a_rejected_argument_without_launching(self) -> None:
        with patch.object(open_app, "_launch_resolved") as launch:
            result = open_app.open_app({"app_name": "Chrome", "arguments": ["--incognito"]})
        self.assertIn("cannot pass that", result)
        launch.assert_not_called()

    def test_a_shortcut_cannot_silently_drop_arguments(self) -> None:
        entry = _entry("Chrome", "/tmp/chrome.lnk", kind="lnk")
        with patch("pathlib.Path.is_file", return_value=True):
            with self.assertRaises(app_index.LaunchError) as caught:
                app_index.launch(entry, ["https://example.com"])
        self.assertIn("cannot receive arguments", str(caught.exception))


class UsageStoreTests(unittest.TestCase):
    """Pinned and recent lists, persisted through the validated JSON store."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self._patch = patch.object(
            app_index, "USAGE_FILE", Path(self._temp.name) / "app_usage.json"
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._temp.cleanup()

    def test_recent_launches_are_most_recent_first_and_deduplicated(self) -> None:
        for name in ("Chrome", "Discord", "Chrome"):
            app_index.record_launch(name)
        usage = app_index.read_usage()
        self.assertEqual(usage["recent"][:2], ["Chrome", "Discord"])
        self.assertEqual(usage["counts"]["Chrome"], 2)

    def test_recent_list_is_capped(self) -> None:
        for index in range(app_index.MAX_RECENT + 10):
            app_index.record_launch(f"App {index}")
        self.assertEqual(len(app_index.read_usage()["recent"]), app_index.MAX_RECENT)

    def test_pinning_is_idempotent_and_reversible(self) -> None:
        app_index.set_pinned("Chrome", True)
        app_index.set_pinned("Chrome", True)
        self.assertEqual(app_index.read_usage()["pinned"], ["Chrome"])
        app_index.set_pinned("Chrome", False)
        self.assertEqual(app_index.read_usage()["pinned"], [])

    def test_pin_count_is_bounded(self) -> None:
        for index in range(app_index.MAX_PINNED):
            app_index.set_pinned(f"App {index}", True)
        with self.assertRaises(ValueError):
            app_index.set_pinned("One too many", True)

    def test_quick_list_drops_uninstalled_entries(self) -> None:
        app_index.set_pinned("Chrome", True)
        app_index.record_launch("Removed App")
        pool = [_entry("Chrome")]
        quick = app_index.quick_list(entries=pool)
        self.assertEqual([item.name for item in quick["pinned"]], ["Chrome"])
        self.assertEqual(quick["recent"], [])

    def test_a_pinned_app_is_not_repeated_in_recent(self) -> None:
        app_index.set_pinned("Chrome", True)
        app_index.record_launch("Chrome")
        quick = app_index.quick_list(entries=[_entry("Chrome")])
        self.assertEqual(len(quick["pinned"]), 1)
        self.assertEqual(quick["recent"], [])


class StalenessTests(unittest.TestCase):
    def test_a_changed_source_folder_invalidates_the_cache(self) -> None:
        cache = {"version": app_index.INDEX_VERSION,
                 "system": app_index._OS, "built_at": 9e9, "signature": -12345.0,
                 "entries": [{"name": "Old", "kind": "exec", "target": "/bin/old", "source": "registry"}]}
        with patch.object(app_index, "_read_cache", return_value=cache), \
             patch.object(app_index, "build_index", return_value=[_entry("New")]) as build:
            entries = app_index.load_index()
        build.assert_called_once()
        self.assertEqual([item.name for item in entries], ["New"])

    def test_a_fresh_matching_cache_is_reused(self) -> None:
        cache = {
            "version": app_index.INDEX_VERSION,
            "system": app_index._OS,
            "built_at": app_index.time.time(),
            "signature": app_index._source_signature(),
            "entries": [{"name": "Cached", "kind": "exec", "target": "/bin/x", "source": "registry"}],
        }
        with patch.object(app_index, "_read_cache", return_value=cache), \
             patch.object(app_index, "build_index") as build:
            entries = app_index.load_index()
        build.assert_not_called()
        self.assertEqual([item.name for item in entries], ["Cached"])


class IconTests(unittest.TestCase):
    def test_a_failed_extraction_never_raises(self) -> None:
        with patch.object(app_icons, "_extract", side_effect=RuntimeError("boom")):
            self.assertIsNone(app_icons.icon_png(_entry("Broken")))

    def test_the_cache_key_follows_the_target(self) -> None:
        first = app_icons._cache_path(_entry("Chrome", "/opt/a/chrome"))
        second = app_icons._cache_path(_entry("Chrome", "/opt/b/chrome"))
        self.assertNotEqual(first.name, second.name)

    def test_clear_cache_only_removes_generated_files(self) -> None:
        with TemporaryDirectory() as temp:
            directory = Path(temp)
            keep = directory / "notes.txt"
            keep.write_text("keep me", encoding="utf-8")
            (directory / ("a" * 32 + ".png")).write_bytes(b"x")
            with patch.object(app_icons, "ICON_DIR", directory):
                removed = app_icons.clear_cache()
            self.assertEqual(removed, 1)
            self.assertTrue(keep.exists())


_MONITORS = [
    MonitorInfo(1, "primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, primary=True),
    MonitorInfo(2, "secondary", 1920, 0, 3840, 1080, 1920, 0, 3840, 1040),
]


def _window(handle: int, title: str, process: str, left: int = 0) -> WindowInfo:
    return WindowInfo(handle=handle, title=title, process=process, pid=handle,
                      left=left, top=0, right=left + 800, bottom=600)


class LayoutManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self._patch = patch.object(
            layout_manager, "LAYOUT_FILE", Path(self._temp.name) / "layouts.json"
        )
        self._patch.start()
        self._windows = [
            _window(1, "Editor — main.py", "code"),
            _window(2, "Discord", "discord", left=1920),
        ]

    def tearDown(self) -> None:
        self._patch.stop()
        self._temp.cleanup()

    def _desktop(self):
        return patch.multiple(
            layout_manager,
            list_windows=lambda: self._windows,
            list_monitors=lambda: _MONITORS,
            monitor_of=lambda window: _MONITORS[1] if window.left >= 1920 else _MONITORS[0],
        )

    def test_save_then_list_reports_the_layout(self) -> None:
        with self._desktop():
            saved = layout_manager.layout_manager({"action": "save", "name": "work"})
            listed = layout_manager.layout_manager({"action": "list"})
        self.assertIn("2 windows", saved)
        self.assertIn("work", listed)

    def test_shell_windows_are_not_captured(self) -> None:
        self._windows.append(_window(3, "Program Manager", "explorer.exe"))
        with self._desktop():
            layout_manager.layout_manager({"action": "save", "name": "work"})
            data = layout_manager._read()
        processes = {row["process"] for row in data["layouts"]["work"]["windows"]}
        self.assertNotIn("explorer.exe", processes)

    def test_an_invalid_name_is_refused(self) -> None:
        with self._desktop():
            result = layout_manager.layout_manager({"action": "save", "name": "../etc"})
        self.assertIn("Layout names may contain", result)

    def test_applying_an_unknown_layout_lists_what_exists(self) -> None:
        with self._desktop():
            layout_manager.layout_manager({"action": "save", "name": "work"})
            result = layout_manager.layout_manager({"action": "apply", "name": "gaming"})
        self.assertIn("no layout called 'gaming'", result)
        self.assertIn("work", result)

    def test_missing_windows_are_reported_not_launched(self) -> None:
        with self._desktop():
            layout_manager.layout_manager({"action": "save", "name": "work"})
        with patch.multiple(
            layout_manager,
            list_windows=lambda: [self._windows[0]],
            list_monitors=lambda: _MONITORS,
            monitor_of=lambda window: _MONITORS[0],
        ), patch.object(layout_manager, "place_window"), \
             patch.object(layout_manager, "operate"), \
             patch.object(layout_manager, "monitor_for", return_value=_MONITORS[0]), \
             patch.object(layout_manager, "_match_window",
                          side_effect=[self._windows[0], None]):
            result = layout_manager.layout_manager({"action": "apply", "name": "work"})
        self.assertIn("Applied layout 'work' to 1 windows", result)
        self.assertIn("Not open", result)

    def test_a_window_whose_saved_monitor_is_gone_is_recentred_not_stale(self) -> None:
        """If a monitor was unplugged since a layout was saved, a window whose
        saved monitor index no longer exists must not be moved to its stale
        absolute coordinates -- which could land it off the one remaining
        monitor entirely -- but re-centred on monitor 1 instead."""
        with self._desktop():
            layout_manager.layout_manager({"action": "save", "name": "work"})
        recentred = []
        with patch.multiple(
            layout_manager,
            list_windows=lambda: self._windows,
            list_monitors=lambda: _MONITORS[:1],  # the second monitor is gone
            monitor_of=lambda window: _MONITORS[0],
        ), patch.object(layout_manager, "operate"), \
             patch.object(layout_manager, "monitor_for", return_value=_MONITORS[0]), \
             patch.object(
                 layout_manager, "move_to_monitor",
                 side_effect=lambda window, monitor: recentred.append(window.handle),
             ), \
             patch.object(layout_manager, "_match_window",
                          side_effect=[self._windows[0], self._windows[1]]):
            result = layout_manager.layout_manager({"action": "apply", "name": "work"})
        self.assertEqual(
            recentred, [2],
            "the window saved on the now-missing second monitor should be "
            "re-centred on monitor 1, and the other window (saved on the "
            "still-present monitor 1) should not be touched by that fallback",
        )
        self.assertIn("Applied layout 'work' to 2 windows", result)

    def test_delete_is_undoable(self) -> None:
        with self._desktop():
            layout_manager.layout_manager({"action": "save", "name": "work"})
        pushed = []
        with patch.object(layout_manager, "push_undo", lambda label, fn: pushed.append((label, fn))):
            layout_manager.layout_manager({"action": "delete", "name": "work"})
        self.assertIn("deleting window layout 'work'", pushed[0][0])
        self.assertEqual(layout_manager._read()["layouts"], {})
        pushed[0][1]()
        self.assertIn("work", layout_manager._read()["layouts"])

    def test_layout_count_is_bounded(self) -> None:
        with self._desktop():
            for index in range(layout_manager.MAX_LAYOUTS):
                layout_manager.layout_manager({"action": "save", "name": f"layout{index}"})
            result = layout_manager.layout_manager({"action": "save", "name": "extra"})
        self.assertIn("Delete one first", result)


class LaunchUndoTests(unittest.TestCase):
    """Opening an application should be reversible like other actions."""

    def test_a_launch_registers_an_undo_entry(self) -> None:
        pushed = []
        opened = _window(7, "Chrome", "chrome")
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=opened), \
             patch.object(open_app, "record_launch"), \
             patch("core.undo.push_undo", lambda label, fn: pushed.append((label, fn))):
            open_app.open_app({"app_name": "Chrome"})
        self.assertEqual(pushed[0][0], "opening Chrome")

    def test_the_undo_refuses_when_the_window_is_already_gone(self) -> None:
        from core.undo import UndoRefused

        pushed = []
        opened = _window(7, "Chrome", "chrome")
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=opened), \
             patch.object(open_app, "record_launch"), \
             patch("core.undo.push_undo", lambda label, fn: pushed.append((label, fn))):
            open_app.open_app({"app_name": "Chrome"})
        with patch("core.window_manager.list_windows", return_value=[]):
            with self.assertRaises(UndoRefused):
                pushed[0][1]()

    def test_the_undo_closes_the_window_it_opened(self) -> None:
        pushed = []
        opened = _window(7, "Chrome", "chrome")
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=opened), \
             patch.object(open_app, "record_launch"), \
             patch("core.undo.push_undo", lambda label, fn: pushed.append((label, fn))):
            open_app.open_app({"app_name": "Chrome"})
        with patch("core.window_manager.list_windows", return_value=[opened]), \
             patch("core.window_manager.operate") as operate:
            message = pushed[0][1]()
        operate.assert_called_once_with(opened, "close")
        self.assertIn("Closed Chrome", message)


if __name__ == "__main__":
    unittest.main()
