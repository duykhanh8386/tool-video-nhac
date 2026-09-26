from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from models.loop_project import LoopProject
from models.settings_model import AppSettings
from render.ffmpeg import build_loop_job
from render.ffprobe import format_duration, probe_media
from render.render_worker import RenderWorker
from ui.common import AUDIO_FILTER, VIDEO_FILTER, FileField, RenderStatus, show_error


class LoopMusicPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = RenderWorker(self)
        root = QVBoxLayout(self)
        title = QLabel("Lặp Video + Nhạc")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        note = QLabel("Main music là master clock. Video cuối luôn được cắt chính xác theo thời lượng main music.")
        note.setObjectName("notice")
        root.addWidget(note)
        columns = QHBoxLayout()
        inputs = QGroupBox("DỮ LIỆU ĐẦU VÀO")
        input_layout = QVBoxLayout(inputs)
        self.video = FileField("File video", VIDEO_FILTER)
        self.main_audio = FileField("File nhạc chính", AUDIO_FILTER)
        self.background_audio = FileField("Nhạc nền phụ", AUDIO_FILTER, optional=True)
        self.output_folder = FileField("Thư mục lưu", directory=True)
        self.output_folder.setText(settings.last_output_folder)
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("final_YYYYMMDD_HHMMSS.mp4")
        for item in (self.video, self.main_audio, self.background_audio, self.output_folder):
            input_layout.addWidget(item)
        input_layout.addWidget(QLabel("Tên file đầu ra"))
        input_layout.addWidget(self.output_name)
        columns.addWidget(inputs, 1)

        options = QGroupBox("TRỘN ÂM THANH VÀ MÃ HÓA")
        form = QFormLayout(options)
        self.main_volume = _volume_slider(100)
        self.bg_volume = _volume_slider(15)
        self.loop_bg = QCheckBox("Lặp nhạc nền phụ")
        self.loop_bg.setChecked(True)
        self.crossfade = QCheckBox("Chuyển tiếp nhạc nền liền mạch")
        self.crossfade.setChecked(True)
        self.crossfade_ms = QComboBox()
        self.crossfade_ms.addItems(["0", "250", "500", "1000", "2000"])
        self.crossfade_ms.setCurrentText("1000")
        self.normalize = QCheckBox("Chuẩn hóa âm lượng cuối")
        self.seamless = QCheckBox("Lặp video liền mạch")
        self.seamless.setChecked(True)
        self.encoder = QComboBox()
        self.encoder.addItems(["Auto", "H264 NVENC", "HEVC NVENC", "libx264"])
        self.encoder.setCurrentText(settings.encoder)
        self.resolution = QComboBox()
        self.resolution.addItems(["Keep source", "1920x1080", "3840x2160"])
        self.fps = QComboBox()
        self.fps.addItems(["Keep source", "30", "60"])
        form.addRow("Âm lượng nhạc chính", self.main_volume)
        form.addRow("Âm lượng nhạc nền", self.bg_volume)
        form.addRow(self.loop_bg)
        form.addRow(self.crossfade)
        form.addRow("Chuyển tiếp (ms)", self.crossfade_ms)
        form.addRow(self.normalize)
        form.addRow(self.seamless)
        form.addRow("Bộ mã hóa", self.encoder)
        form.addRow("Độ phân giải", self.resolution)
        form.addRow("FPS", self.fps)
        columns.addWidget(options)
        root.addLayout(columns)
        button_row = QHBoxLayout()
        analyze = QPushButton("Phân tích")
        analyze.clicked.connect(self.analyze)
        start = QPushButton("Bắt đầu render")
        start.setObjectName("primary")
        start.clicked.connect(self.start_render)
        button_row.addWidget(analyze)
        button_row.addWidget(start)
        button_row.addStretch()
        root.addLayout(button_row)
        self.media_info = QLabel("Chưa phân tích media.")
        self.media_info.setWordWrap(True)
        self.media_info.setObjectName("muted")
        root.addWidget(self.media_info)
        self.status = RenderStatus()
        root.addWidget(self.status)
        root.addStretch()
        self.status.cancel_requested.connect(self.worker.cancel)
        self.worker.progress.connect(self.status.update_progress)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.canceled.connect(lambda: self.status.stopped())

    def analyze(self) -> None:
        try:
            video = probe_media(self.video.text(), self.settings.ffprobe_path)
            audio = probe_media(self.main_audio.text(), self.settings.ffprobe_path)
            if not video.has_video:
                raise ValueError("File đã chọn không có video stream.")
            if not audio.has_audio:
                raise ValueError("Main music không có audio stream.")
            text = f"Video: {video.width}×{video.height}, {video.fps:.3f} fps, {video.video_codec}, {format_duration(video.duration)}\nMain music: {audio.audio_codec}, {audio.sample_rate} Hz, {audio.channels} ch, {format_duration(audio.duration)}\nFinal duration: {format_duration(audio.duration)}"
            if self.background_audio.text():
                bg = probe_media(self.background_audio.text(), self.settings.ffprobe_path)
                if not bg.has_audio:
                    raise ValueError("Background music không có audio stream.")
                text += f"\nBackground: {bg.audio_codec}, {format_duration(bg.duration)}"
            self.media_info.setText(text)
        except Exception as exc:
            show_error(self, "Không thể phân tích media", exc)

    def collect(self) -> LoopProject:
        return LoopProject(
            video=self.video.text(), main_audio=self.main_audio.text(), background_audio=self.background_audio.text(),
            output_folder=self.output_folder.text(), output_name=self.output_name.text(),
            main_volume=self.main_volume.value() / 100, background_volume=self.bg_volume.value() / 100,
            loop_background=self.loop_bg.isChecked(), background_crossfade=self.crossfade.isChecked(),
            crossfade_ms=int(self.crossfade_ms.currentText()), normalize=self.normalize.isChecked(),
            seamless_video=self.seamless.isChecked(), encoder=self.encoder.currentText(),
            resolution=self.resolution.currentText(), fps=self.fps.currentText(),
        )

    def load(self, project: LoopProject) -> None:
        for widget, value in ((self.video, project.video), (self.main_audio, project.main_audio), (self.background_audio, project.background_audio), (self.output_folder, project.output_folder)):
            widget.setText(value)
        self.output_name.setText(project.output_name)
        self.main_volume.setValue(round(project.main_volume * 100))
        self.bg_volume.setValue(round(project.background_volume * 100))
        self.loop_bg.setChecked(project.loop_background)
        self.crossfade.setChecked(project.background_crossfade)
        self.crossfade_ms.setCurrentText(str(project.crossfade_ms))
        self.normalize.setChecked(project.normalize)
        self.seamless.setChecked(project.seamless_video)
        self.encoder.setCurrentText(project.encoder)
        self.resolution.setCurrentText(project.resolution)
        self.fps.setCurrentText(str(project.fps))

    def start_render(self) -> None:
        try:
            project = self.collect()
            job = build_loop_job(project, self.settings)
            self.settings.last_output_folder = project.output_folder
            self.settings.encoder = project.encoder
            self.settings_changed.emit()
            self.status.begin()
            self.worker.start(job)
        except Exception as exc:
            show_error(self, "Không thể bắt đầu render", exc)

    def _finished(self, output: str, _log: str) -> None:
        self.status.success(output)
        QMessageBox.information(self, "Render hoàn tất", f"Final video:\n{output}")

    def _failed(self, message: str, log_path: str) -> None:
        self.status.stopped("Render thất bại")
        show_error(self, "Render thất bại", message, log_path)


def _volume_slider(value: int) -> QSlider:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, 200)
    slider.setValue(value)
    slider.setToolTip("0%–200%")
    return slider
