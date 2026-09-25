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

from __future__ import annotations

from actions.file_handlers.archives import (
    _archive_members,
    _cancel_requested,
    _process_archive,
)
from actions.file_handlers.code import _process_code
from actions.file_handlers.common import (  # noqa: F401 - some are re-exported for tests
    Path,
    _MAX_INPUT_BYTES,
    _detect_type,
    _gemini_client,
    _output_path,
    _read_text_prefix,
    _safe_input_path,
    _source_name,
    _stable_input_snapshot,
    _write_generated,
)
from actions.file_handlers.data import _process_data, _process_json, _process_xml
from actions.file_handlers.documents import (
    _process_pdf,
    _process_pptx,
    _process_text_doc,
)
from actions.file_handlers.images import _process_image
from actions.file_handlers.media import _process_audio, _process_video

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
