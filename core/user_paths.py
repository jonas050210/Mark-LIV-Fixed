"""Resolve user-facing folders without guessing ``~/Desktop``.

Windows Known Folders are the source of truth for Desktop, Documents, Downloads
and media directories.  In particular, OneDrive's Known Folder Move redirects
Desktop to ``...\OneDrive\Desktop`` while leaving ``Path.home() / 'Desktop'``
absent or stale.  Keeping this in one small dependency-free module prevents
individual features from quietly disagreeing about where a user's files live.
"""
from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path


KNOWN_FOLDER_GUIDS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}

_FOLDER_NAMES = {
    "desktop": "Desktop",
    "documents": "Documents",
    "downloads": "Downloads",
    "pictures": "Pictures",
    "music": "Music",
    "videos": "Videos",
}


def _windows_known_folder(guid_text: str) -> Path | None:
    """Ask Windows Shell for a redirected Known Folder, if available.

    ``SHGetKnownFolderPath`` already understands OneDrive Folder Backup,
    organisation-specific OneDrive names and localised folder display names.
    The pointer must be freed by the matching COM allocator even when the path
    itself does not currently exist (for example while OneDrive is reconnecting).
    """
    pointer = ctypes.c_wchar_p()
    try:
        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        guid = GUID()
        raw = uuid.UUID(guid_text).bytes_le
        ctypes.memmove(ctypes.byref(guid), raw, ctypes.sizeof(guid))
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(pointer)
        )
        if result != 0 or not pointer.value:
            return None
        return Path(pointer.value)
    except Exception:
        return None
    finally:
        if pointer.value:
            try:
                ctypes.windll.ole32.CoTaskMemFree(pointer)
            except Exception:
                pass


def _onedrive_candidates(folder: str, home: Path) -> list[Path]:
    """Fallback Desktop candidates for a damaged/missing Shell registration.

    The Shell API above is authoritative.  These fallbacks make the app usable
    on stripped-down Windows images and immediately after OneDrive Folder Backup
    was enabled, before Explorer refreshes its registry values.
    """
    roots: list[Path] = []
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(variable, "").strip()
        if value:
            roots.append(Path(value))
    try:
        roots.extend(path for path in home.glob("OneDrive*") if path.is_dir())
    except OSError:
        pass

    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        candidate = root / folder
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return result


def locations() -> dict[str, Path]:
    """Return Windows Known Folders with OneDrive-aware fallbacks."""
    home = Path.home()
    found: dict[str, Path] = {"home": home}
    for name, folder in _FOLDER_NAMES.items():
        native = _windows_known_folder(KNOWN_FOLDER_GUIDS[name])
        if native is not None:
            found[name] = native
            continue
        one_drive = next((path for path in _onedrive_candidates(folder, home) if path.is_dir()), None)
        found[name] = one_drive if one_drive is not None else home / folder
    return found


def location(name: str) -> Path:
    """Return one canonical user location; unknown names fall back to home."""
    return locations().get(str(name or "").casefold(), Path.home())


def desktop_candidates() -> list[Path]:
    """Return every plausible Desktop used for personal Windows shortcuts.

    Normally this is only the Shell-reported Desktop.  The conventional desktop
    and OneDrive fallbacks are included when they exist, so an old shortcut does
    not disappear while a user is migrating their files to OneDrive.
    """
    home = Path.home()
    candidates = [location("desktop")]
    candidates.append(home / "Desktop")
    candidates.extend(_onedrive_candidates("Desktop", home))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = os.path.normcase(os.path.abspath(str(candidate)))
        except (OSError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            unique.append(candidate)
    return unique


__all__ = ["KNOWN_FOLDER_GUIDS", "desktop_candidates", "location", "locations"]
