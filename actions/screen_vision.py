"""
On-demand screen understanding — "what do you see?", "where is the X button?",
"which app is in front?".

Architecture note (why not native Gemini Computer Use): the project's Gemini
access is the Live voice session plus one-shot REST/Live turns through
core/gemini.py. Native computer-use would need a separate agentic loop with its
own model, per-step screenshot uploads, and substantially more API traffic —
against the $0 philosophy and the existing architecture. This module is the
lightweight alternative the task brief allows: a single screenshot, analysed by
the same Gemini ladder everything else uses, strictly on demand.

Rules this module enforces:
  - on-demand capture only, with a cooldown between analysed frames (no
    continuous upload, no background streaming);
  - one frame per call, downscaled to ≤1280×720 JPEG (small token cost);
  - coordinates are requested NORMALIZED (0-1000) and mapped to real pixels
    locally, so resolution confusion cannot misplace a click;
  - `read` refuses anything shaped like a password / key / secret field, and
    every prompt orders the model to redact credentials instead of reading them;
  - read-only: this module never clicks, types, or moves the mouse. Acting on
    what it found stays in computer_control behind the existing gates.
"""
from __future__ import annotations

import io
import re
import time

try:
    from actions import screen_processor as _sp
except ImportError:  # pragma: no cover
    _sp = None

_COOLDOWN_S = 3.0        # min seconds between analysed frames (API thrift)
_TIMEOUT_MS = 30_000     # vision calls carry a big image; give them room
_LAST_CALL = 0.0

# Bilingual refusal list for `read`: anything matching these is never OCR'd.
_SENSITIVE_RE = re.compile(
    r"passw|passwd|passwort|pwd|api[\s_\-]*key|secret|token|private[\s_\-]*key|"
    r"\bpin\b|\b2fa\b|\botp\b|authenticator|credit[\s_\-]*card|kreditkarte|"
    r"\bcvv\b|\bcvc\b|iban|seed[\s_\-]*phrase|mnemonic|ssn|sozialversicherung",
    re.IGNORECASE,
)

_REDACT_LINE = ("Do not transcribe, repeat, or describe any password, API key, "
                "secret, token, PIN, card number, or other credential visible in "
                "the image. If the answer would be one, write [redacted] instead.")


def _cooled_down() -> float:
    """Seconds remaining on the capture cooldown (0 when a capture may proceed)."""
    return max(0.0, _COOLDOWN_S - (time.monotonic() - _LAST_CALL))


def _mark_called() -> None:
    global _LAST_CALL
    _LAST_CALL = time.monotonic()


def _screen_size() -> tuple[int, int]:
    try:
        import pyautogui
        s = pyautogui.size()
        return int(s.width), int(s.height)
    except Exception:
        pass
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            return int(mon["width"]), int(mon["height"])
    except Exception:
        return 1920, 1080


def _capture() -> tuple[bytes, str]:
    """One compressed frame via the shared capture path. Raises with a message.

    The cooldown is marked here, on success only: a failed capture must not
    burn the budget and lock the next (possibly working) attempt out.
    """
    if _sp is None:
        raise RuntimeError("screen capture is unavailable (screen_processor failed to load)")
    img_b, mime_t = _sp._capture_screen()
    if not img_b:
        raise RuntimeError("screen capture returned an empty frame")
    _mark_called()
    return img_b, mime_t


def _gemini_parts(img_b: bytes, mime_t: str, prompt: str) -> list:
    from google.genai import types as gtypes
    return [gtypes.Part.from_bytes(data=img_b, mime_type=mime_t), prompt]


def find_element(description: str, timeout_ms: int = _TIMEOUT_MS) -> tuple[int, int] | None:
    """Natural-language element → real screen pixels, or None.

    Shared implementation: computer_control's screen_find/screen_click delegate
    here instead of carrying their own capture+prompt+parse copy.
    """
    from core import gemini

    description = (description or "").strip()
    if not description:
        return None
    try:
        img_b, mime_t = _capture()
    except Exception as e:
        print(f"[ScreenVision] capture failed: {e}")
        return None
    w, h = _screen_size()
    prompt = (
        f"This is a screenshot of a computer screen. Locate the UI element "
        f"described as: '{description}'. Reply with ONLY JSON, nothing else: "
        f'{{\n  "found": true,\n  "x": <0-1000>,\n  "y": <0-1000>\n}} '
        f"where x/y are NORMALIZED coordinates (0 = left/top edge, 1000 = "
        f"right/bottom edge) of the element's CENTER. If the element is not "
        f"visible, reply exactly: {{\"found\": false}}. {_REDACT_LINE}"
    )
    try:
        parts = _gemini_parts(img_b, mime_t, prompt)
    except Exception as e:
        print(f"[ScreenVision] image part failed: {e}")
        return None
    data = gemini.as_json(parts, tier=gemini.SMART, timeout_ms=timeout_ms, default=None)
    if not isinstance(data, dict) or not data.get("found"):
        return None
    try:
        nx = float(data.get("x", -1))
        ny = float(data.get("y", -1))
    except (TypeError, ValueError):
        return None
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return None
    x = min(w - 1, max(0, round(nx / 1000 * w)))
    y = min(h - 1, max(0, round(ny / 1000 * h)))
    return x, y


def _describe(question: str) -> str:
    from core import gemini
    try:
        img_b, mime_t = _capture()
        parts = _gemini_parts(img_b, mime_t,
                              f"Look at this computer screenshot and answer briefly "
                              f"(2-4 sentences max): {question}\n{_REDACT_LINE}")
    except Exception as e:
        # The technical cause goes to the log; the spoken reply stays short —
        # nobody wants "libxcb-randr.so not found" read aloud.
        print(f"[ScreenVision] capture failed: {e}")
        return "I could not capture the screen right now."
    answer = gemini.text(parts, tier=gemini.SMART, timeout_ms=_TIMEOUT_MS, default="")
    return answer or "I looked at the screen but got no answer from the vision model."


def _read(target: str) -> str:
    from core import gemini
    if _SENSITIVE_RE.search(target or ""):
        return ("I will not read that — it looks like a password, key, or other "
                "secret field, and those stay off the model.")
    try:
        img_b, mime_t = _capture()
        parts = _gemini_parts(
            img_b, mime_t,
            f"Read the visible text of this UI element in the screenshot: '{target}'. "
            f"Reply with the text only, no commentary. {_REDACT_LINE}")
    except Exception as e:
        print(f"[ScreenVision] capture failed: {e}")
        return "I could not capture the screen right now."
    answer = gemini.text(parts, tier=gemini.SMART, timeout_ms=_TIMEOUT_MS, default="")
    return answer or "I could not read any text there."


def _identify() -> str:
    """Foreground app/window, fully local — no screenshot, no API call."""
    title = ""
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        if win is not None:
            title = (win.title or "").strip()
    except Exception:
        pass
    if not title:
        try:  # Windows fallback without pygetwindow
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
        except Exception:
            pass
    if title:
        return f"The active window is: {title}."
    return "I could not determine the active window."


def screen_vision(parameters=None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "describe") or "describe").strip().lower()
    target = str(params.get("target", "") or "").strip()
    question = str(params.get("question", "") or "").strip()

    if action in ("identify", "active", "window", "app"):
        return _identify()

    if action in ("find", "locate", "click_target", "where"):
        if not target:
            return "Tell me which element to find."
        wait = _cooled_down()
        if wait > 0:
            return (f"I just analysed the screen — ask again in {wait:.0f} seconds "
                    f"and I will look fresh.")
        print(f"[ScreenVision] find: '{target}'")
        coords = find_element(target)
        if coords is None:
            return f"I could not find '{target}' on the screen."
        return (f"Found '{target}' at screen coordinates x={coords[0]}, y={coords[1]}. "
                f"Use computer_control to click there if the user asked for it.")

    if action in ("read", "ocr", "text"):
        if not target:
            return "Tell me which field or text to read."
        wait = _cooled_down()
        if wait > 0:
            return (f"I just analysed the screen — ask again in {wait:.0f} seconds "
                    f"and I will look fresh.")
        print(f"[ScreenVision] read: '{target}'")
        return _read(target)

    # default: describe
    wait = _cooled_down()
    if wait > 0:
        return (f"I just analysed the screen — ask again in {wait:.0f} seconds "
                f"and I will look fresh.")
    print("[ScreenVision] describe")
    return _describe(question or "What is currently visible on the screen?")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "screen_vision",
    "description": (
        "Understands the current screen from a screenshot: describe what is "
        "visible, find a button/field/window by description (returns screen "
        "coordinates), identify the active application, or read visible text. "
        "On-demand only — captures one frame per call. Never reads passwords, "
        "keys, or secret fields. Read-only: it never clicks or types."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "describe | find | identify | read (default: describe)"
            },
            "target": {
                "type": "STRING",
                "description": "Element description for find (e.g. 'the blue Send button'), or field for read"
            },
            "question": {
                "type": "STRING",
                "description": "Question about the screen for describe"
            }
        },
        "required": []
    },
    "handler": screen_vision,
}
