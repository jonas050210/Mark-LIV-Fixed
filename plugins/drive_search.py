"""Find Google Drive files by name without reading or modifying content."""

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


_CONFIG = Path(__file__).resolve().parent.parent / "config"
_CLIENT = _CONFIG / "client_secret_gmail.json"  # Same Desktop OAuth app; separate Drive token.
_TOKEN = _CONFIG / "token_drive.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]

PLUGIN = {
    "name": "drive_search",
    "description": (
        "Search Google Drive files by name and return titles, types, modification dates, "
        "and links. Does not read file contents, summarize documents, or modify files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING", "description": "File name or part of a file name to find."},
        },
        "required": ["query"],
    },
}

PLUGIN_SETTINGS = {
    "title": "GOOGLE DRIVE — FILE SEARCH",
    "fields": [],
    "action": {"label": "CONNECT DRIVE", "run": lambda _values: connect()},
}


def _save_token(creds: Credentials) -> None:
    _CONFIG.mkdir(parents=True, exist_ok=True)
    temporary = _CONFIG / "token_drive.tmp.json"
    temporary.write_text(creds.to_json(), encoding="utf-8")
    temporary.replace(_TOKEN)


def connect() -> tuple[bool, str]:
    if not _CLIENT.is_file():
        return False, "Put your Desktop OAuth JSON at config/client_secret_gmail.json first."
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(_CLIENT), _SCOPES)
        _save_token(flow.run_local_server(port=0))
    except Exception:
        return False, "Drive connection failed. Check the Drive API and OAuth consent settings."
    return True, "Drive connected for file-name search only."


def _credentials() -> Credentials | None:
    if not _TOKEN.is_file():
        return None
    try:
        data = json.loads(_TOKEN.read_text(encoding="utf-8"))
        if not set(_SCOPES).issubset(data.get("scopes", [])):
            return None
        creds = Credentials.from_authorized_user_info(data, _SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        return creds if creds.valid else None
    except Exception:
        return None


def run(parameters: dict) -> str:
    query = str(parameters.get("query", "")).strip()
    if not query:
        return "Tell me part of the Drive file name to search for."
    creds = _credentials()
    if creds is None:
        return "Drive is not connected. Open Plugin Settings and click CONNECT DRIVE."
    escaped = query.replace("\\", "\\\\").replace("'", "\\'")
    try:
        page = build("drive", "v3", credentials=creds, cache_discovery=False).files().list(
            q=f"name contains '{escaped}' and trashed = false",
            pageSize=20,
            fields="nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink)",
            orderBy="modifiedTime desc",
        ).execute()
    except Exception:
        return "Drive search failed. Check the connection and try again."
    files = page.get("files", [])
    if not files:
        return f"No Drive files found with '{query}' in the name."
    more = " More matches exist; narrow the file name." if page.get("nextPageToken") else ""
    return (f"Found {len(files)} Drive files; metadata only, not file contents.{more}\n"
            + json.dumps(files, ensure_ascii=False))
