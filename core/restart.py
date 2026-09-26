"""Launch a replacement MARK LIV process without coupling it to session cleanup."""
from __future__ import annotations

import os
import platform
from pathlib import Path
import subprocess
import sys
from typing import Callable


def launch_replacement(
    base_dir: Path,
    entrypoint: Path,
    *,
    frozen: bool | None = None,
    popen: Callable = subprocess.Popen,
) -> None:
    """Start a detached replacement process or propagate the launch error."""
    environment = os.environ.copy()
    environment["JARVIS_RESTARTED"] = "1"
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    command = [sys.executable] if is_frozen else [sys.executable, str(entrypoint.resolve())]
    common = {
        "cwd": str(Path(base_dir).resolve()),
        "env": environment,
        "close_fds": True,
    }
    if platform.system() == "Windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        popen(command, creationflags=flags, **common)
    else:
        popen(command, start_new_session=True, **common)
