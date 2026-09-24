"""Opt-in Windows hardware checks.

These tests stay skipped in CI and Linux sandboxes because monitor/audio topology
is machine-specific. On the target PC run:

    RUN_WINDOWS_INTEGRATION=1 python -m unittest tests.test_windows_integration -v

They are deliberately read-only: no windows are moved and no audio is played.
"""
from __future__ import annotations

import os
import platform
import unittest


_ENABLED = platform.system() == "Windows" and os.environ.get("RUN_WINDOWS_INTEGRATION") == "1"


@unittest.skipUnless(_ENABLED, "set RUN_WINDOWS_INTEGRATION=1 on Windows")
class WindowsHardwareIntegrationTests(unittest.TestCase):
    def test_dpi_aware_monitors_have_physical_geometry(self) -> None:
        from core.window_manager import list_monitors

        monitors = list_monitors()
        self.assertGreaterEqual(len(monitors), 1)
        for monitor in monitors:
            self.assertGreater(monitor.width, 0)
            self.assertGreater(monitor.height, 0)
            self.assertIsInstance(monitor.primary, bool)

    def test_two_full_hd_displays_report_refresh_when_driver_exposes_it(self) -> None:
        from core.window_manager import list_monitors

        monitors = list_monitors()
        full_hd = [m for m in monitors if (m.width, m.height) == (1920, 1080)]
        self.assertGreaterEqual(len(full_hd), 2, "target machine should have two 1920x1080 monitors")
        # Some remote desktop drivers do not expose refresh. A real local setup
        # must report the requested 180 Hz on both panels.
        if all(m.refresh_hz is not None for m in full_hd[:2]):
            self.assertTrue(all(m.refresh_hz >= 180 for m in full_hd[:2]))

    def test_audio_diagnostics_are_json_safe(self) -> None:
        from core import audio_devices

        snapshot = audio_devices.diagnostics("JBL Quantum 400", "JBL Quantum 400")
        self.assertIn("input", snapshot)
        self.assertIn("output", snapshot)
        self.assertIsInstance(snapshot["input"]["devices"], list)


if __name__ == "__main__":
    unittest.main()
