from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from models.settings_model import AppSettings
from render.ffmpeg import build_audio_job
from render.render_worker import RenderWorker
from ui.common import AUDIO_FILTER, FileField, RenderStatus, show_error


class AudioMixerPage(QWidget):
    settings_changed = Signal()

    TRACKS = (("Âm thanh chính", 100), ("Nhạc nền", 15), ("Âm thanh thiên nhiên", 15), ("Tiếng nước", 15), ("Tiếng mưa", 12), ("Tiếng ồn trắng", 8))

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = RenderWorker(self)
        root = QVBoxLayout(self)
        title = QLabel("Trộn âm thanh")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        note = QLabel("Duration follows main audio. Các track phụ được loop bằng FFmpeg streaming.")
        note.setObjectName("notice")
        root.addWidget(note)
        grid = QGridLayout()
        self.fields: list[FileField] = []
        self.volumes: list[QSlider] = []
        for row, (name, default) in enumerate(self.TRACKS):
            field = FileField(name, AUDIO_FILTER, optional=row > 0)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 200)
            slider.setValue(default)
            slider.setToolTip("0%–200%")
            grid.addWidget(field, row, 0)
            grid.addWidget(QLabel("Âm lượng"), row, 1)
            grid.addWidget(slider, row, 2)
            self.fields.append(field)
            self.volumes.append(slider)
        root.addLayout(grid)
        output_row = QHBoxLayout()
        self.output_folder = FileField("Thư mục lưu", directory=True)
        self.output_folder.setText(settings.last_output_folder)
        output_row.addWidget(self.output_folder, 2)
        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("Tên file đầu ra"))
        self.output_name = QLineEdit("audio_mix.mp3")
        name_col.addWidget(self.output_name)
        output_row.addLayout(name_col, 1)
        self.format = QComboBox()
        self.format.addItems(["MP3", "WAV", "FLAC", "AAC"])
        self.format.currentTextChanged.connect(self._format_changed)
        output_row.addWidget(self.format)
        root.addLayout(output_row)
        action_row = QHBoxLayout()
        self.normalize = QCheckBox("Chuẩn hóa âm lượng cuối")
        start = QPushButton("Bắt đầu trộn")
        start.setObjectName("primary")
        start.clicked.connect(self.start_mix)
        action_row.addWidget(self.normalize)
        action_row.addStretch()
        action_row.addWidget(start)
        root.addLayout(action_row)
        self.status = RenderStatus()
        root.addWidget(self.status)
        root.addStretch()
        self.status.cancel_requested.connect(self.worker.cancel)
        self.worker.progress.connect(self.status.update_progress)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.canceled.connect(lambda: self.status.stopped())

    def _format_changed(self, value: str) -> None:
        current = self.output_name.text()
        stem = current.rsplit(".", 1)[0] if "." in current else current
        self.output_name.setText(f"{stem}.{value.lower()}")

    def start_mix(self) -> None:
        try:
            paths, volumes = [], []
            for field, slider in zip(self.fields, self.volumes):
                if field.text():
                    paths.append(field.text())
                    volumes.append(slider.value() / 100)
            if not self.fields[0].text():
                raise ValueError("Chưa chọn Main audio.")
            job = build_audio_job(paths, volumes, self.output_folder.text(), self.output_name.text(), self.normalize.isChecked(), self.settings)
            self.settings.last_output_folder = self.output_folder.text()
            self.settings_changed.emit()
            self.status.begin()
            self.worker.start(job)
        except Exception as exc:
            show_error(self, "Không thể bắt đầu mix", exc)

    def _finished(self, output: str, _log: str) -> None:
        self.status.success(output)
        QMessageBox.information(self, "Mix hoàn tất", f"Audio output:\n{output}")

    def _failed(self, message: str, log_path: str) -> None:
        self.status.stopped("Mix thất bại")
        show_error(self, "Mix thất bại", message, log_path)
