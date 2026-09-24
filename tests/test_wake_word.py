from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path


class WakeWordReadinessTests(unittest.TestCase):
    def test_readiness_only_inspects_package_metadata(self) -> None:
        """A broken native package must not be executed by the settings check."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "openwakeword"
            models = package / "resources" / "models"
            models.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "raise RuntimeError('openwakeword must not be imported')\n",
                encoding="utf-8",
            )
            for name in (
                "hey_jarvis_v0.1.onnx",
                "melspectrogram.onnx",
                "embedding_model.onnx",
            ):
                (models / name).touch()

            old_path = list(sys.path)
            old_module = sys.modules.pop("openwakeword", None)
            sys.path.insert(0, temp)
            try:
                importlib.invalidate_caches()
                from core.wake_word import is_ready

                self.assertTrue(is_ready())
                self.assertNotIn("openwakeword", sys.modules)
            finally:
                sys.path[:] = old_path
                sys.modules.pop("openwakeword", None)
                if old_module is not None:
                    sys.modules["openwakeword"] = old_module
                importlib.invalidate_caches()


if __name__ == "__main__":
    unittest.main()
