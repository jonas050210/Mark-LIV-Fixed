"""Bounded child-process execution used by code and project actions.

A timeout on ``subprocess.run`` stops the direct child but can leave a spawned
server or shell descendant behind.  This helper starts a new process group and
terminates the whole group on timeout.  It never invokes a shell and returns a
small immutable-ish result dictionary so callers can safely surface diagnostics.
"""
from __future__ import annotations

import os
import platform
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False

    @property
    def combined(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.append(f"STDOUT:\n{self.stdout.strip()}")
        if self.stderr.strip():
            parts.append(f"STDERR:\n{self.stderr.strip()}")
        return "\n\n".join(parts) if parts else "Ran with no output."


def _creationflags() -> int:
    if platform.system() != "Windows":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if platform.system() == "Windows":
            # CTRL_BREAK is not reliable without a console; terminate the
            # process tree through taskkill when available, then fall back.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
    max_output: int = 32_000,
    cancel_event: threading.Event | None = None,
) -> ProcessResult:
    """Run without a shell; bound captured output and kill the process group."""
    timeout = max(0.5, min(float(timeout), 900.0))
    max_output = max(1_024, min(int(max_output), 1_000_000))
    proc: subprocess.Popen | None = None
    stdout_tail = bytearray()
    stderr_tail = bytearray()
    capture_threads: list[threading.Thread] = []

    def drain(stream, tail: bytearray) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                tail.extend(chunk)
                overflow = len(tail) - max_output
                if overflow > 0:
                    del tail[:overflow]
        except (OSError, ValueError):
            return

    def stop_process() -> None:
        if proc is None or proc.poll() is not None:
            return
        _terminate(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if platform.system() != "Windows":
            # The direct child may exit while a descendant ignores SIGTERM.
            # Escalate against the original process group even in that case.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        elif proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def captured() -> tuple[str, str]:
        # A descendant should not be able to keep inherited pipes open forever.
        streams = (getattr(proc, "stdout", None), getattr(proc, "stderr", None))
        for thread in capture_threads:
            thread.join(timeout=0.25)
        for stream, thread in zip(streams, capture_threads, strict=False):
            if stream is not None and thread.is_alive():
                try:
                    os.close(stream.fileno())
                except OSError:
                    pass
        for stream, thread in zip(streams, capture_threads, strict=False):
            thread.join(timeout=0.75)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return (
            bytes(stdout_tail).decode("utf-8", "replace"),
            bytes(stderr_tail).decode("utf-8", "replace"),
        )

    try:
        proc = subprocess.Popen(
            [str(x) for x in argv],
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(platform.system() != "Windows"),
            creationflags=_creationflags(),
        )
        for stream, tail in ((proc.stdout, stdout_tail), (proc.stderr, stderr_tail)):
            thread = threading.Thread(target=drain, args=(stream, tail), daemon=True)
            thread.start()
            capture_threads.append(thread)

        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                stop_process()
                stdout, stderr = captured()
                return ProcessResult(None, stdout, stderr, cancelled=True)
            if time.monotonic() >= deadline:
                stop_process()
                stdout, stderr = captured()
                return ProcessResult(
                    None, stdout, stderr or f"Process timed out after {timeout:g} seconds.",
                    timed_out=True,
                )
            time.sleep(0.05)

        stdout, stderr = captured()
        return ProcessResult(proc.returncode, stdout, stderr)
    except FileNotFoundError:
        return ProcessResult(127, "", "executable was not found")
    except Exception as exc:
        stop_process()
        return ProcessResult(1, "", f"process failed ({type(exc).__name__})")


__all__ = ["ProcessResult", "run_bounded"]
