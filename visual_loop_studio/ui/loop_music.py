from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QRadioButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
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
        note = QLabel(
            "Hỗ trợ 2 chế độ thời lượng: Bắt chuẩn theo độ dài nhạc chính, hoặc Tự do chọn thời lượng video theo ý muốn."
        )
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

        # Chế độ thời lượng video
        dur_group = QGroupBox("THỜI LƯỢNG VIDEO ĐẦU RA")
        dur_layout = QVBoxLayout(dur_group)
        dur_mode_row = QHBoxLayout()
        self.dur_mode_audio = QRadioButton("🎵 Bắt chuẩn theo nhạc chính")
        self.dur_mode_custom = QRadioButton("⏱ Tự do chọn thời lượng video")
        self.dur_mode_audio.setChecked(True)
        dur_mode_row.addWidget(self.dur_mode_audio)
        dur_mode_row.addWidget(self.dur_mode_custom)
        dur_mode_row.addStretch()
        dur_layout.addLayout(dur_mode_row)

        self.custom_dur_container = QWidget()
        custom_layout = QVBoxLayout(self.custom_dur_container)
        custom_layout.setContentsMargins(0, 4, 0, 0)

        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("Đặt thời lượng:"))
        self.custom_hours = QSpinBox()
        self.custom_hours.setRange(0, 99)
        self.custom_hours.setSuffix(" giờ")
        self.custom_minutes = QSpinBox()
        self.custom_minutes.setRange(0, 59)
        self.custom_minutes.setSuffix(" phút")
        self.custom_minutes.setValue(5)
        self.custom_seconds = QSpinBox()
        self.custom_seconds.setRange(0, 59)
        self.custom_seconds.setSuffix(" giây")
        spin_row.addWidget(self.custom_hours)
        spin_row.addWidget(self.custom_minutes)
        spin_row.addWidget(self.custom_seconds)
        spin_row.addStretch()
        custom_layout.addLayout(spin_row)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Chọn nhanh:"))
        for label, secs in (
            ("30s", 30),
            ("1 phút", 60),
            ("3 phút", 180),
            ("5 phút", 300),
            ("10 phút", 600),
            ("30 phút", 1800),
            ("1 giờ", 3600),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, s=secs: self._apply_preset_duration(s))
            preset_row.addWidget(btn)
        preset_row.addStretch()
        custom_layout.addLayout(preset_row)

        dur_layout.addWidget(self.custom_dur_container)
        self.dur_hint = QLabel("💡 Video sẽ được cắt và lặp chính xác theo thời lượng bài nhạc chính.")
        self.dur_hint.setObjectName("muted")
        self.dur_hint.setWordWrap(True)
        dur_layout.addWidget(self.dur_hint)

        self.dur_mode_audio.toggled.connect(self._on_dur_mode_changed)
        self.dur_mode_custom.toggled.connect(self._on_dur_mode_changed)
        self.custom_dur_container.setEnabled(False)

        input_layout.addWidget(dur_group)
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

    def _on_dur_mode_changed(self) -> None:
        is_custom = self.dur_mode_custom.isChecked()
        self.custom_dur_container.setEnabled(is_custom)
        if is_custom:
            self.dur_hint.setText(
                "💡 Chế độ tự do: Nếu thời lượng dài hơn bài nhạc, nhạc chính sẽ tự động lặp lại để chạy hết video."
            )
        else:
            self.dur_hint.setText(
                "💡 Chế độ bắt theo nhạc: Video cuối sẽ được lặp và cắt đúng bằng thời lượng file nhạc chính."
            )

    def _get_custom_duration_seconds(self) -> float:
        return float(self.custom_hours.value() * 3600 + self.custom_minutes.value() * 60 + self.custom_seconds.value())

    def _set_custom_duration_seconds(self, seconds: float) -> None:
        total = max(0, int(round(seconds)))
        hours = total // 3600
        mins = (total % 3600) // 60
        secs = total % 60
        self.custom_hours.setValue(hours)
        self.custom_minutes.setValue(mins)
        self.custom_seconds.setValue(secs)

    def _apply_preset_duration(self, seconds: int) -> None:
        self.dur_mode_custom.setChecked(True)
        self._set_custom_duration_seconds(float(seconds))

    def analyze(self) -> None:
        try:
            video = probe_media(self.video.text(), self.settings.ffprobe_path)
            audio = probe_media(self.main_audio.text(), self.settings.ffprobe_path)
            if not video.has_video:
                raise ValueError("File đã chọn không có video stream.")
            if not audio.has_audio:
                raise ValueError("Main music không có audio stream.")
            
            if self.dur_mode_custom.isChecked():
                target_sec = self._get_custom_duration_seconds()
                if target_sec > 0:
                    dur_text = f"{format_duration(target_sec)} (Tự do chọn)"
                    if target_sec > audio.duration:
                        dur_text += f" — Nhạc sẽ tự lặp lại ({audio.duration:.1f}s/lượt)"
                else:
                    dur_text = f"{format_duration(audio.duration)} (Chưa đặt thời lượng tự do)"
            else:
                dur_text = f"{format_duration(audio.duration)} (Bắt chuẩn theo nhạc chính)"

            text = f"Video: {video.width}×{video.height}, {video.fps:.3f} fps, {video.video_codec}, {format_duration(video.duration)}\nMain music: {audio.audio_codec}, {audio.sample_rate} Hz, {audio.channels} ch, {format_duration(audio.duration)}\nFinal duration: {dur_text}"
            if self.background_audio.text():
                bg = probe_media(self.background_audio.text(), self.settings.ffprobe_path)
                if not bg.has_audio:
                    raise ValueError("Background music không có audio stream.")
                text += f"\nBackground: {bg.audio_codec}, {format_duration(bg.duration)}"
            self.media_info.setText(text)
        except Exception as exc:
            show_error(self, "Không thể phân tích media", exc)

    def collect(self) -> LoopProject:
        duration_mode = "custom" if self.dur_mode_custom.isChecked() else "audio"
        custom_duration = self._get_custom_duration_seconds()
        return LoopProject(
            video=self.video.text(), main_audio=self.main_audio.text(), background_audio=self.background_audio.text(),
            output_folder=self.output_folder.text(), output_name=self.output_name.text(),
            main_volume=self.main_volume.value() / 100, background_volume=self.bg_volume.value() / 100,
            loop_background=self.loop_bg.isChecked(), background_crossfade=self.crossfade.isChecked(),
            crossfade_ms=int(self.crossfade_ms.currentText()), normalize=self.normalize.isChecked(),
            seamless_video=self.seamless.isChecked(), encoder=self.encoder.currentText(),
            resolution=self.resolution.currentText(), fps=self.fps.currentText(),
            duration_mode=duration_mode, custom_duration=custom_duration,
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
        if project.duration_mode == "custom":
            self.dur_mode_custom.setChecked(True)
        else:
            self.dur_mode_audio.setChecked(True)
        if project.custom_duration > 0:
            self._set_custom_duration_seconds(project.custom_duration)

    def start_render(self) -> None:
        try:
            project = self.collect()
            if project.duration_mode == "custom" and project.custom_duration <= 0:
                raise ValueError("Vui lòng đặt thời lượng video lớn hơn 0 giây khi chọn chế độ tự do.")
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
