"""Reliable Windows Explorer locations, search, and selection helpers.

The file action used to recurse from one guessed directory with a small directory
limit. This module resolves real known folders first, uses Everything's read-only
CLI when available, and falls back to a bounded, scored search. It never deletes
or changes files and never interpolates user text into a shell command.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from core.path_policy import PathPolicyError, resolve_user_path
from core.user_paths import locations as _user_locations

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


def locations() -> dict[str, Path]:
    """Return the canonical user folders shared by file and launcher actions.

    On Windows this reads Known Folders rather than constructing ``~/Desktop``;
    therefore OneDrive Folder Backup and a redirected Documents folder work in
    the explorer, dashboard and voice actions alike.
    """
    return _user_locations()


def resolve_location(value: str | Path, *, allow_missing: bool = True) -> Path:
    raw = str(value or "").strip().strip('"').strip("'")
    key = " ".join(raw.casefold().split())
    known = locations()
    if key in _LOCATION_ALIASES:
        path = known[_LOCATION_ALIASES[key]]
    else:
        normalized = raw.replace("\\", "/")
        head, separator, tail = normalized.partition("/")
        if separator and head.casefold() in _LOCATION_ALIASES:
            path = known[_LOCATION_ALIASES[head.casefold()]] / tail
        else:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = Path.home() / path
    return resolve_user_path(
        path,
        allow_missing=allow_missing,
        reject_symlinks=False,
    )


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
            if not path.exists() or not _allowed_result(path):
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


def _allowed_result(path: Path) -> bool:
    try:
        resolve_user_path(path, allow_missing=False)
        return True
    except (PathPolicyError, FileNotFoundError, OSError, ValueError):
        return False


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
                dirs[:] = [
                    directory for directory in dirs
                    if directory.casefold() not in _SKIP_DIRS
                    and not directory.startswith(".")
                    and _allowed_result(Path(current) / directory)
                ]
                for filename in files:
                    if wanted_extension and Path(filename).suffix.casefold() != wanted_extension:
                        continue
                    path = Path(current) / filename
                    if not _allowed_result(path):
                        continue
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
    # Everything and os.walk do not necessarily return the same order. Keep
    # candidate numbering stable so a follow-up such as "open candidate 2"
    # refers to the same file after the search is repeated.
    results.sort(key=lambda path: (-_score(path, query)[0], str(path).casefold()))
    return results[:limit]


def _windows_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def open_in_explorer(path: str | Path, *, select: bool = False) -> str:
    target = resolve_location(path)
    if select and target.exists() and target.is_file():
        subprocess.Popen(["explorer.exe", f"/select,{target}"], creationflags=_windows_flags())
        return f"Opened Explorer and selected {target.name}."
    if not target.exists():
        return f"I could not find {target}."
    subprocess.Popen(["explorer.exe", str(target)], creationflags=_windows_flags())
    return f"Opened {target}."


def format_matches(matches: list[Path], query: str) -> str:
    if not matches:
        return f"I could not find a file matching '{query}'."
    lines = [f"Found {len(matches)} match(es) for '{query}':"]
    lines.extend(f"{index}. {path}" for index, path in enumerate(matches, 1))
    if len(matches) > 1:
        lines.append("Tell me the number or exact path before I open or change one.")
    return "\n".join(lines)
