"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Local HTTPS dashboard with expiring bearer sessions and authenticated command
payloads. The pinned CryptoJS bundle is served locally and integrity checked.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import stat
import secrets
import socket
import ssl
import string
import time
from pathlib import Path

from core.action_runtime import runtime as action_runtime
from core import app_index
from core import app_icons
from core import undo as undo_stack
from core import confirm as confirm_gate
from core import explorer as explorer_core
from actions import layout_manager as layout_core
from actions import media_control as media_control_core
from core.path_policy import (
    PathPolicyError,
    atomic_write_bytes,
    move_no_replace,
    resolve_user_path,
    validate_child_name,
)

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, Response
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 100
LOGIN_ATTEMPT_WINDOW = 300.0
MAX_LOGIN_ATTEMPTS = 10
AUTH_TOKEN_TTL = 12 * 60 * 60
# Window states and Spotify verbs the panels may request. Anything outside these
# sets is rejected before it reaches an action, so the browser cannot widen the
# action surface beyond what the voice layer already exposes.
_APP_LAUNCH_STATES = frozenset({
    "normal", "maximized", "fullscreen", "minimized",
    "left", "right", "top", "bottom",
})
_LAYOUT_PANEL_ACTIONS = frozenset({"list", "save", "apply", "delete"})
_SPOTIFY_PANEL_ACTIONS = frozenset({
    "status", "devices", "play", "pause", "next", "previous",
    "shuffle", "repeat", "seek", "volume", "search", "queue",
})
DEVICE_TOKEN_TTL = 30 * 24 * 60 * 60
MAX_REQUEST_BYTES = 2_000_000


class _RegistryNotReady(RuntimeError):
    """The assistant has not connected its action registry to the dashboard yet."""


class _RequestBodyTooLarge(ValueError):
    pass


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        Path.home() / ".mark" / "uploads",
    ]:
        try:
            safe = resolve_user_path(
                candidate, allow_missing=True, reject_symlinks=True
            )
            safe.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                safe.chmod(0o700)
            except OSError:
                pass
            return resolve_user_path(
                safe, allow_missing=False, reject_symlinks=True
            )
        except Exception:
            pass
    raise RuntimeError("Could not create a safe uploads folder in the user profile")


UPLOADS_DIR = _make_uploads_dir()

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """Derive the AES key from a high-entropy per-session secret."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _derive_mac_key(session_key: str) -> bytes:
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT + b'-MAC').digest()


def _decrypt_cbc(aes_key: bytes, mac_key: bytes, enc_b64: str) -> str:
    """Authenticate and decrypt base64(IV ‖ ciphertext ‖ HMAC-SHA256)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    if not isinstance(enc_b64, str) or len(enc_b64) > 2_000_000:
        raise ValueError("encrypted payload is too large")
    raw = base64.b64decode(enc_b64, validate=True)
    if len(raw) < 64 or len(raw[16:-32]) % 16:
        raise ValueError("invalid encrypted payload")
    signed, supplied_mac = raw[:-32], raw[-32:]
    expected_mac = hmac.new(mac_key, signed, hashlib.sha256).digest()
    if not hmac.compare_digest(supplied_mac, expected_mac):
        raise ValueError("encrypted payload authentication failed")
    iv, ct = signed[:16], signed[16:]
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"
_CRYPTOJS_SHA256 = "769a555de553babc35a3338f344dd7aa16260c93cea2c7db290707c90484e7cc"


def _crypto_js_valid() -> bool:
    try:
        digest = hashlib.sha256(_CRYPTOJS_FILE.read_bytes()).hexdigest()
        return hmac.compare_digest(digest, _CRYPTOJS_SHA256)
    except OSError:
        return False


def _ensure_crypto_js() -> None:
    if not _crypto_js_valid():
        print("[Dashboard] Bundled CryptoJS is missing or failed its integrity check.")
        print("[Dashboard] Restore dashboard/static/crypto-js.min.js before remote use.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _regular_no_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
        return (
            stat.S_ISREG(details.st_mode)
            and not path.is_symlink()
            and not (int(getattr(details, "st_file_attributes", 0)) & 0x400)
        )
    except OSError:
        return False


def _cert_pair_valid() -> bool:
    certs = BASE_DIR / "config" / "certs"
    key_path = certs / "jarvis.key"
    cert_path = certs / "jarvis.crt"
    if not _regular_no_reparse(key_path) or not _regular_no_reparse(cert_path):
        return False
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        return True
    except (OSError, ssl.SSLError):
        return False


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards. A False result is safe
    only for loopback HTTP; the caller must not expose an unencrypted dashboard
    to the LAN.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if certs.exists():
        try:
            cert_details = certs.lstat()
            if (
                certs.is_symlink()
                or int(getattr(cert_details, "st_file_attributes", 0)) & 0x400
                or not stat.S_ISDIR(cert_details.st_mode)
            ):
                print("[Dashboard] Certificate directory is not a trusted directory — loopback HTTP only.")
                return False
        except OSError:
            return False
    if _cert_pair_valid():
        try:
            os.chmod(key_p, 0o600)
        except OSError:
            pass
        return True
    if key_p.exists() or crt_p.exists():
        stamp = int(time.time())
        for candidate in (key_p, crt_p):
            if candidate.exists():
                try:
                    invalid = candidate.with_name(f"{candidate.name}.invalid-{stamp}")
                    os.replace(candidate, invalid)
                    if _regular_no_reparse(invalid):
                        os.chmod(invalid, 0o600)
                except OSError:
                    pass

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — loopback HTTP only.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        atomic_write_bytes(key_p, key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        atomic_write_bytes(crt_p, cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({type(e).__name__}) — loopback HTTP only.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _explorer_row(path: Path) -> dict:
    """Return a JSON-safe, read-only Explorer result row."""
    try:
        stat = path.stat()
        size = int(stat.st_size) if path.is_file() else 0
        modified = float(stat.st_mtime)
    except OSError:
        size = 0
        modified = None
    return {
        "name": path.name or str(path),
        "path": str(path),
        "extension": path.suffix,
        "is_file": path.is_file(),
        "size": size,
        "modified": modified,
    }


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: dict[str, float]    = {}   # auth_token → expiry timestamp
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache: dict[str, bytes] = {}   # session_key → AES bytes
        self._mac_cache: dict[str, bytes] = {}   # session_key → HMAC bytes
        self._clients: set[WebSocket]     = set()
        self._phone_clients: set[WebSocket] = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue(maxsize=100)
        self._wake_callback               = None
        self._connect_callback            = None
        self._capabilities_callback       = None
        self._desktop_callback            = None
        self._action_callback             = None
        self._pending_keys: dict[str, float] = {}
        self._login_attempts: dict[str, list[float]] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=64)
        self._background_tasks: set[asyncio.Task] = set()
        self._broadcast_lock              = asyncio.Lock()
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        expiry_secs = max(30, min(int(expiry_secs), 3_600))
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        while len(self._pending_keys) >= 100:
            oldest = min(self._pending_keys, key=self._pending_keys.get)
            self._pending_keys.pop(oldest, None)
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        while key in self._pending_keys:
            key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    def _login_identity(self, req) -> str:
        """Use the peer address for the short-lived PIN rate limit."""
        client = getattr(req, "client", None)
        host = getattr(client, "host", None)
        return str(host or "unknown")[:128]

    def _allow_login_attempt(self, identity: str) -> bool:
        now = time.time()
        cutoff = now - LOGIN_ATTEMPT_WINDOW
        self._login_attempts = {
            key: [stamp for stamp in values if stamp >= cutoff]
            for key, values in self._login_attempts.items()
            if values and values[-1] >= cutoff
        }
        # Once the peer-cardinality budget is exhausted, unknown addresses share
        # one strict overflow bucket rather than growing this map without bound.
        if identity not in self._login_attempts and len(self._login_attempts) >= 255:
            identity = "__overflow__"
        recent = self._login_attempts.get(identity, [])
        if len(recent) >= MAX_LOGIN_ATTEMPTS:
            return False
        recent.append(now)
        self._login_attempts[identity] = recent
        return True

    def _reset_login_attempts(self, identity: str) -> None:
        """Clear one identity's failed-attempt history after it authenticates.

        Only ever touches that identity's own bucket. The previous version
        fell back to clearing the shared "__overflow__" bucket whenever the
        caller's identity had no bucket of its own — which is the ordinary
        case for any client succeeding on its first try, not just one that
        had actually been folded into the overflow bucket. That let an
        unrelated, freshly-seen client wipe out the rate-limit history the
        overflow bucket was tracking for every other identity sharing it,
        once the 255-identity cardinality budget had been reached.
        """
        self._login_attempts.pop(identity, None)


    @staticmethod
    def _ssl_enabled() -> bool:
        return _cert_pair_valid()

    def get_url(self) -> str:
        if self._ssl_enabled():
            return f"https://{self._ip}:{PORT}"
        return f"http://127.0.0.1:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry; insecure fallback is local-only."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"127.0.0.1:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            if len(self._aes_cache) >= 1_000:
                self._aes_cache.clear()
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _mac_key(self, session_key: str) -> bytes:
        if session_key not in self._mac_cache:
            if len(self._mac_cache) >= 1_000:
                self._mac_cache.clear()
            self._mac_cache[session_key] = _derive_mac_key(session_key)
        return self._mac_cache[session_key]

    async def _close_other_sockets(self) -> None:
        """Drop live websockets after a revocation.

        A socket that was accepted with a token now revoked would otherwise
        keep receiving broadcasts for as long as it stayed connected.
        """
        for client_ws in list(self._clients) + list(self._phone_clients):
            try:
                await client_ws.close(code=1008)
            except Exception:
                pass
        self._clients.clear()
        self._phone_clients.clear()

    def _issue_token(self, session_key: str) -> str:
        now = time.time()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = now + AUTH_TOKEN_TTL
        self._token_keys[token] = session_key
        if len(self._tokens) > 1_000:
            expired = [value for value, expiry in self._tokens.items() if expiry <= now]
            for value in expired:
                self._tokens.pop(value, None)
                self._token_keys.pop(value, None)
            while len(self._tokens) > 1_000:
                oldest = min(self._tokens, key=self._tokens.get)
                self._tokens.pop(oldest, None)
                self._token_keys.pop(oldest, None)
        return token

    def revoke_all_sessions(self, *, keep: str = "") -> dict[str, int]:
        """Cut off every remote session, optionally sparing the caller's own.

        Clearing the remembered devices is not enough on its own: a phone that
        is already logged in holds a bearer token in memory, and that token
        stays valid until it expires on its own. Someone revoking access after
        losing a device would have been told the job was done while the lost
        device still had a live session.
        """
        devices = len(self._device_sessions)
        self._device_sessions.clear()
        doomed = [token for token in self._tokens if token != keep]
        for token in doomed:
            self._tokens.pop(token, None)
            self._token_keys.pop(token, None)
        return {"devices": devices, "sessions": len(doomed)}

    def _valid_token(self, token: str) -> bool:
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        expiry = self._tokens.get(token)
        if expiry is None or expiry <= time.time():
            self._tokens.pop(token, None)
            self._token_keys.pop(token, None)
            return False
        return True

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        if not self._valid_token(token):
            return None
        session_key = self._token_keys.get(token)
        if not session_key:
            return None
        try:
            return _decrypt_cbc(
                self._aes_key(session_key), self._mac_key(session_key), enc_b64
            )
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def _enqueue_command(self, text: str) -> bool:
        try:
            self._command_queue.put_nowait(text)
            return True
        except asyncio.QueueFull:
            return False

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    def set_capabilities_callback(self, fn) -> None:
        """Provide the same action registry used by voice commands."""
        self._capabilities_callback = fn

    def set_action_callback(self, fn) -> None:
        """Route panel buttons through the same action registry as voice commands."""
        self._action_callback = fn

    def set_desktop_callback(self, fn) -> None:
        """Provide a live window/monitor snapshot for the admin panel."""
        self._desktop_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    def _spawn(self, coroutine) -> None:
        """Run a small server task with a hard cardinality limit and cleanup."""
        if len(self._background_tasks) >= 100:
            coroutine.close()
            return
        try:
            task = asyncio.create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return
        self._background_tasks.add(task)

        def _finished(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            if not completed.cancelled():
                try:
                    completed.exception()
                except Exception:
                    pass

        task.add_done_callback(_finished)

    async def broadcast(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return
        try:
            encoded = json.dumps(msg, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return
        if len(encoded) > 50_000:
            msg = {
                "type": str(msg.get("type") or "sys")[:40],
                "text": str(msg.get("text") or msg.get("message") or "")[:20_000],
                "truncated": True,
            }
        else:
            # JSON round-tripping both copies nested values and guarantees the
            # history/WebSocket payload remains serializable after this call.
            msg = json.loads(encoded)
        async with self._broadcast_lock:
            self._history.append(msg)
            if len(self._history) > 300:
                self._history = self._history[-300:]
            clients = list(self._clients)

            async def _send(ws):
                try:
                    await asyncio.wait_for(ws.send_json(msg), timeout=2.0)
                    return None
                except Exception:
                    return ws

            dead = {
                result
                for result in await asyncio.gather(*(_send(ws) for ws in clients))
                if result is not None
            }
            self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        # The interactive docs were already disabled; the schema they are generated
        # from was not, so /openapi.json handed an unauthenticated caller on the
        # network the full list of routes and their parameters.
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.middleware("http")
        async def security_headers(req: Request, call_next):
            if req.url.path.startswith("/api/") and req.url.path not in {"/api/device-login"}:
                token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
                if not token or not self._valid_token(token):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
            length = req.headers.get("content-length")
            if req.method in {"POST", "PUT", "PATCH"} and length is None:
                return JSONResponse({"error": "Content-Length is required"}, status_code=411)
            if length:
                try:
                    limit = (MAX_UPLOAD_MB * 1024 * 1024 + 1_000_000
                             if req.url.path == "/api/upload" else MAX_REQUEST_BYTES)
                    parsed_length = int(length)
                    if parsed_length < 0:
                        return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
                    if parsed_length > limit:
                        return JSONResponse({"error": "Request too large"}, status_code=413)
                except ValueError:
                    return JSONResponse({"error": "Invalid Content-Length"}, status_code=400)
            response = await call_next(req)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), geolocation=()"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "connect-src 'self' ws: wss:; media-src 'self' blob:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            return response

        def action_registry_run(name: str, parameters: dict) -> str:
            """Execute one registered action, or explain that none is wired up."""
            if self._action_callback is None:
                raise _RegistryNotReady("the action registry is not connected yet")
            return str(self._action_callback(name, parameters))

        def _registry_unavailable() -> JSONResponse:
            """The assistant is not running yet — that is not a server fault.

            Answering 500 with a bare exception name told the user nothing and
            implied a defect. The dashboard can be reached while MARK LIV is
            still starting, and the honest answer is to say so.
            """
            return JSONResponse(
                {"ok": False, "error": "MARK LIV is not ready yet. Try again in a moment."},
                status_code=503,
            )

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and self._valid_token(tok)

        async def _read_json_body(req: Request):
            """Parse JSON without trusting a possibly false Content-Length."""
            payload = bytearray()
            async for chunk in req.stream():
                if len(payload) + len(chunk) > MAX_REQUEST_BYTES:
                    raise _RequestBodyTooLarge("request body exceeds the JSON limit")
                payload.extend(chunk)
            if not payload:
                raise ValueError("empty request body")
            return json.loads(payload.decode("utf-8"))

        # Serve only the pinned bundled copy; never execute an unverified download.
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _crypto_js_valid():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            return Response(
                "console.error('Dashboard crypto integrity check failed');",
                media_type="application/javascript",
                status_code=503,
            )

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            identity = self._login_identity(req)
            if not self._allow_login_attempt(identity):
                return JSONResponse(
                    {"ok": False, "error": "Too many login attempts. Try again later."},
                    status_code=429,
                    headers={"Retry-After": str(int(LOGIN_ATTEMPT_WINDOW))},
                )
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid login request"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid login request"}, status_code=400)
            raw_pin = body.get("pin")
            entered = raw_pin.strip().upper()[:32] if isinstance(raw_pin, str) else ""
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                self._reset_login_attempts(identity)
                del self._pending_keys[entered]          # one-time use
                session_key = secrets.token_urlsafe(32)
                tok = self._issue_token(session_key)
                self._aes_key(session_key)
                self._mac_key(session_key)
                if self._connect_callback:
                    self._connect_callback()
                self._spawn(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok, "key": session_key})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(req: Request, key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            identity = self._login_identity(req)
            if not self._allow_login_attempt(identity):
                return JSONResponse(
                    {"ok": False, "error": "Too many login attempts. Try again later."},
                    status_code=429,
                    headers={"Retry-After": str(int(LOGIN_ATTEMPT_WINDOW))},
                )
            now = time.time()
            if not key or len(key) > 32 or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            self._reset_login_attempts(identity)
            del self._pending_keys[key]
            session_key = secrets.token_urlsafe(32)
            tok = self._issue_token(session_key)
            dev_tok = secrets.token_urlsafe(32)
            self._aes_key(session_key)
            self._mac_key(session_key)
            self._device_sessions[dev_tok] = {
                "session_key": session_key,
                "expires_at": time.time() + DEVICE_TOKEN_TTL,
            }
            if len(self._device_sessions) > 100:
                now = time.time()
                self._device_sessions = {
                    token: session for token, session in self._device_sessions.items()
                    if session.get("expires_at", 0) > now
                }
                while len(self._device_sessions) > 100:
                    oldest = min(
                        self._device_sessions,
                        key=lambda token: self._device_sessions[token].get("expires_at", 0),
                    )
                    self._device_sessions.pop(oldest, None)

            if self._connect_callback:
                self._connect_callback()
            self._spawn(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{session_key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            identity = self._login_identity(req)
            if not self._allow_login_attempt(identity):
                return JSONResponse({"ok": False}, status_code=429)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False}, status_code=400)
            raw_device_token = body.get("device_token")
            dev_tok = raw_device_token.strip() if isinstance(raw_device_token, str) else ""
            if not dev_tok or len(dev_tok) > 256:
                return JSONResponse({"ok": False}, status_code=401)
            session = self._device_sessions.get(dev_tok)
            if not session or session.get("expires_at", 0) <= time.time():
                self._device_sessions.pop(dev_tok, None)
                return JSONResponse({"ok": False}, status_code=401)
            self._reset_login_attempts(identity)
            session_key = session["session_key"]
            session["expires_at"] = time.time() + DEVICE_TOKEN_TTL
            tok = self._issue_token(session_key)
            self._aes_key(session_key)
            self._mac_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            self._spawn(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Sign out every remote device, including live sessions.

            The session making the request keeps working; everything else —
            remembered devices and any bearer token already handed out — stops
            immediately.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            caller = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            removed = self.revoke_all_sessions(keep=caller)
            self._spawn(self._close_other_sockets())
            return JSONResponse({
                "ok": True,
                "revoked": removed["devices"],
                "sessions_closed": removed["sessions"],
            })

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc = body.get("enc", "")
            if enc and not isinstance(enc, str):
                return JSONResponse({"error": "Invalid encrypted payload"}, status_code=400)
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                raw_text = body.get("text")
                text = raw_text.strip() if isinstance(raw_text, str) else ""
            if text and len(text) > 20_000:
                return JSONResponse({"error": "Command is too long"}, status_code=413)
            if text:
                if not self._enqueue_command(text):
                    return JSONResponse({"error": "Command queue is busy"}, status_code=429)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        @app.get("/api/capabilities")
        async def capabilities(req: Request):
            """Return the live action registry for the authenticated admin panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                value = self._capabilities_callback() if self._capabilities_callback else []
                return JSONResponse({"ok": True, "actions": value})
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "error": f"Capability snapshot failed ({type(exc).__name__})."},
                    status_code=500,
                )

        @app.get("/api/desktop")
        async def desktop_snapshot(req: Request):
            """Return current windows and monitors without executing anything."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                value = self._desktop_callback() if self._desktop_callback else {}
                return JSONResponse({"ok": True, **value})
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "error": f"Desktop snapshot failed ({type(exc).__name__})."},
                    status_code=500,
                )

        @app.get("/api/explorer/search")
        async def explorer_search(
            req: Request,
            query: str = "",
            location: str = "home",
            extension: str = "",
            limit: int = 20,
        ):
            """Search known folders without modifying files."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            query = str(query or "").strip()[:240]
            extension = str(extension or "").strip()[:32]
            if not query and not extension:
                return JSONResponse({"ok": False, "error": "Enter a filename or extension."}, status_code=400)
            try:
                matches = await asyncio.to_thread(
                    explorer_core.search,
                    query,
                    root=str(location or "home")[:260],
                    extension=extension,
                    limit=max(1, min(int(limit), 50)),
                )
                return JSONResponse({
                    "ok": True,
                    "query": query,
                    "location": str(location or "home"),
                    "extension": extension,
                    "matches": [_explorer_row(path) for path in matches],
                })
            except PathPolicyError:
                return JSONResponse({"ok": False, "error": "Search location is not allowed."}, status_code=403)
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "Search parameters are invalid."}, status_code=400)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Explorer search failed: {type(exc).__name__}"}, status_code=500)

        @app.post("/api/explorer/open")
        async def explorer_open(req: Request):
            """Open or reveal one already-resolved filesystem path."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            raw_value = body.get("path")
            raw_path = raw_value.strip() if isinstance(raw_value, str) else ""
            if not raw_path or len(raw_path) > 400:
                return JSONResponse({"ok": False, "error": "A valid path is required."}, status_code=400)
            try:
                target = resolve_user_path(raw_path, allow_missing=False)
            except FileNotFoundError:
                return JSONResponse({"ok": False, "error": "Path not found."}, status_code=404)
            except (PathPolicyError, OSError, ValueError) as exc:
                return JSONResponse({"ok": False, "error": f"Path denied: {type(exc).__name__}"}, status_code=403)
            try:
                raw_select = body.get("select", False)
                if not isinstance(raw_select, bool):
                    return JSONResponse({"ok": False, "error": "Select must be true or false."}, status_code=400)
                select = raw_select
                message = await asyncio.to_thread(
                    explorer_core.open_in_explorer, target, select=select
                )
                return JSONResponse({"ok": True, "path": str(target), "select": select, "message": message})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Could not open Explorer: {type(exc).__name__}"}, status_code=500)

        @app.get("/api/apps")
        async def app_list(req: Request, query: str = "", refresh: str = "", limit: int = 40):
            """List indexed installed applications, or rank them against a query.

            Read-only: building the index only reads registry keys, shortcut
            folders, and desktop entries. Nothing is launched here.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            query = str(query or "").strip()[:160]
            want_refresh = str(refresh or "").strip().casefold() in {"1", "true", "yes"}
            try:
                entries = await asyncio.to_thread(app_index.load_index, refresh=want_refresh)
                if query:
                    entries = await asyncio.to_thread(
                        app_index.resolve, query, limit=max(1, min(int(limit), 50)), entries=entries
                    )
                def _row(entry) -> dict:
                    return {
                        "name": entry.name,
                        "kind": entry.kind,
                        "source": entry.source,
                        "icon": app_icons.has_icon(entry),
                    }

                rows = [_row(entry) for entry in entries[: max(1, min(int(limit), 200))]]
                quick = await asyncio.to_thread(app_index.quick_list)
                return JSONResponse({
                    "ok": True,
                    "query": query,
                    "count": len(rows),
                    "apps": rows,
                    "pinned": [_row(entry) for entry in quick["pinned"]],
                    "recent": [_row(entry) for entry in quick["recent"]],
                })
            except (TypeError, ValueError):
                return JSONResponse({"ok": False, "error": "Invalid parameters."}, status_code=400)
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "error": f"Application index failed: {type(exc).__name__}"},
                    status_code=500,
                )

        @app.post("/api/apps/launch")
        async def app_launch(req: Request):
            """Launch an indexed application through the normal action pipeline.

            The panel sends a name, never a path or command line, so the
            dashboard cannot be used to execute arbitrary binaries.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

            name = body.get("name")
            name = name.strip() if isinstance(name, str) else ""
            if not name or len(name) > 160 or any(ord(char) < 32 for char in name):
                return JSONResponse({"ok": False, "error": "A valid application name is required."},
                                    status_code=400)

            parameters: dict = {"app_name": name}
            foreground = body.get("foreground", True)
            if not isinstance(foreground, bool):
                return JSONResponse({"ok": False, "error": "foreground must be true or false."},
                                    status_code=400)
            parameters["foreground"] = foreground

            monitor = body.get("monitor")
            if monitor not in (None, ""):
                try:
                    monitor_index = int(monitor)
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": "monitor must be a number."},
                                        status_code=400)
                if not 1 <= monitor_index <= 32:
                    return JSONResponse({"ok": False, "error": "monitor must be between 1 and 32."},
                                        status_code=400)
                # open_app's schema accepts semantic monitor names as well as
                # numbers, so its "monitor" parameter is a string.
                parameters["monitor"] = str(monitor_index)

            state = body.get("state")
            if state not in (None, ""):
                state = str(state).casefold().strip()[:16]
                if state not in _APP_LAUNCH_STATES:
                    return JSONResponse({"ok": False, "error": "Unsupported window state."},
                                        status_code=400)
                parameters["state"] = state

            try:
                result = await asyncio.to_thread(
                    action_registry_run, "open_app", parameters
                )
            except _RegistryNotReady:
                return _registry_unavailable()
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Launch failed: {type(exc).__name__}"},
                                    status_code=500)
            await self.broadcast({"type": "sys", "text": result})
            return JSONResponse({"ok": True, "result": result})

        @app.post("/api/apps/pin")
        async def app_pin(req: Request):
            """Pin or unpin an application in the launcher's quick row."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            name = body.get("name")
            name = name.strip() if isinstance(name, str) else ""
            pinned = body.get("pinned")
            if not name or len(name) > 160 or not isinstance(pinned, bool):
                return JSONResponse({"ok": False, "error": "A name and a pinned flag are required."},
                                    status_code=400)
            try:
                pins = await asyncio.to_thread(app_index.set_pinned, name, pinned)
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Pin failed: {type(exc).__name__}"},
                                    status_code=500)
            return JSONResponse({"ok": True, "pinned": pins})

        @app.get("/api/apps/icon")
        async def app_icon(req: Request, name: str = ""):
            """Serve a cached application icon as PNG.

            The lookup goes through the index by name, so the browser cannot ask
            for an arbitrary file: only an indexed application resolves.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            name = str(name or "").strip()[:160]
            if not name:
                return JSONResponse({"ok": False, "error": "A name is required."}, status_code=400)
            try:
                matches = await asyncio.to_thread(app_index.resolve, name, limit=1)
                if not matches:
                    return JSONResponse({"ok": False, "error": "Unknown application."}, status_code=404)
                data = await asyncio.to_thread(app_icons.icon_png, matches[0])
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Icon failed: {type(exc).__name__}"},
                                    status_code=500)
            if not data:
                return JSONResponse({"ok": False, "error": "No icon available."}, status_code=404)
            return Response(
                content=data,
                media_type="image/png",
                headers={"Cache-Control": "private, max-age=86400"},
            )

        @app.get("/api/layouts")
        async def layout_list(req: Request):
            """Saved window layouts, as a plain list for the panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                data = await asyncio.to_thread(layout_core._read)
                layouts = [
                    {
                        "name": name,
                        "windows": len(layout.get("windows", [])),
                        "saved_at": str(layout.get("saved_at", ""))[:19],
                    }
                    for name, layout in sorted((data.get("layouts") or {}).items())
                ]
                return JSONResponse({"ok": True, "layouts": layouts})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Layouts unavailable: {type(exc).__name__}"},
                                    status_code=500)

        @app.post("/api/layouts")
        async def layout_command(req: Request):
            """Save, apply, or delete a window layout through the action registry."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            action = body.get("action")
            action = action.strip().casefold() if isinstance(action, str) else ""
            if action not in _LAYOUT_PANEL_ACTIONS:
                return JSONResponse({"ok": False, "error": "Unsupported layout action."},
                                    status_code=400)
            name = body.get("name")
            name = name.strip() if isinstance(name, str) else ""
            if action != "list" and (not name or len(name) > 40):
                return JSONResponse({"ok": False, "error": "A layout name is required."},
                                    status_code=400)
            parameters = {"action": action}
            if name:
                parameters["name"] = name
            try:
                result = await asyncio.to_thread(action_registry_run, "layout_manager", parameters)
            except _RegistryNotReady:
                return _registry_unavailable()
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Layout command failed: {type(exc).__name__}"},
                                    status_code=500)
            await self.broadcast({"type": "sys", "text": result})
            return JSONResponse({"ok": True, "result": result})

        @app.get("/api/spotify")
        async def spotify_status(req: Request):
            """Current Spotify playback state for the media panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                snapshot = await asyncio.to_thread(media_control_core.playback_snapshot)
                return JSONResponse({"ok": True, **snapshot})
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "error": f"Spotify status failed: {type(exc).__name__}"},
                    status_code=500,
                )

        @app.post("/api/spotify")
        async def spotify_command(req: Request):
            """Run one Spotify transport command from the media panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

            action = body.get("action")
            action = action.strip().casefold() if isinstance(action, str) else ""
            if action not in _SPOTIFY_PANEL_ACTIONS:
                return JSONResponse({"ok": False, "error": "Unsupported Spotify action."},
                                    status_code=400)

            parameters: dict = {"action": action}
            for key in ("query", "uri", "device", "mode"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    parameters[key] = value.strip()[:500]
            value = body.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not 0 <= float(value) <= 86_400:
                    return JSONResponse({"ok": False, "error": "value is out of range."},
                                        status_code=400)
                parameters["value"] = value
            if isinstance(body.get("enabled"), bool):
                parameters["enabled"] = body["enabled"]

            try:
                result = await asyncio.to_thread(
                    action_registry_run, "media_control", parameters
                )
            except _RegistryNotReady:
                return _registry_unavailable()
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"Spotify command failed: {type(exc).__name__}"},
                                    status_code=500)
            return JSONResponse({"ok": True, "result": result})

        @app.get("/api/actions")
        async def action_runs(req: Request):
            """Live action IDs/status for the admin panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"ok": True, "actions": action_runtime.snapshots()})

        @app.post("/api/actions/{action_id}/cancel")
        async def cancel_action(action_id: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            cancelled = action_runtime.cancel(action_id)
            if cancelled:
                # If this run is waiting on the shared confirmation banner,
                # cancelling its dashboard card must also dismiss that banner.
                confirm_gate.resolve(False, key=action_id)
            return JSONResponse({"ok": cancelled, "action_id": action_id},
                                status_code=200 if cancelled else 404)

        @app.get("/api/undo")
        async def undo_history(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"ok": True, "history": undo_stack.history()})

        @app.post("/api/undo")
        async def undo_action(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            result = await asyncio.to_thread(undo_stack.undo_last)
            await self.broadcast({"type": "sys", "text": result})
            return JSONResponse({"ok": True, "result": result, "history": undo_stack.history()})

        @app.post("/api/admin-command")
        async def admin_command(req: Request):
            """Queue a command from the admin panel through the normal dispatcher."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await _read_json_body(req)
            except _RequestBodyTooLarge:
                return JSONResponse({"ok": False, "error": "Request too large"}, status_code=413)
            except Exception:
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc = body.get("enc", "")
            if enc and not isinstance(enc, str):
                return JSONResponse({"ok": False, "error": "Invalid encrypted payload"}, status_code=400)
            if enc:
                text = self._decrypt(token, enc) or ""
            else:
                raw_text = body.get("text")
                text = raw_text.strip() if isinstance(raw_text, str) else ""
            if len(text) > 20_000:
                return JSONResponse({"ok": False, "error": "Command is too long"}, status_code=413)
            if not text:
                return JSONResponse({"ok": False, "error": "Command is empty or could not be decrypted"}, status_code=400)
            if not self._enqueue_command(text):
                return JSONResponse({"ok": False, "error": "Command queue is busy"}, status_code=429)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket):
            protocols = [
                value.strip() for value in
                websocket.headers.get("sec-websocket-protocol", "").split(",")
            ]
            tok = protocols[1] if len(protocols) == 2 and protocols[0] == "mark-auth" else ""
            if not tok or not self._valid_token(tok):
                await websocket.close(code=4001)
                return
            if len(self._phone_clients) >= 4:
                await websocket.close(code=1013)
                return
            await websocket.accept(subprotocol="mark-auth")
            self._phone_clients.add(websocket)
            self._spawn(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    if not self._valid_token(tok):
                        await websocket.close(code=4001)
                        break
                    if len(data) > 256_000:
                        await websocket.close(code=1009)
                        break
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                self._phone_clients.discard(websocket)
                self._spawn(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            raw_text = str(raw)
            if len(raw_text) > 1_024:
                raise ValueError("filename is too long")
            name = Path(raw_text).name                    # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        def _safe_upload_path(raw: str) -> tuple[str, Path]:
            safe = validate_child_name(_safe_filename(raw))
            root = resolve_user_path(self._uploads_dir, allow_missing=False, reject_symlinks=True)
            path = resolve_user_path(
                root / safe,
                allowed_roots=(root,),
                allow_missing=True,
                reject_symlinks=True,
            )
            if path.parent != root:
                raise ValueError("invalid upload path")
            return safe, path

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                try:
                    safe, dest = _safe_upload_path(file.filename or "upload")
                except ValueError:
                    return JSONResponse({"error": "Invalid filename"}, status_code=400)
                stem, suffix = Path(safe).stem, Path(safe).suffix
                staging = self._uploads_dir / f".upload-{secrets.token_hex(12)}.tmp"
                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with staging.open("xb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                raise OverflowError("upload exceeds the configured limit")
                            fout.write(chunk)
                        fout.flush()
                        os.fsync(fout.fileno())

                    # Publish only after the complete file is durable. A racing
                    # upload or local file is never overwritten, and no consumer
                    # can observe a partially received destination.
                    counter = 1
                    while True:
                        try:
                            move_no_replace(staging, dest)
                            break
                        except FileExistsError:
                            if counter > 10_000:
                                raise FileExistsError("could not allocate an upload filename")
                            safe, dest = _safe_upload_path(f"{stem}_{counter}{suffix}")
                            counter += 1
                except OverflowError:
                    staging.unlink(missing_ok=True)
                    return JSONResponse(
                        {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                        status_code=413,
                    )
                except Exception as exc:
                    staging.unlink(missing_ok=True)
                    return JSONResponse(
                        {"error": f"Upload failed ({type(exc).__name__})."},
                        status_code=500,
                    )

                self._spawn(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                candidates = []
                for scanned, path in enumerate(self._uploads_dir.iterdir(), 1):
                    if scanned > 2_000:
                        break
                    if path.name.startswith(".upload-") or not _regular_no_reparse(path):
                        continue
                    try:
                        file_details = path.stat()
                    except OSError:
                        continue
                    candidates.append(
                        (float(file_details.st_mtime), path.name, int(file_details.st_size))
                    )
                for _modified, name, size in sorted(candidates, reverse=True)[:500]:
                    files.append({"name": name, "size": size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                safe, path = _safe_upload_path(filename)
            except ValueError:
                return JSONResponse({"error": "Not found"}, status_code=404)
            root = self._uploads_dir.resolve()
            if path.parent != root or not _regular_no_reparse(path):
                return JSONResponse({"error": "Not found"}, status_code=404)
            descriptor = None
            try:
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or int(getattr(info, "st_file_attributes", 0)) & 0x400
                ):
                    raise OSError("not a regular file")
                handle = os.fdopen(descriptor, "rb")
                descriptor = None
            except OSError:
                if descriptor is not None:
                    os.close(descriptor)
                return JSONResponse({"error": "Not found"}, status_code=404)

            def chunks():
                try:
                    while True:
                        chunk = handle.read(64 * 1024)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    handle.close()

            return StreamingResponse(
                chunks(),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": f'attachment; filename="{safe}"',
                    "Content-Length": str(info.st_size),
                },
            )

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket):
            protocols = [
                value.strip() for value in
                websocket.headers.get("sec-websocket-protocol", "").split(",")
            ]
            tok = protocols[1] if len(protocols) == 2 and protocols[0] == "mark-auth" else ""
            if not tok or not self._valid_token(tok):
                await websocket.close(code=4001)
                return
            if len(self._clients) >= 20:
                await websocket.close(code=1013)
                return
            await websocket.accept(subprotocol="mark-auth")
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if not self._valid_token(tok):
                        await websocket.close(code=4001)
                        break
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        if enc and not isinstance(enc, str):
                            continue
                        if enc:
                            text = self._decrypt(tok, enc)
                        else:
                            raw_text = data.get("text")
                            text = raw_text.strip() if isinstance(raw_text, str) else ""
                        if text and len(text) <= 20_000 and self._enqueue_command(text):
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
            ws_max_size=300_000, limit_concurrency=100, timeout_keep_alive=10,
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl = self._ssl_enabled()
        # LAN exposure is allowed only with TLS. If cryptography/certificate
        # setup is unavailable, keep the dashboard reachable from this machine
        # without exposing bearer tokens over the local network.
        if use_ssl:
            print("[Dashboard] If another device cannot connect, allow the dashboard ports in your firewall settings.")
        else:
            print("[Dashboard] TLS unavailable — dashboard restricted to this computer.")
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        alias_task = (
            asyncio.create_task(self._serve_alias(), name="dashboard-alias")
            if use_ssl else None
        )

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0" if use_ssl else "127.0.0.1", port=PORT,
            log_level="warning", ws_max_size=300_000, limit_concurrency=100,
            timeout_keep_alive=10,
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        print(f"[Dashboard] {self.get_url()}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        try:
            await uvicorn.Server(cfg).serve()
        finally:
            if alias_task is not None:
                alias_task.cancel()
                try:
                    await alias_task
                except asyncio.CancelledError:
                    pass
            pending = list(self._background_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            self._background_tasks.clear()
