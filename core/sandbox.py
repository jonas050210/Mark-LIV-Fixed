"""Validation helpers for legacy model-generated desktop snippets.

Generated source execution is disabled. An AST allowlist remains only so old
callers receive a precise rejection for obviously unsafe snippets before the
unconditional execution-policy error. A Python subprocess is not a dependable
security sandbox for model-produced code.
"""
from __future__ import annotations

import ast
from pathlib import Path



class UnsafeCode(ValueError):
    pass


_ALLOWED_NODES = {
    ast.Module, ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.List, ast.Tuple, ast.Dict,
    ast.Set, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.If, ast.For,
    ast.While, ast.Break, ast.Continue, ast.IfExp, ast.Subscript, ast.Slice,
    ast.Attribute, ast.Call, ast.keyword, ast.JoinedStr, ast.FormattedValue,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
}
_ALLOWED_CALLS = {
    "print", "len", "str", "int", "float", "bool", "list", "dict", "tuple",
    "range", "enumerate", "sorted", "isinstance",
    "max", "min", "sum", "abs", "zip", "map", "filter", "Path",
}
_ALLOWED_ATTRIBUTES = {
    "home", "exists", "is_file", "is_dir", "iterdir", "stat", "read_text",
    "name", "suffix", "parent", "stem", "resolve", "__str__",
    "disk_usage", "join", "basename", "dirname",
    "splitext", "getsize", "sleep",
}
_BANNED_WORDS = {
    "import", "exec", "eval", "compile", "open", "unlink", "remove", "rmdir",
    "rmtree", "system", "popen", "spawn", "subprocess", "ctypes", "winreg",
    "socket", "requests", "urllib", "pickle", "marshal", "input", "getattr", "hasattr",
}


class _Validator(ast.NodeVisitor):
    def generic_visit(self, node):
        if type(node) not in _ALLOWED_NODES:
            raise UnsafeCode(f"syntax '{type(node).__name__}' is not allowed")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id.startswith("__") or node.id in _BANNED_WORDS:
            raise UnsafeCode(f"name '{node.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr not in _ALLOWED_ATTRIBUTES or (node.attr.startswith("__") and node.attr != "__str__"):
            raise UnsafeCode(f"attribute '{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            if node.func.id not in _ALLOWED_CALLS:
                raise UnsafeCode(f"call '{node.func.id}' is not allowed")
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr not in _ALLOWED_ATTRIBUTES:
                raise UnsafeCode(f"call '{node.func.attr}' is not allowed")
        else:
            raise UnsafeCode("computed calls are not allowed")
        self.generic_visit(node)


def validate_generated_code(code: str) -> str:
    code = (code or "").strip()
    if not code or len(code) > 24_000:
        raise UnsafeCode("empty or oversized generated code")
    lowered = code.casefold()
    if any(word in lowered for word in _BANNED_WORDS) or "__" in code:
        raise UnsafeCode("code contains a blocked capability")
    try:
        tree = ast.parse(code, mode="exec")
        _Validator().visit(tree)
        compile(tree, "<jarvis_sandbox>", "exec")
    except SyntaxError as exc:
        raise UnsafeCode(f"syntax error: {exc}") from exc
    return code


def run_generated_code(code: str, *, root: Path | None = None, timeout: float = 15.0) -> str:
    """Refuse generated source execution regardless of its apparent contents."""
    validate_generated_code(code)
    raise UnsafeCode(
        "model-generated Python execution is disabled; use a declared, validated action"
    )


__all__ = ["UnsafeCode", "validate_generated_code", "run_generated_code"]
