"""Application launcher panel for the HUD.

The launcher index, pins, recent list and icons already existed, but only the
web dashboard could reach them; from the desktop window an application could
only be started by saying its name. This panel puts the same index behind a
search box, so a misheard name is no longer the only way in.

Launching goes through ``actions.open_app`` rather than ``core.app_index``
directly, so a click and a spoken command take exactly the same path: the same
argument validation, the same launch record, the same undo entry.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from ui_panels.base import HudPanel, button, colour, header, hint, panel_style, status

MAX_RESULTS = 40
_ICON_SIZE = 20


class LauncherOverlay(HudPanel):
    """Search, pin and start any indexed application."""

    launched = pyqtSignal(str)
    _OW = 460
    _OH = 520

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(panel_style("LauncherOverlay"))
        self.setFixedSize(self._OW, self._OH)
        self._entries: list = []
        self._rows: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)
        layout.addWidget(header("▸  LAUNCHER"))

        self._search = QLineEdit()
        self._search.setPlaceholderText("Type an application name…")
        self._search.setFixedHeight(30)
        self._search.setFont(QFont("Courier New", 9))
        self._search.setStyleSheet(f"""
            QLineEdit {{ background: {colour('PANEL2')}; color: {colour('WHITE')};
                border: 1px solid {colour('BORDER')}; border-radius: 3px;
                padding: 0 8px; }}
            QLineEdit:focus {{ border-color: {colour('BORDER_B')}; }}
        """)
        self._search.textChanged.connect(self._refresh_results)
        self._search.returnPressed.connect(self._launch_selected)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.setFont(QFont("Courier New", 9))
        self._list.setIconSize(_icon_size())
        self._list.setStyleSheet(f"""
            QListWidget {{ background: {colour('PANEL2')}; color: {colour('TEXT')};
                border: 1px solid {colour('BORDER')}; border-radius: 3px; }}
            QListWidget::item {{ padding: 5px 6px; }}
            QListWidget::item:selected {{ background: {colour('BORDER')};
                color: {colour('WHITE')}; }}
        """)
        self._list.itemDoubleClicked.connect(lambda _item: self._launch_selected())
        layout.addWidget(self._list, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setFont(QFont("Courier New", 8))
        self._status.setStyleSheet(f"color: {colour('TEXT_MED')}; background: transparent;")
        layout.addWidget(self._status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._launch_btn = button("▸  LAUNCH", accent=True)
        self._launch_btn.clicked.connect(self._launch_selected)
        row.addWidget(self._launch_btn)
        self._pin_btn = button("PIN")
        self._pin_btn.clicked.connect(self._toggle_pin)
        row.addWidget(self._pin_btn)
        refresh = button("RESCAN")
        refresh.clicked.connect(self._rescan)
        row.addWidget(refresh)
        close = button("CLOSE")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        layout.addLayout(row)

        layout.addWidget(hint(
            "Enter or double-click launches. Pinned applications stay at the top "
            "and are shared with the web dashboard."
        ))

        self._load_index()
        self._refresh_results()
        self._search.setFocus()

    # ── data ────────────────────────────────────────────────────────────────

    def _load_index(self, *, refresh: bool = False) -> None:
        try:
            from core.app_index import load_index

            self._entries = load_index(refresh=refresh)
        except Exception as exc:
            self._entries = []
            status(self._status, f"The application index is unavailable ({type(exc).__name__}).", ok=False)

    def _pinned_keys(self) -> set[str]:
        try:
            from core.app_index import normalize_key, read_usage

            return {normalize_key(name) for name in read_usage().get("pinned", [])}
        except Exception:
            return set()

    def _current_rows(self) -> list:
        """Search results, or the pinned and recent lists when nothing is typed."""
        query = self._search.text().strip()
        try:
            if query:
                from core.app_index import resolve

                return resolve(query, limit=MAX_RESULTS, entries=self._entries)
            from core.app_index import quick_list

            quick = quick_list(self._entries)
            rows = list(quick.get("pinned", [])) + list(quick.get("recent", []))
            # Pinned and recent come first, but the rest of the index follows:
            # a launcher that shows only what you already use is of no help the
            # first time you open it.
            seen = {entry.key for entry in rows}
            rows.extend(entry for entry in self._entries if entry.key not in seen)
            return rows[:MAX_RESULTS]
        except Exception as exc:
            status(self._status, f"The search failed ({type(exc).__name__}).", ok=False)
            return []

    def _refresh_results(self) -> None:
        self._rows = self._current_rows()
        pinned = self._pinned_keys()
        self._list.clear()
        for entry in self._rows:
            marker = "★ " if entry.key in pinned else "  "
            item = QListWidgetItem(f"{marker}{entry.name}")
            item.setToolTip(f"{entry.target}\n{entry.source or entry.kind}")
            icon = _entry_icon(entry)
            if icon is not None:
                item.setIcon(icon)
            self._list.addItem(item)
        if self._rows:
            self._list.setCurrentRow(0)
        self._sync_buttons()
        if not self._rows:
            text = (
                "No application matches that name."
                if self._search.text().strip()
                else "The application index is empty. Press RESCAN to build it."
            )
            self._status.setText(text)
            self._status.setStyleSheet(
                f"color: {colour('TEXT_MED')}; background: transparent;"
            )

    def _selected(self):
        index = self._list.currentRow()
        if 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def _sync_buttons(self) -> None:
        entry = self._selected()
        self._launch_btn.setEnabled(entry is not None)
        self._pin_btn.setEnabled(entry is not None)
        if entry is not None:
            self._pin_btn.setText("UNPIN" if entry.key in self._pinned_keys() else "PIN")

    # ── actions ─────────────────────────────────────────────────────────────

    def _launch_selected(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        try:
            from actions.open_app import open_app

            message = open_app({"app_name": entry.name})
        except Exception as exc:
            status(self._status, f"Launching failed ({type(exc).__name__}).", ok=False)
            return
        # open_app reports its own failures in words; the panel repeats them
        # verbatim instead of deciding for itself that the launch worked.
        succeeded = message.lower().startswith(("opened", "launched", "brought", "focused"))
        status(self._status, message, ok=succeeded)
        if succeeded:
            self.launched.emit(entry.name)
            self._refresh_results()

    def _toggle_pin(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        pinned = entry.key in self._pinned_keys()
        try:
            from core.app_index import set_pinned

            set_pinned(entry.name, not pinned)
        except ValueError as exc:
            status(self._status, str(exc), ok=False)
            return
        except Exception as exc:
            status(self._status, f"The pin could not be saved ({type(exc).__name__}).", ok=False)
            return
        status(self._status, f"{'Unpinned' if pinned else 'Pinned'} {entry.name}.")
        self._refresh_results()

    def _rescan(self) -> None:
        self._status.setText("Scanning for installed applications…")
        self._load_index(refresh=True)
        self._refresh_results()
        if self._entries:
            status(self._status, f"Indexed {len(self._entries)} applications.")

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._rows:
            step = 1 if event.key() == Qt.Key.Key_Down else -1
            self._list.setCurrentRow(
                max(0, min(len(self._rows) - 1, self._list.currentRow() + step))
            )
            self._sync_buttons()
            return
        super().keyPressEvent(event)


def _icon_size():
    from PyQt6.QtCore import QSize

    return QSize(_ICON_SIZE, _ICON_SIZE)


def _entry_icon(entry) -> QIcon | None:
    """The cached application icon, when one could be extracted.

    Icon extraction touches the filesystem and, on Windows, the shell; a panel
    that cannot draw an icon must still list the application, so every failure
    here is simply no icon.
    """
    try:
        from core.app_icons import icon_png

        data = icon_png(entry)
    except Exception:
        return None
    if not data:
        return None
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        return None
    return QIcon(pixmap)
