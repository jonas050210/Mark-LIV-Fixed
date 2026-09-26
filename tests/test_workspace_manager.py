"""Saved application workspaces must stay bounded, validated and honest."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from actions import workspace_manager


class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.path_patch = patch.object(
            workspace_manager, "WORKSPACES_FILE", Path(self.directory.name) / "workspaces.json"
        )
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.directory.cleanup()

    @staticmethod
    def _steps():
        return [
            {"app_name": "Chrome", "monitor": "secondary", "state": "maximized"},
            {"app_name": "Visual Studio Code", "foreground": False},
        ]

    def test_save_list_and_delete_are_persistent_and_undoable(self) -> None:
        pushed = []
        with patch.object(workspace_manager, "push_undo", lambda label, fn: pushed.append((label, fn))):
            saved = workspace_manager.workspace_manager(
                {"action": "save", "name": "school mode", "steps": self._steps()}
            )
            listed = workspace_manager.workspace_manager({"action": "list"})
            deleted = workspace_manager.workspace_manager(
                {"action": "delete", "name": "school mode"}
            )
        self.assertIn("Saved workspace", saved)
        self.assertIn("school mode", listed)
        self.assertIn("Deleted workspace", deleted)
        self.assertEqual(workspace_manager._read()["workspaces"], {})
        pushed[-1][1]()
        self.assertIn("school mode", workspace_manager._read()["workspaces"])

    def test_run_delegates_to_the_verified_sequence_runner(self) -> None:
        workspace_manager.workspace_manager(
            {"action": "save", "name": "coding", "steps": self._steps()}
        )
        result_from_sequence = {
            "ok": True,
            "status": "succeeded",
            "message": "All 2 step(s) completed.",
            "data": {"steps": [{"app_name": "Chrome", "ok": True}]},
        }
        with patch.object(workspace_manager, "app_sequence", return_value=result_from_sequence) as run:
            result = workspace_manager.workspace_manager({"action": "run", "name": "coding"})
        self.assertTrue(result["ok"])
        self.assertIn("Workspace 'coding'", result["message"])
        self.assertEqual(run.call_args[0][0]["steps"], self._steps())

    def test_invalid_steps_never_reach_persistence_or_runner(self) -> None:
        result = workspace_manager.workspace_manager({
            "action": "save", "name": "bad", "steps": [{"app_name": "Chrome", "state": "sideways"}],
        })
        self.assertIn("invalid window state", result)
        self.assertEqual(workspace_manager._read()["workspaces"], {})

    def test_saved_spoken_fullscreen_is_canonicalized_to_native_maximize(self) -> None:
        result = workspace_manager.workspace_manager({
            "action": "save", "name": "video", "steps": [
                {"app_name": "Chrome", "state": "fullscreen"},
            ],
        })
        self.assertIn("Saved workspace", result)
        steps = workspace_manager._read()["workspaces"]["video"]["steps"]
        self.assertEqual(steps, [{"app_name": "Chrome", "state": "maximized"}])

    def test_profile_capacity_and_unknown_run_are_reported(self) -> None:
        for index in range(workspace_manager.MAX_WORKSPACES):
            workspace_manager.workspace_manager({
                "action": "save", "name": f"profile{index}", "steps": [{"app_name": "Chrome"}],
            })
        result = workspace_manager.workspace_manager({
            "action": "save", "name": "too many", "steps": [{"app_name": "Chrome"}],
        })
        self.assertIn("Delete one first", result)
        missing = workspace_manager.workspace_manager({"action": "run", "name": "unknown"})
        self.assertIn("no workspace called", missing)


if __name__ == "__main__":
    unittest.main()
