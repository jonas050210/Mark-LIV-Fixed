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
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

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
) -> ProcessResult:
    """Run ``argv`` without a shell and kill its process group on timeout."""
    timeout = max(0.5, min(float(timeout), 900.0))
    proc = None
    try:
        proc = subprocess.Popen(
            [str(x) for x in argv],
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(platform.system() != "Windows"),
            creationflags=_creationflags(),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return ProcessResult(proc.returncode, stdout[-max_output:], stderr[-max_output:])
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            _terminate(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                stdout, stderr = proc.communicate()
        else:
            stdout, stderr = "", ""
        # communicate() may contain bytes only in unusual mocked callers.
        stdout = stdout if isinstance(stdout, str) else (stdout or b"").decode("utf-8", "replace")
        stderr = stderr if isinstance(stderr, str) else (stderr or b"").decode("utf-8", "replace")
        return ProcessResult(None, stdout[-max_output:], stderr[-max_output:] or str(exc), True)
    except FileNotFoundError as exc:
        return ProcessResult(127, "", str(exc))
    except Exception as exc:
        return ProcessResult(1, "", str(exc))


__all__ = ["ProcessResult", "run_bounded"]
