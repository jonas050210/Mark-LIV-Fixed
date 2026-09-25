"""Best-effort application icons for the launcher panel.

A launcher that renders as a column of identical text rows is slower to scan
than one with icons, but an icon must never be allowed to break a launch: every
extraction path here degrades to "no icon" rather than raising.

Icons are cached as PNG files under ``config/app_icons``. The cache key is a
digest of the entry's launch target, so reinstalling an application to a new
path produces a new icon instead of a stale one.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from core.app_index import AppEntry

_OS = platform.system()
BASE_DIR = Path(__file__).resolve().parent.parent
ICON_DIR = BASE_DIR / "config" / "app_icons"
ICON_SIZE = 64
MAX_ICON_BYTES = 2_000_000
_SAFE_NAME = re.compile(r"^[a-f0-9]{32}\.png$")


def _cache_path(entry: AppEntry) -> Path:
    digest = hashlib.sha256(f"{entry.kind}:{entry.target}".encode("utf-8")).hexdigest()[:32]
    return ICON_DIR / f"{digest}.png"


def _ensure_dir() -> bool:
    try:
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _save_png(image) -> bytes | None:
    """Normalise any PIL image to a bounded square PNG."""
    import io

    from PIL import Image

    try:
        image = image.convert("RGBA")
        image.thumbnail((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
        canvas.paste(image, ((ICON_SIZE - image.width) // 2, (ICON_SIZE - image.height) // 2))
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        return data if len(data) <= MAX_ICON_BYTES else None
    except Exception:
        return None


def _from_image_file(path: Path) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > 20_000_000:
            return None
        from PIL import Image

        with Image.open(path) as image:
            return _save_png(image)
    except Exception:
        return None


# ── Linux: XDG icon themes ───────────────────────────────────────────────────

def _linux_icon_file(name: str) -> Path | None:
    candidate = Path(name)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    roots = [
        Path.home() / ".local" / "share" / "icons",
        Path("/usr/share/icons"),
        Path("/usr/share/pixmaps"),
        Path("/usr/local/share/icons"),
    ]
    wanted = {f"{name}.png", f"{name}.xpm"}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            # Prefer a large, explicitly sized directory over the first hit.
            matches = [
                path for path in root.rglob("*")
                if path.name in wanted and path.is_file()
            ][:200]
        except OSError:
            continue
        if matches:
            matches.sort(key=lambda path: ("512" in str(path), "256" in str(path),
                                           "128" in str(path), "64" in str(path)),
                         reverse=True)
            return matches[0]
    return None


def _linux_icon_name(desktop_target: str) -> str:
    """Find the Icon= key for a binary we indexed from a desktop entry."""
    stem = Path(desktop_target).stem
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/share:/usr/local/share").split(":")
    roots = [Path(item) / "applications" for item in data_dirs if item]
    roots.append(Path.home() / ".local" / "share" / "applications")
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.glob("*.desktop"))[:2_000]
        except OSError:
            continue
        for path in entries:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:64_000]
            except OSError:
                continue
            if f"Exec={stem}" not in text and f"/{stem}" not in text:
                continue
            for line in text.splitlines():
                if line.startswith("Icon="):
                    return line.split("=", 1)[1].strip()
    return ""


# ── macOS: .icns inside the bundle ───────────────────────────────────────────

def _macos_icon(bundle: Path) -> bytes | None:
    resources = bundle / "Contents" / "Resources"
    if not resources.is_dir():
        return None
    try:
        icons = sorted(resources.glob("*.icns"))[:10]
    except OSError:
        return None
    for path in icons:
        data = _from_image_file(path)
        if data:
            return data
    return None


# ── Windows: extract the associated icon via .NET ────────────────────────────

def _windows_icon(target: str, destination: Path) -> bytes | None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return None
    if not Path(target).is_file():
        return None
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        "$icon = [System.Drawing.Icon]::ExtractAssociatedIcon($args[0]); "
        "if ($icon -eq $null) { exit 1 }; "
        "$bitmap = $icon.ToBitmap(); "
        "$bitmap.Save($args[1], [System.Drawing.Imaging.ImageFormat]::Png); "
        "$bitmap.Dispose(); $icon.Dispose()"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script,
             target, str(destination)],
            capture_output=True, timeout=20,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return _from_image_file(destination)


def _extract(entry: AppEntry, destination: Path) -> bytes | None:
    if entry.kind == "bundle":
        return _macos_icon(Path(entry.target))
    if _OS == "Windows":
        target = entry.target
        if entry.kind == "lnk":
            # ExtractAssociatedIcon resolves a shortcut to its target itself.
            return _windows_icon(target, destination)
        if entry.kind == "exec":
            return _windows_icon(target, destination)
        return None
    if _OS == "Linux" and entry.kind == "exec":
        name = _linux_icon_name(entry.target) or Path(entry.target).stem
        path = _linux_icon_file(name)
        return _from_image_file(path) if path else None
    return None


def icon_png(entry: AppEntry) -> bytes | None:
    """Return a cached 64×64 PNG for an application, or None when unavailable."""
    if not _ensure_dir():
        return None
    cached = _cache_path(entry)
    try:
        if cached.is_file() and 0 < cached.stat().st_size <= MAX_ICON_BYTES:
            return cached.read_bytes()
    except OSError:
        pass
    try:
        data = _extract(entry, cached)
    except Exception:
        data = None
    if not data:
        return None
    try:
        cached.write_bytes(data)
    except OSError:
        pass
    return data


def has_icon(entry: AppEntry) -> bool:
    """Whether a cached icon already exists, without attempting extraction."""
    try:
        return _cache_path(entry).is_file()
    except OSError:
        return False


def clear_cache() -> int:
    """Delete cached icons. Only files this module created are removed."""
    removed = 0
    if not ICON_DIR.is_dir():
        return 0
    for path in ICON_DIR.iterdir():
        if path.is_file() and _SAFE_NAME.fullmatch(path.name):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed
