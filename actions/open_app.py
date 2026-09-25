import difflib
import platform
import time

from core.app_index import (
    LaunchError,
    build_index,
    is_uri,
    launch as launch_app,
    launch_uri,
    load_index,
    meaningful_terms,
    normalize_key,
    resolve as resolve_app,
    score_entry,
)
from core.shortcut_store import resolve as resolve_shortcut

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_SYSTEM = platform.system()

_ALLOWED_STATES = {
    "normal", "maximized", "fullscreen", "minimized",
    "left", "right", "top", "bottom",
}

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


def _alias_target(raw: str) -> str:
    """Rewrite a spoken name to this platform's canonical application name.

    Matching is exact first, then whole-word, and only then a bounded fuzzy
    match.  The previous substring test matched 'code' inside 'vscode' and
    'git' inside 'digital', so the first alias in declaration order won rather
    than the best one.
    """
    key = normalize_key(raw)
    if not key:
        return raw

    entry = _APP_ALIASES.get(key)
    if entry is not None:
        return entry.get(_SYSTEM, raw)

    wanted = meaningful_terms(raw)
    if wanted:
        for alias_key, os_map in _APP_ALIASES.items():
            alias_terms = set(normalize_key(alias_key).split())
            if wanted == alias_terms:
                return os_map.get(_SYSTEM, raw)
        for alias_key, os_map in _APP_ALIASES.items():
            alias_terms = set(normalize_key(alias_key).split())
            if alias_terms and alias_terms <= wanted:
                return os_map.get(_SYSTEM, raw)

    best_key, best_ratio = "", 0.0
    for alias_key in _APP_ALIASES:
        ratio = difflib.SequenceMatcher(None, key, normalize_key(alias_key)).ratio()
        if ratio > best_ratio:
            best_key, best_ratio = alias_key, ratio
    if best_ratio >= 0.82:
        return _APP_ALIASES[best_key].get(_SYSTEM, raw)
    return raw


def _normalize(raw: str) -> str:
    """Backwards-compatible alias resolution used by callers and tests."""
    return _alias_target(raw)


def _resolve_candidates(*queries: str) -> tuple[list, bool]:
    """Match an application against the installed-app index.

    Returns the ranked candidates and whether the index had to be rebuilt.  A
    stale cache is the normal reason a freshly installed application is not
    found, so one silent rebuild is attempted before reporting failure.
    """
    wanted = [query for query in queries if str(query or "").strip()]
    if not wanted:
        return [], False
    try:
        entries = load_index()
    except Exception as exc:
        print(f"[open_app] application index unavailable ({type(exc).__name__}).")
        return [], False

    for query in wanted:
        matches = resolve_app(query, entries=entries)
        if matches:
            return matches, False

    try:
        entries = load_index(refresh=True)
    except Exception as exc:
        print(f"[open_app] application index refresh failed ({type(exc).__name__}).")
        return [], True
    for query in wanted:
        matches = resolve_app(query, entries=entries)
        if matches:
            return matches, True
    return [], True


def _launch_resolved(entry) -> tuple[bool, int | None, str]:
    """Start one indexed application.  Never simulates keyboard input."""
    try:
        pid = launch_app(entry)
        return True, pid, ""
    except LaunchError as exc:
        return False, None, str(exc)
    except Exception as exc:
        return False, None, f"{type(exc).__name__}"


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
        print(f"[open_app] window detection unavailable ({type(exc).__name__}).")
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
        print(f"[open_app] could not focus existing window ({type(exc).__name__}).")
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
    if any(
        parameters.get(key) is True
        for key in ("new_instance", "second_instance", "another_instance")
    ):
        return True
    text = _normalised_window_text(app_name)
    phrases = (
        "another roblox", "second roblox", "new roblox", "roblox instance",
        "another instance of roblox", "second instance of roblox",
    )
    return any(phrase in text for phrase in phrases)


def _wait_for_pid_window(pid: int | None, timeout: float = 12.0):
    """Wait until the launched process owns a visible window."""
    if not pid:
        return []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            from core.window_manager import windows_for_pid
            found = windows_for_pid(pid)
        except Exception:
            return []
        if found:
            return found
        time.sleep(0.2)
    return []


def _await_launched_window(app_name: str, normalized: str, pid: int | None,
                           existing_keys: set[tuple], timeout: float = 12.0):
    """Identify the window a launch produced, by pid first and title second.

    Replaces the old fixed ``time.sleep`` guesses: a fast machine continues as
    soon as the window exists, a slow one still gets the full budget, and a
    launch that produces nothing is reported instead of being called a success.
    """
    by_pid = _wait_for_pid_window(pid, timeout=timeout)
    if by_pid:
        return by_pid[0]
    _, new_windows = _wait_for_windows(app_name, normalized, 1, existing_keys, timeout=timeout)
    return new_windows[-1] if new_windows else None


def _placement_request(parameters: dict) -> tuple[int | None, str]:
    monitor = parameters.get("monitor")
    if monitor in ("", None):
        monitor_index = None
    else:
        try:
            monitor_index = int(monitor)
        except (TypeError, ValueError):
            monitor_index = None
    state = str(parameters.get("state") or "").casefold().strip()
    return monitor_index, state


def _apply_placement(window, monitor_index: int | None, state: str,
                     *, focus: bool) -> tuple[bool, str]:
    """Move a window to the requested monitor/state and verify the outcome."""
    if monitor_index is None and not state:
        return True, ""
    try:
        from core.window_manager import monitor_for, place_window, window_on_monitor
    except Exception as exc:
        return False, f"window placement is unavailable ({type(exc).__name__})"

    monitor = None
    if monitor_index is not None:
        try:
            monitor = monitor_for(monitor_index)
        except ValueError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"monitor lookup failed ({type(exc).__name__})"

    try:
        placed = place_window(window, monitor, state or "normal", focus=focus)
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"placement failed ({type(exc).__name__})"

    if monitor is not None and not window_on_monitor(placed, monitor):
        return False, f"I could not verify the move to monitor {monitor.index}"
    return True, ""


def _placement_summary(monitor_index: int | None, state: str) -> str:
    parts = []
    if state:
        parts.append({"fullscreen": "in fullscreen", "maximized": "maximised",
                      "minimized": "minimised"}.get(state, f"snapped {state}"))
    if monitor_index is not None:
        parts.append(f"on monitor {monitor_index}")
    return (" " + " ".join(parts)) if parts else ""


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    parameters = parameters if isinstance(parameters, dict) else {}
    app_name = str(parameters.get("app_name", ""))[:160].strip()

    if not app_name:
        return "No application name provided."
    if len(app_name) > 160 or any(ord(ch) < 32 for ch in app_name):
        return "That application name is invalid or too long."

    if _SYSTEM not in ("Windows", "Darwin", "Linux"):
        return f"Unsupported operating system: {_SYSTEM}"

    foreground = parameters.get("foreground")
    foreground = True if foreground is None else bool(foreground)
    monitor_index, state = _placement_request(parameters)
    if state and state not in _ALLOWED_STATES:
        return f"state must be one of: {', '.join(sorted(_ALLOWED_STATES))}."

    shortcut_target = resolve_shortcut(app_name)
    normalized = _alias_target(shortcut_target)
    explicit_second = _explicit_second_instance(parameters, app_name)
    is_roblox = _is_roblox(app_name) or _is_roblox(shortcut_target) or _is_roblox(normalized)
    existing = _matching_windows(app_name, normalized)

    if player:
        player.write_log("[open_app] Launch requested")

    # A plain URL or shell URI is not an installed application; hand it to the
    # platform handler directly rather than searching the index for it.
    if is_uri(shortcut_target) or is_uri(normalized):
        target = shortcut_target if is_uri(shortcut_target) else normalized
        try:
            launch_uri(target)
        except Exception as exc:
            return f"I could not open that link ({type(exc).__name__})."
        return f"Opened {app_name}."

    # A normal open is idempotent.  In particular, do not create a duplicate
    # Roblox client just because the launcher was called a second time.
    if existing and not explicit_second:
        window = existing[0]
        if monitor_index is not None or state:
            ok, detail = _apply_placement(window, monitor_index, state, focus=foreground)
            if not ok:
                return f"{app_name} is already open, but {detail}."
            return f"{app_name} was already open; moved it{_placement_summary(monitor_index, state)}."
        if not foreground:
            return f"{app_name} is already open; left it in the background."
        if _focus_window(window):
            return f"{app_name} is already open; switched to it."
        return f"{app_name} is already open, but I could not focus its window."

    candidates, refreshed = _resolve_candidates(normalized, shortcut_target, app_name)
    if not candidates:
        suggestions = _nearby_names(app_name, normalized)
        hint = f" Did you mean: {suggestions}?" if suggestions else ""
        scanned = " I rescanned the installed applications first." if refreshed else ""
        return (
            f"I could not find an installed application called '{app_name}'.{scanned}{hint}"
        )

    if len(candidates) > 1 and not _confident_match(candidates, normalized, app_name):
        names = ", ".join(entry.name for entry in candidates[:4])
        return (
            f"'{app_name}' matches more than one installed application: {names}. "
            "Tell me which one you mean."
        )

    entry = candidates[0]
    previous_foreground = None
    if not foreground:
        try:
            from core.window_manager import foreground_window
            previous_foreground = foreground_window()
        except Exception:
            previous_foreground = None

    print(f"[open_app] Launching an indexed application ({_SYSTEM}, {entry.source}).")
    existing_keys = {_window_key(window) for window in existing}

    try:
        started, pid, failure = _launch_resolved(entry)
        if not started:
            return f"I could not start {entry.name}: {failure or 'the launch was refused'}."

        if is_roblox and explicit_second:
            return _second_roblox_instance(app_name, normalized, existing, existing_keys)

        window = _await_launched_window(app_name, normalized, pid, existing_keys)
        if window is None:
            return (
                f"I started {entry.name}, but no window appeared within the wait window. "
                "It may still be loading or it may have failed to start."
            )

        if monitor_index is not None or state:
            ok, detail = _apply_placement(window, monitor_index, state, focus=foreground)
            if not ok:
                return f"Opened {entry.name}, but {detail}."

        if not foreground:
            try:
                from core.window_manager import operate, restore_foreground
                if previous_foreground is not None:
                    restore_foreground(previous_foreground)
                elif state != "minimized":
                    operate(window, "minimize")
            except Exception:
                pass
            return f"Opened {entry.name} in the background{_placement_summary(monitor_index, state)}."

        return f"Opened {entry.name}{_placement_summary(monitor_index, state)}."
    except Exception as exc:
        print(f"[open_app] Launch failed ({type(exc).__name__}).")
        return f"Failed to open {app_name} ({type(exc).__name__})."


def _confident_match(candidates, *queries: str) -> bool:
    """True when the top candidate is a clear winner rather than a fuzzy guess."""
    best = candidates[0]
    for query in queries:
        if not str(query or "").strip():
            continue
        if score_entry(query, best) >= 92.0:
            return True
    if len(candidates) < 2:
        return True
    top = max(score_entry(query, best) for query in queries if str(query or "").strip())
    second = max(
        score_entry(query, candidates[1]) for query in queries if str(query or "").strip()
    )
    return top - second >= 15.0


def _nearby_names(*queries: str, limit: int = 3) -> str:
    """Closest installed application names, for an honest 'did you mean'."""
    try:
        entries = load_index()
    except Exception:
        return ""
    scored = []
    for entry in entries:
        best = max((difflib.SequenceMatcher(None, normalize_key(query), entry.key).ratio()
                    for query in queries if str(query or "").strip()), default=0.0)
        if best >= 0.45:
            scored.append((best, entry.name))
    scored.sort(key=lambda item: -item[0])
    return ", ".join(name for _, name in scored[:limit])


def _second_roblox_instance(app_name: str, normalized: str, existing, existing_keys: set[tuple]) -> str:
    """Verify an explicitly requested second Roblox client and place it."""
    desired_count = max(1, len(existing) + 1)
    matches, new_windows = _wait_for_windows(app_name, normalized, desired_count, existing_keys)
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
        return f"A second Roblox window opened, but I could not move it: {type(exc).__name__}"
    verified, _ = _wait_for_windows(app_name, normalized, desired_count, set())
    moved = next(
        (window for window in verified if _window_key(window) == _window_key(new_window)),
        None,
    )
    if len(verified) < desired_count or moved is None:
        return "A second Roblox window was launched, but I could not verify it after moving it."
    if not _window_on_monitor(moved, target_monitor):
        return "A second Roblox window opened, but I could not verify its opposite-monitor placement."
    return f"Opened a second Roblox window on monitor {target_monitor.index}."


def refresh_app_index(parameters=None, response=None, player=None, session_memory=None) -> str:
    """Rescan installed applications; used after installing something new."""
    entries = build_index()
    if not entries:
        return "I could not build an application index on this system."
    return f"Rebuilt the application index: {len(entries)} installed applications found."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": (
        "Opens or focuses an application, game, folder, or URL using the indexed list of "
        "installed applications. Never simulates the Start menu or keyboard input. Ordinary "
        "opens focus an existing app instead of duplicating it. Set foreground false to start "
        "or leave an app in the background. Use monitor and state to place the window, for "
        "example monitor 2 with state fullscreen. Set new_instance true only when the user "
        "explicitly asks for another instance; for Roblox this attempts a second window, moves "
        "it to the opposite monitor when possible, and verifies the result."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "maxLength": 160,
                "description": "Name of the application (e.g. 'WhatsApp', 'Chrome', 'Roblox')"
            },
            "foreground": {
                "type": "BOOLEAN",
                "description": "Default true. Set false when the user says to open it in the background or without stealing focus."
            },
            "monitor": {
                "type": "INTEGER",
                "description": "1-based monitor number to place the window on, when the user names one."
            },
            "state": {
                "type": "STRING",
                "enum": ["normal", "maximized", "fullscreen", "minimized", "left", "right", "top", "bottom"],
                "maxLength": 16,
                "description": "Window state after opening: fullscreen, maximized, minimized, or a snap side."
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
