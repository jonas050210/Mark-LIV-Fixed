from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import explorer
from core.window_manager import MonitorInfo, WindowInfo
from actions import file_controller, open_app


class ExplorerTests(unittest.TestCase):
    def test_resolve_location_alias_and_subpath(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            home = Path(directory)
            docs = home / "Documents"
            with patch.object(explorer, "locations", return_value={"home": home, "documents": docs}):
                self.assertEqual(explorer.resolve_location("documents/report.pdf"), docs / "report.pdf")
                self.assertEqual(explorer.resolve_location("docs"), docs)

    def test_search_detects_exact_file_and_lists_ambiguous_matches(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            first = root / "one" / "notes.txt"
            second = root / "two" / "notes.txt"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")

            self.assertEqual(explorer.search(str(first), root=root), [first])
            matches = explorer.search("notes", root=root, extension="txt", limit=10)
            self.assertEqual(matches, [first, second])
            result = explorer.format_matches(matches, "notes")
            self.assertIn("1. " + str(first), result)
            self.assertIn("2. " + str(second), result)
            self.assertIn("exact path", result)

    def test_windows_file_selection_uses_argument_list(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            target = Path(directory) / "report.pdf"
            target.write_text("report", encoding="utf-8")
            with patch.object(explorer, "_OS", "Windows"), \
                 patch.object(explorer.subprocess, "Popen") as popen:
                result = explorer.open_in_explorer(target, select=True)
        self.assertIn("selected report.pdf", result)
        popen.assert_called_once_with(
            ["explorer.exe", f"/select,{target}"], creationflags=0
        )


class FileControllerTests(unittest.TestCase):
    def test_file_search_defaults_to_home_not_desktop(self) -> None:
        with patch.object(file_controller, "find_files", return_value="search result") as find:
            result = file_controller.file_controller({"action": "find", "name": "report"})
        self.assertEqual(result, "search result")
        self.assertEqual(find.call_args.kwargs["path"], "home")

    def test_explorer_does_not_guess_between_duplicate_file_names(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            for folder in (root / "one", root / "two"):
                folder.mkdir()
                (folder / "notes.txt").write_text("notes", encoding="utf-8")
            with patch.object(explorer, "open_in_explorer") as open_in_explorer:
                result = file_controller.open_explorer(str(root), "notes.txt")
        self.assertIn("1. ", result)
        self.assertIn("2. ", result)
        self.assertIn("exact path", result)
        open_in_explorer.assert_not_called()

    def test_explorer_candidate_number_can_select_a_reported_match(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            first = root / "one" / "notes.txt"
            second = root / "two" / "notes.txt"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("one", encoding="utf-8")
            second.write_text("two", encoding="utf-8")
            with patch.object(explorer, "open_in_explorer", return_value="selected") as open_in_explorer:
                result = file_controller.open_explorer(
                    str(root), "notes.txt", select=True, match_index=2
                )
        self.assertEqual(result, "selected")
        open_in_explorer.assert_called_once_with(second, select=True)


class OpenAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = WindowInfo(
            handle=101, title="Roblox", process="RobloxPlayerBeta.exe", pid=11,
            left=0, top=0, right=800, bottom=600,
        )
        self.second_window = WindowInfo(
            handle=202, title="Roblox", process="RobloxPlayerBeta.exe", pid=22,
            left=0, top=0, right=800, bottom=600,
        )

    def test_window_matching_uses_process_name_when_title_is_unhelpful(self) -> None:
        process_window = WindowInfo(
            handle=303, title="Game session", process="RobloxPlayerBeta.exe", pid=33,
            left=0, top=0, right=800, bottom=600,
        )
        with patch("core.window_manager.list_windows", return_value=[process_window]):
            matches = open_app._matching_windows("Roblox", "RobloxPlayerBeta.exe")
        self.assertEqual(matches, [process_window])

    def test_normal_open_focuses_existing_window_without_launching(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[self.window]), \
             patch.object(open_app, "_focus_window", return_value=True) as focus, \
             patch.object(open_app, "_launch") as launch:
            result = open_app.open_app({"app_name": "Roblox"})
        self.assertIn("already open", result)
        focus.assert_called_once_with(self.window)
        launch.assert_not_called()

    def test_explicit_second_roblox_is_moved_and_verified(self) -> None:
        monitors = [
            MonitorInfo(1, "primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, primary=True),
            MonitorInfo(2, "secondary", 1920, 0, 3840, 1080, 1920, 0, 3840, 1040),
        ]
        moved_window = WindowInfo(
            handle=202, title="Roblox", process="RobloxPlayerBeta.exe", pid=22,
            left=1920, top=0, right=2720, bottom=600,
        )
        with patch.object(open_app, "_matching_windows", side_effect=[[self.window], [self.window, self.second_window], [self.window, moved_window]]), \
             patch.object(open_app, "_launch", return_value=True), \
             patch("core.window_manager.list_monitors", return_value=monitors), \
             patch("core.window_manager.move_to_monitor") as move:
            result = open_app.open_app({"app_name": "Roblox", "new_instance": True})
        self.assertIn("second Roblox", result)
        move.assert_called_once_with(self.second_window, monitors[1])

    def test_second_roblox_failure_is_reported_honestly(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[self.window]), \
             patch.object(open_app, "_launch", return_value=True), \
             patch.object(open_app, "_wait_for_windows", return_value=([self.window], [])):
            result = open_app.open_app({"app_name": "Roblox", "new_instance": True})
        self.assertIn("did not open a second", result)
        self.assertNotIn("Opened a second", result)


if __name__ == "__main__":
    unittest.main()
