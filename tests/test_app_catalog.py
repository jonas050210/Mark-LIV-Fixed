from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import app_catalog, computer_settings, window_manager
from core.app_index import AppEntry


class ApplicationCatalogueTests(unittest.TestCase):
    def test_list_is_bounded_and_reports_the_total(self) -> None:
        entries = [AppEntry(f"App {index}", "exec", f"/app/{index}") for index in range(5)]
        with patch.object(app_catalog, "load_index", return_value=entries):
            result = app_catalog.app_catalog({"action": "list", "limit": 2})
        self.assertIn("Installed applications (5)", result)
        self.assertIn("App 0", result)
        self.assertIn("+3 more", result)
        self.assertNotIn("App 4", result)

    def test_refresh_rebuilds_the_index(self) -> None:
        entries = [AppEntry("YouTube", "lnk", "YouTube.lnk")]
        with patch.object(app_catalog, "build_index", return_value=entries) as build:
            result = app_catalog.app_catalog({"action": "refresh"})
        build.assert_called_once_with()
        self.assertIn("1 launchable entries", result)


class NativeWindowRoutingTests(unittest.TestCase):
    def test_legacy_minimize_routes_to_window_manager_without_hotkeys(self) -> None:
        with patch("actions.window_manager.window_manager", return_value="Minimized Editor.") as route, \
             patch.object(computer_settings, "_PYAUTOGUI", False):
            result = computer_settings.computer_settings(
                {"action": "minimize", "target": "Editor"}
            )
        self.assertEqual(result, "Minimized Editor.")
        route.assert_called_once_with(
            {"action": "minimize", "target": "Editor"}, player=None
        )

    def test_active_close_routes_by_window_handle_without_pyautogui(self) -> None:
        with patch("actions.window_manager.window_manager", return_value="Closed Twitch.") as route, \
             patch.object(computer_settings, "_PYAUTOGUI", False):
            result = computer_settings.computer_settings({"action": "close_window"})
        self.assertEqual(result, "Closed Twitch.")
        route.assert_called_once_with(
            {"action": "close", "target": ""}, player=None
        )

    def test_legacy_fullscreen_routes_to_native_maximize_without_a_hotkey(self) -> None:
        with patch("actions.window_manager.window_manager", return_value="Maximized Editor.") as route, \
             patch.object(computer_settings, "_PYAUTOGUI", False):
            result = computer_settings.computer_settings(
                {"action": "full_screen", "target": "Editor"}
            )
        self.assertEqual(result, "Maximized Editor.")
        route.assert_called_once_with(
            {"action": "maximize", "target": "Editor"}, player=None
        )

    def test_unnamed_switch_refuses_alt_tab(self) -> None:
        result = computer_settings.computer_settings({"action": "switch_window"})
        self.assertIn("Alt+Tab was removed", result)
        self.assertIn("window_manager", result)


class ImmediateWindowClosePolicyTests(unittest.TestCase):
    def test_named_window_close_has_no_mark_liv_confirmation(self) -> None:
        self.assertNotIn("confirmation_actions", window_manager.TOOL)

    def test_active_window_close_has_no_mark_liv_confirmation(self) -> None:
        guarded = computer_settings.TOOL["confirmation_actions"]
        self.assertNotIn("close_app", guarded)
        self.assertNotIn("close_window", guarded)
        self.assertIn("shutdown", guarded)
        self.assertIn("restart", guarded)


if __name__ == "__main__":
    unittest.main()
