from __future__ import annotations

import threading
import time
import unittest

from core.action_scheduler import ActionResourceScheduler


class ActionResourceSchedulerTests(unittest.TestCase):
    def test_shared_leases_can_overlap_but_block_an_exclusive_lease(self) -> None:
        scheduler = ActionResourceScheduler()
        first = scheduler.acquire({"desktop": "shared"})
        second = scheduler.acquire({"desktop": "shared"})
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

        acquired = threading.Event()
        result = []

        def request_exclusive() -> None:
            result.append(scheduler.acquire({"desktop": "exclusive"}, timeout_seconds=1))
            acquired.set()

        worker = threading.Thread(target=request_exclusive)
        worker.start()
        time.sleep(0.05)
        self.assertFalse(acquired.is_set())
        first.release()
        self.assertFalse(acquired.is_set())
        second.release()
        self.assertTrue(acquired.wait(1))
        self.assertIsNotNone(result[0])
        result[0].release()
        worker.join(1)
        self.assertEqual(scheduler.snapshot(), {})

    def test_queued_writer_prevents_new_readers_from_starving_it(self) -> None:
        scheduler = ActionResourceScheduler()
        held = scheduler.acquire({"desktop": "shared"})
        writer_ready = threading.Event()
        result = []

        def request_writer() -> None:
            result.append(scheduler.acquire({"desktop": "exclusive"}, timeout_seconds=1))
            writer_ready.set()

        worker = threading.Thread(target=request_writer)
        worker.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if scheduler.snapshot()["desktop"]["waiting_exclusive"]:
                break
            time.sleep(0.01)
        self.assertEqual(scheduler.snapshot()["desktop"]["waiting_exclusive"], 1)
        self.assertIsNone(scheduler.acquire({"desktop": "shared"}, timeout_seconds=0))
        held.release()
        self.assertTrue(writer_ready.wait(1))
        self.assertIsNotNone(result[0])
        result[0].release()
        worker.join(1)

    def test_waiting_request_honours_cancellation_without_taking_the_lease(self) -> None:
        scheduler = ActionResourceScheduler()
        held = scheduler.acquire({"filesystem": "exclusive"})
        cancelled = threading.Event()
        result = []

        worker = threading.Thread(
            target=lambda: result.append(
                scheduler.acquire(
                    {"filesystem": "shared"},
                    cancel_event=cancelled,
                    timeout_seconds=3,
                )
            )
        )
        worker.start()
        time.sleep(0.05)
        cancelled.set()
        worker.join(1)
        self.assertEqual(result, [None])
        self.assertEqual(scheduler.snapshot()["filesystem"]["shared"], 0)
        self.assertTrue(scheduler.snapshot()["filesystem"]["exclusive"])
        held.release()

    def test_multi_resource_request_is_admitted_only_when_every_claim_is_free(self) -> None:
        scheduler = ActionResourceScheduler()
        held = scheduler.acquire({"desktop": "exclusive"})
        result = []
        finished = threading.Event()

        def request_both() -> None:
            result.append(
                scheduler.acquire(
                    {"desktop": "shared", "filesystem": "exclusive"},
                    timeout_seconds=1,
                )
            )
            finished.set()

        worker = threading.Thread(target=request_both)
        worker.start()
        time.sleep(0.05)
        self.assertFalse(finished.is_set())
        pending = scheduler.snapshot()["filesystem"]
        self.assertFalse(pending["exclusive"])
        self.assertEqual(pending["shared"], 0)
        self.assertEqual(pending["waiting_exclusive"], 1)
        held.release()
        self.assertTrue(finished.wait(1))
        self.assertIsNotNone(result[0])
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["desktop"]["shared"], 1)
        self.assertTrue(snapshot["filesystem"]["exclusive"])
        result[0].release()
        worker.join(1)


if __name__ == "__main__":
    unittest.main()
