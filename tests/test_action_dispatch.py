from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core import confirm
from core.action_dispatch import run_dashboard_action
from core.action_loader import discover_actions
from core.action_runtime import runtime as action_runtime


class DashboardActionDispatchTests(unittest.TestCase):
    def tearDown(self) -> None:
        confirm.resolve(False)
        confirm.bind(None, None)

    def test_dashboard_action_is_tracked_and_finishes(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_demo.py"
            path.write_text(
                "def handler(parameters): return 'dashboard complete'\n"
                "TOOL = {'name': 'dashboard_demo', 'description': 'demo', 'handler': handler}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)

            action_id, result = run_dashboard_action(registry, "dashboard_demo", {})

            self.assertTrue(result.ok)
            run = action_runtime.get(action_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.source, "dashboard")
            self.assertEqual(run.status, "succeeded")
            self.assertEqual(run.message, "dashboard complete")
            self.assertIsNotNone(run.finished_at)

    def test_authenticated_dashboard_context_can_run_admin_action(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "admin_panel.py"
            path.write_text(
                "def handler(parameters): return 'admin complete'\n"
                "TOOL = {'name': 'admin_panel', 'description': 'admin', 'handler': handler, 'requires_admin': True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)

            _action_id, result = run_dashboard_action(registry, "admin_panel", {})

            self.assertTrue(result.ok)
            self.assertEqual(result.as_text(), "admin complete")

    def test_dashboard_confirmation_stays_live_until_the_user_decides(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "dashboard_danger.py"
            path.write_text(
                "def handler(parameters): return 'must only run after confirmation'\n"
                "TOOL = {'name': 'dashboard_danger', 'description': 'danger', 'handler': handler, 'requires_confirmation': True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)
            confirm.bind(lambda _title, _detail: None, lambda: None)

            action_id, result = run_dashboard_action(registry, "dashboard_danger", {})

            self.assertEqual(result.status, "confirmation_pending")
            pending = action_runtime.get(action_id)
            self.assertIsNotNone(pending)
            self.assertEqual(pending.status, "confirmation_pending")
            self.assertIsNone(pending.finished_at)
            self.assertTrue(confirm.resolve(False, key=action_id))
            finished = action_runtime.get(action_id)
            self.assertEqual(finished.status, "cancelled")
            self.assertIsNotNone(finished.finished_at)


if __name__ == "__main__":
    unittest.main()
