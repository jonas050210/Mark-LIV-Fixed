"""Atomic, permission-aware JSON persistence.

Runtime configuration, OAuth tokens, shortcuts, and assistant memory are all
read and updated from multiple worker threads.  A plain ``read_text`` followed
by ``write_text`` loses concurrent updates and can leave a truncated file when
the process exits during the write.  This module provides the one persistence
primitive those stores use:

* the full read/modify/write transaction is protected by a process-local lock
  and an advisory cross-process lock;
* data is written to a private temporary file, flushed, and atomically replaced;
* the previous valid value is retained as a private backup;
* malformed primary files are quarantined and recovered from that backup rather
  than being silently treated as an empty configuration.
"""
from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class JsonStoreError(RuntimeError):
    """Base error for a persistent JSON store."""


class JsonStoreCorruptError(JsonStoreError):
    """Raised when neither the primary file nor its backup is valid."""


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _is_reparse_point(details) -> bool:
    return bool(int(getattr(details, "st_file_attributes", 0)) & 0x400)


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _set_descriptor_mode(descriptor: int, mode: int) -> None:
    """Apply POSIX descriptor permissions where the platform supports them."""
    operation = getattr(os, "fchmod", None)
    if not callable(operation):
        return
    try:
        operation(descriptor, mode)
    except OSError:
        pass


@contextmanager
def _advisory_lock(path: Path):
    """Best-effort blocking advisory lock shared by application processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_lock = lock_path.lstat()
    except FileNotFoundError:
        existing_lock = None
    if lock_path.is_symlink() or (
        existing_lock is not None and _is_reparse_point(existing_lock)
    ):
        raise JsonStoreError(f"lock path may not be a link or reparse point: {lock_path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        lock_details = os.fstat(descriptor)
        if not stat.S_ISREG(lock_details.st_mode) or _is_reparse_point(lock_details):
            raise JsonStoreError(f"lock path is not a regular file: {lock_path}")
        _set_descriptor_mode(descriptor, 0o600)
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _read_json(path: Path, max_bytes: int):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or _is_reparse_point(details):
            raise ValueError("JSON store is not a regular file")
        if details.st_size > max_bytes:
            raise ValueError(f"JSON store exceeds the {max_bytes:,}-byte limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError(f"JSON store exceeds the {max_bytes:,}-byte limit")
    finally:
        os.close(descriptor)

    def reject_constant(value: str):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    return json.loads(content.decode("utf-8"), parse_constant=reject_constant)


def _private(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or _is_reparse_point(details):
            return
        _set_descriptor_mode(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":  # opening a directory this way is not portable there
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _atomic_backup(
    source: Path, destination: Path, *, private: bool, max_bytes: int
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    source_descriptor: int | None = None
    output_owns_descriptor = False
    try:
        if private:
            _set_descriptor_mode(descriptor, 0o600)
        source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, source_flags)
        source_details = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_details.st_mode) or _is_reparse_point(source_details):
            raise JsonStoreError("JSON backup source is not a regular file")
        if source_details.st_size > max_bytes:
            raise JsonStoreError("JSON backup source exceeds its size limit")
        with (
            os.fdopen(source_descriptor, "rb", closefd=False) as input_handle,
            os.fdopen(descriptor, "wb") as output_handle,
        ):
            output_owns_descriptor = True
            copied = 0
            while True:
                chunk = input_handle.read(min(1024 * 1024, max_bytes + 1 - copied))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > max_bytes:
                    raise JsonStoreError("JSON backup source grew beyond its size limit")
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temporary, destination)
        if private:
            _private(destination)
        _sync_directory(destination.parent)
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        if not output_owns_descriptor:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class JsonStore(Generic[T]):
    """Atomic JSON store with validation and last-known-good recovery."""

    def __init__(
        self,
        path: str | Path,
        default_factory: Callable[[], T],
        *,
        validator: Callable[[object], bool] | None = None,
        private: bool = True,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(f".{self.path.name}.bak")
        self._default_factory = default_factory
        self._validator = validator or (lambda _value: True)
        self._private_file = bool(private)
        self._max_bytes = max(1_024, min(int(max_bytes), 256 * 1024 * 1024))

    def _validate(self, value: object, source: Path) -> T:
        try:
            valid = bool(self._validator(value))
        except Exception as exc:
            raise JsonStoreCorruptError(
                f"{source} could not be validated: {exc}"
            ) from exc
        if not valid:
            raise JsonStoreCorruptError(f"{source} has an invalid JSON shape")
        return value  # type: ignore[return-value]

    def _read_candidate(self, path: Path) -> T:
        try:
            details = path.lstat()
            if path.is_symlink() or _is_reparse_point(details):
                raise ValueError("symbolic-link or reparse-point stores are not allowed")
            return self._validate(_read_json(path, self._max_bytes), path)
        except JsonStoreCorruptError:
            raise
        except Exception as exc:
            raise JsonStoreCorruptError(f"could not read {path}: {exc}") from exc

    def _quarantine(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        quarantine = self.path.with_name(f".{self.path.name}.corrupt-{stamp}")
        counter = 1
        while quarantine.exists():
            quarantine = self.path.with_name(
                f".{self.path.name}.corrupt-{stamp}-{counter}"
            )
            counter += 1
        try:
            os.replace(self.path, quarantine)
            _private(quarantine)
            return quarantine
        except OSError:
            return None

    def _load_locked(self, *, recover: bool) -> T:
        if not self.path.exists():
            return copy.deepcopy(self._default_factory())
        try:
            value = self._read_candidate(self.path)
            if self._private_file:
                _private(self.path)
            return value
        except JsonStoreCorruptError as primary_error:
            if recover and self.backup_path.exists():
                try:
                    value = self._read_candidate(self.backup_path)
                except JsonStoreCorruptError:
                    pass
                else:
                    self._quarantine()
                    self._write_locked(value, keep_backup=False)
                    return value
            raise primary_error

    def read(self, *, recover: bool = True) -> T:
        lock = _thread_lock(self.path)
        with lock, _advisory_lock(self.path):
            return copy.deepcopy(self._load_locked(recover=recover))

    def _write_locked(self, value: T, *, keep_backup: bool = True) -> None:
        self._validate(value, self.path)
        serialized = (
            json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(serialized) > self._max_bytes:
            raise JsonStoreError(
                f"JSON store would exceed the {self._max_bytes:,}-byte limit"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if keep_backup and self.path.exists():
            try:
                # Back up only valid JSON. A corrupt primary must never replace
                # the last known-good recovery copy.
                self._read_candidate(self.path)
                _atomic_backup(
                    self.path,
                    self.backup_path,
                    private=self._private_file,
                    max_bytes=self._max_bytes,
                )
            except (OSError, JsonStoreCorruptError):
                pass

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        descriptor_open = True
        try:
            if self._private_file:
                _set_descriptor_mode(descriptor, 0o600)
            handle = os.fdopen(descriptor, "wb")
            descriptor_open = False
            with handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if self._private_file:
                _private(self.path)
            _sync_directory(self.path.parent)
        finally:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def write(self, value: T) -> None:
        lock = _thread_lock(self.path)
        with lock, _advisory_lock(self.path):
            self._write_locked(copy.deepcopy(value))

    def update(self, mutator: Callable[[T], T | None]) -> T:
        """Atomically mutate and persist the current value.

        ``mutator`` may modify its argument in place and return ``None``, or
        return a replacement value. The committed value is returned as a copy.
        """
        lock = _thread_lock(self.path)
        with lock, _advisory_lock(self.path):
            current = copy.deepcopy(self._load_locked(recover=True))
            replacement = mutator(current)
            value = current if replacement is None else replacement
            self._write_locked(value)
            return copy.deepcopy(value)


__all__ = [
    "JsonStore",
    "JsonStoreError",
    "JsonStoreCorruptError",
]
