"""Tests for the reduced action surface and the optional matching backends.

Five sub-actions were removed because they either invented data or duplicated a
tool that does the same job correctly:

* ``web_search`` ``price`` and ``compare`` answered from the model rather than
  from search results;
* ``computer_control`` ``screen_find`` and ``screen_click`` clicked pixel
  coordinates guessed from a screenshot;
* ``computer_control`` ``focus_window`` duplicated ``window_manager``;
* ``file_controller`` ``organize_desktop`` bulk-moved every desktop file.

Each one must be gone from the schema and must answer with a pointer to the
correct tool rather than a bare "unknown action".
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core import text_match
from core.app_index import AppEntry
from core.window_manager import WindowInfo, _score_window
from actions import computer_control, file_controller, web_search


def _modes(tool: dict, key: str = "action") -> list[str]:
    return list(tool["parameters"]["properties"][key].get("enum", []))


class RemovedActionTests(unittest.TestCase):
    def test_web_search_no_longer_advertises_price_or_compare(self) -> None:
        modes = _modes(web_search.TOOL, "mode")
        self.assertEqual(sorted(modes), ["news", "research", "search"])
        self.assertNotIn("items", web_search.TOOL["parameters"]["properties"])
        self.assertNotIn("aspect", web_search.TOOL["parameters"]["properties"])

    def test_web_search_implementations_are_gone(self) -> None:
        self.assertFalse(hasattr(web_search, "_price"))
        self.assertFalse(hasattr(web_search, "_compare"))

    def test_an_unknown_web_search_mode_degrades_to_a_plain_search(self) -> None:
        with patch.object(web_search, "_search", return_value="results") as search:
            result = web_search.web_search({"query": "gpu prices", "mode": "price"})
        search.assert_called_once_with("gpu prices")
        self.assertEqual(result, "results")

    def test_computer_control_no_longer_advertises_screen_or_focus_actions(self) -> None:
        actions = _modes(computer_control.TOOL)
        for removed in ("screen_find", "screen_click", "focus_window"):
            self.assertNotIn(removed, actions)

    def test_removed_computer_control_actions_name_the_right_tool(self) -> None:
        for removed in ("screen_find", "screen_click", "focus_window"):
            result = computer_control.computer_control({"action": removed})
            self.assertIn("was removed", result, removed)
            self.assertIn("window_manager", result, removed)

    def test_the_ai_screen_finder_implementation_is_gone(self) -> None:
        self.assertFalse(hasattr(computer_control, "_screen_find"))
        self.assertFalse(hasattr(computer_control, "_focus_window"))

    def test_organize_desktop_is_removed_and_no_longer_confirmed(self) -> None:
        self.assertNotIn("organize_desktop", _modes(file_controller.TOOL))
        self.assertEqual(file_controller.TOOL["confirmation_actions"], ["delete", "write"])
        self.assertFalse(hasattr(file_controller, "organize_desktop"))

    def test_organize_desktop_explains_itself_instead_of_moving_files(self) -> None:
        result = file_controller.file_controller({"action": "organize_desktop"})
        self.assertIn("was removed", result)


class TextMatchTests(unittest.TestCase):
    """The matcher must behave the same with or without RapidFuzz."""

    def test_identical_strings_score_one(self) -> None:
        self.assertEqual(text_match.ratio("chrome", "chrome"), 1.0)

    def test_empty_input_scores_zero(self) -> None:
        self.assertEqual(text_match.ratio("", "chrome"), 0.0)
        self.assertEqual(text_match.partial_ratio("chrome", ""), 0.0)

    def test_unrelated_strings_score_low(self) -> None:
        self.assertLess(text_match.ratio("chrome", "blender"), 0.5)

    def test_partial_ratio_tolerates_a_longer_container(self) -> None:
        score = text_match.partial_ratio("chrome", "inbox 12 gmail google chrome")
        self.assertGreater(score, 0.9)

    def test_comparison_input_is_bounded(self) -> None:
        self.assertIsInstance(text_match.ratio("a" * 10_000, "a" * 10_000), float)

    def test_best_match_honours_the_minimum(self) -> None:
        pool = [AppEntry("Blender", "exec", "/x"), AppEntry("Chrome", "exec", "/y")]
        found = text_match.best_match("chrome", pool, key=lambda item: item.name, minimum=0.6)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Chrome")
        self.assertIsNone(
            text_match.best_match("nothing alike", pool, key=lambda item: item.name, minimum=0.9)
        )

    def test_the_backend_is_one_of_the_two_supported_names(self) -> None:
        self.assertIn(text_match.BACKEND, {"rapidfuzz", "difflib"})


class WindowScoringTests(unittest.TestCase):
    def _window(self, title: str, process: str = "") -> WindowInfo:
        return WindowInfo(handle=1, title=title, process=process, pid=1,
                          left=0, top=0, right=800, bottom=600)

    def test_an_exact_process_match_outranks_a_noisy_title(self) -> None:
        exact = _score_window(self._window("Something", "chrome"), "chrome")[0]
        noisy = _score_window(self._window("Inbox (12) - Gmail - Google Chrome"), "chrome")[0]
        self.assertGreater(exact, noisy)

    def test_a_noisy_title_still_matches_through_partial_similarity(self) -> None:
        score = _score_window(self._window("Inbox (12) - Gmail - Google Chrome"), "chrome")[0]
        self.assertGreater(score, 0)

    def test_an_unrelated_window_scores_zero(self) -> None:
        self.assertEqual(_score_window(self._window("Blender", "blender"), "chrome")[0], 0)


class WindowBackendTests(unittest.TestCase):
    def test_backend_name_never_raises_without_a_desktop(self) -> None:
        from core import window_manager

        self.assertIsInstance(window_manager.backend_name(), str)

    def test_operate_explains_a_missing_desktop_api(self) -> None:
        from core import window_manager

        if window_manager._OS == "Windows":
            self.skipTest("Windows uses the native user32 path")
        with patch.object(window_manager, "desktop_backend", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                window_manager.operate(
                    WindowInfo(1, "x", "y", 1, 0, 0, 10, 10), "focus"
                )
        self.assertIn("pywinctl", str(caught.exception))


class ShortcutResolutionTests(unittest.TestCase):
    def test_an_unreadable_shortcut_resolves_to_nothing_without_raising(self) -> None:
        from pathlib import Path

        from core import app_index

        self.assertEqual(
            app_index._resolve_shortcut_target(Path("/nonexistent/app.lnk")),
            ("", ""),
        )

    def test_a_resolved_shortcut_outranks_an_appsfolder_duplicate(self) -> None:
        from core import app_index

        resolved = AppEntry("Spotify", "exec", r"C:\\Spotify\\Spotify.exe", "startmenu")
        store = AppEntry("Spotify", "aumid", "Spotify!App", "appsfolder")
        winner = app_index._deduplicate([store, resolved])
        self.assertEqual(winner[0].kind, "exec")


if __name__ == "__main__":
    unittest.main()


class HandleStabilityTests(unittest.TestCase):
    """A handle that changes between two enumerations is not a handle.

    Only Windows hands out a real HWND. On every other platform the window
    object is a fresh wrapper each time the desktop is enumerated, so deriving
    the handle from the object's identity would break ``refresh_window`` and,
    through it, every placement verification.
    """

    class _FakeWindow:
        def __init__(self, title: str, pid: int) -> None:
            self.title = title
            self._pid = pid
            self.left = self.top = 0
            self.right, self.bottom = 800, 600
            self.isMinimized = self.isMaximized = False

        def getPID(self) -> int:
            return self._pid

    def test_the_same_window_yields_the_same_handle_twice(self) -> None:
        from core import window_manager

        first = window_manager._stable_handle(self._FakeWindow("Editor", 42), "Editor", 42)
        second = window_manager._stable_handle(self._FakeWindow("Editor", 42), "Editor", 42)
        self.assertEqual(first, second)
        self.assertNotEqual(first, 0)

    def test_different_windows_yield_different_handles(self) -> None:
        from core import window_manager

        self.assertNotEqual(
            window_manager._stable_handle(self._FakeWindow("Editor", 42), "Editor", 42),
            window_manager._stable_handle(self._FakeWindow("Editor", 43), "Editor", 43),
        )

    def test_a_native_handle_always_wins(self) -> None:
        from core import window_manager

        window = self._FakeWindow("Editor", 42)
        window._hWnd = 12345
        self.assertEqual(window_manager._stable_handle(window, "Editor", 42), 12345)

    def test_refresh_falls_back_to_pid_and_title(self) -> None:
        from core import window_manager

        live = WindowInfo(999, "Editor", "code", 42, 0, 0, 800, 600)
        stale = WindowInfo(111, "Editor", "code", 42, 0, 0, 100, 100)
        with patch.object(window_manager, "list_windows", return_value=[live]):
            self.assertEqual(window_manager.refresh_window(stale), live)


class WindowsSwitchArgumentTests(unittest.TestCase):
    """On Windows the switch character is the slash, not the dash."""

    def test_a_slash_switch_is_refused_on_windows(self) -> None:
        from core import app_index

        with patch.object(app_index, "_OS", "Windows"):
            for switch in ("/s", "/Q", "/delete"):
                with self.assertRaises(ValueError, msg=switch):
                    app_index.sanitise_arguments(switch)

    def test_an_existing_posix_path_still_passes_on_windows(self) -> None:
        import tempfile

        from core import app_index

        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            with patch.object(app_index, "_OS", "Windows"):
                self.assertEqual(
                    app_index.sanitise_arguments(handle.name), [handle.name]
                )

    def test_a_dash_switch_is_refused_everywhere(self) -> None:
        from core import app_index

        with self.assertRaises(ValueError):
            app_index.sanitise_arguments("--profile-directory=Default")


class UsageCountTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        from core import app_index

        self.app_index = app_index
        self.previous = app_index.USAGE_FILE
        self.directory = tempfile.TemporaryDirectory()
        app_index.USAGE_FILE = __import__("pathlib").Path(self.directory.name) / "usage.json"

    def tearDown(self) -> None:
        self.app_index.USAGE_FILE = self.previous
        self.directory.cleanup()

    def test_a_name_is_counted_once_regardless_of_case(self) -> None:
        self.app_index.record_launch("Chrome")
        self.app_index.record_launch("chrome")
        counts = self.app_index.read_usage()["counts"]
        self.assertEqual(counts, {"Chrome": 2})

    def test_a_full_count_table_still_records_a_new_launch(self) -> None:
        for index in range(self.app_index.MAX_USAGE_ENTRIES + 20):
            self.app_index.record_launch(f"app{index}")
        counts = self.app_index.read_usage()["counts"]
        self.assertLessEqual(len(counts), self.app_index.MAX_USAGE_ENTRIES)
        # The most recent launch must survive the cut, otherwise the table
        # freezes at its first two hundred entries forever.
        newest = f"app{self.app_index.MAX_USAGE_ENTRIES + 19}"
        self.assertIn(newest, counts)


class LayoutMatchingTests(unittest.TestCase):
    """Two windows of one application must not collapse onto the same window."""

    def _setup(self):
        import tempfile
        from pathlib import Path as _Path

        from actions import layout_manager
        from core.window_manager import MonitorInfo

        directory = tempfile.TemporaryDirectory()
        layout_manager.LAYOUT_FILE = _Path(directory.name) / "layouts.json"
        monitors = [MonitorInfo(0, "HDMI", 0, 0, 1920, 1080, 0, 0, 1920, 1040, 60, True)]
        windows = [
            WindowInfo(1, "Report - Word", "winword.exe", 10, 0, 0, 800, 600),
            WindowInfo(2, "Notes - Word", "winword.exe", 11, 100, 100, 900, 700),
        ]
        return layout_manager, monitors, windows, directory

    def test_applying_a_layout_places_each_window_once(self) -> None:
        layout_manager, monitors, windows, directory = self._setup()
        touched = []
        try:
            with patch.multiple(
                layout_manager,
                list_windows=lambda: windows,
                list_monitors=lambda: monitors,
                monitor_of=lambda window: monitors[0],
                place_window=lambda *a, **k: True,
                operate=lambda window, op, *a: touched.append(window.handle),
            ):
                layout_manager.layout_manager({"action": "save", "name": "work"})
                result = layout_manager.layout_manager({"action": "apply", "name": "work"})
        finally:
            directory.cleanup()
        self.assertIn("2 windows", result)
        self.assertEqual(sorted(set(touched)), [1, 2])

    def test_a_claimed_window_is_not_offered_to_a_second_row(self) -> None:
        layout_manager, _monitors, windows, directory = self._setup()
        try:
            with patch.multiple(layout_manager, list_windows=lambda: windows):
                row = {"process": "winword.exe", "title": "Report - Word"}
                self.assertIsNone(layout_manager._match_window(row, {1, 2}))
                self.assertEqual(layout_manager._match_window(row, {1}).handle, 2)
        finally:
            directory.cleanup()
