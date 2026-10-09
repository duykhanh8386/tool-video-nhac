from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from auth.muse_cookies import (
    MUSE_COOKIE_PROFILES_DIR,
    MUSE_DEFAULT_URL,
    MuseCookieAccount,
    MuseCookieAccountStore,
    check_single_cookie_account,
    inject_cookies_to_driver,
    parse_muse_cookies,
)
from auth.muse_generation import (
    ATTACH_TEXTS,
    DOWNLOAD_TEXTS,
    PROMPT_SELECTORS,
    QUOTA_MARKERS,
    SUBMIT_TEXTS,
    _body_text,
    _click_by_text,
    _clickable,
    _find,
    _hostname,
    _safe_url,
    _video_fingerprint,
    _visible,
)
from auth.muse_login import (
    MUSE_ALLOWED_HOSTS,
    MUSE_START_URL,
    create_muse_chrome_driver,
    validate_muse_start_url,
)
from auth.muse_sessions import (
    MuseSessionManager,
    get_muse_session_manager,
)
from auth.muse_video_batch import (
    MUSE_SESSION_DOWNLOADS_DIR,
    MuseVideoBatchManager,
    MuseVideoSettings,
    SUPPORTED_IMAGE_SUFFIXES,
    get_muse_video_batch_manager,
)
from models.settings_model import AppSettings
from utils.paths import CACHE_DIR


ACTIVE_MODE_STYLE = (
    "QPushButton { "
    "background: #2563eb; color: #ffffff; font-weight: 700; font-size: 13px; "
    "border: 1.5px solid #1d4ed8; border-radius: 7px; padding: 6px 14px; "
    "} "
    "QPushButton:hover { background: #1d4ed8; }"
)

INACTIVE_MODE_STYLE = (
    "QPushButton { "
    "background: #ffffff; color: #475569; font-weight: 600; font-size: 13px; "
    "border: 1.5px solid #cbd5e1; border-radius: 7px; padding: 6px 14px; "
    "} "
    "QPushButton:hover { background: #f8fafc; color: #1e293b; border-color: #94a3b8; }"
)


class PasteCookieDialog(QDialog):
    """Hộp thoại dán trực tiếp chuỗi JSON hoặc text cookie."""

    def __init__(self, default_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("➕ Dán Cookie Tài Khoản Muse AI")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        hint = QLabel(
            "📋 Hướng dẫn: Đăng nhập vào Muse AI (qua Google) trên trình duyệt, dùng extension "
            "(như Cookie-Editor, J2Team) xuất cookie dạng JSON rồi dán vào khung bên dưới.\n"
            "Tool sẽ tự động nhận diện cookie và email tài khoản."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #475569; font-size: 12px; background: #f1f5f9; padding: 8px; border-radius: 6px;")
        layout.addWidget(hint)

        form = QFormLayout()
        self.name_edit = QLineEdit(default_name)
        self.name_edit.setPlaceholderText("Tên tài khoản (ví dụ: Muse 1)")
        self.name_edit.setMinimumHeight(32)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("(Tùy chọn) Nhập email hoặc để trống để tool tự quét...")
        self.email_edit.setMinimumHeight(32)

        form.addRow("Tên hiển thị:", self.name_edit)
        form.addRow("Email tài khoản:", self.email_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("Dán nội dung Cookie (JSON / Text):"))
        self.cookie_text = QPlainTextEdit()
        self.cookie_text.setPlaceholderText(
            '[\n  {\n    "name": "session",\n    "value": "...",\n    "domain": ".muse.ai"\n  }\n]'
        )
        self.cookie_text.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px; border: 1.5px solid #cbd5e1; border-radius: 6px;"
        )
        layout.addWidget(self.cookie_text)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("✔ Thêm tài khoản")
        btn_box.button(QDialogButtonBox.Cancel).setText("Hủy")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def get_data(self) -> tuple[str, str, str]:
        return (
            self.cookie_text.toPlainText().strip(),
            self.name_edit.text().strip(),
            self.email_edit.text().strip(),
        )


class EditEmailDialog(QDialog):
    """Hộp thoại sửa / gắn email cho tài khoản đang xem."""

    def __init__(self, current_name: str, current_email: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("✉ Cập Nhật Email Tài Khoản")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel(f"Cập nhật địa chỉ Email cho tài khoản: <b>{current_name}</b>"))
        self.email_edit = QLineEdit(current_email)
        self.email_edit.setPlaceholderText("example@gmail.com")
        self.email_edit.setMinimumHeight(34)
        layout.addWidget(self.email_edit)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("Lưu Email")
        btn_box.button(QDialogButtonBox.Cancel).setText("Hủy")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def get_email(self) -> str:
        return self.email_edit.text().strip()


class InspectCookieDialog(QDialog):
    """Hộp thoại đối chiếu / kiểm tra chi tiết cấu trúc cookie của tài khoản."""

    def __init__(self, account: MuseCookieAccount, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"🔑 Đối Chiếu Cookie — {account.name}")
        self.setMinimumWidth(640)
        self.setMinimumHeight(480)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        info = QLabel(
            f"<b>Tài khoản:</b> {account.name} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Email:</b> {account.email or 'Chưa xác định'} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Trạng thái:</b> {account.status_display}<br>"
            f"<b>Tổng số cookies:</b> {len(account.cookies)} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Lần kiểm tra cuối:</b> {account.last_checked or 'Chưa kiểm tra'}"
        )
        info.setStyleSheet("background: #f8fafc; border: 1px solid #cbd5e1; padding: 10px; border-radius: 6px;")
        layout.addWidget(info)

        layout.addWidget(QLabel("Chi tiết các cookie keys & giá trị:"))
        detail_view = QPlainTextEdit()
        detail_view.setReadOnly(True)
        detail_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")

        lines = []
        for idx, c in enumerate(account.cookies, 1):
            name = c.get("name", "")
            domain = c.get("domain", "")
            path = c.get("path", "")
            val = str(c.get("value", ""))
            val_masked = val if len(val) <= 40 else val[:36] + "..."
            exp = c.get("expiry", "Session")
            lines.append(f"[{idx}] Name: {name} | Domain: {domain} | Path: {path} | Expiry: {exp}\n    Value: {val_masked}\n")

        if not lines:
            lines.append("Không có cookie nào.")

        detail_view.setPlainText("\n".join(lines))
        layout.addWidget(detail_view)

        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)


class MuseCookieCheckerThread(QThread):
    """Luồng kiểm tra cookie tài khoản Muse trong nền."""

    account_checked = Signal(str, bool, str, str)  # account_id, is_active, message, time_str
    all_finished = Signal(int, int)  # active_count, total_count

    def __init__(self, accounts: list[MuseCookieAccount], headless: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.accounts = accounts
        self.headless = headless
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        active_count = 0
        now_str = time.strftime("%d/%m %H:%M")
        for acc in self.accounts:
            if self._stopped:
                break
            try:
                is_active, _detected_email, msg = check_single_cookie_account(acc, headless=self.headless)
            except Exception as exc:
                is_active = False
                msg = f"Lỗi: {exc}"

            if is_active:
                active_count += 1
            self.account_checked.emit(acc.account_id, is_active, msg, now_str)

        self.all_finished.emit(active_count, len(self.accounts))


class MuseCookieBatchRunnerThread(QThread):
    """Luồng chạy batch tạo video/ảnh qua danh sách tài khoản Cookie (Concurrency 1-5)."""

    progress_signal = Signal(int, str)
    stats_signal = Signal(int, int, int, int)  # pending, completed, failed, quota
    log_signal = Signal(str)
    worker_status_signal = Signal(int, str)  # worker_idx (1-5), status_text
    account_completed_signal = Signal(str, int)  # account_id, count
    batch_finished_signal = Signal(bool, str)

    def __init__(
        self,
        accounts: list[MuseCookieAccount],
        concurrency: int,
        headless: bool,
        task_mode: str,
        prompt: str,
        settings: MuseVideoSettings,
        output_dir: Path,
        source_items: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.accounts = accounts
        self.concurrency = max(1, min(5, concurrency))
        self.headless = headless
        self.task_mode = task_mode
        self.prompt = prompt
        self.settings = settings
        self.output_dir = output_dir
        self.source_items = source_items
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        if not self.accounts:
            self.batch_finished_signal.emit(False, "Không có tài khoản nào được tick để chạy.")
            return

        total_items = len(self.source_items)
        if total_items == 0:
            self.batch_finished_signal.emit(False, "Không có mục nào để xử lý.")
            return

        action_name = "ảnh" if self.task_mode == "image" else "video"
        self.log_signal.emit(
            f"🚀 Bắt đầu batch {action_name}: {total_items} mục trên {len(self.accounts)} tài khoản "
            f"(Chạy song song: {self.concurrency} tài khoản cùng lúc, Chạy ẩn: {self.headless})."
        )

        # Phân bổ round-robin các source_items cho từng tài khoản
        account_assignments: dict[str, list[str]] = {acc.account_id: [] for acc in self.accounts}
        for idx, item in enumerate(self.source_items):
            target_acc = self.accounts[idx % len(self.accounts)]
            account_assignments[target_acc.account_id].append(item)

        pending_total = total_items
        completed_total = 0
        failed_total = 0
        quota_total = 0
        self.stats_signal.emit(pending_total, completed_total, failed_total, quota_total)

        # Worker pool execution
        account_queue = [acc for acc in self.accounts if account_assignments[acc.account_id]]
        queue_lock = threading.Lock()

        def worker_task(worker_id: int) -> None:
            nonlocal pending_total, completed_total, failed_total, quota_total
            while not self._stopped:
                current_acc = None
                with queue_lock:
                    if account_queue:
                        current_acc = account_queue.pop(0)
                if not current_acc:
                    self.worker_status_signal.emit(worker_id, "Sẵn sàng (Đã hết hàng đợi)")
                    break

                assigned = account_assignments[current_acc.account_id]
                acc_label = current_acc.combo_label
                self.worker_status_signal.emit(worker_id, f"Khởi tạo Chrome [{acc_label}]…")
                self.log_signal.emit(f"⚙ [Luồng {worker_id}] Nhận tài khoản {acc_label}: {len(assigned)} {action_name}.")

                profile_dir = MUSE_COOKIE_PROFILES_DIR / current_acc.account_id
                profile_dir.mkdir(parents=True, exist_ok=True)
                download_dir = Path(tempfile.mkdtemp(prefix=f"muse_w{worker_id}_", dir=CACHE_DIR))

                driver = None
                acc_completed = 0
                try:
                    driver = create_muse_chrome_driver(profile_dir, download_dir=download_dir, headless=self.headless)
                    driver.set_page_load_timeout(35)
                    self.worker_status_signal.emit(worker_id, f"Nạp cookie [{acc_label}]…")
                    inject_cookies_to_driver(driver, current_acc.cookies, target_url=MUSE_DEFAULT_URL)
                    time.sleep(1.5)

                    for item_idx, item in enumerate(assigned, 1):
                        if self._stopped:
                            break

                        item_name = Path(item).name if not str(item).startswith("Prompt #") else str(item)
                        self.worker_status_signal.emit(
                            worker_id,
                            f"[{acc_label}] Đang tạo {action_name} ({item_idx}/{len(assigned)}): {item_name}",
                        )
                        self.log_signal.emit(f"🎬 [Luồng {worker_id}] {acc_label}: Bắt đầu xử lý {item_name}…")

                        try:
                            # 1. Upload ảnh nếu có ảnh mẫu
                            source_path = Path(item) if not str(item).startswith("Prompt #") else None
                            if source_path and source_path.is_file():
                                inputs = _find(driver, "css selector", "input[type='file']")
                                if not inputs:
                                    _click_by_text(driver, ATTACH_TEXTS)
                                    inputs = _find(driver, "css selector", "input[type='file']")
                                for field in reversed(inputs):
                                    try:
                                        field.send_keys(str(source_path.resolve()))
                                        break
                                    except Exception:
                                        continue
                                time.sleep(1.0)

                            # 2. Điền prompt
                            for selector in PROMPT_SELECTORS:
                                fields = _find(driver, "css selector", selector)
                                filled = False
                                for f in reversed(fields):
                                    if not _visible(f):
                                        continue
                                    try:
                                        f.click()
                                        tag = str(f.tag_name or "").lower()
                                        if tag == "textarea":
                                            f.clear()
                                        else:
                                            f.send_keys("\ue009", "a")
                                        f.send_keys(self.prompt)
                                        filled = True
                                        break
                                    except Exception:
                                        continue
                                if filled:
                                    break
                            time.sleep(0.5)

                            # 3. Gửi / Bấm Create
                            submitted = _click_by_text(driver, SUBMIT_TEXTS)
                            if not submitted:
                                for btn in reversed(_find(driver, "css selector", "button[type='submit']")):
                                    if _clickable(btn):
                                        btn.click()
                                        submitted = True
                                        break

                            # 4. Chờ tạo kết quả và tải xuống
                            start_wait = time.monotonic()
                            baseline_vids = {_video_fingerprint(v) for v in _find(driver, "css selector", "video")}
                            video_found = False
                            while time.monotonic() - start_wait < 180.0:
                                if self._stopped:
                                    break
                                body = _body_text(driver).lower()
                                if any(m in body for m in QUOTA_MARKERS):
                                    quota_total += 1
                                    raise RuntimeError(f"Hết quota/credit trên tài khoản {acc_label}.")

                                for v in reversed(_find(driver, "css selector", "video")):
                                    if _visible(v) and _video_fingerprint(v) not in baseline_vids:
                                        video_found = True
                                        break
                                if video_found:
                                    break
                                time.sleep(1.5)

                            # 5. Yêu cầu tải xuống
                            _click_by_text(driver, DOWNLOAD_TEXTS, reverse=True)

                            # 6. Chờ file tải về download_dir
                            downloaded_file = None
                            start_dl_wait = time.monotonic()
                            while time.monotonic() - start_dl_wait < 60.0:
                                if self._stopped:
                                    break
                                files = [
                                    p for p in download_dir.iterdir()
                                    if p.is_file() and not p.name.endswith((".crdownload", ".tmp"))
                                ]
                                if files:
                                    downloaded_file = max(files, key=lambda p: p.stat().st_mtime)
                                    time.sleep(0.5)
                                    break
                                time.sleep(1.0)

                            if downloaded_file and downloaded_file.exists():
                                self.output_dir.mkdir(parents=True, exist_ok=True)
                                target_dest = self.output_dir / f"{Path(item_name).stem}_{int(time.time())}{downloaded_file.suffix}"
                                shutil.move(str(downloaded_file), str(target_dest))
                                completed_total += 1
                                acc_completed += 1
                                self.log_signal.emit(f"✔ [Luồng {worker_id}] {acc_label}: Đã lưu {target_dest.name}")
                            else:
                                completed_total += 1
                                acc_completed += 1
                                self.log_signal.emit(f"✔ [Luồng {worker_id}] {acc_label}: Đã gửi thành công {item_name}.")

                        except Exception as item_err:
                            failed_total += 1
                            self.log_signal.emit(f"❌ [Luồng {worker_id}] {acc_label} lỗi khi xử lý {item_name}: {item_err}")
                            if "quota" in str(item_err).lower():
                                break

                        pending_total = max(0, total_items - (completed_total + failed_total))
                        pct = int(((completed_total + failed_total) / total_items) * 100)
                        self.progress_signal.emit(pct, f"Đang xử lý: {completed_total}/{total_items} hoàn tất")
                        self.stats_signal.emit(pending_total, completed_total, failed_total, quota_total)

                    self.account_completed_signal.emit(current_acc.account_id, acc_completed)
                except Exception as acc_err:
                    self.log_signal.emit(f"❌ [Luồng {worker_id}] Lỗi tài khoản {acc_label}: {acc_err}")
                finally:
                    if driver:
                        try:
                            driver.quit()
                        except Exception:
                            pass
                    shutil.rmtree(download_dir, ignore_errors=True)

            self.worker_status_signal.emit(worker_id, "Đã hoàn thành")

        # Launch concurrent workers
        threads = []
        for w_idx in range(1, self.concurrency + 1):
            t = threading.Thread(target=worker_task, args=(w_idx,), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.progress_signal.emit(100, f"Hoàn tất: {completed_total} thành công, {failed_total} lỗi")
        self.log_signal.emit(f"🏁 Đã hoàn tất toàn bộ batch: {completed_total}/{total_items} thành công.")
        self.batch_finished_signal.emit(True, f"Đã xử lý xong batch: {completed_total} thành công.")


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
        self.cookie_store = MuseCookieAccountStore()
        self.session_manager = session_manager or get_muse_session_manager(
            start_url=self.settings.muse_start_url or MUSE_START_URL,
        )
        self.batch_manager = batch_manager or get_muse_video_batch_manager(
            session_manager=self.session_manager,
        )

        self._active_runner: MuseCookieBatchRunnerThread | None = None
        self._active_checker: MuseCookieCheckerThread | None = None
        self._refreshing = False

        self._build_ui()
        self._reload_accounts_table()
        self._update_task_mode_ui()

    @property
    def busy(self) -> bool:
        return (
            (self._active_runner is not None and self._active_runner.isRunning())
            or (self._active_checker is not None and self._active_checker.isRunning())
        )

    def shutdown(self) -> None:
        if self._active_runner and self._active_runner.isRunning():
            self._active_runner.stop()
            self._active_runner.wait(3000)
        if self._active_checker and self._active_checker.isRunning():
            self._active_checker.stop()
            self._active_checker.wait(3000)
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

        title = QLabel("✨ Muse AI Studio — Batch Ảnh/Video (Quản lý Cookie & Chạy Ngầm)")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        notice = QLabel(
            "💡 Tự động đăng nhập vào Muse qua Cookie xuất từ trình duyệt. Không cần nhập mật khẩu Google, "
            "hỗ trợ chạy ngầm hoàn toàn và chạy song song lần lượt từ 1 đến 5 tài khoản cùng lúc đến hết danh sách."
        )
        notice.setWordWrap(True)
        notice.setObjectName("notice")
        root.addWidget(notice)

        # =========================================================================
        # TOOLBAR QUẢN LÝ COOKIE (KHỚP 100% ẢNH MẪU CỦA NGƯỜI DÙNG)
        # =========================================================================
        toolbar_box = QGroupBox("🌐 Quản lý Tài khoản Muse AI (Cookie)")
        toolbar_vbox = QVBoxLayout(toolbar_box)
        toolbar_vbox.setContentsMargins(10, 8, 10, 10)
        toolbar_vbox.setSpacing(8)

        # Dòng 1: Label chú thích giống hệt ảnh mẫu
        hint_label = QLabel(
            "Tài khoản đang XEM (dùng cho nút Kiểm tra / Email / Xoá — KHÔNG phải tài khoản đang chạy; tài khoản chạy = các ô ☑ bên dưới)"
        )
        hint_label.setStyleSheet("color: #475569; font-size: 12px; font-weight: 500;")
        toolbar_vbox.addWidget(hint_label)

        # Dòng 2: Hàng các nút và điều khiển giống hệt ảnh mẫu
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)

        # Dropdown tài khoản đang xem
        self.viewing_account_combo = QComboBox()
        self.viewing_account_combo.setMinimumHeight(32)
        self.viewing_account_combo.setMinimumWidth(260)
        self.viewing_account_combo.currentIndexChanged.connect(self._on_viewing_account_changed)
        row_layout.addWidget(self.viewing_account_combo)

        # Trạng thái tài khoản đang xem
        self.viewing_status_label = QLabel("⚪ Chưa kiểm tra")
        self.viewing_status_label.setStyleSheet("font-weight: 600; color: #0284c7; padding: 0 4px;")
        row_layout.addWidget(self.viewing_status_label)

        # Nút Thư mục Cookie
        self.btn_cookie_folder = QPushButton("📁 Thư mục Cookie")
        self.btn_cookie_folder.setMinimumHeight(32)
        self.btn_cookie_folder.clicked.connect(self._import_cookie_folder)
        row_layout.addWidget(self.btn_cookie_folder)

        # Nút Dán cookie
        self.btn_paste_cookie = QPushButton("+ Dán cookie")
        self.btn_paste_cookie.setMinimumHeight(32)
        self.btn_paste_cookie.clicked.connect(self._open_paste_cookie_dialog)
        row_layout.addWidget(self.btn_paste_cookie)

        # Nút Email
        self.btn_edit_email = QPushButton("✉ Email")
        self.btn_edit_email.setMinimumHeight(32)
        self.btn_edit_email.clicked.connect(self._open_edit_email_dialog)
        row_layout.addWidget(self.btn_edit_email)

        # Nút Đối chiếu cookie
        self.btn_compare_cookie = QPushButton("🔑 Đối chiếu cookie")
        self.btn_compare_cookie.setMinimumHeight(32)
        self.btn_compare_cookie.clicked.connect(self._open_compare_cookie_dialog)
        row_layout.addWidget(self.btn_compare_cookie)

        # Nút Xoá
        self.btn_delete_account = QPushButton("Xoá")
        self.btn_delete_account.setMinimumHeight(32)
        self.btn_delete_account.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1px solid #f87171; color: #991b1b; font-weight: 600; } "
            "QPushButton:hover { background: #fecaca; }"
        )
        self.btn_delete_account.clicked.connect(self._delete_current_account)
        row_layout.addWidget(self.btn_delete_account)

        # Nút Xoá tất cả TK
        self.btn_delete_all = QPushButton("🗑 Xoá tất cả TK")
        self.btn_delete_all.setMinimumHeight(32)
        self.btn_delete_all.clicked.connect(self._delete_all_accounts)
        row_layout.addWidget(self.btn_delete_all)

        # Nút Kiểm tra
        self.btn_check_account = QPushButton("Kiểm tra")
        self.btn_check_account.setMinimumHeight(32)
        self.btn_check_account.clicked.connect(self._check_current_account)
        row_layout.addWidget(self.btn_check_account)

        # Nút Kiểm tra tất cả
        self.btn_check_all = QPushButton("Kiểm tra tất cả")
        self.btn_check_all.setMinimumHeight(32)
        self.btn_check_all.clicked.connect(self._check_all_accounts)
        row_layout.addWidget(self.btn_check_all)

        # Checkbox Chạy ẩn
        self.headless_check = QCheckBox("🔒 Chạy ẩn")
        self.headless_check.setStyleSheet("font-weight: 600; color: #1e293b; padding-left: 4px;")
        self.headless_check.setChecked(bool(getattr(self.settings, "muse_headless", True)))
        self.headless_check.toggled.connect(self._on_headless_toggled)
        row_layout.addWidget(self.headless_check)

        row_layout.addStretch()
        toolbar_vbox.addWidget(row_widget)
        root.addWidget(toolbar_box)

        # =========================================================================
        # BẢNG DANH SÁCH TÀI KHOẢN (CHECKBOX CHỌN CHẠY & CONCURRENCY 1-5)
        # =========================================================================
        accounts_card = QGroupBox("👥 Danh Sách Tài Khoản Sẵn Sàng (Tick các tài khoản muốn chạy)")
        accounts_vbox = QVBoxLayout(accounts_card)
        accounts_vbox.setSpacing(8)

        # Thanh cấu hình Concurrency & chọn nhanh
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(12)

        lbl_conc = QLabel("⚡ Chạy cùng lúc:")
        lbl_conc.setStyleSheet("font-weight: 700; color: #1e293b; font-size: 13px;")
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 5)
        self.concurrency_spin.setValue(int(getattr(self.settings, "muse_concurrency", 1) or 1))
        self.concurrency_spin.setMinimumHeight(32)
        self.concurrency_spin.setMinimumWidth(60)
        self.concurrency_spin.valueChanged.connect(self._on_concurrency_changed)

        lbl_conc_desc = QLabel("tài khoản song song (1 - 5). Sẽ chạy lần lượt các tài khoản được tick đến hết.")
        lbl_conc_desc.setStyleSheet("color: #475569; font-size: 12px;")

        self.btn_select_all = QPushButton("☑ Chọn tất cả")
        self.btn_select_all.setMinimumHeight(30)
        self.btn_select_all.clicked.connect(lambda: self._set_all_enabled(True))

        self.btn_deselect_all = QPushButton("☐ Bỏ chọn tất cả")
        self.btn_deselect_all.setMinimumHeight(30)
        self.btn_deselect_all.clicked.connect(lambda: self._set_all_enabled(False))

        cfg_row.addWidget(lbl_conc)
        cfg_row.addWidget(self.concurrency_spin)
        cfg_row.addWidget(lbl_conc_desc)
        cfg_row.addStretch()
        cfg_row.addWidget(self.btn_select_all)
        cfg_row.addWidget(self.btn_deselect_all)
        accounts_vbox.addLayout(cfg_row)

        # Bảng hiển thị danh sách tài khoản
        self.accounts_table = QTableWidget()
        self.accounts_table.setColumnCount(7)
        self.accounts_table.setHorizontalHeaderLabels([
            "Chạy", "Tên tài khoản", "Email Google / Muse", "Trạng thái",
            "Lần kiểm tra cuối", "Số cookies", "Đã hoàn thành"
        ])
        self.accounts_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.accounts_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.accounts_table.setMinimumHeight(160)
        self.accounts_table.setMaximumHeight(240)
        self.accounts_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.accounts_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self.accounts_table.itemClicked.connect(self._on_table_row_clicked)
        accounts_vbox.addWidget(self.accounts_table)
        root.addWidget(accounts_card)

        # =========================================================================
        # TASK MODE TOGGLE (VIDEO vs ẢNH)
        # =========================================================================
        mode_container = QWidget()
        mode_container.setStyleSheet(
            "background: #f8fafc; border: 1.5px solid #cbd5e1; border-radius: 10px; padding: 4px;"
        )
        mode_layout = QHBoxLayout(mode_container)
        mode_layout.setContentsMargins(6, 6, 6, 6)
        mode_layout.setSpacing(10)

        mode_desc = QLabel("🛠 Chế độ tác vụ AI:")
        mode_desc.setStyleSheet("color: #1e293b; font-weight: 700; font-size: 13px; padding-left: 6px;")
        mode_layout.addWidget(mode_desc)

        self.btn_mode_video = QPushButton("🎬 Tạo Video AI (Image-to-Video)")
        self.btn_mode_video.setMinimumHeight(40)
        self.btn_mode_video.setCursor(Qt.PointingHandCursor)
        self.btn_mode_video.clicked.connect(lambda: self._set_task_mode("video"))

        self.btn_mode_image = QPushButton("🎨 Tạo Ảnh AI (Image Generation)")
        self.btn_mode_image.setMinimumHeight(40)
        self.btn_mode_image.setCursor(Qt.PointingHandCursor)
        self.btn_mode_image.clicked.connect(lambda: self._set_task_mode("image"))

        mode_layout.addWidget(self.btn_mode_video, 1)
        mode_layout.addWidget(self.btn_mode_image, 1)
        root.addWidget(mode_container)

        # =========================================================================
        # NGUỒN ẢNH & THƯ MỤC LƯU
        # =========================================================================
        self.source_group = QGroupBox("📁 Nguồn Ảnh & Thư Mục Xuất File")
        source_grid = QGridLayout(self.source_group)
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
        self.output_folder.setPlaceholderText("Chọn hoặc nhập thư mục lưu kết quả...")

        choose_output = QPushButton("📁 Chọn thư mục lưu")
        choose_output.setMinimumHeight(36)
        choose_output.clicked.connect(self._choose_output_folder)

        source_grid.addWidget(QLabel("MUSE_URL:"), 0, 0)
        source_grid.addWidget(self.url, 0, 1, 1, 3)
        self.source_label = QLabel("Thư mục ảnh:")
        source_grid.addWidget(self.source_label, 1, 0)
        source_grid.addWidget(self.image_folder, 1, 1)
        source_grid.addWidget(choose_images, 1, 2)
        source_grid.addWidget(self.recursive, 1, 3)
        source_grid.addWidget(self.image_count, 2, 1, 1, 3)
        source_grid.addWidget(QLabel("Thư mục output:"), 3, 0)
        source_grid.addWidget(self.output_folder, 3, 1, 1, 2)
        source_grid.addWidget(choose_output, 3, 3)
        root.addWidget(self.source_group)

        # =========================================================================
        # PROMPT BOX
        # =========================================================================
        self.prompt_card = QGroupBox("✨ Prompt Tạo Video AI (Motion Prompt)")
        prompt_vbox = QVBoxLayout(self.prompt_card)
        prompt_vbox.setSpacing(8)

        self.prompt_hint = QLabel("Mô tả chuyển động, góc máy, ánh sáng (áp dụng chung cho batch ảnh):")
        self.prompt_hint.setStyleSheet("color: #475569; font-size: 12px; font-weight: 500;")
        prompt_vbox.addWidget(self.prompt_hint)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("✍️ Nhập prompt điều khiển cho AI Muse tại đây...")
        self.prompt.setMinimumHeight(75)
        self.prompt.setStyleSheet(
            "QPlainTextEdit { background: #ffffff; border: 2px solid #3b82f6; border-radius: 8px; padding: 8px; font-size: 13px; }"
        )
        prompt_vbox.addWidget(self.prompt)
        root.addWidget(self.prompt_card)

        # =========================================================================
        # CÀI ĐẶT MUSE
        # =========================================================================
        self.settings_group = QGroupBox("⚙️ Cài đặt Muse")
        settings_layout = QHBoxLayout(self.settings_group)
        settings_layout.setSpacing(20)

        self.aspect_ratio = self._editable_combo(("", "16:9", "9:16", "1:1"))
        self.duration = self._editable_combo(("", "5s", "8s", "10s"))
        self.resolution = self._editable_combo(("", "720p", "1080p"))

        ar_box = QHBoxLayout()
        ar_box.addWidget(QLabel("📐 Tỷ lệ:"))
        self.aspect_ratio.setMinimumHeight(34)
        ar_box.addWidget(self.aspect_ratio)
        settings_layout.addLayout(ar_box)

        self.duration_box_widget = QWidget()
        dur_box = QHBoxLayout(self.duration_box_widget)
        dur_box.setContentsMargins(0, 0, 0, 0)
        dur_box.addWidget(QLabel("⏱ Độ dài:"))
        self.duration.setMinimumHeight(34)
        dur_box.addWidget(self.duration)
        settings_layout.addWidget(self.duration_box_widget)

        self.image_quantity = QSpinBox()
        self.image_quantity.setRange(1, 50)
        self.image_quantity.setValue(1)
        self.image_quantity.setMinimumHeight(34)
        self.image_quantity_widget = QWidget()
        qty_box = QHBoxLayout(self.image_quantity_widget)
        qty_box.setContentsMargins(0, 0, 0, 0)
        qty_box.addWidget(QLabel("🖼 Số lượng ảnh:"))
        qty_box.addWidget(self.image_quantity)
        settings_layout.addWidget(self.image_quantity_widget)

        res_box = QHBoxLayout()
        res_box.addWidget(QLabel("📺 Độ phân giải:"))
        self.resolution.setMinimumHeight(34)
        res_box.addWidget(self.resolution)
        settings_layout.addLayout(res_box)

        settings_layout.addStretch()
        root.addWidget(self.settings_group)

        # =========================================================================
        # ACTIONS BAR (BẮT ĐẦU, DỪNG, PHÂN BỔ)
        # =========================================================================
        actions_card = QWidget()
        actions_card.setStyleSheet("background: #ffffff; border: 1.5px solid #e2e8f0; border-radius: 10px;")
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
        self.start_all.clicked.connect(self._start_batch)

        self.stop_all = QPushButton("⏹ Dừng tất cả")
        self.stop_all.setMinimumHeight(40)
        self.stop_all.setMinimumWidth(130)
        self.stop_all.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #f87171; color: #991b1b; font-weight: 700; border-radius: 7px; padding: 0 16px; font-size: 13px; } "
            "QPushButton:hover { background: #fecaca; } "
            "QPushButton:disabled { background: #f8fafc; border-color: #e2e8f0; color: #cbd5e1; }"
        )
        self.stop_all.setEnabled(False)
        self.stop_all.clicked.connect(self._stop_batch)

        self.allocate = QPushButton("📊 Phân bổ")
        self.allocate.setMinimumHeight(40)
        self.allocate.setMinimumWidth(130)
        self.allocate.clicked.connect(self._allocate_jobs)

        self.clear = QPushButton("🗑 Xóa dữ liệu UI")
        self.clear.setMinimumHeight(40)
        self.clear.clicked.connect(self._clear_ui)

        primary_row.addWidget(self.start_all)
        primary_row.addWidget(self.stop_all)
        primary_row.addWidget(self.allocate)
        primary_row.addWidget(self.clear)
        primary_row.addStretch()
        actions_vbox.addLayout(primary_row)
        root.addWidget(actions_card)

        # Global Status
        self.global_status = QLabel("Tick các tài khoản muốn chạy, phân bổ ảnh/lượt tạo, sau đó bấm Bắt đầu.")
        self.global_status.setWordWrap(True)
        self.global_status.setStyleSheet("color: #475569; font-size: 12px; padding: 2px 4px;")
        root.addWidget(self.global_status)

        # =========================================================================
        # TIẾN ĐỘ & LOG HOẠT ĐỘNG
        # =========================================================================
        progress_card = QGroupBox("📊 Tiến Độ Xử Lý & Log Hoạt Động")
        progress_vbox = QVBoxLayout(progress_card)
        progress_vbox.setSpacing(8)

        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_progress.setMinimumHeight(20)
        progress_vbox.addWidget(self.overall_progress)

        self.stats_label = QLabel("Chờ: 0 • Thành công: 0 • Lỗi: 0 • Quota: 0")
        self.stats_label.setStyleSheet("font-weight: 600; color: #475569; font-size: 12px;")
        progress_vbox.addWidget(self.stats_label)

        # Status labels for workers 1 to 5
        self.worker_status_labels: dict[int, QLabel] = {}
        workers_box = QHBoxLayout()
        workers_box.setSpacing(10)
        for w_idx in range(1, 6):
            lbl = QLabel(f"Luồng {w_idx}: Sẵn sàng")
            lbl.setStyleSheet("font-size: 11px; background: #f1f5f9; padding: 4px 8px; border-radius: 4px; color: #334155;")
            self.worker_status_labels[w_idx] = lbl
            workers_box.addWidget(lbl)
        progress_vbox.addLayout(workers_box)

        # Monospace Console Log
        self.logs_edit = QPlainTextEdit()
        self.logs_edit.setReadOnly(True)
        self.logs_edit.setMinimumHeight(140)
        self.logs_edit.setMaximumHeight(220)
        self.logs_edit.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px; background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 6px; color: #0f172a; padding: 6px;"
        )
        self.logs_edit.setPlaceholderText("Log chi tiết quá trình chạy batch sẽ hiển thị tại đây...")
        progress_vbox.addWidget(self.logs_edit)
        root.addWidget(progress_card)

    @staticmethod
    def _editable_combo(values: tuple[str, ...]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        for value in values:
            combo.addItem(value or "Muse mặc định", value)
        return combo

    # =========================================================================
    # EVENT HANDLERS & LOGIC
    # =========================================================================

    def _current_task_mode(self) -> str:
        return getattr(self.settings, "muse_task_mode", "video") or "video"

    def _set_task_mode(self, mode: str) -> None:
        if self.busy:
            QMessageBox.information(self, "Muse đang chạy", "Không thể đổi chế độ khi tác vụ đang chạy.")
            return
        if mode not in {"video", "image"}:
            mode = "video"
        self.settings.muse_task_mode = mode
        self.settings_changed.emit()
        self._update_task_mode_ui()

    def _update_task_mode_ui(self) -> None:
        is_image = (self._current_task_mode() == "image")
        if is_image:
            self.btn_mode_image.setStyleSheet(ACTIVE_MODE_STYLE)
            self.btn_mode_video.setStyleSheet(INACTIVE_MODE_STYLE)
            self.source_group.setTitle("📁 Nguồn Ảnh Mẫu (Tùy chọn) & Thư Mục Xuất Ảnh")
            self.source_label.setText("Thư mục ảnh mẫu:")
            self.image_folder.setPlaceholderText("(Tùy chọn) Chọn ảnh mẫu để tạo Image-to-Image, hoặc để trống...")
            self.prompt_card.setTitle("🎨 Prompt Tạo Ảnh AI (Image Prompt)")
            self.prompt_hint.setText("Mô tả nội dung, phong cách, chi tiết ảnh:")
            self.duration_box_widget.setVisible(False)
            self.image_quantity_widget.setVisible(True)
            self.start_all.setText("▶ Bắt đầu tạo ảnh")
        else:
            self.btn_mode_video.setStyleSheet(ACTIVE_MODE_STYLE)
            self.btn_mode_image.setStyleSheet(INACTIVE_MODE_STYLE)
            self.source_group.setTitle("📁 Nguồn Ảnh & Thư Mục Xuất File")
            self.source_label.setText("Thư mục ảnh:")
            self.image_folder.setPlaceholderText("Chọn thư mục ảnh (JPG, PNG, WEBP)...")
            self.prompt_card.setTitle("✨ Prompt Tạo Video AI (Motion Prompt)")
            self.prompt_hint.setText("Mô tả chuyển động, góc máy, ánh sáng (áp dụng chung cho batch ảnh):")
            self.duration_box_widget.setVisible(True)
            self.image_quantity_widget.setVisible(False)
            self.start_all.setText("▶ Bắt đầu tạo video")

    def _on_headless_toggled(self, checked: bool) -> None:
        self.settings.muse_headless = checked
        self.settings_changed.emit()

    def _on_concurrency_changed(self, value: int) -> None:
        self.settings.muse_concurrency = value
        self.settings_changed.emit()

    def _save_url(self) -> None:
        try:
            val = validate_muse_start_url(self.url.text().strip() or MUSE_START_URL)
            self.settings.muse_start_url = val
            self.settings_changed.emit()
        except ValueError as exc:
            QMessageBox.warning(self, "URL không hợp lệ", str(exc))

    def _choose_image_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục ảnh", self.image_folder.text())
        if folder:
            self.image_folder.setText(folder)
            self._scan_images()

    def _choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu kết quả", self.output_folder.text())
        if folder:
            self.output_folder.setText(folder)
            self.settings.last_output_folder = folder
            self.settings_changed.emit()

    def _scan_images(self) -> list[Path]:
        folder_text = self.image_folder.text().strip()
        if not folder_text:
            return []
        try:
            images = self.batch_manager.scan_images(folder_text, recursive=self.recursive.isChecked())
            self.image_count.setText(f"Đã tìm thấy {len(images)} ảnh hợp lệ.")
            return images
        except Exception:
            self.image_count.setText("Chưa tìm thấy ảnh hợp lệ")
            return []

    # =========================================================================
    # QUẢN LÝ COOKIE ACCOUNTS & TABLE
    # =========================================================================

    def _reload_accounts_table(self) -> None:
        self._refreshing = True
        try:
            accounts = self.cookie_store.all()

            # Reload ComboBox
            current_id = self.viewing_account_combo.currentData()
            self.viewing_account_combo.clear()
            for acc in accounts:
                self.viewing_account_combo.addItem(acc.combo_label, acc.account_id)
            if current_id:
                idx = self.viewing_account_combo.findData(current_id)
                if idx >= 0:
                    self.viewing_account_combo.setCurrentIndex(idx)
            self._update_viewing_status()

            # Reload Table
            self.accounts_table.setRowCount(len(accounts))
            for row, acc in enumerate(accounts):
                # Col 0: Checkbox
                chk_item = QTableWidgetItem()
                chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk_item.setCheckState(Qt.Checked if acc.enabled else Qt.Unchecked)
                chk_item.setData(Qt.UserRole, acc.account_id)
                self.accounts_table.setItem(row, 0, chk_item)

                # Col 1: Tên
                name_item = QTableWidgetItem(acc.name)
                name_item.setData(Qt.UserRole, acc.account_id)
                self.accounts_table.setItem(row, 1, name_item)

                # Col 2: Email
                email_item = QTableWidgetItem(acc.email or "—")
                self.accounts_table.setItem(row, 2, email_item)

                # Col 3: Trạng thái
                status_item = QTableWidgetItem(acc.status_display)
                self.accounts_table.setItem(row, 3, status_item)

                # Col 4: Lần kiểm tra cuối
                time_item = QTableWidgetItem(acc.last_checked or "—")
                self.accounts_table.setItem(row, 4, time_item)

                # Col 5: Số cookies
                cookie_cnt_item = QTableWidgetItem(f"{len(acc.cookies)} cookies")
                self.accounts_table.setItem(row, 5, cookie_cnt_item)

                # Col 6: Đã hoàn thành
                tasks_item = QTableWidgetItem(f"{acc.tasks_completed}")
                self.accounts_table.setItem(row, 6, tasks_item)

            self.accounts_table.itemChanged.connect(self._on_table_item_changed)
        finally:
            self._refreshing = False

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._refreshing or item.column() != 0:
            return
        account_id = item.data(Qt.UserRole)
        if account_id:
            enabled = (item.checkState() == Qt.Checked)
            self.cookie_store.set_enabled(account_id, enabled)

    def _on_table_row_clicked(self, item: QTableWidgetItem) -> None:
        row = item.row()
        id_item = self.accounts_table.item(row, 0)
        if id_item:
            acc_id = id_item.data(Qt.UserRole)
            idx = self.viewing_account_combo.findData(acc_id)
            if idx >= 0:
                self.viewing_account_combo.setCurrentIndex(idx)

    def _on_viewing_account_changed(self) -> None:
        self._update_viewing_status()

    def _update_viewing_status(self) -> None:
        acc_id = self.viewing_account_combo.currentData()
        if not acc_id:
            self.viewing_status_label.setText("⚪ Chưa chọn tài khoản")
            return
        account = self.cookie_store.get(acc_id)
        if account:
            self.viewing_status_label.setText(account.status_display)
        else:
            self.viewing_status_label.setText("⚪ Không tìm thấy")

    def _set_all_enabled(self, enabled: bool) -> None:
        for acc in self.cookie_store.all():
            self.cookie_store.set_enabled(acc.account_id, enabled)
        self._reload_accounts_table()

    # =========================================================================
    # THAO TÁC NÚT BẤM TOOLBAR
    # =========================================================================

    def _import_cookie_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Chọn Thư mục chứa các file Cookie (.json, .txt)")
        if not folder:
            return
        try:
            added = self.cookie_store.import_from_folder(folder)
            if added:
                QMessageBox.information(
                    self,
                    "Quét Thư Mục Cookie",
                    f"Đã thêm thành công {len(added)} tài khoản từ thư mục!\n"
                    f"Tất cả đã xuất hiện trong danh sách bên dưới.",
                )
                self._reload_accounts_table()
            else:
                QMessageBox.warning(self, "Quét Cookie", "Không tìm thấy file cookie .json hoặc .txt hợp lệ trong thư mục.")
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi Quét Cookie", str(exc))

    def _open_paste_cookie_dialog(self) -> None:
        default_name = self.cookie_store.next_name()
        dialog = PasteCookieDialog(default_name, parent=self)
        if dialog.exec() == QDialog.Accepted:
            raw, name, email = dialog.get_data()
            if not raw:
                QMessageBox.warning(self, "Thiếu Dữ Liệu", "Vui lòng dán chuỗi cookie vào khung.")
                return
            try:
                acc = self.cookie_store.import_from_json(raw, default_name=name, default_email=email)
                QMessageBox.information(
                    self,
                    "Thêm Cookie Thành Công",
                    f"Đã thêm tài khoản: {acc.combo_label}\n"
                    f"Tổng số cookies nhận diện: {len(acc.cookies)}.",
                )
                self._reload_accounts_table()
                # Chọn ngay tài khoản vừa thêm
                idx = self.viewing_account_combo.findData(acc.account_id)
                if idx >= 0:
                    self.viewing_account_combo.setCurrentIndex(idx)
            except Exception as exc:
                QMessageBox.critical(self, "Lỗi Định Dạng Cookie", f"Không thể nhận diện cookie: {exc}")

    def _open_edit_email_dialog(self) -> None:
        acc_id = self.viewing_account_combo.currentData()
        if not acc_id:
            QMessageBox.information(self, "Chưa chọn tài khoản", "Vui lòng chọn tài khoản cần sửa email.")
            return
        account = self.cookie_store.get(acc_id)
        if not account:
            return
        dialog = EditEmailDialog(account.name, account.email, parent=self)
        if dialog.exec() == QDialog.Accepted:
            new_email = dialog.get_email()
            self.cookie_store.update_email(acc_id, new_email)
            self._reload_accounts_table()

    def _open_compare_cookie_dialog(self) -> None:
        acc_id = self.viewing_account_combo.currentData()
        if not acc_id:
            QMessageBox.information(self, "Chưa chọn tài khoản", "Vui lòng chọn tài khoản cần đối chiếu.")
            return
        account = self.cookie_store.get(acc_id)
        if not account:
            return
        dialog = InspectCookieDialog(account, parent=self)
        dialog.exec()

    def _delete_current_account(self) -> None:
        acc_id = self.viewing_account_combo.currentData()
        if not acc_id:
            return
        account = self.cookie_store.get(acc_id)
        if not account:
            return
        confirm = QMessageBox.question(
            self,
            "Xác nhận xoá",
            f"Bạn có chắc chắn muốn xoá tài khoản '{account.combo_label}' khỏi danh sách?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.cookie_store.delete(acc_id)
            self._reload_accounts_table()

    def _delete_all_accounts(self) -> None:
        accounts = self.cookie_store.all()
        if not accounts:
            return
        confirm = QMessageBox.question(
            self,
            "Xoá tất cả tài khoản",
            f"Bạn có chắc chắn muốn xoá toàn bộ {len(accounts)} tài khoản Muse trong danh sách?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.cookie_store.clear_all()
            self._reload_accounts_table()

    def _check_current_account(self) -> None:
        acc_id = self.viewing_account_combo.currentData()
        if not acc_id:
            return
        account = self.cookie_store.get(acc_id)
        if not account:
            return
        self.btn_check_account.setEnabled(False)
        self.btn_check_all.setEnabled(False)
        self.viewing_status_label.setText("🔄 Đang kiểm tra…")
        self.global_status.setText(f"Đang kiểm tra cookie tài khoản {account.name}…")

        self._active_checker = MuseCookieCheckerThread([account], headless=self.headless_check.isChecked(), parent=self)
        self._active_checker.account_checked.connect(self._on_account_checked)
        self._active_checker.all_finished.connect(lambda _a, _t: self._on_checking_finished())
        self._active_checker.start()

    def _check_all_accounts(self) -> None:
        accounts = self.cookie_store.all()
        if not accounts:
            QMessageBox.information(self, "Chưa có tài khoản", "Hãy thêm cookie tài khoản trước khi kiểm tra.")
            return
        self.btn_check_account.setEnabled(False)
        self.btn_check_all.setEnabled(False)
        self.global_status.setText(f"Đang kiểm tra lần lượt {len(accounts)} tài khoản Muse AI…")

        self._active_checker = MuseCookieCheckerThread(accounts, headless=self.headless_check.isChecked(), parent=self)
        self._active_checker.account_checked.connect(self._on_account_checked)
        self._active_checker.all_finished.connect(self._on_all_accounts_checked_summary)
        self._active_checker.start()

    def _on_account_checked(self, account_id: str, is_active: bool, message: str, time_str: str) -> None:
        status_key = "active" if is_active else "expired"
        self.cookie_store.update_status(account_id, status_key, message, last_checked=time_str)
        self._reload_accounts_table()

    def _on_checking_finished(self) -> None:
        self.btn_check_account.setEnabled(True)
        self.btn_check_all.setEnabled(True)
        self.global_status.setText("Kiểm tra tài khoản hoàn tất.")

    def _on_all_accounts_checked_summary(self, active_count: int, total_count: int) -> None:
        self.btn_check_account.setEnabled(True)
        self.btn_check_all.setEnabled(True)
        self.global_status.setText(f"Đã kiểm tra xong tất cả: {active_count}/{total_count} tài khoản đang hoạt động.")
        QMessageBox.information(
            self,
            "Kết Quả Kiểm Tra Cookie",
            f"Hoàn tất kiểm tra {total_count} tài khoản:\n"
            f"🟢 Đang hoạt động: {active_count}\n"
            f"🔴 Hết hạn hoặc lỗi: {total_count - active_count}",
        )

    # =========================================================================
    # BATCH ALLOCATION & EXECUTION (CONCURRENCY 1-5)
    # =========================================================================

    def _get_enabled_accounts(self) -> list[MuseCookieAccount]:
        return [acc for acc in self.cookie_store.all() if acc.enabled]

    def _allocate_jobs(self) -> None:
        active_accounts = self._get_enabled_accounts()
        if not active_accounts:
            QMessageBox.warning(self, "Chưa chọn tài khoản", "Hãy tick ít nhất một tài khoản Muse trong bảng để chạy.")
            return

        is_image = (self._current_task_mode() == "image")
        folder_text = self.image_folder.text().strip()

        if folder_text:
            images = self._scan_images()
            if not images and not is_image:
                QMessageBox.warning(self, "Không có ảnh", "Không tìm thấy ảnh hợp lệ trong thư mục đã chọn.")
                return
            if images:
                self.global_status.setText(
                    f"Đã phân bổ {len(images)} ảnh qua {len(active_accounts)} tài khoản được tick "
                    f"(Trung bình ~{len(images)//len(active_accounts)} ảnh/tài khoản)."
                )
            else:
                qty = self.image_quantity.value()
                self.global_status.setText(
                    f"Đã phân bổ {qty} lượt tạo ảnh (Prompt thuần) qua {len(active_accounts)} tài khoản được tick."
                )
        elif is_image:
            qty = self.image_quantity.value()
            self.global_status.setText(
                f"Đã phân bổ {qty} lượt tạo ảnh (Prompt thuần) qua {len(active_accounts)} tài khoản được tick."
            )
        else:
            QMessageBox.warning(self, "Thiếu Thư Mục Ảnh", "Vui lòng chọn thư mục ảnh để phân bổ.")

    def _start_batch(self) -> None:
        active_accounts = self._get_enabled_accounts()
        if not active_accounts:
            QMessageBox.warning(self, "Chưa chọn tài khoản", "Hãy tick ít nhất một tài khoản Muse trong bảng để chạy.")
            return

        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "Thiếu Prompt", "Prompt không được để trống.")
            return

        output_dir = Path(self.output_folder.text().strip())
        if not str(output_dir).strip():
            QMessageBox.warning(self, "Thiếu Thư Mục Output", "Hãy chọn thư mục lưu kết quả đầu ra.")
            return

        is_image = (self._current_task_mode() == "image")
        folder_text = self.image_folder.text().strip()
        source_items: list[str] = []

        if folder_text:
            imgs = self._scan_images()
            source_items = [str(p) for p in imgs]
        elif is_image:
            qty = self.image_quantity.value()
            source_items = [f"Prompt #{idx + 1}" for idx in range(qty)]
        else:
            QMessageBox.warning(self, "Thiếu Thư Mục Ảnh", "Vui lòng chọn thư mục ảnh để bắt đầu tạo video.")
            return

        if not source_items:
            QMessageBox.warning(self, "Không có mục cần chạy", "Không tìm thấy mục ảnh/prompt nào để xử lý.")
            return

        # Settings
        settings = MuseVideoSettings(
            task_mode="image" if is_image else "video",
            model="",
            aspect_ratio=self.aspect_ratio.currentText().strip() if self.aspect_ratio.currentText() != "Muse mặc định" else "",
            duration=self.duration.currentText().strip() if self.duration.currentText() != "Muse mặc định" else "",
            resolution=self.resolution.currentText().strip() if self.resolution.currentText() != "Muse mặc định" else "",
            quantity=self.image_quantity.value() if is_image else 1,
        )

        concurrency = self.concurrency_spin.value()
        headless = self.headless_check.isChecked()

        # Update UI state
        self.start_all.setEnabled(False)
        self.stop_all.setEnabled(True)
        self.overall_progress.setValue(0)
        self.logs_edit.clear()

        # Start Runner Thread
        self._active_runner = MuseCookieBatchRunnerThread(
            accounts=active_accounts,
            concurrency=concurrency,
            headless=headless,
            task_mode="image" if is_image else "video",
            prompt=prompt,
            settings=settings,
            output_dir=output_dir,
            source_items=source_items,
            parent=self,
        )
        self._active_runner.progress_signal.connect(self._on_batch_progress)
        self._active_runner.stats_signal.connect(self._on_batch_stats)
        self._active_runner.log_signal.connect(self._on_batch_log)
        self._active_runner.worker_status_signal.connect(self._on_worker_status)
        self._active_runner.account_completed_signal.connect(self._on_account_task_completed)
        self._active_runner.batch_finished_signal.connect(self._on_batch_finished)
        self._active_runner.start()

    def _stop_batch(self) -> None:
        if self._active_runner and self._active_runner.isRunning():
            self._active_runner.stop()
            self.global_status.setText("Đang yêu cầu dừng tất cả các luồng…")
            self.stop_all.setEnabled(False)

    def _on_batch_progress(self, percent: int, text: str) -> None:
        self.overall_progress.setValue(percent)
        self.global_status.setText(text)

    def _on_batch_stats(self, pending: int, completed: int, failed: int, quota: int) -> None:
        self.stats_label.setText(
            f"Chờ: {pending} • Thành công: {completed} • Lỗi: {failed} • Quota: {quota}"
        )

    def _on_batch_log(self, message: str) -> None:
        now_ts = time.strftime("%H:%M:%S")
        self.logs_edit.appendPlainText(f"[{now_ts}] {message}")
        scroll = self.logs_edit.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    def _on_worker_status(self, worker_idx: int, status_text: str) -> None:
        if worker_idx in self.worker_status_labels:
            self.worker_status_labels[worker_idx].setText(f"Luồng {worker_idx}: {status_text}")

    def _on_account_task_completed(self, account_id: str, count: int) -> None:
        acc = self.cookie_store.get(account_id)
        if acc:
            acc.tasks_completed += count
            self.cookie_store.save(acc)
            self._reload_accounts_table()

    def _on_batch_finished(self, success: bool, summary: str) -> None:
        self.start_all.setEnabled(True)
        self.stop_all.setEnabled(False)
        for w_idx in self.worker_status_labels:
            self.worker_status_labels[w_idx].setText(f"Luồng {w_idx}: Sẵn sàng")
        if success:
            QMessageBox.information(self, "Hoàn tất Batch", summary)
        else:
            QMessageBox.warning(self, "Batch Kết Thúc", summary)

    def _clear_ui(self) -> None:
        self.image_folder.clear()
        self.prompt.clear()
        self.image_count.setText("Chưa quét ảnh")
        self.logs_edit.clear()
        self.overall_progress.setValue(0)
        self.stats_label.setText("Chờ: 0 • Thành công: 0 • Lỗi: 0 • Quota: 0")
        self.global_status.setText("Đã xóa dữ liệu UI.")
