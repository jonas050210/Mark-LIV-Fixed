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


if __name__ == "__main__":
    unittest.main()
