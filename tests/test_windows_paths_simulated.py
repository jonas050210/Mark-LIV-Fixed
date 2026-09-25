"""Execute the Windows-only code paths on whatever platform is running.

About a fifth of this project only runs on Windows, and on a Linux or macOS
machine that code was never executed by anything: not the suite, not CI, not a
developer. A wrong registry key, a swapped argument pair, a misparsed shortcut
would survive every green build and fail the first time a Windows user asked
for it.

These tests drive those paths against the stand-ins in ``windows_fakes``. They
prove the code asks for the right things in the right order and handles what
comes back; they cannot prove that the real Windows API behaves as assumed.
That distinction is deliberate and is why the real-hardware suite in
``test_windows_integration.py`` still exists — this is the floor, not a
replacement.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.windows_fakes import (
    FakeLink,
    FakePylnk3,
    FakeWinreg,
    fake_modules,
    make_exe,
)


class RegistryScanTests(unittest.TestCase):
    """The App Paths scan is where most installed Windows software is found."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.chrome = make_exe(self.root, "chrome.exe")
        self.code = make_exe(self.root, "Code.exe")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _scan(self, children: dict):
        from core import app_index

        subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        winreg = FakeWinreg({
            ("HKLM", subkey): children,
            ("HKCU", subkey): {},
        })
        with fake_modules(winreg=winreg):
            return app_index._windows_registry_entries(), winreg

    def test_installed_applications_are_found(self) -> None:
        entries, _ = self._scan({
            "chrome.exe": str(self.chrome),
            "Code.exe": str(self.code),
        })
        names = sorted({entry.name for entry in entries})
        self.assertIn("chrome", names)
        self.assertIn("Code", names)
        self.assertTrue(all(entry.source == "registry" for entry in entries))
        self.assertTrue(all(entry.kind == "exec" for entry in entries))

    def test_an_application_in_both_registry_views_is_listed_once(self) -> None:
        """The scan deliberately reads the 64-bit view, the 32-bit view and the
        user hive, so the same program can come back more than once; the index
        is what has to be free of duplicates."""
        from core import app_index

        subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        winreg = FakeWinreg({
            ("HKLM", subkey): {"chrome.exe": str(self.chrome)},
            ("HKCU", subkey): {"chrome.exe": str(self.chrome)},
        })
        with fake_modules(winreg=winreg), \
             patch.object(app_index, "_OS", "Windows"), \
             patch.object(app_index, "_windows_start_menu_entries", return_value=[]), \
             patch.object(app_index, "_windows_store_entries", return_value=[]), \
             patch.object(app_index, "_write_cache", lambda entries: None):
            entries = app_index.build_index()
        chrome = [entry for entry in entries if entry.name == "chrome"]
        self.assertEqual(len(chrome), 1, "the same program was indexed twice")

    def test_both_registry_views_and_the_user_hive_are_consulted(self) -> None:
        """A 32-bit application on a 64-bit Windows lives in the WOW64 view; an
        install for the current user only lives in HKCU. Missing any of the
        three silently loses applications."""
        _entries, winreg = self._scan({"chrome.exe": str(self.chrome)})
        accesses = [access for _root, _subkey, access in winreg.opened]
        roots = [root for root, _subkey, _access in winreg.opened]
        self.assertTrue(any(access & FakeWinreg.KEY_WOW64_64KEY for access in accesses))
        self.assertTrue(any(access & FakeWinreg.KEY_WOW64_32KEY for access in accesses))
        self.assertIn("HKCU", roots)

    def test_a_quoted_path_is_unquoted(self) -> None:
        entries, _ = self._scan({"chrome.exe": f'"{self.chrome}"'})
        self.assertEqual(Path(entries[0].target), self.chrome)

    def test_an_entry_pointing_at_a_deleted_file_is_dropped(self) -> None:
        entries, _ = self._scan({
            "ghost.exe": str(self.root / "not-here.exe"),
            "chrome.exe": str(self.chrome),
        })
        self.assertEqual({entry.name for entry in entries}, {"chrome"})

    def test_an_empty_value_is_ignored(self) -> None:
        entries, _ = self._scan({"broken.exe": "   "})
        self.assertEqual(entries, [])

    def test_every_opened_key_is_closed(self) -> None:
        _entries, winreg = self._scan({"chrome.exe": str(self.chrome)})
        self.assertGreaterEqual(winreg.closed, 1)

    def test_a_missing_registry_module_is_not_fatal(self) -> None:
        """The same function is imported on Linux; it must simply find nothing."""
        from core import app_index

        self.assertEqual(app_index._windows_registry_entries(), [])


class ShortcutResolutionTests(unittest.TestCase):
    """A .lnk resolved to its executable is what gives a launch a pid."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.target = make_exe(self.root, "app.exe")
        self.link = self.root / "App.lnk"
        self.link.write_bytes(b"L\x00\x00\x00 fake shortcut")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _resolve(self, link_object):
        from core import app_index

        module = FakePylnk3({str(self.link): link_object})
        with fake_modules(pylnk3=module):
            return app_index._resolve_shortcut_target(self.link)

    def test_a_shortcut_resolves_to_its_executable(self) -> None:
        executable, stem = self._resolve(FakeLink(path=str(self.target)))
        self.assertEqual(Path(executable), self.target)
        self.assertEqual(stem, "app")

    def test_the_local_base_path_is_used_as_a_fallback(self) -> None:
        executable, _ = self._resolve(FakeLink(path="", local_base_path=str(self.target)))
        self.assertEqual(Path(executable), self.target)

    def test_environment_variables_in_the_target_are_expanded(self) -> None:
        """Shortcuts routinely point at %ProgramFiles%. os.path.expandvars uses
        the host's own syntax, so the check is written in whichever that is."""
        template = "%FAKE_ROOT%" if os.name == "nt" else "$FAKE_ROOT"
        with patch.dict(os.environ, {"FAKE_ROOT": str(self.root)}):
            executable, _ = self._resolve(FakeLink(path=f"{template}{os.sep}app.exe"))
        self.assertEqual(Path(executable), self.target)

    def test_a_shortcut_to_a_document_is_not_treated_as_a_program(self) -> None:
        document = self.root / "readme.txt"
        document.write_text("x", encoding="utf-8")
        self.assertEqual(self._resolve(FakeLink(path=str(document))), ("", ""))

    def test_a_shortcut_to_a_deleted_program_resolves_to_nothing(self) -> None:
        self.assertEqual(
            self._resolve(FakeLink(path=str(self.root / "gone.exe"))), ("", "")
        )

    def test_an_unreadable_shortcut_is_survived(self) -> None:
        from core import app_index

        with fake_modules(pylnk3=FakePylnk3({})):
            self.assertEqual(app_index._resolve_shortcut_target(self.link), ("", ""))

    def test_without_pylnk3_the_shortcut_is_still_usable(self) -> None:
        """It just loses pid tracking — it must not become an error."""
        from core import app_index

        self.assertEqual(app_index._resolve_shortcut_target(self.link), ("", ""))


class StartMenuScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.programs = self.root / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        self.programs.mkdir(parents=True)
        self.target = make_exe(self.root, "editor.exe")
        for name in ("Editor.lnk", "Uninstall Editor.lnk", "Remove Widget.lnk"):
            (self.programs / name).write_bytes(b"L fake")
        (self.programs / "Games").mkdir()
        (self.programs / "Games" / "Solitaire.lnk").write_bytes(b"L fake")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _scan(self, links: dict):
        from core import app_index

        with patch.dict(os.environ, {"ProgramData": str(self.root), "APPDATA": ""}), \
             fake_modules(pylnk3=FakePylnk3(links)):
            return app_index._windows_start_menu_entries()

    def test_shortcuts_are_found_including_in_subfolders(self) -> None:
        entries = self._scan({})
        names = sorted(entry.name for entry in entries)
        self.assertIn("Editor", names)
        self.assertIn("Solitaire", names)

    def test_uninstallers_are_skipped(self) -> None:
        names = {entry.name for entry in self._scan({})}
        self.assertNotIn("Uninstall Editor", names)
        self.assertNotIn("Remove Widget", names)

    def test_a_resolved_shortcut_is_indexed_as_its_executable(self) -> None:
        entries = self._scan({
            str(self.programs / "Editor.lnk"): FakeLink(path=str(self.target))
        })
        editor = next(entry for entry in entries if entry.name == "Editor")
        self.assertEqual(editor.kind, "exec")
        self.assertEqual(Path(editor.target), self.target)

    def test_an_unresolved_shortcut_is_still_indexed_as_a_shortcut(self) -> None:
        entries = self._scan({})
        editor = next(entry for entry in entries if entry.name == "Editor")
        self.assertEqual(editor.kind, "lnk")
        self.assertTrue(editor.target.endswith("Editor.lnk"))


class StoreApplicationTests(unittest.TestCase):
    """Store apps are listed by Get-StartApps and launched through AppsFolder."""

    def _scan(self, stdout: str, returncode: int = 0):
        from core import app_index

        completed = subprocess.CompletedProcess(["powershell"], returncode, stdout, "")
        with patch.object(app_index.shutil, "which", return_value="powershell.exe"), \
             patch.object(app_index.subprocess, "run", return_value=completed):
            return app_index._windows_store_entries()

    def test_a_package_application_is_indexed_by_its_aumid(self) -> None:
        entries = self._scan("Spotify|SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify\n")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Spotify")
        self.assertEqual(entries[0].kind, "aumid")
        self.assertEqual(entries[0].source, "appsfolder")

    def test_desktop_entries_without_an_aumid_are_left_to_the_other_scans(self) -> None:
        self.assertEqual(self._scan("Notepad|C:\\Windows\\notepad.exe\n"), [])

    def test_a_failed_powershell_call_yields_nothing(self) -> None:
        self.assertEqual(self._scan("Spotify|A_b!App", returncode=1), [])

    def test_malformed_lines_are_skipped(self) -> None:
        entries = self._scan("no separator here\n|missing name\nName|\nGood|A_b!App\n")
        self.assertEqual([entry.name for entry in entries], ["Good"])

    def test_an_absurdly_long_application_id_is_refused(self) -> None:
        self.assertEqual(self._scan("Bad|" + "a" * 600 + "!App"), [])


class WindowsLaunchTests(unittest.TestCase):
    """Launching differs per kind, and each kind has its own refusal."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.exe = make_exe(self.root, "app.exe")
        self.link = self.root / "App.lnk"
        self.link.write_bytes(b"L fake")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_store_application_is_launched_through_explorer(self) -> None:
        from core import app_index
        from core.app_index import AppEntry

        entry = AppEntry("Spotify", "aumid", "SpotifyAB.SpotifyMusic_zpd!Spotify", "appsfolder")
        with patch.object(app_index, "_OS", "Windows"), \
             patch.dict(os.environ, {"WINDIR": r"C:\Windows"}), \
             patch.object(app_index, "_spawn", return_value=1234) as spawn:
            self.assertEqual(app_index.launch(entry), 1234)
        argv = spawn.call_args[0][0]
        self.assertTrue(argv[0].endswith("explorer.exe"))
        self.assertEqual(argv[1], r"shell:AppsFolder\SpotifyAB.SpotifyMusic_zpd!Spotify")

    def test_a_shortcut_is_opened_through_the_shell(self) -> None:
        from core import app_index
        from core.app_index import AppEntry

        entry = AppEntry("App", "lnk", str(self.link), "startmenu")
        startfile_calls = []
        with patch.object(app_index, "_OS", "Windows"), \
             patch.object(app_index.os, "startfile", startfile_calls.append, create=True):
            self.assertIsNone(app_index.launch(entry))
        self.assertEqual(startfile_calls, [str(self.link)])

    def test_a_uri_is_opened_through_the_shell_on_windows(self) -> None:
        from core import app_index
        from core.app_index import AppEntry

        entry = AppEntry("Settings", "uri", "ms-settings:display", "builtin")
        startfile_calls = []
        with patch.object(app_index, "_OS", "Windows"), \
             patch.object(app_index.os, "startfile", startfile_calls.append, create=True):
            self.assertIsNone(app_index.launch(entry))
        self.assertEqual(startfile_calls, ["ms-settings:display"])


class WindowsReminderTests(unittest.TestCase):
    """Scheduling through schtasks, without a Task Scheduler to talk to."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.script = Path(self.directory.name) / "notify.py"
        self.script.write_text("# notify", encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _schedule(self, returncode: int = 0):
        from datetime import datetime, timedelta

        from actions import reminder

        self.when = datetime.now() + timedelta(days=1)
        completed = subprocess.CompletedProcess(["schtasks"], returncode, "", "")
        self.xml_written = {}
        real_create = reminder.atomic_create_text

        def capture(path, content, *args, **kwargs):
            if str(path).endswith(".xml"):
                self.xml_written["content"] = content
            return real_create(path, content, *args, **kwargs)

        with patch.object(reminder, "atomic_create_text", capture), \
             patch.object(reminder.subprocess, "run", return_value=completed) as run:
            handle, backend = reminder._schedule_windows(
                self.when, "JARVISReminder_test", self.script, "take the bins out"
            )
        return handle, backend, run

    def test_a_task_is_created_from_an_xml_definition(self) -> None:
        handle, backend, run = self._schedule()
        self.assertEqual(backend, "schtasks")
        self.assertEqual(handle, "JARVISReminder_test")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["schtasks", "/Create", "/TN", "JARVISReminder_test"])
        self.assertIn("/XML", argv)
        self.assertIn("/F", argv)
        self.assertTrue(argv[argv.index("/XML") + 1].endswith(".xml"))

    def test_the_task_definition_carries_the_time_and_the_script(self) -> None:
        self._schedule()
        xml = self.xml_written.get("content", "")
        self.assertIn(self.when.strftime("%Y-%m-%dT%H:%M:%S"), xml)
        self.assertIn("notify.py", xml)
        self.assertIn("<TimeTrigger>", xml)

    def test_the_task_definition_file_is_not_left_behind(self) -> None:
        from actions import reminder

        self._schedule()
        leftovers = list(reminder._scripts_dir().glob("JARVISReminder_test.xml"))
        self.assertEqual(leftovers, [])

    def test_a_refused_creation_is_reported_as_a_failure(self) -> None:
        handle, backend, _run = self._schedule(returncode=1)
        self.assertEqual((handle, backend), ("", ""))

    def test_a_refused_creation_removes_the_notify_script(self) -> None:
        self._schedule(returncode=1)
        self.assertFalse(self.script.exists())

    def test_cancelling_deletes_the_task_by_name(self) -> None:
        from actions import reminder

        with patch.object(reminder, "_run", return_value=(True, "")) as run:
            removed, _detail = reminder._cancel_job(
                {"backend": "schtasks", "handle": "JARVISReminder_test"}
            )
        self.assertTrue(removed)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["schtasks", "/Delete"])
        self.assertIn("/F", argv)
        self.assertIn("JARVISReminder_test", argv)


class WindowsSettingsTests(unittest.TestCase):
    """Volume, brightness and Wi-Fi, with the platform calls stubbed out."""

    def test_brightness_uses_the_wmi_setter_and_reports_its_status(self) -> None:
        from actions import computer_settings

        ok = subprocess.CompletedProcess(["powershell"], 0, "", "")
        with patch.object(computer_settings, "_OS", "Windows"), \
             patch.object(computer_settings.subprocess, "run", return_value=ok) as run:
            self.assertTrue(computer_settings.brightness_set(40))
        command = run.call_args[0][0]
        self.assertEqual(command[0], "powershell")
        self.assertIn("WmiSetBrightness(1, 40)", " ".join(command))

    def test_a_desktop_monitor_refusing_brightness_is_reported(self) -> None:
        from actions import computer_settings

        failed = subprocess.CompletedProcess(["powershell"], 1, "", "not supported")
        with patch.object(computer_settings, "_OS", "Windows"), \
             patch.object(computer_settings.subprocess, "run", return_value=failed):
            self.assertFalse(computer_settings.brightness_set(40))

    def test_brightness_is_read_as_a_percentage(self) -> None:
        from actions import computer_settings

        ok = subprocess.CompletedProcess(["powershell"], 0, "65\n", "")
        with patch.object(computer_settings, "_OS", "Windows"), \
             patch.object(computer_settings.subprocess, "run", return_value=ok):
            self.assertEqual(computer_settings.brightness_get(), 65)

    def test_the_wifi_toggle_stops_on_a_powershell_error(self) -> None:
        from actions import computer_settings

        failed = subprocess.CompletedProcess(["powershell"], 1, "", "Access is denied.")
        with patch.object(computer_settings, "_OS", "Windows"), \
             patch.object(computer_settings.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as caught:
                computer_settings.toggle_wifi()
        self.assertIn("Access is denied", str(caught.exception))

    def test_the_wifi_script_fails_loudly_when_no_adapter_exists(self) -> None:
        """Without ErrorActionPreference the script would carry on and exit 0."""
        from actions import computer_settings

        ok = subprocess.CompletedProcess(["powershell"], 0, "", "")
        with patch.object(computer_settings, "_OS", "Windows"), \
             patch.object(computer_settings.subprocess, "run", return_value=ok) as run:
            computer_settings.toggle_wifi()
        script = " ".join(run.call_args[0][0])
        self.assertIn("ErrorActionPreference='Stop'", script)
        self.assertIn("No Wi-Fi adapter found", script)


class WindowsPathPolicyTests(unittest.TestCase):
    """Argument handling differs on Windows, where '/' introduces a switch."""

    def test_a_switch_is_refused(self) -> None:
        from core import app_index

        with patch.object(app_index, "_OS", "Windows"):
            for switch in ("/s", "/Q", "/delete", "/f"):
                with self.assertRaises(ValueError, msg=switch):
                    app_index.sanitise_arguments(switch)

    def test_an_existing_path_starting_with_a_slash_is_allowed(self) -> None:
        """On a POSIX-style path that really exists, '/' is not a switch."""
        from core import app_index

        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            with patch.object(app_index, "_OS", "Windows"):
                self.assertEqual(
                    app_index.sanitise_arguments(handle.name), [handle.name]
                )

    def test_a_dash_argument_is_refused_on_every_platform(self) -> None:
        from core import app_index

        with patch.object(app_index, "_OS", "Windows"):
            with self.assertRaises(ValueError):
                app_index.sanitise_arguments("--headless")


if __name__ == "__main__":
    unittest.main()


class WindowEnumerationTests(unittest.TestCase):
    """The user32 window list: the source of every handle, pid and rectangle.

    On Linux this function is never executed, so a wrong buffer size or a
    missed visibility check would only ever be found by a Windows user.
    """

    def _list(self, windows):
        import ctypes

        from core import window_manager
        from tests.windows_fakes import FakeUser32

        user32 = FakeUser32(windows)
        fake_windll = type("WinDLL", (), {"user32": user32})()

        class FakeWintypes:
            HWND = ctypes.c_void_p
            LPARAM = ctypes.c_ssize_t
            DWORD = ctypes.c_ulong

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long),
                ]

        # WINFUNCTYPE only exists on Windows; CFUNCTYPE has the same shape for
        # the purposes of driving the callback here.
        winfunctype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        with patch.object(window_manager.ctypes, "windll", fake_windll, create=True), \
             patch.object(window_manager.ctypes, "wintypes", FakeWintypes, create=True), \
             patch.object(window_manager.ctypes, "WINFUNCTYPE", winfunctype, create=True), \
             patch.object(window_manager, "_process_name", lambda pid: f"proc{pid}.exe"):
            return window_manager._windows_native(), user32

    def test_visible_titled_windows_are_listed(self) -> None:
        windows, _ = self._list([
            {"hwnd": 101, "title": "Report.docx - Word", "pid": 4242,
             "rect": (0, 0, 1920, 1080)},
            {"hwnd": 102, "title": "Inbox - Outlook", "pid": 4243,
             "rect": (100, 100, 900, 700)},
        ])
        self.assertEqual([w.title for w in windows],
                         ["Report.docx - Word", "Inbox - Outlook"])
        self.assertEqual([w.handle for w in windows], [101, 102])
        self.assertEqual([w.pid for w in windows], [4242, 4243])

    def test_the_rectangle_is_read_from_the_api(self) -> None:
        windows, _ = self._list([
            {"hwnd": 1, "title": "W", "pid": 9, "rect": (10, 20, 810, 620)},
        ])
        window = windows[0]
        self.assertEqual(
            (window.left, window.top, window.right, window.bottom), (10, 20, 810, 620)
        )

    def test_hidden_windows_are_skipped(self) -> None:
        windows, _ = self._list([
            {"hwnd": 1, "title": "Hidden", "pid": 9, "visible": False},
            {"hwnd": 2, "title": "Shown", "pid": 9},
        ])
        self.assertEqual([w.title for w in windows], ["Shown"])

    def test_untitled_windows_are_skipped(self) -> None:
        """The desktop is full of invisible titleless helper windows."""
        windows, _ = self._list([
            {"hwnd": 1, "title": "", "pid": 9},
            {"hwnd": 2, "title": "   ", "pid": 9},
            {"hwnd": 3, "title": "Real", "pid": 9},
        ])
        self.assertEqual([w.title for w in windows], ["Real"])

    def test_a_window_whose_rectangle_cannot_be_read_is_skipped(self) -> None:
        windows, _ = self._list([
            {"hwnd": 1, "title": "Closing", "pid": 9, "has_rect": False},
            {"hwnd": 2, "title": "Fine", "pid": 9},
        ])
        self.assertEqual([w.title for w in windows], ["Fine"])

    def test_minimised_and_maximised_states_are_reported(self) -> None:
        windows, _ = self._list([
            {"hwnd": 1, "title": "Min", "pid": 9, "minimized": True},
            {"hwnd": 2, "title": "Max", "pid": 9, "maximized": True},
        ])
        self.assertTrue(windows[0].minimized)
        self.assertFalse(windows[0].maximized)
        self.assertTrue(windows[1].maximized)

    def test_the_process_name_is_resolved_from_the_pid(self) -> None:
        windows, _ = self._list([{"hwnd": 1, "title": "W", "pid": 777}])
        self.assertEqual(windows[0].process, "proc777.exe")

    def test_enumeration_actually_uses_enumwindows(self) -> None:
        _windows, user32 = self._list([{"hwnd": 1, "title": "W", "pid": 9}])
        self.assertIn("EnumWindows", user32.calls)
