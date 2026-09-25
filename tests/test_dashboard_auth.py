"""Authentication and revocation tests for the dashboard.

The dashboard is the only part of MARK LIV reachable from another machine, so
it is the only part where a mistake is exploitable by someone who is not
sitting at the computer. These tests drive the real application through
FastAPI's test client rather than checking the source for the right-looking
lines.
"""
from __future__ import annotations

import time
import unittest

try:
    from fastapi.testclient import TestClient

    from dashboard.server import DashboardServer

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on optional packages
    TestClient = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# Routes a browser must be able to reach before it has a token.
PUBLIC_PATHS = {"/", "/login", "/auto-login", "/static/crypto.js", "/api/device-login"}


@unittest.skipIf(TestClient is None, f"FastAPI is unavailable ({_IMPORT_ERROR})")
class DashboardAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = DashboardServer()
        self.app = self.server._build_app()
        self.client = TestClient(self.app)

    def _session(self, pin: str) -> dict[str, str]:
        self.server._pending_keys[pin] = time.time() + 300
        response = self.client.post("/login", json={"pin": pin})
        self.assertEqual(response.status_code, 200, response.text)
        return {"authorization": f"Bearer {response.json()['token']}"}

    def test_every_api_route_refuses_an_anonymous_caller(self) -> None:
        """Walked from the route table, so a new endpoint cannot be forgotten."""
        unprotected = []
        for route in self.app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            if path in PUBLIC_PATHS or not path.startswith(("/api", "/uploads")):
                continue
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                target = path.replace("{action_id}", "x").replace("{filename}", "x")
                response = self.client.request(method, target, json={})
                if response.status_code not in (401, 403, 405, 422):
                    unprotected.append(f"{method} {path} -> {response.status_code}")
        self.assertEqual(unprotected, [])

    def test_the_api_schema_is_not_published(self) -> None:
        """The docs were disabled; the schema they are built from must be too."""
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)
        self.assertEqual(self.client.get("/docs").status_code, 404)

    def test_a_pairing_pin_works_exactly_once(self) -> None:
        self.server._pending_keys["ONESHOT"] = time.time() + 300
        self.assertEqual(self.client.post("/login", json={"pin": "ONESHOT"}).status_code, 200)
        self.assertEqual(self.client.post("/login", json={"pin": "ONESHOT"}).status_code, 401)

    def test_an_expired_pairing_pin_is_refused(self) -> None:
        self.server._pending_keys["STALE"] = time.time() - 1
        self.assertEqual(self.client.post("/login", json={"pin": "STALE"}).status_code, 401)

    def test_repeated_wrong_pins_are_rate_limited(self) -> None:
        codes = [
            self.client.post("/login", json={"pin": f"WRONG{index}"}).status_code
            for index in range(12)
        ]
        self.assertIn(429, codes, "brute force was never throttled")

    def test_a_tampered_token_is_rejected(self) -> None:
        headers = self._session("GOOD")
        token = headers["authorization"].removeprefix("Bearer ")
        for forged in (token[:-1] + "Z", token.upper(), token + "x", "", "null", "x" * 300):
            response = self.client.get(
                "/api/capabilities", headers={"authorization": f"Bearer {forged}"}
            )
            self.assertEqual(response.status_code, 401, forged[:20])

    def test_an_expired_token_stops_working(self) -> None:
        headers = self._session("TIMED")
        token = headers["authorization"].removeprefix("Bearer ")
        self.server._tokens[token] = time.time() - 1
        self.assertEqual(self.client.get("/api/capabilities", headers=headers).status_code, 401)


@unittest.skipIf(TestClient is None, f"FastAPI is unavailable ({_IMPORT_ERROR})")
class RevocationTests(unittest.TestCase):
    """Revoking access has to actually end the sessions that already exist."""

    def setUp(self) -> None:
        self.server = DashboardServer()
        self.client = TestClient(self.server._build_app())

    def _session(self, pin: str) -> dict[str, str]:
        self.server._pending_keys[pin] = time.time() + 300
        token = self.client.post("/login", json={"pin": pin}).json()["token"]
        return {"authorization": f"Bearer {token}"}

    def test_revoking_ends_every_other_live_session(self) -> None:
        """A phone that is already logged in holds a bearer token in memory.
        Clearing only the remembered devices left that token valid, so someone
        revoking access after losing a device was told it was done while the
        lost device still had a working session."""
        admin = self._session("ADMIN")
        phone = self._session("PHONE")
        self.assertEqual(self.client.get("/api/capabilities", headers=phone).status_code, 200)

        response = self.client.post("/api/revoke-devices", json={}, headers=admin)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["sessions_closed"], 1)

        self.assertEqual(self.client.get("/api/capabilities", headers=phone).status_code, 401)

    def test_the_session_doing_the_revoking_keeps_working(self) -> None:
        admin = self._session("ADMIN")
        self.client.post("/api/revoke-devices", json={}, headers=admin)
        self.assertEqual(self.client.get("/api/capabilities", headers=admin).status_code, 200)

    def test_remembered_devices_are_cleared_too(self) -> None:
        admin = self._session("ADMIN")
        self.server._device_sessions["device-token"] = {"session_key": "k", "expires_at": 1e12}
        response = self.client.post("/api/revoke-devices", json={}, headers=admin)
        self.assertEqual(response.json()["revoked"], 1)
        self.assertEqual(self.server._device_sessions, {})

    def test_a_revoked_device_token_cannot_log_back_in(self) -> None:
        admin = self._session("ADMIN")
        self.server._device_sessions["device-token"] = {"session_key": "k", "expires_at": 1e12}
        self.client.post("/api/revoke-devices", json={}, headers=admin)
        response = self.client.post("/api/device-login", json={"device_token": "device-token"})
        self.assertEqual(response.status_code, 401)


@unittest.skipIf(TestClient is None, f"FastAPI is unavailable ({_IMPORT_ERROR})")
class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = DashboardServer()
        self.client = TestClient(self.server._build_app())
        self.server._pending_keys["P"] = time.time() + 300
        token = self.client.post("/login", json={"pin": "P"}).json()["token"]
        self.headers = {"authorization": f"Bearer {token}"}

    def test_a_disconnected_registry_answers_503_with_a_reason(self) -> None:
        """The dashboard can be open before the assistant has finished starting.
        That is not a server fault and must not be reported as one."""
        for path, payload in (
            ("/api/apps/launch", {"name": "Chrome"}),
            ("/api/layouts", {"action": "list"}),
            ("/api/spotify", {"action": "play"}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 503)
                self.assertIn("not ready", response.json()["error"])

    def test_a_path_instead_of_a_name_is_not_executed(self) -> None:
        launched = []
        self.server._action_callback = lambda name, params: launched.append(params) or "ok"
        self.client.post("/api/apps/launch", json={"name": "/bin/sh"}, headers=self.headers)
        # The name is passed as a name; open_app resolves it against the index
        # and never treats it as a command line.
        self.assertEqual(launched[0]["app_name"], "/bin/sh")
        self.assertNotIn("arguments", launched[0])

    def test_a_control_character_in_a_name_is_refused(self) -> None:
        response = self.client.post(
            "/api/apps/launch", json={"name": "Chrome\x00--evil"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 400)

    def test_uploads_cannot_escape_their_directory(self) -> None:
        for name in (
            "../../../etc/passwd", "..%2f..%2fetc%2fpasswd", "....//etc/passwd",
            "a/../../../etc/passwd", "C:\\Windows\\win.ini",
        ):
            with self.subTest(name=name):
                response = self.client.get(f"/uploads/{name}", headers=self.headers)
                self.assertIn(response.status_code, (400, 403, 404))
                self.assertNotIn("root:", response.text)

    def test_explorer_refuses_locations_outside_the_allowed_roots(self) -> None:
        for query in ("../../../etc", "/etc/shadow", "~/.ssh/id_rsa"):
            with self.subTest(query=query):
                response = self.client.get(
                    "/api/explorer/search", params={"query": query}, headers=self.headers
                )
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
