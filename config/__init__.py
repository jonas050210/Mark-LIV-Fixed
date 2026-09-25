"""Small, type-safe accessors for platform configuration."""
from __future__ import annotations

import platform

from memory.config_manager import load_api_keys


def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )


def get_config() -> dict:
    value = load_api_keys()
    return value if isinstance(value, dict) else {}


def get_os() -> str:
    """Return one of ``windows``, ``mac``, or ``linux``."""
    value = get_config().get("os_system")
    normalized = value.strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in {"windows", "mac", "linux"} else _platform_os()


def is_windows() -> bool:
    return get_os() == "windows"


def is_mac() -> bool:
    return get_os() == "mac"


def is_linux() -> bool:
    return get_os() == "linux"
