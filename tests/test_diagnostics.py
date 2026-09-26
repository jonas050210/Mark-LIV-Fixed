from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from actions import diagnostics as diagnostics_action
from core import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        diagnostics.clear()

    def test_secrets_are_redacted_and_events_are_detached(self) -> None:
        diagnostics.record("network", "Authorization: Bearer abc123 token=private")
        events = diagnostics.snapshot()
        rendered = json.dumps(events)
        self.assertNotIn("abc123", rendered)
        self.assertNotIn("private", rendered)
        events.clear()
        self.assertEqual(len(diagnostics.snapshot()), 1)

    def test_buffer_is_bounded(self) -> None:
        for index in range(600):
            diagnostics.record("test", index, level="info")
        events = diagnostics.snapshot()
        self.assertEqual(len(events), 500)
        self.assertEqual(events[-1]["message"], "599")

    def test_export_writes_redacted_json_atomically(self) -> None:
        diagnostics.record("auth", "api_key=do-not-export")
        with TemporaryDirectory() as directory:
            target = Path(directory) / "diagnostics.json"
            self.assertEqual(diagnostics.export(target), target)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["events"]), 1)
            self.assertNotIn("do-not-export", target.read_text(encoding="utf-8"))
            self.assertEqual(list(target.parent.glob(".*.tmp")), [])
            with self.assertRaises(FileExistsError):
                diagnostics.export(target)

    def test_action_exports_to_downloads(self) -> None:
        from unittest.mock import patch

        diagnostics.record("test", "safe event")
        with TemporaryDirectory() as directory, patch.object(
            diagnostics_action, "locations", return_value={"downloads": Path(directory)}
        ):
            result = diagnostics_action.diagnostics({"action": "export"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["event_count"], 1)
