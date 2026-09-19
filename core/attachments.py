"""Session-scoped local attachment imports using the shared task registry.

Files are copied in bounded chunks on two workers; progress measures bytes
actually copied, not a timer animation. No file is executed or uploaded to a
third party. Consumers receive only verified, ready paths, with ownership IDs.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import mimetypes
import os
import stat
import logging
import tempfile
import threading
import uuid

from core import tasks

MAX_BYTES = 512 * 1024 * 1024
MAX_FILES = 32


class AttachmentManager:
    def __init__(self, ready=None):
        self.session_id = uuid.uuid4().hex
        self._directory = tempfile.TemporaryDirectory(prefix="markliv-attachments-")
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="attachment")
        self._lock = threading.RLock()
        self._items = {}
        self._owned_tasks = {}
        self._closed = False
        self._ready = ready or (lambda item: None)

    def add(self, path, task_id=None):
        with self._lock:
            if self._closed:
                raise ValueError("This attachment session is closed")
            if len(self._items) >= MAX_FILES:
                raise ValueError(f"At most {MAX_FILES} attachments per session; remove unused files first.")
            task = tasks.start("upload", Path(path).name, detail="Queued for local import",
                               queued=True, pausable=True, session_id=self.session_id,
                               owner_task_id=task_id)
            item = {"id": task.id, "name": Path(path).name, "source": str(path),
                    "session_id": self.session_id, "task_id": task_id, "path": "",
                    "type": "", "size": None, "preview": "", "ready": False}
            self._items[task.id] = item
            self._owned_tasks[task.id] = task
            self._pool.submit(self._import, item, task)
        return task.id

    def _import(self, item, task):
        destination = Path(self._directory.name) / (task.id + Path(item["name"]).suffix)
        try:
            if not task.checkpoint():
                task.cancel()
                return
            source = Path(item["source"])
            # Validate the opened descriptor, not a prior path lookup. NONBLOCK
            # prevents a path swapped to a FIFO from hanging a worker at open().
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(source, flags)
            with os.fdopen(fd, "rb") as src:
                before = os.fstat(src.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Not a regular file")
                if before.st_size > MAX_BYTES:
                    raise ValueError("File exceeds the 512 MB local attachment limit")
                mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
                with self._lock:
                    item.update(size=before.st_size, type=mime)
                task.update(total_bytes=before.st_size, done_bytes=0, detail="Importing locally")
                digest, copied, prefix = hashlib.sha256(), 0, b""
                with destination.open("xb") as dst:
                    while True:
                        if not task.checkpoint():
                            task.cancel()
                            return
                        chunk = src.read(256 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > MAX_BYTES:
                            raise ValueError("File grew beyond the attachment limit")
                        dst.write(chunk)
                        digest.update(chunk)
                        if len(prefix) < 4096:
                            prefix += chunk[:4096-len(prefix)]
                        task.update(done_bytes=copied)
                opened_after = os.fstat(src.fileno())
            after = source.stat()
            if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                    or opened_after.st_mtime_ns != before.st_mtime_ns):
                raise ValueError("File replaced or changed while importing; please attach it again")
            if (copied != before.st_size or after.st_mtime_ns != before.st_mtime_ns
                    or after.st_size != before.st_size):
                raise ValueError("File changed while importing; please attach it again")
            task.update(detail="Processing file metadata", progress=None)
            preview = ""
            if mime.startswith("text/") or source.suffix.lower() in (".json", ".md", ".py", ".csv"):
                preview = prefix.decode("utf-8", errors="replace")
            with self._lock:
                if task.cancel_requested or task.id not in self._items:
                    task.cancel()
                    return
                item.update(path=str(destination), sha256=digest.hexdigest(),
                            preview=preview, ready=True)
                task.finish(detail="Ready · local attachment", progress=1.0)
                result = dict(item)
                owner = tasks.get(item["task_id"]) if item["task_id"] else None
                if owner:
                    owner.append_meta("attachments", self._public(result))
            # Notifications are best effort, not part of import verification.
            try:
                self._ready(result)
            except Exception:
                logging.getLogger(__name__).warning("Attachment ready notification failed", exc_info=True)
        except Exception as exc:
            task.fail(error=str(exc), detail="Import failed")
        finally:
            if not item["ready"]:
                self._unlink(destination)

    @staticmethod
    def _public(item):
        return {key: item[key] for key in ("id", "name", "path", "type", "size", "session_id", "task_id")}

    def context(self):
        with self._lock:
            return [self._public(x) for x in self._items.values() if x["ready"]]

    def snapshot(self):
        with self._lock:
            return [dict(x, task=self._owned_tasks[x["id"]].snapshot()) for x in self._items.values()]

    def remove(self, ident):
        with self._lock:
            item = self._items.pop(ident, None)
            task = self._owned_tasks.pop(ident, None)
            if task:
                task.request_cancel()
            if item:
                owner = tasks.get(item["task_id"]) if item["task_id"] else None
                if owner:
                    owner.discard_meta_item("attachments", ident)
                if item["ready"]:
                    self._unlink(Path(item["path"]))

    @staticmethod
    def _unlink(path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A locked preview must not crash the Qt callback. TemporaryDirectory
            # retries disposal at session shutdown; never touch the source file.
            logging.getLogger(__name__).warning("Deferred attachment cleanup: %s", path)

    def close(self):
        with self._lock:
            self._closed = True
            items = self.snapshot()
        for item in items:
            self.remove(item["id"])
        self._pool.shutdown(wait=True, cancel_futures=True)
        for item in items:
            task = tasks.get(item["id"])
            if task and task.state in tasks.ACTIVE_STATES:
                task.cancel()
        try:
            self._directory.cleanup()
        except OSError:
            logging.getLogger(__name__).warning("Attachment directory remains locked at shutdown", exc_info=True)
