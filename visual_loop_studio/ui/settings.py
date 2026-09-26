from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QVBoxLayout

from models.settings_model import AppSettings


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.ffmpeg = QLineEdit(settings.ffmpeg_path)
        self.ffprobe = QLineEdit(settings.ffprobe_path)
        self.comfy_url = QLineEdit(settings.comfyui_url)
        self.comfy_workflow = QLineEdit(settings.comfyui_workflow)
        form.addRow("FFmpeg", self.ffmpeg)
        form.addRow("FFprobe", self.ffprobe)
        form.addRow("ComfyUI URL", self.comfy_url)
        form.addRow("ComfyUI workflow", self.comfy_workflow)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self, settings: AppSettings) -> None:
        settings.ffmpeg_path = self.ffmpeg.text().strip() or "ffmpeg"
        settings.ffprobe_path = self.ffprobe.text().strip() or "ffprobe"
        settings.comfyui_url = self.comfy_url.text().strip()
        settings.comfyui_workflow = self.comfy_workflow.text().strip()
