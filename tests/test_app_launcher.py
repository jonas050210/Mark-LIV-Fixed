"""Regression tests for the indexed launcher and the focus-safety guards.

These cover the behaviours that previously went wrong:

* an application launch fell through to pressing the Windows key and typing the
  name into the Start menu, then reported success unconditionally;
* a close request was carried out with alt+f4, which hits whichever window has
  focus rather than the named one;
* alias resolution matched on substrings, so 'code' matched inside 'vscode';
* there was no way to open an application in the background, on a chosen
  monitor, or in fullscreen.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core import app_index
from core.app_index import AppEntry
from core.window_manager import MonitorInfo, WindowInfo
from actions import computer_control, open_app


def _entry(name: str, target: str = "/usr/bin/app", kind: str = "exec") -> AppEntry:
    return AppEntry(name=name, kind=kind, target=target, source="registry")


def _window(handle: int = 1, title: str = "Google Chrome", process: str = "chrome",
            pid: int = 10, left: int = 0, right: int = 800) -> WindowInfo:
    return WindowInfo(
        handle=handle, title=title, process=process, pid=pid,
        left=left, top=0, right=right, bottom=600,
    )


_MONITORS = [
    MonitorInfo(1, "primary", 0, 0, 1920, 1080, 0, 0, 1920, 1040, primary=True),
    MonitorInfo(2, "secondary", 1920, 0, 3840, 1080, 1920, 0, 3840, 1040),
]


class HotkeyGuardTests(unittest.TestCase):
    """Focus-dependent combinations must be refused, not sent."""

    def test_alt_f4_is_refused_and_names_the_safe_alternative(self) -> None:
        with patch.object(computer_control, "pyautogui", create=True) as gui:
            result = computer_control._hotkey("alt", "f4")
        self.assertIn("Refused", result)
        self.assertIn("window_manager", result)
        gui.hotkey.assert_not_called()

    def test_command_q_and_ctrl_w_are_refused(self) -> None:
        for keys in (("command", "q"), ("ctrl", "w"), ("alt", "tab")):
            with patch.object(computer_control, "pyautogui", create=True) as gui:
                result = computer_control._hotkey(*keys)
            self.assertIn("Refused", result, keys)
            gui.hotkey.assert_not_called()

    def test_windows_key_alone_is_refused(self) -> None:
        with patch.object(computer_control, "pyautogui", create=True) as gui:
            result = computer_control._press("win")
        self.assertIn("Refused", result)
        gui.press.assert_not_called()

    def test_aliases_are_canonicalised_before_the_check(self) -> None:
        with patch.object(computer_control, "pyautogui", create=True) as gui:
            result = computer_control._hotkey("Cmd", "Q")
        self.assertIn("Refused", result)
        gui.hotkey.assert_not_called()

    def test_ordinary_in_app_shortcuts_still_work(self) -> None:
        with patch.object(computer_control, "pyautogui", create=True) as gui, \
             patch.object(computer_control, "_PYAUTOGUI", True):
            result = computer_control._hotkey("ctrl", "s")
        self.assertEqual(result, "Hotkey: ctrl+s")
        gui.hotkey.assert_called_once_with("ctrl", "s")

    def test_the_guard_runs_before_pyautogui_is_required(self) -> None:
        """A refusal must be an explanation, not a missing-dependency error."""
        with patch.object(computer_control, "_PYAUTOGUI", False):
            self.assertIn("Refused", computer_control._hotkey("alt", "f4"))


class AliasResolutionTests(unittest.TestCase):
    """Alias lookup must not match on accidental substrings."""

    def test_unrelated_word_containing_an_alias_is_not_rewritten(self) -> None:
        self.assertEqual(open_app._alias_target("digital"), "digital")

    def test_exact_alias_resolves_to_this_platform_s_name(self) -> None:
        # On Windows the canonical name happens to be "chrome" itself, so
        # asserting that the text changed only holds on the other platforms.
        expected = open_app._APP_ALIASES["chrome"][open_app._SYSTEM]
        self.assertEqual(open_app._alias_target("chrome"), expected)

    def test_multiword_request_prefers_the_full_alias(self) -> None:
        self.assertEqual(
            open_app._alias_target("visual studio code"),
            open_app._alias_target("vscode"),
        )


class IndexScoringTests(unittest.TestCase):
    def test_exact_name_outranks_a_fuzzy_neighbour(self) -> None:
        exact = _entry("Discord")
        fuzzy = _entry("Discord PTB")
        self.assertGreater(
            app_index.score_entry("discord", exact),
            app_index.score_entry("discord", fuzzy),
        )

    def test_unrelated_entries_score_zero(self) -> None:
        self.assertEqual(app_index.score_entry("chrome", _entry("Blender")), 0.0)

    def test_resolve_ranks_the_exact_match_first(self) -> None:
        pool = [_entry("Steam Cleaner"), _entry("Steam"), _entry("Steamworks SDK")]
        matches = app_index.resolve("steam", entries=pool)
        self.assertEqual(matches[0].name, "Steam")

    def test_a_uri_is_recognised_and_a_path_is_not(self) -> None:
        self.assertTrue(app_index.is_uri("ms-settings:"))
        self.assertTrue(app_index.is_uri("https://example.com"))
        self.assertFalse(app_index.is_uri(r"C:\\Program Files\\app.exe"))
        self.assertFalse(app_index.is_uri("chrome"))


class LaunchHonestyTests(unittest.TestCase):
    """A launch must never claim success it did not verify."""

    def test_unknown_application_is_reported_instead_of_typed_into_the_start_menu(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([], True)), \
             patch.object(open_app, "_nearby_names", return_value=""), \
             patch.object(open_app, "_launch_resolved") as launch:
            result = open_app.open_app({"app_name": "Nonexistent Thing"})
        self.assertIn("could not find an installed application", result)
        launch.assert_not_called()

    def test_launch_without_a_window_is_not_called_a_success(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 4242, "")), \
             patch.object(open_app, "_await_launched_window", return_value=None):
            result = open_app.open_app({"app_name": "Chrome"})
        self.assertIn("no window appeared", result)
        self.assertNotIn("Opened Chrome.", result)

    def test_launch_failure_is_reported_with_its_reason(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(False, None, "it is no longer installed")):
            result = open_app.open_app({"app_name": "Chrome"})
        self.assertIn("could not start", result)
        self.assertIn("no longer installed", result)

    def test_ambiguous_match_asks_instead_of_guessing(self) -> None:
        candidates = [_entry("Visual Studio"), _entry("Visual Studio Code")]
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=(candidates, False)), \
             patch.object(open_app, "_launch_resolved") as launch:
            result = open_app.open_app({"app_name": "visual studio thing"})
        self.assertIn("more than one installed application", result)
        launch.assert_not_called()


class SelfHealingLaunchTests(unittest.TestCase):
    def test_stale_executable_is_rescanned_and_retried_once(self) -> None:
        stale = _entry("Editor", "/old/editor.exe")
        repaired = _entry("Editor", "/new/editor.exe")
        with patch.object(open_app, "_launch_resolved", side_effect=[
                 (False, None, "Editor is no longer installed at its indexed location"),
                 (True, 77, ""),
             ]) as launch, \
             patch.object(open_app, "load_index", return_value=[repaired]) as load:
            started, pid, failure, entry, refreshed = open_app._launch_with_repair(
                stale, [], "Editor"
            )
        self.assertTrue(started)
        self.assertEqual(pid, 77)
        self.assertEqual(failure, "")
        self.assertEqual(entry, repaired)
        self.assertTrue(refreshed)
        load.assert_called_once_with(refresh=True)
        self.assertEqual(launch.call_count, 2)

    def test_permission_failure_is_not_retried(self) -> None:
        entry = _entry("Editor")
        with patch.object(open_app, "_launch_resolved", return_value=(False, None, "AccessDenied")), \
             patch.object(open_app, "load_index") as load:
            result = open_app._launch_with_repair(entry, [], "Editor")
        self.assertFalse(result[0])
        self.assertFalse(result[4])
        load.assert_not_called()

    def test_browser_tab_does_not_suppress_installed_web_app_launch(self) -> None:
        browser = _window(title="Twitch - Google Chrome", process="chrome.exe")
        web_app = AppEntry("Twitch", "lnk", r"C:\\Twitch.lnk", "webapp")
        with patch.object(open_app, "_SYSTEM", "Windows"), \
             patch.object(open_app, "load_index", return_value=[web_app]):
            self.assertTrue(open_app._browser_title_match_may_be_a_web_app(
                [browser], "Twitch"
            ))


class PlacementTests(unittest.TestCase):
    """Monitor and window-state requests must be applied and verified."""

    def test_open_on_second_monitor_with_spoken_fullscreen_uses_native_maximize(self) -> None:
        placed = _window(handle=7, left=1920, right=3840)
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=_window(handle=7)), \
             patch("core.window_manager.monitor_for", return_value=_MONITORS[1]), \
             patch("core.window_manager.place_window", return_value=placed) as place:
            result = open_app.open_app(
                {"app_name": "Chrome", "monitor": 2, "state": "fulscreen"}
            )
        self.assertIn("monitor 2", result)
        self.assertIn("maximised", result)
        self.assertEqual(place.call_args[0][2], "maximized")

    def test_unverified_monitor_move_is_reported(self) -> None:
        still_on_monitor_one = _window(handle=7, left=0, right=800)
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=_window(handle=7)), \
             patch("core.window_manager.monitor_for", return_value=_MONITORS[1]), \
             patch("core.window_manager.place_window", return_value=still_on_monitor_one):
            result = open_app.open_app({"app_name": "Chrome", "monitor": 2})
        self.assertIn("could not verify", result)

    def test_invalid_state_is_rejected_before_launching(self) -> None:
        with patch.object(open_app, "_launch_resolved") as launch:
            result = open_app.open_app({"app_name": "Chrome", "state": "sideways"})
        self.assertIn("state must be one of", result)
        launch.assert_not_called()


class BackgroundLaunchTests(unittest.TestCase):
    """Opening without stealing focus must restore the previous foreground."""

    def test_background_launch_restores_the_previous_foreground_window(self) -> None:
        previous = _window(handle=1, title="Editor", process="code")
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=_window(handle=7)), \
             patch("core.window_manager.foreground_window", return_value=previous), \
             patch("core.window_manager.restore_foreground") as restore, \
             patch("core.window_manager.operate"):
            result = open_app.open_app({"app_name": "Chrome", "foreground": False})
        self.assertIn("in the background", result)
        restore.assert_called_once_with(previous)

    def test_already_open_app_is_left_alone_when_background_is_requested(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[_window()]), \
             patch.object(open_app, "_focus_window") as focus:
            result = open_app.open_app({"app_name": "Chrome", "foreground": False})
        self.assertIn("left it in the background", result)
        focus.assert_not_called()

    def test_already_open_app_is_moved_when_a_placement_is_requested(self) -> None:
        placed = _window(handle=1, left=1920, right=3840)
        with patch.object(open_app, "_matching_windows", return_value=[_window()]), \
             patch("core.window_manager.monitor_for", return_value=_MONITORS[1]), \
             patch("core.window_manager.place_window", return_value=placed):
            result = open_app.open_app({"app_name": "Chrome", "monitor": 2})
        self.assertIn("was already open", result)
        self.assertIn("monitor 2", result)


class NoKeyboardFallbackTests(unittest.TestCase):
    """The Start-menu/Spotlight typing fallback must be gone for good."""

    def test_open_app_never_imports_pyautogui(self) -> None:
        source = open_app.__file__
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("pyautogui", text)
        self.assertNotIn('press("win")', text)


class SemanticMonitorTests(unittest.TestCase):
    """Spoken monitor references ('primary', 'left', 'monitor 2') must resolve."""

    def test_primary_and_main_resolve_to_the_primary_display(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            self.assertEqual(window_manager.resolve_monitor_token("primary"), 1)
            self.assertEqual(window_manager.resolve_monitor_token("main"), 1)
            self.assertEqual(window_manager.resolve_monitor_token("main monitor"), 1)

    def test_secondary_and_second_resolve_to_the_other_display(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            self.assertEqual(window_manager.resolve_monitor_token("secondary"), 2)
            self.assertEqual(window_manager.resolve_monitor_token("second"), 2)
            self.assertEqual(window_manager.resolve_monitor_token("second monitor"), 2)

    def test_left_and_right_resolve_by_physical_position(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            self.assertEqual(window_manager.resolve_monitor_token("left"), 1)
            self.assertEqual(window_manager.resolve_monitor_token("right"), 2)

    def test_explicit_number_wins_even_when_phrased(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            self.assertEqual(window_manager.resolve_monitor_token("monitor 2"), 2)
            self.assertEqual(window_manager.resolve_monitor_token("display 1"), 1)
            self.assertEqual(window_manager.resolve_monitor_token("2"), 2)
            self.assertEqual(window_manager.resolve_monitor_token(2), 2)

    def test_an_out_of_range_monitor_raises_a_clear_error(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            with self.assertRaises(ValueError):
                window_manager.resolve_monitor_token("3")

    def test_only_one_monitor_makes_secondary_an_error(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=[_MONITORS[0]]):
            with self.assertRaises(ValueError):
                window_manager.resolve_monitor_token("secondary")

    def test_an_unrecognised_word_raises_a_clear_error(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            with self.assertRaises(ValueError):
                window_manager.resolve_monitor_token("television")

    def test_monitor_for_accepts_semantic_strings(self) -> None:
        from core import window_manager
        with patch.object(window_manager, "list_monitors", return_value=_MONITORS):
            self.assertEqual(window_manager.monitor_for("primary").index, 1)
            self.assertEqual(window_manager.monitor_for("secondary").index, 2)

    def test_open_app_places_a_window_with_a_semantic_monitor_name(self) -> None:
        placed = _window(handle=7, left=1920, right=3840)
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=_window(handle=7)), \
             patch("core.window_manager.list_monitors", return_value=_MONITORS), \
             patch("core.window_manager.place_window", return_value=placed):
            result = open_app.open_app(
                {"app_name": "Chrome", "monitor": "secondary", "state": "fullscreen"}
            )
        self.assertIn("monitor 2", result)
        self.assertIn("maximised", result)


class DirectShortcutLaunchTests(unittest.TestCase):
    """A personal Desktop shortcut must launch directly, not via the app index."""

    def test_a_saved_lnk_shortcut_launches_without_the_installed_app_index(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "YouTube.lnk"
            link.write_bytes(b"L\x00\x00\x00 fake shortcut")
            with patch.object(open_app, "resolve_shortcut", return_value=str(link)), \
                 patch.object(open_app, "_matching_windows", return_value=[]), \
                 patch.object(open_app, "_resolve_candidates") as resolve_candidates, \
                 patch.object(open_app, "launch_path", return_value=None) as launch_path, \
                 patch.object(open_app, "_await_launched_window", return_value=_window(handle=9, title="YouTube")):
                result = open_app.open_app({"app_name": "youtube"})
        self.assertIn("saved shortcut", result)
        self.assertIn("Opened", result)
        launch_path.assert_called_once()
        resolve_candidates.assert_not_called()

    def test_a_missing_shortcut_target_is_reported_not_silently_dropped(self) -> None:
        with patch.object(open_app, "resolve_shortcut", return_value=r"C:\Users\jonas\Desktop\Ghost.lnk"), \
             patch.object(open_app, "_matching_windows", return_value=[]):
            result = open_app.open_app({"app_name": "ghost"})
        # The path does not exist, so this must fall through to ordinary
        # installed-app resolution rather than being treated as a direct target.
        self.assertIn("could not find an installed application", result)


class StructuredResultTests(unittest.TestCase):
    """open_app_result must report a real ok flag, not prose to be re-parsed."""

    def test_a_verified_launch_reports_ok_true(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=_window(handle=7)):
            ok, message = open_app.open_app_result({"app_name": "Chrome"})
        self.assertTrue(ok)
        self.assertIn("Opened Chrome", message)

    def test_a_missing_window_reports_ok_false(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 4242, "")), \
             patch.object(open_app, "_await_launched_window", return_value=None):
            ok, message = open_app.open_app_result({"app_name": "Chrome"})
        self.assertFalse(ok)
        self.assertIn("no window appeared", message)

    def test_registry_handler_preserves_a_verified_launch_failure_status(self) -> None:
        with patch.object(open_app, "open_app_result", return_value=(False, "I started Chrome, but no window appeared.")):
            result = open_app._open_app_action({"app_name": "Chrome"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("no window appeared", result["message"])

    def test_a_partial_placement_failure_reports_ok_false(self) -> None:
        still_on_monitor_one = _window(handle=7, left=0, right=800)
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([_entry("Chrome")], False)), \
             patch.object(open_app, "_launch_resolved", return_value=(True, 99, "")), \
             patch.object(open_app, "_await_launched_window", return_value=_window(handle=7)), \
             patch("core.window_manager.monitor_for", return_value=_MONITORS[1]), \
             patch("core.window_manager.place_window", return_value=still_on_monitor_one):
            ok, message = open_app.open_app_result({"app_name": "Chrome", "monitor": 2})
        self.assertFalse(ok)
        self.assertIn("could not verify", message)

    def test_an_already_open_app_reports_ok_true(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[_window()]), \
             patch.object(open_app, "_focus_window", return_value=True):
            ok, message = open_app.open_app_result({"app_name": "Chrome"})
        self.assertTrue(ok)
        self.assertIn("already open", message)

    def test_open_app_and_open_app_result_agree_on_the_message(self) -> None:
        with patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_resolve_candidates", return_value=([], True)), \
             patch.object(open_app, "_nearby_names", return_value=""):
            message = open_app.open_app({"app_name": "Nonexistent Thing"})
            ok, message_again = open_app.open_app_result({"app_name": "Nonexistent Thing"})
        self.assertFalse(ok)
        self.assertEqual(message, message_again)


class CancellationDuringWaitTests(unittest.TestCase):
    """A cancelled launch must not sit out the full window-wait timeout."""

    def test_a_cancel_set_before_launching_is_honoured_immediately(self) -> None:
        import threading
        cancel_event = threading.Event()
        cancel_event.set()
        with patch.object(open_app, "_launch_resolved") as launch:
            ok, message = open_app.open_app_result(
                {"app_name": "Chrome"}, cancel_event=cancel_event,
            )
        self.assertFalse(ok)
        self.assertIn("cancelled", message)
        launch.assert_not_called()

    def test_a_cancel_set_mid_wait_stops_the_20s_wait_almost_immediately(self) -> None:
        import threading
        import time
        cancel_event = threading.Event()
        threading.Timer(0.05, cancel_event.set).start()

        started = time.monotonic()
        window = open_app._await_launched_window(
            "Chrome", "chrome", None, set(), timeout=20.0, cancel_event=cancel_event,
        )
        elapsed = time.monotonic() - started

        self.assertIsNone(window)
        self.assertLess(elapsed, 5.0)

    def test_a_pre_cancelled_wait_skips_polling_and_does_a_single_final_read(self) -> None:
        import threading
        cancel_event = threading.Event()
        cancel_event.set()
        with patch.object(open_app, "_matching_windows", return_value=[]) as matching:
            matches, new_windows = open_app._wait_for_windows(
                "Chrome", "chrome", 1, cancel_event=cancel_event,
            )
        self.assertEqual(matches, [])
        self.assertEqual(new_windows, [])
        # Cancelled before the first poll: only the unconditional final read
        # happens, none of the up-to-8s of repeated polling.
        matching.assert_called_once()

    def test_direct_shortcut_launch_honours_cancellation(self) -> None:
        import tempfile
        import threading
        from pathlib import Path
        cancel_event = threading.Event()
        cancel_event.set()
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "YouTube.lnk"
            link.write_bytes(b"L\x00\x00\x00 fake shortcut")
            with patch.object(open_app, "resolve_shortcut", return_value=str(link)), \
                 patch.object(open_app, "_matching_windows", return_value=[]), \
                 patch.object(open_app, "launch_path", return_value=None):
                ok, message = open_app.open_app_result(
                    {"app_name": "youtube"}, cancel_event=cancel_event,
                )
        self.assertFalse(ok)
        self.assertIn("cancelled", message)


if __name__ == "__main__":
    unittest.main()
