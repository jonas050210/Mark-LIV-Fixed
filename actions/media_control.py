"""Background media control with a Spotify Connect integration.

Playback uses Spotify's Web API, not UI automation. Searching or controlling a
track therefore does not bring the Spotify window to the foreground. Spotify
must have an active Connect device and playback-control access; the first
``connect`` command performs the one-time OAuth consent flow.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

from core.json_store import JsonStore, JsonStoreCorruptError
from memory.config_manager import load_api_keys

try:
    import requests
except ImportError:  # optional in minimal installations
    requests = None

BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = BASE_DIR / "config" / "spotify_token.json"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"
SPOTIFY_API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing user-read-private user-read-playback-position"


def _config() -> dict:
    return load_api_keys()


def _token_store() -> JsonStore[dict]:
    return JsonStore(
        TOKEN_FILE,
        dict,
        validator=lambda value: isinstance(value, dict),
        private=True,
    )


def _client_id(parameters: dict) -> str:
    return str(
        parameters.get("client_id")
        or os.environ.get("SPOTIFY_CLIENT_ID")
        or _config().get("spotify_client_id")
        or ""
    ).strip()


def _save_token(token: dict) -> None:
    if not isinstance(token, dict):
        raise TypeError("Spotify token must be an object")
    _token_store().write(token)


def _load_token() -> dict:
    raw = os.environ.get("SPOTIFY_ACCESS_TOKEN", "").strip()
    if raw:
        return {"access_token": raw, "expires_at": time.time() + 300}
    if not TOKEN_FILE.exists():
        return {}
    try:
        return _token_store().read()
    except JsonStoreCorruptError as exc:
        print(f"[Spotify] Token store is corrupt ({type(exc).__name__}).")
        return {}


def _refresh_token(token: dict, client_id: str) -> str | None:
    if requests is None:
        return None
    access = str(token.get("access_token") or "")
    expires_at = float(token.get("expires_at") or 0)
    if access and expires_at > time.time() + 60:
        return access
    refresh = str(token.get("refresh_token") or "")
    if not refresh or not client_id:
        return None
    try:
        response = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
            timeout=15,
        )
        if response.status_code != 200:
            return None
        fresh = response.json()
        token.update(fresh)
        token["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
        _save_token(token)
        return str(token.get("access_token") or "") or None
    except Exception:
        return None


def _request(method: str, path: str, token: str, **kwargs):
    if requests is None:
        return None, {"error": "The requests package is not installed."}
    try:
        response = requests.request(
            method, SPOTIFY_API + path,
            headers={"Authorization": f"Bearer {token}"}, timeout=15, **kwargs,
        )
        if response.status_code == 204:
            return response.status_code, {}
        try:
            body = response.json()
        except Exception:
            body = {"message": response.text[:300]}
        return response.status_code, body
    except Exception as exc:
        return None, {"error": f"request failed ({type(exc).__name__})"}


def _error(status, body) -> str:
    if status == 401:
        return "Spotify authorization expired. Say connect Spotify again."
    if status == 403:
        return "Spotify refused playback control. A Spotify Premium account and an active Connect device are required."
    if status == 404:
        return "Spotify has no active playback device. Open Spotify or activate a Spotify Connect device once, then try again."
    detail = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("reason") or detail
    return f"Spotify request failed ({status or 'network error'}): {detail}"


def _device_id(token: str, requested: str = "") -> tuple[str | None, str]:
    status, body = _request("GET", "/me/player/devices", token)
    if status != 200:
        return None, _error(status, body)
    devices = body.get("devices", []) if isinstance(body, dict) else []
    wanted = requested.casefold().strip()
    ordered = sorted(devices, key=lambda item: not bool(item.get("is_active")))
    for device in ordered:
        name = str(device.get("name") or "")
        if wanted and (wanted in name.casefold() or str(device.get("id")) == requested):
            return str(device.get("id")), ""
    for device in ordered:
        if not device.get("is_restricted") and device.get("id"):
            return str(device["id"]), ""
    return None, "Spotify has no usable playback device. Open Spotify or activate a Spotify Connect device once."


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    callback = None
    expected_path = "/callback"

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query, max_num_fields=20)
        accepted = bool(self.callback(query)) if self.callback is not None else False
        if not accepted:
            self.send_error(400, "Invalid or expired OAuth state")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Spotify connection received. You can close this window.")
    def log_message(self, *_args):
        return


def _connect(parameters: dict) -> str:
    if requests is None:
        return "Spotify connection needs the requests package."
    client_id = _client_id(parameters)
    if not client_id:
        return "Set spotify_client_id in config/api_keys.json or SPOTIFY_CLIENT_ID first. Add http://127.0.0.1:8765/callback as a Spotify redirect URI."
    if not re.fullmatch(r"[A-Za-z0-9]{16,128}", client_id):
        return "The Spotify client ID has an invalid format."
    raw_redirect = parameters.get("redirect_uri") or DEFAULT_REDIRECT
    if not isinstance(raw_redirect, str) or len(raw_redirect) > 500:
        return "The Spotify redirect URI has an invalid format."
    redirect_uri = raw_redirect.strip()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    received: dict = {}
    event = threading.Event()

    def receive(query: dict):
        # Ignore unrelated loopback requests rather than letting another local
        # process consume and cancel the one-time OAuth flow.
        supplied_state = (query.get("state") or [""])[0]
        if not secrets.compare_digest(str(supplied_state), state):
            return False
        received.update({key: values[0] for key, values in query.items() if values})
        event.set()
        return True

    parsed = urllib.parse.urlparse(redirect_uri)
    try:
        port = parsed.port
    except ValueError:
        return "The Spotify redirect URI contains an invalid port."
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.path != "/callback" or parsed.username or parsed.password
            or port is None or not 1024 <= port <= 65535):
        return (
            "The Spotify redirect URI must be an HTTP localhost /callback URL "
            "using an unprivileged port."
        )
    handler = type(
        "SpotifyCallbackHandler",
        (_CallbackHandler,),
        {"callback": staticmethod(receive), "expected_path": parsed.path},
    )
    try:
        server = http.server.HTTPServer((parsed.hostname, port), handler)
    except OSError as exc:
        return f"Could not start the Spotify OAuth callback ({type(exc).__name__})."
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.2},
        daemon=True,
        name="spotify-oauth",
    )
    thread.start()
    query = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    opened = webbrowser.open(AUTH_URL + "?" + query)
    completed = event.wait(120) if opened else False
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    if not opened:
        return "I could not open the Spotify authorization page. Open the returned authorization URL manually."
    if not completed:
        return "Spotify authorization timed out."
    if received.get("state") != state:
        return "Spotify authorization state did not match; the connection was refused."
    if received.get("error"):
        return "Spotify authorization was cancelled or refused."
    code = received.get("code")
    if not code:
        return "Spotify did not return an authorization code."
    try:
        response = requests.post(TOKEN_URL, data={
            "client_id": client_id, "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier,
        }, timeout=15)
        if response.status_code != 200:
            return _error(response.status_code, response.json())
        token = response.json()
        token["client_id"] = client_id
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        _save_token(token)
        return "Spotify is connected. Playback commands will use the active Connect device without bringing Spotify to the foreground."
    except Exception as exc:
        return f"Spotify token exchange failed ({type(exc).__name__})."


# ── System media keys ────────────────────────────────────────────────────────
# The Web API needs Premium and an active Connect device.  When neither is
# available the operating system's own media transport still reaches whichever
# player owns the session.  These are hardware transport events, not simulated
# typing into a focused window, so they cannot land in the wrong text field.
_VK_MEDIA = {
    "play": 0xB3, "pause": 0xB3, "playpause": 0xB3,
    "next": 0xB0, "previous": 0xB1, "stop": 0xB2,
}


def _media_key(action: str) -> bool:
    """Send a system media-transport key. Returns True when it was delivered."""
    code = _VK_MEDIA.get(action)
    if code is None:
        return False
    try:
        import ctypes

        ctypes.windll.user32.keybd_event(code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(code, 0, 2, 0)
        return True
    except Exception:
        return False


def _fallback_note(action: str, api_error: str) -> str:
    if _media_key(action):
        return (
            f"Spotify's Web API was unavailable ({api_error}) so I used the system "
            f"media keys instead: {action}."
        )
    return api_error


def playback_snapshot() -> dict:
    """Structured now-playing state for the dashboard media panel.

    Read-only, and it never raises for the ordinary "not configured" cases: the
    panel needs to render a reason, not a stack trace.
    """
    token_data = _load_token()
    client_id = str(token_data.get("client_id") or "")
    token = _refresh_token(token_data, client_id)
    if not token:
        return {"connected": False, "reason": "Spotify is not connected."}

    status, body = _request("GET", "/me/player", token)
    if status == 204 or not body:
        return {"connected": True, "playing": False, "reason": "Nothing is playing."}
    if status != 200:
        return {"connected": True, "playing": False, "reason": _error(status, body)}

    item = body.get("item") or {}
    album = item.get("album") or {}
    images = album.get("images") or []
    artwork = ""
    for image in images:
        url = str(image.get("url") or "")
        if url.startswith("https://"):
            artwork = url
            break
    device = body.get("device") or {}
    return {
        "connected": True,
        "playing": bool(body.get("is_playing")),
        "track": str(item.get("name") or ""),
        "artists": ", ".join(str(a.get("name") or "") for a in item.get("artists", [])),
        "album": str(album.get("name") or ""),
        "artwork": artwork,
        "progress_ms": int(body.get("progress_ms") or 0),
        "duration_ms": int(item.get("duration_ms") or 0),
        "shuffle": bool(body.get("shuffle_state")),
        "repeat": str(body.get("repeat_state") or "off"),
        "volume": int(device.get("volume_percent") or 0),
        "device": str(device.get("name") or ""),
    }


def media_control(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "status")[:32].casefold().strip().replace(" ", "_")
    if action in {"connect", "authorize", "login"}:
        return _connect(p)
    token_data = _load_token()
    client_id = _client_id(p) or str(token_data.get("client_id") or "")
    token = _refresh_token(token_data, client_id)
    if not token:
        return "Spotify is not connected. Set a Spotify client ID and say connect Spotify once; playback itself will stay in the background."

    if action in {"search", "find"}:
        query = str(p.get("query") or p.get("text") or "")[:500].strip()
        if not query:
            return "Tell me what to search for on Spotify."
        status, body = _request("GET", "/search", token, params={"q": query, "type": "track", "limit": 5})
        if status != 200:
            return _error(status, body)
        tracks = body.get("tracks", {}).get("items", [])
        if not tracks:
            return f"Spotify found no tracks for '{query}'."
        return "Spotify results:\n" + "\n".join(
            f"{index}. {item.get('name')} — {', '.join(a.get('name', '') for a in item.get('artists', []))}"
            for index, item in enumerate(tracks, 1)
        )

    if action in {"status", "now_playing", "current"}:
        status, body = _request("GET", "/me/player", token)
        if status == 204 or not body:
            return "Spotify is connected but nothing is currently playing."
        if status != 200:
            return _error(status, body)
        item = body.get("item") or {}
        artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
        return f"Spotify: {item.get('name', 'Unknown')} — {artists} ({'playing' if body.get('is_playing') else 'paused'})."

    if action in {"devices", "list_devices"}:
        status, body = _request("GET", "/me/player/devices", token)
        if status != 200:
            return _error(status, body)
        devices = body.get("devices", [])
        return "Spotify devices:\n" + "\n".join(
            f"- {d.get('name')} ({d.get('type')})" + (" [active]" if d.get("is_active") else "")
            for d in devices
        ) if devices else "No Spotify Connect devices are active."

    device, device_error = _device_id(token, str(p.get("device") or p.get("device_id") or ""))
    if not device:
        transport = {"play": "play", "resume": "play", "pause": "pause", "stop": "pause",
                     "next": "next", "skip": "next", "previous": "previous", "back": "previous"}
        if action in transport and not p.get("query") and not p.get("track") and not p.get("uri"):
            return _fallback_note(transport[action], device_error)
        return device_error
    device_param = {"device_id": device}

    if action in {"play", "resume"}:
        query = str(p.get("query") or p.get("track") or p.get("text") or "").strip()
        payload = {}
        if query:
            status, body = _request("GET", "/search", token, params={"q": query, "type": "track", "limit": 1})
            if status != 200:
                return _error(status, body)
            tracks = body.get("tracks", {}).get("items", [])
            if not tracks:
                return f"Spotify found no track for '{query}'."
            payload["uris"] = [tracks[0]["uri"]]
        elif p.get("uri"):
            payload["uris"] = [str(p["uri"])]
        status, body = _request("PUT", "/me/player/play", token, params=device_param, json=payload)
        if status in {200, 204}:
            return "Spotify playback started in the background."
        return _error(status, body) if payload else _fallback_note("play", _error(status, body))

    if action in {"pause", "stop"}:
        status, body = _request("PUT", "/me/player/pause", token, params=device_param)
        return "Spotify paused." if status in {200, 204} else _fallback_note("pause", _error(status, body))
    if action in {"next", "skip"}:
        status, body = _request("POST", "/me/player/next", token, params=device_param)
        return "Skipped to the next Spotify track." if status in {200, 204} else _fallback_note("next", _error(status, body))
    if action in {"previous", "back"}:
        status, body = _request("POST", "/me/player/previous", token, params=device_param)
        return "Moved to the previous Spotify track." if status in {200, 204} else _fallback_note("previous", _error(status, body))
    if action in {"volume", "set_volume"}:
        try:
            volume = max(0, min(100, int(p.get("value", p.get("volume", 0)))))
        except (TypeError, ValueError):
            return "Volume must be a number from 0 to 100."
        status, body = _request("PUT", "/me/player/volume", token, params={**device_param, "volume_percent": volume})
        return f"Spotify volume set to {volume} percent." if status in {200, 204} else _error(status, body)
    if action in {"queue", "add_to_queue"}:
        uri = str(p.get("uri") or "").strip()
        if not uri:
            return "Provide a Spotify track URI to queue. Search first if needed."
        status, body = _request("POST", "/me/player/queue", token, params={**device_param, "uri": uri})
        return "Added the track to the Spotify queue." if status in {200, 204} else _error(status, body)
    if action in {"shuffle", "set_shuffle"}:
        raw = p.get("value", p.get("enabled", True))
        if isinstance(raw, str):
            enabled = raw.strip().casefold() not in {"off", "false", "no", "0"}
        else:
            enabled = bool(raw)
        status, body = _request("PUT", "/me/player/shuffle", token,
                                params={**device_param, "state": "true" if enabled else "false"})
        if status in {200, 204}:
            return f"Spotify shuffle is now {'on' if enabled else 'off'}."
        return _error(status, body)

    if action in {"repeat", "set_repeat", "loop"}:
        raw = str(p.get("mode") or p.get("value") or "context").casefold().strip()
        mode = {"all": "context", "playlist": "context", "album": "context",
                "one": "track", "song": "track", "single": "track",
                "off": "off", "none": "off", "context": "context", "track": "track"}.get(raw)
        if mode is None:
            return "Repeat mode must be off, track, or context."
        status, body = _request("PUT", "/me/player/repeat", token,
                                params={**device_param, "state": mode})
        if status in {200, 204}:
            label = {"off": "off", "track": "repeating the current track",
                     "context": "repeating the whole context"}[mode]
            return f"Spotify repeat is now {label}."
        return _error(status, body)

    if action in {"seek", "scrub", "jump"}:
        raw = p.get("value", p.get("seconds", p.get("position")))
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return "Tell me the position in seconds to seek to."
        if not 0 <= seconds <= 86_400:
            return "Seek position must be between 0 seconds and 24 hours."
        status, body = _request("PUT", "/me/player/seek", token,
                                params={**device_param, "position_ms": int(seconds * 1000)})
        if status in {200, 204}:
            return f"Moved Spotify playback to {int(seconds // 60)}:{int(seconds % 60):02d}."
        return _error(status, body)

    return ("Use connect, search, play, pause, next, previous, shuffle, repeat, "
            "seek, volume, queue, status, or devices.")


TOOL = {
    "name": "media_control",
    "description": (
        "Controls Spotify playback through Spotify Connect without using the visible Spotify window. "
        "Search and play tracks, pause, skip, shuffle, repeat, seek, change volume, inspect the current song, list devices, "
        "and connect Spotify once. Playback needs Spotify Premium and an active Connect device."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "enum": ["connect", "search", "play", "pause", "next", "previous", "shuffle", "repeat", "seek", "volume", "queue", "status", "devices"], "maxLength": 16, "description": "connect | search | play | pause | next | previous | shuffle | repeat | seek | volume | queue | status | devices"},
            "query": {"type": "STRING", "maxLength": 500, "description": "Song, artist, or search text."},
            "track": {"type": "STRING", "maxLength": 500, "description": "Track search text for play."},
            "uri": {"type": "STRING", "maxLength": 200, "description": "Spotify track URI for play or queue."},
            "value": {"type": "NUMBER", "minimum": 0, "maximum": 86400, "description": "Volume percent for volume, seconds for seek, or 1/0 for shuffle."},
            "mode": {"type": "STRING", "enum": ["off", "track", "context"], "maxLength": 10, "description": "Repeat mode: off, track, or context."},
            "enabled": {"type": "BOOLEAN", "description": "Shuffle on or off."},
            "device": {"type": "STRING", "maxLength": 300, "description": "Optional Spotify device name or ID."},
            "client_id": {"type": "STRING", "minLength": 16, "maxLength": 128, "description": "Spotify application client ID for the one-time connect flow."},
        },
        "required": ["action"],
    },
    "handler": media_control,
    "category": "media",
    "timeout_seconds": 150,
}
