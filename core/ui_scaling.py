"""Reversible widget font/control sizing; no cumulative min-height inflation."""
from PyQt6.QtWidgets import QPushButton, QLineEdit, QComboBox, QCheckBox


def refresh_font(widget, font_factory, authored_fonts):
    base = widget.property("baseFontPoints")
    if base is None:
        base = authored_fonts.get(widget.font().toString(), max(9, widget.font().pointSizeF()))
        widget.setProperty("baseFontPoints", base)
    widget.setFont(font_factory(base, bold=widget.font().bold()))
    if isinstance(widget, (QPushButton, QLineEdit, QComboBox, QCheckBox)):
        original = widget.property("authoredHeightBounds")
        if original is None:
            original = [widget.minimumHeight(), widget.maximumHeight()]
            widget.setProperty("authoredHeightBounds", original)
        minimum, maximum = original
        needed = max(minimum, widget.fontMetrics().height() + 10)
        # Fixed-size controls must grow to fit text, but return when scale drops.
        if minimum == maximum:
            widget.setFixedHeight(needed)
        else:
            widget.setMaximumHeight(max(maximum, needed))
            widget.setMinimumHeight(needed)
    lines = widget.property("maxTextLines")
    if lines:
        widget.setMaximumHeight(widget.fontMetrics().lineSpacing() * int(lines) + 4)
