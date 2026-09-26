"""The on-demand PC status action must stay read-only and honest."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from actions import pc_status


class PcStatusTests(unittest.TestCase):
    @staticmethod
    def _status(**overrides):
        status = {
            "available": True,
            "cpu_percent": 27.5,
            "ram_percent": 48.0,
            "ram_used_gb": 7.7,
            "ram_total_gb": 16.0,
            "gpu_percent": 34.0,
            "gpu_temp_c": 56.0,
            "gpu_name": "NVIDIA GeForce RTX 4060 Ti",
            "cpu_temp_c": 60.0,
            "uptime": "2h 15m",
            "process_count": 120,
        }
        status.update(overrides)
        return status

    def test_status_reports_metrics_without_listing_or_changing_apps(self) -> None:
        with patch.object(pc_status.system_monitor, "get_system_status", return_value=self._status()), \
             patch.object(pc_status, "_top_memory_apps") as top:
            result = pc_status.pc_status({"action": "status"})
        self.assertIn("CPU: 27.5%", result)
        self.assertIn("RAM: 7.7/16.0 GB (48.0%)", result)
        self.assertIn("NVIDIA GeForce RTX 4060 Ti: 34.0%", result)
        self.assertIn("GPU temperature: 56.0°C", result)
        self.assertIn("Uptime: 2h 15m", result)
        top.assert_not_called()

    def test_lag_check_names_real_pressure_and_does_not_close_anything(self) -> None:
        overloaded = self._status(cpu_percent=92.0, ram_percent=91.0, gpu_percent=96.0)
        with patch.object(pc_status.system_monitor, "get_system_status", return_value=overloaded), \
             patch.object(
                 pc_status, "_top_memory_apps", return_value=[("Chrome", 3 * 1024 ** 3)]
             ):
            result = pc_status.pc_status({"action": "lag_check"})
        self.assertIn("CPU usage is high", result)
        self.assertIn("RAM usage is high", result)
        self.assertIn("GPU usage is very high", result)
        self.assertIn("Chrome: 3.0 GB", result)
        self.assertIn("did not close anything", result)

    def test_top_apps_reports_only_the_requested_bounded_number(self) -> None:
        apps = [("Game", 4 * 1024 ** 3), ("Discord", 1024 ** 3)]
        with patch.object(pc_status.system_monitor, "get_system_status", return_value=self._status()), \
             patch.object(pc_status, "_top_memory_apps", return_value=apps) as top:
            result = pc_status.pc_status({"action": "top_apps", "limit": 2})
        top.assert_called_once_with(2)
        self.assertIn("1. Game (4.0 GB RAM)", result)
        self.assertIn("2. Discord (1.0 GB RAM)", result)

    def test_missing_optional_monitoring_dependency_is_explained(self) -> None:
        with patch.object(
            pc_status.system_monitor,
            "get_system_status",
            return_value={"available": False, "error": "psutil missing"},
        ):
            result = pc_status.pc_status({"action": "lag_check"})
        self.assertIn("psutil", result)
        self.assertIn("unavailable", result)


if __name__ == "__main__":
    unittest.main()
