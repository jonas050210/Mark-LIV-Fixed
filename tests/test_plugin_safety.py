from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core import plugin_loader
from core.plugin_loader import discover_plugins


class PluginDiscoverySafetyTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_world_writable_plugin_is_rejected_before_import(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "imported"
            plugin = root / "unsafe.py"
            plugin.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran')\n"
                "PLUGIN={'name':'unsafe','description':'unsafe',"
                "'parameters':{'type':'OBJECT','properties':{}}}\n"
                "def run(parameters): return 'ran'\n",
                encoding="utf-8",
            )
            plugin.chmod(0o666)

            registry = discover_plugins(root, set(), logger=lambda _message: None)

            self.assertFalse(registry.has("unsafe"))
            self.assertFalse(marker.exists())
            row = next(item for item in registry.list_for_ui() if item["name"] == "unsafe")
            self.assertIn("writable by other users", row["error"])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not authoritative on Windows")
    def test_world_writable_plugin_directory_is_rejected_before_import(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "imported"
            plugin = root / "unsafe.py"
            plugin.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ran')\n"
                "PLUGIN={'name':'unsafe','description':'unsafe',"
                "'parameters':{'type':'OBJECT','properties':{}}}\n"
                "def run(parameters): return 'ran'\n",
                encoding="utf-8",
            )
            root.chmod(0o777)
            try:
                registry = discover_plugins(root, set(), logger=lambda _message: None)
            finally:
                root.chmod(0o700)

            self.assertFalse(registry.has("unsafe"))
            self.assertFalse(marker.exists())
            row = next(item for item in registry.list_for_ui() if item["name"] == "unsafe")
            self.assertIn("directory is writable", row["error"])

    def test_plugin_worker_capacity_fails_fast_when_exhausted(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "blocked.py").write_text(
                "import threading\n"
                "gate=threading.Event()\n"
                "entered=threading.Event()\n"
                "PLUGIN={'name':'blocked_plugin','description':'blocked',"
                "'parameters':{'type':'OBJECT','properties':{}}}\n"
                "def run(parameters): entered.set(); gate.wait(5); return 'done'\n",
                encoding="utf-8",
            )
            registry = discover_plugins(root, set(), logger=lambda _message: None)
            record = registry._plugins["blocked_plugin"]
            original = plugin_loader._PLUGIN_RUN_SLOTS
            plugin_loader._PLUGIN_RUN_SLOTS = threading.BoundedSemaphore(1)
            results = []
            worker = threading.Thread(
                target=lambda: results.append(registry.execute("blocked_plugin", {}))
            )
            try:
                worker.start()
                self.assertTrue(record.run.__globals__["entered"].wait(1))
                started = time.monotonic()
                busy = registry.execute("blocked_plugin", {})
                self.assertEqual(busy.status, "busy")
                self.assertLess(time.monotonic() - started, 0.2)
            finally:
                record.run.__globals__["gate"].set()
                worker.join(2)
                plugin_loader._PLUGIN_RUN_SLOTS = original
            self.assertTrue(results)
            self.assertEqual(results[0].status, "succeeded")

    def test_broken_plugin_does_not_prevent_later_plugins_loading(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a_broken.py").write_text("raise RuntimeError('broken')\n", encoding="utf-8")
            (root / "b_good.py").write_text(
                "PLUGIN={'name':'good','description':'good',"
                "'parameters':{'type':'OBJECT','properties':{}}}\n"
                "def run(parameters): return {'ok':True,'status':'succeeded','message':'ok'}\n",
                encoding="utf-8",
            )

            registry = discover_plugins(root, set(), logger=lambda _message: None)

            self.assertTrue(registry.has("good"))
            self.assertEqual(registry.execute("good", {}).status, "succeeded")
            self.assertEqual(len(registry.list_for_ui()), 2)


if __name__ == "__main__":
    unittest.main()


class ImportSideEffectTests(unittest.TestCase):
    """A plugin that is rejected must not already have run.

    Discovery imports every file in the plugins directory and validates it
    afterwards, so a plugin thrown out for a malformed PLUGIN dict had executed
    its module body first. "Rejected" read like "did not run", and it was not
    true.
    """

    def _check(self, source: str) -> None:
        from core.plugin_loader import check_no_import_side_effects

        check_no_import_side_effects(source, "probe.py")

    def test_a_bare_call_at_import_time_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._check("import os\nos.system('echo pwned')\n")
        self.assertIn("runs code at import time", str(caught.exception))

    def test_assigning_into_another_object_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._check("import os\nos.environ['PWNED'] = '1'\n")
        self.assertIn("another object", str(caught.exception))

    def test_a_loop_at_import_time_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._check("for _ in range(10**9):\n    pass\n")

    def test_a_with_block_at_import_time_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._check("open('/tmp/x', 'w').close()\n")

    def test_an_ordinary_plugin_is_accepted(self) -> None:
        self._check(
            '"""Docstring."""\n'
            "import math\n"
            "import logging\n"
            "LOG = logging.getLogger(__name__)\n"
            "SCALE = math.pi\n"
            "class Helper:\n    pass\n"
            "def run(parameters, **kwargs):\n    return 'ok'\n"
            "try:\n    import json\nexcept ImportError:\n    json = None\n"
            "if __name__ == '__main__':\n    pass\n"
            "PLUGIN = {'name': 'x', 'description': 'y',"
            " 'parameters': {'type': 'OBJECT', 'properties': {}}, 'run': run}\n"
        )

    def test_the_shipped_template_still_passes(self) -> None:
        from pathlib import Path

        template = Path(__file__).resolve().parent.parent / "plugins" / "_template.py"
        self._check(template.read_text(encoding="utf-8"))

    def test_unparseable_source_is_refused_before_execution(self) -> None:
        with self.assertRaises(ValueError):
            self._check("def (:\n")

    def test_a_rejected_plugin_leaves_no_trace(self) -> None:
        """End to end: discovery must not run the body of a bad plugin."""
        import os
        import shutil
        import tempfile
        from pathlib import Path

        from core.plugin_loader import discover_plugins

        directory = Path(tempfile.mkdtemp()) / "plugins"
        directory.mkdir()
        bad = directory / "sneaky.py"
        bad.write_text(
            "import os\n"
            "os.environ['MARK_PLUGIN_SIDE_EFFECT'] = 'yes'\n"
            "PLUGIN = {'name': 'sneaky', 'description': 'x',"
            " 'parameters': {'type': 'OBJECT', 'properties': {}}, 'run': lambda p: 'x'}\n",
            encoding="utf-8",
        )
        bad.chmod(0o644)
        os.environ.pop("MARK_PLUGIN_SIDE_EFFECT", None)
        try:
            registry = discover_plugins(directory, set(), logger=lambda _message: None)
            self.assertFalse(registry.has("sneaky"))
            self.assertIsNone(os.environ.get("MARK_PLUGIN_SIDE_EFFECT"))
        finally:
            os.environ.pop("MARK_PLUGIN_SIDE_EFFECT", None)
            shutil.rmtree(directory.parent, ignore_errors=True)
