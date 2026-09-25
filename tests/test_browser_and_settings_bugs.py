"""Regression tests for the browser and system-settings bug hunt.

Four defects are covered here. Two are honesty defects — an operation the
system refused was reported as done — and two are in the URL guard, which is
the only thing standing between a model-supplied address and the user's
browser.
"""
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from actions import browser_control, computer_settings


class UrlGuardTests(unittest.TestCase):
    """A browser resolves more host spellings than ``ipaddress`` does."""

    def _rejected(self, url: str) -> str:
        with self.assertRaises(ValueError, msg=url) as caught:
            browser_control._normalize_url(url)
        return str(caught.exception)

    def test_a_decimal_ip_literal_cannot_reach_loopback(self) -> None:
        # http://2130706433/ is exactly http://127.0.0.1/ to a browser.
        self.assertIn("Private", self._rejected("http://2130706433/"))

    def test_a_hexadecimal_ip_literal_cannot_reach_loopback(self) -> None:
        self.assertIn("Private", self._rejected("http://0x7f000001/"))

    def test_an_octal_ip_literal_cannot_reach_loopback(self) -> None:
        self.assertIn("Private", self._rejected("http://017700000001/"))

    def test_the_cloud_metadata_endpoint_stays_blocked_in_every_spelling(self) -> None:
        for spelling in ("http://169.254.169.254/", "http://2852039166/", "http://0xa9fea9fe/"):
            self.assertIn("Private", self._rejected(spelling))

    def test_a_public_decimal_literal_is_still_allowed(self) -> None:
        # 134744072 is 8.8.8.8 — global, so it must not be refused.
        self.assertEqual(
            browser_control._normalize_url("http://134744072/"), "http://134744072/"
        )

    def test_a_non_web_scheme_says_which_schemes_are_allowed(self) -> None:
        for url in ("javascript:alert(1)", "mailto:someone@example.com", "data:text/html,x"):
            self.assertIn("HTTP", self._rejected(url), url)

    def test_ordinary_addresses_still_work(self) -> None:
        self.assertEqual(browser_control._normalize_url("example.com"), "https://example.com")
        self.assertEqual(browser_control._normalize_url("instagram"), "https://instagram.com")

    def test_host_as_ip_returns_none_for_a_real_name(self) -> None:
        self.assertIsNone(browser_control._host_as_ip("example.com"))


class BrowserDispatchTests(unittest.TestCase):
    def test_an_unknown_action_never_starts_a_browser(self) -> None:
        """Starting Playwright to find out the action was misspelled is a
        ten-second answer to a question that needs none."""
        with patch.object(browser_control._registry, "get") as get:
            result = browser_control.browser_control({"action": "realod"})
        get.assert_not_called()
        self.assertIn("Unknown browser action", result)
        self.assertIn("reload", result)

    def test_the_schema_and_the_dispatcher_agree_on_the_action_list(self) -> None:
        declared = set(browser_control.TOOL["parameters"]["properties"]["action"]["enum"])
        implemented = browser_control._INTERACTIVE_ACTIONS | browser_control._DIRECT_ACTIONS
        self.assertEqual(declared, implemented)

    def test_a_nonsense_scroll_amount_does_not_raise(self) -> None:
        self.assertEqual(browser_control._pixels("a lot"), 500)
        self.assertEqual(browser_control._pixels("250"), 250)
        self.assertEqual(browser_control._pixels(10**9), 20_000)
        self.assertEqual(browser_control._pixels(None), 500)


class VolumeHonestyTests(unittest.TestCase):
    """A volume that did not change must not be reported as changed."""

    def test_a_refused_volume_change_is_reported_as_a_failure(self) -> None:
        with patch.object(computer_settings, "_PYAUTOGUI", True), \
             patch.object(computer_settings, "volume_get", return_value=30), \
             patch.object(computer_settings, "volume_set", return_value=False):
            result = computer_settings.computer_settings({"action": "volume_set", "value": 70})
        self.assertIn("could not set the volume", result)
        self.assertNotIn("Volume set to", result)

    def test_an_accepted_volume_change_is_confirmed(self) -> None:
        with patch.object(computer_settings, "_PYAUTOGUI", True), \
             patch.object(computer_settings, "volume_get", return_value=30), \
             patch.object(computer_settings, "volume_set", return_value=True):
            result = computer_settings.computer_settings({"action": "volume_set", "value": 70})
        self.assertIn("Volume set to 70%", result)

    def test_volume_set_reports_the_exit_status_on_linux(self) -> None:
        ok = subprocess.CompletedProcess(["pactl"], 0, "", "")
        bad = subprocess.CompletedProcess(["pactl"], 1, "", "Connection refused")
        with patch.object(computer_settings, "_OS", "Linux"):
            with patch.object(computer_settings.subprocess, "run", return_value=ok):
                self.assertTrue(computer_settings.volume_set(40))
            with patch.object(computer_settings.subprocess, "run", return_value=bad):
                self.assertFalse(computer_settings.volume_set(40))

    def test_a_missing_audio_tool_is_a_failure_not_an_exception(self) -> None:
        with patch.object(computer_settings, "_OS", "Linux"), \
             patch.object(computer_settings.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(computer_settings.volume_set(40))

    def test_brightness_set_reports_the_exit_status(self) -> None:
        bad = subprocess.CompletedProcess(["brightnessctl"], 1, "", "no such device")
        with patch.object(computer_settings, "_OS", "Linux"), \
             patch.object(computer_settings.subprocess, "run", return_value=bad):
            self.assertFalse(computer_settings.brightness_set(50))


class RefusedOperationTests(unittest.TestCase):
    """A command that exits non-zero must not become "Done"."""

    def test_must_run_raises_with_the_command_s_own_message(self) -> None:
        bad = subprocess.CompletedProcess(["nmcli"], 1, "", "Error: Access denied.")
        with patch.object(computer_settings.subprocess, "run", return_value=bad):
            with self.assertRaises(RuntimeError) as caught:
                computer_settings._must_run(["nmcli", "radio", "wifi"])
        self.assertIn("Access denied", str(caught.exception))

    def test_a_refused_wifi_toggle_is_not_reported_as_done(self) -> None:
        with patch.object(computer_settings, "_PYAUTOGUI", True), \
             patch.dict(computer_settings.ACTION_MAP, {
                 "toggle_wifi": lambda: (_ for _ in ()).throw(RuntimeError("Access is denied."))
             }):
            result = computer_settings.computer_settings({"action": "toggle_wifi"})
        self.assertNotIn("Done", result)
        self.assertIn("Access is denied", result)

    def test_a_missing_tool_is_explained_rather_than_named_by_exception(self) -> None:
        with patch.object(computer_settings, "_PYAUTOGUI", True), \
             patch.dict(computer_settings.ACTION_MAP, {
                 "toggle_wifi": lambda: (_ for _ in ()).throw(FileNotFoundError("nmcli"))
             }):
            result = computer_settings.computer_settings({"action": "toggle_wifi"})
        self.assertIn("no tool for it", result)

    def test_a_working_action_still_reports_done(self) -> None:
        with patch.object(computer_settings, "_PYAUTOGUI", True), \
             patch.dict(computer_settings.ACTION_MAP, {"toggle_wifi": lambda: None}):
            result = computer_settings.computer_settings({"action": "toggle_wifi"})
        self.assertIn("Done", result)


if __name__ == "__main__":
    unittest.main()


class AutomationProfileTests(unittest.TestCase):
    """The automation profile holds signed-in sessions, so it is owner-only.

    The fallback profile exists precisely so that accounts stay logged in
    between sessions. It was created with the default umask, which on a shared
    machine leaves another user able to read the cookie jar.
    """

    def test_the_profile_directory_is_private(self) -> None:
        import os
        import shutil
        import stat as stat_module

        directory = browser_control._automation_profile("unit_test_browser")
        try:
            if os.name == "nt":
                self.skipTest("POSIX permission bits do not apply on Windows")
            mode = stat_module.S_IMODE(os.stat(directory).st_mode)
            self.assertEqual(mode & 0o077, 0, oct(mode))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_an_existing_loose_directory_is_tightened(self) -> None:
        import os
        import shutil
        import stat as stat_module
        from pathlib import Path

        if os.name == "nt":
            self.skipTest("POSIX permission bits do not apply on Windows")
        target = Path.home() / ".jarvis_profiles" / "unit_test_loose"
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o755)
        try:
            browser_control._automation_profile("unit_test_loose")
            mode = stat_module.S_IMODE(target.stat().st_mode)
            self.assertEqual(mode & 0o077, 0, oct(mode))
        finally:
            shutil.rmtree(target, ignore_errors=True)

    def test_page_text_is_delivered_as_untrusted_content(self) -> None:
        """get_text returns whatever the site chose to put on the page."""
        import inspect

        source = inspect.getsource(browser_control._BrowserSession.get_text)
        self.assertIn("wrap(", source)
        self.assertIn("untrusted", source)
