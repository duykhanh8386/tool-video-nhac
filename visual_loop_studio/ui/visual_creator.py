from __future__ import annotations

from dataclasses import asdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from models.settings_model import AppSettings
from models.visual_project import ElementLayout, VisualProject, default_element_layouts
from render.ffmpeg import build_visual_job
from render.render_worker import RenderWorker
from ui.color_filter_panel import ColorFilterPanel
from ui.common import AUDIO_FILTER, IMAGE_FILTER, MEDIA_FILTER, FileField, RenderStatus, show_error
from ui.effect_panel import EffectPanel
from ui.preview import CompositionPreview
from visual.layout import ELEMENT_LABELS, PRESETS, analyze_background, compose_layout, reset_layout
from visual.waveform import WAVEFORM_MODES


class VisualCreatorPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.worker = RenderWorker(self)
        self.elements = default_element_layouts()
        self.analysis = None
        self.layout_variant = 0
        self._updating_element_controls = False
        root = QVBoxLayout(self)
        heading = QLabel("Visual Creator — 60 Seconds")
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
        self.preview.setToolTip("Drag to move • drag the bottom-right handle to resize • Ctrl + mouse wheel to rotate")
        self.preview.set_layouts(self.elements)
        right_layout.addWidget(self.preview, 1)
        preview_controls = QHBoxLayout()
        for text, callback in (("Play", self.preview.play), ("Pause", self.preview.pause), ("Stop", self.preview.stop), ("Restart", self.preview.restart)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            preview_controls.addWidget(button)
        preview_controls.addStretch()
        auto = QPushButton("AUTO COMPOSE")
        auto.setObjectName("primary")
        auto.clicked.connect(lambda: self.auto_compose(False))
        another = QPushButton("TRY ANOTHER LAYOUT")
        another.clicked.connect(lambda: self.auto_compose(True))
        reset = QPushButton("RESET")
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
        self._connect_preview()
        self._refresh_element_combo("title")

    def _build_inputs(self) -> None:
        layout_group = QGroupBox("SMART AUTO LAYOUT")
        layout_box = QVBoxLayout(layout_group)
        top = QHBoxLayout()
        self.layout_mode = QComboBox()
        self.layout_mode.addItems(["AUTO", "TEMPLATE", "MANUAL"])
        self.layout_template = QComboBox()
        self.layout_template.addItems(PRESETS)
        self.layout_template.setCurrentText("Minimal")
        self.layout_template.activated.connect(lambda _index: self.layout_mode.setCurrentText("TEMPLATE"))
        top.addWidget(QLabel("Mode"))
        top.addWidget(self.layout_mode)
        top.addWidget(QLabel("Template"))
        top.addWidget(self.layout_template, 1)
        layout_box.addLayout(top)
        self.analysis_info = QLabel("Background chưa được phân tích.")
        self.analysis_info.setObjectName("muted")
        self.analysis_info.setWordWrap(True)
        layout_box.addWidget(self.analysis_info)

        selected_form = QFormLayout()
        self.element_choice = QComboBox()
        self.element_choice.currentIndexChanged.connect(self._element_selected_from_controls)
        self.element_lock = QCheckBox("Lock position")
        self.element_visible = QCheckBox("Visible")
        self.element_visible.setChecked(True)
        state_row = QHBoxLayout()
        state_row.addWidget(self.element_lock)
        state_row.addWidget(self.element_visible)
        self.element_anchor = QComboBox()
        self.element_anchor.addItems(["top_left", "top_center", "top_right", "center", "bottom_left", "bottom_center", "bottom_right"])
        selected_form.addRow("Selected element", self.element_choice)
        selected_form.addRow(state_row)
        selected_form.addRow("Anchor", self.element_anchor)
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
        controls = (("X", self.element_x), ("Y", self.element_y), ("Width", self.element_width), ("Height", self.element_height), ("Rotation", self.element_rotation), ("Opacity", self.element_opacity), ("Z-order", self.element_z))
        for index, (label, widget) in enumerate(controls):
            row, column = divmod(index, 2)
            coordinates.addWidget(QLabel(label), row, column * 2)
            coordinates.addWidget(widget, row, column * 2 + 1)
        layout_box.addLayout(coordinates)

        element_actions = QHBoxLayout()
        duplicate = QPushButton("Duplicate")
        delete_copy = QPushButton("Delete Copy")
        center_h = QPushButton("Center H")
        center_v = QPushButton("Center V")
        duplicate.clicked.connect(self.duplicate_element)
        delete_copy.clicked.connect(self.delete_element_copy)
        center_h.clicked.connect(lambda: self.preview.align_selected("horizontal"))
        center_v.clicked.connect(lambda: self.preview.align_selected("vertical"))
        for button in (duplicate, delete_copy, center_h, center_v):
            element_actions.addWidget(button)
        layout_box.addLayout(element_actions)

        snap_row = QHBoxLayout()
        self.guides = QCheckBox("Guides")
        self.guides.setChecked(True)
        self.snap_center = QCheckBox("Canvas center")
        self.snap_center.setChecked(True)
        self.snap_margin = QCheckBox("Safe margin")
        self.snap_margin.setChecked(True)
        self.snap_elements = QCheckBox("Other elements")
        self.snap_elements.setChecked(True)
        self.snap_grid = QCheckBox("Grid")
        for checkbox in (self.guides, self.snap_center, self.snap_margin, self.snap_elements, self.snap_grid):
            snap_row.addWidget(checkbox)
        layout_box.addWidget(QLabel("Snap options"))
        layout_box.addLayout(snap_row)
        self.form.addWidget(layout_group)

        text_group = QGroupBox("1. TEXT")
        text_form = QFormLayout(text_group)
        self.title = QLineEdit()
        self.subtitle = QLineEdit()
        self.artist = QLineEdit()
        self.custom_text = QLineEdit()
        self.playlist = QPlainTextEdit()
        self.playlist.setPlaceholderText("01. Track name\n02. Track name")
        self.playlist.setMaximumHeight(95)
        for label, widget in (("Title", self.title), ("Subtitle", self.subtitle), ("Artist", self.artist), ("Custom text", self.custom_text), ("Playlist", self.playlist)):
            text_form.addRow(label, widget)
        self.font_file = FileField("Custom font", "Fonts (*.ttf *.otf *.ttc);;All files (*.*)", optional=True)
        self.font_size = QSpinBox()
        self.font_size.setRange(0, 300)
        self.font_size.setSpecialValueText("Auto")
        self.text_color = QLineEdit("#FFFFFF")
        self.text_opacity = QSlider(Qt.Orientation.Horizontal)
        self.text_opacity.setRange(0, 100)
        self.text_opacity.setValue(100)
        self.stroke_width = QSpinBox()
        self.stroke_width.setRange(0, 12)
        self.stroke_width.setValue(2)
        self.text_shadow = QCheckBox("Enabled")
        self.text_shadow.setChecked(True)
        text_form.addRow(self.font_file)
        text_form.addRow("Base font size", self.font_size)
        text_form.addRow("Color", self.text_color)
        text_form.addRow("Opacity", self.text_opacity)
        text_form.addRow("Stroke width", self.stroke_width)
        text_form.addRow("Shadow", self.text_shadow)
        self.form.addWidget(text_group)

        background_group = QGroupBox("2. BACKGROUND")
        bg_layout = QVBoxLayout(background_group)
        self.background = FileField("Image or video", MEDIA_FILTER)
        self.animation = QComboBox()
        self.animation.addItems(["VEO STYLE GENTLE", "Slow zoom", "Slow pan", "Parallax", "Fog drift", "Nebula drift", "Particle drift", "Glow breathing", "STATIC"])
        bg_layout.addWidget(self.background)
        bg_layout.addWidget(QLabel("Motion preset"))
        bg_layout.addWidget(self.animation)
        self.form.addWidget(background_group)

        asset_group = QGroupBox("3–4. LOGO + ICON / ARTWORK")
        asset_layout = QVBoxLayout(asset_group)
        self.logo = FileField("Logo", IMAGE_FILTER, optional=True)
        self.artwork = FileField("Icon / Artwork", IMAGE_FILTER, optional=True)
        self.platform_icons = FileField("Platform icons", IMAGE_FILTER, optional=True)
        self.artwork_motion = QComboBox()
        self.artwork_motion.addItems(["STATIC", "FLOAT", "PULSE", "BREATH", "ROTATE", "VERY_SLOW_ROTATE", "BEAT_PULSE"])
        self.artwork_motion.setCurrentText("FLOAT")
        asset_layout.addWidget(self.logo)
        asset_layout.addWidget(self.artwork)
        asset_layout.addWidget(self.platform_icons)
        asset_layout.addWidget(QLabel("Artwork motion"))
        asset_layout.addWidget(self.artwork_motion)
        self.form.addWidget(asset_group)

        wave_group = QGroupBox("5. WAVE / EFFECT")
        wave_layout = QVBoxLayout(wave_group)
        self.audio = FileField("Reactive audio", AUDIO_FILTER, optional=True)
        self.waveform = QComboBox()
        self.waveform.addItems(WAVEFORM_MODES)
        self.waveform.setCurrentText("Smooth sine waveform")
        wave_layout.addWidget(self.audio)
        wave_layout.addWidget(self.waveform)
        self.form.addWidget(wave_group)

        effects_group = QGroupBox("FULL-FRAME EFFECTS")
        effects_layout = QVBoxLayout(effects_group)
        self.effects = EffectPanel()
        effects_layout.addWidget(self.effects)
        self.form.addWidget(effects_group)

        color_group = QGroupBox("COLOR FILTER + LUT")
        color_layout = QVBoxLayout(color_group)
        self.color = ColorFilterPanel()
        color_layout.addWidget(self.color)
        self.form.addWidget(color_group)

        output_group = QGroupBox("OUTPUT")
        output_layout = QVBoxLayout(output_group)
        self.output_folder = FileField("Output folder", directory=True)
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
        config_row.addWidget(QLabel("Resolution"))
        config_row.addWidget(self.resolution)
        config_row.addWidget(QLabel("FPS"))
        config_row.addWidget(self.fps)
        config_row.addWidget(self.encoder)
        render = QPushButton("Render 1 Minute")
        render.setObjectName("primary")
        render.clicked.connect(self.start_render)
        output_layout.addWidget(self.output_folder)
        output_layout.addWidget(QLabel("Custom output filename"))
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
        self.title.textChanged.connect(lambda value: self._set_preview_value("title", value))
        self.subtitle.textChanged.connect(lambda value: self._set_preview_value("subtitle", value))
        self.artist.textChanged.connect(lambda value: self._set_preview_value("artist", value))
        self.custom_text.textChanged.connect(lambda value: self._set_preview_value("custom_text", value))
        self.playlist.textChanged.connect(lambda: self._set_preview_value("playlist", self.playlist.toPlainText()))
        self.text_color.textChanged.connect(lambda value: self._set_preview_value("text_color", value))
        self.waveform.currentTextChanged.connect(lambda value: self._set_preview_value("waveform", value))
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
        self.analysis_info.setText("Background đã thay đổi; bấm AUTO COMPOSE để phân tích lại.")

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
            show_error(self, "Auto Compose", "Hãy chọn background trước.")
            return
        if not self.title.text():
            self.title.setText("YOUR TITLE")
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
                    "waveform": self.waveform.currentText() != "NONE",
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
            show_error(self, "Auto Compose failed", exc)

    def reset_composition(self) -> None:
        self.elements = reset_layout()
        self.layout_variant = 0
        self.preview.subject_bbox = None
        self.preview.set_layouts(self.elements)
        self._refresh_element_combo("title")
        self.analysis_info.setText("Layout đã reset. Các lock cũng được bỏ.")

    def duplicate_element(self) -> None:
        new_id = self.preview.duplicate_selected()
        if new_id:
            self._refresh_element_combo(new_id)

    def delete_element_copy(self) -> None:
        if not self.preview.delete_selected_copy():
            QMessageBox.information(self, "Delete Copy", "Element gốc không thể xóa; bạn có thể tắt Visible.")
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
            output_folder=self.output_folder.text(), output_name=self.output_name.text(), resolution=self.resolution.currentText(),
            fps=self.fps.value(), encoder=self.encoder.currentText(), animation=self.animation.currentText(),
            artwork_motion=self.artwork_motion.currentText(), waveform=self.waveform.currentText(),
            layout_mode=self.layout_mode.currentText(), layout_template=self.layout_template.currentText(), layout_variant=self.layout_variant,
            elements={name: ElementLayout(**asdict(layout)) for name, layout in self.elements.items()},
            color_filter=self.color.preset.currentText(), manual_color=self.color.values(), lut=self.color.lut.text(), effects=self.effects.values(),
        )

    def load(self, project: VisualProject) -> None:
        for widget, value in ((self.title, project.title), (self.subtitle, project.subtitle), (self.artist, project.artist), (self.custom_text, project.custom_text)):
            widget.setText(value)
        self.playlist.setPlainText(project.playlist)
        for widget, value in ((self.font_file, project.font_file), (self.background, project.background), (self.logo, project.logo), (self.artwork, project.artwork), (self.platform_icons, project.platform_icons), (self.audio, project.audio), (self.output_folder, project.output_folder), (self.color.lut, project.lut)):
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
        self.animation.setCurrentText(project.animation)
        self.artwork_motion.setCurrentText(project.artwork_motion)
        self.waveform.setCurrentText(project.waveform)
        self.layout_mode.setCurrentText(project.layout_mode)
        self.layout_template.setCurrentText(project.layout_template)
        self.layout_variant = project.layout_variant
        self.elements = {name: ElementLayout(**asdict(layout)) for name, layout in project.elements.items()}
        self.preview.set_layouts(self.elements)
        self._refresh_element_combo("title")
        self.color.preset.setCurrentText(project.color_filter)
        self.color.set_values(project.manual_color)
        self.effects.set_values(project.effects)

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
