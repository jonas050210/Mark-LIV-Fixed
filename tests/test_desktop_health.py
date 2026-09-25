"""Tests for the read-only desktop diagnostic action."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import desktop_health
from core.window_manager import MonitorInfo


_MONITORS = [
    MonitorInfo(1, "primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, primary=True),
    MonitorInfo(2, "secondary", 1920, 0, 3840, 1080, 1920, 0, 3840, 1040),
]


class DesktopHealthTests(unittest.TestCase):
    def test_a_healthy_desktop_reports_ready_with_no_issues(self) -> None:
        with patch.object(desktop_health, "list_monitors", return_value=_MONITORS), \
             patch.object(desktop_health, "backend_name", return_value="user32"), \
             patch.object(desktop_health, "list_windows", return_value=[]), \
             patch("core.app_index.load_index", return_value=[object()]), \
             patch.object(desktop_health.shortcut_store, "all_shortcuts", return_value={}):
            result = desktop_health.desktop_health({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["health"], "ready")
        self.assertEqual(result["data"]["monitor_count"], 2)
        self.assertEqual(result["data"]["primary_monitor"], 1)
        self.assertEqual(result["data"]["issues"], [])

    def test_no_window_backend_is_reported_as_degraded(self) -> None:
        with patch.object(desktop_health, "list_monitors", return_value=_MONITORS), \
             patch.object(desktop_health, "backend_name", return_value="none"), \
             patch.object(desktop_health, "list_windows", return_value=[]), \
             patch("core.app_index.load_index", return_value=[object()]), \
             patch.object(desktop_health.shortcut_store, "all_shortcuts", return_value={}):
            result = desktop_health.desktop_health({})
        self.assertEqual(result["data"]["health"], "degraded")
        self.assertTrue(any("window backend" in issue for issue in result["data"]["issues"]))

    def test_an_empty_application_index_is_flagged(self) -> None:
        with patch.object(desktop_health, "list_monitors", return_value=_MONITORS), \
             patch.object(desktop_health, "backend_name", return_value="user32"), \
             patch.object(desktop_health, "list_windows", return_value=[]), \
             patch("core.app_index.load_index", return_value=[]), \
             patch.object(desktop_health.shortcut_store, "all_shortcuts", return_value={}):
            result = desktop_health.desktop_health({})
        self.assertEqual(result["data"]["health"], "degraded")
        self.assertTrue(any("index is empty" in issue for issue in result["data"]["issues"]))

    def test_never_raises_when_a_subsystem_is_unavailable(self) -> None:
        with patch.object(desktop_health, "list_monitors", side_effect=RuntimeError("boom")), \
             patch.object(desktop_health, "backend_name", return_value="user32"), \
             patch.object(desktop_health, "list_windows", return_value=[]), \
             patch("core.app_index.load_index", return_value=[]), \
             patch.object(desktop_health.shortcut_store, "all_shortcuts", return_value={}):
            result = desktop_health.desktop_health({})
        self.assertTrue(result["ok"])  # the diagnostic itself always succeeds
        self.assertEqual(result["data"]["health"], "degraded")


if __name__ == "__main__":
    unittest.main()
