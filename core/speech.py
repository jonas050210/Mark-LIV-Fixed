"""Decoupled voice output: who speaks a line is decided here, not at the call site.

Engines, in priority order:

#. ``live`` — the Gemini Live session speaks the line (default; the voice the
   user hears all day). Only usable while a session is connected.
#. ``local`` — an offline OS voice (Windows SAPI / macOS ``say`` / Linux
   espeak-ng & co). No network, no account, no paid API — the fallback for
   announcements that must be heard even when Live is down (timers!), and the
   socket a future local JARVIS voice plugs into (see :class:`VoiceProvider`).
#. ``log`` — no audio at all; the line goes to the activity log only.

Callers (timers, reminders, background monitors) use :func:`announce` and
never touch an engine directly. The mode comes from the ``voice_engine``
config key: ``"auto"`` (live when connected, else local, else log),
``"live"``, ``"local"``, or ``"silent"`` (log only — mute everything spoken).

Threading: :func:`announce` is synchronous and safe from any thread. Local
playback runs in a daemon thread so a talking timer never blocks its caller;
a lock serialises overlapping announcements so two timers cannot talk over
each other.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
from typing import Callable

# Engine preference. "auto" = live → local → log.
MODES = ("auto", "live", "local", "silent")


# ── voice providers (the local engine is swappable) ──────────────────────────


class VoiceProvider:
    """Something that can speak text offline. The default implementations are
    the OS voices below; a future local JARVIS voice (Piper/Kokoro/…) plugs in
    by subclassing and calling :func:`set_local_provider` — no caller changes."""

    name = "os-default"

    def speak(self, text: str) -> None:
        """Speak `text` synchronously. Raise on failure."""
        raise NotImplementedError

    def stop(self) -> None:
        """Optional cooperative stop for providers that support interruption."""


class _SapiProvider(VoiceProvider):
    """Windows SAPI, explicit German (Germany) voice — offline, no console."""

    name = "windows-sapi"

    def __init__(self):
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def speak(self, text: str) -> None:
        # Select the actual de-DE voice token, not the user's regional OS default.
        import pythoncom
        from win32com.client import Dispatch
        self._stop.clear()
        pythoncom.CoInitialize()
        try:
            voice = Dispatch("SAPI.SpVoice")
            voices = voice.GetVoices("Language=407")
            if not voices.Count:
                raise OSError("Install a German (Germany) speech voice in Windows Settings")
            voice.Voice = voices.Item(0)
            voice.Speak(text, 17)  # SPF_ASYNC | SPF_IS_NOT_XML on this worker
            while not voice.WaitUntilDone(50):
                if self._stop.is_set():
                    voice.Speak("", 3)  # async + purge pending speech
                    break
        finally:
            pythoncom.CoUninitialize()


class _CommandProvider(VoiceProvider):
    """Speak via an external CLI (macOS `say`, Linux espeak-ng & co).

    argv-built, stdin-fed, bounded by timeout, console-free on Windows. The
    command is resolved with :func:`shutil.which` at first use so a missing
    binary fails fast with a clear error instead of a stack trace.
    """

    def __init__(self, name: str, argv: list[str], *, feed_stdin: bool = False,
                 timeout: float = 60.0) -> None:
        self.name = name
        self._argv = list(argv)
        self._feed_stdin = feed_stdin
        self._timeout = timeout
        self._resolved: str | None = None
        self._lock = threading.Lock()

    def _resolve(self) -> str:
        with self._lock:
            if self._resolved is None:
                path = shutil.which(self._argv[0])
                if not path:
                    raise OSError(f"'{self._argv[0]}' is not installed")
                self._resolved = path
            return self._resolved

    def speak(self, text: str) -> None:
        exe = self._resolve()
        argv = [exe, *self._argv[1:]]
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": self._timeout,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        if self._feed_stdin:
            kwargs["input"] = text.encode("utf-8", "replace")
        else:
            argv.append(text)
        subprocess.run(argv, check=True, **kwargs)


def default_local_provider() -> VoiceProvider:
    """Best offline voice for this OS. Never raises — worst case a provider
    that fails at speak() time with a clear error (handled by announce())."""
    if sys.platform == "win32":
        return _SapiProvider()
    if sys.platform == "darwin":
        return _CommandProvider("macos-say", ["say", "-v", "Anna"])
    # Linux: first installed of the common offline speakers.
    for name, argv, stdin in (
        ("espeak-ng", ["espeak-ng", "-v", "de"], True),
        ("espeak", ["espeak", "-v", "de"], True),
        ("spd-say", ["spd-say", "-w", "-l", "de-DE"], False),
    ):
        if shutil.which(argv[0]):
            return _CommandProvider(name, argv, feed_stdin=stdin)
    return _CommandProvider("espeak-ng", ["espeak-ng", "-v", "de"], feed_stdin=True)


# ── router ───────────────────────────────────────────────────────────────────


class SpeechRouter:
    """Decides per announcement which engine speaks it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live_is_connected: Callable[[], bool] = lambda: False
        self._live_speak: Callable[[str], None] = lambda _t: None
        self._log: Callable[[str], None] = lambda _m: None
        self._local: VoiceProvider | None = None
        self._transcript: Callable[[str], None] | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=10)
        self._worker_started = False
        self._pending_text: set[str] = set()
        self._activity = (lambda: None, lambda: None)

    # ── wiring ───────────────────────────────────────────────────────────────

    def set_live_provider(
        self,
        is_connected: Callable[[], bool],
        speak: Callable[[str], None],
    ) -> None:
        with self._lock:
            self._live_is_connected = is_connected
            self._live_speak = speak

    def set_log(self, log: Callable[[str], None]) -> None:
        with self._lock:
            self._log = log

    def set_transcript(self, publish: Callable[[str], None]) -> None:
        """Use the existing assistant transcript sink; Live commits its own final text."""
        with self._lock:
            self._transcript = publish

    def set_activity(self, on_start, on_done):
        with self._lock:
            self._activity = on_start, on_done

    def stop(self):
        """Discard queued notifications and cooperatively stop offline playback."""
        while True:
            try:
                text = self._queue.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._pending_text.discard(text)
            self._queue.task_done()
        with self._lock:
            local = self._local
        if local is not None:
            local.stop()

    def set_local_provider(self, provider: VoiceProvider) -> None:
        """Plug in a custom local voice (the future JARVIS voice socket)."""
        with self._lock:
            self._local = provider

    # ── mode ─────────────────────────────────────────────────────────────────

    def mode(self) -> str:
        try:
            from memory.config_manager import get_voice_engine

            m = str(get_voice_engine() or "auto").strip().lower()
        except Exception:
            m = "auto"
        return m if m in MODES else "auto"

    # ── announce ─────────────────────────────────────────────────────────────

    def announce(self, text: str, *, background: bool = True) -> str:
        """Speak `text` on the best available engine. Returns the engine used
        ("live" / "local:<name>" / "log", or "queued" when backgrounded). Never raises.

        With `background=True` (default) the call returns immediately and the
        line is spoken from a single daemon worker — announcements never stack
        over each other and never block the caller. `background=False` speaks
        synchronously (for shutdown paths and tests).
        """
        text = (text or "").strip()
        if not text:
            return "log"
        if background:
            self._ensure_worker()
            with self._lock:
                if text in self._pending_text:
                    return "queued"
                self._pending_text.add(text)
            try:
                self._queue.put_nowait(text)
            except queue.Full:
                # Preserve queued announcements; never drop an earlier important result.
                with self._lock:
                    self._pending_text.discard(text)
                    transcript, log = self._transcript, self._log
                try:
                    if transcript:
                        transcript(text)
                    else:
                        log(f"SAY: {text}")
                except Exception:
                    pass  # A closing UI must not break the caller's tool/action.
                return "log"
            return "queued"
        return self._speak_now(text)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker_started:
                return
            self._worker_started = True
        t = threading.Thread(target=self._worker, name="speech-router",
                             daemon=True)
        t.start()

    def _worker(self) -> None:
        while True:
            try:
                text = self._queue.get()
            except Exception:
                return
            try:
                self._speak_now(text)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._pending_text.discard(text)
                self._queue.task_done()

    def _speak_now(self, text: str) -> str:
        mode = self.mode()
        with self._lock:
            live_ok = self._live_is_connected
            live_say = self._live_speak
            log = self._log
            local = self._local
            transcript = self._transcript
            on_start, on_done = self._activity

        if mode in ("auto", "live"):
            try:
                if live_ok():
                    if live_say(text) is not False:
                        return "live"
            except Exception as e:
                if mode == "auto":
                    try:
                        log(f"SYS: Live voice failed ({e}) — trying local voice.")
                    except Exception:
                        pass
            if mode == "live":
                try:
                    if transcript is not None:
                        transcript(text)
                    else:
                        log(f"SAY: {text}")
                except Exception:
                    pass
                return "log"

        if transcript is not None:
            try:
                transcript(text)
            except Exception:
                pass

        if mode in ("auto", "local"):
            try:
                if local is None:
                    local = default_local_provider()
                    with self._lock:
                        self._local = local
                try:
                    on_start()
                    local.speak(text)
                finally:
                    on_done()
                return f"local:{local.name}"
            except Exception as e:
                try:
                    log(f"SYS: Local voice unavailable ({e}).")
                except Exception:
                    pass

        try:
            if transcript is None:
                log(f"SAY: {text}")
        except Exception:
            pass
        return "log"


_router = SpeechRouter()


def get_speech_router() -> SpeechRouter:
    return _router


def announce(text: str, *, background: bool = True) -> str:
    """Speak `text` on the best available engine. Never raises."""
    try:
        return _router.announce(text, background=background)
    except Exception:
        return "log"


def set_local_provider(provider: VoiceProvider) -> None:
    _router.set_local_provider(provider)
