from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import app_lifecycle, file_controller, window_manager
from core import window_manager as core_windows
from core.app_index import AppEntry
from core.window_manager import WindowInfo
from memory import config_manager


def _window(handle: int, title: str = "Editor") -> WindowInfo:
    return WindowInfo(handle, title, "editor.exe", handle, 0, 0, 800, 600)


class ApplicationLifecycleTests(unittest.TestCase):
    def test_status_reports_running_window_and_launch_source(self) -> None:
        entry = AppEntry("Editor", "exec", "/apps/editor.exe", "registry")
        with patch.object(app_lifecycle, "_snapshot", return_value=([_window(1)], [entry])):
            result = app_lifecycle.app_lifecycle({"action": "status", "app_name": "Editor"})
        self.assertIn("running", result)
        self.assertIn("exec/registry", result)

    def test_restart_never_force_kills_a_window_that_refuses_to_close(self) -> None:
        entry = AppEntry("Editor", "exec", "/apps/editor.exe", "registry")
        with patch.object(app_lifecycle, "_snapshot", return_value=([_window(1)], [entry])), \
             patch.object(app_lifecycle, "operate"), \
             patch.object(app_lifecycle.time, "monotonic", side_effect=[0.0, 11.0]):
            result = app_lifecycle._restart("Editor")
        self.assertIn("did not force-kill", result)

    def test_restart_refuses_to_confuse_a_web_app_with_a_browser_tab(self) -> None:
        entry = AppEntry("Twitch", "lnk", r"C:\\Twitch.lnk", "webapp")
        browser_window = WindowInfo(
            4, "Twitch", "chrome.exe", 44, 0, 0, 800, 600
        )
        with patch.object(app_lifecycle, "_snapshot", return_value=([browser_window], [entry])), \
             patch.object(app_lifecycle, "operate") as operate:
            result = app_lifecycle._restart("Twitch")
        self.assertIn("cannot safely distinguish", result)
        operate.assert_not_called()


class MultiWindowActionTests(unittest.TestCase):
    def test_minimize_all_addresses_each_matching_window(self) -> None:
        windows = [_window(1), _window(2)]
        with patch.object(window_manager, "find_windows", return_value=windows), \
             patch.object(window_manager, "operate") as operate:
            result = window_manager.window_manager(
                {"action": "minimize_all", "target": "Editor"}
            )
        self.assertIn("Minimized 2 of 2", result)
        self.assertEqual(operate.call_count, 2)

    def test_batch_action_requires_a_named_target(self) -> None:
        result = window_manager.window_manager({"action": "close_all"})
        self.assertIn("target application is required", result)

    def test_close_does_not_claim_success_while_window_remains_visible(self) -> None:
        window = _window(1)
        with patch.object(window_manager, "find_windows", return_value=[window]), \
             patch.object(window_manager, "operate"), \
             patch.object(window_manager, "_wait_for_closed", return_value={1}):
            result = window_manager.window_manager({"action": "close", "target": "Editor"})
        self.assertIn("still open", result)
        self.assertIn("save prompt", result)

    def test_destructive_matching_rejects_only_fuzzy_neighbours(self) -> None:
        windows = [_window(1, "Visual Studio Code")]
        with patch.object(core_windows, "list_windows", return_value=windows):
            self.assertEqual(core_windows.find_windows("Chrome", min_score=80), [])


class OpenWithApplicationTests(unittest.TestCase):
    def test_resolved_file_is_passed_as_a_real_application_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.pdf"
            target.write_bytes(b"pdf")
            with patch.object(file_controller, "_resolve_path", return_value=target), \
                 patch.object(file_controller, "_is_safe_path", return_value=True), \
                 patch("actions.open_app.open_app", return_value="Opened Edge.") as launch:
                result = file_controller.open_with_application(str(target), "Edge")
        self.assertEqual(result, "Opened Edge.")
        launch.assert_called_once_with({"app_name": "Edge", "arguments": [str(target)]})

    def test_final_search_result_is_rechecked_against_path_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "report.pdf"
            target.write_bytes(b"pdf")
            with patch.object(file_controller, "_resolve_path", return_value=root), \
                 patch.object(file_controller.explorer, "search", return_value=[target]), \
                 patch.object(file_controller, "_is_safe_path", side_effect=[True, False]), \
                 patch("actions.open_app.open_app") as launch:
                result = file_controller.open_with_application(
                    str(root), "Edge", name="report.pdf"
                )
        self.assertIn("Access denied", result)
        launch.assert_not_called()


class HudFrameRateConfigTests(unittest.TestCase):
    def test_all_declared_frame_rate_options_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(config_manager, "CONFIG_FILE", Path(directory) / "config.json"):
            for value in (30, 60, 120, 240, 0):
                config_manager.save_hud_max_fps(value)
                self.assertEqual(config_manager.get_hud_max_fps(), value)

    def test_invalid_frame_rate_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            config_manager.save_hud_max_fps(144)

    def test_clipboard_detection_panel_is_removed(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("ui.py").read_text(encoding="utf-8")
        self.assertNotIn("ClipboardPanel", source)
        self.assertNotIn("clipboard().dataChanged", source)


if __name__ == "__main__":
    unittest.main()
