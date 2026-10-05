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
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from auth.muse_login import MUSE_START_URL, MuseAccountStore, validate_muse_start_url
from auth.muse_sessions import (
    MUSE_SESSION_COUNT,
    MuseSessionManager,
    MuseSessionOpenMode,
    MuseSessionState,
    MuseTabCandidate,
    get_muse_session_manager,
)
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
    password: QLineEdit
    muse_tab: QComboBox
    tab_selection: QLabel
    login_state: QLabel
    profile: QLabel
    assigned_count: QLabel
    allocation: QPlainTextEdit
    current_image: QLabel
    progress: QProgressBar
    stats: QLabel
    error: QLabel
    logs: QPlainTextEdit
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
        self._pending_batch_start: tuple[Future, str, MuseVideoSettings, str] | None = None
        self._pending_browser_open: Future | None = None
        self._pending_tab_scan: Future | None = None
        self._tab_candidates: dict[int, tuple[MuseTabCandidate, ...]] = {}
        self._pending_started_workers: set[int] = set()
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
            "Mỗi tài khoản dùng một Chrome profile/driver riêng. Nếu bạn nhập mật khẩu, tool chỉ điền trên "
            "accounts.google.com, không lưu mật khẩu và không giả User-Agent; CAPTCHA/2FA làm thủ công. "
            "mỗi worker gửi tối đa 3 ảnh với một prompt rồi tải lần lượt đủ 3 video trước lượt tiếp theo; "
            "tool không lưu bí mật và không vượt CAPTCHA, "
            "mã xác minh, 2FA, quota hoặc rate limit."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        root.addWidget(notice)

        connection_group = QGroupBox("Cách kết nối Muse")
        connection_row = QHBoxLayout(connection_group)
        self.connection_mode = QComboBox()
        self.connection_mode.addItem(
            "Phương án 1 — Chrome thường: tự điền Google hoặc đăng nhập tay",
            MuseSessionOpenMode.MANUAL_BROWSER.value,
        )
        self.connection_mode.addItem(
            "Phương án 2 — Chọn tab Muse đang mở",
            MuseSessionOpenMode.EXISTING_TAB.value,
        )
        mode_index = self.connection_mode.findData(
            self.settings.muse_connection_mode or MuseSessionOpenMode.MANUAL_BROWSER.value
        )
        self.connection_mode.setCurrentIndex(max(0, mode_index))
        self.connection_mode.currentIndexChanged.connect(self._connection_mode_changed)
        self.open_browsers = QPushButton("Mở 3 Chrome/Muse")
        self.open_browsers.clicked.connect(self._open_browser_sessions)
        self.scan_tabs = QPushButton("Quét tab Muse đang mở")
        self.scan_tabs.clicked.connect(self._scan_muse_tabs)
        connection_row.addWidget(self.connection_mode, 1)
        connection_row.addWidget(self.open_browsers)
        connection_row.addWidget(self.scan_tabs)
        root.addWidget(connection_group)

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
        self.start_all = QPushButton("Xử lý")
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
        self.start_requirements = QLabel()
        self.start_requirements.setWordWrap(True)
        self.start_requirements.setObjectName("notice")
        root.addWidget(self.start_requirements)

        self.tabs = QTabWidget()
        for worker_id in range(1, MUSE_SESSION_COUNT + 1):
            page, widgets = self._build_worker_tab(worker_id)
            self._workers[worker_id] = widgets
            self.tabs.addTab(page, f"Tài khoản {worker_id}")
        root.addWidget(self.tabs, 1)

    def _build_worker_tab(self, worker_id: int) -> tuple[QWidget, _WorkerWidgets]:
        page = QScrollArea()
        page.setWidgetResizable(True)
        content = QWidget()
        content.setMinimumHeight(520)
        page.setWidget(content)
        root = QVBoxLayout(content)
        account_group = QGroupBox(f"Tài khoản Muse {worker_id}")
        account_group.setMinimumHeight(270)
        grid = QGridLayout(account_group)
        account = QComboBox()
        account.setEditable(True)
        account.setMinimumHeight(36)
        account.lineEdit().setPlaceholderText("Email Google được phép sử dụng")
        password = QLineEdit()
        password.setMinimumHeight(36)
        password.setEchoMode(QLineEdit.EchoMode.Password)
        password.setClearButtonEnabled(True)
        password.setPlaceholderText("Mật khẩu Google (không lưu; có thể để trống để tự đăng nhập)")
        muse_tab = QComboBox()
        muse_tab.setMinimumHeight(36)
        muse_tab.setPlaceholderText("Bấm Quét tab Muse rồi chọn đúng tab của tài khoản này")
        tab_selection = QLabel("Chưa chọn tab Muse")
        tab_selection.setObjectName("muted")
        tab_selection.setWordWrap(True)
        muse_tab.currentIndexChanged.connect(
            lambda _index, value=worker_id: self._tab_selection_changed(value)
        )
        stop = QPushButton("Dừng")
        retry = QPushButton("Chạy lại ảnh lỗi")
        stop.clicked.connect(lambda _checked=False, value=worker_id: self._stop_worker(value))
        retry.clicked.connect(lambda _checked=False, value=worker_id: self._retry_failed(value))
        login_state = QLabel("Chưa đăng nhập")
        login_state.setWordWrap(True)
        profile = QLabel()
        profile.setObjectName("muted")
        profile.setWordWrap(True)
        login_help = QLabel(
            "Phương án 1: nếu nhập mật khẩu, tool chỉ điền email/mật khẩu trên accounts.google.com; "
            "nếu để trống thì bạn tự đăng nhập. CAPTCHA, passkey và 2FA luôn làm thủ công. "
            "Sau khi Google xác minh, tool tự mở Muse và tiếp tục batch; mật khẩu bị xóa khỏi UI ngay khi bàn giao. "
            "Phương án 2: bấm Mở 3 Chrome/Muse, mở sẵn Muse trong từng cửa sổ, bấm Quét tab Muse, "
            "rồi chọn một dòng READY cho mỗi tài khoản. Dòng ĐÃ CHỌN TAB màu xanh là đã chọn xong. "
            "Waitlist vẫn do người dùng xử lý."
        )
        login_help.setObjectName("muted")
        login_help.setWordWrap(True)
        grid.addWidget(QLabel("Email Google"), 0, 0)
        grid.addWidget(account, 0, 1, 1, 2)
        grid.addWidget(QLabel("Mật khẩu Google"), 1, 0)
        grid.addWidget(password, 1, 1, 1, 2)
        grid.addWidget(QLabel("Tab Muse"), 2, 0)
        grid.addWidget(muse_tab, 2, 1, 1, 2)
        grid.addWidget(tab_selection, 3, 1, 1, 2)
        grid.addWidget(QLabel("Đăng nhập"), 4, 0)
        grid.addWidget(login_state, 4, 1, 1, 2)
        grid.addWidget(QLabel("Profile"), 5, 0)
        grid.addWidget(profile, 5, 1, 1, 2)
        grid.addWidget(login_help, 6, 0, 1, 3)
        root.addWidget(account_group)

        assigned_count = QLabel("Được phân bổ: 0 ảnh")
        allocation = QPlainTextEdit()
        allocation.setReadOnly(True)
        allocation.setMinimumHeight(70)
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
        logs.setMinimumHeight(100)
        logs.setPlaceholderText("Log riêng của worker")
        root.addWidget(QLabel("Log riêng"))
        root.addWidget(logs, 1)
        return page, _WorkerWidgets(
            account=account,
            password=password,
            muse_tab=muse_tab,
            tab_selection=tab_selection,
            login_state=login_state,
            profile=profile,
            assigned_count=assigned_count,
            allocation=allocation,
            current_image=current_image,
            progress=progress,
            stats=stats,
            error=error,
            logs=logs,
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

    def _connection_mode(self) -> MuseSessionOpenMode:
        value = str(self.connection_mode.currentData() or MuseSessionOpenMode.MANUAL_BROWSER.value)
        try:
            return MuseSessionOpenMode(value)
        except ValueError:
            return MuseSessionOpenMode.MANUAL_BROWSER

    def _connection_mode_changed(self) -> None:
        mode = self._connection_mode()
        self.settings.muse_connection_mode = mode.value
        self.settings_changed.emit()
        self.refresh()

    def _account_emails(self) -> dict[int, str]:
        emails = {
            worker_id: widgets.account.currentText().strip().casefold()
            for worker_id, widgets in self._workers.items()
        }
        invalid = [str(worker_id) for worker_id, email in emails.items() if "@" not in email]
        if invalid:
            raise ValueError("Hãy nhập email hợp lệ cho tài khoản " + ", ".join(invalid) + ".")
        if len(set(emails.values())) != MUSE_SESSION_COUNT:
            raise ValueError("Ba tài khoản Muse phải dùng ba email khác nhau.")
        return emails

    def _account_credentials(self) -> dict[int, tuple[str, str]]:
        emails = self._account_emails()
        return {
            worker_id: (email, self._workers[worker_id].password.text())
            for worker_id, email in emails.items()
        }

    def _clear_password_inputs(self) -> None:
        for widgets in self._workers.values():
            widgets.password.clear()

    def _open_browser_sessions(self) -> None:
        try:
            if self.session_manager.busy or self.batch_manager.busy:
                raise RuntimeError("Muse đang xử lý; hãy chờ hoặc bấm Dừng tất cả.")
            credentials = self._account_credentials()
            self._pending_browser_open = self.session_manager.open_all_sessions(
                credentials,
                force_relogin=False,
                manual_browser=True,
            )
            self._clear_password_inputs()
            self.global_status.setText(
                "Đang mở 3 Chrome/Edge thường. Tool sẽ điền tài khoản nào có mật khẩu; "
                "CAPTCHA/2FA làm thủ công. Giữ nguyên cửa sổ để tool tự nhận READY."
            )
        except Exception as exc:
            QMessageBox.warning(self, "Không thể mở Chrome/Muse", str(exc))

    def _scan_muse_tabs(self) -> None:
        try:
            if self._pending_tab_scan is not None:
                raise RuntimeError("Đang quét tab Muse; hãy chờ kết quả.")
            if self.batch_manager.busy:
                raise RuntimeError("Không thể đổi tab khi batch Muse đang chạy.")
            self._pending_tab_scan = self.session_manager.discover_muse_tabs()
            self.global_status.setText("Đang quét các tab muse.ai trong 3 Chrome profile của tool…")
        except Exception as exc:
            QMessageBox.warning(self, "Không thể quét tab Muse", str(exc))

    def _tab_selection_changed(self, worker_id: int) -> None:
        widgets = self._workers.get(worker_id)
        if widgets is None:
            return
        handle = str(widgets.muse_tab.currentData() or "").strip()
        if not handle:
            widgets.tab_selection.setText("Chưa chọn tab Muse")
            widgets.tab_selection.setStyleSheet("")
            return
        widgets.tab_selection.setText(f"ĐÃ CHỌN TAB: {widgets.muse_tab.currentText()}")
        widgets.tab_selection.setStyleSheet("color: #34d399; font-weight: 600;")

    def _selected_tab_handles(self) -> dict[int, str]:
        selections = {
            worker_id: str(widgets.muse_tab.currentData() or "").strip()
            for worker_id, widgets in self._workers.items()
        }
        missing = [str(worker_id) for worker_id, handle in selections.items() if not handle]
        if missing:
            raise ValueError(
                "Hãy bấm Quét tab Muse và chọn tab READY cho tài khoản " + ", ".join(missing) + "."
            )
        return selections

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
            if self._pending_batch_start is not None:
                raise RuntimeError("Ba phiên Muse đang được mở; hãy chờ hoặc bấm Dừng tất cả.")
            batch = self.batch_manager.snapshot()
            if not batch.source_paths:
                raise ValueError("Hãy chọn và phân bổ ảnh trước khi bắt đầu.")
            prompt = self.prompt.toPlainText().strip()
            if not prompt:
                raise ValueError("Prompt tạo video chung không được để trống.")
            output_dir = self.output_folder.text().strip()
            if not output_dir:
                raise ValueError("Hãy chọn thư mục lưu video đầu ra.")
            emails = self._account_emails()
            settings = self._video_settings()
            self.batch_manager.prepare_start(prompt, settings, output_dir)
            if self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB:
                login_future = self.session_manager.bind_existing_tabs(
                    emails,
                    self._selected_tab_handles(),
                )
                opening_message = "Đang gắn 3 worker vào 3 tab Muse đã chọn; tab READY sẽ bắt đầu xử lý."
            else:
                credentials = self._account_credentials()
                login_future = self.session_manager.open_all_sessions(
                    credentials,
                    force_relogin=False,
                    manual_browser=True,
                )
                self._clear_password_inputs()
                opening_message = (
                    "Đang mở 3 Chrome/Edge thường và đăng nhập các tài khoản có mật khẩu. "
                    "CAPTCHA/2FA làm thủ công; tool sẽ chờ đủ profile READY rồi gửi đồng thời."
                )
            self._pending_started_workers.clear()
            self._pending_batch_start = (
                login_future,
                prompt,
                settings,
                output_dir,
            )
            self.global_status.setText(opening_message)
        except Exception as exc:
            QMessageBox.warning(self, "Không thể bắt đầu batch Muse", str(exc))

    def _stop_all(self) -> None:
        self._pending_batch_start = None
        self._pending_started_workers.clear()
        batch_count = self.batch_manager.stop_all()
        session_count = self.session_manager.stop_all()
        self.global_status.setText(
            f"Đang dừng {session_count} phiên đăng nhập và {batch_count} worker Muse; "
            "prompt, tiến độ và kết quả được giữ nguyên. YouTube không bị tác động."
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
            self._collect_pending_browser_open()
            self._collect_pending_tab_scan()
            session_values = {item.session_id: item for item in self.session_manager.snapshots()}
            batch = self.batch_manager.snapshot()
            for worker in batch.workers:
                self._render_worker(worker, session_values[worker.worker_id])
            emails = [widgets.account.currentText().strip().casefold() for widgets in self._workers.values()]
            accounts_valid = all("@" in email for email in emails) and len(set(emails)) == MUSE_SESSION_COUNT
            opening = self._pending_batch_start is not None
            workflow_busy = batch.running or self.session_manager.busy or opening
            tab_mode = self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB
            tabs_selected = all(bool(widgets.muse_tab.currentData()) for widgets in self._workers.values())
            self.start_all.setEnabled(
                accounts_valid
                and (not tab_mode or tabs_selected)
                and bool(batch.source_paths)
                and bool(self.prompt.toPlainText().strip())
                and bool(self.output_folder.text().strip())
                and not workflow_busy
            )
            self._render_start_requirements(session_values, batch)
            self.stop_all.setEnabled(workflow_busy)
            self.open_browsers.setEnabled(not workflow_busy)
            self.scan_tabs.setEnabled(
                not workflow_busy
                and self._pending_tab_scan is None
                and any(item.driver_open for item in session_values.values())
            )
            self.resume.setEnabled(bool(batch.jobs) and not workflow_busy)
            self.allocate.setEnabled(not workflow_busy)
            self.redistribute.setEnabled(bool(batch.jobs) and not workflow_busy)
            self.clear.setEnabled(not workflow_busy)
            self._collect_pending_batch_start()
            self._collect_futures()
        finally:
            self._refreshing = False

    def _render_start_requirements(self, session_values, batch) -> None:
        not_ready = [
            str(session_id)
            for session_id, item in sorted(session_values.items())
            if item.state != MuseSessionState.READY or not item.driver_open
        ]
        blockers: list[str] = []
        emails = [widgets.account.currentText().strip().casefold() for widgets in self._workers.values()]
        invalid_accounts = [str(index) for index, email in enumerate(emails, 1) if "@" not in email]
        if invalid_accounts:
            blockers.append("nhập email Google hợp lệ cho tài khoản " + ", ".join(invalid_accounts))
        elif len(set(emails)) != MUSE_SESSION_COUNT:
            blockers.append("ba email Google phải khác nhau")
        if self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB:
            missing_tabs = [
                str(worker_id)
                for worker_id, widgets in self._workers.items()
                if not widgets.muse_tab.currentData()
            ]
            if missing_tabs:
                blockers.append("quét và chọn tab Muse cho tài khoản " + ", ".join(missing_tabs))
        if not batch.source_paths:
            blockers.append("bấm Phân bổ ảnh")
        if not self.prompt.toPlainText().strip():
            blockers.append("nhập prompt chung")
        if not self.output_folder.text().strip():
            blockers.append("chọn thư mục output")
        if blockers:
            message = "Chưa thể bắt đầu: " + "; ".join(blockers) + "."
        elif self._pending_batch_start is not None or self.session_manager.busy:
            message = "Đang đăng nhập; tài khoản READY sẽ tự bắt đầu batch độc lập."
        elif batch.running:
            message = "Ba worker Muse đang chạy độc lập."
        elif not_ready:
            if self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB:
                message = "Sẵn sàng kiểm tra 3 tab đã chọn rồi chạy batch."
            else:
                message = (
                    "Sẵn sàng mở Chrome cho tài khoản "
                    + ", ".join(not_ready)
                    + "; hãy tự hoàn tất Google/Muse, sau đó tool chạy batch."
                )
        else:
            message = f"Sẵn sàng chạy cả 3 tài khoản với {len(batch.source_paths)} ảnh."
        self.start_requirements.setText(message)
        self.start_all.setToolTip(message)

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
        widgets.stop.setEnabled(session.task_running or worker.task_running)
        widgets.retry.setEnabled(worker.failed > 0 and not worker.task_running)
        widgets.account.setEnabled(not session.driver_open and not session.task_running and not worker.task_running)
        widgets.password.setEnabled(
            self._connection_mode() != MuseSessionOpenMode.EXISTING_TAB
            and not session.driver_open
            and not session.task_running
            and not worker.task_running
        )
        widgets.muse_tab.setEnabled(
            self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB
            and not session.task_running
            and not worker.task_running
        )
        self.tabs.setTabText(
            worker.worker_id - 1,
            f"Tài khoản {worker.worker_id} • {SESSION_LABELS[session.state]}",
        )

    def _collect_pending_batch_start(self) -> None:
        pending = self._pending_batch_start
        if pending is None:
            return
        login_future, prompt, settings, output_dir = pending
        if not login_future.done():
            ready_count = sum(
                1
                for item in self.session_manager.snapshots()
                if item.state == MuseSessionState.READY and item.driver_open
            )
            self.global_status.setText(
                f"Đang chờ đủ phiên Muse READY ({ready_count}/3); chưa worker nào gửi prompt."
            )
            return
        ready_ids = {
            item.session_id
            for item in self.session_manager.snapshots()
            if item.state == MuseSessionState.READY and item.driver_open
        }
        login_error: Exception | None = None
        try:
            login_future.result()
        except Exception as exc:
            login_error = exc
        if not ready_ids:
            self._pending_batch_start = None
            self._pending_started_workers.clear()
            message = str(login_error or "Không có tài khoản Muse nào READY.")
            self.global_status.setText(f"Không có tài khoản nào READY để chạy batch: {message}")
            QMessageBox.warning(self, "Không thể bắt đầu batch Muse", message)
            return
        try:
            self._track(self.batch_manager.start_all(prompt, settings, output_dir))
            self._pending_started_workers.update(ready_ids)
        except Exception as exc:
            self.global_status.setText(f"Không thể khởi động đồng thời các worker Muse: {exc}")
            self._pending_batch_start = None
            self._pending_started_workers.clear()
            QMessageBox.warning(self, "Không thể bắt đầu batch Muse", str(exc))
            return
        self._pending_batch_start = None
        started = sorted(self._pending_started_workers)
        self._pending_started_workers.clear()
        labels = ", ".join(str(value) for value in started)
        if login_error is None:
            self.global_status.setText(
                f"Tài khoản {labels} đã READY; các worker vừa được phát lệnh đồng thời."
            )
        else:
            self.global_status.setText(
                f"Tài khoản {labels} được phát lệnh đồng thời; phiên lỗi bị bỏ qua: {login_error}"
            )

    def _collect_pending_browser_open(self) -> None:
        future = self._pending_browser_open
        if future is None or not future.done():
            return
        self._pending_browser_open = None
        try:
            future.result()
            self.global_status.setText(
                "Ba Chrome profile đã READY. Có thể bấm Quét tab Muse hoặc bấm Xử lý."
            )
        except Exception as exc:
            self.global_status.setText(f"Một hoặc nhiều phiên Muse chưa READY: {exc}")

    def _collect_pending_tab_scan(self) -> None:
        future = self._pending_tab_scan
        if future is None or not future.done():
            return
        self._pending_tab_scan = None
        try:
            candidates = tuple(future.result())
        except Exception as exc:
            self.global_status.setText(f"Không thể quét tab Muse: {exc}")
            return
        grouped = {
            worker_id: tuple(item for item in candidates if item.session_id == worker_id)
            for worker_id in range(1, MUSE_SESSION_COUNT + 1)
        }
        self._tab_candidates = grouped
        ready_total = 0
        for worker_id, widgets in self._workers.items():
            previous = str(widgets.muse_tab.currentData() or "")
            widgets.muse_tab.clear()
            widgets.muse_tab.addItem("Chọn tab Muse…", "")
            for candidate in grouped[worker_id]:
                suffix = candidate.url if len(candidate.url) <= 80 else candidate.url[:77] + "…"
                widgets.muse_tab.addItem(f"{candidate.label} • {suffix}", candidate.handle)
                if candidate.ready:
                    ready_total += 1
            index = widgets.muse_tab.findData(previous)
            if index < 0:
                ready_candidates = [item for item in grouped[worker_id] if item.ready]
                if len(ready_candidates) == 1:
                    index = widgets.muse_tab.findData(ready_candidates[0].handle)
            widgets.muse_tab.setCurrentIndex(max(0, index))
            self._tab_selection_changed(worker_id)
        self.global_status.setText(
            f"Đã tìm thấy {len(candidates)} tab Muse, trong đó {ready_total} tab READY. "
            "Mỗi tài khoản hãy chọn đúng một tab của profile tương ứng."
        )

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
