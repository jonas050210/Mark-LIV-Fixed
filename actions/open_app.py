import time
import subprocess
import platform
import shutil

from core.shortcut_store import resolve as resolve_shortcut

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "roblox":             {"Windows": "RobloxPlayerBeta.exe",     "Darwin": "Roblox",              "Linux": "roblox"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw  

def _launch_windows(app_name: str) -> bool:

    if shutil.which(app_name) or shutil.which(app_name.split(".")[0]):
        try:
            subprocess.Popen(
                [app_name],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=(_SYSTEM != "Windows"),
            )
            time.sleep(1.5)
            return True
        except Exception as e:
            print(f"[open_app] subprocess failed: {e}")

    if ":" in app_name:
        # URI launch without a shell: never interpolate model text into cmd.exe.
        try:
            import os
            os.startfile(app_name)  # type: ignore[attr-defined]
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        time.sleep(2.5)
        return True
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    return False


def _launch_macos(app_name: str) -> bool:

    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["open", "-a", f"{app_name}.app"],
            capture_output=True, timeout=8
        )
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]

def _launch_linux(app_name: str) -> bool:

    # terminal emulators: try common ones in order
    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            if shutil.which(term):
                try:
                    subprocess.Popen([term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                    return True
                except Exception:
                    continue

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen(
                [binary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(
            ["xdg-open", app_name],
            capture_output=True, timeout=5
        )
        return True
    except Exception:
        pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_name],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def _normalised_window_text(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in str(value or "")).strip()


def _is_roblox(value: str) -> bool:
    return "roblox" in _normalised_window_text(value)


def _matching_windows(requested: str, normalized: str):
    """Return visible windows belonging to an application, without launching it."""
    try:
        from core.window_manager import list_windows
        windows = list_windows()
    except Exception as exc:
        print(f"[open_app] window detection unavailable: {exc}")
        return []

    requested_text = _normalised_window_text(requested)
    normalized_text = _normalised_window_text(normalized)
    terms = {
        term for value in (requested_text, normalized_text)
        for term in value.split()
        if len(term) >= 3 and term not in {"open", "launch", "start", "application", "app"}
    }
    matches = []
    for window in windows:
        title = _normalised_window_text(getattr(window, "title", ""))
        process = _normalised_window_text(getattr(window, "process", ""))
        if _is_roblox(requested) or _is_roblox(normalized):
            matched = "roblox" in title or "roblox" in process
        else:
            matched = any(term in title or term in process for term in terms)
        if matched:
            matches.append(window)
    return matches


def _focus_window(window) -> bool:
    try:
        from core.window_manager import operate
        operate(window, "focus")
        return True
    except Exception as exc:
        print(f"[open_app] could not focus existing window: {exc}")
        return False


def _window_key(window):
    return (
        int(getattr(window, "handle", 0) or 0),
        int(getattr(window, "pid", 0) or 0),
        str(getattr(window, "title", "")),
    )


def _wait_for_windows(requested: str, normalized: str, minimum: int,
                      existing_keys: set[tuple] | None = None, timeout: float = 8.0):
    """Wait briefly for a launched app's window, returning the newest matches."""
    deadline = time.monotonic() + timeout
    existing_keys = existing_keys or set()
    latest = []
    while time.monotonic() < deadline:
        latest = _matching_windows(requested, normalized)
        new_windows = [window for window in latest if _window_key(window) not in existing_keys]
        if len(latest) >= minimum and (not existing_keys or new_windows):
            return latest, new_windows
        time.sleep(0.25)
    latest = _matching_windows(requested, normalized)
    return latest, [window for window in latest if _window_key(window) not in existing_keys]


def _window_on_monitor(window, monitor) -> bool:
    center_x = (int(window.left) + int(window.right)) // 2
    center_y = (int(window.top) + int(window.bottom)) // 2
    return (
        monitor.left <= center_x < monitor.right and
        monitor.top <= center_y < monitor.bottom
    )


def _opposite_monitor(window):
    """Pick a monitor other than the one containing the supplied window."""
    from core.window_manager import list_monitors
    monitors = list_monitors()
    if len(monitors) < 2:
        return None
    center_x = (int(window.left) + int(window.right)) // 2
    center_y = (int(window.top) + int(window.bottom)) // 2
    current = None
    for monitor in monitors:
        if monitor.left <= center_x < monitor.right and monitor.top <= center_y < monitor.bottom:
            current = monitor
            break
    for monitor in monitors:
        if current is None or monitor.index != current.index:
            return monitor
    return None


def _explicit_second_instance(parameters: dict, app_name: str) -> bool:
    if bool(parameters.get("new_instance") or parameters.get("second_instance") or
            parameters.get("another_instance")):
        return True
    text = _normalised_window_text(app_name)
    phrases = (
        "another roblox", "second roblox", "new roblox", "roblox instance",
        "another instance of roblox", "second instance of roblox",
    )
    return any(phrase in text for phrase in phrases)


def _launch(launcher, normalized: str, shortcut_target: str) -> bool:
    if launcher(normalized):
        return True
    if normalized.casefold() != shortcut_target.casefold() and launcher(shortcut_target):
        return True
    return False


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    parameters = parameters or {}
    app_name = str(parameters.get("app_name", "")).strip()

    if not app_name:
        return "No application name provided."
    if len(app_name) > 160 or any(ord(ch) < 32 for ch in app_name):
        return "That application name is invalid or too long."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    shortcut_target = resolve_shortcut(app_name)
    normalized = _normalize(shortcut_target)
    explicit_second = _explicit_second_instance(parameters, app_name)
    is_roblox = _is_roblox(app_name) or _is_roblox(shortcut_target) or _is_roblox(normalized)
    existing = _matching_windows(app_name, normalized)

    if player:
        player.write_log(f"[open_app] {app_name}")

    # A normal open is idempotent.  In particular, do not create a duplicate
    # Roblox client just because the launcher was called a second time.
    if existing and not explicit_second:
        if _focus_window(existing[0]):
            return f"{app_name} is already open; switched to it."
        return f"{app_name} is already open, but I could not focus its window."

    if shortcut_target.casefold() != app_name.casefold():
        print(f"[open_app] Shortcut: '{app_name}' → '{shortcut_target}'")
    print(f"[open_app] Launching: '{app_name}' → '{normalized}' ({_SYSTEM})")

    existing_keys = {_window_key(window) for window in existing}
    try:
        if not _launch(launcher, normalized, shortcut_target):
            return (
                f"Could not confirm that {app_name} launched. "
                f"It may still be loading, or it might not be installed."
            )

        if is_roblox and explicit_second:
            desired_count = max(1, len(existing) + 1)
            matches, new_windows = _wait_for_windows(
                app_name, normalized, desired_count, existing_keys
            )
            if len(matches) < desired_count or not new_windows:
                return (
                    "Roblox did not open a second window. It may prevent multiple "
                    "instances, so I did not claim that a second client started."
                )
            new_window = new_windows[-1]
            try:
                from core.window_manager import move_to_monitor
                target_monitor = _opposite_monitor(existing[0] if existing else matches[0])
                if target_monitor is None:
                    return "A second Roblox window opened, but no opposite monitor was available."
                move_to_monitor(new_window, target_monitor)
            except Exception as exc:
                return f"A second Roblox window opened, but I could not move it: {exc}"
            verified, _ = _wait_for_windows(
                app_name, normalized, desired_count, set()
            )
            moved = next(
                (window for window in verified if _window_key(window) == _window_key(new_window)),
                None,
            )
            if len(verified) < desired_count or moved is None:
                return "A second Roblox window was launched, but I could not verify it after moving it."
            if not _window_on_monitor(moved, target_monitor):
                return "A second Roblox window opened, but I could not verify its opposite-monitor placement."
            return f"Opened a second Roblox window on monitor {target_monitor.index}."

        # For ordinary launches, report success only when the launcher accepted
        # the request.  Roblox receives the stricter second-window verification
        # above; existing applications are handled before launch.
        return f"Opened {app_name}."
    except Exception as exc:
        print(f"[open_app] Error: {exc}")
        return f"Failed to open {app_name}: {exc}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": "Opens or focuses an application, game, folder, or URL. Ordinary opens focus an existing app instead of duplicating it. Set new_instance true only when the user explicitly asks for another instance; for Roblox this attempts a second window, moves it to the opposite monitor when possible, and verifies the result.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Name of the application (e.g. 'WhatsApp', 'Chrome', 'Roblox')"
            },
            "new_instance": {
                "type": "BOOLEAN",
                "description": "Only set true when the user explicitly asks for another or second instance."
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
}
