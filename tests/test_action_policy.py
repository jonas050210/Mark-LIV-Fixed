from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core import confirm
from actions import background_monitor as monitor_action
from actions import reminder as reminder_action
from actions import web_search as web_search_action
from actions.browser_control import _normalize_url
from core.action_loader import discover_actions
from core.action_result import ActionResult
from core.action_runtime import runtime as action_runtime
from core.process_runner import run_bounded


class ActionSchemaTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_world_writable_action_is_rejected_before_import(self) -> None:
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            action = Path(directory) / "untrusted.py"
            action.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran')\n"
                "def handler(parameters): return 'done'\n"
                "TOOL = {'name':'untrusted','description':'bad','handler':handler}\n",
                encoding="utf-8",
            )
            action.chmod(0o666)
            registry = discover_actions(Path(directory), logger=lambda _message: None)
            self.assertFalse(registry.has("untrusted"))
            self.assertFalse(marker.exists())

    def test_wrong_types_and_unknown_parameters_are_rejected_before_handler(self) -> None:
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "called"
            action = Path(directory) / "typed.py"
            action.write_text(
                "from pathlib import Path\n"
                f"MARKER = Path({str(marker)!r})\n"
                "def handler(parameters): MARKER.write_text('called'); return 'done'\n"
                "TOOL = {'name':'typed','description':'typed','parameters':{"
                "'type':'OBJECT','properties':{'count':{'type':'INTEGER'}},"
                "'required':['count']},'handler':handler}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)
            wrong = registry.execute("typed", {"count": "3"})
            unknown = registry.execute("typed", {"count": 3, "extra": True})
            self.assertEqual(wrong.status, "invalid_parameters")
            self.assertEqual(unknown.status, "invalid_parameters")
            self.assertFalse(marker.exists())

    def test_malformed_parameter_schema_rejects_action_at_discovery(self) -> None:
        with TemporaryDirectory() as directory:
            action = Path(directory) / "bad_schema.py"
            action.write_text(
                "def handler(parameters): return 'done'\n"
                "TOOL = {'name':'bad_schema','description':'bad','parameters':{"
                "'type':'OBJECT','properties':{'items':{'type':'ARRAY'}}},'handler':handler}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)
            self.assertFalse(registry.has("bad_schema"))
            row = next(item for item in registry.admin_manifest() if item["name"] == "bad_schema")
            self.assertIn("Invalid TOOL parameter schema", row["error"])

    def test_non_boolean_security_metadata_rejects_action(self) -> None:
        with TemporaryDirectory() as directory:
            action = Path(directory) / "bad_metadata.py"
            action.write_text(
                "def handler(parameters): return 'done'\n"
                "TOOL = {'name':'bad_metadata','description':'bad',"
                "'parameters':{'type':'OBJECT','properties':{}},"
                "'requires_confirmation':'false','handler':handler}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)
            self.assertFalse(registry.has("bad_metadata"))

    def test_action_result_data_must_be_bounded_json(self) -> None:
        invalid = ActionResult.from_handler(
            "example",
            ActionResult.success("example", "ok", value=float("nan")),
        )
        self.assertEqual(invalid.status, "invalid_result")
        self.assertFalse(invalid.ok)

    def test_legacy_i_could_not_messages_are_not_reported_as_success(self) -> None:
        """Many established actions use this human-friendly failure phrasing."""
        result = ActionResult.from_handler(
            "file_controller", "I could not copy it: permission was denied."
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")


class BrowserInputTests(unittest.TestCase):
    def test_url_normalization_accepts_web_urls_and_rejects_local_protocols(self) -> None:
        self.assertEqual(_normalize_url("example.com"), "https://example.com")
        self.assertEqual(_normalize_url("https://example.com/a"), "https://example.com/a")
        for unsafe in (
            "file:///etc/passwd",
            "javascript://alert(1)",
            "chrome://settings",
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "https://user:password@example.com/",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                _normalize_url(unsafe)


class ConfirmationBindingTests(unittest.TestCase):
    def tearDown(self) -> None:
        confirm.resolve(False)
        confirm.bind(None, None)

    def test_confirmation_is_bound_to_action_id_and_shows_target(self) -> None:
        with TemporaryDirectory() as directory:
            action = Path(directory) / "danger.py"
            action.write_text(
                "def handler(parameters): return 'executed'\n"
                "TOOL = {'name':'danger_target','description':'danger',"
                "'parameters':{'type':'OBJECT','properties':{"
                "'action':{'type':'STRING'},'path':{'type':'STRING'}},"
                "'required':['action','path']},'handler':handler,"
                "'requires_confirmation':True}\n",
                encoding="utf-8",
            )
            registry = discover_actions(Path(directory), logger=lambda _message: None)
            shown = []
            confirm.bind(lambda title, detail: shown.append((title, detail)), lambda: None)
            first_id = action_runtime.start("danger_target", {"path": "/first"})
            first = registry.execute(
                "danger_target",
                {"action": "delete", "path": "/first"},
                {"action_id": first_id, "trusted": True},
            )
            self.assertEqual(first.status, "confirmation_pending")
            self.assertIn("path: /first", shown[0][1])
            self.assertEqual(confirm.pending_key(), first_id)

            second_id = action_runtime.start("danger_target", {"path": "/second"})
            second = registry.execute(
                "danger_target",
                {"action": "delete", "path": "/second"},
                {"action_id": second_id, "trusted": True},
            )
            self.assertEqual(second.status, "confirmation_busy")
            self.assertFalse(confirm.resolve(False, key=second_id))
            self.assertEqual(confirm.pending_key(), first_id)
            self.assertTrue(confirm.resolve(False, key=first_id))


class BackgroundMonitorSafetyTests(unittest.TestCase):
    def test_add_monitor_refuses_more_than_one_hundred_topics(self) -> None:
        monitors = {
            f"topic-{index}": {"topic": f"weather {index}"}
            for index in range(100)
        }

        def update(mutator):
            mutator(monitors)
            return monitors

        with patch.object(monitor_action, "_update", side_effect=update):
            result = monitor_action.add_monitor("new science discovery")
        self.assertIn("limit reached", result)
        self.assertEqual(len(monitors), 100)

    def test_legacy_monitor_listing_is_bounded_sanitized_and_filtered(self) -> None:
        legacy = {
            "bad": {"topic": "bitcoin price"},
            "control": {"topic": "space\x00\n launch"},
            **{
                f"topic-{index}": {"topic": f"science {index}"}
                for index in range(150)
            },
        }
        with patch.object(monitor_action, "_load", return_value=legacy):
            topics = monitor_action.list_monitors()
        self.assertLessEqual(len(topics), 100)
        self.assertNotIn("bitcoin price", topics)
        self.assertTrue(all("\x00" not in topic and "\n" not in topic for topic in topics))


class ReminderSafetyTests(unittest.TestCase):
    def test_private_reminder_script_uses_the_shared_path_policy(self) -> None:
        # Keep the temporary home in the real home so Windows does not mix a
        # short (RUNNER~1) TEMP spelling with Path.home's long spelling.
        with TemporaryDirectory(dir=Path.home()) as directory:
            home = Path(directory)
            with patch("actions.reminder.Path.home", return_value=home):
                script = reminder_action._write_notify_script(
                    "JARVISReminder_test", "quote ' and newline removed", "linux"
                )
            self.assertTrue(script.is_file())
            self.assertEqual(script.parent, home / ".jarvis" / "reminders")
            if sys.platform != "win32":
                self.assertEqual(script.stat().st_mode & 0o077, 0)


class WebSearchWorkerTests(unittest.TestCase):
    def test_worker_slot_exhaustion_fails_fast_and_recovers(self) -> None:
        original = web_search_action._search_slots
        web_search_action._search_slots = threading.BoundedSemaphore(4)
        release = threading.Event()
        entered = []

        def blocked():
            entered.append(True)
            release.wait(2)
            return "done"

        try:
            for _ in range(4):
                self.assertIsNone(web_search_action._run_bounded(blocked, 0.01, "test"))
            deadline = time.monotonic() + 1
            while len(entered) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(entered), 4)
            called = []
            started = time.monotonic()
            self.assertIsNone(
                web_search_action._run_bounded(
                    lambda: called.append(True), 1, "exhausted-test"
                )
            )
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(called)
            release.set()
            acquired = 0
            deadline = time.monotonic() + 1
            while acquired < 4 and time.monotonic() < deadline:
                if web_search_action._search_slots.acquire(blocking=False):
                    acquired += 1
                else:
                    time.sleep(0.01)
            for _ in range(acquired):
                web_search_action._search_slots.release()
            self.assertEqual(acquired, 4)
            self.assertEqual(
                web_search_action._run_bounded(lambda: "recovered", 1, "test"),
                "recovered",
            )
        finally:
            release.set()
            web_search_action._search_slots = original


class ProcessCancellationTests(unittest.TestCase):
    def test_output_capture_keeps_only_a_bounded_tail(self) -> None:
        result = run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 2000000 + 'END')"],
            timeout=5,
            max_output=4096,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
        self.assertTrue(result.stdout.endswith("END"))

    def test_cancellation_terminates_child_process(self) -> None:
        event = threading.Event()
        timer = threading.Timer(0.2, event.set)
        timer.start()
        started = time.monotonic()
        try:
            result = run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=10,
                cancel_event=event,
            )
        finally:
            timer.cancel()
        self.assertTrue(result.cancelled)
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group behavior")
    def test_timeout_escalates_to_descendant_ignoring_sigterm(self) -> None:
        code = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c',"
            "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
            "print(p.pid, flush=True); time.sleep(30)"
        )
        result = run_bounded([sys.executable, "-c", code], timeout=0.5)
        self.assertTrue(result.timed_out)
        child_pid = int(result.stdout.strip().splitlines()[0])
        deadline = time.monotonic() + 2
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
                stat_path = Path(f"/proc/{child_pid}/stat")
                try:
                    process_state = stat_path.read_text().split()[2]
                except FileNotFoundError:
                    alive = False
                    break
                if process_state == "Z":
                    alive = False
                    break
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.02)
        self.assertFalse(alive, "descendant survived timeout escalation")


if __name__ == "__main__":
    unittest.main()
