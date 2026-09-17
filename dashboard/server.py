"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Reachability is opt-in. By default the server binds 127.0.0.1 — only this PC can
open the dashboard at all, and no firewall rule is requested. Enabling LAN access
(the Remote Access overlay, or config) rebinds every interface live and then asks
the OS firewall for a rule. Pairing keys are one-time, KEY_LEN characters, and
every unauthenticated login route is rate limited per client address.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import hmac
import math
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency.
# Imported for real, not just for its FastAPI wrappers: those import fine without
# it, and FastAPI only notices at ROUTE-DEFINITION time — where it raises and
# takes the entire dashboard down instead of just the upload endpoint.
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    import multipart  # noqa: F401
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# Length of the one-time pairing key. Six characters of this 31-letter alphabet
# was ~887 M candidates offered to three endpoints that accepted unlimited
# guesses — enough for a script on the same Wi-Fi to chew through a meaningful
# fraction of it in an evening. Eight is ~852 bn, and together with the throttle
# below a guess costs seconds instead of microseconds, which is what actually
# decides whether brute force is a hobby or a non-starter. The login page is
# generated with this number, so the two cannot drift apart.
KEY_LEN = 8

# ── Login throttling ─────────────────────────────────────────────────────────
# A longer key only raises the cost of one guess; this is what bounds how many
# guesses anybody gets. Counted per client address, and shared by the routes that
# hand out the guessable secret (typed key, QR link) so hopping between them
# does not reset anything.
THROTTLE_WINDOW   = 60.0    # how long a failed attempt counts against you
THROTTLE_MAX      = 5       # misses allowed per window before a lockout
THROTTLE_BASE     = 30.0    # first lockout, in seconds
THROTTLE_CEILING  = 900.0   # and the most it ever grows to (15 min)

# The persistent-device route gets its own, far more generous budget: its secret
# is 256 bits of secrets.token_urlsafe (brute force is hopeless), and a phone
# that reloads the login page after a server restart must not lock the owner out
# of typing a real key. The limit here is anti-spam, not anti-guessing.
DEVICE_THROTTLE_MAX = 20

# ── Reachability ─────────────────────────────────────────────────────────────
BIND_LOCAL = "127.0.0.1"    # default: this PC only, no firewall rule needed
BIND_LAN   = "0.0.0.0"      # opt-in: every interface, firewall rule requested

# How long a socket gets to wind down before it is forced. uvicorn's own default
# is "wait forever", and the dashboard's WebSocket handler only ends when the
# phone hangs up — so without a bound, one connected phone would be able to
# block a rebind (or a shutdown) indefinitely.
GRACEFUL_SHUTDOWN = 3


def _too_many(wait: float) -> JSONResponse:
    """429 + Retry-After: the standard 'come back later', which the login page
    reads to tell the user how long the wait actually is."""
    secs = max(1, int(math.ceil(wait)))
    return JSONResponse(
        {"ok": False, "error": f"Too many attempts — try again in {secs}s",
         "retry_after": secs},
        status_code=429, headers={"Retry-After": str(secs)},
    )


def _invalid(wait: float) -> JSONResponse:
    """A wrong key. The wording is the same whether the key never existed or has
    expired, so the route does not tell a guesser which of the two it hit — and
    when this miss triggered a lockout, that is what gets reported instead."""
    if wait > 0:
        return _too_many(wait)
    return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                        status_code=401)


def _pair_page(title: str, msg: str, status: int = 200,
               retry_after: float = 0.0) -> HTMLResponse:
    """The QR route answers a browser, so a refused attempt has to be a page.
    Same wording rules as _invalid(): nothing about how close the guess was."""
    secs = max(1, int(math.ceil(retry_after))) if retry_after > 0 else 0
    colour = "#f87171" if status >= 400 else "#dde3ed"
    head = {"Retry-After": str(secs)} if secs else {}
    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  h2{{color:{colour};margin-bottom:12px}}p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body><div><h2>{title}</h2>
<p>{msg}</p>
</div></body></html>""", status_code=status, headers=head)


async def _run_uvicorn(server) -> BaseException | None:
    """Await a uvicorn server without letting its failure kill the event loop.

    uvicorn answers a port it cannot bind with sys.exit(STARTUP_FAILURE). Inside
    a task that is fatal well beyond the dashboard: asyncio deliberately
    re-raises SystemExit out of Task.__step__, so it escapes run_forever() and
    takes the whole loop with it — and this app's loop is the assistant (audio,
    session, tools). One busy port 8000 used to be enough to leave the UI up and
    everything behind it dead, with no traceback in the log.

    Caught here it becomes a return value, so the caller can report it, fall back
    to loopback, and carry on.
    """
    try:
        await server.serve()
        return None
    except SystemExit as e:                 # "I could not bind"
        return e
    except asyncio.CancelledError:
        raise                               # a rebind or shutdown — not an error
    except BaseException as e:              # anything else uvicorn manages to raise
        return e


def _lan_enabled_from_config() -> bool:
    """The owner's LAN-access choice, read at startup.

    Off when the config cannot be read: a dashboard that failed *open* would be
    reachable from the whole network because of a corrupt file, which is exactly
    the accident an opt-in default exists to prevent.
    """
    try:
        from memory.config_manager import get_dashboard_lan_enabled
        return bool(get_dashboard_lan_enabled())
    except Exception:
        return False


def _client_ip(req) -> str:
    """The address attempts are counted against.

    Always the peer address, never X-Forwarded-For: nothing proxies this server,
    and a header the client controls would let an attacker pick a fresh bucket
    per request — the throttle would be decoration.
    """
    try:
        return req.client.host if req.client else "unknown"
    except Exception:
        return "unknown"


class _LoginThrottle:
    """Per-address attempt limiting with a lockout that doubles each time.

    Per address rather than global on purpose: a phone fumbling the key must not
    lock out the rest of the household, and — the more important direction — an
    attacker must not be able to lock the OWNER out by spamming wrong keys,
    which one shared counter would hand them for free.

    Used from the event-loop thread only (every caller is a coroutine and nothing
    here awaits), so it needs no lock.
    """

    def __init__(self, window: float = THROTTLE_WINDOW, max_attempts: int = THROTTLE_MAX,
                 base: float = THROTTLE_BASE, ceiling: float = THROTTLE_CEILING):
        self._window  = float(window)
        self._max     = int(max_attempts)
        self._base    = float(base)
        self._ceiling = float(ceiling)
        self._misses:  dict[str, list]  = {}   # address → timestamps of misses
        self._strikes: dict[str, int]   = {}   # address → consecutive lockouts
        self._until:   dict[str, float] = {}   # address → lockout expiry
        self._seen:    dict[str, float] = {}   # address → last time it tried

    def retry_after(self, addr: str) -> float:
        """Seconds this address still has to wait; 0 means it may try."""
        until = self._until.get(addr)
        if until is None:
            return 0.0
        # Still knocking while locked out counts as activity, so the escalating
        # strike count is not garbage-collected underneath an ongoing attack.
        self._seen[addr] = time.time()
        left = until - time.time()
        if left <= 0:
            self._until.pop(addr, None)
            return 0.0
        return left

    def fail(self, addr: str) -> float:
        """Record a wrong secret. Returns the lockout it triggered (0 = none)."""
        now   = time.time()
        self._seen[addr] = now
        hits  = [t for t in self._misses.get(addr, ()) if now - t < self._window]
        hits.append(now)
        if len(hits) < self._max:
            self._misses[addr] = hits
            return 0.0
        strikes = self._strikes.get(addr, 0) + 1
        self._strikes[addr] = strikes
        wait = min(self._base * (2 ** (strikes - 1)), self._ceiling)
        self._until[addr]  = now + wait
        self._misses[addr] = []        # the lockout replaces the window
        return wait

    def success(self, addr: str) -> None:
        """A correct secret clears that address's slate entirely."""
        self._misses.pop(addr, None)
        self._strikes.pop(addr, None)
        self._until.pop(addr, None)
        self._seen.pop(addr, None)

    def prune(self) -> None:
        """Forget addresses that have gone quiet — these dicts live as long as
        the app does, and a dashboard on a busy network would otherwise collect
        one entry per visitor forever.

        Two different ages on purpose. A miss only counts inside its window, so
        those go early. A strike count is kept for as long as any lockout could
        possibly last: dropping it early would let an attacker reset the doubling
        simply by waiting out one lockout — or by the owner pressing NEW KEY,
        which is when this runs. Called from new_key(), never from a timer.
        """
        now      = time.time()
        quiet_by = now - max(self._window, self._ceiling)
        for addr in [a for a, ts in self._misses.items()
                     if not any(now - t < self._window for t in ts)]:
            self._misses.pop(addr, None)
        for addr in [a for a, seen in self._seen.items() if seen < quiet_by]:
            self._seen.pop(addr, None)
            self._misses.pop(addr, None)
            self._strikes.pop(addr, None)
            self._until.pop(addr, None)

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


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


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
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

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        # Attempt limiting. One budget for the guessable pairing key, shared by
        # /login and /auto-login; a looser one for persistent device tokens (see
        # DEVICE_THROTTLE_MAX for why they are not the same).
        self._key_throttle    = _LoginThrottle()
        self._device_throttle = _LoginThrottle(max_attempts=DEVICE_THROTTLE_MAX)
        # Reachability. LAN access is opt-in and the socket is rebound live when
        # the owner changes their mind — see serve().
        self._loop            = None
        self._server          = None
        self._lan             = False
        self._rebind_event    = asyncio.Event()
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        self._key_throttle.prune()        # the owner is here, so tidy the counters
        self._device_throttle.prune()
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(KEY_LEN))
        self._pending_keys[key] = now + expiry_secs
        return key

    # ── secret comparison ───────────────────────────────────────────────────
    #
    # Bearer tokens stay in a set: they are 256 bits from secrets.token_urlsafe,
    # a set lookup compares hashes rather than the secret, and _auth() runs on
    # every single request. The two below are the comparisons an unauthenticated
    # stranger drives, over secrets a human can type — so they scan with
    # hmac.compare_digest, which costs the same whether the guess shares a prefix
    # with a real key or not. There are only ever a handful of live entries, so
    # the scan is free.

    def _match_pending_key(self, entered: str) -> str | None:
        """The live one-time key equal to `entered`, or None (expired included)."""
        now = time.time()
        for k, exp in list(self._pending_keys.items()):
            if exp > now and hmac.compare_digest(k, entered):
                return k
        return None

    def _match_device(self, dev_tok: str) -> str | None:
        """The stored device token equal to `dev_tok`, or None."""
        for tok in list(self._device_sessions):
            if hmac.compare_digest(tok, dev_tok):
                return tok
        return None

    # ── reachability ────────────────────────────────────────────────────────

    def lan_enabled(self) -> bool:
        """True when the dashboard is listening on every interface (not just this PC)."""
        return self._lan

    def set_lan_access(self, enabled: bool) -> None:
        """Turn LAN reachability on or off, live. Safe from any thread — the UI
        calls it from Qt while the server runs on the asyncio loop."""
        enabled = bool(enabled)
        loop = self._loop
        if loop is None or loop.is_closed():
            self._lan = enabled            # serve() has not started: it will read this
            return
        try:
            loop.call_soon_threadsafe(self._request_rebind, enabled)
        except RuntimeError:
            self._lan = enabled

    def _request_rebind(self, enabled: bool) -> None:
        """Runs on the loop thread: flag the change and wake the serve loop."""
        if enabled == self._lan:
            return
        self._lan = enabled
        self._rebind_event.set()

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            # The page adapts to KEY_LEN instead of hardcoding a length of its
            # own, so the input cannot reject the key the server just issued.
            return HTMLResponse(self._login_html.replace("__KEY_LEN__", str(KEY_LEN)))

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
            ip   = _client_ip(req)
            wait = self._key_throttle.retry_after(ip)
            if wait > 0:
                return _too_many(wait)
            try:
                body = await req.json()
            except Exception:
                body = {}                  # a malformed body is just another miss
            entered = str(body.get("pin", "")).strip().upper()
            matched = self._match_pending_key(entered) if entered else None
            if matched is None:
                return _invalid(self._key_throttle.fail(ip))
            del self._pending_keys[matched]              # one-time use
            self._key_throttle.success(ip)
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = matched              # the stored spelling
            self._aes_key(matched)                       # pre-derive & cache
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established."}
            ))
            # Bearer token in response body — no cookies needed (works on any browser/HTTP)
            return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(req: Request, key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            ip   = _client_ip(req)
            wait = self._key_throttle.retry_after(ip)
            if wait > 0:
                return _pair_page("Too Many Attempts",
                                  f"Wait {int(math.ceil(wait))}s, then press "
                                  "<strong style='color:#dde3ed'>Remote Control</strong> "
                                  "in JARVIS for a new QR code.",
                                  status=429, retry_after=wait)
            matched = self._match_pending_key(key.strip().upper()) if key else None
            if matched is None:
                wait = self._key_throttle.fail(ip)
                return _pair_page("Link Expired",
                                  "Press <strong style='color:#dde3ed'>Remote Control</strong> "
                                  "in JARVIS to get a new QR code.",
                                  status=429 if wait > 0 else 401, retry_after=wait)
            del self._pending_keys[matched]
            self._key_throttle.success(ip)
            key     = matched        # the stored spelling; the rest of this
            tok     = secrets.token_urlsafe(32)   # handler hands it to the phone
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
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
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            ip   = _client_ip(req)
            wait = self._device_throttle.retry_after(ip)
            if wait > 0:
                return _too_many(wait)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            matched = self._match_device(dev_tok) if dev_tok else None
            if matched is None:
                wait = self._device_throttle.fail(ip)
                return _too_many(wait) if wait > 0 else JSONResponse({"ok": False},
                                                                     status_code=401)
            self._device_throttle.success(ip)
            session_key = self._device_sessions[matched]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
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

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
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
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    # One malformed frame must not cost the socket. Anything on
                    # the LAN that found this port can send a JSON array, a bare
                    # string or invalid JSON — receive_json() raises on the last
                    # and data.get() raises AttributeError on the first two, and
                    # since only WebSocketDisconnect was caught, that exception
                    # ended the receive loop: the phone stayed "connected" but
                    # never controlled anything again until it was reloaded.
                    try:
                        data = await websocket.receive_json()
                    except WebSocketDisconnect:
                        raise
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    def _bind_host(self) -> str:
        return BIND_LAN if self._lan else BIND_LOCAL

    def _shown_host(self) -> str:
        """The address worth printing: the LAN one only when the LAN is listening."""
        return self._ip if self._lan else BIND_LOCAL

    def describe_reach(self) -> str:
        """One honest line about who can open this dashboard right now."""
        if self._lan:
            return f"reachable from your network at {self._ip}:{PORT}"
        return f"this PC only (127.0.0.1:{PORT}) — LAN access is off"

    async def _serve_alias(self, host: str) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        if host == BIND_LAN:
            # A loopback socket never reaches the OS firewall, so no rule is
            # requested for it — asking for one would open the port to the
            # network the moment LAN access is switched on later.
            asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host=host, port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN,
        )
        shown = self._ip if host == BIND_LAN else BIND_LOCAL
        print(f"[Dashboard] Manual entry:  {shown}:{PORT + 1}  (type in browser, accept cert once)")
        failed = await _run_uvicorn(uvicorn.Server(cfg))
        if failed is not None:
            # The main port is the one that matters; a busy alias port only costs
            # the "type it in by hand" shortcut.
            print(f"[Dashboard] port {PORT + 1} unavailable — manual entry will not "
                  f"work ({failed!r}).")

    async def _run_once(self) -> str:
        """Hold the socket(s) until uvicorn stops or a rebind is asked for.

        Returns "rebind" (the LAN setting changed; the caller restarts with the
        new host), "failed" (it never got the socket) or "stopped" (the server
        ran and then finished — shutdown).
        """
        host     = self._bind_host()
        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        ssl_args = ({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)}
                    if use_ssl else {})

        cfg        = uvicorn.Config(self.app, host=host, port=PORT,
                                    log_level="warning",
                                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN,
                                    **ssl_args)
        server     = uvicorn.Server(cfg)
        self._server = server
        serve_task   = asyncio.create_task(_run_uvicorn(server))
        alias_task   = asyncio.create_task(self._serve_alias(host)) if use_ssl else None
        rebind_task  = asyncio.create_task(self._rebind_event.wait())

        # Nothing is announced, and no firewall hole is requested, until the
        # socket actually exists. Both used to happen before the bind: a UAC
        # prompt for a dashboard that then failed to start opens a hole for
        # nothing, and a banner promising an address nobody is listening on sends
        # the user to a phone that cannot connect. Normally this is a few
        # milliseconds.
        for _ in range(200):                          # up to ~10 s
            if server.started or serve_task.done():
                break
            await asyncio.sleep(0.05)

        if server.started and host == BIND_LAN:
            # Runs in a thread, as before — never blocks the server coming up.
            asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        if server.started or not serve_task.done():   # the latter: slow, not dead
            proto = "https" if use_ssl else "http"
            print(f"[Dashboard] {proto}://{self._shown_host()}:{PORT} — {self.describe_reach()}")
            print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")

        try:
            done, _pending = await asyncio.wait({serve_task, rebind_task},
                                                return_when=asyncio.FIRST_COMPLETED)
        finally:
            rebind_task.cancel()
            if alias_task is not None:
                alias_task.cancel()
            for t in (rebind_task, alias_task):
                if t is None:
                    continue
                try:
                    await t
                except BaseException:
                    pass
            self._server = None

        if serve_task in done:
            # A request that arrives while the socket is dying cannot be honoured;
            # leaving it set would make the next pass "rebind" instantly and spin.
            self._rebind_event.clear()
            failure = serve_task.exception() or (serve_task.result()
                                                 if not serve_task.cancelled() else None)
            if failure is not None:
                print(f"[Dashboard] server stopped: {failure!r}")
                return "failed"
            if not server.started:
                # uvicorn never got the socket — something else holds the port.
                print(f"[Dashboard] could not bind {host}:{PORT} — is another "
                      f"program using that port?")
                return "failed"
            return "stopped"

        # A rebind was asked for. Wind uvicorn down politely, but bounded: we are
        # about to take its port away, so the old socket has to be gone first, and
        # a hung shutdown must not wedge the dashboard forever.
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, 5.0)
        except asyncio.CancelledError:
            serve_task.cancel()
            raise
        except Exception:
            pass                     # timed out — wait_for already cancelled it
        self._rebind_event.clear()
        return "rebind"

    async def serve(self) -> None:
        """Run the dashboard until the app shuts down, rebinding on LAN changes.

        The rebind is real rather than "restart to apply": uvicorn runs inside a
        loop that also waits on _rebind_event, so flipping the setting in the UI
        moves the socket between 127.0.0.1 and every interface within a second.
        A phone connected at that moment drops and has to reconnect — when LAN
        access is being turned OFF, dropping it is the whole point.
        """
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        self._loop = asyncio.get_running_loop()
        self._lan  = _lan_enabled_from_config()
        while True:
            wanted_lan = self._lan
            why = await self._run_once()
            if why == "rebind":
                print(f"[Dashboard] rebinding — now {self.describe_reach()}")
                continue
            if why == "failed" and wanted_lan:
                # The LAN bind did not take (a port already held on that
                # interface). Losing the phone dashboard is not the only option:
                # fall back to this PC and keep running, and say so out loud.
                self._lan = False
                print("[Dashboard] LAN bind failed — staying on 127.0.0.1 only.")
                continue
            return
