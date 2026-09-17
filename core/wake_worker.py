"""
core/wake_worker.py — the only process in this project that imports openwakeword.

WHY THIS FILE EXISTS
    Importing openwakeword loads onnxruntime's native DLLs, and inside the
    running app (PyQt6 + PortAudio + OpenCV + numpy already resident) that load
    faults: `Windows fatal exception: access violation` in
    `onnxruntime/capi/_pybind_state.py:32`, killing the whole process with no
    Python traceback and no dialog — pressing ⚙ → WAKE WORD simply closed
    JARVIS. The identical imports succeed in a fresh interpreter, so the model
    runs *here*, in a child process, and the app never touches those DLLs.
    When this process dies — natively, for any reason — the parent notices the
    closed pipe, marks wake word unavailable, tells the user, and keeps running.
    A crash in here costs a feature; a crash in there costs the session.

MODES
    serve     long-lived. Reads 16 kHz int16 mono PCM frames from stdin, runs
              openwakeword's streaming model, writes `detect` / `health` /
              `log` events to stdout. This is what WakeWordDetector spawns.
    download  one-shot. Runs openwakeword.utils.download_models() so that even
              the *setup* path (pip install + model download) never imports the
              package inside the app.
    selftest  one-shot. Import + load + one predict on a second of silence, with
              timings and versions. check_wake_word.py runs this to prove that
              the isolated engine actually works on a given machine.

TRANSPORT
    Length-prefixed, sentinel-framed messages — see core/wake_proto.py, which
    both ends import. fd 1 belongs to the protocol; Python-level stdout is
    redirected to stderr so library prints cannot land in the middle of a frame.

EXIT CODES
    0 clean · 1 catchable Python failure (reported as an `error` event first) ·
    2 bad usage · 3 orphaned (the parent stopped feeding us) · anything else
    means the OS killed this process, which is how a native crash shows up.

This file must stay dependency-free apart from openwakeword itself: it is
launched as `python core/wake_worker.py`, imports nothing from the app, and so
starts from a clean interpreter with none of the app's DLLs resident.
"""
from __future__ import annotations

import os
import sys

# ── Import the protocol from this directory, then get this directory OUT of
# sys.path. Running as a script puts core/ at sys.path[0], and core/ holds
# modules named `gemini.py`, `tts.py`, `echo.py`, `types`-adjacent things —
# any of which could shadow a stdlib or site-packages module that onnxruntime
# imports later. The child's namespace is kept pristine on purpose: it is the
# one thing that makes the DLL load behave the way it does in check_wake_word.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import wake_proto as proto          # noqa: E402  (must follow the path setup)
while _HERE in sys.path:
    sys.path.remove(_HERE)

import argparse                     # noqa: E402
import faulthandler                 # noqa: E402
import json                         # noqa: E402
import queue                        # noqa: E402
import threading                    # noqa: E402
import time                         # noqa: E402

# The child buffers this many mic blocks (64 ms each at 16 kHz/1024 samples) —
# about four seconds. Enough to ride out a slow predict() without the parent's
# pipe filling up, small enough that a stalled engine cannot eat memory.
AUDIO_QUEUE = 64

# How often the heartbeat goes out (overridable with --heartbeat, which the
# isolation tests use to keep them short). The parent treats 6 missed beats as a
# hung engine, so this has to stay well under its timeout.
HEALTH_EVERY = 5.0

# Ignore a second detection for this long after one fired. openwakeword's score
# stays high for the tail of the utterance, and the app should wake once.
REFRACTORY = 1.5


def _log(msg: str) -> None:
    """Diagnostics to stderr, which the parent leaves connected to the console.
    Never to stdout: that pipe carries the protocol."""
    try:
        err = sys.stderr
        if err is None:            # pythonw with no console attached
            return
        err.write(f"[wake-worker {os.getpid()}] {msg}\n")
        err.flush()
    except Exception:
        pass


class _Channel:
    """Serialised frame writer.

    Three threads here can emit (inference, heartbeat, shutdown) and a frame
    interleaved with another frame is unrecoverable for the reader, so every
    send is one atomic write under a lock. A failed write means the parent is
    gone; that is sticky, because the alternative is a log line per frame.
    """

    def __init__(self, stream) -> None:
        self._s = stream
        self._lock = threading.Lock()
        self.dead = False

    def send(self, obj) -> bool:
        with self._lock:
            if self.dead:
                return False
            try:
                proto.write_all(self._s, proto.json_frame(obj))
                return True
            except Exception as e:
                self.dead = True
                _log(f"lost the parent pipe: {e}")
                return False


def _bootstrap_stdio():
    """Give the protocol its own handle on fd 1 and make everything else stderr.

    faulthandler comes first: the failure this process exists to absorb is a
    native crash *during the import below*, and if it happens the stack has to
    land somewhere the user can copy out of the console.
    """
    try:
        faulthandler.enable()
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        try:
            s = getattr(sys, name, None)
            if s is not None and hasattr(s, "reconfigure"):
                # Same trap main.py defuses at startup: a legacy Windows console
                # code page cannot encode the arrows and emojis in library logs.
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        # dup, not sys.stdout.buffer: we are about to rebind sys.stdout, and a
        # raw unbuffered handle of our own cannot be closed or rewrapped by
        # anything the imported libraries do to sys.stdout.
        out = os.fdopen(os.dup(1), "wb", buffering=0)
    except Exception as e:                                  # pragma: no cover
        _log(f"could not take a private handle on stdout ({e}); using sys.stdout")
        out = sys.stdout.buffer
    if sys.stderr is not None:
        sys.stdout = sys.stderr      # prints from openwakeword/tqdm go to the console
    return out


def _best_score(scores) -> tuple[float, str]:
    """Highest wake score out of openwakeword's per-model dict.

    Keys are the model names the loader was given ("hey_jarvis", or a versioned
    "hey_jarvis_v0.1" in older releases), so match on the phrase rather than on
    an exact key — and fall back to the best of whatever is there, which keeps
    this working if a different wake phrase is ever configured.
    """
    if isinstance(scores, dict):
        best, name = 0.0, ""
        for k, v in scores.items():
            try:
                fv = float(v)
            except Exception:
                continue
            if "jarvis" in str(k).lower() and fv > best:
                best, name = fv, str(k)
        if not name and scores:
            for k, v in scores.items():
                try:
                    fv = float(v)
                except Exception:
                    continue
                if fv > best:
                    best, name = fv, str(k)
        return best, name
    try:
        return float(scores), ""
    except Exception:
        return 0.0, ""


# ── mode: serve ─────────────────────────────────────────────────────────────

def _serve(args, out: _Channel) -> int:
    """Read PCM from stdin, run the model, report detections. Blocks until the
    parent closes the pipe, asks us to stop, or disappears."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            # Launched by hand in a terminal. Reading frames from a keyboard
            # would just hang, so say what this file is for and leave.
            sys.stderr.write(
                "wake_worker.py --mode serve is spawned by the app (WakeWordDetector).\n"
                "To check the engine by hand use:  python core/wake_worker.py --mode selftest\n"
            )
            return 2
    except Exception:
        pass

    try:
        import numpy as np
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "numpy",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1

    # ── The step that kills the app when it happens there: import + load. ──
    # Every failure here is reported as a JSON event first, so the parent can
    # tell "openwakeword is broken" from "the process vanished".
    t0 = time.time()
    try:
        import openwakeword                       # noqa: F401  (native DLL load)
        from openwakeword.model import Model
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "import",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    t_import = time.time() - t0
    try:
        model = Model(wakeword_models=[args.model], inference_framework=args.framework)
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "load",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    t_load = time.time() - t0 - t_import

    state = {
        "threshold": float(args.threshold),
        "frames_in": 0,
        "frames_run": 0,
        "last_score": 0.0,
        "last_detect": 0.0,
        "last_input": time.monotonic(),
    }
    stop = threading.Event()
    audio_q: "queue.Queue[bytes]" = queue.Queue(maxsize=AUDIO_QUEUE)
    started = time.time()

    out.send({
        "t": proto.T_READY,
        "pid": os.getpid(),
        "model": args.model,
        "framework": args.framework,
        "threshold": state["threshold"],
        "sample_rate": args.sample_rate,
        "models": sorted(str(k) for k in getattr(model, "models", {}) or {}),
        "seconds": {"import": round(t_import, 2), "load": round(t_load, 2)},
        "versions": _versions(),
    })
    _log(f"engine ready (import {t_import:.1f}s, load {t_load:.1f}s) — "
         f"threshold {state['threshold']}")

    def _infer() -> None:
        """The model's own thread. predict() is stateful/streaming, so exactly
        one thread may ever call it — that is also why the reader hands raw
        bytes over a queue instead of doing the work inline."""
        while not stop.is_set():
            try:
                body = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:
                break
            try:
                # A writable copy on purpose: frombuffer() gives a read-only view
                # of the frame bytes, and openwakeword has only ever been fed
                # writable arrays (the old in-process detector handed it the mic
                # callback's .copy()). 2 kB once per 64 ms block costs about a
                # microsecond — far cheaper than finding out that some version
                # writes into its input.
                frame = np.frombuffer(body, dtype=np.int16).copy()
                if frame.size == 0:
                    continue
                scores = model.predict(frame)
            except Exception as e:
                out.send({"t": proto.T_LOG, "level": "warn",
                          "msg": f"predict failed: {type(e).__name__}: {e}"})
                continue
            state["frames_run"] += 1
            score, name = _best_score(scores)
            state["last_score"] = score
            if score >= state["threshold"]:
                now = time.monotonic()
                if now - state["last_detect"] < REFRACTORY:
                    continue
                state["last_detect"] = now
                # Reset the streaming buffers so the tail of this utterance
                # cannot fire a second time, and drop whatever audio queued up
                # while we were deciding — it is the same "Hey Jarvis".
                try:
                    model.reset()
                except Exception:
                    pass
                _drain(audio_q)
                out.send({"t": proto.T_DETECT, "score": round(float(score), 4),
                          "model": name, "ts": time.time()})

    def _heartbeat() -> None:
        """Proves the engine is alive AND keeps us from outliving the parent."""
        while not stop.wait(args.heartbeat):
            out.send({"t": proto.T_HEALTH, "frames": state["frames_in"],
                      "run": state["frames_run"], "queue": audio_q.qsize(),
                      "score": round(state["last_score"], 4),
                      "uptime": round(time.time() - started, 1)})
            idle = time.monotonic() - state["last_input"]
            if idle > args.orphan_timeout:
                # No bytes at all — not even a ping — for that long means the
                # app died without closing the pipe (or is wedged). An orphaned
                # onnxruntime process is ~200 MB of the user's RAM forever, so
                # leave. os._exit: a hung parent is exactly the situation where
                # a polite shutdown could hang too.
                _log(f"no input from the parent for {idle:.0f}s — exiting")
                os._exit(3)

    infer_t = threading.Thread(target=_infer, name="WakeInfer", daemon=True)
    beat_t = threading.Thread(target=_heartbeat, name="WakeHeartbeat", daemon=True)
    infer_t.start()
    beat_t.start()

    # ── stdin reader (this thread): frames in, commands in, EOF = we're done ──
    scanner = proto.FrameScanner()
    stream = sys.stdin.buffer
    read1 = getattr(stream, "read1", None)     # BufferedReader: return what arrived
    stray_logged = False
    rc = 0
    while not stop.is_set():
        try:
            chunk = read1(65536) if read1 is not None else stream.read(65536)
        except Exception as e:
            _log(f"stdin read failed ({e}) — parent is gone")
            break
        if not chunk:
            _log("stdin closed by the parent — shutting down")
            break
        state["last_input"] = time.monotonic()
        for payload in scanner.feed(chunk):
            tag, body = proto.split(payload)
            if tag == proto.TAG_AUDIO:
                state["frames_in"] += 1
                _push_fresh(audio_q, body)
            elif tag == proto.TAG_JSON:
                msg = proto.decode_json(body)
                if not msg:
                    continue
                cmd = msg.get("cmd")
                if cmd == proto.CMD_STOP:
                    stop.set()
                elif cmd == proto.CMD_THRESHOLD:
                    try:
                        state["threshold"] = float(msg.get("value"))
                        _log(f"threshold is now {state['threshold']}")
                    except Exception:
                        pass
                # CMD_PING needs no handling: it already fed `last_input`.
        if scanner.stray and not stray_logged:
            stray_logged = True
            _log(f"ignoring {len(scanner.stray)} bytes of non-protocol input")

    stop.set()
    out.send({"t": proto.T_BYE, "frames": state["frames_in"],
              "run": state["frames_run"], "uptime": round(time.time() - started, 1)})
    infer_t.join(timeout=2.0)
    return rc


def _push_fresh(q: "queue.Queue", body: bytes) -> None:
    """Enqueue audio, dropping the OLDEST block when full.

    For a wake word, recent audio is the only audio that matters: if inference
    falls behind, the right thing is to skip forward, not to detect a "Hey
    Jarvis" the user said four seconds ago and answer a sentence that is over.
    """
    while True:
        try:
            q.put_nowait(body)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except Exception:
                return
        except Exception:
            return


def _drain(q: "queue.Queue") -> None:
    try:
        while True:
            q.get_nowait()
    except Exception:
        pass


def _versions() -> dict:
    """Everything worth knowing about the child's environment in one event. When
    wake word misbehaves on a machine we cannot see, this line in the log is the
    difference between guessing and knowing."""
    v = {"python": sys.version.split()[0], "platform": sys.platform}
    for mod in ("numpy", "onnxruntime", "openwakeword"):
        try:
            m = sys.modules.get(mod) or __import__(mod)
            v[mod] = str(getattr(m, "__version__", "?"))
        except Exception:
            v[mod] = "n/a"
    return v


# ── mode: download ──────────────────────────────────────────────────────────

def _download(args, out: _Channel) -> int:
    """Fetch the pretrained models. Runs here, not in the app, because
    `import openwakeword.utils` loads the same native DLLs as everything else."""
    try:
        import openwakeword.utils as oww_utils
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "import",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    t0 = time.time()
    try:
        try:
            oww_utils.download_models([args.model])
        except TypeError:
            oww_utils.download_models()      # older signature: no model list
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "download",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    out.send({"t": "downloaded", "model": args.model,
              "seconds": round(time.time() - t0, 1),
              "files": _model_files()})
    return 0


def _model_files() -> list:
    """Names + sizes of the model directory, for the parent's log line. Found by
    path (no openwakeword import needed) so it works even if the load failed."""
    try:
        import openwakeword
        d = os.path.join(os.path.dirname(os.path.abspath(openwakeword.__file__)),
                         "resources", "models")
        return sorted(f"{n} ({os.path.getsize(os.path.join(d, n)) // 1024} kB)"
                      for n in os.listdir(d))
    except Exception:
        return []


# ── mode: selftest ──────────────────────────────────────────────────────────

def _selftest(args, out: _Channel) -> int:
    """Prove the whole isolated path works on this machine: import, load, and
    run one prediction over a second of silence. If a native DLL conflict exists
    anywhere, it happens HERE — in a process that is allowed to die."""
    try:
        import numpy as np
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "numpy",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1

    t0 = time.time()
    try:
        import openwakeword                       # noqa: F401
        from openwakeword.model import Model
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "import",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    t_import = time.time() - t0
    try:
        model = Model(wakeword_models=[args.model], inference_framework=args.framework)
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "load",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    t_load = time.time() - t0 - t_import
    try:
        t1 = time.time()
        scores = model.predict(np.zeros(args.sample_rate, dtype=np.int16))
        t_predict = time.time() - t1
    except Exception as e:
        out.send({"t": proto.T_ERROR, "stage": "predict",
                  "msg": f"{type(e).__name__}: {e}"})
        return 1
    score, name = _best_score(scores)
    out.send({"t": "selftest", "ok": True, "model": name or args.model,
              "score": round(float(score), 4),
              "seconds": {"import": round(t_import, 2), "load": round(t_load, 2),
                          "predict": round(t_predict, 3)},
              "versions": _versions()})
    return 0


# ── entry point ─────────────────────────────────────────────────────────────

def _parse(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="wake_worker.py", add_help=True,
                                description="Isolated openwakeword engine for MARK LIV.")
    p.add_argument("--mode", choices=("serve", "download", "selftest"), default="serve")
    p.add_argument("--model", default="hey_jarvis",
                   help="pretrained openwakeword phrase to listen for")
    p.add_argument("--framework", default="onnx", choices=("onnx", "tflite"),
                   help="onnx on Windows/macOS (tflite-runtime is a Linux-only wheel)")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--sample-rate", type=int, default=16000)
    p.add_argument("--parent-pid", type=int, default=0,
                   help="informational: the app process that spawned us")
    p.add_argument("--orphan-timeout", type=float, default=60.0,
                   help="exit after this long without any input from the parent")
    p.add_argument("--heartbeat", type=float, default=HEALTH_EVERY,
                   help="seconds between health events (also the orphan check interval)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    out = _Channel(_bootstrap_stdio())
    try:
        if args.mode == "serve":
            return _serve(args, out)
        if args.mode == "download":
            return _download(args, out)
        return _selftest(args, out)
    except SystemExit:
        raise
    except BaseException as e:                    # last resort: tell the parent
        try:
            out.send({"t": proto.T_ERROR, "stage": args.mode,
                      "msg": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
        _log(f"fatal: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
