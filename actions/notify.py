"""
notify.py — an on-demand desktop notification.

How this differs from actions/reminder.py
─────────────────────────────────────────
`reminder` creates a task-scheduler entry whose toast fires later, without the
app running. This action shows a notification *now*, because the user asked for
it — "put a note on my screen", "notify me that the render finished".

The delivery ladder is: `win10toast`, then the notification the Windows shell
itself provides (`Shell_NotifyIconW` through `ctypes`), then `notify-send` on
Linux, then the built-in `msg` command, then the activity log. The shell tier
matters on Windows 11, where `win10toast` frequently raises and `msg.exe` is
absent from Home editions — without it a Windows user would only ever get the
log entry. It needs no package at all: `ctypes` is standard library.

Every subprocess step is invoked as an argument vector, never through a shell,
so a notification text can neither inject a command nor be interpreted as one.
The shell tier passes the text to the API as data, not as a command line. macOS
deliberately falls through to the log instead of building an AppleScript string
out of model output.

Nothing here reads configuration, so no secret can end up in a toast.
"""
from __future__ import annotations

import platform
import re
import subprocess
import sys

_MAX_MESSAGE = 250          # a toast is not a document
_DEFAULT_SECONDS = 10
_SECONDS_LIMIT = (1, 60)
_DEFAULT_TITLE = "J.A.R.V.I.S"
_WS_RE = re.compile(r"\s+")


def _clean(raw, fallback: str, limit: int) -> str:
    """Collapse whitespace and cap the length so a toast stays readable."""
    text = _WS_RE.sub(" ", str(raw or "")).strip()
    if not text:
        return fallback
    return text[:limit]


def _clamp(raw, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _windows_toast(title: str, message: str, seconds: int) -> bool:
    try:
        from win10toast import ToastNotifier
    except ImportError:
        return False
    try:
        ToastNotifier().show_toast(title, message, duration=seconds, threaded=True)
        return True
    except Exception:
        return False


# NOTIFYICONDATAW field flags and messages (shell32).
_NIF_ICON, _NIF_TIP, _NIF_INFO = 0x02, 0x04, 0x10
_NIM_ADD, _NIM_DELETE = 0x00, 0x02
_NIIF_INFO = 0x01
_IDI_INFORMATION = 32516          # MAKEINTRESOURCE for the standard info icon
_HWND_MESSAGE = -3                # parent for a message-only window
_ICON_ID = 0x4A52                 # "JR", arbitrary but stable per process


def _windows_balloon(title: str, message: str, seconds: int) -> bool:
    """Show a notification through the Windows shell itself.

    Standard library only (`ctypes`), no package, no PowerShell and no batch
    file: the text is written into the `NOTIFYICONDATAW` structure as data, so
    there is no command line for anything to be injected into. On Windows 10 and
    11 the shell renders this as a normal toast and keeps it in the notification
    centre.

    Everything here is defined *inside* the platform gate. `ctypes.wintypes`
    measures differently on Linux (`DWORD` is 8 bytes there, 4 on Windows;
    `WCHAR` is 4 instead of 2), so a module-level definition would describe the
    wrong structure on a non-Windows machine — and `ctypes.windll` does not
    exist there at all.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        import threading
        from ctypes import wintypes

        user32, shell32, kernel32 = (ctypes.windll.user32, ctypes.windll.shell32,
                                     ctypes.windll.kernel32)

        class _NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        # Handles are pointer-sized: without an explicit restype ctypes assumes
        # c_int and silently truncates a 64-bit HWND.
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                           wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p]
        user32.LoadIconW.restype = ctypes.c_void_p
        user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]

        # A message-only window: it owns the tray icon but is never visible.
        hwnd = user32.CreateWindowExW(0, "STATIC", "MARK LIV notification", 0,
                                      0, 0, 0, 0, _HWND_MESSAGE, None,
                                      kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            return False

        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = _ICON_ID
        nid.uFlags = _NIF_ICON | _NIF_TIP | _NIF_INFO
        nid.hIcon = user32.LoadIconW(None, _IDI_INFORMATION)
        nid.szTip = title[:127]
        nid.szInfo = message[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = _NIIF_INFO
        nid.uTimeoutOrVersion = max(1, seconds) * 1000

        if not shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(nid)):
            user32.DestroyWindow(hwnd)
            return False

        def _retire():
            """Drop the icon again, so the tray does not collect entries."""
            try:
                shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(nid))
            except Exception:
                pass
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass

        threading.Timer(max(1, seconds), _retire).start()
        return True
    except Exception:
        return False


def _windows_msg(message: str, seconds: int) -> bool:
    """`msg *` is built into Windows and needs no third-party package."""
    if not sys.platform.startswith("win"):
        return False
    try:
        subprocess.run(["msg", "*", f"/TIME:{seconds}", message],
                       check=False, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _linux_notify(title: str, message: str, seconds: int) -> bool:
    if platform.system() != "Linux":
        return False
    try:
        subprocess.run(["notify-send", "-t", str(seconds * 1000), title, message],
                       check=False, timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def notify_action(parameters: dict, player=None) -> str:
    """
    Show a desktop notification immediately.

    parameters:
        message  : the text to display (required)
        title    : optional heading, defaults to the assistant name
        duration : optional seconds the toast stays up, 1-60, default 10
    """
    p = parameters if isinstance(parameters, dict) else {}

    message = _clean(p.get("message"), "", _MAX_MESSAGE)
    if not message:
        return "There is nothing to notify about — give me a message first."

    title = _clean(p.get("title"), _DEFAULT_TITLE, 60)
    seconds = _clamp(p.get("duration"), _DEFAULT_SECONDS, *_SECONDS_LIMIT)

    delivered = (_windows_toast(title, message, seconds)
                 or _windows_balloon(title, message, seconds)
                 or _linux_notify(title, message, seconds)
                 or _windows_msg(message, seconds))

    if player is not None:
        try:
            player.write_log(f"NOTIFY: {title} — {message[:80]}")
        except Exception:
            pass

    if delivered:
        return f"Notification shown: {message}"
    # No desktop notifier available (headless Linux, macOS, or the toast package
    # is missing). The log entry above is the fallback, and saying so out loud
    # beats silently claiming success.
    return (f"No desktop notifier answered, so I logged it instead: {message}")


TOOL = {
    "name": "notify",
    "description": (
        "Shows a desktop notification popup on the user's screen right now. Use "
        "it when the user asks to be notified, reminded visually, or wants a short "
        "message pushed to the screen — for example after a long task finishes. "
        "This is immediate and one-off; for something that must fire at a specific "
        "future date or time even when the app is closed, use the reminder tool."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "message": {
                "type": "STRING",
                "description": "The notification text, up to 250 characters.",
            },
            "title": {
                "type": "STRING",
                "description": "Optional short heading for the notification.",
            },
            "duration": {
                "type": "INTEGER",
                "description": "Seconds the notification stays visible, 1-60. "
                               "Defaults to 10.",
            },
        },
        "required": ["message"],
    },
    "handler": notify_action,
}
