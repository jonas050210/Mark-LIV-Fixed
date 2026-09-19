"""Multi-step agent: dispatch, plan, execute, verify.

Covers core/dispatcher.py, core/planner.py, core/verify.py and the
agent_task action. Registries are faked; the planner's local-AI fallback is
stubbed out (offline deterministic). Runnable with pytest or directly
(`python tests/test_agent.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import local_ai as lai  # noqa: E402
from core import planner  # noqa: E402
from core import verify as ver  # noqa: E402
from core.dispatcher import ToolDispatcher  # noqa: E402


TOOLS = ["open_app", "close_app", "web_search", "timer", "reminder",
         "save_memory", "recall_memory", "system_status"]


class FakeActions:
    def __init__(self, results):
        self._results = dict(results)
        self.calls = []

    def has(self, name):
        return name in self._results

    def names(self):
        return set(self._results)

    def run(self, name, params, ctx=None):
        self.calls.append((name, dict(params)))
        result = self._results[name]
        if isinstance(result, Exception):
            raise result
        return result


class FakePluginsNew:
    """PluginRegistry shape: run(name, parameters, player, session_memory,
    dispatcher=None)."""

    def __init__(self, results):
        self._results = dict(results)

    def has(self, name):
        return name in self._results

    def list_for_ui(self):
        return [{"name": n, "valid": True, "enabled": True}
                for n in self._results]

    def run(self, name, parameters, player=None, session_memory=None,
            dispatcher=None):
        return self._results[name]


class FakePluginsLegacy:
    """Old double shape: call(name, params, ctx)."""

    def __init__(self, results):
        self._results = dict(results)

    def has(self, name):
        return name in self._results

    def get_tool_declarations(self):
        return [{"name": n} for n in self._results]

    def call(self, name, params, ctx):
        return self._results[name]


def _dispatcher(**results):
    d = ToolDispatcher()
    d.bind(action_registry=FakeActions(results))
    return d


# ── dispatcher ───────────────────────────────────────────────────────────────


def test_dispatch_routes_to_actions():
    d = _dispatcher(open_app="Spotify is now running.")
    assert d.run("open_app", {"app_name": "Spotify"}) == "Spotify is now running."


def test_dispatch_supports_both_plugin_shapes():
    d = ToolDispatcher()
    d.bind(plugin_registry=FakePluginsNew({"plug_new": "new-ok"}))
    assert d.run("plug_new", {}) == "new-ok"
    d2 = ToolDispatcher()
    d2.bind(plugin_registry=FakePluginsLegacy({"plug_old": "old-ok"}))
    assert d2.run("plug_old", {}) == "old-ok"


def test_dispatch_inline_runner_first_none_falls_through():
    d = ToolDispatcher()
    d.bind(action_registry=FakeActions({"recall_memory": "action-recall"}),
           inline_runner=lambda n, p, c: "inline-recall" if n == "recall_memory" else None)
    assert d.run("recall_memory", {}) == "inline-recall"
    d2 = ToolDispatcher()
    d2.bind(action_registry=FakeActions({"timer": "t"}),
           inline_runner=lambda n, p, c: None)
    assert d2.run("timer", {}) == "t"


def test_dispatch_unknown_tool_and_errors_are_text():
    d = _dispatcher(open_app=RuntimeError("boom"))
    assert "Unknown tool" in d.run("nope", {})
    assert "failed" in d.run("open_app", {})


def test_dispatch_unbound_raises():
    d = ToolDispatcher()
    try:
        d.run("x", {})
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")
    assert d.is_bound is False


def test_available_tools_merges_registries():
    d = ToolDispatcher()
    d.bind(action_registry=FakeActions({"b": "1"}),
           plugin_registry=FakePluginsNew({"a": "2"}))
    assert d.available_tools() == ["a", "b"]


# ── planner: decomposition ───────────────────────────────────────────────────


def test_decompose_single_step_english_and_german():
    steps = planner.decompose("open Spotify", TOOLS)
    assert steps == [{"tool": "open_app", "params": {"app_name": "Spotify"},
                      "why": "open Spotify"}]
    steps = planner.decompose("schließe Discord", TOOLS)
    assert len(steps) == 1 and steps[0]["tool"] == "close_app"
    assert steps[0]["params"]["app_name"] == "Discord"


def test_decompose_multi_step_splits_clauses():
    steps = planner.decompose("open Spotify and then search for jazz", TOOLS)
    assert [s["tool"] for s in steps] == ["open_app", "web_search"]
    steps = planner.decompose("schließe Chrome und dann öffne Discord", TOOLS)
    assert [s["tool"] for s in steps] == ["close_app", "open_app"]


def test_decompose_skips_unavailable_tools():
    steps = planner.decompose("open Spotify", ["web_search"])
    assert steps == []


def test_plan_news_and_weather_ground_to_web_search():
    steps = planner.plan("news about AI", ["web_search"])
    assert len(steps) == 1 and steps[0]["tool"] == "web_search"
    assert steps[0]["params"].get("mode") == "news"
    steps = planner.plan("weather in Berlin", ["web_search"])
    assert steps[0]["tool"] == "web_search"
    assert "Berlin" in steps[0]["params"]["query"]
    with patch.object(lai, "plan_steps", return_value=None):
        assert planner.plan("weather in Berlin", ["timer"]) == []


def test_plan_empty_tool_list_plans_nothing():
    assert planner.plan("open Spotify", []) == []
    assert planner.plan("open Spotify", None)[0]["tool"] == "open_app"


def test_plan_reminder_needs_time():
    with patch.object(lai, "plan_steps", return_value=None):
        steps = planner.plan("remind me in 10 minutes to call mom",
                             ["reminder"])
        assert len(steps) == 1
        params = steps[0]["params"]
        assert set(params) == {"date", "time", "message"}
        assert "call mom" in params["message"]
        assert planner.plan("remind me to call mom", ["reminder"]) == []
        steps = planner.plan("erinnere mich morgen an den Zahnarzt",
                             ["reminder"])
        assert steps and "Zahnarzt" in steps[0]["params"]["message"]


def test_plan_save_memory_slug_key():
    steps = planner.plan("note that my cat is Minka", ["save_memory"])
    assert steps[0]["params"]["key"] == "my_cat_is_minka"


def test_plan_summarize_without_target_skipped():
    with patch.object(lai, "plan_steps", return_value=None):
        assert planner.plan("summarize", ["summarize"]) == []


def test_plan_falls_back_to_local_ai_then_empty():
    with patch.object(lai, "plan_steps", return_value=[
            {"tool": "open_app", "params": {"app_name": "X"}}]):
        steps = planner.plan("something the rules cannot parse", TOOLS)
        assert steps and steps[0]["tool"] == "open_app"
    with patch.object(lai, "plan_steps", return_value=None):
        assert planner.plan("something the rules cannot parse", TOOLS) == []
    with patch.object(lai, "plan_steps", return_value=[
            {"tool": "nope", "params": {}}]):
        # Ungrounded local steps are dropped, never executed.
        assert planner.plan("something the rules cannot parse", TOOLS) == []


# ── planner: execution ───────────────────────────────────────────────────────


def test_plan_never_emits_agent_task():
    with patch.object(lai, "plan_steps", return_value=[
            {"tool": "agent_task", "params": {"goal": "x"}},
            {"tool": "open_app", "params": {"app_name": "X"}}]):
        steps = planner.plan("something the rules cannot parse", TOOLS)
        assert [s["tool"] for s in steps] == ["open_app"]


def test_run_plan_single_step_reports_tool_words():
    d = _dispatcher(open_app="Spotify is now running.")
    with patch.object(planner, "verify",
                      return_value=ver.Verification(True, "ok", "recheck")):
        assert planner.run_plan("open Spotify", d) == "Spotify is now running."


def test_run_plan_multi_step_narrates_all_done():
    d = _dispatcher(open_app="Spotify is now running.",
                    web_search="Jazz: ...")
    with patch.object(planner, "verify",
                      return_value=ver.Verification(True, "ok", "text")):
        out = planner.run_plan("open Spotify and then search for jazz", d)
        assert "All 2 steps done" in out


def test_run_plan_stops_at_first_unverified_step():
    d = _dispatcher(open_app="Spotify is now running.",
                    web_search="Search failed: offline")
    calls = {"n": 0}

    def _fake_verify(tool, params, result, dispatcher=None):
        calls["n"] += 1
        if tool == "open_app":
            return ver.Verification(True, "running", "recheck")
        return ver.Verification(False, "search failed", "text")

    with patch.object(planner, "verify", side_effect=_fake_verify):
        out = planner.run_plan("open Spotify and then search for jazz", d)
    assert "Step 2" in out and "stopped" in out
    assert "Finished 1 of 2" in out or "Done:" in out


def test_run_plan_no_plan_and_no_dispatcher():
    d = _dispatcher(open_app="x")
    with patch.object(lai, "plan_steps", return_value=None):
        out = planner.run_plan("blargh flargh zzz", d)
        assert "could not break that down" in out
    assert "not connected" in planner.run_plan("open Spotify", None)


# ── verify ───────────────────────────────────────────────────────────────────


def test_is_failure_text():
    assert ver.is_failure_text("") is True
    assert ver.is_failure_text("Could not start Spotify.") is True
    assert ver.is_failure_text("Search failed: offline") is True
    assert ver.is_failure_text("Unknown tool 'x'.") is True
    assert ver.is_failure_text("Spotify is now running.") is False
    assert ver.is_failure_text("Volume set to 50%.") is False


def test_verify_open_app_rechecks_process_table():
    import core.app_controller as ac

    with patch.object(ac, "is_running", return_value=True):
        v = ver.verify("open_app", {"app_name": "Spotify"}, "Opened Spotify.")
        assert v.ok is True and v.method == "recheck"
    with patch.object(ac, "is_running", return_value=False):
        v = ver.verify("open_app", {"app_name": "Spotify"}, "Opened Spotify.")
        assert v.ok is False  # claimed success, process missing


def test_verify_close_app_rechecks_gone():
    import core.app_controller as ac

    with patch.object(ac, "is_running", return_value=False):
        assert ver.verify("close_app", {"app_name": "X"}, "Closed.").ok is True
    with patch.object(ac, "is_running", return_value=True):
        assert ver.verify("close_app", {"app_name": "X"}, "Closed.").ok is False


def test_verify_file_ops_check_disk(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        target = str(Path(td) / "note.txt")
        Path(target).write_text("hi", encoding="utf-8")
        v = ver.verify("file_processor", {"path": target}, "Read it.")
        assert v.ok is True
        v = ver.verify("file_controller",
                       {"action": "delete", "path": target}, "Deleted.")
        assert v.ok is False  # still there!
        Path(target).unlink()
        v = ver.verify("file_controller",
                       {"action": "delete", "path": target}, "Deleted.")
        assert v.ok is True


def test_verify_degrades_to_text_without_process_access():
    import core.app_controller as ac

    with patch.object(ac, "is_running", return_value=None):
        v = ver.verify("open_app", {"app_name": "X"}, "Opened X.")
        assert v.ok is True and v.method == "text"  # not a false failure
        v = ver.verify("close_app", {"app_name": "X"}, "Closed.")
        assert v.method == "text"
        v = ver.verify("open_app", {"app_name": "X"}, "Failed to open X.")
        assert v.ok is False  # text analysis still catches real failures


def test_verify_unknown_tool_uses_text():
    assert ver.verify("timer", {"action": "start"}, "Timer set.").ok is True
    assert ver.verify("timer", {"action": "start"}, "Timer failed.").ok is False


# ── agent_task action ────────────────────────────────────────────────────────


def test_agent_task_handler():
    import actions.agent_task as at

    assert "No goal" in at.agent_task({})
    # Unbound dispatcher refuses honestly.
    from core.dispatcher import get_dispatcher

    disp = get_dispatcher()
    assert "not connected" in at.agent_task(
        {"goal": "open Spotify"}, dispatcher=ToolDispatcher())
    assert isinstance(disp, ToolDispatcher)  # singleton intact
    # Happy path through a fake dispatcher.
    d = _dispatcher(open_app="Spotify is now running.")
    with patch.object(planner, "verify",
                      return_value=ver.Verification(True, "ok", "recheck")):
        out = at.agent_task({"goal": "open Spotify"}, dispatcher=d)
        assert out == "Spotify is now running."


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
