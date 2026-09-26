from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from models.settings_model import AppSettings
from models.visual_project import ElementLayout, VisualProject, default_element_layouts
from render.ffmpeg import build_visual_job
from render.render_worker import RenderWorker
from ui.ai_video import AiVideoWorker
from ui.color_filter_panel import ColorFilterPanel
from ui.common import AUDIO_FILTER, IMAGE_FILTER, MEDIA_FILTER, FileField, RenderStatus, show_error
from ui.effect_panel import EffectPanel
from ui.preview import CompositionPreview
from utils.media import is_still_image
from utils.paths import unique_output
from visual.layout import ELEMENT_LABELS, PRESETS, analyze_background, compose_layout, reset_layout


class VisualCreatorPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = RenderWorker(self)
        self.ai_worker = AiVideoWorker(self)
        self.elements = default_element_layouts()
        self.analysis = None
        self.layout_variant = 0
        self._updating_element_controls = False
        root = QVBoxLayout(self)
        heading = QLabel("Tạo Visual — 60 giây")
        heading.setObjectName("pageTitle")
        root.addWidget(heading)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(450)
        panel = QWidget()
        self.form = QVBoxLayout(panel)
        scroll.setWidget(panel)
        splitter.addWidget(scroll)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.preview = CompositionPreview()
        self.preview.ffmpeg_path = settings.ffmpeg_path
        self.preview.setToolTip("Kéo để di chuyển • kéo góc phải dưới để đổi kích thước • Ctrl + lăn chuột để xoay")
        self.preview.set_layouts(self.elements)
        right_layout.addWidget(self.preview, 1)
        preview_controls = QHBoxLayout()
        for text, callback in (("Phát", self.preview.play), ("Tạm dừng", self.preview.pause), ("Dừng", self.preview.stop), ("Phát lại", self.preview.restart)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            preview_controls.addWidget(button)
        preview_controls.addStretch()
        auto = QPushButton("TỰ SẮP XẾP")
        auto.setObjectName("primary")
        auto.clicked.connect(lambda: self.auto_compose(False))
        another = QPushButton("THỬ BỐ CỤC KHÁC")
        another.clicked.connect(lambda: self.auto_compose(True))
        reset = QPushButton("ĐẶT LẠI")
        reset.clicked.connect(self.reset_composition)
        preview_controls.addWidget(reset)
        preview_controls.addWidget(another)
        preview_controls.addWidget(auto)
        right_layout.addLayout(preview_controls)
        self.status = RenderStatus()
        self.status.cancel_requested.connect(self.worker.cancel)
        right_layout.addWidget(self.status)
        splitter.addWidget(right)
        splitter.setSizes([470, 900])

        self._build_inputs()
        self.worker.progress.connect(self.status.update_progress)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.canceled.connect(lambda: self.status.stopped())
        self.ai_worker.progress.connect(self._ai_progress)
        self.ai_worker.finished.connect(self._ai_finished)
        self.ai_worker.failed.connect(self._ai_failed)
        self.ai_worker.canceled.connect(self._ai_canceled)
        self._connect_preview()
        self._refresh_element_combo("title")

    def _build_inputs(self) -> None:
        layout_group = QGroupBox("BỐ CỤC THÔNG MINH")
        layout_box = QVBoxLayout(layout_group)
        top = QHBoxLayout()
        self.layout_mode = QComboBox()
        self.layout_mode.addItems(["AUTO", "TEMPLATE", "MANUAL"])
        self.layout_template = QComboBox()
        self.layout_template.addItems(PRESETS)
        self.layout_template.setCurrentText("Minimal")
        self.layout_template.activated.connect(lambda _index: self.layout_mode.setCurrentText("TEMPLATE"))
        top.addWidget(QLabel("Chế độ"))
        top.addWidget(self.layout_mode)
        top.addWidget(QLabel("Mẫu bố cục"))
        top.addWidget(self.layout_template, 1)
        layout_box.addLayout(top)
        self.analysis_info = QLabel("Ảnh nền chưa được phân tích.")
        self.analysis_info.setObjectName("muted")
        self.analysis_info.setWordWrap(True)
        layout_box.addWidget(self.analysis_info)

        selected_form = QFormLayout()
        self.element_choice = QComboBox()
        self.element_choice.currentIndexChanged.connect(self._element_selected_from_controls)
        self.element_lock = QCheckBox("Khóa vị trí")
        self.element_visible = QCheckBox("Hiển thị")
        self.element_visible.setChecked(True)
        state_row = QHBoxLayout()
        state_row.addWidget(self.element_lock)
        state_row.addWidget(self.element_visible)
        self.element_anchor = QComboBox()
        self.element_anchor.addItems(["top_left", "top_center", "top_right", "center", "bottom_left", "bottom_center", "bottom_right"])
        selected_form.addRow("Thành phần đang chọn", self.element_choice)
        selected_form.addRow(state_row)
        selected_form.addRow("Điểm neo", self.element_anchor)
        layout_box.addLayout(selected_form)

        coordinates = QGridLayout()
        self.element_x = _normalized_spin()
        self.element_y = _normalized_spin()
        self.element_width = _normalized_spin(.03)
        self.element_height = _normalized_spin(.03)
        self.element_rotation = QDoubleSpinBox()
        self.element_rotation.setRange(-360, 360)
        self.element_rotation.setDecimals(1)
        self.element_opacity = QSpinBox()
        self.element_opacity.setRange(0, 100)
        self.element_z = QSpinBox()
        self.element_z.setRange(0, 999)
        controls = (("X", self.element_x), ("Y", self.element_y), ("Rộng", self.element_width), ("Cao", self.element_height), ("Góc xoay", self.element_rotation), ("Độ mờ", self.element_opacity), ("Thứ tự lớp", self.element_z))
        for index, (label, widget) in enumerate(controls):
            row, column = divmod(index, 2)
            coordinates.addWidget(QLabel(label), row, column * 2)
            coordinates.addWidget(widget, row, column * 2 + 1)
        layout_box.addLayout(coordinates)

        element_actions = QHBoxLayout()
        duplicate = QPushButton("Nhân bản")
        delete_copy = QPushButton("Xóa bản sao")
        center_h = QPushButton("Giữa ngang")
        center_v = QPushButton("Giữa dọc")
        duplicate.clicked.connect(self.duplicate_element)
        delete_copy.clicked.connect(self.delete_element_copy)
        center_h.clicked.connect(lambda: self.preview.align_selected("horizontal"))
        center_v.clicked.connect(lambda: self.preview.align_selected("vertical"))
        for button in (duplicate, delete_copy, center_h, center_v):
            element_actions.addWidget(button)
        layout_box.addLayout(element_actions)

        snap_row = QHBoxLayout()
        self.guides = QCheckBox("Đường căn")
        self.guides.setChecked(True)
        self.snap_center = QCheckBox("Tâm khung")
        self.snap_center.setChecked(True)
        self.snap_margin = QCheckBox("Lề an toàn")
        self.snap_margin.setChecked(True)
        self.snap_elements = QCheckBox("Thành phần khác")
        self.snap_elements.setChecked(True)
        self.snap_grid = QCheckBox("Lưới")
        for checkbox in (self.guides, self.snap_center, self.snap_margin, self.snap_elements, self.snap_grid):
            snap_row.addWidget(checkbox)
        layout_box.addWidget(QLabel("Tùy chọn căn dính"))
        layout_box.addLayout(snap_row)
        self.form.addWidget(layout_group)

        text_group = QGroupBox("1. VĂN BẢN")
        text_form = QFormLayout(text_group)
        self.title = QLineEdit()
        self.subtitle = QLineEdit()
        self.artist = QLineEdit()
        self.custom_text = QLineEdit()
        self.playlist = QPlainTextEdit()
        self.playlist.setPlaceholderText("01. Tên bài hát\n02. Tên bài hát")
        self.playlist.setMaximumHeight(95)
        for label, widget in (("Tiêu đề", self.title), ("Tiêu đề phụ", self.subtitle), ("Nghệ sĩ", self.artist), ("Nội dung thêm", self.custom_text), ("Danh sách bài hát", self.playlist)):
            text_form.addRow(label, widget)
        self.font_file = FileField("Font tùy chọn", "Fonts (*.ttf *.otf *.ttc);;Tất cả file (*.*)", optional=True)
        self.font_size = QSpinBox()
        self.font_size.setRange(0, 300)
        self.font_size.setSpecialValueText("Tự động")
        self.text_color = QLineEdit("#FFFFFF")
        self.text_opacity = QSlider(Qt.Orientation.Horizontal)
        self.text_opacity.setRange(0, 100)
        self.text_opacity.setValue(100)
        self.stroke_width = QSpinBox()
        self.stroke_width.setRange(0, 12)
        self.stroke_width.setValue(2)
        self.text_shadow = QCheckBox("Bật")
        self.text_shadow.setChecked(True)
        text_form.addRow(self.font_file)
        text_form.addRow("Cỡ chữ cơ sở", self.font_size)
        text_form.addRow("Màu chữ", self.text_color)
        text_form.addRow("Độ mờ", self.text_opacity)
        text_form.addRow("Độ dày viền", self.stroke_width)
        text_form.addRow("Bóng chữ", self.text_shadow)
        self.form.addWidget(text_group)

        background_group = QGroupBox("2. ẢNH NỀN VÀ CHUYỂN ĐỘNG AI")
        bg_layout = QVBoxLayout(background_group)
        self.background = FileField("Ảnh hoặc video nền", MEDIA_FILTER)
        self.animation = QComboBox()
        _add_options(self.animation, [
            ("Camera cố định — khuyên dùng", "STATIC"),
            ("Chuyển động nhẹ kiểu cũ", "VEO STYLE GENTLE"),
            ("Phóng chậm", "Slow zoom"), ("Lia chậm", "Slow pan"),
            ("Chiều sâu parallax", "Parallax"), ("Sương trôi", "Fog drift"),
            ("Tinh vân trôi", "Nebula drift"), ("Hạt trôi", "Particle drift"),
            ("Ánh sáng thở", "Glow breathing"),
        ])
        bg_layout.addWidget(self.background)
        bg_layout.addWidget(QLabel("Chuyển động toàn khung"))
        bg_layout.addWidget(self.animation)
        camera_note = QLabel("Camera cố định giữ nguyên khung. Muốn tay/chân, khói hoặc ánh sáng chuyển động theo nội dung, hãy tạo video AI từ ảnh và prompt bên dưới.")
        camera_note.setWordWrap(True)
        camera_note.setObjectName("notice")
        bg_layout.addWidget(camera_note)
        self.ai_prompt = QPlainTextEdit()
        self.ai_prompt.setMaximumHeight(105)
        self.ai_prompt.setPlaceholderText("Ví dụ: Người phụ nữ thở nhẹ và chớp mắt; hai bàn tay chuyển động rất nhẹ. Khói hương bay tự nhiên, lửa nến rung nhẹ. Không thay đổi khuôn mặt hoặc bố cục.")
        bg_layout.addWidget(QLabel("Prompt chuyển động AI"))
        bg_layout.addWidget(self.ai_prompt)
        ai_config = QGridLayout()
        self.ai_model = QComboBox()
        _add_options(self.ai_model, [
            ("Veo 3.1 Fast — nhanh", "veo-3.1-fast-generate-preview"),
            ("Veo 3.1 — chất lượng", "veo-3.1-generate-preview"),
            ("Veo 3.1 Lite — tiết kiệm", "veo-3.1-lite-generate-preview"),
        ])
        self.ai_duration = QComboBox()
        _add_options(self.ai_duration, [("4 giây", 4), ("6 giây", 6), ("8 giây — vòng lặp tốt nhất", 8)])
        _set_combo_value(self.ai_duration, 8)
        self.ai_resolution = QComboBox()
        self.ai_resolution.addItems(["720p", "1080p"])
        self.ai_aspect_ratio = QComboBox()
        self.ai_aspect_ratio.addItems(["16:9", "9:16"])
        ai_config.addWidget(QLabel("Mô hình"), 0, 0)
        ai_config.addWidget(self.ai_model, 0, 1)
        ai_config.addWidget(QLabel("Thời lượng"), 1, 0)
        ai_config.addWidget(self.ai_duration, 1, 1)
        ai_config.addWidget(QLabel("Độ phân giải AI"), 2, 0)
        ai_config.addWidget(self.ai_resolution, 2, 1)
        ai_config.addWidget(QLabel("Tỷ lệ khung"), 3, 0)
        ai_config.addWidget(self.ai_aspect_ratio, 3, 1)
        bg_layout.addLayout(ai_config)
        self.ai_resolution.currentTextChanged.connect(self._ai_resolution_changed)
        ai_actions = QHBoxLayout()
        self.ai_generate = QPushButton("Tạo video chuyển động bằng Veo")
        self.ai_generate.setObjectName("primary")
        self.ai_generate.clicked.connect(self.start_ai_video)
        self.ai_cancel = QPushButton("Hủy tạo AI")
        self.ai_cancel.setEnabled(False)
        self.ai_cancel.clicked.connect(self.ai_worker.cancel)
        ai_actions.addWidget(self.ai_generate, 1)
        ai_actions.addWidget(self.ai_cancel)
        bg_layout.addLayout(ai_actions)
        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 100)
        self.ai_progress.setValue(0)
        self.ai_status = QLabel("Cần Gemini API key trong Cài đặt để gọi Veo.")
        self.ai_status.setWordWrap(True)
        self.ai_status.setObjectName("muted")
        bg_layout.addWidget(self.ai_progress)
        bg_layout.addWidget(self.ai_status)
        self.form.addWidget(background_group)

        asset_group = QGroupBox("3–4. LOGO, BIỂU TƯỢNG VÀ ẢNH BÌA")
        asset_layout = QVBoxLayout(asset_group)
        self.logo = FileField("Logo", IMAGE_FILTER, optional=True)
        self.artwork = FileField("Biểu tượng / Ảnh bìa", IMAGE_FILTER, optional=True)
        self.platform_icons = FileField("Biểu tượng nền tảng", IMAGE_FILTER, optional=True)
        self.artwork_motion = QComboBox()
        _add_options(self.artwork_motion, [
            ("Đứng yên", "STATIC"), ("Trôi nhẹ", "FLOAT"), ("Nhịp phồng", "PULSE"),
            ("Thở nhẹ", "BREATH"), ("Xoay", "ROTATE"), ("Xoay rất chậm", "VERY_SLOW_ROTATE"),
            ("Nhịp theo beat", "BEAT_PULSE"),
        ])
        _set_combo_value(self.artwork_motion, "FLOAT")
        asset_layout.addWidget(self.logo)
        asset_layout.addWidget(self.artwork)
        asset_layout.addWidget(self.platform_icons)
        asset_layout.addWidget(QLabel("Chuyển động ảnh bìa"))
        asset_layout.addWidget(self.artwork_motion)
        self.form.addWidget(asset_group)

        wave_group = QGroupBox("5. FILE SÓNG / HIỆU ỨNG")
        wave_layout = QVBoxLayout(wave_group)
        self.waveform_media = FileField("File sóng chạy độc lập (PNG/GIF/MOV/MP4)", MEDIA_FILTER, optional=True)
        self.audio = FileField("Nhạc kèm theo visual — không điều khiển sóng", AUDIO_FILTER, optional=True)
        self.waveform = QComboBox()
        _add_options(self.waveform, [("Chạy file sóng lặp độc lập", "FILE"), ("Tắt lớp sóng", "NONE")])
        self.waveform_remove_white = QCheckBox("Xóa nền trắng của file sóng")
        self.waveform_remove_white.setChecked(True)
        wave_layout.addWidget(self.waveform_media)
        wave_layout.addWidget(self.audio)
        wave_layout.addWidget(self.waveform)
        wave_layout.addWidget(self.waveform_remove_white)
        wave_note = QLabel("File sóng chỉ chạy lặp theo thời gian của chính file, không tăng giảm theo âm lượng nhạc.")
        wave_note.setWordWrap(True)
        wave_note.setObjectName("muted")
        wave_layout.addWidget(wave_note)
        self.form.addWidget(wave_group)

        effects_group = QGroupBox("HIỆU ỨNG TOÀN KHUNG")
        effects_layout = QVBoxLayout(effects_group)
        self.effects = EffectPanel()
        effects_layout.addWidget(self.effects)
        self.form.addWidget(effects_group)

        color_group = QGroupBox("BỘ LỌC MÀU VÀ LUT")
        color_layout = QVBoxLayout(color_group)
        self.color = ColorFilterPanel()
        color_layout.addWidget(self.color)
        self.form.addWidget(color_group)

        output_group = QGroupBox("XUẤT VIDEO")
        output_layout = QVBoxLayout(output_group)
        self.output_folder = FileField("Thư mục lưu", directory=True)
        self.output_folder.setText(self.settings.last_output_folder)
        self.output_name = QLineEdit()
        self.output_name.setPlaceholderText("visual_YYYYMMDD_HHMMSS.mp4")
        config_row = QHBoxLayout()
        self.resolution = QComboBox()
        self.resolution.addItems(["1920x1080", "3840x2160"])
        self.fps = QSpinBox()
        self.fps.setRange(24, 60)
        self.fps.setValue(self.settings.fps)
        self.encoder = QComboBox()
        self.encoder.addItems(["Auto", "H264 NVENC", "HEVC NVENC", "libx264"])
        self.encoder.setCurrentText(self.settings.encoder)
        config_row.addWidget(QLabel("Độ phân giải"))
        config_row.addWidget(self.resolution)
        config_row.addWidget(QLabel("FPS"))
        config_row.addWidget(self.fps)
        config_row.addWidget(self.encoder)
        render = QPushButton("Render video 1 phút")
        render.setObjectName("primary")
        render.clicked.connect(self.start_render)
        output_layout.addWidget(self.output_folder)
        output_layout.addWidget(QLabel("Tên file đầu ra tùy chọn"))
        output_layout.addWidget(self.output_name)
        output_layout.addLayout(config_row)
        output_layout.addWidget(render)
        self.form.addWidget(output_group)
        self.form.addStretch()

    def _connect_preview(self) -> None:
        self.background.changed.connect(self._background_changed)
        self.artwork.changed.connect(lambda value: self.preview.set_source("artwork", value))
        self.logo.changed.connect(lambda value: self.preview.set_source("logo", value))
        self.platform_icons.changed.connect(lambda value: self.preview.set_source("platform_icons", value))
        self.waveform_media.changed.connect(lambda value: self.preview.set_source("waveform_media", value))
        self.title.textChanged.connect(lambda value: self._set_preview_value("title", value))
        self.subtitle.textChanged.connect(lambda value: self._set_preview_value("subtitle", value))
        self.artist.textChanged.connect(lambda value: self._set_preview_value("artist", value))
        self.custom_text.textChanged.connect(lambda value: self._set_preview_value("custom_text", value))
        self.playlist.textChanged.connect(lambda: self._set_preview_value("playlist", self.playlist.toPlainText()))
        self.text_color.textChanged.connect(lambda value: self._set_preview_value("text_color", value))
        self.waveform.currentIndexChanged.connect(lambda _index: self._set_preview_value("waveform", _combo_value(self.waveform)))
        self.animation.currentIndexChanged.connect(lambda _index: self._set_preview_value("background_motion", _combo_value(self.animation)))
        self.color.changed.connect(self._preview_effects)
        self.effects.changed.connect(self._preview_effects)
        self.preview.element_selected.connect(self._select_element_from_preview)
        self.preview.layout_changed.connect(self._layout_changed_from_preview)
        for widget in (self.element_lock, self.element_visible, self.element_anchor, self.element_x, self.element_y, self.element_width, self.element_height, self.element_rotation, self.element_opacity, self.element_z):
            signal = widget.currentTextChanged if isinstance(widget, QComboBox) else widget.toggled if isinstance(widget, QCheckBox) else widget.valueChanged
            signal.connect(self._write_element_controls)
        self.guides.toggled.connect(lambda value: self._set_preview_option("show_guides", value))
        self.snap_center.toggled.connect(lambda value: self._set_preview_option("snap_center", value))
        self.snap_margin.toggled.connect(lambda value: self._set_preview_option("snap_safe_margin", value))
        self.snap_elements.toggled.connect(lambda value: self._set_preview_option("snap_elements", value))
        self.snap_grid.toggled.connect(lambda value: self._set_preview_option("snap_grid", value))

    def _background_changed(self, value: str) -> None:
        self.preview.set_source("background", value)
        self.analysis = None
        self.preview.subject_bbox = None
        self.analysis_info.setText("Ảnh nền đã thay đổi; bấm TỰ SẮP XẾP để phân tích lại.")

    def _set_preview_value(self, name: str, value) -> None:
        setattr(self.preview, name, value)
        self.preview.update()

    def _set_preview_option(self, name: str, value: bool) -> None:
        setattr(self.preview, name, value)
        self.preview.update()

    def _preview_effects(self) -> None:
        self.preview.effects = [item.name for item in self.effects.values() if item.enabled]
        self.preview.color_filter = self.color.preset.currentText()
        self.preview.manual_color = self.color.values()
        self.preview.update()

    def auto_compose(self, another: bool = False) -> None:
        if not self.background.text():
            show_error(self, "Tự sắp xếp", "Hãy chọn ảnh hoặc video nền trước.")
            return
        if not self.title.text():
            self.title.setText("TIÊU ĐỀ")
        if another:
            self.layout_variant += 1
        else:
            self.layout_variant = 0
        try:
            if self.analysis is None:
                self.analysis_info.setText("Đang phân tích vùng quan trọng, độ chi tiết và độ sáng…")
                self.analysis = analyze_background(self.background.text(), self.settings.ffmpeg_path)
            template = self.layout_template.currentText() if self.layout_mode.currentText() == "TEMPLATE" else "Minimal"
            active = {
                name for name, present in {
                    "title": bool(self.title.text()), "subtitle": bool(self.subtitle.text()),
                    "artist": bool(self.artist.text()), "custom_text": bool(self.custom_text.text()),
                    "playlist": bool(self.playlist.toPlainText()), "logo": bool(self.logo.text()),
                    "artwork": bool(self.artwork.text()), "platform_icons": bool(self.platform_icons.text()),
                    "waveform": _combo_value(self.waveform) != "NONE" and bool(self.waveform_media.text()),
                }.items() if present
            }
            result = compose_layout(self.analysis, self.elements, template, self.layout_variant, active)
            self.elements = result.elements
            self.preview.set_layouts(self.elements)
            self.preview.subject_bbox = result.analysis.subject_bbox
            self.text_color.setText(result.text_color)
            self.analysis_info.setText(
                f"Subject: {result.analysis.subject_side} • method: {result.analysis.method} • "
                f"faces: {result.analysis.face_count} • people: {result.analysis.human_count} • "
                f"brightness: {result.analysis.mean_brightness:.2f} • variant: {self.layout_variant + 1}"
            )
            self._refresh_element_combo(self.preview.selected_element)
            if self.layout_mode.currentText() == "MANUAL":
                self.layout_mode.setCurrentText("AUTO")
        except Exception as exc:
            show_error(self, "Không thể tự sắp xếp", exc)

    def reset_composition(self) -> None:
        self.elements = reset_layout()
        self.layout_variant = 0
        self.preview.subject_bbox = None
        self.preview.set_layouts(self.elements)
        self._refresh_element_combo("title")
        self.analysis_info.setText("Đã đặt lại bố cục và bỏ toàn bộ khóa vị trí.")

    def duplicate_element(self) -> None:
        new_id = self.preview.duplicate_selected()
        if new_id:
            self._refresh_element_combo(new_id)

    def delete_element_copy(self) -> None:
        if not self.preview.delete_selected_copy():
            QMessageBox.information(self, "Xóa bản sao", "Không thể xóa thành phần gốc; bạn có thể bỏ chọn Hiển thị.")
        self._refresh_element_combo(self.preview.selected_element)

    def _refresh_element_combo(self, selected: str = "") -> None:
        self._updating_element_controls = True
        self.element_choice.clear()
        for element_id, layout in sorted(self.elements.items(), key=lambda item: item[1].z_order):
            base = element_id.split("_copy_", 1)[0]
            suffix = element_id[len(base):].replace("_copy_", " copy ")
            self.element_choice.addItem(ELEMENT_LABELS.get(base, base.title()) + suffix, element_id)
        index = self.element_choice.findData(selected)
        self.element_choice.setCurrentIndex(max(0, index))
        self._updating_element_controls = False
        self._load_element_controls()

    def _element_selected_from_controls(self, _index: int) -> None:
        if self._updating_element_controls:
            return
        element_id = self.element_choice.currentData()
        if element_id:
            self.preview.select_element(element_id)
            self._load_element_controls()

    def _select_element_from_preview(self, element_id: str) -> None:
        index = self.element_choice.findData(element_id)
        if index >= 0 and index != self.element_choice.currentIndex():
            self._updating_element_controls = True
            self.element_choice.setCurrentIndex(index)
            self._updating_element_controls = False
        self._load_element_controls()

    def _load_element_controls(self) -> None:
        element_id = self.element_choice.currentData()
        layout = self.elements.get(element_id)
        if not layout:
            return
        self._updating_element_controls = True
        self.element_lock.setChecked(layout.locked)
        self.element_visible.setChecked(layout.visible)
        self.element_anchor.setCurrentText(layout.anchor)
        self.element_x.setValue(layout.x)
        self.element_y.setValue(layout.y)
        self.element_width.setValue(layout.width)
        self.element_height.setValue(layout.height)
        self.element_rotation.setValue(layout.rotation)
        self.element_opacity.setValue(round(layout.opacity * 100))
        self.element_z.setValue(layout.z_order)
        self._updating_element_controls = False

    def _write_element_controls(self, *_args) -> None:
        if self._updating_element_controls:
            return
        element_id = self.element_choice.currentData()
        layout = self.elements.get(element_id)
        if not layout:
            return
        layout.x = self.element_x.value()
        layout.y = self.element_y.value()
        layout.width = self.element_width.value()
        layout.height = self.element_height.value()
        layout.anchor = self.element_anchor.currentText()
        layout.rotation = self.element_rotation.value()
        layout.opacity = self.element_opacity.value() / 100
        layout.z_order = self.element_z.value()
        layout.locked = self.element_lock.isChecked()
        layout.visible = self.element_visible.isChecked()
        layout.normalized()
        self.layout_mode.setCurrentText("MANUAL")
        self.preview.update()

    def _layout_changed_from_preview(self) -> None:
        self.layout_mode.setCurrentText("MANUAL")
        self._load_element_controls()

    def collect(self) -> VisualProject:
        return VisualProject(
            title=self.title.text(), subtitle=self.subtitle.text(), artist=self.artist.text(), custom_text=self.custom_text.text(), playlist=self.playlist.toPlainText(),
            font_file=self.font_file.text(), font_size=self.font_size.value(), text_color=self.text_color.text(),
            text_opacity=self.text_opacity.value(), stroke_width=self.stroke_width.value(), text_shadow=self.text_shadow.isChecked(),
            background=self.background.text(), logo=self.logo.text(), artwork=self.artwork.text(), platform_icons=self.platform_icons.text(), audio=self.audio.text(),
            waveform_media=self.waveform_media.text(), waveform_remove_white=self.waveform_remove_white.isChecked(),
            output_folder=self.output_folder.text(), output_name=self.output_name.text(), resolution=self.resolution.currentText(),
            fps=self.fps.value(), encoder=self.encoder.currentText(), animation=_combo_value(self.animation),
            artwork_motion=_combo_value(self.artwork_motion), waveform=_combo_value(self.waveform),
            ai_prompt=self.ai_prompt.toPlainText(), ai_model=_combo_value(self.ai_model),
            ai_duration=int(_combo_value(self.ai_duration)), ai_resolution=self.ai_resolution.currentText(),
            ai_aspect_ratio=self.ai_aspect_ratio.currentText(),
            layout_mode=self.layout_mode.currentText(), layout_template=self.layout_template.currentText(), layout_variant=self.layout_variant,
            elements={name: ElementLayout(**asdict(layout)) for name, layout in self.elements.items()},
            color_filter=self.color.preset.currentText(), manual_color=self.color.values(), lut=self.color.lut.text(), effects=self.effects.values(),
        )

    def load(self, project: VisualProject) -> None:
        for widget, value in ((self.title, project.title), (self.subtitle, project.subtitle), (self.artist, project.artist), (self.custom_text, project.custom_text)):
            widget.setText(value)
        self.playlist.setPlainText(project.playlist)
        for widget, value in ((self.font_file, project.font_file), (self.background, project.background), (self.logo, project.logo), (self.artwork, project.artwork), (self.platform_icons, project.platform_icons), (self.waveform_media, project.waveform_media), (self.audio, project.audio), (self.output_folder, project.output_folder), (self.color.lut, project.lut)):
            widget.setText(value)
        self.font_size.setValue(project.font_size)
        self.text_color.setText(project.text_color)
        self.text_opacity.setValue(project.text_opacity)
        self.stroke_width.setValue(project.stroke_width)
        self.text_shadow.setChecked(project.text_shadow)
        self.output_name.setText(project.output_name)
        self.resolution.setCurrentText(project.resolution)
        self.fps.setValue(project.fps)
        self.encoder.setCurrentText(project.encoder)
        _set_combo_value(self.animation, project.animation)
        _set_combo_value(self.artwork_motion, project.artwork_motion)
        _set_combo_value(self.waveform, project.waveform if project.waveform in {"FILE", "NONE"} else "FILE")
        self.waveform_remove_white.setChecked(project.waveform_remove_white)
        self.ai_prompt.setPlainText(project.ai_prompt)
        _set_combo_value(self.ai_model, project.ai_model)
        _set_combo_value(self.ai_duration, project.ai_duration)
        self.ai_resolution.setCurrentText(project.ai_resolution)
        self.ai_aspect_ratio.setCurrentText(project.ai_aspect_ratio)
        self.layout_mode.setCurrentText(project.layout_mode)
        self.layout_template.setCurrentText(project.layout_template)
        self.layout_variant = project.layout_variant
        self.elements = {name: ElementLayout(**asdict(layout)) for name, layout in project.elements.items()}
        self.preview.set_layouts(self.elements)
        self._refresh_element_combo("title")
        self.color.preset.setCurrentText(project.color_filter)
        self.color.set_values(project.manual_color)
        self.effects.set_values(project.effects)

    def _ai_resolution_changed(self, value: str) -> None:
        if value in {"1080p", "4k"}:
            _set_combo_value(self.ai_duration, 8)
            self.ai_duration.setEnabled(False)
        else:
            self.ai_duration.setEnabled(True)

    def start_ai_video(self) -> None:
        try:
            source = self.background.text()
            if not source or not is_still_image(source):
                raise ValueError("Hãy chọn một file ảnh nền PNG/JPG/WebP trước khi tạo video AI.")
            prompt = self.ai_prompt.toPlainText().strip()
            if not prompt:
                raise ValueError("Hãy nhập prompt mô tả tay/chân, người hoặc hiệu ứng cần chuyển động.")
            api_key = self.settings.gemini_api_key.strip() or os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                raise ValueError("Chưa có Gemini API key. Mở menu Cài đặt và nhập key cho Veo.")
            folder = self.output_folder.text() or str(Path(source).resolve().parent)
            output = unique_output(folder, f"ai_motion_{datetime.now():%Y%m%d_%H%M%S}.mp4", "ai_motion", ".mp4")
            self.ai_generate.setEnabled(False)
            self.ai_cancel.setEnabled(True)
            self.ai_progress.setValue(1)
            self.ai_worker.start(
                api_key=api_key,
                image_path=source,
                prompt=prompt,
                output_path=output,
                model=str(_combo_value(self.ai_model)),
                aspect_ratio=self.ai_aspect_ratio.currentText(),
                duration=int(_combo_value(self.ai_duration)),
                resolution=self.ai_resolution.currentText(),
            )
        except Exception as exc:
            show_error(self, "Không thể tạo video AI", exc)

    def _ai_progress(self, value: int, message: str) -> None:
        self.ai_progress.setValue(max(0, min(100, value)))
        self.ai_status.setText(message)

    def _ai_finished(self, output: str) -> None:
        self.ai_generate.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(100)
        self.ai_status.setText(f"Đã tạo và chọn làm video nền: {output}")
        self.background.setText(output)
        _set_combo_value(self.animation, "STATIC")
        self.fps.setValue(24)
        QMessageBox.information(
            self,
            "Tạo video AI hoàn tất",
            "Video Veo đã được chọn làm nền. Camera toàn khung đang ở chế độ cố định và FPS đã đặt về 24 để khớp video AI.",
        )

    def _ai_failed(self, message: str) -> None:
        self.ai_generate.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Tạo video AI thất bại.")
        show_error(self, "Tạo video AI thất bại", message)

    def _ai_canceled(self) -> None:
        self.ai_generate.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Đã hủy tạo video AI.")

    def start_render(self) -> None:
        try:
            project = self.collect()
            job = build_visual_job(project, self.settings)
            self.settings.last_output_folder = project.output_folder
            self.settings.encoder = project.encoder
            self.settings_changed.emit()
            self.status.begin()
            self.worker.start(job)
        except Exception as exc:
            show_error(self, "Không thể bắt đầu render", exc)

    def _finished(self, output: str, _log: str) -> None:
        self.status.success(output)
        QMessageBox.information(self, "Render hoàn tất", f"Visual 60 giây đã được tạo:\n{output}")

    def _failed(self, message: str, log_path: str) -> None:
        self.status.stopped("Render thất bại")
        show_error(self, "Render thất bại", message, log_path)


def _normalized_spin(minimum: float = 0.0) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(minimum, 1.0)
    spin.setDecimals(3)
    spin.setSingleStep(.01)
    return spin


def _add_options(combo: QComboBox, options: list[tuple[str, object]]) -> None:
    for label, value in options:
        combo.addItem(label, value)


def _combo_value(combo: QComboBox):
    value = combo.currentData()
    return combo.currentText() if value is None else value


def _set_combo_value(combo: QComboBox, value) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)
