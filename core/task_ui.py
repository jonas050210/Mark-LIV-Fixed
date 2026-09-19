"""Compact task widgets; consume the shared registry, never own execution state."""
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QProgressBar, QSizePolicy)
from core import tasks as _task_registry


class TaskRow(QFrame):
    """One line of the activity panel: what is running, how far, and a stop button.

    Updated in place rather than rebuilt — a download reports several times a
    second, and recreating widgets at that rate is exactly the kind of work the
    HUD should not be doing.
    """

    def __init__(self, task_id: str, theme, font, parent=None):
        C, scaled_font = theme, font
        super().__init__(parent)
        self._STATE_COLOUR = {"running": C.PRI, "done": C.GREEN, "failed": C.MUTED_C, "cancelled": C.TEXT_MED}
        self.theme = C
        self.task_id = task_id
        self.setObjectName("TaskRow")
        self.setStyleSheet(
            f"QFrame#TaskRow {{ background: {C.PANEL2}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 5)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(4)
        self._title = QLabel("")
        self._title.setFont(scaled_font(10, bold=True))
        self._title.setMinimumWidth(0)
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._title.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        self._state = QLabel("")
        self._state.setFont(scaled_font(9, bold=True))
        self._state.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._cancel = QPushButton("\u2715")
        self._cancel.setFont(scaled_font(9))
        side = max(22, self._cancel.fontMetrics().height() + 8)
        self._cancel.setFixedSize(side, side)
        self._cancel.setToolTip("Stop this task")
        self._cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel.setStyleSheet(
            f"QPushButton {{ color: {C.TEXT_DIM}; background: transparent; "
            f"border: none; }} QPushButton:hover {{ color: {C.MUTED_C}; }}"
        )
        self._cancel.clicked.connect(self._request_cancel)
        top.addWidget(self._title, stretch=1)
        top.addWidget(self._state)
        top.addWidget(self._cancel)
        lay.addLayout(top)

        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setRange(0, 100)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {C.PANEL}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {C.PRI}; border-radius: 3px; }}"
        )
        lay.addWidget(self._bar)

        controls = QHBoxLayout()
        self._pause = QPushButton("Pause")
        self._pause.setFont(scaled_font(9))
        self._pause.clicked.connect(self._toggle_pause)
        controls.addWidget(self._pause)
        details = QPushButton("Details")
        details.setFont(scaled_font(9))
        details.clicked.connect(self._inspect)
        controls.addWidget(details)
        controls.addStretch()
        lay.addLayout(controls)
        self._steps = QLabel("")
        self._steps.setWordWrap(True)
        self._steps.setTextFormat(Qt.TextFormat.PlainText)
        self._steps.setFont(scaled_font(9))
        lay.addWidget(self._steps)
        self._detail = QLabel("")
        self._detail.setFont(scaled_font(9))
        self._detail.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._detail.setWordWrap(True)
        self._detail.setProperty("maxTextLines", 2)
        self._detail.setMaximumHeight(self._detail.fontMetrics().lineSpacing() * 2 + 4)
        lay.addWidget(self._detail)

    def _toggle_pause(self):
        task = _task_registry.get(self.task_id) if _task_registry else None
        if task:
            task.resume() if task.pause_requested else task.request_pause()

    def _inspect(self):
        from PyQt6.QtWidgets import QDialog, QTextEdit, QDialogButtonBox
        snap = getattr(self, "_snapshot", {})
        meta = snap.get("meta", {})
        steps = [f"{i + 1}. [{step.get('state', 'queued')}] {step.get('title', '')}"
                 for i, step in enumerate(meta.get("steps", []))]
        text = "\n".join(str(x) for x in (snap.get("detail"), snap.get("error"), *steps,
                           *meta.get("unplanned", []), meta.get("output"),
                           *meta.get("events", [])) if x)
        dialog = QDialog(self.window())
        dialog.setWindowTitle(str(snap.get("title", "Task")))
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.resize(min(640, self.window().width()), min(480, self.window().height()))
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(self._detail.font())
        view.setPlainText(text or "No additional details.")
        layout.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        dialog.show()

    def _request_cancel(self):
        """Ask the owner to stop. The owner's loop notices and does the rest."""
        if _task_registry is None:
            return
        try:
            row = next((t for t in _task_registry.snapshot(limit=32)
                        if t["id"] == self.task_id), None)
            if row and row["state"] in ("running", "queued", "paused"):
                _task_registry.request_cancel(self.task_id)
                self._state.setText("CANCELLING")
        except Exception:
            pass

    def set_data(self, snap: dict) -> None:
        C = self.theme
        state = str(snap.get("state") or "running")
        colour = self._STATE_COLOUR.get(state, C.TEXT_MED)
        self._title.setToolTip(str(snap.get("title", "")))
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setText(
            f"{snap.get('label', chr(0x2022))} {snap.get('title', '')}".strip()[:64])
        label = "COMPLETED" if state == "done" else state.upper()
        if state in ("running", "queued", "paused") and snap.get("cancel_requested"):
            label = "CANCEL REQUESTED"
        elif state != "paused" and snap.get("pause_requested"):
            label = "PAUSE REQUESTED"
        self._state.setText(label)
        self._state.setStyleSheet(f"color: {colour}; background: transparent;")
        self._cancel.setVisible(state in ("running", "queued", "paused"))
        self._cancel.setEnabled(not snap.get("cancel_requested"))
        self._pause.setVisible(bool(snap.get("pausable")) and state in ("running", "queued", "paused"))
        self._pause.setText("Resume" if snap.get("pause_requested") else "Pause")
        self._snapshot = snap

        steps = snap.get("meta", {}).get("steps", [])
        symbols = {"done": "✓", "running": "▶", "queued": "○", "failed": "✕", "blocked": "–"}
        self._steps.setText("  ·  ".join(f"Step {i+1} {symbols.get(step.get('state'), '○')}"
                                           for i, step in enumerate(steps)))
        self._steps.setVisible(bool(steps))
        progress = snap.get("progress")
        if state == "running" and progress is None:
            self._bar.setRange(0, 0)          # busy indicator: honest "unknown"
        else:
            self._bar.setRange(0, 100)
            if state == "done":
                self._bar.setValue(100)
            elif state == "failed":
                self._bar.setValue(0 if progress is None else int(progress * 100))
            else:
                self._bar.setValue(int((progress or 0.0) * 100))
        chunk = C.MUTED_C if state == "failed" else (
            C.TEXT_DIM if state == "cancelled" else C.PRI)
        self._bar.setStyleSheet(
            f"QProgressBar {{ background: {C.PANEL}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}"
        )
        self._detail.setText("  \u00b7  ".join(
            part for part in (self._line(snap), (snap.get("detail") or "").strip())
            if part)[:600])

    @staticmethod
    def _line(snap: dict) -> str:
        """Numbers, not adjectives: bytes/speed/ETA only when they are known."""
        bits = []
        done, total = snap.get("done_bytes"), snap.get("total_bytes")
        if total:
            bits.append(f"{snap.get('done_human', '')} / {snap.get('total_human', '')}"
                        if done is not None else snap.get("total_human", ""))
        elif done:
            bits.append(snap.get("done_human", ""))
        if snap.get("percent") is not None and snap.get("state") == "running":
            bits.append(f"{snap['percent']}%")
        if snap.get("speed_human") and snap.get("state") == "running":
            bits.append(snap["speed_human"])
        if snap.get("eta_human") and snap.get("state") == "running":
            bits.append(f"ETA {snap['eta_human']}")
        if snap.get("state") in ("done", "failed", "cancelled") and snap.get("elapsed_human"):
            bits.append(f"took {snap['elapsed_human']}")
        return "  \u00b7  ".join(b for b in bits if b)


class TaskPanel(QWidget):
    """Live view of core/tasks: downloads, timers, installs and agent runs.

    Polling, not signals: the registry is a small dict behind a lock, so a 4 Hz
    QTimer that reads `revision()` and only touches the widgets when it changes
    keeps the HUD out of every other thread's way — the same pattern the content
    panel uses via `_content_sig`. The panel hides itself when nothing is
    running so it never eats layout space for no reason.
    """

    snapshot_changed = pyqtSignal(object)

    _POLL_MS = 250
    _MAX_ROWS = 32

    def __init__(self, theme, font, parent=None):
        super().__init__(parent)
        C = self.theme = theme
        scaled_font = font
        self.font_factory = font
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._rows: dict[str, TaskRow] = {}
        self._order: list[str] = []
        self._revision = -1
        self._sig = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._header = QLabel("\u25b8 TASKS")
        self._header.setFont(scaled_font(9, bold=True))
        self._header.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._header)
        self._rows_box = QVBoxLayout()
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.setSpacing(4)
        lay.addLayout(self._rows_box)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._poll)
        self._tmr.start(self._POLL_MS)
        self.hide()

    def _poll(self) -> None:
        # Polling continues while the panel is hidden: that is how it learns it
        # has work to show. Only the registry being absent stops it.
        if _task_registry is None:
            return
        try:
            revision = _task_registry.revision()
        except Exception:
            return
        self._revision = revision
        try:
            rows = _task_registry.snapshot(include_finished_for=8, limit=32)
        except Exception:
            return
        live = [r for r in rows if r.get("state") in ("running", "queued", "paused")]
        shown = (live[: self._MAX_ROWS]
                 + [r for r in rows if r.get("state") not in ("running", "queued", "paused")][
                     : max(0, self._MAX_ROWS - len(live))])
        sig = repr([(r["id"], r["state"], r.get("percent"), r.get("detail"),
                     r.get("done_bytes"), r.get("speed_bps"), r.get("meta"),
                     r.get("cancel_requested"), r.get("pause_requested")) for r in shown])
        if sig == self._sig:
            return
        self._sig = sig
        self._render(shown)
        self.snapshot_changed.emit(rows)

    def _render(self, shown: list[dict]) -> None:
        for stale in set(self._rows) - {r["id"] for r in shown}:
            row = self._rows.pop(stale)
            self._rows_box.removeWidget(row)
            row.deleteLater()
        order: list[str] = []
        for snap in shown:
            task_id = snap["id"]
            order.append(task_id)
            row = self._rows.get(task_id)
            if row is None:
                row = TaskRow(task_id, self.theme, self.font_factory)
                self._rows[task_id] = row
            row.set_data(snap)
            if task_id not in self._order:
                self._rows_box.addWidget(row)
        if order != self._order:
            for task_id in order:                     # keep running work on top
                self._rows_box.removeWidget(self._rows[task_id])
            for task_id in order:
                self._rows_box.addWidget(self._rows[task_id])
            self._order = order
        self.setVisible(bool(shown))
