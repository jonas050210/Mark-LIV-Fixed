"""Setup orchestration regressions: no downloads or native browser dependencies."""
import io
import subprocess
import sys
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
