"""Unified app lifecycle: resolve/launch/verify/close, honestly.

Covers core/app_controller.py, the open_app/close_app actions, the
computer_settings close interception, the app_inventory status action, and
app_finder launch-error diagnosis. Processes are faked (no real psutil
needed); the resolver/launcher are stubbed. Runnable with pytest or directly
(`python tests/test_app_lifecycle.py`).
"""
from __future__ import annotations

import sys
import time
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


# ── restart ──────────────────────────────────────────────────────────────────


def test_restart_refuses_self_and_needs_a_name():
    import actions.restart_app as ra

    assert "Which app" in ra.restart_app({})
    assert "will not restart myself" in ra.restart_app({"app_name": "jarvis"})


def test_restart_closes_then_reopens_and_verifies_both():
    import actions.restart_app as ra
    from core import tasks

    tasks.reset()
    with patch.object(ac, "is_self_name", return_value=False), \
         patch.object(ac, "is_running", return_value=True), \
         patch.object(ac, "close", return_value=(True, "Chrome is closed.")), \
         patch.object(ac, "open", return_value=(True, "Chrome is now running.")):
        out = ra.restart_app({"app_name": "Chrome"})
    assert "closed" in out and "running" in out
    row = tasks.snapshot()[0]
    assert row["state"] == "done" and "running again" in row["detail"]


def test_restart_never_starts_a_second_copy_when_close_fails():
    import actions.restart_app as ra
    from core import tasks

    tasks.reset()
    with patch.object(ac, "is_self_name", return_value=False), \
         patch.object(ac, "is_running", return_value=True), \
         patch.object(ac, "close", return_value=(False, "Spotify refused to close.")), \
         patch.object(ac, "open", return_value=(True, "opened")) as m:
        out = ra.restart_app({"app_name": "Spotify"})
    assert "did not start a second copy" in out
    m.assert_not_called()
    assert tasks.snapshot()[0]["state"] == "failed"


def test_restart_reports_an_app_that_closed_but_did_not_come_back():
    import actions.restart_app as ra
    from core import tasks

    tasks.reset()
    with patch.object(ac, "is_self_name", return_value=False), \
         patch.object(ac, "is_running", return_value=True), \
         patch.object(ac, "close", return_value=(True, "Discord is closed.")), \
         patch.object(ac, "open", return_value=(False, "Discord did not start.")):
        out = ra.restart_app({"app_name": "Discord"})
    assert "did not come back" in out
    row = tasks.snapshot()[0]
    assert row["state"] == "failed" and "would not start again" in row["detail"]


def test_restart_of_a_closed_app_starts_it_and_says_so():
    import actions.restart_app as ra
    from core import tasks

    tasks.reset()
    with patch.object(ac, "is_self_name", return_value=False), \
         patch.object(ac, "is_running", return_value=False), \
         patch.object(ac, "close", return_value=(True, "closed")) as closed, \
         patch.object(ac, "open", return_value=(True, "Discord is now running.")):
        out = ra.restart_app({"app_name": "Discord"})
    assert "was not running, so I started it" in out
    closed.assert_not_called()
    assert tasks.snapshot()[0]["detail"] == "started (it was not running)"

    # ... and when even that fails, the failure is the sentence, not a promise.
    tasks.reset()
    with patch.object(ac, "is_self_name", return_value=False), \
         patch.object(ac, "is_running", return_value=False), \
         patch.object(ac, "open", return_value=(False, "Discord did not start.")):
        out = ra.restart_app({"app_name": "Discord"})
    assert "was not running, and it did not start" in out
    assert tasks.snapshot()[0]["detail"] == "would not start"


# ── window control ───────────────────────────────────────────────────────────


def test_window_control_delegates_and_never_closes():
    import inspect

    import actions.window_control as wc

    with patch.object(ac, "window_action",
                      return_value=(True, "Brought 'Chrome' to the front.")) as m:
        out = wc.window_control({"action": "focus", "app_name": "Chrome"})
    assert "front" in out
    m.assert_called_once_with("focus", name="Chrome", title="", limit=40)

    # `query` is the older parameter name for a title hint.
    with patch.object(ac, "window_action", return_value=(True, "ok")) as m:
        wc.window_control({"action": "minimize", "query": "Spotify Premium"})
    m.assert_called_once_with("minimize", name="", title="Spotify Premium", limit=40)

    # The verb goes through untouched: the controller is the one validator.
    with patch.object(ac, "window_action", return_value=(False, "Unknown action")) as m:
        assert "Unknown" in wc.window_control({"action": "close", "title": "X"})
    assert m.call_args.args[0] == "close"

    # A controller crash is reported, not raised.
    with patch.object(ac, "window_action", side_effect=RuntimeError("boom")):
        assert "failed" in wc.window_control({"action": "list"})
    # Nothing in this tool can close a window.
    src = inspect.getsource(wc)
    assert "terminate" not in src and "kill" not in src


def test_window_action_validates_the_verb_before_choosing_a_target():
    # A bogus verb is refused as a bogus verb, even with no target given. The
    # first version of this answered "Which window?" — advice that leads nowhere,
    # because no window name makes "teleport" a real action.
    ok, message = ac.window_action("teleport")
    assert ok is False and "Unknown window action" in message
    # A real verb with no target asks for one.
    ok, message = ac.window_action("focus")
    assert ok is False and "Which window" in message
    # And no verb at all is not silently treated as focus-with-no-target.
    ok, message = ac.window_action("")
    assert ok is False and "Unknown window action" in message


# ── uninstall ────────────────────────────────────────────────────────────────


class _FakePlan:
    name = "Spotify"
    exe = "/usr/bin/spotify-uninstall"
    source = "registry"
    detail = "Vendor uninstaller"

    def command_line(self):
        return self.exe


def test_uninstall_needs_a_name_and_refuses_without_an_uninstaller():
    import actions.uninstall_app as ua

    assert "Which app" in ua.uninstall_app({})
    with patch.object(ac, "uninstall_plan",
                      return_value=(None, "I found no reliable uninstaller for 'Foo'.")):
        assert "no reliable uninstaller" in ua.uninstall_app({"app_name": "Foo"})


def test_uninstall_runs_behind_the_gate_and_verifies_with_the_resolver():
    import actions.uninstall_app as ua
    from core import confirm, tasks

    tasks.reset()
    spoken: list[str] = []
    shown: list[tuple] = []
    with patch.object(confirm, "_show_cb", lambda t, d: shown.append((t, d))), \
         patch.object(confirm, "_hide_cb", lambda: None), \
         patch.object(ac, "uninstall_plan", return_value=(_FakePlan(), "")), \
         patch.object(ac, "run_uninstall",
                      return_value=("removed", "Spotify was uninstalled.")) as ran, \
         patch("core.app_finder.resolve", return_value=None):
        pending = ua.uninstall_app({"app_name": "Spotify"}, speak=spoken.append)
        assert "[CONFIRMATION_PENDING]" in pending
        assert shown and shown[0][0] == "Uninstall Spotify"
        assert "Uninstall Spotify" in pending or "Uninstall" in shown[0][0]
        ran.assert_not_called()
        confirm.resolve(True)                       # the human pressed CONFIRM

        deadline = time.time() + 10
        while time.time() < deadline and not spoken:
            time.sleep(0.05)

    assert ran.call_count == 1
    assert spoken and "verified" in spoken[0]
    row = tasks.snapshot()[0]
    assert row["kind"] == "uninstall" and row["state"] == "done"


def test_uninstall_does_not_claim_a_removal_it_could_not_confirm():
    import actions.uninstall_app as ua
    from core import confirm, tasks

    tasks.reset()
    with patch.object(confirm, "_show_cb", lambda t, d: None), \
         patch.object(confirm, "_hide_cb", lambda: None), \
         patch.object(ac, "uninstall_plan", return_value=(_FakePlan(), "")), \
         patch.object(ac, "run_uninstall",
                      return_value=("running", "The uninstaller is open on your screen.")), \
         patch.object(confirm, "TIMEOUT_SECONDS", 30.0):
        ua.uninstall_app({"app_name": "Spotify"})
        confirm.resolve(True)
        deadline = time.time() + 10
        while time.time() < deadline and tasks.snapshot()[0]["state"] == "running":
            time.sleep(0.05)
    row = tasks.snapshot()[0]
    assert row["state"] == "done" and "not confirmed" in row["detail"]

    # A hard failure keeps the uninstaller's own words and fails the task.
    tasks.reset()
    with patch.object(confirm, "_show_cb", lambda t, d: None), \
         patch.object(confirm, "_hide_cb", lambda: None), \
         patch.object(ac, "uninstall_plan", return_value=(_FakePlan(), "")), \
         patch.object(ac, "run_uninstall",
                      return_value=("failed", "The uninstaller exited with an error.")):
        from core import confirm as cf
        with patch("core.app_finder.resolve", return_value=object()):
            ua.uninstall_app({"app_name": "Spotify"})
            cf.resolve(True)
            deadline = time.time() + 10
            while time.time() < deadline and tasks.snapshot()[0]["state"] == "running":
                time.sleep(0.05)
    row = tasks.snapshot()[0]
    assert row["state"] == "failed" and "error" in row["error"].lower()


def test_uninstall_is_fail_closed_without_an_interface():
    import actions.uninstall_app as ua
    from core import confirm

    with patch.object(confirm, "_show_cb", None), \
         patch.object(ac, "uninstall_plan", return_value=(_FakePlan(), "")), \
         patch.object(ac, "run_uninstall", return_value=("removed", "gone")) as ran:
        out = ua.uninstall_app({"app_name": "Spotify"})
    assert "not available" in out
    ran.assert_not_called()


# ── the verification layer knows the new tools ───────────────────────────────


def test_verify_rechecks_uninstall_through_the_resolver():
    from core import verify as v

    with patch("core.app_finder.resolve", return_value=None):
        verdict = v.verify("uninstall_app", {"app_name": "Spotify"}, "gone")
    assert verdict.ok is True and verdict.method == "recheck"

    with patch("core.app_finder.resolve", return_value=object()), \
         patch.object(ac, "is_running", return_value=True):
        verdict = v.verify("uninstall_app", {"app_name": "Spotify"},
                           "Spotify was uninstalled.")
    assert verdict.ok is False and "still running" in verdict.detail


def test_verify_rechecks_restart_and_window_list():
    from core import verify as v

    with patch.object(ac, "is_running", return_value=True):
        assert v.verify("restart_app", {"app_name": "Chrome"}, "restarted").ok is True
    with patch.object(ac, "is_running", return_value=False):
        assert v.verify("restart_app", {"app_name": "Chrome"}, "restarted").ok is False
    # No process access: no strong check, the tool's own words are all there is.
    with patch.object(ac, "is_running", return_value=None):
        assert v.verify("restart_app", {"app_name": "Chrome"}, "restarted").method == "text"

    with patch.object(ac, "list_windows", return_value=(["Chrome", "Spotify"], "")):
        verdict = v.verify("window_control", {"action": "list"}, "2 windows")
    assert verdict.ok is True and verdict.evidence["windows"] == 2
    with patch.object(ac, "list_windows", return_value=([], "")):
        assert v.verify("window_control", {"action": "list"}, "none").ok is False
    # focus/minimize cannot be re-checked cheaply -> text analysis, not a guess.
    with patch.object(ac, "list_windows", return_value=(["Chrome"], "")):
        verdict = v.verify("window_control", {"action": "focus", "app_name": "Chrome"},
                           "Brought 'Chrome' to the front.")
    assert verdict.method == "text" and verdict.ok is True


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
