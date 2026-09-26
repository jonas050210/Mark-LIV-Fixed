"""Validated transactional store for user-defined application shortcuts."""
from __future__ import annotations

import re
from pathlib import Path

from core.json_store import JsonStore, JsonStoreCorruptError

BASE_DIR = Path(__file__).resolve().parent.parent
SHORTCUTS_FILE = BASE_DIR / "config" / "shortcuts.json"
# A spoken alias is often more than one word ("school mode", "my code").
# Keep it human-friendly while excluding control characters and path separators;
# aliases are dictionary keys, never paths.
_ALIAS_RE = re.compile(r"^[^\\/][^\\/]{0,39}$")


def _clean_alias(value: str) -> str:
    alias = " ".join(str(value or "").strip().casefold().split())
    valid = (
        bool(alias)
        and len(alias) <= 40
        and bool(_ALIAS_RE.fullmatch(alias))
        and alias[0].isalnum()
        and all(char.isalnum() or char in " ._-" for char in alias)
    )
    if not valid:
        raise ValueError(
            "shortcut names may contain letters, numbers, spaces, dots, hyphens, and underscores"
        )
    return alias


def _clean_target(value: str) -> str:
    target = str(value or "").strip()
    # OneDrive business folders and nested project shortcuts regularly exceed
    # the old 240-character limit, even though Windows and the launcher accept
    # the path. Keep a defensive upper bound, but do not make cloud-synced
    # desktop aliases impossible to save.
    if not target or len(target) > 1_024 or any(ord(char) < 32 for char in target):
        raise ValueError("shortcut target is empty or invalid")
    return target


def _valid_shortcuts(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > 1_000:
        return False
    try:
        for alias, target in value.items():
            if _clean_alias(alias) != alias or _clean_target(target) != target:
                return False
    except (TypeError, ValueError):
        return False
    return True


def _store() -> JsonStore[dict[str, str]]:
    return JsonStore(SHORTCUTS_FILE, dict, validator=_valid_shortcuts, private=True)


def _read() -> dict[str, str]:
    if not SHORTCUTS_FILE.exists():
        return {}
    try:
        return _store().read()
    except JsonStoreCorruptError as exc:
        print(f"[Shortcuts] Store is corrupt ({type(exc).__name__}).")
        return {}


def resolve(value: str) -> str:
    """Return a saved target, or the original value when it is not an alias."""
    raw = str(value or "").strip()
    try:
        alias = _clean_alias(raw)
    except ValueError:
        return raw
    return _read().get(alias, raw)


def save(alias: str, target: str) -> str:
    alias = _clean_alias(alias)
    target = _clean_target(target)
    if alias == target.casefold():
        raise ValueError("a shortcut cannot point to itself")

    def update(values: dict[str, str]) -> None:
        values[alias] = target

    _store().update(update)
    return target


def remove(alias: str) -> bool:
    alias = _clean_alias(alias)
    existed = False

    def update(values: dict[str, str]) -> None:
        nonlocal existed
        existed = alias in values
        values.pop(alias, None)

    _store().update(update)
    return existed


def all_shortcuts() -> dict[str, str]:
    return dict(_read())
