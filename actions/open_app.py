import platform
import time
from pathlib import Path

from core.app_index import (
    LaunchError,
    record_launch,
    sanitise_arguments,
    is_direct_launch_target,
    is_uri,
    launch as launch_app,
    launch_path,
    launch_uri,
    load_index,
    meaningful_terms,
    normalize_key,
    resolve as resolve_app,
    score_entry,
)
from core.shortcut_store import resolve as resolve_shortcut
from core.text_match import ratio as _text_ratio

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
    "roblox":             {"Windows": "Roblox Player",            "Darwin": "Roblox",              "Linux": "roblox"},
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
        score = _text_ratio(key, normalize_key(alias_key))
        if score > best_ratio:
            best_key, best_ratio = alias_key, score
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


def _note_opened_pages(arguments) -> None:
    """Tell the browser handoff about a web page opened through an argument.

    Opening Chrome with a URL puts a page on screen exactly as a browser_control
    'go_to' would. Recording it here is what lets a following "click the login
    button" resume on that page instead of starting the automation window on a
    blank one.
    """
    for argument in arguments or []:
        try:
            from core import browser_handoff

            if browser_handoff.is_web_url(argument):
                browser_handoff.note(str(argument))
        except Exception:
            return


def _launch_resolved(entry, arguments=None) -> tuple[bool, int | None, str]:
    """Start one indexed application. Never simulates keyboard input."""
    try:
        pid = launch_app(entry, arguments)
        return True, pid, ""
    except LaunchError as exc:
        return False, None, str(exc)
    except Exception as exc:
        return False, None, f"{type(exc).__name__}"


def _launch_with_repair(entry, arguments, *queries: str):
    """Launch once, rebuilding a stale index before one bounded retry.

    Installers frequently replace versioned executables in place. A cached path
    can therefore become invalid even though the application is still installed.
    Only failures that look stale trigger the rescan; permission and OS errors
    are returned directly rather than retried blindly.
    """
    started, pid, failure = _launch_resolved(entry, arguments)
    stale_markers = (
        "no longer installed", "no longer exists", "indexed location",
        "shortcut for", "filenotfounderror",
    )
    if started or not any(marker in str(failure).casefold() for marker in stale_markers):
        return started, pid, failure, entry, False

    try:
        refreshed_entries = load_index(refresh=True)
    except Exception as exc:
        return False, None, f"{failure}; rescan failed ({type(exc).__name__})", entry, True

    replacement = None
    for query in queries:
        matches = resolve_app(query, limit=1, entries=refreshed_entries)
        if matches:
            replacement = matches[0]
            break
    if replacement is None:
        return False, None, f"{failure}; the rescan no longer found the application", entry, True

    started, pid, retry_failure = _launch_resolved(replacement, arguments)
    return started, pid, retry_failure, replacement, True


def _open_direct_path(app_name: str, path: str, arguments: list,
                      monitor_ref, state: str, foreground: bool,
                      cancel_event=None) -> tuple[bool, str]:
    """Launch a personal shortcut/executable the installed-app index cannot see.

    Follows the same wait-for-window, verify, and place pipeline as an
    indexed launch, so a saved Desktop shortcut behaves identically to any
    other application once it is running. Returns (ok, message): ok is False
    for anything short of a verified, correctly placed window.
    """
    label = Path(path).stem or app_name
    print(f"[open_app] Launching direct path '{path}' for '{app_name}'.")
    existing_keys = _visible_window_keys()
    try:
        pid = launch_path(path, arguments)
    except LaunchError as exc:
        return False, f"I could not open '{app_name}' from its saved shortcut: {exc}."
    except Exception as exc:
        return False, f"I could not open '{app_name}' from its saved shortcut ({type(exc).__name__})."

    record_launch(label)
    _note_opened_pages(arguments)
    window = _await_launched_window(
        app_name, label, pid, existing_keys, timeout=30.0, cancel_event=cancel_event,
    )
    if window is None:
        if cancel_event is not None and cancel_event.is_set():
            return False, f"Launching '{app_name}' from its saved shortcut was cancelled."
        return False, (
            f"I started '{app_name}' from its saved shortcut, but no window appeared "
            "within the wait window. It may still be loading or it may have failed to start."
        )
    _remember_launch(label, window)

    resolved_index = None
    if monitor_ref is not None or state:
        ok, detail, resolved_index = _apply_placement(window, monitor_ref, state, focus=foreground)
        if not ok:
            return False, f"Opened '{app_name}' from its saved shortcut, but {detail}."

    if not foreground:
        try:
            from core.window_manager import operate
            if state != "minimized":
                operate(window, "minimize")
        except Exception:
            pass
        return True, (
            f"Opened '{app_name}' from its saved shortcut in the background"
            f"{_placement_summary(resolved_index, state)}."
        )

    return True, f"Opened '{app_name}' from its saved shortcut{_placement_summary(resolved_index, state)}."


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


def _browser_title_match_may_be_a_web_app(existing, *queries: str) -> bool:
    """Return True when a browser-titled window must not suppress a PWA launch.

    A normal Chrome tab titled "Twitch" is indistinguishable from the Twitch
    PWA by title alone. If the installed-app index says the requested name is a
    parameterised web-app shortcut, launching that shortcut is the reliable and
    idempotent operation: Chromium focuses an existing app window or creates it.
    """
    if _SYSTEM != "Windows" or not existing:
        return False
    browser_names = {"chrome", "msedge", "edge", "brave", "vivaldi", "opera"}
    if not any(
        any(name in _normalised_window_text(getattr(window, "process", ""))
            for name in browser_names)
        for window in existing
    ):
        return False
    try:
        entries = load_index()
    except Exception:
        return False
    for query in queries:
        matches = resolve_app(query, limit=1, entries=entries)
        if matches and getattr(matches[0], "source", "") == "webapp":
            return True
    return False


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


def _visible_window_keys() -> set[tuple]:
    try:
        from core.window_manager import list_windows

        return {_window_key(window) for window in list_windows()}
    except Exception:
        return set()


def _wait_for_windows(requested: str, normalized: str, minimum: int,
                      existing_keys: set[tuple] | None = None, timeout: float = 8.0,
                      cancel_event=None):
    """Wait briefly for a launched app's window, returning the newest matches."""
    deadline = time.monotonic() + timeout
    existing_keys = existing_keys or set()
    latest = []
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            break
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


def _wait_for_pid_window(pid: int | None, timeout: float = 12.0, cancel_event=None):
    """Wait until the launched process owns a visible window."""
    if not pid:
        return []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return []
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
                           existing_keys: set[tuple], timeout: float = 20.0,
                           allow_focused_existing: bool = False, cancel_event=None):
    """Identify a launched window without serial PID and title waits.

    PID ownership, name matching and the before/after window snapshot are
    checked together. This matters for shortcuts and Store apps, which often do
    not return the final process id, and for Chromium PWAs whose window title
    can be the current page rather than the installed app's name. A single new
    visible window is accepted after a short settling period; multiple unrelated
    windows are never guessed between.

    A cooperative `cancel_event` is checked every iteration so a cancelled
    multi-app sequence does not have to wait out the full timeout (up to 30s)
    before the step is abandoned.
    """
    started_at = time.monotonic()
    deadline = started_at + max(1.0, timeout)
    single_new_since: float | None = None
    # A window matching the requested name may already have existed just before
    # the launch snapshot (or appear while cancellation is being requested).
    # Give a new process a tiny settling interval before accepting a title-only
    # match; PID ownership remains immediate above. This also makes a cancelled
    # wait deterministic instead of sometimes returning an unrelated Chrome
    # window before its cancellation event can be observed.
    title_match_not_before = started_at + 0.20
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return None
        try:
            from core.window_manager import list_windows, windows_for_pid

            windows = list_windows()
        except Exception:
            windows = []

        if pid:
            try:
                owned = windows_for_pid(pid)
            except Exception:
                owned = []
            if owned:
                return owned[0]

        requested_text = _normalised_window_text(app_name)
        normalized_text = _normalised_window_text(normalized)
        terms = {
            term for value in (requested_text, normalized_text)
            for term in value.split()
            if len(term) >= 3 and term not in {
                "open", "launch", "start", "application", "app",
            }
        }
        new_windows = [window for window in windows if _window_key(window) not in existing_keys]
        matching_all = []
        for window in windows:
            title = _normalised_window_text(getattr(window, "title", ""))
            process = _normalised_window_text(getattr(window, "process", ""))
            if (_is_roblox(app_name) and ("roblox" in title or "roblox" in process)) or any(
                term in title or term in process for term in terms
            ):
                matching_all.append(window)
        matching_new = [
            window for window in matching_all if _window_key(window) not in existing_keys
        ]
        if matching_new and time.monotonic() >= title_match_not_before:
            return matching_new[-1]

        if allow_focused_existing and time.monotonic() - started_at >= 0.75:
            try:
                from core.window_manager import foreground_window

                focused = foreground_window()
            except Exception:
                focused = None
            if focused is not None:
                for window in matching_all:
                    if int(window.handle) == int(focused.handle):
                        return window

        if len(new_windows) == 1:
            if single_new_since is None:
                single_new_since = time.monotonic()
            elif time.monotonic() - single_new_since >= 0.75:
                return new_windows[0]
        else:
            single_new_since = None
        time.sleep(0.2)
    return None


def _placement_request(parameters: dict) -> tuple[int | str | None, str]:
    """Read the requested monitor and state, without resolving the monitor yet.

    The monitor may be a plain number or a semantic name ("primary",
    "secondary", "left", "right", "monitor 2"); resolution against the
    monitors actually connected happens in _apply_placement, where a bad
    reference can be reported instead of silently discarded.
    """
    monitor = parameters.get("monitor")
    if monitor in ("", None) or isinstance(monitor, bool):
        monitor_ref = None
    elif isinstance(monitor, (int, str)):
        monitor_ref = monitor
    else:
        monitor_ref = None
    state = str(parameters.get("state") or "").casefold().strip()
    # Voice "fullscreen" means the Windows maximise control for this app, not
    # F11. The taskbar remains visible; core.window_manager handles the native
    # operation by window handle.
    if state in {"fullscreen", "fulscreen", "full_screen", "full screen", "full"}:
        state = "maximized"
    return monitor_ref, state


def _apply_placement(window, monitor_ref: int | str | None, state: str,
                     *, focus: bool) -> tuple[bool, str, int | None]:
    """Move a window to the requested monitor/state and verify the outcome.

    Returns (ok, detail, resolved_monitor_index). The resolved index is
    reported even on failure when available, so a caller building a status
    message never has to fall back to an unresolved token like "primary".
    """
    if monitor_ref is None and not state:
        return True, "", None
    try:
        from core.window_manager import monitor_for, place_window, window_on_monitor
    except Exception as exc:
        return False, f"window placement is unavailable ({type(exc).__name__})", None

    monitor = None
    if monitor_ref is not None:
        try:
            monitor = monitor_for(monitor_ref)
        except ValueError as exc:
            return False, str(exc), None
        except Exception as exc:
            return False, f"monitor lookup failed ({type(exc).__name__})", None

    try:
        placed = place_window(window, monitor, state or "normal", focus=focus)
    except ValueError as exc:
        return False, str(exc), (monitor.index if monitor else None)
    except Exception as exc:
        return False, f"placement failed ({type(exc).__name__})", (monitor.index if monitor else None)

    if monitor is not None and not window_on_monitor(placed, monitor):
        return False, f"I could not verify the move to monitor {monitor.index}", monitor.index
    return True, "", (monitor.index if monitor else None)


def _placement_summary(monitor_index: int | None, state: str) -> str:
    parts = []
    if state:
        parts.append({"fullscreen": "in fullscreen", "maximized": "maximised",
                      "minimized": "minimised"}.get(state, f"snapped {state}"))
    if monitor_index is not None:
        parts.append(f"on monitor {monitor_index}")
    return (" " + " ".join(parts)) if parts else ""


def _open_app_core(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
    cancel_event=None,
) -> tuple[bool, str]:
    """Do the actual launch/focus/place work; every caller gets a real ok flag.

    `open_app` and `open_app_result` are both thin wrappers around this. It
    exists so that a caller which needs to know whether the launch actually
    succeeded -- such as app_sequence's step runner -- never has to re-derive
    that from English prose, which drifts every time a message is reworded.
    """
    parameters = parameters if isinstance(parameters, dict) else {}
    app_name = str(parameters.get("app_name", ""))[:160].strip()

    if not app_name:
        return False, "No application name provided."
    if len(app_name) > 160 or any(ord(ch) < 32 for ch in app_name):
        return False, "That application name is invalid or too long."

    if _SYSTEM not in ("Windows", "Darwin", "Linux"):
        return False, f"Unsupported operating system: {_SYSTEM}"

    foreground = parameters.get("foreground")
    foreground = True if foreground is None else bool(foreground)
    try:
        arguments = sanitise_arguments(
            parameters.get("arguments", parameters.get("open_with"))
        )
    except ValueError as exc:
        return False, f"I cannot pass that to the application: {exc}."
    monitor_ref, state = _placement_request(parameters)
    if state and state not in _ALLOWED_STATES:
        return False, f"state must be one of: {', '.join(sorted(_ALLOWED_STATES))}."

    if cancel_event is not None and cancel_event.is_set():
        return False, f"Opening {app_name} was cancelled before it started."

    shortcut_target = resolve_shortcut(app_name)
    normalized = _alias_target(shortcut_target)
    explicit_second = _explicit_second_instance(parameters, app_name)
    is_roblox = _is_roblox(app_name) or _is_roblox(shortcut_target) or _is_roblox(normalized)
    existing = _matching_windows(app_name, normalized)
    if _browser_title_match_may_be_a_web_app(
        existing, normalized, shortcut_target, app_name
    ):
        # Let Chromium route the installed app id. A title-only browser match
        # may be an ordinary tab and is not proof that the PWA is open.
        existing = []

    if player:
        player.write_log("[open_app] Launch requested")

    # A plain URL or shell URI is not an installed application; hand it to the
    # platform handler directly rather than searching the index for it.
    if is_uri(shortcut_target) or is_uri(normalized):
        target = shortcut_target if is_uri(shortcut_target) else normalized
        try:
            launch_uri(target)
        except Exception as exc:
            return False, f"I could not open that link ({type(exc).__name__})."
        return True, f"Opened {app_name}."

    # A normal open is idempotent.  In particular, do not create a duplicate
    # Roblox client just because the launcher was called a second time.
    if existing and not explicit_second:
        window = existing[0]
        if monitor_ref is not None or state:
            ok, detail, resolved_index = _apply_placement(window, monitor_ref, state, focus=foreground)
            if not ok:
                return False, f"{app_name} is already open, but {detail}."
            return True, f"{app_name} was already open; moved it{_placement_summary(resolved_index, state)}."
        if not foreground:
            return True, f"{app_name} is already open; left it in the background."
        if _focus_window(window):
            return True, f"{app_name} is already open; switched to it."
        return False, f"{app_name} is already open, but I could not focus its window."

    # A saved shortcut (or the spoken name itself) may point straight at a
    # file the installed-app index never scans, such as a personal .lnk on
    # the Desktop. Launch it directly instead of reporting "not installed".
    direct_target = next(
        (candidate for candidate in (shortcut_target, app_name) if is_direct_launch_target(candidate)),
        None,
    )
    if direct_target:
        return _open_direct_path(
            app_name, direct_target, arguments, monitor_ref, state, foreground,
            cancel_event=cancel_event,
        )

    candidates, refreshed = _resolve_candidates(normalized, shortcut_target, app_name)
    if not candidates:
        suggestions = _nearby_names(app_name, normalized)
        hint = f" Did you mean: {suggestions}?" if suggestions else ""
        scanned = " I rescanned the installed applications first." if refreshed else ""
        return False, (
            f"I could not find an installed application called '{app_name}'.{scanned}{hint}"
        )

    if len(candidates) > 1 and not _confident_match(candidates, normalized, app_name):
        names = ", ".join(entry.name for entry in candidates[:4])
        return False, (
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

    print(
        f"[open_app] Launching '{entry.name}' "
        f"(platform={_SYSTEM}, kind={entry.kind}, source={entry.source})."
    )
    existing_keys = _visible_window_keys()
    if not existing_keys:
        existing_keys = {_window_key(window) for window in existing}

    try:
        started, pid, failure, entry, repaired = _launch_with_repair(
            entry, arguments, normalized, shortcut_target, app_name
        )
        if repaired:
            print(
                f"[open_app] Rebuilt stale index; retry target "
                f"kind={entry.kind}, source={entry.source}."
            )
        if not started:
            repair_note = " after rebuilding the application index" if repaired else ""
            return False, (
                f"I could not start {entry.name}{repair_note}: "
                f"{failure or 'the launch was refused'}."
            )

        if is_roblox and explicit_second:
            return _second_roblox_instance(
                app_name, normalized, existing, existing_keys, cancel_event=cancel_event,
            )

        record_launch(entry.name)
        _note_opened_pages(arguments)
        wait_seconds = 30.0 if entry.kind in {"lnk", "aumid"} else 20.0
        window = _await_launched_window(
            app_name, normalized, pid, existing_keys, timeout=wait_seconds,
            allow_focused_existing=(entry.source == "webapp"),
            cancel_event=cancel_event,
        )
        if window is None:
            if cancel_event is not None and cancel_event.is_set():
                return False, f"Opening {entry.name} was cancelled."
            print(
                f"[open_app] No verified window for '{entry.name}' "
                f"(pid={pid or 'delegated'}, waited={int(wait_seconds)}s)."
            )
            return False, (
                f"I started {entry.name}, but no window appeared within the wait window. "
                "It may still be loading or it may have failed to start."
            )

        _remember_launch(entry.name, window)

        resolved_index = None
        if monitor_ref is not None or state:
            ok, detail, resolved_index = _apply_placement(window, monitor_ref, state, focus=foreground)
            if not ok:
                return False, f"Opened {entry.name}, but {detail}."

        if not foreground:
            try:
                from core.window_manager import operate, restore_foreground
                if previous_foreground is not None:
                    restore_foreground(previous_foreground)
                elif state != "minimized":
                    operate(window, "minimize")
            except Exception:
                pass
            return True, f"Opened {entry.name} in the background{_placement_summary(resolved_index, state)}."

        return True, f"Opened {entry.name}{_placement_summary(resolved_index, state)}."
    except Exception as exc:
        print(f"[open_app] Launch failed ({type(exc).__name__}).")
        return False, f"Failed to open {app_name} ({type(exc).__name__})."


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
    cancel_event=None,
) -> str:
    """Open, focus, or reposition an installed application, saved shortcut, or link.

    Returns prose only, for callers that just relay a message to the user.
    Use `open_app_result` when the caller needs to branch on whether the
    launch actually succeeded.
    """
    _, message = _open_app_core(parameters, response, player, session_memory, cancel_event)
    return message


def open_app_result(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
    cancel_event=None,
) -> tuple[bool, str]:
    """Same launch as `open_app`, but with a real (ok, message) pair.

    `ok` is False for anything short of a verified, correctly placed window --
    including partial outcomes like "opened, but could not verify placement" --
    so a caller such as app_sequence's step runner can decide whether to
    retry without re-parsing English sentences that are free to be reworded.
    """
    return _open_app_core(parameters, response, player, session_memory, cancel_event)


def _remember_launch(name: str, window) -> None:
    """Make an application launch reversible by closing the window it opened.

    Only the window this launch produced is closed, and only if it is still the
    same window, so an undo cannot take down something the user opened since.
    """
    handle = int(getattr(window, "handle", 0) or 0)
    if not handle:
        return

    def _undo() -> str:
        from core import undo as undo_stack
        from core.window_manager import list_windows, operate
        current = next((item for item in list_windows() if item.handle == handle), None)
        if current is None:
            undo_stack.refuse(f"{name} is no longer open, so there is nothing to close.")
        operate(current, "close")
        return f"Closed {name} again."

    try:
        from core.undo import push_undo
        push_undo(f"opening {name}", _undo)
    except Exception:
        pass


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
        best = max((_text_ratio(normalize_key(query), entry.key)
                    for query in queries if str(query or "").strip()), default=0.0)
        if best >= 0.45:
            scored.append((best, entry.name))
    scored.sort(key=lambda item: -item[0])
    return ", ".join(name for _, name in scored[:limit])


def _second_roblox_instance(
    app_name: str, normalized: str, existing, existing_keys: set[tuple], cancel_event=None,
) -> tuple[bool, str]:
    """Verify an explicitly requested second Roblox client and place it."""
    desired_count = max(1, len(existing) + 1)
    matches, new_windows = _wait_for_windows(
        app_name, normalized, desired_count, existing_keys, cancel_event=cancel_event,
    )
    if len(matches) < desired_count or not new_windows:
        return False, (
            "Roblox did not open a second window. It may prevent multiple "
            "instances, so I did not claim that a second client started."
        )
    new_window = new_windows[-1]
    try:
        from core.window_manager import move_to_monitor
        target_monitor = _opposite_monitor(existing[0] if existing else matches[0])
        if target_monitor is None:
            return False, "A second Roblox window opened, but no opposite monitor was available."
        move_to_monitor(new_window, target_monitor)
    except Exception as exc:
        return False, f"A second Roblox window opened, but I could not move it: {type(exc).__name__}"
    verified, _ = _wait_for_windows(app_name, normalized, desired_count, set(), cancel_event=cancel_event)
    moved = next(
        (window for window in verified if _window_key(window) == _window_key(new_window)),
        None,
    )
    if len(verified) < desired_count or moved is None:
        return False, "A second Roblox window was launched, but I could not verify it after moving it."
    if not _window_on_monitor(moved, target_monitor):
        return False, "A second Roblox window opened, but I could not verify its opposite-monitor placement."
    return True, f"Opened a second Roblox window on monitor {target_monitor.index}."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "open_app",
    "description": (
        "Opens or focuses an application, game, folder, or URL using the indexed list of "
        "installed applications. Never simulates the Start menu or keyboard input. Ordinary "
        "opens focus an existing app instead of duplicating it. Set foreground false to start "
        "or leave an app in the background. Use monitor and state to place the window, for "
        "example monitor 'primary' with state fullscreen, or monitor '2' with state left. "
        "To open and place several applications in one command, use app_sequence instead so "
        "each one is opened, waited for, and verified before the next one starts. Set "
        "new_instance true only when the user explicitly asks for another instance. Pass "
        "arguments to open a URL or document with the application, for example Chrome with "
        "https://youtube.com. For a web page the "
        "user wants to look at or work on, prefer browser_control: it opens the same real "
        "browser and can then click, type and read on the page. For Roblox new_instance "
        "attempts a second window, moves it to the opposite monitor when possible, and "
        "verifies the result."
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
                "type": "STRING",
                "maxLength": 40,
                "description": (
                    "Which monitor to place the window on, when the user names one: a "
                    "1-based number ('1', '2'), 'primary'/'main' (the Windows primary "
                    "display), 'secondary'/'second' (the other display), 'left'/'right' "
                    "(by physical position), or 'monitor 2'/'display 2'."
                )
            },
            "state": {
                "type": "STRING",
                "enum": ["normal", "maximized", "fullscreen", "minimized", "left", "right", "top", "bottom"],
                "maxLength": 16,
                "description": "Window state after opening. fullscreen means native maximized with the taskbar visible; also supports maximized, minimized, or a snap side."
            },
            "arguments": {
                "type": "ARRAY",
                "maxItems": 8,
                "items": {"type": "STRING", "maxLength": 2048},
                "description": "Documents or URLs to open with the application, e.g. ['https://youtube.com']. Command-line switches are rejected."
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
