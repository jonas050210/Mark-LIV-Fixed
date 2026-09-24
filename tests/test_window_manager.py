from __future__ import annotations

import unittest
from unittest.mock import patch

from core.window_manager import MonitorInfo, WindowInfo, find_window, snap_window


class WindowManagerTests(unittest.TestCase):
    def test_named_window_matching_prefers_process_name(self) -> None:
        windows = [
            WindowInfo(1, "Inbox - Browser", "chrome.exe", 10, 0, 0, 100, 100),
            WindowInfo(2, "Discord", "Discord.exe", 11, 0, 0, 100, 100),
        ]
        with patch("core.window_manager.list_windows", return_value=windows):
            self.assertEqual(find_window("Discord").handle, 2)
            self.assertEqual(find_window("chrome").handle, 1)

    def test_snap_uses_monitor_work_area(self) -> None:
        window = WindowInfo(1, "Test", "test.exe", 1, 50, 50, 850, 650)
        monitor = MonitorInfo(1, "DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1040)
        with patch("core.window_manager.operate") as operate:
            snap_window(window, monitor, "right")
            calls = [call.args for call in operate.call_args_list]
            self.assertIn((window, "move", 960, 0, 960, 1040), calls)
            self.assertIn((window, "focus"), calls)


if __name__ == "__main__":
    unittest.main()
