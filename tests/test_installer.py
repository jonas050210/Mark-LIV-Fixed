from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import installer


def _result(code: int = 0, timed_out: bool = False):
    return SimpleNamespace(returncode=code, timed_out=timed_out)


class InstallerTests(unittest.TestCase):
    def test_available_uses_module_discovery_without_importing(self) -> None:
        with patch("core.installer.importlib.util.find_spec", return_value=object()) as find:
            self.assertTrue(installer._available("example"))
        find.assert_called_once_with("example")

    def test_pip_reports_success_and_failure_without_shell(self) -> None:
        messages = []
        with patch("core.installer.run_bounded", return_value=_result()) as run:
            self.assertTrue(installer._pip("safe-package", messages.append))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [installer.sys.executable, "-m", "pip", "install"])
        self.assertIn("safe-package", argv)

        with patch("core.installer.run_bounded", return_value=_result(1)):
            self.assertFalse(installer._pip("broken", messages.append))
        self.assertTrue(any("exit code 1" in message for message in messages))

        with patch("core.installer.run_bounded", return_value=_result(0, True)):
            self.assertFalse(installer._pip("slow", messages.append))
        self.assertTrue(any("timed out" in message for message in messages))

    def test_no_missing_dependencies_does_not_install(self) -> None:
        messages = []
        with patch.object(installer, "_available", return_value=True), patch.object(
            installer, "_pip"
        ) as pip, patch("core.installer.platform.system", return_value="Linux"):
            installer.install_for_config(
                {"stt_engine": "whisper", "tts_engine": "edgetts"}, messages.append
            )
        pip.assert_not_called()
        self.assertTrue(any("already installed" in message for message in messages))

    def test_missing_packages_are_deduplicated_and_installed(self) -> None:
        installed = []
        with patch.object(installer, "_CORE", [("one", "same"), ("two", "same")]), patch.object(
            installer, "_STT", {"none": []}
        ), patch.object(installer, "_TTS", {"none": []}), patch.object(
            installer, "_available", side_effect=lambda module: module == "playwright"
        ), patch.object(
            installer, "_pip", side_effect=lambda package, _log=None: installed.append(package) or True
        ), patch("core.installer.platform.system", return_value="Linux"):
            installer.install_for_config({"stt_engine": "none", "tts_engine": "none"})
        self.assertEqual(installed, ["same"])

    def test_windows_dependencies_and_playwright_browser_are_installed(self) -> None:
        messages = []
        installed = []
        with patch.object(installer, "_CORE", []), patch.object(
            installer, "_WINDOWS", [("winmod", "win-package")]
        ), patch.object(installer, "_STT", {"none": []}), patch.object(
            installer, "_TTS", {"none": []}
        ), patch.object(installer, "_available", return_value=False), patch.object(
            installer, "_pip", side_effect=lambda package, _log=None: installed.append(package) or True
        ), patch("core.installer.platform.system", return_value="Windows"), patch(
            "core.installer.run_bounded", return_value=_result()
        ) as run:
            installer.install_for_config(
                {"stt_engine": "none", "tts_engine": "none"}, messages.append
            )
        self.assertEqual(installed, ["win-package", "playwright"])
        self.assertIn("playwright", run.call_args.args[0])
        self.assertTrue(any("browser ready" in message for message in messages))

    def test_failed_browser_install_is_reported(self) -> None:
        messages = []
        with patch.object(installer, "_CORE", [("coremod", "core-package")]), patch.object(
            installer, "_STT", {"none": []}
        ), patch.object(installer, "_TTS", {"none": []}), patch.object(
            installer, "_available", return_value=False
        ), patch.object(installer, "_pip", return_value=True), patch(
            "core.installer.platform.system", return_value="Linux"
        ), patch("core.installer.run_bounded", return_value=_result(1)):
            installer.install_for_config(
                {"stt_engine": "none", "tts_engine": "none"}, messages.append
            )
        self.assertTrue(any("did not complete" in message for message in messages))
