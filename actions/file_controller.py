import errno
import heapq
import os
import shutil
import platform
import secrets
import stat
import time
from pathlib import Path
from datetime import datetime

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

from core.undo import push_undo, refuse
from core import explorer
from core.path_policy import (
    FileFingerprint,
    PathPolicyError,
    atomic_create_text,
    atomic_replace_bytes_if_unchanged,
    atomic_replace_text_if_unchanged,
    fingerprint,
    move_no_replace,
    resolve_user_path,
    unchanged,
    validate_child_name,
)

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# Undo keeps a file's previous contents in memory so `write` can be reversed.
# Above this size it does not — a 200 MB log would sit in RAM for the rest of
# the session to protect an edit nobody is going to take back.
_UNDO_CONTENT_LIMIT = 1_000_000
_COPY_TREE_MAX_ENTRIES = 10_000
_COPY_TREE_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _is_reparse(details) -> bool:
    return bool(int(getattr(details, "st_file_attributes", 0)) & 0x400)


def _entry_is_link(entry: os.DirEntry) -> bool:
    try:
        return entry.is_symlink() or _is_reparse(entry.stat(follow_symlinks=False))
    except OSError:
        return True


def _path_is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or _is_reparse(path.lstat())
    except OSError:
        return True


def _undo_move(src: Path, dst: Path, expected):
    """Reverse a move only while the moved object is still unchanged."""
    def _fn():
        if not dst.exists():
            refuse(f"'{dst.name}' is no longer there, so it cannot be moved back")
        if not unchanged(dst, expected):
            refuse(f"'{dst.name}' changed after the move and was left alone")
        if src.exists():
            refuse(f"'{src}' now exists and will not be overwritten")
        src.parent.mkdir(parents=True, exist_ok=True)
        move_no_replace(dst, src)
        return f"'{src.name}' is back in {src.parent.name}/."
    return _fn


def _why(exc: Exception) -> str:
    """A reason a person can act on, instead of an exception's class name.

    "Could not copy: ValueError" tells the user nothing: they cannot tell a
    rejected name from a missing folder from a full disk, and neither can the
    model deciding what to try next. The validation layers already raise with a
    sentence explaining themselves; this surfaces it.
    """
    message = str(exc).strip()
    if isinstance(exc, PermissionError):
        return "permission was denied"
    if isinstance(exc, FileNotFoundError):
        return "the path no longer exists"
    if isinstance(exc, FileExistsError):
        return "something with that name already exists"
    if isinstance(exc, IsADirectoryError):
        return "the target is a folder"
    if isinstance(exc, NotADirectoryError):
        return "part of that path is not a folder"
    if isinstance(exc, OSError) and exc.errno == 28:
        return "the disk is full"
    if message and len(message) <= 200:
        return message
    return f"an unexpected {type(exc).__name__}"


def _undo_create(target: Path, expected):
    """Remove a created object only if it still has the captured identity."""
    def _fn():
        if not target.exists():
            return f"'{target.name}' is already gone."
        if not unchanged(target, expected):
            refuse(f"'{target.name}' changed after creation and was left alone")
        if target.is_dir() and any(target.iterdir()):
            refuse(f"'{target.name}' is no longer empty and was left alone")
        target.rmdir() if target.is_dir() else target.unlink()
        return f"Removed '{target.name}'."
    return _fn


def _undo_write(target: Path, existed: bool, previous: bytes | None, expected):
    """Restore bytes only if the assistant's written version is unchanged."""
    def _fn():
        if not target.exists():
            if not existed:
                return f"'{target.name}' is already gone."
            refuse(f"'{target.name}' is missing, so its previous contents cannot be restored")
        if not unchanged(target, expected):
            refuse(f"'{target.name}' changed after the write and was left alone")
        if not existed:
            target.unlink()
            return f"Removed '{target.name}' — it did not exist before."
        if previous is None:
            refuse(f"the previous contents of '{target.name}' were not captured")
        atomic_replace_bytes_if_unchanged(target, previous, expected)
        return f"Restored the previous contents of '{target.name}'."
    return _fn


def _restore_from_trash(original: Path) -> str:
    """Best-effort undelete.

    delete_file uses send2trash, which is the right call: the file lands in the
    Recycle Bin / Trash where the person can also find it themselves. Getting it
    back out again is shell work and only reliable on Windows, where pywin32 is
    already a dependency. Everywhere else this says where the file is instead of
    pretending it failed — the file is not lost either way."""
    if _OS == "Windows":
        try:
            import win32com.client
            shell = win32com.client.Dispatch("Shell.Application")
            bin_folder = shell.NameSpace(10)      # ssfBITBUCKET
            for item in bin_folder.Items():
                if str(bin_folder.GetDetailsOf(item, 1)).strip().lower() == \
                        str(original.parent).strip().lower():
                    if str(item.Name).strip().lower() == original.name.strip().lower():
                        item.InvokeVerb("UNDELETE")
                        return f"'{original.name}' restored from the Recycle Bin."
        except Exception as e:
            print(f"[file] Recycle Bin restore failed ({type(e).__name__}).")
    return (f"'{original.name}' is in the Recycle Bin — I could not pull it back "
            f"automatically, but it is there and can be restored by hand.")


_SAFE_ROOTS: list[Path] = [
    Path.home(),
]

def _is_safe_path(
    target: Path, *, mutation: bool = False, recursive: bool = False
) -> bool:
    try:
        resolve_user_path(
            target,
            allowed_roots=_SAFE_ROOTS,
            allow_missing=True,
            reject_symlinks=mutation or recursive,
            protect_ancestors=mutation or recursive,
        )
        return True
    except (OSError, ValueError, PathPolicyError):
        return False

def _recursive_mutation_safe(root: Path) -> bool:
    """Refuse recursive changes that would include protected data or links."""
    if not root.is_dir():
        return True
    stack = [root]
    scanned = 0
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _COPY_TREE_MAX_ENTRIES:
                        return False
                    child = Path(entry.path)
                    if _entry_is_link(entry) or not _is_safe_path(child):
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(child)
        return True
    except OSError:
        return False


def _read_regular_bytes(path: Path, limit: int) -> tuple[bytes, FileFingerprint]:
    """Read a small undo snapshot through a no-follow regular-file descriptor."""
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        details = os.fstat(handle.fileno())
        if not stat.S_ISREG(details.st_mode) or _is_reparse(details):
            raise ValueError("snapshot target is not a regular file")
        if details.st_size > limit:
            raise ValueError("snapshot target exceeds the undo limit")
        content = handle.read(limit + 1)
        if len(content) > limit:
            raise ValueError("snapshot target grew beyond the undo limit")
        captured = FileFingerprint(
            "file",
            int(details.st_size),
            int(details.st_mtime_ns),
            int(getattr(details, "st_ino", 0)),
        )
    return content, captured


def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

def _get_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Downloads"

def _get_documents() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Documents"

def _get_pictures() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Pictures"

def _get_music() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Music"

def _get_videos() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Videos"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    raw = str(raw or "")
    if len(raw) > 4096:
        raise ValueError("path is too long")
    raw = raw.strip().strip('"').strip("'")
    lower = raw.lower()
    if lower in shortcuts:
        return shortcuts[lower]

    # "desktop/notes/a.md" and "desktop\notes\a.md" — a shortcut followed by a
    # sub-path.  Without this branch the whole string falls through to the
    # relative-path return below and is resolved against the process CWD instead
    # of the real Desktop: an "Access denied" when the project lives outside the
    # home directory, or — worse — a silent write into a stray "desktop" folder
    # inside the project when it lives inside it.
    head, sep, rest = raw.replace("\\", "/").partition("/")
    if sep and head.lower() in shortcuts:
        rest = rest.strip("/")
        return shortcuts[head.lower()] / rest if rest else shortcuts[head.lower()]

    # Preserve the lexical path so later mutation checks can detect symlink or
    # junction components. Relative commands are still anchored to the user.
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.home() / candidate
    return Path(os.path.abspath(candidate))

def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _safe_trash(target: Path) -> str:

    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        items = []
        total = 0
        scan_capped = False
        with os.scandir(target) as entries:
            for scanned, entry in enumerate(entries, 1):
                if scanned > 10_000:
                    scan_capped = True
                    break
                item = Path(entry.path)
                if not show_hidden and item.name.startswith("."):
                    continue
                if _entry_is_link(entry) or not _is_safe_path(item):
                    continue
                total += 1
                if len(items) >= 200:
                    continue
                if item.is_dir():
                    items.append(f"📁 {item.name}/")
                else:
                    size = _format_size(item.stat().st_size)
                    items.append(f"📄 {item.name} ({size})")

        if not items:
            return f"Directory is empty: {target.name}/"

        items.sort(key=str.casefold)
        suffix = f"\n[Showing {len(items)} of {total} scanned items]" if total > len(items) else ""
        if scan_capped:
            suffix += "\n[Stopped after scanning 10,000 directory entries]"
        count_label = f"at least {total}" if scan_capped else str(total)
        return f"Contents of {target.name}/ ({count_label} items):\n" + "\n".join(items) + suffix

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {type(e).__name__}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        text = str(content)
        if len(text.encode("utf-8")) > _UNDO_CONTENT_LIMIT:
            return "File content is larger than the 1 MB safety limit."
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target, mutation=True):
            return f"Access denied: {target}"
        if target.exists():
            return f"A file or folder named '{target.name}' already exists; nothing was overwritten."
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_create_text(target, text)
        after = fingerprint(target)
        push_undo(f"created {target.name}", _undo_create(target, after))
        return f"File created: {target.name}"
    except Exception as e:
        return f"I could not create the file: {_why(e)}."


def create_folder(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target, mutation=True):
            return f"Access denied: {target}"
        already = target.exists()
        if already and not target.is_dir():
            return f"A file named '{target.name}' already exists here."
        target.mkdir(parents=True, exist_ok=True)
        # Only offer to undo a folder we actually made. "mkdir -p" on something
        # that was already there is not a change, and undoing it would delete a
        # directory the user has had for years.
        if not already:
            push_undo(
                f"created folder {target.name}",
                _undo_create(target, fingerprint(target)),
            )
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"I could not create the folder: {_why(e)}."


def delete_file(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target, mutation=True):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if target.is_dir() and not _recursive_mutation_safe(target):
            return (
                "Directory deletion refused because it contains protected data, "
                "symbolic links, unreadable entries, or too many items."
            )

        # Safe-directory check — protect critical user folders
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        original = target.resolve()
        result   = _safe_trash(target)
        if result.startswith("Moved to Trash"):
            push_undo(f"deleted {original.name}",
                      lambda p=original: _restore_from_trash(p))
        return result

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {type(e).__name__}"


class _CopyRefused(Exception):
    """A deliberate policy refusal, not a system error.

    Carries the exact sentence to show the user, so the caller that raised it
    can return that text verbatim instead of routing it through `_why`, which
    is meant for translating opaque system exceptions, not for re-explaining a
    refusal that was already explained.
    """


def _report(report_progress, percent: float, message: str) -> None:
    """Emit live progress, tolerating a listener that raises."""
    if report_progress is None:
        return
    try:
        report_progress(max(0, min(100, int(percent))), message)
    except Exception:
        pass


def _copy_regular_file(src: Path, dst: Path, *, cancel_event=None, report_progress=None) -> None:
    """Stream-copy one regular file into a not-yet-existing destination path.

    Raises on any problem; a destination file created before the failure is
    removed so a half-written copy is never left behind. Shared by copy_file
    and move_file's cross-drive fallback so both get identical safety
    guarantees (link/type refusal, the 2 GiB cap, fsync before publish) and
    the same live progress reporting.
    """
    created_destination = False
    try:
        source_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_descriptor = os.open(src, source_flags)
        source_details = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_details.st_mode) or _is_reparse(source_details):
            os.close(source_descriptor)
            raise OSError("source is not a regular file")
        if source_details.st_size > _COPY_TREE_MAX_BYTES:
            os.close(source_descriptor)
            raise OSError("file copy refused: source exceeds 2 GiB")
        total = source_details.st_size
        last_reported = -100
        with os.fdopen(source_descriptor, "rb") as source:
            output = dst.open("xb")
            created_destination = True
            copied_bytes = 0
            with output:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("file copy was cancelled")
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    if copied_bytes > _COPY_TREE_MAX_BYTES:
                        raise OSError("file grew beyond the 2 GiB copy limit")
                    output.write(chunk)
                    if total > 0:
                        percent = copied_bytes * 100 / total
                        if percent >= last_reported + 2 or copied_bytes == total:
                            last_reported = percent
                            _report(
                                report_progress, min(percent, 99),
                                f"Copying {src.name} "
                                f"({_format_size(copied_bytes)}/{_format_size(total)})",
                            )
                output.flush()
                os.fsync(output.fileno())
        try:
            shutil.copystat(src, dst, follow_symlinks=False)
        except OSError:
            pass
    except Exception:
        if created_destination:
            dst.unlink(missing_ok=True)
        raise


def _copy_directory_tree(src: Path, dst: Path, *, cancel_event=None, report_progress=None) -> None:
    """Stage a full copy of a directory tree next to dst, then publish it atomically.

    Raises `_CopyRefused` for a deliberate policy refusal (too many entries,
    a link, protected data, an unsupported type, or a cooperative cancel) and
    any other exception for a system failure. Nothing is left half-published
    on either kind of failure. Shared by copy_file and move_file's cross-drive
    fallback so both get identical safety guarantees and progress reporting.
    """
    scanned = 0
    estimated_bytes = 0
    for current, dirs, files in os.walk(src, followlinks=False):
        if cancel_event is not None and cancel_event.is_set():
            raise _CopyRefused("Directory copy was cancelled before publication.")
        for child in [*(Path(current) / n for n in dirs), *(Path(current) / n for n in files)]:
            scanned += 1
            if scanned > _COPY_TREE_MAX_ENTRIES:
                raise _CopyRefused(
                    f"Directory copy refused: more than {_COPY_TREE_MAX_ENTRIES:,} entries."
                )
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode) or _is_reparse(child_stat):
                raise _CopyRefused(
                    f"Directory copy refused: link or reparse point found at {child.name}."
                )
            if not _is_safe_path(child):
                raise _CopyRefused("Directory copy refused: protected data was found in the source.")
            if stat.S_ISREG(child_stat.st_mode):
                estimated_bytes += child_stat.st_size
                if estimated_bytes > _COPY_TREE_MAX_BYTES:
                    raise _CopyRefused("Directory copy refused: total file size exceeds 2 GiB.")
            elif not stat.S_ISDIR(child_stat.st_mode):
                raise _CopyRefused(f"Directory copy refused: unsupported file type at {child.name}.")

    # Build out of sight and publish with a no-replace rename. This prevents
    # failed/racing copies from exposing a partial tree.
    staging = dst.parent / f".{dst.name}.copying-{secrets.token_hex(6)}"
    copied_bytes = 0
    last_reported = -100

    def _bounded_copy(source_name, destination_name):
        nonlocal copied_bytes, last_reported
        source_path = Path(source_name)
        destination_path = Path(destination_name)
        flags = (
            os.O_RDONLY
            | int(getattr(os, "O_BINARY", 0))
            | int(getattr(os, "O_NONBLOCK", 0))
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if not _is_safe_path(source_path, mutation=True):
            raise OSError("source changed to a link or protected path")
        descriptor = os.open(source_path, flags)
        try:
            opened_details = os.fstat(descriptor)
            if not stat.S_ISREG(opened_details.st_mode) or _is_reparse(opened_details):
                raise OSError("source changed to an unsupported file type")
            source_file = os.fdopen(descriptor, "rb")
            descriptor = -1
            with source_file, destination_path.open("xb") as output:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("directory copy was cancelled")
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    copied_bytes += len(chunk)
                    if copied_bytes > _COPY_TREE_MAX_BYTES:
                        raise OSError("directory grew beyond the 2 GiB copy limit")
                    output.write(chunk)
                    if estimated_bytes > 0:
                        percent = copied_bytes * 100 / estimated_bytes
                        if percent >= last_reported + 2 or copied_bytes >= estimated_bytes:
                            last_reported = percent
                            _report(
                                report_progress, min(percent, 99),
                                f"Copying {source_path.name} "
                                f"({_format_size(copied_bytes)}/{_format_size(estimated_bytes)})",
                            )
                output.flush()
                os.fsync(output.fileno())
            shutil.copystat(source_path, destination_path, follow_symlinks=False)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return str(destination_path)

    try:
        shutil.copytree(
            str(src), str(staging), symlinks=True,
            copy_function=_bounded_copy,
        )
        for current, dirs, files in os.walk(staging, followlinks=False):
            if any((Path(current) / n).is_symlink() for n in [*dirs, *files]):
                raise OSError("source changed during copy and introduced a symbolic link")
        move_no_replace(staging, dst)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _move_across_devices(src: Path, dst: Path, *, cancel_event=None, report_progress=None) -> str:
    """Move a file or directory onto a different filesystem/drive.

    `move_no_replace` (built on the OS rename primitive) can only relink a
    name within one filesystem; there is no atomic kernel primitive for
    moving across drives. This copies the data first, verifying it lands
    intact, and only removes the original -- to the Recycle Bin/Trash, never
    permanently -- once the copy is proven complete, so a crash mid-copy or a
    failed removal never leaves the file existing nowhere.
    """
    is_dir = src.is_dir()
    try:
        if is_dir:
            if not _recursive_mutation_safe(src):
                return (
                    "Directory move refused because it contains protected data, "
                    "symbolic links, unreadable entries, or too many items."
                )
            try:
                _copy_directory_tree(src, dst, cancel_event=cancel_event, report_progress=report_progress)
            except _CopyRefused as exc:
                return f"Cross-drive move refused: {exc}"
        else:
            try:
                _copy_regular_file(src, dst, cancel_event=cancel_event, report_progress=report_progress)
            except InterruptedError:
                return "Cross-drive move was cancelled; the original was not touched."
    except Exception as exc:
        return f"I could not move it across drives: {_why(exc)}."

    expected = None if is_dir else fingerprint(dst)
    trashed = _safe_trash(src)
    if not trashed.startswith("Moved to Trash"):
        # The copy landed but the original could not be safely removed: undo
        # the copy rather than leave a silent, unannounced duplicate behind.
        shutil.rmtree(dst, ignore_errors=True) if is_dir else dst.unlink(missing_ok=True)
        return f"I could not move it across drives: {trashed}"

    _report(report_progress, 100, f"Moved {dst.name} across drives")

    if is_dir:
        return (
            f"Moved: {src.name} → {dst.parent.name}/ (different drive: copied, then "
            "trashed the original). Directory moves across drives are not "
            "auto-undoable, the same way directory copies are not."
        )

    def _undo_cross_device_move():
        if not dst.exists():
            refuse(f"'{dst.name}' is no longer at the new location")
        if not unchanged(dst, expected):
            refuse(f"'{dst.name}' changed after the move and was left alone")
        dst.unlink()
        return _restore_from_trash(src)

    push_undo(
        f"moved {src.name} to {dst.parent.name}/ (across drives)",
        _undo_cross_device_move,
    )
    return f"Moved: {src.name} → {dst.parent.name}/ (different drive: copied, then trashed the original)."


def move_file(
    path: str, name: str = "", destination: str = "",
    cancel_event=None, report_progress=None,
) -> str:
    try:
        base = _resolve_path(path)
        src = base / validate_child_name(name) if name else base
        dst = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src, mutation=True):
            return f"Access denied (source): {src}"
        if src.is_dir() and not _recursive_mutation_safe(src):
            return (
                "Directory move refused because it contains protected data, "
                "symbolic links, unreadable entries, or too many items."
            )
        if not _is_safe_path(dst, mutation=True):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name
        if not _is_safe_path(dst, mutation=True):
            return f"Access denied (destination): {dst}"
        if dst.exists():
            return f"Destination already exists: {dst.name}. Nothing was overwritten."

        dst.parent.mkdir(parents=True, exist_ok=True)
        origin = src.resolve()
        final = dst.resolve(strict=False)

        try:
            move_no_replace(src, final)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # Different filesystems/drives: the OS rename primitive cannot do
            # this atomically, so fall back to a verified copy-then-trash.
            return _move_across_devices(
                origin, final, cancel_event=cancel_event, report_progress=report_progress,
            )

        after = fingerprint(final)
        push_undo(
            f"moved {origin.name} to {final.parent.name}/",
            _undo_move(origin, final, after),
        )
        return f"Moved: {origin.name} → {final.parent.name}/"

    except Exception as e:
        return f"I could not move it: {_why(e)}."


def copy_file(
    path: str, name: str = "", destination: str = "",
    cancel_event=None, report_progress=None,
) -> str:
    try:
        base = _resolve_path(path)
        src = base / validate_child_name(name) if name else base
        dst = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src, recursive=True):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst, mutation=True):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name
        if not _is_safe_path(dst, mutation=True):
            return f"Access denied (destination): {dst}"
        if dst.exists():
            return f"Destination already exists: {dst.name}. Nothing was overwritten."
        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            try:
                _copy_directory_tree(src, dst, cancel_event=cancel_event, report_progress=report_progress)
            except _CopyRefused as exc:
                return str(exc)
            _report(report_progress, 100, f"Copied {src.name}")
            return (
                f"Copied: {src.name} → {dst.parent.name}/. Directory copies are "
                "not auto-deleted by undo because their contents may change."
            )

        _copy_regular_file(src, dst, cancel_event=cancel_event, report_progress=report_progress)
        _report(report_progress, 100, f"Copied {src.name}")
        copied = dst.resolve()
        expected = fingerprint(copied)

        def _undo_copy():
            if not copied.exists():
                return f"The copy '{copied.name}' is already gone."
            if not unchanged(copied, expected):
                refuse(f"the copy '{copied.name}' changed and was left alone")
            copied.unlink()
            return f"Removed the copy in {copied.parent.name}/."

        push_undo(f"copied {src.name} to {dst.parent.name}/", _undo_copy)
        return f"Copied: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"I could not copy it: {_why(e)}."


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target, mutation=True):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if target.is_dir() and not _recursive_mutation_safe(target):
            return (
                "Directory rename refused because it contains protected data, "
                "symbolic links, unreadable entries, or too many items."
            )

        clean_name = validate_child_name(new_name)
        new_path = target.parent / clean_name
        if not _is_safe_path(new_path, mutation=True):
            return f"Access denied: {new_path}"
        if new_path.exists():
            return f"A file named '{clean_name}' already exists here."

        old_path = target.resolve()
        move_no_replace(target, new_path)
        final = new_path.resolve()
        push_undo(
            f"renamed {old_path.name} to {clean_name}",
            _undo_move(old_path, final, fingerprint(final)),
        )
        return f"Renamed: {old_path.name} → {clean_name}"

    except Exception as e:
        return f"I could not rename it: {_why(e)}."


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target, recursive=True):
            return f"Access denied: {target}"
        if not target.exists():
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"

        limit = max(1, min(int(max_chars), 20_000))
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(target, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as handle:
            opened_details = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_details.st_mode) or _is_reparse(opened_details):
                raise ValueError("read target is not a regular file")
            content = handle.read(limit + 1)
        if len(content) > limit:
            content = content[:limit] + "\n\n[Truncated — file is larger than the preview limit]"
        return content

    except Exception as e:
        return f"Could not read file: {type(e).__name__}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False, overwrite: bool = False) -> str:
    try:
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target, mutation=True):
            return f"Access denied: {target}"
        if target.exists() and not target.is_file():
            return f"Not a file: {target.name}"
        existed = target.exists()
        if existed and not append and not overwrite:
            return (
                f"'{target.name}' already exists. Use append or explicitly request "
                "an overwrite; nothing was changed."
            )
        text = str(content)
        if len(text.encode("utf-8")) > _UNDO_CONTENT_LIMIT:
            return "Write content is larger than the 1 MB safety limit."
        target.parent.mkdir(parents=True, exist_ok=True)

        if append and existed:
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(target, flags)
            with os.fdopen(descriptor, "ab") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                    raise ValueError("append target is not a regular file")
                before_size = int(info.st_size)
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            expected = fingerprint(target)

            def undo_append():
                if not target.exists() or not unchanged(target, expected):
                    refuse(f"'{target.name}' changed after the append and was left alone")
                flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0))
                flags |= int(getattr(os, "O_NOFOLLOW", 0))
                descriptor = os.open(target, flags)
                with os.fdopen(descriptor, "r+b") as handle:
                    details = os.fstat(handle.fileno())
                    current = (
                        "file" if stat.S_ISREG(details.st_mode) and not _is_reparse(details) else "other",
                        int(details.st_size),
                        int(details.st_mtime_ns),
                        int(getattr(details, "st_ino", 0)),
                    )
                    wanted = (
                        expected.kind,
                        expected.size,
                        expected.modified_ns,
                        expected.inode,
                    )
                    if current != wanted:
                        refuse(f"'{target.name}' changed during undo and was left alone")
                    handle.truncate(before_size)
                    handle.flush()
                    os.fsync(handle.fileno())
                return f"Removed the text appended to '{target.name}'."

            push_undo(f"appended to {target.name}", undo_append)
            return f"Appended to: {target.name}"

        if existed:
            try:
                previous, before = _read_regular_bytes(target, _UNDO_CONTENT_LIMIT)
            except (OSError, ValueError):
                return f"'{target.name}' changed or could not be snapshotted safely."
            atomic_replace_text_if_unchanged(target, text, before)
        else:
            previous, before = None, None
            atomic_create_text(target, text)
        expected = fingerprint(target)
        push_undo(
            f"wrote to {target.name}",
            _undo_write(target, existed, previous, expected),
        )
        return f"Written to: {target.name}"
    except Exception as e:
        return f"Could not write file: {type(e).__name__}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        matches = explorer.search(name, root=path, extension=extension, limit=max_results)
        return explorer.format_matches(matches, name or extension or "files")
    except Exception as exc:
        return f"Search error: {type(exc).__name__}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = max(1, min(int(count), 50))
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        deadline = time.monotonic() + 10.0
        heap: list[tuple[int, str, Path]] = []
        scanned = 0
        timed_out = False
        for item in search_path.rglob("*"):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if _path_is_link(item) or not item.is_file() or not _is_safe_path(item):
                continue
            try:
                entry = (item.stat().st_size, str(item).casefold(), item)
                scanned += 1
                if len(heap) < count:
                    heapq.heappush(heap, entry)
                elif entry[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, entry)
            except OSError:
                continue

        top = [(size, item) for size, _key, item in sorted(heap, reverse=True)]

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, f in top:
            lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")
        if timed_out:
            lines.append(f"[Stopped after 10 seconds; scanned {scanned} files.]")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {type(e).__name__}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        usage = shutil.disk_usage(target)
        pct    = usage.used / usage.total * 100
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {type(e).__name__}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        base = _resolve_path(path)
        target = base / validate_child_name(name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {type(e).__name__}"

def open_explorer(
    path: str = "home",
    name: str = "",
    select: bool = False,
    match_index: int | None = None,
) -> str:
    try:
        target = _resolve_path(path)
    except (OSError, ValueError, PathPolicyError) as exc:
        return f"Access denied: {type(exc).__name__}"
    if not _is_safe_path(target):
        return f"Access denied: {target}"
    if name:
        try:
            clean_name = validate_child_name(name)
        except ValueError as exc:
            return f"Invalid file name: {type(exc).__name__}"
        candidate = target / clean_name
        if candidate.exists():
            target = candidate
        else:
            matches = explorer.search(name, root=target, limit=20)
            if match_index is not None:
                if not 1 <= match_index <= len(matches):
                    return f"Choose a candidate number between 1 and {len(matches)}."
                target = matches[match_index - 1]
            elif len(matches) != 1:
                return explorer.format_matches(matches, name)
            else:
                target = matches[0]
    if not _is_safe_path(target):
        return f"Access denied: {target}"
    try:
        return explorer.open_in_explorer(target, select=select)
    except FileNotFoundError:
        return "Explorer is not available on this operating system."
    except Exception as exc:
        return f"Could not open Explorer: {type(exc).__name__}"


def open_with_application(
    path: str,
    application: str,
    name: str = "",
    match_index: int | None = None,
) -> str:
    """Resolve one safe file and pass it to an indexed application as argv."""
    app_name = str(application or "").strip()[:160]
    if not app_name:
        return "An application name is required for open_with."
    try:
        target = _resolve_path(path)
    except (OSError, ValueError, PathPolicyError) as exc:
        return f"Access denied: {type(exc).__name__}"
    if not _is_safe_path(target):
        return f"Access denied: {target}"
    if name:
        try:
            clean_name = validate_child_name(name)
        except ValueError as exc:
            return f"Invalid file name: {type(exc).__name__}"
        candidate = target / clean_name
        if candidate.exists():
            target = candidate
        else:
            matches = explorer.search(name, root=target, limit=20)
            if match_index is not None:
                if not 1 <= match_index <= len(matches):
                    return f"Choose a candidate number between 1 and {len(matches)}."
                target = matches[match_index - 1]
            elif len(matches) != 1:
                return explorer.format_matches(matches, name)
            else:
                target = matches[0]
    if not _is_safe_path(target):
        return f"Access denied: {target}"
    if not target.exists() or not target.is_file():
        return f"I could not find a file to open at {target}."
    from actions.open_app import open_app
    return open_app({"app_name": app_name, "arguments": [str(target)]})


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError("boolean value must be true or false")


def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
    cancel_event=None,
    report_progress=None,
) -> str:
    params = parameters if isinstance(parameters, dict) else {}
    action = str(params.get("action") or "").lower().strip()
    path = str(params.get("path") or ("home" if action == "find" else "desktop"))
    name = str(params.get("name") or "")
    match_index = params.get("match_index")
    if match_index is not None:
        try:
            match_index = int(match_index)
        except (TypeError, ValueError):
            return "The Explorer candidate number must be an integer."

    if player:
        player.write_log(f"[file] {action} requested")

    try:
        if action in {"open", "open_folder", "explorer"}:
            return open_explorer(path, name=name, select=False, match_index=match_index)

        elif action == "open_with":
            return open_with_application(
                path,
                application=params.get("application", ""),
                name=name,
                match_index=match_index,
            )

        elif action in {"select", "show_in_explorer", "reveal"}:
            return open_explorer(path, name=name, select=True, match_index=match_index)

        elif action == "list":
            return list_files(path)

        elif action == "create_file":
            return create_file(path, name=name, content=params.get("content", ""))

        elif action == "create_folder":
            return create_folder(path, name=name)

        elif action == "delete":
            return delete_file(path, name=name)

        elif action == "move":
            return move_file(
                path,
                name=name,
                destination=params.get("destination", ""),
                cancel_event=cancel_event,
                report_progress=report_progress,
            )

        elif action == "copy":
            return copy_file(
                path,
                name=name,
                destination=params.get("destination", ""),
                cancel_event=cancel_event,
                report_progress=report_progress,
            )

        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))

        elif action == "read":
            return read_file(path, name=name)

        elif action == "write":
            return write_file(
                path,
                name=name,
                content=params.get("content", ""),
                append=_as_bool(params.get("append", False)),
                overwrite=_as_bool(params.get("overwrite", False)),
            )

        elif action == "find":
            # Searching defaults to the user's home folder rather than Desktop:
            # files downloaded, saved in Documents, or moved elsewhere should
            # not disappear merely because the caller omitted `path`.
            search_path = str(params.get("path") or "home")
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                path=search_path,
                max_results=min(int(params.get("max_results", 20)), 50),
            )

        elif action == "largest":
            return get_largest_files(
                path=path,
                count=int(params.get("count", 10)),
            )

        elif action == "disk_usage":
            return get_disk_usage(path)

        elif action == "organize_desktop":
            return (
                "Bulk desktop reorganisation was removed: it moved every file "
                "into six folders in one step, which is hard to reason about "
                "and easy to regret. Ask me to move specific files instead."
            )

        elif action == "info":
            return get_file_info(path, name=name)

        else:
            return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {type(e).__name__}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_controller",
    "description": "Reliable Explorer and file control: open a file normally or with a named installed application, open folders, reveal exact files, search known folders, list, create, delete, move, copy, rename, read, write, and inspect disk usage.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": ["open", "open_with", "open_folder", "explorer", "select", "show_in_explorer", "reveal", "list", "create_file", "create_folder", "delete", "move", "copy", "rename", "read", "write", "find", "largest", "disk_usage", "info"],
                "maxLength": 32,
                "description": "open | select | list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | info"
            },
            "path": {
                "type": "STRING",
                "maxLength": 500,
                "description": "File/folder path or shortcut: desktop, downloads, documents, home"
            },
            "application": {
                "type": "STRING",
                "maxLength": 160,
                "description": "Installed application name for open_with, such as Edge or Photoshop"
            },
            "destination": {
                "type": "STRING",
                "maxLength": 500,
                "description": "Destination path for move/copy. Move works across drives too: if the fast rename is impossible because source and destination are on different filesystems, it copies the data, verifies it, and only then removes the original."
            },
            "new_name": {
                "type": "STRING",
                "maxLength": 255,
                "description": "New name for rename"
            },
            "content": {
                "type": "STRING",
                "maxLength": 900000,
                "description": "Content for create_file/write"
            },
            "append": {
                "type": "BOOLEAN",
                "description": "Append to an existing file instead of replacing it."
            },
            "overwrite": {
                "type": "BOOLEAN",
                "description": "Explicitly replace an existing file. Existing writes require confirmation."
            },
            "name": {
                "type": "STRING",
                "maxLength": 255,
                "description": "Exact file name, file query, or file name to reveal in Explorer"
            },
            "extension": {
                "type": "STRING",
                "maxLength": 32,
                "description": "File extension to search (e.g. .pdf)"
            },
            "count": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 50,
                "description": "Number of results for largest"
            },
            "max_results": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 50,
                "description": "Maximum number of search candidates (1-50)"
            },
            "match_index": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 50,
                "description": "1-based candidate number to open or select after a search returned multiple matches"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": file_controller,
    "confirmation_actions": ["delete", "write"],
    "undoable": True,
}
