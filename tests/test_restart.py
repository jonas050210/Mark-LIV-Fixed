from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.restart import launch_replacement


class RestartTests(unittest.TestCase):
    def test_source_launch_uses_interpreter_entrypoint_and_detaches(self) -> None:
        calls = []
        with TemporaryDirectory() as directory, patch("core.restart.platform.system", return_value="Linux"):
            root = Path(directory)
            entrypoint = root / "main.py"
            entrypoint.touch()
            launch_replacement(root, entrypoint, frozen=False, popen=lambda *a, **kw: calls.append((a, kw)))
        args, kwargs = calls[0]
        self.assertEqual(args[0][1], str(entrypoint.resolve()))
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["env"]["JARVIS_RESTARTED"], "1")
        self.assertEqual(kwargs["cwd"], str(root.resolve()))

    def test_frozen_launch_has_no_script_argument(self) -> None:
        calls = []
        with TemporaryDirectory() as directory, patch("core.restart.platform.system", return_value="Linux"):
            root = Path(directory)
            launch_replacement(root, root / "main.py", frozen=True, popen=lambda *a, **kw: calls.append((a, kw)))
        self.assertEqual(len(calls[0][0][0]), 1)

    def test_windows_uses_creation_flags_not_posix_session(self) -> None:
        calls = []
        with TemporaryDirectory() as directory, patch("core.restart.platform.system", return_value="Windows"), patch.object(
            __import__("core.restart", fromlist=["subprocess"]).subprocess,
            "DETACHED_PROCESS", 8, create=True,
        ), patch.object(
            __import__("core.restart", fromlist=["subprocess"]).subprocess,
            "CREATE_NEW_PROCESS_GROUP", 16, create=True,
        ):
            root = Path(directory)
            launch_replacement(root, root / "main.py", frozen=False, popen=lambda *a, **kw: calls.append((a, kw)))
        self.assertEqual(calls[0][1]["creationflags"], 24)
        self.assertNotIn("start_new_session", calls[0][1])
        self.assertNotEqual(calls[0][1]["env"], os.environ)
