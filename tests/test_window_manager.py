from __future__ import annotations

import unittest
from unittest.mock import patch

from core.window_manager import MonitorInfo, WindowInfo, find_window, focus_window, place_window, snap_window
from core import undo as undo_stack
from actions import window_manager as window_manager_module
from actions.window_manager import window_manager


class WindowManagerTests(unittest.TestCase):
    def test_named_window_matching_prefers_process_name(self) -> None:
        windows = [
            WindowInfo(1, "Inbox - Browser", "chrome.exe", 10, 0, 0, 100, 100),
            WindowInfo(2, "Discord", "Discord.exe", 11, 0, 0, 100, 100),
        ]
        with patch("core.window_manager.list_windows", return_value=windows):
            self.assertEqual(find_window("Discord").handle, 2)
            self.assertEqual(find_window("chrome").handle, 1)

    def test_windows_focus_is_verified_against_the_real_foreground_window(self) -> None:
        target = WindowInfo(1, "Editor", "editor.exe", 1, 0, 0, 800, 600)
        other = WindowInfo(2, "Browser", "browser.exe", 2, 0, 0, 800, 600)
        with patch("core.window_manager._OS", "Windows"), \
             patch("core.window_manager.operate") as operate, \
             patch("core.window_manager.foreground_window", return_value=target):
            self.assertTrue(focus_window(target, timeout=0))
        operate.assert_called_once_with(target, "focus")

        with patch("core.window_manager._OS", "Windows"), \
             patch("core.window_manager.operate"), \
             patch("core.window_manager.foreground_window", return_value=other):
            self.assertFalse(focus_window(target, timeout=0))

    def test_focus_action_does_not_claim_success_when_focus_cannot_be_verified(self) -> None:
        window = WindowInfo(1, "Editor", "editor.exe", 1, 0, 0, 800, 600)
        with patch("actions.window_manager.find_windows", return_value=[window]), \
             patch("actions.window_manager.focus_window", return_value=False):
            result = window_manager({"action": "focus", "target": "Editor"})
        self.assertIn("could not verify", result.casefold())
        self.assertNotIn("Switched to", result)

    def test_window_move_registers_an_undo_snapshot(self) -> None:
        window = WindowInfo(1, "Test", "test.exe", 1, 50, 60, 850, 660)
        undo_stack.clear()
        with patch("actions.window_manager.find_windows", return_value=[window]), \
             patch("actions.window_manager.operate") as operate:
            result = window_manager({"action": "move", "target": "Test", "x": 200, "y": 220,
                                     "width": 900, "height": 700})
            self.assertIn("Moved Test", result)
            self.assertEqual(undo_stack.history(), ["window layout of Test"])
            undo_stack.undo_last()
            calls = [call.args for call in operate.call_args_list]
            self.assertIn((window, "move", 50, 60, 800, 600), calls)
        undo_stack.clear()

    def test_snap_uses_monitor_work_area(self) -> None:
        window = WindowInfo(1, "Test", "test.exe", 1, 50, 50, 850, 650)
        monitor = MonitorInfo(1, "DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1040)
        with patch("core.window_manager.operate") as operate:
            snap_window(window, monitor, "right")
            calls = [call.args for call in operate.call_args_list]
            self.assertIn((window, "move", 960, 0, 960, 1040), calls)
            self.assertIn((window, "focus"), calls)

    def test_spoken_fullscreen_uses_native_maximize_not_a_monitor_sized_move(self) -> None:
        window = WindowInfo(1, "Test", "test.exe", 1, 50, 50, 850, 650)
        monitor = MonitorInfo(1, "DISPLAY1", 0, 0, 1920, 1080, 0, 0, 1920, 1040)
        with patch("core.window_manager.operate") as operate, \
             patch("core.window_manager.refresh_window", return_value=window):
            place_window(window, monitor, "fullscreen")
        calls = [call.args for call in operate.call_args_list]
        self.assertIn((window, "maximize"), calls)
        self.assertNotIn((window, "move", 0, 0, 1920, 1080), calls)

    def test_direct_fullscreen_action_uses_maximize_and_keeps_taskbar_available(self) -> None:
        window = WindowInfo(1, "Editor", "editor.exe", 1, 50, 50, 850, 650)
        undo_stack.clear()
        with patch("actions.window_manager.find_windows", return_value=[window]), \
             patch("actions.window_manager.place_window", return_value=window) as place:
            result = window_manager({"action": "fulscreen", "target": "Editor"})
        place.assert_called_once_with(window, None, "maximized")
        self.assertIn("maximized", result)
        self.assertIn("taskbar", result)
        undo_stack.clear()

    def test_minimize_others_keeps_named_app_and_registers_one_safe_undo(self) -> None:
        keep = WindowInfo(1, "Roblox", "roblox.exe", 1, 0, 0, 800, 600)
        other = WindowInfo(2, "Discord", "discord.exe", 2, 20, 20, 900, 700)
        already_minimized = WindowInfo(
            3, "Spotify", "spotify.exe", 3, 40, 40, 840, 640, minimized=True
        )
        registered = []
        with patch("actions.window_manager.find_windows", return_value=[keep]), \
             patch("actions.window_manager.list_windows", return_value=[keep, other, already_minimized]), \
             patch("actions.window_manager.push_undo", lambda label, undo: registered.append((label, undo))), \
             patch("actions.window_manager.operate") as operate:
            result = window_manager({"action": "minimize_others", "target": "Roblox"})

        calls = [call.args for call in operate.call_args_list]
        self.assertIn((other, "minimize"), calls)
        self.assertNotIn((keep, "minimize"), calls)
        self.assertNotIn((already_minimized, "minimize"), calls)
        self.assertIn((keep, "focus"), calls)
        self.assertIn("Say undo", result)
        self.assertEqual(len(registered), 1)

        live_other = WindowInfo(2, "Discord", "discord.exe", 2, 20, 20, 900, 700, minimized=True)
        with patch("actions.window_manager.refresh_window", return_value=live_other), \
             patch("actions.window_manager.operate") as undo_operate:
            detail = registered[0][1]()
        undo_calls = [call.args for call in undo_operate.call_args_list]
        self.assertIn((live_other, "restore"), undo_calls)
        self.assertIn((live_other, "move", 20, 20, 880, 680), undo_calls)
        self.assertIn("Restored 1", detail)

    def test_minimize_others_requires_a_named_window_to_keep(self) -> None:
        result = window_manager({"action": "minimize_others"})
        self.assertIn("Name the application", result)

    def test_tidy_undo_refuses_to_overwrite_a_window_changed_afterwards(self) -> None:
        original = WindowInfo(2, "Discord", "discord.exe", 2, 20, 20, 900, 700)
        changed = WindowInfo(2, "Discord", "discord.exe", 2, 20, 20, 900, 700)
        with patch("actions.window_manager.refresh_window", return_value=changed), \
             patch("actions.window_manager.operate") as operate:
            with self.assertRaises(undo_stack.UndoRefused):
                window_manager_module._restore_minimized_windows(
                    ((original, window_manager_module._window_state(original)),)
                )
        operate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
