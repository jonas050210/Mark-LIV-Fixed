"""GPU telemetry must report real NVML temperature, not invent a CPU fallback."""
from __future__ import annotations

import sys
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

    def test_pynvml_failure_is_retried_after_a_short_cooldown(self) -> None:
        """A temporary driver startup failure must not leave GPU temperature N/A forever."""
        calls: list[str] = []
        fake_nvml = SimpleNamespace(
            NVML_TEMPERATURE_GPU=0,
            nvmlInit=lambda: calls.append("init"),
            nvmlDeviceGetHandleByIndex=lambda index: "gpu-0",
            nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=37),
            nvmlDeviceGetTemperature=lambda handle, sensor: 62,
            nvmlDeviceGetName=lambda handle: b"NVIDIA GeForce RTX 4060 Ti",
        )
        with patch.multiple(
            system_monitor,
            _pynvml_ok=False,
            _pynvml_retry_after=200.0,
            _pynvml_module=None,
            _pynvml_handle=None,
        ), patch.dict(sys.modules, {"pynvml": fake_nvml}):
            with patch.object(system_monitor.time, "monotonic", return_value=199.0):
                self.assertEqual(
                    system_monitor._pynvml_gpu_metrics(), system_monitor._blank_gpu_metrics()
                )
            self.assertEqual(calls, [])
            with patch.object(system_monitor.time, "monotonic", return_value=200.0):
                result = system_monitor._pynvml_gpu_metrics()

        self.assertEqual(calls, ["init"])
        self.assertEqual(result["utilization_percent"], 37.0)
        self.assertEqual(result["temperature_c"], 62.0)
        self.assertEqual(result["name"], "NVIDIA GeForce RTX 4060 Ti")

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
