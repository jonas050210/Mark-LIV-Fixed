from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dashboard.server as dashboard_module
from dashboard.server import DashboardServer


def _bare_server() -> DashboardServer:
    server = DashboardServer.__new__(DashboardServer)
    server._tokens = {}
    server._token_keys = {}
    server._aes_cache = {}
    server._mac_cache = {}
    server._pending_keys = {}
    server._command_queue = asyncio.Queue(maxsize=2)
    return server


class DashboardSessionSafetyTests(unittest.TestCase):
    def test_expired_token_is_rejected_and_removed(self) -> None:
        server = _bare_server()
        server._tokens["expired"] = time.time() - 1
        server._token_keys["expired"] = "secret"

        self.assertFalse(server._valid_token("expired"))
        self.assertNotIn("expired", server._tokens)
        self.assertNotIn("expired", server._token_keys)

    def test_authentication_tokens_and_pairing_keys_are_capped(self) -> None:
        server = _bare_server()
        for index in range(1_010):
            server._issue_token(f"session-{index}")
        self.assertLessEqual(len(server._tokens), 1_000)
        self.assertEqual(set(server._tokens), set(server._token_keys))

        for _ in range(110):
            server.new_key()
        self.assertLessEqual(len(server._pending_keys), 100)

    def test_plain_http_fallback_is_loopback_only(self) -> None:
        server = _bare_server()
        server._ip = "192.168.1.25"
        with patch.object(server, "_ssl_enabled", return_value=False):
            self.assertEqual(server.get_url(), "http://127.0.0.1:8000")
            self.assertEqual(server.get_manual_url(), "127.0.0.1:8000")

    def test_command_queue_refuses_unbounded_backlog(self) -> None:
        server = _bare_server()
        self.assertTrue(server._enqueue_command("one"))
        self.assertTrue(server._enqueue_command("two"))
        self.assertFalse(server._enqueue_command("three"))

    def test_key_derivation_caches_are_bounded(self) -> None:
        server = _bare_server()
        for index in range(1_100):
            server._aes_key(f"aes-{index}")
            server._mac_key(f"mac-{index}")
        self.assertLessEqual(len(server._aes_cache), 1_000)
        self.assertLessEqual(len(server._mac_cache), 1_000)

    @unittest.skipUnless(
        dashboard_module._DEPS_OK,
        "dashboard HTTP dependencies are unavailable",
    )
    def test_stream_limit_rejects_body_larger_than_false_content_length(self) -> None:
        from fastapi.testclient import TestClient

        server = DashboardServer()
        client = TestClient(server.app)
        login = client.post("/login", json={"pin": server.new_key()})
        self.assertEqual(login.status_code, 200)
        body = b'{"text":"' + (b"x" * dashboard_module.MAX_REQUEST_BYTES) + b'"}'
        response = client.post(
            "/api/command",
            content=body,
            headers={
                "Authorization": f"Bearer {login.json()['token']}",
                "Content-Type": "application/json",
                "Content-Length": "1",
            },
        )
        self.assertEqual(response.status_code, 413)

    @unittest.skipUnless(
        dashboard_module._DEPS_OK and dashboard_module._UPLOAD_OK,
        "dashboard HTTP dependencies are unavailable",
    )
    def test_uploads_publish_complete_files_without_overwrite(self) -> None:
        from fastapi.testclient import TestClient

        with TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            with patch.object(dashboard_module, "UPLOADS_DIR", root):
                server = DashboardServer()
            client = TestClient(server.app)
            login = client.post("/login", json={"pin": server.new_key()})
            self.assertEqual(login.status_code, 200)
            headers = {"Authorization": f"Bearer {login.json()['token']}"}

            first = client.post(
                "/api/upload",
                headers=headers,
                files={"file": ("note.txt", b"complete", "text/plain")},
            )
            second = client.post(
                "/api/upload",
                headers=headers,
                files={"file": ("note.txt", b"second", "text/plain")},
            )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual((root / "note.txt").read_bytes(), b"complete")
            self.assertEqual((root / "note_1.txt").read_bytes(), b"second")
            self.assertFalse(list(root.glob(".upload-*.tmp")))


if __name__ == "__main__":
    unittest.main()
