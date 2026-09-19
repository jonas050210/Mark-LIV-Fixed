"""Standard German audio for completed Live responses.

Native Gemini audio auto-selects pronunciation and has no language-code lock.
Use a real de-DE acoustic voice instead, then feed 24 kHz PCM to the existing
player (same output device, echo guard, interruption, visualizer and dashboard).
No transcript or player is duplicated here.
"""
import asyncio
import sys
import threading
import time

VOICE = "de-DE-ConradNeural"
SAMPLE_RATE = 24000


def _decode(data):
    import miniaudio
    decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.SIGNED16,
                              nchannels=1, sample_rate=SAMPLE_RATE)
    return decoded.samples.tobytes()


def _sapi_pcm(text, stop=None):
    if sys.platform != "win32":
        raise OSError("Offline Standard German rendering requires a Windows German speech voice")
    import pythoncom
    from win32com.client import Dispatch
    pythoncom.CoInitialize()
    try:
        voice = Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices("Language=407")
        if not tokens.Count:
            raise OSError("Install German (Germany) speech in Windows Settings")
        voice.Voice = tokens.Item(0)
        stream = Dispatch("SAPI.SpMemoryStream")
        stream.Format.Type = 26  # SAFT24kHz16BitMono
        voice.AllowAudioOutputFormatChangesOnNextSet = False
        voice.AudioOutputStream = stream
        voice.Speak(text, 17)  # SPF_ASYNC | SPF_IS_NOT_XML; text is never instructions
        deadline = time.monotonic() + 30
        while not voice.WaitUntilDone(100):
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                voice.Speak("", 3)  # purge synthesis; do not block a later response
                raise TimeoutError("German speech synthesis cancelled or timed out")
        return bytes(stream.GetData())
    finally:
        pythoncom.CoUninitialize()


async def render_pcm(text):
    """Bounded neural synthesis, then genuine de-DE SAPI fallback. Never plays audio."""
    async def neural():
        import edge_tts
        chunks = bytearray()
        async for chunk in edge_tts.Communicate(text, VOICE).stream():
            if chunk["type"] == "audio":
                chunks.extend(chunk["data"])
        if not chunks:
            raise OSError("German voice returned no audio")
        return await asyncio.to_thread(_decode, bytes(chunks))
    try:
        return await asyncio.wait_for(neural(), timeout=20)
    except Exception:
        stop = threading.Event()
        try:
            return await asyncio.to_thread(_sapi_pcm, text, stop)
        finally:
            stop.set()  # cancellation also reaches the COM worker


def cancel_pending(owner):
    """Loop-thread only. Cancel old synthesis, without cancelling a newer turn."""
    pending = getattr(owner, "_standard_voice_task", None)
    if (pending is not None and not pending.done()
            and getattr(owner, "_standard_voice_generation", None) != owner._speech_generation):
        pending.cancel()
