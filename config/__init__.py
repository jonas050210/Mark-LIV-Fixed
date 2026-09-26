"""Small, type-safe accessors for the Windows-only runtime configuration."""
from __future__ import annotations

from memory.config_manager import load_api_keys


def get_config() -> dict:
    value = load_api_keys()
    return value if isinstance(value, dict) else {}


def get_os() -> str:
    return "windows"


def is_windows() -> bool:
    return True
