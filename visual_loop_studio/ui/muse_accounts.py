from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
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
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from auth.muse_login import MUSE_START_URL, MuseAccountStore, validate_muse_start_url
from auth.muse_sessions import MUSE_SESSION_COUNT, MuseSessionManager, MuseSessionState, get_muse_session_manager
from auth.muse_video_batch import (
    MuseVideoBatchManager,
    MuseVideoSettings,
    MuseVideoWorkerSnapshot,
    MuseVideoWorkerState,
    get_muse_video_batch_manager,
)
from models.settings_model import AppSettings


SESSION_LABELS = {
    MuseSessionState.IDLE: "Chưa mở",
    MuseSessionState.OPENING: "Đang mở Chrome",
    MuseSessionState.LOGIN_REQUIRED: "Cần đăng nhập thủ công",
    MuseSessionState.READY: "READY",
    MuseSessionState.SENDING: "Đang gửi",
    MuseSessionState.GENERATING: "Đang tạo",
    MuseSessionState.COMPLETED: "Hoàn tất",
    MuseSessionState.FAILED: "Lỗi",
    MuseSessionState.STOPPING: "Đang dừng",
}

WORKER_LABELS = {
    MuseVideoWorkerState.IDLE: "Chưa phân bổ",
    MuseVideoWorkerState.LOGIN_REQUIRED: "Cần đăng nhập",
    MuseVideoWorkerState.READY: "Sẵn sàng",
    MuseVideoWorkerState.RUNNING: "Đang tạo video",
    MuseVideoWorkerState.PAUSED: "Đã tạm dừng",
    MuseVideoWorkerState.STOPPING: "Đang dừng",
    MuseVideoWorkerState.QUOTA_EXHAUSTED: "Hết quota/rate limit",
    MuseVideoWorkerState.FAILED: "Lỗi",
    MuseVideoWorkerState.COMPLETED: "Hoàn tất hàng đợi",
}


@dataclass
class _WorkerWidgets:
    account: QComboBox
    login_state: QLabel
    profile: QLabel
    assigned_count: QLabel
    allocation: QPlainTextEdit
    current_image: QLabel
    progress: QProgressBar
    stats: QLabel
    error: QLabel
    logs: QPlainTextEdit
    login: QPushButton
    stop: QPushButton
    retry: QPushButton


class MuseAccountsPage(QWidget):
    settings_changed = Signal()

    def __init__(
        self,
        settings: AppSettings,
        parent=None,
        *,
        session_manager: MuseSessionManager | None = None,
        batch_manager: MuseVideoBatchManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = MuseAccountStore()
        self.session_manager = session_manager or get_muse_session_manager(
            start_url=self.settings.muse_start_url or MUSE_START_URL,
        )
        self.batch_manager = batch_manager or get_muse_video_batch_manager(
            session_manager=self.session_manager,
        )
        self._workers: dict[int, _WorkerWidgets] = {}
        self._futures: list[Future] = []
        self._refreshing = False
        self._build_ui()
        self._reload_account_choices()
        self._restore_ui()
        self.refresh()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(400)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start()

    @property
    def busy(self) -> bool:
        return self.session_manager.busy or self.batch_manager.busy

    def shutdown(self) -> None:
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.stop()
        self.batch_manager.shutdown()
        self.session_manager.shutdown()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel("Muse AI — Batch ảnh thành video trên 3 tài khoản")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        notice = QLabel(
            "Mỗi tài khoản dùng một Chrome profile/driver riêng. Tool không nhập mật khẩu, không vượt CAPTCHA, "
            "2FA, quota hoặc rate limit; job đã gửi sẽ không tự bấm Generate lần hai."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        root.addWidget(notice)

        source_group = QGroupBox("Nguồn ảnh và prompt chung")
        source_grid = QGridLayout(source_group)
        self.url = QLineEdit(self.settings.muse_start_url or MUSE_START_URL)
        self.url.editingFinished.connect(self._save_url)
        self.image_folder = QLineEdit()
        self.image_folder.setPlaceholderText("Thư mục chứa JPG, JPEG, PNG hoặc WEBP")
        choose_images = QPushButton("Chọn thư mục ảnh")
        choose_images.clicked.connect(self._choose_image_folder)
        self.recursive = QCheckBox("Quét thư mục con")
        self.image_count = QLabel("Chưa quét ảnh")
        self.image_count.setObjectName("muted")
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Prompt tạo video chung — được snapshot khi bắt đầu batch")
        self.prompt.setMaximumHeight(90)
        self.output_folder = QLineEdit(self.settings.last_output_folder)
        self.output_folder.setPlaceholderText("Thư mục lưu video MP4")
        choose_output = QPushButton("Chọn output")
        choose_output.clicked.connect(self._choose_output_folder)
        source_grid.addWidget(QLabel("MUSE_URL"), 0, 0)
        source_grid.addWidget(self.url, 0, 1, 1, 3)
        source_grid.addWidget(QLabel("Thư mục ảnh"), 1, 0)
        source_grid.addWidget(self.image_folder, 1, 1)
        source_grid.addWidget(choose_images, 1, 2)
        source_grid.addWidget(self.recursive, 1, 3)
        source_grid.addWidget(self.image_count, 2, 1, 1, 3)
        source_grid.addWidget(QLabel("Prompt tạo video chung"), 3, 0)
        source_grid.addWidget(self.prompt, 3, 1, 1, 3)
        source_grid.addWidget(QLabel("Thư mục output"), 4, 0)
        source_grid.addWidget(self.output_folder, 4, 1, 1, 2)
        source_grid.addWidget(choose_output, 4, 3)
        root.addWidget(source_group)

        settings_group = QGroupBox("Cài đặt video chung (để trống = Muse mặc định)")
        settings_row = QHBoxLayout(settings_group)
        self.model = self._editable_combo(("",))
        self.aspect_ratio = self._editable_combo(("", "16:9", "9:16", "1:1"))
        self.duration = self._editable_combo(("", "5s", "8s", "10s"))
        self.resolution = self._editable_combo(("", "720p", "1080p"))
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 1)
        self.quantity.setValue(1)
        for label, widget in (
            ("Model", self.model),
            ("Tỷ lệ", self.aspect_ratio),
            ("Độ dài", self.duration),
            ("Độ phân giải", self.resolution),
            ("Video/ảnh", self.quantity),
        ):
            settings_row.addWidget(QLabel(label))
            settings_row.addWidget(widget)
        root.addWidget(settings_group)

        actions = QHBoxLayout()
        self.allocate = QPushButton("Phân bổ ảnh")
        self.start_all = QPushButton("Bắt đầu cả 3")
        self.start_all.setObjectName("primary")
        self.stop_all = QPushButton("Dừng tất cả")
        self.resume = QPushButton("Tiếp tục")
        self.redistribute = QPushButton("Phân bổ lại ảnh chưa gửi")
        self.clear = QPushButton("Xóa dữ liệu UI")
        self.allocate.clicked.connect(self._allocate)
        self.start_all.clicked.connect(self._start_all)
        self.stop_all.clicked.connect(self._stop_all)
        self.resume.clicked.connect(self._resume)
        self.redistribute.clicked.connect(self._redistribute)
        self.clear.clicked.connect(self._clear)
        for button in (
            self.allocate,
            self.start_all,
            self.stop_all,
            self.resume,
            self.redistribute,
            self.clear,
        ):
            actions.addWidget(button)
        root.addLayout(actions)
        self.global_status = QLabel("Đăng nhập đủ 3 tài khoản, phân bổ ảnh, sau đó bắt đầu.")
        self.global_status.setWordWrap(True)
        self.global_status.setObjectName("muted")
        root.addWidget(self.global_status)

        self.tabs = QTabWidget()
        for worker_id in range(1, MUSE_SESSION_COUNT + 1):
            page, widgets = self._build_worker_tab(worker_id)
            self._workers[worker_id] = widgets
            self.tabs.addTab(page, f"Tài khoản {worker_id}")
        root.addWidget(self.tabs, 1)

    def _build_worker_tab(self, worker_id: int) -> tuple[QWidget, _WorkerWidgets]:
        page = QWidget()
        root = QVBoxLayout(page)
        account_group = QGroupBox(f"Tài khoản Muse {worker_id}")
        grid = QGridLayout(account_group)
        account = QComboBox()
        account.setEditable(True)
        account.lineEdit().setPlaceholderText("Email Google được phép sử dụng")
        login = QPushButton("Đăng nhập")
        stop = QPushButton("Dừng")
        retry = QPushButton("Chạy lại ảnh lỗi")
        login.clicked.connect(lambda _checked=False, value=worker_id: self._login(value))
        stop.clicked.connect(lambda _checked=False, value=worker_id: self._stop_worker(value))
        retry.clicked.connect(lambda _checked=False, value=worker_id: self._retry_failed(value))
        login_state = QLabel("Chưa đăng nhập")
        login_state.setWordWrap(True)
        profile = QLabel()
        profile.setObjectName("muted")
        profile.setWordWrap(True)
        grid.addWidget(QLabel("Email"), 0, 0)
        grid.addWidget(account, 0, 1)
        grid.addWidget(login, 0, 2)
        grid.addWidget(QLabel("Đăng nhập"), 1, 0)
        grid.addWidget(login_state, 1, 1, 1, 2)
        grid.addWidget(QLabel("Profile"), 2, 0)
        grid.addWidget(profile, 2, 1, 1, 2)
        root.addWidget(account_group)

        assigned_count = QLabel("Được phân bổ: 0 ảnh")
        allocation = QPlainTextEdit()
        allocation.setReadOnly(True)
        allocation.setMaximumHeight(110)
        allocation.setPlaceholderText("Danh sách ảnh được phân bổ sẽ hiển thị trước khi chạy.")
        root.addWidget(assigned_count)
        root.addWidget(allocation)

        current_image = QLabel("Ảnh hiện tại: —")
        current_image.setWordWrap(True)
        progress = QProgressBar()
        progress.setRange(0, 100)
        stats = QLabel("Chờ: 0 • Thành công: 0 • Lỗi: 0 • Quota: 0")
        error = QLabel()
        error.setWordWrap(True)
        error.setStyleSheet("color: #fca5a5;")
        root.addWidget(current_image)
        root.addWidget(progress)
        root.addWidget(stats)
        root.addWidget(error)

        button_row = QHBoxLayout()
        button_row.addWidget(stop)
        button_row.addWidget(retry)
        button_row.addStretch()
        root.addLayout(button_row)
        logs = QPlainTextEdit()
        logs.setReadOnly(True)
        logs.setPlaceholderText("Log riêng của worker")
        root.addWidget(QLabel("Log riêng"))
        root.addWidget(logs, 1)
        return page, _WorkerWidgets(
            account=account,
            login_state=login_state,
            profile=profile,
            assigned_count=assigned_count,
            allocation=allocation,
            current_image=current_image,
            progress=progress,
            stats=stats,
            error=error,
            logs=logs,
            login=login,
            stop=stop,
            retry=retry,
        )

    @staticmethod
    def _editable_combo(values: tuple[str, ...]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        for value in values:
            combo.addItem(value or "Muse mặc định", value)
        return combo

    def _restore_ui(self) -> None:
        snapshot = self.batch_manager.snapshot()
        if snapshot.source_paths:
            try:
                common = Path(snapshot.source_paths[0]).parent
                self.image_folder.setText(str(common))
            except Exception:
                pass
        self.prompt.setPlainText(snapshot.prompt)
        if snapshot.output_dir:
            self.output_folder.setText(snapshot.output_dir)
        self.model.setCurrentText(snapshot.settings.model or "Muse mặc định")
        self.aspect_ratio.setCurrentText(snapshot.settings.aspect_ratio or "Muse mặc định")
        self.duration.setCurrentText(snapshot.settings.duration or "Muse mặc định")
        self.resolution.setCurrentText(snapshot.settings.resolution or "Muse mặc định")

    def _reload_account_choices(self) -> None:
        emails = [account.email_label for account in self.store.all()]
        session_snapshots = self.session_manager.snapshots()
        emails.extend(item.email for item in session_snapshots if item.email)
        unique = sorted({item for item in emails if item}, key=str.casefold)
        for worker_id, widgets in self._workers.items():
            selected = widgets.account.currentText().strip() or self.session_manager.snapshot(worker_id).email
            widgets.account.clear()
            widgets.account.addItem("")
            for email in unique:
                widgets.account.addItem(email)
            widgets.account.setCurrentText(selected)

    def _settings_value(self, combo: QComboBox) -> str:
        value = combo.currentText().strip()
        return "" if value == "Muse mặc định" else value

    def _video_settings(self) -> MuseVideoSettings:
        return MuseVideoSettings(
            model=self._settings_value(self.model),
            aspect_ratio=self._settings_value(self.aspect_ratio),
            duration=self._settings_value(self.duration),
            resolution=self._settings_value(self.resolution),
            quantity=self.quantity.value(),
        )

    def _choose_image_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục ảnh", self.image_folder.text())
        if folder:
            self.image_folder.setText(folder)
            self._scan_count()

    def _choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video", self.output_folder.text())
        if folder:
            self.output_folder.setText(folder)
            self.settings.last_output_folder = folder
            self.settings_changed.emit()

    def _scan_count(self) -> list[Path]:
        images = self.batch_manager.scan_images(
            self.image_folder.text().strip(),
            recursive=self.recursive.isChecked(),
        )
        self.image_count.setText(f"Đã tìm thấy {len(images)} ảnh hợp lệ.")
        return images

    def _allocate(self) -> None:
        try:
            images = self._scan_count()
            allocations = self.batch_manager.allocate_images(images)
        except Exception as exc:
            QMessageBox.warning(self, "Không thể phân bổ ảnh", str(exc))
            return
        self.global_status.setText(
            "Đã phân bổ round-robin: " + ", ".join(f"TK {index + 1}: {len(items)}" for index, items in enumerate(allocations))
        )
        self.refresh()

    def _start_all(self) -> None:
        try:
            future = self.batch_manager.start_all(
                self.prompt.toPlainText(),
                self._video_settings(),
                self.output_folder.text().strip(),
            )
            self._track(future)
            self.global_status.setText("Ba worker Muse đã bắt đầu đồng thời.")
        except Exception as exc:
            QMessageBox.warning(self, "Không thể bắt đầu batch Muse", str(exc))

    def _stop_all(self) -> None:
        count = self.batch_manager.stop_all()
        self.global_status.setText(
            f"Đang dừng {count} worker Muse; prompt, tiến độ và kết quả được giữ nguyên. YouTube không bị tác động."
        )

    def _resume(self) -> None:
        try:
            self._track(self.batch_manager.resume())
            self.global_status.setText("Đang tiếp tục các job chưa hoàn tất; job đã gửi không bị gửi lại.")
        except Exception as exc:
            QMessageBox.warning(self, "Không thể tiếp tục", str(exc))

    def _redistribute(self) -> None:
        try:
            allocations = self.batch_manager.redistribute_unsubmitted()
            self.global_status.setText(
                "Đã phân bổ lại ảnh chưa gửi: "
                + ", ".join(f"TK {index + 1}: {len(items)}" for index, items in enumerate(allocations))
            )
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "Không thể phân bổ lại", str(exc))

    def _clear(self) -> None:
        try:
            self.batch_manager.clear_ui_data()
        except Exception as exc:
            QMessageBox.warning(self, "Không thể xóa dữ liệu UI", str(exc))
            return
        self.image_folder.clear()
        self.prompt.clear()
        self.image_count.setText("Chưa quét ảnh")
        self.global_status.setText("Đã xóa dữ liệu UI; lịch sử job vẫn được giữ để chống tạo trùng.")
        self.refresh()

    def _login(self, worker_id: int) -> None:
        email = self._workers[worker_id].account.currentText().strip()
        try:
            self._track(self.session_manager.open_session(worker_id, email, force_relogin=True))
        except Exception as exc:
            QMessageBox.warning(self, f"Tài khoản {worker_id}", str(exc))

    def _stop_worker(self, worker_id: int) -> None:
        if self.batch_manager.stop_worker(worker_id):
            return
        if self.session_manager.stop_session(worker_id):
            return
        self.global_status.setText(f"Tài khoản {worker_id} không có tác vụ đang chạy.")

    def _retry_failed(self, worker_id: int) -> None:
        try:
            count = self.batch_manager.retry_failed(worker_id)
            if count:
                self._track(self.batch_manager.resume())
            self.global_status.setText(f"Tài khoản {worker_id}: chạy lại thủ công {count} ảnh lỗi.")
        except Exception as exc:
            QMessageBox.warning(self, "Không thể chạy lại ảnh lỗi", str(exc))

    def _save_url(self) -> None:
        try:
            value = validate_muse_start_url(self.url.text().strip() or MUSE_START_URL)
        except ValueError as exc:
            QMessageBox.warning(self, "MUSE_URL không hợp lệ", str(exc))
            self.url.setText(self.settings.muse_start_url or MUSE_START_URL)
            return
        if value != self.session_manager.start_url and any(item.driver_open for item in self.session_manager.snapshots()):
            QMessageBox.warning(self, "Muse đang mở", "URL mới sẽ áp dụng sau khi mở lại ứng dụng.")
        self.settings.muse_start_url = value
        self.url.setText(value)
        self.settings_changed.emit()

    def _track(self, future: Future) -> None:
        self._futures.append(future)

    def refresh(self) -> None:
        self._refreshing = True
        try:
            session_values = {item.session_id: item for item in self.session_manager.snapshots()}
            batch = self.batch_manager.snapshot()
            for worker in batch.workers:
                self._render_worker(worker, session_values[worker.worker_id])
            ready = all(
                item.state == MuseSessionState.READY and item.driver_open
                for item in session_values.values()
            )
            self.start_all.setEnabled(ready and bool(batch.source_paths) and not batch.running)
            self.stop_all.setEnabled(batch.running)
            self.resume.setEnabled(bool(batch.jobs) and not batch.running)
            self.allocate.setEnabled(not batch.running)
            self.redistribute.setEnabled(bool(batch.jobs) and not batch.running)
            self.clear.setEnabled(not batch.running)
            self._collect_futures()
        finally:
            self._refreshing = False

    def _render_worker(self, worker: MuseVideoWorkerSnapshot, session) -> None:
        widgets = self._workers[worker.worker_id]
        editor_focused = bool(widgets.account.lineEdit() and widgets.account.lineEdit().hasFocus())
        if session.email and not editor_focused and (
            session.driver_open or session.task_running or not widgets.account.currentText().strip()
        ):
            widgets.account.setCurrentText(session.email)
        if session.email and session.state == MuseSessionState.READY:
            account_status = session.email
        elif session.email:
            account_status = f"Mục tiêu: {session.email}"
        else:
            account_status = "Chưa chọn tài khoản"
        widgets.login_state.setText(
            f"{SESSION_LABELS[session.state]} — {account_status} • Worker: {WORKER_LABELS[worker.state]}"
        )
        widgets.profile.setText(session.profile_dir)
        widgets.assigned_count.setText(f"Được phân bổ: {len(worker.assigned)} ảnh")
        allocation_text = "\n".join(Path(value).name for value in worker.assigned)
        if widgets.allocation.toPlainText() != allocation_text:
            widgets.allocation.setPlainText(allocation_text)
        widgets.current_image.setText(
            f"Ảnh hiện tại: {Path(worker.current_image).name if worker.current_image else '—'}"
        )
        widgets.progress.setValue(worker.progress)
        widgets.stats.setText(
            f"Chờ: {worker.pending} • Thành công: {worker.completed} • Lỗi: {worker.failed} • Quota: {worker.quota}"
        )
        widgets.error.setText(worker.error)
        logs = "\n".join(worker.logs)
        if widgets.logs.toPlainText() != logs:
            widgets.logs.setPlainText(logs)
            scrollbar = widgets.logs.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
        widgets.login.setEnabled(not session.task_running and not worker.task_running)
        widgets.stop.setEnabled(session.task_running or worker.task_running)
        widgets.retry.setEnabled(worker.failed > 0 and not worker.task_running)
        widgets.account.setEnabled(not session.driver_open and not session.task_running and not worker.task_running)

    def _collect_futures(self) -> None:
        pending: list[Future] = []
        for future in self._futures:
            if not future.done():
                pending.append(future)
                continue
            try:
                future.result()
            except Exception as exc:
                self.global_status.setText(str(exc))
        self._futures = pending
