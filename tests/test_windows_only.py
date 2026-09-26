from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import setup

ROOT = Path(__file__).resolve().parents[1]


class WindowsOnlyRuntimeTests(unittest.TestCase):
    def test_direct_main_launch_refuses_non_windows_before_optional_imports(self) -> None:
        if sys.platform == "win32":
            self.skipTest("the refusal guard only applies to non-Windows hosts")
        completed = subprocess.run(
            [sys.executable, "main.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("supports Windows 10 and Windows 11 only", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_normal_setup_refuses_non_windows_before_pip(self) -> None:
        with patch.object(setup, "OS", "Linux"), patch.object(
            setup, "CHECK_ONLY", False
        ), patch.object(setup, "_run") as run:
            with self.assertRaises(SystemExit) as caught:
                setup.main()
        self.assertEqual(caught.exception.code, 1)
        run.assert_not_called()

    def test_check_mode_remains_portable(self) -> None:
        with patch.object(setup, "OS", "Linux"), patch.object(
            setup, "CHECK_ONLY", True
        ), patch.object(setup, "_check_install_inputs") as check:
            setup.main()
        check.assert_called_once_with()
