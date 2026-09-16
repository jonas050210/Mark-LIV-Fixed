import base64
import json
import unittest
from unittest.mock import MagicMock, patch

from plugins import gmail_mailbox as gmail


class GmailMailboxTests(unittest.TestCase):
    def test_prefers_plain_body_and_skips_attachments(self):
        def part(kind, text, filename=""):
            return {"mimeType": kind, "filename": filename,
                    "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}

        payload = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                part("text/plain", "Plain message"),
                part("text/html", "<b>HTML message</b>"),
            ]},
            part("text/plain", "Secret attachment", "attached.txt"),
        ]}
        self.assertEqual(gmail._body(payload), "Plain message")

    @patch.object(gmail, "_today_bounds", return_value=(100, 200))
    @patch.object(gmail, "_credentials", return_value=object())
    @patch.object(gmail, "build")
    def test_reads_only_todays_unread_without_mutation(self, build, _credentials, _bounds):
        messages = MagicMock()
        build.return_value.users.return_value.messages.return_value = messages
        messages.list.return_value.execute.return_value = {"messages": [{"id": "1"}, {"id": "2"}]}
        messages.get.return_value.execute.side_effect = [
            {"internalDate": "150000", "payload": {"headers": [
                {"name": "From", "value": "Alice"}, {"name": "Subject", "value": "Meeting"}],
                "mimeType": "text/plain", "body": {"data": "SGVsbG8="}}},
            {"internalDate": "99000", "snippet": "old"},
        ]

        result = gmail.run({"operation": "today_unread"})

        self.assertIn("is:unread after:99 before:200", str(messages.list.call_args))
        self.assertEqual(json.loads(result.split("\n", 1)[1]),
                         [{"id": "1", "from": "Alice", "subject": "Meeting", "body": "Hello"}])
        messages.modify.assert_not_called()

    @patch.object(gmail, "_credentials", return_value=object())
    @patch.object(gmail, "build")
    def test_search_includes_read_and_archived_mail(self, build, _credentials):
        messages = build.return_value.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {"messages": [{"id": "abc"}]}
        messages.get.return_value.execute.return_value = {
            "payload": {"headers": [], "mimeType": "text/plain", "body": {"data": "SGVsbG8="}}}
        result = gmail.run({"operation": "search", "query": "from:alice@example.com"})
        self.assertEqual(messages.list.call_args.kwargs["q"], "from:alice@example.com")
        self.assertEqual(json.loads(result.split("\n", 1)[1])[0]["id"], "abc")

    @patch.object(gmail, "push_undo")
    def test_archive_changes_only_inbox_messages_and_registers_undo(self, push_undo):
        messages = MagicMock()
        messages.get.return_value.execute.side_effect = [
            {"labelIds": ["INBOX", "UNREAD"]}, {"labelIds": ["STARRED"]}]
        result = gmail._modify(MagicMock(users=lambda: MagicMock(messages=lambda: messages)),
                               ["inbox-id", "archived-id"], "INBOX", False, "archive")
        self.assertIn("(1 messages)", result)
        self.assertEqual(messages.batchModify.call_args.kwargs["body"],
                         {"ids": ["inbox-id"], "removeLabelIds": ["INBOX"]})
        push_undo.call_args.args[1]()
        self.assertEqual(messages.batchModify.call_args.kwargs["body"],
                         {"ids": ["inbox-id"], "addLabelIds": ["INBOX"]})

    @patch.object(gmail, "_credentials", return_value=object())
    @patch.object(gmail, "build")
    def test_create_label_and_apply_to_selected_message(self, build, _credentials):
        service = build.return_value
        service.users.return_value.labels.return_value.list.return_value.execute.side_effect = [
            {"labels": []}, {"labels": [{"name": "Projects", "id": "Label_1", "type": "user"}]}]
        created = gmail.run({"operation": "create_label", "label_name": "Projects"})
        self.assertIn("Created", created)
        service.users.return_value.labels.return_value.create.assert_called_once()
        service.users.return_value.messages.return_value.get.return_value.execute.return_value = {"labelIds": []}
        with patch.object(gmail, "push_undo"):
            applied = gmail.run({"operation": "apply_label", "label_name": "Projects", "message_ids": ["abc"]})
        self.assertIn("Done", applied)
        self.assertEqual(service.users.return_value.messages.return_value.batchModify.call_args.kwargs["body"],
                         {"ids": ["abc"], "addLabelIds": ["Label_1"]})


if __name__ == "__main__":
    unittest.main()
