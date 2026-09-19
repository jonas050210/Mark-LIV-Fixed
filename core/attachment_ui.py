"""Compact cards for the existing FileDropZone and AttachmentManager."""
from PyQt6.QtCore import QTimer, Qt, QSize, pyqtSignal
from PyQt6.QtGui import QImageReader, QPixmap
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QProgressBar, QScrollArea, QDialog, QTextEdit, QFileDialog)
from core import tasks


class FileDropZone(QWidget):
    """Only an input affordance. Cards/manager own selection and removal state."""
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("AttachmentDropZone")
        self.setStyleSheet("#AttachmentDropZone {border:1px dashed #306070; border-radius:6px;}")
        layout = QVBoxLayout(self)
        button = QPushButton("＋ Attach files")
        button.clicked.connect(self._browse)
        layout.addWidget(button)
        hint = QLabel("or drop multiple local files here")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        self.setToolTip("Local attachments · up to 32 files, 512 MiB each. Nothing is uploaded automatically.")

    def _browse(self):
        from pathlib import Path
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files to JARVIS", str(Path.home()), "All Files (*)")
        for path in paths:
            self.file_selected.emit(path)

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            event.ignore()
            return
        for path in paths:
            self.file_selected.emit(path)
        event.acceptProposedAction()


class AttachmentCards(QScrollArea):
    summary_changed = pyqtSignal(str)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWidgetResizable(True)
        self.setMaximumHeight(150)
        self.setMinimumHeight(0)
        body = QWidget()
        self.rows = QVBoxLayout(body)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.setWidget(body)
        self.cards = {}
        self._summary = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.hide()

    def refresh(self):
        items = self.manager.snapshot()
        ready = sum(item["ready"] for item in items)
        errors = sum(item["task"]["state"] == "failed" for item in items)
        summary = (f"{ready}/{len(items)} files ready locally" + (f" · {errors} failed" if errors else "")
                   if items else "No files attached · local imports only")
        if summary != self._summary:
            self._summary = summary
            self.summary_changed.emit(summary)
        for ident in set(self.cards) - {x["id"] for x in items}:
            self.cards.pop(ident)[0].deleteLater()
        for item in items:
            ident = item["id"]
            if ident not in self.cards:
                card = QWidget()
                lay = QVBoxLayout(card)
                label = QLabel()
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                lay.addWidget(label)
                bar = QProgressBar()
                bar.setMaximumHeight(12)
                bar.setTextVisible(False)
                lay.addWidget(bar)
                actions = QHBoxLayout()
                preview = QPushButton("Preview")
                preview.clicked.connect(lambda _, i=ident: self.preview(i))
                remove = QPushButton("Cancel / remove")
                remove.clicked.connect(lambda _, i=ident: self.manager.remove(i))
                actions.addWidget(preview)
                actions.addWidget(remove)
                lay.addLayout(actions)
                self.rows.addWidget(card)
                self.cards[ident] = card, label, bar, preview
            _, label, bar, preview = self.cards[ident]
            snap = item["task"]
            state = "Ready" if item["ready"] else snap.get("detail", "Queued")
            label.setText(f"{item['name']} · {item['type'] or 'File'} · "
                          f"{tasks.format_bytes(item['size'])}\n{state}"
                          + (f" · {snap['error']}" if snap.get("error") else ""))
            progress = snap.get("percent")
            bar.setRange(0, 0 if progress is None and snap.get("state") in tasks.ACTIVE_STATES else 100)
            if progress is not None:
                bar.setValue(progress)
            preview.setEnabled(item["ready"] and bool(item["preview"] or item["type"].startswith("image/")))
        self.setVisible(bool(items))

    def preview(self, ident):
        item = next((x for x in self.manager.snapshot() if x["id"] == ident and x["ready"]), None)
        if not item:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(item["name"])
        dialog.resize(540, 380)
        layout = QVBoxLayout(dialog)
        if item["preview"]:
            view = QTextEdit()
            view.setReadOnly(True)
            view.setPlainText(item["preview"])
            layout.addWidget(view)
        else:
            view = QLabel()
            reader = QImageReader(item["path"])
            size = reader.size()
            # Reject decompression bombs; do not open externally (would steal game focus).
            if not size.isValid() or size.width() * size.height() > 40_000_000:
                view.setText("Image too large or unsupported for a safe inline preview.")
            else:
                reader.setScaledSize(size.scaled(QSize(500, 320), Qt.AspectRatioMode.KeepAspectRatio))
                image = reader.read()
                if image.isNull():
                    view.setText(f"Cannot preview this image: {reader.errorString()}")
                else:
                    view.setPixmap(QPixmap.fromImage(image))
            layout.addWidget(view)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
