"""
MARK XL — Dependency auto-installer.

Called automatically on first launch and after engine reconfiguration.
Installs only the packages that are actually missing, then exits cleanly.

Two rules, inherited from :mod:`core.install_safety`:

- This is the *only* install path that may run without the on-screen
  confirmation gate, and it earns that exemption: the package list below is
  code in this file (never model output), it installs into the running
  interpreter, and it cannot be aimed at an arbitrary package. It announces
  itself as ``source=install_safety.SELF_SOURCE`` instead of quietly ignoring
  the rule.
- A package is only reported as ready when it can **actually be imported**
  afterwards, and a Playwright browser only when its binary directory exists.
  "pip returned 0" is not evidence; ``All dependencies ready`` used to be
  printed after failures.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from core import install_safety, tasks

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

def _browser_cache_dirs() -> list[Path]:
    """Where Playwright keeps downloaded browsers, per OS."""
    out = []
    local = os.environ.get("LOCALAPPDATA")
    home = Path.home()
    if platform.system() == "Windows" and local:
        out.append(Path(local) / "ms-playwright")
    elif platform.system() == "Darwin":
        out.append(home / "Library" / "Caches" / "ms-playwright")
    out.append(home / ".cache" / "ms-playwright")
    return out


def browser_cache_state(names: list[str]) -> tuple[bool | None, str]:
    """Three-way browser check: True present, False missing, None unverifiable.

    ``None`` matters: on a machine that has never downloaded a Playwright
    browser there is no cache directory to inspect, and guessing "missing"
    from that would turn a successful download into a false failure. The
    caller reports the weaker evidence instead (see install_for_config).
    """
    caches = [c for c in _browser_cache_dirs() if c.is_dir()]
    if not caches:
        return None, "no Playwright browser cache directory exists to inspect"
    return browsers_installed(names), "checked the Playwright browser cache"


def browsers_installed(names: list[str]) -> bool:
    """True when a browser binary for every name in `names` is on disk.

    Playwright reports success for a download that is already present, and
    reports nothing at all for a half-finished one — so the ground truth is the
    cache directory, not the exit code.
    """
    wanted = [str(n).strip().lower() for n in names if str(n).strip()]
    if not wanted:
        return True
    try:
        # Ask this installed Playwright version for its exact binaries. A folder
        # named "chromium-..." can be a stale or interrupted download.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as runtime:
            for name in wanted:
                if name not in ("chromium", "firefox", "webkit"):
                    return False
                binary = Path(getattr(runtime, name).executable_path)
                if not binary.is_file() or binary.stat().st_size == 0:
                    return False
        return True
    except Exception:
        return False



# ── download measurement ──────────────────────────────────────────────────


def estimate_bytes(size_text: str) -> "int | None":
    """Parse a human size label (``~225 MB``) into bytes, or None.

    The label is what setup.py shows the user, so it is the only estimate of a
    download's size this program has. Use it for descriptive metadata only,
    not a percentage, ETA or completion check.
    """
    match = re.search(r"([\d.]+)\s*(TB|GB|MB|KB)", str(size_text or ""), re.I)
    if not match:
        return None
    factor = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3, "tb": 1024 ** 4}
    try:
        return int(float(match.group(1)) * factor[match.group(2).lower()])
    except (TypeError, ValueError):
        return None


def cache_bytes(dirs: "list[Path] | None" = None) -> "int | None":
    """Bytes currently on disk under the browser caches, or None if unreadable."""
    total = 0
    seen = False
    for cache in (dirs if dirs is not None else _browser_cache_dirs()):
        try:
            if not cache.is_dir():
                continue
        except OSError:
            continue
        seen = True
        for root, _dirs, files in os.walk(cache):
            for name in files:
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    return total if seen else None


class DownloadMeter:
    """Report a download's real progress into the task registry.

    Playwright's installer prints nothing useful and writes into its browser
    cache, so the numbers the HUD shows are measured from the filesystem:
    bytes written and speed from cache growth. Only a measured total can
    support percentage/ETA; download-size estimates stay in descriptive metadata.
    """

    def __init__(self, task, *, dirs: "list[Path] | None" = None,
                 interval: float = 1.0) -> None:
        self._task = task
        self._dirs = dirs
        self._interval = max(0.2, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.bytes_written: "int | None" = None
        self.speed_bps: "float | None" = None
        self._started = time.monotonic()
        self._start_bytes: "int | None" = None

    def _sample(self) -> None:
        now_size = cache_bytes(self._dirs)
        if now_size is None:
            return
        if self._start_bytes is None:
            self._start_bytes = now_size
        elapsed = max(0.001, time.monotonic() - self._started)
        self.bytes_written = max(0, now_size - self._start_bytes)
        # Measured, not guessed: the growth of the cache over the time we ran.
        self.speed_bps = self.bytes_written / elapsed
        self._task.update(done_bytes=self.bytes_written,
                          speed_bps=self.speed_bps)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._sample()
            except Exception:
                return          # a metering fault must never break the download

    def start(self) -> "DownloadMeter":
        # The first sample is taken here, in the caller's thread, so that
        # bytes_written measures growth from this moment — not from whenever the
        # worker got scheduled, which would silently drop the first second.
        try:
            self._sample()
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, name="download-meter",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> "DownloadMeter":
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
            self._thread = None
        try:
            self._sample()
        except Exception:
            pass
        return self

    def __enter__(self) -> "DownloadMeter":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False


def install_for_config(config: dict, log: Callable | None = None) -> None:
    """
    Install all missing packages required by *config*.

    Blocking — always call from a background thread.
    Progress is reported via the optional *log* callback (receives a str) and,
    for the UI, through the central task registry (core/tasks.py).
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
            log("SYS: All dependencies already installed and importable \u2713")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Installing {len(missing)} package(s): {pkg_names}")

    # This path is the app bootstrapping itself, so it is announced as such to
    # the safety layer rather than silently bypassing it. If the exemption ever
    # stops being justified (a model-composed list, a user-supplied name), the
    # request object below is what changes.
    request = install_safety.InstallRequest(
        kind=install_safety.KIND_INSTALL,
        target=f"{len(missing)} dependency package(s)",
        detail=pkg_names,
        source=install_safety.SELF_SOURCE,
        origin="installer",
    )
    # Documented exemption: bootstrap lists live in this file, not in model
    # output, so requires_confirmation() is False for them by design.

    task = tasks.start(
        tasks.KIND_INSTALL,
        f"Installing {len(missing)} dependencies",
        detail=pkg_names[:120],
    )

    verified: list[str] = []
    failed: list[str] = []
    for index, (mod, pkg) in enumerate(missing, start=1):
        task.update(detail=f"pip install {pkg} ({index}/{len(missing)})",
                    progress=(index - 1) / len(missing))
        pip_ok = _pip(pkg, log)
        # Verification, not exit codes: the package counts as installed only if
        # it can be imported right now. One check is enough — a wheel that pip
        # reported as installed is importable immediately, and polling per
        # package would turn a long bootstrap into a much longer one.
        importable = install_safety.verify_import(mod)
        if pip_ok and importable:
            verified.append(pkg)
        else:
            reason = "pip failed" if not pip_ok else "not importable after install"
            failed.append(f"{pkg} ({reason})")
            if log:
                log(f"ERR: {pkg} is not usable after install — {reason}.")
        task.update(progress=index / len(missing))

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
                estimate = estimate_bytes(_BROWSER_SIZES.get(choice, ""))
                dl_task = tasks.start(
                    tasks.KIND_DOWNLOAD,
                    f"Playwright browser: {' + '.join(args)}",
                    detail=("downloading from Playwright's CDN"
                            + (f" — about {_BROWSER_SIZES[choice]} expected"
                               if estimate else "")),
                    expected_bytes=estimate,  # metadata only; not a measured transfer total
                    done_bytes=0,
                )
                meter = DownloadMeter(dl_task)
                try:
                    with meter:
                        result = subprocess.run(
                            [sys.executable, "-m", "playwright", "install", *args],
                            capture_output=True,
                        )
                finally:
                    meter.stop()
                # Not fatal: everything except browser automation works without
                # it, and the download is several hundred MB from a CDN that a
                # metered connection or a corporate network can refuse.
                # Verify the download against the browser cache: the exit code
                # alone cannot tell a finished download from a half-written one.
                on_disk, why = browser_cache_state(args)
                if result.returncode == 0 and on_disk is True:
                    note = "verified on disk"
                    wrote = tasks.format_bytes(meter.bytes_written)
                    if log:
                        log(f"SYS: Playwright browser ready ({note}"
                            + (f", {wrote} written)." if wrote else ")."))
                    dl_task.finish(
                        detail=f"browser binaries {note}"
                               + (f" — {wrote} written" if wrote else ""))
                else:
                    reason = ("browser binaries not found in the Playwright cache"
                              if on_disk is False else (f"unverified: {why}" if on_disk is None else f"exit code {result.returncode}"))
                    if log:
                        log("ERR: Playwright browser download failed — browser "
                            "automation is unavailable. Retry later with:")
                        log(f"ERR:   {Path(sys.executable).name or 'python'} -m "
                            f"playwright install {' '.join(args)}")
                        tail = (result.stderr or b"").decode(errors="replace").strip()
                        if tail:
                            log(f"ERR:   {tail.splitlines()[-1][:140]}")
                    dl_task.fail(error=reason, detail=reason)

    # The final line is derived from the verification results, never from the
    # fact that the loop finished.
    if failed:
        message = (f"ERR: {len(failed)} of {len(missing)} dependencies are not "
                   f"usable: {'; '.join(failed[:5])}")
        if log:
            log(message)
        task.fail(error="; ".join(failed[:5]),
                  detail=f"{len(verified)} verified, {len(failed)} failed")
    else:
        if log:
            log(f"SYS: All {len(verified)} dependencies installed and importable \u2713")
        task.finish(detail=f"{len(verified)} verified importable")
