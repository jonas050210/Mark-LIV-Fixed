from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import app_lifecycle, file_controller, window_manager
from core import window_manager as core_windows
from core.action_result import ActionResult
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
        self.assertFalse(ActionResult.from_handler("app_lifecycle", result).ok)
        operate.assert_not_called()

    def test_restart_leaves_an_unindexed_running_app_open(self) -> None:
        window = _window(1, "Personal Editor")
        with patch.object(app_lifecycle, "_snapshot", return_value=([window], [])), \
             patch.object(app_lifecycle, "operate") as operate, \
             patch.object(app_lifecycle, "open_app_result") as launch:
            result = app_lifecycle._restart("Personal Editor")

        self.assertIn("cannot find a verified launch entry", result)
        self.assertIn("left its window open", result)
        self.assertFalse(ActionResult.from_handler("app_lifecycle", result).ok)
        operate.assert_not_called()
        launch.assert_not_called()


    def test_diagnose_reports_every_window_even_when_pid_is_unknown(self) -> None:
        """Windows with no reported pid must not collapse into a single row.

        _process_details dedupes by pid so a genuinely repeated process is not
        listed twice, but pid 0 means "unknown", not "the same process" -- two
        different windows that both fail to report a pid are still two
        different windows and both belong in the diagnostic output.
        """
        entry = AppEntry("Editor", "exec", "/apps/editor.exe", "registry")
        unknown_a = WindowInfo(1, "Editor — file1.py", "editor.exe", 0, 0, 0, 800, 600)
        unknown_b = WindowInfo(2, "Editor — file2.py", "editor.exe", 0, 0, 0, 800, 600)
        with patch.object(
            app_lifecycle, "_snapshot", return_value=([unknown_a, unknown_b], [entry])
        ):
            result = app_lifecycle.app_lifecycle(
                {"action": "diagnose", "app_name": "Editor"}
            )
        self.assertEqual(
            result.count("PID unknown"), 2,
            "both windows with an unreported pid should be listed, not just the first",
        )


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
                 patch("actions.open_app.open_app_result", return_value=(True, "Opened Edge.")) as launch:
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
                 patch("actions.open_app.open_app_result") as launch:
                result = file_controller.open_with_application(
                    str(root), "Edge", name="report.pdf"
                )
        self.assertIn("Access denied", result)
        launch.assert_not_called()

    def test_open_with_keeps_an_unverified_application_launch_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.pdf"
            target.write_bytes(b"pdf")
            with patch.object(file_controller, "_resolve_path", return_value=target), \
                 patch.object(file_controller, "_is_safe_path", return_value=True), \
                 patch(
                     "actions.open_app.open_app_result",
                     return_value=(False, "I started Edge, but no window appeared."),
                 ):
                result = file_controller.open_with_application(str(target), "Edge")

        self.assertTrue(result.startswith("Could not open 'report.pdf' with Edge:"))
        self.assertFalse(ActionResult.from_handler("file_controller", result).ok)

    def test_open_with_passes_runtime_cancellation_to_the_launcher(self) -> None:
        cancel_event = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.pdf"
            target.write_bytes(b"pdf")
            with patch.object(file_controller, "_resolve_path", return_value=target), \
                 patch.object(file_controller, "_is_safe_path", return_value=True), \
                 patch("actions.open_app.open_app_result", return_value=(False, "cancelled")) as launch:
                file_controller.open_with_application(
                    str(target), "Edge", cancel_event=cancel_event
                )

        launch.assert_called_once_with(
            {"app_name": "Edge", "arguments": [str(target)]}, cancel_event=cancel_event
        )


class HudFrameRateTests(unittest.TestCase):
    def test_hud_target_is_fixed_at_180_fps_without_a_settings_control(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("ui.py").read_text(encoding="utf-8")
        self.assertIn("HUD_TARGET_FPS = 180", source)
        self.assertIn("HUD_FRAME_SECONDS = 1.0 / HUD_TARGET_FPS", source)
        self.assertNotIn("HUD MAX FPS", source)
        self.assertNotIn("_fps_combo", source)
        self.assertNotIn("_change_hud_fps", source)
        self.assertNotIn("set_max_fps", source)

    def test_old_persisted_fps_setting_has_no_active_configuration_api(self) -> None:
        self.assertFalse(hasattr(config_manager, "get_hud_max_fps"))
        self.assertFalse(hasattr(config_manager, "save_hud_max_fps"))
        self.assertFalse(hasattr(config_manager, "HUD_FPS_OPTIONS"))

    def test_hud_monitor_uses_the_shared_gpu_temperature_reader(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("ui.py").read_text(encoding="utf-8")
        self.assertIn("get_gpu_metrics as _read_gpu_metrics", source)
        self.assertIn('MetricBar("GPU °C"', source)
        self.assertNotIn('MetricBar("TMP"', source)

    def test_clipboard_detection_panel_is_removed(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath("ui.py").read_text(encoding="utf-8")
        self.assertNotIn("ClipboardPanel", source)
        self.assertNotIn("clipboard().dataChanged", source)


if __name__ == "__main__":
    unittest.main()
