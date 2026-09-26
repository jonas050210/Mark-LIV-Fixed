#computer_control.py
import io
import platform
import time
from pathlib import Path

from core.path_policy import atomic_create_bytes, resolve_user_path
from core.user_paths import location as _user_location

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    from config import get_os
    return get_os()


_SAFE_SCREENSHOT_ROOTS = (Path.home(),)

def _safe_screenshot_path(requested: str | None) -> Path:
    # Windows Known Folders follows OneDrive Folder Backup, unlike a guessed
    # ``Path.home() / 'Desktop'``.
    fallback_parent = _user_location("desktop")
    if not fallback_parent.is_dir():
        fallback_parent = Path.home()
    fallback = fallback_parent / "jarvis_screenshot.png"
    path = resolve_user_path(
        requested or fallback,
        allowed_roots=_SAFE_SCREENSHOT_ROOTS,
        allow_missing=True,
        reject_symlinks=True,
    )
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        path = path.with_suffix(".png")
    path.parent.mkdir(parents=True, exist_ok=True)
    base = path
    counter = 1
    while path.exists():
        path = base.with_name(f"{base.stem}_{counter}{base.suffix}")
        counter += 1
        if counter > 10_000:
            raise FileExistsError("could not allocate a screenshot filename")
    return path

def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")

def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    if len(text) > 2_000:
        raise ValueError("typed text is limited to 2,000 characters")
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed {len(text)} characters."


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if len(text) > 5_000:
        raise ValueError("smart-typed text is limited to 5,000 characters")
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        previous = pyperclip.paste()
        try:
            pyperclip.copy(text)
            time.sleep(0.1)
            paste_key = "command" if _get_os() == "mac" else "ctrl"
            pyautogui.hotkey(paste_key, "v")
            time.sleep(0.15)
        finally:
            pyperclip.copy(previous)
        return f"Smart-typed {len(text)} characters using the clipboard."

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed {len(text)} characters."


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


# Key combinations that act on "whatever currently has focus" rather than on a
# named target.  The model reaches for these when asked to close or switch an
# application, which closes the wrong window whenever focus is not where it
# assumed.  window_manager addresses a window by handle, so it cannot miss.
_BLOCKED_COMBOS: dict[frozenset[str], str] = {
    frozenset({"alt", "f4"}): "close a specific window",
    frozenset({"command", "q"}): "quit a specific application",
    frozenset({"command", "w"}): "close a specific window",
    frozenset({"ctrl", "w"}): "close a specific window",
    frozenset({"ctrl", "shift", "w"}): "close a specific window",
    frozenset({"alt", "tab"}): "switch to a named window",
    frozenset({"command", "tab"}): "switch to a named window",
    frozenset({"ctrl", "alt", "delete"}): "reach the Windows security screen",
    frozenset({"win", "d"}): "minimise windows",
    frozenset({"win", "m"}): "minimise windows",
    frozenset({"win", "l"}): "lock the workstation",
}
_BLOCKED_SINGLE_KEYS = {
    "win": "open the Start menu",
    "winleft": "open the Start menu",
    "winright": "open the Start menu",
    "command": "open Spotlight",
    "super": "open the application launcher",
}
_KEY_ALIASES = {
    "windows": "win", "winleft": "win", "winright": "win", "super": "win",
    "cmd": "command", "meta": "command", "control": "ctrl", "del": "delete",
    "escape": "esc", "return": "enter",
}


def _canonical_keys(keys) -> list[str]:
    canonical = []
    for key in keys:
        name = str(key or "").strip().casefold()
        if not name:
            continue
        canonical.append(_KEY_ALIASES.get(name, name))
    return canonical


def _guard_hotkey(keys: list[str]) -> str:
    """Refuse focus-dependent combinations and name the safe alternative."""
    if not keys:
        return ""
    combo = frozenset(keys)
    intent = _BLOCKED_COMBOS.get(combo)
    if intent is None and len(keys) == 1:
        intent = _BLOCKED_SINGLE_KEYS.get(keys[0])
    if intent is None:
        return ""
    return (
        f"Refused '{'+'.join(keys)}': that combination acts on whichever window "
        f"currently has focus, so it can hit the wrong application. "
        f"Use window_manager to {intent} by name, or open_app to launch one."
    )


def _hotkey(*keys) -> str:
    canonical = _canonical_keys(keys)
    if not canonical:
        return "No keys were supplied for the hotkey."
    refusal = _guard_hotkey(canonical)
    if refusal:
        return refusal
    _require_pyautogui()
    pyautogui.hotkey(*canonical)
    return f"Hotkey: {'+'.join(canonical)}"


def _press(key: str) -> str:
    canonical = _canonical_keys([key])
    if not canonical:
        return "No key was supplied."
    refusal = _guard_hotkey(canonical)
    if refusal:
        return refusal
    _require_pyautogui()
    pyautogui.press(canonical[0])
    return f"Pressed: {canonical[0]}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    clicks     = amount if direction in ("up", "right") else -amount
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_copy() -> str:
    """Copy the current selection without sending clipboard contents to the model."""
    _require_pyautogui()
    copy_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(copy_key, "c")
    time.sleep(0.2)
    return "Copied the current selection to the clipboard."


def _clipboard_paste(text: str) -> str:
    if len(text) > 5_000:
        raise ValueError("pasted text is limited to 5,000 characters")
    if _PYPERCLIP:
        previous = pyperclip.paste()
        try:
            pyperclip.copy(text)
            time.sleep(0.1)
            _require_pyautogui()
            paste_key = "command" if _get_os() == "mac" else "ctrl"
            pyautogui.hotkey(paste_key, "v")
            time.sleep(0.15)
        finally:
            pyperclip.copy(previous)
        return f"Pasted {len(text)} characters."
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img = pyautogui.screenshot()
    buffer = io.BytesIO()
    image_format = "JPEG" if path.suffix.lower() in {".jpg", ".jpeg"} else "PNG"
    img.save(buffer, format=image_format)
    atomic_create_bytes(path, buffer.getvalue())
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — copy selection without exposing clipboard contents
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
    """
    params = parameters if isinstance(parameters, dict) else {}
    raw_action = params.get("action", "")
    action = raw_action[:32].lower().strip() if isinstance(raw_action, str) else ""

    if not action:
        return "No action specified for computer_control."

    if player:
        player.write_log(f"[Computer] {action}")

    # Parameters may contain text destined for a password field or clipboard;
    # never mirror raw automation payloads into logs.
    print(f"[ComputerControl] ▶ {action}")

    try:

        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw  = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_copy()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action in {"focus_window", "screen_find", "screen_click"}:
            return (
                f"'{action}' was removed. Use window_manager to focus, minimise, "
                "maximise or close a named window, and browser_control to click "
                "an element inside a web page."
            )

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action} failed ({type(e).__name__}).")
        return f"computer_control '{action}' failed ({type(e).__name__})."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": "Direct computer control inside the window that already has focus: type, click, in-app hotkeys, scroll, move the mouse, take a screenshot. Do NOT use this to open, close, minimise, maximise, or switch applications: alt+f4, command+q, ctrl+w, alt+tab and the Windows/Command key are refused because they hit whichever window happens to have focus. Use window_manager for window operations and open_app to launch applications.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["type", "smart_type", "click", "left_click", "double_click", "right_click", "move", "drag", "hotkey", "press", "scroll", "copy", "paste", "screenshot", "wait", "clear_field"],
                "maxLength": 32,
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field"
            },
            "text": {
                "type": "STRING",
                "maxLength": 5000,
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "minimum": -100000,
                "maximum": 100000,
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "minimum": -100000,
                "maximum": 100000,
                "description": "Y coordinate"
            },
            "x1": {"type": "INTEGER", "minimum": -100000, "maximum": 100000, "description": "Drag start X coordinate"},
            "y1": {"type": "INTEGER", "minimum": -100000, "maximum": 100000, "description": "Drag start Y coordinate"},
            "x2": {"type": "INTEGER", "minimum": -100000, "maximum": 100000, "description": "Drag end X coordinate"},
            "y2": {"type": "INTEGER", "minimum": -100000, "maximum": 100000, "description": "Drag end Y coordinate"},
            "keys": {
                "type": "STRING",
                "maxLength": 100,
                "description": "In-application key combination, e.g. 'ctrl+c' or 'ctrl+s'. Window and application management (closing, switching, launching) is refused here; use window_manager or open_app instead."
            },
            "key": {
                "type": "STRING",
                "maxLength": 50,
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "enum": ["up", "down", "left", "right"],
                "maxLength": 8,
                "description": "up | down | left | right"
            },
            "amount": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 100,
                "description": "Scroll amount (default: 3)"
            },
            "seconds": {
                "type": "NUMBER",
                "minimum": 0,
                "maximum": 30,
                "description": "Seconds to wait"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "maxLength": 1000,
                "description": "Save path for screenshot"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
