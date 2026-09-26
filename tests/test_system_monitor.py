"""GPU telemetry must report real NVML temperature, not invent a CPU fallback."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from actions import system_monitor


class GpuTelemetryTests(unittest.TestCase):
    def test_pynvml_metrics_are_preferred_and_include_temperature_and_model(self) -> None:
        expected = {
            "utilization_percent": 42.0,
            "temperature_c": 61.0,
            "name": "NVIDIA GeForce RTX 4060 Ti",
        }
        with patch.object(system_monitor, "_pynvml_gpu_metrics", return_value=expected), \
             patch.object(system_monitor, "_native_nvml_gpu_metrics") as native:
            result = system_monitor.get_gpu_metrics()
        self.assertEqual(result, expected)
        native.assert_not_called()

    def test_native_nvml_is_used_when_python_binding_is_unavailable(self) -> None:
        expected = {
            "utilization_percent": 17.0,
            "temperature_c": 48.0,
            "name": "NVIDIA GeForce RTX 4060 Ti",
        }
        with patch.object(system_monitor, "_pynvml_gpu_metrics", return_value=system_monitor._blank_gpu_metrics()), \
             patch.object(system_monitor, "_native_nvml_gpu_metrics", return_value=expected):
            result = system_monitor.get_gpu_metrics()
        self.assertEqual(result, expected)

    def test_system_status_exposes_gpu_temperature_separately_from_cpu_temperature(self) -> None:
        fake_psutil = SimpleNamespace(
            cpu_percent=lambda interval: 30.0,
            virtual_memory=lambda: SimpleNamespace(percent=50.0, used=8 * 1024 ** 3, total=16 * 1024 ** 3),
            boot_time=lambda: 100.0,
            pids=lambda: [1, 2, 3],
        )
        gpu = {
            "utilization_percent": 53.6,
            "temperature_c": 59.4,
            "name": "NVIDIA GeForce RTX 4060 Ti",
        }
        with patch.object(system_monitor, "_PSUTIL", True), \
             patch.object(system_monitor, "psutil", fake_psutil), \
             patch.object(system_monitor, "_get_cpu_temp", return_value=-1.0), \
             patch.object(system_monitor, "get_gpu_metrics", return_value=gpu), \
             patch.object(system_monitor.time, "time", return_value=3700.0):
            status = system_monitor.get_system_status()
        self.assertEqual(status["cpu_temp_c"], None)
        self.assertEqual(status["gpu_temp_c"], 59.4)
        self.assertEqual(status["gpu_name"], "NVIDIA GeForce RTX 4060 Ti")
        self.assertEqual(status["gpu_percent"], 53.6)

    def test_gpu_temperature_alert_names_the_actual_card(self) -> None:
        fake_psutil = SimpleNamespace(
            cpu_percent=lambda interval: 10.0,
            virtual_memory=lambda: SimpleNamespace(percent=20.0),
        )
        gpu = {
            "utilization_percent": 60.0,
            "temperature_c": 88.0,
            "name": "NVIDIA GeForce RTX 4060 Ti",
        }
        monitor = system_monitor.SystemMonitor()
        with patch.object(system_monitor, "_PSUTIL", True), \
             patch.object(system_monitor, "psutil", fake_psutil), \
             patch.object(system_monitor, "_get_cpu_temp", return_value=-1.0), \
             patch.object(system_monitor, "get_gpu_metrics", return_value=gpu), \
             patch.object(monitor, "_can_alert", return_value=True):
            alert = monitor.check()
        self.assertIn("RTX 4060 Ti", alert)
        self.assertIn("88°C", alert)


if __name__ == "__main__":
    unittest.main()
