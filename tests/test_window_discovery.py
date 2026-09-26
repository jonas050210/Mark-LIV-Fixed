"""Window discovery: the fixes behind "the assistant cannot see my apps".

Three real-world failure modes are covered here:

1. A generic target ("browser") never matched any real browser process, so the
   user saying "make the Browser fullscreen" was told nothing was open even
   with three browser windows on screen.
2. A page opened seconds ago may still show "New Tab" as its title, so a
   strict title lookup misses the window the user is looking at. The action
   layer now retries briefly and can fall back to the window this assistant
   itself launched, or to the browser that received the last URL.
3. When nothing matches, the failure message now lists what IS visible so the
   model can correct itself in the same conversation instead of insisting
   "it is not open".
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core import browser_handoff
from core.window_manager import (
    WindowInfo,
    clear_launch_memory,
    find_window,
    remember_last_window,
)
from core import undo as undo_stack
from actions import window_manager as window_manager_module
from actions.window_manager import window_manager


def _win(handle: int, title: str, process: str, pid: int = 0) -> WindowInfo:
    return WindowInfo(handle, title, process, pid, 0, 0, 1280, 720)


class GenericBrowserWordTests(unittest.TestCase):
    def test_browser_matches_any_browser_process(self) -> None:
        windows = [
            _win(1, "Funny cat compilation", "chrome.exe", 11),
            _win(2, "Discord", "Discord.exe", 12),
        ]
        with patch("core.window_manager.list_windows", return_value=windows):
            self.assertEqual(find_window("Browser").handle, 1)
            self.assertEqual(find_window("browser").handle, 1)
            self.assertEqual(find_window("Internet").handle, 1)

    def test_browser_word_does_not_grab_non_browser_windows(self) -> None:
        windows = [_win(2, "Meeting notes", "notepad.exe", 12)]
        with patch("core.window_manager.list_windows", return_value=windows):
            self.assertIsNone(find_window("Browser"))

    def test_browser_word_does_not_outweigh_an_exact_title_match(self) -> None:
        windows = [
            _win(1, "Inbox", "chrome.exe", 11),
            _win(2, "Firefox", "firefox.exe", 13),
        ]
        with patch("core.window_manager.list_windows", return_value=windows):
            # "firefox" is an exact process match (100) and beats the generic
            # browser alias (85), so the specific request still wins.
            self.assertEqual(find_window("Firefox").handle, 2)


class NotFoundMessageTests(unittest.TestCase):
    def test_failure_lists_visible_windows_for_self_correction(self) -> None:
        windows = [_win(1, "YouTube - Google Chrome", "chrome.exe", 11)]
        with patch("actions.window_manager.find_windows", return_value=[]), \
             patch("actions.window_manager.list_windows", return_value=windows), \
             patch.object(window_manager_module, "_SETTLE_TIMEOUT_SECONDS", 0.05), \
             patch.object(window_manager_module, "_SETTLE_POLL_SECONDS", 0.001):
            result = window_manager({"action": "focus", "target": "YouTube"})
        self.assertIn("could not find", result.casefold())
        self.assertIn("YouTube - Google Chrome", result)
        self.assertIn("chrome", result)

    def test_failure_explains_an_unreadable_desktop(self) -> None:
        with patch("actions.window_manager.find_windows", return_value=[]), \
             patch("actions.window_manager.list_windows", return_value=[]), \
             patch("actions.window_manager.backend_name", return_value="none"), \
             patch.object(window_manager_module, "_SETTLE_TIMEOUT_SECONDS", 0.05), \
             patch.object(window_manager_module, "_SETTLE_POLL_SECONDS", 0.001):
            result = window_manager({"action": "focus", "target": "YouTube"})
        self.assertIn("no open windows could be read", result)
        self.assertIn("none", result)


class LaunchMemoryFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_launch_memory()
        undo_stack.clear()
        # Shrink the retry loop so the race tests run in milliseconds while
        # still exercising the real retry-then-fallback path.
        window_manager_module._SETTLE_POLL_SECONDS = 0.001
        window_manager_module._SETTLE_TIMEOUT_SECONDS = 0.05

    def tearDown(self) -> None:
        window_manager_module._SETTLE_POLL_SECONDS = 0.25
        window_manager_module._SETTLE_TIMEOUT_SECONDS = 2.5
        clear_launch_memory()
        undo_stack.clear()

    def test_fullscreen_resolves_the_window_opened_moments_ago(self) -> None:
        """'Open YouTube' then 'make it fullscreen' must not depend on the
        page having finished loading its title."""
        launched = _win(7, "New Tab", "chrome.exe", 77)
        remember_last_window("YouTube", launched)
        with patch("actions.window_manager.find_windows", return_value=[]), \
             patch("actions.window_manager.refresh_window", return_value=launched), \
             patch("actions.window_manager.operate") as operate, \
             patch("actions.window_manager.place_window", return_value=launched) as place:
            result = window_manager({"action": "fullscreen", "target": "YouTube"})
        self.assertIn("maximized", result)
        place.assert_called_once_with(launched, None, "maximized")

    def test_launch_memory_is_not_a_blank_cheque(self) -> None:
        """A remembered launch only answers for the name it was launched as."""
        launched = _win(7, "New Tab", "chrome.exe", 77)
        remember_last_window("YouTube", launched)
        other = [_win(3, "Discord", "Discord.exe", 33)]
        with patch("actions.window_manager.find_windows", return_value=[]), \
             patch("actions.window_manager.list_windows", return_value=other):
            result = window_manager({"action": "minimize", "target": "Discord"})
        self.assertIn("could not find", result.casefold())

    def test_settling_title_race_is_retried_before_failing(self) -> None:
        """A title that lands a moment late is found by the retry loop."""
        slow = _win(9, "YouTube - Google Chrome", "chrome.exe", 99)
        seen = {"calls": 0}

        def _late_match(target, min_score=1):
            seen["calls"] += 1
            return [slow] if seen["calls"] >= 3 else []

        with patch("actions.window_manager.find_windows", side_effect=_late_match), \
             patch("actions.window_manager.operate"):
            result = window_manager({"action": "minimize", "target": "YouTube"})
        self.assertIn("Minimized YouTube", result)
        self.assertGreaterEqual(seen["calls"], 3)


class BrowserHandoffFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_launch_memory()
        browser_handoff.clear()
        window_manager_module._SETTLE_POLL_SECONDS = 0.001
        window_manager_module._SETTLE_TIMEOUT_SECONDS = 0.05

    def tearDown(self) -> None:
        window_manager_module._SETTLE_POLL_SECONDS = 0.25
        window_manager_module._SETTLE_TIMEOUT_SECONDS = 2.5
        clear_launch_memory()
        browser_handoff.clear()

    def test_last_opened_site_resolves_its_single_browser_window(self) -> None:
        """The tab is playing a video whose title never says 'YouTube'; the
        handoff record of the URL just opened still identifies the window."""
        browser_handoff.note("https://www.youtube.com/watch?v=abc")
        page = _win(5, "Funny cat compilation - Google Chrome", "chrome.exe", 55)

        def _by_target(target, min_score=1):
            if str(target).casefold() in {"browser", "internet"}:
                return [page]
            return []

        with patch("actions.window_manager.find_windows", side_effect=_by_target), \
             patch("actions.window_manager.refresh_window", return_value=page), \
             patch("actions.window_manager.place_window", return_value=page) as place, \
             patch("actions.window_manager.operate"):
            result = window_manager({"action": "fullscreen", "target": "YouTube"})
        self.assertIn("maximized", result)
        place.assert_called_once_with(page, None, "maximized")

    def test_ambiguous_browser_windows_are_never_guessed(self) -> None:
        """Two browser windows and no launch memory: refuse rather than pick."""
        browser_handoff.note("https://www.youtube.com/")
        two = [_win(5, "A - Google Chrome", "chrome.exe", 55),
               _win(6, "B - Google Chrome", "chrome.exe", 56)]

        def _by_target(target, min_score=1):
            if str(target).casefold() in {"browser", "internet"}:
                return two
            return []

        with patch("actions.window_manager.find_windows", side_effect=_by_target), \
             patch("actions.window_manager.list_windows", return_value=two):
            result = window_manager({"action": "focus", "target": "YouTube"})
        self.assertIn("could not find", result.casefold())


class LaunchMemoryCoreTests(unittest.TestCase):
    def test_memory_expires(self) -> None:
        import core.window_manager as core_wm

        window = _win(1, "Anything", "app.exe", 1)
        remember_last_window("Thing", window)
        self.assertIsNotNone(core_wm.last_launched_window())
        # Age the entry past the retention window.
        with core_wm._LAUNCH_MEMORY_LOCK:
            core_wm._LAST_LAUNCH = (0.0, "Thing", window)
        self.assertIsNone(core_wm.last_launched_window())
        clear_launch_memory()
        self.assertEqual(core_wm.last_launch_label(), "")


class UrlLaunchTrackingTests(unittest.TestCase):
    """A URL launch must leave a usable identity behind for the next command.

    "Open YouTube" used to end at launch_uri(): no window was waited for, so
    the following "make it fullscreen" started from nothing and failed.
    """

    def setUp(self) -> None:
        clear_launch_memory()
        browser_handoff.clear()

    tearDown = setUp

    def test_url_launch_records_window_and_handoff(self) -> None:
        from actions import open_app

        url = "https://youtube.com"
        window = _win(21, "New Tab", "chrome.exe", 221)
        remembered = []
        with patch.object(open_app, "resolve_shortcut", return_value=url), \
             patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_browser_title_match_may_be_a_web_app", return_value=False), \
             patch.object(open_app, "_visible_window_keys", return_value=set()), \
             patch.object(open_app, "launch_uri") as launch_uri, \
             patch.object(open_app, "_await_launched_window", return_value=window) as await_window, \
             patch.object(open_app, "_remember_launch",
                          side_effect=lambda name, win: remembered.append((name, win))):
            result = open_app.open_app({"app_name": "YouTube"})

        self.assertIn("Opened", result)
        launch_uri.assert_called_once_with(url)
        await_window.assert_called_once()
        # The window is remembered under the spoken name, so a following
        # window command resolves it even while the title is still loading.
        self.assertEqual(remembered, [("YouTube", window)])
        # And a following interactive browser command resumes on this page.
        self.assertEqual(browser_handoff.peek(), url)

    def test_url_launch_still_succeeds_when_no_window_is_detectable(self) -> None:
        """A page opened in an existing window's new tab may be legitimately
        invisible to the window list; the launch still reports success."""
        from actions import open_app

        url = "https://youtube.com"
        with patch.object(open_app, "resolve_shortcut", return_value=url), \
             patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_browser_title_match_may_be_a_web_app", return_value=False), \
             patch.object(open_app, "_visible_window_keys", return_value=set()), \
             patch.object(open_app, "launch_uri"), \
             patch.object(open_app, "_await_launched_window", return_value=None), \
             patch.object(open_app, "_remember_launch") as remember:
            result = open_app.open_app({"app_name": "YouTube"})

        self.assertIn("Opened", result)
        remember.assert_not_called()


class IdempotentReopenTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_launch_memory()

    tearDown = setUp

    def test_reopen_during_the_title_race_focuses_instead_of_relaunching(self) -> None:
        """'Open YouTube' said again while the page is still titled 'New Tab'
        must focus the window it just opened, not start a second instance."""
        from actions import open_app

        window = _win(31, "New Tab", "chrome.exe", 331)
        remember_last_window("YouTube", window)
        with patch.object(open_app, "resolve_shortcut", return_value="YouTube"), \
             patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_browser_title_match_may_be_a_web_app", return_value=False), \
             patch.object(open_app, "_visible_window_keys", return_value=set()), \
             patch.object(open_app, "_resolve_candidates") as resolve, \
             patch.object(open_app, "_focus_window", return_value=True) as focus, \
             patch("core.window_manager.refresh_window", return_value=window):
            result = open_app.open_app({"app_name": "YouTube"})

        self.assertIn("already open", result)
        resolve.assert_not_called()
        focus.assert_called_once()

    def test_reopen_of_an_unrelated_app_ignores_the_launch_memory(self) -> None:
        from actions import open_app

        window = _win(31, "New Tab", "chrome.exe", 331)
        remember_last_window("YouTube", window)
        with patch.object(open_app, "resolve_shortcut", return_value="Spotify"), \
             patch.object(open_app, "_matching_windows", return_value=[]), \
             patch.object(open_app, "_browser_title_match_may_be_a_web_app", return_value=False), \
             patch.object(open_app, "_visible_window_keys", return_value=set()), \
             patch.object(open_app, "_resolve_candidates", return_value=([], False)):
            result = open_app.open_app({"app_name": "Spotify"})

        self.assertIn("could not find", result)


if __name__ == "__main__":
    unittest.main()
