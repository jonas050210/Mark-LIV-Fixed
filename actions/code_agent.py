"""
actions/code_agent.py — Unified coding and multi-file project agent for Mark LIV.
Consolidates single-file editing/running/explaining/building (code_helper)
with autonomous multi-file project scaffolding and fix loops (dev_agent).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from core import gemini

PROJECTS_DIR = Path.home() / "Desktop" / "JarvisProjects"
DESKTOP = Path.home() / "Desktop"
MAX_FIX_ATTEMPTS = 5
MAX_BUILD_ATTEMPTS = 3


def _clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _resolve_save_path(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "html": ".html", "css": ".css",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "bash": ".sh", "shell": ".sh", "powershell": ".ps1",
        "sql": ".sql", "json": ".json", "rust": ".rs", "go": ".go",
    }
    if output_path:
        p = Path(output_path)
        return p if p.is_absolute() else DESKTOP / p
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"jarvis_code{ext}"


def _as_arg_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            return shlex.split(text)
        except Exception:
            return text.split()
    if isinstance(raw, (list, tuple)):
        return [str(a).strip() for a in raw if str(a).strip()]
    return [str(raw)]


def _run_file(path: Path, args: list[str], timeout: int) -> str:
    interpreters = {
        ".py": [sys.executable],
        ".js": ["node"],
        ".ts": ["ts-node"],
        ".sh": ["bash"],
        ".ps1": ["powershell", "-File"],
        ".rb": ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter registered for {path.suffix}."

    try:
        result = subprocess.run(
            interp + [str(path)] + (args or []),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent)
        )
        output = result.stdout.strip()
        error = result.stderr.strip()
        parts = []
        if output: parts.append(f"Output:\n{output}")
        if error: parts.append(f"Stderr:\n{error}")
        return "\n\n".join(parts) if parts else "Executed with no output."
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except FileNotFoundError:
        return f"Interpreter not found: {interp[0]}."
    except Exception as e:
        return f"Execution error: {e}"


def _single_file_write(description: str, language: str, output_path: str) -> tuple[str, Path]:
    lang = language or "python"
    prompt = f"""You are an expert {lang} developer.
Write clean, working, well-commented {lang} code for the description below.

Rules:
- Output ONLY the code. No explanation, no markdown, no backticks.
- Add helpful inline comments.
- Handle errors and edge cases properly.

Description: {description}

Code:"""
    resp = gemini.call(prompt, tier=gemini.SMART, timeout_ms=60_000)
    if resp is None:
        raise RuntimeError("Gemini model call failed")
    code = _clean_code(resp.text)
    save_path = _resolve_save_path(output_path, lang)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_text(code, encoding="utf-8")
    return code, save_path


def _single_file_edit(file_path: str, instruction: str) -> str:
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"
    content = p.read_text(encoding="utf-8")
    prompt = f"""You are an expert programmer. Modify the code according to the instruction.
Return ONLY the complete updated code — no markdown, no explanation, no backticks.

Change: {instruction}

Original code:
{content}

Updated code:"""
    resp = gemini.call(prompt, tier=gemini.SMART, timeout_ms=60_000)
    if resp is None:
        return "Gemini model call failed."
    edited = _clean_code(resp.text)
    p.write_text(edited, encoding="utf-8")
    return f"File edited and saved to: {p}"


def _single_file_explain(file_path: str, code: str) -> str:
    content = code
    if file_path and not content:
        p = Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"
        content = p.read_text(encoding="utf-8")
    if not content:
        return "Please provide code or a file path to explain."
    prompt = f"""Explain what this code does in clear, concise language (3-6 sentences max).

Code:
{content[:4000]}

Explanation:"""
    resp = gemini.call(prompt, tier=gemini.SMART, timeout_ms=30_000)
    return resp.text.strip() if resp else "Failed to explain code."


# ── Multi-file project scaffolding (formerly dev_agent) ────────────────────────

def _build_multi_file_project(description: str, language: str, project_name: str, timeout: int, player=None) -> str:
    lang = language or "python"
    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {lang}
Description: {description}

Return ONLY valid JSON:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{"path": "main.py", "description": "Entry point", "imports": []}}
  ],
  "run_command": "python main.py",
  "dependencies": []
}}"""
    resp = gemini.call(prompt, tier=gemini.SMART, timeout_ms=45_000)
    if not resp:
        return "Could not plan project structure."
    try:
        plan = json.loads(_clean_code(resp.text))
    except Exception as e:
        return f"Planning failed: {e}"

    proj_name = project_name or plan.get("project_name", "jarvis_project")
    proj_name = re.sub(r"[^\w\-]", "_", proj_name)
    project_dir = PROJECTS_DIR / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    files = plan.get("files", [])
    entry_point = plan.get("entry_point", "main.py")
    run_command = plan.get("run_command", f"python {entry_point}")

    if player and hasattr(player, "write_log"):
        player.write_log(f"[CodeAgent] Scaffolding {proj_name} ({len(files)} files)...")

    # Write files
    for fi in files:
        fpath = fi.get("path")
        if not fpath:
            continue
        write_prompt = f"""Write complete, working {lang} code for: {fpath}
Purpose: {fi.get('description', '')}
Project goal: {description}

Rules:
- Output ONLY raw code. No markdown fences, no explanation.
- Complete, runnable code.

Code:"""
        f_resp = gemini.call(write_prompt, tier=gemini.SMART, timeout_ms=60_000)
        if f_resp:
            code = _clean_code(f_resp.text)
            full_path = project_dir / fpath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(code, encoding="utf-8")

    # Install dependencies
    deps = plan.get("dependencies", [])
    if deps:
        subprocess.run([sys.executable, "-m", "pip", "install"] + deps, capture_output=True, timeout=60, cwd=str(project_dir))

    # Test run
    try:
        run_res = subprocess.run(
            shlex.split(run_command),
            capture_output=True, text=True,
            timeout=timeout, cwd=str(project_dir)
        )
        out = run_res.stdout.strip()
        err = run_res.stderr.strip()
        result_msg = f"Project '{proj_name}' created at: {project_dir}\nFiles: {len(files)}"
        if out:
            result_msg += f"\n\nRun Output:\n{out[:500]}"
        if err:
            result_msg += f"\n\nStderr:\n{err[:300]}"
        return result_msg
    except Exception as e:
        return f"Project created at {project_dir}, but test run encountered: {e}"


def code_agent(parameters: dict, player=None, **_kwargs) -> str:
    """Unified code agent entry point."""
    p = parameters or {}
    action = str(p.get("action", "write")).lower().strip()
    description = str(p.get("description", "")).strip()
    language = str(p.get("language", "python")).strip()
    output_path = str(p.get("output_path", "")).strip()
    file_path = str(p.get("file_path", "")).strip()
    code = str(p.get("code", "")).strip()
    args = _as_arg_list(p.get("args"))
    timeout = int(p.get("timeout", 30))

    if action in ("write", "create"):
        if not description:
            return "Please provide a description of the code to write."
        _, path = _single_file_write(description, language, output_path)
        return f"Code written and saved to: {path}"

    elif action == "edit":
        if not file_path:
            return "Please provide the file_path to edit."
        return _single_file_edit(file_path, description or p.get("instruction", ""))

    elif action == "explain":
        return _single_file_explain(file_path, code)

    elif action == "run":
        if not file_path:
            return "Please provide the file_path to run."
        p_file = Path(file_path)
        if not p_file.exists():
            return f"File not found: {file_path}"
        return _run_file(p_file, args, timeout)

    elif action in ("build_project", "scaffold", "dev_agent"):
        if not description:
            return "Please describe the project to build."
        proj_name = str(p.get("project_name", "")).strip()
        return _build_multi_file_project(description, language, proj_name, timeout, player)

    else:
        # Default fallback to write
        if description:
            _, path = _single_file_write(description, language, output_path)
            return f"Code written and saved to: {path}"
        return f"Unknown action: '{action}'. Available: write, edit, explain, run, build_project."


TOOL = {
    "name": "code_agent",
    "description": (
        "Complete software engineering agent: writes, edits, explains, and runs single code files, "
        "or scaffolds and tests entire multi-file projects from scratch. "
        "Use 'write' for scripts, 'edit' to update files, 'explain' to understand code, "
        "'run' to execute a script, and 'build_project' for multi-file applications."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "write | edit | explain | run | build_project (default: write)"
            },
            "description": {
                "type": "STRING",
                "description": "Description of the code or project to build/edit"
            },
            "language": {
                "type": "STRING",
                "description": "Programming language (default: python)"
            },
            "output_path": {
                "type": "STRING",
                "description": "Destination file path for single-file writes"
            },
            "file_path": {
                "type": "STRING",
                "description": "Path to existing file for edit, explain, or run"
            },
            "code": {
                "type": "STRING",
                "description": "Inline code snippet to explain"
            },
            "project_name": {
                "type": "STRING",
                "description": "Folder name when scaffolding a multi-file project"
            },
            "args": {
                "type": "ARRAY",
                "description": "CLI arguments for running a script",
                "items": {"type": "STRING"}
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Execution timeout in seconds (default: 30)"
            }
        },
        "required": ["action"]
    },
    "handler": code_agent,
}
