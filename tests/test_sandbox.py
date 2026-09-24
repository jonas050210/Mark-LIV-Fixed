from __future__ import annotations

import unittest

from core.sandbox import UnsafeCode, run_generated_code


class GeneratedCodeSandboxTests(unittest.TestCase):
    def test_read_only_inspection_runs_in_child(self) -> None:
        result = run_generated_code("print(Path.home().name)", timeout=3)
        self.assertTrue(result)

    def test_imports_and_destructive_methods_are_rejected(self) -> None:
        for code in ("import os", "Path.home().unlink()", "exec('print(1)')"):
            with self.subTest(code=code):
                with self.assertRaises(UnsafeCode):
                    run_generated_code(code, timeout=3)


if __name__ == "__main__":
    unittest.main()
