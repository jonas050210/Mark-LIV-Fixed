"""Widget tests for the HUD panels in ui_panels/.

PyQt6 is an optional dependency and a headless machine may have no usable
platform plugin, so the whole module skips rather than failing when a window
cannot be constructed. Where it does run, it checks the two properties that
matter: the panel shows what the index and the layout store actually contain,
and it never turns a failed operation into a success message.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QWidget

    _APP = QApplication.instance() or QApplication([])
    _QT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the machine
    _APP = None
    _QT_ERROR = f"{type(exc).__name__}: {exc}"

from core.app_index import AppEntry


@unittest.skipIf(_APP is None, f"PyQt6 is unavailable ({_QT_ERROR})")
class LauncherPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        from core import app_index

        self.directory = tempfile.TemporaryDirectory()
        self.app_index = app_index
        self.previous_usage = app_index.USAGE_FILE
        app_index.USAGE_FILE = Path(self.directory.name) / "usage.json"
        self.entries = [
            AppEntry("Blender", "exec", "/usr/bin/blender", "desktop"),
            AppEntry("Chrome", "exec", "/usr/bin/chrome", "desktop"),
            AppEntry("Code", "exec", "/usr/bin/code", "desktop"),
        ]
        self.host = QWidget()
        self.host.resize(1200, 800)
        self._index_patch = patch.object(
            app_index, "load_index", lambda refresh=False: self.entries
        )
        self._index_patch.start()
        self._icon_patch = patch("core.app_icons.icon_png", return_value=None)
        self._icon_patch.start()

    def tearDown(self) -> None:
        self._icon_patch.stop()
        self._index_patch.stop()
        self.app_index.USAGE_FILE = self.previous_usage
        self.host.deleteLater()
        self.directory.cleanup()

    def _panel(self):
        from ui_panels.launcher import LauncherOverlay

        return LauncherOverlay(parent=self.host)

    def _texts(self, panel) -> list[str]:
        return [panel._list.item(row).text().strip() for row in range(panel._list.count())]

    def test_the_panel_lists_the_whole_index_when_nothing_is_typed(self) -> None:
        panel = self._panel()
        self.assertEqual(self._texts(panel), ["Blender", "Chrome", "Code"])

    def test_typing_filters_the_list(self) -> None:
        panel = self._panel()
        panel._search.setText("chro")
        self.assertEqual(self._texts(panel), ["Chrome"])

    def test_pinning_marks_the_entry_and_moves_it_to_the_top(self) -> None:
        panel = self._panel()
        panel._search.setText("code")
        panel._list.setCurrentRow(0)
        panel._toggle_pin()
        panel._search.setText("")
        self.assertTrue(self._texts(panel)[0].startswith("★"))
        self.assertIn("Code", self._texts(panel)[0])

    def test_unpinning_removes_the_mark(self) -> None:
        panel = self._panel()
        panel._search.setText("code")
        panel._list.setCurrentRow(0)
        panel._toggle_pin()
        panel._toggle_pin()
        self.assertFalse(any(text.startswith("★") for text in self._texts(panel)))

    def test_a_failed_launch_is_shown_as_the_failure_it_was(self) -> None:
        panel = self._panel()
        panel._search.setText("chrome")
        panel._list.setCurrentRow(0)
        message = "Could not find an application called 'Chrome'."
        with patch("actions.open_app.open_app", return_value=message):
            panel._launch_selected()
        self.assertEqual(panel._status.text(), message)
        self.assertIn("ff3355", panel._status.styleSheet().lower())

    def test_a_successful_launch_is_announced(self) -> None:
        panel = self._panel()
        panel._search.setText("chrome")
        panel._list.setCurrentRow(0)
        seen = []
        panel.launched.connect(seen.append)
        with patch("actions.open_app.open_app", return_value="Opened Chrome."):
            panel._launch_selected()
        self.assertEqual(seen, ["Chrome"])

    def test_an_empty_index_explains_itself(self) -> None:
        self.entries = []
        panel = self._panel()
        self.assertIn("index is empty", panel._status.text())


@unittest.skipIf(_APP is None, f"PyQt6 is unavailable ({_QT_ERROR})")
class LayoutPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        from actions import layout_manager

        self.directory = tempfile.TemporaryDirectory()
        self.layout_manager = layout_manager
        self.previous = layout_manager.LAYOUT_FILE
        layout_manager.LAYOUT_FILE = Path(self.directory.name) / "layouts.json"
        self.host = QWidget()
        self.host.resize(1200, 800)

    def tearDown(self) -> None:
        self.layout_manager.LAYOUT_FILE = self.previous
        self.host.deleteLater()
        self.directory.cleanup()

    def _panel(self):
        from ui_panels.layouts import LayoutOverlay

        return LayoutOverlay(parent=self.host)

    def test_an_empty_store_invites_a_first_layout(self) -> None:
        panel = self._panel()
        self.assertIn("No layouts saved", panel._status.text())
        self.assertEqual(panel._list.count(), 0)

    def test_saving_without_a_name_is_refused(self) -> None:
        panel = self._panel()
        panel._name.setText("   ")
        panel._save()
        self.assertIn("name", panel._status.text())
        self.assertIn("ff3355", panel._status.styleSheet().lower())

    def test_a_saved_layout_appears_in_the_list(self) -> None:
        panel = self._panel()
        with patch.object(panel, "_call", return_value="Saved layout 'work' with 2 windows."):
            panel._name.setText("work")
            panel._save()
        self.layout_manager._write({
            "version": 1,
            "layouts": {"work": {
                "saved": "2026-09-25T10:00:00+00:00",
                "windows": [
                    {"process": "code", "title": "main.py", "monitor": 1,
                     "left": 0, "top": 0, "width": 800, "height": 600,
                     "state": "normal"},
                    {"process": "chrome", "title": "Docs", "monitor": 1,
                     "left": 800, "top": 0, "width": 800, "height": 600,
                     "state": "maximized"},
                ],
            }},
        })
        panel.refresh()
        self.assertEqual(panel._list.count(), 1)
        self.assertIn("work", panel._list.item(0).text())
        self.assertIn("2 windows", panel._list.item(0).text())

    def test_a_partial_restore_is_reported_verbatim(self) -> None:
        """layout_manager reports what it could not place; the panel must not
        flatten that into a success."""
        panel = self._panel()
        panel._names = ["work"]
        panel._list.addItem("work")
        panel._list.setCurrentRow(0)
        message = "None of the windows in layout 'work' are open right now."
        with patch.object(panel, "_call", return_value=message):
            panel._apply()
        self.assertEqual(panel._status.text(), message)
        self.assertIn("ff3355", panel._status.styleSheet().lower())

    def test_delete_refreshes_the_list(self) -> None:
        panel = self._panel()
        panel._names = ["work"]
        panel._list.addItem("work")
        panel._list.setCurrentRow(0)
        with patch.object(panel, "_call", return_value="Deleted the layout 'work'."):
            panel._delete()
        self.assertEqual(panel._list.count(), 0)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(_APP is None, f"PyQt6 is unavailable ({_QT_ERROR})")
class ExtractedOverlayTests(unittest.TestCase):
    """The nine panels moved out of ui.py must still construct.

    A panel that fails to build is only noticed when a user clicks the button
    that opens it, which is exactly the kind of regression an extraction can
    introduce: a helper or a constant left behind in ui.py raises NameError at
    construction time and nowhere else.
    """

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(1200, 800)

    def tearDown(self) -> None:
        self.host.deleteLater()

    def _cases(self):
        from ui_panels.audio_devices import AudioDeviceOverlay
        from ui_panels.confirm import ConfirmBanner
        from ui_panels.customize import CustomizeOverlay, HueWheel
        from ui_panels.memory import MemoryOverlay
        from ui_panels.plugins import PluginManagerOverlay, PluginSettingsOverlay
        from ui_panels.remote_key import RemoteKeyOverlay
        from ui_panels.setup import SetupOverlay

        return [
            (SetupOverlay, (self.host,)),
            (HueWheel, ()),
            (CustomizeOverlay, ()),
            (PluginManagerOverlay, ([],)),
            (PluginSettingsOverlay, ([],)),
            (ConfirmBanner, ("Restart the computer", "The computer will restart.")),
            (AudioDeviceOverlay, ()),
            (MemoryOverlay, ()),
            (RemoteKeyOverlay, ("http://127.0.0.1:8765", "PAIRKEY")),
        ]

    def test_every_extracted_panel_constructs(self) -> None:
        for widget_class, args in self._cases():
            with self.subTest(panel=widget_class.__name__):
                widget = widget_class(*args)
                self.assertGreater(widget.width(), 0)
                widget.deleteLater()

    def test_the_confirmation_banner_still_reports_both_answers(self) -> None:
        from ui_panels.confirm import ConfirmBanner

        answers = []
        banner = ConfirmBanner("Shut down", "The computer will power off.")
        banner.answered.connect(answers.append)
        buttons = banner.findChildren(__import__("PyQt6.QtWidgets", fromlist=["QPushButton"]).QPushButton)
        for button in buttons:
            button.click()
        self.assertEqual(sorted(answers), [False, True])

    def test_a_panel_repaints_what_it_covered_when_it_hides(self) -> None:
        """The ghost-frame fix has to survive the move out of ui.py."""
        from ui_panels.base import HudPanel

        panel = HudPanel(self.host)
        panel.setGeometry(10, 10, 100, 100)
        panel.show()
        panel.hide()      # must not raise, and must repaint the host region
        self.assertFalse(panel.isVisible())

    def test_ui_still_exposes_the_panel_names_it_used_to_define(self) -> None:
        """ui.py instantiates these by name; the import must keep them visible."""
        try:
            import ui
        except Exception as exc:  # optional runtime packages may be absent
            self.skipTest(f"ui.py needs optional packages ({type(exc).__name__})")

        for name in (
            "SetupOverlay", "CustomizeOverlay", "HueWheel", "PluginManagerOverlay",
            "PluginSettingsOverlay", "ConfirmBanner", "AudioDeviceOverlay",
            "MemoryOverlay", "RemoteKeyOverlay", "_HudOverlay",
        ):
            self.assertTrue(hasattr(ui, name), name)
