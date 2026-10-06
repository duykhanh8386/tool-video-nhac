"""Muse Batch — UI page for batch-processing images to videos via Muse AI.

Supports up to 3 accounts running in parallel, each with its own Chrome
profile/driver to prevent conflicts.  The page handles:

* Distributing images across accounts
* Sending prompts with images to the Muse chat
* Waiting for the generated video and downloading it
* Checkpoint-based resume (never re-sends a prompt that was already sent)
* Migrating valid MP4s when the output folder changes
* Per-tab Muse connection via existing Chrome with remote-debugging
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ai.muse_web import (
    MUSE_URL,
    MuseAutomationError,
    MuseCancelled,
    clear_checkpoint,
    debug_port_for,
    is_profile_ready,
    is_valid_mp4,
    load_checkpoint,
    migrate_outputs,
    open_muse_login,
    process_muse_job,
    profile_dir_for,
)
from models.settings_model import AppSettings
from ui.common import FileField, show_error
from utils.media import IMAGE_EXTENSIONS
from utils.paths import unique_output

MAX_ACCOUNTS = 3
VIDEOS_PER_IMAGE = 1  # default

# ---------------------------------------------------------------------------
# Worker signals bridge  (thread → Qt)
# ---------------------------------------------------------------------------

class _WorkerSignals(QWidget):
    """Hidden widget that owns the cross-thread signals."""
    progress = Signal(int, int, str)      # account_index, percent, message
    job_done = Signal(int, str, str)      # account_index, image_path, output_path
    job_failed = Signal(int, str, str)    # account_index, image_path, error
    all_done = Signal(int)                # account_index


# ---------------------------------------------------------------------------
# Account state
# ---------------------------------------------------------------------------

@dataclass
class AccountState:
    index: int
    google_password: str = ""
    tab_label: str = ""
    login_email: str = ""
    ready: bool = False
    running: bool = False
    cancel_event: threading.Event | None = None
    thread: threading.Thread | None = None
    assigned_images: list[str] | None = None
    completed: list[str] | None = None
    failures: list[str] | None = None
    chrome_process: Any = None


# ---------------------------------------------------------------------------
# Main page widget
# ---------------------------------------------------------------------------

class MuseBatchPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._signals = _WorkerSignals(self)
        self._signals.progress.connect(self._on_worker_progress)
        self._signals.job_done.connect(self._on_job_done)
        self._signals.job_failed.connect(self._on_job_failed)
        self._signals.all_done.connect(self._on_all_done)

        self.accounts: list[AccountState] = [
            AccountState(index=i) for i in range(MAX_ACCOUNTS)
        ]
        self._all_images: list[str] = []
        self._image_dir = ""
        self._output_dir = ""
        self._prev_output_dir = ""
        self._prompt = ""
        self._muse_url = MUSE_URL
        self._videos_per_image = VIDEOS_PER_IMAGE
        self._connection_mode = "existing"  # "existing" = attach to running Chrome
        self._global_running = False

        # Video settings (optional overrides)
        self._model = ""
        self._aspect = ""
        self._duration = ""
        self._resolution = ""

        self._build_ui()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._periodic_refresh)
        self._refresh_timer.start(3000)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        heading = QLabel("Muse AI — Batch ảnh thành video trên 3 tài khoản")
        heading.setObjectName("pageTitle")
        heading.setWordWrap(True)
        root.addWidget(heading)

        note = QLabel(
            "Mỗi tài khoản dùng một Chrome profile/driver riêng. Nếu bạn nhập mật khẩu, "
            "tool chỉ điền trên accounts.google.com, không lưu mật khẩu; CAPTCHA/2FA làm thủ công. "
            "Mỗi worker gửi tới đa 3 ảnh với một prompt rồi tải lần lượt đủ 3 video trước lượt tiếp theo; "
            "tool không lưu bí mật và không vượt CAPTCHA, mà xác minh, 2FA, quota hoặc rate limit."
        )
        note.setWordWrap(True)
        note.setObjectName("muted")
        root.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        form = QVBoxLayout(panel)
        scroll.setWidget(panel)
        root.addWidget(scroll, 1)

        # Connection mode
        conn_group = QGroupBox("Cách kết nối Muse")
        conn_layout = QVBoxLayout(conn_group)
        self.conn_mode_combo = QComboBox()
        self.conn_mode_combo.addItem("Phương án 2 — Chọn tab Muse đang mở", "existing")
        conn_layout.addWidget(self.conn_mode_combo)
        conn_actions = QHBoxLayout()
        self.btn_open_chrome = QPushButton("Mở Chrome/Muse đã tick")
        self.btn_open_chrome.clicked.connect(self._open_all_chrome)
        self.btn_scan_tabs = QPushButton("Quét tab Muse đang mở")
        self.btn_scan_tabs.clicked.connect(self._scan_tabs)
        conn_actions.addWidget(self.btn_open_chrome)
        conn_actions.addWidget(self.btn_scan_tabs)
        conn_layout.addLayout(conn_actions)
        form.addWidget(conn_group)

        # Source & prompt
        source_group = QGroupBox("Nguồn ảnh và prompt chung")
        source_layout = QFormLayout(source_group)
        self.muse_url_edit = QLineEdit(MUSE_URL)
        source_layout.addRow("MUSE_URL", self.muse_url_edit)
        self.image_dir_field = FileField("Thư mục ảnh", directory=True)
        self.image_dir_field.changed.connect(self._image_dir_changed)
        btn_row = QHBoxLayout()
        self.btn_choose_dir = QPushButton("Chọn thư mục ảnh")
        self.btn_choose_dir.clicked.connect(self.image_dir_field.browse)
        self.btn_scan_dir = QPushButton("Quét thư mục con")
        self.btn_scan_dir.clicked.connect(lambda: self._scan_image_dir(recursive=True))
        btn_row.addWidget(self.image_dir_field, 1)
        btn_row.addWidget(self.btn_scan_dir)
        source_layout.addRow(btn_row)
        self.image_count_label = QLabel("Chưa quét ảnh")
        self.image_count_label.setObjectName("muted")
        source_layout.addRow(self.image_count_label)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setMaximumHeight(80)
        self.prompt_edit.setPlaceholderText("tạo video chuyển động nhân vật nhẹ nhàng camera giữ nguyên")
        source_layout.addRow("Prompt tạo video chung", self.prompt_edit)

        self.output_dir_field = FileField("Thư mục output", directory=True)
        self.output_dir_field.changed.connect(self._output_dir_changed)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_dir_field, 1)
        self.btn_choose_output = QPushButton("Chọn output")
        self.btn_choose_output.clicked.connect(self.output_dir_field.browse)
        out_row.addWidget(self.btn_choose_output)
        source_layout.addRow(out_row)
        form.addWidget(source_group)

        # Video settings (optional)
        settings_group = QGroupBox("Cài đặt video chung (để trống = Muse mặc định)")
        settings_layout = QGridLayout(settings_group)
        self.model_combo = QComboBox()
        self.model_combo.addItem("Muse mặc định", "")
        self.aspect_combo = QComboBox()
        self.aspect_combo.addItem("Muse mặc định", "")
        self.duration_combo = QComboBox()
        self.duration_combo.addItem("Muse mặc định", "")
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItem("Muse mặc định", "")
        self.vpi_spin = QSpinBox()
        self.vpi_spin.setRange(1, 5)
        self.vpi_spin.setValue(1)
        settings_layout.addWidget(QLabel("Model"), 0, 0)
        settings_layout.addWidget(self.model_combo, 0, 1)
        settings_layout.addWidget(QLabel("Tỷ lệ"), 0, 2)
        settings_layout.addWidget(self.aspect_combo, 0, 3)
        settings_layout.addWidget(QLabel("Độ dài"), 0, 4)
        settings_layout.addWidget(self.duration_combo, 0, 5)
        settings_layout.addWidget(QLabel("Độ phân giải"), 0, 6)
        settings_layout.addWidget(self.resolution_combo, 0, 7)
        settings_layout.addWidget(QLabel("Video/ảnh"), 0, 8)
        settings_layout.addWidget(self.vpi_spin, 0, 9)
        form.addWidget(settings_group)

        # Action buttons
        actions_group = QHBoxLayout()
        self.btn_distribute = QPushButton("Phân bổ ảnh")
        self.btn_distribute.clicked.connect(self._distribute_images)
        self.btn_start = QPushButton("Xử lý")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start_all)
        self.btn_stop_all = QPushButton("Dừng tất cả")
        self.btn_stop_all.clicked.connect(self._stop_all)
        self.btn_continue = QPushButton("Tiếp tục")
        self.btn_continue.clicked.connect(self._continue_all)
        self.btn_redistribute = QPushButton("Phân bổ lại ảnh chưa gửi")
        self.btn_redistribute.clicked.connect(self._redistribute_unsent)
        self.btn_clear_ui = QPushButton("Xóa dữ liệu UI")
        self.btn_clear_ui.clicked.connect(self._clear_ui)
        for btn in (self.btn_distribute, self.btn_start, self.btn_stop_all,
                     self.btn_continue, self.btn_redistribute, self.btn_clear_ui):
            actions_group.addWidget(btn)
        form.addLayout(actions_group)

        # Status
        self.global_status = QLabel("")
        self.global_status.setWordWrap(True)
        self.global_status.setObjectName("notice")
        form.addWidget(self.global_status)
        self.ready_label = QLabel("")
        self.ready_label.setWordWrap(True)
        self.ready_label.setObjectName("notice")
        form.addWidget(self.ready_label)

        # Per-account tabs
        self.tabs = QTabWidget()
        self._account_widgets: list[dict[str, Any]] = []
        for i in range(MAX_ACCOUNTS):
            tab = QWidget()
            tab_layout = QFormLayout(tab)

            pw_edit = QLineEdit()
            pw_edit.setPlaceholderText("Mật khẩu Google (không lưu; có thể để trống để tự đăng nhập)")
            pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
            tab_layout.addRow("Mật khẩu Google", pw_edit)

            tab_combo = QComboBox()
            tab_combo.addItem(f"Chat — Muse — READY • {MUSE_URL}", MUSE_URL)
            tab_layout.addRow("Tab Muse", tab_combo)

            status_label = QLabel("Chưa kết nối")
            status_label.setWordWrap(True)
            status_label.setObjectName("notice")
            tab_layout.addRow("Đăng nhập", status_label)

            progress_bar = QProgressBar()
            progress_bar.setRange(0, 100)
            tab_layout.addRow(progress_bar)

            progress_label = QLabel("")
            progress_label.setWordWrap(True)
            progress_label.setObjectName("muted")
            tab_layout.addRow(progress_label)

            log_text = QPlainTextEdit()
            log_text.setReadOnly(True)
            log_text.setMaximumHeight(160)
            tab_layout.addRow(log_text)

            self._account_widgets.append({
                "password": pw_edit,
                "tab_combo": tab_combo,
                "status": status_label,
                "progress_bar": progress_bar,
                "progress_label": progress_label,
                "log": log_text,
            })
            self.tabs.addTab(tab, f"Tài khoản {i + 1} • Chưa mở")
        form.addWidget(self.tabs)
        form.addStretch()

    # -----------------------------------------------------------------------
    # Image scanning
    # -----------------------------------------------------------------------

    def _scan_image_dir(self, recursive: bool = False) -> None:
        folder = self.image_dir_field.text()
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, "Lỗi", "Hãy chọn thư mục ảnh trước.")
            return
        root = Path(folder)
        iterator = root.rglob("*") if recursive else root.iterdir()
        images = sorted(
            str(p.resolve()) for p in iterator
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
        )
        self._all_images = images
        self._image_dir = folder
        self.image_count_label.setText(f"Tìm thấy {len(images)} ảnh")
        self._update_global_status()

    def _image_dir_changed(self, path: str) -> None:
        if path and Path(path).is_dir():
            self._scan_image_dir()

    def _output_dir_changed(self, path: str) -> None:
        old = self._output_dir
        self._output_dir = path
        if old and path and old != path:
            self._prev_output_dir = old
            # Migrate valid MP4s
            count = migrate_outputs(old, path)
            if count:
                QMessageBox.information(
                    self, "Đã di chuyển",
                    f"Đã chép {count} file MP4 hợp lệ từ thư mục cũ sang thư mục mới."
                )

    # -----------------------------------------------------------------------
    # Chrome / tab management
    # -----------------------------------------------------------------------

    def _open_all_chrome(self) -> None:
        """Launch isolated Chrome instances for each account."""
        for i, acct in enumerate(self.accounts):
            try:
                proc = open_muse_login(i)
                acct.chrome_process = proc
                self._log(i, f"Đã mở Chrome profile cho tài khoản {i + 1} (cổng {debug_port_for(i)})")
                self.tabs.setTabText(i, f"Tài khoản {i + 1} • Đang mở")
            except Exception as exc:
                self._log(i, f"Lỗi mở Chrome: {exc}")

    def _scan_tabs(self) -> None:
        """Check which accounts have a reachable Chrome with Muse tab."""
        for i, acct in enumerate(self.accounts):
            ready = is_profile_ready(i)
            acct.ready = ready
            widgets = self._account_widgets[i]
            if ready:
                widgets["status"].setText(
                    f"READY — {self._get_login_email(i)} • Worker: Sẵn sàng"
                )
                widgets["status"].setStyleSheet("color: #4ade80;")
                self.tabs.setTabText(i, f"Tài khoản {i + 1} • READY")
                widgets["tab_combo"].setItemText(
                    0, f"Chat — Muse — READY • {MUSE_URL}"
                )
            else:
                widgets["status"].setText("Chưa kết nối hoặc chưa đăng nhập")
                widgets["status"].setStyleSheet("color: #f87171;")
                self.tabs.setTabText(i, f"Tài khoản {i + 1} • Chưa mở")
        self._update_global_status()

    def _get_login_email(self, account_index: int) -> str:
        """Attempt to read the logged-in email from Chrome profile data."""
        profile = profile_dir_for(account_index)
        prefs_path = profile / "Default" / "Preferences"
        if prefs_path.is_file():
            try:
                import json
                with prefs_path.open("r", encoding="utf-8") as f:
                    prefs = json.load(f)
                email = prefs.get("account_info", [{}])[0].get("email", "")
                if email:
                    return email
            except Exception:
                pass
        return f"tài khoản {account_index + 1}"

    # -----------------------------------------------------------------------
    # Image distribution
    # -----------------------------------------------------------------------

    def _distribute_images(self) -> None:
        if not self._all_images:
            self._scan_image_dir()
        if not self._all_images:
            QMessageBox.warning(self, "Lỗi", "Không có ảnh nào để phân bổ.")
            return

        ready_accounts = [a for a in self.accounts if a.ready]
        if not ready_accounts:
            QMessageBox.warning(
                self, "Lỗi",
                "Chưa có tài khoản nào READY. Hãy mở Chrome và quét tab trước."
            )
            return

        # Filter out images that already have valid output MP4
        output_dir = self.output_dir_field.text()
        remaining = []
        for img in self._all_images:
            stem = Path(img).stem
            out_path = Path(output_dir) / f"{stem}.mp4" if output_dir else None
            if out_path and is_valid_mp4(out_path):
                continue  # already done
            remaining.append(img)

        if not remaining:
            QMessageBox.information(self, "Hoàn tất", "Tất cả ảnh đã có video MP4 hợp lệ.")
            return

        # Round-robin distribute
        n = len(ready_accounts)
        for acct in ready_accounts:
            acct.assigned_images = []
            acct.completed = []
            acct.failures = []

        for idx, img in enumerate(remaining):
            ready_accounts[idx % n].assigned_images.append(img)

        summary_parts = []
        for acct in ready_accounts:
            count = len(acct.assigned_images) if acct.assigned_images else 0
            summary_parts.append(f"TK{acct.index + 1}: {count}")
            self._log(acct.index, f"Được phân bổ {count} ảnh")

        total = len(remaining)
        self.global_status.setText(
            f"Sẵn sàng chạy với {total} ảnh. Phân bổ: {', '.join(summary_parts)}"
        )
        self.ready_label.setText(
            f"Tài khoản {', '.join(str(a.index+1) for a in ready_accounts)} đã READY; "
            f"các worker vừa được phát lệnh đồng thời."
        )

    # -----------------------------------------------------------------------
    # Start / stop
    # -----------------------------------------------------------------------

    def _start_all(self) -> None:
        """Launch worker threads for all ready accounts with assigned images."""
        prompt = self.prompt_edit.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "Lỗi", "Hãy nhập prompt trước.")
            return
        output_dir = self.output_dir_field.text()
        if not output_dir:
            QMessageBox.warning(self, "Lỗi", "Hãy chọn thư mục output.")
            return
        self._prompt = prompt
        self._output_dir = output_dir
        self._muse_url = self.muse_url_edit.text().strip() or MUSE_URL
        self._videos_per_image = self.vpi_spin.value()

        # Distribute if not done yet
        has_work = any(a.assigned_images for a in self.accounts)
        if not has_work:
            self._distribute_images()

        self._global_running = True
        for acct in self.accounts:
            if acct.assigned_images and not acct.running:
                self._launch_worker(acct)
        self._update_global_status()

    def _stop_all(self) -> None:
        for acct in self.accounts:
            if acct.cancel_event:
                acct.cancel_event.set()
        self._global_running = False
        self.global_status.setText("Đã yêu cầu dừng tất cả worker.")

    def _continue_all(self) -> None:
        """Resume workers that still have images left."""
        self._prompt = self.prompt_edit.toPlainText().strip()
        self._output_dir = self.output_dir_field.text()
        self._muse_url = self.muse_url_edit.text().strip() or MUSE_URL
        self._global_running = True
        for acct in self.accounts:
            if acct.assigned_images and not acct.running:
                self._launch_worker(acct)
        self._update_global_status()

    def _redistribute_unsent(self) -> None:
        """Collect images that haven't been completed and re-distribute."""
        unsent = []
        for acct in self.accounts:
            if acct.assigned_images:
                completed_set = set(acct.completed or [])
                for img in acct.assigned_images:
                    if img not in completed_set:
                        unsent.append(img)
            acct.assigned_images = []
        self._all_images = unsent
        self._distribute_images()

    def _clear_ui(self) -> None:
        for acct in self.accounts:
            acct.assigned_images = []
            acct.completed = []
            acct.failures = []
        for w in self._account_widgets:
            w["log"].clear()
            w["progress_bar"].setValue(0)
            w["progress_label"].setText("")
        self.global_status.setText("")
        self.ready_label.setText("")

    # -----------------------------------------------------------------------
    # Worker thread
    # -----------------------------------------------------------------------

    def _launch_worker(self, acct: AccountState) -> None:
        acct.running = True
        acct.cancel_event = threading.Event()
        acct.completed = acct.completed or []
        acct.failures = acct.failures or []
        t = threading.Thread(
            target=self._worker_loop,
            args=(acct.index,),
            daemon=True,
        )
        acct.thread = t
        t.start()
        self.tabs.setTabText(acct.index, f"Tài khoản {acct.index + 1} • ĐANG CHẠY")

    def _worker_loop(self, account_index: int) -> None:
        acct = self.accounts[account_index]
        images = list(acct.assigned_images or [])
        cancel = acct.cancel_event
        prompt = self._prompt
        output_dir = self._output_dir
        muse_url = self._muse_url

        for img_path in images:
            if cancel.is_set():
                break

            stem = Path(img_path).stem
            out_path = str(Path(output_dir) / f"{stem}.mp4")

            # Skip if already valid
            if is_valid_mp4(out_path):
                self._signals.job_done.emit(account_index, img_path, out_path)
                continue

            # Check checkpoint — if prompt was already sent, only download
            ckpt = load_checkpoint(account_index, img_path)
            if ckpt and ckpt.protocol == 3 and ckpt.prompt == prompt and ckpt.download_done:
                if is_valid_mp4(ckpt.output_path):
                    # Copy to new output if needed
                    if str(Path(ckpt.output_path).resolve()) != str(Path(out_path).resolve()):
                        try:
                            import shutil
                            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(ckpt.output_path, out_path)
                        except Exception:
                            pass
                    self._signals.job_done.emit(account_index, img_path, out_path)
                    continue

            try:
                result = process_muse_job(
                    image_path=img_path,
                    prompt=prompt,
                    output_path=out_path,
                    account_index=account_index,
                    muse_url=muse_url,
                    progress=lambda pct, msg, idx=account_index: (
                        self._signals.progress.emit(idx, pct, msg)
                    ),
                    cancelled=cancel.is_set,
                )
                self._signals.job_done.emit(account_index, img_path, str(result))
            except MuseCancelled:
                break
            except Exception as exc:
                self._signals.job_failed.emit(account_index, img_path, str(exc))

        acct.running = False
        self._signals.all_done.emit(account_index)

    # -----------------------------------------------------------------------
    # Signal handlers (run on Qt thread)
    # -----------------------------------------------------------------------

    def _on_worker_progress(self, account_index: int, pct: int, msg: str) -> None:
        w = self._account_widgets[account_index]
        w["progress_bar"].setValue(pct)
        w["progress_label"].setText(msg)

    def _on_job_done(self, account_index: int, image_path: str, output_path: str) -> None:
        acct = self.accounts[account_index]
        if acct.completed is not None:
            acct.completed.append(image_path)
        self._log(account_index, f"✓ Thành công: {Path(image_path).name} → {Path(output_path).name}")
        self._update_tab_status(account_index)
        self._update_global_status()

    def _on_job_failed(self, account_index: int, image_path: str, error: str) -> None:
        acct = self.accounts[account_index]
        if acct.failures is not None:
            acct.failures.append(image_path)
        self._log(account_index, f"✗ Lỗi: {Path(image_path).name} — {error}")
        self._update_tab_status(account_index)
        self._update_global_status()

    def _on_all_done(self, account_index: int) -> None:
        acct = self.accounts[account_index]
        acct.running = False
        done = len(acct.completed or [])
        failed = len(acct.failures or [])
        total = len(acct.assigned_images or [])
        self._log(account_index, f"Worker xong: {done}/{total} thành công, {failed} lỗi")
        self.tabs.setTabText(account_index, f"Tài khoản {account_index + 1} • XONG")
        w = self._account_widgets[account_index]
        w["progress_bar"].setValue(100 if failed == 0 else 0)
        self._update_global_status()

        # Check if all workers are done
        if not any(a.running for a in self.accounts):
            self._global_running = False
            total_done = sum(len(a.completed or []) for a in self.accounts)
            total_fail = sum(len(a.failures or []) for a in self.accounts)
            self.global_status.setText(
                f"Toàn bộ worker đã xong: {total_done} thành công, {total_fail} lỗi."
            )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _log(self, account_index: int, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        w = self._account_widgets[account_index]
        w["log"].appendPlainText(f"[{ts}] {message}")

    def _update_tab_status(self, account_index: int) -> None:
        acct = self.accounts[account_index]
        done = len(acct.completed or [])
        total = len(acct.assigned_images or [])
        w = self._account_widgets[account_index]
        if total > 0:
            w["progress_bar"].setValue(round(done / total * 100))

    def _update_global_status(self) -> None:
        ready_count = sum(1 for a in self.accounts if a.ready)
        running_count = sum(1 for a in self.accounts if a.running)
        total_assigned = sum(len(a.assigned_images or []) for a in self.accounts)
        total_done = sum(len(a.completed or []) for a in self.accounts)
        total_fail = sum(len(a.failures or []) for a in self.accounts)

        parts = []
        if ready_count:
            ready_ids = ", ".join(str(a.index + 1) for a in self.accounts if a.ready)
            parts.append(f"Tài khoản {ready_ids} đã READY")
        if running_count:
            parts.append(f"{running_count} worker đang chạy")
        if total_assigned:
            parts.append(f"{total_done}/{total_assigned} ảnh xong")
        if total_fail:
            parts.append(f"{total_fail} lỗi")

        self.ready_label.setText("; ".join(parts) if parts else "")

    def _periodic_refresh(self) -> None:
        """Periodically update tab labels with running state."""
        for i, acct in enumerate(self.accounts):
            if acct.running:
                done = len(acct.completed or [])
                total = len(acct.assigned_images or [])
                self.tabs.setTabText(
                    i, f"Tài khoản {i + 1} • {done}/{total}"
                )

    # -----------------------------------------------------------------------
    # Properties for MainWindow integration
    # -----------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._global_running or any(a.running for a in self.accounts)

    def cancel_all(self) -> None:
        self._stop_all()
