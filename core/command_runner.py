"""Small, non-interactive subprocess runner shared by setup and validation.

Reader threads work with Windows pipes (selectors do not). Output is forwarded
as chunks, including partial lines, while the main thread owns the deadline.
"""
from __future__ import annotations

import codecs
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Bounded cleanup, including children holding our output pipes open."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        # The parent may have exited before a stubborn descendant.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def run_live(args, *, label: str, cwd=None, env=None, timeout: float = 60,
             heartbeat: float = 2) -> CommandResult:
    """Stream both pipes without waiting for newlines; enforce a wall deadline."""
    started = time.monotonic()
    child_env = dict(os.environ if env is None else env)
    child_env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8:replace",
                     PIP_NO_INPUT="1")
    proc = subprocess.Popen(
        args, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    events = queue.Queue()

    def read_pipe(pipe, channel):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := pipe.read1(4096):
                events.put((channel, decoder.decode(chunk)))
            tail = decoder.decode(b"", final=True)
            if tail:
                events.put((channel, tail))
        finally:
            pipe.close()
            events.put((channel, None))

    readers = [threading.Thread(target=read_pipe, args=(pipe, channel), daemon=True)
               for channel, pipe in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    output = [[], []]
    closed = 0
    timed_out = False
    next_heartbeat = started + heartbeat
    cleanup_deadline = None
    try:
        while closed < 2 or proc.poll() is None:
            now = time.monotonic()
            if not timed_out and now - started >= timeout:
                timed_out = True
                print(f"\n[TIMEOUT] {label} after {now - started:.1f}s "
                      f"(limit {timeout:g}s); terminating process tree...", flush=True)
                _terminate_tree(proc)
                cleanup_deadline = time.monotonic() + 1
            if cleanup_deadline is not None and now >= cleanup_deadline:
                break  # Never wait forever for an inherited pipe handle.
            if now >= next_heartbeat and not timed_out:
                print(f"\n  [RUNNING] {label} — elapsed {now - started:.1f}s "
                      f"/ {timeout:g}s limit", flush=True)
                next_heartbeat = now + heartbeat
            try:
                channel, text = events.get(timeout=0.05)
            except queue.Empty:
                continue
            if text is None:
                closed += 1
            else:
                output[channel].append(text)
                # Merge the display to preserve ordering even when redirected.
                sys.stdout.write(text)
                sys.stdout.flush()
    except BaseException:
        _terminate_tree(proc)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=0.1)
    return CommandResult(proc.returncode, "".join(output[0]), "".join(output[1]),
                         time.monotonic() - started, timed_out)
