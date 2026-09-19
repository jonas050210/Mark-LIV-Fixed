"""Product regressions: real registry/workers, safe geometry and shared speech.

No live accounts, games, microphone or Windows desktop required. Native Qt
smokes are separate and skip explicitly when host libraries are absent.
"""
import ast
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from core import tasks, planner, local_ai
from core.attachments import AttachmentManager
from core.gaming import is_game
from core.hud_layout import Rect, activity_layout
from core.speech import SpeechRouter, VoiceProvider
from core.verify import Verification

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_tasks():
    tasks.reset()
    yield
    tasks.reset()


@pytest.mark.parametrize("width,height", [(280, 240), (480, 600), (1920, 1080), (3440, 1440)])
@pytest.mark.parametrize("anchor", ["topleft", "topright", "center"])
def test_hud_dock_is_contained_and_separate(width, height, anchor):
    for scale in (.55, 1, 1.6):
        dock, work = activity_layout(width, height, anchor, scale)
        assert dock.width > 0
        assert 0 <= dock.x <= width - dock.width
        assert 0 <= dock.y <= height - dock.height
        assert not dock.intersects(work)
        assert 0 <= work.x and work.x + work.width <= width
        assert work.y + work.height <= height
        if anchor == "topright":
            assert width - dock.x - dock.width <= 12.01


def test_hud_uses_obstacle_edges_not_screen_coordinates():
    obstacle = Rect(380, 0, 220, 220)
    dock, work = activity_layout(600, 640, "topright", obstacles=[obstacle])
    assert not dock.intersects(obstacle)
    assert not dock.intersects(work)


def test_pause_is_acknowledged_only_at_owner_checkpoint_and_cancel_releases_it():
    task = tasks.start("agent", "work", pausable=True, queued=True)
    assert task.request_pause()
    assert task.state == "queued"
    result = []
    worker = threading.Thread(target=lambda: result.append(task.checkpoint()))
    worker.start()
    deadline = time.monotonic() + 2
    while task.state != "paused" and time.monotonic() < deadline:
        time.sleep(.01)
    assert task.state == "paused" and not task.snapshot()["finished"]
    tasks.clear_finished()
    assert tasks.get(task.id) is task
    task.request_cancel()
    worker.join(2)
    assert not worker.is_alive() and result == [False]
    task.cancel()
    task.finish("late success must not override cancellation")
    assert task.state == "cancelled"


def test_resume_and_immutable_snapshot():
    task = tasks.start("agent", "plan", pausable=True, steps=[{"state": "queued"}])
    task.request_pause()
    task.resume()
    assert task.checkpoint()
    snap = task.snapshot()
    snap["meta"]["steps"][0]["state"] = "done"
    assert task.meta["steps"][0]["state"] == "queued"
    task.finish()
    task.update(detail="late update")
    assert task.detail != "late update"


def test_uncooperative_owners_do_not_advertise_pause():
    task = tasks.start("install", "external installer")
    assert task.request_pause() is False
    assert task.state == "running"


def test_independent_work_runs_past_a_blocked_dependency():
    steps = [
        {"tool": "open_app", "params": {"app_name": "A"}},
        {"tool": "close_app", "params": {"app_name": "A"}},
        {"tool": "web_search", "params": {"query": "weather"}},
    ]
    dispatch = Mock()
    dispatch.run.return_value = "Tool result"
    with patch.object(planner, "plan", return_value=steps), patch.object(planner, "verify", side_effect=[
            Verification(False, "not running", "text"), Verification(True, "weather found", "text")]):
        result = planner.run_plan("objective", dispatch, tool_names=[s["tool"] for s in steps])
    assert [call.args[0] for call in dispatch.run.call_args_list] == ["open_app", "web_search"]
    row = tasks.snapshot()[0]
    assert row["state"] == "failed"
    assert [s["state"] for s in row["meta"]["steps"]] == ["failed", "blocked", "done"]
    assert "Carried on" in result


def test_empty_tool_result_is_not_invented_completion():
    dispatch = Mock()
    dispatch.run.return_value = None
    with patch.object(planner, "plan", return_value=[{"tool": "web_search", "params": {}}]):
        result = planner.run_plan("objective", dispatch, tool_names=["web_search"])
    assert tasks.snapshot()[0]["state"] == "failed"
    assert "unverified" in result


def test_meta_instruction_unwraps_a_concrete_objective():
    steps = planner.decompose("Analyze this task: open Spotify and search for jazz", ["open_app", "web_search"])
    assert [s["tool"] for s in steps] == ["open_app", "web_search"]
    assert steps[0]["params"]["app_name"] == "Spotify"


def test_dependency_grounding_rejects_forward_or_malformed_references():
    for deps in ([1], "previous", [-1]):
        response = '[{"tool":"open_app","params":{},"depends_on":' + __import__('json').dumps(deps) + '}]'
        with patch.object(local_ai, "is_available", return_value=True), patch.object(local_ai, "_generate", return_value=response):
            assert local_ai.plan_steps("objective", ["open_app"]) is None


class RecordingVoice(VoiceProvider):
    name = "recording"

    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


@pytest.mark.parametrize("mode", ["local", "auto", "silent", "live"])
def test_offline_and_silent_final_responses_reach_existing_transcript_once(mode):
    router = SpeechRouter()
    transcript, voice = [], RecordingVoice()
    router.set_transcript(transcript.append)
    router.set_local_provider(voice)
    with patch.object(router, "mode", return_value=mode):
        router.announce("Der Timer ist abgelaufen.", background=False)
    assert transcript == ["Der Timer ist abgelaufen."]
    assert voice.spoken == (transcript if mode in ("auto", "local") else [])


def test_live_does_not_publish_instruction_as_final_speech_and_false_acceptance_falls_back():
    router = SpeechRouter()
    transcript, voice = [], RecordingVoice()
    router.set_transcript(transcript.append)
    router.set_local_provider(voice)
    router.set_live_provider(lambda: True, lambda _: True)
    with patch.object(router, "mode", return_value="auto"):
        assert router.announce("timer", background=False) == "live"
        assert transcript == []  # Final Live output transcription is the sole authority.
        router.set_live_provider(lambda: True, lambda _: False)
        assert router.announce("timer", background=False) == "local:recording"
    assert transcript == voice.spoken == ["timer"]


def test_live_uses_german_renderer_not_an_unsupported_language_hint():
    tree = ast.parse((ROOT / "main.py").read_text())
    assert not any(isinstance(n, ast.keyword) and n.arg == "language_code"
                   for n in ast.walk(tree))
    source = (ROOT / "main.py").read_text()
    assert "response.data and not self._standard_german" in source
    assert "_queue_standard_voice(full_out" in source
    from core.german_voice import VOICE
    assert VOICE == "de-DE-ConradNeural"
    assert "Language=407" in (ROOT / "core/speech.py").read_text()


def test_game_detection_does_not_classify_fullscreen_utilities_as_games():
    assert not is_game({"exe": "chrome.exe", "fullscreen": True, "path": r"C:\Chrome\chrome.exe"})
    assert is_game({"exe": "cs2.exe"})
    assert is_game({"exe": "custom.exe"}, ["custom.exe"])
    assert is_game({"exe": "game.exe", "fullscreen": True, "path": r"D:\Steam\steamapps\common\Game\game.exe"})
    assert not is_game({"exe": "launcher.exe", "fullscreen": False, "path": r"D:\Steam\steamapps\common\Game\launcher.exe"})


def test_gaming_blocks_unsafe_utility_activation_but_allows_explicit_focus():
    from core import app_controller as ac
    win = Mock(title="Utility", isMinimized=False, isActive=True)
    with patch.object(ac, "_window_api", return_value=Mock()), patch.object(ac, "_matching_windows", return_value=[win]), patch("core.gaming.protect_focus", return_value=True):
        assert not ac.window_action("maximize", title="Utility")[0]
        win.maximize.assert_not_called()
        assert ac.window_action("focus", title="Utility")[0]
        win.activate.assert_called_once()


def test_multiple_files_import_real_bytes_and_keep_session_ownership(tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("hello")
    second.write_bytes(b"x" * 900000)
    ready = threading.Event()
    manager = AttachmentManager(ready=lambda _: ready.set() if len(manager.context()) == 2 else None)
    try:
        owner = tasks.start("agent", "read files")
        a = manager.add(first, owner.id)
        b = manager.add(second, owner.id)
        assert ready.wait(5)
        context = manager.context()
        assert len(context) == 2
        assert all(x["session_id"] == manager.session_id and x["task_id"] == owner.id for x in context)
        assert tasks.get(a).done_bytes == 5
        assert tasks.get(b).done_bytes == 900000
        assert all(Path(x["path"]).is_file() for x in context)
        assert len(owner.meta["attachments"]) == 2
        manager.remove(a)
        assert len(manager.context()) == 1
        assert first.read_text() == "hello"  # removal never deletes the user's original
    finally:
        manager.close()


def test_import_failure_and_cancel_never_enter_context(tmp_path):
    manager = AttachmentManager()
    try:
        ident = manager.add(tmp_path / "missing.txt")
        deadline = time.monotonic() + 3
        while tasks.get(ident).state in tasks.ACTIVE_STATES and time.monotonic() < deadline:
            time.sleep(.01)
        assert tasks.get(ident).state == "failed"
        assert not manager.context()
    finally:
        manager.close()


def test_mini_and_gaming_settings_roundtrip(tmp_path, monkeypatch):
    from memory import config_manager as cm
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path)
    assert cm.save_gui_settings({"mini_mode": True, "gaming_mode": True})
    assert cm.get_gui_settings()["mini_mode"] is True
    assert cm.get_gui_settings()["gaming_mode"] is True
    assert cm.save_gui_settings({"ui_scale": 1.25})
    assert cm.get_gui_settings()["gaming_mode"] is True


def test_unplanned_objective_is_not_reported_as_complete():
    dispatch = Mock()
    dispatch.available_tools.return_value = ["open_app"]
    dispatch.run.return_value = "Opened Spotify"
    with patch.object(planner, "verify", return_value=Verification(True, "running")):
        result = planner.run_plan("open Spotify and paint the moon purple", dispatch)
    assert "full objective is not complete" in result
    assert tasks.snapshot()[0]["state"] == "failed"


def test_task_control_is_cooperative_and_inspectable():
    from actions.task_control import task_control
    import json
    task = tasks.start("agent", "goal", pausable=True)
    assert "Pause requested" in task_control({"action": "pause", "task_id": task.id})
    assert task.pause_requested and task.state == "running"
    assert json.loads(task_control({"action": "inspect", "task_id": task.id}))["id"] == task.id
    assert "Resume requested" in task_control({"action": "resume", "task_id": task.id})
    assert not task.pause_requested
    assert "requested" in task_control({"action": "cancel", "task_id": task.id})
    assert task.cancel_requested and task.state == "running"


def test_silent_response_does_not_duplicate_as_say_log():
    router = SpeechRouter()
    transcript, logs = [], []
    router.set_transcript(transcript.append)
    router.set_log(logs.append)
    with patch.object(router, "mode", return_value="silent"):
        router.announce("Fertig.", background=False)
    assert transcript == ["Fertig."] and logs == []


def test_german_renderer_falls_back_without_reentering_live(monkeypatch):
    import asyncio
    import sys
    from core import german_voice
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    monkeypatch.setattr(german_voice, "_sapi_pcm", lambda text, stop=None: text.encode())
    assert asyncio.run(german_voice.render_pcm("Fertig")) == b"Fertig"


def test_final_german_audio_uses_existing_queue_and_discards_interrupted_synthesis():
    import asyncio
    from types import SimpleNamespace
    from core import german_voice
    tree = ast.parse((ROOT / "main.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "JarvisLive")
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_queue_standard_voice")
    namespace = {"asyncio": asyncio}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "main-voice", "exec"), namespace)

    async def exercise(interrupted):
        queue = asyncio.Queue()
        fake = SimpleNamespace(_standard_voice_lock=None, _speech_generation=1,
                               audio_in_queue=queue, _turn_done_event=asyncio.Event(),
                               ui=SimpleNamespace(write_log=Mock()))
        async def render(text):
            assert text == "Die Aufgabe ist erledigt."
            if interrupted:
                fake._speech_generation += 1
            return b"x" * 4800
        with patch.object(german_voice, "render_pcm", side_effect=render):
            await namespace["_queue_standard_voice"](fake, "Die Aufgabe ist erledigt.", 1, queue)
        assert queue.qsize() == (0 if interrupted else 2)
        assert fake._turn_done_event.is_set() is (not interrupted)
    asyncio.run(exercise(False))
    asyncio.run(exercise(True))


def test_window_shell_fallback_rejects_expansion_and_ambiguous_matches():
    from core import app_controller as ac
    assert not ac._shell_safe("App $(whoami)")
    assert not ac._shell_safe("App `n")
    with patch.object(ac, "_window_api", return_value=Mock()), patch.object(ac, "_matching_windows", return_value=[
            Mock(title="Document one"), Mock(title="Document two")]):
        ok, message = ac.window_action("resize", title="Document", width=800, height=600)
    assert not ok and "Several windows" in message


def test_finished_history_does_not_grow_behind_a_live_task():
    live = tasks.start("timer", "Long timer")
    for i in range(100):
        tasks.start("task", str(i)).finish()
    rows = tasks.snapshot(limit=200)
    assert rows[0]["id"] == live.id and len(rows) <= tasks.MAX_TASKS


def test_concurrent_metadata_updates_preserve_attachment_ownership():
    owner = tasks.start("agent", "work")
    threads = [threading.Thread(target=owner.append_meta, args=("attachments", {"id": n})) for n in range(20)]
    for thread in threads:
        thread.start()
    owner.patch_meta(steps=[{"state": "running"}])
    for thread in threads:
        thread.join()
    assert len(owner.snapshot()["meta"]["attachments"]) == 20
    assert owner.meta["steps"][0]["state"] == "running"


def test_windows_snap_uses_negative_monitor_work_area_and_never_activates(monkeypatch):
    import sys
    from types import SimpleNamespace
    from core import app_controller as ac
    state = {"rect": (20, 20, 820, 620)}
    def position(hwnd, order, x, y, width, height, flags):
        assert flags & 0x10  # SWP_NOACTIVATE
        state["rect"] = x, y, x+width, y+height
    gui = SimpleNamespace(IsWindow=lambda _: True, GetWindowRect=lambda _: state["rect"], SetWindowPos=position)
    api = SimpleNamespace(EnumDisplayMonitors=lambda: [(1, None, None), (2, None, None)],
                          MonitorFromWindow=lambda _: 1,
                          GetMonitorInfo=lambda _: {"Work": (-1920, 0, 0, 1040)})
    con = SimpleNamespace(SWP_NOACTIVATE=0x10, SWP_NOZORDER=4)
    monkeypatch.setitem(sys.modules, "win32gui", gui)
    monkeypatch.setitem(sys.modules, "win32api", api)
    monkeypatch.setitem(sys.modules, "win32con", con)
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(GetWindowThreadProcessId=lambda _: (1, 1000001)))
    monkeypatch.setattr(ac, "_psutil", lambda: SimpleNamespace(Process=lambda _: SimpleNamespace(name=lambda: "notepad.exe")))
    with patch.object(sys, "platform", "win32"):
        ok, message = ac._geometry_action(SimpleNamespace(_hWnd=123), "snap_right", monitor=2)
    assert ok and "verified" in message
    assert state["rect"] == (-960, 0, 0, 1040)


def test_existing_file_does_not_prove_a_failed_write_succeeded(tmp_path):
    from core.verify import verify, is_failure_text
    path = tmp_path / "existing.txt"
    path.write_text("old content")
    verdict = verify("file_controller", {"action": "write", "path": str(path)},
                     "Permission denied; found the old file but could not write it.")
    assert not verdict.ok
    assert is_failure_text("File not found here")
    assert not verify("open_app", {"app_name": "A"}, "[CONFIRMATION_PENDING] Waiting for approval").ok


def test_browser_directory_alone_is_not_verified_installation(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from core import installer
    cache = tmp_path / "chromium-123"
    cache.mkdir()
    binary = cache / "chrome.exe"
    runtime = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(binary)))
    context = Mock()
    context.__enter__ = Mock(return_value=runtime)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=lambda: context))
    assert not installer.browsers_installed(["chromium"])
    binary.write_bytes(b"")
    assert not installer.browsers_installed(["chromium"])
    binary.write_bytes(b"binary fixture")
    assert installer.browsers_installed(["chromium"])


def test_full_speech_queue_with_closed_transcript_does_not_raise():
    router = SpeechRouter()
    router._ensure_worker = lambda: None
    router.set_transcript(Mock(side_effect=RuntimeError("window closed")))
    for i in range(router._queue.maxsize):
        router.announce(f"queued result {i}")
    assert router.announce("overflow result") == "log"
    assert router._queue.qsize() == router._queue.maxsize
    assert "overflow result" not in router._pending_text


def test_sapi_render_uses_german_fixed_rate_and_plain_text(monkeypatch):
    import sys
    from types import SimpleNamespace
    from core import german_voice
    voice, stream, com = Mock(), Mock(), Mock()
    voice.GetVoices.return_value.Count = 1
    stream.GetData.return_value = b"pcm"
    client = SimpleNamespace(Dispatch=lambda name: voice if name == "SAPI.SpVoice" else stream)
    monkeypatch.setitem(sys.modules, "pythoncom", com)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(german_voice, "sys", SimpleNamespace(platform="win32"))
    assert german_voice._sapi_pcm("Fertig.") == b"pcm"
    voice.GetVoices.assert_called_once_with("Language=407")
    voice.Speak.assert_called_once_with("Fertig.", 17)
    assert voice.AllowAudioOutputFormatChangesOnNextSet is False
    assert voice.AudioOutputStream is stream
    assert stream.Format.Type == 26
    com.CoUninitialize.assert_called_once()
