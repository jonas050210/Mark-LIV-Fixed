"""Regression tests for the second product-quality pass. No live OS actions."""
import ast
import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core import tasks, planner, local_ai, app_controller as ac
from core.attachments import AttachmentManager
from core.hud_layout import Rect, fit_window, valid_placement
from core.plan_validation import ground_steps

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_registry():
    tasks.reset()
    yield
    tasks.reset()


def method_from_source(file, cls_name, name, **namespace):
    """Execute the production method headlessly, not a reimplementation of it."""
    tree = ast.parse((ROOT / file).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
    method = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(ROOT / file), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("deps", [[True], [-1], [1], [2], "0", None])
def test_invalid_dependency_graph_never_becomes_independent_work(deps):
    raw = [{"tool": "work", "params": {}}, {"tool": "work", "params": {}, "depends_on": deps}]
    assert ground_steps(raw, ["work"], allow_partial=True) == []


def test_partial_grounding_remaps_dependencies_and_preserves_missing_objective():
    raw = [{"tool": "agent_task"}, {"tool": "work"},
           {"tool": "work", "depends_on": [1]}, {"tool": "work", "depends_on": [0]},
           {"tool": "work", "depends_on": [3]}]
    result = planner._ground_local_steps(raw, ["agent_task", "work"])
    assert len(result) == 2
    assert result[1]["depends_on"] == [0]
    assert len(result[0]["unplanned"]) == 3


def test_local_model_overflow_fails_objective_instead_of_reporting_all_done(monkeypatch):
    monkeypatch.setattr(local_ai, "is_available", lambda: True)
    monkeypatch.setattr(local_ai, "_generate", lambda *a, **k: json.dumps([{"tool": "work"}] * 7))
    dispatcher = SimpleNamespace(run=Mock(return_value="Done."))
    result = planner.run_plan("unrecognized objective", dispatcher, tool_names=["work"], verify_steps=False)
    assert dispatcher.run.call_count == 6
    assert "not complete" in result
    assert tasks.snapshot()[0]["state"] == "failed"


def test_cancel_during_tool_prevents_recovery(monkeypatch):
    from core.verify import Verification
    task = tasks.start("agent", "test", pausable=True)
    monkeypatch.setattr(planner, "plan", lambda *a: [{"tool": "open_app", "params": {"app_name": "X"}}])
    monkeypatch.setattr(planner, "verify", lambda *a: Verification(False, "not running", method="recheck"))
    def run(*args):
        task.request_cancel()
        return "Could not start X"
    dispatcher = SimpleNamespace(run=Mock(side_effect=run))
    result = planner.run_plan("open X", dispatcher, task=task, tool_names=["open_app", "app_inventory"])
    assert dispatcher.run.call_count == 1
    assert task.state == "cancelled" and "No further actions" in result
    assert task.meta["steps"][0]["state"] == "failed"


def await_terminal(ident):
    task = tasks.get(ident)
    deadline = time.monotonic() + 3
    while task.state in tasks.ACTIVE_STATES and time.monotonic() < deadline:
        time.sleep(.005)
    assert task.state in tasks.TERMINAL_STATES
    return task


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO regression")
def test_attachment_nonregular_file_does_not_block_worker(tmp_path):
    source = tmp_path / "pipe.txt"
    os.mkfifo(source)
    manager = AttachmentManager()
    try:
        ident = manager.add(source)
        task = await_terminal(ident)
        assert task.state == "failed" and "regular" in task.error
        assert manager.context() == []
    finally:
        manager.close()


def test_failed_attachment_retains_outcome_after_registry_history_expires(tmp_path):
    manager = AttachmentManager()
    try:
        ident = manager.add(tmp_path / "missing.txt")
        assert await_terminal(ident).state == "failed"
        for i in range(tasks.MAX_TASKS + 2):
            tasks.start("task", str(i)).finish()
        assert tasks.get(ident) is None
        snap = manager.snapshot()[0]
        assert snap["task"]["state"] == "failed" and snap["task"]["error"]
    finally:
        manager.close()


def test_attachment_removal_revokes_owner_context(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("untrusted file contents")
    owner = tasks.start("agent", "task")
    ready = threading.Event()
    manager = AttachmentManager(ready=lambda _: ready.set())
    try:
        ident = manager.add(source, owner.id)
        assert ready.wait(3)
        assert owner.meta["attachments"][0]["id"] == ident
        manager.remove(ident)
        assert owner.meta["attachments"] == []
        assert manager.context() == []
        assert source.exists()
    finally:
        manager.close()


def test_import_notification_does_not_start_an_unsolicited_chat_turn():
    item = dict(id="t1", path="/staged.txt", name="note.txt")
    fake = SimpleNamespace(_attachments=SimpleNamespace(context=lambda: [item]),
                           _file_hint=Mock(), _log=Mock(), on_text_command=Mock())
    ready = method_from_source("ui.py", "MainWindow", "_attachment_ready")
    ready(fake, item)
    fake.on_text_command.assert_not_called()
    fake._log.append_log.assert_called_once()


@pytest.mark.parametrize("generation,enabled,closing", [(1, True, False), (2, False, False), (2, True, True)])
def test_stale_game_observation_cannot_hide_main_window(generation, enabled, closing):
    fake = SimpleNamespace(_presentation_generation=2, _closing=closing, _game_poll_pending=True,
                           _game_was_active=False, _mini=Mock(), hide=Mock())
    observed = method_from_source("ui.py", "MainWindow", "_on_game_observed",
                                  _read_gui_settings=lambda: {"gaming_mode": enabled})
    observed(fake, (generation, {"exe": "cs2.exe"}))
    fake.hide.assert_not_called()
    fake._mini.show_passively.assert_not_called()
    assert fake._game_poll_pending is False


def test_game_observation_uses_game_monitor_without_restoring_after_focus_loss():
    main_screen = SimpleNamespace(name=lambda: "DISPLAY1")
    game_screen = SimpleNamespace(name=lambda: "DISPLAY2")
    fake = SimpleNamespace(_presentation_generation=2, _closing=False, _game_poll_pending=True,
                           _game_was_active=False, _mini=Mock(), hide=Mock(),
                           screen=lambda: main_screen)
    observed = method_from_source("ui.py", "MainWindow", "_on_game_observed",
                                  _read_gui_settings=lambda: {"gaming_mode": True},
                                  QApplication=SimpleNamespace(screens=lambda: [main_screen, game_screen]))
    observed(fake, (2, {"exe": "cs2.exe", "monitor_device": "DISPLAY2"}))
    fake._mini.show_passively.assert_called_once_with(game_screen)
    observed(fake, (2, {}))
    fake.hide.assert_called_once()
    assert fake._game_was_active is False


@pytest.mark.parametrize("area", [Rect(-1920, -400, 1920, 1040), Rect(100, 100, 240, 220), Rect(0, 0, 1, 1)])
def test_persisted_mini_fits_small_and_negative_work_areas(area):
    fitted = fit_window(Rect(3000, 1500, 1000, 1000), area)
    assert area.x <= fitted.x <= fitted.x + fitted.width <= area.x + area.width
    assert area.y <= fitted.y <= fitted.y + fitted.height <= area.y + area.height


def test_mini_geometry_persists_without_overwriting_other_settings(tmp_path, monkeypatch):
    from memory import config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "settings.json")
    cm.CONFIG_FILE.write_text(json.dumps({"voice_engine": "silent"}))
    value = dict(x=-1800, y=-100, width=360, height=440)
    assert cm.save_mini_geometry(value)
    assert cm.get_mini_geometry() == value
    assert json.loads(cm.CONFIG_FILE.read_text())["voice_engine"] == "silent"
    assert not cm.save_mini_geometry({**value, "width": True})
    assert cm.get_mini_geometry() == value
    assert valid_placement({**value, "x": 2**60}) is None


@pytest.mark.parametrize("action", ["focus", "minimize", "maximize", "restore"])
@pytest.mark.parametrize("unknown", [True, False])
def test_window_actions_do_not_invent_state_when_refused_or_unreadable(action, unknown):
    win = SimpleNamespace(title="Editor", activate=Mock(), minimize=Mock(), maximize=Mock(), restore=Mock())
    if not unknown:
        win.isActive = win.isMinimized = win.isMaximized = False
        if action == "restore":
            win.isMinimized = True
    with patch.object(ac, "_window_api", return_value=Mock()), \
            patch.object(ac, "_matching_windows", return_value=[win]), \
            patch("core.gaming.protect_focus", return_value=False):
        ok, message = ac.window_action(action, title="Editor")
    assert not ok
    assert "unverified" in message or "not the active" in message


@pytest.mark.parametrize("stdout,title,expected", [("False", "Editor", False), ("True", "Game", False), ("True", "Editor", True)])
def test_windows_focus_fallback_requires_acceptance_and_foreground_readback(stdout, title, expected):
    with patch.object(ac.sys, "platform", "win32"), \
            patch.object(ac, "_window_titles_from_os", return_value=(["Editor"], "")), \
            patch.object(ac, "_no_window", return_value={}), \
            patch.object(ac.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=stdout)), \
            patch.object(ac, "foreground_window", return_value={"title": title}):
        assert ac._focus_via_os("Editor")[0] is expected


def test_cancelled_german_synthesis_cannot_resume_a_blocked_queue_put():
    from core import german_voice
    queue_voice = method_from_source("main.py", "JarvisLive", "_queue_standard_voice", asyncio=asyncio)
    async def exercise():
        queue = asyncio.Queue(maxsize=1)
        fake = SimpleNamespace(_standard_voice_lock=None, _speech_generation=1, audio_in_queue=queue,
                               _turn_done_event=asyncio.Event(), ui=SimpleNamespace(write_log=Mock()))
        with patch.object(german_voice, "render_pcm", return_value=b"x" * 4800):
            producer = asyncio.create_task(queue_voice(fake, "Fertig.", 1, queue))
            await asyncio.sleep(.02)
            assert queue.full() and not producer.done()
            fake._speech_generation += 1
            queue.get_nowait()
            await asyncio.wait_for(producer, .5)
        assert queue.empty()
        assert not fake._turn_done_event.is_set()
    asyncio.run(exercise())


def test_interrupt_mutates_audio_queue_on_its_own_event_loop():
    stop = method_from_source("main.py", "JarvisLive", "interrupt", asyncio=asyncio,
                              get_speech_router=lambda: SimpleNamespace(stop=Mock()))
    async def exercise():
        owner = threading.get_ident()
        class BoundQueue(asyncio.Queue):
            def get_nowait(self):
                assert threading.get_ident() == owner
                return super().get_nowait()
        queue = BoundQueue()
        queue.put_nowait(b"old speech")
        fake = SimpleNamespace(_speaking_lock=threading.Lock(), _is_speaking=True,
                               _speech_generation=1, audio_in_queue=queue, _loop=asyncio.get_running_loop(),
                               _turn_done_event=asyncio.Event(), set_speaking=Mock(), _visemes=Mock(), ui=Mock())
        fake._turn_done_event.set()
        await asyncio.to_thread(stop, fake)
        await asyncio.sleep(0)
        assert queue.empty() and not fake._turn_done_event.is_set()
        assert fake._speech_generation == 2
    asyncio.run(exercise())


@pytest.mark.parametrize("prefix", ["analyze task:", "plan:", "Analysiere Aufgabe:"])
def test_analysis_request_does_not_launch_planned_app(prefix):
    dispatcher = SimpleNamespace(run=Mock(return_value="Done"))
    result = planner.run_plan(f"{prefix} open Spotify", dispatcher, tool_names=["open_app"])
    dispatcher.run.assert_not_called()
    assert "open_app" in result and "No actions have been run" in result
    assert tasks.active() == []


def test_legacy_current_file_accessor_uses_only_ready_managed_files():
    accessor = method_from_source("ui.py", "JarvisUI", "current_file")
    manager = SimpleNamespace(context=Mock(return_value=[{"path": "/staged/verified.txt"}]))
    fake = SimpleNamespace(_win=SimpleNamespace(_attachments=manager))
    assert accessor.fget(fake) == "/staged/verified.txt"
    manager.context.return_value = []
    assert accessor.fget(fake) is None


def test_file_drop_rejects_remote_urls_and_emits_each_local_file():
    drop = method_from_source("core/attachment_ui.py", "FileDropZone", "dropEvent")
    local = lambda path: SimpleNamespace(isLocalFile=lambda: True, toLocalFile=lambda: path)
    remote = SimpleNamespace(isLocalFile=lambda: False)
    event = Mock()
    event.mimeData.return_value.urls.return_value = [remote]
    zone = SimpleNamespace(file_selected=SimpleNamespace(emit=Mock()))
    drop(zone, event)
    zone.file_selected.emit.assert_not_called()
    event.ignore.assert_called_once()
    event.mimeData.return_value.urls.return_value = [remote, local("a.txt"), local("b.txt")]
    drop(zone, event)
    assert [call.args for call in zone.file_selected.emit.call_args_list] == [("a.txt",), ("b.txt",)]
    event.acceptProposedAction.assert_called_once()


def test_cancelling_synthesis_releases_lock_for_next_response():
    from core import german_voice
    queue_voice = method_from_source("main.py", "JarvisLive", "_queue_standard_voice", asyncio=asyncio)
    async def exercise():
        queue = asyncio.Queue()
        fake = SimpleNamespace(_standard_voice_lock=None, _speech_generation=1, audio_in_queue=queue,
                               _turn_done_event=asyncio.Event(), ui=Mock())
        started = asyncio.Event()
        async def render(text):
            if text == "old":
                started.set()
                await asyncio.Event().wait()
            return b"new"
        with patch.object(german_voice, "render_pcm", side_effect=render):
            old = asyncio.create_task(queue_voice(fake, "old", 1, queue))
            await started.wait()
            fake._speech_generation = 2
            german_voice.cancel_pending(fake)
            with pytest.raises(asyncio.CancelledError):
                await old
            await asyncio.wait_for(queue_voice(fake, "new", 2, queue), .5)
        assert queue.get_nowait() == b"new" and queue.empty()
        assert fake._standard_voice_task is None
    asyncio.run(exercise())


def test_offline_synthesis_receives_cancellation(monkeypatch):
    import sys
    from core import german_voice
    entered, stopped = threading.Event(), threading.Event()
    def synth(text, stop):
        entered.set()
        if stop.wait(2):
            stopped.set()
        return b"unused"
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    monkeypatch.setattr(german_voice, "_sapi_pcm", synth)
    async def exercise():
        pending = asyncio.create_task(german_voice.render_pcm("Text"))
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert await asyncio.to_thread(stopped.wait, 1)
    asyncio.run(exercise())
