from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from render.progress import RenderProgress


MEDIA_FILTER = "All Supported Media (*.*)"
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;All files (*.*)"
AUDIO_FILTER = "Audio (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.wma *.aiff);;All files (*.*)"
VIDEO_FILTER = "Video (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mpeg *.mpg *.ts);;All files (*.*)"


class FileField(QWidget):
    changed = Signal(str)

    def __init__(self, label: str, file_filter: str = MEDIA_FILTER, directory: bool = False, optional: bool = False, parent=None):
        super().__init__(parent)
        self.file_filter = file_filter
        self.directory = directory
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel(label + (" (optional)" if optional else ""))
        title.setObjectName("fieldLabel")
        layout.addWidget(title)
        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Chọn thư mục..." if directory else "Chọn file...")
        self.edit.textChanged.connect(self.changed)
        browse = QPushButton("Browse")
        browse.clicked.connect(self.browse)
        row.addWidget(self.edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

    def browse(self) -> None:
        start = self.edit.text().strip()
        if start and not Path(start).exists():
            start = ""
        if self.directory:
            value = QFileDialog.getExistingDirectory(self, "Chọn thư mục", start)
        else:
            value, _ = QFileDialog.getOpenFileName(self, "Chọn file", start, self.file_filter)
        if value:
            self.edit.setText(value)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:
        self.edit.setText(value or "")


class RenderStatus(QWidget):
    cancel_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.info = QLabel("Sẵn sàng")
        self.info.setObjectName("muted")
        row = QHBoxLayout()
        self.cancel = QPushButton("Cancel")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_requested)
        self.open_output = QPushButton("Play Output")
        self.open_output.setEnabled(False)
        self.open_folder = QPushButton("Open Folder")
        self.open_folder.setEnabled(False)
        row.addWidget(self.cancel)
        row.addStretch()
        row.addWidget(self.open_folder)
        row.addWidget(self.open_output)
        layout.addWidget(self.progress)
        layout.addWidget(self.info)
        layout.addLayout(row)
        self.output_path = ""
        self.started_at = 0.0
        self.open_output.clicked.connect(self._play)
        self.open_folder.clicked.connect(self._folder)

    def begin(self) -> None:
        self.started_at = time.monotonic()
        self.progress.setValue(0)
        self.info.setText("Đang khởi tạo FFmpeg…")
        self.cancel.setEnabled(True)
        self.open_output.setEnabled(False)

    def update_progress(self, value: RenderProgress) -> None:
        self.progress.setValue(round(value.percent * 10))
        current = _clock(value.current_seconds)
        remaining_seconds = max(0, value.current_seconds * (100 / max(value.percent, .001) - 1) / value.speed) if value.percent and value.speed > 0 else 0
        remaining = _clock(remaining_seconds) if remaining_seconds else "--:--:--"
        elapsed = _clock(time.monotonic() - self.started_at) if self.started_at else "00:00:00"
        size = value.total_size / 1024 / 1024
        self.info.setText(f"{value.percent:5.1f}%  •  time {current}  •  {value.fps:.1f} fps  •  {value.speed:.2f}x  •  elapsed {elapsed}  •  còn {remaining}  •  {size:.1f} MB")

    def success(self, output: str) -> None:
        self.output_path = output
        self.progress.setValue(1000)
        self.info.setText(f"Hoàn tất: {output}")
        self.cancel.setEnabled(False)
        self.open_output.setEnabled(True)
        self.open_folder.setEnabled(True)

    def stopped(self, message: str = "Đã hủy render.") -> None:
        self.cancel.setEnabled(False)
        self.info.setText(message)

    def _play(self) -> None:
        if self.output_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_path))

    def _folder(self) -> None:
        if self.output_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.output_path).parent)))


def show_error(parent: QWidget, title: str, error: Exception | str, log_path: str = "") -> None:
    message = str(error)
    if log_path:
        message += f"\n\nLog: {log_path}"
    QMessageBox.critical(parent, title, message)


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
