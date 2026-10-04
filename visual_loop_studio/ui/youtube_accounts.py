from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from auth.google_youtube import (
    GoogleDesktopClientConfig,
    GoogleYouTubeAuthService,
    YouTubeAccount,
    YouTubeAccountStore,
)
from auth.youtube_studio import open_youtube_studio
from models.settings_model import AppSettings
from ui.common import FileField


class YouTubeAuthWorker(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.running = False
        self._cancel = threading.Event()

    def start(self, operation: str, callback: Callable[[], object]) -> None:
        if self.running:
            raise RuntimeError("Một tác vụ xác thực YouTube đang chạy.")
        self.running = True
        self._cancel.clear()
        threading.Thread(target=self._run, args=(operation, callback), daemon=True).start()

    def cancel(self) -> None:
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _run(self, operation: str, callback: Callable[[], object]) -> None:
        try:
            result = callback()
        except Exception as exc:
            self.running = False
            self.failed.emit(operation, str(exc))
        else:
            self.running = False
            self.finished.emit(operation, result)


class YouTubeAccountsPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.store = YouTubeAccountStore()
        self.worker = YouTubeAuthWorker(self)
        self.worker.finished.connect(self._operation_finished)
        self.worker.failed.connect(self._operation_failed)
        self._build_ui()
        self.reload_accounts()

    @property
    def busy(self) -> bool:
        return self.worker.running

    def shutdown(self) -> None:
        self.worker.cancel()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        title = QLabel("Tài khoản Google / YouTube")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        notice = QLabel(
            "Ứng dụng dùng OAuth 2.0 Desktop App + PKCE. Google mở trong trình duyệt hệ thống; "
            "Visual Loop Studio không nhận, tự điền hoặc lưu mật khẩu. Chỉ scope youtube.readonly được yêu cầu."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        layout.addWidget(notice)

        oauth_group = QGroupBox("Kết nối bằng Google OAuth")
        oauth_layout = QVBoxLayout(oauth_group)
        self.client_file = FileField("OAuth client JSON (loại Desktop app)", "JSON (*.json);;All files (*.*)")
        self.client_file.setText(self.settings.google_oauth_client_json)
        self.client_file.changed.connect(self._client_path_changed)
        oauth_layout.addWidget(self.client_file)
        identity_row = QHBoxLayout()
        self.email = QLineEdit()
        self.email.setPlaceholderText("Email dùng làm login_hint và nhãn nhận diện (không phải mật khẩu)")
        self.channel_id = QLineEdit()
        self.channel_id.setPlaceholderText("YouTube Channel ID cần xác nhận (khuyên dùng)")
        identity_row.addWidget(self.email, 1)
        identity_row.addWidget(self.channel_id, 1)
        oauth_layout.addLayout(identity_row)
        action_row = QHBoxLayout()
        self.connect_button = QPushButton("Đăng nhập Google")
        self.connect_button.setObjectName("primary")
        self.relogin_button = QPushButton("Đăng nhập lại")
        self.verify_button = QPushButton("Kiểm tra đúng kênh")
        self.disconnect_button = QPushButton("Ngắt kết nối + thu hồi token")
        self.connect_button.clicked.connect(self._connect)
        self.relogin_button.clicked.connect(self._relogin)
        self.verify_button.clicked.connect(self._verify)
        self.disconnect_button.clicked.connect(self._disconnect)
        for button in (
            self.connect_button,
            self.relogin_button,
            self.verify_button,
            self.disconnect_button,
        ):
            action_row.addWidget(button)
        action_row.addStretch()
        oauth_layout.addLayout(action_row)
        layout.addWidget(oauth_group)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Nhãn tài khoản", "Tên kênh", "Channel ID", "Trạng thái", "Cập nhật"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        studio_group = QGroupBox("YouTube Studio trong Chrome profile riêng")
        studio_layout = QVBoxLayout(studio_group)
        studio_note = QLabel(
            "Lần đầu, hãy tự đăng nhập và hoàn tất 2FA trong Chrome/Edge. Những lần sau app mở lại đúng profile. "
            "Nếu phiên hết hạn, Google sẽ hiện màn hình yêu cầu xác thực lại. Không có tự điền email/mật khẩu, CAPTCHA hoặc 2FA."
        )
        studio_note.setWordWrap(True)
        studio_note.setObjectName("muted")
        studio_layout.addWidget(studio_note)
        studio_row = QHBoxLayout()
        self.open_studio_button = QPushButton("Mở YouTube Studio")
        self.open_studio_button.clicked.connect(self._open_studio)
        self.status = QLabel("Sẵn sàng")
        self.status.setObjectName("muted")
        studio_row.addWidget(self.open_studio_button)
        studio_row.addWidget(self.status, 1)
        studio_layout.addLayout(studio_row)
        layout.addWidget(studio_group)
        self._update_buttons()

    def reload_accounts(self) -> None:
        accounts = self.store.all()
        self.table.setRowCount(len(accounts))
        for row, account in enumerate(accounts):
            values = (
                account.email_label,
                account.channel_title,
                account.channel_id,
                "Cần đăng nhập lại" if account.needs_reauth else "Đã kết nối",
                account.updated_at,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(256, account.account_id)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self._update_buttons()

    def _client_path_changed(self, value: str) -> None:
        self.settings.google_oauth_client_json = value.strip()
        self.settings_changed.emit()

    def _service(self) -> GoogleYouTubeAuthService:
        path = self.client_file.text()
        if not path or not Path(path).is_file():
            raise ValueError("Hãy chọn OAuth client JSON loại Desktop app trước.")
        return GoogleYouTubeAuthService(GoogleDesktopClientConfig.from_file(path), store=self.store)

    def _connect(self) -> None:
        email = self.email.text().strip()
        if not email:
            QMessageBox.warning(self, "Thiếu nhãn tài khoản", "Hãy nhập email để làm login_hint và nhãn nhận diện.")
            return
        try:
            service = self._service()
            self._run(
                "connect",
                lambda: service.authenticate(
                    email,
                    self.channel_id.text().strip(),
                    cancelled=self.worker.cancelled,
                ),
                "Đang chờ bạn xác nhận trong trình duyệt hệ thống…",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Không thể bắt đầu OAuth", str(exc))

    def _relogin(self) -> None:
        account = self._selected_account()
        if not account:
            return
        try:
            service = self._service()
            self._run(
                "relogin",
                lambda: service.relogin(account.account_id, cancelled=self.worker.cancelled),
                "Đang chờ xác nhận đăng nhập lại trong trình duyệt…",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Không thể đăng nhập lại", str(exc))

    def _verify(self) -> None:
        account = self._selected_account()
        if not account:
            return
        try:
            service = self._service()
            self._run(
                "verify",
                lambda: service.verify_account(account.account_id),
                "Đang làm mới token và kiểm tra channels.list(mine=true)…",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Không thể kiểm tra kênh", str(exc))

    def _disconnect(self) -> None:
        account = self._selected_account()
        if not account:
            return
        answer = QMessageBox.question(
            self,
            "Ngắt kết nối YouTube",
            f"Thu hồi quyền Google và xóa refresh token của kênh {account.channel_title}?\n\n"
            "Chrome profile YouTube Studio được giữ lại; bạn có thể tự xóa riêng nếu cần.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            service = self._service()
            self._run(
                "disconnect",
                lambda: service.disconnect(account.account_id, revoke=True),
                "Đang thu hồi token tại Google…",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Không thể ngắt kết nối", str(exc))

    def _open_studio(self) -> None:
        account = self._selected_account()
        if not account:
            return
        try:
            _process, profile = open_youtube_studio(account.account_id, account.chrome_profile_dir)
            self.status.setText(f"Đã mở YouTube Studio bằng profile riêng: {profile}")
        except Exception as exc:
            QMessageBox.critical(self, "Không thể mở YouTube Studio", str(exc))

    def _run(self, operation: str, callback: Callable[[], object], message: str) -> None:
        self.worker.start(operation, callback)
        self.status.setText(message)
        self._update_buttons()

    def _operation_finished(self, operation: str, result: object) -> None:
        self.reload_accounts()
        messages = {
            "connect": "Đã kết nối và kiểm tra đúng kênh YouTube.",
            "relogin": "Đã đăng nhập lại và thay refresh token an toàn.",
            "verify": "Token hợp lệ và tài khoản vẫn sở hữu đúng kênh.",
            "disconnect": "Đã thu hồi token và ngắt kết nối tài khoản.",
        }
        self.status.setText(messages.get(operation, "Tác vụ hoàn tất."))
        if isinstance(result, YouTubeAccount):
            self.channel_id.setText(result.channel_id)
        self._update_buttons()

    def _operation_failed(self, operation: str, message: str) -> None:
        self.reload_accounts()
        self.status.setText("Tác vụ thất bại; không có thông tin xác thực nào được hiển thị trong lỗi.")
        self._update_buttons()
        QMessageBox.critical(self, "Xác thực Google/YouTube thất bại", message)

    def _selection_changed(self) -> None:
        account = self._selected_account(show_warning=False)
        if account:
            self.email.setText(account.email_label)
            self.channel_id.setText(account.channel_id)
        self._update_buttons()

    def _selected_account(self, *, show_warning: bool = True) -> YouTubeAccount | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        account = self.store.get(str(item.data(256))) if item else None
        if not account and show_warning:
            QMessageBox.warning(self, "Chưa chọn tài khoản", "Hãy chọn một tài khoản YouTube trong bảng.")
        return account

    def _update_buttons(self) -> None:
        selected = self._selected_account(show_warning=False) is not None
        enabled = not self.worker.running
        self.connect_button.setEnabled(enabled)
        self.relogin_button.setEnabled(enabled and selected)
        self.verify_button.setEnabled(enabled and selected)
        self.disconnect_button.setEnabled(enabled and selected)
        self.open_studio_button.setEnabled(enabled and selected)
