"""Diagnose the optional local wake-word installation.

Run this from the MARK LIV directory:

    python check_wake_word.py

Each native import is made in a short-lived subprocess.  If ONNX Runtime or a
compiled dependency has an access violation, the diagnostic reports it instead
of taking the assistant down too.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import subprocess
import sys
from pathlib import Path

TIMEOUT = 10


def _run_import(label: str, module: str) -> bool:
    code = (
        "import importlib, sys; "
        f"importlib.import_module({module!r}); "
        f"print({label!r} + ' import: OK')"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print(f"{label}: TIMEOUT (>{TIMEOUT}s)")
        return False

    if result.returncode == 0:
        print((result.stdout or f"{label}: import: OK").strip())
        return True

    # Windows native faults commonly arrive as 3221225477 / 0xC0000005;
    # Unix native crashes are negative signal return codes.
    if result.returncode < 0 or result.returncode in (0xC0000005, 3221225477):
        code_text = f"0x{result.returncode & 0xFFFFFFFF:08X}"
        print(f"{label}: NATIVE_CRASH ({code_text})")
    else:
        print(f"{label}: FAILED (exit {result.returncode})")
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    if detail:
        print(f"  {detail[-1][:240]}")
    return False


def main() -> int:
    print("MARK LIV wake-word diagnostic")
    print(f"Python: {sys.executable}")
    try:
        version = importlib.metadata.version("openwakeword")
        print(f"openwakeword distribution: {version}")
    except importlib.metadata.PackageNotFoundError:
        print("openwakeword distribution: not installed")

    # Importing the app helper is safe and should not import openwakeword.
    try:
        from core.wake_word import is_ready

        ready = is_ready()
        imported = "openwakeword" in sys.modules
        print(f"readiness: {'READY' if ready else 'not downloaded'}")
        print(f"GUI-safe readiness check: {'FAIL' if imported else 'OK'}")
    except Exception as exc:
        print(f"readiness check: FAILED ({exc})")
        ready = False
        imported = True

    if not importlib.util.find_spec("openwakeword"):
        print("\nInstall it from the app with ⚙ → WAKE WORD, or run:")
        print(f'  "{sys.executable}" -m pip install openwakeword')
        return 0

    print("\nIsolated dependency checks:")
    numpy_ok = _run_import("numpy", "numpy")
    ort_ok = _run_import("onnxruntime", "onnxruntime")
    oww_ok = _run_import("openwakeword", "openwakeword")

    if not (numpy_ok and ort_ok and oww_ok):
        print("\nRepair commands:")
        print(f'  "{sys.executable}" -m pip install --force-reinstall numpy')
        print(f'  "{sys.executable}" -m pip install --force-reinstall onnxruntime')
        print(f'  "{sys.executable}" -m pip install --force-reinstall openwakeword')
        return 1
    if not ready:
        print("\nThe package imports correctly but its models are missing.")
        print("Open the app settings and press WAKE WORD: DOWNLOAD.")
        return 0

    print("\nWake word dependencies and model files look ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
