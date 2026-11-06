from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ai.comfyui import WAN_VARIANT_DMD, WAN_VARIANT_QUALITY
from models.settings_model import AppSettings
from playlist_ai.core.file_namer import ConflictResolution
from playlist_ai.core.job_queue import JobQueueManager, JobState, PlaylistBatchRunner
from playlist_ai.core.prompt_parser import MappedItem, parse_and_map_prompts
from playlist_ai.core.track_scanner import TrackInfo, scan_mp3_folder
from playlist_ai.providers.base import CookieExpiredError, IProvider, ProviderError
from playlist_ai.providers.muse_provider import MuseProvider
from playlist_ai.providers.wan_provider import WanProvider
from utils.paths import USER_DATA_ROOT


class PasteCookieDialog(QDialog):
    """Hộp thoại dán cookie Muse AI khi cần nhập hoặc khi cookie hết hạn giữa chừng."""

    def __init__(self, current_cookie: str = "", parent=None, is_expired: bool = False):
        super().__init__(parent)
        self.setWindowTitle("🔑 Cập nhật Cookie Muse AI" if not is_expired else "⚠ Cookie Muse Đã Hết Hạn — Nhập Cookie Mới")
        self.resize(550, 360)
        self.new_cookie = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info_text = (
            "<b>Cookie của Muse AI đã hết hạn hoặc không hợp lệ.</b><br>"
            "Vui lòng đăng nhập lại trên trình duyệt, copy cookie (JSON hoặc string) và dán vào bên dưới.<br>"
            "<i>Tiến trình tạo video sẽ tiếp tục ngay tại bài đang xử lý mà không bị mất dữ liệu!</i>"
            if is_expired
            else "Dán cookie Muse AI (hỗ trợ định dạng JSON hoặc Header string):"
        )
        info_lbl = QLabel(info_text)
        info_lbl.setWordWrap(True)
        if is_expired:
            info_lbl.setStyleSheet("color: #b45309; background: #fef3c7; padding: 10px; border-radius: 6px;")
        layout.addWidget(info_lbl)

        self.cookie_text = QPlainTextEdit()
        self.cookie_text.setPlaceholderText("Dán chuỗi cookie hoặc mảng JSON cookie vào đây...")
        self.cookie_text.setPlainText(current_cookie)
        layout.addWidget(self.cookie_text, 1)

        btn_row = QHBoxLayout()
        btn_cancel = QPushButton("Hủy bỏ")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("💾 Cập nhật & Tiếp tục" if is_expired else "💾 Lưu Cookie")
        btn_save.setStyleSheet("background: #2563eb; color: white; font-weight: bold; padding: 6px 16px;")
        btn_save.clicked.connect(self._on_save)

        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

    def _on_save(self):
        val = self.cookie_text.toPlainText().strip()
        if not val:
            QMessageBox.warning(self, "Chưa nhập cookie", "Vui lòng dán nội dung cookie.")
            return
        self.new_cookie = val
        self.accept()


class PlaylistStudioPage(QWidget):
    """
    Trang Tạo Video Playlist AI:
    - Quét bài hát từ thư mục MP3, trích xuất ID3 title.
    - Nhập prompt từng dòng, tự động map theo thứ tự bài hát.
    - Chèn Title vào Prompt theo template tùy biến.
    - Chọn AI: Wan 2.2 Local (ComfyUI) hoặc Muse AI (Cookie), cho phép đổi giữa chừng.
    - Xử lý Cookie Muse hết hạn không mất tiến độ.
    - Quản lý hàng đợi, retry riêng từng bài, resume và thanh tiến độ.
    """

    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.queue_manager = JobQueueManager()
        self.runner: Optional[PlaylistBatchRunner] = None
        self.tracks: List[TrackInfo] = []
        self.mapped_items: List[MappedItem] = []

        # Các provider được khởi tạo
        self.wan_provider = WanProvider(
            comfyui_url=self.settings.comfyui_url or "http://127.0.0.1:8188",
            model_variant=getattr(self.settings, "playlist_wan_variant", WAN_VARIANT_DMD) or WAN_VARIANT_DMD,
            ffmpeg_path=self.settings.ffmpeg_path or "ffmpeg",
        )
        self.muse_provider = MuseProvider(
            cookie_raw=getattr(self.settings, "playlist_muse_cookie", "") or "",
        )

        self._build_ui()
        self._load_saved_state()

    @property
    def busy(self) -> bool:
        return bool(self.runner and self.runner.isRunning())

    def shutdown(self) -> None:
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self.runner.wait(3000)
        if hasattr(self.muse_provider, "close"):
            try:
                self.muse_provider.close()
            except Exception:
                pass

    # =========================================================================
    # XÂY DỰNG GIAO DIỆN
    # =========================================================================
    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Tiêu đề trang
        header_row = QHBoxLayout()
        header_title = QLabel("🎵 Tạo Video Playlist AI (Ghép Prompt ↔ MP3 Title)")
        header_title.setStyleSheet("font-size: 18px; font-weight: bold; color: #0f172a;")
        header_sub = QLabel("Quét ID3 Title bài hát, ghép tự động với danh sách Prompt, render video theo thứ tự bằng Wan Local hoặc Muse.")
        header_sub.setStyleSheet("color: #64748b; font-size: 12px;")
        
        header_layout = QVBoxLayout()
        header_layout.addWidget(header_title)
        header_layout.addWidget(header_sub)
        header_row.addLayout(header_layout, 1)

        main_layout.addLayout(header_row)

        # Splitter chia 2 cột: Cột trái (Nhập liệu & Cấu hình) | Cột phải (Hàng đợi & Kết quả)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # === PANEL TRÁI: Nhập liệu ===
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setStyleSheet("QScrollArea { border: 1px solid #e2e8f0; border-radius: 8px; background: white; }")
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(12, 12, 12, 12)
        left_layout.setSpacing(14)

        # 1. Thư mục MP3 & Output
        folder_box = QGroupBox("📁 1. Chọn Thư mục Nhạc & Output")
        folder_layout = QVBoxLayout(folder_box)
        folder_layout.setSpacing(8)

        # Thư mục MP3
        mp3_row = QHBoxLayout()
        mp3_row.addWidget(QLabel("Thư mục MP3:"))
        self.mp3_folder_edit = QLineEdit()
        self.mp3_folder_edit.setPlaceholderText("Chọn folder chứa các file .mp3...")
        self.mp3_folder_edit.setText(getattr(self.settings, "playlist_last_mp3_folder", ""))
        self.mp3_folder_edit.textChanged.connect(self._on_mp3_folder_changed)
        mp3_row.addWidget(self.mp3_folder_edit, 1)

        btn_browse_mp3 = QPushButton("Chọn Folder...")
        btn_browse_mp3.clicked.connect(self._browse_mp3_folder)
        mp3_row.addWidget(btn_browse_mp3)
        folder_layout.addLayout(mp3_row)

        self.tracks_count_lbl = QLabel("Chưa quét file nào.")
        self.tracks_count_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        folder_layout.addWidget(self.tracks_count_lbl)

        # Thư mục Output
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Thư mục Video:"))
        self.output_folder_edit = QLineEdit()
        self.output_folder_edit.setPlaceholderText("Thư mục lưu video đầu ra...")
        default_out = (
            getattr(self.settings, "playlist_last_output_folder", "")
            or getattr(self.settings, "last_output_folder", "")
            or str(Path.home() / "Videos" / "Playlist_AI")
        )
        self.output_folder_edit.setText(default_out)
        out_row.addWidget(self.output_folder_edit, 1)

        btn_browse_out = QPushButton("Chọn...")
        btn_browse_out.clicked.connect(self._browse_output_folder)
        out_row.addWidget(btn_browse_out)

        btn_open_out = QPushButton("📂 Mở")
        btn_open_out.setToolTip("Mở thư mục output trong File Explorer")
        btn_open_out.clicked.connect(self._open_output_folder)
        out_row.addWidget(btn_open_out)
        folder_layout.addLayout(out_row)

        left_layout.addWidget(folder_box)

        # 2. Ô nhập Prompt & Ghép nối
        prompt_box = QGroupBox("✍ 2. Danh sách Prompt & Ghép bài hát")
        prompt_layout = QVBoxLayout(prompt_box)
        prompt_layout.setSpacing(8)

        # Counter badge
        counter_row = QHBoxLayout()
        counter_lbl_title = QLabel("Mỗi dòng 1 prompt:")
        counter_lbl_title.setStyleSheet("font-weight: 500;")
        counter_row.addWidget(counter_lbl_title)

        self.prompt_counter_lbl = QLabel("0 prompt / 0 bài")
        self.prompt_counter_lbl.setStyleSheet(
            "background: #e2e8f0; color: #1e293b; padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 11px;"
        )
        counter_row.addWidget(self.prompt_counter_lbl)
        counter_row.addStretch()
        prompt_layout.addLayout(counter_row)

        self.prompts_edit = QPlainTextEdit()
        self.prompts_edit.setPlaceholderText(
            "Ví dụ:\n"
            "cozy coffee shop in rain, neon lights, lo-fi aesthetic\n"
            "sunset over misty mountain, peaceful river, soft golden hour\n"
            "night city drive, bokeh reflections, anime style\n"
            "(Mỗi dòng ứng với 1 bài hát theo thứ tự số/tên file)"
        )
        self.prompts_edit.setMinimumHeight(130)
        self.prompts_edit.textChanged.connect(self._on_prompts_changed)
        prompt_layout.addWidget(self.prompts_edit)

        # Tùy chọn lệch số lượng
        mismatch_row = QHBoxLayout()
        mismatch_row.addWidget(QLabel("Khi prompt ít hơn bài:"))
        self.mismatch_combo = QComboBox()
        self.mismatch_combo.addItem("Lặp vòng lại prompt (Loop)", "loop")
        self.mismatch_combo.addItem("Dùng prompt cuối cho các bài sau (Last)", "last")
        saved_mismatch = getattr(self.settings, "playlist_mismatch_mode", "loop") or "loop"
        self.mismatch_combo.setCurrentIndex(1 if saved_mismatch == "last" else 0)
        self.mismatch_combo.currentIndexChanged.connect(self._on_prompts_changed)
        mismatch_row.addWidget(self.mismatch_combo, 1)
        prompt_layout.addLayout(mismatch_row)

        self.mismatch_warn_lbl = QLabel("")
        self.mismatch_warn_lbl.setStyleSheet("color: #b45309; font-size: 11px; font-weight: bold;")
        self.mismatch_warn_lbl.setVisible(False)
        prompt_layout.addWidget(self.mismatch_warn_lbl)

        # Template chèn Title vào Prompt
        template_group = QGroupBox("Chèn Title bài hát vào Prompt")
        template_group_layout = QVBoxLayout(template_group)
        template_group_layout.setSpacing(6)

        self.use_template_check = QCheckBox("Bật chèn tên bài hát (Title) vào prompt")
        self.use_template_check.setChecked(bool(getattr(self.settings, "playlist_use_template", True)))
        self.use_template_check.toggled.connect(self._on_template_toggled)
        template_group_layout.addWidget(self.use_template_check)

        tmpl_row = QHBoxLayout()
        tmpl_row.addWidget(QLabel("Template:"))
        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText('{prompt}, mood theo bài hát "{title}", cinematic, smooth motion')
        default_tmpl = getattr(
            self.settings,
            "playlist_prompt_template",
            '{prompt}, mood theo bài hát "{title}", cinematic, smooth motion',
        ) or '{prompt}, mood theo bài hát "{title}", cinematic, smooth motion'
        self.template_edit.setText(default_tmpl)
        self.template_edit.textChanged.connect(self._on_prompts_changed)
        tmpl_row.addWidget(self.template_edit, 1)
        template_group_layout.addLayout(tmpl_row)

        tmpl_hint = QLabel("💡 Giữ nguyên biến {prompt} và {title}. Title chỉ dùng gợi ý mood/nội dung, không vẽ chữ lên ảnh.")
        tmpl_hint.setStyleSheet("color: #64748b; font-size: 11px;")
        template_group_layout.addWidget(tmpl_hint)

        prompt_layout.addWidget(template_group)
        left_layout.addWidget(prompt_box)

        # 3. Chọn AI & Cấu hình Video
        ai_box = QGroupBox("🤖 3. Chọn Công cụ AI & Cài đặt Video")
        ai_layout = QVBoxLayout(ai_box)
        ai_layout.setSpacing(10)

        # Chọn AI
        ai_sel_row = QHBoxLayout()
        ai_sel_row.addWidget(QLabel("Công cụ AI:"))
        self.provider_combo = QComboBox()
        self.provider_combo.addItem("Wan 2.2 Local (ComfyUI)", "wan")
        self.provider_combo.addItem("Muse AI (Cookie Web)", "muse")
        saved_provider = getattr(self.settings, "playlist_provider", "wan") or "wan"
        self.provider_combo.setCurrentIndex(1 if saved_provider == "muse" else 0)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_selection_changed)
        ai_sel_row.addWidget(self.provider_combo, 1)

        self.btn_update_pending_ai = QPushButton("⚡ Đổi AI cho các bài chưa chạy")
        self.btn_update_pending_ai.setToolTip("Đổi AI được chọn cho toàn bộ bài đang ở trạng thái 'Chờ' hoặc 'Lỗi'")
        self.btn_update_pending_ai.clicked.connect(self._update_pending_jobs_ai)
        ai_sel_row.addWidget(self.btn_update_pending_ai)
        ai_layout.addLayout(ai_sel_row)

        # Khối chi tiết AI Wan
        self.wan_panel = QWidget()
        wan_layout = QVBoxLayout(self.wan_panel)
        wan_layout.setContentsMargins(0, 4, 0, 4)
        wan_layout.setSpacing(6)

        wan_cfg_row = QHBoxLayout()
        wan_cfg_row.addWidget(QLabel("Model Wan:"))
        self.wan_variant_combo = QComboBox()
        self.wan_variant_combo.addItem("DMD 4 bước (Cực nhanh)", WAN_VARIANT_DMD)
        self.wan_variant_combo.addItem("Chất lượng 20 bước (Đẹp hơn, chậm hơn)", WAN_VARIANT_QUALITY)
        saved_var = getattr(self.settings, "playlist_wan_variant", WAN_VARIANT_DMD) or WAN_VARIANT_DMD
        self.wan_variant_combo.setCurrentIndex(1 if saved_var == WAN_VARIANT_QUALITY else 0)
        self.wan_variant_combo.currentIndexChanged.connect(self._on_wan_variant_changed)
        wan_cfg_row.addWidget(self.wan_variant_combo, 1)

        self.btn_check_wan = QPushButton("🔌 Kiểm tra ComfyUI")
        self.btn_check_wan.clicked.connect(self._check_wan_connection)
        wan_cfg_row.addWidget(self.btn_check_wan)
        wan_layout.addLayout(wan_cfg_row)

        self.wan_status_lbl = QLabel("Sẵn sàng kiểm tra kết nối ComfyUI.")
        self.wan_status_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        wan_layout.addWidget(self.wan_status_lbl)
        ai_layout.addWidget(self.wan_panel)

        # Khối chi tiết AI Muse
        self.muse_panel = QWidget()
        muse_layout = QVBoxLayout(self.muse_panel)
        muse_layout.setContentsMargins(0, 4, 0, 4)
        muse_layout.setSpacing(6)

        muse_cfg_row = QHBoxLayout()
        self.btn_paste_cookie = QPushButton("📋 Dán / Sửa Cookie Muse")
        self.btn_paste_cookie.clicked.connect(self._open_cookie_dialog)
        muse_cfg_row.addWidget(self.btn_paste_cookie)

        self.btn_check_muse = QPushButton("🔑 Kiểm tra Cookie")
        self.btn_check_muse.clicked.connect(self._check_muse_connection)
        muse_cfg_row.addWidget(self.btn_check_muse)
        muse_layout.addLayout(muse_cfg_row)

        self.muse_status_lbl = QLabel(
            "Đã lưu cookie." if getattr(self.settings, "playlist_muse_cookie", "") else "Chưa có cookie Muse. Bấm 'Dán Cookie' để thêm."
        )
        self.muse_status_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        muse_layout.addWidget(self.muse_status_lbl)
        ai_layout.addWidget(self.muse_panel)

        # Thông số video
        vid_cfg_row = QHBoxLayout()
        vid_cfg_row.addWidget(QLabel("Thời lượng clip:"))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(3, 10)
        self.duration_spin.setSuffix(" giây")
        self.duration_spin.setValue(int(getattr(self.settings, "playlist_duration", 5) or 5))
        vid_cfg_row.addWidget(self.duration_spin)

        vid_cfg_row.addWidget(QLabel("Độ phân giải:"))
        self.res_combo = QComboBox()
        self.res_combo.addItem("1280x704 (16:9 Chuẩn, Khuyên dùng)", "1280x704")
        self.res_combo.addItem("832x480 (16:9 Nhanh/Nhẹ)", "832x480")
        self.res_combo.addItem("1920x1088 (FullHD 16:9)", "1920x1088")
        saved_res = getattr(self.settings, "playlist_resolution", "1280x704") or "1280x704"
        if saved_res == "1920x1080":
            saved_res = "1920x1088"
        for i in range(self.res_combo.count()):
            if self.res_combo.itemData(i) == saved_res:
                self.res_combo.setCurrentIndex(i)
                break
        vid_cfg_row.addWidget(self.res_combo)
        ai_layout.addLayout(vid_cfg_row)

        # Tùy chọn Thumbnail & Trùng file
        opt_row = QHBoxLayout()
        self.keep_thumb_check = QCheckBox("Giữ ảnh gốc (.png) làm thumbnail")
        self.keep_thumb_check.setChecked(bool(getattr(self.settings, "playlist_keep_thumbnail", True)))
        opt_row.addWidget(self.keep_thumb_check)

        opt_row.addSpacing(12)
        opt_row.addWidget(QLabel("Nếu trùng tên:"))
        self.conflict_combo = QComboBox()
        self.conflict_combo.addItem("Thêm hậu tố _2, _3", "suffix")
        self.conflict_combo.addItem("Ghi đè file cũ", "overwrite")
        self.conflict_combo.addItem("Bỏ qua file đã có", "skip")
        saved_conflict = getattr(self.settings, "playlist_conflict_mode", "suffix") or "suffix"
        for i in range(self.conflict_combo.count()):
            if self.conflict_combo.itemData(i) == saved_conflict:
                self.conflict_combo.setCurrentIndex(i)
                break
        opt_row.addWidget(self.conflict_combo)
        ai_layout.addLayout(opt_row)

        left_layout.addWidget(ai_box)

        # Xem trước Mapping (Collapsible or Preview table)
        preview_box = QGroupBox("👁 Xem trước Prompt cuối ghép với bài hát")
        preview_layout = QVBoxLayout(preview_box)
        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(4)
        self.preview_table.setHorizontalHeaderLabels(["STT", "Tên bài (Title)", "Prompt gốc", "Prompt cuối gửi AI"])
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.preview_table.setMinimumHeight(150)
        self.preview_table.setAlternatingRowColors(True)
        preview_layout.addWidget(self.preview_table)
        left_layout.addWidget(preview_box)

        left_scroll.setWidget(left_widget)
        splitter.addWidget(left_scroll)

        # === PANEL PHẢI: Hàng đợi, Trạng thái & Log ===
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(12)

        # Hàng nút điều khiển
        action_box = QGroupBox("🕹 Điều khiển Hàng đợi Tác vụ")
        action_layout = QHBoxLayout(action_box)
        action_layout.setSpacing(8)

        self.btn_start = QPushButton("🚀 BẮT ĐẦU TẠO VIDEO")
        self.btn_start.setStyleSheet(
            "background: #16a34a; color: white; font-weight: bold; font-size: 13px; padding: 8px 16px; border-radius: 6px;"
        )
        self.btn_start.clicked.connect(self._start_batch)
        action_layout.addWidget(self.btn_start)

        self.btn_pause = QPushButton("⏸ Tạm dừng")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        action_layout.addWidget(self.btn_pause)

        self.btn_stop = QPushButton("⏹ Dừng lại")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_batch)
        action_layout.addWidget(self.btn_stop)

        self.btn_retry_failed = QPushButton("🔄 Chạy lại bài lỗi")
        self.btn_retry_failed.clicked.connect(self._retry_failed_jobs)
        action_layout.addWidget(self.btn_retry_failed)

        self.btn_clear_queue = QPushButton("🗑 Làm mới / Xóa")
        self.btn_clear_queue.clicked.connect(self._clear_queue)
        action_layout.addWidget(self.btn_clear_queue)

        right_layout.addWidget(action_box)

        # Tiến trình tổng
        progress_box = QGroupBox("📊 Tiến trình tổng thể")
        prog_layout = QVBoxLayout(progress_box)
        self.overall_progress_bar = QProgressBar()
        self.overall_progress_bar.setRange(0, 100)
        self.overall_progress_bar.setValue(0)
        self.overall_progress_bar.setTextVisible(True)
        prog_layout.addWidget(self.overall_progress_bar)

        self.overall_status_lbl = QLabel("Sẵn sàng.")
        self.overall_status_lbl.setStyleSheet("color: #475569; font-size: 12px; font-weight: 500;")
        prog_layout.addWidget(self.overall_status_lbl)
        right_layout.addWidget(progress_box)

        # Bảng Job
        jobs_box = QGroupBox("📋 Danh sách Job Video")
        jobs_layout = QVBoxLayout(jobs_box)
        self.job_table = QTableWidget()
        self.job_table.setColumnCount(6)
        self.job_table.setHorizontalHeaderLabels([
            "STT", "Tên bài hát (Title)", "AI", "Trạng thái", "Video đã lưu", "Thao tác"
        ])
        self.job_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.job_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.job_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.job_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.job_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.job_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.job_table.setAlternatingRowColors(True)
        self.job_table.cellDoubleClicked.connect(self._on_job_cell_double_clicked)
        jobs_layout.addWidget(self.job_table)
        right_layout.addWidget(jobs_box, 1)

        # Nhật ký hoạt động
        log_box = QGroupBox("📜 Nhật ký hoạt động")
        log_layout = QVBoxLayout(log_box)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(140)
        self.log_text.setStyleSheet(
            "background: #0f172a; color: #f8fafc; font-family: Consolas, monospace; font-size: 11px; border-radius: 4px;"
        )
        log_layout.addWidget(self.log_text)
        right_layout.addWidget(log_box)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter, 1)

        # Khởi tạo hiển thị panel AI ban đầu
        self._update_ai_panels()

    # =========================================================================
    # SỰ KIỆN & QUÉT THƯ MỤC
    # =========================================================================
    def _browse_mp3_folder(self) -> None:
        init_dir = self.mp3_folder_edit.text().strip() or getattr(self.settings, "last_input_folder", "") or ""
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục chứa file MP3", init_dir)
        if folder:
            self.mp3_folder_edit.setText(folder)

    def _on_mp3_folder_changed(self, text: str) -> None:
        path = Path(text.strip())
        if path.is_dir():
            self.tracks = scan_mp3_folder(path)
            self.tracks_count_lbl.setText(f"✔ Đã tìm thấy {len(self.tracks)} bài hát .mp3")
            setattr(self.settings, "playlist_last_mp3_folder", str(path))
            self.settings_changed.emit()
        else:
            self.tracks = []
            self.tracks_count_lbl.setText("Thư mục không tồn tại.")
        self._update_mapping()

    def _browse_output_folder(self) -> None:
        init_dir = self.output_folder_edit.text().strip() or getattr(self.settings, "last_output_folder", "") or ""
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video đầu ra", init_dir)
        if folder:
            self.output_folder_edit.setText(folder)
            setattr(self.settings, "playlist_last_output_folder", folder)
            self.settings_changed.emit()

    def _open_output_folder(self) -> None:
        out = self.output_folder_edit.text().strip()
        if out and Path(out).is_dir():
            os.startfile(out)
        else:
            QMessageBox.information(self, "Thông báo", "Thư mục output chưa được tạo hoặc không tồn tại.")

    def _on_prompts_changed(self) -> None:
        self._update_mapping()

    def _on_template_toggled(self, checked: bool) -> None:
        self.template_edit.setEnabled(checked)
        self._update_mapping()

    def _update_mapping(self) -> None:
        prompt_text = self.prompts_edit.toPlainText()
        mismatch = self.mismatch_combo.currentData() or "loop"
        use_tmpl = self.use_template_check.isChecked()
        tmpl = self.template_edit.text().strip() or '{prompt}, mood theo bài hát "{title}", cinematic, smooth motion'

        self.mapped_items, p_count, t_count, warn = parse_and_map_prompts(
            prompt_text=prompt_text,
            tracks=self.tracks,
            template=tmpl,
            use_template=use_tmpl,
            mismatch_mode=mismatch,
        )

        # Cập nhật Badge đếm
        if t_count > 0 and p_count == t_count:
            self.prompt_counter_lbl.setText(f"{p_count} prompt / {t_count} bài (Khớp hoàn hảo)")
            self.prompt_counter_lbl.setStyleSheet(
                "background: #dcfce7; color: #166534; padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 11px;"
            )
        elif t_count > 0 and p_count < t_count:
            mode_text = "Lặp vòng" if mismatch == "loop" else "Dùng cuối"
            self.prompt_counter_lbl.setText(f"{p_count} prompt / {t_count} bài ({mode_text})")
            self.prompt_counter_lbl.setStyleSheet(
                "background: #fef3c7; color: #92400e; padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 11px;"
            )
        elif p_count > t_count and t_count > 0:
            excess = p_count - t_count
            self.prompt_counter_lbl.setText(f"{p_count} prompt / {t_count} bài (Thừa {excess})")
            self.prompt_counter_lbl.setStyleSheet(
                "background: #fee2e2; color: #991b1b; padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 11px;"
            )
        else:
            self.prompt_counter_lbl.setText(f"{p_count} prompt / {t_count} bài")
            self.prompt_counter_lbl.setStyleSheet(
                "background: #e2e8f0; color: #1e293b; padding: 2px 8px; border-radius: 10px; font-weight: bold; font-size: 11px;"
            )

        if warn:
            self.mismatch_warn_lbl.setText(f"⚠ {warn}")
            self.mismatch_warn_lbl.setVisible(True)
        else:
            self.mismatch_warn_lbl.setVisible(False)

        # Cập nhật bảng Preview
        self.preview_table.setRowCount(len(self.mapped_items))
        for r, item in enumerate(self.mapped_items):
            self.preview_table.setItem(r, 0, QTableWidgetItem(str(item.index)))
            self.preview_table.setItem(r, 1, QTableWidgetItem(item.track.title))
            self.preview_table.setItem(r, 2, QTableWidgetItem(item.raw_prompt))
            self.preview_table.setItem(r, 3, QTableWidgetItem(item.final_prompt))

    # =========================================================================
    # QUẢN LÝ CẤU HÌNH AI
    # =========================================================================
    def _on_provider_selection_changed(self) -> None:
        self._update_ai_panels()
        prov = self.provider_combo.currentData()
        setattr(self.settings, "playlist_provider", prov)
        self.settings_changed.emit()

    def _update_ai_panels(self) -> None:
        prov = self.provider_combo.currentData()
        self.wan_panel.setVisible(prov == "wan")
        self.muse_panel.setVisible(prov == "muse")

    def _on_wan_variant_changed(self) -> None:
        var = self.wan_variant_combo.currentData()
        self.wan_provider.model_variant = var
        setattr(self.settings, "playlist_wan_variant", var)
        self.settings_changed.emit()

    def _check_wan_connection(self) -> None:
        self.wan_status_lbl.setText("Đang kiểm tra kết nối ComfyUI...")
        ok, msg = self.wan_provider.check_health()
        if ok:
            self.wan_status_lbl.setText(f"✔ {msg}")
            self.wan_status_lbl.setStyleSheet("color: #16a34a; font-weight: bold; font-size: 11px;")
        else:
            self.wan_status_lbl.setText(f"❌ {msg}")
            self.wan_status_lbl.setStyleSheet("color: #dc2626; font-size: 11px;")

    def _open_cookie_dialog(self, is_expired: bool = False) -> None:
        current_cookie = getattr(self.settings, "playlist_muse_cookie", "") or ""
        dlg = PasteCookieDialog(current_cookie=current_cookie, parent=self, is_expired=is_expired)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cookie = dlg.new_cookie
            setattr(self.settings, "playlist_muse_cookie", new_cookie)
            self.muse_provider.update_cookie(new_cookie)
            self.muse_status_lbl.setText("✔ Đã lưu cookie mới. Sẵn sàng sử dụng.")
            self.muse_status_lbl.setStyleSheet("color: #16a34a; font-size: 11px;")
            self.settings_changed.emit()
            self._log("🔑 Đã cập nhật Cookie Muse AI thành công.")
            if is_expired and self.runner and self.runner._paused:
                self.runner.resume()

    def _check_muse_connection(self) -> None:
        self.muse_status_lbl.setText("Đang kiểm tra trạng thái cookie...")
        ok, msg = self.muse_provider.check_health()
        if ok:
            self.muse_status_lbl.setText(f"✔ {msg}")
            self.muse_status_lbl.setStyleSheet("color: #16a34a; font-weight: bold; font-size: 11px;")
        else:
            self.muse_status_lbl.setText(f"❌ {msg}")
            self.muse_status_lbl.setStyleSheet("color: #dc2626; font-size: 11px;")

    def _update_pending_jobs_ai(self) -> None:
        """Đổi provider của các bài đang 'Chờ' hoặc 'Lỗi' sang provider đang chọn."""
        prov = self.provider_combo.currentData()
        count = self.queue_manager.update_pending_provider(prov)
        self._refresh_jobs_table()
        QMessageBox.information(
            self,
            "Đã đổi AI",
            f"Đã cập nhật công cụ AI sang '{prov.upper()}' cho {count} bài chưa hoàn thành.",
        )

    # =========================================================================
    # QUẢN LÝ BẢNG JOB & THỰC THI HÀNG ĐỢI
    # =========================================================================
    def _load_saved_state(self) -> None:
        """Tải các job từ lần chạy trước (resume)."""
        self.queue_manager.load()
        self._refresh_jobs_table()

    def _refresh_jobs_table(self) -> None:
        jobs = self.queue_manager.jobs
        self.job_table.setRowCount(len(jobs))

        for row, job in enumerate(jobs):
            self.job_table.setItem(row, 0, QTableWidgetItem(str(job.index)))
            self.job_table.setItem(row, 1, QTableWidgetItem(job.track_title))
            self.job_table.setItem(row, 2, QTableWidgetItem(job.provider_key.upper()))

            status_item = QTableWidgetItem(job.status)
            if job.status == "Hoàn thành":
                status_item.setForeground(QColor("#16a34a"))
            elif job.status in {"Đang tạo ảnh", "Đang tạo chuyển động"}:
                status_item.setForeground(QColor("#2563eb"))
            elif job.status == "Lỗi":
                status_item.setForeground(QColor("#dc2626"))
            elif job.status == "Tạm dừng":
                status_item.setForeground(QColor("#d97706"))
            elif job.status == "Bỏ qua":
                status_item.setForeground(QColor("#64748b"))
            self.job_table.setItem(row, 3, status_item)

            vid_name = Path(job.output_video_path).name if job.output_video_path else ""
            self.job_table.setItem(row, 4, QTableWidgetItem(vid_name))

            btn_retry = QPushButton("Chạy lại")
            btn_retry.setEnabled(not self.busy)
            btn_retry.clicked.connect(lambda _, jid=job.id: self._retry_single_job(jid))
            self.job_table.setCellWidget(row, 5, btn_retry)

    def _start_batch(self) -> None:
        if self.busy:
            return

        out_folder = self.output_folder_edit.text().strip()
        if not out_folder:
            QMessageBox.warning(self, "Chưa chọn thư mục", "Vui lòng chọn thư mục lưu video.")
            return

        # Nếu chưa có job nào trong hàng đợi, tạo mới từ kết quả map
        if not self.queue_manager.jobs:
            if not self.mapped_items:
                QMessageBox.warning(
                    self,
                    "Chưa có bài hát",
                    "Vui lòng chọn thư mục MP3 và nhập danh sách prompt trước khi bắt đầu.",
                )
                return

            res_parts = (self.res_combo.currentData() or "1280x704").split("x")
            raw_w, raw_h = int(res_parts[0]), int(res_parts[1])
            w = max(256, round(raw_w / 32) * 32)
            h = max(256, round(raw_h / 32) * 32)

            self.queue_manager.set_jobs_from_mapped_items(
                mapped_items=self.mapped_items,
                provider_key=self.provider_combo.currentData() or "wan",
                duration_seconds=self.duration_spin.value(),
                width=w,
                height=h,
                keep_thumbnail=self.keep_thumb_check.isChecked(),
            )
            setattr(self.settings, "playlist_resolution", self.res_combo.currentData())
            setattr(self.settings, "playlist_duration", self.duration_spin.value())
            setattr(self.settings, "playlist_keep_thumbnail", self.keep_thumb_check.isChecked())
            setattr(self.settings, "playlist_conflict_mode", self.conflict_combo.currentData())
            self.settings_changed.emit()
            self._refresh_jobs_table()

        # Tạo và khởi chạy PlaylistBatchRunner
        conflict_mode: ConflictResolution = self.conflict_combo.currentData() or "suffix"
        providers_dict: Dict[str, IProvider] = {
            "wan": self.wan_provider,
            "muse": self.muse_provider,
        }

        self.runner = PlaylistBatchRunner(
            queue_manager=self.queue_manager,
            providers=providers_dict,
            output_dir=out_folder,
            conflict_mode=conflict_mode,
            ffmpeg_path=self.settings.ffmpeg_path or "ffmpeg",
        )
        self.runner.job_status_changed.connect(self._on_job_status_changed)
        self.runner.log_signal.connect(self._log)
        self.runner.progress_signal.connect(self._on_progress)
        self.runner.cookie_expired_signal.connect(self._on_cookie_expired)
        self.runner.all_finished_signal.connect(self._on_all_finished)

        self._set_running_ui(True)
        self.runner.start()

    def _set_running_ui(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_clear_queue.setEnabled(not running)
        self.btn_retry_failed.setEnabled(not running)
        self.btn_update_pending_ai.setEnabled(not running)

    def _toggle_pause(self) -> None:
        if not self.runner:
            return
        if self.runner._paused:
            self.runner.resume()
            self.btn_pause.setText("⏸ Tạm dừng")
        else:
            self.runner.pause()
            self.btn_pause.setText("▶ Tiếp tục")

    def _stop_batch(self) -> None:
        if self.runner and self.runner.isRunning():
            self.runner.stop()
            self.btn_stop.setEnabled(False)

    def _retry_failed_jobs(self) -> None:
        if self.busy:
            return
        failed_count = 0
        for j in self.queue_manager.jobs:
            if j.status == "Lỗi":
                j.status = "Chờ"
                j.error_message = ""
                failed_count += 1
        if failed_count == 0:
            QMessageBox.information(self, "Thông báo", "Không có bài nào bị lỗi.")
            return
        self.queue_manager.save()
        self._refresh_jobs_table()
        self._start_batch()

    def _retry_single_job(self, job_id: str) -> None:
        if self.busy:
            return
        job = self.queue_manager.get_job(job_id)
        if not job:
            return
        out_folder = self.output_folder_edit.text().strip()
        if not out_folder:
            QMessageBox.warning(self, "Chưa chọn thư mục", "Vui lòng chọn thư mục lưu video.")
            return

        conflict_mode: ConflictResolution = self.conflict_combo.currentData() or "suffix"
        providers_dict: Dict[str, IProvider] = {
            "wan": self.wan_provider,
            "muse": self.muse_provider,
        }

        self.runner = PlaylistBatchRunner(
            queue_manager=self.queue_manager,
            providers=providers_dict,
            output_dir=out_folder,
            conflict_mode=conflict_mode,
            ffmpeg_path=self.settings.ffmpeg_path or "ffmpeg",
        )
        self.runner.job_status_changed.connect(self._on_job_status_changed)
        self.runner.log_signal.connect(self._log)
        self.runner.progress_signal.connect(self._on_progress)
        self.runner.cookie_expired_signal.connect(self._on_cookie_expired)
        self.runner.all_finished_signal.connect(self._on_all_finished)

        self._set_running_ui(True)
        self.runner.retry_single_job(job_id)

    def _clear_queue(self) -> None:
        if self.busy:
            return
        ans = QMessageBox.question(
            self,
            "Xác nhận",
            "Bạn có chắc muốn xóa toàn bộ hàng đợi hiện tại và làm mới?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.queue_manager.clear()
            self._refresh_jobs_table()
            self.overall_progress_bar.setValue(0)
            self.overall_status_lbl.setText("Đã làm mới hàng đợi.")

    def _on_job_status_changed(self, job_dict: dict) -> None:
        job_id = job_dict.get("id")
        for row in range(self.job_table.rowCount()):
            if row < len(self.queue_manager.jobs) and self.queue_manager.jobs[row].id == job_id:
                status = job_dict.get("status", "")
                status_item = QTableWidgetItem(status)
                if status == "Hoàn thành":
                    status_item.setForeground(QColor("#16a34a"))
                elif status in {"Đang tạo ảnh", "Đang tạo chuyển động"}:
                    status_item.setForeground(QColor("#2563eb"))
                elif status == "Lỗi":
                    status_item.setForeground(QColor("#dc2626"))
                elif status == "Tạm dừng":
                    status_item.setForeground(QColor("#d97706"))
                elif status == "Bỏ qua":
                    status_item.setForeground(QColor("#64748b"))
                self.job_table.setItem(row, 3, status_item)

                out_vid = job_dict.get("output_video_path", "")
                vid_name = Path(out_vid).name if out_vid else ""
                self.job_table.setItem(row, 4, QTableWidgetItem(vid_name))
                break

    def _on_progress(self, percent: int, msg: str) -> None:
        self.overall_progress_bar.setValue(percent)
        self.overall_status_lbl.setText(msg)

    def _on_cookie_expired(self, error_msg: str) -> None:
        self._log(f"⚠ PHÁT HIỆN COOKIE HẾT HẠN: {error_msg}")
        # Mở dialog cho phép nhập cookie mới ngay lập tức
        QTimer.singleShot(100, lambda: self._open_cookie_dialog(is_expired=True))

    def _on_all_finished(self, success: bool, msg: str) -> None:
        self._set_running_ui(False)
        self.btn_pause.setText("⏸ Tạm dừng")
        self.overall_status_lbl.setText(msg)
        self._log(f"🏁 {msg}")
        self._refresh_jobs_table()
        if success:
            QMessageBox.information(self, "Hoàn tất", msg)

    def _on_job_cell_double_clicked(self, row: int, col: int) -> None:
        if row < len(self.queue_manager.jobs):
            job = self.queue_manager.jobs[row]
            if job.output_video_path and Path(job.output_video_path).is_file():
                os.startfile(job.output_video_path)

    def _log(self, text: str) -> None:
        self.log_text.appendPlainText(text)
