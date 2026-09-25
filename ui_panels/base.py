"""Shared foundation for the HUD side panels.

These panels live outside ``ui.py`` on purpose. That file is already 5500 lines
and every new feature that lands in it makes the next one harder to place, so
new panels get their own module and ``ui.py`` only learns how to open them.

The palette is read from ``ui.C`` at call time rather than imported at module
level: ``ui`` imports this package, so importing ``ui`` back at definition time
would be a cycle. Looking the colours up lazily also means a live theme change
is picked up the next time a panel is built.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QPushButton, QWidget

# Used only when the panel is built without the host application present, which
# is the case in the widget tests.
_FALLBACK = {
    "PANEL": "#010d14",
    "PANEL2": "#010f18",
    "BORDER": "#0d3347",
    "BORDER_B": "#1a5c7a",
    "PRI": "#00d4ff",
    "ACC": "#ff6b00",
    "GREEN": "#00ff88",
    "RED": "#ff3355",
    "TEXT": "#8ffcff",
    "TEXT_DIM": "#3a8a9a",
    "TEXT_MED": "#5ab8cc",
    "WHITE": "#d8f8ff",
}


class _Palette:
    """Attribute access to the running UI's palette.

    The extracted panels were written against ``ui.C`` and read it inside
    stylesheet strings at construction time. Importing ``ui`` here would be a
    cycle, so this proxy resolves each attribute when it is asked for — which
    also means a live theme change is picked up the next time a panel is built.
    """

    def __getattr__(self, name: str) -> str:
        return colour(name)


C = _Palette()


def colour(name: str) -> str:
    """One palette entry, from the running UI when there is one."""
    try:
        import ui  # imported lazily: ui imports this package

        return str(getattr(ui.C, name))
    except Exception:
        return _FALLBACK.get(name, "#8ffcff")


def default_accent() -> str:
    """The palette's default accent colour, for the appearance panel."""
    try:
        import ui

        return str(ui.DEFAULT_UI_COLOR)
    except Exception:
        return _FALLBACK["PRI"]


class HudPanel(QWidget):
    """A floating panel positioned by hand over the HUD.

    Panels are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide: the HUD keeps painting
    around them and the last frame stays on screen as a ghost. Repainting the
    area we were covering, just before we stop covering it, is what prevents
    that.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def hideEvent(self, event):  # noqa: N802 - Qt naming
        host = self.parentWidget()
        if host is not None:
            host.update(self.geometry())
        super().hideEvent(event)

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        host = self.parentWidget()
        if host is not None:
            host.update(self.geometry())
        super().closeEvent(event)


def panel_style(widget_class: str) -> str:
    return f"""
        {widget_class} {{
            background: rgba(1, 13, 20, 250);
            border: 1px solid {colour('BORDER_B')};
            border-radius: 6px;
        }}
    """


def header(text: str) -> QLabel:
    label = QLabel(text)
    label.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
    label.setStyleSheet(f"color: {colour('PRI')}; background: transparent;")
    return label


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setFont(QFont("Courier New", 8))
    label.setStyleSheet(f"color: {colour('TEXT_DIM')}; background: transparent;")
    return label


def button(text: str, *, accent: bool = False, height: int = 28) -> QPushButton:
    tone = colour("ACC") if accent else colour("TEXT_MED")
    edge = colour("ACC") if accent else colour("BORDER")
    widget = QPushButton(text)
    widget.setFixedHeight(height)
    widget.setFont(QFont("Courier New", 8, QFont.Weight.Bold if accent else QFont.Weight.Normal))
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setStyleSheet(f"""
        QPushButton {{ background: transparent; color: {tone};
            border: 1px solid {edge}; border-radius: 3px; padding: 0 10px; }}
        QPushButton:hover {{ color: {colour('WHITE')};
            border-color: {colour('BORDER_B')}; }}
        QPushButton:disabled {{ color: {colour('TEXT_DIM')};
            border-color: {colour('BORDER')}; }}
    """)
    return widget


def status(label: QLabel, text: str, *, ok: bool = True) -> None:
    """Report an outcome on a panel's status line.

    Failures are shown in the failure colour and never reworded into something
    reassuring: a panel that says "done" when nothing happened is the one bug
    this project refuses to ship.
    """
    label.setText(text)
    label.setStyleSheet(
        f"color: {colour('GREEN') if ok else colour('RED')}; background: transparent;"
    )
