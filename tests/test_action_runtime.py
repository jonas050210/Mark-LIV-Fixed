from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.action_loader import discover_actions
from core.action_result import ActionResult
from core import confirm
from core.action_runtime import runtime as action_runtime


class ActionRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        # Do not let a pending confirmation leak into another test process.
        confirm.resolve(False)
        confirm.bind(None, None)

    def test_plain_handler_is_wrapped_in_structured_result(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "demo_action.py"
            path.write_text(
                "def handler(parameters):\n"
                "    return 'hello'\n"
                "TOOL = {'name': 'demo_action', 'description': 'demo', 'handler': handler}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            result = registry.execute("demo_action", {}, {"trusted": True})
            self.assertIsInstance(result, ActionResult)
            self.assertTrue(result.ok)
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(result.as_text(), "hello")

    def test_confirmation_is_not_a_model_parameter(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "danger.py"
            path.write_text(
                "def handler(parameters):\n"
                "    return 'executed'\n"
                "TOOL = {'name': 'danger', 'description': 'danger', 'handler': handler, 'requires_confirmation': True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            shown = []
            confirm.bind(lambda title, detail: shown.append((title, detail)), lambda: None)
            result = registry.execute("danger", {"confirmed": "yes"}, {"trusted": True})
            self.assertEqual(result.status, "confirmation_pending")
            self.assertTrue(shown)
            confirm.resolve(False)

    def test_confirmation_cancel_finishes_the_live_action(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "danger.py"
            path.write_text(
                "def handler(parameters): return 'executed'\n"
                "TOOL = {'name': 'danger_live', 'description': 'danger', 'handler': handler, 'requires_confirmation': True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            confirm.bind(lambda _title, _detail: None, lambda: None)
            run_id = action_runtime.start("danger_live", {})
            result = registry.execute(
                "danger_live", {}, {"action_id": run_id, "trusted": True}
            )
            self.assertEqual(result.status, "confirmation_pending")
            self.assertIsNotNone(action_runtime.get(run_id))
            self.assertEqual(action_runtime.get(run_id).status, "confirmation_pending")

            confirm.resolve(False)

            run = action_runtime.get(run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "cancelled")
            self.assertIsNotNone(run.finished_at)
            self.assertEqual(run.message, "Cancelled by user")

    def test_confirmation_without_an_interface_fails_closed(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "headless_danger.py"
            path.write_text(
                "def handler(parameters): return 'executed'\n"
                "TOOL = {'name': 'headless_danger', 'description': 'danger', 'handler': handler, 'requires_confirmation': True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            confirm.bind(None, None)
            run_id = action_runtime.start("headless_danger", {})
            result = registry.execute(
                "headless_danger", {}, {"action_id": run_id, "trusted": True}
            )
            self.assertEqual(result.status, "confirmation_failed")
            self.assertEqual(action_runtime.get(run_id).status, "failed")
            self.assertNotEqual(result.as_text(), "executed")

    def test_admin_capability_requires_trusted_dispatch_context(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "admin_only.py"
            path.write_text(
                "def handler(parameters):\n"
                "    return 'executed'\n"
                "TOOL = {'name': 'admin_only', 'description': 'admin', 'handler': handler, 'requires_admin': True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            result = registry.execute("admin_only", {}, {})
            self.assertEqual(result.status, "forbidden")

    def test_optional_import_is_exposed_as_unavailable_capability(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "optional_action.py"
            path.write_text(
                "import package_that_is_not_installed\n"
                "def handler(parameters): return 'never'\n"
                "TOOL = {'name': 'optional_action', 'description': 'optional', 'parameters': {'type': 'OBJECT'}, 'handler': handler}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            record = registry.record("optional_action")
            self.assertIsNotNone(record)
            self.assertFalse(record.available)
            self.assertEqual(registry.execute("optional_action", {}, {}).status, "unavailable")

    def test_cancellation_event_stops_a_cooperative_handler(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "cooperative.py"
            path.write_text(
                "import time\n"
                "def handler(parameters, cancel_event=None):\n"
                "    while not cancel_event.is_set(): time.sleep(.01)\n"
                "    return 'stopped'\n"
                "TOOL = {'name': 'cooperative', 'description': 'cooperative', 'handler': handler, 'timeout_seconds': 5}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            run_id = action_runtime.start("cooperative", {})
            result_box = []
            import threading
            worker = threading.Thread(target=lambda: result_box.append(
                registry.execute("cooperative", {}, {"action_id": run_id, "trusted": True})
            ))
            worker.start()
            time.sleep(.05)
            self.assertTrue(action_runtime.cancel(run_id))
            worker.join(1)
            self.assertTrue(result_box)
            self.assertEqual(result_box[0].status, "cancelled")

    def test_legacy_handler_gets_a_deadline(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "slow.py"
            path.write_text(
                "import time\n"
                "def handler(parameters):\n"
                "    time.sleep(2)\n"
                "    return 'late'\n"
                "TOOL = {'name': 'slow', 'description': 'slow', 'handler': handler, 'timeout_seconds': 1}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(temp), logger=lambda _msg: None)
            started = time.monotonic()
            result = registry.execute("slow", {}, {"trusted": True})
            elapsed = time.monotonic() - started
            self.assertEqual(result.status, "timed_out")
            self.assertLess(elapsed, 1.7)


if __name__ == "__main__":
    unittest.main()
