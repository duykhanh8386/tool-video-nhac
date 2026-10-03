from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QFileDialog, QFontComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QColorDialog, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from ai.comfyui import (
    LOCAL_MODEL_HUNYUAN, LOCAL_MODEL_LTX, WAN_VARIANT_DMD, WAN_VARIANT_QUALITY,
    local_model_label, normalize_wan_variant,
)
from ai.google_vids_web import GOOGLE_VIDS_PROFILE_DIR, google_vids_profile_ready
from ai.local_runtime import (
    LocalRuntimeManager, default_runtime_root, detect_runtime_backend,
    is_runtime_installed, required_free_bytes, server_ready, validate_local_model_backend,
)
from models.settings_model import AppSettings
from models.visual_project import (
    ElementLayout, TextStyle, VisualProject, default_element_layouts, default_text_styles,
    normalize_text_color,
)
from render.ffmpeg import build_visual_job
from render.render_worker import RenderWorker
from ui.ai_video import AiVideoWorker
from ui.local_ai_video import LocalAiVideoWorker
from ui.local_ai_setup import LocalAiSetupWorker
from ui.color_filter_panel import ColorFilterPanel
from ui.common import AUDIO_FILTER, IMAGE_FILTER, MEDIA_FILTER, FileField, RenderStatus, show_error
from ui.effect_panel import EffectPanel
from ui.google_vids_video import GoogleVidsWorker
from ui.preview import CompositionPreview
from utils.media import background_files, is_still_image
from utils.paths import unique_output
from utils.secret_store import resolve_secret
from visual.layout import ELEMENT_LABELS, PRESETS, analyze_background, compose_layout, reset_layout


TEXT_ANIMATION_OPTIONS = [
    ("Tĩnh", "none"),
    ("Sóng từng chữ", "wave"),
    ("Nhảy luân phiên", "bounce"),
    ("Trôi mềm", "float"),
    ("Rung glitch", "jitter"),
    ("Gõ từng chữ", "typewriter"),
    ("Neon nhấp nháy", "neon"),
]


class VisualCreatorPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = RenderWorker(self)
        self.ai_worker = AiVideoWorker(self)
        self.google_vids_worker = GoogleVidsWorker(self)
        self.local_runtime = LocalRuntimeManager()
        self.local_ai_worker = LocalAiVideoWorker(self.local_runtime, self)
        self.local_setup_worker = LocalAiSetupWorker(self)
        self.elements = default_element_layouts()
        self.text_styles = default_text_styles()
        self.analysis = None
        self.layout_variant = 0
        self._updating_element_controls = False
        self._batch_queue: list[str] = []
        self._batch_total = 0
        self._batch_completed: list[str] = []
        self._batch_failures: list[str] = []
        self._batch_current = ""
        self._batch_mode = False
        self._batch_from_ai = False
        self._batch_template: VisualProject | None = None
        self._local_batch_queue: list[str] = []
        self._local_batch_total = 0
        self._local_batch_completed: list[str] = []
        self._local_batch_failures: list[str] = []
        self._local_batch_current = ""
        self._local_batch_clip = ""
        self._local_batch_mode = False
        self._local_batch_template: VisualProject | None = None
        self._ai_batch_engine = "LOCAL"
        self._active_ai_engine = "LOCAL"
        self._ai_batch_api_key = ""
        self._ai_source_images: list[str] = []
        self._ai_generated_backgrounds: list[str] = []
        self._last_ai_source_mode = ""
        self._silent_local_check = False
        root = QVBoxLayout(self)
        heading = QLabel("Tạo Visual — 60 giây")
        heading.setObjectName("pageTitle")
        root.addWidget(heading)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumWidth(450)
        panel = QWidget()
        self.form = QVBoxLayout(panel)
        self.scroll.setWidget(panel)
        splitter.addWidget(self.scroll)

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
        self.status.cancel_requested.connect(self.cancel_render)
        right_layout.addWidget(self.status)
        splitter.addWidget(right)
        splitter.setSizes([470, 900])

        self._build_inputs()
        self.worker.progress.connect(self.status.update_progress)
        self.worker.finished.connect(self._finished)
        self.worker.failed.connect(self._failed)
        self.worker.canceled.connect(self._canceled)
        self.ai_worker.progress.connect(self._ai_progress)
        self.ai_worker.finished.connect(self._ai_finished)
        self.ai_worker.failed.connect(self._ai_failed)
        self.ai_worker.canceled.connect(self._ai_canceled)
        self.google_vids_worker.progress.connect(self._ai_progress)
        self.google_vids_worker.finished.connect(self._ai_finished)
        self.google_vids_worker.failed.connect(self._ai_failed)
        self.google_vids_worker.canceled.connect(self._ai_canceled)
        self.google_vids_worker.login_finished.connect(self._vids_login_finished)
        self.google_vids_worker.login_failed.connect(self._vids_login_failed)
        self.local_ai_worker.progress.connect(self._local_ai_progress)
        self.local_ai_worker.finished.connect(self._local_ai_finished)
        self.local_ai_worker.failed.connect(self._local_ai_failed)
        self.local_ai_worker.canceled.connect(self._local_ai_canceled)
        self.local_ai_worker.checked.connect(self._local_check_finished)
        self.local_setup_worker.progress.connect(self._local_setup_progress)
        self.local_setup_worker.finished.connect(self._local_setup_finished)
        self.local_setup_worker.failed.connect(self._local_setup_failed)
        self.local_setup_worker.canceled.connect(self._local_setup_canceled)
        self._connect_preview()
        self._refresh_element_combo("title")
        QTimer.singleShot(1200, self._auto_start_local_ai)

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
        self.element_blend = QComboBox()
        _add_options(self.element_blend, [
            ("Normal — bình thường", "normal"),
            ("Lighten — bỏ vùng tối", "lighten"),
            ("Screen — sáng mềm", "screen"),
            ("Linear Dodge (Add) — cộng sáng", "addition"),
        ])
        selected_form.addRow("Thành phần đang chọn", self.element_choice)
        selected_form.addRow(state_row)
        selected_form.addRow("Điểm neo", self.element_anchor)
        selected_form.addRow("Hòa trộn ảnh", self.element_blend)
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
        self.text_animation_combos: dict[str, QComboBox] = {}
        text_inputs = (
            ("title", "Tiêu đề", self.title),
            ("subtitle", "Tiêu đề phụ", self.subtitle),
            ("artist", "Nghệ sĩ", self.artist),
            ("custom_text", "Nội dung thêm", self.custom_text),
            ("playlist", "Danh sách bài hát", self.playlist),
        )
        for kind, label, widget in text_inputs:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(widget, 3)
            animation_combo = QComboBox()
            animation_combo.setMinimumWidth(145)
            animation_combo.setToolTip("Hiệu ứng chuyển động riêng cho từng ký tự của ô này")
            _add_options(animation_combo, TEXT_ANIMATION_OPTIONS)
            animation_combo.currentIndexChanged.connect(
                lambda _index, kind=kind, combo=animation_combo: self._set_text_animation(
                    kind, str(_combo_value(combo))
                )
            )
            self.text_animation_combos[kind] = animation_combo
            row_layout.addWidget(animation_combo, 2)
            text_form.addRow(label, row)
        animation_note = QLabel("Mỗi ô có animation độc lập; preview và video xuất ra dùng cùng hiệu ứng.")
        animation_note.setWordWrap(True)
        animation_note.setObjectName("muted")
        text_form.addRow(animation_note)
        self.font_family = QFontComboBox()
        self.font_family.setToolTip("Danh sách font đã cài trên Windows, hiển thị tương tự Microsoft Word.")
        self.selected_font_size = QSpinBox()
        self.selected_font_size.setRange(8, 300)
        self.selected_font_size.setSuffix(" px @1080p")
        self.font_bold = QCheckBox("Đậm")
        self.font_italic = QCheckBox("Nghiêng")
        font_style_row = QHBoxLayout()
        font_style_row.addWidget(self.font_bold)
        font_style_row.addWidget(self.font_italic)
        font_style_row.addStretch()
        self.font_file = FileField("File font riêng ghi đè danh sách font", "Fonts (*.ttf *.otf *.ttc);;Tất cả file (*.*)", optional=True)
        self.text_color = QLineEdit("#FFFFFF")
        self.text_color.setMaxLength(7)
        self.text_color.setToolTip("Mã màu HEX gồm 6 ký tự, ví dụ #067894")
        self.text_color_mode = QComboBox()
        _add_options(self.text_color_mode, [("M\u00e0u \u0111\u01a1n", "solid"), ("Gradient tuy\u1ebfn t\u00ednh", "linear_gradient")])
        self.text_color_pick = QPushButton("Ch\u1ecdn")
        self.text_gradient_end = QLineEdit("#67E8F9")
        self.text_gradient_end.setMaxLength(7)
        self.text_gradient_end.setToolTip("Mã màu HEX gồm 6 ký tự, ví dụ #67E8F9")
        self.text_gradient_end_pick = QPushButton("Ch\u1ecdn")
        self.text_gradient_direction = QComboBox()
        _add_options(self.text_gradient_direction, [
            ("Ngang: tr\u00e1i \u2192 ph\u1ea3i", "left_to_right"), ("Ngang: ph\u1ea3i \u2192 tr\u00e1i", "right_to_left"),
            ("D\u1ecdc: tr\u00ean \u2192 d\u01b0\u1edbi", "top_to_bottom"), ("D\u1ecdc: d\u01b0\u1edbi \u2192 tr\u00ean", "bottom_to_top"),
            ("Ch\u00e9o: tr\u00e1i tr\u00ean \u2192 ph\u1ea3i d\u01b0\u1edbi", "diagonal_down"),
            ("Ch\u00e9o: tr\u00e1i d\u01b0\u1edbi \u2192 ph\u1ea3i tr\u00ean", "diagonal_up"),
        ])
        self.text_opacity = QSlider(Qt.Orientation.Horizontal)
        self.text_opacity.setRange(0, 100)
        self.text_opacity.setValue(100)
        self.stroke_width = QSpinBox()
        self.stroke_width.setRange(0, 12)
        self.stroke_width.setValue(2)
        self.text_shadow = QCheckBox("Bật")
        self.text_shadow.setChecked(True)
        text_form.addRow("Font của text đang chọn", self.font_family)
        text_form.addRow("Cỡ chữ text đang chọn", self.selected_font_size)
        text_form.addRow("Kiểu chữ", font_style_row)
        text_form.addRow(self.font_file)
        text_form.addRow("Màu chữ", self.text_color)
        text_form.addRow("Độ mờ", self.text_opacity)
        text_form.addRow("Độ dày viền", self.stroke_width)
        text_form.addRow("Bóng chữ", self.text_shadow)
        text_form.addRow("Ki\u1ec3u m\u00e0u text \u0111ang ch\u1ecdn", self.text_color_mode)
        text_form.addRow("B\u1ea3ng m\u00e0u ch\u00ednh", self.text_color_pick)
        text_form.addRow("M\u00e0u cu\u1ed1i gradient", self.text_gradient_end)
        text_form.addRow("B\u1ea3ng m\u00e0u cu\u1ed1i", self.text_gradient_end_pick)
        text_form.addRow("H\u01b0\u1edbng gradient", self.text_gradient_direction)
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

        source_group = QGroupBox("NGUỒN ẢNH ĐỂ TẠO CHUYỂN ĐỘNG AI")
        source_layout = QVBoxLayout(source_group)
        self.ai_source_mode = QComboBox()
        _add_options(self.ai_source_mode, [
            ("Một ảnh đang chọn ở trên", "SINGLE"),
            ("Chọn nhiều ảnh riêng lẻ", "MULTI"),
            ("Toàn bộ ảnh trong một thư mục", "FOLDER"),
        ])
        source_layout.addWidget(QLabel("Cách chọn ảnh nguồn"))
        source_layout.addWidget(self.ai_source_mode)
        self.ai_multi_panel = QWidget()
        multi_layout = QHBoxLayout(self.ai_multi_panel)
        multi_layout.setContentsMargins(0, 0, 0, 0)
        self.ai_choose_images = QPushButton("Chọn nhiều ảnh…")
        self.ai_choose_images.clicked.connect(self._choose_ai_source_images)
        self.ai_multi_summary = QLabel("Chưa chọn ảnh")
        self.ai_multi_summary.setWordWrap(True)
        self.ai_multi_summary.setObjectName("muted")
        multi_layout.addWidget(self.ai_choose_images)
        multi_layout.addWidget(self.ai_multi_summary, 1)
        source_layout.addWidget(self.ai_multi_panel)
        self.ai_source_folder = FileField("Thư mục ảnh nguồn AI", directory=True, optional=True)
        self.ai_source_recursive = QCheckBox("Lấy cả ảnh trong thư mục con")
        self.ai_source_summary = QLabel("")
        self.ai_source_summary.setWordWrap(True)
        self.ai_source_summary.setObjectName("notice")
        source_layout.addWidget(self.ai_source_folder)
        source_layout.addWidget(self.ai_source_recursive)
        source_layout.addWidget(self.ai_source_summary)
        bg_layout.addWidget(source_group)
        self.ai_source_mode.currentIndexChanged.connect(self._ai_source_mode_changed)
        self.ai_source_folder.changed.connect(self._ai_source_folder_changed)
        self.ai_source_recursive.toggled.connect(self._ai_source_recursive_changed)

        self.ai_prompt = QPlainTextEdit()
        self.ai_prompt.setMaximumHeight(105)
        self.ai_prompt.setPlaceholderText("Ví dụ: Người phụ nữ thở nhẹ và chớp mắt; hai bàn tay chuyển động rất nhẹ. Khói hương bay tự nhiên, lửa nến rung nhẹ. Không thay đổi khuôn mặt hoặc bố cục.")
        bg_layout.addWidget(QLabel("Prompt chuyển động AI"))
        bg_layout.addWidget(self.ai_prompt)
        self.ai_engine = QComboBox()
        _add_options(self.ai_engine, [
            ("AI Video Local — Wan / LTX / Hunyuan, không token", "LOCAL"),
            ("Veo 3.1 — Cloud, dùng API credit", "VEO"),
            ("Google Vids Web — dùng quota Google AI Ultra (Beta)", "GOOGLE_VIDS"),
        ])
        bg_layout.addWidget(QLabel("Bộ máy tạo chuyển động"))
        bg_layout.addWidget(self.ai_engine)

        self.veo_panel = QWidget()
        ai_config = QGridLayout(self.veo_panel)
        ai_config.setContentsMargins(0, 0, 0, 0)
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
        bg_layout.addWidget(self.veo_panel)
        self.ai_resolution.currentTextChanged.connect(self._ai_resolution_changed)

        self.vids_panel = QWidget()
        vids_layout = QVBoxLayout(self.vids_panel)
        vids_layout.setContentsMargins(0, 0, 0, 0)
        vids_actions = QHBoxLayout()
        self.vids_login = QPushButton("Đăng nhập Google Vids (chỉ lần đầu)")
        self.vids_login.clicked.connect(self._start_vids_login)
        self.vids_check = QPushButton("Kiểm tra phiên đăng nhập")
        self.vids_check.clicked.connect(self._check_vids_login)
        vids_actions.addWidget(self.vids_login, 1)
        vids_actions.addWidget(self.vids_check)
        vids_layout.addLayout(vids_actions)
        self.vids_show_browser = QCheckBox("Hiện Chrome khi chạy Vids (khuyên bật trong giai đoạn Beta)")
        self.vids_show_browser.setChecked(True)
        vids_layout.addWidget(self.vids_show_browser)
        vids_note = QLabel(
            "Tool dùng một profile Chrome riêng và chỉ lưu cookie/phiên do Chrome tạo, không lưu mật khẩu. "
            "Google Vids Web hiện không có API tạo clip nên chế độ Beta điều khiển giao diện web; "
            "nếu Google đổi giao diện, tool sẽ dừng item và lưu ảnh lỗi thay vì treo hàng đợi. "
            "Đầu ra Vids hiện là MP4 720p, 24 FPS, khoảng 10 giây. Prompt tiếng Anh cho kết quả tốt nhất."
        )
        vids_note.setWordWrap(True)
        vids_note.setObjectName("notice")
        vids_layout.addWidget(vids_note)
        bg_layout.addWidget(self.vids_panel)

        self.local_panel = QWidget()
        local_config = QGridLayout(self.local_panel)
        local_config.setContentsMargins(0, 0, 0, 0)
        self.local_wan_variant = QComboBox()
        _add_options(self.local_wan_variant, [
            ("Wan 2.2 5B Chất lượng — 20 bước (~10 GB)", WAN_VARIANT_QUALITY),
            ("Wan 2.2 5B DMD — 4 bước (+~645 MB, Apache 2.0)", WAN_VARIANT_DMD),
            ("LTX-Video 2B Distilled — 8 bước (~11,5 GB)", LOCAL_MODEL_LTX),
            ("HunyuanVideo 1.5 480p — 8 bước (~21,5 GB, NVIDIA 16 GB+)", LOCAL_MODEL_HUNYUAN),
        ])
        self.local_wan_variant.currentIndexChanged.connect(self._local_wan_variant_changed)
        self.local_resolution = QComboBox()
        _add_options(self.local_resolution, [
            ("720p 1280×704 — RTX 3060", "1280x704"),
            ("Nhanh 832×480", "832x480"),
        ])
        self.local_frames = QComboBox()
        _add_options(self.local_frames, [
            ("49 frame — khoảng 2 giây", 49),
            ("81 frame — khoảng 3,4 giây", 81),
            ("121 frame — khoảng 5 giây", 121),
        ])
        _set_combo_value(self.local_frames, 81)
        self.local_steps = QSpinBox()
        self.local_steps.setRange(8, 40)
        self.local_steps.setValue(20)
        self.local_cfg = QDoubleSpinBox()
        self.local_cfg.setRange(1.0, 15.0)
        self.local_cfg.setSingleStep(.5)
        self.local_cfg.setValue(5.0)
        self.local_seed = QSpinBox()
        self.local_seed.setRange(-1, 2147483647)
        self.local_seed.setSpecialValueText("Ngẫu nhiên")
        self.local_seed.setValue(-1)
        self.local_negative_prompt = QPlainTextEdit()
        self.local_negative_prompt.setMaximumHeight(70)
        self.local_negative_prompt.setPlaceholderText("Tùy chọn; để trống dùng negative prompt an toàn tích hợp sẵn.")
        local_config.addWidget(QLabel("Model AI Local"), 0, 0)
        local_config.addWidget(self.local_wan_variant, 0, 1)
        local_config.addWidget(QLabel("Độ phân giải local"), 1, 0)
        local_config.addWidget(self.local_resolution, 1, 1)
        local_config.addWidget(QLabel("Thời lượng"), 2, 0)
        local_config.addWidget(self.local_frames, 2, 1)
        local_config.addWidget(QLabel("Số bước"), 3, 0)
        local_config.addWidget(self.local_steps, 3, 1)
        local_config.addWidget(QLabel("CFG"), 4, 0)
        local_config.addWidget(self.local_cfg, 4, 1)
        local_config.addWidget(QLabel("Seed"), 5, 0)
        local_config.addWidget(self.local_seed, 5, 1)
        self.local_check = QPushButton("Kiểm tra ComfyUI + model")
        self.local_check.clicked.connect(self._check_local_ai)
        local_config.addWidget(self.local_check, 7, 0, 1, 2)
        local_config.addWidget(QLabel("Negative prompt"), 6, 0)
        self.local_setup = QPushButton("Cài AI Local tự động (chỉ lần đầu)")
        self.local_setup.clicked.connect(self._start_local_setup)
        local_config.addWidget(self.local_setup, 8, 0, 1, 2)
        local_config.addWidget(self.local_negative_prompt, 6, 1)
        bg_layout.addWidget(self.local_panel)
        self.local_note = QLabel(
            "Wan 2.2 Native chạy trên máy, không tốn token/credit. RTX 3060 dùng model offload "
            "và tạo từng video tuần tự để giữ VRAM ổn định. Lần đầu bấm Cài AI Local; "
            "sau đó ComfyUI sẽ tự chạy ẩn cùng chương trình."
        )
        self.local_note.setWordWrap(True)
        self.local_note.setObjectName("notice")
        bg_layout.addWidget(self.local_note)
        self._local_wan_variant_changed()

        ai_actions = QHBoxLayout()
        self.ai_generate = QPushButton("Tạo video AI local")
        self.ai_generate.setObjectName("primary")
        self.ai_generate.clicked.connect(self.start_ai_video)
        self.ai_generate_batch = QPushButton("Tạo AI + render toàn bộ ảnh đã chọn")
        self.ai_generate_batch.setObjectName("primary")
        self.ai_generate_batch.clicked.connect(self.start_ai_batch)
        self.ai_cancel = QPushButton("Hủy tạo AI")
        self.ai_cancel.setEnabled(False)
        self.ai_cancel.clicked.connect(self.cancel_ai_generation)
        ai_actions.addWidget(self.ai_generate, 1)
        ai_actions.addWidget(self.ai_generate_batch, 1)
        ai_actions.addWidget(self.ai_cancel)
        bg_layout.addLayout(ai_actions)
        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 100)
        self.ai_progress.setValue(0)
        self.ai_status = QLabel("AI local chạy bằng GPU của máy này, không dùng token/credit. Hãy kiểm tra ComfyUI trước lần chạy đầu.")
        self.ai_status.setWordWrap(True)
        self.ai_status.setObjectName("muted")
        bg_layout.addWidget(self.ai_progress)
        bg_layout.addWidget(self.ai_status)
        self.ai_engine.currentIndexChanged.connect(self._ai_engine_changed)
        self._ai_source_mode_changed()
        self._ai_engine_changed()
        self.form.addWidget(background_group)

        asset_group = QGroupBox("3–4. LOGO, BIỂU TƯỢNG VÀ ẢNH BÌA")
        asset_layout = QVBoxLayout(asset_group)
        self.logo = FileField("Logo", IMAGE_FILTER, optional=True)
        self.artwork = FileField("Biểu tượng / Ảnh bìa", IMAGE_FILTER, optional=True)
        self.platform_icons = FileField("Biểu tượng nền tảng", IMAGE_FILTER, optional=True)
        self.input_blends: dict[str, QComboBox] = {}
        self.artwork_motion = QComboBox()
        _add_options(self.artwork_motion, [
            ("Đứng yên", "STATIC"), ("Trôi nhẹ", "FLOAT"), ("Nhịp phồng", "PULSE"),
            ("Thở nhẹ", "BREATH"), ("Xoay", "ROTATE"), ("Xoay rất chậm", "VERY_SLOW_ROTATE"),
            ("Nhịp theo beat", "BEAT_PULSE"),
        ])
        _set_combo_value(self.artwork_motion, "FLOAT")
        for kind, label, field in (
            ("logo", "Hòa trộn Logo", self.logo),
            ("artwork", "Hòa trộn Ảnh bìa", self.artwork),
            ("platform_icons", "Hòa trộn Icon nền tảng", self.platform_icons),
        ):
            asset_layout.addWidget(field)
            row = QHBoxLayout()
            combo = QComboBox()
            _add_options(combo, [
                ("Ảnh gốc (Normal)", "normal"),
                ("Lighten — bỏ nền đen", "lighten"),
                ("Screen — bỏ nền đen, sáng mềm", "screen"),
                ("Linear Dodge (Add)", "addition"),
            ])
            self.input_blends[kind] = combo
            row.addWidget(QLabel(label))
            row.addWidget(combo, 1)
            asset_layout.addLayout(row)
        asset_blend_note = QLabel(
            "Mỗi input có hòa trộn riêng. Chọn Ảnh gốc để giữ nguyên file; chọn Lighten/Screen để bỏ nền đen "
            "trong cả preview và video render."
        )
        asset_blend_note.setWordWrap(True)
        asset_blend_note.setObjectName("muted")
        asset_layout.addWidget(asset_blend_note)
        asset_layout.addWidget(QLabel("Chuyển động ảnh bìa"))
        asset_layout.addWidget(self.artwork_motion)
        self.form.addWidget(asset_group)

        wave_group = QGroupBox("5. FILE SÓNG / HIỆU ỨNG")
        wave_layout = QVBoxLayout(wave_group)
        self.waveform_media = FileField("File sóng chạy độc lập (PNG/GIF/MOV/MP4)", MEDIA_FILTER, optional=True)
        self.audio = FileField("Nhạc kèm theo visual — không điều khiển sóng", AUDIO_FILTER, optional=True)
        self.waveform = QComboBox()
        _add_options(self.waveform, [("Chạy file sóng lặp độc lập", "FILE"), ("Tắt lớp sóng", "NONE")])
        self.waveform_blend = QComboBox()
        _add_options(self.waveform_blend, [
            ("Ảnh gốc (Normal)", "normal"),
            ("Lighten — bỏ nền đen", "lighten"),
            ("Screen — bỏ nền đen, sáng mềm", "screen"),
            ("Linear Dodge (Add)", "addition"),
        ])
        self.input_blends["waveform"] = self.waveform_blend
        self.waveform_remove_white = QCheckBox("Xóa nền trắng của file sóng")
        self.waveform_remove_white.setChecked(True)
        wave_layout.addWidget(self.waveform_media)
        wave_layout.addWidget(self.audio)
        wave_layout.addWidget(self.waveform)
        wave_blend_row = QHBoxLayout()
        wave_blend_row.addWidget(QLabel("Hòa trộn file sóng"))
        wave_blend_row.addWidget(self.waveform_blend, 1)
        wave_layout.addLayout(wave_blend_row)
        wave_layout.addWidget(self.waveform_remove_white)
        wave_note = QLabel(
            "File sóng chỉ chạy lặp theo thời gian của chính file, không tăng giảm theo âm lượng nhạc. "
            "Chọn Lighten/Screen để bỏ nền đen; bật Xóa nền trắng nếu MOV không có alpha và bị đóng nền trắng."
        )
        wave_note.setWordWrap(True)
        wave_note.setObjectName("muted")
        wave_layout.addWidget(wave_note)
        self.form.addWidget(wave_group)

        effects_group = QGroupBox("HIỆU ỨNG TOÀN KHUNG")
        effects_layout = QVBoxLayout(effects_group)
        self.effect_overlay = FileField(
            "Overlay toàn cảnh có chuyển động (PNG/GIF/MOV/MP4)",
            MEDIA_FILTER,
            optional=True,
        )
        overlay_controls = QHBoxLayout()
        self.effect_overlay_blend = QComboBox()
        _add_options(self.effect_overlay_blend, [
            ("Normal — giữ alpha của file", "normal"),
            ("Lighten — bỏ nền đen", "lighten"),
            ("Screen — bỏ nền đen, sáng mềm", "screen"),
            ("Linear Dodge (Add) — cộng sáng", "addition"),
        ])
        _set_combo_value(self.effect_overlay_blend, "lighten")
        self.effect_overlay_opacity = QSlider(Qt.Orientation.Horizontal)
        self.effect_overlay_opacity.setRange(0, 100)
        self.effect_overlay_opacity.setValue(100)
        self.effect_overlay_opacity.setToolTip("Độ mờ của overlay toàn cảnh")
        self.effect_overlay_remove_white = QCheckBox("Xóa nền trắng của overlay toàn cảnh")
        self.effect_overlay_remove_white.setChecked(False)
        overlay_controls.addWidget(QLabel("Hòa trộn"))
        overlay_controls.addWidget(self.effect_overlay_blend, 1)
        overlay_controls.addWidget(QLabel("Độ mờ"))
        overlay_controls.addWidget(self.effect_overlay_opacity, 1)
        overlay_note = QLabel(
            "MOV/GIF/MP4 được phát và lặp theo thời lượng gốc, không bị giữ ở frame đầu. "
            "Dùng Lighten/Screen cho overlay nền đen; dùng Normal cho file có kênh alpha. "
            "Nếu file không có alpha thật và đã bị đóng nền trắng, bật ‘Xóa nền trắng’ ở trên."
        )
        overlay_note.setWordWrap(True)
        overlay_note.setObjectName("notice")
        effects_layout.addWidget(self.effect_overlay)
        effects_layout.addLayout(overlay_controls)
        effects_layout.addWidget(self.effect_overlay_remove_white)
        effects_layout.addWidget(overlay_note)
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
        # Kept only for loading older project files. New batch rendering reuses the
        # multi-image AI source and generated clips instead of asking for a folder twice.
        self.background_folder = FileField("Thư mục background để render hàng loạt", directory=True, optional=True)
        self.batch_recursive = QCheckBox("Lấy cả ảnh/video trong thư mục con")
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
        self.render_backgrounds = QPushButton("Render toàn bộ background đã chọn")
        self.render_backgrounds.setObjectName("primary")
        self.render_backgrounds.clicked.connect(self.start_batch_render)
        output_layout.addWidget(self.output_folder)
        output_layout.addWidget(QLabel("Tên file đầu ra tùy chọn"))
        output_layout.addWidget(self.output_name)
        output_layout.addLayout(config_row)
        output_layout.addWidget(render)
        self.batch_note = QLabel(
            "Dùng chính các ảnh đã chọn ở phần AI phía trên; nếu đã tạo AI hàng loạt, nút này dùng lại các clip AI. "
            "Mỗi background tạo một video 60 giây và giữ nguyên toàn bộ text, logo, ảnh, sóng, hiệu ứng, font và bố cục preview."
        )
        self.batch_note.setWordWrap(True)
        self.batch_note.setObjectName("muted")
        output_layout.addWidget(self.batch_note)
        output_layout.addWidget(self.render_backgrounds)
        self.form.addWidget(output_group)
        self.form.addStretch()

    def _connect_preview(self) -> None:
        self.background.changed.connect(self._background_changed)
        self.background.changed.connect(self._ai_background_source_changed)
        self.background.changed.connect(lambda _value: self._refresh_ai_source_summary())
        self.artwork.changed.connect(lambda value: self.preview.set_source("artwork", value))
        self.logo.changed.connect(lambda value: self.preview.set_source("logo", value))
        self.platform_icons.changed.connect(lambda value: self.preview.set_source("platform_icons", value))
        self.waveform_media.changed.connect(lambda value: self.preview.set_source("waveform_media", value))
        self.waveform_remove_white.toggled.connect(
            lambda value: self._set_preview_value("waveform_remove_white", value)
        )
        for kind, combo in self.input_blends.items():
            combo.currentIndexChanged.connect(
                lambda _index, kind=kind, combo=combo: self._set_input_blend(
                    kind, str(_combo_value(combo))
                )
            )
        self.effect_overlay.changed.connect(lambda value: self.preview.set_source("effect_overlay", value))
        self.effect_overlay_blend.currentIndexChanged.connect(
            lambda _index: self._set_preview_value("effect_overlay_blend", _combo_value(self.effect_overlay_blend))
        )
        self.effect_overlay_opacity.valueChanged.connect(
            lambda value: self._set_preview_value("effect_overlay_opacity", value / 100)
        )
        self.effect_overlay_remove_white.toggled.connect(
            lambda value: self._set_preview_value("effect_overlay_remove_white", value)
        )
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
        self.preview.element_activated.connect(self._activate_element_from_preview)
        self.preview.layout_changed.connect(self._layout_changed_from_preview)
        for widget in (self.element_lock, self.element_visible, self.element_anchor, self.element_blend, self.element_x, self.element_y, self.element_width, self.element_height, self.element_rotation, self.element_opacity, self.element_z):
            signal = widget.currentTextChanged if isinstance(widget, QComboBox) else widget.toggled if isinstance(widget, QCheckBox) else widget.valueChanged
            signal.connect(self._write_element_controls)
        self.font_family.currentFontChanged.connect(self._write_text_style_controls)
        self.selected_font_size.valueChanged.connect(self._write_text_style_controls)
        self.font_bold.toggled.connect(self._write_text_style_controls)
        self.font_italic.toggled.connect(self._write_text_style_controls)
        self.text_color_mode.currentIndexChanged.connect(self._write_text_style_controls)
        self.text_color.textChanged.connect(self._write_text_style_controls)
        self.text_gradient_end.textChanged.connect(self._write_text_style_controls)
        self.text_color.editingFinished.connect(lambda: self._finish_text_color_edit(self.text_color, False))
        self.text_gradient_end.editingFinished.connect(lambda: self._finish_text_color_edit(self.text_gradient_end, True))
        self.text_gradient_direction.currentIndexChanged.connect(self._write_text_style_controls)
        self.text_color_pick.clicked.connect(lambda: self._choose_text_color(self.text_color))
        self.text_gradient_end_pick.clicked.connect(lambda: self._choose_text_color(self.text_gradient_end))
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

    def _choose_text_color(self, field: QLineEdit) -> None:
        current = QColor(field.text())
        color = QColorDialog.getColor(current if current.isValid() else QColor("white"), self, "Ch\u1ecdn m\u00e0u ch\u1eef")
        if color.isValid():
            field.setText(color.name())

    def _finish_text_color_edit(self, field: QLineEdit, gradient_end: bool) -> None:
        element_id = self.element_choice.currentData()
        kind = str(element_id or "").split("_copy_", 1)[0]
        style = self.text_styles.get(kind)
        fallback = (style.color_end if gradient_end else style.color_start) if style else "#FFFFFF"
        normalized = normalize_text_color(field.text(), fallback)
        if field.text() != normalized:
            field.setText(normalized)

    def _sync_text_color_controls(self, enabled: bool) -> None:
        self.text_color_mode.setEnabled(enabled)
        self.text_color.setEnabled(enabled)
        self.text_color_pick.setEnabled(enabled)
        gradient_enabled = enabled and str(_combo_value(self.text_color_mode)) == "linear_gradient"
        self.text_gradient_end.setEnabled(gradient_enabled)
        self.text_gradient_end_pick.setEnabled(gradient_enabled)
        self.text_gradient_direction.setEnabled(gradient_enabled)

    def _set_text_animation(self, kind: str, animation: str) -> None:
        style = self.text_styles.get(kind)
        if not style:
            return
        style.animation = animation
        style.normalized()
        self.preview.text_styles = self.text_styles
        if style.animation == "typewriter":
            self.preview.restart()
        else:
            self.preview.update()

    def _set_preview_option(self, name: str, value: bool) -> None:
        setattr(self.preview, name, value)
        self.preview.update()

    def _set_input_blend(self, kind: str, value: str) -> None:
        mode = value if value in {"normal", "lighten", "screen", "addition"} else "normal"
        for element_id, layout in self.elements.items():
            if element_id.split("_copy_", 1)[0] == kind:
                layout.blend_mode = mode
        selected = str(self.element_choice.currentData() or "")
        if selected.split("_copy_", 1)[0] == kind:
            self._load_element_controls()
        self.preview.update()

    def _sync_input_blend_controls(self) -> None:
        for kind, combo in self.input_blends.items():
            layout = self.elements.get(kind)
            if not layout:
                continue
            combo.blockSignals(True)
            _set_combo_value(combo, layout.blend_mode)
            combo.blockSignals(False)

    def _preview_effects(self) -> None:
        self.preview.effects = [item for item in self.effects.values() if item.enabled]
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
            self._sync_input_blend_controls()
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
        self._sync_input_blend_controls()
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

    def _activate_element_from_preview(self, element_id: str) -> None:
        kind = element_id.split("_copy_", 1)[0]
        file_fields = {
            "logo": self.logo,
            "artwork": self.artwork,
            "platform_icons": self.platform_icons,
            "waveform": self.waveform_media,
        }
        text_fields = {
            "title": self.title,
            "subtitle": self.subtitle,
            "artist": self.artist,
            "custom_text": self.custom_text,
            "playlist": self.playlist,
        }
        if kind in file_fields:
            file_fields[kind].browse()
            return
        widget = text_fields.get(kind)
        if widget:
            self.scroll.ensureWidgetVisible(widget, 30, 80)
            widget.setFocus()

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
        _set_combo_value(self.element_blend, layout.blend_mode)
        image_element = element_id.split("_copy_", 1)[0] in {"artwork", "logo", "platform_icons", "waveform"}
        self.element_blend.setEnabled(image_element)
        self._load_text_style_controls(element_id)
        self._updating_element_controls = False

    def _load_text_style_controls(self, element_id: str) -> None:
        kind = element_id.split("_copy_", 1)[0]
        style = self.text_styles.get(kind)
        enabled = style is not None
        for widget in (self.font_family, self.selected_font_size, self.font_bold, self.font_italic):
            widget.setEnabled(enabled)
        self._sync_text_color_controls(enabled)
        if not style:
            return
        self.font_family.setCurrentFont(QFont(style.font_family))
        self.selected_font_size.setValue(style.font_size)
        self.font_bold.setChecked(style.bold)
        self.font_italic.setChecked(style.italic)
        _set_combo_value(self.text_color_mode, style.color_mode)
        self.text_color.setText(style.color_start)
        self.text_gradient_end.setText(style.color_end)
        _set_combo_value(self.text_gradient_direction, style.gradient_direction)
        self._sync_text_color_controls(True)

    def _write_text_style_controls(self, *_args) -> None:
        if self._updating_element_controls:
            return
        element_id = self.element_choice.currentData()
        kind = str(element_id or "").split("_copy_", 1)[0]
        if kind not in self.text_styles:
            return
        previous = self.text_styles[kind]
        self.text_styles[kind] = TextStyle(
            font_family=self.font_family.currentFont().family(),
            font_size=self.selected_font_size.value(),
            bold=self.font_bold.isChecked(),
            italic=self.font_italic.isChecked(),
            color_mode=str(_combo_value(self.text_color_mode)),
            color_start=normalize_text_color(self.text_color.text(), previous.color_start),
            color_end=normalize_text_color(self.text_gradient_end.text(), previous.color_end),
            gradient_direction=str(_combo_value(self.text_gradient_direction)),
            animation=self.text_styles[kind].animation,
        ).normalized()
        self.preview.text_styles = self.text_styles
        self._sync_text_color_controls(True)
        self.preview.update()

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
        layout.blend_mode = str(_combo_value(self.element_blend)) if self.element_blend.isEnabled() else "normal"
        layout.locked = self.element_lock.isChecked()
        layout.visible = self.element_visible.isChecked()
        layout.normalized()
        kind = str(element_id).split("_copy_", 1)[0]
        if element_id == kind and kind in self.input_blends:
            combo = self.input_blends[kind]
            combo.blockSignals(True)
            _set_combo_value(combo, layout.blend_mode)
            combo.blockSignals(False)
        self.layout_mode.setCurrentText("MANUAL")
        self.preview.update()

    def _layout_changed_from_preview(self) -> None:
        self.layout_mode.setCurrentText("MANUAL")
        self._load_element_controls()

    def collect(self) -> VisualProject:
        return VisualProject(
            title=self.title.text(), subtitle=self.subtitle.text(), artist=self.artist.text(), custom_text=self.custom_text.text(), playlist=self.playlist.toPlainText(),
            font_file=self.font_file.text(), font_size=0,
            text_styles={name: TextStyle(**asdict(style)) for name, style in self.text_styles.items()}, text_color=self.text_color.text(),
            text_opacity=self.text_opacity.value(), stroke_width=self.stroke_width.value(), text_shadow=self.text_shadow.isChecked(),
            background=self.background.text(), logo=self.logo.text(), artwork=self.artwork.text(), platform_icons=self.platform_icons.text(), audio=self.audio.text(),
            waveform_media=self.waveform_media.text(), waveform_remove_white=self.waveform_remove_white.isChecked(),
            effect_overlay=self.effect_overlay.text(),
            effect_overlay_blend=str(_combo_value(self.effect_overlay_blend)),
            effect_overlay_opacity=self.effect_overlay_opacity.value(),
            effect_overlay_remove_white=self.effect_overlay_remove_white.isChecked(),
            output_folder=self.output_folder.text(), background_folder=self.background_folder.text(),
            batch_recursive=self.batch_recursive.isChecked(), output_name=self.output_name.text(), resolution=self.resolution.currentText(),
            fps=self.fps.value(), encoder=self.encoder.currentText(), animation=_combo_value(self.animation),
            artwork_motion=_combo_value(self.artwork_motion), waveform=_combo_value(self.waveform),
            ai_prompt=self.ai_prompt.toPlainText(), ai_model=_combo_value(self.ai_model),
            ai_duration=int(_combo_value(self.ai_duration)), ai_resolution=self.ai_resolution.currentText(),
            ai_aspect_ratio=self.ai_aspect_ratio.currentText(),
            ai_engine=str(_combo_value(self.ai_engine)),
            ai_source_mode=str(_combo_value(self.ai_source_mode)),
            ai_source_folder=self.ai_source_folder.text(),
            ai_source_images=list(self._ai_source_images),
            ai_source_recursive=self.ai_source_recursive.isChecked(),
            ai_generated_backgrounds=list(self._ai_generated_backgrounds),
            vids_show_browser=self.vids_show_browser.isChecked(),
            local_wan_variant=self._selected_wan_variant(),
            local_resolution=str(_combo_value(self.local_resolution)),
            local_frames=int(_combo_value(self.local_frames)), local_steps=self.local_steps.value(),
            local_cfg=self.local_cfg.value(), local_seed=self.local_seed.value(),
            local_negative_prompt=self.local_negative_prompt.toPlainText(),
            layout_mode=self.layout_mode.currentText(), layout_template=self.layout_template.currentText(), layout_variant=self.layout_variant,
            elements={name: ElementLayout(**asdict(layout)) for name, layout in self.elements.items()},
            color_filter=self.color.preset.currentText(), manual_color=self.color.values(), lut=self.color.lut.text(), effects=self.effects.values(),
        )

    def load(self, project: VisualProject) -> None:
        for widget, value in ((self.title, project.title), (self.subtitle, project.subtitle), (self.artist, project.artist), (self.custom_text, project.custom_text)):
            widget.setText(value)
        self.playlist.setPlainText(project.playlist)
        for widget, value in ((self.font_file, project.font_file), (self.background, project.background), (self.logo, project.logo), (self.artwork, project.artwork), (self.platform_icons, project.platform_icons), (self.waveform_media, project.waveform_media), (self.effect_overlay, project.effect_overlay), (self.audio, project.audio), (self.output_folder, project.output_folder), (self.background_folder, project.background_folder), (self.ai_source_folder, project.ai_source_folder), (self.color.lut, project.lut)):
            widget.setText(value)
        self.batch_recursive.setChecked(project.batch_recursive)
        self.text_styles = {name: TextStyle(**asdict(style)) for name, style in project.text_styles.items()}
        self.preview.text_styles = self.text_styles
        for kind, combo in self.text_animation_combos.items():
            combo.blockSignals(True)
            _set_combo_value(combo, self.text_styles.get(kind, TextStyle()).animation)
            combo.blockSignals(False)
        self.text_color.blockSignals(True)
        self.text_color.setText(project.text_color)
        self.text_color.blockSignals(False)
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
        _set_combo_value(self.effect_overlay_blend, project.effect_overlay_blend)
        self.effect_overlay_opacity.setValue(project.effect_overlay_opacity)
        self.effect_overlay_remove_white.setChecked(project.effect_overlay_remove_white)
        self.ai_prompt.setPlainText(project.ai_prompt)
        _set_combo_value(self.ai_model, project.ai_model)
        _set_combo_value(self.ai_duration, project.ai_duration)
        self.ai_resolution.setCurrentText(project.ai_resolution)
        self.ai_aspect_ratio.setCurrentText(project.ai_aspect_ratio)
        _set_combo_value(self.ai_engine, project.ai_engine)
        _set_combo_value(self.ai_source_mode, project.ai_source_mode)
        self._ai_source_images = [str(path) for path in project.ai_source_images]
        self.ai_source_recursive.setChecked(project.ai_source_recursive)
        self._ai_generated_backgrounds = [str(path) for path in project.ai_generated_backgrounds]
        self.vids_show_browser.setChecked(project.vids_show_browser)
        self._ai_source_mode_changed()
        _set_combo_value(self.local_wan_variant, project.local_wan_variant)
        _set_combo_value(self.local_resolution, project.local_resolution)
        if self._selected_wan_variant() == LOCAL_MODEL_HUNYUAN:
            _set_combo_value(self.local_resolution, "832x480")
        _set_combo_value(self.local_frames, project.local_frames)
        self.local_steps.setValue(project.local_steps)
        self.local_cfg.setValue(project.local_cfg)
        self.local_seed.setValue(project.local_seed)
        self.local_negative_prompt.setPlainText(project.local_negative_prompt)
        self._ai_engine_changed()
        self.layout_mode.setCurrentText(project.layout_mode)
        self.layout_template.setCurrentText(project.layout_template)
        self.layout_variant = project.layout_variant
        self.elements = {name: ElementLayout(**asdict(layout)) for name, layout in project.elements.items()}
        self.preview.set_layouts(self.elements)
        self._sync_input_blend_controls()
        self._refresh_element_combo("title")
        self.color.preset.setCurrentText(project.color_filter)
        self.color.set_values(project.manual_color)
        self.effects.set_values(project.effects)

    def _choose_ai_source_images(self) -> None:
        start = ""
        if self._ai_source_images:
            start = str(Path(self._ai_source_images[0]).parent)
        elif self.background.text():
            start = str(Path(self.background.text()).parent)
        values, _ = QFileDialog.getOpenFileNames(
            self,
            "Chọn các ảnh sẽ tạo chuyển động AI",
            start,
            IMAGE_FILTER,
        )
        if not values:
            return
        self._invalidate_generated_ai_backgrounds()
        self._ai_source_images = [str(Path(value).resolve()) for value in values if is_still_image(value)]
        if self._ai_source_images:
            self.background.setText(self._ai_source_images[0])
        self._refresh_ai_source_summary()

    def _ai_source_mode_changed(self, *_args) -> None:
        mode = str(_combo_value(self.ai_source_mode))
        if self._last_ai_source_mode and mode != self._last_ai_source_mode:
            self._invalidate_generated_ai_backgrounds()
        self._last_ai_source_mode = mode
        self.ai_multi_panel.setVisible(mode == "MULTI")
        self.ai_source_folder.setVisible(mode == "FOLDER")
        self.ai_source_recursive.setVisible(mode == "FOLDER")
        self.ai_generate.setVisible(mode == "SINGLE")
        self.ai_generate_batch.setVisible(mode != "SINGLE")
        self._refresh_ai_source_summary()

    def _invalidate_generated_ai_backgrounds(self) -> None:
        if self._local_batch_mode or self._batch_from_ai:
            return
        self._ai_generated_backgrounds.clear()

    def _ai_source_folder_changed(self, _value: str) -> None:
        self._invalidate_generated_ai_backgrounds()
        self._refresh_ai_source_summary()

    def _ai_source_recursive_changed(self, _checked: bool) -> None:
        self._invalidate_generated_ai_backgrounds()
        self._refresh_ai_source_summary()

    def _ai_background_source_changed(self, _value: str) -> None:
        if str(_combo_value(self.ai_source_mode)) == "SINGLE":
            self._invalidate_generated_ai_backgrounds()

    def _selected_ai_sources(self, project: VisualProject | None = None) -> list[str]:
        project = project or self.collect()
        mode = str(project.ai_source_mode or "SINGLE").upper()
        if mode == "SINGLE":
            sources = [project.background] if Path(project.background).is_file() and is_still_image(project.background) else []
            error = "Hãy chọn một ảnh nền PNG/JPG/WebP."
        elif mode == "MULTI":
            sources = [path for path in project.ai_source_images if Path(path).is_file() and is_still_image(path)]
            error = "Hãy bấm ‘Chọn nhiều ảnh…’ và chọn ít nhất một ảnh hợp lệ."
        elif mode == "FOLDER":
            if not project.ai_source_folder.strip():
                raise ValueError("Hãy chọn thư mục ảnh nguồn AI hợp lệ.")
            folder = Path(project.ai_source_folder)
            if not folder.is_dir():
                raise ValueError("Hãy chọn thư mục ảnh nguồn AI hợp lệ.")
            sources = [
                path for path in background_files(str(folder), project.ai_source_recursive)
                if is_still_image(path)
            ]
            error = "Thư mục đã chọn không có ảnh PNG/JPG/WebP/BMP/TIFF được hỗ trợ."
        else:
            raise ValueError(f"Chế độ nguồn ảnh AI không hợp lệ: {mode}")
        if not sources:
            raise ValueError(error)
        return list(dict.fromkeys(sources))

    def _refresh_ai_source_summary(self) -> None:
        mode = str(_combo_value(self.ai_source_mode))
        if mode == "SINGLE":
            source = self.background.text()
            text = f"Sẽ tạo 1 video từ: {Path(source).name}" if source and Path(source).is_file() and is_still_image(source) else "Chưa chọn ảnh nền hợp lệ."
        elif mode == "MULTI":
            count = sum(Path(path).is_file() and is_still_image(path) for path in self._ai_source_images)
            text = f"Đã chọn {count} ảnh. Mỗi ảnh sẽ tạo một video riêng bằng cùng prompt."
        else:
            try:
                folder_text = self.ai_source_folder.text()
                folder = Path(folder_text) if folder_text else Path("__missing_ai_source_folder__")
                count = sum(
                    is_still_image(path)
                    for path in background_files(str(folder), self.ai_source_recursive.isChecked())
                ) if folder.is_dir() else 0
                text = f"Tìm thấy {count} ảnh. Mỗi ảnh sẽ tạo một video riêng bằng cùng prompt."
            except OSError:
                text = "Không đọc được thư mục ảnh nguồn."
        generated_count = sum(Path(path).is_file() for path in self._ai_generated_backgrounds)
        if generated_count:
            text += f" Đã lưu state {generated_count} clip AI để render/re-render bố cục 60 giây."
        if mode == "MULTI":
            self.ai_multi_summary.setText(text)
        self.ai_source_summary.setText(text)
        if hasattr(self, "render_backgrounds"):
            self.render_backgrounds.setText(
                f"Render toàn bộ {generated_count} background AI đã tạo"
                if generated_count else "Render toàn bộ background đã chọn"
            )

    def _ai_resolution_changed(self, value: str) -> None:
        if value in {"1080p", "4k"}:
            _set_combo_value(self.ai_duration, 8)
            self.ai_duration.setEnabled(False)
        else:
            self.ai_duration.setEnabled(True)

    def _ai_engine_changed(self, *_args) -> None:
        engine = str(_combo_value(self.ai_engine) or "LOCAL").upper()
        local = engine == "LOCAL"
        veo = engine == "VEO"
        vids = engine == "GOOGLE_VIDS"
        self.local_panel.setVisible(local)
        self.local_note.setVisible(local)
        self.veo_panel.setVisible(veo)
        self.vids_panel.setVisible(vids)
        self.ai_generate.setText(
            "Tạo video AI local"
            if local else "Tạo video chuyển động bằng Veo"
            if veo else "Tạo video bằng Google Vids Web"
        )
        self.ai_generate_batch.setText(
            "Tạo AI local + render toàn bộ ảnh"
            if local else "Tạo Veo + render toàn bộ ảnh"
            if veo else "Tạo Google Vids + render toàn bộ ảnh"
        )
        if not self.local_ai_worker.running and not self.ai_worker.running and not self.google_vids_worker.busy:
            if local:
                text = (
                    f"{local_model_label(self._selected_wan_variant())} chạy local, không dùng token/credit. "
                    "Hãy kiểm tra ComfyUI trước lần chạy đầu."
                )
            elif veo:
                text = "Veo dùng Gemini API key và có thể tính credit."
            else:
                text = (
                    "Google Vids đã có phiên đăng nhập; có thể tạo clip bằng quota web."
                    if google_vids_profile_ready()
                    else "Chưa có phiên Google Vids. Hãy đăng nhập một lần trước khi tạo clip."
                )
            self.ai_status.setText(text)

    def _start_vids_login(self) -> None:
        try:
            if self.worker.running or self.local_ai_worker.running or self.ai_worker.running or self.local_setup_worker.running or self.google_vids_worker.busy:
                raise RuntimeError("Một tác vụ AI/render khác đang chạy.")
            self.vids_login.setEnabled(False)
            self.vids_check.setEnabled(False)
            self.ai_generate.setEnabled(False)
            self.ai_generate_batch.setEnabled(False)
            self.ai_cancel.setEnabled(True)
            self.ai_progress.setValue(1)
            self.ai_status.setText(
                "Đang mở Chrome riêng. Hãy tự đăng nhập Google, mở được Vids rồi đóng cửa sổ Chrome."
            )
            self.google_vids_worker.login(str(GOOGLE_VIDS_PROFILE_DIR))
        except Exception as exc:
            self.vids_login.setEnabled(True)
            self.vids_check.setEnabled(True)
            show_error(self, "Không thể mở đăng nhập Google Vids", exc)

    def _check_vids_login(self) -> None:
        if google_vids_profile_ready():
            QMessageBox.information(
                self,
                "Google Vids",
                "Đã tìm thấy profile Google Vids riêng của Visual Loop Studio. Nếu Google đã hết phiên, lần tạo tiếp theo sẽ yêu cầu đăng nhập lại.",
            )
            self.ai_status.setText("Google Vids đã có profile đăng nhập trên máy này.")
        else:
            show_error(
                self,
                "Google Vids chưa đăng nhập",
                "Chưa tìm thấy phiên đăng nhập. Hãy bấm ‘Đăng nhập Google Vids (chỉ lần đầu)’.",
            )

    def _vids_login_finished(self, _profile: str) -> None:
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(100)
        self.ai_status.setText("Đã lưu phiên Google Vids. Tool không lưu mật khẩu Google.")
        QMessageBox.information(
            self,
            "Đăng nhập Google Vids hoàn tất",
            "Đã lưu profile Chrome riêng trên máy này. Từ giờ có thể chọn ảnh/prompt và chạy Google Vids trong app.",
        )

    def _vids_login_failed(self, message: str) -> None:
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Đăng nhập Google Vids chưa hoàn tất.")
        show_error(self, "Đăng nhập Google Vids thất bại", message)

    def _selected_wan_variant(self) -> str:
        value = str(_combo_value(self.local_wan_variant) or WAN_VARIANT_QUALITY)
        return normalize_wan_variant(value)

    @staticmethod
    def _local_model_slug(value: str) -> str:
        return {
            WAN_VARIANT_QUALITY: "wan22",
            WAN_VARIANT_DMD: "wan22_dmd",
            LOCAL_MODEL_LTX: "ltx2b",
            LOCAL_MODEL_HUNYUAN: "hunyuan15",
        }[normalize_wan_variant(value)]

    def _local_wan_variant_changed(self, _index: int = -1) -> None:
        variant = self._selected_wan_variant()
        fixed_fast = variant in {WAN_VARIANT_DMD, LOCAL_MODEL_LTX, LOCAL_MODEL_HUNYUAN}
        if variant == WAN_VARIANT_DMD:
            self.local_steps.setRange(4, 4)
            self.local_steps.setValue(4)
            self.local_cfg.setRange(1.0, 1.0)
            self.local_cfg.setValue(1.0)
            self.local_setup.setText("Cài / tải Wan DMD 4 bước đang chọn")
            self.local_note.setText(
                "Wan DMD dùng checkpoint Wan TI2V 5B gốc cùng LoRA DMD 4 bước, CFG 1.0. "
                "Tool tự chuyển 600 tensor LoRA sang định dạng ComfyUI và kiểm tra trước khi chạy. "
                "Model nền và LoRA đều dùng giấy phép Apache 2.0, phù hợp sử dụng thương mại. "
                "Để giảm rung, nên mô tả một hành động chính và ưu tiên 49/81 frame ở 832×480. "
                "Nếu cần bám prompt và ổn định cao nhất, hãy dùng Wan Chất lượng 20 bước."
            )
        elif variant == LOCAL_MODEL_LTX:
            self.local_steps.setRange(8, 8)
            self.local_steps.setValue(8)
            self.local_cfg.setRange(1.0, 1.0)
            self.local_cfg.setValue(1.0)
            _set_combo_value(self.local_resolution, "832x480")
            self.local_setup.setText("Cài / tải LTX-Video 2B Distilled")
            self.local_note.setText(
                "LTX-Video 2B Distilled là chế độ render/farm nhanh, chạy local 8 bước, CFG 1 và không tốn token. "
                "Giấy phép cho phép thương mại khi doanh thu năm dưới 10 triệu USD; nội dung công khai cần ghi là AI tạo. "
                "Model khoảng 11,5 GB; 832×480 được khuyến nghị để ưu tiên tốc độ."
            )
        elif variant == LOCAL_MODEL_HUNYUAN:
            self.local_steps.setRange(8, 8)
            self.local_steps.setValue(8)
            self.local_cfg.setRange(1.0, 1.0)
            self.local_cfg.setValue(1.0)
            _set_combo_value(self.local_resolution, "832x480")
            self.local_setup.setText("Cài / tải HunyuanVideo 1.5 480p")
            self.local_note.setText(
                "HunyuanVideo 1.5 I2V Step Distilled chạy 8 bước, chỉ bật cho NVIDIA CUDA và khuyến nghị từ 16 GB VRAM. "
                "Model khoảng 21,5 GB, chạy local không tốn token. Giấy phép Tencent có giới hạn vùng lãnh thổ "
                "và yêu cầu đánh dấu nội dung AI khi công bố; hãy kiểm tra giấy phép trước khi phân phối quốc tế."
            )
        else:
            self.local_steps.setRange(8, 40)
            self.local_steps.setValue(20)
            self.local_cfg.setRange(1.0, 15.0)
            self.local_cfg.setValue(5.0)
            self.local_setup.setText("Cài / tải Wan Chất lượng đang chọn")
            self.local_note.setText(
                "Wan 2.2 Chất lượng mặc định 20 bước, ưu tiên chi tiết và độ ổn định chuyển động. "
                "Chạy hoàn toàn trên máy, không tốn token/credit; ComfyUI tự chạy ẩn sau khi cài."
            )
        self.local_steps.setEnabled(not fixed_fast)
        self.local_cfg.setEnabled(not fixed_fast)
        self.local_resolution.setEnabled(variant != LOCAL_MODEL_HUNYUAN)

    def _local_options(self, project: VisualProject | None = None) -> dict:
        project = project or self.collect()
        variant = normalize_wan_variant(project.local_wan_variant)
        resolution = "832x480" if variant == LOCAL_MODEL_HUNYUAN else project.local_resolution
        width_text, height_text = resolution.lower().split("x", 1)
        return {
            "comfyui_url": self.settings.comfyui_url,
            "workflow_path": self.settings.comfyui_workflow,
            "runtime_root": self.settings.local_ai_root,
            "wan_variant": variant,
            "prompt": project.ai_prompt.strip(),
            "width": int(width_text),
            "height": int(height_text),
            "length": int(project.local_frames),
            "steps": int(project.local_steps),
            "cfg": float(project.local_cfg),
            "seed": int(project.local_seed),
            "negative_prompt": project.local_negative_prompt,
        }

    def start_ai_video(self) -> None:
        try:
            source = self.background.text()
            if not source or not Path(source).is_file() or not is_still_image(source):
                raise ValueError("Hãy chọn một file ảnh nền PNG/JPG/WebP trước khi tạo video AI.")
            prompt = self.ai_prompt.toPlainText().strip()
            if not prompt:
                raise ValueError("Hãy nhập prompt mô tả tay/chân, người hoặc hiệu ứng cần chuyển động.")
            if self.ai_worker.running or self.local_ai_worker.running or self.google_vids_worker.busy or self.worker.running or self.local_setup_worker.running:
                raise RuntimeError("Một tác vụ AI/render khác đang chạy.")
            engine = str(_combo_value(self.ai_engine) or "LOCAL").upper()
            self._active_ai_engine = engine
            if engine == "LOCAL":
                if not self._local_runtime_available():
                    self._offer_local_setup()
                    return
                folder = Path(self.output_folder.text() or str(Path(source).resolve().parent)) / "AI_Local_Clips"
                slug = self._local_model_slug(self._selected_wan_variant())
                output = unique_output(
                    str(folder), f"{slug}_{Path(source).stem}.mp4", f"{slug}_{Path(source).stem}", ".mp4"
                )
                self.ai_generate.setEnabled(False)
                self.ai_cancel.setEnabled(True)
                self.ai_progress.setValue(1)
                self.local_ai_worker.start(
                    image_path=source,
                    output_path=str(output),
                    **self._local_options(),
                )
                return
            if engine == "GOOGLE_VIDS":
                if not google_vids_profile_ready():
                    raise ValueError(
                        "Chưa đăng nhập Google Vids. Hãy bấm ‘Đăng nhập Google Vids (chỉ lần đầu)’ trước."
                    )
                folder = Path(self.output_folder.text() or str(Path(source).resolve().parent)) / "Google_Vids_Clips"
                output = unique_output(
                    str(folder), f"google_vids_{Path(source).stem}.mp4", f"google_vids_{Path(source).stem}", ".mp4"
                )
                self.ai_generate.setEnabled(False)
                self.ai_generate_batch.setEnabled(False)
                self.vids_login.setEnabled(False)
                self.vids_check.setEnabled(False)
                self.ai_cancel.setEnabled(True)
                self.ai_progress.setValue(1)
                self.google_vids_worker.start(
                    image_path=source,
                    prompt=prompt,
                    output_path=str(output),
                    profile_dir=str(GOOGLE_VIDS_PROFILE_DIR),
                    show_browser=self.vids_show_browser.isChecked(),
                )
                return
            api_key = resolve_secret("gemini_api_key", "GEMINI_API_KEY", self.settings.gemini_api_key)
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

    def cancel_ai_generation(self) -> None:
        self._local_batch_queue.clear()
        self._local_batch_mode = False
        if self.local_setup_worker.running:
            self.local_setup_worker.cancel()
        if self.local_ai_worker.running:
            self.local_ai_worker.cancel()
        if self.ai_worker.running:
            self.ai_worker.cancel()
        if self.google_vids_worker.busy:
            self.google_vids_worker.cancel()
        if self.worker.running:
            self.worker.cancel()

    def _check_local_ai(self) -> None:
        try:
            if not self._local_runtime_available():
                self._offer_local_setup()
                return
            self.local_check.setEnabled(False)
            self.ai_status.setText(f"Đang kiểm tra ComfyUI, node và {local_model_label(self._selected_wan_variant())}…")
            self.local_ai_worker.check(
                self.settings.comfyui_url,
                self.settings.comfyui_workflow,
                self.settings.local_ai_root,
                self._selected_wan_variant(),
            )
        except Exception as exc:
            self.local_check.setEnabled(True)
            show_error(self, "Không thể kiểm tra ComfyUI", exc)

    def _local_check_finished(self, success: bool, message: str) -> None:
        self.local_check.setEnabled(True)
        self.ai_status.setText(message)
        silent = self._silent_local_check
        self._silent_local_check = False
        if not success and not silent:
            show_error(self, "AI Local chưa sẵn sàng", message)

    def _local_runtime_available(self) -> bool:
        return server_ready(self.settings.comfyui_url) or is_runtime_installed(
            self.settings.local_ai_root, wan_variant=self._selected_wan_variant()
        )

    def _offer_local_setup(self) -> None:
        backend = detect_runtime_backend()
        variant = self._selected_wan_variant()
        validate_local_model_backend(variant, backend)
        model_label = local_model_label(variant)
        answer = QMessageBox.question(
            self,
            "Cài model AI Local",
            f"Đã nhận diện: {backend.label}.\n\n"
            f"Máy chưa có {model_label}. Tool sẽ tải model đang chọn cùng các thành phần dùng chung, "
            "cài một lần và tự chạy ẩn ở các lần sau.\n\nCài ngay?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_local_setup()

    def _start_local_setup(self) -> None:
        try:
            if (
                self.local_setup_worker.running
                or self.local_ai_worker.running
                or self.ai_worker.running
                or self.google_vids_worker.busy
                or self.worker.running
            ):
                raise RuntimeError("Một tác vụ AI/render khác đang chạy.")
            configured = Path(self.settings.local_ai_root) if self.settings.local_ai_root else default_runtime_root()
            initial = configured if configured.exists() else configured.parent
            selected = QFileDialog.getExistingDirectory(
                self,
                "Chọn ổ hoặc thư mục lưu AI Local",
                str(initial),
            )
            if not selected:
                return
            selected_path = Path(selected).resolve()
            root = selected_path if selected_path.name.lower() == "visualloopstudio_ai" else selected_path / "VisualLoopStudio_AI"
            backend = detect_runtime_backend()
            variant = self._selected_wan_variant()
            validate_local_model_backend(variant, backend)
            model_label = local_model_label(variant)
            required_gb = required_free_bytes(root, backend, variant) / 1024**3
            answer = QMessageBox.question(
                self,
                "Xác nhận cài AI Local",
                f"GPU/backend nhận diện: {backend.label}\n"
                f"ComfyUI và {model_label} sẽ được tải vào:\n{root}\n\n"
                f"Cần khoảng {required_gb:.1f} GB trống ở trạng thái hiện tại. File tải dở sẽ được giữ để tiếp tục.\n\nBắt đầu?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.settings.local_ai_root = str(root)
            self.settings.local_ai_auto_start = True
            self.settings_changed.emit()
            self.local_setup.setEnabled(False)
            self.local_wan_variant.setEnabled(False)
            self.local_check.setEnabled(False)
            self.ai_generate.setEnabled(False)
            self.ai_generate_batch.setEnabled(False)
            self.ai_cancel.setEnabled(True)
            self.ai_progress.setValue(0)
            self.ai_status.setText("Đang chuẩn bị cài AI Local…")
            self.local_setup_worker.start(str(root), variant)
        except Exception as exc:
            show_error(self, "Không thể cài AI Local", exc)

    def _local_setup_progress(self, value: int, message: str) -> None:
        self.ai_progress.setValue(max(0, min(100, value)))
        self.ai_status.setText(message)

    def _local_setup_finished(self, root: str) -> None:
        self.settings.local_ai_root = root
        self.settings.local_ai_auto_start = True
        self.settings_changed.emit()
        self.local_setup.setEnabled(True)
        self.local_wan_variant.setEnabled(True)
        self.local_check.setEnabled(False)
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_status.setText("Đã cài AI Local. Đang tự khởi động ComfyUI…")
        try:
            self.local_ai_worker.check(
                self.settings.comfyui_url,
                self.settings.comfyui_workflow,
                root,
                self._selected_wan_variant(),
            )
        except Exception as exc:
            self.local_check.setEnabled(True)
            show_error(self, "Đã cài nhưng chưa khởi động được ComfyUI", exc)

    def _local_setup_failed(self, message: str) -> None:
        self.local_setup.setEnabled(True)
        self.local_wan_variant.setEnabled(True)
        self.local_check.setEnabled(True)
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Cài AI Local chưa hoàn tất; có thể chạy lại để tiếp tục tải.")
        show_error(self, "Cài AI Local thất bại", message)

    def _local_setup_canceled(self) -> None:
        self.local_setup.setEnabled(True)
        self.local_wan_variant.setEnabled(True)
        self.local_check.setEnabled(True)
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Đã dừng cài AI Local. File tải dở được giữ để tiếp tục lần sau.")

    def _auto_start_local_ai(self) -> None:
        if not self.settings.local_ai_auto_start or not is_runtime_installed(
            self.settings.local_ai_root, wan_variant=self._selected_wan_variant()
        ):
            return
        if self.local_ai_worker.running or self.local_ai_worker.checking or server_ready(self.settings.comfyui_url):
            return
        try:
            self._silent_local_check = True
            self.ai_status.setText("Đang tự khởi động ComfyUI ẩn trong nền…")
            self.local_ai_worker.check(
                self.settings.comfyui_url,
                self.settings.comfyui_workflow,
                self.settings.local_ai_root,
                self._selected_wan_variant(),
            )
        except Exception:
            self._silent_local_check = False

    def _local_ai_progress(self, value: int, message: str) -> None:
        self.ai_progress.setValue(max(0, min(100, value)))
        if self._local_batch_mode:
            current = self._local_batch_total - len(self._local_batch_queue)
            message = f"AI {current}/{self._local_batch_total} • {Path(self._local_batch_current).name} • {message}"
        self.ai_status.setText(message)

    def _local_ai_finished(self, output: str) -> None:
        if self._local_batch_mode:
            self._ai_batch_clip_finished(output)
            return
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(100)
        self.ai_status.setText(f"Đã tạo {local_model_label(self._selected_wan_variant())} và chọn làm video nền: {output}")
        self.background.setText(output)
        _set_combo_value(self.animation, "STATIC")
        self.fps.setValue(24)
        QMessageBox.information(
            self,
            "Tạo video local hoàn tất",
            "Video AI local đã được chọn làm nền. Camera toàn khung cố định và FPS đã đặt về 24.",
        )

    def _local_ai_failed(self, message: str) -> None:
        if self._local_batch_mode:
            self._local_batch_failures.append(f"{Path(self._local_batch_current).name}: {message}")
            self._continue_ai_batch()
            return
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Tạo video AI local thất bại.")
        show_error(self, "Tạo video AI local thất bại", message)

    def _local_ai_canceled(self) -> None:
        self._local_batch_queue.clear()
        self._local_batch_mode = False
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        saved = len(self._ai_generated_backgrounds)
        self.ai_progress.setValue(100 if saved else 0)
        self.ai_status.setText(
            f"Đã hủy AI local; vẫn giữ {saved} clip để render lại."
            if saved else "Đã hủy tạo video AI local."
        )
        self._refresh_ai_source_summary()
        self.status.stopped("Đã hủy hàng đợi AI local.")

    def _ai_progress(self, value: int, message: str) -> None:
        self.ai_progress.setValue(max(0, min(100, value)))
        if self._local_batch_mode:
            current = self._local_batch_total - len(self._local_batch_queue)
            message = f"AI {current}/{self._local_batch_total} • {Path(self._local_batch_current).name} • {message}"
        self.ai_status.setText(message)

    def _ai_finished(self, output: str) -> None:
        if self._local_batch_mode:
            self._ai_batch_clip_finished(output)
            return
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(100)
        self.ai_status.setText(f"Đã tạo và chọn làm video nền: {output}")
        self.background.setText(output)
        _set_combo_value(self.animation, "STATIC")
        self.fps.setValue(24)
        engine_name = "Google Vids Web" if self._active_ai_engine == "GOOGLE_VIDS" else "Veo"
        QMessageBox.information(
            self,
            "Tạo video AI hoàn tất",
            f"Video {engine_name} đã được chọn làm nền. Camera toàn khung đang ở chế độ cố định "
            "và FPS đã đặt về 24 để khớp video AI.",
        )

    def _ai_failed(self, message: str) -> None:
        if self._local_batch_mode:
            self._local_batch_failures.append(f"{Path(self._local_batch_current).name}: {message}")
            self._continue_ai_batch()
            return
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_status.setText("Tạo video AI thất bại.")
        show_error(self, "Tạo video AI thất bại", message)

    def _ai_canceled(self) -> None:
        if self._local_batch_mode:
            self._local_batch_queue.clear()
            self._local_batch_mode = False
            self.status.stopped("Đã hủy hàng đợi AI.")
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        saved = len(self._ai_generated_backgrounds)
        self.ai_progress.setValue(100 if saved else 0)
        self.ai_status.setText(
            f"Đã hủy AI; vẫn giữ {saved} clip để render lại."
            if saved else "Đã hủy tạo video AI."
        )
        self._refresh_ai_source_summary()

    def _ai_batch_clip_finished(self, output: str) -> None:
        self._local_batch_clip = output
        self.ai_progress.setValue(100)
        normalized = str(Path(output))
        if normalized not in self._ai_generated_backgrounds:
            self._ai_generated_backgrounds.append(normalized)
        if normalized not in self._local_batch_completed:
            self._local_batch_completed.append(normalized)
        self.preview.set_source("background", normalized)
        self._refresh_ai_source_summary()
        self._continue_ai_batch()

    def _continue_ai_batch(self) -> None:
        if self._local_batch_queue:
            QTimer.singleShot(0, self._start_next_local_ai)
        else:
            QTimer.singleShot(0, self._start_generated_ai_render_batch)

    def _start_generated_ai_render_batch(self) -> None:
        if not self._local_batch_mode:
            return
        generated = [
            str(Path(path)) for path in self._ai_generated_backgrounds
            if Path(path).is_file()
        ]
        if not generated:
            self._finish_local_batch()
            return
        self._local_batch_mode = False
        self._batch_from_ai = True
        self._batch_template = VisualProject.from_dict(
            self._local_batch_template.to_dict() if self._local_batch_template else {}
        )
        self._batch_template.ai_generated_backgrounds = list(generated)
        self._batch_queue = list(dict.fromkeys(generated))
        self._batch_total = len(self._batch_queue)
        self._batch_completed = []
        self._batch_failures = []
        self._batch_current = ""
        self._batch_mode = True
        self.ai_status.setText(
            f"Đã tạo {len(generated)}/{self._local_batch_total} clip AI. Đang render toàn bộ với bố cục preview đã lưu…"
        )
        self._start_next_batch()

    def start_render(self) -> None:
        try:
            if (
                self.worker.running
                or self.local_ai_worker.running
                or self.ai_worker.running
                or self.google_vids_worker.busy
                or self.local_setup_worker.running
            ):
                raise RuntimeError("Một tác vụ AI/render khác đang chạy.")
            self._batch_mode = False
            self._batch_queue.clear()
            self._local_batch_mode = False
            self._local_batch_queue.clear()
            project = self.collect()
            job = build_visual_job(project, self.settings)
            self.settings.last_output_folder = project.output_folder
            self.settings.encoder = project.encoder
            self.settings_changed.emit()
            self.status.begin()
            self.worker.start(job)
        except Exception as exc:
            show_error(self, "Không thể bắt đầu render", exc)

    def start_batch_render(self) -> None:
        try:
            if (
                self.worker.running
                or self.local_ai_worker.running
                or self.ai_worker.running
                or self.google_vids_worker.busy
                or self.local_setup_worker.running
            ):
                raise RuntimeError("Một tác vụ AI/render khác đang chạy.")
            template = self.collect()
            if not template.output_folder:
                raise ValueError("Hãy chọn thư mục lưu video đầu ra.")
            generated = [
                str(Path(path)) for path in self._ai_generated_backgrounds
                if Path(path).is_file()
            ]
            if generated:
                backgrounds = list(dict.fromkeys(generated))
            elif str(template.ai_source_mode).upper() == "SINGLE":
                backgrounds = [template.background] if Path(template.background).is_file() else []
            else:
                backgrounds = self._selected_ai_sources(template)
            if not backgrounds:
                raise ValueError(
                    "Chưa có background để render. Hãy chọn nhiều ảnh ở phần nguồn AI hoặc tạo AI hàng loạt trước."
                )
            self._batch_template = VisualProject.from_dict(template.to_dict())
            self._batch_queue = backgrounds
            self._batch_total = len(backgrounds)
            self._batch_completed = []
            self._batch_failures = []
            self._batch_current = ""
            self._batch_mode = True
            self._batch_from_ai = False
            self._local_batch_mode = False
            self._local_batch_queue.clear()
            self.settings.last_output_folder = template.output_folder
            self.settings.encoder = template.encoder
            self.settings_changed.emit()
            self._start_next_batch()
        except Exception as exc:
            show_error(self, "Không thể render hàng loạt", exc)

    def start_ai_batch(self) -> None:
        try:
            if (
                self.worker.running
                or self.local_ai_worker.running
                or self.ai_worker.running
                or self.google_vids_worker.busy
                or self.local_setup_worker.running
            ):
                raise RuntimeError("Một tác vụ AI/render khác đang chạy.")
            template = self.collect()
            if not template.ai_prompt.strip():
                raise ValueError("Hãy nhập prompt chuyển động dùng chung cho tất cả ảnh.")
            if not template.output_folder:
                raise ValueError("Hãy chọn thư mục lưu video đầu ra.")
            images = self._selected_ai_sources(template)
            engine = str(template.ai_engine or "LOCAL").upper()
            self._ai_batch_api_key = ""
            if engine == "LOCAL":
                if not self._local_runtime_available():
                    self._offer_local_setup()
                    return
                self._local_options(template)
            elif engine == "GOOGLE_VIDS":
                if not google_vids_profile_ready():
                    raise ValueError(
                        "Chưa đăng nhập Google Vids. Hãy bấm ‘Đăng nhập Google Vids (chỉ lần đầu)’ trước."
                    )
            else:
                self._ai_batch_api_key = resolve_secret(
                    "gemini_api_key", "GEMINI_API_KEY", self.settings.gemini_api_key
                )
                if not self._ai_batch_api_key:
                    raise ValueError("Chưa có Gemini API key. Mở menu Cài đặt và nhập key cho Veo.")
                answer = QMessageBox.warning(
                    self,
                    "Xác nhận dùng Veo API",
                    f"Sắp gửi {len(images)} ảnh lên Veo. Mỗi ảnh là một tác vụ API và có thể tính credit. Tiếp tục?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self._batch_mode = False
            self._batch_from_ai = False
            self._batch_queue.clear()
            self._ai_generated_backgrounds.clear()
            template.ai_generated_backgrounds = []
            self._refresh_ai_source_summary()
            self._local_batch_template = VisualProject.from_dict(template.to_dict())
            self._local_batch_queue = images
            self._local_batch_total = len(images)
            self._local_batch_completed = []
            self._local_batch_failures = []
            self._local_batch_current = ""
            self._local_batch_clip = ""
            self._local_batch_mode = True
            self._ai_batch_engine = engine
            self._active_ai_engine = engine
            self.ai_generate.setEnabled(False)
            self.ai_generate_batch.setEnabled(False)
            if engine == "GOOGLE_VIDS":
                self.vids_login.setEnabled(False)
                self.vids_check.setEnabled(False)
            self.ai_cancel.setEnabled(True)
            self.settings.last_output_folder = template.output_folder
            self.settings.encoder = template.encoder
            self.settings_changed.emit()
            self._start_next_local_ai()
        except Exception as exc:
            show_error(self, "Không thể chạy AI hàng loạt", exc)

    def start_local_ai_batch(self) -> None:
        """Backward-compatible action name used by older UI/project code."""
        self.start_ai_batch()

    def _start_next_local_ai(self) -> None:
        while self._local_batch_mode and self._local_batch_queue:
            source = self._local_batch_queue.pop(0)
            self._local_batch_current = source
            current = self._local_batch_total - len(self._local_batch_queue)
            engine_label = {
                "LOCAL": local_model_label(self._local_batch_template.local_wan_variant),
                "GOOGLE_VIDS": "Google Vids Web",
                "VEO": "Veo",
            }.get(self._ai_batch_engine, self._ai_batch_engine)
            clip_folder_name = {
                "LOCAL": "AI_Local_Clips",
                "GOOGLE_VIDS": "Google_Vids_Clips",
                "VEO": "AI_Veo_Clips",
            }.get(self._ai_batch_engine, "AI_Clips")
            clip_folder = Path(self._local_batch_template.output_folder) / clip_folder_name
            local_slug = self._local_model_slug(self._local_batch_template.local_wan_variant)
            clip_slug = {
                "LOCAL": local_slug,
                "GOOGLE_VIDS": "google_vids",
                "VEO": "veo",
            }.get(self._ai_batch_engine, "ai")
            clip = unique_output(
                str(clip_folder),
                f"{clip_slug}_{Path(source).stem}.mp4",
                f"{clip_slug}_{Path(source).stem}",
                ".mp4",
            )
            try:
                self.ai_progress.setValue(1)
                self.ai_status.setText(
                    f"AI {current}/{self._local_batch_total} • đang gửi {Path(source).name} sang {engine_label}…"
                )
                if self._ai_batch_engine == "LOCAL":
                    self.local_ai_worker.start(
                        image_path=source,
                        output_path=str(clip),
                        **self._local_options(self._local_batch_template),
                    )
                elif self._ai_batch_engine == "GOOGLE_VIDS":
                    project = self._local_batch_template
                    self.google_vids_worker.start(
                        image_path=source,
                        prompt=project.ai_prompt.strip(),
                        output_path=str(clip),
                        profile_dir=str(GOOGLE_VIDS_PROFILE_DIR),
                        show_browser=project.vids_show_browser,
                    )
                else:
                    project = self._local_batch_template
                    self.ai_worker.start(
                        api_key=self._ai_batch_api_key,
                        image_path=source,
                        prompt=project.ai_prompt.strip(),
                        output_path=str(clip),
                        model=project.ai_model,
                        aspect_ratio=project.ai_aspect_ratio,
                        duration=int(project.ai_duration),
                        resolution=project.ai_resolution,
                    )
                return
            except Exception as exc:
                self._local_batch_failures.append(f"{Path(source).name}: {exc}")
        if self._local_batch_mode:
            self._start_generated_ai_render_batch()

    def _start_next_batch(self) -> None:
        while self._batch_mode and self._batch_queue:
            source = self._batch_queue.pop(0)
            self._batch_current = source
            template = self._batch_template or VisualProject()
            project = template.copy_for_background(
                source,
                ai_video=self._batch_from_ai or source in self._ai_generated_backgrounds,
            )
            project.output_name = f"{Path(source).stem}_visual.mp4"
            current = self._batch_total - len(self._batch_queue)
            try:
                job = build_visual_job(project, self.settings)
                self.status.begin()
                self.status.info.setText(f"Đang render {current}/{self._batch_total}: {Path(source).name}")
                self.worker.start(job)
                return
            except Exception as exc:
                self._batch_failures.append(f"{Path(source).name}: {exc}")
        if self._batch_mode:
            self._finish_batch()

    def cancel_render(self) -> None:
        self._batch_queue.clear()
        self._batch_mode = False
        self._local_batch_queue.clear()
        self._local_batch_mode = False
        if self.local_ai_worker.running:
            self.local_ai_worker.cancel()
        if self.ai_worker.running:
            self.ai_worker.cancel()
        if self.google_vids_worker.busy:
            self.google_vids_worker.cancel()
        if self.worker.running:
            self.worker.cancel()
        else:
            self.status.stopped("Đã hủy render.")

    def _finished(self, output: str, _log: str) -> None:
        if self._local_batch_mode:
            self._local_batch_completed.append(output)
            if self._local_batch_queue:
                QTimer.singleShot(0, self._start_next_local_ai)
            else:
                self._finish_local_batch(output)
            return
        if self._batch_mode:
            self._batch_completed.append(output)
            if self._batch_queue:
                QTimer.singleShot(0, self._start_next_batch)
            else:
                self._finish_batch(output)
            return
        self.status.success(output)
        QMessageBox.information(self, "Render hoàn tất", f"Visual 60 giây đã được tạo:\n{output}")

    def _failed(self, message: str, log_path: str) -> None:
        if self._local_batch_mode:
            detail = f"{Path(self._local_batch_current).name}: {message}"
            if log_path:
                detail += f" (log: {log_path})"
            self._local_batch_failures.append(detail)
            if self._local_batch_queue:
                QTimer.singleShot(0, self._start_next_local_ai)
            else:
                self._finish_local_batch()
            return
        if self._batch_mode:
            detail = f"{Path(self._batch_current).name}: {message}"
            if log_path:
                detail += f" (log: {log_path})"
            self._batch_failures.append(detail)
            if self._batch_queue:
                QTimer.singleShot(0, self._start_next_batch)
            else:
                self._finish_batch()
            return
        self.status.stopped("Render thất bại")
        show_error(self, "Render thất bại", message, log_path)

    def _canceled(self) -> None:
        was_ai_batch = self._batch_from_ai
        self._batch_queue.clear()
        self._batch_mode = False
        self._batch_from_ai = False
        self._local_batch_queue.clear()
        self._local_batch_mode = False
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        self.status.stopped("Đã hủy render.")
        if was_ai_batch and self._ai_generated_backgrounds:
            self.ai_status.setText(
                f"Đã hủy render; vẫn giữ {len(self._ai_generated_backgrounds)} clip AI để render lại."
            )

    def _finish_local_batch(self, last_output: str = "") -> None:
        self._local_batch_mode = False
        self.ai_generate.setEnabled(True)
        self.ai_generate_batch.setEnabled(True)
        self.vids_login.setEnabled(True)
        self.vids_check.setEnabled(True)
        self.ai_cancel.setEnabled(False)
        success_count = len(self._local_batch_completed)
        failure_count = len(self._local_batch_failures)
        if last_output or self._local_batch_completed:
            self.status.success(last_output or self._local_batch_completed[-1])
        else:
            self.status.stopped("Không có video AI nào render thành công.")
        self.ai_progress.setValue(100 if success_count else 0)
        self.ai_status.setText(
            f"Đã xong hàng đợi {self._ai_batch_engine}: {success_count}/{self._local_batch_total} video."
        )
        clip_folder_name = {
            "LOCAL": "AI_Local_Clips",
            "GOOGLE_VIDS": "Google_Vids_Clips",
            "VEO": "AI_Veo_Clips",
        }.get(self._ai_batch_engine, "AI_Clips")
        message = (
            f"Đã hoàn tất: {success_count}/{self._local_batch_total} video thành công.\n"
            f"Clip AI trung gian được giữ trong thư mục "
            f"{clip_folder_name}."
        )
        if failure_count:
            preview = "\n".join(self._local_batch_failures[:8])
            if failure_count > 8:
                preview += f"\n... và {failure_count - 8} lỗi khác"
            message += f"\n\nCó {failure_count} file lỗi:\n{preview}"
        QMessageBox.information(self, "AI hàng loạt hoàn tất", message)

    def _finish_batch(self, last_output: str = "") -> None:
        from_ai = self._batch_from_ai
        self._batch_mode = False
        self._batch_from_ai = False
        success_count = len(self._batch_completed)
        failure_count = len(self._batch_failures)
        if last_output or self._batch_completed:
            self.status.success(last_output or self._batch_completed[-1])
        else:
            self.status.stopped("Không có video nào render thành công.")
        if from_ai:
            self.ai_generate.setEnabled(True)
            self.ai_generate_batch.setEnabled(True)
            self.vids_login.setEnabled(True)
            self.vids_check.setEnabled(True)
            self.ai_cancel.setEnabled(False)
            self.ai_progress.setValue(100 if self._ai_generated_backgrounds else 0)
            self.ai_status.setText(
                f"Đã tạo {len(self._ai_generated_backgrounds)}/{self._local_batch_total} clip AI và "
                f"render {success_count}/{self._batch_total} visual 60 giây."
            )
            message = (
                f"AI: {len(self._ai_generated_backgrounds)}/{self._local_batch_total} clip đã tạo.\n"
                f"Visual 60 giây: {success_count}/{self._batch_total} video đã render với đầy đủ bố cục preview."
            )
            if self._local_batch_failures:
                ai_preview = "\n".join(self._local_batch_failures[:8])
                message += f"\n\nCó {len(self._local_batch_failures)} ảnh tạo AI lỗi:\n{ai_preview}"
        else:
            message = f"Đã hoàn tất hàng đợi: {success_count}/{self._batch_total} video thành công."
        if failure_count:
            preview = "\n".join(self._batch_failures[:8])
            if failure_count > 8:
                preview += f"\n... và {failure_count - 8} lỗi khác"
            message += f"\n\nCó {failure_count} file lỗi:\n{preview}"
        self._refresh_ai_source_summary()
        QMessageBox.information(
            self,
            "AI + render hàng loạt hoàn tất" if from_ai else "Render hàng loạt hoàn tất",
            message,
        )


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
