"""Window-layout panel for the HUD.

Saving and restoring a named arrangement of windows is a thing you do *while
looking at the arrangement*, which makes it a poor fit for a spoken command and
a good fit for a panel. Everything here calls ``actions.layout_manager``, so the
panel and the voice command share one implementation and one store.

The panel deliberately shows the layout_manager's own sentences rather than
inventing its own: that action reports partial results ("applied to 2 of 4
windows"), and a panel that flattened those into "done" would be lying about
the two windows it never touched.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QVBoxLayout,
)

from ui_panels.base import HudPanel, button, colour, header, hint, panel_style, status


class LayoutOverlay(HudPanel):
    """Save the current window arrangement, restore it, or delete it."""

    _OW = 460
    _OH = 460

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(panel_style("LayoutOverlay"))
        self.setFixedSize(self._OW, self._OH)
        self._names: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)
        layout.addWidget(header("▤  WINDOW LAYOUTS"))

        self._list = QListWidget()
        self._list.setFont(QFont("Courier New", 9))
        self._list.setStyleSheet(f"""
            QListWidget {{ background: {colour('PANEL2')}; color: {colour('TEXT')};
                border: 1px solid {colour('BORDER')}; border-radius: 3px; }}
            QListWidget::item {{ padding: 5px 6px; }}
            QListWidget::item:selected {{ background: {colour('BORDER')};
                color: {colour('WHITE')}; }}
        """)
        self._list.itemDoubleClicked.connect(lambda _item: self._apply())
        self._list.currentRowChanged.connect(lambda _row: self._sync_buttons())
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._apply_btn = button("▸  RESTORE", accent=True)
        self._apply_btn.clicked.connect(self._apply)
        row.addWidget(self._apply_btn)
        self._delete_btn = button("DELETE")
        self._delete_btn.clicked.connect(self._delete)
        row.addWidget(self._delete_btn)
        close = button("CLOSE")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        layout.addLayout(row)

        save_row = QHBoxLayout()
        save_row.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("Name for the current arrangement…")
        self._name.setFixedHeight(28)
        self._name.setFont(QFont("Courier New", 9))
        self._name.setStyleSheet(f"""
            QLineEdit {{ background: {colour('PANEL2')}; color: {colour('WHITE')};
                border: 1px solid {colour('BORDER')}; border-radius: 3px;
                padding: 0 8px; }}
            QLineEdit:focus {{ border-color: {colour('BORDER_B')}; }}
        """)
        self._name.returnPressed.connect(self._save)
        save_row.addWidget(self._name, 1)
        save_btn = button("SAVE")
        save_btn.clicked.connect(self._save)
        save_row.addWidget(save_btn)
        layout.addLayout(save_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setFont(QFont("Courier New", 8))
        self._status.setStyleSheet(f"color: {colour('TEXT_MED')}; background: transparent;")
        layout.addWidget(self._status)

        layout.addWidget(hint(
            "A layout stores which application a window belonged to and where it "
            "sat, not the window itself. Applications that are closed are "
            "reported, never launched."
        ))

        self.refresh()

    # ── data ────────────────────────────────────────────────────────────────

    def _call(self, params: dict) -> str:
        try:
            from actions.layout_manager import layout_manager

            return layout_manager(params)
        except Exception as exc:
            return f"The layout request failed ({type(exc).__name__})."

    def refresh(self) -> None:
        self._list.clear()
        self._names = []
        try:
            from actions.layout_manager import read_layouts

            rows = read_layouts()
        except Exception as exc:
            status(self._status, f"The layout store is unavailable ({type(exc).__name__}).", ok=False)
            return
        for name, entry in sorted(rows.items()):
            count = len(entry.get("windows", []))
            saved = str(entry.get("saved", ""))[:10]
            self._names.append(name)
            self._list.addItem(f"{name}  —  {count} window{'s' if count != 1 else ''}  ({saved})")
        if self._names:
            self._list.setCurrentRow(0)
        else:
            self._status.setText("No layouts saved yet. Arrange your windows, then save one.")
            self._status.setStyleSheet(
                f"color: {colour('TEXT_MED')}; background: transparent;"
            )
        self._sync_buttons()

    def _selected(self) -> str:
        index = self._list.currentRow()
        return self._names[index] if 0 <= index < len(self._names) else ""

    def _sync_buttons(self) -> None:
        has_selection = bool(self._selected())
        self._apply_btn.setEnabled(has_selection)
        self._delete_btn.setEnabled(has_selection)

    # ── actions ─────────────────────────────────────────────────────────────

    def _save(self) -> None:
        name = self._name.text().strip()
        if not name:
            status(self._status, "Give the layout a name first.", ok=False)
            return
        message = self._call({"action": "save", "name": name})
        status(self._status, message, ok=message.startswith("Saved"))
        if message.startswith("Saved"):
            self._name.clear()
            self.refresh()

    def _apply(self) -> None:
        name = self._selected()
        if not name:
            return
        message = self._call({"action": "apply", "name": name})
        status(self._status, message, ok=message.startswith("Applied"))

    def _delete(self) -> None:
        name = self._selected()
        if not name:
            return
        message = self._call({"action": "delete", "name": name})
        status(self._status, message, ok=message.startswith("Deleted"))
        self.refresh()

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
