from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from models.settings_model import AppSettings


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Cài đặt")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.ffmpeg = QLineEdit(settings.ffmpeg_path)
        self.ffprobe = QLineEdit(settings.ffprobe_path)
        self.comfy_url = QLineEdit(settings.comfyui_url)
        self.comfy_workflow = QLineEdit(settings.comfyui_workflow)
        self.comfy_url.setPlaceholderText("http://127.0.0.1:8188")
        self.comfy_workflow.setPlaceholderText("Để trống dùng workflow Wan 2.2 Native tích hợp sẵn")
        self.local_ai_root = QLineEdit(settings.local_ai_root)
        self.local_ai_root.setPlaceholderText("Được chọn tự động khi bấm Cài AI Local")
        root_row = QWidget()
        root_layout = QHBoxLayout(root_row)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.local_ai_root, 1)
        browse_root = QPushButton("Chọn")
        browse_root.clicked.connect(self._browse_local_root)
        root_layout.addWidget(browse_root)
        self.local_auto_start = QCheckBox("Tự chạy ComfyUI ẩn khi mở chương trình")
        self.local_auto_start.setChecked(settings.local_ai_auto_start)
        self.gemini_key = QLineEdit(settings.gemini_api_key)
        self.gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key.setPlaceholderText("Có thể dùng biến môi trường GEMINI_API_KEY")
        form.addRow("FFmpeg", self.ffmpeg)
        form.addRow("FFprobe", self.ffprobe)
        form.addRow("ComfyUI URL", self.comfy_url)
        form.addRow("Workflow API JSON (tùy chọn)", self.comfy_workflow)
        form.addRow("Thư mục AI Local", root_row)
        form.addRow("", self.local_auto_start)
        form.addRow("Gemini API key (Veo)", self.gemini_key)
        layout.addLayout(form)
        note = QLabel(
            "Wan 2.2 Native kết nối ComfyUI localhost và không dùng token/credit; nên để trống "
            "workflow để dùng bản tích hợp. Gemini API key chỉ dùng cho Veo và chỉ được lưu cục bộ."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_local_root(self) -> None:
        value = self.local_ai_root.text().strip()
        start = value if value and Path(value).exists() else ""
        selected = QFileDialog.getExistingDirectory(self, "Chọn thư mục AI Local", start)
        if selected:
            self.local_ai_root.setText(selected)

    def apply(self, settings: AppSettings) -> None:
        settings.ffmpeg_path = self.ffmpeg.text().strip() or "ffmpeg"
        settings.ffprobe_path = self.ffprobe.text().strip() or "ffprobe"
        settings.comfyui_url = self.comfy_url.text().strip()
        settings.comfyui_workflow = self.comfy_workflow.text().strip()
        settings.local_ai_root = self.local_ai_root.text().strip()
        settings.local_ai_auto_start = self.local_auto_start.isChecked()
        settings.gemini_api_key = self.gemini_key.text().strip()
