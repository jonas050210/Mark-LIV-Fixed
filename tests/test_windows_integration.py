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


@unittest.skipUnless(_ENABLED, "set RUN_WINDOWS_INTEGRATION=1 on Windows")
class WindowsLauncherIntegrationTests(unittest.TestCase):
    """The Windows-only launcher paths, which no other machine can exercise.

    Registry scanning, ``.lnk`` resolution, icon extraction and the scheduler
    are all written against APIs that simply do not exist elsewhere, so this is
    the only place they are ever executed. Each test asserts a property that
    must hold on any Windows install rather than a value specific to one
    machine.
    """

    def test_the_index_finds_real_installed_applications(self) -> None:
        from core.app_index import build_index

        entries = build_index()
        self.assertGreater(len(entries), 5, "no applications were indexed")
        self.assertTrue(any(entry.source == "registry" for entry in entries))

    def test_every_indexed_executable_actually_exists(self) -> None:
        from pathlib import Path

        from core.app_index import load_index

        missing = [
            entry.name for entry in load_index()
            if entry.kind == "exec" and not Path(entry.target).is_file()
        ]
        self.assertEqual(missing, [], "indexed executables that are not on disk")

    def test_start_menu_shortcuts_resolve_to_executables(self) -> None:
        """pylnk3 turns a .lnk into a real target, which is what gives a launch
        a process id and lets it carry arguments."""
        try:
            import pylnk3  # noqa: F401
        except ImportError:
            self.skipTest("pylnk3 is not installed")

        from core.app_index import load_index

        start_menu = [entry for entry in load_index() if entry.source == "startmenu"]
        if not start_menu:
            self.skipTest("this machine has no Start-menu entries")
        resolved = [entry for entry in start_menu if entry.kind == "exec"]
        self.assertTrue(resolved, "no Start-menu shortcut could be resolved to an .exe")

    def test_notepad_launches_and_reports_a_pid(self) -> None:
        import os
        import signal
        import time

        from core.app_index import AppEntry, launch

        notepad = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "notepad.exe")
        if not os.path.isfile(notepad):
            self.skipTest("notepad.exe is not present")
        pid = launch(AppEntry("Notepad", "exec", notepad, "registry"))
        self.assertIsInstance(pid, int)
        try:
            time.sleep(1.0)
            self.assertTrue(_pid_alive(pid), "the launched process exited immediately")
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def test_a_windows_switch_is_refused_as_an_argument(self) -> None:
        from core.app_index import sanitise_arguments

        for switch in ("/s", "/Q", "/delete"):
            with self.assertRaises(ValueError, msg=switch):
                sanitise_arguments(switch)

    def test_an_icon_can_be_extracted_for_at_least_one_application(self) -> None:
        from core.app_icons import icon_png
        from core.app_index import load_index

        entries = [entry for entry in load_index() if entry.kind == "exec"][:10]
        if not entries:
            self.skipTest("no executable entries to extract an icon from")
        extracted = [entry.name for entry in entries if icon_png(entry)]
        self.assertTrue(extracted, "no icon could be extracted from ten applications")

    def test_the_window_backend_is_the_native_one(self) -> None:
        from core.window_manager import backend_name, list_windows

        self.assertEqual(backend_name(), "user32")
        windows = list_windows()
        self.assertTrue(windows, "no windows were enumerated")
        self.assertTrue(all(window.handle for window in windows))
        self.assertTrue(any(window.pid for window in windows))

    def test_a_scheduled_reminder_can_be_listed_and_cancelled(self) -> None:
        """The whole round trip against the real Task Scheduler."""
        from datetime import datetime, timedelta

        from actions import reminder as reminder_module

        when = datetime.now() + timedelta(hours=6)
        created = reminder_module.reminder({
            "action": "set",
            "date": when.strftime("%Y-%m-%d"),
            "time": when.strftime("%H:%M"),
            "message": "MARK LIV integration check",
        })
        if "couldn't register" in created:
            self.skipTest("this account may not create scheduled tasks")
        self.assertIn("Reminder set", created)
        try:
            listing = reminder_module.reminder({"action": "list"})
            self.assertIn("MARK LIV integration check", listing)
        finally:
            cancelled = reminder_module.reminder({
                "action": "cancel", "message": "MARK LIV integration check",
            })
        self.assertIn("Cancelled", cancelled)
        self.assertNotIn(
            "MARK LIV integration check", reminder_module.reminder({"action": "list"})
        )

    def test_reading_the_volume_gives_a_percentage(self) -> None:
        from actions.computer_settings import volume_get

        value = volume_get()
        if value is None:
            self.skipTest("this machine does not report its volume")
        self.assertTrue(0 <= value <= 100)


def _pid_alive(pid: int) -> bool:
    import subprocess

    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    return str(pid) in result.stdout
