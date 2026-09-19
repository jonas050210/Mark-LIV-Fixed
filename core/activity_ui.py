"""Shared presentation for the main task workspace and the compact JARVIS window.

No task state or transcript is owned here: both views consume the existing
registry and main-window signals. All widget work stays on the Qt thread.
"""
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QLineEdit, QScrollArea, QSizeGrip)
from core.hud_layout import activity_layout, fit_window, Rect


class ActivitySurface(QWidget):
    def __init__(self, hud, panel, settings, parent=None):
        super().__init__(parent)
        self.hud, self.panel, self.settings = hud, panel, settings
        self._rows = []
        panel.snapshot_changed.connect(self._on_snapshot)
        hud.setParent(self)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidget(panel)
        self.scroll.hide()
        self.result = QLabel(self)
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setStyleSheet("background:#10232e; color:#d8f8ff; padding:8px;")
        self.result.hide()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.arrange)
        self.timer.start(50)

    def _on_snapshot(self, rows):
        self._rows = rows
        self.arrange()

    def arrange(self):
        from core import tasks
        if not self.isVisible():
            return
        cfg = self.settings()
        rows = self._rows
        live = any(row["state"] in tasks.ACTIVE_STATES for row in rows)
        finished = [row for row in rows if row["state"] in tasks.TERMINAL_STATES]
        show_result = bool(finished) and not live
        footer = min(64, self.height() // 4) if show_result else 0
        self.hud.setGeometry(0, 0, self.width(), self.height() - footer)
        self.result.setVisible(show_result)
        if show_result:
            row = max(finished, key=lambda r: r["finished_at"] or 0)
            state = "COMPLETED" if row["state"] == "done" else row["state"].upper()
            self.result.setText(f"{state} · {row['title']}\n{row['error'] or row['detail']}")
            self.result.setGeometry(8, self.height() - footer, max(1, self.width() - 16), footer)
        dock, workspace = activity_layout(self.width(), self.height() - footer,
                                          cfg.get("hud_anchor", "topright"),
                                          cfg.get("hud_size", 1.0))
        self.scroll.setGeometry(round(workspace.x), round(workspace.y),
                                round(workspace.width), round(workspace.height))
        # Disabling animated task layout must not disable the task workspace.
        static_dock = live and not cfg.get("snap_hud_on_task", True)
        if static_dock:
            self.hud.setGeometry(round(dock.x), round(dock.y),
                                 max(1, round(dock.width)), max(1, round(dock.height)))
        active = static_dock or (self.hud.is_task_active() and self.hud._layout_t > .97)
        self.scroll.setVisible(active)
        if active:
            self.scroll.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange()


class MiniWindow(QWidget):
    closed = pyqtSignal()
    bring_forward = pyqtSignal()
    send = pyqtSignal(str)
    mute = pyqtSignal()
    interrupt = pyqtSignal()

    def __init__(self, hud, log, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("JARVIS Mini")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.resize(360, 440)
        self.setMinimumSize(300, 320)
        self.setStyleSheet("QWidget {background:#07131e; color:#daf4ff;} "
                           "QPushButton,QLineEdit {padding:6px; border:1px solid #306070;}")
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.status = QLabel("READY")
        top.addWidget(self.status, 1)
        forward = QPushButton("Open JARVIS")
        forward.clicked.connect(self.bring_forward)
        top.addWidget(forward)
        layout.addLayout(top)
        self.hud = hud
        hud.compact = True
        hud.setMinimumHeight(90)
        hud.setMaximumHeight(120)
        layout.addWidget(hud)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.activity = QLabel("No active task")
        self.activity.setTextFormat(Qt.TextFormat.PlainText)
        self.activity.setWordWrap(True)
        layout.addWidget(self.activity)
        layout.addWidget(log, 1)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask JARVIS…")
        self.input.returnPressed.connect(self._send)
        layout.addWidget(self.input)
        actions = QHBoxLayout()
        for title, signal in (("Mute", self.mute), ("Stop speaking", self.interrupt)):
            button = QPushButton(title)
            button.clicked.connect(signal)
            actions.addWidget(button)
        actions.addWidget(QSizeGrip(self))
        layout.addLayout(actions)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.timeout.connect(self._save_geometry)
        from memory.config_manager import get_mini_geometry
        saved = get_mini_geometry()
        if saved:
            self.setGeometry(saved["x"], saved["y"], saved["width"], saved["height"])

    def _save_geometry(self):
        from memory.config_manager import save_mini_geometry
        g = self.geometry()
        save_mini_geometry(dict(x=g.x(), y=g.y(), width=g.width(), height=g.height()))

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.isVisible() and hasattr(self, "_geometry_timer"):
            self._geometry_timer.start(350)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isVisible() and hasattr(self, "_geometry_timer"):
            self._geometry_timer.start(350)

    def closeEvent(self, event):
        self._geometry_timer.stop()
        self._save_geometry()
        event.accept()
        self.closed.emit()

    def _send(self):
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.send.emit(text)

    def show_confirmation(self, title, detail):
        self.notice.setText(f"Confirmation required: {title}\nOpen JARVIS to review. No action runs until you confirm.")
        self.notice.setToolTip(detail)
        self.notice.show()

    def hide_confirmation(self):
        self.notice.hide()

    def summary(self, title, detail, count, kind, state):
        self.activity.setText(f"{state.upper()} · {title} · {detail}" if count else "No active task")

    def show_passively(self, screen):
        if screen is None:
            return
        area = screen.availableGeometry()
        # Keep the whole native frame, including title bar, on the work area.
        frame, client = self.frameGeometry(), self.geometry()
        extra_w = max(0, frame.width() - client.width())
        extra_h = max(0, frame.height() - client.height())
        self.setMinimumSize(min(300, max(1, area.width() - extra_w)),
                            min(320, max(1, area.height() - extra_h)))
        desired = Rect(frame.x(), frame.y(), frame.width(), frame.height())
        bounds = Rect(area.x(), area.y(), area.width(), area.height())
        fitted = fit_window(desired, bounds)
        self.resize(max(1, round(fitted.width) - extra_w), max(1, round(fitted.height) - extra_h))
        self.move(round(fitted.x), round(fitted.y))
        self.show()  # WA_ShowWithoutActivating; never activateWindow here
