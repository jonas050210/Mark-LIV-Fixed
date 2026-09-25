"""Shared plumbing for the file-type handlers.

``actions/file_processor.py`` was 1626 lines holding eleven unrelated file
formats, which meant every change to, say, video trimming was made inside the
same file as PDF extraction and archive expansion. The handlers now live in
this package, one module per family of formats, and the entry point keeps only
the dispatcher and the tool declaration.

This module holds what all of them need: path validation, the stable-input
snapshot, parameter coercion, output naming, and the size limits. Handlers
import it with ``from actions.file_handlers.common import *``; ``__all__``
below is what that exports, underscore names included.
"""
import re
import json
import os
import shutil
import stat
import tempfile
import tarfile
import secrets
import zipfile
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

# Model choice, timeout and fallback ladder all live in core/gemini.py.
from core import gemini
from core.path_policy import (
    PathPolicyError,
    atomic_create_text,
    move_no_replace,
    resolve_user_path,
)
from core.process_runner import run_bounded

_MAX_INPUT_BYTES = 100 * 1024 * 1024
_MAX_CLOUD_BYTES = 20 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 5_000
_MAX_ARCHIVE_EXPANDED = 1 * 1024 * 1024 * 1024
_MAX_ARCHIVE_RATIO = 200


def _safe_input_path(raw: str) -> Path:
    if len(raw) > 4096:
        raise ValueError("file path is too long")
    candidate = resolve_user_path(raw, allow_missing=False, reject_symlinks=True)
    if not candidate.is_file():
        raise FileNotFoundError(raw)
    if candidate.is_symlink():
        raise PermissionError("symbolic-link inputs are not processed")
    size = candidate.stat().st_size
    if size > _MAX_INPUT_BYTES:
        raise ValueError("file is larger than the 100 MB processing limit")
    file_type = _detect_type(candidate)
    type_limits = {
        "json": 10 * 1024 * 1024,
        "xml": 10 * 1024 * 1024,
        "text": 20 * 1024 * 1024,
        "code": 20 * 1024 * 1024,
        "csv": 50 * 1024 * 1024,
        "excel": 50 * 1024 * 1024,
        "docx": 50 * 1024 * 1024,
        "pptx": 50 * 1024 * 1024,
        "pdf": 50 * 1024 * 1024,
        "image": 50 * 1024 * 1024,
    }
    limit = type_limits.get(file_type, _MAX_INPUT_BYTES)
    if size > limit:
        raise ValueError(
            f"{file_type or 'file'} input is larger than its {limit // (1024 * 1024)} MB processing limit"
        )
    return candidate


def _is_reparse(details) -> bool:
    return bool(int(getattr(details, "st_file_attributes", 0)) & 0x400)


@contextmanager
def _stable_input_snapshot(source: Path):
    """Yield a private, bounded sibling snapshot so parsers never reopen a raced path."""
    snapshot = source.parent / (
        f".{source.stem}.processing-input-{secrets.token_hex(6)}{source.suffix}"
    )
    source_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    source_flags |= int(getattr(os, "O_NONBLOCK", 0))
    source_flags |= int(getattr(os, "O_NOFOLLOW", 0))
    source_descriptor = os.open(source, source_flags)
    destination_descriptor = -1
    try:
        details = os.fstat(source_descriptor)
        if not stat.S_ISREG(details.st_mode) or _is_reparse(details):
            raise PermissionError("input is not a regular file")
        if details.st_size > _MAX_INPUT_BYTES:
            raise ValueError("file is larger than the processing limit")
        destination_descriptor = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0)),
            0o600,
        )
        copied = 0
        with os.fdopen(source_descriptor, "rb") as input_file, os.fdopen(
            destination_descriptor, "wb"
        ) as output_file:
            source_descriptor = -1
            destination_descriptor = -1
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > _MAX_INPUT_BYTES:
                    raise ValueError("file grew beyond the processing limit")
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        yield snapshot
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        snapshot.unlink(missing_ok=True)


def _source_stem(path: Path) -> str:
    stem = path.stem
    marker = ".processing-input-"
    if stem.startswith(".") and marker in stem:
        return stem[1:].rsplit(marker, 1)[0]
    return stem


def _source_name(path: Path) -> str:
    return _source_stem(path) + path.suffix


def _require_cloud_size(path: Path) -> None:
    if path.stat().st_size > _MAX_CLOUD_BYTES:
        raise ValueError(
            "cloud processing is limited to 20 MB; use a local conversion or a smaller file"
        )


def _gemini_client(tier: str = gemini.SMART):
    """Summarising documents and reading images — the reasoning tier, with a
    long deadline because the input can be a whole file."""
    class _W:
        def generate_content(self, contents):
            resp = gemini.call(contents, tier=tier, timeout_ms=90000)
            if resp is None:
                raise RuntimeError("every Gemini model on the ladder failed")
            return resp

    return _W()


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    image_exts = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"}
    video_exts = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "m4v", "3gp"}
    audio_exts = {"mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus"}
    code_exts  = {"py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c",
                  "cpp", "cs", "go", "rs", "rb", "php", "swift", "kt", "sh",
                  "bash", "ps1", "lua", "r", "m", "sql", "yaml", "toml"}
    archive_exts = {"zip", "rar", "tar", "gz", "7z", "bz2", "xz"}

    if ext in image_exts:  return "image"
    if ext in video_exts:  return "video"
    if ext in audio_exts:  return "audio"
    if ext in code_exts:   return "code"
    if ext in archive_exts: return "archive"
    if ext == "pdf":       return "pdf"
    if ext in ("docx", "doc"): return "docx"
    if ext in ("txt", "md", "rst", "log"): return "text"
    if ext in ("csv", "tsv"): return "csv"
    if ext in ("xlsx", "xls", "ods"): return "excel"
    if ext == "json":      return "json"
    if ext == "xml":       return "xml"
    if ext in ("pptx", "ppt"): return "pptx"
    return "unknown"


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"'{key}' must be true or false")


def _int_param(params: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be a whole number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
    return result


def _float_param(params: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be a number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
    return result


def _text_param(params: dict, key: str, default: str, maximum: int = 256) -> str:
    value = params.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be text")
    if len(value) > maximum:
        raise ValueError(f"'{key}' is too long")
    return value


def _media_time_param(params: dict, key: str, default: str, *, allow_empty: bool = False) -> str:
    value = _text_param(params, key, default, 64).strip()
    if allow_empty and not value:
        return ""
    if not re.fullmatch(r"(?:\d{1,3}:)?[0-5]?\d:[0-5]?\d(?:\.\d+)?|\d+(?:\.\d+)?", value):
        raise ValueError(f"'{key}' must be seconds or a HH:MM:SS timestamp")
    return value


def _staging_output(final_path: Path) -> Path:
    return final_path.parent / (
        f".{final_path.stem}.processing-{secrets.token_hex(6)}{final_path.suffix}"
    )


def _publish_generated(staging: Path, final_path: Path) -> None:
    try:
        details = staging.lstat()
    except OSError as exc:
        raise OSError("the converter did not produce an output file") from exc
    if (
        staging.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or _is_reparse(details)
        or details.st_size > 1024 * 1024 * 1024
    ):
        raise OSError("the converter did not produce a safe regular output file")
    move_no_replace(staging, final_path)


def _write_generated(final_path: Path, writer) -> None:
    staging = _staging_output(final_path)
    try:
        writer(staging)
        _publish_generated(staging, final_path)
    finally:
        staging.unlink(missing_ok=True)


def _run_command(argv: list[str], timeout: float, cancel_event=None) -> tuple[bool, str]:
    command = list(argv)
    if command and Path(command[0]).name.casefold() in {"ffmpeg", "ffmpeg.exe"}:
        # Keep a converter from filling the disk before its deadline fires.
        command = [*command[:-1], "-fs", str(1024 * 1024 * 1024), command[-1]]
    result = run_bounded(
        command, timeout=timeout, max_output=4_000, cancel_event=cancel_event
    )
    if result.cancelled:
        return False, "cancelled"
    if result.timed_out:
        return False, f"timed out after {timeout:.0f} seconds"
    if result.returncode != 0:
        return False, f"converter exited with code {result.returncode}"
    return True, ""


def _read_text_prefix(path: Path, max_bytes: int = 1_000_000) -> str:
    with path.open("rb") as handle:
        return handle.read(max_bytes).decode("utf-8", errors="replace")


def _display_text(value, maximum: int = 160) -> str:
    text = str(value)
    text = "".join(character if ord(character) >= 32 else " " for character in text)
    return text[:maximum]


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"


def _file_size_str(path: Path) -> str:
    return _format_bytes(path.stat().st_size)

def _output_path(src: Path, suffix: str, new_ext: str = None) -> Path:
    """Allocate a non-existing output beside ``src`` without overwriting."""
    ext = new_ext or src.suffix
    source_stem = _source_stem(src)
    base = src.parent / f"{source_stem}_{suffix}{ext}"
    candidate = base
    index = 1
    while candidate.exists():
        candidate = src.parent / f"{source_stem}_{suffix}_{index}{ext}"
        index += 1
        if index > 10_000:
            raise FileExistsError("could not allocate a unique output name")
    return resolve_user_path(candidate, allow_missing=True, reject_symlinks=True)

__all__ = [
    # third-party and standard modules the handlers use
    "re", "json", "os", "shutil", "stat", "tempfile", "tarfile", "secrets",
    "zipfile", "contextmanager", "islice", "Path", "gemini", "PathPolicyError",
    "atomic_create_text", "move_no_replace", "resolve_user_path", "run_bounded",
    # limits
    "_MAX_INPUT_BYTES", "_MAX_CLOUD_BYTES", "_MAX_ARCHIVE_MEMBERS",
    "_MAX_ARCHIVE_EXPANDED", "_MAX_ARCHIVE_RATIO",
    # helpers
    "_safe_input_path", "_is_reparse", "_stable_input_snapshot", "_source_stem",
    "_source_name", "_require_cloud_size", "_gemini_client", "_detect_type",
    "_bool_param", "_int_param", "_float_param", "_text_param",
    "_media_time_param", "_staging_output", "_publish_generated",
    "_write_generated", "_run_command", "_read_text_prefix", "_display_text",
    "_format_bytes", "_file_size_str", "_output_path",
]
