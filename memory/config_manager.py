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
    """Plugins are enabled by default the moment they're discovered (opt-out model).

    A hand-edited config may hold a non-dict `plugins_enabled` (or the read
    may fail outright) - neither must disable, let alone crash: only an
    explicit per-plugin False opts out.
    """
    try:
        flags = load_api_keys().get("plugins_enabled", {})
    except Exception:
        return True
    if not isinstance(flags, dict):
        return True
    return bool(flags.get(plugin_name, True))


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


# ── Optional local AI fallback (Ollama) ───────────────────────────────────────
# Used only as a fallback for intent/planning (core/local_ai.py). Everything
# works without it: when the server is unreachable the callers degrade to
# their rule-based paths.

def get_local_ai_enabled() -> bool:
    """Whether the local model may be tried as a fallback (default True)."""
    try:
        return bool(load_api_keys().get("local_ai_enabled", True))
    except Exception:
        return True


def save_local_ai_enabled(enabled: bool) -> bool:
    return _patch_config(local_ai_enabled=bool(enabled))


# ── Voice output routing (core/speech.py) ─────────────────────────────────────
# "auto" (default) = Live session when connected, else the offline OS voice,
# else the activity log. "live" / "local" pin one engine, "silent" logs only.

_VOICE_ENGINES = ("auto", "live", "local", "silent")


def get_voice_engine() -> str:
    """Voice output mode for announcements (default "auto")."""
    try:
        mode = str(load_api_keys().get("voice_engine", "auto") or "auto")
    except Exception:
        return "auto"
    mode = mode.strip().lower()
    return mode if mode in _VOICE_ENGINES else "auto"


def save_voice_engine(mode: str) -> bool:
    mode = (mode or "auto").strip().lower()
    if mode not in _VOICE_ENGINES:
        mode = "auto"
    return _patch_config(voice_engine=mode)


def get_local_tts_voice() -> str:
    """Preferred offline voice id/name ('' for the OS default).

    Reserved for the future local JARVIS voice: custom providers plugged into
    core/speech.py read their selection from here, so no caller changes when
    one arrives.
    """
    try:
        return (load_api_keys().get("local_tts_voice", "") or "").strip()
    except Exception:
        return ""


def save_local_tts_voice(name: str) -> bool:
    return _patch_config(local_tts_voice=(name or "").strip())


# ────────────────────────────────────────────────────────────────────────────────
# GUI / HUD presentation -- Mark LIV 54.
# Every one of these has a sane default and is read by the UI at build
# time, on resize, or when the user opens the customize overlay.
# They are independent: changing the font size does not move the
# panels around, and turning the animation off does not change the
# colour. Missing/garbage values fall back to defaults.
#
# "ui_mode" controls how much of the chrome is shown:
#   compact   -- single column, small panels, hud only when needed
#   normal    -- current 3-column layout (left sysmonitor / centre hud / right log)
#   expanded  -- wider content panel, larger fonts, more breathing room
#
# "hud_anchor" controls where the face/reactor sits when an active task is
# running. The HUD still animates smoothly between idle (centred) and
# active positions, and any combination of (ui_mode, hud_anchor, hud_size_*)
# is valid.
UI_MODES        = ("compact", "normal", "expanded")
HUD_ANCHORS     = ("center", "topleft", "topright")
PANEL_MODES     = ("auto", "always", "off")    # task/activity overlay display

# Bounded numeric ranges -- every getter clamps. These are deliberately
# generous so an aggressive user can shrink / enlarge things to taste, but
# a typo cannot produce a UI that is invisible or off-screen.
_MIN_SCALE,  _MAX_SCALE  = 0.65, 1.60
_MIN_FONTSZ, _MAX_FONTSZ = 0.75, 1.75
_MIN_WIDTH,  _MAX_WIDTH  = 280,  900
_MIN_HUDSZ,  _MAX_HUDSZ  = 0.55, 1.60
_MIN_ANIM,   _MAX_ANIM   = 0.0,  2.0          # 0 = off, 1 = normal, 2 = extra
_MIN_ALPHA,  _MAX_ALPHA  = 0.40, 1.00


def _clamp(value, lo, hi, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(default)
    if v != v:        # NaN
        return float(default)
    return max(lo, min(hi, v))


def get_ui_mode() -> str:
    """UI density: 'compact' | 'normal' | 'expanded'. Default 'normal'."""
    v = str(load_api_keys().get("ui_mode", "normal") or "normal").strip().lower()
    return v if v in UI_MODES else "normal"


def save_ui_mode(mode: str) -> bool:
    v = str(mode or "").strip().lower()
    v = v if v in UI_MODES else "normal"
    return _patch_config(ui_mode=v)


def get_ui_scale() -> float:
    """Global UI scale (1.0 = the layout as authored)."""
    return _clamp(load_api_keys().get("ui_scale", 1.0),
                  _MIN_SCALE, _MAX_SCALE, 1.0)


def save_ui_scale(value) -> bool:
    return _patch_config(ui_scale=_clamp(value, _MIN_SCALE, _MAX_SCALE, 1.0))


def get_font_scale() -> float:
    """Font-size multiplier (1.0 = default Courier sizes)."""
    return _clamp(load_api_keys().get("font_scale", 1.0),
                  _MIN_FONTSZ, _MAX_FONTSZ, 1.0)


def save_font_scale(value) -> bool:
    return _patch_config(font_scale=_clamp(value, _MIN_FONTSZ, _MAX_FONTSZ, 1.0))


def get_panel_width() -> int:
    """Side-panel width in pixels (left + right are the same width)."""
    try:
        v = int(load_api_keys().get("panel_width", 320))
    except (TypeError, ValueError):
        return 320
    return max(_MIN_WIDTH, min(_MAX_WIDTH, v))


def save_panel_width(value) -> bool:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 320
    return _patch_config(panel_width=max(_MIN_WIDTH, min(_MAX_WIDTH, v)))


def get_hud_size() -> float:
    """Face/reactor size multiplier (1.0 = the size the HUD was authored at)."""
    return _clamp(load_api_keys().get("hud_size", 1.0),
                  _MIN_HUDSZ, _MAX_HUDSZ, 1.0)


def save_hud_size(value) -> bool:
    return _patch_config(hud_size=_clamp(value, _MIN_HUDSZ, _MAX_HUDSZ, 1.0))


def get_animation_enabled() -> bool:
    """False freezes the face/reactor animation entirely (the waveform still
    pulses from the real audio level)."""
    return bool(load_api_keys().get("animation_enabled", True))


def save_animation_enabled(enabled: bool) -> bool:
    return _patch_config(animation_enabled=bool(enabled))


def get_animation_speed() -> float:
    """Animation speed multiplier (1.0 = default cadence). 0 freezes it."""
    return _clamp(load_api_keys().get("animation_speed", 1.0),
                  _MIN_ANIM, _MAX_ANIM, 1.0)


def save_animation_speed(value) -> bool:
    return _patch_config(animation_speed=_clamp(value, _MIN_ANIM, _MAX_ANIM, 1.0))


def get_hud_transparency() -> float:
    """Panel alpha (1.0 = opaque). Used for side-panels + content area."""
    return _clamp(load_api_keys().get("hud_transparency", 1.0),
                  _MIN_ALPHA, _MAX_ALPHA, 1.0)


def save_hud_transparency(value) -> bool:
    return _patch_config(hud_transparency=_clamp(
        value, _MIN_ALPHA, _MAX_ALPHA, 1.0))


def get_hud_anchor() -> str:
    """Where the face/reactor sits while a task is active. 'center' = idle
    position (always used when no task is running)."""
    v = str(load_api_keys().get("hud_anchor", "topleft") or "topleft").strip().lower()
    return v if v in HUD_ANCHORS else "topleft"


def save_hud_anchor(anchor: str) -> bool:
    v = str(anchor or "").strip().lower()
    v = v if v in HUD_ANCHORS else "topleft"
    return _patch_config(hud_anchor=v)


def get_task_overlay_mode() -> str:
    """When to show the task/activity overlay chip:
       auto   = only while a task is running
       always = always visible (minimised to a chip otherwise)
       off    = never show the floating chip (use the inline panel only)"""
    v = str(load_api_keys().get("task_overlay_mode", "auto") or "auto").strip().lower()
    return v if v in PANEL_MODES else "auto"


def save_task_overlay_mode(mode: str) -> bool:
    v = str(mode or "").strip().lower()
    v = v if v in PANEL_MODES else "auto"
    return _patch_config(task_overlay_mode=v)


def get_visualizer_style() -> str:
    """Reactor-core visualizer style:
       classic   = rings + spokes (default, the historical MARK LIV look)
       minimal   = just the outer ring + spectrum ring (calmer)
       spectrum  = full radial bars, no decorative arcs"""
    v = str(load_api_keys().get("visualizer_style", "classic") or "classic").strip().lower()
    return v if v in ("classic", "minimal", "spectrum") else "classic"


def save_visualizer_style(value: str) -> bool:
    v = str(value or "").strip().lower()
    v = v if v in ("classic", "minimal", "spectrum") else "classic"
    return _patch_config(visualizer_style=v)


def get_log_max_lines() -> int:
    """Maximum number of lines kept in the activity log."""
    try:
        v = int(load_api_keys().get("log_max_lines", 600))
    except (TypeError, ValueError):
        return 600
    return max(50, min(5000, v))


def save_log_max_lines(value) -> bool:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 600
    return _patch_config(log_max_lines=max(50, min(5000, v)))


def get_show_debug_log() -> bool:
    """Whether the activity log surfaces raw plumbing lines. Off by default --
    the HUD is built for readable conversation, not console output.
    Turning it on restores the boot transcript + plugin loader lines."""
    return bool(load_api_keys().get("show_debug_log", False))


def save_show_debug_log(enabled: bool) -> bool:
    return _patch_config(show_debug_log=bool(enabled))


def get_snap_hud_on_task() -> bool:
    """Whether to smoothly move the HUD into the corner when a longer task
    starts (so the workspace is free). The HUD still moves back to centre
    the moment the task finishes."""
    return bool(load_api_keys().get("snap_hud_on_task", True))


def save_snap_hud_on_task(enabled: bool) -> bool:
    return _patch_config(snap_hud_on_task=bool(enabled))


def get_auto_task_delay_ms() -> int:
    """How long a task has to be running before the HUD snaps into the corner.
    Short tasks (typos, instant replies) never trigger the snap. 0 disables
    the delay entirely."""
    try:
        v = int(load_api_keys().get("auto_task_delay_ms", 900))
    except (TypeError, ValueError):
        return 900
    return max(0, min(10000, v))


def save_auto_task_delay_ms(value) -> bool:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 900
    return _patch_config(auto_task_delay_ms=max(0, min(10000, v)))


# One helper that bundles the whole GUI block so callers can fetch /
# persist everything in a single round-trip (used by the customize overlay).
def get_gui_settings() -> dict:
    return {
        "ui_mode":           get_ui_mode(),
        "mini_mode": bool(load_api_keys().get("mini_mode", False)),
        "standard_german_voice": bool(load_api_keys().get("standard_german_voice", True)),
        "gaming_mode": bool(load_api_keys().get("gaming_mode", False)),
        "ui_scale":          get_ui_scale(),
        "font_scale":        get_font_scale(),
        "panel_width":       get_panel_width(),
        "hud_size":          get_hud_size(),
        "hud_anchor":        get_hud_anchor(),
        "hud_transparency":  get_hud_transparency(),
        "animation_enabled": get_animation_enabled(),
        "animation_speed":   get_animation_speed(),
        "task_overlay_mode": get_task_overlay_mode(),
        "visualizer_style":  get_visualizer_style(),
        "log_max_lines":     get_log_max_lines(),
        "show_debug_log":    get_show_debug_log(),
        "snap_hud_on_task":  get_snap_hud_on_task(),
        "auto_task_delay_ms": get_auto_task_delay_ms(),
    }


def save_gui_settings(values: dict) -> bool:
    """Persist the GUI block. Unknown keys are ignored; bad values fall back
    to defaults; partial updates preserve the rest. Refuses to write if the
    existing config is corrupt (same rule as every other saver here)."""
    if not isinstance(values, dict) or not values:
        return True
    with _CONFIG_LOCK:
        data, intact = _read_config_state()
        if not intact:
            return _refuse_corrupt_write()
        for key in ("mini_mode", "gaming_mode", "standard_german_voice"):
            data[key] = bool(values.get(key, data.get(key, key == "standard_german_voice")))
        data["ui_mode"]           = str(values.get("ui_mode", get_ui_mode())).strip().lower()
        if data["ui_mode"] not in UI_MODES:
            data["ui_mode"] = "normal"
        data["ui_scale"]          = _clamp(values.get("ui_scale", get_ui_scale()),
                                            _MIN_SCALE, _MAX_SCALE, 1.0)
        data["font_scale"]        = _clamp(values.get("font_scale", get_font_scale()),
                                            _MIN_FONTSZ, _MAX_FONTSZ, 1.0)
        data["panel_width"]       = max(_MIN_WIDTH, min(_MAX_WIDTH,
                                            int(values.get("panel_width", get_panel_width()))))
        data["hud_size"]          = _clamp(values.get("hud_size", get_hud_size()),
                                            _MIN_HUDSZ, _MAX_HUDSZ, 1.0)
        anchor = str(values.get("hud_anchor", get_hud_anchor())).strip().lower()
        data["hud_anchor"]        = anchor if anchor in HUD_ANCHORS else "topleft"
        data["hud_transparency"]  = _clamp(values.get("hud_transparency", get_hud_transparency()),
                                            _MIN_ALPHA, _MAX_ALPHA, 1.0)
        data["animation_enabled"] = bool(values.get("animation_enabled",
                                                     get_animation_enabled()))
        data["animation_speed"]   = _clamp(values.get("animation_speed", get_animation_speed()),
                                            _MIN_ANIM, _MAX_ANIM, 1.0)
        panel = str(values.get("task_overlay_mode", get_task_overlay_mode())).strip().lower()
        data["task_overlay_mode"] = panel if panel in PANEL_MODES else "auto"
        viz = str(values.get("visualizer_style", get_visualizer_style())).strip().lower()
        data["visualizer_style"]  = viz if viz in ("classic", "minimal", "spectrum") else "classic"
        data["log_max_lines"]     = max(50, min(5000,
                                            int(values.get("log_max_lines", get_log_max_lines()))))
        data["show_debug_log"]    = bool(values.get("show_debug_log", get_show_debug_log()))
        data["snap_hud_on_task"]  = bool(values.get("snap_hud_on_task", get_snap_hud_on_task()))
        data["auto_task_delay_ms"] = max(0, min(10000,
                                            int(values.get("auto_task_delay_ms", get_auto_task_delay_ms()))))
        _write_config_unlocked(data)
        return True


def get_mini_geometry():
    from core.hud_layout import valid_placement
    return valid_placement(load_api_keys().get("mini_geometry"))


def save_mini_geometry(value):
    from core.hud_layout import valid_placement
    placement = valid_placement(value)
    return _patch_config(mini_geometry=placement) if placement else False
