#!/usr/bin/env python3
"""
check_plugins.py — regression tests for MARK LIV's drop-in plugin contract.

No Qt, no audio, no network. The point is to prove that plugin files live where
JARVIS actually scans, bad plugins cannot abort discovery, and the real bundled
plugins can be imported headlessly.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# Legacy Windows consoles cannot encode the symbols below. A test report must
# never be the thing that crashes, so match main.py's startup hardening.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.plugin_loader import discover_plugins  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CORE_NAMES = {
    "close_camera", "manage_monitor", "recall_memory", "save_memory",
    "screen_process", "shutdown_jarvis", "system_status", "undo",
}
REAL_PLUGINS = {"telegram_remote", "chat_takeover", "document_review"}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)), flush=True)


def write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def test_root_has_no_plugins() -> None:
    section("Plugin files live under plugins/")
    allowed = {"main.py", "ui.py", "setup.py"}
    offenders = []
    for path in sorted(REPO.glob("*.py")):
        if path.name in allowed or path.name.startswith("check_"):
            continue
        offenders.append(path.name)
    check("no plugin-looking .py file sits in the repo root", not offenders,
          ", ".join(offenders))


def test_discovery_contract() -> None:
    section("Discovery validates and keeps scanning")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        write(d / "aa_valid.py", """
            PLUGIN = {'name': 'good_plugin', 'description': 'Good plugin.',
                      'parameters': {'type': 'OBJECT', 'properties': {}}}
            def run(parameters):
                return 'ok'
        """)
        write(d / "bb_no_plugin.py", """
            def run(parameters):
                return 'no metadata'
        """)
        write(d / "cc_bad_name.py", """
            PLUGIN = {'name': 'bad-name', 'description': 'Bad name.',
                      'parameters': {'type': 'OBJECT', 'properties': {}}}
            def run(parameters):
                return 'bad'
        """)
        write(d / "dd_bad_params.py", """
            PLUGIN = {'name': 'bad_params', 'description': 'Bad params.',
                      'parameters': {'type': 'STRING'}}
            def run(parameters):
                return 'bad'
        """)
        write(d / "ee_missing_run.py", """
            PLUGIN = {'name': 'missing_run', 'description': 'No run.',
                      'parameters': {'type': 'OBJECT', 'properties': {}}}
        """)
        write(d / "ff_collision.py", """
            PLUGIN = {'name': 'system_status', 'description': 'Collision.',
                      'parameters': {'type': 'OBJECT', 'properties': {}}}
            def run(parameters):
                return 'bad'
        """)
        write(d / "gg_duplicate.py", """
            PLUGIN = {'name': 'good_plugin', 'description': 'Duplicate.',
                      'parameters': {'type': 'OBJECT', 'properties': {}}}
            def run(parameters):
                return 'dupe'
        """)
        write(d / "hh_raising_import.py", """
            raise RuntimeError('boom at import')
        """)
        write(d / "_helper.py", """
            raise RuntimeError('helpers are skipped, not imported')
        """)

        logs: list[str] = []
        registry = discover_plugins(d, CORE_NAMES, logger=logs.append, notify=logs.append)
        records = registry.list_for_ui()
        names = [r["name"] for r in records]
        rejected = [r for r in records if not r["valid"]]
        check("scan never raises and sees every non-helper file", len(records) == 8,
              f"records={len(records)} names={names}")
        check("valid stub loads", registry.has("good_plugin"))
        check("helper file with leading underscore is skipped", "_helper" not in names)
        check("seven bad plugin shapes are rejected", len(rejected) == 7,
              ", ".join(r["file"] for r in rejected))
        for filename, phrase in {
            "bb_no_plugin.py": "Missing PLUGIN",
            "cc_bad_name.py": "valid identifier",
            "dd_bad_params.py": "OBJECT",
            "ee_missing_run.py": "Missing callable run",
            "ff_collision.py": "collides",
            "gg_duplicate.py": "already used",
            "hh_raising_import.py": "boom at import",
        }.items():
            rec = next((r for r in records if r["file"] == filename), None)
            check(f"{filename} reports the documented error", bool(rec and phrase in rec["error"]),
                  "" if not rec else rec["error"])


def test_launch_hook() -> None:
    section("Optional launch hook")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        flag = d / "launched.txt"
        write(d / "launchy.py", f"""
            from pathlib import Path
            FLAG = Path({str(flag)!r})
            PLUGIN = {{'name': 'launchy', 'description': 'Has a launch hook.',
                       'parameters': {{'type': 'OBJECT', 'properties': {{}}}}}}
            def on_launch(player=None):
                FLAG.write_text(getattr(player, 'name', 'none'), encoding='utf-8')
            def run(parameters):
                return 'ok'
        """)
        registry = discover_plugins(d, CORE_NAMES, logger=lambda _m: None)

        class Player:
            name = "facade"

        registry.launch_enabled(Player())
        for _ in range(50):
            if flag.exists():
                break
            time.sleep(0.02)
        check("launch_enabled runs on_launch for an enabled plugin", flag.exists())
        if flag.exists():
            check("launch hook receives the UI facade", flag.read_text(encoding="utf-8") == "facade")


def _requirements_modules() -> set[str]:
    modules = set()
    req = REPO / "requirements.txt"
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        line = line.split(";", 1)[0].strip()
        line = line.split("[", 1)[0].strip()
        for sep in (">=", "<=", "==", "~=", "!=", "<", ">"):
            line = line.split(sep, 1)[0].strip()
        if line:
            modules.add(line.replace("-", "_"))
    modules.update({
        "PIL",       # pillow
        "bs4",       # beautifulsoup4
        "cv2",       # opencv-python
        "docx",      # python-docx
        "pptx",      # python-pptx
        "google",    # google-genai / google API packages
        "paho",      # paho-mqtt
        "win32com", "win32gui", "win32api", "win32con",  # pywin32
    })
    return modules


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    roots.discard("__future__")
    return roots


def test_real_plugins() -> None:
    section("Bundled plugins are headless-loadable")
    allowed_modules = set(getattr(sys, "stdlib_module_names", set()))
    allowed_modules.update(_requirements_modules())
    allowed_modules.update(p.name for p in REPO.iterdir() if p.is_dir())

    for name in sorted(REAL_PLUGINS):
        path = REPO / "plugins" / f"{name}.py"
        check(f"{name}.py exists in plugins/", path.exists())
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            check(f"{name}.py parses", True)
        except SyntaxError as e:
            check(f"{name}.py parses", False, str(e))
            continue

        bad_imports = sorted(_imported_roots(path) - allowed_modules)
        check(f"{name}.py imports only stdlib, local modules, or requirements", not bad_imports,
              ", ".join(bad_imports))

        spec = importlib.util.spec_from_file_location(f"plugins.{name}", path)
        module = importlib.util.module_from_spec(spec) if spec and spec.loader else None
        try:
            assert module is not None and spec is not None and spec.loader is not None
            sys.modules[f"plugins.{name}"] = module
            spec.loader.exec_module(module)
            check(f"plugins.{name} imports cleanly", True)
        except Exception as e:
            sys.modules.pop(f"plugins.{name}", None)
            check(f"plugins.{name} imports cleanly", False, repr(e))
            continue

        meta = getattr(module, "PLUGIN", None)
        check(f"{name} declares PLUGIN", isinstance(meta, dict))
        if isinstance(meta, dict):
            check(f"{name} has the expected plugin name", meta.get("name") == name, repr(meta.get("name")))
            check(f"{name} has a description", isinstance(meta.get("description"), str) and bool(meta.get("description", "").strip()))
            params = meta.get("parameters")
            check(f"{name} parameters are an OBJECT schema", isinstance(params, dict) and params.get("type") == "OBJECT")
        check(f"{name} declares callable run()", callable(getattr(module, "run", None)))

    logs: list[str] = []
    registry = discover_plugins(REPO / "plugins", CORE_NAMES, logger=logs.append, notify=logs.append)
    tools = sorted(d["name"] for d in registry.get_tool_declarations())
    check("headless discovery loads all three real plugins", REAL_PLUGINS.issubset(set(tools)),
          f"tools={tools}")


def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print(" MARK LIV — plugin loading and placement")
    print("=" * 72)
    test_root_has_no_plugins()
    test_discovery_contract()
    test_launch_hook()
    test_real_plugins()
    failed = [r for r in RESULTS if not r[1]]
    print("\n" + "=" * 72)
    print(f"Checks: {len(RESULTS)}  Passed: {len(RESULTS) - len(failed)}  Failed: {len(failed)}  Time: {time.time() - t0:.1f}s")
    if failed:
        print("FAILED:")
        for name, _ok, detail in failed:
            print(f"  - {name}" + (f" — {detail}" if detail else ""))
        return 1
    print("All plugin checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
