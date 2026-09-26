from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from models.loop_project import LoopProject
from models.settings_model import AppSettings
from render.ffmpeg import build_loop_job
from render.render_worker import RenderWorker
from ui.common import AUDIO_FILTER, VIDEO_FILTER, show_error


class BatchRenderPage(QWidget):
    COLUMNS = ["Video", "Nhạc chính", "Nhạc nền", "Thư mục lưu", "Tên đầu ra", "Trạng thái", "Tiến độ", "Còn lại"]

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.queue: list[int] = []
        self.workers: list[RenderWorker] = []
        self.active: dict[RenderWorker, int] = {}
        self.max_concurrent = 1
        root = QVBoxLayout(self)
        title = QLabel("Render hàng loạt")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        controls = QHBoxLayout()
        add = QPushButton("Thêm tác vụ")
        remove = QPushButton("Xóa mục đã chọn")
        clear = QPushButton("Xóa mục hoàn tất")
        self.concurrency = QComboBox()
        self.concurrency.addItems(["1", "2", "3"])
        self.concurrency.setToolTip("Mặc định 1 để ổn định NVENC/RAM; chỉ tăng khi GPU đủ tài nguyên.")
        start = QPushButton("Chạy hàng đợi")
        start.setObjectName("primary")
        stop = QPushButton("Hủy tác vụ đang chạy")
        add.clicked.connect(self.add_job)
        remove.clicked.connect(self.remove_selected)
        clear.clicked.connect(self.clear_completed)
        start.clicked.connect(self.start_queue)
        stop.clicked.connect(self.cancel_all)
        controls.addWidget(add)
        controls.addWidget(remove)
        controls.addWidget(clear)
        controls.addStretch()
        controls.addWidget(QLabel("Số tác vụ đồng thời"))
        controls.addWidget(self.concurrency)
        controls.addWidget(stop)
        controls.addWidget(start)
        root.addLayout(controls)
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table, 1)
        self.summary = QLabel("Queue trống")
        self.summary.setObjectName("muted")
        root.addWidget(self.summary)

    @property
    def running(self) -> bool:
        return any(worker.running for worker in self.workers)

    def cancel_all(self) -> None:
        self.queue.clear()
        for worker in tuple(self.active):
            worker.cancel()

    def add_job(self) -> None:
        video, _ = QFileDialog.getOpenFileName(self, "Video", "", VIDEO_FILTER)
        if not video:
            return
        audio, _ = QFileDialog.getOpenFileName(self, "Main music", str(Path(video).parent), AUDIO_FILTER)
        if not audio:
            return
        background, _ = QFileDialog.getOpenFileName(self, "Background audio (Cancel để bỏ qua)", str(Path(audio).parent), AUDIO_FILTER)
        folder = QFileDialog.getExistingDirectory(self, "Output folder", self.settings.last_output_folder or str(Path(video).parent))
        if not folder:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [video, audio, background, folder, f"final_{row + 1}.mp4", "Pending", "0%", "—"]
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(value))
        self.summary.setText(f"{self.table.rowCount()} job(s)")

    def remove_selected(self) -> None:
        if self.running:
            QMessageBox.warning(self, "Batch đang chạy", "Hãy cancel job hiện tại trước khi xóa hàng.")
            return
        for row in sorted({item.row() for item in self.table.selectedItems()}, reverse=True):
            self.table.removeRow(row)

    def clear_completed(self) -> None:
        if self.running:
            return
        for row in range(self.table.rowCount() - 1, -1, -1):
            if self._text(row, 5) in {"Done", "Failed", "Canceled"}:
                self.table.removeRow(row)

    def start_queue(self) -> None:
        if self.running:
            return
        self.queue = [row for row in range(self.table.rowCount()) if self._text(row, 5) in {"Pending", "Failed", "Canceled"}]
        if not self.queue:
            QMessageBox.information(self, "Batch", "Không có job đang chờ.")
            return
        self.max_concurrent = int(self.concurrency.currentText())
        self._pump()

    def _pump(self) -> None:
        while self.queue and len(self.active) < self.max_concurrent:
            self._start_one(self.queue.pop(0))
        if not self.queue and not self.active:
            self.summary.setText("Batch hoàn tất")

    def _start_one(self, row: int) -> None:
        project = LoopProject(
            video=self._text(row, 0), main_audio=self._text(row, 1), background_audio=self._text(row, 2),
            output_folder=self._text(row, 3), output_name=self._text(row, 4), encoder=self.settings.encoder,
        )
        try:
            job = build_loop_job(project, self.settings)
            self._set(row, 5, "Rendering")
            worker = RenderWorker(self)
            self.workers.append(worker)
            self.active[worker] = row
            worker.progress.connect(lambda value, worker=worker: self._progress(worker, value))
            worker.finished.connect(lambda output, log, worker=worker: self._finished(worker, output, log))
            worker.failed.connect(lambda message, log, worker=worker: self._failed(worker, message, log))
            worker.canceled.connect(lambda worker=worker: self._canceled(worker))
            worker.start(job)
        except Exception as exc:
            self._set(row, 5, "Failed")
            self._set(row, 7, str(exc))
            self._pump()

    def _progress(self, worker: RenderWorker, value) -> None:
        row = self.active.get(worker, -1)
        if row >= 0:
            self._set(row, 6, f"{value.percent:.1f}%")
            if value.speed > 0:
                remaining = max(0, value.current_seconds * (100 / max(value.percent, .001) - 1) / value.speed)
                self._set(row, 7, f"{remaining / 60:.1f} min")

    def _finished(self, worker: RenderWorker, output: str, _log: str) -> None:
        row = self.active.get(worker, -1)
        self._set(row, 5, "Done")
        self._set(row, 6, "100%")
        self._set(row, 7, output)
        self._release(worker)
        self._pump()

    def _failed(self, worker: RenderWorker, message: str, log_path: str) -> None:
        row = self.active.get(worker, -1)
        self._set(row, 5, "Failed")
        self._set(row, 7, message)
        show_error(self, "Batch job thất bại", message, log_path)
        self._release(worker)
        self._pump()

    def _canceled(self, worker: RenderWorker) -> None:
        row = self.active.get(worker, -1)
        self._set(row, 5, "Canceled")
        self._release(worker)
        if not self.active:
            self.summary.setText("Queue đã dừng")

    def _release(self, worker: RenderWorker) -> None:
        self.active.pop(worker, None)
        worker.deleteLater()

    def _text(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text().strip() if item else ""

    def _set(self, row: int, column: int, value: str) -> None:
        item = self.table.item(row, column)
        if item:
            item.setText(value)
        else:
            self.table.setItem(row, column, QTableWidgetItem(value))
