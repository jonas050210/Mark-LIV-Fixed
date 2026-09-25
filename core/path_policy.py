"""Shared filesystem boundary for model- and dashboard-directed operations."""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class PathPolicyError(PermissionError):
    pass


_SECRET_FILE_RE = re.compile(
    r"^(?:\.env(?:\..*)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.*(?:credential|client[_-]?secret|"
    r"private[_-]?key).*|(?:token|cookies?)(?:[._-].*)?\.json)$",
    re.IGNORECASE,
)


def _home() -> Path:
    return Path.home().expanduser().resolve()


def protected_roots(home: Path | None = None) -> tuple[Path, ...]:
    home = (home or _home()).resolve(strict=False)
    project = Path(__file__).resolve().parent.parent
    values = [
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        home / ".azure",
        home / ".kube",
        home / ".docker",
        home / ".jarvis",
        home / ".jarvis_profiles",
        home / ".config" / "gcloud",
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
        home / ".config" / "microsoft-edge",
        home / ".config" / "BraveSoftware",
        home / ".config" / "vivaldi",
        home / ".config" / "opera",
        home / ".mozilla",
        home / ".pki",
        home / ".password-store",
        home / ".bash_history",
        home / ".zsh_history",
        home / ".python_history",
        home / ".local" / "share" / "keyrings",
        home / "Library" / "Keychains",
        home / "Library" / "Application Support" / "Google" / "Chrome",
        home / "Library" / "Application Support" / "Microsoft Edge",
        home / "Library" / "Application Support" / "BraveSoftware",
        home / "Library" / "Application Support" / "Vivaldi",
        home / "Library" / "Application Support" / "com.operasoftware.Opera",
        home / "Library" / "Application Support" / "Firefox",
        home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
        home / "AppData" / "Local" / "BraveSoftware",
        home / "AppData" / "Local" / "Vivaldi",
        home / "AppData" / "Roaming" / "Opera Software",
        home / "AppData" / "Roaming" / "Mozilla" / "Firefox",
        home / "AppData" / "Roaming" / "Microsoft" / "Credentials",
        home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "PowerShell" / "PSReadLine",
        home / "AppData" / "Local" / "Microsoft" / "Vault",
        project / ".git",
        project / "config" / "api_keys.json",
        project / "config" / "spotify_token.json",
        project / "config" / "shortcuts.json",
        project / "config" / "certs",
        project / "config" / "browser_profiles",
        project / "memory" / "long_term.json",
    ]
    return tuple(path.resolve(strict=False) for path in values)


def _inside(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _contains_symlink(path: Path, stop: Path) -> bool:
    """Return whether an existing component below ``stop`` is a symlink."""
    try:
        relative = path.absolute().relative_to(stop.absolute())
    except ValueError:
        return True
    current = stop
    for part in relative.parts:
        current = current / part
        try:
            details = current.lstat()
            if current.is_symlink() or (
                int(getattr(details, "st_file_attributes", 0)) & 0x400
            ):
                return True
        except FileNotFoundError:
            break
        except OSError:
            return True
    return False


def resolve_user_path(
    value: str | Path,
    *,
    base: str | Path | None = None,
    allowed_roots: Iterable[str | Path] | None = None,
    allow_missing: bool = True,
    allow_protected: bool = False,
    reject_symlinks: bool = False,
    protect_ancestors: bool = False,
) -> Path:
    """Resolve a user path and enforce roots, protected data, and symlinks.

    Relative paths are anchored at ``base`` or the user's home directory, never
    the process working directory.
    """
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        raise PathPolicyError("a path is required")
    if len(raw) > 4096:
        raise PathPolicyError("path is too long")
    candidate = Path(raw).expanduser()
    anchor = Path(base).expanduser() if base is not None else _home()
    if not candidate.is_absolute():
        candidate = anchor / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathPolicyError(f"path could not be resolved: {exc}") from exc

    roots = [Path(root).expanduser().resolve(strict=False) for root in (allowed_roots or (_home(),))]
    containing = next((root for root in roots if _inside(resolved, root)), None)
    if containing is None:
        raise PathPolicyError("path is outside the allowed user folders")

    if not allow_protected:
        for protected in protected_roots(_home()):
            if _inside(resolved, protected) or (
                protect_ancestors and _inside(protected, resolved)
            ):
                raise PathPolicyError("path contains or encloses private application or credential data")
        if _SECRET_FILE_RE.match(resolved.name):
            raise PathPolicyError("credential and token files are protected")

    if not allow_missing and not resolved.exists():
        raise FileNotFoundError(resolved)
    if reject_symlinks and _contains_symlink(candidate, containing):
        raise PathPolicyError("symbolic-link paths are not allowed for this operation")
    return resolved


def validate_child_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value in {".", ".."}:
        raise ValueError("a file or folder name is required")
    if len(value.encode("utf-8", errors="ignore")) > 255:
        raise ValueError("the name is too long")
    if Path(value).name != value or "/" in value or "\\" in value:
        raise ValueError("the name must not contain a folder path")
    if any(ord(char) < 32 for char in value):
        raise ValueError("the name contains control characters")
    return value


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace a file in its existing directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        details = path.lstat()
        existing_mode = details.st_mode & 0o777 if not path.is_symlink() else None
    except OSError:
        existing_mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if existing_mode is not None:
            try:
                os.fchmod(descriptor, existing_mode)
            except OSError:
                pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, content.encode(encoding))


def atomic_create_bytes(path: Path, content: bytes) -> None:
    """Atomically create ``path`` while refusing to replace an existing entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_create_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    atomic_create_bytes(path, content.encode(encoding))


def move_no_replace(source: Path, destination: Path) -> None:
    """Rename within a filesystem while atomically refusing destination reuse.

    Python's ``Path.rename`` replaces an existing destination on POSIX. This
    wrapper uses each platform's no-replace primitive and fails closed when one
    is unavailable. Cross-device moves deliberately raise ``EXDEV`` so callers
    can choose a copy/verify/remove strategy appropriate to their data type.
    """
    source = Path(source)
    destination = Path(destination)
    system = platform.system()
    if system == "Windows":  # MoveFile/rename fails when destination exists.
        os.rename(source, destination)
        return

    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if system == "Linux" and hasattr(libc, "renameat2"):
        at_fdcwd = -100
        result = libc.renameat2(
            at_fdcwd, ctypes.c_char_p(source_bytes),
            at_fdcwd, ctypes.c_char_p(destination_bytes),
            1,  # RENAME_NOREPLACE
        )
    elif system == "Darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(
            ctypes.c_char_p(source_bytes), ctypes.c_char_p(destination_bytes), 0x4
        )  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


@dataclass(frozen=True)
class FileFingerprint:
    kind: str
    size: int
    modified_ns: int
    inode: int


def fingerprint(path: Path) -> FileFingerprint | None:
    try:
        details = path.lstat()
        if path.is_symlink() or (
            int(getattr(details, "st_file_attributes", 0)) & 0x400
        ):
            kind = "link"
        elif path.is_dir():
            kind = "dir"
        elif path.is_file():
            kind = "file"
        else:
            kind = "other"
        return FileFingerprint(
            kind,
            int(details.st_size),
            int(details.st_mtime_ns),
            int(getattr(details, "st_ino", 0)),
        )
    except OSError:
        return None


def unchanged(path: Path, expected: FileFingerprint | None) -> bool:
    return expected is not None and fingerprint(path) == expected


def atomic_replace_bytes_if_unchanged(
    path: Path, content: bytes, expected: FileFingerprint
) -> None:
    """Replace a regular file only while its captured identity still matches."""
    if expected.kind != "file":
        raise FileExistsError("only unchanged regular files can be replaced")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        details = path.lstat()
        existing_mode = details.st_mode & 0o777 if not path.is_symlink() else None
    except OSError:
        existing_mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if existing_mode is not None:
            try:
                os.fchmod(descriptor, existing_mode)
            except OSError:
                pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if not unchanged(path, expected):
            raise FileExistsError("the file changed before it could be replaced")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_replace_text_if_unchanged(
    path: Path, content: str, expected: FileFingerprint, *, encoding: str = "utf-8"
) -> None:
    atomic_replace_bytes_if_unchanged(path, content.encode(encoding), expected)


__all__ = [
    "PathPolicyError",
    "FileFingerprint",
    "atomic_create_bytes",
    "atomic_create_text",
    "atomic_replace_bytes_if_unchanged",
    "atomic_replace_text_if_unchanged",
    "atomic_write_bytes",
    "atomic_write_text",
    "fingerprint",
    "move_no_replace",
    "protected_roots",
    "resolve_user_path",
    "unchanged",
    "validate_child_name",
]
