#!/usr/bin/env python3
"""
check_wake_isolation.py — regression tests for the wake-word engine subprocess.

WHAT IT PROVES
    1. The protocol survives garbage, split reads and corrupted frames.
    2. The app process NEVER imports openwakeword — not for readiness checks,
       not for setup, not while the engine runs.
    3. Audio in → detection out works end to end across the process boundary.
    4. A NATIVE crash inside the engine (a real SIGSEGV / access violation, the
       exact failure that used to close the whole app) costs the engine only:
       the parent survives, reports it, and can be started again.
    5. A wedged engine is detected and shut down instead of silently never
       firing again.
    6. No orphan processes: the engine dies when the app dies, both politely
       (stdin closed) and hard (app killed with no cleanup at all).
    7. install_and_download() works without importing openwakeword in-process,
       and reports failures honestly.

HOW
    There is no real openwakeword here and nothing is downloaded: the script
    builds a stub package that mimics the real import graph (openwakeword →
    vad → native runtime) and can be told, through environment variables, to
    crash natively, hang, or fail in Python. The stub lives in a temp directory
    that is put on PYTHONPATH, so both the test process and the engine child see
    it as `openwakeword`. Requires numpy (which the app requires anyway);
    no network, no openwakeword, no onnxruntime.

Run:  python check_wake_isolation.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Legacy Windows consoles cannot encode the symbols below — the same trap
# main.py defuses at startup, and the reason a test report must never be the
# thing that crashes.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import wake_proto as proto          # noqa: E402
from core import wake_word                    # noqa: E402

# ── reporting ───────────────────────────────────────────────────────────────

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)), flush=True)


# ── the stub openwakeword package ───────────────────────────────────────────
#
# Environment variables steer it (they reach the engine child because the
# detector copies os.environ when it spawns):
#   OWW_STUB_CRASH=<stage>       hard segfault during import|vad|load|download
#   OWW_STUB_LOAD_ERROR=1        Model() raises (catchable failure)
#   OWW_STUB_DL_FAIL=1           download_models() raises
#   OWW_STUB_DETECT_AFTER=<n>    the n-th predict() returns a detection score
#   OWW_STUB_HANG=1              predict() never returns (wedged engine)
#   OWW_STUB_PREDICT_ERROR=1     predict() raises (engine must keep running)

STUB_INIT = '''
"""Stub of the real openwakeword package — see check_wake_isolation.py.

Mirrors the import chain that faulted on the user's machine:
openwakeword/__init__.py imports .vad, which loads the native runtime.
"""
import os

__version__ = "0.6.0-stub"
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources", "models")

MODELS = {"hey_jarvis": {"download_url": "https://example.invalid/hey_jarvis.tflite"}}
FEATURE_MODELS = {"melspectrogram": {"download_url": "https://example.invalid/melspectrogram.tflite"}}
VAD_MODELS = {"silero_vad": {"download_url": "https://example.invalid/silero_vad.tflite"}}


def hard_crash():
    """A genuine native fault: no traceback, no exception, nothing catchable."""
    try:
        import ctypes
        ctypes.string_at(0)              # dereference NULL → SIGSEGV / 0xC0000005
    except Exception:
        pass
    try:
        import signal
        os.kill(os.getpid(), signal.SIGSEGV)
    except Exception:
        os._exit(127)


def maybe_crash(stage):
    if os.environ.get("OWW_STUB_CRASH", "") == stage:
        hard_crash()


maybe_crash("import")
from . import vad            # noqa: E402,F401  (the real package does this too)
'''

STUB_VAD = '''
"""Stub of openwakeword/vad.py — in the real package line 48 imports the native
inference runtime, which is where the access violation happened."""
import os
from . import maybe_crash

maybe_crash("vad")
'''

STUB_MODEL = '''
"""Stub of openwakeword/model.py with the parts the worker actually uses."""
import os
import time

import numpy as np

from . import maybe_crash, MODELS_DIR


class Model:
    def __init__(self, wakeword_models=None, inference_framework="tflite", **kw):
        maybe_crash("load")
        delay = float(os.environ.get("OWW_STUB_LOAD_DELAY", "0") or "0")
        if delay:
            time.sleep(delay)          # a real model load takes 1-3 s
        if os.environ.get("OWW_STUB_LOAD_ERROR"):
            raise ValueError("stub: could not find pretrained model")
        self.names = list(wakeword_models or ["hey_jarvis"])
        missing = [n for n in self.names
                   if not any(f.startswith(n) for f in os.listdir(MODELS_DIR))]
        if missing:
            raise ValueError(f"stub: no model files for {missing}")
        self.framework = inference_framework
        self.models = {f"{n}_v0.1": object() for n in self.names}
        self.calls = 0
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def predict(self, x, **kw):
        if not isinstance(x, np.ndarray):
            raise ValueError("stub: input must be a numpy array")
        if os.environ.get("OWW_STUB_HANG"):
            while True:
                time.sleep(3600)
        if os.environ.get("OWW_STUB_PREDICT_ERROR"):
            raise RuntimeError("stub: inference blew up")
        self.calls += 1
        after = int(os.environ.get("OWW_STUB_DETECT_AFTER", "0") or "0")
        score = 0.97 if (after and self.calls >= after) else 0.01
        return {name: score for name in self.models}
'''

STUB_UTILS = '''
"""Stub of openwakeword/utils.py — only download_models() matters here."""
import os

from . import maybe_crash, MODELS_DIR

FILES = ["melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis.onnx",
         "silero_vad.onnx", "hey_jarvis.tflite"]


def download_models(model_names=None, target_directory=None):
    maybe_crash("download")
    if os.environ.get("OWW_STUB_DL_FAIL"):
        raise RuntimeError("stub: network unreachable")
    if model_names is not None and not isinstance(model_names, list):
        raise ValueError("The model_names argument must be a list of strings")
    d = target_directory or MODELS_DIR
    os.makedirs(d, exist_ok=True)
    for name in FILES:
        with open(os.path.join(d, name), "wb") as f:
            f.write(b"stub-model-bytes" * 64)
'''


def build_stub(root: Path) -> Path:
    """Write the stub package (with its model files already present, so the
    import-free is_ready() file check says "downloaded" like a real install)."""
    pkg = root / "openwakeword"
    models = pkg / "resources" / "models"
    models.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(STUB_INIT, encoding="utf-8")
    (pkg / "vad.py").write_text(STUB_VAD, encoding="utf-8")
    (pkg / "model.py").write_text(STUB_MODEL, encoding="utf-8")
    (pkg / "utils.py").write_text(STUB_UTILS, encoding="utf-8")
    for name in ("melspectrogram.onnx", "embedding_model.onnx", "hey_jarvis.onnx"):
        (models / name).write_bytes(b"stub-model-bytes" * 64)
    return pkg


# ── helpers ─────────────────────────────────────────────────────────────────

def pid_alive(pid: int | None) -> bool:
    """Cross-platform 'is this process still there' without psutil."""
    if not pid:
        return False
    if os.name == "nt":                            # pragma: no cover (Windows)
        import ctypes
        QUERY_LIMITED = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(QUERY_LIMITED, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def wait_for(predicate, timeout: float, poll: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(poll)
    return bool(predicate())


class Recorder:
    """Collects what the detector reports, from its own threads."""

    def __init__(self) -> None:
        self.detects = 0
        self.down: list[str] = []
        self.logs: list[str] = []
        self.notes: list[str] = []
        self._lock = threading.Lock()

    def on_detect(self) -> None:
        with self._lock:
            self.detects += 1

    def on_down(self, reason: str) -> None:
        with self._lock:
            self.down.append(reason)

    def logger(self, msg: str) -> None:
        with self._lock:
            self.logs.append(msg)

    def notify(self, msg: str) -> None:
        with self._lock:
            self.notes.append(msg)

    def text(self) -> str:
        with self._lock:
            return " | ".join(self.logs + self.notes)


def make_detector(rec: Recorder, **kw) -> "wake_word.WakeWordDetector":
    return wake_word.WakeWordDetector(on_detect=rec.on_detect,
                                      logger=rec.logger, notify=rec.notify,
                                      on_down=rec.on_down, **kw)


def clear_stub_env() -> None:
    for k in list(os.environ):
        if k.startswith("OWW_STUB_"):
            del os.environ[k]


def fake_audio(blocks: int = 8, size: int = 1024, two_d: bool = False):
    """int16 blocks shaped like sounddevice delivers them."""
    import numpy as np
    out = []
    for i in range(blocks):
        a = (np.sin(np.arange(size) * 0.05) * 3000).astype(np.int16)
        out.append(a.reshape(-1, 1) if two_d else a)
    return out


# ── tests ───────────────────────────────────────────────────────────────────

def test_protocol() -> None:
    section("protocol (core/wake_proto.py)")

    payload = bytes((proto.TAG_JSON,)) + b'{"t":"ready"}'
    frames = proto.FrameScanner().feed(proto.encode(payload))
    check("one frame round-trips", frames == [payload])

    raw = proto.audio_frame(b"\x01\x02" * 512)
    # A frame cut in half across two pipe reads, as a real pipe delivers it.
    got = []
    sc = proto.FrameScanner()
    for i in range(0, len(raw), 7):
        got += sc.feed(raw[i:i + 7])
    check("frame split across reads", len(got) == 1 and got[0][0] == proto.TAG_AUDIO,
          f"{len(raw)} bytes in {len(raw) // 7 + 1} reads")

    noisy = (b"[onnxruntime:W] some warning\r\n" + proto.encode(payload)
             + b"tqdm: 100%|####|\r\n" + proto.json_frame({"t": "health"}))
    sc = proto.FrameScanner()
    got = sc.feed(noisy)
    check("library noise on stdout is skipped", len(got) == 2,
          f"{len(sc.stray)} bytes kept as stray")

    corrupt = bytearray(proto.encode(payload))
    corrupt[-1] ^= 0xFF                            # flip the checksum
    corrupt += proto.encode(payload)
    sc = proto.FrameScanner()
    got = sc.feed(bytes(corrupt))
    check("corrupt frame resyncs on the next one", len(got) == 1)

    sc = proto.FrameScanner()
    got = sc.feed(proto.SENTINEL + b"\xff\xff\xff\xff" + b"x" * 100)
    check("absurd length is rejected, not buffered", got == [] and sc.pending() < 200,
          f"pending={sc.pending()}")

    class ShortWriter:                # a stream that only accepts 3 bytes a time
        def __init__(self): self.buf = bytearray()
        def write(self, data):
            data = bytes(data)[:3]
            self.buf += data
            return len(data)

    w = ShortWriter()
    proto.write_all(w, b"A" * 40)
    check("write_all survives partial pipe writes", len(w.buf) == 40)

    class DeadWriter:
        closed = False
        def write(self, data): return 0

    try:
        proto.write_all(DeadWriter(), b"x")
        ok = False
    except OSError:
        ok = True
    check("write_all raises when the pipe is gone", ok)


def test_readiness_is_import_free() -> None:
    section("readiness checks never import the engine")

    check("is_installed() sees the stub", wake_word.is_installed())
    check("is_ready() sees the model files", wake_word.is_ready())
    banned = [m for m in ("openwakeword", "onnxruntime", "tflite_runtime")
              if m in sys.modules or any(k.startswith(m + ".") for k in sys.modules)]
    check("app process imported nothing native", not banned, f"loaded: {banned}")


def test_happy_path() -> None:
    section("engine lifecycle: start → detect → stop")

    clear_stub_env()
    os.environ["OWW_STUB_DETECT_AFTER"] = "3"
    # The engine takes 2 s to load its model here (1-3 s on a real machine).
    # start() is called on the Qt thread and on the asyncio loop, so it must
    # return long before that — blocking there is what made the settings drawer
    # look broken in the first place.
    os.environ["OWW_STUB_LOAD_DELAY"] = "2.0"
    rec = Recorder()
    det = make_detector(rec)
    t0 = time.monotonic()
    started = det.start()
    took = time.monotonic() - t0
    check("start() launches the engine", started, det.unavailable_reason or "")
    check("start() does not block on the 2 s model load",
          started and took < 1.0 and not det.ready, f"returned after {took:.2f}s")
    del os.environ["OWW_STUB_LOAD_DELAY"]
    ready = det.wait_ready(25.0)
    check("engine reports READY", ready, json.dumps(det.engine_info)[:160])
    check("child pid recorded", pid_alive(det.child_pid), f"pid={det.child_pid}")
    if not ready:
        det.stop()
        return

    for frame in fake_audio(blocks=10):
        det.feed(frame)
    check("detection crosses the process boundary",
          wait_for(lambda: rec.detects >= 1, 10.0), f"detects={rec.detects}")

    for frame in fake_audio(blocks=40, two_d=True):
        det.feed(frame)                              # (n,1) blocks, like sounddevice
    time.sleep(0.6)
    check("refractory period prevents a double wake", rec.detects <= 3,
          f"detects={rec.detects}")
    check("2-D mic blocks are accepted", det.stats["frames_sent"] > 10,
          f"sent={det.stats['frames_sent']}")
    check("no frames dropped on a healthy engine", det.stats["frames_dropped"] == 0,
          f"dropped={det.stats['frames_dropped']}")

    st = det.status()
    check("status() reports a running engine", st["running"] and st["ready"],
          f"detections={st['detections']}")

    pid = det.child_pid
    det.stop()
    check("stop() clears the state", not det.running and not det.ready)
    check("a polite stop is a clean exit, not a kill", det.last_exit_code == 0,
          f"exit={det.last_exit_code} (0 means the child got the STOP command)")
    check("engine process is really gone",
          wait_for(lambda: not pid_alive(pid), 10.0), f"pid={pid}")

    # Restarting after a clean stop must work — this is the ⚙ toggle path.
    rec2 = Recorder()
    det2 = make_detector(rec2)
    check("restart after stop works", det2.start() and det2.wait_ready(25.0))
    det2.stop()
    clear_stub_env()


def test_native_crash() -> None:
    section("native crash inside the engine (the reported bug)")

    clear_stub_env()
    os.environ["OWW_STUB_CRASH"] = "import"        # fault while loading the runtime
    rec = Recorder()
    det = make_detector(rec)
    det.start()
    died = wait_for(lambda: bool(rec.down), 25.0)
    check("parent survives the engine's native crash", died,
          rec.down[0] if rec.down else "no on_down callback")
    check("app process is still alive and importable", True,
          f"pid={os.getpid()}")
    check("detector marks itself unavailable", not det.ready and not det.running,
          str(det.unavailable_reason))
    check("user is told through notify()", len(rec.notes) >= 1,
          (rec.notes[0] if rec.notes else "")[:120])
    check("death is counted", det.stats["engine_deaths"] == 1,
          f"deaths={det.stats['engine_deaths']}")
    st = det.status()
    check("exit status decoded for humans", bool(st["unavailable"]),
          str(st["unavailable"]))
    det.stop()

    banned = [m for m in ("openwakeword", "onnxruntime") if m in sys.modules]
    check("crash did not pull the engine into the app", not banned, f"loaded: {banned}")

    # And the UI button must be able to try again.
    clear_stub_env()
    rec2 = Recorder()
    det2 = make_detector(rec2)
    ok = det2.start() and det2.wait_ready(25.0)
    check("retry after a crash works", ok, det2.unavailable_reason or "")
    det2.stop()


def test_crash_guard() -> None:
    section("crash loop is stopped, not repeated forever")

    clear_stub_env()
    os.environ["OWW_STUB_CRASH"] = "load"
    rec = Recorder()
    det = make_detector(rec)
    # start() itself returns False when the engine dies inside its short grace
    # period (a fast crash is reported synchronously), so the loop counts deaths
    # rather than start() results.
    for _ in range(wake_word._MAX_CRASHES + 2):
        before = len(rec.down)
        det.start()
        if len(rec.down) == before:
            wait_for(lambda: len(rec.down) > before, 20.0)
        det.stop()
        if det.stats["engine_deaths"] >= wake_word._MAX_CRASHES:
            break
    check(f"{wake_word._MAX_CRASHES} crashes were recorded",
          det.stats["engine_deaths"] >= wake_word._MAX_CRASHES,
          f"deaths={det.stats['engine_deaths']}")
    refused = not det.start()
    check(f"start() refuses after {wake_word._MAX_CRASHES} crashes", refused,
          str(det.unavailable_reason))
    check("refusal is explained to the user",
          any("repeated engine crashes" in n for n in rec.notes),
          (rec.notes[-1] if rec.notes else "")[:140])
    det.stop()
    clear_stub_env()


def test_python_error_paths() -> None:
    section("catchable engine failures are reported, not swallowed")

    clear_stub_env()
    os.environ["OWW_STUB_LOAD_ERROR"] = "1"
    rec = Recorder()
    det = make_detector(rec)
    det.start()
    died = wait_for(lambda: bool(rec.down), 20.0)
    check("a Python error in the child reaches the parent", died,
          rec.down[0] if rec.down else "nothing reported")
    check("the error text is in the log", "stub" in rec.text().lower(),
          rec.text()[:160])
    det.stop()

    clear_stub_env()
    os.environ["OWW_STUB_PREDICT_ERROR"] = "1"
    rec2 = Recorder()
    det2 = make_detector(rec2)
    det2.start()
    det2.wait_ready(20.0)
    for f in fake_audio(blocks=4):
        det2.feed(f)
    survived = wait_for(lambda: "predict failed" in rec2.text(), 15.0)
    check("a failing predict() does not kill the engine",
          survived and det2.ready, rec2.text()[:140])
    det2.stop()
    clear_stub_env()


def test_wedged_engine() -> None:
    section("wedged engine (alive but not answering)")

    clear_stub_env()
    os.environ["OWW_STUB_HANG"] = "1"
    # Shorten the watchdog so the test does not take half a minute. The real
    # values are 5 s pings / 30 s timeout.
    old_ping, old_health = wake_word._PING_EVERY, wake_word._HEALTH_TIMEOUT
    wake_word._PING_EVERY, wake_word._HEALTH_TIMEOUT = 0.3, 2.0
    try:
        rec = Recorder()
        det = make_detector(rec)
        det.start()
        det.wait_ready(20.0)
        for f in fake_audio(blocks=4):
            det.feed(f)
        caught = wait_for(lambda: bool(rec.down), 25.0)
        check("a hung engine is detected and shut down", caught,
              rec.down[0] if rec.down else "still hanging")
        check("detector is not left 'ready'", not det.ready)
        pid = det.child_pid
        det.stop()
        check("the hung child is killed, not left behind",
              wait_for(lambda: not pid_alive(pid), 10.0), f"pid={pid}")
    finally:
        wake_word._PING_EVERY, wake_word._HEALTH_TIMEOUT = old_ping, old_health
        clear_stub_env()


def test_never_ready() -> None:
    section("engine that never finishes loading")

    clear_stub_env()
    os.environ["OWW_STUB_LOAD_DELAY"] = "600"      # loads forever
    old_ping, old_ready = wake_word._PING_EVERY, wake_word._READY_TIMEOUT
    wake_word._PING_EVERY, wake_word._READY_TIMEOUT = 0.3, 2.0
    try:
        rec = Recorder()
        det = make_detector(rec)
        check("start() still returns immediately", det.start())
        caught = wait_for(lambda: bool(rec.down), 25.0)
        check("a never-ready engine is given up on", caught,
              rec.down[0] if rec.down else "still waiting")
        check("the reason says it never reported ready",
              "did not report ready" in (det.unavailable_reason or ""),
              str(det.unavailable_reason))
        check("the app is told (this is the deaf-JARVIS case)", len(rec.notes) >= 1,
              (rec.notes[0] if rec.notes else "")[:120])
        pid = det.child_pid
        det.stop()
        check("the loading child is not left behind",
              wait_for(lambda: not pid_alive(pid), 10.0), f"pid={pid}")
    finally:
        wake_word._PING_EVERY, wake_word._READY_TIMEOUT = old_ping, old_ready
        clear_stub_env()


def test_audio_path() -> None:
    section("the microphone path (feed)")

    clear_stub_env()
    import numpy as np

    # White-box: no engine behind it, so nothing drains the queue. This is the
    # worst case feed() can meet — a dead or wedged child — and the real-time
    # audio callback must still return in microseconds rather than block.
    rec = Recorder()
    det = make_detector(rec)
    det._running = True
    det._child_ready = True
    block = np.zeros(1024, dtype=np.int16)
    for _ in range(wake_word._SEND_QUEUE + 10):
        det.feed(block)
    t0 = time.monotonic()
    for _ in range(200):
        det.feed(block.reshape(-1, 1))
    per_call = (time.monotonic() - t0) / 200 * 1e6
    check("feed() drops instead of blocking a full queue",
          det.stats["frames_dropped"] >= 10,
          f"dropped={det.stats['frames_dropped']}")
    check("feed() costs microseconds, not milliseconds", per_call < 500.0,
          f"{per_call:.1f} µs per 64 ms block (2-D input)")
    det._running = False
    det._child_ready = False

    # A 2-D block and a non-numpy object must both be survivable.
    try:
        det.feed(None)
        det.feed([1, 2, 3])
        ok = True
    except Exception as e:
        ok = False
        print(f"      raised: {e}")
    check("feed() never raises on junk input", ok)


def test_no_orphans() -> None:
    section("no orphan engine processes")

    clear_stub_env()
    worker = REPO / "core" / "wake_worker.py"
    env = dict(os.environ)

    # (a) the app stops feeding but keeps the pipe open → the orphan watchdog
    #     must end the child (this is the "app wedged / killed without closing
    #     handles" case).
    p = subprocess.Popen(
        [sys.executable, str(worker), "--mode", "serve", "--model", "hey_jarvis",
         "--orphan-timeout", "3", "--heartbeat", "0.3"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        bufsize=0, env=env,
    )
    time.sleep(1.5)                                  # loaded, idle, still wanted
    still_open = p.poll() is None
    rc = None
    try:
        rc = p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
    check("silent parent → engine exits by itself", still_open and rc == 3,
          f"alive while the parent was still there: {still_open}, returncode={rc}")
    for f in (p.stdin, p.stdout):
        try:
            f.close()
        except Exception:
            pass

    # (b) the app is killed hard (os._exit — no atexit, no stop(), no pipe
    #     close call): the child must still notice and leave.
    script = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from core.wake_word import WakeWordDetector\n"
        "d = WakeWordDetector(on_detect=lambda: None, logger=lambda m: None)\n"
        "d.start()\n"
        "print(d.child_pid, flush=True)\n"
        "d.wait_ready(25)\n"
        "os._exit(0)\n"          # exactly what main.py's shutdown_jarvis does
    )
    q = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, timeout=90, env=env)
    grandchild = int((q.stdout.strip().splitlines() or ["0"])[-1] or 0)
    check("nested app started its engine", grandchild > 0,
          f"pid={grandchild} stderr={q.stderr.strip()[-160:]}")
    if grandchild:
        gone = wait_for(lambda: not pid_alive(grandchild), 30.0)
        check("engine dies with a hard-killed app", gone, f"pid={grandchild}")


def test_setup_without_import() -> None:
    section("install_and_download() and selftest()")

    clear_stub_env()
    pkg = Path(wake_word._package_dir())
    models = pkg / "resources" / "models"
    backup = models.parent / "models.bak"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    shutil.move(str(models), str(backup))            # pretend nothing was downloaded
    try:
        check("is_ready() is False with no model files", not wake_word.is_ready())
        rec = Recorder()
        ok, msg = wake_word.install_and_download(logger=rec.logger, notify=rec.notify)
        check("install_and_download() succeeds via the child", ok, msg[:200])
        check("models are back on disk", wake_word.is_ready())
        check("download reported what it fetched", "downloaded" in rec.text().lower(),
              rec.text()[:160])
        banned = [m for m in ("openwakeword", "onnxruntime") if m in sys.modules]
        check("setup did not import openwakeword in-process", not banned, f"{banned}")

        os.environ["OWW_STUB_DL_FAIL"] = "1"
        rec2 = Recorder()
        ok2, msg2 = wake_word.install_and_download(logger=rec2.logger)
        check("a failing download is reported, not raised", (not ok2) and bool(msg2),
              msg2[:160])

        os.environ["OWW_STUB_CRASH"] = "download"
        rec3 = Recorder()
        ok3, msg3 = wake_word.install_and_download(logger=rec3.logger)
        check("a native crash during download cannot hurt the app",
              (not ok3) and "died" in msg3.lower(), msg3[:200])
    finally:
        clear_stub_env()
        shutil.rmtree(models, ignore_errors=True)
        shutil.move(str(backup), str(models))

    ok, detail = wake_word.selftest(logger=lambda m: None)
    check("selftest() loads and runs the model in a child", ok, detail[:200])


def test_exit_code_wording() -> None:
    section("exit status wording")
    check("windows access violation is named",
          "access violation" in wake_word.describe_exit(-1073741819),
          wake_word.describe_exit(-1073741819))
    check("posix SIGSEGV is named",
          "SIGSEGV" in wake_word.describe_exit(-11), wake_word.describe_exit(-11))
    check("clean exit is plain", wake_word.describe_exit(0) == "clean exit")


def main() -> int:
    print("=" * 72)
    print(" MARK LIV — wake-word process isolation: regression tests")
    print("=" * 72)
    print(f"python   {sys.version.split()[0]}  ({sys.executable})")
    print(f"repo     {REPO}")
    try:
        import numpy as np
        print(f"numpy    {np.__version__}")
    except Exception as e:
        print(f"\nSKIP: numpy is required to run these tests ({e}).")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="mark-wake-stub-"))
    build_stub(tmp)
    # The stub has to be importable by find_spec() here AND by the engine child,
    # which inherits this environment.
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(tmp)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
    )
    sys.path.insert(0, str(tmp))
    importlib_invalidate = getattr(__import__("importlib"), "invalidate_caches", None)
    if importlib_invalidate:
        importlib_invalidate()
    print(f"stub     {tmp / 'openwakeword'}")
    print(f"worker   {REPO / 'core' / 'wake_worker.py'}")

    t0 = time.time()
    try:
        test_protocol()
        test_readiness_is_import_free()
        test_exit_code_wording()
        test_happy_path()
        test_native_crash()
        test_python_error_paths()
        test_crash_guard()
        test_wedged_engine()
        test_never_ready()
        test_audio_path()
        test_no_orphans()
        test_setup_without_import()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("test suite ran to completion", False, f"{type(e).__name__}: {e}")
    finally:
        # Last word on the invariant everything else depends on.
        banned = [m for m in ("openwakeword", "onnxruntime", "tflite_runtime")
                  if m in sys.modules or any(k.startswith(m + ".") for k in sys.modules)]
        check("FINAL: openwakeword was never imported in this process", not banned,
              f"loaded: {banned}")
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [n for n, ok, _d in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f" {passed}/{len(RESULTS)} passed in {time.time() - t0:.1f}s")
    if failed:
        print(" FAILED:")
        for n in failed:
            print(f"   • {n}")
    else:
        print(" The wake-word engine is crash-isolated: nothing it does can")
        print(" take the app process down, and nothing it leaves behind survives it.")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
