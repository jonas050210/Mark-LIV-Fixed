"""Run the unit suite and expose concise failures in GitHub Actions."""

from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import unittest

_MAX_DIAGNOSTIC_CHARS = 6_000
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _workflow_escape(value: str) -> str:
    """Escape untrusted text for a GitHub workflow command."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _failed_tests(result: unittest.TestResult) -> list[tuple[str, str, str]]:
    failures: list[tuple[str, str, str]] = []
    for kind, entries in (("Failure", result.failures), ("Error", result.errors)):
        for test, traceback in entries:
            failures.append((kind, test.id(), traceback[-_MAX_DIAGNOSTIC_CHARS:]))
    return failures


def _write_step_summary(failures: list[tuple[str, str, str]]) -> None:
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination or not failures:
        return

    with Path(destination).open("a", encoding="utf-8", newline="\n") as summary:
        summary.write("## Unit-test failures\n\n")
        for kind, test_id, traceback in failures:
            summary.write(f"### {kind}: `{test_id}`\n\n```text\n")
            summary.write(traceback)
            if not traceback.endswith("\n"):
                summary.write("\n")
            summary.write("```\n\n")


def main() -> int:
    os.chdir(_REPOSITORY_ROOT)
    sys.path.insert(0, str(_REPOSITORY_ROOT))
    suite = unittest.defaultTestLoader.discover("tests")
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    sys.stdout.write(output.getvalue())

    failures = _failed_tests(result)
    _write_step_summary(failures)
    for kind, test_id, traceback in failures:
        print(
            f"::error title={_workflow_escape(kind + ': ' + test_id)}::"
            f"{_workflow_escape(traceback)}"
        )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
