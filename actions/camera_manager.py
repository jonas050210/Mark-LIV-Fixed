"""Camera setup and diagnostics without guessing which webcam to use.

Vision requests themselves still go through the live-session ``screen_process``
tool, which is what attaches pixels to Gemini.  This action only discovers,
checks, and selects a camera index so a person can fix a wrong/default webcam
before asking MARK LIV to look at it.
"""
from __future__ import annotations

from actions import screen_processor
from memory.config_manager import load_api_keys, patch_config

_MAX_CAMERA_INDEX = 32
_SCAN_LIMIT = 8


def _clean_index(value) -> int:
    if isinstance(value, bool):
        raise ValueError("camera index must be a number")
    try:
        index = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("camera index must be a number") from exc
    if not 0 <= index <= _MAX_CAMERA_INDEX:
        raise ValueError(f"camera index must be between 0 and {_MAX_CAMERA_INDEX}")
    return index


def _ready() -> tuple[bool, str]:
    if not screen_processor._CV2:
        return False, "OpenCV is not installed. Run setup again to install camera support."
    if not screen_processor._NUMPY:
        return False, "NumPy is not installed. Run setup again to install camera support."
    return True, ""


def _configured_index() -> int:
    raw = load_api_keys().get("camera_index", 0)
    try:
        return _clean_index(raw)
    except ValueError:
        return 0


def _scan() -> list[int]:
    usable = []
    backend = screen_processor._cv2_backend()
    for index in range(_SCAN_LIMIT):
        if screen_processor._probe_camera(index, backend, warmup=4):
            usable.append(index)
    return usable


def camera_manager(parameters: dict | None = None, player=None) -> str:
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "status").strip().casefold().replace(" ", "_")
    ready, reason = _ready()
    configured = _configured_index()

    if action in {"status", "get", "show"}:
        if not ready:
            return reason
        return (
            f"Camera support is ready. Configured camera index: {configured}. "
            "Use scan to find usable cameras, set with an index, or test the configured camera."
        )

    if not ready:
        return reason

    if action in {"scan", "list", "discover"}:
        found = _scan()
        if not found:
            return (
                f"No usable camera was found in indexes 0–{_SCAN_LIMIT - 1}. Check the privacy shutter, "
                "Windows camera privacy permission, and whether another app is using the webcam."
            )
        return (
            "Usable camera indexes: " + ", ".join(map(str, found)) + ". "
            f"Currently configured: {configured}."
        )

    try:
        requested = _clean_index(p.get("index", p.get("camera_index")))
    except ValueError as exc:
        return str(exc) + "."

    backend = screen_processor._cv2_backend()
    usable = screen_processor._probe_camera(requested, backend, warmup=6)
    if not usable:
        return (
            f"Camera {requested} did not produce a usable frame. I left camera {configured} selected. "
            "Check its privacy shutter/permission or run a camera scan."
        )

    if action in {"test", "check"}:
        return f"Camera {requested} produced a usable test frame."
    if action in {"set", "select", "use"}:
        try:
            patch_config(camera_index=requested)
        except Exception as exc:
            return f"Camera {requested} works, but I could not save the selection ({type(exc).__name__})."
        return f"Camera {requested} is working and is now selected for webcam requests."
    return "Use status, scan, test, or set for camera setup."


TOOL = {
    "name": "camera_manager",
    "description": (
        "Checks camera support, lists usable webcam indexes, tests a chosen webcam, or saves "
        "which camera webcam vision should use. Use this to fix a wrong camera, a black image, "
        "or a camera not found. Do not use it to analyse what is in a picture; use screen_process "
        "with angle camera for that."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["status", "scan", "test", "set"],
                "description": "status | scan | test | set",
            },
            "index": {
                "type": "INTEGER",
                "minimum": 0,
                "maximum": _MAX_CAMERA_INDEX,
                "description": "Camera index for test or set.",
            },
        },
        "required": ["action"],
    },
    "handler": camera_manager,
    "category": "vision",
}
