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
