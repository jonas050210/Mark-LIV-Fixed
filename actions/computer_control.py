#computer_control.py
import io
import json
import platform
import re
import string
import subprocess
import sys

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}
import time
import random
from pathlib import Path

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

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "jarvis_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


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


def _clipboard_get() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

def _focus_window(title: str) -> str:
    os_name = _get_os()

    if os_name == "windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=5, **_WIN_HIDE,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (Windows) failed: {e}"

    if os_name == "mac":
        script = (
            f'tell application "System Events" to '
            f'set frontmost of (first process whose name contains "{title}") to true'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (macOS) failed: {e}"

    if os_name == "linux":
        try:
            result = subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                time.sleep(0.3)
                return f"Focused window: {title}"
        except FileNotFoundError:
            pass
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", title, "windowactivate"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except FileNotFoundError:
            return "focus_window (Linux) requires wmctrl or xdotool"
        except Exception as e:
            return f"focus_window (Linux) failed: {e}"

    return f"focus_window: unknown OS '{os_name}'"

# ── screen_find ───────────────────────────────────────────────────────────────
# Finds a UI element by description and returns its screen coordinates.
#
#  • Captures the ACTIVE window only (via mss) — the element the user means is
#    almost always in the window in front, and a smaller image gives the model
#    a much better hit rate than a full multi-monitor desktop. If the active
#    window is the assistant's own window, or none can be determined, the whole
#    virtual desktop is captured instead.
#  • The model answers with a bounding box on Gemini's native 0–1000 grid
#    ([ymin, xmin, ymax, xmax]); the box is converted back to pixels. A large
#    box (a whole toolbar, a whole panel) triggers a second, zoomed pass on the
#    crop around it so the click lands on the element and not on its region.
#  • Models: gemini-flash-latest first, gemini-flash-lite-latest as fallback.
#    A 503/504/429 puts that model on a 10-minute block list so the next call
#    goes straight to the other one. Every request has a 20-second timeout, so
#    a click never hangs the assistant.

_FIND_MODELS      = ("gemini-flash-latest", "gemini-flash-lite-latest")
_FIND_BLOCK_S     = 600.0     # 10 minutes
_FIND_TIMEOUT_S   = 20.0
_FIND_BLOCKED: dict[str, float] = {}   # model → monotonic time until which it is blocked
_OWN_WINDOW_TAGS  = ("mark liii", "jarvis")


def _own_window_titles() -> tuple[str, ...]:
    tags = list(_OWN_WINDOW_TAGS)
    try:
        nm = (_load_config().get("assistant_name") or "").strip().lower()
        if nm:
            tags.append(nm)
    except Exception:
        pass
    return tuple(tags)


def _active_window_rect() -> tuple[tuple[int, int, int, int], str] | None:
    """(left, top, width, height), title of the foreground window — or None if
    there is none / it is our own window / it is minimised."""
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win is None:
            return None
        title = (win.title or "").strip()
        tl = title.lower()
        if not title or any(tag in tl for tag in _own_window_titles()):
            return None
        if getattr(win, "isMinimized", False):
            return None
        l, t, w, h = int(win.left), int(win.top), int(win.width), int(win.height)
        if w < 40 or h < 40:
            return None
        return (l, t, w, h), title
    except Exception:
        return None


def _grab(region: tuple[int, int, int, int] | None):
    """Screenshot via mss. region=(left, top, width, height) or None for the
    whole virtual desktop. Returns (png_bytes, (left, top, width, height))."""
    import mss
    import mss.tools
    with mss.mss() as sct:
        virt = sct.monitors[0]   # whole virtual desktop
        vl, vt = int(virt["left"]), int(virt["top"])
        vw, vh = int(virt["width"]), int(virt["height"])
        if region is None:
            l, t, w, h = vl, vt, vw, vh
        else:
            l, t, w, h = region
            # Clamp to the virtual desktop (windows can hang off-screen).
            r = min(l + w, vl + vw)
            b = min(t + h, vt + vh)
            l = max(l, vl)
            t = max(t, vt)
            w, h = max(1, r - l), max(1, b - t)
        shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
        png = mss.tools.to_png(shot.rgb, shot.size)
        return png, (l, t, w, h)


def _find_model_order() -> list[str]:
    now = time.monotonic()
    ready   = [m for m in _FIND_MODELS if _FIND_BLOCKED.get(m, 0.0) <= now]
    blocked = [m for m in _FIND_MODELS if m not in ready]
    return ready + blocked        # blocked ones only as a last resort


def _is_overload(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in (
        "503", "504", "429", "unavailable", "resource_exhausted", "resource exhausted",
        "overloaded", "deadline", "rate limit", "quota",
    ))


def _ask_box(client, gtypes, png: bytes, description: str, deadline: float):
    """One model round-trip. Returns [ymin, xmin, ymax, xmax] on the 0–1000 grid
    or None (NOT_FOUND / all models failed)."""
    prompt = (
        "This is a screenshot of a computer screen. Find the UI element described as: "
        f"'{description}'. Answer with ONLY a JSON object of the form "
        '{"box_2d": [ymin, xmin, ymax, xmax]} using the 0-1000 coordinate grid '
        "(top-left is 0,0, bottom-right is 1000,1000). Make the box as tight as "
        "possible around the exact element (the button, icon, field or text itself, "
        "not the region around it). If the element is not visible, answer exactly: "
        "NOT_FOUND"
    )
    last_err = ""
    for model in _find_model_order():
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            break
        timeout_ms = int(min(_FIND_TIMEOUT_S, remaining) * 1000)
        try:
            resp = client.models.generate_content(
                model=model,
                contents=[gtypes.Part.from_bytes(data=png, mime_type="image/png"), prompt],
                config=gtypes.GenerateContentConfig(
                    temperature=0.0,
                    http_options=gtypes.HttpOptions(timeout=timeout_ms),
                ),
            )
            text = (resp.text or "").strip()
            if "NOT_FOUND" in text.upper():
                return None
            m = re.search(r"\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]", text)
            if not m:
                last_err = f"{model}: unparsable answer {text[:80]!r}"
                continue
            box = [int(m.group(i)) for i in range(1, 5)]
            box = [max(0, min(1000, v)) for v in box]
            return box
        except Exception as e:
            last_err = f"{model}: {e}"
            if _is_overload(str(e)):
                _FIND_BLOCKED[model] = time.monotonic() + _FIND_BLOCK_S
                print(f"[ComputerControl] ⚠️ {model} overloaded — blocked for 10 min, trying next")
            else:
                print(f"[ComputerControl] ⚠️ {model} failed: {e}")
            continue
    if last_err:
        print(f"[ComputerControl] ⚠️ screen_find: {last_err}")
    return None


def _screen_find(description: str) -> tuple[int, int] | None:
    api_key = _get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None

    t0       = time.monotonic()
    deadline = t0 + _FIND_TIMEOUT_S
    try:
        from google import genai
        from google.genai import types as gtypes

        # ── 1. capture ─────────────────────────────────────────────────────
        region = None
        scope  = "full screen"
        aw = _active_window_rect()
        if aw is not None:
            region = aw[0]
            scope  = "active window"
        png, (L, T, W, H) = _grab(region)

        client = genai.Client(api_key=api_key)

        # ── 2. first pass: box on the 0–1000 grid → pixels ────────────────
        box = _ask_box(client, gtypes, png, description, deadline)
        if box is None:
            print(f"[ComputerControl] screen_find '{description}' -> NOT_FOUND [{scope}]")
            return None
        ymin, xmin, ymax, xmax = box
        bx1 = L + xmin / 1000.0 * W
        by1 = T + ymin / 1000.0 * H
        bx2 = L + xmax / 1000.0 * W
        by2 = T + ymax / 1000.0 * H

        # ── 3. zoom pass when the box is large ────────────────────────────
        bw, bh = bx2 - bx1, by2 - by1
        big = (bw > 0.18 * W) or (bh > 0.18 * H) or (bw * bh > 0.04 * W * H)
        if big and (deadline - time.monotonic()) > 4.0:
            pad = 0.35
            zl = int(max(L, bx1 - bw * pad))
            zt = int(max(T, by1 - bh * pad))
            zr = int(min(L + W, bx2 + bw * pad))
            zb = int(min(T + H, by2 + bh * pad))
            zw, zh = max(1, zr - zl), max(1, zb - zt)
            if zw >= 24 and zh >= 24:
                png2, (L2, T2, W2, H2) = _grab((zl, zt, zw, zh))
                box2 = _ask_box(client, gtypes, png2, description, deadline)
                if box2 is not None:
                    y1, x1, y2, x2 = box2
                    bx1 = L2 + x1 / 1000.0 * W2
                    by1 = T2 + y1 / 1000.0 * H2
                    bx2 = L2 + x2 / 1000.0 * W2
                    by2 = T2 + y2 / 1000.0 * H2
                    scope += " +zoom"

        x = int(round((bx1 + bx2) / 2.0))
        y = int(round((by1 + by2) / 2.0))
        print(f"[ComputerControl] screen_find '{description}' -> ({x}, {y}) [{scope}] "
              f"{time.monotonic() - t0:.1f}s")
        return x, y

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")

    return None

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
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
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
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if not action:
        return "No action specified for computer_control."

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

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
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc   = params.get("description", "")
            coords = _screen_find(desc)
            if coords:
                # Glide to the target first: a visible move lets the user see
                # where the click is about to land, and some apps only arm a
                # control on hover.
                _require_pyautogui()
                pyautogui.moveTo(coords[0], coords[1], duration=0.25)
                time.sleep(0.1)
                _click(x=coords[0], y=coords[1])
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"
            },
            "text": {
                "type": "STRING",
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "description": "Y coordinate"
            },
            "keys": {
                "type": "STRING",
                "description": "Key combination e.g. 'ctrl+c'"
            },
            "key": {
                "type": "STRING",
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down | left | right"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount (default: 3)"
            },
            "seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait"
            },
            "title": {
                "type": "STRING",
                "description": "Window title for focus_window"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for screen_find/screen_click"
            },
            "type": {
                "type": "STRING",
                "description": "Data type for random_data"
            },
            "field": {
                "type": "STRING",
                "description": "Field for user_data: name|email|city"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
