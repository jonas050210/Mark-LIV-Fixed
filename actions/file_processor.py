"""
file_processor.py — JARVIS Universal File Processor

Supported types:
  image   → describe, ocr, resize, convert, compress, crop
  pdf     → summarize, extract_text, extract_pages, to_word
  docx    → summarize, extract_text, reformat, translate_hint
  txt/md  → summarize, reformat, translate_hint, word_count
  csv     → analyze, filter, sort, convert, stats
  xlsx    → analyze, filter, convert, stats
  json    → validate, format, extract, convert
  code    → explain, review, fix, run, document
  audio   → transcribe, trim, convert, info
  video   → trim, extract_audio, extract_frame, info, compress
  zip     → list, extract
  pptx    → summarize, extract_text, to_pdf
"""

import re
import json
import os
import shutil
import stat
import tempfile
import tarfile
import secrets
import zipfile
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

# Model choice, timeout and fallback ladder all live in core/gemini.py.
from core import gemini
from core.path_policy import (
    PathPolicyError,
    atomic_create_text,
    move_no_replace,
    resolve_user_path,
)
from core.process_runner import run_bounded

_MAX_INPUT_BYTES = 100 * 1024 * 1024
_MAX_CLOUD_BYTES = 20 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 5_000
_MAX_ARCHIVE_EXPANDED = 1 * 1024 * 1024 * 1024
_MAX_ARCHIVE_RATIO = 200


def _safe_input_path(raw: str) -> Path:
    if len(raw) > 4096:
        raise ValueError("file path is too long")
    candidate = resolve_user_path(raw, allow_missing=False, reject_symlinks=True)
    if not candidate.is_file():
        raise FileNotFoundError(raw)
    if candidate.is_symlink():
        raise PermissionError("symbolic-link inputs are not processed")
    size = candidate.stat().st_size
    if size > _MAX_INPUT_BYTES:
        raise ValueError("file is larger than the 100 MB processing limit")
    file_type = _detect_type(candidate)
    type_limits = {
        "json": 10 * 1024 * 1024,
        "xml": 10 * 1024 * 1024,
        "text": 20 * 1024 * 1024,
        "code": 20 * 1024 * 1024,
        "csv": 50 * 1024 * 1024,
        "excel": 50 * 1024 * 1024,
        "docx": 50 * 1024 * 1024,
        "pptx": 50 * 1024 * 1024,
        "pdf": 50 * 1024 * 1024,
        "image": 50 * 1024 * 1024,
    }
    limit = type_limits.get(file_type, _MAX_INPUT_BYTES)
    if size > limit:
        raise ValueError(
            f"{file_type or 'file'} input is larger than its {limit // (1024 * 1024)} MB processing limit"
        )
    return candidate


def _is_reparse(details) -> bool:
    return bool(int(getattr(details, "st_file_attributes", 0)) & 0x400)


@contextmanager
def _stable_input_snapshot(source: Path):
    """Yield a private, bounded sibling snapshot so parsers never reopen a raced path."""
    snapshot = source.parent / (
        f".{source.stem}.processing-input-{secrets.token_hex(6)}{source.suffix}"
    )
    source_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    source_flags |= int(getattr(os, "O_NONBLOCK", 0))
    source_flags |= int(getattr(os, "O_NOFOLLOW", 0))
    source_descriptor = os.open(source, source_flags)
    destination_descriptor = -1
    try:
        details = os.fstat(source_descriptor)
        if not stat.S_ISREG(details.st_mode) or _is_reparse(details):
            raise PermissionError("input is not a regular file")
        if details.st_size > _MAX_INPUT_BYTES:
            raise ValueError("file is larger than the processing limit")
        destination_descriptor = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0)),
            0o600,
        )
        copied = 0
        with os.fdopen(source_descriptor, "rb") as input_file, os.fdopen(
            destination_descriptor, "wb"
        ) as output_file:
            source_descriptor = -1
            destination_descriptor = -1
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > _MAX_INPUT_BYTES:
                    raise ValueError("file grew beyond the processing limit")
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        yield snapshot
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        snapshot.unlink(missing_ok=True)


def _source_stem(path: Path) -> str:
    stem = path.stem
    marker = ".processing-input-"
    if stem.startswith(".") and marker in stem:
        return stem[1:].rsplit(marker, 1)[0]
    return stem


def _source_name(path: Path) -> str:
    return _source_stem(path) + path.suffix


def _require_cloud_size(path: Path) -> None:
    if path.stat().st_size > _MAX_CLOUD_BYTES:
        raise ValueError(
            "cloud processing is limited to 20 MB; use a local conversion or a smaller file"
        )


def _gemini_client(tier: str = gemini.SMART):
    """Summarising documents and reading images — the reasoning tier, with a
    long deadline because the input can be a whole file."""
    class _W:
        def generate_content(self, contents):
            resp = gemini.call(contents, tier=tier, timeout_ms=90000)
            if resp is None:
                raise RuntimeError("every Gemini model on the ladder failed")
            return resp

    return _W()


def _detect_type(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    image_exts = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "svg", "ico"}
    video_exts = {"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "m4v", "3gp"}
    audio_exts = {"mp3", "wav", "ogg", "m4a", "aac", "flac", "wma", "opus"}
    code_exts  = {"py", "js", "ts", "jsx", "tsx", "html", "css", "java", "c",
                  "cpp", "cs", "go", "rs", "rb", "php", "swift", "kt", "sh",
                  "bash", "ps1", "lua", "r", "m", "sql", "yaml", "toml"}
    archive_exts = {"zip", "rar", "tar", "gz", "7z", "bz2", "xz"}

    if ext in image_exts:  return "image"
    if ext in video_exts:  return "video"
    if ext in audio_exts:  return "audio"
    if ext in code_exts:   return "code"
    if ext in archive_exts: return "archive"
    if ext == "pdf":       return "pdf"
    if ext in ("docx", "doc"): return "docx"
    if ext in ("txt", "md", "rst", "log"): return "text"
    if ext in ("csv", "tsv"): return "csv"
    if ext in ("xlsx", "xls", "ods"): return "excel"
    if ext == "json":      return "json"
    if ext == "xml":       return "xml"
    if ext in ("pptx", "ppt"): return "pptx"
    return "unknown"


def _bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise ValueError(f"'{key}' must be true or false")


def _int_param(params: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be a whole number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
    return result


def _float_param(params: dict, key: str, default: float, minimum: float, maximum: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{key}' must be a number") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"'{key}' must be between {minimum} and {maximum}")
    return result


def _text_param(params: dict, key: str, default: str, maximum: int = 256) -> str:
    value = params.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be text")
    if len(value) > maximum:
        raise ValueError(f"'{key}' is too long")
    return value


def _media_time_param(params: dict, key: str, default: str, *, allow_empty: bool = False) -> str:
    value = _text_param(params, key, default, 64).strip()
    if allow_empty and not value:
        return ""
    if not re.fullmatch(r"(?:\d{1,3}:)?[0-5]?\d:[0-5]?\d(?:\.\d+)?|\d+(?:\.\d+)?", value):
        raise ValueError(f"'{key}' must be seconds or a HH:MM:SS timestamp")
    return value


def _staging_output(final_path: Path) -> Path:
    return final_path.parent / (
        f".{final_path.stem}.processing-{secrets.token_hex(6)}{final_path.suffix}"
    )


def _publish_generated(staging: Path, final_path: Path) -> None:
    try:
        details = staging.lstat()
    except OSError as exc:
        raise OSError("the converter did not produce an output file") from exc
    if (
        staging.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or _is_reparse(details)
        or details.st_size > 1024 * 1024 * 1024
    ):
        raise OSError("the converter did not produce a safe regular output file")
    move_no_replace(staging, final_path)


def _write_generated(final_path: Path, writer) -> None:
    staging = _staging_output(final_path)
    try:
        writer(staging)
        _publish_generated(staging, final_path)
    finally:
        staging.unlink(missing_ok=True)


def _run_command(argv: list[str], timeout: float, cancel_event=None) -> tuple[bool, str]:
    command = list(argv)
    if command and Path(command[0]).name.casefold() in {"ffmpeg", "ffmpeg.exe"}:
        # Keep a converter from filling the disk before its deadline fires.
        command = [*command[:-1], "-fs", str(1024 * 1024 * 1024), command[-1]]
    result = run_bounded(
        command, timeout=timeout, max_output=4_000, cancel_event=cancel_event
    )
    if result.cancelled:
        return False, "cancelled"
    if result.timed_out:
        return False, f"timed out after {timeout:.0f} seconds"
    if result.returncode != 0:
        return False, f"converter exited with code {result.returncode}"
    return True, ""


def _read_text_prefix(path: Path, max_bytes: int = 1_000_000) -> str:
    with path.open("rb") as handle:
        return handle.read(max_bytes).decode("utf-8", errors="replace")


def _display_text(value, maximum: int = 160) -> str:
    text = str(value)
    text = "".join(character if ord(character) >= 32 else " " for character in text)
    return text[:maximum]


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"


def _file_size_str(path: Path) -> str:
    return _format_bytes(path.stat().st_size)

def _output_path(src: Path, suffix: str, new_ext: str = None) -> Path:
    """Allocate a non-existing output beside ``src`` without overwriting."""
    ext = new_ext or src.suffix
    source_stem = _source_stem(src)
    base = src.parent / f"{source_stem}_{suffix}{ext}"
    candidate = base
    index = 1
    while candidate.exists():
        candidate = src.parent / f"{source_stem}_{suffix}_{index}{ext}"
        index += 1
        if index > 10_000:
            raise FileExistsError("could not allocate a unique output name")
    return resolve_user_path(candidate, allow_missing=True, reject_symlinks=True)

def _process_image(path: Path, action: str, params: dict, speak=None) -> str:
    try:
        from PIL import Image
    except ImportError:
        return "Pillow is not installed. Run: pip install Pillow"

    action = action or "describe"
    try:
        with Image.open(path) as probe:
            width, height = probe.size
            if width < 1 or height < 1 or width * height > 100_000_000:
                return "Image rejected: dimensions exceed the 100-megapixel safety limit."
            probe.verify()
    except Exception as exc:
        return f"Image rejected: {type(exc).__name__}"

    if action in ("describe", "ocr", "analyze", "read", "extract_text"):
        try:
            _require_cloud_size(path)
            model  = _gemini_client()
            img    = Image.open(path)
            prompt = {
                "describe": "Describe this image in detail.",
                "ocr":      "Extract all text visible in this image. Return only the text, formatted clearly.",
                "analyze":  "Analyze this image thoroughly: objects, colors, composition, any text, context.",
                "read":     "Read all text in this image, preserving structure and formatting.",
                "extract_text": "Extract all text from this image.",
            }.get(action, "Describe this image.")

            if params.get("instruction"):
                prompt = params["instruction"]

            response = model.generate_content([prompt, img])
            result   = response.text.strip()

            if len(result) > 500 and _bool_param(params, "save", True):
                out = _output_path(path, "result", ".txt")
                atomic_create_text(out, result)
                return f"{result[:300]}...\n\nFull result saved to: {out}"
            return result
        except Exception as e:
            return f"AI image analysis failed: {type(e).__name__}"

    if action == "resize":
        try:
            width = _int_param(params, "width", 0, 0, 20_000)
            height = _int_param(params, "height", 0, 0, 20_000)
            scale = _float_param(params, "scale", 0, 0, 20)
            img = Image.open(path)
            w, h = img.size
            if scale:
                new_size = (int(w * scale), int(h * scale))
            elif width and height:
                new_size = (width, height)
            elif width:
                new_size = (width, int(h * width / w))
            elif height:
                new_size = (int(w * height / h), height)
            else:
                return "Please specify width, height, or scale."
            if min(new_size) < 1 or new_size[0] * new_size[1] > 100_000_000:
                return "Requested image dimensions are invalid or exceed 100 megapixels."
            out = _output_path(path, f"resized_{new_size[0]}x{new_size[1]}")
            resized = img.resize(new_size, Image.LANCZOS)
            _write_generated(out, lambda staging: resized.save(staging))
            return f"Resized from {w}x{h} to {new_size[0]}x{new_size[1]}. Saved: {out.name}"
        except Exception as e:
            return f"Resize failed: {type(e).__name__}"

    if action == "convert":
        fmt = _text_param(params, "format", "png", 20).lower().strip(".")
        fmt_map = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
                   "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF"}
        if fmt not in fmt_map:
            return "Supported image formats are jpg, jpeg, png, webp, bmp, and tiff."
        pil_fmt = fmt_map[fmt]
        try:
            img = Image.open(path).convert("RGB") if fmt in {"jpg", "jpeg"} else Image.open(path)
            out = _output_path(path, "converted", f".{fmt}")
            _write_generated(out, lambda staging: img.save(staging, pil_fmt))
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            return f"Convert failed: {type(e).__name__}"

    if action == "compress":
        try:
            quality = _int_param(params, "quality", 70, 1, 95)
            img = Image.open(path).convert("RGB")
            out = _output_path(path, f"compressed_q{quality}", ".jpg")
            _write_generated(
                out,
                lambda staging: img.save(staging, "JPEG", quality=quality, optimize=True),
            )
            before = _file_size_str(path)
            after  = _file_size_str(out)
            return f"Compressed: {before} → {after}. Saved: {out.name}"
        except Exception as e:
            return f"Compress failed: {type(e).__name__}"

    if action == "info":
        try:
            img = Image.open(path)
            return (f"Image info: {img.format}, {img.size[0]}x{img.size[1]}px, "
                    f"mode: {img.mode}, size: {_file_size_str(path)}")
        except Exception as e:
            return f"Info failed: {type(e).__name__}"

    return _process_image(path, "describe", {"instruction": f"{action}: {params}"})

def _process_pdf(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "summarize"

    def _extract_pdf_text(max_chars=50_000, max_pages=200) -> str:
        chunks = []
        length = 0
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = pdf.pages[:max_pages]
                for page in pages:
                    chunk = (page.extract_text() or "") + "\n"
                    chunks.append(chunk)
                    length += len(chunk)
                    if length >= max_chars:
                        break
        except ImportError:
            try:
                import PyPDF2
                with open(path, "rb") as handle:
                    reader = PyPDF2.PdfReader(handle)
                    for page in islice(reader.pages, max_pages):
                        chunk = (page.extract_text() or "") + "\n"
                        chunks.append(chunk)
                        length += len(chunk)
                        if length >= max_chars:
                            break
            except ImportError:
                return ""
        return "".join(chunks)[:max_chars]

    if action in ("summarize", "extract_text", "translate_hint", "analyze", "reformat"):
        text = _extract_pdf_text()
        if not text.strip():
            return "Could not extract text from PDF (may be scanned/image-based)."

        if action == "extract_text":
            out = _output_path(path, "text", ".txt")
            atomic_create_text(out, text)
            return f"Text extracted ({len(text)} chars). Saved: {out.name}"

        prompt_map = {
            "summarize":      f"Summarize this PDF document concisely:\n\n{text}",
            "analyze":        f"Analyze this document thoroughly:\n\n{text}",
            "translate_hint": f"What language is this document in and what does it say? Summarize:\n\n{text}",
            "reformat":       f"Reformat this text cleanly with proper structure:\n\n{text}",
        }
        try:
            model    = _gemini_client()
            response = model.generate_content(prompt_map.get(action, f"Analyze:\n\n{text}"))
            result   = response.text.strip()
            if len(result) > 600 and _bool_param(params, "save", True):
                out = _output_path(path, action, ".txt")
                atomic_create_text(out, result)
                return f"{result[:400]}...\n\nFull result saved: {out.name}"
            return result
        except Exception as e:
            return f"AI analysis failed: {type(e).__name__}"

    if action == "info":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                pages = len(pdf.pages)
            return f"PDF: {pages} pages, size: {_file_size_str(path)}"
        except Exception:
            return f"PDF size: {_file_size_str(path)}"

    if action == "to_word":
        text = _extract_pdf_text()
        if not text:
            return "Could not extract text to convert."
        try:
            from docx import Document
            doc  = Document()
            doc.add_heading(_source_stem(path), 0)
            for para in text.split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
            out = _output_path(path, "converted", ".docx")
            _write_generated(out, doc.save)
            return f"Converted to Word document. Saved: {out.name}"
        except ImportError:
            return "python-docx not installed. Run: pip install python-docx"

    return f"Unknown PDF action: '{action}'. Try: summarize, extract_text, info, to_word"

def _process_text_doc(path: Path, file_type: str, action: str,
                       params: dict, speak=None) -> str:
    action = action or "summarize"

    def _read_content() -> str:
        if file_type == "docx":
            try:
                if path.suffix.lower() == ".docx":
                    _archive_members(path)
                from docx import Document
                doc = Document(path)
                chunks = []
                length = 0
                for paragraph in doc.paragraphs:
                    chunk = str(paragraph.text)[:10_000]
                    chunks.append(chunk)
                    length += len(chunk) + 1
                    if length >= 50_000:
                        break
                return "\n".join(chunks)[:50_000]
            except ImportError:
                return "python-docx not installed."
            except ValueError as e:
                return f"Office document rejected: {type(e).__name__}"
            except Exception as e:
                return f"Read failed: {type(e).__name__}"
        else:
            return _read_text_prefix(path, max_bytes=2_000_000)

    content = _read_content()
    if content.startswith(("Office document rejected:", "Read failed:", "python-docx not installed.")):
        return content
    if not content.strip():
        return "File appears to be empty."

    if action == "word_count":
        words = len(content.split())
        chars = len(content)
        lines = content.count("\n")
        return f"Word count: {words} words, {chars} characters, {lines} lines."

    if action == "extract_text":
        if file_type != "txt":
            out = _output_path(path, "extracted", ".txt")
            atomic_create_text(out, content)
            return f"Text extracted. Saved: {out.name}"
        return content[:2000]

    instruction = params.get("instruction", "")
    prompt_map  = {
        "summarize":  f"Summarize this document concisely:\n\n{content[:40000]}",
        "analyze":    f"Analyze this document:\n\n{content[:40000]}",
        "reformat":   f"Reformat this text with clean structure, proper headings and paragraphs:\n\n{content[:40000]}",
        "fix":        f"Fix grammar, spelling and style issues in this text:\n\n{content[:40000]}",
        "translate_hint": f"What language is this and what does it say? Summarize:\n\n{content[:10000]}",
        "to_bullet":  f"Convert this text into a clear bullet-point summary:\n\n{content[:40000]}",
        "custom":     f"{instruction}\n\n{content[:40000]}",
    }

    if action not in prompt_map:

        action  = "custom"
        instruction = action

    try:
        model    = _gemini_client()
        response = model.generate_content(prompt_map[action])
        result   = response.text.strip()
        if len(result) > 600 and _bool_param(params, "save", True):
            out = _output_path(path, action, ".txt")
            atomic_create_text(out, result)
            return f"{result[:400]}...\n\nFull result saved: {out.name}"
        return result
    except Exception as e:
        return f"AI processing failed: {type(e).__name__}"


def _process_data(path: Path, file_type: str, action: str,
                  params: dict, speak=None) -> str:
    try:
        import pandas as pd
    except ImportError:
        return "pandas not installed. Run: pip install pandas openpyxl"

    action = action or "analyze"

    try:
        if file_type == "csv":
            df = pd.read_csv(
                path,
                encoding="utf-8",
                encoding_errors="replace",
                nrows=200_001,
            )
        else:
            if path.suffix.lower() == ".xlsx":
                _kind, _members, expanded = _archive_members(path)
                if expanded > 200 * 1024 * 1024:
                    return "Spreadsheet rejected: expanded workbook exceeds 200 MB."
                try:
                    from openpyxl import load_workbook
                    workbook = load_workbook(path, read_only=True, data_only=True)
                    try:
                        if any(
                            sheet.max_row > 200_001 or sheet.max_column > 500
                            for sheet in workbook.worksheets
                        ):
                            return (
                                "Spreadsheet rejected: more than 200,000 rows "
                                "or 500 columns."
                            )
                    finally:
                        workbook.close()
                except ImportError:
                    pass
            df = pd.read_excel(path, nrows=200_001)
        if len(df) > 200_000 or len(df.columns) > 500:
            return "Dataset rejected: more than 200,000 rows or 500 columns."
    except Exception as e:
        return f"Could not read file: {type(e).__name__}"

    column_labels = [_display_text(column) for column in df.columns]
    available_columns = ", ".join(column_labels)[:4_000]
    if action == "info":
        return (f"Rows: {len(df)}, Columns: {len(df.columns)}\n"
                f"Columns: {available_columns}\n"
                f"Size: {_file_size_str(path)}")

    if action == "stats":
        try:
            desc = df.describe(include="all").to_string()
            return f"Statistics:\n{desc[:2000]}"
        except Exception as e:
            return f"Stats failed: {type(e).__name__}"

    if action == "analyze":
        preview = df.head(50).to_string()[:20_000]
        prompt  = (f"Analyze this dataset. Columns: {available_columns}\n"
                   f"Rows: {len(df)}\nPreview:\n{preview}\n\n"
                   f"Give insights, patterns, and notable findings.")
        try:
            model    = _gemini_client()
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"AI analysis failed: {type(e).__name__}"

    if action in ("convert", "to_csv", "to_excel", "to_json"):
        fmt = (
            _text_param(params, "format", "csv", 20).lower().lstrip(".")
            if action == "convert"
            else {"to_csv": "csv", "to_excel": "xlsx", "to_json": "json"}[action]
        )
        try:
            if fmt == "csv":
                out = _output_path(path, "converted", ".csv")
                _write_generated(
                    out, lambda staging: df.to_csv(staging, index=False, encoding="utf-8")
                )
            elif fmt == "xlsx":
                out = _output_path(path, "converted", ".xlsx")
                _write_generated(out, lambda staging: df.to_excel(staging, index=False))
            elif fmt == "json":
                out = _output_path(path, "converted", ".json")
                _write_generated(
                    out,
                    lambda staging: df.to_json(
                        staging, orient="records", force_ascii=False, indent=2
                    ),
                )
            else:
                return "Supported data conversion formats are csv, xlsx, and json."
            return f"Converted to {fmt.upper()}. Saved: {out.name}"
        except Exception as e:
            return f"Convert failed: {type(e).__name__}"

    if action == "filter":
        try:
            col = _text_param(params, "column", "", 256)
            value = _text_param(params, "value", "", 4_000)
            condition = _text_param(params, "condition", "equals", 32)
        except ValueError as exc:
            return f"Filter failed: {exc}"
        if not col or col not in df.columns:
            return f"Column '{_display_text(col)}' not found. Available: {available_columns}"
        try:
            if condition == "equals":     filtered = df[df[col] == value]
            elif condition == "contains": filtered = df[df[col].astype(str).str.contains(str(value), case=False, regex=False, na=False)]
            elif condition == "gt":       filtered = df[df[col] > float(value)]
            elif condition == "lt":       filtered = df[df[col] < float(value)]
            else:                         filtered = df[df[col] == value]
            out = _output_path(path, "filtered", ".csv")
            _write_generated(out, lambda staging: filtered.to_csv(staging, index=False))
            return f"Filtered: {len(filtered)} rows match. Saved: {out.name}"
        except Exception as e:
            return f"Filter failed: {type(e).__name__}"

    if action == "sort":
        try:
            col = (
                df.columns[0]
                if "column" not in params
                else _text_param(params, "column", "", 256)
            )
            asc = _bool_param(params, "ascending", True)
        except ValueError as exc:
            return f"Sort failed: {exc}"
        try:
            sorted_df = df.sort_values(col, ascending=asc)
            if file_type == "excel":
                out = _output_path(path, "sorted", ".xlsx")
                _write_generated(
                    out, lambda staging: sorted_df.to_excel(staging, index=False)
                )
            else:
                out = _output_path(path, "sorted", ".csv")
                _write_generated(
                    out,
                    lambda staging: sorted_df.to_csv(
                        staging, index=False, encoding="utf-8"
                    ),
                )
            return f"Sorted by '{_display_text(col)}'. Saved: {out.name}"
        except Exception as e:
            return f"Sort failed: {type(e).__name__}"

    preview = df.head(30).to_string()[:20_000]
    try:
        model    = _gemini_client()
        response = model.generate_content(
            f"Task: {action}\nDataset ({len(df)} rows, cols: {available_columns}):\n{preview}"
        )
        return response.text.strip()
    except Exception as e:
        return f"Processing failed: {type(e).__name__}"


def _process_json(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "analyze"
    try:
        content = path.read_text(encoding="utf-8")
        data    = json.loads(content)
    except Exception as e:
        return f"Invalid JSON: {type(e).__name__}"

    if action == "validate":
        return f"Valid JSON. Type: {type(data).__name__}, size: {_file_size_str(path)}"

    if action == "format":
        out = _output_path(path, "formatted", ".json")
        atomic_create_text(out, json.dumps(data, indent=2, ensure_ascii=False))
        return f"Formatted JSON saved: {out.name}"

    if action in ("analyze", "summarize", "extract"):
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:8000]
        prompt  = f"Task: {action} this JSON data:\n{preview}"
        if params.get("instruction"):
            prompt = f"{params['instruction']}\n\nJSON data:\n{preview}"
        try:
            model    = _gemini_client()
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"AI processing failed: {type(e).__name__}"

    if action == "to_csv":
        try:
            import pandas as pd
            if isinstance(data, list):
                df  = pd.DataFrame(data)
                out = _output_path(path, "converted", ".csv")
                _write_generated(out, lambda staging: df.to_csv(staging, index=False))
                return f"Converted to CSV. Saved: {out.name}"
            return "JSON must be an array of objects to convert to CSV."
        except ImportError:
            return "pandas not installed."

    return _process_json(path, "analyze", {"instruction": action})


def _process_xml(path: Path, action: str, params: dict, speak=None) -> str:
    import xml.etree.ElementTree as ET

    action = action or "validate"
    try:
        content = path.read_bytes()
        upper_content = content.upper()
        if b"<!DOCTYPE" in upper_content or b"<!ENTITY" in upper_content:
            return "Invalid XML: DTD and entity declarations are not allowed."
        root = ET.fromstring(content)
        tree = ET.ElementTree(root)
    except Exception as exc:
        return f"Invalid XML: {type(exc).__name__}"

    if action == "validate":
        return f"Valid XML. Root element: {root.tag}, size: {_file_size_str(path)}"
    if action == "format":
        ET.indent(tree, space="  ")
        out = _output_path(path, "formatted", ".xml")
        _write_generated(
            out,
            lambda staging: tree.write(
                staging, encoding="utf-8", xml_declaration=True
            ),
        )
        return f"Formatted XML saved: {out.name}"
    if action in {"analyze", "summarize", "extract"}:
        preview = ET.tostring(root, encoding="unicode")[:8000]
        instruction = str(params.get("instruction") or f"{action} this XML document")
        try:
            response = _gemini_client().generate_content(
                f"Instruction (trusted): {instruction}\n\nXML data (untrusted):\n{preview}"
            )
            return response.text.strip()
        except Exception as exc:
            return f"AI processing failed: {type(exc).__name__}"
    return "Unknown XML action. Try: validate, format, analyze, summarize, extract"


def _process_code(path: Path, action: str, params: dict, speak=None) -> str:
    action  = action or "explain"
    content = _read_text_prefix(path, max_bytes=2_000_000)
    ext     = path.suffix.lstrip(".")

    if action == "run":
        return (
            "Direct code execution is disabled. Review the file first and run it "
            "yourself in an isolated environment if you trust it."
        )

    if action == "info":
        lines = content.count("\n")
        words = len(content.split())
        return f"Code file: {lines} lines, {words} words, {_file_size_str(path)}"

    prompt_map = {
        "explain":   f"Explain this {ext} code clearly:\n\n```{ext}\n{content[:30000]}\n```",
        "review":    f"Review this {ext} code for bugs, issues, and improvements:\n\n```{ext}\n{content[:30000]}\n```",
        "fix":       f"Fix any bugs in this {ext} code and return the corrected version:\n\n```{ext}\n{content[:30000]}\n```",
        "optimize":  f"Optimize this {ext} code for performance and readability:\n\n```{ext}\n{content[:30000]}\n```",
        "document":  f"Add proper documentation/comments to this {ext} code:\n\n```{ext}\n{content[:30000]}\n```",
        "summarize": f"Summarize what this {ext} code does:\n\n```{ext}\n{content[:30000]}\n```",
        "test":      f"Write unit tests for this {ext} code:\n\n```{ext}\n{content[:30000]}\n```",
    }

    instruction = params.get("instruction", "")
    if action not in prompt_map:
        prompt = f"{action}\n\n```{ext}\n{content[:30000]}\n```"
        if instruction:
            prompt = f"{instruction}\n\n```{ext}\n{content[:30000]}\n```"
    else:
        prompt = prompt_map[action]

    try:
        model    = _gemini_client()
        response = model.generate_content(prompt)
        result   = response.text.strip()

        if action in ("fix", "optimize", "document") and _bool_param(params, "save", True):
            out = _output_path(path, action)
            code_match = re.search(r"```(?:\w+)?\n(.*?)```", result, re.DOTALL)
            code_to_save = code_match.group(1) if code_match else result
            atomic_create_text(out, code_to_save)
            return f"{result[:400]}...\n\nSaved: {out.name}"
        return result
    except Exception as e:
        return f"AI processing failed: {type(e).__name__}"

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

def _cancel_requested(params: dict) -> bool:
    event = params.get("_cancel_event")
    return bool(event is not None and hasattr(event, "is_set") and event.is_set())


def _archive_relative(name: str) -> Path:
    normalized = str(name or "").replace("\\", "/")
    if (
        not normalized
        or len(normalized) > 4096
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError("archive contains an empty or invalid member name")
    member = Path(normalized)
    if (member.is_absolute() or member.drive
            or any(":" in part or part in {"", ".", ".."} for part in member.parts)):
        raise ValueError(f"unsafe archive path: {name}")
    return member


def _archive_members(path: Path):
    suffixes = "".join(path.suffixes[-2:]).lower()
    if path.suffix.lower() in {".zip", ".docx", ".xlsx", ".pptx"}:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive has too many members")
            total = 0
            rows = []
            for info in infos:
                relative = _archive_relative(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"archive links are not allowed: {info.filename}")
                total += int(info.file_size)
                if total > _MAX_ARCHIVE_EXPANDED:
                    raise ValueError("archive expands beyond the 1 GB limit")
                compressed = max(1, int(info.compress_size))
                if info.file_size > 10 * 1024 * 1024 and info.file_size / compressed > _MAX_ARCHIVE_RATIO:
                    raise ValueError(f"suspicious compression ratio: {info.filename}")
                rows.append((relative, info.is_dir(), int(info.file_size), info))
            return "zip", rows, total

    if suffixes in {".tar.gz", ".tar.bz2", ".tar.xz"} or path.suffix.lower() == ".tar":
        rows = []
        total = 0
        with tarfile.open(path, mode="r:*") as archive:
            for index, info in enumerate(archive):
                if index >= _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive has too many members")
                relative = _archive_relative(info.name)
                if info.issym() or info.islnk() or info.isdev() or info.isfifo():
                    raise ValueError(f"archive links/devices are not allowed: {info.name}")
                if not (info.isfile() or info.isdir()):
                    raise ValueError(f"unsupported archive member: {info.name}")
                total += int(info.size)
                if total > _MAX_ARCHIVE_EXPANDED:
                    raise ValueError("archive expands beyond the 1 GB limit")
                rows.append((relative, info.isdir(), int(info.size), info.name))
        if path.stat().st_size and total > 10 * 1024 * 1024 and total / path.stat().st_size > _MAX_ARCHIVE_RATIO:
            raise ValueError("archive has a suspicious overall compression ratio")
        return "tar", rows, total
    raise ValueError(f"unsupported archive format: {path.suffix.lower()}")


def _archive_rejection(exc: Exception) -> str:
    """Return an actionable archive error without echoing member filenames."""
    message = str(exc).casefold()
    categories = (
        ("links/devices are not allowed", "archive links/devices are not allowed"),
        ("links are not allowed", "archive links are not allowed"),
        ("compression ratio", "suspicious compression ratio"),
        ("too many members", "archive has too many members"),
        ("expands beyond", "archive expands beyond the safe size limit"),
        ("unsafe archive path", "archive contains an unsafe path"),
        ("empty or invalid member", "archive contains an invalid member name"),
        ("unsupported archive member", "archive contains an unsupported member type"),
        ("unsupported archive format", "unsupported archive format"),
    )
    for marker, safe in categories:
        if marker in message:
            return safe
    return f"archive could not be validated ({type(exc).__name__})"


def _process_archive(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "list"
    try:
        archive_type, members, total = _archive_members(path)
    except Exception as exc:
        return f"Archive rejected: {_archive_rejection(exc)}"

    if action == "list":
        names = [
            _display_text(str(relative), 500) + ("/" if is_dir else "")
            for relative, is_dir, _size, _info in members
        ]
        preview = "\n".join(names[:30])
        suffix = f"\n... and {len(names) - 30} more" if len(names) > 30 else ""
        return f"Archive contains {len(names)} members ({_file_size_str(path)} compressed, {_format_bytes(total)} expanded):\n{preview}{suffix}"

    if action != "extract":
        return f"Unknown archive action: '{action}'. Try: list, extract"

    requested = params.get("destination")
    if requested is not None and not isinstance(requested, str):
        return "Extract destination must be text."
    if requested is not None and len(requested) > 4096:
        return "Extract destination is too long."
    try:
        destination = resolve_user_path(
            requested or (path.parent / _source_stem(path)),
            allow_missing=True,
            reject_symlinks=True,
        )
        if not requested:
            base = destination
            index = 1
            while destination.exists():
                destination = base.parent / f"{base.name}_{index}"
                index += 1
                if index > 10_000:
                    raise FileExistsError("could not allocate a destination folder")
    except (PathPolicyError, OSError, ValueError) as exc:
        return f"Extract failed: unsafe destination ({type(exc).__name__})"
    if destination.exists():
        return "Extract destination already exists; nothing was overwritten."

    staging = destination.parent / (
        f".{destination.name}.extracting-{secrets.token_hex(6)}"
    )
    extracted_bytes = 0

    def _copy_member(source, output, declared_size: int) -> None:
        nonlocal extracted_bytes
        member_bytes = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            member_bytes += len(chunk)
            extracted_bytes += len(chunk)
            if member_bytes > declared_size or extracted_bytes > _MAX_ARCHIVE_EXPANDED:
                raise ValueError("archive expanded beyond its declared safe limits")
            output.write(chunk)

    try:
        staging.mkdir(parents=True, exist_ok=False, mode=0o700)
        if archive_type == "zip":
            with zipfile.ZipFile(path) as archive:
                for relative, is_dir, _size, info in members:
                    if _cancel_requested(params):
                        raise InterruptedError("cancelled")
                    target = staging / relative
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, target.open("xb") as output:
                        _copy_member(source, output, _size)
        else:
            with tarfile.open(path, mode="r:*") as archive:
                for relative, is_dir, _size, member_name in members:
                    if _cancel_requested(params):
                        raise InterruptedError("cancelled")
                    target = staging / relative
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    source = archive.extractfile(member_name)
                    if source is None:
                        raise ValueError(f"could not read archive member: {member_name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source, target.open("xb") as output:
                        _copy_member(source, output, _size)
        move_no_replace(staging, destination)
        return f"Extracted {len(members)} members to: {destination}"
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return f"Extract failed and partial output was cleaned up: {type(exc).__name__}"

def _process_pptx(path: Path, action: str, params: dict, speak=None) -> str:
    action = action or "summarize"

    def _read_pptx_text() -> str:
        try:
            if path.suffix.lower() == ".pptx":
                _archive_members(path)
            from pptx import Presentation
            prs = Presentation(path)
            text = []
            total = 0
            for i, slide in enumerate(prs.slides, 1):
                if i > 200 or total >= 50_000:
                    break
                slide_parts = [f"\n--- Slide {i} ---\n"]
                for shape in slide.shapes:
                    value = getattr(shape, "text", "")
                    if isinstance(value, str) and value.strip():
                        slide_parts.append(value.strip()[:10_000] + "\n")
                slide_text = "".join(slide_parts)
                text.append(slide_text)
                total += len(slide_text)
            return "\n".join(text)[:50_000]
        except ImportError:
            return "python-pptx not installed."
        except ValueError as exc:
            return f"Office document rejected: {type(exc).__name__}"
        except Exception as exc:
            return f"Presentation read failed: {type(exc).__name__}"

    if action in ("summarize", "extract_text", "analyze"):
        text = _read_pptx_text()
        if text.startswith(("Office document rejected:", "Presentation read failed:", "python-pptx not installed.")):
            return text
        if action == "extract_text":
            out = _output_path(path, "text", ".txt")
            atomic_create_text(out, text)
            return f"Text extracted. Saved: {out.name}"
        try:
            model    = _gemini_client()
            prompt   = f"{'Summarize' if action == 'summarize' else 'Analyze'} this presentation:\n{text[:30000]}"
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"AI processing failed: {type(e).__name__}"

    return f"Unknown PPTX action: '{action}'. Try: summarize, extract_text, analyze"

def file_processor(parameters: dict, player=None, speak=None, cancel_event=None) -> str:
    if not isinstance(parameters, dict):
        return "Parameters must be an object."
    raw_path = parameters.get("file_path", "")
    if not isinstance(raw_path, str):
        return "File path must be text."
    file_path_str = raw_path.strip()
    if not file_path_str:
        return "No file path provided."

    try:
        path = _safe_input_path(file_path_str)
    except FileNotFoundError:
        return "File not found or unavailable."
    except (PermissionError, ValueError) as exc:
        return f"Access denied: {type(exc).__name__}"

    file_type = _detect_type(path)
    raw_action = parameters.get("action", "")
    raw_instruction = parameters.get("instruction", "")
    if raw_action is not None and not isinstance(raw_action, str):
        return "Action must be text."
    if raw_instruction is not None and not isinstance(raw_instruction, str):
        return "Instruction must be text."
    action = (raw_action or "")[:40].lower().strip()
    instruction = (raw_instruction or "")[:20_000]
    params = {
        **parameters,
        "instruction": instruction,
        "_cancel_event": cancel_event,
    }

    try:
        with _stable_input_snapshot(path) as stable_path:
            return _process_stable_file(
                stable_path, file_type, action, instruction, params, player, speak
            )
    except (OSError, PermissionError, ValueError) as exc:
        return f"File could not be safely opened: {type(exc).__name__}"


def _process_stable_file(
    path: Path, file_type: str, action: str, instruction: str,
    params: dict, player=None, speak=None,
) -> str:
    if _cancel_requested(params):
        return "File processing was cancelled."

    type_limits = {
        "text": 20 * 1024 * 1024,
        "code": 20 * 1024 * 1024,
        "json": 20 * 1024 * 1024,
        "xml": 20 * 1024 * 1024,
        "csv": 25 * 1024 * 1024,
        "excel": 25 * 1024 * 1024,
    }
    if path.stat().st_size > type_limits.get(file_type, _MAX_INPUT_BYTES):
        return f"File is too large to safely process as {file_type}."
    if path.suffix.lower() in {".docx", ".xlsx", ".pptx"}:
        try:
            _kind, office_members, office_total = _archive_members(path)
            if len(office_members) > 2_000 or office_total > 250 * 1024 * 1024:
                return "Office document rejected: expanded document is too large."
        except Exception as exc:
            return f"Office document rejected: {type(exc).__name__}"

    log_msg = f"[FileProcessor] {file_type.upper()} | action={action or 'auto'}"
    print(log_msg)
    if player:
        player.write_log(log_msg)

    if file_type == "unknown":
        try:
            content = _read_text_prefix(path, max_bytes=10_000)
            model   = _gemini_client()
            prompt  = f"File: {_source_name(path)}\nContent preview:\n{content}\n\nTask: {action or instruction or 'Describe what this file contains and what can be done with it.'}"
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            return f"Unknown file type ({path.suffix}). Could not process: {type(e).__name__}"

    dispatch = {
        "image":   _process_image,
        "pdf":     _process_pdf,
        "docx":    lambda p, a, pm, s: _process_text_doc(p, "docx", a, pm, s),
        "text":    lambda p, a, pm, s: _process_text_doc(p, "text", a, pm, s),
        "csv":     lambda p, a, pm, s: _process_data(p, "csv",   a, pm, s),
        "excel":   lambda p, a, pm, s: _process_data(p, "excel", a, pm, s),
        "json":    _process_json,
        "xml":     _process_xml,
        "code":    _process_code,
        "audio":   _process_audio,
        "video":   _process_video,
        "archive": _process_archive,
        "pptx":    _process_pptx,
    }

    handler = dispatch.get(file_type)
    if not handler:
        return f"Unsupported file type: {file_type}"

    try:
        result = handler(path, action, params, speak)
        return result or "Done."
    except Exception as e:
        import traceback
        traceback.print_tb(e.__traceback__)
        return f"Processing failed: {type(e).__name__}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_processor",
    "description": "Processes any file that the user has uploaded or dropped onto the interface. Use this when the user refers to an uploaded file and wants an action on it. Supports: images (describe/ocr/resize/compress/convert), PDFs (summarize/extract_text/to_word), Word docs & text files (summarize/fix/reformat/translate), CSV/Excel (analyze/stats/filter/sort/convert), JSON/XML (validate/format/analyze), code files (explain/review/fix/optimize/document/test; direct execution is disabled), audio (transcribe/trim/convert/info), video (trim/extract_audio/extract_frame/compress/transcribe/info), archives (list/extract), presentations (summarize/extract_text). ALWAYS call this tool when a file has been uploaded and the user gives a command about it. If the user's command is ambiguous, pick the most logical action for that file type.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "maxLength": 500,
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "maxLength": 80,
                "description": "What to do with the file. Examples by type:\nimage: describe | ocr | resize | compress | convert | info\npdf: summarize | extract_text | to_word | info\ndocx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\ncsv/excel: analyze | stats | filter | sort | convert | info\njson: validate | format | analyze | to_csv\ncode: explain | review | fix | optimize | document | test\naudio: transcribe | trim | convert | info\nvideo: trim | extract_audio | extract_frame | compress | transcribe | info | convert\narchive: list | extract\npptx: summarize | extract_text | analyze"
            },
            "instruction": {
                "type": "STRING",
                "maxLength": 5000,
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "maxLength": 20,
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width": {
                "type": "INTEGER",
                "minimum": 0,
                "maximum": 20000,
                "description": "Target width for image resize"
            },
            "height": {
                "type": "INTEGER",
                "minimum": 0,
                "maximum": 20000,
                "description": "Target height for image resize"
            },
            "scale": {
                "type": "NUMBER",
                "minimum": 0,
                "maximum": 20,
                "description": "Scale factor for image resize (e.g. 0.5)"
            },
            "quality": {
                "type": "INTEGER",
                "minimum": 1,
                "maximum": 100,
                "description": "Quality 1-100 for image/video compress"
            },
            "start": {
                "type": "STRING",
                "maxLength": 32,
                "description": "Start time for trim: seconds or HH:MM:SS"
            },
            "end": {
                "type": "STRING",
                "maxLength": 32,
                "description": "End time for trim: seconds or HH:MM:SS"
            },
            "timestamp": {
                "type": "STRING",
                "maxLength": 32,
                "description": "Timestamp for video frame extraction HH:MM:SS"
            },
            "column": {
                "type": "STRING",
                "maxLength": 500,
                "description": "Column name for CSV filter/sort"
            },
            "value": {
                "type": "STRING",
                "maxLength": 2000,
                "description": "Filter value for CSV filter"
            },
            "condition": {
                "type": "STRING",
                "enum": ["equals", "contains", "gt", "lt"],
                "maxLength": 20,
                "description": "Filter condition: equals|contains|gt|lt"
            },
            "ascending": {
                "type": "BOOLEAN",
                "description": "Sort order for CSV sort (default: true)"
            },
            "save": {
                "type": "BOOLEAN",
                "description": "Save result to file (default: true)"
            },
            "destination": {
                "type": "STRING",
                "maxLength": 500,
                "description": "Output folder for archive extract"
            }
        },
        "required": []
    },
    "handler": file_processor,
    "confirmation_actions": ["extract"],
    "timeout_seconds": 180,
}
