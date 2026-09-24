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
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

try:
    import requests
except ImportError:  # optional in minimal installations
    requests = None

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "api_keys.json"
TOKEN_FILE = BASE_DIR / "config" / "spotify_token.json"
DEFAULT_REDIRECT = "http://127.0.0.1:8765/callback"
SPOTIFY_API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing user-read-private user-read-playback-position"


def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _client_id(parameters: dict) -> str:
    return str(
        parameters.get("client_id")
        or os.environ.get("SPOTIFY_CLIENT_ID")
        or _config().get("spotify_client_id")
        or ""
    ).strip()


def _save_token(token: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = TOKEN_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(token, indent=2), encoding="utf-8")
    os.replace(temporary, TOKEN_FILE)


def _load_token() -> dict:
    raw = os.environ.get("SPOTIFY_ACCESS_TOKEN", "").strip()
    if raw:
        return {"access_token": raw, "expires_at": time.time() + 300}
    try:
        value = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
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
        return None, {"error": str(exc)}


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
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if self.callback is not None:
            self.callback(query)
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
    redirect_uri = str(parameters.get("redirect_uri") or DEFAULT_REDIRECT).strip()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    received: dict = {}
    event = threading.Event()

    def receive(query: dict):
        received.update({key: values[0] for key, values in query.items() if values})
        event.set()

    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return "The Spotify redirect URI must point to localhost for the local OAuth callback."
    try:
        server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), _CallbackHandler)
    except OSError as exc:
        return f"Could not start the Spotify OAuth callback on {redirect_uri}: {exc}"
    _CallbackHandler.callback = receive
    thread = threading.Thread(target=server.handle_request, daemon=True, name="spotify-oauth")
    thread.start()
    query = urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256",
        "code_challenge": challenge,
    })
    if not webbrowser.open(AUTH_URL + "?" + query):
        server.server_close()
        return "I could not open the Spotify authorization page. Open the returned authorization URL manually."
    if not event.wait(120):
        server.server_close()
        return "Spotify authorization timed out."
    thread.join(timeout=2)
    server.server_close()
    if received.get("state") != state:
        return "Spotify authorization state did not match; the connection was refused."
    if received.get("error"):
        return f"Spotify authorization was cancelled: {received['error']}."
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
        return f"Spotify token exchange failed: {exc}"


def media_control(parameters: dict | None = None, player=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "status").casefold().strip().replace(" ", "_")
    if action in {"connect", "authorize", "login"}:
        return _connect(p)
    token_data = _load_token()
    client_id = _client_id(p) or str(token_data.get("client_id") or "")
    token = _refresh_token(token_data, client_id)
    if not token:
        return "Spotify is not connected. Set a Spotify client ID and say connect Spotify once; playback itself will stay in the background."

    if action in {"search", "find"}:
        query = str(p.get("query") or p.get("text") or "").strip()
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
        return "Spotify playback started in the background." if status in {200, 204} else _error(status, body)

    if action in {"pause", "stop"}:
        status, body = _request("PUT", "/me/player/pause", token, params=device_param)
        return "Spotify paused." if status in {200, 204} else _error(status, body)
    if action in {"next", "skip"}:
        status, body = _request("POST", "/me/player/next", token, params=device_param)
        return "Skipped to the next Spotify track." if status in {200, 204} else _error(status, body)
    if action in {"previous", "back"}:
        status, body = _request("POST", "/me/player/previous", token, params=device_param)
        return "Moved to the previous Spotify track." if status in {200, 204} else _error(status, body)
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
    return "Use connect, search, play, pause, next, previous, volume, queue, status, or devices."


TOOL = {
    "name": "media_control",
    "description": (
        "Controls Spotify playback through Spotify Connect without using the visible Spotify window. "
        "Search and play tracks, pause, skip, change volume, inspect the current song, list devices, "
        "and connect Spotify once. Playback needs Spotify Premium and an active Connect device."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "connect | search | play | pause | next | previous | volume | queue | status | devices"},
            "query": {"type": "STRING", "description": "Song, artist, or search text."},
            "track": {"type": "STRING", "description": "Track search text for play."},
            "uri": {"type": "STRING", "description": "Spotify track URI for play or queue."},
            "value": {"type": "INTEGER", "description": "Spotify volume from 0 to 100."},
            "device": {"type": "STRING", "description": "Optional Spotify device name or ID."},
            "client_id": {"type": "STRING", "description": "Spotify application client ID for the one-time connect flow."},
        },
        "required": ["action"],
    },
    "handler": media_control,
    "category": "media",
    "timeout_seconds": 150,
}
