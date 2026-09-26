"""Cross-platform window and monitor control.

The voice/dashboard layer should talk to this module instead of blindly sending
hotkeys to whichever window currently has focus.  Windows uses the native user32
API when available; other platforms use optional pygetwindow/wmctrl helpers and
return a useful error when the desktop does not expose a window manager.
"""
from __future__ import annotations

import ctypes
import hashlib
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from core import window_events
from core.text_match import partial_ratio

_OS = platform.system()


def _configure_windows_dpi() -> None:
    """Make HWND and monitor coordinates physical/pixel accurate on Windows."""
    if _OS != "Windows":
        return
    try:
        user32 = ctypes.windll.user32
        # PER_MONITOR_AWARE_V2.  The call is safe to repeat and is ignored on
        # older Windows versions; the shcore fallback covers Windows 8.1/10.
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        pass


_configure_windows_dpi()


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process: str
    pid: int
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool = False
    maximized: bool = False

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    name: str
    left: int
    top: int
    right: int
    bottom: int
    work_left: int
    work_top: int
    work_right: int
    work_bottom: int
    refresh_hz: int | None = None
    primary: bool = False

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def work_width(self) -> int:
        return max(0, self.work_right - self.work_left)

    @property
    def work_height(self) -> int:
        return max(0, self.work_bottom - self.work_top)


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _process_name(pid: int) -> str:
    try:
        import psutil

        name = psutil.Process(pid).name()
        return name or str(pid)
    except Exception:
        return str(pid)


# ── Spoken "browser" versus the process that actually owns the window ────────
#
# A user who says "make the Browser fullscreen" is naming an application by its
# job, not by its executable. Chrome's window is owned by chrome.exe, Edge's by
# msedge.exe, and none of those strings resembles the word "browser", so the
# old scorer could not connect the request to any window at all. Matching a
# generic browser word against any known browser process closes that gap
# without loosening matching for anything else: the alias only fires when the
# *user's own target* is one of these generic words.
_BROWSER_PROCESS_STEMS = {
    "chrome", "googlechrome", "chromium", "chromehtml", "msedge", "microsoftedge",
    "edge", "edgehtml", "firefox", "brave", "bravebrowser", "opera", "operagx",
    "operabrowser", "vivaldi", "vivaldistable", "thorium", "librewolf",
    "waterfox", "palemoon", "safari", "browser", "webbrowser",
}
_GENERIC_BROWSER_WORDS = {
    "browser", "web browser", "webbrowser", "internet", "internet browser",
    "browser window", "the browser", "my browser",
}


def _is_browser_process(name: str) -> bool:
    stem = _normalise(Path(str(name or "")).stem).replace(" ", "")
    return stem in _BROWSER_PROCESS_STEMS


def _is_generic_browser_target(target: str) -> bool:
    return _normalise(target) in _GENERIC_BROWSER_WORDS


_DESKTOP_BACKEND: object | None = None
_DESKTOP_BACKEND_NAME = ""


def desktop_backend():
    """Return the desktop window library, or None when there is no desktop.

    PyWinCtl is preferred over PyGetWindow: it is the maintained successor, it
    reports the owning process id, and it supports macOS and Linux properly
    rather than only Windows. Both raise on import in a headless session, so
    the import is lazy and its failure is cached instead of retried per call.
    """
    global _DESKTOP_BACKEND, _DESKTOP_BACKEND_NAME
    if _DESKTOP_BACKEND is not None or _DESKTOP_BACKEND_NAME == "none":
        return _DESKTOP_BACKEND
    for module_name in ("pywinctl", "pygetwindow"):
        try:
            module = __import__(module_name)
        except Exception:
            continue
        if not hasattr(module, "getAllWindows"):
            continue
        _DESKTOP_BACKEND = module
        _DESKTOP_BACKEND_NAME = module_name
        return module
    _DESKTOP_BACKEND_NAME = "none"
    return None


def backend_name() -> str:
    """Name of the active window backend, for diagnostics."""
    if _OS == "Windows":
        return "user32"
    desktop_backend()
    return _DESKTOP_BACKEND_NAME or "none"


def _stable_handle(win, title: str, pid: int) -> int:
    """A handle that survives re-enumeration.

    Only Windows gives every window a real HWND. Using ``id()`` of the backend
    wrapper instead would look like a handle but change on every enumeration,
    so ``refresh_window`` would never find the window it just moved and every
    placement would report as unverified. A digest of pid and title is stable
    for as long as both are, which is exactly the lifetime a caller needs.
    """
    native = int(getattr(win, "_hWnd", 0) or 0)
    if native:
        return native
    digest = hashlib.blake2b(f"{pid}:{title}".encode("utf-8", "replace"), digest_size=7)
    return int.from_bytes(digest.digest(), "big")


def _window_pid(win) -> int:
    """PyWinCtl exposes the owning pid; PyGetWindow does not."""
    getter = getattr(win, "getPID", None)
    if callable(getter):
        try:
            return int(getter() or 0)
        except Exception:
            return 0
    return 0


def _windows() -> list[WindowInfo]:
    if _OS == "Windows":
        return _windows_native()
    module = desktop_backend()
    if module is None:
        return _windows_wmctrl()
    try:
        out: list[WindowInfo] = []
        for win in module.getAllWindows():
            title = str(getattr(win, "title", "") or "").strip()
            if not title:
                continue
            pid = _window_pid(win)
            out.append(
                WindowInfo(
                    handle=_stable_handle(win, title, pid),
                    title=title,
                    process=_process_name(pid) if pid else "",
                    pid=pid,
                    left=int(getattr(win, "left", 0) or 0),
                    top=int(getattr(win, "top", 0) or 0),
                    right=int(getattr(win, "right", 0) or 0),
                    bottom=int(getattr(win, "bottom", 0) or 0),
                    minimized=bool(getattr(win, "isMinimized", False)),
                    maximized=bool(getattr(win, "isMaximized", False)),
                )
            )
        return out
    except Exception:
        return _windows_wmctrl()


def _windows_native() -> list[WindowInfo]:
    user32 = ctypes.windll.user32
    wintypes = ctypes.wintypes
    out: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(None, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        out.append(
            WindowInfo(
                handle=int(hwnd),
                title=title,
                process=_process_name(int(pid.value)),
                pid=int(pid.value),
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
                minimized=bool(user32.IsIconic(hwnd)),
                maximized=bool(user32.IsZoomed(hwnd)),
            )
        )

    callback_ref = callback_type(callback)
    user32.EnumWindows(callback_ref, 0)
    return out


def _windows_wmctrl() -> list[WindowInfo]:
    try:
        result = subprocess.run(
            ["wmctrl", "-lG"], capture_output=True, text=True, timeout=3
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    out: list[WindowInfo] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            _wid, _desktop, left, top, width, height, _host, title = parts[:8]
            out.append(
                WindowInfo(
                    handle=int(_wid, 16),
                    title=title,
                    process="",
                    pid=0,
                    left=int(left),
                    top=int(top),
                    right=int(left) + int(width),
                    bottom=int(top) + int(height),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


# Cache for list_windows when desktop events are flowing: (revision, stamp,
# windows). The TTL is belt-and-braces — an event we do not hook should not
# be able to keep a stale list alive for a whole session.
_WINDOW_CACHE: tuple[int, float, list[WindowInfo]] | None = None
_WINDOW_CACHE_TTL = 5.0


def list_windows() -> list[WindowInfo]:
    """Return visible titled windows, ordered as the desktop reports them.

    When the WinEvent pump is running, the list is cached between desktop
    events: nothing changed, so re-enumerating and re-resolving every process
    name would buy nothing. Geometry events invalidate the cache too, and
    ``operate`` clears it synchronously — the OS event for our own move can
    arrive after a caller has already re-read the window to verify it.
    """
    global _WINDOW_CACHE
    if window_events.active():
        cached = _WINDOW_CACHE
        now = time.monotonic()
        if (
            cached is not None
            and cached[0] == window_events.revision()
            and now - cached[1] < _WINDOW_CACHE_TTL
        ):
            return list(cached[2])
        windows = _windows()
        _WINDOW_CACHE = (window_events.revision(), now, windows)
        return list(windows)
    return _windows()


def _invalidate_window_cache() -> None:
    global _WINDOW_CACHE
    _WINDOW_CACHE = None


def _parse_xrandr_monitors() -> list[MonitorInfo]:
    try:
        result = subprocess.run(
            ["xrandr", "--query"], capture_output=True, text=True, timeout=3
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []
    out: list[MonitorInfo] = []
    pattern = re.compile(
        r"\b(\d+)x(\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
    )
    for line in result.stdout.splitlines():
        if " connected" not in line:
            continue
        match = pattern.search(line)
        if not match:
            continue
        width, height = int(match.group(1)), int(match.group(2))
        left, top = int(match.group("x")), int(match.group("y"))
        rate_match = re.search(r"\s(\d+(?:\.\d+)?)\*?\+?", line[match.end() :])
        hz = round(float(rate_match.group(1))) if rate_match else None
        name = line.split()[0]
        out.append(
            MonitorInfo(
                index=len(out) + 1,
                name=name,
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
                work_left=left,
                work_top=top,
                work_right=left + width,
                work_bottom=top + height,
                refresh_hz=hz,
                primary=" primary " in f" {line} ",
            )
        )
    return out


def _windows_refresh_native(device: str) -> int | None:
    try:
        user32 = ctypes.windll.user32
        class DEVMODEW(ctypes.Structure):
            _fields_ = [
                ("dmDeviceName", ctypes.c_wchar * 32),
                ("dmSpecVersion", ctypes.c_ushort), ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort), ("dmDriverExtra", ctypes.c_ushort),
                ("dmFields", ctypes.c_ulong), ("dmPositionX", ctypes.c_long),
                ("dmPositionY", ctypes.c_long), ("dmDisplayOrientation", ctypes.c_ulong),
                ("dmDisplayFixedOutput", ctypes.c_ulong), ("dmColor", ctypes.c_short),
                ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", ctypes.c_ushort),
                ("dmBitsPerPel", ctypes.c_ulong), ("dmPelsWidth", ctypes.c_ulong),
                ("dmPelsHeight", ctypes.c_ulong), ("dmDisplayFlags", ctypes.c_ulong),
                ("dmDisplayFrequency", ctypes.c_ulong),
            ]
        mode = DEVMODEW()
        mode.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(device, -1, ctypes.byref(mode)):
            hz = int(mode.dmDisplayFrequency or 0)
            return hz or None
    except Exception:
        pass
    return None


def _windows_display_is_virtual(device: str) -> bool:
    """Exclude software-only displays that should never receive desktop windows."""
    try:
        user32 = ctypes.windll.user32

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("DeviceName", ctypes.c_wchar * 32),
                ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", ctypes.c_ulong),
                ("DeviceID", ctypes.c_wchar * 128),
                ("DeviceKey", ctypes.c_wchar * 128),
            ]

        details = DISPLAY_DEVICEW()
        details.cb = ctypes.sizeof(details)
        if user32.EnumDisplayDevicesW(device, 0, ctypes.byref(details), 0):
            label = f"{details.DeviceString} {details.DeviceID}".casefold()
            return "meta virtual monitor" in label
    except Exception:
        pass
    return False


def _windows_monitors_native() -> list[MonitorInfo]:
    """ctypes monitor fallback when pywin32 is not installed."""
    out: list[MonitorInfo] = []
    try:
        user32 = ctypes.windll.user32
        from ctypes import wintypes
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        class MONITORINFOEX(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong),
                        ("szDevice", ctypes.c_wchar * 32)]
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(RECT), wintypes.LPARAM,
        )
        def callback(handle, _dc, _rect, _data):
            info = MONITORINFOEX()
            info.cbSize = ctypes.sizeof(info)
            if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                return True
            if _windows_display_is_virtual(info.szDevice):
                return True
            m = info.rcMonitor; w = info.rcWork
            out.append(MonitorInfo(
                index=len(out) + 1, name=info.szDevice or f"DISPLAY{len(out)+1}",
                left=m.left, top=m.top, right=m.right, bottom=m.bottom,
                work_left=w.left, work_top=w.top, work_right=w.right, work_bottom=w.bottom,
                refresh_hz=_windows_refresh_native(info.szDevice), primary=bool(info.dwFlags & 1),
            ))
            return True
        cb = callback_type(callback)
        user32.EnumDisplayMonitors(0, 0, cb, 0)
    except Exception:
        return []
    return out


def _windows_monitors() -> list[MonitorInfo]:
    try:
        import win32api

        out: list[MonitorInfo] = []
        for handle, _dc, rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(handle)
            device = info.get("Device", f"DISPLAY{len(out) + 1}")
            if _windows_display_is_virtual(device):
                continue
            work = info.get("Work", rect)
            hz = None
            try:
                settings = win32api.EnumDisplaySettings(
                    device, win32api.ENUM_CURRENT_SETTINGS
                )
                hz = int(getattr(settings, "DisplayFrequency", 0) or 0) or None
            except Exception:
                pass
            out.append(
                MonitorInfo(
                    index=len(out) + 1,
                    name=device,
                    left=int(rect[0]), top=int(rect[1]),
                    right=int(rect[2]), bottom=int(rect[3]),
                    work_left=int(work[0]), work_top=int(work[1]),
                    work_right=int(work[2]), work_bottom=int(work[3]),
                    refresh_hz=hz,
                    primary=bool(info.get("Flags", 0) & 1),
                )
            )
        return out
    except Exception:
        return _windows_monitors_native()


def list_monitors() -> list[MonitorInfo]:
    if _OS == "Windows":
        monitors = _windows_monitors()
    elif _OS == "Linux":
        monitors = _parse_xrandr_monitors()
    else:
        monitors = []
    if monitors:
        return monitors
    return [
        MonitorInfo(
            index=1, name="default", left=0, top=0, right=1920, bottom=1080,
            work_left=0, work_top=0, work_right=1920, work_bottom=1080,
            refresh_hz=None, primary=True,
        )
    ]


def _score_window(window: WindowInfo, target: str) -> tuple[int, WindowInfo]:
    wanted = _normalise(target)
    title = _normalise(window.title)
    process = _normalise(Path(window.process).stem)
    if not wanted:
        return (0, window)
    if wanted == title or wanted == process:
        return (100, window)
    if wanted in title or wanted in process:
        return (80, window)
    # "Make the browser fullscreen" names the application by its job. The
    # generic word only ever matches a real browser process, never a title —
    # a document called "Browser Comparison.xlsx" is not what was asked for.
    if _is_generic_browser_target(wanted) and _is_browser_process(window.process):
        return (85, window)
    words = set(wanted.split())
    overlap = len(words & set(title.split())) + len(words & set(process.split()))
    if overlap:
        return (overlap * 10, window)
    # A window title carries text the user never says — "Inbox (12) - Gmail -
    # Google Chrome" for a request of "chrome" — so fall back to a partial
    # similarity rather than declaring no match at all.
    best = max(partial_ratio(wanted, title), partial_ratio(wanted, process))
    return ((int(best * 60) if best >= 0.75 else 0), window)


def find_windows(target: str, *, min_score: int = 1) -> list[WindowInfo]:
    """Return visible windows matching an app/title above a score threshold.

    Interactive focus can accept fuzzy matches, while batch or destructive
    callers pass ``min_score=80`` so a typo cannot close several unrelated
    windows merely because their titles are vaguely similar.
    """
    wanted = str(target or "").strip()
    if not wanted:
        return []
    threshold = max(1, min(100, int(min_score)))
    scored = sorted(
        (_score_window(window, wanted) for window in list_windows()),
        key=lambda item: item[0], reverse=True,
    )
    return [window for score, window in scored if score >= threshold]


# ── Memory of the window the assistant itself put on screen ─────────────────
#
# "Open YouTube" followed by "make it fullscreen" is the most common
# multi-step window request there is, and the second step must not depend on
# the first step's page having finished loading its title. open_app records
# every window it verifiably launches here, so a later window command has a
# fallback identity ("the window I just opened as X") when title matching
# alone finds nothing.
_LAUNCH_MEMORY_LOCK = threading.Lock()
_LAST_LAUNCH: tuple[float, str, WindowInfo] | None = None
LAUNCH_MEMORY_SECONDS = 15 * 60.0


def remember_last_window(name: str, window) -> None:
    """Record the window a launch produced, with the name it was asked for."""
    handle = int(getattr(window, "handle", 0) or 0)
    if not handle:
        return
    global _LAST_LAUNCH
    with _LAUNCH_MEMORY_LOCK:
        _LAST_LAUNCH = (time.time(), str(name or "").strip(), window)


def last_launch_label() -> str:
    with _LAUNCH_MEMORY_LOCK:
        return _LAST_LAUNCH[1] if _LAST_LAUNCH else ""


def last_launched_window(max_age_seconds: float = LAUNCH_MEMORY_SECONDS) -> WindowInfo | None:
    """The remembered launch window, when it is still recent enough to matter."""
    with _LAUNCH_MEMORY_LOCK:
        entry = _LAST_LAUNCH
    if entry is None:
        return None
    opened_at, _label, window = entry
    if time.time() - opened_at > max(0.0, float(max_age_seconds)):
        return None
    return window


def clear_launch_memory() -> None:
    """Drop the remembered launch (used by tests and session teardown)."""
    global _LAST_LAUNCH
    with _LAUNCH_MEMORY_LOCK:
        _LAST_LAUNCH = None


# Words that carry no identity; a shared one of these must never make two
# different names "agree".
_LABEL_NOISE_WORDS = {
    "the", "a", "an", "app", "application", "please", "open", "opened",
    "launch", "launched", "start", "started", "window", "and", "on", "in",
}


def _launch_labels_agree(target: str, label: str) -> bool:
    """Whether a spoken target plausibly names a remembered launch.

    Deliberately stricter than the window scorer: this decision hands over a
    specific window that title matching could NOT confirm, so a weak
    resemblance must not be enough.
    """
    wanted = _normalise(target)
    known = _normalise(label)
    if not wanted or not known:
        return False
    if wanted == known or wanted in known or known in wanted:
        return True
    wanted_words = set(wanted.split()) - _LABEL_NOISE_WORDS
    known_words = set(known.split()) - _LABEL_NOISE_WORDS
    if wanted_words and known_words and wanted_words & known_words:
        return True
    return partial_ratio(wanted, known) >= 0.75


def recent_launch_window_for(target: str) -> WindowInfo | None:
    """The remembered launch window when ``target`` plausibly names it.

    This is the identity that survives a page whose title has not loaded yet
    ("New Tab" while YouTube starts): the assistant knows which window it put
    on screen and under which spoken name, so "open YouTube … now fullscreen
    it" and "open YouTube (again)" both resolve to that window instead of
    starting a second instance or reporting nothing found.
    """
    with _LAUNCH_MEMORY_LOCK:
        entry = _LAST_LAUNCH
    if entry is None:
        return None
    opened_at, label, window = entry
    if time.time() - opened_at > LAUNCH_MEMORY_SECONDS:
        return None
    if not _launch_labels_agree(target, label):
        return None
    return window


def _window_key(window: WindowInfo) -> tuple[int, int, str]:
    return (int(window.handle or 0), int(window.pid or 0), str(window.title))


def watch_launched_window(label: str, timeout: float = 6.0) -> None:
    """Remember the first window that appears after a native launch, in the
    background.

    A URL handed to the OS opens the default browser with no pid and — until
    the page loads — no name-bearing title, so there is nothing to match a
    later "make it fullscreen" against. This watches for a new (or re-titled)
    window for a few seconds and records it as the most recent launch. Best
    effort by design: on failure the next window command simply falls back to
    title matching, exactly as before.
    """
    def _watch() -> None:
        try:
            before = {_window_key(w) for w in list_windows()}
            deadline = time.monotonic() + max(1.0, float(timeout))
            settled_since: float | None = None
            while time.monotonic() < deadline:
                # Wake the instant the desktop reports a new or re-titled
                # window rather than after the full poll interval.
                window_events.wait_for_change(0.25)
                windows = list_windows()
                new = [w for w in windows if _window_key(w) not in before]
                browser_new = [w for w in new if _is_browser_process(w.process)]
                if browser_new:
                    remember_last_window(label, browser_new[-1])
                    return
                if len(new) == 1:
                    if settled_since is None:
                        settled_since = time.monotonic()
                    elif time.monotonic() - settled_since >= 0.75:
                        remember_last_window(label, new[0])
                        return
                else:
                    settled_since = None
        except Exception:
            pass

    threading.Thread(
        target=_watch, daemon=True, name="window-launch-watch"
    ).start()


def find_window(target: str = "") -> WindowInfo | None:
    if str(target or "").strip():
        matches = find_windows(target)
        return matches[0] if matches else None
    windows = list_windows()
    if not windows:
        return None
    if _OS == "Windows":
        try:
            hwnd = int(ctypes.windll.user32.GetForegroundWindow())
            for window in windows:
                if window.handle == hwnd:
                    return window
        except Exception:
            pass
    return windows[0]


def _native_window(handle: int, operation: str, *args) -> None:
    user32 = ctypes.windll.user32
    if operation == "minimize":
        user32.ShowWindow(handle, 6)
    elif operation == "maximize":
        user32.ShowWindow(handle, 3)
    elif operation == "restore":
        user32.ShowWindow(handle, 9)
    elif operation == "focus":
        user32.ShowWindow(handle, 9)
        user32.SetForegroundWindow(handle)
    elif operation == "close":
        user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
    elif operation == "move":
        left, top, width, height = args
        user32.MoveWindow(handle, int(left), int(top), int(width), int(height), True)
    else:
        raise ValueError(f"unsupported window operation: {operation}")


def _desktop_window_for(info: WindowInfo):
    """Locate the backend object for a WindowInfo, by handle then by title."""
    module = desktop_backend()
    if module is None:
        return None
    windows = list(module.getAllWindows())
    for window in windows:
        title = str(getattr(window, "title", "") or "").strip()
        if _stable_handle(window, title, _window_pid(window)) == info.handle:
            return window
    for window in windows:
        if str(getattr(window, "title", "") or "").strip() == info.title:
            return window
    return None


def operate(window: WindowInfo, operation: str, *args) -> None:
    # Our own change: the OS event for it can arrive after a caller has
    # already re-read the window to verify the result, so drop the cache now.
    _invalidate_window_cache()
    if _OS == "Windows":
        _native_window(window.handle, operation, *args)
        return
    target = _desktop_window_for(window)
    if target is None:
        raise RuntimeError(
            "no desktop window API is available on this system "
            "(install pywinctl, or run inside a graphical session)"
        )
    if operation == "minimize":
        target.minimize()
    elif operation == "maximize":
        target.maximize()
    elif operation == "restore":
        target.restore()
    elif operation == "focus":
        target.activate()
    elif operation == "close":
        target.close()
    elif operation == "move":
        left, top, width, height = args
        target.moveTo(int(left), int(top))
        target.resizeTo(int(width), int(height))
    else:
        raise ValueError(f"unsupported window operation: {operation}")


def foreground_window() -> WindowInfo | None:
    """The window that currently owns input focus, when the platform reports it."""
    if _OS != "Windows":
        return None
    try:
        handle = int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return None
    if not handle:
        return None
    for window in list_windows():
        if window.handle == handle:
            return window
    return None


def focus_window(window: WindowInfo, *, timeout: float = 0.75) -> bool:
    """Focus a known window and verify it owns input on Windows.

    ``SetForegroundWindow`` may be refused by Windows' foreground-lock rules
    without raising an exception. Returning a successful call as "switched"
    in that situation tells the user the wrong thing. Native Windows can report
    the actual foreground HWND, so poll it briefly after the request. Other
    desktop backends do not expose equivalent reliable focus state; there a
    successful backend call remains the strongest honest result available.
    """
    operate(window, "focus")
    if _OS != "Windows":
        return True

    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        current = foreground_window()
        if current is not None and int(current.handle) == int(window.handle):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def restore_foreground(window: WindowInfo | None) -> bool:
    """Give focus back to a window that was in front before a launch."""
    if window is None:
        return False
    try:
        return focus_window(window)
    except Exception:
        return False


def _descendant_pids(pid: int) -> set[int]:
    pids = {int(pid)}
    try:
        import psutil

        for child in psutil.Process(pid).children(recursive=True):
            pids.add(int(child.pid))
    except Exception:
        pass
    return pids


def windows_for_pid(pid: int | None) -> list[WindowInfo]:
    """Visible windows owned by a process or any of its children.

    Many launchers (Chrome, Steam, Electron apps) hand off to a second process,
    so the window that appears frequently belongs to a child rather than to the
    pid returned by the spawn itself.
    """
    if not pid:
        return []
    wanted = _descendant_pids(int(pid))
    return [window for window in list_windows() if int(window.pid or 0) in wanted]


_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}


def resolve_monitor_token(token: int | str | None) -> int:
    """Resolve a spoken or typed monitor reference to a 1-based index.

    Accepts a plain 1-based number ("2"), an explicit reference ("monitor 2",
    "display 2", "screen 2"), or a semantic name: primary/main (the Windows
    primary display, per the user's own definition of "main monitor"),
    secondary/second (the other display, whichever one that is), or
    left/right (chosen by physical position, for setups where the user
    thinks in terms of arrangement rather than numbers). Raises ValueError
    with a message safe to show the user when the token cannot be resolved
    against the monitors actually connected.
    """
    monitors = list_monitors()
    if isinstance(token, bool):
        raise ValueError("monitor must be a number or name, not true/false")
    if isinstance(token, int):
        if 1 <= token <= len(monitors):
            return token
        raise ValueError(f"monitor must be between 1 and {len(monitors)}")
    if token is None:
        raise ValueError("no monitor was specified")

    text = str(token).strip()
    if not text:
        raise ValueError("no monitor was specified")

    # An explicit number always wins, whether bare ("2") or phrased
    # ("monitor 2", "display 2", "second screen" would still fall through to
    # the word map below because it has no digit).
    digits = re.findall(r"\d+", text)
    if digits:
        value = int(digits[0])
        if 1 <= value <= len(monitors):
            return value
        raise ValueError(
            f"monitor must be between 1 and {len(monitors)} (there is no monitor {value})"
        )

    words = set(re.findall(r"[a-z]+", text.casefold()))
    if not words:
        raise ValueError("no monitor was specified")

    primary = next((m for m in monitors if m.primary), monitors[0])
    non_primary = [m for m in monitors if m.index != primary.index]

    if words & {"primary", "main"}:
        return primary.index
    if words & {"secondary", "second", "other"}:
        if non_primary:
            return non_primary[0].index
        raise ValueError("only one monitor is connected")
    if words & {"left", "leftmost"}:
        return min(monitors, key=lambda m: m.left).index
    if words & {"right", "rightmost"}:
        return max(monitors, key=lambda m: m.left).index
    for word, value in _ORDINAL_WORDS.items():
        if word in words:
            if 1 <= value <= len(monitors):
                return value
            raise ValueError(f"monitor must be between 1 and {len(monitors)}")

    raise ValueError(
        "monitor must be a number, 'primary', 'secondary', 'left', 'right', or 'monitor N'"
    )


def monitor_for(index: int | str | None) -> MonitorInfo:
    """Resolve a monitor reference, defaulting to monitor 1 when unset.

    A bare int keeps its historical lenient behaviour (falsy values default
    to monitor 1). Strings — semantic ("primary", "secondary", "left") or
    numeric ("2") — are resolved through resolve_monitor_token, whose
    ValueError is a clear, user-facing message rather than a silent
    fallback, since nothing before this depended on strings being ignored.
    """
    monitors = list_monitors()
    if index is None or index == "":
        wanted = 1
    elif isinstance(index, str):
        wanted = resolve_monitor_token(index)
    else:
        try:
            wanted = int(index or 1)
        except (TypeError, ValueError):
            wanted = 1
    if wanted < 1 or wanted > len(monitors):
        raise ValueError(f"monitor must be between 1 and {len(monitors)}")
    return monitors[wanted - 1]


def describe_windows() -> str:
    windows = list_windows()
    if not windows:
        # An empty list is worth explaining: on a machine where no window
        # backend answers, "no windows" reads like "nothing is open" when the
        # real story is "the desktop could not be queried".
        return (
            "No visible windows were found. If windows are definitely open, the "
            f"window backend on this system reported '{backend_name()}' and may "
            "not be able to see the desktop."
        )
    lines = []
    for i, window in enumerate(windows, 1):
        process = f" [{window.process}]" if window.process else ""
        state = " minimized" if window.minimized else " maximized" if window.maximized else ""
        lines.append(f"{i}. {window.title}{process} ({window.width}x{window.height}){state}")
    summary = "" if len(windows) <= 40 else f" …and {len(windows) - 40} more"
    return f"Open windows ({len(windows)}):\n" + "\n".join(lines[:40]) + summary


def describe_monitors() -> str:
    lines = []
    for monitor in list_monitors():
        hz = f", {monitor.refresh_hz} Hz" if monitor.refresh_hz else ""
        primary = ", primary" if monitor.primary else ""
        lines.append(
            f"Monitor {monitor.index}: {monitor.name} — "
            f"{monitor.width}x{monitor.height}{hz}, position {monitor.left},{monitor.top}{primary}"
        )
    return "Monitors:\n" + "\n".join(lines)


def move_to_monitor(window: WindowInfo, monitor: MonitorInfo) -> None:
    width = min(max(window.width, 400), monitor.work_width)
    height = min(max(window.height, 250), monitor.work_height)
    left = monitor.work_left + max(0, (monitor.work_width - width) // 2)
    top = monitor.work_top + max(0, (monitor.work_height - height) // 2)
    operate(window, "restore")
    operate(window, "move", left, top, width, height)
    operate(window, "focus")


def snap_window(window: WindowInfo, monitor: MonitorInfo, side: str) -> None:
    side = str(side or "left").casefold()
    if side not in {"left", "right", "top", "bottom", "full", "fullscreen"}:
        raise ValueError("side must be left, right, top, bottom, or full")
    half_w = monitor.work_width // 2
    half_h = monitor.work_height // 2
    if side == "left":
        rect = (monitor.work_left, monitor.work_top, half_w, monitor.work_height)
    elif side == "right":
        rect = (monitor.work_left + half_w, monitor.work_top, monitor.work_width - half_w, monitor.work_height)
    elif side == "top":
        rect = (monitor.work_left, monitor.work_top, monitor.work_width, half_h)
    elif side == "bottom":
        rect = (monitor.work_left, monitor.work_top + half_h, monitor.work_width, monitor.work_height - half_h)
    else:
        rect = (monitor.work_left, monitor.work_top, monitor.work_width, monitor.work_height)
    operate(window, "restore")
    operate(window, "move", *rect)
    operate(window, "focus")


PLACEMENT_STATES = (
    "normal", "maximized", "fullscreen", "minimized",
    "left", "right", "top", "bottom",
)


def window_on_monitor(window: WindowInfo, monitor: MonitorInfo) -> bool:
    """True when the window's centre point lies inside the monitor's bounds."""
    center_x = (int(window.left) + int(window.right)) // 2
    center_y = (int(window.top) + int(window.bottom)) // 2
    return (
        monitor.left <= center_x < monitor.right and
        monitor.top <= center_y < monitor.bottom
    )


def monitor_of(window: WindowInfo) -> MonitorInfo | None:
    for monitor in list_monitors():
        if window_on_monitor(window, monitor):
            return monitor
    return None


def place_window(window: WindowInfo, monitor: MonitorInfo | None = None,
                 state: str = "normal", *, focus: bool = True) -> WindowInfo:
    """Move a window to a monitor and apply a window state, then re-read it.

    The returned WindowInfo is a fresh reading rather than the caller's stale
    one, so the result can be verified instead of assumed.
    """
    state = str(state or "normal").casefold().strip()
    # In ordinary speech, "fullscreen" almost always means the Windows
    # maximise button (the square at top-right), not F11/video-game fullscreen.
    # Maximising through the native window manager keeps the taskbar visible and
    # avoids sending a focus-dependent key to the wrong application. Keep the
    # legacy spelling accepted, but give it that safer, expected meaning.
    if state in {"full", "full_screen", "full screen", "fullscreen", "fulscreen"}:
        state = "maximized"
    if state in {"maximize", "maximised", "maximise"}:
        state = "maximized"
    if state in {"minimize", "minimised"}:
        state = "minimized"
    if state not in PLACEMENT_STATES:
        raise ValueError(f"state must be one of: {', '.join(PLACEMENT_STATES)}")

    if state == "minimized":
        operate(window, "minimize")
        return refresh_window(window) or window

    if monitor is not None:
        # Maximised or snapped windows ignore MoveWindow, so restore first.
        operate(window, "restore")
        if state in {"left", "right", "top", "bottom"}:
            snap_window(window, monitor, state)
        else:
            move_to_monitor(window, monitor)
            if state == "maximized":
                operate(window, "maximize")
    else:
        if state == "maximized":
            operate(window, "maximize")
        elif state in {"left", "right", "top", "bottom"}:
            target = monitor_of(window) or list_monitors()[0]
            snap_window(window, target, state)
        else:
            operate(window, "restore")

    if focus:
        try:
            operate(window, "focus")
        except Exception:
            pass
    return refresh_window(window) or window


def refresh_window(window: WindowInfo) -> WindowInfo | None:
    """Re-read a window so callers can verify a placement.

    The handle is tried first. A window whose title changed while it was being
    moved would otherwise look closed, so process and title are accepted as a
    second identity.
    """
    candidates = list_windows()
    for candidate in candidates:
        if candidate.handle == window.handle:
            return candidate
    if window.pid:
        for candidate in candidates:
            if candidate.pid == window.pid and candidate.title == window.title:
                return candidate
    return None
