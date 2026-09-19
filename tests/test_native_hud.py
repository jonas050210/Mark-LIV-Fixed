"""Real Qt construction and layout smoke. Run on Windows before release."""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_native_settings_workspace_and_mini_share_state(tmp_path, monkeypatch):
    qt = pytest.importorskip("PyQt6.QtWidgets", reason="Native Qt host libraries unavailable", exc_type=ImportError)
    from PyQt6.QtCore import Qt
    from core import tasks
    from memory import config_manager as cm
    import ui
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cm, "CONFIG_DIR", tmp_path)
    ui.reload_gui_settings()
    app = qt.QApplication.instance() or qt.QApplication([])
    with patch.object(ui.MainWindow, "_update_metrics"), patch.object(ui.MainWindow, "_check_config", return_value=True):
        window = ui.MainWindow("")
    try:
        window.show()
        app.processEvents()
        window._open_gui_settings()  # catches missing imports and broken controls
        app.processEvents()
        assert window._gui_overlay._collect()["standard_german_voice"] is True
        assert window._gui_overlay._mini_mode.text() == "JARVIS Mini Mode"
        window._gui_overlay.hide()
        assert window._mini.hud is not window.hud
        mini_chat = window._mini.findChild(qt.QTextEdit)
        assert mini_chat.document() is window._log.document()
        assert window._mini.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert window._activity_surface.scroll.widget() is window._task_panel
        task = tasks.start("agent", "Test plan", pausable=True,
                           steps=[{"title": "First", "state": "running"}])
        window.hud.set_task_active(True)
        window.hud._layout_t = 1
        window._task_panel._poll()
        window._activity_surface.arrange()
        assert window._activity_surface.scroll.isVisible()
        assert "Step 1 ▶" in window._task_panel._rows[task.id]._steps.text()
        task.fail("Verified failure")
        window._task_panel._poll()
        window.hud.set_task_active(False)
        window._activity_surface.arrange()
        assert not window._activity_surface.scroll.isVisible()
        assert "FAILED" in window._activity_surface.result.text()
        for factor in (.75, 1, 1.25, 1.5):
            cm.save_gui_settings({"ui_scale": factor, "font_scale": factor})
            window._on_gui_settings_saved(cm.get_gui_settings())
            app.processEvents()
            assert window._input.font().pointSize() >= 9
        assert not window.grab().isNull()
        assert not window.hud.grab().isNull()
    finally:
        tasks.reset()
        window.close()
        app.processEvents()
        monkeypatch.undo()
        ui.reload_gui_settings()


def test_native_control_scaling_is_reversible():
    qt = pytest.importorskip("PyQt6.QtWidgets", reason="Native Qt host libraries unavailable", exc_type=ImportError)
    from PyQt6.QtGui import QFont
    from core.ui_scaling import refresh_font
    app = qt.QApplication.instance() or qt.QApplication([])
    button = qt.QPushButton("Pause")
    button.setFixedHeight(20)
    button.setProperty("baseFontPoints", 9)
    heights = []
    for scale in (1, 2, 1):
        def font(base, bold=False):
            return QFont("Arial", int(base * scale), QFont.Weight.Bold if bold else QFont.Weight.Normal)
        refresh_font(button, font, {})
        heights.append(button.minimumHeight())
        assert button.maximumHeight() == button.minimumHeight()
        assert button.minimumHeight() >= button.fontMetrics().height() + 10
    assert heights[1] > heights[0] == heights[2]
    button.deleteLater()
    app.processEvents()
