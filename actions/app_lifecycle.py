"""Reliable application status, diagnosis, and graceful restart actions."""
from __future__ import annotations

import time
from pathlib import Path

from actions.open_app import _alias_target, _matching_windows, open_app
from core.app_index import load_index, resolve
from core.window_manager import operate


def _process_details(windows) -> list[str]:
    rows: list[str] = []
    seen: set[int] = set()
    for window in windows:
        pid = int(getattr(window, "pid", 0) or 0)
        if pid in seen:
            continue
        seen.add(pid)
        state = "running"
        if pid:
            try:
                import psutil

                process = psutil.Process(pid)
                state = process.status()
                if not process.is_running():
                    state = "stopped"
            except Exception:
                state = "running"
        rows.append(f"PID {pid or 'unknown'} ({state})")
    return rows


def _snapshot(name: str) -> tuple[list, list]:
    normalized = _alias_target(name)
    windows = _matching_windows(name, normalized)
    try:
        index = load_index()
        entries = resolve(normalized, entries=index) or resolve(name, entries=index)
    except Exception:
        entries = []
    return windows, entries


def _status(name: str, *, detailed: bool = False) -> str:
    windows, entries = _snapshot(name)
    if not windows and not entries:
        return f"I could not find '{name}' installed or running."
    parts = [f"{name}: {'running' if windows else 'not running'}"]
    if windows:
        parts.append(f"{len(windows)} visible window(s)")
        parts.append(", ".join(_process_details(windows)))
    if entries:
        entry = entries[0]
        target = Path(entry.target).name if entry.kind in {"exec", "lnk", "bundle"} else entry.target
        parts.append(f"installed as {entry.name} [{entry.kind}/{entry.source}] target={target}")
    if detailed and windows:
        window_rows = [
            f"HWND {window.handle}: {window.title} [{window.process}]"
            for window in windows[:20]
        ]
        parts.append("windows: " + "; ".join(window_rows))
    return "; ".join(parts) + "."


def _restart(name: str) -> str:
    windows, entries = _snapshot(name)
    if not windows and not entries:
        return f"I could not find an installed application called '{name}'."

    for window in windows:
        try:
            operate(window, "close")
        except Exception as exc:
            return f"I could not close {window.title or name} ({type(exc).__name__})."

    deadline = time.monotonic() + 10.0
    normalized = _alias_target(name)
    while windows and time.monotonic() < deadline:
        time.sleep(0.2)
        windows = _matching_windows(name, normalized)
    if windows:
        return (
            f"{name} did not close within 10 seconds, so I did not force-kill it or "
            "start a duplicate."
        )
    return open_app({"app_name": entries[0].name if entries else name})


def app_lifecycle(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "status").strip().casefold().replace(" ", "_")
    name = str(p.get("app_name") or p.get("target") or "").strip()[:160]
    if not name:
        return "An application name is required."
    if action in {"status", "is_running"}:
        return _status(name)
    if action in {"diagnose", "diagnostics"}:
        return _status(name, detailed=True)
    if action in {"restart", "relaunch"}:
        return _restart(name)
    return "Unknown app lifecycle action. Use status, diagnose, or restart."


TOOL = {
    "name": "app_lifecycle",
    "description": (
        "Checks whether an application is installed and running, diagnoses its indexed "
        "launch type and visible windows, or gracefully restarts it. Restart addresses "
        "windows by handle, waits for them to close, never uses hotkeys, and never "
        "force-kills a process that may contain unsaved work."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["status", "diagnose", "restart"],
                "description": "status | diagnose | restart",
            },
            "app_name": {
                "type": "STRING",
                "maxLength": 160,
                "description": "Installed application name or visible window name.",
            },
        },
        "required": ["action", "app_name"],
    },
    "handler": app_lifecycle,
    "risk": "medium",
}
