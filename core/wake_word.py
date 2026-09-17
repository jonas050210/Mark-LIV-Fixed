"""
Local wake-word detection for JARVIS ("Hey Jarvis").

Design goals:
  • ZERO cost when the feature is off — openwakeword is never imported by this
    process at all, not even inside a function (see below). If the user never
    enables wake word, none of this touches the app.
  • ZERO latency on the audio path — the microphone callback only ever does a
    cheap, non-blocking queue push (feed()); a writer thread hands the block to
    the engine process and the model runs over there, so neither the real-time
    audio thread nor the Gemini stream is ever slowed.
  • ZERO risk to the app — the engine runs in a CHILD PROCESS. When it dies, and
    on this machine it did die, JARVIS logs it, falls back to listening
    continuously, and carries on.
  • Fully local & offline — audio fed here never leaves the machine; there is no
    network call except the one-time model download the user triggers from the UI.

WHY A CHILD PROCESS, AND NOT A THREAD (this file used to do it in a thread)
    openwakeword ships small ONNX models that run comfortably on a CPU, and a
    background thread is the obvious way to keep them off the audio path. It is
    also fatal on Windows: importing openwakeword loads onnxruntime's native
    DLLs into THIS process, where PyQt6, PortAudio, OpenCV and numpy are already
    resident, and that combination produced

        Windows fatal exception: access violation
        onnxruntime/capi/_pybind_state.py:32 in <module>       ← DLL load
        openwakeword/vad.py:48 → openwakeword/__init__.py:3
        core/wake_word.py:169 in start()  ("from openwakeword.model import Model")
        main.py:684 _ensure_wake_detector → main.py:731 _ui_wake_toggle
        ui.py:4909 _toggle_wake_word  (Qt mainloop)

    i.e. pressing ⚙ → WAKE WORD closed the whole app. A DLL fault during load
    cannot be caught — there is no Python frame left to raise into — so no amount
    of try/except around the import helps. The same imports succeed in a *fresh*
    interpreter, which check_wake_word.py verifies in isolated subprocesses, so
    the only robust fix is to never load them here: core/wake_worker.py owns the
    model, and this module owns the pipe that talks to it (core/wake_proto.py).

    A plain subprocess is used rather than multiprocessing's "spawn" context
    because spawn re-imports the parent's __main__ — main.py — inside the child,
    which would load PyQt6/PortAudio/OpenCV there first and recreate the very
    collision we are escaping. Details in core/wake_proto.py.

Public API (unchanged, main.py and ui.py keep working as before):
    WakeWordDetector(on_detect, threshold, logger, notify, on_down)
        .start() -> bool   .stop()   .feed(frame_int16)   .ready
    is_installed()   is_ready()   install_and_download(logger, notify)
"""
from __future__ import annotations

import atexit
import importlib
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Callable

try:
    from . import wake_proto as proto
except ImportError:                       # imported as a top-level module
    import wake_proto as proto            # type: ignore

# Pretrained openwakeword model that listens for "Hey Jarvis".
WAKE_MODEL = "hey_jarvis"
# Score in [0,1]; above this counts as a detection. Tunable per environment.
DEFAULT_THRESHOLD = 0.5
# Mic frames arrive at 16 kHz int16; this is just the detector's input rate.
SAMPLE_RATE = 16000

# Inference framework for the engine process. onnxruntime is the only option on
# Windows/macOS — openwakeword declares tflite-runtime for Linux only.
FRAMEWORK = "onnx"

# ── Timing / limits for the parent side of the pipe ─────────────────────────
# Mic blocks pending for the writer thread. 64 blocks of 64 ms ≈ 4 s of audio:
# enough to absorb a slow predict(), small enough that a dead engine cannot make
# the queue grow. feed() drops (never blocks) once this is full.
_SEND_QUEUE = 64
# How often the parent pings the child. The child exits if it hears NOTHING for
# --orphan-timeout, so a parent killed without closing its handles (power loss,
# TerminateProcess) cannot leave a 200 MB onnxruntime process behind forever.
_PING_EVERY = 5.0
_ORPHAN_TIMEOUT = 60.0
# The child sends a heartbeat every 5 s; this many seconds without ANY message
# from it means the engine is wedged (alive but not reading, not predicting).
_HEALTH_TIMEOUT = 30.0
# start() waits this long for an early hard failure, so "the engine could not
# even launch" is reported synchronously. Deliberately short: start() is called
# on the Qt thread and on the event loop, and blocking either is how the old
# in-process import made the settings drawer look broken (2.1 s of freeze).
_START_GRACE = 0.4
# An engine that never reports READY (it hung while loading the model, or the
# machine is doing something extraordinary) is worse than one that crashes: the
# microphone gate is already closed behind it, so JARVIS would be deaf and
# nothing would ever say so. After this long the child is killed and reported.
_READY_TIMEOUT = 60.0
# Ignore repeat detections for this long. The child has its own refractory
# period and resets its buffers; this one is the parent's insurance against a
# double wake from two events that were already in flight.
_REFRACTORY = 1.5
# A crash loop is worse than no wake word: three unexpected deaths inside this
# window stops the auto-retry, and the user gets told instead of watching JARVIS
# spawn and kill a process forever.
_MAX_CRASHES = 3
_CRASH_WINDOW = 900.0
# Model download: pip + ~20 MB of GitHub release assets on an unknown line.
_DOWNLOAD_TIMEOUT = 900.0

_BASE_DIR = Path(__file__).resolve().parent.parent
_WORKER = Path(__file__).resolve().parent / "wake_worker.py"

# Every detector that is alive, so the interpreter can shut the children down on
# a normal exit. Weak: a detector that is dropped must not be kept alive by this.
_LIVE: "weakref.WeakSet[WakeWordDetector]" = weakref.WeakSet()

# NTSTATUS codes that a Windows child can die with, as subprocess reports them
# (a large negative number). Named here because "engine died: -1073741819" tells
# the user nothing, while "access violation" tells them exactly which class of
# problem this is — and it is the class this whole design exists to survive.
_WIN_STATUS = {
    0xC0000005: "access violation (a native DLL faulted)",
    0xC0000135: "DLL not found",
    0xC0000139: "DLL entry point not found (mismatched build)",
    0xC0000142: "DLL initialisation failed",
    0xC00000FD: "stack overflow",
    0xC0000409: "stack buffer overrun (/GS fastfail)",
    0x80000003: "breakpoint (a debugger-only instruction)",
}


def _frozen() -> bool:
    """True when running from a PyInstaller-style bundle, where sys.executable is
    the app itself and not a Python interpreter — so there is no interpreter to
    hand to the engine subprocess. Wake word is then unavailable rather than
    trying to re-launch the whole app as its own child."""
    return bool(getattr(sys, "frozen", False))


def _package_dir() -> Path | None:
    """Locate the installed openwakeword package WITHOUT importing it.

    find_spec() reads only the package's metadata — the module body is never
    executed, so none of openwakeword's heavy native dependencies
    (onnxruntime / tflite) are loaded. That is the whole point: is_ready() runs
    on the GUI thread the moment the ⚙ settings drawer opens, and importing
    there loaded those DLLs onto the Qt thread — a broken pair of them aborts
    the entire process with no Python traceback at all, which looked like
    "clicking the gear closes the app silently". (core/audio_devices.py fixed
    the same lesson for the device list; this is the remaining call site.)
    """
    try:
        spec = importlib.util.find_spec("openwakeword")
        if spec is None:
            return None
        if spec.submodule_search_locations:
            return Path(list(spec.submodule_search_locations)[0])
        if spec.origin:
            return Path(spec.origin).resolve().parent
    except Exception:
        pass
    return None


def is_installed() -> bool:
    """True if the openwakeword package is importable (no model check)."""
    try:
        return importlib.util.find_spec("openwakeword") is not None
    except Exception:
        return False


def is_ready() -> bool:
    """True if openwakeword is installed AND its model files are present on disk.

    This is a cheap, DETERMINISTIC file-existence check. It deliberately does NOT
    construct a Model to probe readiness — doing that is slow and, worse, can clash
    with the detector's own Model when it's already running, which intermittently
    returned False and made the UI flicker to 'not downloaded'. It also does NOT
    import the openwakeword package at all (see _package_dir): the check runs on
    the GUI thread when the settings drawer opens, and an import there can take
    the whole app down. Never raises.
    """
    if not is_installed():
        return False
    try:
        pkg_dir = _package_dir()
        if pkg_dir is None:
            return False
        models_dir = pkg_dir / "resources" / "models"
        if not models_dir.is_dir():
            return False
        has_wake = (any(models_dir.glob(f"{WAKE_MODEL}*.onnx"))
                    or any(models_dir.glob(f"{WAKE_MODEL}*.tflite")))
        has_mel = (any(models_dir.glob("melspectrogram*.onnx"))
                   or any(models_dir.glob("melspectrogram*.tflite")))
        has_emb = (any(models_dir.glob("embedding_model*.onnx"))
                   or any(models_dir.glob("embedding_model*.tflite")))
        return bool(has_wake and has_mel and has_emb)
    except Exception:
        return False


def describe_exit(code: int | None) -> str:
    """Turn a child's exit status into a sentence a person can act on.

    Negative means "killed by something": on POSIX it is a signal number (small),
    on Windows subprocess reports the raw NTSTATUS as a huge negative number
    (0xC0000005 → -1073741819). The two cannot be confused, and naming the
    Windows ones matters — "access violation" is the exact class of failure this
    whole design exists to survive.
    """
    if code is None:
        return "still running"
    if code == 0:
        return "clean exit"
    if code == 3:
        return "engine exited because the app stopped feeding it"
    if code < 0:
        sig = -code
        if sig <= 64:                            # a POSIX signal
            try:
                import signal
                return f"killed by signal {signal.Signals(sig).name}"
            except Exception:
                return f"killed by signal {sig}"
        status = code & 0xFFFFFFFF               # a Windows NTSTATUS
        what = _WIN_STATUS.get(status)
        return (f"{what} [0x{status:08X}]" if what
                else f"native failure [0x{status:08X}]")
    return f"exited with code {code} (a Python-level error — see the console above)"


# numpy is needed by feed() to flatten a microphone block into PCM bytes. It is
# imported lazily (and the result cached, including a failure) so that this
# module stays importable — is_ready() runs on the GUI thread — in an environment
# without numpy, and so a missing numpy costs one failed import instead of one
# per audio callback.
_NP = None
_NP_TRIED = False


def _numpy():
    """numpy, or None when it cannot be imported."""
    global _NP, _NP_TRIED
    if _NP_TRIED:
        return _NP
    _NP_TRIED = True
    try:
        import numpy as np
        _NP = np
    except Exception:
        _NP = None
    return _NP


def install_and_download(logger: Callable[[str], None] = print,
                         notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """
    One-click setup for the UI button: pip-install openwakeword if missing, then
    download the wake model. Returns (ok, message). Never raises — every failure
    is reported through the returned message and the logger.

    Neither step imports openwakeword in THIS process. pip is a subprocess
    (as before) and the download itself runs in the same throwaway engine child
    that would otherwise be the only place the package is touched — because
    `import openwakeword.utils` loads exactly the native DLLs that take the app
    down. Success is then verified with the import-free is_ready() file check.
    """
    _tell = notify or (lambda _msg: None)
    try:
        if _frozen():
            return False, ("this is a packaged build without a Python interpreter "
                           "to run the wake-word engine in — install openwakeword "
                           "and its models into a normal Python environment.")

        if not is_installed():
            logger("Wake word: installing openwakeword (one-time)…")
            _tell("Wake word: installing openwakeword (one-time)…")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "openwakeword"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_DOWNLOAD_TIMEOUT,
            )
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                return False, f"pip install failed: {tail[0][:160]}"
            # A package installed while we were running is invisible to find_spec
            # until the import system's directory caches are dropped — without
            # this, is_installed() below still says False and setup "fails" on a
            # success.
            try:
                importlib.invalidate_caches()
            except Exception:
                pass
            if not is_installed():
                return False, ("pip reported success but openwakeword is still not "
                               "importable (wrong interpreter?) — "
                               f"{sys.executable}")

        # Download the pretrained melspectrogram/embedding + wake models — in the
        # engine child, so a native crash during the import costs this subprocess
        # and not the app.
        logger("Wake word: downloading models…")
        _tell("Wake word: downloading models…")
        ok, detail, rc = _run_worker_once(
            ["--mode", "download", "--model", WAKE_MODEL],
            timeout=_DOWNLOAD_TIMEOUT,
        )
        if not ok:
            msg = f"model download failed: {detail}" if detail else "model download failed"
            logger(f"Wake word: {msg} (exit {rc})")
            return False, msg[:400]
        if detail:
            logger(f"Wake word: {detail}")

        if not is_ready():
            return False, ("installed, but the wake model files are not on disk — "
                           "the download did not complete.")
        logger("Wake word: ready.")
        return True, "Wake word installed and ready."
    except subprocess.TimeoutExpired:
        return False, "setup timed out (network?)"
    except Exception as e:
        return False, f"setup error: {e}"


def _run_worker_once(extra_args: list, timeout: float = 120.0) -> tuple[bool, str, int]:
    """Run core/wake_worker.py to completion and parse its framed reply.

    Returns (ok, detail, returncode). Used for the one-shot modes (download,
    selftest); the long-lived `serve` mode is driven by WakeWordDetector instead.
    A nonzero returncode with no `error` event means the child died natively —
    which is information, not a mystery, and is reported as such.
    """
    if not _WORKER.exists():
        return False, f"engine script missing: {_WORKER}", -1
    cmd = [sys.executable, "-X", "utf8", str(_WORKER), *extra_args]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # Unbuffered so a child that dies mid-download does not take the last lines
    # of its own error output with it into a lost stdio buffer.
    env["PYTHONUNBUFFERED"] = "1"
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           cwd=str(_BASE_DIR), env=env)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {int(timeout)}s", -1
    except Exception as e:
        return False, f"could not start the engine process: {e}", -1

    err_tail = ""
    try:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        if err:
            err_tail = err.splitlines()[-1][:200]
    except Exception:
        pass

    detail, ok = "", False
    for payload in proto.frames_in(r.stdout or b""):
        tag, body = proto.split(payload)
        if tag != proto.TAG_JSON:
            continue
        msg = proto.decode_json(body)
        if not msg:
            continue
        t = msg.get("t")
        if t == proto.T_ERROR:
            return False, f"{msg.get('stage', '?')}: {msg.get('msg', '')}", r.returncode
        if t == "downloaded":
            ok = True
            files = ", ".join(msg.get("files") or []) or "no files"
            detail = f"models downloaded in {msg.get('seconds', '?')}s — {files}"
        elif t == "selftest":
            ok = True
            v = msg.get("versions") or {}
            s = msg.get("seconds") or {}
            detail = (f"engine OK — model '{msg.get('model')}' loaded in "
                      f"{s.get('load', '?')}s, one prediction in {s.get('predict', '?')}s "
                      f"(openwakeword {v.get('openwakeword', '?')}, "
                      f"onnxruntime {v.get('onnxruntime', '?')}, "
                      f"numpy {v.get('numpy', '?')}, python {v.get('python', '?')})")
    if r.returncode != 0 and not ok:
        # No Python-level error event, but the process did not exit cleanly: this
        # is the native-crash signature. Say so, with the code decoded.
        why = describe_exit(r.returncode)
        return False, (f"the engine process died before replying — {why}"
                       + (f"; last output: {err_tail}" if err_tail else "")), r.returncode
    if not ok and not detail:
        return False, f"the engine process produced no reply ({describe_exit(r.returncode)})", r.returncode
    return ok, detail, r.returncode


def selftest(logger: Callable[[str], None] = print) -> tuple[bool, str]:
    """Load the model in a throwaway child and report what happened. Diagnostic
    helper (check_wake_word.py, and anyone chasing a wake-word bug): it answers
    "does the isolated engine work on THIS machine?" without ever risking the
    app process."""
    if not is_ready():
        return False, "openwakeword or its model files are missing — download first."
    ok, detail, _rc = _run_worker_once(
        ["--mode", "selftest", "--model", WAKE_MODEL, "--framework", FRAMEWORK],
        timeout=180.0,
    )
    logger(f"Wake word selftest: {detail}")
    return ok, detail


class WakeWordDetector:
    """
    Keeps the wake model in a dedicated CHILD PROCESS. The mic thread calls
    feed() with raw int16 frames; a writer thread pipes them to the engine; a
    reader thread takes the engine's events and invokes on_detect() (called from
    that reader thread — the callback must marshal to whatever loop/UI it needs,
    exactly as it did when the model ran in a thread here).

    If the engine dies, the app does not: on_down() fires, ready goes False, and
    start() can be called again to retry.
    """

    def __init__(self, on_detect: Callable[[], None],
                 threshold: float = DEFAULT_THRESHOLD,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None,
                 on_down: Callable[[str], None] | None = None):
        self._on_detect = on_detect
        self._threshold = float(threshold)
        self._logger = logger
        # See PluginRegistry: `logger` is the console and gets everything,
        # `notify` is the activity log and gets only what the user must act on.
        self._notify = notify or (lambda _msg: None)
        # Called when the engine goes away under us (crash, hang, kill). The app
        # uses it to reopen the microphone: with the gate still closed and no
        # engine behind it, JARVIS would be deaf until someone found the button.
        self._on_down = on_down

        self._send_q: "queue.Queue[tuple[int, object]]" = queue.Queue(maxsize=_SEND_QUEUE)
        self._proc: subprocess.Popen | None = None
        self._read_thread: threading.Thread | None = None
        self._io_thread: threading.Thread | None = None

        # Run generation: every start() bumps it, and the pipe threads carry the
        # one they were spawned for. A thread left over from a previous run (its
        # join timed out, or stop() and start() overlapped) then cannot report
        # the death of a child it no longer belongs to — which would otherwise
        # mark a freshly started engine as crashed.
        self._gen = 0
        # Held for the whole of a start() or a stop(), so the two can never
        # overlap: the ⚙ button runs on the Qt thread while the reconnect path
        # runs on the event loop, and an interleaved start/stop would leave two
        # engine processes with threads watching the wrong one.
        self._life_lock = threading.Lock()
        self._lock = threading.RLock()          # start/stop state transitions
        self._write_lock = threading.Lock()     # one frame at a time on the pipe
        self._gone_once = threading.Lock()      # _on_child_gone runs exactly once

        self._running = False
        self._stopping = False
        self._child_ready = False               # engine said READY
        self._ready = False
        self._ready_evt = threading.Event()
        self._gone_handled = False

        self._last_msg = 0.0                    # monotonic time of any child message
        self._started_at = 0.0                  # when the current engine was launched
        self._last_detect = 0.0
        self._crash_times: list[float] = []
        # Set when WE decide to kill the engine (a wedge), so the report says
        # "stopped responding" instead of the misleading "killed by SIGTERM".
        self._pending_reason: str | None = None
        self.child_pid: int | None = None
        #: Exit status of the most recent engine process — 0 after a clean stop,
        #: a signal/NTSTATUS when it died. Diagnostics, and how the tests tell a
        #: polite shutdown from a killed one.
        self.last_exit_code: int | None = None
        self.engine_info: dict = {}
        #: Set when the engine cannot run; cleared by a successful start().
        self.unavailable_reason: str | None = None
        self.stats = {"frames_sent": 0, "frames_dropped": 0, "detections": 0,
                      "engine_deaths": 0, "starts": 0}
        try:
            _LIVE.add(self)
        except Exception:
            pass

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Launch the engine process. Returns True when it is up and running.

        Non-blocking by design: loading onnxruntime + the model takes 1–3 s in
        the child, and this is called from the Qt thread and from the asyncio
        loop, where the old in-process import froze the UI. `ready` flips True a
        moment later, when the child reports that the model is loaded; use
        wait_ready() if a caller genuinely needs to know. Safe to call again
        (no-op while running, retry after a crash). Never raises.
        """
        with self._life_lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        """The body of start(), with no other lifecycle change in flight."""
        with self._lock:
            if self._running:
                return True
            self.unavailable_reason = None
            if _frozen():
                self.unavailable_reason = "packaged build: no interpreter for the engine process"
                self._logger(f"Wake word: {self.unavailable_reason}.")
                return False
            if not is_ready():
                self.unavailable_reason = "openwakeword or its models are not installed"
                self._logger("Wake word: models are not on disk — use the DOWNLOAD button first.")
                return False
            if _numpy() is None:
                # feed() converts mic blocks to PCM with numpy; without it the
                # engine would run and hear nothing, which is worse than saying so.
                self.unavailable_reason = "numpy is not importable in the app process"
                self._logger(f"Wake word: {self.unavailable_reason} — cannot forward audio.")
                self._notify("Wake word unavailable — use the WAKE NOW button.")
                return False
            if self._crash_guard_tripped():
                self.unavailable_reason = (f"the engine crashed {len(self._crash_times)}× in "
                                           f"the last {int(_CRASH_WINDOW // 60)} minutes")
                self._logger(f"Wake word: not retrying — {self.unavailable_reason}.")
                self._notify("Wake word is disabled after repeated engine crashes — "
                             "run `python check_wake_word.py` and try "
                             "`pip install --force-reinstall onnxruntime`.")
                return False
            # Bumped before the spawn: a thread left over from the previous run
            # must not be able to write to the new child's pipe in the window
            # between Popen() returning and the state below being set.
            self._gen += 1
            try:
                if not self._spawn():
                    return False
            except Exception as e:
                self.unavailable_reason = f"could not start the engine process: {e}"
                self._logger(f"Wake word: {self.unavailable_reason}")
                self._notify("Wake word unavailable — use the WAKE NOW button.")
                return False

            self._running = True
            self._stopping = False
            self._gone_handled = False
            self._pending_reason = None
            self._ready_evt.clear()
            self._last_msg = time.monotonic()
            self._started_at = self._last_msg
            self.stats["starts"] += 1
            self._drain_send_queue()
            self._read_thread = threading.Thread(target=self._read_loop, daemon=True,
                                                 name="WakeEngineRead", args=(self._gen,))
            self._io_thread = threading.Thread(target=self._io_loop, daemon=True,
                                               name="WakeEngineIO", args=(self._gen,))
            self._read_thread.start()
            self._io_thread.start()
            self._logger(f"Wake word: starting the engine in an isolated process "
                         f"(pid {self.child_pid}) — loading the model…")

        # Short grace period OUTSIDE the lock: catch an engine that dies on
        # launch, so start() can report False instead of True-then-broken.
        self._ready_evt.wait(_START_GRACE)
        with self._lock:
            if not self._running:
                return False
            if not self._child_ready:
                # Still loading. That is normal, and audio arriving before READY
                # is dropped rather than queued — a "Hey Jarvis" said two seconds
                # ago must not wake JARVIS now.
                self._logger("Wake word: engine is loading; audio until then is ignored.")
            return True

    def stop(self) -> None:
        """Shut the engine process down and free everything. Idempotent, never
        raises, safe from any thread (including the detector's own)."""
        with self._life_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        with self._lock:
            proc = self._proc
            was_running = self._running
            self._running = False
            self._stopping = True
            self._ready = False
            self._child_ready = False
            # _proc deliberately stays set for now: the polite STOP below is
            # written through it, and it is cleared once the child is really gone
            # (and only if nobody has started a newer one in the meantime).
        if not was_running and proc is None:
            with self._lock:
                self._proc = None
            return
        # Ask first: a clean stop lets the child flush its buffers and say bye,
        # which is what keeps the console output readable and the exit code 0.
        try:
            self._write(proto.json_frame({"cmd": proto.CMD_STOP}))
        except Exception:
            pass
        if proc is not None:
            for _ in range(12):                       # up to ~0.6 s
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=1.5)
            except Exception:
                pass
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass
        try:
            self.last_exit_code = proc.poll() if proc is not None else None
        except Exception:
            pass
        with self._lock:
            if self._proc is proc:
                self._proc = None
            self._stopping = False
        self._teardown(proc)
        if was_running:
            self._logger(f"Wake word: engine stopped (exit {self.last_exit_code}).")

    @property
    def ready(self) -> bool:
        """True only while the engine process is up AND has reported the model
        loaded — i.e. a 'Hey Jarvis' right now would actually be heard."""
        return self._ready

    @property
    def running(self) -> bool:
        """True while the engine process is supposed to be alive (it may still be
        loading the model — see `ready`)."""
        return self._running

    def wait_ready(self, timeout: float = 15.0) -> bool:
        """Block until the engine reports READY (or dies, or times out). Not used
        by the app — start() must never block the UI — but tests and diagnostics
        need a synchronous answer."""
        return bool(self._ready_evt.wait(timeout) and self._ready)

    def status(self) -> dict:
        """Snapshot for logs and diagnostics. Cheap and side-effect free."""
        with self._lock:
            proc = self._proc
            code = None
            if proc is not None:
                try:
                    code = proc.poll()
                except Exception:
                    pass
            return {"running": self._running, "ready": self._ready,
                    "pid": self.child_pid, "exit_code": code,
                    "last_exit_code": self.last_exit_code,
                    "engine": dict(self.engine_info),
                    "unavailable": self.unavailable_reason,
                    "crashes": len(self._crash_times),
                    **{k: v for k, v in self.stats.items()}}

    # ── audio in ────────────────────────────────────────────────────────────

    def feed(self, frame_int16) -> None:
        """Called from the mic callback (real-time thread). Must stay cheap and
        never block — the frame is copied into bytes and dropped if the writer
        thread is backed up. Dropping is correct: a wake word is live audio or it
        is nothing, and the alternative (blocking) stalls PortAudio itself."""
        if not self._running or not self._child_ready:
            return
        np = _numpy()
        if np is None:
            return
        try:
            arr = frame_int16
            if getattr(arr, "ndim", 1) > 1:
                arr = arr[:, 0]              # sounddevice hands over (n, 1)
            # tobytes() is the copy that matters: the buffer behind `arr` belongs
            # to PortAudio and is only valid during the callback.
            pcm = np.ascontiguousarray(arr, dtype=np.int16).tobytes()
            if not pcm:
                return
            self._send_q.put_nowait((proto.TAG_AUDIO, pcm))
            self.stats["frames_sent"] += 1
        except queue.Full:
            self.stats["frames_dropped"] += 1
        except Exception:
            pass

    # ── child process plumbing ──────────────────────────────────────────────

    def _spawn(self) -> bool:
        """Start the engine subprocess. Caller holds self._lock."""
        cmd = [sys.executable, "-X", "utf8", str(_WORKER),
               "--mode", "serve",
               "--model", WAKE_MODEL,
               "--framework", FRAMEWORK,
               "--threshold", repr(self._threshold),
               "--sample-rate", str(SAMPLE_RATE),
               "--parent-pid", str(os.getpid()),
               "--orphan-timeout", repr(_ORPHAN_TIMEOUT)]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # stdin/stdout are the protocol pipes. stderr is deliberately INHERITED:
        # when the engine dies natively, its faulthandler stack lands in the same
        # console the user is already watching, which is how this bug was
        # diagnosed in the first place. CREATE_NO_WINDOW is applied globally by
        # main.py's Popen patch, so no console window flashes on Windows.
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, bufsize=0, cwd=str(_BASE_DIR), env=env)
        self._proc = proc
        self.child_pid = proc.pid
        return True

    def _write(self, frame: bytes) -> bool:
        """One frame to the child's stdin. False means the pipe is gone."""
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                return False
            try:
                if proc.stdin.closed:
                    return False
                proto.write_all(proc.stdin, frame)
                return True
            except Exception as e:
                self._logger(f"Wake word: lost the engine pipe ({type(e).__name__}: {e}).")
                return False

    def _io_loop(self, gen: int) -> None:
        """Writer thread + watchdog in one loop.

        Audio frames go out through a queue rather than being written in feed()
        because a pipe write can block (the child is mid-predict, the kernel
        buffer is full) and feed() runs on the real-time audio callback, where
        blocking is not allowed. The same loop sends the keep-alive ping and
        notices an engine that stopped answering.
        """
        next_ping = time.monotonic() + _PING_EVERY
        while self._running and gen == self._gen:
            now = time.monotonic()
            timeout = min(1.0, max(0.02, next_ping - now))
            try:
                item = self._send_q.get(timeout=timeout)
            except queue.Empty:
                item = None
            except Exception:
                break
            if item is not None:
                tag, body = item
                frame = (proto.audio_frame(body) if tag == proto.TAG_AUDIO
                         else proto.json_frame(body))
                if not self._write(frame):
                    break
            now = time.monotonic()
            if now >= next_ping:
                next_ping = now + _PING_EVERY
                if not self._write(proto.json_frame({"cmd": proto.CMD_PING})):
                    break
                if not self._child_ready and (now - self._started_at) > _READY_TIMEOUT:
                    self._pending_reason = (f"the engine did not report ready within "
                                            f"{int(_READY_TIMEOUT)}s of starting")
                    self._logger(f"Wake word: {self._pending_reason} — shutting it down.")
                    self._kill_child()
                    break
                # A child that is alive but has said nothing for six missed
                # heartbeats is wedged (deadlocked in a native call, or blocked
                # on something it should not be). Treat it like a crash: the
                # alternative is a wake word that silently never fires again.
                if self._child_ready and (now - self._last_msg) > _HEALTH_TIMEOUT:
                    self._pending_reason = (f"the engine stopped responding "
                                            f"({now - self._last_msg:.0f}s without a heartbeat)")
                    self._logger(f"Wake word: {self._pending_reason} — shutting it down.")
                    self._kill_child()
                    break
            try:
                if self._proc is not None and self._proc.poll() is not None:
                    break                       # child already exited; reader sees EOF
            except Exception:
                break
        self._on_child_gone(gen)

    def _read_loop(self, gen: int) -> None:
        """Reader thread: decode the child's events until its stdout closes."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        scanner = proto.FrameScanner()
        stray_reported = False
        while gen == self._gen:
            try:
                chunk = stream.read(65536)     # raw pipe: returns what arrived
            except Exception:
                break                          # closed from under us (stop) or broken
            if not chunk:
                break                          # EOF: the engine process is gone
            self._last_msg = time.monotonic()
            for payload in scanner.feed(chunk):
                tag, body = proto.split(payload)
                if tag != proto.TAG_JSON:
                    continue
                msg = proto.decode_json(body)
                if msg:
                    try:
                        self._handle_msg(msg)
                    except Exception as e:
                        self._logger(f"Wake word: bad engine event — {e}")
            if scanner.stray and not stray_reported:
                stray_reported = True
                # A library in the child printed to stdout. The protocol survives
                # it by design; mentioning it once explains the odd console line.
                self._logger("Wake word: ignored "
                             f"{len(scanner.stray)} bytes of non-protocol output from the engine.")
        self._on_child_gone(gen)

    def _handle_msg(self, msg: dict) -> None:
        t = msg.get("t")
        if t == proto.T_READY:
            if not self._running:
                return          # a stop() raced the engine's first message
            self._child_ready = True
            self._ready = True
            self.engine_info = {k: msg.get(k) for k in
                                ("pid", "model", "framework", "models", "versions", "seconds")
                                if msg.get(k) is not None}
            v = msg.get("versions") or {}
            models = ", ".join(msg.get("models") or []) or msg.get("model", WAKE_MODEL)
            secs = msg.get("seconds") or {}
            self._logger(f"Wake word: engine ready in child pid {msg.get('pid')} "
                         f"[{models}] — openwakeword {v.get('openwakeword', '?')}, "
                         f"onnxruntime {v.get('onnxruntime', '?')}, numpy {v.get('numpy', '?')} "
                         f"(import {secs.get('import', '?')}s, load {secs.get('load', '?')}s). "
                         f"Listening for 'Hey Jarvis'.")
            self._ready_evt.set()
        elif t == proto.T_DETECT:
            now = time.monotonic()
            if now - self._last_detect < _REFRACTORY:
                return
            self._last_detect = now
            self.stats["detections"] += 1
            self._logger(f"Wake word: heard 'Hey Jarvis' (score {msg.get('score')}).")
            try:
                self._on_detect()
            except Exception as e:
                self._logger(f"Wake word: on_detect error — {e}")
        elif t == proto.T_HEALTH:
            pass                                   # _last_msg already updated
        elif t == proto.T_LOG:
            self._logger(f"Wake word: [{msg.get('level', 'info')}] {msg.get('msg', '')}")
        elif t == proto.T_ERROR:
            self._logger(f"Wake word: engine error in {msg.get('stage', '?')} — {msg.get('msg', '')}")
        elif t == proto.T_BYE:
            pass
        else:
            self._logger(f"Wake word: unknown engine event {t!r}")

    def _kill_child(self) -> None:
        """Hard-stop the engine process (used for a wedged child)."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        for action in ("terminate", "kill"):
            try:
                if proc.poll() is not None:
                    return
                getattr(proc, action)()
                proc.wait(timeout=1.5)
            except Exception:
                pass

    def _on_child_gone(self, gen: int) -> None:
        """The engine process is gone (EOF, failed write, or a wedge we killed).

        Runs exactly once per start, from whichever thread noticed first, and it
        is the reason a native crash in onnxruntime costs the user a feature
        instead of the whole session.
        """
        with self._gone_once:
            if self._gone_handled or gen != self._gen:
                return
            self._gone_handled = True

        with self._lock:
            proc = self._proc
            self._proc = None
            was_running = self._running
            intentional = self._stopping
            self._running = False
            self._ready = False
            self._child_ready = False

        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=2.0)
            except Exception:
                try:
                    code = proc.poll()
                except Exception:
                    code = None

        if intentional or not was_running:
            self._teardown(proc)
            return

        # Unexpected death. Count it, describe it, tell the user, and hand the
        # decision back to the app (which reopens the mic so JARVIS is not deaf).
        self.stats["engine_deaths"] += 1
        now = time.monotonic()
        self._crash_times = [t for t in self._crash_times if now - t < _CRASH_WINDOW]
        self._crash_times.append(now)
        self.last_exit_code = code
        reason = self._pending_reason or describe_exit(code)
        self._pending_reason = None
        self.unavailable_reason = reason
        self._ready_evt.set()          # unblock anyone in wait_ready()
        self._logger(f"Wake word: the engine process (pid {self.child_pid}) died — {reason}. "
                     f"The app is unaffected; wake word is off until it is started again.")
        # Wording covers all three ways this happens — a native crash, a wedge
        # we killed, and an engine that never finished loading. The exact cause
        # is on the console line above and in the reason handed to on_down().
        self._notify("Wake word engine is down — JARVIS keeps listening normally. "
                     "Open ⚙ → WAKE WORD to try again.")
        self._teardown(proc, gen)
        if self._on_down is not None:
            try:
                self._on_down(reason)
            except Exception as e:
                self._logger(f"Wake word: on_down error — {e}")

    def _teardown(self, proc: subprocess.Popen | None = None,
                  gen: int | None = None) -> None:
        """Join the pipe threads and close the handles. Never joins the calling
        thread (the reader thread tears down after its own EOF).

        `gen` guards against a teardown that arrives late — after a newer start()
        has already replaced the threads and the child. Those belong to the new
        run, so only the dead process's own handles are closed here.
        """
        me = threading.current_thread()
        with self._lock:
            stale = gen is not None and gen != self._gen
            if stale:
                threads = []
            else:
                threads = [t for t in (self._read_thread, self._io_thread)
                           if t is not None and t is not me]
                self._read_thread = None
                self._io_thread = None
                if proc is None:
                    proc = self._proc
                if self._proc is proc:
                    self._proc = None
        for t in threads:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        # Pipes are closed only after the child is dead and the threads are out
        # of the read/write calls — closing a handle another thread is blocked on
        # is undefined behaviour on Windows. The write lock is taken with a
        # timeout for the same reason: if a writer is wedged on a full pipe the
        # handles still have to be released (the child is dead by now, so that
        # write is about to fail anyway).
        have_lock = self._write_lock.acquire(timeout=2.0)
        if proc is not None:
            for f in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
                try:
                    if f is not None and not f.closed:
                        f.close()
                except Exception:
                    pass
        if have_lock:
            self._write_lock.release()
        self._drain_send_queue()

    def _drain_send_queue(self) -> None:
        try:
            while True:
                self._send_q.get_nowait()
        except Exception:
            pass

    def _crash_guard_tripped(self) -> bool:
        now = time.monotonic()
        self._crash_times = [t for t in self._crash_times if now - t < _CRASH_WINDOW]
        return len(self._crash_times) >= _MAX_CRASHES


@atexit.register
def _shutdown_engines() -> None:
    """Best effort: no engine process should outlive the app on a normal exit.

    A killed app is covered without this — the child sees EOF on its stdin and
    exits, and the orphan watchdog catches the rest — but a clean interpreter
    shutdown should not leave a 200 MB process behind either. atexit does NOT run
    for os._exit(), which main.py's shutdown_jarvis uses, so that path stops the
    detector explicitly.
    """
    try:
        for det in list(_LIVE):
            try:
                det.stop()
            except Exception:
                pass
    except Exception:
        pass
