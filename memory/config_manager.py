import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"
_CONFIG_LOCK = threading.RLock()

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()


def _read_config_state() -> tuple[dict, bool]:
    """Return `(data, intact)`.

    `intact` is False when the file exists but could not be read or parsed.
    The distinction is what protects the config: a missing or empty file is a
    fresh install and safe to initialise, while an unreadable one still holds
    the API key and every setting, and must never be overwritten.
    """
    if not CONFIG_FILE.exists():
        return {}, True
    try:
        raw = CONFIG_FILE.read_text(encoding="utf-8")
    except Exception as e:
        print(f"❌ Failed to read api_keys.json: {e}")
        return {}, False
    if not raw.strip():
        return {}, True          # present but empty: nothing to lose, start fresh
    try:
        return json.loads(raw), True
    except Exception as e:
        print(f"❌ Failed to parse api_keys.json: {e}")
        return {}, False


def _read_config_unlocked() -> dict:
    return _read_config_state()[0]


def _refuse_corrupt_write() -> bool:
    """Keep a copy of an unreadable config and tell the user. Always False.

    Every save is a read-modify-write. Reading a file that failed to parse
    returns `{}`, so merging into it and writing back would replace a config
    that still holds the API key with one that holds almost nothing: a corrupt
    file would silently become an empty one. OneDrive placeholders, sync
    conflicts and interrupted editors all produce exactly that state, so the
    write is refused, the evidence is kept, and the caller gets False.
    """
    kept = None
    if CONFIG_FILE.exists():
        target = CONFIG_DIR / CONFIG_FILE.name.replace(".json", ".corrupt.json")
        try:
            ensure_config_dir()
            shutil.copyfile(CONFIG_FILE, target)
            kept = target
        except Exception as e:
            print(f"❌ Could not quarantine the unreadable config: {e}")
    print("❌ Refusing to write api_keys.json: it exists but cannot be parsed.")
    if kept is not None:
        print(f"   A copy was kept at {kept}")
        print("   Repair that copy and move it back, or delete it to start "
              "fresh — settings will not persist until you do.")
    else:
        print("   Repair or delete the file — settings will not persist "
              "until you do.")
    return False


def _write_config_unlocked(data: dict) -> None:
    """Atomically replace api_keys.json in its own directory.

    Jonas keeps the checkout in OneDrive. A direct write can leave a truncated
    JSON file if the process exits or the sync client observes it halfway
    through. Writing a sibling temp file, flushing it, and then using
    os.replace() means readers see either the old complete config or the new
    complete config — never half of one.
    """
    ensure_config_dir()
    fd, tmp_name = tempfile.mkstemp(
        prefix=CONFIG_FILE.name + ".", suffix=".tmp", dir=str(CONFIG_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=4)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

def save_api_keys(gemini_api_key: str) -> bool:
    return _patch_config(gemini_api_key=gemini_api_key.strip())


def save_initial_setup(gemini_api_key: str, os_system: str) -> bool:
    """Persist first-launch values while preserving every existing setting."""
    return _patch_config(
        gemini_api_key=(gemini_api_key or "").strip(),
        os_system=(os_system or "").strip(),
    )


# Core provider credentials shown in the native API Keys overlay. Plugin-owned
# credentials stay in the plugin settings namespace and are not duplicated here.
_PROVIDER_API_KEYS = frozenset({
    "gemini_api_key",
    "elevenlabs_api_key",
})


def save_provider_api_keys(values: dict[str, str]) -> bool:
    """Atomically persist known provider keys without replacing other settings.

    Callers may pass only the fields that changed. Unknown names are ignored so
    a UI or plugin cannot accidentally turn this helper into an unrestricted
    config writer.
    """
    updates: dict[str, str] = {}
    for name, value in (values or {}).items():
        if name in _PROVIDER_API_KEYS:
            updates[name] = str(value or "").strip()
    return _patch_config(**updates) if updates else True


def load_api_keys() -> dict:
    with _CONFIG_LOCK:
        return _read_config_unlocked()

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> bool:
    """Persist assistant name and user name to config."""
    return _patch_config(assistant_name=assistant_name.strip() or "JARVIS",
                  user_name=user_name.strip())


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> bool:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    v = (voice_name or "").strip()
    return _patch_config(voice_name=v if v in AVAILABLE_VOICES else DEFAULT_VOICE)

def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until 'Hey Jarvis')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> bool:
    return _patch_config(wake_word_enabled=bool(enabled))

def get_push_to_talk_enabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return load_api_keys().get("push_to_talk_enabled", False)


def save_push_to_talk_enabled(enabled: bool) -> bool:
    return _save_flag("push_to_talk_enabled", enabled)


def get_dashboard_lan_enabled() -> bool:
    """Whether the phone dashboard may be reached from other devices.

    Off by default, and that is the point: the server then binds the loopback
    interface only, so nothing else on the network can even open the pairing
    screen — the strongest answer to "who gets to guess my key" is "only this
    PC". Turning it on binds every interface and asks the OS firewall for a rule
    (dashboard/server.py does both), which is what you want on a trusted home
    Wi-Fi and a decision nobody should inherit silently.
    """
    return load_api_keys().get("dashboard_lan_enabled", False)


def save_dashboard_lan_enabled(enabled: bool) -> bool:
    return _save_flag("dashboard_lan_enabled", enabled)


HUD_STYLES = ("face", "core")


def get_hud_style() -> str:
    """Which centrepiece the HUD draws: the animated head, or the reactor core.

    Taste, not capability — both render in the same software painter and cost
    about the same. Defaults to the head because that is what MARK LIV shipped
    with; anyone who preferred the older look can switch back in ⚙ and the
    choice survives a restart.
    """
    v = str(load_api_keys().get("hud_style", "face")).strip().lower()
    return v if v in HUD_STYLES else "face"


def save_hud_style(style: str) -> bool:
    s = str(style or "").strip().lower()
    return _save_flag("hud_style", s if s in HUD_STYLES else "face")


# ── Live-session tuning ──────────────────────────────────────────────────────
# Everything here is optional and has a working default, so an untouched
# config behaves exactly like a configured one. Each value is also a way out:
# if a future model dislikes one of these, set it back and nothing else changes.

def get_thinking_enabled() -> bool:
    """Whether the Live model may spend tokens thinking before it answers.

    Off by default. A voice assistant is judged on how fast it starts talking,
    and the reasoning that actually needs deliberation in this app is delegated
    to the planning tools, which run on a separate non-Live model.
    """
    return bool(load_api_keys().get("thinking_enabled", False))


def save_thinking_enabled(enabled: bool) -> bool:
    return _save_flag("thinking_enabled", enabled)


def get_turn_tuning() -> dict:
    """How eagerly the server decides you have stopped speaking.

    OFF by default, and that default was earned. Cutting turns shorter looks
    like a free speed win and is not: proactive audio has to judge whether an
    utterance was even addressed to the assistant, and a turn clipped early
    gives it less to judge, so it stays quiet — and the reply to your first
    sentence only arrives once your second one has given it enough context.
    That reads as the assistant being a turn behind, which is far worse than
    the fraction of a second the tuning saves.

    Turn it on with "turn_tuning": {"enabled": true} if your own microphone and
    speaking pace suit it. `silence_ms` is the one that is felt: the pause the
    server sits through before accepting your turn is over.
    """
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms":  _int("prefix_ms", 150, 0, 1000),
        # "high" = quicker to decide speech has ended.
        "end_sensitivity":   str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def save_turn_tuning(values: dict) -> bool:
    with _CONFIG_LOCK:
        data, intact = _read_config_state()
        if not intact:
            return _refuse_corrupt_write()
        cur = data.get("turn_tuning")
        cur = dict(cur) if isinstance(cur, dict) else {}
        cur.update(values or {})
        data["turn_tuning"] = cur
        _write_config_unlocked(data)
        return True

def get_proactive_audio_enabled() -> bool:
    """Whether the model gets to decide an utterance was not aimed at it and
    stay quiet.

    On by default — it is what stops the assistant answering the room. But it
    is also the first thing to switch off if replies ever seem to arrive a turn
    late: what looks like lag is usually the model having judged your previous
    sentence as not addressed to it, and only changing its mind once the next
    one arrives.
    """
    return bool(load_api_keys().get("proactive_audio", True))


def save_proactive_audio_enabled(enabled: bool) -> bool:
    return _save_flag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    """How finely the model tokenises the screenshots and camera frames it is
    sent. 'medium' keeps on-screen text readable at a fraction of the tokens a
    full-resolution frame costs; 'low' is cheaper still but starts losing small
    text, which is most of what screen captures are for."""
    v = str(load_api_keys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"


def save_media_resolution(value: str) -> bool:
    v = str(value or "").strip().lower()
    return _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


def _save_flag(key: str, value) -> bool:
    """Read-modify-write one key without disturbing the rest of the config."""
    return _patch_config(**{key: bool(value) if isinstance(value, bool) else value})

def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> bool:
    return _patch_config(morning_brief_enabled=bool(enabled))


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> bool:
    """Read-modify-write one or more keys in api_keys.json atomically.

    Returns False and writes nothing if the existing file is unreadable
    (see _refuse_corrupt_write).
    """
    with _CONFIG_LOCK:
        data, intact = _read_config_state()
        if not intact:
            return _refuse_corrupt_write()
        data.update(fields)
        _write_config_unlocked(data)
        return True

def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> bool:
    return _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> bool:
    return _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# several printer plugins) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> bool:
    """Merge `values` into a namespace's stored config atomically.

    Returns False and writes nothing if the existing file is unreadable.
    """
    with _CONFIG_LOCK:
        data, intact = _read_config_state()
        if not intact:
            return _refuse_corrupt_write()
        pc = data.get("plugin_config")
        if not isinstance(pc, dict):
            pc = {}
        cur = pc.get(namespace)
        if not isinstance(cur, dict):
            cur = {}
        cur.update(values)
        pc[namespace] = cur
        data["plugin_config"] = pc
        _write_config_unlocked(data)
        return True

def save_plugin_enabled(plugin_name: str, enabled: bool) -> bool:
    with _CONFIG_LOCK:
        data, intact = _read_config_state()
        if not intact:
            return _refuse_corrupt_write()
        plugins_cfg = data.get("plugins_enabled")
        if not isinstance(plugins_cfg, dict):
            plugins_cfg = {}
        plugins_cfg[plugin_name] = enabled
        data["plugins_enabled"] = plugins_cfg
        _write_config_unlocked(data)
        return True
