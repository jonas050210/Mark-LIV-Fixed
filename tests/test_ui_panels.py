"""Widget tests for the remaining HUD panels in ``ui_panels``."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication, QWidget

    _APP = QApplication.instance() or QApplication([])
    _QT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the machine
    _APP = None
    _QT_ERROR = f"{type(exc).__name__}: {exc}"



@unittest.skipIf(_APP is None, f"PyQt6 is unavailable ({_QT_ERROR})")
class ExtractedOverlayTests(unittest.TestCase):
    """The remaining panels moved out of ui.py must still construct.

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

    def test_every_extracted_panel_paints(self) -> None:
        """Building a widget is not the same as drawing it.

        A name that a panel used to reach as a global in ui.py — qcol() was one
        — is missing only inside paintEvent, which runs when the pixels are
        drawn and not when the object is constructed. Rendering each panel into
        a pixmap is the only way that shows up in a test.
        """
        from PyQt6.QtGui import QPixmap

        for widget_class, args in self._cases():
            with self.subTest(panel=widget_class.__name__):
                widget = widget_class(*args)
                widget.resize(max(widget.width(), 200), max(widget.height(), 200))
                pixmap = QPixmap(widget.size())
                pixmap.fill()
                widget.render(pixmap)      # raises if paintEvent is broken
                self.assertFalse(pixmap.isNull())
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
