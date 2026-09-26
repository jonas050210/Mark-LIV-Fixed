"""Open-window browser: see what the assistant sees, focus or close by click.

This panel exists because of a support pattern that will not go away: a user
says "make YouTube fullscreen", the assistant answers that it cannot find such
a window, and both sides stare at a screen that clearly shows it. Showing the
window list the desktop actually reports — titles, processes, states, and the
backend name that produced the list — turns that argument into a five-second
diagnosis, and clicking a row focuses the window without any voice round trip.
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)
from ui_panels.base import C, HudPanel


def _elide(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class WindowsOverlay(HudPanel):
    """Every visible window, one clickable row each."""

    _OW = 560
    _refresh_sig = pyqtSignal(list, str)   # (rows, backend) — from the worker thread

    def __init__(self, parent=None, *, log=None):
        super().__init__(parent)
        self._log_cb = log
        self._windows: list = []
        self._last_keys: tuple = ()
        self._last_backend: str = ""
        self.setStyleSheet(f"""
            WindowsOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._refresh_sig.connect(self._apply_snapshot)
        # A gentle live refresh while the panel is open: the desktop changes,
        # and a window list that goes stale behind the user's back is exactly
        # the confusion this panel exists to clear up. The rebuild is skipped
        # when nothing changed, so an idle desktop costs nothing and nothing
        # flickers.
        self._auto_timer = QTimer(self)
        self._auto_timer.setInterval(2000)
        self._auto_timer.timeout.connect(
            lambda: self.refresh() if self.isVisible() else None
        )
        self._auto_timer.start()
        self._rebuild()

    # ── layout plumbing (same dance as the memory panel) ─────────────────────

    def _clear_layout(self):
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()
        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width() - self.width()) // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    # ── content ──────────────────────────────────────────────────────────────

    def _snapshot(self):
        """Read the desktop from a worker thread; the list is small but the
        per-window process lookups are not free, and the Qt thread should not
        pay for them."""
        try:
            from core.window_manager import (
                backend_name, list_monitors, list_windows, monitor_of,
            )

            windows = list_windows()
            monitors = list_monitors()
            rows = []
            for window in windows:
                try:
                    monitor = monitor_of(window) or next(
                        (m for m in monitors if m.left == 0 and m.top == 0), monitors[0]
                    )
                    monitor_index = monitor.index if monitor else "?"
                except Exception:
                    monitor_index = "?"
                rows.append((window, monitor_index))
            return rows, backend_name()
        except Exception as exc:
            return [], f"error: {type(exc).__name__}"

    def refresh(self):
        """Re-read the desktop on a worker thread; the signal marshals the
        result back to the Qt thread, whenever the read actually finishes."""

        def _work():
            try:
                rows, backend = self._snapshot()
                self._refresh_sig.emit(rows, backend)
            except RuntimeError:
                # The panel was closed and destroyed while the desktop was
                # being read; there is nothing left to update.
                pass

        threading.Thread(target=_work, daemon=True, name="windows-panel-refresh").start()

    def _apply_snapshot(self, rows, backend: str):
        keys = tuple(
            (int(w.handle or 0), int(w.pid or 0), str(w.title), bool(w.minimized),
             bool(w.maximized), int(w.left), int(w.top), int(w.right), int(w.bottom))
            for w, _monitor in rows
        )
        unchanged = keys == self._last_keys and getattr(self, "_last_backend", None) == backend
        self._windows = [w for w, _m in rows]
        if unchanged and self._lay.count() > 1:
            return   # nothing moved, nothing re-titled: leave the pixels alone
        self._last_keys = keys
        self._last_backend = backend
        self._rebuild(rows=rows, backend=backend)

    def _rebuild(self, rows=None, backend: str = ""):
        before = self.geometry()
        self._clear_layout()

        hdr = QLabel("🪟  OPEN WINDOWS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.Hline)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        if rows is None:
            # First build: show a loading state, then pull the real list.
            cap = QLabel("Reading the desktop…")
            cap.setFont(QFont("Courier New", 9))
            cap.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(cap)
            QTimer.singleShot(0, self.refresh)
        else:
            count = len(rows)
            cap = QLabel(
                f"{count} window(s) visible · backend '{backend}' — this is exactly "
                f"what the assistant can see. Click a row to focus it, ✕ to close it."
            )
            cap.setWordWrap(True)
            cap.setFont(QFont("Courier New", 7))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            self._lay.addWidget(cap)

            if count == 0:
                empty = QLabel(
                    "No visible windows were reported. If windows are open, the "
                    f"desktop backend '{backend}' cannot see this session."
                )
                empty.setWordWrap(True)
                empty.setFont(QFont("Courier New", 9))
                empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
                self._lay.addWidget(empty)
            else:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFixedHeight(min(420, 52 * count + 12))
                scroll.setStyleSheet(
                    f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                    f"background: transparent; }}"
                )
                inner = QWidget()
                ilay = QVBoxLayout(inner)
                ilay.setContentsMargins(6, 6, 6, 6)
                ilay.setSpacing(3)

                for window, monitor_index in rows:
                    ilay.addWidget(self._row(window, monitor_index))

                ilay.addStretch()
                scroll.setWidget(inner)
                self._lay.addWidget(scroll)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        refresh_btn = QPushButton("⟳  REFRESH")
        refresh_btn.setFixedHeight(30)
        refresh_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 10px; }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """)
        refresh_btn.clicked.connect(self.refresh)
        btns.addWidget(refresh_btn)
        btns.addStretch()

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 10px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        btns.addWidget(close)
        self._lay.addLayout(btns)

        self._settle(before)
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _row(self, window, monitor_index) -> QWidget:
        holder = QWidget()
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)

        state = "MIN" if window.minimized else "MAX" if window.maximized else ""
        state_txt = f"  ·  {state}" if state else ""
        title = _elide(window.title, 58)
        process = _elide(window.process or "?", 18)
        label = QPushButton(
            f"{title}\n{process}  ·  {window.width}x{window.height}  ·  M{monitor_index}{state_txt}"
        )
        label.setFont(QFont("Courier New", 8))
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.setToolTip(f"{window.title}\nFocus this window")
        label.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
        """)
        label.clicked.connect(lambda _=False, w=window: self._operate(w, "focus"))
        line.addWidget(label, 1)

        rm = QPushButton("✕")
        rm.setFixedSize(26, 26)
        rm.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.setToolTip("Close this window")
        rm.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
        """)
        rm.clicked.connect(lambda _=False, w=window: self._operate(w, "close"))
        line.addWidget(rm)

        holder.setLayout(line)
        return holder

    def _operate(self, window, operation: str):
        """Focus or close a window off the Qt thread, then refresh the list."""

        def _work():
            try:
                from core.window_manager import operate

                operate(window, operation)
            except Exception as exc:
                if self._log_cb:
                    try:
                        self._log_cb(
                            f"ERR: Windows panel could not {operation} "
                            f"'{_elide(window.title, 40)}' ({type(exc).__name__})."
                        )
                    except Exception:
                        pass

        threading.Thread(
            target=_work, daemon=True, name=f"windows-panel-{operation}"
        ).start()
        QTimer.singleShot(600, self.refresh)
