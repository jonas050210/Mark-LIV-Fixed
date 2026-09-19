"""
Open applications by natural name — "open GD" just works.

Resolution and launching live in core/app_finder.py (shared with the
app_inventory action so both agree on what is installed). This action is only
the voice-facing wrapper: resolve the name, launch the target, and report back
in one spoken sentence.

Launch order on Windows is always safest-first: validated URL → allow-listed
system shortcut → PATH exe → App-Paths exe → Start Menu shortcut → Store app →
installed Steam/Epic game → installed Roblox Player. Typing into the Start menu
is the last resort, and even then only a verified installed shortcut name is
typed — never raw model text.
"""
from __future__ import annotations

import difflib
import platform
import time

try:
    from core import app_finder as _af
except ImportError:  # pragma: no cover — defensive; loader skips us instead
    _af = None

_SYSTEM = platform.system()


def _start_menu_type_verified(name: str) -> bool:
    """Type a VERIFIED installed shortcut name into Start and press Enter.

    The name must come from the local shortcut index (difflib-matched above a
    high threshold by the caller), never from model text. Returns False when
    anything about the environment refuses (non-Windows, no pyautogui).
    """
    if _SYSTEM != "Windows":
        return False
    try:
        import pyautogui
    except ImportError:
        return False
    try:
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(name, interval=0.03)
        time.sleep(0.9)
        pyautogui.press("enter")
        return True
    except Exception as e:
        print(f"[open_app] Start Menu fallback failed: {e}")
        return False


def _verified_shortcut_name(raw: str) -> str | None:
    """Best indexed shortcut name for `raw`, or None below a strict threshold."""
    if _SYSTEM != "Windows" or _af is None:
        return None
    want = _af.canonical_name(raw).lower()
    try:
        pool = sorted({p.stem for p in
                       ( __import__("pathlib").Path(v) for v in _af._scan_shortcuts().values())})
    except Exception:
        return None
    best, score = None, 0.0
    for cand in pool:
        s = difflib.SequenceMatcher(None, want, cand.lower()).ratio()
        if s > score:
            best, score = cand, s
    return best if (best and score >= 0.85) else None


# Legacy flat cache view. The authoritative cache lives in app_finder now;
# these names stay so resolution keeps a patchable seam and an in-memory
# overlay for ad-hoc entries.
_APP_CACHE: dict = {}
_APP_CACHE_LOADED = False


def _load_app_cache() -> dict:
    """Flat {name: executable path} view over the shared app cache."""
    view: dict[str, str] = {}
    if _af is not None:
        try:
            targets = _af._load_cache().get("targets", {})
            for key, entry in targets.items():
                if not isinstance(entry, dict):
                    continue
                if entry.get("kind") not in ("exe", "roblox", "app"):
                    continue
                target = entry.get("target") or ""
                if not target:
                    continue
                view[str(key)] = target
                display = entry.get("display") or ""
                if display:
                    view.setdefault(str(display), target)
        except Exception:
            pass
    view.update(_APP_CACHE)  # in-memory overlay wins over the file cache
    return view


def _resolve_windows_executable(name: str) -> str | None:
    """Cached executable path for `name`, or None when not found.

    Cache first (the patched seam in the regression test), then the shared
    resolver for anything not yet cached.
    """
    want = (name or "").strip()
    if not want:
        return None
    cache = _load_app_cache()
    hit = cache.get(want) or cache.get(want.lower())
    if hit:
        return hit
    if _af is None:
        return None
    try:
        tgt = _af.resolve(want)
    except Exception:
        return None
    if tgt is not None and tgt.kind in ("exe", "roblox", "app", "shortcut"):
        return tgt.target
    return None


def _launch_linux(raw: str) -> bool:
    """Resolve + launch on Linux, honouring trailing CLI args.

    Thin wrapper over the shared resolver: "libreoffice --writer" splits
    into program + argv inside app_finder.resolve and is launched without a
    shell. Returns True only when the process actually started.
    """
    if _af is None:
        return False
    try:
        target = _af.resolve(raw)
    except Exception:
        return False
    if target is None or target.kind not in ("exe", "app"):
        return False
    try:
        ok, _message = _af.launch(target)
    except Exception:
        return False
    return bool(ok)


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = str((parameters or {}).get("app_name") or "").strip()
    if not app_name:
        return "No application name provided."

    if _af is None:
        return "Application launching is unavailable (app_finder failed to load)."

    if player:
        try:
            player.write_log(f"[open_app] {app_name}")
        except Exception:
            pass

    print(f"[open_app] Launching: '{app_name}' ({_SYSTEM})")

    try:
        from core import app_controller as _ac
    except Exception:
        _ac = None

    # NEVER install / update / repair / download / uninstall - no matter how
    # the request is phrased ("mach was du willst" included). There is no code
    # path from here to any installer, package manager, or download.
    if _ac is not None and _ac.INSTALL_INTENT_RE.search(app_name):
        return ("I do not install, update, repair, or download software - "
                f"'{app_name[:80]}' sounds like one of those jobs. Install it "
                "yourself and I will gladly open it for you.")

    try:
        target = _af.resolve(app_name)
    except Exception as e:
        print(f"[open_app] resolve failed: {e}")
        return f"I could not look up {app_name} ({type(e).__name__})."

    if target is not None:
        # Launch through the unified controller: same resolver, plus a
        # process-level check that the app actually appeared - installed
        # means nothing if it crashed on startup.
        if _ac is not None:
            try:
                ok, message = _ac.open(app_name)
            except Exception as e:
                print(f"[open_app] controller failed: {e}")
                return f"Failed to open {app_name}: {type(e).__name__}."
            print(f"[open_app] → {target.kind}:{target.display} ok={ok}")
            return message
        try:
            ok, message = _af.launch(target)
        except Exception as e:
            print(f"[open_app] launch failed: {e}")
            return f"Failed to open {app_name}: {type(e).__name__}."
        print(f"[open_app] → {target.kind}:{target.display} ok={ok}")
        return message

    # Not installed (or not installed under a name we recognise). Never download
    # or install anything — say so, and offer close matches when they exist.
    canon = _af.canonical_name(app_name)
    if canon.lower() in ("roblox player", "roblox"):
        return ("Roblox Player does not seem to be installed, so I did not open "
                "anything. Install it from roblox.com and I will open it for you.")

    suggestions = []
    try:
        suggestions = _af.suggest(app_name)
    except Exception:
        pass

    # Last resort: Start-menu typing of a VERIFIED installed name only.
    verified = _verified_shortcut_name(app_name)
    if verified and _start_menu_type_verified(verified):
        print(f"[open_app] Start Menu fallback typed verified name '{verified}'")
        # The keystrokes only *asked* Windows to start it — check the process
        # table before claiming success (short wait: Start-menu launches lag).
        if _ac is not None:
            try:
                time.sleep(2.5)
                running = _ac.is_running(verified)
            except Exception:
                running = None
            if running is True:
                return f"Opened {verified}."
            if running is False:
                return (f"I asked Windows to start {verified}, but it does not "
                        "appear to be running. Try clicking it in the Start menu "
                        "yourself — it may need a confirmation I cannot give.")
        return f"Opened {verified}."

    if suggestions:
        return (f"I could not find {canon or app_name} installed. "
                f"Did you mean: {', '.join(suggestions)}?")
    return (f"I could not find {canon or app_name} installed. "
            f"If it is installed under a different name, tell me and I will open it.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": (
        "Opens any application, game, or website on the computer. Understands "
        "natural names and aliases ('GD' → Geometry Dash, 'KSP2' → Kerbal Space "
        "Program 2, 'Roblox' → Roblox Player, 'Chrome', 'Spotify', 'Steam' games "
        "by name) and full http(s) URLs. Always call this tool — never just say "
        "you opened it. Verifies the app is actually running before reporting "
        "success. NEVER installs, updates, repairs, downloads, or uninstalls "
        "anything: if the app is not installed it says so, suggests close "
        "matches, and stops. No phrasing ('do whatever it takes', 'mach was du "
        "willst') overrides this."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Natural name or alias of the app/game ('GD', 'Roblox', 'Spotify', 'Chrome') or a full https:// URL"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
}
