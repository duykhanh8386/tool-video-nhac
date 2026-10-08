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
    enabled: QCheckBox
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
        self._pending_batch_start: tuple[Future, str, MuseVideoSettings, str, tuple[int, ...]] | None = None
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
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        page_scroll = QScrollArea()
        page_scroll.setWidgetResizable(True)
        outer_layout.addWidget(page_scroll)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        page_scroll.setWidget(content)

        title = QLabel("✨ Muse AI Studio — Batch Ảnh Thành Video (3 Tài Khoản)")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        notice = QLabel(
            "💡 Hỗ trợ tạo video đa luồng song song trên tối đa 3 tài khoản Muse AI (profile Chrome độc lập). "
            "Tự động điền tài khoản an toàn, không lưu mật khẩu. Mọi xác minh Google / CAPTCHA thực hiện trực tiếp trên trình duyệt."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        root.addWidget(notice)

        connection_group = QGroupBox("🌐 Cách kết nối tài khoản Muse")
        connection_row = QHBoxLayout(connection_group)
        connection_row.setSpacing(10)
        self.connection_mode = QComboBox()
        self.connection_mode.setMinimumHeight(36)
        self.connection_mode.addItem(
            "Phương án 1 — Profile riêng: tự điền Google hoặc đăng nhập tay",
            MuseSessionOpenMode.MANUAL_BROWSER.value,
        )
        self.connection_mode.addItem(
            "Phương án 2 — Chọn tab Muse đang mở sẵn",
            MuseSessionOpenMode.EXISTING_TAB.value,
        )
        mode_index = self.connection_mode.findData(
            self.settings.muse_connection_mode or MuseSessionOpenMode.MANUAL_BROWSER.value
        )
        self.connection_mode.setCurrentIndex(max(0, mode_index))
        self.connection_mode.currentIndexChanged.connect(self._connection_mode_changed)
        self.open_browsers = QPushButton("🌐 Mở Chrome/Muse đã tick")
        self.open_browsers.setMinimumHeight(36)
        self.open_browsers.clicked.connect(self._open_browser_sessions)
        self.scan_tabs = QPushButton("🔍 Quét tab Muse đang mở")
        self.scan_tabs.setMinimumHeight(36)
        self.scan_tabs.clicked.connect(self._scan_muse_tabs)
        connection_row.addWidget(self.connection_mode, 1)
        connection_row.addWidget(self.open_browsers)
        connection_row.addWidget(self.scan_tabs)
        root.addWidget(connection_group)

        # Card 1: Nguồn ảnh & Thư mục lưu
        source_group = QGroupBox("📁 Nguồn Ảnh & Thư Mục Xuất File")
        source_grid = QGridLayout(source_group)
        source_grid.setVerticalSpacing(10)
        source_grid.setHorizontalSpacing(10)
        self.url = QLineEdit(self.settings.muse_start_url or MUSE_START_URL)
        self.url.setMinimumHeight(36)
        self.url.editingFinished.connect(self._save_url)
        self.image_folder = QLineEdit()
        self.image_folder.setMinimumHeight(36)
        self.image_folder.setPlaceholderText("Chọn hoặc nhập đường dẫn thư mục ảnh (JPG, PNG, WEBP)...")
        choose_images = QPushButton("📂 Chọn thư mục ảnh")
        choose_images.setMinimumHeight(36)
        choose_images.clicked.connect(self._choose_image_folder)
        self.recursive = QCheckBox("Quét cả thư mục con")
        self.image_count = QLabel("Chưa quét ảnh")
        self.image_count.setStyleSheet("color: #2563eb; font-weight: 600; padding: 2px 4px;")
        self.output_folder = QLineEdit(self.settings.last_output_folder)
        self.output_folder.setMinimumHeight(36)
        self.output_folder.setPlaceholderText("Chọn hoặc nhập thư mục lưu video MP4 kết quả...")
        choose_output = QPushButton("📁 Chọn thư mục lưu")
        choose_output.setMinimumHeight(36)
        choose_output.clicked.connect(self._choose_output_folder)

        source_grid.addWidget(QLabel("MUSE_URL:"), 0, 0)
        source_grid.addWidget(self.url, 0, 1, 1, 3)
        source_grid.addWidget(QLabel("Thư mục ảnh:"), 1, 0)
        source_grid.addWidget(self.image_folder, 1, 1)
        source_grid.addWidget(choose_images, 1, 2)
        source_grid.addWidget(self.recursive, 1, 3)
        source_grid.addWidget(self.image_count, 2, 1, 1, 3)
        source_grid.addWidget(QLabel("Thư mục output:"), 3, 0)
        source_grid.addWidget(self.output_folder, 3, 1, 1, 2)
        source_grid.addWidget(choose_output, 3, 3)
        root.addWidget(source_group)

        # Card 2: Hộp nhập Prompt chuyên biệt (AI Motion Prompt Box)
        prompt_card = QGroupBox("✨ Prompt Tạo Video AI (Motion Prompt)")
        prompt_vbox = QVBoxLayout(prompt_card)
        prompt_vbox.setSpacing(8)

        prompt_header = QHBoxLayout()
        prompt_hint = QLabel("Mô tả chuyển động, góc máy, ánh sáng (áp dụng chung cho batch ảnh):")
        prompt_hint.setStyleSheet("color: #475569; font-size: 12px; font-weight: 500;")
        prompt_tag = QLabel("📸 Snapshot khi bắt đầu batch")
        prompt_tag.setStyleSheet(
            "color: #1d4ed8; font-size: 11px; font-weight: 600; background: #eff6ff; "
            "border: 1px solid #bfdbfe; border-radius: 4px; padding: 2px 8px;"
        )
        prompt_header.addWidget(prompt_hint)
        prompt_header.addStretch()
        prompt_header.addWidget(prompt_tag)
        prompt_vbox.addLayout(prompt_header)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "✍️ Nhập prompt điều khiển chuyển động cho AI Muse tại đây...\n"
            "Ví dụ: Tạo video chuyển động nhân vật nhẹ nhàng, cinematic lighting, ultra smooth camera pan, 4k highly detailed"
        )
        self.prompt.setMinimumHeight(85)
        self.prompt.setStyleSheet(
            "QPlainTextEdit { "
            "background: #ffffff; "
            "border: 2px solid #3b82f6; "
            "border-radius: 8px; "
            "padding: 10px 12px; "
            "color: #0f172a; "
            "font-size: 13px; "
            "line-height: 1.4; "
            "} "
            "QPlainTextEdit:focus { "
            "border: 2px solid #1d4ed8; "
            "background: #ffffff; "
            "}"
        )
        prompt_vbox.addWidget(self.prompt)
        root.addWidget(prompt_card)

        # Card 3: Cài đặt video
        settings_group = QGroupBox("⚙️ Cài đặt video Muse (để trống = mặc định của Muse)")
        settings_layout = QHBoxLayout(settings_group)
        settings_layout.setSpacing(20)
        # Giữ ngầm model và quantity để bảo toàn tương thích tuyệt đối
        self.model = self._editable_combo(("",))
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 1)
        self.quantity.setValue(1)

        # Các tùy chọn video thiết thực hiển thị trên giao diện
        self.aspect_ratio = self._editable_combo(("", "16:9", "9:16", "1:1"))
        self.duration = self._editable_combo(("", "5s", "8s", "10s"))
        self.resolution = self._editable_combo(("", "720p", "1080p"))
        for icon_label, widget in (
            ("📐 Tỷ lệ khung hình:", self.aspect_ratio),
            ("⏱ Độ dài video:", self.duration),
            ("📺 Độ phân giải:", self.resolution),
        ):
            box = QHBoxLayout()
            lbl = QLabel(icon_label)
            lbl.setStyleSheet("color: #1e293b; font-weight: 600; font-size: 13px;")
            box.addWidget(lbl)
            widget.setMinimumHeight(36)
            widget.setMinimumWidth(130)
            box.addWidget(widget)
            settings_layout.addLayout(box)
        settings_layout.addStretch()
        root.addWidget(settings_group)

        # Card 4: Thanh điều khiển chính và công cụ phụ
        actions_card = QWidget()
        actions_card.setStyleSheet(
            "background: #ffffff; border: 1.5px solid #e2e8f0; border-radius: 10px;"
        )
        actions_vbox = QVBoxLayout(actions_card)
        actions_vbox.setContentsMargins(12, 10, 12, 10)
        actions_vbox.setSpacing(10)

        primary_row = QHBoxLayout()
        primary_row.setSpacing(12)
        self.start_all = QPushButton("▶ Bắt đầu xử lý")
        self.start_all.setObjectName("primary")
        self.start_all.setMinimumHeight(40)
        self.start_all.setMinimumWidth(160)
        self.start_all.setStyleSheet(
            "QPushButton#primary { background: #2563eb; border: 1.5px solid #1d4ed8; color: #ffffff; font-weight: 700; font-size: 14px; border-radius: 7px; padding: 0 20px; } "
            "QPushButton#primary:hover { background: #1d4ed8; } "
            "QPushButton#primary:disabled { background: #e2e8f0; border-color: #cbd5e1; color: #94a3b8; }"
        )

        self.stop_all = QPushButton("⏹ Dừng tất cả")
        self.stop_all.setMinimumHeight(40)
        self.stop_all.setMinimumWidth(130)
        self.stop_all.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #f87171; color: #991b1b; font-weight: 700; border-radius: 7px; padding: 0 16px; font-size: 13px; } "
            "QPushButton:hover { background: #fecaca; border-color: #ef4444; } "
            "QPushButton:disabled { background: #f8fafc; border-color: #e2e8f0; color: #cbd5e1; }"
        )

        self.allocate = QPushButton("📊 Phân bổ ảnh")
        self.allocate.setMinimumHeight(40)
        self.allocate.setMinimumWidth(130)
        self.allocate.setStyleSheet(
            "QPushButton { background: #f8fafc; border: 1.5px solid #cbd5e1; color: #0f172a; font-weight: 600; border-radius: 7px; padding: 0 16px; font-size: 13px; } "
            "QPushButton:hover { background: #f1f5f9; border-color: #94a3b8; } "
            "QPushButton:disabled { background: #f8fafc; border-color: #e2e8f0; color: #94a3b8; }"
        )

        primary_row.addWidget(self.start_all)
        primary_row.addWidget(self.stop_all)
        primary_row.addWidget(self.allocate)
        primary_row.addStretch()

        utility_row = QHBoxLayout()
        utility_row.setSpacing(10)
        self.resume = QPushButton("⏯ Tiếp tục")
        self.resume.setMinimumHeight(34)
        self.redistribute = QPushButton("🔁 Phân bổ lại ảnh chưa gửi")
        self.redistribute.setMinimumHeight(34)
        self.clear = QPushButton("🗑 Xóa dữ liệu UI")
        self.clear.setMinimumHeight(34)
        utility_row.addWidget(self.resume)
        utility_row.addWidget(self.redistribute)
        utility_row.addWidget(self.clear)
        utility_row.addStretch()

        actions_vbox.addLayout(primary_row)
        actions_vbox.addLayout(utility_row)
        root.addWidget(actions_card)

        self.allocate.clicked.connect(self._allocate)
        self.start_all.clicked.connect(self._start_all)
        self.stop_all.clicked.connect(self._stop_all)
        self.resume.clicked.connect(self._resume)
        self.redistribute.clicked.connect(self._redistribute)
        self.clear.clicked.connect(self._clear)

        self.global_status = QLabel("Tick các tài khoản muốn chạy, phân bổ ảnh, sau đó bắt đầu.")
        self.global_status.setWordWrap(True)
        self.global_status.setStyleSheet("color: #475569; font-size: 12px; padding: 2px 4px;")
        root.addWidget(self.global_status)
        self.start_requirements = QLabel()
        self.start_requirements.setWordWrap(True)
        self.start_requirements.setObjectName("notice")
        root.addWidget(self.start_requirements)

        self.tabs = QTabWidget()
        self.tabs.setMinimumHeight(560)
        for worker_id in range(1, MUSE_SESSION_COUNT + 1):
            page, widgets = self._build_worker_tab(worker_id)
            self._workers[worker_id] = widgets
            self.tabs.addTab(page, f"Tài khoản {worker_id}")
        root.addWidget(self.tabs)

    def _build_worker_tab(self, worker_id: int) -> tuple[QWidget, _WorkerWidgets]:
        page = QScrollArea()
        page.setWidgetResizable(True)
        content = QWidget()
        content.setMinimumHeight(520)
        page.setWidget(content)
        root = QVBoxLayout(content)
        account_group = QGroupBox(f"Tài khoản Muse {worker_id}")
        account_group.setMinimumHeight(305)
        grid = QGridLayout(account_group)
        enabled = QCheckBox("Dùng tài khoản này để chạy")
        enabled.setChecked(True)
        enabled.toggled.connect(lambda _checked: self._worker_selection_changed())
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
        stop = QPushButton("⏹ Dừng worker")
        stop.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #f87171; color: #991b1b; font-weight: 600; border-radius: 6px; padding: 6px 14px; } "
            "QPushButton:hover { background: #fecaca; } "
            "QPushButton:disabled { background: #f8fafc; border-color: #e2e8f0; color: #cbd5e1; }"
        )
        retry = QPushButton("🔄 Chạy lại ảnh lỗi")
        stop.clicked.connect(lambda _checked=False, value=worker_id: self._stop_worker(value))
        retry.clicked.connect(lambda _checked=False, value=worker_id: self._retry_failed(value))
        login_state = QLabel("Chưa đăng nhập")
        login_state.setWordWrap(True)
        login_state.setStyleSheet("font-weight: 600; color: #0284c7;")
        profile = QLabel()
        profile.setObjectName("muted")
        profile.setWordWrap(True)
        login_help = QLabel(
            "Phương án 1: tài khoản có mật khẩu được mở bằng driver/profile riêng biệt; tool chỉ điền "
            "email/mật khẩu trên accounts.google.com. Nếu để trống thì bạn tự đăng nhập trong Chrome thường. "
            "CAPTCHA, passkey và 2FA luôn làm thủ công. Sau khi Google xác minh, tool bấm nút Continue as "
            "của đúng cửa sổ Chrome, xác nhận email rồi tự mở Muse; mật khẩu bị xóa khỏi UI ngay khi bàn giao. "
            "Phương án 2: bấm Mở Chrome/Muse đã tick, mở sẵn Muse trong từng cửa sổ, bấm Quét tab Muse, "
            "rồi chọn một dòng READY cho mỗi tài khoản. Dòng ĐÃ CHỌN TAB màu xanh là đã chọn xong. "
            "Waitlist vẫn do người dùng xử lý."
        )
        login_help.setObjectName("muted")
        login_help.setWordWrap(True)
        grid.addWidget(enabled, 0, 0, 1, 3)
        grid.addWidget(QLabel("Email Google"), 1, 0)
        grid.addWidget(account, 1, 1, 1, 2)
        grid.addWidget(QLabel("Mật khẩu Google"), 2, 0)
        grid.addWidget(password, 2, 1, 1, 2)
        grid.addWidget(QLabel("Tab Muse"), 3, 0)
        grid.addWidget(muse_tab, 3, 1, 1, 2)
        grid.addWidget(tab_selection, 4, 1, 1, 2)
        grid.addWidget(QLabel("Đăng nhập"), 5, 0)
        grid.addWidget(login_state, 5, 1, 1, 2)
        grid.addWidget(QLabel("Profile"), 6, 0)
        grid.addWidget(profile, 6, 1, 1, 2)
        grid.addWidget(login_help, 7, 0, 1, 3)
        root.addWidget(account_group)

        assigned_count = QLabel("Được phân bổ: 0 ảnh")
        assigned_count.setStyleSheet("font-weight: 700; color: #0f172a; font-size: 13px;")
        allocation = QPlainTextEdit()
        allocation.setReadOnly(True)
        allocation.setMinimumHeight(70)
        allocation.setMaximumHeight(110)
        allocation.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px; background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 6px; color: #0f172a; padding: 6px;"
        )
        allocation.setPlaceholderText("Danh sách ảnh được phân bổ sẽ hiển thị trước khi chạy.")
        root.addWidget(assigned_count)
        root.addWidget(allocation)

        current_image = QLabel("Ảnh hiện tại: —")
        current_image.setWordWrap(True)
        current_image.setStyleSheet("font-weight: 600; color: #1e293b;")
        progress = QProgressBar()
        progress.setRange(0, 100)
        stats = QLabel("Chờ: 0 • Thành công: 0 • Lỗi: 0 • Quota: 0")
        stats.setStyleSheet("font-weight: 600; color: #475569; font-size: 12px;")
        error = QLabel()
        error.setWordWrap(True)
        error.setStyleSheet("color: #dc2626; font-weight: 600;")
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
        logs.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px; background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 6px; color: #0f172a; padding: 6px;"
        )
        logs.setPlaceholderText("Log riêng của worker")
        root.addWidget(QLabel("Log riêng:"))
        root.addWidget(logs, 1)
        return page, _WorkerWidgets(
            enabled=enabled,
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
        enabled_ids = set(snapshot.enabled_worker_ids)
        for worker_id, widgets in self._workers.items():
            widgets.enabled.setChecked(worker_id in enabled_ids)
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

    def _selected_worker_ids(self) -> tuple[int, ...]:
        selected = tuple(
            worker_id
            for worker_id, widgets in self._workers.items()
            if widgets.enabled.isChecked()
        )
        if not selected:
            raise ValueError("Hãy tick ít nhất một tài khoản Muse để chạy.")
        return selected

    def _worker_selection_changed(self) -> None:
        selected = tuple(
            worker_id
            for worker_id, widgets in self._workers.items()
            if widgets.enabled.isChecked()
        )
        if selected and not self.batch_manager.busy:
            try:
                self.batch_manager.set_enabled_workers(selected)
            except Exception:
                pass
        if not self._refreshing:
            self.refresh()

    def _account_emails(self, worker_ids: tuple[int, ...] | None = None) -> dict[int, str]:
        selected = worker_ids or self._selected_worker_ids()
        emails = {
            worker_id: self._workers[worker_id].account.currentText().strip().casefold()
            for worker_id in selected
        }
        invalid = [str(worker_id) for worker_id, email in emails.items() if "@" not in email]
        if invalid:
            raise ValueError("Hãy nhập email hợp lệ cho tài khoản " + ", ".join(invalid) + ".")
        if len(set(emails.values())) != len(emails):
            raise ValueError("Các tài khoản Muse được tick phải dùng các email khác nhau.")
        return emails

    def _account_credentials(
        self,
        worker_ids: tuple[int, ...] | None = None,
    ) -> dict[int, tuple[str, str]]:
        emails = self._account_emails(worker_ids)
        return {
            worker_id: (email, self._workers[worker_id].password.text())
            for worker_id, email in emails.items()
        }

    def _clear_password_inputs(self, worker_ids: tuple[int, ...] | None = None) -> None:
        selected = worker_ids or tuple(self._workers)
        for worker_id in selected:
            self._workers[worker_id].password.clear()

    def _open_browser_sessions(self) -> None:
        try:
            if self.session_manager.busy or self.batch_manager.busy:
                raise RuntimeError("Muse đang xử lý; hãy chờ hoặc bấm Dừng tất cả.")
            selected_ids = self._selected_worker_ids()
            credentials = self._account_credentials(selected_ids)
            self._pending_browser_open = self.session_manager.open_all_sessions(
                credentials,
                force_relogin=False,
                manual_browser=True,
            )
            self._clear_password_inputs(selected_ids)
            self.global_status.setText(
                f"Đang mở {len(selected_ids)} Chrome profile riêng cho tài khoản đã tick. "
                "Tool sẽ điền tài khoản nào có mật khẩu; "
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
        widgets.tab_selection.setStyleSheet("color: #059669; font-weight: 700;")

    def _selected_tab_handles(
        self,
        worker_ids: tuple[int, ...] | None = None,
    ) -> dict[int, str]:
        selected = worker_ids or self._selected_worker_ids()
        selections = {
            worker_id: str(self._workers[worker_id].muse_tab.currentData() or "").strip()
            for worker_id in selected
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
            selected_ids = self._selected_worker_ids()
            allocations = self.batch_manager.allocate_images(images, worker_ids=selected_ids)
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
                if self.image_folder.text().strip():
                    self._allocate()
                    batch = self.batch_manager.snapshot()
                if not batch.source_paths:
                    raise ValueError("Hãy chọn thư mục ảnh và phân bổ ảnh trước khi bắt đầu.")
            prompt = self.prompt.toPlainText().strip()
            if not prompt:
                raise ValueError("Prompt tạo video chung không được để trống.")
            output_dir = self.output_folder.text().strip()
            if not output_dir:
                raise ValueError("Hãy chọn thư mục lưu video đầu ra.")
            selected_ids = self._selected_worker_ids()
            emails = self._account_emails(selected_ids)
            settings = self._video_settings()
            self.batch_manager.prepare_start(prompt, settings, output_dir, worker_ids=selected_ids)
            if self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB:
                login_future = self.session_manager.bind_existing_tabs(
                    emails,
                    self._selected_tab_handles(selected_ids),
                )
                opening_message = (
                    f"Đang gắn {len(selected_ids)} worker đã tick vào tab Muse; "
                    "các tab READY sẽ bắt đầu xử lý."
                )
            else:
                credentials = self._account_credentials(selected_ids)
                login_future = self.session_manager.open_all_sessions(
                    credentials,
                    force_relogin=False,
                    manual_browser=True,
                )
                self._clear_password_inputs(selected_ids)
                opening_message = (
                    f"Đang mở {len(selected_ids)} Chrome profile riêng cho tài khoản đã tick. "
                    "CAPTCHA/2FA làm thủ công; tool sẽ chờ đủ profile READY rồi gửi đồng thời."
                )
            self._pending_started_workers.clear()
            self._pending_batch_start = (
                login_future,
                prompt,
                settings,
                output_dir,
                selected_ids,
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
            allocations = self.batch_manager.redistribute_unsubmitted(
                worker_ids=self._selected_worker_ids(),
            )
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
            selected_ids = tuple(
                worker_id
                for worker_id, widgets in self._workers.items()
                if widgets.enabled.isChecked()
            )
            emails = [self._workers[worker_id].account.currentText().strip().casefold() for worker_id in selected_ids]
            accounts_valid = bool(selected_ids) and all("@" in email for email in emails) and len(set(emails)) == len(emails)
            opening = self._pending_batch_start is not None
            workflow_busy = batch.running or self.session_manager.busy or opening
            tab_mode = self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB
            tabs_selected = all(bool(self._workers[worker_id].muse_tab.currentData()) for worker_id in selected_ids)
            self.start_all.setEnabled(
                accounts_valid
                and (not tab_mode or tabs_selected)
                and (bool(batch.source_paths) or bool(self.image_folder.text().strip()))
                and bool(self.prompt.toPlainText().strip())
                and bool(self.output_folder.text().strip())
                and not workflow_busy
            )
            self._render_start_requirements(session_values, batch, selected_ids)
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

    def _render_start_requirements(self, session_values, batch, selected_ids: tuple[int, ...]) -> None:
        not_ready = [
            str(session_id)
            for session_id, item in sorted(session_values.items())
            if session_id in selected_ids
            and (item.state != MuseSessionState.READY or not item.driver_open)
        ]
        blockers: list[str] = []
        if not selected_ids:
            blockers.append("tick ít nhất một tài khoản")
        emails = {
            worker_id: self._workers[worker_id].account.currentText().strip().casefold()
            for worker_id in selected_ids
        }
        invalid_accounts = [str(worker_id) for worker_id, email in emails.items() if "@" not in email]
        if selected_ids and invalid_accounts:
            blockers.append("nhập email Google hợp lệ cho tài khoản " + ", ".join(invalid_accounts))
        elif selected_ids and len(set(emails.values())) != len(emails):
            blockers.append("email Google của các tài khoản đã tick phải khác nhau")
        if self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB:
            missing_tabs = [
                str(worker_id)
                for worker_id in selected_ids
                if not self._workers[worker_id].muse_tab.currentData()
            ]
            if missing_tabs:
                blockers.append("quét và chọn tab Muse cho tài khoản " + ", ".join(missing_tabs))
        if not batch.source_paths and not self.image_folder.text().strip():
            blockers.append("chọn thư mục ảnh")
        if not self.prompt.toPlainText().strip():
            blockers.append("nhập prompt chung")
        if not self.output_folder.text().strip():
            blockers.append("chọn thư mục output")
        if blockers:
            message = "Chưa thể bắt đầu: " + "; ".join(blockers) + "."
            self.start_requirements.setStyleSheet(
                "background: #fef2f2; color: #991b1b; border: 1.5px solid #fecaca; border-radius: 8px; padding: 10px 14px; font-weight: 500;"
            )
        elif self._pending_batch_start is not None or self.session_manager.busy:
            message = "Đang đăng nhập; tài khoản READY sẽ tự bắt đầu batch độc lập."
            self.start_requirements.setStyleSheet(
                "background: #eff6ff; color: #1e40af; border: 1.5px solid #bfdbfe; border-radius: 8px; padding: 10px 14px; font-weight: 500;"
            )
        elif batch.running:
            message = "Các worker Muse được chọn đang chạy độc lập."
            self.start_requirements.setStyleSheet(
                "background: #eff6ff; color: #1e40af; border: 1.5px solid #bfdbfe; border-radius: 8px; padding: 10px 14px; font-weight: 500;"
            )
        elif not_ready:
            if self._connection_mode() == MuseSessionOpenMode.EXISTING_TAB:
                message = f"Sẵn sàng kiểm tra {len(selected_ids)} tab đã chọn rồi chạy batch."
            else:
                message = (
                    "Sẵn sàng mở Chrome cho tài khoản "
                    + ", ".join(not_ready)
                    + "; hãy tự hoàn tất Google/Muse, sau đó tool chạy batch."
                )
            self.start_requirements.setStyleSheet(
                "background: #fffbeb; color: #92400e; border: 1.5px solid #fde68a; border-radius: 8px; padding: 10px 14px; font-weight: 500;"
            )
        else:
            labels = ", ".join(str(value) for value in selected_ids)
            count_label = f"{len(batch.source_paths)} ảnh" if batch.source_paths else "ảnh từ thư mục đã chọn"
            message = f"Sẵn sàng chạy tài khoản {labels} với {count_label}."
            self.start_requirements.setStyleSheet(
                "background: #f0fdf4; color: #166534; border: 1.5px solid #bbf7d0; border-radius: 8px; padding: 10px 14px; font-weight: 600;"
            )
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
        widgets.enabled.setEnabled(
            not self.batch_manager.busy
            and not self.session_manager.busy
            and self._pending_batch_start is None
        )
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
        login_future, prompt, settings, output_dir, selected_ids = pending
        if not login_future.done():
            ready_count = sum(
                1
                for item in self.session_manager.snapshots()
                if item.session_id in selected_ids
                and item.state == MuseSessionState.READY
                and item.driver_open
            )
            self.global_status.setText(
                f"Đang chờ các phiên đã tick READY ({ready_count}/{len(selected_ids)}); chưa worker nào gửi prompt."
            )
            return
        ready_ids = {
            item.session_id
            for item in self.session_manager.snapshots()
            if item.session_id in selected_ids
            and item.state == MuseSessionState.READY
            and item.driver_open
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
            self._track(
                self.batch_manager.start_all(
                    prompt,
                    settings,
                    output_dir,
                    worker_ids=ready_ids,
                )
            )
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
                "Các Chrome profile đã chọn đã READY. Có thể bấm Quét tab Muse hoặc bấm Xử lý."
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
