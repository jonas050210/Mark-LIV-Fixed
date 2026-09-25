"""Validated, atomic access to MARK LIV's runtime configuration."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from core.json_store import JsonStore, JsonStoreCorruptError


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"


def _new_config() -> dict[str, Any]:
    return {}


def _is_config(value: object) -> bool:
    return isinstance(value, dict)


def _stored_bool(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _require_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be true or false")
    return value


def _clean_display_name(value: object, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    cleaned = " ".join(
        "".join(char for char in value if ord(char) >= 32 and char != "\x7f").split()
    )[:80]
    return cleaned or default


def _store() -> JsonStore[dict[str, Any]]:
    # Constructing this lightweight wrapper on demand lets tests safely patch
    # CONFIG_FILE without also having to replace a module-global store object.
    return JsonStore(CONFIG_FILE, _new_config, validator=_is_config, private=True)


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def config_exists() -> bool:
    return CONFIG_FILE.is_file()


def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return _store().read()
    except JsonStoreCorruptError as exc:
        # Do not overwrite a malformed configuration with defaults. Setters use
        # the same store and will fail until a backup can be recovered or the
        # corrupt file is repaired.
        print(f"❌ Failed to load api_keys.json safely ({type(exc).__name__}).")
        return {}


def patch_config(**fields: Any) -> dict:
    """Atomically merge fields into the configuration and return the result."""
    ensure_config_dir()

    def mutate(data: dict[str, Any]) -> None:
        data.update(fields)

    return _store().update(mutate)


def save_api_keys(gemini_api_key: str) -> None:
    patch_config(gemini_api_key=str(gemini_api_key or "").strip())


def get_gemini_key() -> str | None:
    value = load_api_keys().get("gemini_api_key")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    return _clean_display_name(
        load_api_keys().get("assistant_name", "JARVIS"), "JARVIS"
    )


def get_user_name() -> str:
    return _clean_display_name(load_api_keys().get("user_name", ""))


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    patch_config(
        assistant_name=_clean_display_name(assistant_name, "JARVIS"),
        user_name=_clean_display_name(user_name),
    )


AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE = "Charon"


def get_voice() -> str:
    value = load_api_keys().get("voice_name", DEFAULT_VOICE)
    return value if isinstance(value, str) and value in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    value = str(voice_name or "").strip()
    patch_config(voice_name=value if value in AVAILABLE_VOICES else DEFAULT_VOICE)


def get_wake_word_enabled() -> bool:
    return _stored_bool(load_api_keys().get("wake_word_enabled"), False)


def save_wake_word_enabled(enabled: bool) -> None:
    patch_config(wake_word_enabled=_require_bool(enabled, "wake_word_enabled"))


def get_push_to_talk_enabled() -> bool:
    return _stored_bool(load_api_keys().get("push_to_talk_enabled"), False)


def save_push_to_talk_enabled(enabled: bool) -> None:
    patch_config(push_to_talk_enabled=_require_bool(enabled, "push_to_talk_enabled"))


HUD_STYLES = ("face", "core")


def get_hud_style() -> str:
    value = load_api_keys().get("hud_style", "face")
    normalized = str(value).strip().lower() if isinstance(value, str) else "face"
    return normalized if normalized in HUD_STYLES else "face"


def save_hud_style(style: str) -> None:
    normalized = str(style or "").strip().lower()
    patch_config(hud_style=normalized if normalized in HUD_STYLES else "face")


HUD_FPS_OPTIONS = (30, 60, 120, 240, 0)  # 0 = unlimited


def get_hud_max_fps() -> int:
    value = load_api_keys().get("hud_max_fps", 60)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 60
    return parsed if parsed in HUD_FPS_OPTIONS else 60


def save_hud_max_fps(fps: int) -> None:
    try:
        parsed = int(fps)
    except (TypeError, ValueError) as exc:
        raise ValueError("HUD FPS must be 30, 60, 120, 240, or 0 for unlimited") from exc
    if parsed not in HUD_FPS_OPTIONS:
        raise ValueError("HUD FPS must be 30, 60, 120, 240, or 0 for unlimited")
    patch_config(hud_max_fps=parsed)


def get_thinking_enabled() -> bool:
    return _stored_bool(load_api_keys().get("thinking_enabled"), False)


def save_thinking_enabled(enabled: bool) -> None:
    patch_config(thinking_enabled=_require_bool(enabled, "thinking_enabled"))


def get_turn_tuning() -> dict:
    value = load_api_keys().get("turn_tuning")
    cfg = value if isinstance(value, dict) else {}

    def bounded_int(key: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    end = str(cfg.get("end_sensitivity", "high") or "high").strip().lower()
    start = str(cfg.get("start_sensitivity", "default") or "default").strip().lower()
    if end not in {"low", "default", "medium", "high"}:
        end = "high"
    if start not in {"low", "default", "medium", "high"}:
        start = "default"
    return {
        "enabled": _stored_bool(cfg.get("enabled"), False),
        "silence_ms": bounded_int("silence_ms", 550, 200, 3000),
        "prefix_ms": bounded_int("prefix_ms", 150, 0, 1000),
        "end_sensitivity": end,
        "start_sensitivity": start,
    }


def save_turn_tuning(values: dict) -> None:
    updates = dict(values) if isinstance(values, dict) else {}

    def mutate(data: dict[str, Any]) -> None:
        current = data.get("turn_tuning")
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(updates)
        data["turn_tuning"] = merged

    _store().update(mutate)


def get_proactive_audio_enabled() -> bool:
    return _stored_bool(load_api_keys().get("proactive_audio"), True)


def save_proactive_audio_enabled(enabled: bool) -> None:
    patch_config(proactive_audio=_require_bool(enabled, "proactive_audio"))


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    value = load_api_keys().get("media_resolution", "medium")
    normalized = str(value).strip().lower() if isinstance(value, str) else "medium"
    return normalized if normalized in MEDIA_RESOLUTIONS else "medium"


def save_media_resolution(value: str) -> None:
    normalized = str(value or "").strip().lower()
    patch_config(
        media_resolution=normalized if normalized in MEDIA_RESOLUTIONS else "medium"
    )


def _save_flag(key: str, value) -> None:
    patch_config(**{key: _require_bool(value, key)})


def get_brief_enabled() -> bool:
    return _stored_bool(load_api_keys().get("morning_brief_enabled"), True)


def save_brief_enabled(enabled: bool) -> None:
    patch_config(morning_brief_enabled=_require_bool(enabled, "morning_brief_enabled"))


def _patch_config(**fields) -> None:
    """Backward-compatible private alias used by older call sites."""
    patch_config(**fields)


def _clean_device_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(
        "".join(char for char in value if ord(char) >= 32 and char != "\x7f").split()
    )[:300]


def get_input_device() -> str:
    return _clean_device_name(load_api_keys().get("input_device", ""))


def save_input_device(name: str) -> None:
    patch_config(input_device=_clean_device_name(name))


def get_output_device() -> str:
    return _clean_device_name(load_api_keys().get("output_device", ""))


def save_output_device(name: str) -> None:
    patch_config(output_device=_clean_device_name(name))


def get_plugin_enabled(plugin_name: str) -> bool:
    value = load_api_keys().get("plugins_enabled")
    plugins = value if isinstance(value, dict) else {}
    return _stored_bool(plugins.get(str(plugin_name)), True)


def get_plugin_config(namespace: str) -> dict:
    value = load_api_keys().get("plugin_config")
    all_config = value if isinstance(value, dict) else {}
    section = all_config.get(str(namespace))
    return dict(section) if isinstance(section, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    if not isinstance(values, dict):
        raise TypeError("plugin configuration must be a dictionary")
    namespace = str(namespace or "").strip()
    if not namespace:
        raise ValueError("plugin configuration namespace is required")

    def mutate(data: dict[str, Any]) -> None:
        raw = data.get("plugin_config")
        all_config = dict(raw) if isinstance(raw, dict) else {}
        existing = all_config.get(namespace)
        section = dict(existing) if isinstance(existing, dict) else {}
        section.update(values)
        all_config[namespace] = section
        data["plugin_config"] = all_config

    _store().update(mutate)


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    name = str(plugin_name or "").strip()
    if not name:
        raise ValueError("plugin name is required")

    def mutate(data: dict[str, Any]) -> None:
        raw = data.get("plugins_enabled")
        plugins = dict(raw) if isinstance(raw, dict) else {}
        plugins[name] = _require_bool(enabled, "plugin enabled")
        data["plugins_enabled"] = plugins

    _store().update(mutate)
