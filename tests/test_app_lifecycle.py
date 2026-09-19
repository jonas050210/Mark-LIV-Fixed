"""Unified app lifecycle: resolve/launch/verify/close, honestly.

Covers core/app_controller.py, the open_app/close_app actions, the
computer_settings close interception, the app_inventory status action, and
app_finder launch-error diagnosis. Processes are faked (no real psutil
needed); the resolver/launcher are stubbed. Runnable with pytest or directly
(`python tests/test_app_lifecycle.py`).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import app_controller as ac  # noqa: E402
from core import app_finder as af  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeIterProc:
    def __init__(self, pid, name, exe="", cmdline=()):
        self.info = {"pid": pid, "name": name, "exe": exe, "cmdline": list(cmdline)}


class FakeProcess:
    """Minimal psutil.Process double with controllable liveness."""

    def __init__(self, pid, name, alive_container):
        self._pid = pid
        self._name = name
        self._alive = alive_container
        self.terminated = False
        self.killed = False

    def name(self):
        return self._name

    def terminate(self):
        self.terminated = True
        self._alive.discard(self._pid)

    def kill(self):
        self.killed = True
        self._alive.discard(self._pid)


class FakePsutil:
    def __init__(self, procs):
        # procs: list of (pid, name, cmdline-tuple)
        self._procs = list(procs)
        self.alive = {p[0] for p in procs}

    def process_iter(self, attrs=None):
        return [FakeIterProc(pid, name, "", cmd) for pid, name, cmd in self._procs
                if pid in self.alive]

    def Process(self, pid):
        for p_pid, p_name, _ in self._procs:
            if p_pid == pid and pid in self.alive:
                return FakeProcess(pid, p_name, self.alive)
        raise RuntimeError("NoSuchProcess")

    @staticmethod
    def wait_procs(procs, timeout=None):
        gone, alive_now = [], []
        for p in procs:
            (gone if p._pid not in p._alive else alive_now).append(p)
        return gone, alive_now


def _install_fake_psutil(procs):
    fake = FakePsutil(procs)
    return patch.dict(sys.modules, {"psutil": fake}), fake


def _target(display="Spotify", path="/usr/bin/spotify"):
    return SimpleNamespace(display_name=display, display=display,
                           path=path, kind="exe")


# ── process-name resolution ──────────────────────────────────────────────────


def test_process_names_prefer_resolved_target():
    names = ac.process_names_for("Spotify", _target(path="C:/X/Spotify.exe"))
    assert names[0] == "spotify.exe"


def test_process_names_fall_back_to_hints():
    assert "spotify.exe" in ac.process_names_for("spotify")
    assert "robloxplayerbeta.exe" in ac.process_names_for("roblox")
    assert "geometrydash.exe" in ac.process_names_for("gd")


def test_is_running_matches_basename_only():
    patcher, _ = _install_fake_psutil([
        (1111, "spotify.exe", ("C:/X/spotify.exe",)),
        (2222, "notepad.exe", ("spotify-playlist.txt",)),  # cmdline decoy
    ])
    with patcher, patch.object(ac, "_resolve", return_value=_target()):
        assert ac.is_running("Spotify") is True
        # The decoy must not match: basename-only, never cmdline substrings.
        assert ac.is_running("playlist") is False


def test_is_running_ignores_own_processes():
    import os

    patcher, _ = _install_fake_psutil([
        (os.getpid(), "spotify.exe", ("spotify.exe",)),  # own pid, same name
        (9999, "python.exe", ("python.exe", "jarvis-main.py")),  # sibling
    ])
    with patcher, patch.object(ac, "_resolve", return_value=_target()):
        assert ac.is_running("Spotify") is False
        assert ac.is_running("jarvis") is False


def test_is_running_unknown_without_psutil():
    with patch.dict(sys.modules, {"psutil": None}):
        # None in sys.modules makes `import psutil` raise ImportError.
        with patch.object(ac, "_resolve", return_value=_target()):
            assert ac.is_running("Spotify") is None


# ── open ─────────────────────────────────────────────────────────────────────


def test_open_refuses_install_intent_in_both_languages():
    for text in ["install Spotify", "Spotify installieren", "update Chrome",
                 "Chrome updaten", "repair Discord", "Discord reparieren",
                 "download VLC", "uninstall Teams", "Teams deinstallieren"]:
        ok, msg = ac.open(text)
        assert ok is False, text
        assert "do not install" in msg, text


def test_open_refuses_installer_like_targets():
    tgt = _target(display="Setup", path="C:/DL/SpotifySetup.exe")
    with patch.object(ac, "_resolve", return_value=tgt):
        ok, msg = ac.open("Spotify")
        assert ok is False
        assert "installer" in msg.lower()


def test_open_not_found_says_so_and_never_installs():
    with patch.object(ac, "_resolve", return_value=None):
        ok, msg = ac.open("NoSuchAppXYZ")
        assert ok is False
        assert "could not find" in msg
        assert "do not install" in msg


def test_open_launches_and_verifies_running():
    with patch.object(ac, "_resolve", return_value=_target()), \
         patch.object(af, "launch", return_value=(True, "Opened.")), \
         patch.object(ac, "is_running", return_value=True):
        ok, msg = ac.open("Spotify", verify_seconds=0.1)
        assert ok is True
        assert "running" in msg


def test_open_reports_launch_failure_honestly():
    def _boom(_tgt):
        raise OSError("nope")

    with patch.object(ac, "_resolve", return_value=_target()), \
         patch.object(af, "launch", side_effect=_boom):
        ok, msg = ac.open("Spotify", verify_seconds=0.1)
        assert ok is False
        assert "Could not start" in msg


def test_open_reports_started_but_not_running():
    with patch.object(ac, "_resolve", return_value=_target()), \
         patch.object(af, "launch", return_value=(True, "Opened.")), \
         patch.object(ac, "is_running", return_value=False):
        ok, msg = ac.open("Spotify", verify_seconds=0.1)
        assert ok is False
        assert "did not appear to start" in msg


# ── close ────────────────────────────────────────────────────────────────────


def test_status_reads_display_and_target_attrs():
    # Real LaunchTargets carry display/target (not display_name/path).
    tgt = SimpleNamespace(display="Google Chrome",
                          target="/opt/google/chrome/chrome", kind="exe")
    patcher, _ = _install_fake_psutil([(9, "chrome", ("chrome",))])
    with patcher, patch.object(ac, "_resolve", return_value=tgt):
        st = ac.status("chrome")
    assert st.installed is True
    assert st.display_name == "Google Chrome"
    assert st.install_path == "/opt/google/chrome/chrome"
    assert st.running is True


def test_find_processes_drops_runtime_hints_when_target_known():
    patcher, _ = _install_fake_psutil([
        (21, "javaw.exe", ("javaw.exe",)),
        (22, "minecraft.exe", ("minecraft.exe",)),
    ])
    tgt = SimpleNamespace(display="Minecraft",
                          target="C:/MC/minecraft.exe", kind="exe")
    with patcher:
        known, _ = ac.find_processes("minecraft", tgt)
        assert {m["name"] for m in known} == {"minecraft.exe"}
        blind, _ = ac.find_processes("minecraft", None)
        assert {m["name"] for m in blind} == {"javaw.exe", "minecraft.exe"}


def test_close_refuses_shared_runtime():
    # Killing every java.exe to close Minecraft would be uncontrolled
    # termination - refuse honestly instead.
    patcher, _ = _install_fake_psutil([(21, "javaw.exe", ("javaw.exe",))])
    with patcher, patch.object(ac, "_resolve", return_value=None):
        ok, msg = ac.close("minecraft")
    assert ok is False and "cannot safely close" in msg


def test_open_does_not_stall_without_process_access():
    import time as _t

    tgt = _target()
    with patch.object(ac, "_resolve", return_value=tgt), \
            patch.object(af, "launch", return_value=(True, "started")), \
            patch.object(ac, "is_running", return_value=None):
        start = _t.monotonic()
        ok, msg = ac.open("Spotify", verify_seconds=6.0)
        elapsed = _t.monotonic() - start
    assert ok is True and "cannot verify" in msg
    assert elapsed < 5.0, f"stalled {elapsed:.1f}s with no process access"


def test_close_refuses_empty_self_and_install_intent():
    ok, _ = ac.close("")
    assert ok is False
    for name in ["jarvis", "JARVIS", "mark", "mark liv", "yourself"]:
        ok, msg = ac.close(name)
        assert ok is False, name
        assert "myself" in msg, name
    ok, msg = ac.close("install Spotify")
    assert ok is False and "do not install" in msg


def test_close_refuses_system_processes():
    patcher, _ = _install_fake_psutil([(4, "explorer.exe", ("explorer.exe",))])
    with patcher, patch.object(ac, "_resolve", return_value=_target("Explorer", "C:/W/explorer.exe")):
        ok, msg = ac.close("explorer")
        assert ok is False
        assert "system" in msg.lower()


def test_close_not_running_is_honest():
    patcher, _ = _install_fake_psutil([(1111, "other.exe", ("other.exe",))])
    with patcher, patch.object(ac, "_resolve", return_value=_target()):
        ok, msg = ac.close("Spotify")
        assert ok is False
        assert "not running" in msg


def test_close_terminates_and_verifies_gone():
    patcher, fake = _install_fake_psutil([(1111, "spotify.exe", ("spotify.exe",))])
    with patcher, patch.object(ac, "_resolve", return_value=_target()):
        ok, msg = ac.close("Spotify", timeout=1.0)
        assert ok is True, msg
        assert "closed" in msg
        assert 1111 not in fake.alive


def test_close_skips_own_pid_but_kills_the_rest():
    import os

    patcher, fake = _install_fake_psutil([
        (os.getpid(), "spotify.exe", ("spotify.exe",)),
        (1111, "spotify.exe", ("spotify.exe",)),
    ])
    with patcher, patch.object(ac, "_resolve", return_value=_target()):
        ok, _ = ac.close("Spotify", timeout=1.0)
        assert ok is True
        assert os.getpid() in fake.alive  # self survives
        assert 1111 not in fake.alive


# ── status ───────────────────────────────────────────────────────────────────


def test_status_reports_installed_and_running():
    with patch.object(ac, "_resolve", return_value=_target()), \
         patch.object(ac, "is_running", return_value=True), \
         patch.object(ac, "find_processes", return_value=(
             [{"pid": 1111, "name": "spotify.exe", "exe": "", "cmdline": ""}], "")):
        st = ac.status("Spotify")
        assert st.installed is True and st.running is True
        assert st.pids == [1111]
        assert "RUNNING" in st.short()


def test_status_marks_unknown_without_process_access():
    with patch.object(ac, "_resolve", return_value=_target()), \
         patch.object(ac, "is_running", return_value=None):
        st = ac.status("Spotify")
        assert st.installed is True and st.running_unknown is True
        assert "unknown" in st.short()


# ── launch-error diagnosis ───────────────────────────────────────────────────


def test_diagnose_winerror_1223_and_friends():
    e = OSError("cancelled")
    e.winerror = 1223
    assert "permission prompt" in af.diagnose_launch_error(e).lower()
    e.winerror = 740
    assert "administrator" in af.diagnose_launch_error(e).lower()
    e.winerror = 2
    assert "no longer" in af.diagnose_launch_error(e).lower()
    # Never raises, even on garbage.
    assert isinstance(af.diagnose_launch_error(ValueError("x")), str)
    assert isinstance(af.diagnose_launch_error(None), str)


def test_looks_like_installer():
    assert ac.looks_like_installer("C:/DL/SpotifySetup.exe") is True
    assert ac.looks_like_installer("C:/DL/update.exe") is True
    assert ac.looks_like_installer("C:/X/Spotify.exe") is False
    assert ac.looks_like_installer("") is False


# ── action handlers ──────────────────────────────────────────────────────────


def test_open_app_action_uses_controller_and_refuses_installers():
    import actions.open_app as oa

    with patch.object(ac, "open", return_value=(True, "Spotify is now running.")) as m:
        out = oa.open_app({"app_name": "install Spotify"})
        assert "do not install" in out
        m.assert_not_called()
    fake_af = SimpleNamespace(
        resolve=lambda name: _target(), canonical_name=lambda s: s,
        suggest=lambda s: [])
    with patch.object(oa, "_af", fake_af), \
         patch.object(ac, "open", return_value=(True, "Spotify is now running.")):
        assert "running" in oa.open_app({"app_name": "Spotify"})


def test_close_app_action_delegates():
    import actions.close_app as ca

    with patch.object(ac, "close", return_value=(True, "Spotify is closed.")):
        assert "closed" in ca.close_app({"app_name": "Spotify"})
    assert "No application name" in ca.close_app({})


def test_computer_settings_close_routes_to_controller():
    import actions.computer_settings as cs

    with patch.object(cs, "_PYAUTOGUI", True), \
         patch.object(ac, "close", return_value=(True, "Spotify is closed.")) as m:
        out = cs.computer_settings({"action": "close_app", "value": "Spotify"})
        assert "closed" in out
        m.assert_called_once_with("Spotify")
    # No Alt+F4 fallback for unnamed closes: ask instead.
    with patch.object(cs, "_PYAUTOGUI", True):
        out = cs.computer_settings({"action": "close_window"})
        assert "which app" in out.lower()
    # The hotkey functions are gone for good.
    assert not hasattr(cs, "close_app") or not callable(
        getattr(cs, "close_app", None))
    assert "close_app" not in cs.ACTION_MAP
    assert "close_window" not in cs.ACTION_MAP


def test_close_target_extraction_never_guesses():
    import actions.computer_settings as cs

    assert cs._close_target_from_text("close Spotify") == "Spotify"
    assert cs._close_target_from_text("bitte schließe Discord") == "Discord"
    assert cs._close_target_from_text("close it") == ""
    assert cs._close_target_from_text("close this window") == ""
    assert cs._close_target_from_text("hello") == ""


def test_inventory_status_action():
    import actions.app_inventory as inv

    st = ac.AppStatus(name="Spotify", display_name="Spotify", installed=True,
                      install_path="C:/X", running=True, pids=[1])
    with patch.object(ac, "status", return_value=st):
        out = inv.app_inventory({"action": "status", "query": "Spotify"})
        assert "RUNNING" in out
    assert "which app" in inv.app_inventory({"action": "status"}).lower()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {e!r}")
        else:
            print(f"  [PASS] {fn.__name__}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
