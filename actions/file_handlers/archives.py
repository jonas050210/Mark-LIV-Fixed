"""Archive handling, with the guards against zip bombs and path traversal."""
from __future__ import annotations

from actions.file_handlers.common import (
    Path,
    PathPolicyError,
    _MAX_ARCHIVE_EXPANDED,
    _MAX_ARCHIVE_MEMBERS,
    _MAX_ARCHIVE_RATIO,
    _display_text,
    _file_size_str,
    _format_bytes,
    _source_stem,
    move_no_replace,
    resolve_user_path,
    secrets,
    shutil,
    tarfile,
    zipfile,
)


def _cancel_requested(params: dict) -> bool:
    event = params.get("_cancel_event")
    return bool(event is not None and hasattr(event, "is_set") and event.is_set())
def _archive_relative(name: str) -> Path:
    normalized = str(name or "").replace("\\", "/")
    if (
        not normalized
        or len(normalized) > 4096
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("archive contains an empty or invalid member name")
    member = Path(normalized)
    if (member.is_absolute() or member.drive
            or any(":" in part or part in {"", ".", ".."} for part in member.parts)):
        raise ValueError(f"unsafe archive path: {name}")
    return member
def _archive_members(path: Path):
    suffixes = "".join(path.suffixes[-2:]).lower()
    if path.suffix.lower() in {".zip", ".docx", ".xlsx", ".pptx"}:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive has too many members")
            total = 0
            rows = []
            for info in infos:
                relative = _archive_relative(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"archive links are not allowed: {info.filename}")
                total += int(info.file_size)
                if total > _MAX_ARCHIVE_EXPANDED:
                    raise ValueError("archive expands beyond the 1 GB limit")
                compressed = max(1, int(info.compress_size))
                if info.file_size > 10 * 1024 * 1024 and info.file_size / compressed > _MAX_ARCHIVE_RATIO:
                    raise ValueError(f"suspicious compression ratio: {info.filename}")
                rows.append((relative, info.is_dir(), int(info.file_size), info))
            return "zip", rows, total

    if suffixes in {".tar.gz", ".tar.bz2", ".tar.xz"} or path.suffix.lower() == ".tar":
        rows = []
        total = 0
        with tarfile.open(path, mode="r:*") as archive:
            for index, info in enumerate(archive):
                if index >= _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive has too many members")
                relative = _archive_relative(info.name)
                if info.issym() or info.islnk() or info.isdev() or info.isfifo():
                    raise ValueError(f"archive links/devices are not allowed: {info.name}")
                if not (info.isfile() or info.isdir()):
                    raise ValueError(f"unsupported archive member: {info.name}")
                total += int(info.size)
                if total > _MAX_ARCHIVE_EXPANDED:
                    raise ValueError("archive expands beyond the 1 GB limit")
                rows.append((relative, info.isdir(), int(info.size), info.name))
        if path.stat().st_size and total > 10 * 1024 * 1024 and total / path.stat().st_size > _MAX_ARCHIVE_RATIO:
            raise ValueError("archive has a suspicious overall compression ratio")
        return "tar", rows, total
    raise ValueError(f"unsupported archive format: {path.suffix.lower()}")
def _archive_rejection(exc: Exception) -> str:
    """Return an actionable archive error without echoing member filenames."""
    message = str(exc).casefold()
    categories = (
        ("links/devices are not allowed", "archive links/devices are not allowed"),
        ("links are not allowed", "archive links are not allowed"),
        ("compression ratio", "suspicious compression ratio"),
        ("too many members", "archive has too many members"),
        ("expands beyond", "archive expands beyond the safe size limit"),
        ("unsafe archive path", "archive contains an unsafe path"),
        ("empty or invalid member", "archive contains an invalid member name"),
        ("unsupported archive member", "archive contains an unsupported member type"),
        ("unsupported archive format", "unsupported archive format"),
    )
    for marker, safe in categories:
        if marker in message:
            return safe
    return f"archive could not be validated ({type(exc).__name__})"
def _process_archive(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "list"
    try:
        archive_type, members, total = _archive_members(path)
    except Exception as exc:
        return f"Archive rejected: {_archive_rejection(exc)}"

    if action == "list":
        names = [
            _display_text(str(relative), 500) + ("/" if is_dir else "")
            for relative, is_dir, _size, _info in members
        ]
        preview = "\n".join(names[:30])
        suffix = f"\n... and {len(names) - 30} more" if len(names) > 30 else ""
        return f"Archive contains {len(names)} members ({_file_size_str(path)} compressed, {_format_bytes(total)} expanded):\n{preview}{suffix}"

    if action != "extract":
        return f"Unknown archive action: '{action}'. Try: list, extract"

    requested = params.get("destination")
    if requested is not None and not isinstance(requested, str):
        return "Extract destination must be text."
    if requested is not None and len(requested) > 4096:
        return "Extract destination is too long."
    try:
        destination = resolve_user_path(
            requested or (path.parent / _source_stem(path)),
            allow_missing=True,
            reject_symlinks=True,
        )
        if not requested:
            base = destination
            index = 1
            while destination.exists():
                destination = base.parent / f"{base.name}_{index}"
                index += 1
                if index > 10_000:
                    raise FileExistsError("could not allocate a destination folder")
    except (PathPolicyError, OSError, ValueError) as exc:
        return f"Extract failed: unsafe destination ({type(exc).__name__})"
    if destination.exists():
        return "Extract destination already exists; nothing was overwritten."

    staging = destination.parent / (
        f".{destination.name}.extracting-{secrets.token_hex(6)}"
    )
    extracted_bytes = 0

    def _copy_member(source, output, declared_size: int) -> None:
        nonlocal extracted_bytes
        member_bytes = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            member_bytes += len(chunk)
            extracted_bytes += len(chunk)
            if member_bytes > declared_size or extracted_bytes > _MAX_ARCHIVE_EXPANDED:
                raise ValueError("archive expanded beyond its declared safe limits")
            output.write(chunk)

    try:
        staging.mkdir(parents=True, exist_ok=False, mode=0o700)
        if archive_type == "zip":
            with zipfile.ZipFile(path) as archive:
                for relative, is_dir, _size, info in members:
                    if _cancel_requested(params):
                        raise InterruptedError("cancelled")
                    target = staging / relative
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, target.open("xb") as output:
                        _copy_member(source, output, _size)
        else:
            with tarfile.open(path, mode="r:*") as archive:
                for relative, is_dir, _size, member_name in members:
                    if _cancel_requested(params):
                        raise InterruptedError("cancelled")
                    target = staging / relative
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    source = archive.extractfile(member_name)
                    if source is None:
                        raise ValueError(f"could not read archive member: {member_name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source, target.open("xb") as output:
                        _copy_member(source, output, _size)
        move_no_replace(staging, destination)
        return f"Extracted {len(members)} members to: {destination}"
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return f"Extract failed and partial output was cleaned up: {type(exc).__name__}"
