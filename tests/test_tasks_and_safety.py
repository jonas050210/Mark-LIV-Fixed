"""The central activity registry, the install gate, and what reaches the HUD.

Covers core/tasks.py (the one place downloads/timers/installs/agent runs report
to), core/install_safety.py (the one gate every install path goes through),
actions/timer.py's feed into it, actions/system_monitor.py's status rendering,
and two regressions that are easy to undo by accident: plugin discovery must not
import every plugin body at startup, and clipboard *detection* must stay gone.

No Qt, no network, no real downloads. Runnable with pytest or directly
(`python tests/test_tasks_and_safety.py`).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import system_monitor as sm  # noqa: E402
from actions import timer as timer_action_mod  # noqa: E402
from core import install_safety as safety  # noqa: E402
from core import plugin_loader  # noqa: E402
from core import tasks  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _reset() -> None:
    tasks.reset()


# ── the task registry ────────────────────────────────────────────────────────


def test_task_lifecycle_and_states():
    _reset()
    task = tasks.start(tasks.KIND_DOWNLOAD, "Steam download",
                       detail="starting", total_bytes=1000, done_bytes=250)
    row = tasks.snapshot()[0]
    assert row["state"] == "running"
    assert row["percent"] == 25 and row["done_human"] == "250 B"
    task.update(done_bytes=500, speed_bps=100.0)
    row = tasks.snapshot()[0]
    # Bytes are the numbers a download actually has: progress must follow them.
    assert row["percent"] == 50 and row["done_human"] == "500 B"
    assert row["eta_seconds"] == 5.0          # 500 bytes left at 100 B/s
    assert row["speed_human"] == "100 B/s"
    task.finish(detail="saved")
    row = tasks.snapshot()[0]
    assert row["state"] == "done" and row["percent"] == 100
    assert row["detail"] == "saved" and row["finished"] is True


def test_task_fail_and_cancel_are_distinct():
    _reset()
    a = tasks.start("task", "one")
    b = tasks.start("task", "two")
    a.fail(error="disk full")
    b.cancel("Stopped at your request.")
    by_id = {r["id"]: r for r in tasks.snapshot()}
    assert by_id[a.id]["state"] == "failed" and by_id[a.id]["error"] == "disk full"
    assert by_id[b.id]["state"] == "cancelled"
    # Both are closed, so neither is offered for cancellation any more.
    assert tasks.active() == []


def test_task_cancel_request_reaches_the_owner():
    _reset()
    seen: list[str] = []
    task = tasks.start("task", "long job",
                       on_cancel=lambda: seen.append("asked"))
    tasks.request_cancel(task.id)
    assert seen == ["asked"]
    assert tasks.get(task.id).cancel_requested is True
    # A finished task ignores a late request; it is not "running" any more.
    task.finish()
    tasks.request_cancel(task.id)
    assert tasks.get(task.id).state == "done"


def test_task_snapshot_keeps_running_work_visible():
    _reset()
    for i in range(9):
        tasks.start("task", f"old {i}").finish()
    live = tasks.start("download", "the only live one")
    rows = tasks.snapshot(limit=5)
    assert rows[0]["id"] == live.id, "running tasks must sort ahead of finished ones"
    assert len(rows) <= 5


def test_task_revision_moves_on_every_change():
    _reset()
    start = tasks.revision()
    task = tasks.start("task", "x")
    after_start = tasks.revision()
    task.update(detail="working")
    assert after_start > start and tasks.revision() > after_start


def test_task_registry_never_grows_without_bound():
    _reset()
    for i in range(tasks.MAX_TASKS + 12):
        tasks.start("task", f"job {i}").finish()
    assert len(tasks.snapshot(limit=100)) <= tasks.MAX_TASKS + 1


def test_task_unknown_fields_are_ignored_not_fatal():
    _reset()
    task = tasks.start("download", "x")
    task.update(detail="ok", nonsense=object(), progress=0.5)
    row = tasks.snapshot()[0]
    assert row["detail"] == "ok" and row["percent"] == 50


def test_format_helpers_are_honest_about_unknowns():
    assert tasks.format_bytes(None) == "" and tasks.format_speed(None) == ""
    assert tasks.format_bytes(1536) == "1.5 KB"
    assert tasks.format_duration(None) == "" and tasks.format_duration(65) == "1m 05s"
    assert tasks.format_duration(3725) == "1h 02m"


# ── the install gate ─────────────────────────────────────────────────────────


def test_classify_intent_reads_verbs():
    assert safety.classify_intent("install Steam") == safety.KIND_INSTALL
    assert safety.classify_intent("uninstall Discord") == safety.KIND_UNINSTALL
    assert safety.classify_intent("deinstalliere WhatsApp") == safety.KIND_UNINSTALL
    assert safety.classify_intent("update my games") == safety.KIND_UPDATE
    assert safety.classify_intent("aktualisiere Chrome") == safety.KIND_UPDATE
    assert safety.classify_intent("download the driver") == safety.KIND_DOWNLOAD
    assert safety.classify_intent("open Discord") is None


def test_installer_targets_are_recognised():
    assert safety.is_installer_target(r"C:\Temp\setup.exe")
    assert safety.is_installer_target("/tmp/FooSetup-1.2.exe")
    assert safety.is_installer_target("unins000.exe")
    assert not safety.is_installer_target("/Applications/Spotify.app")


def test_protected_targets_cover_the_os_not_its_games():
    for name in ("Windows 11", "Microsoft Visual C++ 2015 Redistributable",
                 ".NET Runtime 8", "NVIDIA Graphics Driver", "kernel",
                 "my system", "operating system", "Realtek Audio Driver",
                 "Driver Booster", "Windows Update"):
        assert safety.protected_target(name), f"{name} must be refused"
    for name in ("Discord", "Spotify", "System Shock 2", "Driver: San Francisco",
                 "Visual Studio Code", "Team Fortress 2", "Git"):
        assert safety.protected_target(name) is None, f"{name} must be allowed"


def test_only_the_bootstrap_may_skip_the_gate():
    install = safety.InstallRequest(safety.KIND_INSTALL, "Discord")
    assert safety.requires_confirmation(install) is True
    bootstrap = safety.InstallRequest(
        safety.KIND_INSTALL, "3 dependency package(s)",
        source=safety.SELF_SOURCE)
    assert safety.requires_confirmation(bootstrap) is False


def test_guard_is_fail_closed_without_an_interface():
    ran: list[str] = []
    request = safety.InstallRequest(safety.KIND_INSTALL, "Discord")
    with patch("core.confirm._show_cb", None):
        out = safety.guard(request, lambda: ran.append("ran") or "done")
    assert ran == [], "nothing may run while the interface is unbound"
    assert "not available" in out


def test_guard_refuses_protected_targets_before_asking():
    shown: list[str] = []
    ran: list[str] = []
    with patch("core.confirm._show_cb", lambda t, d: shown.append(t)):
        out = safety.guard(
            safety.InstallRequest(safety.KIND_UNINSTALL, "Windows 11"),
            lambda: ran.append("ran") or "done")
    assert ran == [] and shown == []
    assert reference_starts_with(out, "[REFUSED]")


def reference_starts_with(text: str, prefix: str) -> bool:
    return str(text).startswith(prefix)


def test_guard_parks_the_work_behind_the_banner():
    shown: list[tuple[str, str]] = []
    ran: list[str] = []
    request = safety.InstallRequest(
        safety.KIND_UPDATE, "Rust",
        detail="Runs steam://update/252490.", origin="game_updater")
    with patch("core.confirm._show_cb",
               lambda t, d: shown.append((t, d))), \
         patch("core.confirm._hide_cb", lambda: None):
        out = safety.guard(request, lambda: ran.append("ran") or "updated",
                           key="game_update")
    assert ran == [], "the work must wait for the button"
    assert shown and shown[0][0] == "Update Rust"
    assert "Runs steam://update/252490." in shown[0][1]
    assert "[CONFIRMATION_PENDING]" in out


def test_verify_polls_until_true_and_reports_timeout():
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    ok, detail = safety.verify(probe, timeout=2.0, interval=0.01)
    assert ok is True and "after 3 check" in detail

    def never() -> bool:
        return False

    ok, detail = safety.verify(never, timeout=0.05, interval=0.01)
    assert ok is False and "not verified" in detail

    # A probe that raises is "not yet", never a pass.
    def boom() -> bool:
        raise RuntimeError("no access")

    ok, detail = safety.verify(boom, timeout=0.05, interval=0.01)
    assert ok is False and "no access" in detail


def test_unverified_wording_never_reads_as_success():
    text = safety.unverified("install", "Dota 2", "Steam accepted my request")
    assert "could not verify" in text and "Dota 2" in text
    assert "installed" not in text.lower()


# ── timers report into the registry ──────────────────────────────────────────


def test_timer_shows_up_and_finishes_in_the_task_registry():
    _reset()
    spoken: list[str] = []
    out = timer_action_mod.timer_action({"duration": "1s", "action": "start"},
                                        speak=spoken.append)
    assert "Timer set" in out
    rows = tasks.snapshot()
    assert rows and rows[0]["kind"] == "timer" and rows[0]["state"] == "running"
    deadline = time.time() + 30
    while time.time() < deadline:
        row = tasks.snapshot()[0]
        if row["state"] != "running":
            break
        time.sleep(0.2)
    row = tasks.snapshot()[0]
    assert row["state"] == "done", row
    assert row["percent"] == 100
    assert spoken and "up" in spoken[0], spoken


def test_timer_cancel_from_the_task_panel_stops_it():
    _reset()
    timer_action_mod.timer_action({"duration": "5m", "label": "pasta"})
    task = tasks.active()[0]
    tasks.request_cancel(task.id)          # exactly what the panel's ✕ does
    deadline = time.time() + 10
    while time.time() < deadline and tasks.get(task.id).state == "running":
        time.sleep(0.1)
    assert tasks.get(task.id).state == "cancelled"
    assert "No timers are running" in timer_action_mod.timer_action({"action": "list"})


# ── system status ────────────────────────────────────────────────────────────


def test_system_status_reports_what_it_can_and_omits_what_it_cannot():
    status = sm.get_system_status()
    for key in ("cpu_percent", "ram_percent", "ram_used_gb", "ram_total_gb",
                "uptime", "process_count", "disks", "tasks"):
        assert key in status, key
    text = sm.format_system_status(status)
    assert "CPU" in text and "RAM" in text and "Uptime" in text
    # A machine without a GPU/temperature says nothing about them rather than
    # inventing a number.
    if status.get("gpu_percent") is None:
        assert "GPU" not in text
    if status.get("cpu_temp_c") is None:
        assert "temperature" not in text


def test_system_status_counts_running_tasks():
    _reset()
    task = tasks.start("download", "Steam download", progress=0.25)
    status = sm.get_system_status()
    assert status["tasks"]["running"] == 1
    assert "Steam download" in status["tasks"]["titles"][0]
    task.finish()


# ── downloads report measured numbers ────────────────────────────────────────


def test_download_meter_reports_measured_bytes_and_speed():
    import tempfile
    import time

    from core import installer

    assert installer.estimate_bytes("~225 MB") == 225 * 1024 ** 2
    assert installer.estimate_bytes("nonsense") is None

    _reset()
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td)
        task = tasks.start(tasks.KIND_DOWNLOAD, "browser binaries",
                           total_bytes=installer.estimate_bytes("~1 MB"),
                           done_bytes=0)
        meter = installer.DownloadMeter(task, dirs=[cache], interval=0.2).start()
        for i in range(3):
            (cache / f"part{i}.bin").write_bytes(b"x" * 100_000)
            time.sleep(0.25)
        meter.stop()
    assert meter.bytes_written == 300_000
    assert meter.speed_bps and meter.speed_bps > 0
    row = tasks.snapshot()[0]
    assert row["percent"] == 29 and row["done_human"] == "293.0 KB"
    assert row["speed_human"].endswith("/s") and row["eta_seconds"] is not None

    # A cache that does not exist (yet) yields no numbers rather than zeros.
    _reset()
    task = tasks.start(tasks.KIND_DOWNLOAD, "nothing", done_bytes=0)
    meter = installer.DownloadMeter(task, dirs=[Path("/nonexistent-cache-xyz")],
                                    interval=0.2).start()
    time.sleep(0.3)
    meter.stop()
    assert meter.bytes_written is None
    assert tasks.snapshot()[0]["percent"] is None


# ── plugins: lazily loaded, telegram parked ──────────────────────────────────


def test_plain_plugins_are_not_imported_at_discovery():
    logs: list[str] = []
    registry = plugin_loader.discover_plugins(
        REPO / "plugins", set(), logger=logs.append, notify=lambda _m: None)
    deferred = {name: rec.deferred for name, rec in registry._plugins.items()}
    assert deferred.get("chat_takeover") is True, deferred
    assert any("deferred" in line for line in logs)
    # ... and the first real use imports it, exactly once.
    out = registry.run("chat_takeover", {"action": "status"})
    assert isinstance(out, str) and out
    assert registry._plugins["chat_takeover"].deferred is False


def test_a_plugin_that_raises_at_import_is_still_rejected_at_discovery():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        (tmp_path / "boom.py").write_text(
            "PLUGIN = {'name': 'boom', 'description': 'd',\n"
            "          'parameters': {'type': 'OBJECT', 'properties': {}}}\n"
            "raise RuntimeError('bang')\n", encoding="utf-8")
        registry = plugin_loader.discover_plugins(tmp_path, set(),
                                                  logger=lambda _m: None)
        records = {r["name"]: r for r in registry.list_for_ui()}
        assert records["boom"]["valid"] is False
        assert "bang" in records["boom"]["error"]


def _write_plugin(dir_path, filename: str, body: str) -> None:
    (dir_path / filename).write_text(body, encoding="utf-8")


_PLUGIN_HEAD = ("PLUGIN = {'name': '%s', 'description': 'd',\n"
                "          'parameters': {'type': 'OBJECT', 'properties': {}}}\n")


def test_a_missing_dependency_is_diagnosed_without_importing():
    """Discovery must not run the body just to find out a package is absent."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_plugin(tmp, "needs.py",
                      _PLUGIN_HEAD % "needs"
                      + "import definitely_not_installed_pkg\n"
                      + "def run(parameters=None, **kw):\n    return 'never'\n")
        logs: list[str] = []
        registry = plugin_loader.discover_plugins(tmp, set(), logger=logs.append)
    records = {r["name"]: r for r in registry.list_for_ui()}
    assert records["needs"]["valid"] is False
    assert "not installed" in records["needs"]["error"]
    assert "pip install definitely_not_installed_pkg" in records["needs"]["error"]
    assert "definitely_not_installed_pkg" not in sys.modules
    assert any("rejected" in line for line in logs)


def test_a_deferred_body_that_fails_on_first_use_reports_honestly():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Deferrable (only definitions here) but the definitions cannot be made:
        # the failure lands on the call that needed the plugin.
        _write_plugin(tmp, "fragile.py",
                      _PLUGIN_HEAD % "fragile"
                      + "VALUE = _not_defined_anywhere()\n"
                      + "def run(parameters=None, **kw):\n    return 'never'\n")
        logs: list[str] = []
        registry = plugin_loader.discover_plugins(tmp, set(), logger=logs.append)
        rec = registry._plugins["fragile"]
        assert rec.deferred is True, "the body should have been deferred"
        assert [d["name"] for d in registry.get_tool_declarations()] == ["fragile"]
        out = registry.run("fragile", {})
    assert "not available" in out and "_not_defined_anywhere" in out
    assert rec.deferred is False and rec.valid is False
    assert any("first" in line and "fragile" in line for line in logs)


def test_plugin_names_cannot_shadow_a_core_tool_or_each_other():
    import tempfile

    head = _PLUGIN_HEAD % "open_app"
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_plugin(tmp, "shadow.py", head + "def run(parameters=None, **kw):\n    return 'x'\n")
        registry = plugin_loader.discover_plugins(tmp, {"open_app"},
                                                  logger=lambda _m: None)
    rec = registry.list_for_ui()[0]
    assert rec["valid"] is False and "collides with a core tool" in rec["error"]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        body = ("def run(parameters=None, **kw):\n    return 'x'\n")
        _write_plugin(tmp, "a_twin.py", _PLUGIN_HEAD % "twin" + body)
        _write_plugin(tmp, "b_twin.py", _PLUGIN_HEAD % "twin" + body)
        registry = plugin_loader.discover_plugins(tmp, set(), logger=lambda _m: None)
    records = {r["file"]: r for r in registry.list_for_ui()}
    assert records["a_twin.py"]["valid"] is True
    assert records["b_twin.py"]["valid"] is False
    assert "already used by plugin" in records["b_twin.py"]["error"]


def test_parked_plugin_is_discovered_but_not_offered_or_run():
    registry = plugin_loader.discover_plugins(
        REPO / "plugins", set(), logger=lambda _m: None)
    assert registry.has("telegram_remote"), "parking must not delete anything"
    assert "telegram_remote" in [r["name"] for r in registry.list_for_ui()]
    assert "telegram_remote" in {d["name"] for d in registry.get_tool_declarations()}
    assert "telegram_remote" not in registry.names()
    filtered = plugin_loader.filter_parked(registry.get_tool_declarations())
    assert "telegram_remote" not in {d["name"] for d in filtered}
    out = registry.run("telegram_remote", {})
    assert "disabled" in out and "Nothing was run" in out


def test_parked_launch_hook_is_not_started():
    """The parked plugin's on_launch must not run — that is what starts bridges."""
    started: list[str] = []
    registry = plugin_loader.discover_plugins(
        REPO / "plugins", set(), logger=lambda _m: None)
    rec = registry._plugins["telegram_remote"]
    with patch.object(rec, "on_launch", lambda *a, **k: started.append("called")):
        registry.launch_enabled(player=None)
    assert started == [], "a parked plugin must not start anything"


# ── clipboard detection stays removed ────────────────────────────────────────


def test_clipboard_detection_is_gone_from_the_hud():
    source = (REPO / "ui.py").read_text(encoding="utf-8", errors="replace")
    for forbidden in ("ClipboardPanel", "_clipboard_sig", "CLIPBOARD DETECTED",
                      "clipboard().dataChanged"):
        assert forbidden not in source, forbidden
    main = (REPO / "main.py").read_text(encoding="utf-8", errors="replace")
    assert "clipboard" not in main.lower()


def test_ui_mounts_the_task_panel():
    source = (REPO / "ui.py").read_text(encoding="utf-8", errors="replace")
    assert "class TaskPanel" in source and "class TaskRow" in source
    assert "self._task_panel = TaskPanel()" in source
    assert "core import tasks as _task_registry" in source


# ── the assistant's voice stays neutral ──────────────────────────────────────


def test_prompt_does_not_force_a_dialect():
    prompt = (REPO / "core" / "prompt.txt").read_text(encoding="utf-8")
    lowered = prompt.lower()
    for dialect in ("bavarian", "bayerisch", "dialect", "dialekt", "mundart",
                    "fränkisch", "franggn"):
        assert dialect not in lowered, dialect
    assert "[VOICE]" in prompt
    assert "steady register" in lowered


# ── a game download reports Steam's own numbers ──────────────────────────────


def test_steam_download_progress_comes_from_steam_manifest():
    """Steam writes BytesDownloaded/BytesToDownload while it works.

    Those are the numbers the panel shows — for as long as the download runs,
    not only until the tool returned — and the row closes when the manifest
    says the app is up to date.
    """
    import tempfile
    import threading

    from actions import game_updater as gu

    with tempfile.TemporaryDirectory() as td:
        common = Path(td) / "steamapps" / "common"
        common.mkdir(parents=True)
        steam_path = common.parent.parent
        manifest = common.parent / "appmanifest_570.acf"

        def write(state, got, want):
            manifest.write_text(
                '"AppState"\n{\n\t"appid"\t\t"570"\n\t"name"\t\t"Dota 2"\n'
                f'\t"StateFlags"\t"{state}"\n\t"SizeOnDisk"\t"500"\n'
                f'\t"BytesDownloaded"\t"{got}"\n\t"BytesToDownload"\t"{want}"\n}}\n',
                encoding="utf-8")

        _reset()
        with patch.object(gu, "_get_steam_libraries", return_value=[common.parent]):
            # 1. no counters yet -> state only, no invented percentage.
            write(1026, 0, 0)
            task = tasks.start(tasks.KIND_INSTALL, "Install Dota 2", detail="queued")
            threading.Thread(
                target=gu._watch_steam_download,
                args=(steam_path, "570", "Dota 2", task),
                kwargs={"poll": 0.15}, daemon=True).start()
            time.sleep(0.35)
            row = tasks.snapshot()[0]
            assert row["state"] == "running" and row["percent"] is None
            assert row["done_bytes"] is None
            assert "downloading Dota 2" in row["detail"]

            # 2. Steam starts counting -> measured bytes, speed and ETA.
            write(1026, 100, 400)
            time.sleep(0.35)
            write(1026, 300, 400)
            time.sleep(0.35)
            row = tasks.snapshot()[0]
            assert row["done_bytes"] == 300 and row["total_bytes"] == 400
            assert row["percent"] == 75 and row["done_human"] == "300 B"
            assert row["speed_bps"] and row["speed_bps"] > 0
            assert row["eta_seconds"] is not None and row["eta_human"]

            # 3. Steam says up to date -> the row closes itself.
            write(4, 400, 400)
            deadline = time.time() + 5
            while time.time() < deadline and task.state == "running":
                time.sleep(0.05)
            row = tasks.snapshot()[0]
            assert row["state"] == "done"
            assert row["detail"] == "Steam reports the download finished"
            assert row["percent"] == 100


# ── the timer module keeps its own promises while reporting ──────────────────


def test_timer_parsing_unchanged_by_the_registry_work():
    parse = timer_action_mod._parse_duration
    assert parse("10m") == 600 and parse("90s") == 90
    assert parse("1h30m") == 5400 and parse("10:30") == 630
    assert parse("10") == 600                  # bare number = minutes
    assert parse("nonsense") == 0


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
