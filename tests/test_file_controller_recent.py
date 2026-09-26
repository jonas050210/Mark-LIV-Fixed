"""Recent-file listing should be useful without opening or changing anything."""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from actions import file_controller


class RecentFilesTests(unittest.TestCase):
    def test_recent_lists_newest_regular_visible_files_first(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            old = root / "old.txt"
            newest = root / "newest.pdf"
            hidden = root / ".private.txt"
            old.write_text("old", encoding="utf-8")
            newest.write_bytes(b"new")
            hidden.write_text("hidden", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 120, now - 120))
            os.utime(newest, (now - 10, now - 10))
            os.utime(hidden, (now, now))

            result = file_controller.get_recent_files(str(root), count=2)

        self.assertIn("Most recent files", result)
        self.assertLess(result.index("newest.pdf"), result.index("old.txt"))
        self.assertNotIn(".private.txt", result)

    def test_recent_refuses_a_path_outside_the_user_folder(self) -> None:
        with patch.object(file_controller, "_is_safe_path", return_value=False):
            result = file_controller.get_recent_files("/outside-home")
        self.assertIn("Access denied", result)

    def test_recent_defaults_to_downloads_through_the_action(self) -> None:
        with patch.object(file_controller, "get_recent_files", return_value="recent files") as recent:
            result = file_controller.file_controller({"action": "recent"})
        self.assertEqual(result, "recent files")
        recent.assert_called_once_with(path="downloads", count=10, extension="")

    def test_recent_can_filter_to_the_latest_requested_file_type(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            newest_text = root / "newest.txt"
            pdf = root / "report.pdf"
            newest_text.write_text("new", encoding="utf-8")
            pdf.write_bytes(b"pdf")
            now = time.time()
            os.utime(pdf, (now - 60, now - 60))
            os.utime(newest_text, (now, now))

            result = file_controller.get_recent_files(str(root), extension="pdf")

        self.assertIn("report.pdf", result)
        self.assertNotIn("newest.txt", result)

    def test_reveal_recent_selects_the_newest_matching_file_without_opening_it(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            old = root / "old.pdf"
            newest = root / "newest.pdf"
            newer_non_match = root / "ignore-me.txt"
            old.write_bytes(b"old")
            newest.write_bytes(b"new")
            newer_non_match.write_text("newer but not a PDF", encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 60, now - 60))
            os.utime(newest, (now, now))
            os.utime(newer_non_match, (now + 60, now + 60))
            with patch.object(file_controller.explorer, "open_in_explorer", return_value="Selected newest.pdf") as reveal:
                result = file_controller.reveal_recent_file(str(root), extension=".pdf")

        self.assertEqual(result, "Selected newest.pdf")
        reveal.assert_called_once_with(newest, select=True)

    def test_reveal_recent_skips_an_incomplete_download_for_plain_latest_requests(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            completed = root / "setup.exe"
            incomplete = root / "setup.exe.crdownload"
            completed.write_bytes(b"complete")
            incomplete.write_bytes(b"still downloading")
            now = time.time()
            os.utime(completed, (now - 30, now - 30))
            os.utime(incomplete, (now, now))
            with patch.object(file_controller.explorer, "open_in_explorer", return_value="selected") as reveal:
                result = file_controller.reveal_recent_file(str(root))

        self.assertEqual(result, "selected")
        reveal.assert_called_once_with(completed, select=True)

    def test_download_status_separates_incomplete_and_completed_files(self) -> None:
        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            completed = root / "lesson.pdf"
            incomplete = root / "video.mp4.part"
            completed.write_bytes(b"pdf")
            incomplete.write_bytes(b"part")

            result = file_controller.get_download_status(str(root))

        self.assertIn("Still downloading or temporary", result)
        self.assertIn("video.mp4.part", result)
        self.assertIn("Recent completed files", result)
        self.assertIn("lesson.pdf", result)
        self.assertIn("not opened", result)

    def test_download_status_action_defaults_to_downloads(self) -> None:
        with patch.object(file_controller, "get_download_status", return_value="download status") as status:
            result = file_controller.file_controller({"action": "download_status"})
        self.assertEqual(result, "download status")
        status.assert_called_once_with(path="downloads", count=10)

    def test_reveal_recent_action_defaults_to_downloads(self) -> None:
        with patch.object(file_controller, "reveal_recent_file", return_value="Selected note.txt") as reveal:
            result = file_controller.file_controller({"action": "reveal_recent"})
        self.assertEqual(result, "Selected note.txt")
        reveal.assert_called_once_with(path="downloads", extension="")


if __name__ == "__main__":
    unittest.main()
