"""Cross-platform window and monitor control.

The voice/dashboard layer should talk to this module instead of blindly sending
hotkeys to whichever window currently has focus.  Windows uses the native user32
API when available; other platforms use optional pygetwindow/wmctrl helpers and
return a useful error when the desktop does not expose a window manager.
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_OS = platform.system()


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


def _windows() -> list[WindowInfo]:
    if _OS == "Windows":
        return _windows_native()
    try:
        import pygetwindow as gw

        out: list[WindowInfo] = []
        for win in gw.getAllWindows():
            title = str(getattr(win, "title", "") or "").strip()
            if not title:
                continue
            out.append(
                WindowInfo(
                    handle=int(getattr(win, "_hWnd", 0) or 0),
                    title=title,
                    process="",
                    pid=0,
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


def list_windows() -> list[WindowInfo]:
    """Return visible titled windows, ordered as the desktop reports them."""
    return _windows()


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


def _windows_monitors() -> list[MonitorInfo]:
    try:
        import win32api

        out: list[MonitorInfo] = []
        for handle, _dc, rect in win32api.EnumDisplayMonitors():
            info = win32api.GetMonitorInfo(handle)
            device = info.get("Device", f"DISPLAY{len(out) + 1}")
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
        return []


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
    words = set(wanted.split())
    overlap = len(words & set(title.split())) + len(words & set(process.split()))
    return (overlap * 10, window)


def find_window(target: str = "") -> WindowInfo | None:
    windows = list_windows()
    if not windows:
        return None
    if not str(target or "").strip():
        if _OS == "Windows":
            try:
                hwnd = int(ctypes.windll.user32.GetForegroundWindow())
                for window in windows:
                    if window.handle == hwnd:
                        return window
            except Exception:
                pass
        return windows[0]
    scored = sorted((_score_window(window, target) for window in windows), key=lambda x: x[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None


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


def _pygetwindow_for(info: WindowInfo):
    import pygetwindow as gw

    for window in gw.getAllWindows():
        if int(getattr(window, "_hWnd", 0) or 0) == info.handle:
            return window
        if str(getattr(window, "title", "") or "").strip() == info.title:
            return window
    return None


def operate(window: WindowInfo, operation: str, *args) -> None:
    if _OS == "Windows":
        _native_window(window.handle, operation, *args)
        return
    target = _pygetwindow_for(window)
    if target is None:
        raise RuntimeError("desktop window API could not find that window")
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


def monitor_for(index: int | str | None) -> MonitorInfo:
    monitors = list_monitors()
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
        return "No visible windows were found."
    lines = []
    for i, window in enumerate(windows, 1):
        process = f" [{window.process}]" if window.process else ""
        state = " minimized" if window.minimized else " maximized" if window.maximized else ""
        lines.append(f"{i}. {window.title}{process} ({window.width}x{window.height}){state}")
    return "Open windows:\n" + "\n".join(lines[:40])


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
