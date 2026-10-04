from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai.batch_inputs import BatchPrompt, expand_batch_inputs, image_files, normalize_prompt, parse_prompt_file
from ai.job_manager import AiJobManager
from ai.providers.base import GenerationRequest
from ai.providers.registry import create_provider_registry
from models.ai_job import ACTIVE_STATUSES, TERMINAL_STATUSES, AiGenerationJob, JobStatus
from models.settings_model import AppSettings
from ui.common import FileField, IMAGE_FILTER, show_error
from utils.media import IMAGE_EXTENSIONS


class AiBatchPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.providers = create_provider_registry(settings)
        self.manager = AiJobManager(
            self.providers,
            concurrency=settings.ai_batch_concurrency,
            retry_limit=settings.ai_retry_limit,
            ffprobe_path=settings.ffprobe_path,
        )
        self._prompt_rows: list[BatchPrompt] = []
        self._loading_prompt = False
        self._build_ui()
        self.manager.start()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(900)
        self.refresh_timer.timeout.connect(self.refresh_jobs)
        self.refresh_timer.start()
        self.refresh_jobs()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("AI Video hàng loạt")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        notice = QLabel(
            "Seedance dùng BytePlus LAS API chính thức; Veo dùng Gemini API; AI Local dùng ComfyUI. "
            "Muse Web chạy tuần tự trên một tài khoản được chọn và dừng khi hết quota. "
            "Không sử dụng cookie, kho tài khoản hoặc xoay proxy để né quota."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        root.addWidget(notice)

        config = QGroupBox("Cấu hình tạo video")
        grid = QGridLayout(config)
        self.provider = QComboBox()
        for provider in self.providers.values():
            self.provider.addItem(provider.display_name, provider.provider_id)
        self.model = QComboBox()
        self.duration = QComboBox()
        self.aspect_ratio = QComboBox()
        self.resolution = QComboBox()
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 100)
        self.quantity.setValue(1)
        self.generate_audio = QCheckBox("Tạo âm thanh đồng bộ nếu model hỗ trợ")
        self.provider_health = QLabel()
        self.provider_health.setObjectName("muted")
        self.muse_account_label = QLabel("Tài khoản Muse")
        self.muse_account = QComboBox()
        self.refresh_muse_accounts = QPushButton("Làm mới")
        self.refresh_muse_accounts.clicked.connect(self._refresh_muse_accounts)
        self.batch_preview = QLabel("Dự kiến: 0 job")
        self.batch_preview.setObjectName("muted")
        grid.addWidget(QLabel("Provider"), 0, 0)
        grid.addWidget(self.provider, 0, 1)
        grid.addWidget(QLabel("Model"), 0, 2)
        grid.addWidget(self.model, 0, 3)
        grid.addWidget(QLabel("Thời lượng"), 1, 0)
        grid.addWidget(self.duration, 1, 1)
        grid.addWidget(QLabel("Tỉ lệ"), 1, 2)
        grid.addWidget(self.aspect_ratio, 1, 3)
        grid.addWidget(QLabel("Độ phân giải"), 2, 0)
        grid.addWidget(self.resolution, 2, 1)
        grid.addWidget(QLabel("Số video/đầu vào"), 2, 2)
        grid.addWidget(self.quantity, 2, 3)
        grid.addWidget(self.generate_audio, 3, 0, 1, 2)
        grid.addWidget(self.provider_health, 3, 2, 1, 2)
        grid.addWidget(self.muse_account_label, 4, 0)
        grid.addWidget(self.muse_account, 4, 1, 1, 2)
        grid.addWidget(self.refresh_muse_accounts, 4, 3)
        grid.addWidget(self.batch_preview, 5, 0, 1, 4)
        root.addWidget(config)

        inputs = QGroupBox("Prompt và ảnh đầu vào")
        input_layout = QGridLayout(inputs)
        self.prompt = QPlainTextEdit()
        self.prompt.setMaximumHeight(105)
        self.prompt.setPlaceholderText("Nhập một prompt dùng chung, hoặc nạp TXT/CSV…")
        self.prompt.textChanged.connect(self._prompt_edited)
        prompt_actions = QVBoxLayout()
        load_prompts = QPushButton("Nạp TXT/CSV")
        load_prompts.clicked.connect(self._load_prompt_file)
        normalize = QPushButton("Chuẩn hóa prompt")
        normalize.clicked.connect(self._normalize_prompt)
        self.prompt_summary = QLabel("1 prompt nhập trực tiếp")
        self.prompt_summary.setObjectName("muted")
        prompt_actions.addWidget(load_prompts)
        prompt_actions.addWidget(normalize)
        prompt_actions.addWidget(self.prompt_summary)
        prompt_actions.addStretch()
        input_layout.addWidget(QLabel("Prompt"), 0, 0)
        input_layout.addWidget(self.prompt, 1, 0)
        input_layout.addLayout(prompt_actions, 1, 1)

        self.images = QListWidget()
        self.images.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.images.setMaximumHeight(115)
        image_actions = QVBoxLayout()
        choose_images = QPushButton("Chọn ảnh")
        choose_images.clicked.connect(self._choose_images)
        choose_folder = QPushButton("Chọn thư mục")
        choose_folder.clicked.connect(self._choose_image_folder)
        remove_images = QPushButton("Bỏ ảnh đã chọn")
        remove_images.clicked.connect(self._remove_selected_images)
        self.recursive = QCheckBox("Gồm thư mục con")
        self.multi_reference = QCheckBox("Dùng toàn bộ ảnh làm reference cho mỗi job")
        self.asset_rights = QCheckBox("Tôi xác nhận có quyền sử dụng các ảnh/tài sản đã chọn")
        image_actions.addWidget(choose_images)
        image_actions.addWidget(choose_folder)
        image_actions.addWidget(remove_images)
        image_actions.addWidget(self.recursive)
        image_actions.addWidget(self.multi_reference)
        input_layout.addWidget(QLabel("Ảnh tham chiếu / khung đầu"), 2, 0)
        input_layout.addWidget(self.images, 3, 0)
        input_layout.addLayout(image_actions, 3, 1)
        input_layout.addWidget(self.asset_rights, 4, 0, 1, 2)
        root.addWidget(inputs)

        output_row = QHBoxLayout()
        self.output_folder = FileField("Thư mục lưu MP4", directory=True)
        self.output_folder.setText(self.settings.last_output_folder)
        output_row.addWidget(self.output_folder, 1)
        self.create_jobs = QPushButton("TẠO BATCH VÀ CHẠY")
        self.create_jobs.setObjectName("primary")
        self.create_jobs.clicked.connect(self._create_batch)
        output_row.addWidget(self.create_jobs)
        root.addLayout(output_row)

        filters = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.addItem("Tất cả trạng thái", "")
        for status in JobStatus:
            self.status_filter.addItem(status.value, status.value)
        self.status_filter.currentIndexChanged.connect(self.refresh_jobs)
        self.provider_filter = QComboBox()
        self.provider_filter.addItem("Tất cả provider", "")
        self.provider_filter.currentIndexChanged.connect(self.refresh_jobs)
        self.model_filter = QComboBox()
        self.model_filter.addItem("Tất cả model", "")
        self.model_filter.currentIndexChanged.connect(self.refresh_jobs)
        self.batch_filter = QComboBox()
        self.batch_filter.addItem("Tất cả batch", "")
        self.batch_filter.currentIndexChanged.connect(self.refresh_jobs)
        self.time_filter = QComboBox()
        self.time_filter.addItem("Mọi thời gian", 0)
        self.time_filter.addItem("Hôm nay", 1)
        self.time_filter.addItem("7 ngày", 7)
        self.time_filter.addItem("30 ngày", 30)
        self.time_filter.currentIndexChanged.connect(self.refresh_jobs)
        for widget in (self.status_filter, self.provider_filter, self.model_filter, self.batch_filter, self.time_filter):
            filters.addWidget(widget)
        filters.addStretch()
        root.addLayout(filters)

        toolbar = QHBoxLayout()
        cancel_selected = QPushButton("Hủy job chọn")
        cancel_selected.clicked.connect(self._cancel_selected)
        cancel_all = QPushButton("Dừng tất cả")
        cancel_all.clicked.connect(self.cancel_all)
        retry = QPushButton("Chạy lại lỗi")
        retry.clicked.connect(self._retry_selected)
        remove = QPushButton("Xóa bản ghi hoàn tất")
        remove.clicked.connect(self._remove_terminal)
        open_folder = QPushButton("Mở thư mục")
        open_folder.clicked.connect(self._open_selected_folder)
        open_file = QPushButton("Mở video")
        open_file.clicked.connect(self._open_selected_file)
        export = QPushButton("Xuất báo cáo")
        export.clicked.connect(self._export_report)
        toolbar.addStretch()
        for button in (cancel_selected, cancel_all, retry, remove, open_file, open_folder, export):
            toolbar.addWidget(button)
        root.addLayout(toolbar)

        columns = [
            "Job", "Trạng thái", "%", "Provider", "Model", "Ảnh", "Output",
            "Lần chạy", "Tạo lúc", "Cập nhật", "Thông tin",
        ]
        self.table = QTableWidget(0, len(columns))
        self.table.setHorizontalHeaderLabels(columns)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._show_job_details)
        root.addWidget(self.table, 1)
        self.summary = QLabel("Chưa có job.")
        self.summary.setObjectName("muted")
        root.addWidget(self.summary)

        self.provider.currentIndexChanged.connect(self._provider_changed)
        self.model.currentIndexChanged.connect(self._model_changed)
        self.duration.currentIndexChanged.connect(self._refresh_batch_preview)
        self.quantity.valueChanged.connect(self._refresh_batch_preview)
        self.multi_reference.toggled.connect(self._refresh_batch_preview)
        self._provider_changed()

    def reload_settings(self, settings: AppSettings) -> None:
        self.settings = settings
        if settings.ai_batch_concurrency != self.manager.concurrency and not self.manager.busy:
            previous = self.manager
            previous.shutdown(wait=True)
            self.manager = AiJobManager(
                self.providers,
                store=previous.store,
                concurrency=settings.ai_batch_concurrency,
                retry_limit=settings.ai_retry_limit,
                ffprobe_path=settings.ffprobe_path,
                audit_log=previous.audit_log,
            )
            self.manager.start()
        self.manager.ffprobe_path = settings.ffprobe_path
        self.manager.retry_limit = settings.ai_retry_limit
        local = self.providers.get("comfyui")
        if local:
            local.comfyui_url = settings.comfyui_url
            local.workflow_path = settings.comfyui_workflow
        seedance = self.providers.get("byteplus_seedance")
        if seedance:
            seedance.base_url = settings.byteplus_las_base_url.rstrip("/")
        muse = self.providers.get("muse_web")
        if muse:
            muse.start_url = settings.muse_start_url
        self._provider_changed()

    def _provider_changed(self) -> None:
        provider = self._selected_provider()
        muse_selected = provider.provider_id == "muse_web"
        self.muse_account_label.setVisible(muse_selected)
        self.muse_account.setVisible(muse_selected)
        self.refresh_muse_accounts.setVisible(muse_selected)
        if muse_selected:
            self._refresh_muse_accounts()
        current_model = self.model.currentData()
        self.model.blockSignals(True)
        self.model.clear()
        for capability in provider.capabilities():
            self.model.addItem(capability.display_name, capability.model_id)
        if current_model:
            index = self.model.findData(current_model)
            if index >= 0:
                self.model.setCurrentIndex(index)
        self.model.blockSignals(False)
        health = provider.health_check()
        self.provider_health.setText(("✓ " if health.available else "⚠ ") + health.message)
        self._model_changed()

    def _refresh_muse_accounts(self) -> None:
        current = str(self.muse_account.currentData() or "")
        self.muse_account.clear()
        self.muse_account.addItem("Chọn một tài khoản Muse đã đăng nhập", "")
        provider = self.providers.get("muse_web")
        if provider is not None:
            for account in provider.store.all():
                if account.status == "connected":
                    self.muse_account.addItem(account.email_label, account.account_id)
        index = self.muse_account.findData(current)
        self.muse_account.setCurrentIndex(index if index >= 0 else 0)

    def _model_changed(self) -> None:
        capability = self._selected_capability()
        if capability is None:
            return
        current_duration = self.duration.currentData()
        self.duration.clear()
        for value in capability.duration_choices():
            self.duration.addItem(f"{value} giây", value)
        preferred = current_duration if current_duration and capability.supports_duration(int(current_duration)) else 8
        index = self.duration.findData(preferred)
        self.duration.setCurrentIndex(index if index >= 0 else 0)
        self.aspect_ratio.clear()
        for value in capability.aspect_ratios:
            self.aspect_ratio.addItem(value, value)
        self.resolution.clear()
        for value in capability.resolutions:
            self.resolution.addItem(value, value)
        self.generate_audio.setEnabled(capability.supports_audio)
        self.generate_audio.setChecked(capability.supports_audio)
        self._refresh_batch_preview()

    def _selected_provider(self):
        return self.providers[str(self.provider.currentData())]

    def _selected_capability(self):
        provider = self._selected_provider()
        model_id = str(self.model.currentData() or "")
        return provider.capability(model_id) if model_id else None

    def _prompt_edited(self) -> None:
        if not self._loading_prompt and self._prompt_rows:
            self._prompt_rows.clear()
        self._refresh_prompt_summary()
        self._refresh_batch_preview()

    def _load_prompt_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Nạp danh sách prompt", "", "Prompt (*.txt *.csv)")
        if not path:
            return
        try:
            rows = parse_prompt_file(path)
        except Exception as exc:
            show_error(self, "Không thể đọc prompt", exc)
            return
        self._prompt_rows = rows
        self._loading_prompt = True
        self.prompt.setPlainText(rows[0].prompt if len(rows) == 1 else "")
        self._loading_prompt = False
        self._refresh_prompt_summary()
        self._refresh_batch_preview()

    def _refresh_prompt_summary(self) -> None:
        if self._prompt_rows:
            self.prompt_summary.setText(f"Đã nạp {len(self._prompt_rows)} prompt")
        else:
            self.prompt_summary.setText("1 prompt nhập trực tiếp" if self.prompt.toPlainText().strip() else "Chưa có prompt")

    def _normalize_prompt(self) -> None:
        if self._prompt_rows:
            normalized = [BatchPrompt(normalize_prompt(item.prompt), item.image_path) for item in self._prompt_rows]
            changed = sum(before.prompt != after.prompt for before, after in zip(self._prompt_rows, normalized))
            self._prompt_rows = normalized
            self._refresh_batch_preview()
            QMessageBox.information(self, "Chuẩn hóa prompt", f"Đã chuẩn hóa {changed}/{len(normalized)} prompt.")
            return
        original = self.prompt.toPlainText()
        suggested = normalize_prompt(original)
        if not suggested:
            return
        if suggested == original:
            QMessageBox.information(self, "Chuẩn hóa prompt", "Prompt đã sạch, không cần thay đổi.")
            return
        answer = QMessageBox.question(
            self,
            "Áp dụng prompt đã chuẩn hóa?",
            f"Bản gốc:\n{original}\n\nĐề xuất:\n{suggested}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.prompt.setPlainText(suggested)

    def _choose_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Chọn ảnh đầu vào", "", IMAGE_FILTER)
        self._add_images(paths)

    def _choose_image_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục ảnh", "")
        if not folder:
            return
        try:
            self._add_images(image_files(folder, self.recursive.isChecked()))
        except Exception as exc:
            show_error(self, "Không thể đọc thư mục ảnh", exc)

    def _add_images(self, paths: list[str]) -> None:
        existing = {self.images.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.images.count())}
        for value in paths:
            path = Path(value)
            resolved = str(path.resolve())
            if resolved in existing or not path.is_file() or path.suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
            self.images.addItem(resolved)
            self.images.item(self.images.count() - 1).setData(Qt.ItemDataRole.UserRole, resolved)
            existing.add(resolved)
        self._refresh_batch_preview()

    def _remove_selected_images(self) -> None:
        for item in self.images.selectedItems():
            self.images.takeItem(self.images.row(item))
        self._refresh_batch_preview()

    def _selected_images(self) -> list[str]:
        return [str(self.images.item(index).data(Qt.ItemDataRole.UserRole)) for index in range(self.images.count())]

    def _prompt_inputs(self) -> list[BatchPrompt]:
        if self._prompt_rows:
            return list(self._prompt_rows)
        prompt = normalize_prompt(self.prompt.toPlainText())
        return [BatchPrompt(prompt)] if prompt else []

    def _expanded_inputs(self) -> list[BatchPrompt]:
        prompts = self._prompt_inputs()
        images = self._selected_images()
        if self.multi_reference.isChecked() and images:
            if any(item.image_path for item in prompts):
                raise ValueError("Không thể kết hợp image trong CSV với chế độ nhiều ảnh reference.")
            return prompts
        return expand_batch_inputs(prompts, images)

    def _refresh_batch_preview(self) -> None:
        try:
            total = len(self._expanded_inputs()) * self.quantity.value()
        except ValueError as exc:
            self.batch_preview.setText(f"Dự kiến: cấu hình đầu vào chưa hợp lệ — {exc}")
            return
        provider_id = str(self.provider.currentData() or "")
        duration = int(self.duration.currentData() or 0)
        rate = max(0.0, float(self.settings.ai_estimated_cloud_cost_per_second))
        estimated = 0.0 if provider_id == "comfyui" else total * duration * rate
        suffix = f" • chi phí ước tính ${estimated:.2f}" if rate and provider_id != "comfyui" else ""
        self.batch_preview.setText(f"Dự kiến: {total} job{suffix}")

    def _create_batch(self) -> None:
        try:
            selected_images = self._selected_images()
            expanded = self._expanded_inputs()
            if (selected_images or any(item.image_path for item in expanded)) and not self.asset_rights.isChecked():
                raise ValueError("Hãy xác nhận quyền sử dụng các ảnh/tài sản đã chọn.")
            provider = self._selected_provider()
            capability = self._selected_capability()
            output_folder = self.output_folder.text()
            if not output_folder:
                raise ValueError("Hãy chọn thư mục lưu video.")
            destination = Path(output_folder).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            total = len(expanded) * self.quantity.value()
            if total <= 0:
                raise ValueError("Chưa có prompt hợp lệ để tạo job.")
            muse_account_id = ""
            if provider.provider_id == "muse_web":
                muse_account_id = str(self.muse_account.currentData() or "")
                if not muse_account_id:
                    raise ValueError("Hãy chọn một tài khoản Muse đã đăng nhập.")
                if total > 20:
                    raise ValueError("Muse Web giới hạn tối đa 20 job mỗi batch và không tự xoay tài khoản.")
            if not capability.supports_text and any(not item.image_path for item in expanded):
                raise ValueError(f"{capability.display_name} yêu cầu ảnh đầu vào.")
            health = provider.health_check()
            if not health.available:
                raise ValueError(health.message)
            duration = int(self.duration.currentData())
            rate = max(0.0, float(self.settings.ai_estimated_cloud_cost_per_second))
            estimated_cost = 0.0 if provider.provider_id == "comfyui" else total * duration * rate
            if provider.provider_id != "comfyui" and (self.settings.ai_daily_budget or self.settings.ai_batch_budget) and not rate:
                raise ValueError(
                    "Đã bật giới hạn ngân sách nhưng chưa cấu hình đơn giá cloud ước tính trong Cài đặt."
                )
            if self.settings.ai_batch_budget and estimated_cost > self.settings.ai_batch_budget:
                raise ValueError(
                    f"Chi phí batch ước tính ${estimated_cost:.2f} vượt giới hạn "
                    f"${self.settings.ai_batch_budget:.2f}."
                )
            spent_today = self.manager.estimated_cost_today()
            if self.settings.ai_daily_budget and spent_today + estimated_cost > self.settings.ai_daily_budget:
                raise ValueError(
                    f"Tổng ước tính hôm nay ${spent_today + estimated_cost:.2f} vượt ngân sách "
                    f"${self.settings.ai_daily_budget:.2f}."
                )
            if provider.provider_id != "comfyui" or total > 10:
                cost_text = f" Chi phí ước tính: ${estimated_cost:.2f}." if rate and provider.provider_id != "comfyui" else ""
                answer = QMessageBox.warning(
                    self,
                    "Xác nhận tạo AI",
                    f"Sắp tạo {total} job bằng {provider.display_name}.{cost_text} "
                    "Cloud API có thể tính phí thực tế khác ước tính. Tiếp tục?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            requests = []
            sequence = 0
            for item in expanded:
                for copy_index in range(self.quantity.value()):
                    sequence += 1
                    stem = Path(item.image_path).stem if item.image_path else "text"
                    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:48] or "input"
                    filename = f"{provider.provider_id}_{safe_stem}_{stamp}_{sequence:04d}.mp4"
                    requests.append(GenerationRequest(
                        provider_id=provider.provider_id,
                        model_id=capability.model_id,
                        prompt=item.prompt,
                        output_path=str(destination / filename),
                        source_images=(
                            list(selected_images)
                            if self.multi_reference.isChecked() and selected_images
                            else ([item.image_path] if item.image_path else [])
                        ),
                        duration=int(self.duration.currentData()),
                        aspect_ratio=str(self.aspect_ratio.currentData()),
                        resolution=str(self.resolution.currentData()),
                        generate_audio=self.generate_audio.isChecked(),
                        seed=-1,
                        options={
                            "estimated_cost_usd": 0.0 if provider.provider_id == "comfyui" else duration * rate,
                            **(
                                {"muse_account_id": muse_account_id, "muse_timeout_seconds": 15 * 60}
                                if provider.provider_id == "muse_web" else {}
                            ),
                        },
                    ))
            batch = self.manager.add_batch(requests, name=f"{capability.display_name} {stamp}")
            self.settings.last_output_folder = str(destination)
            self.settings_changed.emit()
            self.summary.setText(f"Đã tạo batch {batch.id[:8]} gồm {len(batch.job_ids)} job.")
            self.refresh_jobs()
        except Exception as exc:
            show_error(self, "Không thể tạo AI batch", exc)

    def _selected_job_ids(self) -> list[str]:
        result = []
        for index in sorted({item.row() for item in self.table.selectedItems()}):
            item = self.table.item(index, 0)
            if item:
                result.append(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        return [item for item in result if item]

    def _cancel_selected(self) -> None:
        self.manager.cancel(self._selected_job_ids())

    def _retry_selected(self) -> None:
        self.manager.retry(self._selected_job_ids())

    def _remove_terminal(self) -> None:
        selected = self._selected_job_ids()
        self.manager.remove_terminal(selected or None)
        self.refresh_jobs()

    def _open_selected_folder(self) -> None:
        selected = set(self._selected_job_ids())
        job = next((item for item in self.manager.snapshot() if item.id in selected), None)
        folder = Path(job.request.output_path).parent if job else Path(self.output_folder.text() or ".")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))

    def _open_selected_file(self) -> None:
        selected = set(self._selected_job_ids())
        job = next((item for item in self.manager.snapshot() if item.id in selected), None)
        if not job:
            return
        target = Path(job.request.output_path)
        if not target.is_file():
            QMessageBox.information(self, "Chưa có video", "Job này chưa có file video đầu ra hợp lệ.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.resolve())))

    def _show_job_details(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        job_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        job = next((value for value in self.manager.snapshot() if value.id == job_id), None)
        if not job:
            return
        details = {
            "job_id": job.id,
            "batch_id": job.batch_id,
            "provider_job_id": job.external_id,
            "status": job.status.value,
            "progress": job.progress,
            "provider": job.request.provider_id,
            "model": job.request.model_id,
            "prompt": job.request.prompt,
            "source_images": job.request.source_images,
            "duration": job.request.duration,
            "aspect_ratio": job.request.aspect_ratio,
            "resolution": job.request.resolution,
            "estimated_cost_usd": round(job.estimated_cost, 6),
            "attempts": f"{job.attempt_count}/{job.max_attempts}",
            "error_code": job.error_code,
            "error_message": job.error_message,
            "output_path": job.request.output_path,
            "output_checksum": job.output_checksum,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }
        dialog = QMessageBox(self)
        dialog.setWindowTitle(f"Chi tiết job {job.id[:8]}")
        dialog.setText("Request/response đã chuẩn hóa; không chứa API key hoặc cookie.")
        dialog.setDetailedText(json.dumps(details, ensure_ascii=False, indent=2))
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.exec()

    def _export_report(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Xuất báo cáo AI jobs",
            f"ai_jobs_{datetime.now():%Y%m%d_%H%M%S}.csv",
            "CSV (*.csv);;JSON (*.json)",
        )
        if not path:
            return
        jobs = self.manager.snapshot()
        rows = [{
            "job_id": job.id,
            "batch_id": job.batch_id,
            "status": job.status.value,
            "provider": job.request.provider_id,
            "model": job.request.model_id,
            "duration": job.request.duration,
            "aspect_ratio": job.request.aspect_ratio,
            "resolution": job.request.resolution,
            "attempts": job.attempt_count,
            "estimated_cost_usd": round(job.estimated_cost, 6),
            "output_path": job.request.output_path,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
        } for job in jobs]
        target = Path(path)
        try:
            if "JSON" in selected_filter or target.suffix.casefold() == ".json":
                if target.suffix.casefold() != ".json":
                    target = target.with_suffix(".json")
                target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                if target.suffix.casefold() != ".csv":
                    target = target.with_suffix(".csv")
                with target.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["job_id"])
                    writer.writeheader()
                    writer.writerows(rows)
            QMessageBox.information(self, "Xuất báo cáo", f"Đã lưu: {target}")
        except Exception as exc:
            show_error(self, "Không thể xuất báo cáo", exc)

    def refresh_jobs(self) -> None:
        selected_ids = set(self._selected_job_ids())
        all_jobs = self.manager.snapshot()
        self._sync_job_filters(all_jobs)
        status_filter = str(self.status_filter.currentData() or "")
        provider_filter = str(self.provider_filter.currentData() or "")
        model_filter = str(self.model_filter.currentData() or "")
        batch_filter = str(self.batch_filter.currentData() or "")
        days = int(self.time_filter.currentData() or 0)
        jobs = list(all_jobs)
        if status_filter:
            jobs = [item for item in jobs if item.status.value == status_filter]
        if provider_filter:
            jobs = [item for item in jobs if item.request.provider_id == provider_filter]
        if model_filter:
            jobs = [item for item in jobs if item.request.model_id == model_filter]
        if batch_filter:
            jobs = [item for item in jobs if item.batch_id == batch_filter]
        if days:
            threshold = datetime.now(timezone.utc) - timedelta(days=days)
            jobs = [item for item in jobs if self._created_after(item, threshold)]
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            source = Path(job.request.source_images[0]).name if job.request.source_images else "Text-to-video"
            provider = self.providers.get(job.request.provider_id)
            values = (
                job.id[:8],
                job.status.value,
                str(job.progress),
                provider.display_name if provider else job.request.provider_id,
                job.request.model_id,
                source,
                Path(job.request.output_path).name,
                f"{job.attempt_count}/{job.max_attempts}",
                job.created_at,
                job.updated_at,
                job.message or job.error_message,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, job.id)
                self.table.setItem(row, column, item)
            if job.id in selected_ids:
                self.table.selectRow(row)
        counts = {status: sum(item.status == status for item in all_jobs) for status in JobStatus}
        active = sum(counts[status] for status in ACTIVE_STATUSES)
        terminal = sum(counts[status] for status in TERMINAL_STATUSES)
        estimated_today = self.manager.estimated_cost_today()
        self.summary.setText(
            f"Tổng {len(all_jobs)} • đang chạy/chờ {active} • hoàn tất {counts[JobStatus.COMPLETED]} "
            f"• lỗi {counts[JobStatus.FAILED]} • bị chặn {counts[JobStatus.BLOCKED]} • kết thúc {terminal} "
            f"• chi phí hôm nay (ước tính) ${estimated_today:.2f}"
        )

    def _sync_job_filters(self, jobs: list[AiGenerationJob]) -> None:
        batches = {batch.id: batch.name or batch.id[:8] for batch in self.manager.batches()}
        choices = (
            (self.provider_filter, "Tất cả provider", {
                job.request.provider_id: (
                    self.providers[job.request.provider_id].display_name
                    if job.request.provider_id in self.providers else job.request.provider_id
                ) for job in jobs
            }),
            (self.model_filter, "Tất cả model", {job.request.model_id: job.request.model_id for job in jobs}),
            (self.batch_filter, "Tất cả batch", {
                job.batch_id: batches.get(job.batch_id, job.batch_id[:8]) for job in jobs
            }),
        )
        for combo, all_label, values in choices:
            current = str(combo.currentData() or "")
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(all_label, "")
            for value, label in sorted(values.items(), key=lambda item: item[1].casefold()):
                combo.addItem(label, value)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    @staticmethod
    def _created_after(job: AiGenerationJob, threshold: datetime) -> bool:
        try:
            return datetime.fromisoformat(job.created_at).astimezone(timezone.utc) >= threshold
        except (TypeError, ValueError):
            return False

    @property
    def busy(self) -> bool:
        return self.manager.busy

    def cancel_all(self) -> None:
        self.manager.cancel_all()

    def shutdown(self) -> None:
        self.refresh_timer.stop()
        self.manager.shutdown(wait=False)
