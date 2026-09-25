"""Tests for the single background-job loop.

The topic monitor and the proactive check-in used to be two hand-written
``while True`` loops with two different answers to "may I speak now?". These
tests pin down the shared behaviour: intervals are honoured, the gate is
obeyed, and one failing job neither stops the loop nor spins on the failure.
"""
from __future__ import annotations

import asyncio
import unittest

from core.background_scheduler import BackgroundScheduler


def _run(coro):
    return asyncio.run(coro)


class SchedulerTests(unittest.TestCase):
    def test_a_job_runs_once_its_interval_has_passed(self) -> None:
        calls = []

        async def job():
            calls.append("ran")

        scheduler = BackgroundScheduler()
        scheduler.add("job", interval=100, run=job)
        _run(scheduler.run_once(now=0.0))
        self.assertEqual(calls, ["ran"])
        _run(scheduler.run_once(now=50.0))
        self.assertEqual(calls, ["ran"], "not due yet")
        _run(scheduler.run_once(now=101.0))
        self.assertEqual(calls, ["ran", "ran"])

    def test_an_initial_delay_holds_the_first_run_back(self) -> None:
        calls = []

        async def job():
            calls.append("ran")

        scheduler = BackgroundScheduler()
        scheduler.add("job", interval=100, run=job, initial_delay=300)
        scheduler.jobs[0].schedule_first(0.0)
        _run(scheduler.run_once(now=299.0))
        self.assertEqual(calls, [])
        _run(scheduler.run_once(now=300.0))
        self.assertEqual(calls, ["ran"])

    def test_a_closed_gate_holds_every_job_that_needs_silence(self) -> None:
        calls = []

        async def job():
            calls.append("ran")

        scheduler = BackgroundScheduler(gate=lambda: False)
        scheduler.add("job", interval=1, run=job)
        _run(scheduler.run_once(now=10.0))
        self.assertEqual(calls, [])

    def test_a_job_that_does_not_need_silence_runs_anyway(self) -> None:
        calls = []

        async def job():
            calls.append("ran")

        scheduler = BackgroundScheduler(gate=lambda: False)
        scheduler.add("job", interval=1, run=job, requires_silence=False)
        _run(scheduler.run_once(now=10.0))
        self.assertEqual(calls, ["ran"])

    def test_a_gated_job_is_not_skipped_but_retried(self) -> None:
        """A closed gate must postpone the job, not consume its turn."""
        calls = []

        async def job():
            calls.append("ran")

        open_gate = {"value": False}
        scheduler = BackgroundScheduler(gate=lambda: open_gate["value"])
        scheduler.add("job", interval=100, run=job)
        _run(scheduler.run_once(now=10.0))
        self.assertEqual(calls, [])
        open_gate["value"] = True
        _run(scheduler.run_once(now=11.0))
        self.assertEqual(calls, ["ran"])

    def test_one_failing_job_does_not_stop_the_others(self) -> None:
        calls = []

        async def broken():
            raise RuntimeError("no network")

        async def healthy():
            calls.append("ran")

        scheduler = BackgroundScheduler()
        scheduler.add("broken", interval=10, run=broken)
        scheduler.add("healthy", interval=10, run=healthy)
        ran = _run(scheduler.run_once(now=0.0))
        self.assertEqual(ran, ["healthy"])
        self.assertEqual(calls, ["ran"])

    def test_a_failing_job_backs_off_instead_of_retrying_every_tick(self) -> None:
        attempts = []

        async def broken():
            attempts.append(1)
            raise RuntimeError("no network")

        scheduler = BackgroundScheduler()
        scheduler.add("broken", interval=10, run=broken)
        _run(scheduler.run_once(now=0.0))
        _run(scheduler.run_once(now=11.0))
        self.assertEqual(len(attempts), 1, "second attempt came too early")
        _run(scheduler.run_once(now=21.0))
        self.assertEqual(len(attempts), 2)

    def test_a_recovered_job_returns_to_its_normal_interval(self) -> None:
        state = {"fail": True}
        attempts = []

        async def flaky():
            attempts.append(1)
            if state["fail"]:
                raise RuntimeError("no network")

        scheduler = BackgroundScheduler()
        scheduler.add("flaky", interval=10, run=flaky)
        _run(scheduler.run_once(now=0.0))     # fails, backs off to 20
        state["fail"] = False
        _run(scheduler.run_once(now=21.0))    # succeeds, interval resets
        _run(scheduler.run_once(now=32.0))
        self.assertEqual(len(attempts), 3)

    def test_cancellation_is_never_swallowed(self) -> None:
        async def cancelling():
            raise asyncio.CancelledError

        scheduler = BackgroundScheduler()
        scheduler.add("cancelling", interval=10, run=cancelling)
        with self.assertRaises(asyncio.CancelledError):
            _run(scheduler.run_once(now=0.0))

    def test_the_interval_has_a_floor(self) -> None:
        async def job():
            return None

        scheduler = BackgroundScheduler()
        created = scheduler.add("job", interval=0, run=job)
        self.assertGreaterEqual(created.interval, 1.0)


if __name__ == "__main__":
    unittest.main()
