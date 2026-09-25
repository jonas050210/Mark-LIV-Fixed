"""A smoke test for the parts of start-up that do not need a GUI or a key.

Everything else in this suite tests one module at a time, which leaves the
question the unit tests cannot answer: does the application still assemble?
Importing ``main`` pulls in every action, the dashboard, the plugin loader and
the scheduler, and a mistake there — a circular import, a renamed symbol, a
tool schema that no longer matches its handler — is invisible until launch.

The GUI and the Gemini session are not started: no API key and no display are
needed for anything checked here.
"""
from __future__ import annotations

import inspect
import unittest


class ImportSmokeTests(unittest.TestCase):
    def test_every_action_module_imports(self) -> None:
        import importlib
        from pathlib import Path

        actions_dir = Path(__file__).resolve().parent.parent / "actions"
        failures = []
        for path in sorted(actions_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                importlib.import_module(f"actions.{path.stem}")
            except Exception as exc:  # pragma: no cover - the failure is the point
                failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [])

    def test_every_file_handler_imports(self) -> None:
        import importlib

        for name in ("common", "images", "documents", "data", "code", "media", "archives"):
            importlib.import_module(f"actions.file_handlers.{name}")

    def test_the_core_package_imports(self) -> None:
        import importlib

        for name in (
            "action_loader", "action_runtime", "app_index", "app_icons",
            "background_scheduler", "browser_handoff", "confirm", "json_store",
            "path_policy", "sandbox", "text_match", "undo", "window_manager",
        ):
            importlib.import_module(f"core.{name}")


class RegistrySmokeTests(unittest.TestCase):
    """The registry is what the model sees; it has to be internally consistent."""

    @classmethod
    def setUpClass(cls) -> None:
        from pathlib import Path

        from core.action_loader import discover_actions

        cls.registry = discover_actions(
            Path(__file__).resolve().parent.parent / "actions", logger=lambda _m: None
        )
        cls.tools = cls.registry.get_tool_declarations()
        cls.records = {
            record.name: record for record in cls.registry._actions.values()
        }

    def test_actions_are_discovered(self) -> None:
        self.assertGreaterEqual(len(self.tools), 10)

    def test_every_tool_has_a_callable_handler(self) -> None:
        for tool in self.tools:
            record = self.records[tool["name"]]
            self.assertTrue(callable(record.handler), tool["name"])

    def test_every_handler_accepts_at_least_one_argument(self) -> None:
        for tool in self.tools:
            handler = self.records[tool["name"]].handler
            signature = inspect.signature(handler)
            self.assertTrue(signature.parameters, tool["name"])

    def test_tool_names_are_unique(self) -> None:
        names = [tool["name"] for tool in self.tools]
        self.assertEqual(len(names), len(set(names)))

    def test_every_tool_declares_typed_parameters(self) -> None:
        for tool in self.tools:
            params = tool.get("parameters", {})
            self.assertEqual(params.get("type", "OBJECT").upper(), "OBJECT", tool["name"])
            for field, spec in params.get("properties", {}).items():
                self.assertIn("type", spec, f"{tool['name']}.{field}")

    def test_no_tool_still_advertises_a_removed_action(self) -> None:
        removed = {"screen_find", "screen_click", "organize_desktop"}
        for tool in self.tools:
            enums = [
                value
                for spec in tool.get("parameters", {}).get("properties", {}).values()
                for value in spec.get("enum", [])
            ]
            self.assertFalse(removed & set(enums), tool["name"])

    def test_confirmation_actions_name_real_options(self) -> None:
        """A confirmation gate pointing at an action that no longer exists
        would silently stop gating anything."""
        import importlib

        for tool in self.tools:
            module = importlib.import_module(f"actions.{_module_for(tool['name'])}")
            gated = getattr(module, "TOOL", {}).get("confirmation_actions") or []
            if not gated:
                continue
            spec = tool.get("parameters", {}).get("properties", {}).get("action", {})
            enum = set(spec.get("enum", []))
            if not enum:
                continue
            self.assertTrue(set(gated) <= enum, f"{tool['name']}: {set(gated) - enum}")


def _module_for(tool_name: str) -> str:
    """Map a tool name back to its module; they match except where noted."""
    return {"system_control": "computer_settings"}.get(tool_name, tool_name)


try:
    import main as _main_module

    _MAIN_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on optional audio packages
    _main_module = None
    _MAIN_ERROR = f"{type(exc).__name__}: {exc}"


@unittest.skipIf(_main_module is None, f"main.py needs optional packages ({_MAIN_ERROR})")
class AssemblySmokeTests(unittest.TestCase):
    def test_main_imports_without_starting_anything(self) -> None:
        main = _main_module
        self.assertTrue(hasattr(main, "JarvisLive"))
        self.assertTrue(hasattr(main, "main"))

    def test_the_background_scheduler_is_wired_with_both_jobs(self) -> None:
        main = _main_module

        live = main.JarvisLive.__new__(main.JarvisLive)
        live._may_interrupt = lambda: True
        live._check_monitored_topics = lambda: None
        live._proactive_check_in = lambda: None
        scheduler = main.JarvisLive._build_scheduler(live)
        names = sorted(job.name for job in scheduler.jobs)
        self.assertEqual(names, ["proactive check-in", "topic monitor"])

    def test_the_topic_monitor_waits_before_its_first_check(self) -> None:
        main = _main_module

        live = main.JarvisLive.__new__(main.JarvisLive)
        live._may_interrupt = lambda: True
        live._check_monitored_topics = lambda: None
        live._proactive_check_in = lambda: None
        monitor = next(
            job for job in main.JarvisLive._build_scheduler(live).jobs
            if job.name == "topic monitor"
        )
        self.assertGreaterEqual(monitor.initial_delay, 60)

    def test_a_background_job_stays_silent_without_a_session(self) -> None:
        import threading

        main = _main_module

        live = main.JarvisLive.__new__(main.JarvisLive)
        live.session = None
        live._awake = True
        live._is_speaking = False
        live._speaking_lock = threading.Lock()
        live._last_user_speech = 0.0
        self.assertFalse(main.JarvisLive._may_interrupt(live))

    def test_a_background_job_stays_silent_while_speaking(self) -> None:
        import threading
        import time as _time

        main = _main_module

        live = main.JarvisLive.__new__(main.JarvisLive)
        live.session = object()
        live._awake = True
        live._is_speaking = True
        live._speaking_lock = threading.Lock()
        live._last_user_speech = _time.monotonic() - 600
        self.assertFalse(main.JarvisLive._may_interrupt(live))

    def test_a_background_job_stays_silent_right_after_the_user_spoke(self) -> None:
        import threading
        import time as _time

        main = _main_module

        live = main.JarvisLive.__new__(main.JarvisLive)
        live.session = object()
        live._awake = True
        live._is_speaking = False
        live._speaking_lock = threading.Lock()
        live._last_user_speech = _time.monotonic()
        self.assertFalse(main.JarvisLive._may_interrupt(live))

    def test_a_background_job_may_speak_into_a_long_silence(self) -> None:
        import threading
        import time as _time

        main = _main_module

        live = main.JarvisLive.__new__(main.JarvisLive)
        live.session = object()
        live._awake = True
        live._is_speaking = False
        live._speaking_lock = threading.Lock()
        live._last_user_speech = _time.monotonic() - 600
        self.assertTrue(main.JarvisLive._may_interrupt(live))


if __name__ == "__main__":
    unittest.main()
