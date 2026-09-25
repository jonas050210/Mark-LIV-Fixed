"""Source-code handling: explain, review, fix, run, document."""
from __future__ import annotations

from actions.file_handlers.common import (
    Path,
    _bool_param,
    _file_size_str,
    _gemini_client,
    _output_path,
    _read_text_prefix,
    atomic_create_text,
    re,
)


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
