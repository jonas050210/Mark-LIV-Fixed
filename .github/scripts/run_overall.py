"""Run overall verification and expose failed gates in GitHub Actions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_MAX_DIAGNOSTIC_CHARS = 6_000


def _workflow_escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _failed_checks(report: object) -> list[dict]:
    if not isinstance(report, dict):
        return []
    checks = report.get("checks", [])
    if not isinstance(checks, list):
        return []
    return [
        check for check in checks
        if isinstance(check, dict) and check.get("status") == "failed"
    ]


def _write_step_summary(failures: list[dict]) -> None:
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination or not failures:
        return
    with Path(destination).open("a", encoding="utf-8", newline="\n") as summary:
        summary.write("## Overall-verification failures\n\n")
        for check in failures:
            summary.write(f"### `{check.get('name', 'unknown check')}`\n\n```text\n")
            summary.write(str(check.get("detail", "No diagnostic was reported.")))
            summary.write("\n```\n\n")


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        report_path = Path(directory) / "overall.json"
        completed = subprocess.run(
            [sys.executable, "test_overall.py", "--json", str(report_path)],
            cwd=_REPOSITORY_ROOT,
            check=False,
        )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}

    failures = _failed_checks(report)
    _write_step_summary(failures)
    for check in failures:
        name = str(check.get("name", "unknown check"))
        detail = str(check.get("detail", "No diagnostic was reported."))
        print(
            f"::error title={_workflow_escape('Overall check: ' + name)}::"
            f"{_workflow_escape(detail[-_MAX_DIAGNOSTIC_CHARS:])}"
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
