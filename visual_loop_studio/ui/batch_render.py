from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
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


class BatchRenderPage(QWidget):
    COLUMNS = [
        "#",
        "Video",
        "Nhạc chính",
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

        title = QLabel("🎬 Render hàng loạt (Tự động bắt cặp Video & Nhạc)")
        title.setObjectName("pageTitle")
        root.addWidget(title)

        # Card 1: Chọn folder nguồn & đầu ra
        folder_group = QGroupBox("📁 Nguồn Video & Nhạc theo Thư mục")
        folder_grid = QGridLayout(folder_group)
        folder_grid.setVerticalSpacing(8)
        folder_grid.setHorizontalSpacing(10)

        self.video_folder = QLineEdit()
        self.video_folder.setMinimumHeight(36)
        self.video_folder.setPlaceholderText("Chọn thư mục chứa các file video (.mp4, .mov, .mkv, .webm...)...")
        self.choose_video_btn = QPushButton("🎬 Chọn folder Video")
        self.choose_video_btn.setMinimumHeight(36)
        self.choose_video_btn.clicked.connect(self._choose_video_folder)
        self.video_count = QLabel("0 video")
        self.video_count.setStyleSheet("color: #2563eb; font-weight: 600;")

        self.audio_folder = QLineEdit()
        self.audio_folder.setMinimumHeight(36)
        self.audio_folder.setPlaceholderText("Chọn thư mục chứa các file nhạc (.mp3, .wav, .aac, .m4a...)...")
        self.choose_audio_btn = QPushButton("🎵 Chọn folder Nhạc")
        self.choose_audio_btn.setMinimumHeight(36)
        self.choose_audio_btn.clicked.connect(self._choose_audio_folder)
        self.audio_count = QLabel("0 bài nhạc")
        self.audio_count.setStyleSheet("color: #2563eb; font-weight: 600;")

        self.output_folder = QLineEdit(self.settings.last_output_folder)
        self.output_folder.setMinimumHeight(36)
        self.output_folder.setPlaceholderText("Chọn thư mục lưu video kết quả...")
        self.choose_output_btn = QPushButton("📁 Chọn folder Lưu")
        self.choose_output_btn.setMinimumHeight(36)
        self.choose_output_btn.clicked.connect(self._choose_output_folder)

        folder_grid.addWidget(QLabel("Folder Video:"), 0, 0)
        folder_grid.addWidget(self.video_folder, 0, 1)
        folder_grid.addWidget(self.choose_video_btn, 0, 2)
        folder_grid.addWidget(self.video_count, 0, 3)

        folder_grid.addWidget(QLabel("Folder Nhạc:"), 1, 0)
        folder_grid.addWidget(self.audio_folder, 1, 1)
        folder_grid.addWidget(self.choose_audio_btn, 1, 2)
        folder_grid.addWidget(self.audio_count, 1, 3)

        folder_grid.addWidget(QLabel("Thư mục lưu:"), 2, 0)
        folder_grid.addWidget(self.output_folder, 2, 1)
        folder_grid.addWidget(self.choose_output_btn, 2, 2)

        root.addWidget(folder_group)

        # Card 2: Cài đặt thời lượng & Tự động bắt cặp
        settings_group = QGroupBox("⏱️ Cài đặt Thời Lượng Chung & Tự Động Bắt Cặp")
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(10)

        config_row = QHBoxLayout()
        config_row.setSpacing(12)

        config_row.addWidget(QLabel("Thời lượng chung:"))
        self.duration_combo = QComboBox()
        self.duration_combo.setMinimumHeight(36)
        self.duration_combo.addItem("Theo độ dài bài nhạc (Chuẩn - Tự động)", "audio")
        self.duration_combo.addItem("30 giây (Shorts / TikTok / Reels)", "30")
        self.duration_combo.addItem("60 giây (1 Phút)", "60")
        self.duration_combo.addItem("90 giây (1.5 Phút)", "90")
        self.duration_combo.addItem("120 giây (2 Phút)", "120")
        self.duration_combo.addItem("180 giây (3 Phút)", "180")
        self.duration_combo.addItem("300 giây (5 Phút)", "300")
        self.duration_combo.addItem("600 giây (10 Phút)", "600")
        self.duration_combo.addItem("Tùy chỉnh số giây...", "custom")
        self.duration_combo.currentIndexChanged.connect(self._duration_mode_changed)
        config_row.addWidget(self.duration_combo)

        self.duration_spin = QSpinBox()
        self.duration_spin.setMinimumHeight(36)
        self.duration_spin.setRange(5, 86400)
        self.duration_spin.setValue(60)
        self.duration_spin.setSuffix(" giây")
        self.duration_spin.setEnabled(False)
        config_row.addWidget(self.duration_spin)

        self.loop_mismatch = QCheckBox("Lặp lại bên ít hơn để ghép hết bên nhiều hơn")
        self.loop_mismatch.setChecked(True)
        self.loop_mismatch.setToolTip(
            "Nếu tick: ví dụ 3 video và 10 bài nhạc thì 3 video sẽ tự lặp lại để tạo đủ 10 video.\n"
            "Nếu bỏ tick: chỉ ghép đến số lượng của bên ít hơn."
        )
        config_row.addWidget(self.loop_mismatch)
        config_row.addStretch()

        settings_layout.addLayout(config_row)

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
        self.encoder_combo.addItem("Auto (GPU tự động siêu tốc)", "Auto")
        self.encoder_combo.addItem("Intel QSV (Intel QuickSync GPU)", "Intel QSV")
        self.encoder_combo.addItem("H264 NVENC (NVIDIA GPU)", "H264 NVENC")
        self.encoder_combo.addItem("Windows Media Foundation", "Windows Media Foundation")
        self.encoder_combo.addItem("libx264 (CPU - Siêu nhanh)", "libx264")
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
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục Nhạc", self.audio_folder.text() or "")
        if folder:
            self.audio_folder.setText(folder)
            files = scan_folder(folder, AUDIO_EXTS)
            self.audio_count.setText(f"{len(files)} bài nhạc")

    def _choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục Lưu Video", self.output_folder.text() or "")
        if folder:
            self.output_folder.setText(folder)
            self.settings.last_output_folder = folder

    def _duration_mode_changed(self) -> None:
        mode = self.duration_combo.currentData()
        if mode == "audio":
            self.duration_spin.setEnabled(False)
        elif mode == "custom":
            self.duration_spin.setEnabled(True)
        else:
            self.duration_spin.setEnabled(False)
            try:
                self.duration_spin.setValue(int(mode))
            except ValueError:
                pass

    def _auto_pair(self) -> None:
        video_dir = self.video_folder.text().strip()
        audio_dir = self.audio_folder.text().strip()
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

        self.video_count.setText(f"{len(videos)} video")
        self.audio_count.setText(f"{len(audios)} bài nhạc")

        if not videos:
            QMessageBox.warning(self, "Không tìm thấy video", f"Không tìm thấy file video nào trong thư mục:\n{video_dir}")
            return
        if not audios:
            QMessageBox.warning(self, "Không tìm thấy nhạc", f"Không tìm thấy file nhạc nào trong thư mục:\n{audio_dir}")
            return

        loop_mismatch = self.loop_mismatch.isChecked()
        total_pairs = max(len(videos), len(audios)) if loop_mismatch else min(len(videos), len(audios))

        dur_mode = self.duration_combo.currentData()
        dur_label = "Theo nhạc" if dur_mode == "audio" else f"{self.duration_spin.value()}s"
        enc_choice = self.encoder_combo.currentText()

        added = 0
        for i in range(total_pairs):
            v_file = videos[i % len(videos)]
            a_file = audios[i % len(audios)]
            out_name = f"final_{i + 1:02d}_{v_file.stem}.mp4"

            row = self.table.rowCount()
            self.table.insertRow(row)

            self._set_item(row, 0, str(row + 1))
            self._set_item(row, 1, v_file.name, str(v_file))
            self._set_item(row, 2, a_file.name, str(a_file))
            self._set_item(row, 3, out_dir, out_dir)
            self._set_item(row, 4, out_name)
            self._set_item(row, 5, dur_label)
            self._set_item(row, 6, enc_choice)
            self._set_item(row, 7, "Pending")
            self._set_item(row, 8, "0%")
            self._set_item(row, 9, "—")
            added += 1

        self._update_summary()
        QMessageBox.information(
            self,
            "Đã ghép cặp thành công",
            f"Đã bắt cặp thành công {added} tác vụ vào hàng đợi!\n"
            f"• Video: {len(videos)} file\n"
            f"• Nhạc: {len(audios)} file\n"
            f"• Thời lượng chung: {dur_label}\n"
            f"• Bộ mã hóa: {enc_choice}\n\n"
            f"Bấm '▶ Bắt đầu Render Hàng Đợi' để chạy tự động.",
        )

    def add_job(self) -> None:
        video, _ = QFileDialog.getOpenFileName(self, "Chọn file Video", "", VIDEO_FILTER)
        if not video:
            return
        audio, _ = QFileDialog.getOpenFileName(self, "Chọn file Nhạc chính", str(Path(video).parent), AUDIO_FILTER)
        if not audio:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục Lưu", self.output_folder.text() or self.settings.last_output_folder or str(Path(video).parent)
        )
        if not folder:
            return

        dur_mode = self.duration_combo.currentData()
        dur_label = "Theo nhạc" if dur_mode == "audio" else f"{self.duration_spin.value()}s"
        enc_choice = self.encoder_combo.currentText()

        row = self.table.rowCount()
        self.table.insertRow(row)
        out_name = f"final_{row + 1:02d}_{Path(video).stem}.mp4"

        self._set_item(row, 0, str(row + 1))
        self._set_item(row, 1, Path(video).name, video)
        self._set_item(row, 2, Path(audio).name, audio)
        self._set_item(row, 3, folder, folder)
        self._set_item(row, 4, out_name)
        self._set_item(row, 5, dur_label)
        self._set_item(row, 6, enc_choice)
        self._set_item(row, 7, "Pending")
        self._set_item(row, 8, "0%")
        self._set_item(row, 9, "—")

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
            if self._text(row, 7) in {"Done", "Failed", "Canceled"}:
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
            if self._text(row, 7) in {"Pending", "Failed", "Canceled"}
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
        out_folder = self._get_path(row, 3)
        out_name = self._text(row, 4)
        duration_text = self._text(row, 5)
        enc_text = self._text(row, 6)

        if duration_text == "Theo nhạc":
            dur_mode = "audio"
            cust_dur = 0.0
        else:
            dur_mode = "custom"
            try:
                cust_dur = float(duration_text.replace("s", "").replace(" giây", "").strip())
            except ValueError:
                cust_dur = 60.0

        project = LoopProject(
            video=video_path,
            main_audio=audio_path,
            background_audio="",
            output_folder=out_folder,
            output_name=out_name,
            encoder=enc_text or "Auto",
            duration_mode=dur_mode,
            custom_duration=cust_dur,
        )

        try:
            job = build_loop_job(project, self.settings)
            self._set(row, 7, "Rendering")
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
            self._set(row, 7, "Failed")
            self._set(row, 9, str(exc))
            self._update_summary()
            self._pump()

    def _progress(self, worker: RenderWorker, value) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 8, f"{value.percent:.1f}%")
            if value.speed > 0:
                remaining = max(0, value.current_seconds * (100 / max(value.percent, 0.001) - 1) / value.speed)
                self._set(row, 9, f"{remaining:.0f}s còn lại ({value.speed:.1f}x)")

    def _finished(self, worker: RenderWorker, output: str, _log: str) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 7, "Done")
            self._set(row, 8, "100%")
            self._set(row, 9, Path(output).name)
        self._release(worker)
        self._update_summary()
        self._pump()

    def _failed(self, worker: RenderWorker, message: str, log_path: str) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 7, "Failed")
            self._set(row, 9, message)
        show_error(self, "Batch job thất bại", message, log_path)
        self._release(worker)
        self._update_summary()
        self._pump()

    def _canceled(self, worker: RenderWorker) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 7, "Canceled")
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

        pending = sum(1 for r in range(total) if self._text(r, 7) == "Pending")
        running = sum(1 for r in range(total) if self._text(r, 7) == "Rendering")
        done = sum(1 for r in range(total) if self._text(r, 7) == "Done")
        failed = sum(1 for r in range(total) if self._text(r, 7) in {"Failed", "Canceled"})

        msg = f"📊 Tổng cộng: {total} job(s) | ⏳ Chờ: {pending} | 🚀 Đang render: {running} | ✅ Hoàn tất: {done} | ❌ Lỗi/Hủy: {failed}"
        if extra_message:
            msg += f" — {extra_message}"
        self.summary.setText(msg)

    def _set_item(self, row: int, col: int, text: str, user_data: str | None = None) -> None:
        item = QTableWidgetItem(text)
        if user_data is not None:
            item.setData(Qt.ItemDataRole.UserRole, user_data)
            item.setToolTip(user_data)
        self.table.setItem(row, col, item)

    def _get_path(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        if not item:
            return ""
        data = item.data(Qt.ItemDataRole.UserRole)
        return str(data) if data else item.text().strip()

    def _text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _set(self, row: int, col: int, value: str) -> None:
        item = self.table.item(row, col)
        if item:
            item.setText(value)
        else:
            self.table.setItem(row, col, QTableWidgetItem(value))
