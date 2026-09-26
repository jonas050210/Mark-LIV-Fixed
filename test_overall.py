#!/usr/bin/env python3
"""Safe project-wide verification runner for MARK LIV.

This is intentionally a runner, not a second copy of the unit-test suite.  It
checks the repository contract, action discovery, dashboard assets, and then
runs the normal tests in a subprocess so a test cannot leave this process in a
partially imported state.

The default run is non-destructive and does not open applications, touch user
files, use the network, or access real devices.  Windows hardware checks are
opt-in with ``--windows`` and should only be run on the machine being tested.
"""
from __future__ import annotations

import argparse
import ast
import compileall
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

for _stream_name in ("stdout", "stderr"):
    try:
        _stream = getattr(sys, _stream_name, None)
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
TESTS = REPO / "tests"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    duration_ms: int


class Overall:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.checks: list[Check] = []

    def run(self, name: str, fn: Callable[[], str | None]) -> None:
        started = time.monotonic()
        try:
            detail = fn() or "OK"
            status = "passed"
        except SkipCheck as exc:
            detail = str(exc)
            status = "skipped"
        except Exception as exc:  # the report should identify the failed gate
            detail = f"{type(exc).__name__}: {exc}"
            status = "failed"
        elapsed = int((time.monotonic() - started) * 1000)
        result = Check(name, status, detail, elapsed)
        self.checks.append(result)
        icon = {"passed": "PASS", "skipped": "SKIP", "failed": "FAIL"}[status]
        print(f"[{icon:4}] {name} ({elapsed} ms) — {detail}")


class SkipCheck(Exception):
    """A check that is intentionally unavailable in this environment."""


def _required_files() -> str:
    required = (
        "README.md",
        "requirements.txt",
        "setup.py",
        "core/action_loader.py",
        "core/action_runtime.py",
        "core/explorer.py",
        "actions/open_app.py",
        "actions/file_controller.py",
        "dashboard/server.py",
        "dashboard/static/app.html",
        "dashboard/static/login.html",
    )
    missing = [item for item in required if not (REPO / item).is_file()]
    if missing:
        raise RuntimeError("missing required files: " + ", ".join(missing))
    return f"{len(required)} required files present"


def _compile_python() -> str:
    ok = compileall.compile_dir(
        str(REPO), quiet=1, maxlevels=20,
        rx=re.compile(r"(^|[\\/])(?:\.git|\.venv|venv|__pycache__)(?:[\\/]|$)"),
    )
    if not ok:
        raise RuntimeError("compileall reported a syntax error")
    return "all Python files compile"


def _parse_python_contracts() -> str:
    parsed = 0
    for path in sorted(REPO.rglob("*.py")):
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parsed += 1
    return f"AST parsed {parsed} Python files"


def _discover_actions() -> str:
    from core.action_loader import discover_actions

    messages: list[str] = []
    registry = discover_actions(REPO / "actions", logger=messages.append)
    names = registry.names()
    required = {"open_app", "file_controller", "session_manager", "window_manager"}
    missing = required - names
    if missing:
        raise RuntimeError("required actions missing: " + ", ".join(sorted(missing)))
    if len(names) != len(set(names)):
        raise RuntimeError("duplicate action names discovered")

    records = getattr(registry, "_all_records", [])
    bad = []
    for record in records:
        if not record.valid or not record.available:
            continue
        params = record.parameters
        if not isinstance(params, dict) or params.get("type") != "OBJECT":
            bad.append(f"{record.name}: parameters must be an OBJECT schema")
        elif not isinstance(params.get("properties", {}), dict):
            bad.append(f"{record.name}: properties must be an object")
        # Bundled action handlers are intentionally lazy: only their checked
        # manifest/source snapshot belongs in startup memory. The registry loads
        # and revalidates the callable on the first real invocation.
        if callable(record.handler):
            bad.append(f"{record.name}: handler was imported during lazy discovery")
        if not record.source or not record.source_path or not record.module_name:
            bad.append(f"{record.name}: trusted lazy source snapshot is missing")
    if bad:
        raise RuntimeError("; ".join(bad))
    return f"{len(names)} active lazy actions; {len(records)} records inspected"


def _subprocess_safety() -> str:
    """Reject Python-shell interpolation in normal application code."""
    violations = []
    for path in sorted(REPO.rglob("*.py")):
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if function not in {"run", "Popen", "call", "check_call", "check_output"}:
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    violations.append(f"{path.relative_to(REPO)}:{node.lineno}")
    if violations:
        raise RuntimeError("shell=True subprocess calls: " + ", ".join(violations))
    return "no shell=True subprocess calls found"


def _dashboard_assets() -> str:
    html = (REPO / "dashboard/static/app.html").read_text(encoding="utf-8")
    required_fragments = (
        "/api/actions",
        "/api/explorer/search",
        "/api/explorer/open",
        "cancelAction",
        "searchExplorer",
    )
    missing = [fragment for fragment in required_fragments if fragment not in html]
    if missing:
        raise RuntimeError("dashboard is missing: " + ", ".join(missing))

    node = shutil.which("node")
    if not node:
        raise SkipCheck("Node.js is not installed; dashboard JavaScript syntax was not checked")
    scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
    if not scripts:
        raise RuntimeError("dashboard contains no inline JavaScript")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write("\n".join(scripts))
        script_path = Path(handle.name)
    try:
        completed = subprocess.run(
            [node, "--check", str(script_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).strip() or "node --check failed")
    return f"{len(scripts)} dashboard script blocks are syntactically valid"


def _dashboard_route_contract() -> str:
    import dashboard.server as dashboard_server

    if not dashboard_server._DEPS_OK:
        raise SkipCheck("FastAPI/uvicorn are not installed; dashboard route construction was skipped")
    dashboard = dashboard_server.DashboardServer()
    routes = {getattr(route, "path", "") for route in dashboard.app.routes}
    required = {
        "/login",
        "/api/capabilities",
        "/api/desktop",
        "/api/actions",
        "/api/explorer/search",
        "/api/explorer/open",
    }
    missing = required - routes
    if missing:
        raise RuntimeError("dashboard routes missing: " + ", ".join(sorted(missing)))
    return f"dashboard constructed with {len(routes)} routes"


def _setup_and_requirements() -> str:
    ast.parse((REPO / "setup.py").read_text(encoding="utf-8"), filename="setup.py")
    lines = (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not requirements:
        raise RuntimeError("requirements.txt contains no dependencies")
    if "PyQt6>=6.6,<7" not in requirements:
        raise RuntimeError("the required PyQt6 dependency is missing")
    completed = subprocess.run(
        [sys.executable, "setup.py", "--check"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError("setup.py --check failed: " + detail)
    return f"setup.py --check passed; {len(requirements)} requirement entries found"


def _secret_hygiene() -> str:
    expected = (
        "config/api_keys.json",
        "config/spotify_token.json",
        "config/app_index.json",
        "config/certs/jarvis.key",
        "memory/long_term.json",
        "memory/sessions.json",
    )
    # Ask git itself rather than searching .gitignore for a substring. A pattern
    # written as "config/api_keys.json  # comment" appears in the file but
    # matches nothing, because git has no trailing-comment syntax.
    missing = [
        entry for entry in expected
        if subprocess.run(
            ["git", "check-ignore", "-q", entry],
            cwd=REPO, capture_output=True, check=False, timeout=10,
        ).returncode != 0
    ]
    if missing:
        raise RuntimeError(
            ".gitignore does not actually ignore: " + ", ".join(missing)
            + " (a trailing '# comment' on a pattern line breaks it)"
        )
    forbidden_names = {
        "api_keys.json", "spotify_token.json", "client_secret.json",
        "token.json", "jarvis.key", "jarvis.crt",
    }
    unprotected = []
    protected = 0
    for path in REPO.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.name not in forbidden_names:
            continue
        relative = str(path.relative_to(REPO))
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", relative],
            cwd=REPO, capture_output=True, check=False, timeout=10,
        ).returncode == 0
        if ignored:
            protected += 1
        else:
            unprotected.append(relative)
    if unprotected:
        raise RuntimeError("secret-like files are not ignored: " + ", ".join(unprotected))
    suffix = f"; {protected} local secret files protected" if protected else ""
    return "secret paths are ignored and no unprotected secret files are present" + suffix


def _run_unit_tests() -> str:
    if not TESTS.is_dir():
        raise RuntimeError("tests directory is missing")
    env = os.environ.copy()
    env.pop("RUN_WINDOWS_INTEGRATION", None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(TESTS), "-p", "test_*.py", "-v"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    if completed.returncode:
        tail = (completed.stderr or completed.stdout).strip().splitlines()[-30:]
        raise RuntimeError("unit suite failed:\n" + "\n".join(tail))
    summary = "\n".join((completed.stderr or completed.stdout).strip().splitlines()[-4:])
    return summary or "unit suite passed"


# The modules where a gap in the tests is a safety problem rather than a
# cosmetic one: they decide what is allowed to touch the filesystem, what is
# persisted, what a failed action reports, and what a launch actually starts.
#
# The floors are a few points below what the suite actually reaches, because
# the same tests cover slightly different lines on each platform: POSIX
# permission bits, Windows reparse points and the native window API are each
# unreachable somewhere. A floor that only holds on Linux would fail the
# Windows runner for no defect.
COVERAGE_FLOORS = {
    "core/action_result.py": 90,
    "core/action_runtime.py": 85,
    "core/app_index.py": 76,
    "core/background_scheduler.py": 88,
    "core/browser_handoff.py": 90,
    "core/confirm.py": 70,
    "core/json_store.py": 84,
    "core/path_policy.py": 62,
    "core/sandbox.py": 78,
    "core/text_match.py": 82,
    "core/undo.py": 85,
    "memory/session_store.py": 88,
    "actions/layout_manager.py": 74,
    "actions/open_app.py": 60,
    "actions/reminder.py": 65,
    "dashboard/server.py": 40,
    "core/speech_shaping.py": 85,
    "core/untrusted.py": 85,
    # Windows cannot execute the POSIX ownership and permission branches.
    "core/plugin_loader.py": 66,
}


def _run_coverage() -> str:
    """Measure line coverage and enforce a floor on the modules that matter.

    A total percentage across a codebase this size says very little — it is
    dominated by the GUI and by platform branches that cannot run here. What is
    worth gating is the handful of modules that decide what MARK LIV is allowed
    to do, so each of those carries its own floor.
    """
    try:
        import coverage  # noqa: F401
    except ImportError as exc:
        raise SkipCheck("coverage is not installed (pip install coverage)") from exc

    env = os.environ.copy()
    env.pop("RUN_WINDOWS_INTEGRATION", None)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    data_file = REPO / ".coverage.overall"
    env["COVERAGE_FILE"] = str(data_file)
    common = [sys.executable, "-m", "coverage"]
    try:
        completed = subprocess.run(
            common + ["run", "--source", ".", "--omit", "tests/*,*/site-packages/*",
                      "-m", "unittest", "discover", "-s", str(TESTS), "-p", "test_*.py"],
            cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=False,
        )
        if completed.returncode:
            tail = (completed.stderr or completed.stdout).strip().splitlines()[-20:]
            raise RuntimeError("tests failed under coverage:\n" + "\n".join(tail))
        report = subprocess.run(
            common + ["json", "-o", "-", "--quiet"],
            cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        if report.returncode:
            raise RuntimeError("coverage report failed: " + report.stderr.strip()[-400:])
        measured = json.loads(report.stdout)
    finally:
        for leftover in REPO.glob(".coverage.overall*"):
            leftover.unlink(missing_ok=True)

    files = measured.get("files", {})
    below = []
    for name, floor in sorted(COVERAGE_FLOORS.items()):
        entry = files.get(name) or files.get(name.replace("/", os.sep))
        if entry is None:
            below.append(f"{name}: not measured")
            continue
        percent = entry["summary"]["percent_covered"]
        if percent + 0.5 < floor:
            below.append(f"{name}: {percent:.0f}% < {floor}%")
    if below:
        raise RuntimeError("coverage below the agreed floor — " + "; ".join(below))
    total = measured.get("totals", {}).get("percent_covered", 0.0)
    return (
        f"{len(COVERAGE_FLOORS)} safety-critical modules at or above their floor; "
        f"{total:.0f}% overall"
    )


def _run_windows_tests() -> str:
    if platform.system() != "Windows":
        raise SkipCheck("Windows integration checks require Windows")
    env = os.environ.copy()
    env["RUN_WINDOWS_INTEGRATION"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_windows_integration", "-v"],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout).strip()[-5000:])
    return "Windows hardware integration suite passed"


def _environment() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=10,
        ).stdout.strip()
    except OSError:
        commit = ""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "commit": commit,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", action="store_true", help="run opt-in Windows hardware checks")
    parser.add_argument("--coverage", action="store_true",
                        help="measure line coverage and enforce the per-module floors")
    parser.add_argument("--json", metavar="PATH", help="write the machine-readable report to PATH")
    args = parser.parse_args(argv)

    print("MARK LIV overall verification")
    print(json.dumps(_environment(), indent=2))
    runner = Overall(args)
    runner.run("required files", _required_files)
    runner.run("Python compilation", _compile_python)
    runner.run("Python AST contracts", _parse_python_contracts)
    runner.run("action registry", _discover_actions)
    runner.run("subprocess safety", _subprocess_safety)
    runner.run("dashboard assets", _dashboard_assets)
    runner.run("dashboard route contract", _dashboard_route_contract)
    runner.run("setup and requirements", _setup_and_requirements)
    runner.run("secret hygiene", _secret_hygiene)
    runner.run("unit test suite", _run_unit_tests)
    if args.coverage:
        runner.run("coverage floors", _run_coverage)
    else:
        runner.checks.append(Check(
            "coverage floors", "skipped",
            "use --coverage to measure line coverage of the safety-critical modules", 0,
        ))
        print("[SKIP] coverage floors — use --coverage to measure them")
    if args.windows:
        runner.run("Windows integration suite", _run_windows_tests)
    else:
        runner.checks.append(Check(
            "Windows integration suite", "skipped",
            "use --windows on a Windows machine to enable hardware checks", 0,
        ))
        print("[SKIP] Windows integration suite — use --windows on a Windows machine to enable hardware checks")

    report = {
        "ok": not any(check.status == "failed" for check in runner.checks),
        "environment": _environment(),
        "checks": [asdict(check) for check in runner.checks],
        "passed": sum(check.status == "passed" for check in runner.checks),
        "failed": sum(check.status == "failed" for check in runner.checks),
        "skipped": sum(check.status == "skipped" for check in runner.checks),
    }
    if args.json:
        output = Path(args.json).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {output}")
    print(
        f"Overall result: {'PASS' if report['ok'] else 'FAIL'} "
        f"({report['passed']} passed, {report['failed']} failed, {report['skipped']} skipped)"
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
