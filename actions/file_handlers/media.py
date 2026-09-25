"""Audio and video handling: transcribe, trim, convert, extract, compress."""
from __future__ import annotations

from actions.file_handlers.common import (
    Path,
    _bool_param,
    _file_size_str,
    _float_param,
    _gemini_client,
    _int_param,
    _media_time_param,
    _output_path,
    _publish_generated,
    _require_cloud_size,
    _run_command,
    _staging_output,
    _text_param,
    atomic_create_text,
    json,
    run_bounded,
    shutil,
    tempfile,
)


def _process_audio(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "transcribe"

    if action == "info":
        if shutil.which("ffprobe") is None:
            return f"Audio file: {_file_size_str(path)} (install ffmpeg for more info)"
        try:
            result = run_bounded(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                timeout=10,
                max_output=200_000,
                cancel_event=params.get("_cancel_event"),
            )
            if result.returncode != 0 or result.timed_out or result.cancelled:
                raise RuntimeError(result.stderr or "ffprobe failed")
            data = json.loads(result.stdout)
            audio_stream = next(
                (item for item in data.get("streams", []) if item.get("codec_type") == "audio"),
                {},
            )
            duration = float(data.get("format", {}).get("duration", 0) or 0)
            mins, secs = divmod(int(duration), 60)
            channels = audio_stream.get("channels", "?")
            rate = audio_stream.get("sample_rate", "?")
            return (
                f"Audio: {mins}m {secs}s, {channels} ch, {rate}Hz, "
                f"{_file_size_str(path)}"
            )
        except Exception as exc:
            return f"Info failed: {type(exc).__name__}"

    if action == "transcribe":
        try:
            _require_cloud_size(path)
            model = _gemini_client()
            content = path.read_bytes()
            mime    = {
                "mp3": "audio/mp3", "wav": "audio/wav",
                "ogg": "audio/ogg", "m4a": "audio/mp4",
                "aac": "audio/aac", "flac": "audio/flac",
            }.get(path.suffix.lstrip(".").lower(), "audio/mpeg")
            response = model.generate_content([
                "Transcribe all speech in this audio file accurately.",
                {"mime_type": mime, "data": content}
            ])
            result = response.text.strip()
            if _bool_param(params, "save", True):
                out = _output_path(path, "transcript", ".txt")
                atomic_create_text(out, result)
                return f"Transcription saved: {out.name}\n\nPreview: {result[:300]}"
            return result
        except Exception as e:
            return f"Transcription failed: {type(e).__name__}"

    if action == "convert":
        fmt = _text_param(params, "format", "mp3", 20).lower().lstrip(".")
        if fmt not in {"mp3", "wav", "ogg", "flac", "aac"}:
            return "Supported audio formats are mp3, wav, ogg, flac, and aac."
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not found. Install ffmpeg to convert audio."
        out = _output_path(path, "converted", f".{fmt}")
        staging = _staging_output(out)
        try:
            ok, detail = _run_command(
                ["ffmpeg", "-nostdin", "-n", "-i", str(path), str(staging)],
                300,
                params.get("_cancel_event"),
            )
            if not ok or not staging.is_file():
                return f"Convert failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            return f"Convert failed: {type(e).__name__}"
        finally:
            staging.unlink(missing_ok=True)

    if action == "trim":
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not found. Install ffmpeg to trim audio."
        staging = None
        try:
            start = _float_param(params, "start", 0, 0, 604_800)
            end = _float_param(params, "end", 0, 0, 604_800)
            if end and end <= start:
                return "Audio trim end must be later than start."
            out = _output_path(path, f"trim_{int(start)}s_{int(end)}s")
            staging = _staging_output(out)
            command = [
                "ffmpeg", "-nostdin", "-n", "-ss", str(start), "-i", str(path)
            ]
            if end:
                command += ["-t", str(end - start)]
            command += [str(staging)]
            ok, detail = _run_command(
                command, 300, params.get("_cancel_event")
            )
            if not ok or not staging.is_file():
                return f"Trim failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            return f"Trimmed audio ({int(start)}s–{int(end)}s). Saved: {out.name}"
        except Exception as e:
            return f"Trim failed: {type(e).__name__}"
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)

    return f"Unknown audio action: '{action}'. Try: transcribe, info, convert, trim"
def _process_video(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "info"


    def _ffmpeg_available() -> bool:
        return shutil.which("ffmpeg") is not None

    if action == "info":
        try:
            result = run_bounded(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                timeout=10,
                max_output=200_000,
                cancel_event=params.get("_cancel_event"),
            )
            if result.returncode != 0 or result.timed_out:
                raise RuntimeError(result.stderr or "ffprobe failed")
            data = json.loads(result.stdout)
            fmt      = data.get("format", {})
            duration = float(fmt.get("duration", 0))
            mins, secs = divmod(int(duration), 60)
            size     = _file_size_str(path)
            streams  = data.get("streams", [])
            video_s  = next((s for s in streams if s["codec_type"] == "video"), {})
            w        = video_s.get("width", "?")
            h        = video_s.get("height", "?")
            fps      = video_s.get("r_frame_rate", "?")
            return f"Video: {mins}m {secs}s, {w}x{h}, {fps} fps, {size}"
        except Exception:
            return f"Video file: {_file_size_str(path)}"

    if action == "extract_audio":
        if not _ffmpeg_available():
            return "ffmpeg not found. Install ffmpeg to extract audio."
        out = _output_path(path, "audio", ".mp3")
        staging = _staging_output(out)
        try:
            ok, detail = _run_command(
                ["ffmpeg", "-nostdin", "-n", "-i", str(path), "-q:a", "0", "-map", "a", str(staging)],
                300,
                params.get("_cancel_event"),
            )
            if not ok or not staging.is_file():
                staging.unlink(missing_ok=True)
                return f"Extract audio failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            return f"Audio extracted. Saved: {out.name}"
        except Exception as e:
            staging.unlink(missing_ok=True)
            return f"Extract audio failed: {type(e).__name__}"

    if action == "trim":
        if not _ffmpeg_available():
            return "ffmpeg not found."
        staging = None
        try:
            start = _media_time_param(params, "start", "00:00:00")
            end = _media_time_param(params, "end", "", allow_empty=True)
            out = _output_path(path, "trim", path.suffix)
            staging = _staging_output(out)
            cmd = ["ffmpeg", "-nostdin", "-n", "-i", str(path), "-ss", start]
            if end:
                cmd += ["-to", str(end)]
            cmd += ["-c", "copy", str(staging)]
            ok, detail = _run_command(cmd, 600, params.get("_cancel_event"))
            if not ok or not staging.is_file():
                staging.unlink(missing_ok=True)
                return f"Trim failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            return f"Trimmed video saved: {out.name}"
        except Exception as e:
            if staging is not None:
                staging.unlink(missing_ok=True)
            return f"Trim failed: {type(e).__name__}"

    if action == "extract_frame":
        if not _ffmpeg_available():
            return "ffmpeg not found."
        staging = None
        try:
            timestamp = _media_time_param(params, "timestamp", "00:00:01")
            label = timestamp.replace(":", "-").replace(".", "-")
            out = _output_path(path, f"frame_{label}", ".jpg")
            staging = _staging_output(out)
            ok, detail = _run_command(
                ["ffmpeg", "-nostdin", "-n", "-i", str(path), "-ss", str(timestamp),
                 "-vframes", "1", str(staging)],
                30,
                params.get("_cancel_event"),
            )
            if not ok or not staging.is_file():
                staging.unlink(missing_ok=True)
                return f"Extract frame failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            return f"Frame extracted at {timestamp}. Saved: {out.name}"
        except Exception as e:
            if staging is not None:
                staging.unlink(missing_ok=True)
            return f"Extract frame failed: {type(e).__name__}"

    if action == "compress":
        if not _ffmpeg_available():
            return "ffmpeg not found."
        staging = None
        try:
            crf = _int_param(params, "quality", 28, 0, 51)
            out = _output_path(path, f"compressed_crf{crf}", ".mp4")
            staging = _staging_output(out)
            ok, detail = _run_command(
                ["ffmpeg", "-nostdin", "-n", "-i", str(path),
                 "-c:v", "libx264", "-crf", str(crf),
                 "-preset", "medium", "-c:a", "copy", str(staging)],
                900,
                params.get("_cancel_event"),
            )
            if not ok or not staging.is_file():
                staging.unlink(missing_ok=True)
                return f"Compress failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            before = _file_size_str(path)
            after = _file_size_str(out)
            return f"Compressed: {before} → {after}. Saved: {out.name}"
        except Exception as e:
            if staging is not None:
                staging.unlink(missing_ok=True)
            return f"Compress failed: {type(e).__name__}"

    if action == "transcribe":
        if not _ffmpeg_available():
            return "ffmpeg not found. Needed for video transcription."
        try:
            with tempfile.TemporaryDirectory(prefix="mark-transcribe-") as directory:
                tmp_audio = Path(directory) / "audio.mp3"
                ok, detail = _run_command(
                    ["ffmpeg", "-nostdin", "-i", str(path), "-q:a", "0", "-map", "a", str(tmp_audio)],
                    300,
                    params.get("_cancel_event"),
                )
                if not ok or not tmp_audio.is_file():
                    return f"Video transcription failed: {detail or 'audio extraction produced no file'}"
                local_params = {**params, "save": False}
                result = _process_audio(tmp_audio, "transcribe", local_params, speak)
                if result.startswith("Transcription failed:"):
                    return result
                if _bool_param(params, "save", True):
                    out = _output_path(path, "transcript", ".txt")
                    atomic_create_text(out, result)
                    return f"Transcription saved: {out.name}\n\nPreview: {result[:300]}"
                return result
        except Exception as e:
            return f"Video transcription failed: {type(e).__name__}"

    if action == "convert":
        fmt = _text_param(params, "format", "mp4", 20).lower().lstrip(".")
        if not _ffmpeg_available():
            return "ffmpeg not found."
        if fmt not in {"mp4", "mkv", "mov", "webm", "avi"}:
            return "Supported video formats are mp4, mkv, mov, webm, and avi."
        out = _output_path(path, "converted", f".{fmt}")
        staging = _staging_output(out)
        try:
            ok, detail = _run_command(
                ["ffmpeg", "-nostdin", "-n", "-i", str(path), str(staging)],
                900,
                params.get("_cancel_event"),
            )
            if not ok or not staging.is_file():
                staging.unlink(missing_ok=True)
                return f"Convert failed: {detail or 'no output was produced'}"
            _publish_generated(staging, out)
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            staging.unlink(missing_ok=True)
            return f"Convert failed: {type(e).__name__}"

    return f"Unknown video action: '{action}'. Try: info, trim, extract_audio, extract_frame, compress, transcribe, convert"
