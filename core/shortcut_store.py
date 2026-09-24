"""Small, deterministic store for user-defined application shortcuts.

Shortcut resolution happens before the language model is asked to improvise an
application name. The file contains only aliases and launch targets; it never
executes a command or stores credentials.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SHORTCUTS_FILE = BASE_DIR / "config" / "shortcuts.json"
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_lock = threading.RLock()


def _clean_alias(value: str) -> str:
    alias = " ".join(str(value or "").strip().casefold().split())
    if not alias or len(alias) > 40 or not _ALIAS_RE.fullmatch(alias):
        raise ValueError("shortcut names may contain only letters, numbers, dots, hyphens, and underscores")
    return alias


def _clean_target(value: str) -> str:
    target = str(value or "").strip()
    if not target or len(target) > 240 or any(ord(char) < 32 for char in target):
        raise ValueError("shortcut target is empty or invalid")
    return target


def _read() -> dict[str, str]:
    try:
        raw = json.loads(SHORTCUTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {
            _clean_alias(key): _clean_target(value)
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, str)
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _write(values: dict[str, str]) -> None:
    SHORTCUTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="shortcuts-", suffix=".json", dir=SHORTCUTS_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(sorted(values.items())), handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, SHORTCUTS_FILE)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def resolve(value: str) -> str:
    """Return a saved target, or the original value when it is not an alias."""
    raw = str(value or "").strip()
    try:
        alias = _clean_alias(raw)
    except ValueError:
        return raw
    with _lock:
        return _read().get(alias, raw)


def save(alias: str, target: str) -> str:
    alias = _clean_alias(alias)
    target = _clean_target(target)
    if alias == target.casefold():
        raise ValueError("a shortcut cannot point to itself")
    with _lock:
        values = _read()
        values[alias] = target
        _write(values)
    return target


def remove(alias: str) -> bool:
    alias = _clean_alias(alias)
    with _lock:
        values = _read()
        existed = alias in values
        values.pop(alias, None)
        if existed:
            _write(values)
        return existed


def all_shortcuts() -> dict[str, str]:
    with _lock:
        return dict(_read())
