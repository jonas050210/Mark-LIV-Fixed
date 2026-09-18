"""
MARK XL — Dependency auto-installer.

Called automatically on first launch and after engine reconfiguration.
Installs only the packages that are actually missing, then exits cleanly.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable

# ── Package lists ─────────────────────────────────────────────────────────
# Each entry: (import_name, pip_package_name)

_CORE: list[tuple[str, str]] = [
    ("psutil",             "psutil"),
    ("PIL",                "pillow"),
    ("sounddevice",        "sounddevice"),
    ("numpy",              "numpy"),
    ("requests",           "requests"),
    ("bs4",                "beautifulsoup4"),
    ("ddgs",               "ddgs"),
    ("pyautogui",          "pyautogui"),
    ("pyperclip",          "pyperclip"),
    ("pygetwindow",        "pygetwindow"),
    ("mss",                "mss"),
    ("cv2",                "opencv-python"),
    ("soundfile",          "soundfile"),
    ("miniaudio",          "miniaudio"),
    ("send2trash",         "send2trash"),
    ("pptx",               "python-pptx"),
    ("youtube_transcript_api", "youtube-transcript-api"),
]

# Windows-only (pywinauto, pycaw, win10toast, comtypes)
_WINDOWS: list[tuple[str, str]] = [
    ("comtypes",   "comtypes"),
    ("pycaw",      "pycaw"),
    ("win10toast", "win10toast"),
    ("pywinauto",  "pywinauto"),
]

# STT engine packages
_STT: dict[str, list[tuple[str, str]]] = {
    "whisper": [("faster_whisper", "faster-whisper")],
    "vosk":    [("vosk",           "vosk")],
}

# Playwright browser binaries. These are the biggest thing any MARK LIV install
# ever downloads, so the choice is explicit and can be pinned by the environment
# (same variable name setup.py reads).
BROWSER_ENV = "MARK_LIV_BROWSERS"
_BROWSER_ARGS = {
    "none": [],
    "chromium": ["chromium"],
    "firefox": ["firefox"],
    "chromium-firefox": ["chromium", "firefox"],
}
_BROWSER_SIZES = {
    "none": "0 MB",
    "chromium": "~225 MB",
    "firefox": "~86 MB",
    "chromium-firefox": "~311 MB",
}


def _browser_choice() -> str:
    """Which browsers this install should fetch (see BROWSER_ENV)."""
    raw = (os.environ.get(BROWSER_ENV) or "").strip().lower().replace("_", "-")
    if not raw:
        return "chromium"          # covers Chrome/Edge/Brave/Vivaldi/Opera
    if raw in ("none", "skip", "minimal"):
        return "none"
    return raw if raw in _BROWSER_ARGS else "chromium"


# TTS engine packages
_TTS: dict[str, list[tuple[str, str]]] = {
    "edgetts":    [("edge_tts", "edge-tts")],
    # kokoro>=0.9 dropped AlbertModel/AutoModel from transformers — version pin is critical
    "kokoro":     [("kokoro",   "kokoro>=0.9"), ("soundfile", "soundfile")],
    "elevenlabs": [],   # uses only requests, already in core
}


# ── Helpers ───────────────────────────────────────────────────────────────

def _available(module: str) -> bool:
    """Return True if the module can be imported (no actual import)."""
    return importlib.util.find_spec(module) is not None


def _pip(package: str, log: Callable | None = None) -> bool:
    if log:
        log(f"SYS: pip install {package} …")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", package,
            "--quiet", "--disable-pip-version-check",
        ],
        capture_output=True,
    )
    ok = result.returncode == 0
    if not ok and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: {package} install failed — {stderr[:140]}")
    return ok


# ── Public API ────────────────────────────────────────────────────────────

def install_for_config(config: dict, log: Callable | None = None) -> None:
    """
    Install all missing packages required by *config*.

    Blocking — always call from a background thread.
    Progress is reported via the optional *log* callback (receives a str).
    """
    stt = config.get("stt_engine", "whisper").lower()
    tts = config.get("tts_engine", "edgetts").lower()

    needed: list[tuple[str, str]] = list(_CORE)
    needed += _STT.get(stt, [])
    needed += _TTS.get(tts, [])
    if platform.system() == "Windows":
        needed += _WINDOWS

    # Deduplicate (preserve order, key = pip name)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for mod, pkg in needed:
        if pkg not in seen:
            seen.add(pkg)
            unique.append((mod, pkg))

    missing = [(mod, pkg) for mod, pkg in unique if not _available(mod)]

    if not missing:
        if log:
            log("SYS: All dependencies already installed ✓")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Installing {len(missing)} package(s): {pkg_names}")

    for _mod, pkg in missing:
        _pip(pkg, log)

    # Playwright: install the package, then the browser binaries it drives.
    if not _available("playwright"):
        if not _pip("playwright", log):
            if log:
                log("ERR: playwright package failed to install — browser "
                    "automation stays unavailable.")
        else:
            choice = _browser_choice()
            args = _BROWSER_ARGS[choice]
            if not args:
                if log:
                    log(f"SYS: Skipping Playwright browsers ({BROWSER_ENV}=none). "
                        f"Browser automation stays off until you run:")
                    log(f"SYS:   {Path(sys.executable).name or 'python'} -m "
                        f"playwright install chromium")
            else:
                if log:
                    log(f"SYS: Downloading Playwright browser ({' + '.join(args)}, "
                        f"{_BROWSER_SIZES[choice]} — one-time, this is the big one)…")
                result = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", *args],
                    capture_output=True,
                )
                # Not fatal: everything except browser automation works without
                # it, and the download is several hundred MB from a CDN that a
                # metered connection or a corporate network can refuse.
                if result.returncode == 0:
                    if log:
                        log("SYS: Playwright browser ready.")
                elif log:
                    log("ERR: Playwright browser download failed — browser "
                        "automation is unavailable. Retry later with:")
                    log(f"ERR:   {Path(sys.executable).name or 'python'} -m "
                        f"playwright install {' '.join(args)}")
                    tail = (result.stderr or b"").decode(errors="replace").strip()
                    if tail:
                        log(f"ERR:   {tail.splitlines()[-1][:140]}")

    if log:
        log("SYS: All dependencies ready ✓")
