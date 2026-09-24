"""Small, deny-by-default runner for model-generated desktop snippets.

This is a safety boundary, not a claim of a kernel sandbox.  The generated
program runs in a separate interpreter with a clean environment, a process
limit, no imports, no real ``Path``/``os``/``ctypes`` objects, and a strict AST
allowlist.  A task that needs capabilities outside this surface is refused.
"""
from __future__ import annotations

import ast
import base64
import os
import sys
from pathlib import Path

from core.process_runner import run_bounded


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
    "copy2", "copytree", "disk_usage", "join", "basename", "dirname",
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


def _child_script() -> str:
    """The child interpreter body; kept as a string so it has no project imports."""
    return r'''
import ast, base64, os, sys, time as _time
from pathlib import Path as _RealPath

code = base64.b64decode(sys.argv[1]).decode("utf-8")
root = _RealPath(sys.argv[2]).resolve()

class SafePath:
    def __init__(self, value):
        raw = _RealPath(value).expanduser()
        resolved = raw.resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError("path is outside the allowed home directory")
        self._p = resolved
    @classmethod
    def home(cls): return cls(root)
    def __truediv__(self, other): return SafePath(self._p / str(other))
    @property
    def parent(self): return SafePath(self._p.parent)
    @property
    def name(self): return self._p.name
    @property
    def stem(self): return self._p.stem
    @property
    def suffix(self): return self._p.suffix
    def exists(self): return self._p.exists()
    def is_file(self): return self._p.is_file()
    def is_dir(self): return self._p.is_dir()
    def iterdir(self): return [SafePath(x) for x in self._p.iterdir()]
    def stat(self): return self._p.stat()
    def read_text(self, encoding="utf-8", errors="strict"): return self._p.read_text(encoding=encoding, errors=errors)[:100000]
    def resolve(self): return self
    def __str__(self): return str(self._p)

class SafeShutil:
    @staticmethod
    def copy2(src, dst):
        from shutil import copy2
        return copy2(str(src), str(dst))
    @staticmethod
    def copytree(src, dst):
        from shutil import copytree
        return copytree(str(src), str(dst))
    @staticmethod
    def disk_usage(path):
        from shutil import disk_usage
        return disk_usage(str(path))

class SafeOsPath:
    join = staticmethod(os.path.join)
    exists = staticmethod(os.path.exists)
    isfile = staticmethod(os.path.isfile)
    isdir = staticmethod(os.path.isdir)
    basename = staticmethod(os.path.basename)
    dirname = staticmethod(os.path.dirname)
    splitext = staticmethod(os.path.splitext)
    getsize = staticmethod(os.path.getsize)

safe_builtins = {
    "print": print, "len": len, "str": str, "int": int, "float": float,
    "bool": bool, "list": list, "dict": dict, "tuple": tuple,
    "range": range, "enumerate": enumerate, "sorted": sorted,
    "isinstance": isinstance,
    "max": max, "min": min, "sum": sum, "abs": abs,
    "zip": zip, "map": map, "filter": filter,
}
namespace = {
    "__builtins__": safe_builtins, "Path": SafePath, "shutil": SafeShutil(),
    "os_path": SafeOsPath(), "time": type("SafeTime", (), {"sleep": staticmethod(_time.sleep)})(),
}
exec(compile(code, "<jarvis_sandbox>", "exec"), namespace, namespace)
'''


def run_generated_code(code: str, *, root: Path | None = None, timeout: float = 15.0) -> str:
    code = validate_generated_code(code)
    root = (root or Path.home()).expanduser().resolve()
    home = Path.home().resolve()
    if root != home and home not in root.parents:
        raise UnsafeCode("sandbox root must be inside the user home directory")
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    result = run_bounded(
        [sys.executable, "-I", "-S", "-c", _child_script(), encoded, str(root)],
        cwd=str(root), env=env, timeout=timeout, max_output=20_000,
    )
    if result.timed_out:
        return f"Generated desktop code timed out after {timeout:.0f}s and was stopped."
    if result.returncode not in (0, None):
        return f"Generated desktop code failed: {result.stderr.strip()[:800]}"
    return result.stdout.strip() or "Done."


__all__ = ["UnsafeCode", "validate_generated_code", "run_generated_code"]
