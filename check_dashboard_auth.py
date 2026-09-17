#!/usr/bin/env python3
"""
check_dashboard_auth.py — regression tests for the phone dashboard's front door.

WHAT IT PROVES
    1. The pairing key is long enough, unambiguous, unique, and single-use — and
       the login page is generated from the server's own length, so the field can
       never reject the key JARVIS just issued.
    2. Guessing is not free: the typed route and the QR route share one per-
       address budget, a lockout refuses even the CORRECT key until it expires,
       the wait doubles each round, and a spoofed X-Forwarded-For buys nothing.
    3. Persistent device tokens are limited too, on their own looser budget, so a
       phone reloading the page cannot lock its owner out of typing a real key.
    4. A paired device gets a bearer token that authenticates; a wrong one does
       not.
    5. Reachability is opt-in and real: the socket starts on 127.0.0.1, no
       firewall rule is requested, the LAN address cannot connect — and flipping
       the switch rebinds the LIVE socket (in both directions) without losing the
       keys already issued.

HOW
    Endpoints are driven in-process through FastAPI's TestClient — no ports, no
    browser. The reachability test is the exception: it runs a real uvicorn on a
    free port in a thread, connects to it with real sockets, and stubs out only
    the things that would touch this machine (TLS generation, the OS firewall).

Needs the dashboard's own dependencies:
    pip install fastapi "uvicorn[standard]" cryptography python-multipart httpx

Run:  python check_dashboard_auth.py
"""
from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import math
import re
import socket
import sys
import threading
import time
from pathlib import Path

# Legacy Windows consoles cannot encode the symbols below — the same trap
# main.py defuses at startup, and the reason a test report must never be the
# thing that crashes.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import dashboard.server as ds                     # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)), flush=True)


def new_client():
    """A fresh server + client. Fresh per test, because the throttle remembers."""
    from fastapi.testclient import TestClient
    srv = ds.DashboardServer()
    return srv, TestClient(srv.app)


IP = "testclient"          # what TestClient reports as the peer address


class _FakeReq:
    """Stand-in for starlette's Request — _client_ip reads .client and nothing else."""

    def __init__(self, host: str | None = None, headers: dict | None = None):
        self.client = type("Peer", (), {"host": host})() if host else None
        self.headers = headers or {}


# ── 1. the pairing key ──────────────────────────────────────────────────────

def test_key() -> None:
    section("The pairing key")

    check("the key length is what the server says it is",
          ds.KEY_LEN >= 8, f"KEY_LEN={ds.KEY_LEN}")
    space = len(ds._KEY_CHARS) ** ds.KEY_LEN
    check("the keyspace is beyond brute force at this rate",
          space >= 2 ** 32, f"{space:.3g} ≈ 2^{math.log2(space):.0f}")
    check("the alphabet has no look-alikes",
          not (set("OIL01") & set(ds._KEY_CHARS)))

    srv, c = new_client()
    keys = [srv.new_key() for _ in range(200)]
    check("every key is KEY_LEN characters of that alphabet",
          all(len(k) == ds.KEY_LEN and all(ch in ds._KEY_CHARS for ch in k) for k in keys),
          keys[0])
    check("keys do not repeat", len(set(keys)) == len(keys))

    page = c.get("/login").text
    check("the login page is generated from the server's length",
          f"const KEY_LEN = {ds.KEY_LEN};" in page
          and f'maxlength="{ds.KEY_LEN}"' in page)
    check("no template token survives into the page", "__KEY_LEN__" not in page)
    check("the page counts a lockout down instead of leaving the user guessing",
          "function lock(" in page and "retry_after" in page)
    check("…and a throttled device keeps its pairing",
          "r.status === 429" in page)

    key = srv.new_key()
    r = c.post("/login", json={"pin": key})
    check("the right key logs in", r.status_code == 200 and r.json().get("ok") is True)
    tok = r.json().get("token", "")
    check("…and gets a bearer token", len(tok) >= 32, f"{len(tok)} chars")
    check("the key is one-time", c.post("/login", json={"pin": key}).status_code == 401)
    check("a near-miss is refused",
          c.post("/login", json={"pin": key[:-1] + ("A" if key[-1] != "A" else "B")}).status_code
          in (401, 429))
    check("lowercase is accepted (phones autocorrect)",
          c.post("/login", json={"pin": srv.new_key().lower()}).status_code == 200)

    expired = srv.new_key(expiry_secs=-1)
    check("an expired key is refused",
          c.post("/login", json={"pin": expired}).status_code == 401)
    check("a malformed body is a refusal, not a 500",
          c.post("/login", content=b"{not json", headers={"Content-Type": "application/json"}
                 ).status_code == 401)


# ── 2. guessing is not free ─────────────────────────────────────────────────

def test_throttle_unit() -> None:
    section("The throttle itself")

    t = ds._LoginThrottle(window=60, max_attempts=5, base=30, ceiling=900)
    for _ in range(4):
        t.fail("phone")
    check("four misses are not a lockout yet", t.retry_after("phone") == 0.0)
    wait = t.fail("phone")
    check("the fifth locks the address out", wait == 30 and t.retry_after("phone") > 0,
          f"{wait}s")
    check("another address is untouched by it", t.retry_after("laptop") == 0.0)
    check("a correct key clears the slate",
          (t.success("phone"), t.retry_after("phone") == 0.0)[1])

    t2 = ds._LoginThrottle(window=60, max_attempts=5, base=30, ceiling=900)
    waits = []
    for round_no in range(4):
        for _ in range(5):
            w = t2.fail("attacker")
        waits.append(w)
        t2._until["attacker"] = time.time() - 1     # the wait passes
        t2.retry_after("attacker")
    check("the lockout doubles every round", waits == [30, 60, 120, 240], str(waits))
    for _ in range(10):
        for _ in range(5):
            w = t2.fail("attacker")
        t2._until["attacker"] = time.time() - 1
        t2.retry_after("attacker")
    check("…up to a ceiling, so it stays survivable for a real user",
          w == 900, f"{w}s")

    t3 = ds._LoginThrottle(window=0.2, max_attempts=2, base=5)
    t3.fail("x"); t3.fail("x")
    check("misses age out of the window", (time.sleep(0.25), t3.fail("x"))[1] == 0.0)
    t4 = ds._LoginThrottle(window=0.05, max_attempts=2, base=1, ceiling=1)
    for i in range(50):
        t4.fail(f"visitor{i}")
    check("fresh misses are kept — they are the whole point", len(t4._misses) == 50)
    time.sleep(0.08)
    t4.prune()
    check("prune() forgets visitors whose misses aged out",
          len(t4._misses) == 0, f"{len(t4._misses)} left")

    # A lockout that is still running must not be swept, and neither must the
    # strike count that makes the next one longer.
    t5 = ds._LoginThrottle(window=60, max_attempts=1, base=30, ceiling=900)
    t5.fail("attacker")
    t5.prune()
    check("prune() keeps a lockout that is still running",
          t5.retry_after("attacker") > 0 and t5._strikes.get("attacker") == 1)
    t5._until["attacker"] = time.time() - 1      # the wait passes
    t5.retry_after("attacker")
    t5.prune()                                   # the owner pressed NEW KEY
    wait2 = t5.fail("attacker")
    check("…and never resets the doubling", wait2 == 60, f"{wait2}s (strike 2)")


def test_throttle_endpoints() -> None:
    section("Guessing through the real endpoints")

    check("attempts are counted against the peer address",
          ds._client_ip(_FakeReq("192.168.1.7")) == "192.168.1.7")
    check("…never against a header the client controls",
          ds._client_ip(_FakeReq("192.168.1.7", {"x-forwarded-for": "8.8.8.8"})
                        ) == "192.168.1.7")
    check("a request with no peer still lands in a bucket",
          ds._client_ip(_FakeReq()) == "unknown")

    srv, c = new_client()
    codes = [c.post("/login", json={"pin": "WRONGWR1"}).status_code for _ in range(4)]
    check("the first misses are plain refusals", codes == [401] * 4, str(codes))
    r = c.post("/login", json={"pin": "WRONGWR2"})
    check("the fifth is a 429", r.status_code == 429, r.text[:60])
    check("…with Retry-After, as the standard asks",
          int(r.headers.get("retry-after", "0")) >= 1,
          r.headers.get("retry-after", "none"))
    check("…and with a message the page can show", "retry_after" in r.json())

    good = srv.new_key()
    r = c.post("/login", json={"pin": good})
    check("a lockout refuses even the CORRECT key", r.status_code == 429,
          "no bypass while locked")
    check("the key survives the lockout (it is not consumed by a refusal)",
          good in srv._pending_keys)

    srv._key_throttle._until[IP] = time.time() - 1      # the wait passes
    r = c.post("/login", json={"pin": good})
    check("…and works again once it expires", r.status_code == 200 and r.json().get("ok"))

    # The QR route must not be a second, unlimited door.
    srv2, c2 = new_client()
    for _ in range(3):
        c2.post("/login", json={"pin": "WRONGWR1"})
    r1 = c2.get("/auto-login", params={"key": "WRONGWR1"})
    r2 = c2.get("/auto-login", params={"key": "WRONGWR1"})
    check("the QR route shares one budget with the typed route",
          r1.status_code == 401 and r2.status_code == 429,
          f"{r1.status_code} then {r2.status_code}")
    check("a blocked QR scan says how long to wait",
          "Retry-After" in r2.headers or "retry-after" in r2.headers)

    # A header the attacker controls must not reset anything.
    srv3, c3 = new_client()
    last = None
    for i in range(6):
        last = c3.post("/login", json={"pin": "WRONGWR1"},
                       headers={"X-Forwarded-For": f"10.0.0.{i}"})
    check("a spoofed X-Forwarded-For buys no fresh attempts",
          last.status_code == 429, f"last={last.status_code}")

    # Device tokens: limited, but on their own budget.
    srv4, c4 = new_client()
    seen = [c4.post("/api/device-login", json={"device_token": f"nope{i}"}).status_code
            for i in range(30)]
    check("unknown device tokens are refused", 401 in seen)
    check("…and eventually throttled", 429 in seen, f"{seen.count(401)} refusals first")
    check("device-token misses did not lock out the real key",
          c4.post("/login", json={"pin": srv4.new_key()}).status_code == 200)


# ── 3. the QR route and paired devices ──────────────────────────────────────

def test_qr_and_devices() -> None:
    section("QR pairing and the devices it leaves behind")

    srv, c = new_client()
    key = srv.new_key()
    r = c.get("/auto-login", params={"key": key})
    html = r.text
    tok = re.search(r"jarvis_token','([^']+)'", html)
    dev = re.search(r"jarvis_device_token','([^']+)'", html)
    ses = re.search(r"jarvis_key','([^']+)'", html)
    check("a valid QR link pairs the phone", r.status_code == 200 and tok and dev)
    check("the phone gets the session key it needs for AES",
          ses is not None and ses.group(1) == key)
    check("the QR link is one-time",
          c.get("/auto-login", params={"key": key}).status_code in (401, 429))

    check("the bearer token authenticates a real endpoint",
          c.get("/api/files", headers={"Authorization": f"Bearer {tok.group(1)}"}
                ).status_code == 200)
    check("a made-up bearer token does not",
          c.get("/api/files", headers={"Authorization": "Bearer nope"}).status_code == 401)
    check("no token at all does not either", c.get("/api/files").status_code == 401)

    r2 = c.post("/api/device-login", json={"device_token": dev.group(1)})
    check("a paired device can come back without a new key",
          r2.status_code == 200 and r2.json().get("ok") is True)
    new_tok = r2.json().get("token", "")
    check("…and its fresh token works",
          c.get("/api/files", headers={"Authorization": f"Bearer {new_tok}"}
                ).status_code == 200)
    check("the device token it was issued is still there",
          r2.json().get("key") == key)

    check("an unknown device token is refused",
          c.post("/api/device-login", json={"device_token": "garbage"}).status_code == 401)
    check("a malformed device-login body is a 400, not a 500",
          c.post("/api/device-login", content=b"{", headers={"Content-Type": "application/json"}
                 ).status_code == 400)

    c.get("/api/revoke-devices", headers={"Authorization": f"Bearer {new_tok}"})
    c.post("/api/revoke-devices", headers={"Authorization": f"Bearer {new_tok}"})
    check("revoking devices really logs them out",
          c.post("/api/device-login", json={"device_token": dev.group(1)}
                 ).status_code in (401, 429))


# ── 4. secret comparison ────────────────────────────────────────────────────

def test_comparison() -> None:
    section("How secrets are compared")

    srv, _c = new_client()
    key = srv.new_key()
    check("the stored key is found by its own value", srv._match_pending_key(key) == key)
    check("a wrong key finds nothing", srv._match_pending_key("ZZZZZZZZ") is None)
    check("an empty key finds nothing", srv._match_pending_key("") is None)
    expired = srv.new_key(expiry_secs=-1)
    check("an expired key is not matched", srv._match_pending_key(expired) is None)

    src = (REPO / "dashboard" / "server.py").read_text(encoding="utf-8", errors="replace")
    check("login routes compare with hmac.compare_digest",
          src.count("hmac.compare_digest") >= 2, f"{src.count('hmac.compare_digest')} uses")
    check("no membership test is left on the guessable secrets",
          "entered in self._pending_keys" not in src
          and "key not in self._pending_keys" not in src
          and "dev_tok not in self._device_sessions" not in src)
    check("a refusal does not say whether the key was wrong or expired",
          src.count('"Invalid or expired key"') >= 1
          and "was never issued" not in src)


# ── 5. who can reach it ─────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _accepts(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_status(host: str, port: int, path: str = "/login", timeout: float = 3.0):
    try:
        c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.request("GET", path)
        status = c.getresponse().status
        c.close()
        return status
    except Exception:
        return None


def _http_login(host: str, port: int, key: str) -> bool:
    try:
        c = http.client.HTTPConnection(host, port, timeout=3.0)
        c.request("POST", "/login", body=json.dumps({"pin": key}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
        return bool(data.get("ok"))
    except Exception:
        return False


def _wait(pred, timeout: float = 12.0, step: float = 0.1) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


@contextlib.contextmanager
def live_server(lan_config: bool | None = None, port: int | None = None):
    """Run the real dashboard on a real port, in a thread, for one test.

    Only the parts that would touch this machine are stubbed: no TLS pair is
    generated, no alias port is opened, and the OS firewall is recorded instead
    of poked (it would prompt for sudo/UAC). Yields (server, port, firewall
    calls, thread) and shuts the server down on the way out.
    """
    port = _free_port() if port is None else port
    fw_calls: list = []
    saved = (ds.PORT, ds._ensure_certs, ds._ensure_network_access,
             ds.DashboardServer._ssl_enabled, ds._lan_enabled_from_config)
    ds.PORT = port
    ds._ensure_certs = lambda: False
    ds._ensure_network_access = lambda p: fw_calls.append(p)
    ds.DashboardServer._ssl_enabled = staticmethod(lambda: False)
    if lan_config is not None:
        ds._lan_enabled_from_config = lambda: bool(lan_config)
    srv = ds.DashboardServer()
    th = threading.Thread(target=lambda: asyncio.run(srv.serve()), daemon=True)
    th.start()
    try:
        yield srv, port, fw_calls, th
    finally:
        loop = srv._loop
        if loop is not None and not loop.is_closed():
            for _ in range(60):
                server = srv._server
                if server is not None:
                    loop.call_soon_threadsafe(setattr, server, "should_exit", True)
                    break
                time.sleep(0.05)
        th.join(timeout=15)
        (ds.PORT, ds._ensure_certs, ds._ensure_network_access,
         ds.DashboardServer._ssl_enabled, ds._lan_enabled_from_config) = saved


def test_reachability() -> None:
    section("Who can reach it — a real socket, rebound live")

    import tempfile
    import memory.config_manager as cm

    saved_cfg, saved_fn = cm.CONFIG_FILE, cm.get_dashboard_lan_enabled
    with tempfile.TemporaryDirectory(prefix="markliv-cfg-") as td:
        cm.CONFIG_FILE = Path(td) / "api_keys.json"
        try:
            check("LAN access is off in a fresh config",
                  cm.get_dashboard_lan_enabled() is False)
            cm.save_dashboard_lan_enabled(True)
            check("the switch survives a restart",
                  cm.get_dashboard_lan_enabled() is True)
            cm.save_dashboard_lan_enabled(False)
            check("…and so does turning it back off",
                  cm.get_dashboard_lan_enabled() is False)
        finally:
            cm.CONFIG_FILE = saved_cfg

    try:
        cm.get_dashboard_lan_enabled = lambda: True
        check("the server reads that setting", ds._lan_enabled_from_config() is True)
        cm.get_dashboard_lan_enabled = lambda: False
        check("…and its absence means loopback", ds._lan_enabled_from_config() is False)

        def unreadable():
            raise RuntimeError("config corrupt")
        cm.get_dashboard_lan_enabled = unreadable
        check("an unreadable config fails CLOSED, not open",
              ds._lan_enabled_from_config() is False)
    finally:
        cm.get_dashboard_lan_enabled = saved_fn

    srv0, _ = new_client()
    check("a new server binds loopback by default",
          srv0._bind_host() == ds.BIND_LOCAL == "127.0.0.1", srv0.describe_reach())
    check("…and says so plainly", "this PC only" in srv0.describe_reach())
    srv0.set_lan_access(True)                 # no loop yet: taken on the next start
    check("the switch is honoured before the server starts",
          srv0._bind_host() == ds.BIND_LAN == "0.0.0.0")

    lan_ip = ds._local_ip()
    with live_server() as (srv, port, fw_calls, th):
        key_before = srv.new_key()          # issued state must survive a rebind
        check("the dashboard comes up", _wait(lambda: _accepts("127.0.0.1", port)))
        check("it serves the login page on loopback",
              _http_status("127.0.0.1", port) == 200)
        check("no firewall rule is requested for a loopback bind",
              fw_calls == [], str(fw_calls))

        if lan_ip and not lan_ip.startswith("127."):
            check("the LAN address cannot reach it while access is off",
                  not _accepts(lan_ip, port), lan_ip)

            srv.set_lan_access(True)
            check("flipping the switch rebinds the LIVE socket",
                  _wait(lambda: _accepts(lan_ip, port)), f"{lan_ip}:{port}")
            check("…and only then asks the OS firewall for the port",
                  _wait(lambda: fw_calls == [port]), str(fw_calls))
            check("loopback keeps working across the rebind",
                  _accepts("127.0.0.1", port) and _http_status("127.0.0.1", port) == 200)
            check("a key issued before the rebind still pairs a phone",
                  _http_login(lan_ip, port, key_before))
            check("the server reports the new reachability",
                  "your network" in srv.describe_reach(), srv.describe_reach())

            srv.set_lan_access(False)
            # Both halves of the end state, waited for together: a rebind closes
            # the old socket a few milliseconds before the new one is listening,
            # and "the LAN address is gone" alone would pass in that gap.
            check("turning it off takes the LAN socket away and keeps loopback",
                  _wait(lambda: not _accepts(lan_ip, port)
                        and _accepts("127.0.0.1", port)))
            check("…and the login page is still served on loopback",
                  _http_status("127.0.0.1", port) == 200)
            check("no extra firewall rule was opened on the way back",
                  fw_calls == [port], str(fw_calls))
        else:
            check("LAN reachability measured without a non-loopback address",
                  True, f"_local_ip()={lan_ip!r} — bind host checks only")
            srv.set_lan_access(True)
            check("the bind host follows the switch", srv._bind_host() == ds.BIND_LAN)
            srv.set_lan_access(False)
    check("the server thread ended when asked", not th.is_alive())


def test_lan_bind_failure() -> None:
    section("A LAN bind that fails must not cost the dashboard")

    lan_ip = ds._local_ip()
    if not lan_ip or lan_ip.startswith("127."):
        check("no non-loopback address here to fail against", True,
              f"_local_ip()={lan_ip!r}")
        return

    # Hold the chosen port on the LAN interface only: loopback stays free. On
    # POSIX this prevents a later 0.0.0.0 bind and proves the fallback path.
    # Windows may allow a wildcard socket to coexist with an existing specific
    # address socket, so the premise is measured after startup instead of being
    # assumed. SO_EXCLUSIVEADDRUSE asks Windows for the stricter behaviour when
    # available, but the branch below remains mandatory because uvicorn/socket
    # semantics can still vary between systems.
    port    = _free_port()
    blocker = socket.socket()
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        with contextlib.suppress(Exception):
            blocker.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    try:
        blocker.bind((lan_ip, port))
        blocker.listen(1)
        with live_server(lan_config=True, port=port) as (srv, p, fw_calls, th):
            check("the dashboard comes up anyway",
                  _wait(lambda: _http_status("127.0.0.1", p) == 200))
            if srv._lan is True and sys.platform == "win32":
                detail = ("Windows allowed the 0.0.0.0 bind to coexist with the "
                          "specific LAN blocker, so this machine did not produce "
                          "a LAN-bind failure to test.")
                check("SKIP: Windows did not force the LAN-bind failure", True, detail)
                check("SKIP: fallback assertions do not apply to a real LAN socket", True,
                      srv.describe_reach())
                check("a firewall rule was opened for the LAN socket that exists",
                      fw_calls == [port], str(fw_calls))
            else:
                check("it fell back instead of giving up", srv._lan is False,
                      srv.describe_reach())
                check("the LAN port is still answered by whoever holds it, not JARVIS",
                      _http_status(lan_ip, p, timeout=2.0) is None)
                check("no firewall rule was opened for a socket that never bound",
                      fw_calls == [], str(fw_calls))
            check("the server is still running", th.is_alive())
    finally:
        blocker.close()


def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print(" MARK LIV — dashboard front door: key, throttle, reachability")
    print("=" * 72)
    if not ds._DEPS_OK:
        print("\nSKIP: fastapi/uvicorn are not installed, so there is no dashboard to test.")
        print("      pip install fastapi \"uvicorn[standard]\" cryptography")
        return 0
    try:
        ds.DashboardServer()
    except Exception as e:
        print(f"\nSKIP: the dashboard app could not be built here ({e}).")
        print("      pip install python-multipart httpx")
        return 0

    try:
        test_key()
        test_throttle_unit()
        test_throttle_endpoints()
        test_qr_and_devices()
        test_comparison()
        test_reachability()
        test_lan_bind_failure()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("the suite ran to completion", False, repr(e))

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [n for n, ok, _d in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f" {passed}/{len(RESULTS)} passed in {time.time() - t0:.1f}s")
    if failed:
        print(" FAILED:")
        for n in failed:
            print(f"   • {n}")
    else:
        print(" The dashboard is closed by default, its key is worth guessing")
        print(" at only a handful of attempts a minute, and the switch that")
        print(" opens it to the network moves the real socket.")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
