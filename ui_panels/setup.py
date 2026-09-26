"""First-run panel containing only the required Gemini API-key input."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ui_panels.base import C


class SetupOverlay(QWidget):
    done = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 26, 30, 26)
        layout.setSpacing(10)

        label = QLabel("GEMINI API KEY")
        label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        label.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        label.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        layout.addWidget(label)

        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(34)
        self._normal_style = f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """
        self._key_input.setStyleSheet(self._normal_style)
        self._key_input.returnPressed.connect(self._submit)
        layout.addWidget(self._key_input)

        submit = QPushButton("CONTINUE")
        submit.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        submit.setFixedHeight(36)
        submit.setCursor(Qt.CursorShape.PointingHandCursor)
        submit.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        submit.clicked.connect(self._submit)
        layout.addWidget(submit)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._normal_style + f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            self._key_input.setFocus()
            return
        self.done.emit(key)
