"""
core/wake_proto.py — the wire format between JARVIS and its wake-word subprocess.

WHY THERE IS A SUBPROCESS AT ALL
    openwakeword pulls in onnxruntime, and importing that loads a stack of native
    DLLs. Inside the *running* app those DLLs meet an already-loaded
    PyQt6 / PortAudio / OpenCV / numpy stack and the process dies with a Windows
    access violation inside the DLL load itself — no Python traceback, no dialog,
    nothing to catch. `try/except` cannot help: the fault happens below the
    interpreter. (Reported: ⚙ → WAKE WORD closed the whole app; the faulthandler
    dump ended in `onnxruntime/capi/_pybind_state.py:32`, reached from
    `openwakeword/vad.py:48` ← `core/wake_word.py:169` on the Qt main thread.)
    The same three imports succeed in a *fresh* interpreter — `test_overall.py --suite wake_word`
    proves that on the user's machine — so the cure is to never load them here.
    All openwakeword work lives in `core/wake_worker.py`, a child process, and
    this module is the pipe protocol that joins the two.

WHY NOT `multiprocessing` (which is the obvious first choice)
    The "spawn" start method re-imports the parent's `__main__` module inside the
    child (`spawn._fixup_main_from_path`). For this app `__main__` *is* main.py,
    so the child would import PyQt6, sounddevice/PortAudio, OpenCV and the whole
    action registry *before* it ever got to openwakeword — i.e. it would rebuild
    the exact DLL soup we are trying to escape, and then crash the same way, just
    out of sight. A plain `subprocess.Popen([sys.executable, "core/wake_worker.py"])`
    starts from a genuinely empty interpreter and imports nothing but what the
    worker asks for. That guarantee is the whole point, so the transport is a
    hand-rolled length-prefixed pipe protocol instead of a `mp.Queue`.

WHY A SENTINEL AND A CHECKSUM, NOT JUST A LENGTH PREFIX
    The child's stdout is a pipe we own, but it is also the stdout that any C
    library inside the child can `printf()` to — onnxruntime, tflite, speex and
    tqdm all print at import or run time, and the worker cannot intercept writes
    that bypass Python. One stray byte inside a length-prefixed stream shifts
    every following frame, and the parent then reads garbage forever: the symptom
    would be "wake word silently stopped working", which is miserable to diagnose
    from a distance. So every frame starts with a 6-byte sentinel and ends with a
    1-byte checksum; anything that is not a valid frame is discarded and the
    scanner resynchronises on the next sentinel.

Wire format (both directions, identical):

    SENTINEL(6) | uint32 big-endian payload length | payload | checksum(1)

    payload[0]  is a tag: TAG_AUDIO (raw little-endian int16 PCM) or TAG_JSON
                (a UTF-8 JSON object). Audio is binary rather than base64-in-JSON
                because feed() runs on the real-time microphone callback: 64 ms
                blocks arrive 15× per second and must cost microseconds, not a
                JSON round-trip.

This module is deliberately stdlib-only and importable from both sides: the
worker imports it as a plain top-level module (it lives in the same directory),
the app imports it as `core.wake_proto`. Keeping the format in one file is why
the two cannot drift apart.
"""
from __future__ import annotations

import json
import struct

# NUL + "MWK1" + NUL. Six bytes that a length-prefixed binary stream will not
# produce by accident, and that survive being split across two reads.
SENTINEL = b"\x00MWK1\x00"
_LEN = struct.Struct(">I")                      # payload length, big endian
_HDR = len(SENTINEL) + _LEN.size                # bytes before the payload

# Payload tags — payload[0] of every frame.
TAG_AUDIO = 0x01    # parent → child: raw int16 PCM, 16 kHz mono
TAG_JSON = 0x02     # both directions: one UTF-8 JSON object

# A frame bigger than this is not ours — it is a misread length, so resync
# instead of trying to buffer 4 GB of nonsense. Audio frames are ~2 kB and the
# JSON events are a few hundred bytes.
MAX_PAYLOAD = 4 * 1024 * 1024

# How much non-frame text to keep around for a diagnostic log line. Anything
# past that is dropped: a chatty library must not grow the parent's memory.
MAX_STRAY = 2048

# ── JSON event / command vocabulary ─────────────────────────────────────────
# child → parent ("t")
T_READY = "ready"        # model loaded, engine listening: {pid, versions, models}
T_DETECT = "detect"      # wake phrase heard: {score, model}
T_HEALTH = "health"      # heartbeat: {frames, score, uptime, queue}
T_LOG = "log"            # the child wants something in the app log: {level, msg}
T_ERROR = "error"        # the child failed in a catchable way: {stage, msg}
T_BYE = "bye"            # clean shutdown

# parent → child ("cmd")
CMD_PING = "ping"              # keeps the child's orphan watchdog fed
CMD_STOP = "stop"              # shut down cleanly
CMD_THRESHOLD = "threshold"    # {"value": 0.5} — retune without a restart


def _checksum(payload: bytes) -> int:
    """One byte that catches a truncated or interleaved frame. sum() over a
    bytes object is C-speed and 2 kB of it costs nothing on the audio path."""
    return (sum(payload) + len(payload)) & 0xFF


def encode(payload: bytes) -> bytes:
    """Wrap one payload into a frame. Returns the whole frame so callers can
    hand it to a single write (a frame split across two writes could interleave
    with another thread's frame and produce garbage — see write_all)."""
    return SENTINEL + _LEN.pack(len(payload)) + payload + bytes((_checksum(payload),))


def audio_frame(pcm: bytes) -> bytes:
    """Frame for one block of raw int16 PCM (16 kHz, mono, native byte order —
    which is little-endian on every platform this app runs on)."""
    return encode(bytes((TAG_AUDIO,)) + pcm)


def json_frame(obj) -> bytes:
    """Frame for one JSON-serialisable event/command."""
    body = json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
    return encode(bytes((TAG_JSON,)) + body)


def split(payload: bytes) -> tuple[int, bytes]:
    """(tag, body) of a decoded payload."""
    if not payload:
        return -1, b""
    return payload[0], payload[1:]


def decode_json(body: bytes):
    """Parse a TAG_JSON body. Returns None instead of raising: a malformed event
    must cost one log line, never the reader thread."""
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def write_all(stream, data: bytes) -> None:
    """Write every byte of `data` to a raw (unbuffered) stream.

    Both pipes are opened with bufsize=0 so nothing is held back by a library
    buffer — which means a write can be *partial*: the default Windows anonymous
    pipe buffer is 4 kB and one audio frame is already 2 kB, so the kernel is
    allowed to take half of it. Ignoring the short write would desynchronise the
    stream, and a desynchronised stream looks exactly like a crashed child.
    Raises OSError when the pipe is gone; callers use that as "child is dead".
    """
    view = memoryview(data)
    while len(view):
        n = stream.write(view)
        if not n:
            raise OSError("pipe closed by the other end")
        view = view[n:]


class FrameScanner:
    """Incremental, self-resynchronising frame decoder.

    Feed it whatever the pipe produced (any chunk size, frames split anywhere)
    and it returns the list of complete, checksum-valid payloads. Bytes that are
    not part of a valid frame end up in `stray` — that is a library printing to
    stdout, not corruption, and the caller may log it once and move on.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stray = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self._buf += chunk
        out: list[bytes] = []
        while True:
            i = self._buf.find(SENTINEL)
            if i < 0:
                # No frame start in sight. Keep only the last few bytes — the
                # sentinel may be split across two reads — and treat the rest as
                # stray output.
                keep = len(SENTINEL) - 1
                if len(self._buf) > keep:
                    self._take_stray(len(self._buf) - keep)
                break
            if i > 0:
                self._take_stray(i)
            if len(self._buf) < _HDR:
                break                                   # header still incomplete
            (n,) = _LEN.unpack_from(self._buf, len(SENTINEL))
            if n > MAX_PAYLOAD:
                # Impossible length → this was not a real frame start after all.
                # Drop one byte and look for the next sentinel.
                self._take_stray(1)
                continue
            end = _HDR + n
            if len(self._buf) < end + 1:
                break                                   # payload still arriving
            payload = bytes(self._buf[_HDR:end])
            if self._buf[end] != _checksum(payload):
                self._take_stray(1)                     # bad frame → resync
                continue
            del self._buf[:end + 1]
            out.append(payload)
        return out

    def _take_stray(self, n: int) -> None:
        if n <= 0:
            return
        room = MAX_STRAY - len(self.stray)
        if room > 0:
            self.stray += self._buf[:min(n, room)]
        del self._buf[:n]

    def pending(self) -> int:
        """Bytes buffered but not yet part of a complete frame (diagnostics)."""
        return len(self._buf)


def frames_in(data: bytes) -> list[bytes]:
    """One-shot decode of a captured stdout blob (used for the short-lived
    `download` / `selftest` modes, where the parent waits for the process to
    finish and then parses everything it printed)."""
    return FrameScanner().feed(data)
