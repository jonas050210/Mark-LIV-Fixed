"""Reliable Windows Explorer locations, search, and selection helpers.

The file action used to recurse from one guessed directory with a small directory
limit. This module resolves real known folders first, uses Everything's read-only
CLI when available, and falls back to a bounded, scored search. It never deletes
or changes files and never interpolates user text into a shell command.
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import time
import uuid
from pathlib import Path

_OS = platform.system()

_KNOWN_FOLDER_GUIDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}
_LOCATION_ALIASES = {
    "desktop": "desktop", "downloads": "downloads", "download": "downloads",
    "documents": "documents", "document": "documents", "docs": "documents",
    "pictures": "pictures", "photos": "pictures", "images": "pictures",
    "music": "music", "songs": "music", "videos": "videos", "video": "videos",
    "home": "home", "user folder": "home", "user": "home",
}
_SKIP_DIRS = {
    ".git", ".cache", ".npm", ".venv", "node_modules", "__pycache__",
    "appdata", "application data", "local settings",
}


def _known_folder_windows(guid_text: str) -> Path | None:
    if _OS != "Windows":
        return None
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
        guid = GUID()
        raw = uuid.UUID(guid_text).bytes_le
        ctypes.memmove(ctypes.byref(guid), raw, ctypes.sizeof(guid))
        pointer = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
        if result != 0 or not pointer.value:
            return None
        value = Path(pointer.value)
        ctypes.windll.ole32.CoTaskMemFree(pointer)
        return value
    except Exception:
        return None


def locations() -> dict[str, Path]:
    home = Path.home()
    out: dict[str, Path] = {"home": home}
    for name, guid in _KNOWN_FOLDER_GUIDS.items():
        native = _known_folder_windows(guid)
        if native is not None:
            out[name] = native
            continue
        if name == "desktop":
            fallback = os.environ.get("XDG_DESKTOP_DIR")
            out[name] = Path(fallback) if fallback else home / "Desktop"
        elif name == "downloads":
            fallback = os.environ.get("XDG_DOWNLOAD_DIR")
            out[name] = Path(fallback) if fallback else home / "Downloads"
        else:
            out[name] = home / name.capitalize()
    return out


def resolve_location(value: str | Path, *, allow_missing: bool = True) -> Path:
    raw = str(value or "").strip().strip('"').strip("'")
    key = " ".join(raw.casefold().split())
    known = locations()
    if key in _LOCATION_ALIASES:
        return known[_LOCATION_ALIASES[key]]
    normalized = raw.replace("\\", "/")
    head, separator, tail = normalized.partition("/")
    if separator and head.casefold() in _LOCATION_ALIASES:
        return known[_LOCATION_ALIASES[head.casefold()]] / tail
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.home() / path
    return path if allow_missing or path.exists() else path


def _everything_search(query: str, root: Path | None, limit: int) -> list[Path]:
    """Use Everything's console client when the user has it installed."""
    executable = shutil.which("es.exe") or shutil.which("es")
    if not executable:
        return []
    command = [executable, "-n", str(limit)]
    if root is not None and root.exists():
        command += ["-path", str(root)]
    command.append(query)
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=5, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode != 0:
            return []
        candidates = []
        root_resolved = root.resolve() if root is not None and root.exists() else None
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            path = Path(line.strip())
            if not path.exists():
                continue
            if root_resolved is not None:
                try:
                    if not path.resolve().is_relative_to(root_resolved):
                        continue
                except (OSError, ValueError):
                    continue
            candidates.append(path)
            if len(candidates) >= limit:
                break
        return candidates
    except (OSError, subprocess.SubprocessError):
        return []


def _score(path: Path, query: str) -> tuple[int, str]:
    wanted = query.casefold().strip()
    name = path.name.casefold()
    full = str(path).casefold()
    if name == wanted:
        score = 1000
    elif name.startswith(wanted):
        score = 700
    elif wanted in name:
        score = 500
    elif wanted in full:
        score = 250
    else:
        score = 0
    # Do not turn a non-match into a match merely because it is recent.
    # Recency is only a tie-breaker after the filename/path matched.
    if score:
        try:
            age_days = max(0, int((time.time() - path.stat().st_mtime) / 86400))
            score += max(0, 30 - min(age_days, 30))
        except OSError:
            pass
    return score, full


def _walk_search(query: str, roots: list[Path], extension: str, limit: int,
                 timeout: float = 8.0) -> list[Path]:
    deadline = time.monotonic() + timeout
    candidates: list[tuple[int, str, Path]] = []
    wanted_extension = extension.casefold().strip()
    if wanted_extension and not wanted_extension.startswith("."):
        wanted_extension = "." + wanted_extension
    for root in roots:
        if time.monotonic() >= deadline or not root.exists() or not root.is_dir():
            continue
        try:
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                if time.monotonic() >= deadline:
                    break
                dirs[:] = [d for d in dirs if d.casefold() not in _SKIP_DIRS and not d.startswith(".")]
                for filename in files:
                    if wanted_extension and Path(filename).suffix.casefold() != wanted_extension:
                        continue
                    path = Path(current) / filename
                    score, full = _score(path, query)
                    if score:
                        candidates.append((score, full, path))
        except (OSError, PermissionError):
            continue
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score_value, _full, path in candidates[:limit]]


def search(query: str, root: str | Path = "home", extension: str = "",
           limit: int = 20) -> list[Path]:
    query = str(query or "").strip()
    limit = max(1, min(int(limit or 20), 50))
    search_root = resolve_location(root)
    if not query and extension:
        query = str(extension).strip().lstrip("*.")
    if not query:
        return []
    exact_candidates = [resolve_location(query)]
    query_path = Path(query).expanduser()
    if not query_path.is_absolute():
        exact_candidates.insert(0, search_root / query_path)
    for exact in exact_candidates:
        if exact.exists() and exact.is_file():
            return [exact]
    roots = [search_root] if search_root.exists() and search_root.is_dir() else []
    results = _everything_search(query, search_root if roots else None, limit) if roots else []
    if extension:
        results = [p for p in results if p.suffix.casefold() == (extension if extension.startswith(".") else "." + extension).casefold()]
    if len(results) < limit:
        seen = {str(p).casefold() for p in results}
        for path in _walk_search(query, roots, extension, limit):
            if str(path).casefold() not in seen:
                results.append(path)
                seen.add(str(path).casefold())
            if len(results) >= limit:
                break
    return results[:limit]


def _windows_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def open_in_explorer(path: str | Path, *, select: bool = False) -> str:
    target = resolve_location(path)
    if select and target.exists() and target.is_file():
        folder = target.parent
        if _OS == "Windows":
            subprocess.Popen(["explorer.exe", f"/select,{target}"], creationflags=_windows_flags())
        elif _OS == "Darwin":
            subprocess.Popen(["open", "-R", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return f"Opened Explorer and selected {target.name}."
    if not target.exists():
        return f"I could not find {target}."
    if _OS == "Windows":
        subprocess.Popen(["explorer.exe", str(target)], creationflags=_windows_flags())
    elif _OS == "Darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return f"Opened {target}."


def format_matches(matches: list[Path], query: str) -> str:
    if not matches:
        return f"I could not find a file matching '{query}'."
    lines = [f"Found {len(matches)} match(es) for '{query}':"]
    lines.extend(f"{index}. {path}" for index, path in enumerate(matches, 1))
    if len(matches) > 1:
        lines.append("Tell me the number or exact path before I open or change one.")
    return "\n".join(lines)
