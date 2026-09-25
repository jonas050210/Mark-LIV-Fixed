"""Local ``Hey Jarvis`` wake-word support.

The wake-word package is optional and contains native dependencies.  Two rules
are important here:

* Checking the settings drawer must never import :mod:`openwakeword`.  Importing
  it loads NumPy/ONNX native libraries and, on some Windows installations, can
  terminate the GUI process before Python gets a chance to raise an exception.
* The model and the one-time download run in a child process.  A bad native
  wheel therefore cannot take the Qt process down with it.

The public API is intentionally small so the rest of the application can use
this module without knowing how the engine is isolated.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import queue
import struct
import subprocess
import sys
import threading
from array import array

from core.process_runner import run_bounded
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable

WAKE_MODEL = "hey_jarvis"
DEFAULT_THRESHOLD = 0.5
SAMPLE_RATE = 16000
_DOWNLOAD_TIMEOUT = 15 * 60
_FRAME_LIMIT = 2 * 1024 * 1024


def _package_directory() -> Path | None:
    """Return openwakeword's directory without importing the package.

    ``find_spec`` is deliberately used only with the top-level package name.
    Looking up a dotted name would import its parent package.  The returned
    directory is enough to inspect the model files and keeps the settings UI
    completely independent of the native wake-word stack.
    """
    try:
        spec = importlib.util.find_spec("openwakeword")
    except Exception:
        return None
    if spec is None:
        return None

    locations = spec.submodule_search_locations
    if locations:
        try:
            return Path(next(iter(locations)))
        except (StopIteration, TypeError, OSError, ValueError):
            return None
    if spec.origin and spec.origin not in {"built-in", "frozen"}:
        try:
            return Path(spec.origin).resolve().parent
        except (OSError, ValueError):
            return None
    return None


def is_installed() -> bool:
    """Return whether the package can be located, without executing it."""
    return _package_directory() is not None


def _models_directory() -> Path | None:
    package_dir = _package_directory()
    if package_dir is None:
        return None
    models = package_dir / "resources" / "models"
    return models if models.is_dir() else None


def is_ready() -> bool:
    """Return whether all files needed by the ONNX detector are present.

    This function is called while the Qt settings drawer is opening.  Do not
    replace this with ``import openwakeword`` or ``Model(...)``: either one can
    load native DLLs on the GUI thread and was the cause of the silent settings
    crash.
    """
    models_dir = _models_directory()
    if models_dir is None:
        return False
    try:
        has_wake = any(models_dir.glob(f"{WAKE_MODEL}*.onnx")) or any(
            models_dir.glob(f"{WAKE_MODEL}*.tflite")
        )
        has_mel = any(models_dir.glob("melspectrogram*.onnx")) or any(
            models_dir.glob("melspectrogram*.tflite")
        )
        has_embedding = any(models_dir.glob("embedding_model*.onnx")) or any(
            models_dir.glob("embedding_model*.tflite")
        )
        return bool(has_wake and has_mel and has_embedding)
    except OSError:
        return False


def _download_models_child() -> int:
    """Download models in the isolated helper process.

    This function is only called by ``python core/wake_word.py
    --download-models``.  Keeping the import here is what prevents
    openwakeword/onnxruntime from entering the main process.
    """
    try:
        # The library's progress bar is useful in a terminal but its output is
        # not part of the parent/child detector protocol (there is none during
        # setup), so leave it visible to the captured child stdout.
        import openwakeword.utils as utils

        download = getattr(utils, "download_models", None)
        if download is None:
            return 2
        try:
            download([WAKE_MODEL])
        except TypeError:
            # Compatibility with older openwakeword releases whose helper had
            # no model_names parameter and downloaded the default set.
            download()
        return 0
    except Exception as exc:
        print(f"model download failed ({type(exc).__name__})", file=sys.stderr, flush=True)
        return 1


def _read_exact(stream, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_child_event(event: str) -> None:
    sys.stdout.write(event + "\n")
    sys.stdout.flush()


def _serve_child(threshold: float = DEFAULT_THRESHOLD) -> int:
    """Run the native detector in a process that never imports the GUI."""
    try:
        # Some releases print a banner while importing.  Keep that out of the
        # tiny line protocol consumed by the parent.
        with redirect_stdout(sys.stderr):
            import numpy as np
            from openwakeword.model import Model
            model = Model(
                wakeword_models=[WAKE_MODEL],
                inference_framework="onnx",
            )
    except Exception as exc:
        print(f"wake engine failed to start ({type(exc).__name__})", file=sys.stderr, flush=True)
        return 1

    _write_child_event("READY")
    source = getattr(sys.stdin, "buffer", sys.stdin)
    while True:
        header = _read_exact(source, 4)
        if header is None:
            return 0
        (length,) = struct.unpack("!I", header)
        if length > _FRAME_LIMIT:
            print("wake frame is too large", file=sys.stderr, flush=True)
            return 2
        payload = _read_exact(source, length)
        if payload is None:
            return 0
        if payload == b"STOP":
            return 0
        if not payload.startswith(b"A"):
            continue
        try:
            scores = model.predict(np.frombuffer(payload[1:], dtype=np.int16))
            score = 0.0
            if isinstance(scores, dict):
                for name, value in scores.items():
                    if "jarvis" in str(name).lower():
                        score = max(score, float(value))
                if score == 0.0 and scores:
                    score = max(float(value) for value in scores.values())
            if score >= threshold:
                _write_child_event("DETECT")
        except Exception as exc:
            print(f"wake inference failed ({type(exc).__name__})", file=sys.stderr, flush=True)


def install_and_download(
    logger: Callable[[str], None] = print,
    notify: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Install openwakeword and download its models in an isolated process.

    Returns ``(ok, message)`` and never lets a setup failure escape into the Qt
    worker.  ``invalidate_caches`` matters because the package may have been
    installed while this application was already running.
    """
    tell = notify or (lambda _message: None)
    try:
        if not is_installed():
            message = "Wake word: installing openwakeword (one-time)…"
            logger(message)
            tell(message)
            result = run_bounded(
                [sys.executable, "-m", "pip", "install", "openwakeword"],
                timeout=_DOWNLOAD_TIMEOUT,
                max_output=100_000,
            )
            if result.timed_out:
                return False, "pip install timed out after 15 minutes."
            if result.returncode != 0:
                return False, f"pip install failed (exit code {result.returncode})."
            importlib.invalidate_caches()

        if not is_installed():
            return False, "openwakeword was installed but cannot be located by this Python."

        message = "Wake word: downloading models…"
        logger(message)
        tell(message)
        command = [sys.executable, str(Path(__file__).resolve()), "--download-models"]
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        result = run_bounded(
            command,
            timeout=_DOWNLOAD_TIMEOUT,
            env=env,
            max_output=100_000,
        )
        if result.timed_out:
            return False, "model download timed out after 15 minutes."

        # Forward useful child progress to the existing console/activity log.
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line:
                logger(f"Wake word: {line}")
        if result.returncode != 0:
            if result.returncode is not None and result.returncode < 0:
                return False, f"model download helper stopped by signal {-result.returncode}."
            return False, f"model download failed (exit code {result.returncode})."

        importlib.invalidate_caches()
        if not is_ready():
            return False, "download finished, but the required wake model files are missing."
        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except Exception as exc:
        return False, f"setup failed ({type(exc).__name__})."


class WakeWordDetector:
    """Supervise an isolated openwakeword process.

    ``feed`` only copies a frame into a bounded queue.  The child process owns
    all NumPy/ONNX/openwakeword imports, so a native access violation cannot
    close the assistant or its settings window.
    """

    def __init__(
        self,
        on_detect: Callable[[], None],
        threshold: float = DEFAULT_THRESHOLD,
        logger: Callable[[str], None] = print,
        notify: Callable[[str], None] | None = None,
    ):
        self._on_detect = on_detect
        self._threshold = threshold  # kept for API compatibility
        self._logger = logger
        self._notify = notify or (lambda _message: None)
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=50)
        self._process: subprocess.Popen[bytes] | None = None
        self._send_thread: threading.Thread | None = None
        self._read_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._running = False
        self._ready = False
        self._stopping = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Start the child without blocking the Qt thread on model loading."""
        with self._lock:
            if self._running:
                return True
            self._stopping = False
        try:
            command = [
                sys.executable,
                "-u",
                str(Path(__file__).resolve()),
                "--serve",
                str(self._threshold),
            ]
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=flags,
            )
        except Exception as exc:
            self._logger(
                f"Wake word: could not start isolated engine ({type(exc).__name__})."
            )
            self._notify("Wake word unavailable — use the WAKE NOW button.")
            return False

        with self._lock:
            self._process = process
            self._running = True
            self._ready = False
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True, name="WakeWordPipeWriter")
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True, name="WakeWordPipeReader")
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True, name="WakeWordPipeErrors")
        self._send_thread.start()
        self._read_thread.start()
        self._stderr_thread.start()
        self._logger(f"Wake word: starting isolated engine (pid {process.pid})…")
        return True

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            process = self._process
            self._running = False
            self._ready = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.write(struct.pack("!I", 4) + b"STOP")
                process.stdin.flush()
                process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        with self._lock:
            self._process = None

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    def feed(self, frame_int16) -> None:
        """Copy an int16 mic block without ever blocking the audio callback."""
        with self._lock:
            running = self._running
        if not running:
            return
        try:
            frame = frame_int16[:, 0] if getattr(frame_int16, "ndim", 1) > 1 else frame_int16
            if hasattr(frame, "astype"):
                frame = frame.astype("int16", copy=True)
                data = frame.tobytes()
            elif hasattr(frame, "tobytes"):
                data = frame.tobytes()
            else:
                data = array("h", frame).tobytes()
            self._queue.put_nowait(data)
        except queue.Full:
            pass
        except Exception:
            pass

    def _send_loop(self) -> None:
        while True:
            try:
                data = self._queue.get()
            except Exception:
                return
            if data is None:
                return
            with self._lock:
                process = self._process
                running = self._running
            if not running or process is None or process.stdin is None:
                return
            try:
                payload = b"A" + data
                process.stdin.write(struct.pack("!I", len(payload)) + payload)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                return

    def _stderr_loop(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._logger(f"Wake word: engine error: {line[:240]}")
        except (OSError, ValueError):
            pass

    def _read_loop(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for raw in iter(process.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if line == "READY":
                    with self._lock:
                        self._ready = True
                    self._logger(f"Wake word: engine ready in child pid {process.pid} [{WAKE_MODEL}].")
                elif line == "DETECT":
                    try:
                        self._on_detect()
                    except Exception as exc:
                        self._logger(
                            f"Wake word: on_detect error ({type(exc).__name__})."
                        )
                elif line:
                    # Be tolerant if a native dependency writes text to stdout.
                    self._logger(f"Wake word: engine: {line[:240]}")
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass
            with self._lock:
                was_running = self._running
                stopping = self._stopping
                self._running = False
                self._ready = False
            if was_running and not stopping:
                code = process.poll()
                self._logger(f"Wake word: engine stopped ({code}); wake word is off.")
                self._notify("Wake word engine stopped — use the WAKE NOW button.")


if __name__ == "__main__":
    if "--download-models" in sys.argv:
        raise SystemExit(_download_models_child())
    if "--serve" in sys.argv:
        try:
            threshold = float(sys.argv[sys.argv.index("--serve") + 1])
        except (IndexError, ValueError):
            threshold = DEFAULT_THRESHOLD
        raise SystemExit(_serve_child(threshold))
    print("This module is used by MARK LIV; run python check_wake_word.py for diagnostics.")
