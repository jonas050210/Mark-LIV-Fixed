"""Tests for listing and cancelling reminders.

A reminder is handed to the operating system's own scheduler so that it fires
whether or not MARK LIV is running. Until now that also meant the assistant had
no record of it: a reminder could only be removed through Task Scheduler,
launchctl or atrm by hand. These tests cover the registry that closes that gap,
and in particular the case that matters most — a cancellation the scheduler
refuses must be reported as a failure, because the reminder will still fire.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from actions import reminder as reminder_module


class ReminderRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.previous = reminder_module.REMINDER_FILE
        reminder_module.REMINDER_FILE = Path(self.directory.name) / "reminders.json"
        self.script = Path(self.directory.name) / "notify.py"
        self.script.write_text("# notify", encoding="utf-8")

    def tearDown(self) -> None:
        reminder_module.REMINDER_FILE = self.previous
        self.directory.cleanup()

    def _set(self, message: str, *, hours: int = 2, backend=("job-1", "systemd")) -> str:
        when = datetime.now() + timedelta(hours=hours)
        with patch.object(reminder_module, "_get_os", return_value="linux"), \
             patch.object(reminder_module, "_write_notify_script", return_value=self.script), \
             patch.object(reminder_module, "_schedule_linux", return_value=backend):
            return reminder_module.reminder({
                "action": "set",
                "date": when.strftime("%Y-%m-%d"),
                "time": when.strftime("%H:%M"),
                "message": message,
            })

    def test_an_empty_registry_says_so(self) -> None:
        self.assertIn("no reminders", reminder_module.reminder({"action": "list"}))

    def test_a_scheduled_reminder_appears_in_the_list(self) -> None:
        self.assertIn("Reminder set", self._set("call mum"))
        listing = reminder_module.reminder({"action": "list"})
        self.assertIn("call mum", listing)
        self.assertIn("1.", listing)

    def test_a_failed_schedule_is_not_recorded(self) -> None:
        result = self._set("ghost", backend=("", ""))
        self.assertIn("couldn't register", result)
        self.assertIn("no reminders", reminder_module.reminder({"action": "list"}))

    def test_cancelling_by_number_removes_the_reminder(self) -> None:
        self._set("call mum")
        self._set("standup", hours=20)
        with patch.object(reminder_module, "_run", return_value=(True, "")):
            result = reminder_module.reminder({"action": "cancel", "index": 1})
        self.assertIn("Cancelled", result)
        self.assertIn("call mum", result)
        self.assertNotIn("call mum", reminder_module.reminder({"action": "list"}))

    def test_cancelling_by_message_removes_the_reminder(self) -> None:
        self._set("call mum")
        self._set("standup", hours=20)
        with patch.object(reminder_module, "_run", return_value=(True, "")):
            reminder_module.reminder({"action": "cancel", "message": "standup"})
        self.assertNotIn("standup", reminder_module.reminder({"action": "list"}))

    def test_a_single_reminder_can_be_cancelled_without_naming_it(self) -> None:
        self._set("only one")
        with patch.object(reminder_module, "_run", return_value=(True, "")):
            self.assertIn("Cancelled", reminder_module.reminder({"action": "cancel"}))

    def test_a_refused_cancellation_is_reported_as_a_failure(self) -> None:
        """The job is still registered, so the reminder will still fire."""
        self._set("call mum")
        with patch.object(reminder_module, "_run", return_value=(False, "unit not found")):
            result = reminder_module.reminder({"action": "cancel", "index": 1})
        self.assertIn("could not cancel", result.lower())
        self.assertIn("still scheduled", result)
        # And it must stay in the list, because it is still going to fire.
        self.assertIn("call mum", reminder_module.reminder({"action": "list"}))

    def test_an_ambiguous_cancellation_asks_instead_of_guessing(self) -> None:
        self._set("meeting with Anna")
        self._set("meeting with Bob", hours=5)
        result = reminder_module.reminder({"action": "cancel", "message": "meeting"})
        self.assertIn("could not tell which", result)

    def test_a_reminder_whose_time_has_passed_is_pruned(self) -> None:
        reminder_module._write_registry([{
            "id": "old", "when": "2020-01-01 09:00", "message": "ancient",
            "backend": "systemd", "handle": "old", "script": "",
        }])
        self.assertIn("no reminders", reminder_module.reminder({"action": "list"}))

    def test_the_default_action_still_sets_a_reminder(self) -> None:
        when = datetime.now() + timedelta(hours=3)
        with patch.object(reminder_module, "_get_os", return_value="linux"), \
             patch.object(reminder_module, "_write_notify_script", return_value=self.script), \
             patch.object(reminder_module, "_schedule_linux", return_value=("j", "systemd")):
            result = reminder_module.reminder({
                "date": when.strftime("%Y-%m-%d"),
                "time": when.strftime("%H:%M"),
                "message": "no action given",
            })
        self.assertIn("Reminder set", result)

    def test_an_at_job_without_a_number_is_flagged_as_uncancellable(self) -> None:
        result = self._set("fuzzy", backend=("", "at"))
        self.assertIn("Reminder set", result)
        self.assertIn("not be able to cancel", result)

    def test_the_tool_declares_the_three_actions(self) -> None:
        enum = reminder_module.TOOL["parameters"]["properties"]["action"]["enum"]
        self.assertEqual(sorted(enum), ["cancel", "list", "set"])


class ScheduledJobCancellationTests(unittest.TestCase):
    """Each backend must be cancelled with its own command, or not at all."""

    def test_windows_uses_schtasks_delete(self) -> None:
        with patch.object(reminder_module, "_run", return_value=(True, "")) as run:
            ok, _ = reminder_module._cancel_job(
                {"backend": "schtasks", "handle": "JARVISReminder_1"}
            )
        self.assertTrue(ok)
        self.assertEqual(run.call_args[0][0][:3], ["schtasks", "/Delete", "/TN"])

    def test_at_uses_atrm_with_the_job_number(self) -> None:
        with patch.object(reminder_module, "_run", return_value=(True, "")) as run:
            reminder_module._cancel_job({"backend": "at", "handle": "42"})
        self.assertEqual(run.call_args[0][0], ["atrm", "42"])

    def test_an_unknown_backend_fails_loudly(self) -> None:
        ok, detail = reminder_module._cancel_job({"backend": "cron", "handle": "x"})
        self.assertFalse(ok)
        self.assertIn("cron", detail)

    def test_a_reminder_without_a_handle_cannot_be_cancelled(self) -> None:
        ok, detail = reminder_module._cancel_job({"backend": "at", "handle": ""})
        self.assertFalse(ok)
        self.assertIn("handle", detail)


if __name__ == "__main__":
    unittest.main()


class ReminderUndoTests(unittest.TestCase):
    """Cancelling a reminder must be reversible.

    Cancellation deletes the scheduler job and the registry entry together, so
    without an undo entry "no, not that one" means dictating the date, the time
    and the message again. Re-scheduling is the exact inverse, which is what
    makes it an honest undo rather than an approximation.
    """

    def setUp(self) -> None:
        from core import undo

        self.undo = undo
        undo.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.previous = reminder_module.REMINDER_FILE
        reminder_module.REMINDER_FILE = Path(self.directory.name) / "reminders.json"
        self.script = Path(self.directory.name) / "notify.py"
        self.script.write_text("# notify", encoding="utf-8")
        self.when = datetime.now() + timedelta(days=1)

    def tearDown(self) -> None:
        reminder_module.REMINDER_FILE = self.previous
        self.undo.clear()
        self.directory.cleanup()

    def _set(self, message: str = "dentist") -> str:
        with patch.object(reminder_module, "_get_os", return_value="linux"), \
             patch.object(reminder_module, "_write_notify_script", return_value=self.script), \
             patch.object(reminder_module, "_schedule_linux", return_value=("77", "systemd")):
            return reminder_module.reminder({
                "action": "set",
                "date": self.when.strftime("%Y-%m-%d"),
                "time": self.when.strftime("%H:%M"),
                "message": message,
            })

    def test_a_cancelled_reminder_can_be_restored(self) -> None:
        self._set()
        with patch.object(reminder_module, "_run", return_value=(True, "")):
            reminder_module.reminder({"action": "cancel", "index": 1})
        self.assertIn("no reminders", reminder_module.reminder({"action": "list"}))

        with patch.object(reminder_module, "_get_os", return_value="linux"), \
             patch.object(reminder_module, "_write_notify_script", return_value=self.script), \
             patch.object(reminder_module, "_schedule_linux", return_value=("78", "systemd")):
            message = self.undo.undo_last()
        self.assertIn("Restored", message)
        self.assertIn("dentist", reminder_module.reminder({"action": "list"}))

    def test_a_newly_set_reminder_can_be_taken_back(self) -> None:
        self._set("gym")
        with patch.object(reminder_module, "_run", return_value=(True, "")):
            message = self.undo.undo_last()
        self.assertIn("Removed the reminder", message)
        self.assertIn("no reminders", reminder_module.reminder({"action": "list"}))

    def test_taking_back_a_reminder_the_scheduler_will_not_drop_is_reported(self) -> None:
        self._set("gym")
        with patch.object(reminder_module, "_run", return_value=(False, "unit not found")):
            message = self.undo.undo_last()
        self.assertIn("could not remove", message.lower())
        self.assertIn("gym", reminder_module.reminder({"action": "list"}))

    def test_a_reminder_whose_time_has_passed_is_not_restored(self) -> None:
        past = datetime.now() - timedelta(hours=2)
        result = reminder_module._restore_reminder({"message": "old"}, past)
        self.assertIn("already passed", result)

    def test_a_failed_schedule_registers_no_undo(self) -> None:
        with patch.object(reminder_module, "_get_os", return_value="linux"), \
             patch.object(reminder_module, "_write_notify_script", return_value=self.script), \
             patch.object(reminder_module, "_schedule_linux", return_value=("", "")):
            reminder_module.reminder({
                "action": "set",
                "date": self.when.strftime("%Y-%m-%d"),
                "time": self.when.strftime("%H:%M"),
                "message": "ghost",
            })
        self.assertFalse(self.undo.can_undo())
