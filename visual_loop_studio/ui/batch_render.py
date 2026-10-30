from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from models.loop_project import LoopProject
from models.settings_model import AppSettings
from render.ffmpeg import build_loop_job
from render.nvenc import resolve_encoder
from render.render_worker import RenderWorker
from ui.common import AUDIO_FILTER, VIDEO_FILTER, show_error

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".wmv", ".m4v", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".wma", ".aiff"}


def natural_sort_key(value: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def scan_folder(folder_path: str, valid_extensions: set[str]) -> list[Path]:
    p = Path(folder_path)
    if not p.is_dir():
        return []
    try:
        files = [f for f in p.iterdir() if f.is_file() and f.suffix.lower() in valid_extensions]
        files.sort(key=lambda item: natural_sort_key(item.name))
        return files
    except Exception:
        return []


def parse_duration_string(text: str) -> tuple[str, float]:
    """
    Phân tích chuỗi thời lượng (ví dụ: '00:30:00', '01:00:00', '15:30', '1800', '60s', 'Theo nhạc').
    Trả về (duration_mode, custom_duration):
      - 'audio', 0.0 nếu theo bài nhạc
      - 'custom', số_giây nếu là thời lượng cụ thể
    """
    cleaned = (text or "").strip()
    if not cleaned or cleaned.lower().startswith("theo") or cleaned.lower() == "audio":
        return "audio", 0.0

    # Kiểm tra định dạng HH:MM:SS hoặc MM:SS
    parts = cleaned.split(":")
    if len(parts) == 3:
        try:
            h = float(parts[0].strip())
            m = float(parts[1].strip())
            s = float(parts[2].strip().replace("s", ""))
            return "custom", max(1.0, h * 3600 + m * 60 + s)
        except ValueError:
            pass
    elif len(parts) == 2:
        try:
            m = float(parts[0].strip())
            s = float(parts[1].strip().replace("s", ""))
            return "custom", max(1.0, m * 60 + s)
        except ValueError:
            pass

    # Kiểm tra số giây thuần (ví dụ: "1800", "60s", "120 giây")
    digits_match = re.search(r"(\d+(\.\d+)?)", cleaned)
    if digits_match:
        try:
            sec = float(digits_match.group(1))
            return "custom", max(1.0, sec)
        except ValueError:
            pass

    return "audio", 0.0


def format_seconds_to_hhmmss(seconds: float) -> str:
    """Định dạng số giây sang chuỗi Giờ:Phút:Giây (HH:MM:SS) kiểu '00:30:00'."""
    total = max(0, int(round(seconds)))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


class BatchRenderPage(QWidget):
    COLUMNS = [
        "#",
        "Video",
        "Nhạc chính",
        "Nhạc backtrack",
        "Thư mục lưu",
        "Tên file xuất",
        "Thời lượng",
        "Bộ mã hóa",
        "Trạng thái",
        "Tiến độ",
        "Thời gian / Kết quả",
    ]

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.queue: list[int] = []
        self.workers: list[RenderWorker] = []
        self.active: dict[RenderWorker, int] = {}
        self.max_concurrent = 1

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel("🎬 Render hàng loạt (Tự động bắt cặp Video & Nhạc + Backtrack)")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        # Card 1: Chọn folder nguồn & đầu ra
        folder_group = QGroupBox("📁 Nguồn Video, Nhạc chính & Nhạc Backtrack theo Thư mục")
        folder_grid = QGridLayout(folder_group)
        folder_grid.setVerticalSpacing(8)
        folder_grid.setHorizontalSpacing(10)

        # Row 0: Folder Video
        self.video_folder = QLineEdit()
        self.video_folder.setMinimumHeight(36)
        self.video_folder.setPlaceholderText("Chọn thư mục chứa các file video (.mp4, .mov, .mkv, .webm...)...")
        self.choose_video_btn = QPushButton("🎬 Chọn folder Video")
        self.choose_video_btn.setMinimumHeight(36)
        self.choose_video_btn.clicked.connect(self._choose_video_folder)
        self.video_count = QLabel("0 video")
        self.video_count.setStyleSheet("color: #2563eb; font-weight: 600;")

        # Row 1: Folder Nhạc chính
        self.audio_folder = QLineEdit()
        self.audio_folder.setMinimumHeight(36)
        self.audio_folder.setPlaceholderText("Chọn thư mục chứa các file nhạc chính (.mp3, .wav, .aac, .m4a...)...")
        self.choose_audio_btn = QPushButton("🎵 Chọn folder Nhạc")
        self.choose_audio_btn.setMinimumHeight(36)
        self.choose_audio_btn.clicked.connect(self._choose_audio_folder)
        self.audio_count = QLabel("0 bài nhạc")
        self.audio_count.setStyleSheet("color: #2563eb; font-weight: 600;")

        # Row 2: Folder Backtrack (Tùy chọn)
        self.backtrack_folder = QLineEdit()
        self.backtrack_folder.setMinimumHeight(36)
        self.backtrack_folder.setPlaceholderText("Chọn thư mục chứa nhạc nền backtrack / beat phụ (Tùy chọn, để trống nếu không dùng)...")
        backtrack_btn_box = QHBoxLayout()
        backtrack_btn_box.setSpacing(6)
        self.choose_backtrack_btn = QPushButton("🎶 Chọn folder Backtrack")
        self.choose_backtrack_btn.setMinimumHeight(36)
        self.choose_backtrack_btn.clicked.connect(self._choose_backtrack_folder)
        self.clear_backtrack_btn = QPushButton("✕ Xóa")
        self.clear_backtrack_btn.setMinimumHeight(36)
        self.clear_backtrack_btn.setToolTip("Xóa chọn folder backtrack")
        self.clear_backtrack_btn.clicked.connect(self._clear_backtrack_folder)
        backtrack_btn_box.addWidget(self.choose_backtrack_btn)
        backtrack_btn_box.addWidget(self.clear_backtrack_btn)
        self.backtrack_count = QLabel("0 bài backtrack")
        self.backtrack_count.setStyleSheet("color: #7c3aed; font-weight: 600;")

        # Row 3: Thư mục lưu
        self.output_folder = QLineEdit(self.settings.last_output_folder)
        self.output_folder.setMinimumHeight(36)
        self.output_folder.setPlaceholderText("Chọn thư mục lưu video kết quả...")
        self.choose_output_btn = QPushButton("📂 Chọn folder Lưu")
        self.choose_output_btn.setMinimumHeight(36)
        self.choose_output_btn.clicked.connect(self._choose_output_folder)

        folder_grid.addWidget(QLabel("Folder Video:"), 0, 0)
        folder_grid.addWidget(self.video_folder, 0, 1)
        folder_grid.addWidget(self.choose_video_btn, 0, 2)
        folder_grid.addWidget(self.video_count, 0, 3)

        folder_grid.addWidget(QLabel("Folder Nhạc chính:"), 1, 0)
        folder_grid.addWidget(self.audio_folder, 1, 1)
        folder_grid.addWidget(self.choose_audio_btn, 1, 2)
        folder_grid.addWidget(self.audio_count, 1, 3)

        folder_grid.addWidget(QLabel("Folder Backtrack:"), 2, 0)
        folder_grid.addWidget(self.backtrack_folder, 2, 1)
        folder_grid.addLayout(backtrack_btn_box, 2, 2)
        folder_grid.addWidget(self.backtrack_count, 2, 3)

        folder_grid.addWidget(QLabel("Thư mục lưu:"), 3, 0)
        folder_grid.addWidget(self.output_folder, 3, 1)
        folder_grid.addWidget(self.choose_output_btn, 3, 2)

        root.addWidget(folder_group)

        # Card 2: Cài đặt thời lượng & Âm lượng dB / Trim
        settings_group = QGroupBox("⏱️ Cài Đặt Thời Lượng Chung & Âm Lượng (dB) / Cắt Nhạc")
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(10)

        # Row 1: Duration settings
        config_row = QHBoxLayout()
        config_row.setSpacing(12)

        config_row.addWidget(QLabel("Thời lượng chung:"))
        self.duration_combo = QComboBox()
        self.duration_combo.setMinimumHeight(36)
        self.duration_combo.addItem("Theo độ dài bài nhạc (Chuẩn - Tự động)", "audio")
        self.duration_combo.addItem("00:00:30 (30 giây - Shorts / Reels)", "00:00:30")
        self.duration_combo.addItem("00:01:00 (1 phút)", "00:01:00")
        self.duration_combo.addItem("00:02:00 (2 phút)", "00:02:00")
        self.duration_combo.addItem("00:03:00 (3 phút)", "00:03:00")
        self.duration_combo.addItem("00:05:00 (5 phút)", "00:05:00")
        self.duration_combo.addItem("00:10:00 (10 phút)", "00:10:00")
        self.duration_combo.addItem("00:15:00 (15 phút)", "00:15:00")
        self.duration_combo.addItem("00:30:00 (30 phút)", "00:30:00")
        self.duration_combo.addItem("01:00:00 (1 tiếng)", "01:00:00")
        self.duration_combo.addItem("02:00:00 (2 tiếng)", "02:00:00")
        self.duration_combo.addItem("Tùy chỉnh Giờ:Phút:Giây (Tự điền)...", "custom")
        self.duration_combo.currentIndexChanged.connect(self._duration_mode_changed)
        config_row.addWidget(self.duration_combo)

        dur_input_label = QLabel("Giờ:Phút:Giây:")
        dur_input_label.setStyleSheet("color: #475569; font-weight: 500;")
        config_row.addWidget(dur_input_label)

        self.duration_input = QLineEdit("00:30:00")
        self.duration_input.setMinimumHeight(36)
        self.duration_input.setFixedWidth(115)
        self.duration_input.setPlaceholderText("00:30:00")
        self.duration_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_input.setStyleSheet(
            "QLineEdit { font-size: 13px; font-weight: bold; color: #1e293b; background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 6px; padding: 0 8px; } "
            "QLineEdit:disabled { background: #f1f5f9; color: #94a3b8; border-color: #e2e8f0; }"
        )
        self.duration_input.setToolTip("Nhập thời lượng theo định dạng Giờ:Phút:Giây (ví dụ: 00:30:00 hoặc 01:00:00). Cả video và nhạc sẽ tự động loop và chuyển cảnh mượt mà đến đúng thời lượng này.")
        self.duration_input.setEnabled(False)
        self.duration_input.textEdited.connect(self._on_duration_input_edited)
        config_row.addWidget(self.duration_input)

        self.loop_mismatch = QCheckBox("Lặp lại bên ít hơn để ghép hết bên nhiều hơn")
        self.loop_mismatch.setChecked(True)
        self.loop_mismatch.setToolTip(
            "Nếu tick: ví dụ 3 video và 10 bài nhạc thì 3 video sẽ tự lặp lại để tạo đủ 10 video.\n"
            "Nếu bỏ tick: chỉ ghép đến số lượng của bên ít hơn."
        )
        config_row.addWidget(self.loop_mismatch)
        config_row.addStretch()
        settings_layout.addLayout(config_row)

        # Row 2: Volume (dB) and Trim settings (as in user screenshot)
        audio_config_row = QHBoxLayout()
        audio_config_row.setSpacing(14)

        # Main music volume (dB)
        lbl_main_vol = QLabel("Main music volume (dB):")
        lbl_main_vol.setStyleSheet("font-weight: 600; color: #1e293b;")
        self.main_vol_db = QDoubleSpinBox()
        self.main_vol_db.setRange(-60.0, 20.0)
        self.main_vol_db.setValue(0.0)
        self.main_vol_db.setSingleStep(0.5)
        self.main_vol_db.setSuffix(" dB")
        self.main_vol_db.setDecimals(1)
        self.main_vol_db.setMinimumHeight(32)
        self.main_vol_db.setFixedWidth(90)
        self.main_vol_db.setToolTip("Âm lượng nhạc chính theo dB. 0.0 dB là âm lượng gốc 100%.")

        # Background volume (dB)
        lbl_bg_vol = QLabel("Background volume (dB):")
        lbl_bg_vol.setStyleSheet("font-weight: 600; color: #1e293b;")
        self.bg_vol_db = QDoubleSpinBox()
        self.bg_vol_db.setRange(-60.0, 20.0)
        self.bg_vol_db.setValue(-18.0)
        self.bg_vol_db.setSingleStep(0.5)
        self.bg_vol_db.setSuffix(" dB")
        self.bg_vol_db.setDecimals(1)
        self.bg_vol_db.setMinimumHeight(32)
        self.bg_vol_db.setFixedWidth(90)
        self.bg_vol_db.setToolTip("Âm lượng nhạc nền / backtrack theo dB. Mặc định -18.0 dB (nhỏ êm vừa phải).")

        # Main music trim start (seconds)
        lbl_trim_start = QLabel("Main music trim start (seconds):")
        lbl_trim_start.setStyleSheet("font-weight: 600; color: #1e293b;")
        self.main_trim_start = QDoubleSpinBox()
        self.main_trim_start.setRange(0.0, 3600.0)
        self.main_trim_start.setValue(0.0)
        self.main_trim_start.setSingleStep(0.5)
        self.main_trim_start.setSuffix(" s")
        self.main_trim_start.setDecimals(1)
        self.main_trim_start.setMinimumHeight(32)
        self.main_trim_start.setFixedWidth(90)
        self.main_trim_start.setToolTip("Cắt bỏ X giây đầu của bài nhạc chính (ví dụ 5.0s để bỏ đoạn dạo đầu).")

        # Main music trim end (seconds)
        lbl_trim_end = QLabel("Main music trim end (seconds):")
        lbl_trim_end.setStyleSheet("font-weight: 600; color: #1e293b;")
        self.main_trim_end = QDoubleSpinBox()
        self.main_trim_end.setRange(0.0, 3600.0)
        self.main_trim_end.setValue(0.0)
        self.main_trim_end.setSingleStep(0.5)
        self.main_trim_end.setSuffix(" s")
        self.main_trim_end.setDecimals(1)
        self.main_trim_end.setMinimumHeight(32)
        self.main_trim_end.setFixedWidth(90)
        self.main_trim_end.setToolTip("Cắt bỏ X giây cuối của bài nhạc chính (ví dụ 5.0s để bỏ đoạn outro).")

        audio_config_row.addWidget(lbl_main_vol)
        audio_config_row.addWidget(self.main_vol_db)
        audio_config_row.addWidget(lbl_bg_vol)
        audio_config_row.addWidget(self.bg_vol_db)
        audio_config_row.addWidget(lbl_trim_start)
        audio_config_row.addWidget(self.main_trim_start)
        audio_config_row.addWidget(lbl_trim_end)
        audio_config_row.addWidget(self.main_trim_end)
        audio_config_row.addStretch()
        settings_layout.addLayout(audio_config_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(10)

        self.auto_pair_btn = QPushButton("⚡ Tự động quét & Ghép cặp từ trên xuống")
        self.auto_pair_btn.setMinimumHeight(38)
        self.auto_pair_btn.setStyleSheet(
            "QPushButton { background: #2563eb; color: #ffffff; font-weight: bold; font-size: 13px; border-radius: 6px; padding: 0 18px; } "
            "QPushButton:hover { background: #1d4ed8; }"
        )
        self.auto_pair_btn.clicked.connect(self._auto_pair)

        self.add_manual_btn = QPushButton("➕ Thêm 1 cặp video/nhạc lẻ")
        self.add_manual_btn.setMinimumHeight(38)
        self.add_manual_btn.clicked.connect(self.add_job)

        action_row.addWidget(self.auto_pair_btn)
        action_row.addWidget(self.add_manual_btn)

        hint_label = QLabel("💡 Mẹo: Click đúp vào ô 'Nhạc chính' hoặc 'Nhạc backtrack' (hoặc click chuột phải vào hàng) để chọn riêng bài nhạc cho video đó.")
        hint_label.setStyleSheet("color: #047857; font-size: 12px; font-weight: 500; margin-left: 8px;")
        action_row.addWidget(hint_label)
        action_row.addStretch()
        settings_layout.addLayout(action_row)

        root.addWidget(settings_group)

        # Card 3: Điều khiển hàng đợi (Queue Controls)
        controls_group = QGroupBox("🚀 Điều Khiển Render & Tăng Tốc Phần Cứng")
        controls_vbox = QVBoxLayout(controls_group)
        controls_vbox.setSpacing(8)

        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(10)

        self.start_btn = QPushButton("▶ Bắt đầu Render Hàng Đợi")
        self.start_btn.setObjectName("primary")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setStyleSheet(
            "QPushButton { background: #16a34a; border: 1.5px solid #15803d; color: #ffffff; font-size: 13px; font-weight: bold; border-radius: 6px; padding: 0 18px; } "
            "QPushButton:hover { background: #15803d; } "
            "QPushButton:disabled { background: #e2e8f0; border-color: #cbd5e1; color: #94a3b8; }"
        )
        self.start_btn.clicked.connect(self.start_queue)

        self.stop_btn = QPushButton("⏹ Hủy tác vụ đang chạy")
        self.stop_btn.setMinimumHeight(38)
        self.stop_btn.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #f87171; color: #991b1b; font-weight: 600; border-radius: 6px; padding: 0 14px; } "
            "QPushButton:hover { background: #fecaca; } "
            "QPushButton:disabled { background: #f8fafc; border-color: #e2e8f0; color: #cbd5e1; }"
        )
        self.stop_btn.clicked.connect(self.cancel_all)

        self.remove_btn = QPushButton("🗑 Xóa mục đã chọn")
        self.remove_btn.setMinimumHeight(36)
        self.remove_btn.clicked.connect(self.remove_selected)

        self.clear_done_btn = QPushButton("🧹 Xóa mục hoàn tất")
        self.clear_done_btn.setMinimumHeight(36)
        self.clear_done_btn.clicked.connect(self.clear_completed)

        self.clear_all_btn = QPushButton("❌ Xóa tất cả")
        self.clear_all_btn.setMinimumHeight(36)
        self.clear_all_btn.clicked.connect(self.clear_all)

        ctrl_row.addWidget(self.start_btn)
        ctrl_row.addWidget(self.stop_btn)
        ctrl_row.addWidget(self.remove_btn)
        ctrl_row.addWidget(self.clear_done_btn)
        ctrl_row.addWidget(self.clear_all_btn)
        ctrl_row.addStretch()

        options_row = QHBoxLayout()
        options_row.setSpacing(10)

        options_row.addWidget(QLabel("Bộ mã hóa (GPU/CPU):"))
        self.encoder_combo = QComboBox()
        self.encoder_combo.setMinimumHeight(34)
        self.encoder_combo.addItem("Auto (GPU tự động siêu tốc - Copy / QSV / NVENC)", "Auto")
        self.encoder_combo.addItem("Direct Stream Copy (Siêu tốc như VideoMusicLooper - Không lag)", "copy")
        self.encoder_combo.addItem("Intel QSV (Intel Arc / QuickSync GPU)", "Intel QSV")
        self.encoder_combo.addItem("H264 NVENC (NVIDIA GPU Siêu tốc P1)", "H264 NVENC")
        self.encoder_combo.addItem("libx264 (CPU - Siêu nhanh Ultrafast)", "libx264")
        self.encoder_combo.addItem("Windows Media Foundation", "Windows Media Foundation")
        self.encoder_combo.setToolTip("Mặc định Auto sẽ dùng cơ chế Direct Stream Copy giống hệt VideoMusicLooper (không nén lại video, render 30p-1h chỉ 5-10s và 0% lag). Hoặc chọn thẳng Direct Stream Copy.")
        options_row.addWidget(self.encoder_combo)

        options_row.addWidget(QLabel("Số tác vụ đồng thời:"))
        self.concurrency = QComboBox()
        self.concurrency.setMinimumHeight(34)
        self.concurrency.addItems(["1", "2", "3", "4"])
        self.concurrency.setToolTip("Mặc định 1 để ổn định GPU; tăng lên 2-4 nếu máy có phần cứng mạnh.")
        options_row.addWidget(self.concurrency)
        options_row.addStretch()

        controls_vbox.addLayout(ctrl_row)
        controls_vbox.addLayout(options_row)
        root.addWidget(controls_group)

        # Card 4: Bảng hàng đợi
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)

        # Enable double-click to edit audio/video and context menu
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        root.addWidget(self.table, 1)

        self.summary = QLabel("Hàng đợi trống. Hãy chọn folder video và nhạc rồi bấm Ghép cặp.")
        self.summary.setStyleSheet("color: #475569; font-size: 13px; font-weight: 600; padding: 4px;")
        root.addWidget(self.summary)

    @property
    def running(self) -> bool:
        return any(worker.running for worker in self.workers)

    def _choose_video_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục Video", self.video_folder.text() or "")
        if folder:
            self.video_folder.setText(folder)
            files = scan_folder(folder, VIDEO_EXTS)
            self.video_count.setText(f"{len(files)} video")

    def _choose_audio_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục Nhạc chính", self.audio_folder.text() or "")
        if folder:
            self.audio_folder.setText(folder)
            files = scan_folder(folder, AUDIO_EXTS)
            self.audio_count.setText(f"{len(files)} bài nhạc")

    def _choose_backtrack_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục Nhạc Backtrack", self.backtrack_folder.text() or "")
        if folder:
            self.backtrack_folder.setText(folder)
            files = scan_folder(folder, AUDIO_EXTS)
            self.backtrack_count.setText(f"{len(files)} bài backtrack")

    def _clear_backtrack_folder(self) -> None:
        self.backtrack_folder.clear()
        self.backtrack_count.setText("0 bài backtrack")

    def _choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục Lưu Video", self.output_folder.text() or "")
        if folder:
            self.output_folder.setText(folder)
            self.settings.last_output_folder = folder

    def _duration_mode_changed(self) -> None:
        val = self.duration_combo.currentData()
        if val == "audio":
            self.duration_input.setEnabled(False)
        elif val == "custom":
            self.duration_input.setEnabled(True)
            self.duration_input.setFocus()
            self.duration_input.selectAll()
        else:
            self.duration_input.setEnabled(True)
            self.duration_input.setText(str(val))

    def _on_duration_input_edited(self, _text: str) -> None:
        idx = self.duration_combo.findData("custom")
        if idx >= 0 and self.duration_combo.currentIndex() != idx:
            self.duration_combo.blockSignals(True)
            self.duration_combo.setCurrentIndex(idx)
            self.duration_combo.blockSignals(False)

    def _get_current_duration_label(self) -> str:
        mode = self.duration_combo.currentData()
        if mode == "audio":
            return "Theo nhạc"
        raw = self.duration_input.text().strip()
        dur_mode, seconds = parse_duration_string(raw)
        if dur_mode == "audio":
            return "Theo nhạc"
        return format_seconds_to_hhmmss(seconds)

    def _auto_pair(self) -> None:
        video_dir = self.video_folder.text().strip()
        audio_dir = self.audio_folder.text().strip()
        backtrack_dir = self.backtrack_folder.text().strip()
        out_dir = self.output_folder.text().strip() or self.settings.last_output_folder

        if not video_dir or not Path(video_dir).is_dir():
            QMessageBox.warning(self, "Thiếu thư mục video", "Hãy chọn thư mục chứa các file video hợp lệ.")
            return
        if not audio_dir or not Path(audio_dir).is_dir():
            QMessageBox.warning(self, "Thiếu thư mục nhạc", "Hãy chọn thư mục chứa các file nhạc hợp lệ.")
            return
        if not out_dir:
            out_dir = str(Path(video_dir) / "output_render")
            self.output_folder.setText(out_dir)

        videos = scan_folder(video_dir, VIDEO_EXTS)
        audios = scan_folder(audio_dir, AUDIO_EXTS)
        backtracks = scan_folder(backtrack_dir, AUDIO_EXTS) if backtrack_dir and Path(backtrack_dir).is_dir() else []

        self.video_count.setText(f"{len(videos)} video")
        self.audio_count.setText(f"{len(audios)} bài nhạc")
        if backtracks:
            self.backtrack_count.setText(f"{len(backtracks)} bài backtrack")

        if not videos:
            QMessageBox.warning(self, "Không tìm thấy video", f"Không tìm thấy file video nào trong thư mục:\n{video_dir}")
            return
        if not audios:
            QMessageBox.warning(self, "Không tìm thấy nhạc", f"Không tìm thấy file nhạc nào trong thư mục:\n{audio_dir}")
            return

        loop_mismatch = self.loop_mismatch.isChecked()
        total_pairs = max(len(videos), len(audios)) if loop_mismatch else min(len(videos), len(audios))

        dur_label = self._get_current_duration_label()
        enc_choice = self.encoder_combo.currentText()

        added = 0
        seen_names: dict[str, int] = {}
        for i in range(total_pairs):
            v_file = videos[i % len(videos)]
            a_file = audios[i % len(audios)]
            b_file = backtracks[i % len(backtracks)] if backtracks else None

            base_name = f"{a_file.stem}.mp4"
            if base_name in seen_names:
                seen_names[base_name] += 1
                out_name = f"{a_file.stem}_{seen_names[base_name]}.mp4"
            else:
                seen_names[base_name] = 1
                out_name = base_name

            row = self.table.rowCount()
            self.table.insertRow(row)

            self._set_item(row, 0, str(row + 1))
            self._set_item(row, 1, v_file.name, str(v_file))
            self._set_item(row, 2, a_file.name, str(a_file))
            if b_file:
                self._set_item(row, 3, b_file.name, str(b_file))
            else:
                self._set_item(row, 3, "(Không có)", "")
            self._set_item(row, 4, out_dir, out_dir)
            self._set_item(row, 5, out_name)
            self._set_item(row, 6, dur_label)
            self._set_item(row, 7, enc_choice)
            self._set_item(row, 8, "Pending")
            self._set_item(row, 9, "0%")
            self._set_item(row, 10, "—")
            added += 1

        self._update_summary()
        msg_text = (
            f"Đã bắt cặp thành công {added} tác vụ vào hàng đợi!\n"
            f"• Video: {len(videos)} file\n"
            f"• Nhạc chính: {len(audios)} file\n"
        )
        if backtracks:
            msg_text += f"• Nhạc backtrack: {len(backtracks)} file (đã tự ghép kèm theo)\n"
        else:
            msg_text += "• Nhạc backtrack: Không có (chỉ ghép video + nhạc chính)\n"
        msg_text += (
            f"• Thời lượng chung: {dur_label}\n"
            f"• Bộ mã hóa: {enc_choice}\n"
            f"• Âm lượng: Nhạc chính {self.main_vol_db.value():+.1f}dB, Backtrack {self.bg_vol_db.value():+.1f}dB\n\n"
            f"💡 Mẹo: Có thể click đúp vào bất kỳ ô 'Nhạc chính' hoặc 'Nhạc backtrack' nào để chọn riêng bài nhạc khác cho video đó!\n"
            f"Bấm '▶ Bắt đầu Render Hàng Đợi' để chạy tự động."
        )
        QMessageBox.information(self, "Đã ghép cặp thành công", msg_text)

    def add_job(self) -> None:
        video, _ = QFileDialog.getOpenFileName(self, "Chọn file Video", "", VIDEO_FILTER)
        if not video:
            return
        audio, _ = QFileDialog.getOpenFileName(self, "Chọn file Nhạc chính", str(Path(video).parent), AUDIO_FILTER)
        if not audio:
            return
        backtrack, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn file Nhạc backtrack (Tùy chọn - Bấm Cancel nếu không có)",
            self.backtrack_folder.text() or str(Path(audio).parent),
            AUDIO_FILTER,
        )
        folder = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục Lưu", self.output_folder.text() or self.settings.last_output_folder or str(Path(video).parent)
        )
        if not folder:
            return

        dur_label = self._get_current_duration_label()
        enc_choice = self.encoder_combo.currentText()

        row = self.table.rowCount()
        self.table.insertRow(row)
        out_name = f"{Path(audio).stem}.mp4"

        self._set_item(row, 0, str(row + 1))
        self._set_item(row, 1, Path(video).name, video)
        self._set_item(row, 2, Path(audio).name, audio)
        if backtrack:
            self._set_item(row, 3, Path(backtrack).name, backtrack)
        else:
            self._set_item(row, 3, "(Không có)", "")
        self._set_item(row, 4, folder, folder)
        self._set_item(row, 5, out_name)
        self._set_item(row, 6, dur_label)
        self._set_item(row, 7, enc_choice)
        self._set_item(row, 8, "Pending")
        self._set_item(row, 9, "0%")
        self._set_item(row, 10, "—")

        self._update_summary()

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        """Cho phép người dùng click đúp vào dòng để chọn/đổi riêng nhạc chính, backtrack hoặc video."""
        if self.running and self._text(row, 8) == "Rendering":
            QMessageBox.warning(self, "Đang render", "Tác vụ này đang render, không thể đổi file lúc này.")
            return

        if col == 1:
            self._pick_video_for_row(row)
        elif col == 2:
            self._pick_main_audio_for_row(row)
        elif col == 3:
            self._pick_backtrack_for_row(row)

    def _pick_main_audio_for_row(self, row: int) -> None:
        current = self._get_path(row, 2)
        start_dir = str(Path(current).parent) if current and Path(current).is_file() else (self.audio_folder.text() or "")
        new_file, _ = QFileDialog.getOpenFileName(self, f"Chọn bài nhạc chính riêng cho dòng #{row + 1}", start_dir, AUDIO_FILTER)
        if not new_file:
            return
        p = Path(new_file)
        self._set_item(row, 2, p.name, str(p))
        # Update output filename to reflect new music stem
        self._set_item(row, 5, f"{p.stem}.mp4")
        self._update_summary()

    def _pick_backtrack_for_row(self, row: int) -> None:
        current = self._get_path(row, 3)
        start_dir = (
            str(Path(current).parent)
            if current and Path(current).is_file()
            else (self.backtrack_folder.text() or self.audio_folder.text() or "")
        )
        new_file, _ = QFileDialog.getOpenFileName(self, f"Chọn bài nhạc backtrack riêng cho dòng #{row + 1}", start_dir, AUDIO_FILTER)
        if not new_file:
            return
        p = Path(new_file)
        self._set_item(row, 3, p.name, str(p))
        self._update_summary()

    def _clear_backtrack_for_row(self, row: int) -> None:
        self._set_item(row, 3, "(Không có)", "")
        self._update_summary()

    def _pick_video_for_row(self, row: int) -> None:
        current = self._get_path(row, 1)
        start_dir = str(Path(current).parent) if current and Path(current).is_file() else (self.video_folder.text() or "")
        new_file, _ = QFileDialog.getOpenFileName(self, f"Chọn video riêng cho dòng #{row + 1}", start_dir, VIDEO_FILTER)
        if not new_file:
            return
        p = Path(new_file)
        self._set_item(row, 1, p.name, str(p))
        self._update_summary()

    def _on_table_context_menu(self, pos: QPoint) -> None:
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        menu = QMenu(self)

        act_change_main = menu.addAction("🎵 Chọn / Đổi nhạc chính cho video này...")
        act_change_bg = menu.addAction("🎶 Chọn / Đổi nhạc backtrack cho video này...")
        act_clear_bg = menu.addAction("❌ Xóa nhạc backtrack (dòng này)")
        menu.addSeparator()
        act_change_video = menu.addAction("🎬 Chọn / Đổi video cho dòng này...")
        menu.addSeparator()
        act_delete_row = menu.addAction("🗑️ Xóa dòng này khỏi hàng đợi")

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_change_main:
            self._pick_main_audio_for_row(row)
        elif chosen == act_change_bg:
            self._pick_backtrack_for_row(row)
        elif chosen == act_clear_bg:
            self._clear_backtrack_for_row(row)
        elif chosen == act_change_video:
            self._pick_video_for_row(row)
        elif chosen == act_delete_row:
            if self.running and self._text(row, 8) == "Rendering":
                QMessageBox.warning(self, "Đang render", "Không thể xóa tác vụ đang render.")
                return
            self.table.removeRow(row)
            self._renumber_rows()
            self._update_summary()

    def remove_selected(self) -> None:
        if self.running:
            QMessageBox.warning(self, "Batch đang chạy", "Hãy hủy các job đang chạy trước khi xóa hàng.")
            return
        for row in sorted({item.row() for item in self.table.selectedItems()}, reverse=True):
            self.table.removeRow(row)
        self._renumber_rows()
        self._update_summary()

    def clear_completed(self) -> None:
        if self.running:
            return
        for row in range(self.table.rowCount() - 1, -1, -1):
            if self._text(row, 8) in {"Done", "Failed", "Canceled"}:
                self.table.removeRow(row)
        self._renumber_rows()
        self._update_summary()

    def clear_all(self) -> None:
        if self.running:
            QMessageBox.warning(self, "Batch đang chạy", "Hãy hủy các job đang chạy trước khi xóa toàn bộ.")
            return
        self.table.setRowCount(0)
        self._update_summary()

    def cancel_all(self) -> None:
        self.queue.clear()
        for worker in tuple(self.active):
            worker.cancel()
        self._update_summary()

    def start_queue(self) -> None:
        if self.running:
            return
        self.queue = [
            row
            for row in range(self.table.rowCount())
            if self._text(row, 8) in {"Pending", "Failed", "Canceled"}
        ]
        if not self.queue:
            QMessageBox.information(self, "Hàng đợi", "Không có tác vụ nào đang chờ xử lý.")
            return
        self.max_concurrent = int(self.concurrency.currentText())
        self._pump()

    def _pump(self) -> None:
        while self.queue and len(self.active) < self.max_concurrent:
            self._start_one(self.queue.pop(0))
        if not self.queue and not self.active:
            self._update_summary("Batch hoàn tất tất cả tác vụ!")

    def _start_one(self, row: int) -> None:
        video_path = self._get_path(row, 1)
        audio_path = self._get_path(row, 2)
        backtrack_path = self._get_path(row, 3)
        if backtrack_path in {"(Không có)", "none", "None", ""}:
            backtrack_path = ""
        out_folder = self._get_path(row, 4)
        out_name = self._text(row, 5)
        duration_text = self._text(row, 6)
        enc_text = self._text(row, 7)

        dur_mode, cust_dur = parse_duration_string(duration_text)

        project = LoopProject(
            video=video_path,
            main_audio=audio_path,
            background_audio=backtrack_path,
            output_folder=out_folder,
            output_name=out_name,
            main_volume_db=float(self.main_vol_db.value()),
            background_volume_db=float(self.bg_vol_db.value()),
            main_trim_start=float(self.main_trim_start.value()),
            main_trim_end=float(self.main_trim_end.value()),
            encoder=enc_text or "Auto",
            duration_mode=dur_mode,
            custom_duration=cust_dur,
            seamless_video=True,
        )

        try:
            job = build_loop_job(project, self.settings)
            self._set(row, 8, "Rendering")
            worker = RenderWorker(self)
            self.workers.append(worker)
            self.active[worker] = row
            worker.progress.connect(lambda value, worker=worker: self._progress(worker, value))
            worker.finished.connect(lambda output, log, worker=worker: self._finished(worker, output, log))
            worker.failed.connect(lambda message, log, worker=worker: self._failed(worker, message, log))
            worker.canceled.connect(lambda worker=worker: self._canceled(worker))
            worker.start(job)
            self._update_summary()
        except Exception as exc:
            self._set(row, 8, "Failed")
            self._set(row, 10, str(exc))
            self._update_summary()
            self._pump()

    def _progress(self, worker: RenderWorker, value) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 9, f"{value.percent:.1f}%")
            if value.speed > 0:
                remaining = max(0, value.current_seconds * (100 / max(value.percent, 0.001) - 1) / value.speed)
                self._set(row, 10, f"{remaining:.0f}s còn lại ({value.speed:.1f}x)")

    def _finished(self, worker: RenderWorker, output: str, _log: str) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 8, "Done")
            self._set(row, 9, "100%")
            self._set(row, 10, Path(output).name)
        self._release(worker)
        self._update_summary()
        self._pump()

    def _failed(self, worker: RenderWorker, message: str, log_path: str) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 8, "Failed")
            self._set(row, 10, message)
        show_error(self, "Batch job thất bại", message, log_path)
        self._release(worker)
        self._update_summary()
        self._pump()

    def _canceled(self, worker: RenderWorker) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 8, "Canceled")
        self._release(worker)
        self._update_summary("Queue đã dừng theo yêu cầu")

    def _release(self, worker: RenderWorker) -> None:
        self.active.pop(worker, None)
        worker.deleteLater()

    def _renumber_rows(self) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                item.setText(str(row + 1))

    def _update_summary(self, extra_message: str = "") -> None:
        total = self.table.rowCount()
        if total == 0:
            self.summary.setText("Hàng đợi trống. Hãy chọn folder video và nhạc rồi bấm Ghép cặp.")
            return

        pending = sum(1 for r in range(total) if self._text(r, 8) == "Pending")
        running = sum(1 for r in range(total) if self._text(r, 8) == "Rendering")
        done = sum(1 for r in range(total) if self._text(r, 8) == "Done")
        failed = sum(1 for r in range(total) if self._text(r, 8) in {"Failed", "Canceled"})

        msg = f"📊 Tổng cộng: {total} job(s) | ⏳ Chờ: {pending} | 🚀 Đang render: {running} | ✅ Hoàn tất: {done} | ❌ Lỗi/Hủy: {failed}"
        if extra_message:
            msg += f" • {extra_message}"
        self.summary.setText(msg)

    def _set_item(self, row: int, col: int, text: str, user_data: str | None = None) -> None:
        item = QTableWidgetItem(text)
        if user_data is not None:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
            item.setToolTip(f"{user_data}\n(💡 Click đúp hoặc chuột phải để đổi file này)")
        elif col in (1, 2, 3):
            item.setToolTip(f"{text}\n(💡 Click đúp hoặc chuột phải để đổi file này)")
        self.table.setItem(row, col, item)

    def _get_path(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        if not item:
            return ""
        data = item.data(Qt.ItemDataRole.UserRole)
        return str(data) if data is not None else item.text().strip()

    def _text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _set(self, row: int, col: int, value: str) -> None:
        item = self.table.item(row, col)
        if item:
            item.setText(value)
        else:
            self.table.setItem(row, col, QTableWidgetItem(value))
