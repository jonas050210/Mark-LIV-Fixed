"""WinEvent-driven change notification for the desktop window list.

WHY THIS EXISTS
    The window commands poll: "is the title there yet?" every quarter second.
    That works, but it pays for latency with sleep time — a page that lands
    its title 100 ms after a lookup still waits out the rest of the poll
    interval — and every ``list_windows()`` call re-enumerates and re-resolves
    process names even when nothing on the desktop has changed since the last
    one. Windows already broadcasts every window event we care about
    (show, hide, destroy, title change, move, foreground switch). Subscribing
    turns both costs into event handling.

WHAT THE PUMP DOES
    A daemon thread installs one ``SetWinEventHook`` per event range with
    ``WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS`` and runs a message
    loop; the OS calls back on that thread whenever something happens. The
    callback is deliberately trivial — bump a counter, set an event — because
    it must never enumerate windows or call back into the API that raised it.

WHAT CALLERS GET
    * ``start()`` — boot-time, idempotent, safe on any platform.
    * ``active()`` — whether events are flowing (False everywhere the hook
      could not be installed, including every non-Windows desktop).
    * ``wait_for_change(timeout)`` — block until an event, or the timeout.
      Degrades to a plain sleep when inactive, so callers need no branching.
    * ``revision()`` — a counter that changes with the desktop. A cached
      window list stamped with a revision is valid until the revision moves.

Everything degrades silently: without the hook the assistant behaves exactly
as it did before this module existed.
"""
from __future__ import annotations

import ctypes
import platform
import threading
import time

_OS = platform.system()

# WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS — the callback is called in
# this process (on the pumping thread), and our own windows are excluded so a
# Qt paint can never wake us.
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_FLAGS = _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS

_OBJID_WINDOW = 0

_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_DESTROY = 0x8001
_EVENT_OBJECT_SHOW = 0x8002
_EVENT_OBJECT_HIDE = 0x8003
_EVENT_OBJECT_NAMECHANGE = 0x8004
_EVENT_OBJECT_LOCATIONCHANGE = 0x800B
_EVENT_OBJECT_MINIMIZESTART = 0x0016
_EVENT_OBJECT_MINIMIZEEND = 0x0017

_EVENTS = (
    _EVENT_SYSTEM_FOREGROUND,
    _EVENT_OBJECT_DESTROY,
    _EVENT_OBJECT_SHOW,
    _EVENT_OBJECT_HIDE,
    _EVENT_OBJECT_NAMECHANGE,
    # Noisy while anything animates, but the callback is two assignments, and
    # geometry freshness is what makes a cached window list safe for the
    # placement code that re-reads a window right after moving it.
    _EVENT_OBJECT_LOCATIONCHANGE,
    _EVENT_OBJECT_MINIMIZESTART,
    _EVENT_OBJECT_MINIMIZEEND,
)

_lock = threading.Lock()
_change = threading.Event()
_thread: threading.Thread | None = None
_failed = False
_active = False
_revision = 0


def _bump() -> None:
    global _revision
    with _lock:
        _revision += 1
    _change.set()


def revision() -> int:
    """A counter that changes whenever the desktop's window state changes."""
    with _lock:
        return _revision


def active() -> bool:
    """Whether desktop events are currently being delivered."""
    return _active


def start() -> bool:
    """Install the hooks once. True when events are flowing or already were.

    Never raises: a desktop that refuses the hook simply leaves the assistant
    on the polling behaviour it always had. This is the only entry point that
    turns the pump on; call it once at application boot.
    """
    global _thread
    if _OS != "Windows" or _failed:
        return False
    with _lock:
        if _active or _failed:
            return _active
        if _thread is not None and _thread.is_alive():
            return False   # still coming up
        _thread = threading.Thread(
            target=_pump, daemon=True, name="window-events"
        )
        _thread.start()
    # Give the pump a moment to install its hooks so an immediate caller is
    # not told "inactive" for a thread that is microseconds from working.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if _active or _failed:
            break
        time.sleep(0.02)
    return _active


def wait_for_change(timeout: float) -> bool:
    """Block until a desktop window event arrives, or the timeout passes.

    Returns True when an event arrived inside the timeout. When the hook is
    not running this is an ordinary sleep returning False, so a caller's
    polling loop works unchanged on every platform.

    Deliberately does NOT start the pump: only the application's own boot
    does that. A library call that flipped the desktop into event mode as a
    side effect would silently change the behaviour of everything after it —
    including test processes, where a live cache between events is exactly
    the kind of cross-test state that makes results depend on test order.
    """
    budget = max(0.0, float(timeout))
    if not _active or _OS != "Windows" or _failed:
        time.sleep(budget)
        return False
    deadline = time.monotonic() + budget
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _change.clear()
        if _change.wait(remaining):
            return True


def _pump() -> None:
    """The message loop that turns OS window events into counter bumps."""
    global _active, _failed
    hooks = []
    try:
        user32 = ctypes.windll.user32
        from ctypes import wintypes

        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,   # HWINEVENTHOOK hook
            wintypes.DWORD,    # DWORD event
            wintypes.HWND,     # HWND hwnd
            wintypes.LONG,     # LONG idObject
            wintypes.LONG,     # LONG idChild
            wintypes.DWORD,    # DWORD idEventThread
            wintypes.DWORD,    # DWORD dwmsEventTime
        )

        def _callback(_hook, event, _hwnd, id_object, _id_child, _thread, _ts):
            # Cheap by design: never enumerate windows or call user32 from
            # inside the callback. Cursor and caret events arrive with a
            # non-window object id and are filtered out here.
            if id_object == _OBJID_WINDOW or event == _EVENT_SYSTEM_FOREGROUND:
                _bump()

        # Keep the ctypes trampoline alive for the life of the thread; a
        # garbage-collected callback pointer would crash the OS on the next
        # event.
        keep_alive = callback_type(_callback)

        user32.SetWinEventHook.argtypes = (
            wintypes.UINT, wintypes.UINT, wintypes.HANDLE,
            callback_type, wintypes.DWORD, wintypes.DWORD, wintypes.UINT,
        )
        user32.SetWinEventHook.restype = wintypes.HANDLE
        for event in _EVENTS:
            hook = user32.SetWinEventHook(
                event, event, None, keep_alive, 0, 0, _FLAGS
            )
            if hook:
                hooks.append(hook)
        if not hooks:
            _failed = True
            return

        _active = True
        _bump()   # wake early waiters: events are live from here on

        msg = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result <= 0:   # WM_QUIT (0) or an error (-1)
                break
    except Exception:
        pass
    finally:
        _active = False
        _failed = True
        try:
            user32 = ctypes.windll.user32
            for hook in hooks:
                user32.UnhookWinEvent(hook)
        except Exception:
            pass
