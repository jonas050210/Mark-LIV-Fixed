"""Setup orchestration regressions: no downloads or native browser dependencies."""
import io
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import setup as S


def test_default_setup_never_reads_console_and_verifies_all(monkeypatch, capsys):
    class Console(io.StringIO):
        def isatty(self):
            return True

        def readline(self, *args):
            raise AssertionError("setup must not read console input")

    monkeypatch.setattr(sys, "stdin", Console())
    monkeypatch.delenv(S.ENV_BROWSERS, raising=False)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("prompt")))
    packages = Mock(return_value=True)
    browsers = Mock(return_value=True)
    verify = Mock(return_value=True)
    monkeypatch.setattr(S, "_auto_install", packages)
    monkeypatch.setattr(S, "_install_browsers", browsers)
    monkeypatch.setattr(S, "_verify_requirements", verify)
    monkeypatch.setattr(S, "_check_assets", lambda: None)
    assert S.main([]) == 0
    packages.assert_called_once()
    browsers.assert_called_once_with(["chromium", "firefox", "webkit"])
    verify.assert_called_once()
    assert "\nREADY —" in capsys.readouterr().out


def test_browser_failure_cannot_report_ready(monkeypatch, capsys):
    monkeypatch.delenv(S.ENV_BROWSERS, raising=False)
    monkeypatch.setattr(S, "_auto_install", lambda path: True)
    monkeypatch.setattr(S, "_install_browsers", lambda names: False)
    assert S.main([]) == 1
    output = capsys.readouterr().out
    assert "NOT READY" in output
    assert "\nREADY —" not in output


def test_dry_run_is_noninteractive_and_has_no_install_side_effects(monkeypatch, capsys):
    monkeypatch.delenv(S.ENV_BROWSERS, raising=False)
    forbidden = Mock(side_effect=AssertionError("dry run started an installation"))
    for name in ("_run", "_auto_install", "_install_browsers"):
        monkeypatch.setattr(S, name, forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    assert S.main(["--dry-run"]) == 0
    assert "playwright install chromium firefox webkit" in capsys.readouterr().out


def test_browser_probe_uses_playwright_paths_and_closed_stdin(monkeypatch, tmp_path):
    location = tmp_path / "cache with spaces" / "webkit-123"
    run = Mock(return_value=SimpleNamespace(stdout=f"WebKit\n  Install location: {location}\n"))
    monkeypatch.setattr(S.subprocess, "run", run)
    assert S._browser_locations("webkit") == [location]
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert run.call_args.kwargs["timeout"] == 30
    assert run.call_args.args[0][-2:] == ["--dry-run", "webkit"]


def test_browser_downloads_skip_complete_engines_and_verify_every_engine(monkeypatch, tmp_path):
    paths = {}
    for browser in ("chromium", "firefox", "webkit"):
        folder = tmp_path / browser
        folder.mkdir()
        paths[browser] = folder
    (paths["chromium"] / "INSTALLATION_COMPLETE").touch()
    monkeypatch.setattr(S, "_browser_locations", lambda name: [paths[name]])
    installs = Mock()
    launches = Mock(return_value=SimpleNamespace(returncode=0, timed_out=False))
    monkeypatch.setattr(S, "_run", installs)
    monkeypatch.setattr(S, "run_live", launches)
    assert S._install_browsers(list(paths))
    assert [call.args[1][-1] for call in installs.call_args_list] == ["firefox", "webkit"]
    assert [call.args[0][-1] for call in launches.call_args_list] == list(paths)


def test_broken_browser_launch_is_failure_even_when_download_present(monkeypatch, tmp_path):
    (tmp_path / "INSTALLATION_COMPLETE").touch()
    monkeypatch.setattr(S, "_browser_locations", lambda name: [tmp_path])
    monkeypatch.setattr(S, "_run", Mock(side_effect=AssertionError("must skip download")))
    monkeypatch.setattr(S, "run_live", lambda *a, **k: SimpleNamespace(returncode=1, timed_out=False))
    assert not S._install_browsers(["chromium"])


def test_failed_download_does_not_prevent_attempting_other_engines(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_browser_locations", lambda name: [tmp_path / name])
    install = Mock(side_effect=subprocess.CalledProcessError(1, "playwright"))
    monkeypatch.setattr(S, "_run", install)
    assert not S._install_browsers(["chromium", "firefox", "webkit"])
    assert install.call_count == 3


def test_satisfied_packages_do_not_invoke_pip(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_run", Mock(side_effect=AssertionError("unexpected pip")))
    assert S._install_requirements([], tmp_path / "requirements.txt")


def test_entry_point_imports_pinned(monkeypatch, capsys):
    """Regression pin: "NameError: name 'argparse' is not defined".

    A fresh "py setup.py" must reach its plan line with argparse and the live
    runner import intact. If a future edit drops "import argparse" (the
    reported regression), these asserts fail the suite instead of shipping a
    setup script that crashes before it can explain itself.
    """
    assert isinstance(getattr(S, "argparse", None), types.ModuleType), \
        "setup.argparse is missing — build_parser() would NameError at startup"
    assert getattr(S, "run_live", None) is not None, \
        "setup.run_live is missing — _run() would fail during the install steps"
    parser = S.build_parser()
    assert parser.parse_args([]).browsers is None
    assert parser.parse_args(["--yes"]).yes is True
    assert parser.parse_args(["--browsers", "firefox"]).browsers == "firefox"


def test_main_completes_with_dead_stdin(monkeypatch, capsys):
    """Zero user input: main() must finish with a console it cannot read.

    stdin is replaced with a stream that raises on any read and input() is
    rigged to fail — if any code path asks a question, this test goes red.
    """

    class DeadStdin(io.StringIO):
        def read(self, *args):
            raise AssertionError("setup read stdin")
        def readline(self, *args):
            raise AssertionError("setup read stdin")
        def __iter__(self):
            raise AssertionError("setup iterated stdin")

    monkeypatch.setattr(sys, "stdin", DeadStdin())
    monkeypatch.delenv(S.ENV_BROWSERS, raising=False)
    monkeypatch.setattr("builtins.input", Mock(side_effect=AssertionError("setup prompted")))
    assert S.main(["--dry-run"]) == 0


def test_missing_core_module_fails_with_verdict_not_traceback(tmp_path):
    """A partial clone (no core/) must say NOT READY, not dump a traceback."""
    target = tmp_path / "setup.py"
    shutil.copy(REPO / "setup.py", target)
    proc = subprocess.run([sys.executable, str(target)], cwd=tmp_path,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "NOT READY" in out
    assert "core.command_runner" in out
    assert "Traceback (most recent call last)" not in out
