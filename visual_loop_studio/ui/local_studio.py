from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ai.comfyui import (
    LOCAL_MODEL_HUNYUAN,
    LOCAL_MODEL_LTX,
    WAN_VARIANT_DMD,
    WAN_VARIANT_QUALITY,
    check_comfyui,
    local_model_label,
)
from ai.local_runtime import LocalRuntimeManager, server_ready
from auth.local_generation import LocalVideoBatchRunnerThread
from models.settings_model import AppSettings


class LocalStudioPage(QWidget):
    """Tab riêng biệt chuyên dụng cho Farm Video AI Local (Wan 2.2 / LTX / Hunyuan qua ComfyUI).
    Không cần đăng nhập Google, không token/credit, render trực tiếp trên GPU máy tính.
    """

    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.runner_thread: LocalVideoBatchRunnerThread | None = None
        self.runtime_manager = LocalRuntimeManager()

        self._build_ui()
        self._check_comfyui_status(silent=True)

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(12)
        scroll.setWidget(container)
        outer_layout.addWidget(scroll)

        # Header
        title = QLabel("AI Video Local Studio (Wan / LTX / Hunyuan)")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        desc = QLabel(
            "Tạo video hàng loạt từ danh sách prompt chạy trực tiếp trên GPU máy tính thông qua ComfyUI. "
            "Hoàn toàn miễn phí, không tốn credit, không cần đăng nhập tài khoản Google hay Cookies. "
            "Tối ưu hóa đặc biệt với Wan 2.2 DMD (4 bước) kết hợp 832×480 để render nhanh nhất có thể với video dài 5 giây."
        )
        desc.setWordWrap(True)
        desc.setObjectName("notice")
        root.addWidget(desc)

        # =========================================================================
        # 1. CẤU HÌNH COMFYUI & TỐI ƯU HÓA MODEL AI
        # =========================================================================
        comfy_box = QGroupBox("⚡ Cấu Hình ComfyUI & Tối Ưu Tốc Độ Render")
        comfy_grid = QGridLayout(comfy_box)
        comfy_grid.setSpacing(10)

        # Hàng 0: Địa chỉ ComfyUI và kiểm tra
        comfy_grid.addWidget(QLabel("Địa chỉ ComfyUI:"), 0, 0)
        self.url_edit = QLineEdit(getattr(self.settings, "local_farm_comfyui_url", "http://127.0.0.1:8188") or "http://127.0.0.1:8188")
        self.url_edit.setMinimumHeight(32)
        self.url_edit.editingFinished.connect(self._save_settings)
        comfy_grid.addWidget(self.url_edit, 0, 1)

        self.btn_check_comfy = QPushButton("✔ Kiểm tra ComfyUI")
        self.btn_check_comfy.setMinimumHeight(32)
        self.btn_check_comfy.clicked.connect(lambda: self._check_comfyui_status(silent=False))
        comfy_grid.addWidget(self.btn_check_comfy, 0, 2)

        self.btn_start_comfy = QPushButton("🚀 Khởi động AI Local")
        self.btn_start_comfy.setMinimumHeight(32)
        self.btn_start_comfy.setStyleSheet("background: #0284c7; color: #ffffff; font-weight: 600;")
        self.btn_start_comfy.clicked.connect(self._start_comfyui_server)
        comfy_grid.addWidget(self.btn_start_comfy, 0, 3)

        self.status_badge = QLabel("⚪ Đang kiểm tra ComfyUI...")
        self.status_badge.setStyleSheet("font-weight: 600; color: #0284c7; padding: 2px 6px;")
        comfy_grid.addWidget(self.status_badge, 0, 4)

        # Hàng 1: Model AI Local
        comfy_grid.addWidget(QLabel("Model AI Local:"), 1, 0)
        self.model_combo = QComboBox()
        self.model_combo.setMinimumHeight(32)
        self.model_combo.addItem("Wan 2.2 5B DMD — 4 bước (Khuyên dùng: Siêu nhanh, tối ưu nhất)", WAN_VARIANT_DMD)
        self.model_combo.addItem("LTX-Video 2B Distilled — 8 bước (Nhanh)", LOCAL_MODEL_LTX)
        self.model_combo.addItem("Wan 2.2 5B Chất lượng — 20 bước", WAN_VARIANT_QUALITY)
        self.model_combo.addItem("HunyuanVideo 1.5 480p — 8 bước", LOCAL_MODEL_HUNYUAN)
        saved_model = getattr(self.settings, "local_farm_model", WAN_VARIANT_DMD) or WAN_VARIANT_DMD
        idx = self.model_combo.findData(saved_model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.model_combo.currentIndexChanged.connect(self._save_settings)
        comfy_grid.addWidget(self.model_combo, 1, 1, 1, 2)

        # Hàng 1: Độ phân giải
        comfy_grid.addWidget(QLabel("Độ phân giải:"), 1, 3)
        self.resolution_combo = QComboBox()
        self.resolution_combo.setMinimumHeight(32)
        self.resolution_combo.addItem("Nhanh 832×480 (Tối ưu tốc độ cao nhất)", "832x480")
        self.resolution_combo.addItem("720p 1280×704 (Chất lượng sắc nét)", "1280x704")
        saved_res = getattr(self.settings, "local_farm_resolution", "832x480") or "832x480"
        r_idx = self.resolution_combo.findData(saved_res)
        if r_idx >= 0:
            self.resolution_combo.setCurrentIndex(r_idx)
        self.resolution_combo.currentIndexChanged.connect(self._save_settings)
        comfy_grid.addWidget(self.resolution_combo, 1, 4)

        # Hàng 2: Tỉ lệ khung hình và thời lượng
        comfy_grid.addWidget(QLabel("Tỉ lệ khung hình:"), 2, 0)
        self.ratio_combo = QComboBox()
        self.ratio_combo.setMinimumHeight(32)
        self.ratio_combo.addItems(["16:9", "9:16"])
        saved_ratio = getattr(self.settings, "local_farm_aspect_ratio", "16:9") or "16:9"
        ratio_idx = self.ratio_combo.findText(saved_ratio)
        if ratio_idx >= 0:
            self.ratio_combo.setCurrentIndex(ratio_idx)
        self.ratio_combo.currentIndexChanged.connect(self._save_settings)
        comfy_grid.addWidget(self.ratio_combo, 2, 1)

        comfy_grid.addWidget(QLabel("Thời lượng video:"), 2, 2)
        self.duration_combo = QComboBox()
        self.duration_combo.setMinimumHeight(32)
        self.duration_combo.addItem("5 giây (121 frames @ 24fps) — Chuẩn tối ưu", 5)
        self.duration_combo.addItem("3.4 giây (81 frames)", 3)
        self.duration_combo.addItem("2 giây (49 frames)", 2)
        saved_dur = int(getattr(self.settings, "local_farm_duration", 5) or 5)
        d_idx = self.duration_combo.findData(saved_dur)
        if d_idx >= 0:
            self.duration_combo.setCurrentIndex(d_idx)
        self.duration_combo.currentIndexChanged.connect(self._save_settings)
        comfy_grid.addWidget(self.duration_combo, 2, 3, 1, 2)

        root.addWidget(comfy_box)

        # =========================================================================
        # 2. ẢNH NGUỒN AI (TÙY CHỌN)
        # =========================================================================
        img_box = QGroupBox("🖼 Ảnh Nền Nguồn (Tùy Chọn)")
        img_layout = QHBoxLayout(img_box)
        img_layout.setSpacing(10)

        self.img_mode_combo = QComboBox()
        self.img_mode_combo.setMinimumHeight(32)
        self.img_mode_combo.addItem("Tạo thuần từ Text Prompt (Tự động sinh canvas trung tính chuẩn)", "prompt_only")
        self.img_mode_combo.addItem("Dùng 1 ảnh làm nền chung cho tất cả prompt", "single_image")
        self.img_mode_combo.currentIndexChanged.connect(self._on_img_mode_changed)
        img_layout.addWidget(self.img_mode_combo)

        self.img_path_edit = QLineEdit()
        self.img_path_edit.setPlaceholderText("Đường dẫn file ảnh nền (.png, .jpg, .webp)...")
        self.img_path_edit.setMinimumHeight(32)
        self.img_path_edit.setEnabled(False)
        img_layout.addWidget(self.img_path_edit, 1)

        self.btn_choose_image = QPushButton("📂 Chọn ảnh")
        self.btn_choose_image.setMinimumHeight(32)
        self.btn_choose_image.setEnabled(False)
        self.btn_choose_image.clicked.connect(self._choose_single_image)
        img_layout.addWidget(self.btn_choose_image)

        root.addWidget(img_box)

        # =========================================================================
        # 3. DANH SÁCH PROMPT
        # =========================================================================
        prompt_box = QGroupBox("📝 Danh Sách Prompt (Mỗi dòng 1 prompt)")
        prompt_vbox = QVBoxLayout(prompt_box)
        prompt_vbox.setSpacing(8)

        p_toolbar = QHBoxLayout()
        self.btn_import_txt = QPushButton("📁 NHẬP FILE .TXT")
        self.btn_import_txt.setMinimumHeight(34)
        self.btn_import_txt.setStyleSheet("background: #1d4ed8; color: #ffffff; font-weight: 700; padding: 4px 14px;")
        self.btn_import_txt.clicked.connect(self._import_txt_file)
        p_toolbar.addWidget(self.btn_import_txt)

        self.prompt_count_label = QLabel("📊 Tổng số: 0 prompt")
        self.prompt_count_label.setStyleSheet("font-weight: 700; color: #1e293b; background: #f1f5f9; padding: 4px 8px; border-radius: 4px;")
        p_toolbar.addWidget(self.prompt_count_label)

        p_toolbar.addStretch()

        self.btn_clear_prompts = QPushButton("🗑 Xóa danh sách")
        self.btn_clear_prompts.setMinimumHeight(34)
        self.btn_clear_prompts.clicked.connect(self._clear_prompts)
        p_toolbar.addWidget(self.btn_clear_prompts)
        prompt_vbox.addLayout(p_toolbar)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Nhập hoặc dán các prompt vào đây, mỗi dòng là một prompt riêng biệt...\n"
            "Ví dụ:\n"
            "A breathtaking drone shot of a lush Japanese garden in Kyoto during autumn, vibrant red maple leaves falling gently into a koi pond, 4k cinematic\n"
            "Cinematic low-angle camera move through ancient vermilion torii gates under gentle snowfall, soft winter light, hyperrealistic\n"
            "Majestic misty mountain waterfall in early dawn, slow camera zoom, photorealistic water ripples and atmospheric fog"
        )
        self.prompt_edit.setMinimumHeight(150)
        self.prompt_edit.textChanged.connect(self._update_prompt_count)
        prompt_vbox.addWidget(self.prompt_edit)

        root.addWidget(prompt_box)

        # =========================================================================
        # 4. THƯ MỤC LƯU VIDEO OUTPUT
        # =========================================================================
        out_box = QGroupBox("📁 Thư Mục Xuất Video Hoàn Thiện")
        out_layout = QHBoxLayout(out_box)
        out_layout.setSpacing(8)

        default_out = (
            getattr(self.settings, "local_farm_last_output_folder", "")
            or getattr(self.settings, "last_output_folder", "")
            or str(Path.home() / "Videos")
        )
        self.output_edit = QLineEdit(default_out)
        self.output_edit.setMinimumHeight(34)
        self.output_edit.editingFinished.connect(self._save_settings)
        out_layout.addWidget(self.output_edit, 1)

        btn_choose_out = QPushButton("📂 Chọn thư mục lưu")
        btn_choose_out.setMinimumHeight(34)
        btn_choose_out.clicked.connect(self._choose_output_folder)
        out_layout.addWidget(btn_choose_out)

        root.addWidget(out_box)

        # =========================================================================
        # 5. ĐIỀU KHIỂN & TIẾN ĐỘ CHẠY BATCH
        # =========================================================================
        action_box = QGroupBox("🎬 Bảng Điều Khiển Tác Vụ Farm Video")
        action_vbox = QVBoxLayout(action_box)
        action_vbox.setSpacing(10)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_start = QPushButton("▶ BẮT ĐẦU FARM VIDEO AI LOCAL")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setStyleSheet(
            "QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #8b5cf6, stop:1 #6366f1); "
            "color: #ffffff; font-size: 15px; font-weight: 800; border-radius: 8px; padding: 0 24px; } "
            "QPushButton:hover { background: #6366f1; }"
        )
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start, 2)

        self.btn_pause = QPushButton("⏸ Tạm dừng")
        self.btn_pause.setMinimumHeight(44)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        btn_row.addWidget(self.btn_pause, 1)

        self.btn_stop = QPushButton("⏹ Dừng lại")
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #ef4444; color: #b91c1c; font-weight: 700; border-radius: 8px; } "
            "QPushButton:hover { background: #fecaca; }"
        )
        self.btn_stop.clicked.connect(self._stop_batch)
        btn_row.addWidget(self.btn_stop, 1)

        action_vbox.addLayout(btn_row)

        # Badge thống kê
        stats_layout = QHBoxLayout()
        self.stat_total = QLabel("Tổng: 0")
        self.stat_done = QLabel("✔ Thành công: 0")
        self.stat_failed = QLabel("✖ Lỗi: 0")
        self.stat_rem = QLabel("⏳ Còn lại: 0")

        for lbl, bg, fg in (
            (self.stat_total, "#f1f5f9", "#334155"),
            (self.stat_done, "#dcfce7", "#15803d"),
            (self.stat_failed, "#fee2e2", "#b91c1c"),
            (self.stat_rem, "#fef3c7", "#b45309"),
        ):
            lbl.setStyleSheet(f"font-weight: 700; padding: 6px 12px; border-radius: 6px; background: {bg}; color: {fg}; font-size: 13px;")
            stats_layout.addWidget(lbl)
        stats_layout.addStretch()
        action_vbox.addLayout(stats_layout)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumHeight(22)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; font-weight: bold; } "
            "QProgressBar::chunk { background: #6366f1; border-radius: 5px; }"
        )
        action_vbox.addWidget(self.progress_bar)

        self.status_label = QLabel("Sẵn sàng.")
        self.status_label.setStyleSheet("color: #64748b; font-weight: 500;")
        action_vbox.addWidget(self.status_label)

        root.addWidget(action_box)

        # =========================================================================
        # 6. LOG CONSOLE THỜI GIAN THỰC
        # =========================================================================
        log_box = QGroupBox("📜 Nhật Ký Hoạt Động (Logs)")
        log_vbox = QVBoxLayout(log_box)
        log_vbox.setSpacing(6)

        log_tb = QHBoxLayout()
        log_tb.addStretch()
        btn_copy_log = QPushButton("📋 Copy Log")
        btn_copy_log.clicked.connect(self._copy_log)
        log_tb.addWidget(btn_copy_log)
        btn_clear_log = QPushButton("🗑 Xóa Log")
        btn_clear_log.clicked.connect(self._clear_log)
        log_tb.addWidget(btn_clear_log)
        log_vbox.addLayout(log_tb)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(170)
        self.log_view.setStyleSheet(
            "background: #0f172a; color: #e2e8f0; font-family: Consolas, monospace; font-size: 12px; border-radius: 6px; padding: 6px;"
        )
        log_vbox.addWidget(self.log_view)

        root.addWidget(log_box)

    # -------------------------------------------------------------------------
    # HÀM XỬ LÝ LOG & THÔNG BÁO
    # -------------------------------------------------------------------------
    def _append_log(self, text: str) -> None:
        t_str = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{t_str}] {text}")
        self.log_view.ensureCursorVisible()

    def _clear_log(self) -> None:
        self.log_view.clear()

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_view.toPlainText())
        QMessageBox.information(self, "Thông báo", "Đã copy toàn bộ log vào Clipboard!")

    def _save_settings(self) -> None:
        setattr(self.settings, "local_farm_comfyui_url", self.url_edit.text().strip())
        setattr(self.settings, "local_farm_model", self.model_combo.currentData())
        setattr(self.settings, "local_farm_resolution", self.resolution_combo.currentData())
        setattr(self.settings, "local_farm_aspect_ratio", self.ratio_combo.currentText())
        setattr(self.settings, "local_farm_duration", self.duration_combo.currentData())
        setattr(self.settings, "local_farm_last_output_folder", self.output_edit.text().strip())
        self.settings_changed.emit()

    # -------------------------------------------------------------------------
    # HÀM KIỂM TRA & KHỞI CHẠY COMFYUI
    # -------------------------------------------------------------------------
    def _check_comfyui_status(self, silent: bool = False) -> None:
        url = self.url_edit.text().strip()
        model_val = self.model_combo.currentData() or WAN_VARIANT_DMD

        def check_task():
            try:
                if not server_ready(url):
                    return False, f"Không thể kết nối ComfyUI tại {url}"
                stats = check_comfyui(url, require_default_models=False, wan_variant=model_val)
                devices = stats.get("devices") or []
                dev_name = "GPU/CPU"
                if devices and isinstance(devices, list):
                    d = devices[0]
                    dev_name = str(d.get("name") or d.get("type") or "GPU")
                    vram = d.get("vram_total")
                    if isinstance(vram, (int, float)):
                        dev_name += f" ({vram / 1024**3:.1f} GB)"
                return True, f"🟢 ComfyUI sẵn sàng ({dev_name})"
            except Exception as e:
                return False, f"🔴 Lỗi: {e}"

        def done_callback(ok: bool, msg: str):
            if ok:
                self.status_badge.setText(msg)
                self.status_badge.setStyleSheet("font-weight: 700; color: #16a34a; padding: 2px 6px;")
                if not silent:
                    self._append_log(f"✔ {msg}")
            else:
                self.status_badge.setText("🔴 Chưa kết nối")
                self.status_badge.setStyleSheet("font-weight: 700; color: #dc2626; padding: 2px 6px;")
                if not silent:
                    self._append_log(f"❌ {msg}")

        def runner():
            ok, msg = check_task()
            from PySide6.QtCore import QMetaObject, Q_ARG
            # Chuyển về UI thread
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: done_callback(ok, msg))

        threading.Thread(target=runner, daemon=True).start()

    def _start_comfyui_server(self) -> None:
        url = self.url_edit.text().strip()
        model_val = self.model_combo.currentData() or WAN_VARIANT_DMD
        self._append_log("⚙ Đang kiểm tra và khởi động ComfyUI Local Server...")
        self.status_badge.setText("🟡 Đang khởi động...")
        self.status_badge.setStyleSheet("font-weight: 700; color: #d97706; padding: 2px 6px;")

        def runner():
            try:
                self.runtime_manager.ensure_running(
                    "",
                    url,
                    progress=lambda v, m: self._append_log(f"⚙ ComfyUI [{v}%]: {m}"),
                    wan_variant=model_val,
                )
                self._append_log("🟢 ComfyUI Server đã sẵn sàng phục vụ!")
                self._check_comfyui_status(silent=True)
            except Exception as e:
                self._append_log(f"❌ Lỗi khởi động ComfyUI: {e}")
                self._check_comfyui_status(silent=True)

        threading.Thread(target=runner, daemon=True).start()

    # -------------------------------------------------------------------------
    # HÀM CHỌN ẢNH NGUỒN TÙY CHỌN
    # -------------------------------------------------------------------------
    def _on_img_mode_changed(self) -> None:
        is_single = self.img_mode_combo.currentData() == "single_image"
        self.img_path_edit.setEnabled(is_single)
        self.btn_choose_image.setEnabled(is_single)

    def _choose_single_image(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn ảnh nền nguồn AI", "", "Ảnh (*.png *.jpg *.jpeg *.webp)"
        )
        if file_path:
            self.img_path_edit.setText(file_path)

    # -------------------------------------------------------------------------
    # HÀM QUẢN LÝ PROMPT
    # -------------------------------------------------------------------------
    def _import_txt_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "Chọn file prompt .txt", "", "Text Files (*.txt);;All Files (*)")
        if not file_path:
            return

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
            if not lines:
                QMessageBox.warning(self, "Thông báo", "File .txt không chứa prompt nào hợp lệ.")
                return

            self.prompt_edit.setPlainText("\n".join(lines))
            self._update_prompt_count()
            self._append_log(f"✔ Đã nạp {len(lines)} prompt từ file {Path(file_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Lỗi", f"Không thể đọc file: {e}")

    def _clear_prompts(self) -> None:
        if not self.prompt_edit.toPlainText().strip():
            return
        if QMessageBox.question(self, "Xác nhận", "Bạn có chắc muốn xóa toàn bộ danh sách prompt?") == QMessageBox.Yes:
            self.prompt_edit.clear()
            self._update_prompt_count()

    def _update_prompt_count(self) -> None:
        prompts = self._get_prompts()
        self.prompt_count_label.setText(f"📊 Tổng số: {len(prompts)} prompt")
        self.stat_total.setText(f"Tổng: {len(prompts)}")
        self.stat_rem.setText(f"⏳ Còn lại: {len(prompts)}")

    def _get_prompts(self) -> list[str]:
        raw = self.prompt_edit.toPlainText()
        return [p.strip() for p in raw.splitlines() if p.strip() and not p.strip().startswith("#")]

    def _choose_output_folder(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video hoàn thiện")
        if dir_path:
            self.output_edit.setText(dir_path)
            self._save_settings()

    # -------------------------------------------------------------------------
    # HÀM BẮT ĐẦU / DỪNG BATCH FARM VIDEO
    # -------------------------------------------------------------------------
    def _start_batch(self) -> None:
        prompts = self._get_prompts()
        if not prompts:
            QMessageBox.warning(self, "Thiếu dữ liệu", "Vui lòng nhập hoặc nạp danh sách prompt trước khi chạy!")
            return

        out_dir = Path(self.output_edit.text().strip()).resolve()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "Lỗi thư mục", f"Không thể tạo thư mục lưu video: {e}")
            return

        source_img = ""
        if self.img_mode_combo.currentData() == "single_image":
            source_img = self.img_path_edit.text().strip()
            if not source_img or not Path(source_img).is_file():
                QMessageBox.warning(self, "Thiếu ảnh nguồn", "Vui lòng chọn file ảnh nguồn hợp lệ hoặc chuyển sang chế độ tạo từ Text Prompt!")
                return

        comfy_url = self.url_edit.text().strip()
        model_val = self.model_combo.currentData() or WAN_VARIANT_DMD
        res_val = self.resolution_combo.currentData() or "832x480"
        ratio_val = self.ratio_combo.currentText()
        dur_val = int(self.duration_combo.currentData() or 5)

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("⏸ Tạm dừng")
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)

        self._save_settings()

        self.runner_thread = LocalVideoBatchRunnerThread(
            prompts=prompts,
            output_dir=out_dir,
            comfyui_url=comfy_url,
            model_variant=model_val,
            resolution=res_val,
            aspect_ratio=ratio_val,
            duration_seconds=dur_val,
            source_image_path=source_img,
            ffmpeg_path=self.settings.ffmpeg_path,
            runtime_manager=self.runtime_manager,
            parent=self,
        )

        self.runner_thread.log_signal.connect(self._append_log)
        self.runner_thread.progress_signal.connect(self._on_progress)
        self.runner_thread.stats_signal.connect(self._on_stats)
        self.runner_thread.worker_status_signal.connect(self.status_label.setText)
        self.runner_thread.batch_finished_signal.connect(self._on_batch_finished)

        self.runner_thread.start()

    def _toggle_pause(self) -> None:
        if self.runner_thread and self.runner_thread.isRunning():
            is_paused = self.runner_thread.toggle_pause()
            if is_paused:
                self.btn_pause.setText("▶ Tiếp tục")
                self._append_log("⏸ Đã yêu cầu tạm dừng hàng đợi...")
            else:
                self.btn_pause.setText("⏸ Tạm dừng")
                self._append_log("▶ Tiếp tục chạy tác vụ...")

    def _stop_batch(self) -> None:
        if self.runner_thread and self.runner_thread.isRunning():
            if QMessageBox.question(self, "Xác nhận", "Bạn có chắc muốn dừng farm video AI Local?") == QMessageBox.Yes:
                self._append_log("⏹ Đang gửi lệnh dừng tới hàng đợi...")
                self.runner_thread.stop()
                self.btn_stop.setEnabled(False)

    def _on_progress(self, pct: int, msg: str) -> None:
        self.progress_bar.setValue(pct)
        self.status_label.setText(msg)

    def _on_stats(self, total: int, completed: int, failed: int, remaining: int) -> None:
        self.stat_total.setText(f"Tổng: {total}")
        self.stat_done.setText(f"✔ Thành công: {completed}")
        self.stat_failed.setText(f"✖ Lỗi: {failed}")
        self.stat_rem.setText(f"⏳ Còn lại: {remaining}")

    def _on_batch_finished(self, success: bool, msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.status_label.setText(msg)
        self._append_log(f"🏁 {msg}")
        if success:
            QMessageBox.information(self, "Hoàn tất", msg)
        else:
            QMessageBox.warning(self, "Thông báo", msg)
