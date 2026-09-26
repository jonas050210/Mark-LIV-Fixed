"""Desktop window events: the pump that replaces polling, and the cache it
enables.

The WinEvent hook itself only exists on Windows, so its live behaviour is
tested there and skipped elsewhere. What IS tested everywhere: the fallback
(wait without a hook is an ordinary sleep), the revision counter that
wake-ups and cache invalidation are built on, and — most importantly — the
cached ``list_windows`` contract, because a stale window list would be worse
than no cache at all.
"""
from __future__ import annotations

import platform
import threading
import time
import unittest
from unittest.mock import patch

from core import window_events
from core import window_manager as core_wm
from core.window_manager import WindowInfo


def _win(handle: int, title: str) -> WindowInfo:
    return WindowInfo(handle, title, "app.exe", handle, 0, 0, 800, 600)


class FallbackTests(unittest.TestCase):
    def test_wait_without_the_hook_is_a_plain_sleep(self) -> None:
        started = time.monotonic()
        self.assertFalse(window_events.wait_for_change(0.05))
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_start_is_a_safe_no_op_off_windows(self) -> None:
        if platform.system() == "Windows":
            self.skipTest("the hook exists on this platform")
        self.assertFalse(window_events.start())
        self.assertFalse(window_events.active())

    def test_waiting_never_switches_the_desktop_into_event_mode(self) -> None:
        """wait_for_change must not install the hook as a side effect.

        A library call that flipped the pump on would silently enable the
        between-events window cache for the rest of the process — exactly the
        cross-test state that makes results depend on test order.
        """
        self.assertFalse(window_events.wait_for_change(0.01))
        self.assertFalse(window_events.active())

    def test_revision_bumps(self) -> None:
        before = window_events.revision()
        window_events._bump()
        self.assertGreater(window_events.revision(), before)

    @unittest.skipIf(platform.system() != "Windows", "hook only exists on Windows")
    def test_wait_returns_true_when_an_event_arrives(self) -> None:
        # The pump is started explicitly here — wait_for_change must never
        # turn it on by itself — and switched back off afterwards so no later
        # test in this process sees a live event cache it did not ask for.
        self.assertTrue(window_events.start())
        try:
            timer = threading.Timer(0.05, window_events._bump)
            timer.start()
            try:
                started = time.monotonic()
                self.assertTrue(window_events.wait_for_change(3.0))
                self.assertLess(time.monotonic() - started, 2.0)
            finally:
                timer.join()
        finally:
            window_events._active = False
            window_events._failed = True


class WindowCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        core_wm._invalidate_window_cache()

    tearDown = setUp

    def _patched_events(self, active: bool, revision_box: dict):
        return (
            patch.object(core_wm.window_events, "active", return_value=active),
            patch.object(core_wm.window_events, "revision",
                         side_effect=lambda: revision_box["value"]),
        )

    def test_repeated_reads_between_events_are_served_from_the_cache(self) -> None:
        calls = {"count": 0}

        def _fake_windows():
            calls["count"] += 1
            return [_win(1, "One")]

        active, revision = self._patched_events(True, {"value": 7})
        with active, revision, patch.object(core_wm, "_windows", side_effect=_fake_windows):
            first = core_wm.list_windows()
            second = core_wm.list_windows()
        self.assertEqual(calls["count"], 1)
        self.assertEqual(first, second)

    def test_a_desktop_event_invalidates_the_cache(self) -> None:
        calls = {"count": 0}

        def _fake_windows():
            calls["count"] += 1
            return [_win(1, "One")]

        box = {"value": 7}
        active, revision = self._patched_events(True, box)
        with active, revision, patch.object(core_wm, "_windows", side_effect=_fake_windows):
            core_wm.list_windows()
            box["value"] = 8   # something on the desktop changed
            core_wm.list_windows()
        self.assertEqual(calls["count"], 2)

    def test_the_cache_is_bypassed_when_events_are_inactive(self) -> None:
        calls = {"count": 0}

        def _fake_windows():
            calls["count"] += 1
            return [_win(1, "One")]

        active, revision = self._patched_events(False, {"value": 7})
        with active, revision, patch.object(core_wm, "_windows", side_effect=_fake_windows):
            core_wm.list_windows()
            core_wm.list_windows()
        self.assertEqual(calls["count"], 2)

    def test_operate_clears_the_cache_before_acting(self) -> None:
        # Put a live-looking cache in place, then start an operation. On this
        # platform operate() raises right after invalidating (no desktop
        # backend), which conveniently proves the invalidation happens first:
        # a caller re-reading the window to verify a move must never be served
        # the pre-move cache while the OS event is still in flight.
        calls = {"count": 0}

        def _fake_windows():
            calls["count"] += 1
            return [_win(1, "One")]

        box = {"value": 7}
        active, revision = self._patched_events(True, box)
        with active, revision, patch.object(core_wm, "_windows", side_effect=_fake_windows):
            core_wm.list_windows()
            self.assertIsNotNone(core_wm._WINDOW_CACHE)
            with self.assertRaises(RuntimeError):
                core_wm.operate(_win(1, "One"), "focus")
        self.assertIsNone(core_wm._WINDOW_CACHE)


if __name__ == "__main__":
    unittest.main()
