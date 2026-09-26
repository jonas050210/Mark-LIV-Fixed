"""Structured data: CSV and spreadsheets, JSON, XML."""
from __future__ import annotations

from actions.file_handlers.archives import _archive_members
from actions.file_handlers.common import (
    Path,
    _bool_param,
    _display_text,
    _file_size_str,
    _gemini_client,
    _output_path,
    _text_param,
    _write_generated,
    atomic_create_text,
    json,
)


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
    # XML files are user-controlled. defusedxml rejects entities, DTDs and
    # expansion attacks in the parser itself instead of relying on a brittle
    # byte-pattern pre-check.
    from defusedxml import ElementTree as SafeET
    import xml.etree.ElementTree as ET

    action = action or "validate"
    try:
        content = path.read_bytes()
        root = SafeET.fromstring(content)
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
