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
