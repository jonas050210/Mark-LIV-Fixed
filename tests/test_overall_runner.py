"""Real subprocess tests for live output, closed stdin and bounded cleanup."""
import io
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import test_overall as overall
from core import command_runner as runner


def test_partial_stdout_and_stderr_are_visible_before_exit(monkeypatch, tmp_path):
    # The child cannot finish until the parent has displayed BOTH partial lines.
    # A read-until-newline or communicate()-then-print implementation deadlocks.
    gate = tmp_path / "output_seen"

    class Display(io.StringIO):
        def write(self, text):
            count = super().write(text)
            if "stdout-partial" in self.getvalue() and "stderr-partial" in self.getvalue():
                gate.touch()
            return count

    display = Display()
    monkeypatch.setattr(sys, "stdout", display)
    script = (
        "import sys,time; from pathlib import Path; "
        "sys.stdout.write('stdout-partial'); sys.stdout.flush(); "
        "sys.stderr.write('stderr-partial'); sys.stderr.flush(); "
        "gate=Path(sys.argv[1]);\n"
        "while not gate.exists(): time.sleep(.02)\n"
        "assert sys.stdin.read() == ''; print('EOF verified')"
    )
    result = runner.run_live([sys.executable, "-u", "-c", script, str(gate)],
                             label="stream test", timeout=5)
    assert result.returncode == 0 and not result.timed_out
    assert "stdout-partial" in result.stdout
    assert result.stderr == "stderr-partial"
    assert "EOF verified" in display.getvalue()


def test_silent_suite_emits_heartbeat_and_times_out(capsys):
    result = runner.run_live([sys.executable, "-c", "import time; time.sleep(60)"],
                             label="silent suite", timeout=0.5, heartbeat=0.1)
    output = capsys.readouterr().out
    assert result.timed_out and result.returncode != 0
    assert result.seconds < 8
    assert "[RUNNING] silent suite" in output
    assert "elapsed" in output
    assert "[TIMEOUT] silent suite" in output


def test_timeout_terminates_descendants_with_inherited_pipes(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
    )
    result = runner.run_live([sys.executable, "-u", "-c", script, str(pid_file)],
                             label="process tree", timeout=1, heartbeat=0.2)
    assert result.timed_out and result.seconds < 8
    import psutil
    pid = int(pid_file.read_text())
    try:
        child = psutil.Process(pid)
        assert child.status() == psutil.STATUS_ZOMBIE or not child.is_running()
    except psutil.NoSuchProcess:
        pass


def test_windows_cleanup_uses_bounded_taskkill_tree(monkeypatch):
    # Exercise the Windows branch on any CI host without running taskkill here.
    run = Mock()
    proc = Mock(pid=123)
    proc.poll.return_value = None
    with monkeypatch.context() as patch:
        patch.setattr(runner.os, "name", "nt")
        patch.setattr(runner.subprocess, "run", run)
        runner._terminate_tree(proc)
    assert run.call_args.args[0] == ["taskkill", "/PID", "123", "/T", "/F"]
    assert run.call_args.kwargs["timeout"] == 5
    proc.kill.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)


def test_timeout_status_and_report_continue_to_next_suite(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["label"])
        timeout = len(calls) == 1
        return SimpleNamespace(returncode=-9 if timeout else 0, seconds=0.1,
                               stdout="[PASS] finished check\n", stderr="", timed_out=timeout)

    monkeypatch.setattr(overall, "run_live", fake_run)
    monkeypatch.setattr(overall, "SUITE_ORDER", [("setup_autodetect", "setup"),
                                                ("config_manager", "config")])
    report = tmp_path / "report.txt"
    assert overall.main(["--report", str(report)]) == 1
    assert calls == ["setup_autodetect", "config_manager"]
    output = capsys.readouterr().out
    assert "[TIMEOUT] setup_autodetect" in output
    assert "[PASS] config_manager" in output
    assert "TIMEOUT" in report.read_text()
    assert "Failed suites: 1" in report.read_text()


def test_setup_autodetect_completes_with_stdin_pipe_left_open():
    # In particular do not send an EOF to release a mistakenly invoked input().
    with subprocess.Popen([sys.executable, "-u", str(REPO / "test_overall.py"),
                           "--_run-suite", "setup_autodetect"], cwd=REPO,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("setup_autodetect waited for stdin")
        assert proc.returncode == 0, proc.stderr.read().decode(errors="replace")
        assert b"[PASS] --dry-run" in proc.stdout.read()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_timeout_override_rejects_unbounded_values(value):
    with pytest.raises(SystemExit) as error:
        overall.main(["--timeout", value])
    assert error.value.code == 2


def test_compile_scan_skips_environment_and_caches(monkeypatch, tmp_path):
    for name in ("app.py", ".venv/package.py", "venv/package.py", "__pycache__/noise.py",
                 "core/useful.py", ".git/noise.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(overall, "REPO", tmp_path)
    assert sorted(overall._python_sources()) == ["app.py", str(Path("core/useful.py"))]
