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
