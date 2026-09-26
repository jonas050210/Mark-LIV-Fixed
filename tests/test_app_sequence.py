"""Tests for the sequential multi-step planner/executor.

These cover the behaviours the user actually asked for: steps are run one at
a time (never in a burst), a failing step is retried a bounded number of
times, the remaining steps still run after a step ultimately fails, progress
is reported as it happens, the request can be cancelled between steps rather
than only at the very start or end, and a step can be either an application
launch or a Spotify media command.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from actions import app_sequence


def _steps(*names: str) -> list[dict]:
    return [{"app_name": name} for name in names]


class ValidationTests(unittest.TestCase):
    def test_an_empty_step_list_is_rejected_without_opening_anything(self) -> None:
        with patch.object(app_sequence, "open_app_result") as opener:
            result = app_sequence.app_sequence({"steps": []})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_parameters")
        opener.assert_not_called()

    def test_a_step_without_an_app_name_or_media_is_rejected(self) -> None:
        with patch.object(app_sequence, "open_app_result") as opener:
            result = app_sequence.app_sequence({"steps": [{"monitor": "1"}]})
        self.assertFalse(result["ok"])
        opener.assert_not_called()

    def test_too_many_steps_are_rejected(self) -> None:
        steps = _steps(*[f"app{i}" for i in range(app_sequence._MAX_STEPS + 1)])
        with patch.object(app_sequence, "open_app_result") as opener:
            result = app_sequence.app_sequence({"steps": steps})
        self.assertFalse(result["ok"])
        opener.assert_not_called()

    def test_an_invalid_state_is_rejected(self) -> None:
        with patch.object(app_sequence, "open_app_result") as opener:
            result = app_sequence.app_sequence(
                {"steps": [{"app_name": "Chrome", "state": "sideways"}]}
            )
        self.assertFalse(result["ok"])
        opener.assert_not_called()

    def test_a_step_cannot_be_both_an_app_step_and_a_media_step(self) -> None:
        with patch.object(app_sequence, "open_app_result") as opener, \
             patch.object(app_sequence, "media_control") as media:
            result = app_sequence.app_sequence({"steps": [
                {"app_name": "Spotify", "media": {"action": "play"}},
            ]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_parameters")
        opener.assert_not_called()
        media.assert_not_called()

    def test_a_media_step_needs_a_recognised_action(self) -> None:
        result = app_sequence.app_sequence({"steps": [
            {"media": {"action": "levitate"}},
        ]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_parameters")

    def test_a_media_step_needs_an_action_at_all(self) -> None:
        result = app_sequence.app_sequence({"steps": [{"media": {}}]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_parameters")

    def test_an_unknown_media_parameter_is_rejected(self) -> None:
        result = app_sequence.app_sequence({"steps": [
            {"media": {"action": "play", "spam": "no"}},
        ]})
        self.assertFalse(result["ok"])
        self.assertIn("unknown media parameter", result["message"].casefold())


class SequentialExecutionTests(unittest.TestCase):
    def test_applications_are_opened_one_at_a_time_in_order(self) -> None:
        seen = []

        def _fake_open(params, player=None, cancel_event=None):
            seen.append(params["app_name"])
            return True, f"Opened {params['app_name']}."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open):
            result = app_sequence.app_sequence(
                {"steps": _steps("Roblox", "Arena AI", "YouTube")}
            )
        self.assertEqual(seen, ["Roblox", "Arena AI", "YouTube"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["data"]["steps"]), 3)

    def test_placement_is_forwarded_for_each_step(self) -> None:
        calls = []

        def _fake_open(params, player=None, cancel_event=None):
            calls.append(params)
            return True, f"Opened {params['app_name']}."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open):
            app_sequence.app_sequence({"steps": [
                {"app_name": "Roblox", "monitor": "primary", "state": "fullscreen"},
                {"app_name": "Arena AI", "monitor": "2", "state": "left"},
            ]})
        self.assertEqual(calls[0]["monitor"], "primary")
        self.assertEqual(calls[0]["state"], "maximized")
        self.assertEqual(calls[1]["monitor"], "2")
        self.assertEqual(calls[1]["state"], "left")

    def test_the_cancel_event_is_forwarded_to_open_app_result(self) -> None:
        cancel_event = threading.Event()
        received = []

        def _fake_open(params, player=None, cancel_event=None):
            received.append(cancel_event)
            return True, "Opened."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open):
            app_sequence.app_sequence(
                {"steps": _steps("Chrome")}, cancel_event=cancel_event,
            )
        self.assertEqual(received, [cancel_event])


class MediaStepTests(unittest.TestCase):
    def test_a_media_step_is_sent_to_media_control(self) -> None:
        calls = []

        def _fake_media(parameters, player=None):
            calls.append(parameters)
            return "Spotify playback started in the background."

        with patch.object(app_sequence, "media_control", side_effect=_fake_media):
            result = app_sequence.app_sequence({"steps": [
                {"media": {"action": "play", "query": "Bohemian Rhapsody"}},
            ]})
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0]["action"], "play")
        self.assertEqual(calls[0]["query"], "Bohemian Rhapsody")

    def test_a_media_failure_is_reported_and_retried(self) -> None:
        attempts = {"n": 0}

        def _fake_media(parameters, player=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return "Spotify has no active playback device. Open Spotify or activate a Spotify Connect device once, then try again."
            return "Spotify playback started in the background."

        with patch.object(app_sequence, "media_control", side_effect=_fake_media), \
             patch.object(app_sequence, "_RETRY_BACKOFF_SECONDS", 0):
            result = app_sequence.app_sequence({"steps": [{"media": {"action": "play"}}]})
        self.assertEqual(attempts["n"], 2)
        self.assertTrue(result["ok"])

    def test_a_mixed_plan_runs_app_and_media_steps_in_order(self) -> None:
        order = []

        def _fake_open(params, player=None, cancel_event=None):
            order.append(("open_app", params["app_name"]))
            return True, f"Opened {params['app_name']}."

        def _fake_media(parameters, player=None):
            order.append(("media", parameters["action"]))
            return "Spotify playback started in the background."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open), \
             patch.object(app_sequence, "media_control", side_effect=_fake_media):
            result = app_sequence.app_sequence({"steps": [
                {"app_name": "Spotify"},
                {"media": {"action": "play", "query": "Test Track"}},
                {"app_name": "Chrome"},
            ]})
        self.assertEqual(order, [
            ("open_app", "Spotify"), ("media", "play"), ("open_app", "Chrome"),
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "succeeded")


class RetryAndContinuationTests(unittest.TestCase):
    def test_a_failing_step_is_retried_once_before_giving_up(self) -> None:
        attempts = {"n": 0}

        def _fake_open(params, player=None, cancel_event=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return False, "I could not find an installed application called 'Roblox'."
            return True, "Opened Roblox."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open), \
             patch.object(app_sequence, "_RETRY_BACKOFF_SECONDS", 0):
            result = app_sequence.app_sequence({"steps": _steps("Roblox")})
        self.assertEqual(attempts["n"], 2)
        self.assertTrue(result["ok"])

    def test_a_step_that_keeps_failing_does_not_block_the_next_step(self) -> None:
        seen = []

        def _fake_open(params, player=None, cancel_event=None):
            seen.append(params["app_name"])
            if params["app_name"] == "Roblox":
                return False, "I could not find an installed application called 'Roblox'."
            return True, f"Opened {params['app_name']}."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open), \
             patch.object(app_sequence, "_RETRY_BACKOFF_SECONDS", 0):
            result = app_sequence.app_sequence({"steps": _steps("Roblox", "YouTube")})
        self.assertEqual(seen.count("Roblox"), app_sequence._MAX_ATTEMPTS_PER_STEP)
        self.assertIn("YouTube", seen)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["data"]["steps"][0]["ok"], False)
        self.assertEqual(result["data"]["steps"][1]["ok"], True)

    def test_an_unexpected_exception_is_reported_not_raised(self) -> None:
        def _boom(params, player=None, cancel_event=None):
            raise RuntimeError("boom")

        with patch.object(app_sequence, "open_app_result", side_effect=_boom), \
             patch.object(app_sequence, "_RETRY_BACKOFF_SECONDS", 0):
            result = app_sequence.app_sequence({"steps": _steps("Roblox")})
        self.assertFalse(result["ok"])
        self.assertIn("unexpectedly", result["data"]["steps"][0]["message"])

    def test_a_partial_placement_success_from_open_app_result_is_trusted_as_is(self) -> None:
        # open_app_result already classifies "opened, but placement unverified"
        # as ok=False -- app_sequence must not re-derive this from the text.
        def _fake_open(params, player=None, cancel_event=None):
            return False, f"Opened {params['app_name']}, but I could not verify the move."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open), \
             patch.object(app_sequence, "_RETRY_BACKOFF_SECONDS", 0):
            result = app_sequence.app_sequence({"steps": _steps("Chrome")})
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["steps"][0]["ok"], False)


class CancellationTests(unittest.TestCase):
    def test_cancelling_after_the_first_step_skips_the_rest(self) -> None:
        cancel_event = threading.Event()

        def _fake_open(params, player=None, cancel_event=None):
            if params["app_name"] == "Roblox":
                cancel_event.set()
            return True, f"Opened {params['app_name']}."

        with patch.object(app_sequence, "open_app_result", side_effect=_fake_open):
            result = app_sequence.app_sequence(
                {"steps": _steps("Roblox", "Arena AI", "YouTube")},
                cancel_event=cancel_event,
            )
        self.assertEqual(result["status"], "cancelled")
        steps = result["data"]["steps"]
        self.assertTrue(steps[0]["ok"])
        self.assertFalse(steps[1]["ok"])
        self.assertFalse(steps[2]["ok"])
        self.assertIn("cancelled", steps[1]["message"].casefold())


class ProgressReportingTests(unittest.TestCase):
    def test_progress_is_reported_for_the_plan_and_each_step(self) -> None:
        updates = []

        def _report(progress, message=""):
            updates.append((progress, message))

        with patch.object(app_sequence, "open_app_result", return_value=(True, "Opened Roblox.")):
            app_sequence.app_sequence(
                {"steps": _steps("Roblox")}, report_progress=_report
            )
        self.assertGreaterEqual(len(updates), 3)
        self.assertEqual(updates[-1][0], 100)
        self.assertTrue(any("Plan" in message for _, message in updates))


if __name__ == "__main__":
    unittest.main()
