from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.action_loader import discover_actions
from core.action_result import ActionResult
from core import confirm


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
