from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QDoubleSpinBox, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from models.settings_model import AppSettings
from models.ai_audit import AiAuditLog
from utils.secret_store import delete_secret, resolve_secret, set_secret


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
        self.gemini_key = QLineEdit(resolve_secret("gemini_api_key", "GEMINI_API_KEY", settings.gemini_api_key))
        self.gemini_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemini_key.setPlaceholderText("Có thể dùng biến môi trường GEMINI_API_KEY")
        self.byteplus_key = QLineEdit(resolve_secret("byteplus_las_api_key", "BYTEPLUS_LAS_API_KEY"))
        self.byteplus_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.byteplus_key.setPlaceholderText("API key ModelArk/LAS chính thức hoặc BYTEPLUS_LAS_API_KEY")
        self.byteplus_url = QLineEdit(settings.byteplus_las_base_url)
        self.byteplus_url.setPlaceholderText("https://operator.las.ap-southeast-1.bytepluses.com/api/v1")
        self.ai_concurrency = QSpinBox()
        self.ai_concurrency.setRange(1, 8)
        self.ai_concurrency.setValue(max(1, int(settings.ai_batch_concurrency)))
        self.ai_retry = QSpinBox()
        self.ai_retry.setRange(0, 10)
        self.ai_retry.setValue(max(0, int(settings.ai_retry_limit)))
        self.ai_budget = QDoubleSpinBox()
        self.ai_budget.setRange(0, 1_000_000)
        self.ai_budget.setDecimals(2)
        self.ai_budget.setPrefix("$ ")
        self.ai_budget.setSpecialValueText("Không giới hạn")
        self.ai_budget.setValue(max(0.0, float(settings.ai_daily_budget)))
        self.ai_batch_budget = QDoubleSpinBox()
        self.ai_batch_budget.setRange(0, 1_000_000)
        self.ai_batch_budget.setDecimals(2)
        self.ai_batch_budget.setPrefix("$ ")
        self.ai_batch_budget.setSpecialValueText("Không giới hạn")
        self.ai_batch_budget.setValue(max(0.0, float(settings.ai_batch_budget)))
        self.ai_cloud_rate = QDoubleSpinBox()
        self.ai_cloud_rate.setRange(0, 10_000)
        self.ai_cloud_rate.setDecimals(6)
        self.ai_cloud_rate.setPrefix("$ ")
        self.ai_cloud_rate.setSuffix(" / giây video")
        self.ai_cloud_rate.setSpecialValueText("Chưa cấu hình")
        self.ai_cloud_rate.setValue(max(0.0, float(settings.ai_estimated_cloud_cost_per_second)))
        form.addRow("FFmpeg", self.ffmpeg)
        form.addRow("FFprobe", self.ffprobe)
        form.addRow("ComfyUI URL", self.comfy_url)
        form.addRow("Workflow API JSON (tùy chọn)", self.comfy_workflow)
        form.addRow("Thư mục AI Local", root_row)
        form.addRow("", self.local_auto_start)
        form.addRow("Gemini API key (Veo)", self.gemini_key)
        form.addRow("BytePlus LAS API key (Seedance)", self.byteplus_key)
        form.addRow("BytePlus LAS Base URL", self.byteplus_url)
        form.addRow("Luồng AI hàng loạt", self.ai_concurrency)
        form.addRow("Số lần retry lỗi tạm thời", self.ai_retry)
        form.addRow("Ngân sách AI/ngày", self.ai_budget)
        form.addRow("Ngân sách AI/batch", self.ai_batch_budget)
        form.addRow("Đơn giá cloud ước tính", self.ai_cloud_rate)
        layout.addLayout(form)
        note = QLabel(
            "Wan 2.2 Native kết nối ComfyUI localhost và không dùng token/credit; nên để trống "
            "workflow để dùng bản tích hợp. Gemini và BytePlus API key được lưu bằng Windows Credential Manager, "
            "không ghi vào settings.json. Seedance sử dụng API BytePlus chính thức, không dùng cookie hay tài khoản Dola. "
            "Đơn giá cloud là giá ước tính do bạn nhập theo hợp đồng nhà cung cấp để app chặn ngân sách trước khi gửi."
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
        previous_gemini = resolve_secret("gemini_api_key", "GEMINI_API_KEY", settings.gemini_api_key)
        previous_byteplus = resolve_secret("byteplus_las_api_key", "BYTEPLUS_LAS_API_KEY")
        previous_daily_budget = settings.ai_daily_budget
        previous_batch_budget = settings.ai_batch_budget
        previous_rate = settings.ai_estimated_cloud_cost_per_second
        settings.ffmpeg_path = self.ffmpeg.text().strip() or "ffmpeg"
        settings.ffprobe_path = self.ffprobe.text().strip() or "ffprobe"
        settings.comfyui_url = self.comfy_url.text().strip()
        settings.comfyui_workflow = self.comfy_workflow.text().strip()
        settings.local_ai_root = self.local_ai_root.text().strip()
        settings.local_ai_auto_start = self.local_auto_start.isChecked()
        self._save_secret("gemini_api_key", self.gemini_key.text().strip())
        self._save_secret("byteplus_las_api_key", self.byteplus_key.text().strip())
        settings.gemini_api_key = ""
        settings.byteplus_las_base_url = self.byteplus_url.text().strip().rstrip("/") or AppSettings().byteplus_las_base_url
        settings.ai_batch_concurrency = self.ai_concurrency.value()
        settings.ai_retry_limit = self.ai_retry.value()
        settings.ai_daily_budget = self.ai_budget.value()
        settings.ai_batch_budget = self.ai_batch_budget.value()
        settings.ai_estimated_cloud_cost_per_second = self.ai_cloud_rate.value()
        try:
            AiAuditLog().record(
                "settings.ai_updated",
                target_type="settings",
                target_id="ai",
                details={
                    "gemini_credential_changed": previous_gemini != self.gemini_key.text().strip(),
                    "byteplus_credential_changed": previous_byteplus != self.byteplus_key.text().strip(),
                    "daily_budget_from": previous_daily_budget,
                    "daily_budget_to": settings.ai_daily_budget,
                    "batch_budget_from": previous_batch_budget,
                    "batch_budget_to": settings.ai_batch_budget,
                    "estimated_rate_from": previous_rate,
                    "estimated_rate_to": settings.ai_estimated_cloud_cost_per_second,
                },
            )
        except OSError:
            pass

    @staticmethod
    def _save_secret(name: str, value: str) -> None:
        if value:
            if not set_secret(name, value):
                raise RuntimeError("Chỉ có thể lưu API key an toàn bằng Windows Credential Manager trên Windows.")
        else:
            delete_secret(name)
