from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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

from auth.zsky_auth import (
    ZSKY_START_URL,
    ZskyAccount,
    ZskyAccountStore,
    check_zsky_login_status,
    clean_chrome_profile_locks,
    create_zsky_chrome_driver,
    inject_cookies_to_driver,
    parse_cookie_input,
)
from auth.zsky_generation import ZskyVideoBatchRunnerThread
from models.settings_model import AppSettings


class AddZskyAccountDialog(QDialog):
    """Hộp thoại thêm tài khoản Zsky AI."""

    def __init__(self, default_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("➕ Thêm Tài Khoản Zsky AI")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        hint = QLabel(
            "💡 Sau khi tạo, bạn có thể bấm '🌐 Mở Chrome đăng nhập' để đăng nhập Google. "
            "Session đăng nhập sẽ được lưu vĩnh viễn trong Chrome Profile của tài khoản này."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #475569; font-size: 12px; background: #f1f5f9; padding: 8px; border-radius: 6px;")
        layout.addWidget(hint)

        form = QFormLayout()
        self.name_edit = QLineEdit(default_name)
        self.name_edit.setPlaceholderText("Ví dụ: Zsky 1, Zsky Gmail 2...")
        self.name_edit.setMinimumHeight(32)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("(Tùy chọn) Ghi chú email Google...")
        self.email_edit.setMinimumHeight(32)

        form.addRow("Tên tài khoản:", self.name_edit)
        form.addRow("Email Google:", self.email_edit)
        layout.addLayout(form)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("✔ Tạo tài khoản")
        btn_box.button(QDialogButtonBox.Cancel).setText("Hủy")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def get_data(self) -> tuple[str, str]:
        return self.name_edit.text().strip(), self.email_edit.text().strip()


class PasteCookieDialog(QDialog):
    """Hộp thoại dán trực tiếp chuỗi JSON hoặc text cookie cho tài khoản Zsky AI (tương tự Muse)."""

    def __init__(self, default_name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("➕ Dán Cookie Tài Khoản Zsky AI")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        hint = QLabel(
            "📋 Hướng dẫn: Đăng nhập vào Zsky.ai (qua Google) trên trình duyệt thường, dùng extension "
            "(như Cookie-Editor, J2Team) xuất cookie dạng JSON rồi dán vào khung bên dưới.\n"
            "Tool sẽ tự động nhận diện cookie, email và tạo ngay tài khoản mới sẵn sàng farm!"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #0369a1; font-size: 12px; background: #e0f2fe; padding: 8px; border-radius: 6px;")
        layout.addWidget(hint)

        form = QFormLayout()
        self.name_edit = QLineEdit(default_name)
        self.name_edit.setPlaceholderText("Tên tài khoản (ví dụ: Zsky 1)")
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
            '[\n  {\n    "name": "session",\n    "value": "...",\n    "domain": ".zsky.ai"\n  }\n]'
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


class ZskyCheckAccountThread(QThread):
    """Kiểm tra trạng thái đăng nhập tài khoản Zsky trong nền."""

    result_signal = Signal(str, bool, str)  # account_id, is_logged_in, label

    def __init__(self, account: ZskyAccount, parent=None) -> None:
        super().__init__(parent)
        self.account = account

    def run(self) -> None:
        driver = None
        try:
            profile_dir = Path(self.account.profile_dir).resolve()
            clean_chrome_profile_locks(profile_dir)
            driver = create_zsky_chrome_driver(profile_dir, headless=True)
            driver.set_page_load_timeout(30)
            if self.account.cookies:
                inject_cookies_to_driver(driver, self.account.cookies, target_url=ZSKY_START_URL)
            else:
                driver.get(ZSKY_START_URL)
                time.sleep(3.0)
            is_logged_in, label = check_zsky_login_status(driver)
            self.result_signal.emit(self.account.account_id, is_logged_in, label)
        except Exception as e:
            self.result_signal.emit(self.account.account_id, False, str(e))
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass


class ZskyStudioPage(QWidget):
    settings_changed = Signal()

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.account_store = ZskyAccountStore()

        self._active_runner: ZskyVideoBatchRunnerThread | None = None
        self._active_checker: ZskyCheckAccountThread | None = None
        self._manual_drivers: dict[str, Any] = {}

        self._build_ui()
        self._reload_accounts_table()

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
        for _acc_id, d in list(self._manual_drivers.items()):
            try:
                d.quit()
            except Exception:
                pass
        self._manual_drivers.clear()

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer_layout.addWidget(scroll)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        scroll.setWidget(content)

        # Tiêu đề trang
        title = QLabel("🚀 Zsky AI Studio — Tự Động Farm Video (Google Login & Auto Trim 1.5s)")
        title.setObjectName("pageTitle")
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #1e293b;")
        root.addWidget(title)

        notice = QLabel(
            "💡 Tự động hóa tạo video trên Zsky.ai: Tự gửi prompt (#promptInput), tự bấm tạo (#generateBtn), "
            "vào trang My Creations tải tất cả video về bằng nút Download (<button class='card-action-btn' title='Download'>), "
            "sau đó tự động dùng FFmpeg cắt và xóa 1.5 giây cuối video theo đúng yêu cầu."
        )
        notice.setWordWrap(True)
        notice.setStyleSheet(
            "color: #0369a1; background: #e0f2fe; border: 1px solid #7dd3fc; padding: 10px; border-radius: 8px; font-size: 13px;"
        )
        root.addWidget(notice)

        # =========================================================================
        # 1. QUẢN LÝ TÀI KHOẢN GOOGLE / ZSKY
        # =========================================================================
        acc_box = QGroupBox("👥 Quản Lý Tài Khoản Zsky (Chrome Profiles & Đăng Nhập Google)")
        acc_vbox = QVBoxLayout(acc_box)
        acc_vbox.setSpacing(8)

        # Toolbar tài khoản
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.account_combo = QComboBox()
        self.account_combo.setMinimumHeight(32)
        self.account_combo.setMinimumWidth(240)
        self.account_combo.currentIndexChanged.connect(self._on_account_combo_changed)
        toolbar.addWidget(self.account_combo)

        self.status_badge = QLabel("⚪ Chưa kiểm tra")
        self.status_badge.setStyleSheet("font-weight: 600; color: #0284c7; padding: 0 4px;")
        toolbar.addWidget(self.status_badge)

        self.btn_add_account = QPushButton("➕ Thêm tài khoản")
        self.btn_add_account.setMinimumHeight(32)
        self.btn_add_account.setStyleSheet("background: #2563eb; color: #ffffff; font-weight: 600;")
        self.btn_add_account.clicked.connect(self._open_add_account_dialog)
        toolbar.addWidget(self.btn_add_account)

        self.btn_paste_cookie = QPushButton("➕ Dán Cookie")
        self.btn_paste_cookie.setMinimumHeight(32)
        self.btn_paste_cookie.setStyleSheet("background: #0d9488; color: #ffffff; font-weight: 600;")
        self.btn_paste_cookie.clicked.connect(self._open_paste_cookie_dialog)
        toolbar.addWidget(self.btn_paste_cookie)

        self.btn_open_browser = QPushButton("🌐 Mở Chrome đăng nhập")
        self.btn_open_browser.setMinimumHeight(32)
        self.btn_open_browser.setStyleSheet("background: #0284c7; color: #ffffff; font-weight: 600;")
        self.btn_open_browser.clicked.connect(self._open_browser_for_current_account)
        toolbar.addWidget(self.btn_open_browser)

        self.btn_check_account = QPushButton("✔ Kiểm tra")
        self.btn_check_account.setMinimumHeight(32)
        self.btn_check_account.clicked.connect(self._check_current_account)
        toolbar.addWidget(self.btn_check_account)

        self.btn_delete_account = QPushButton("🗑 Xoá tài khoản")
        self.btn_delete_account.setMinimumHeight(32)
        self.btn_delete_account.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1px solid #f87171; color: #991b1b; font-weight: 600; } "
            "QPushButton:hover { background: #fecaca; }"
        )
        self.btn_delete_account.clicked.connect(self._delete_current_account)
        toolbar.addWidget(self.btn_delete_account)

        toolbar.addStretch()
        acc_vbox.addLayout(toolbar)

        # Bảng danh sách tài khoản
        self.accounts_table = QTableWidget()
        self.accounts_table.setColumnCount(5)
        self.accounts_table.setHorizontalHeaderLabels([
            "☑ Chạy",
            "Tên tài khoản",
            "Email Google",
            "Trạng thái",
            "Số video đã tạo",
        ])
        self.accounts_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.accounts_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.accounts_table.setMinimumHeight(130)
        self.accounts_table.setMaximumHeight(200)
        self.accounts_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.accounts_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.accounts_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.accounts_table.itemChanged.connect(self._on_table_item_changed)
        acc_vbox.addWidget(self.accounts_table)

        root.addWidget(acc_box)

        # =========================================================================
        # 2. CẤU HÌNH & TÙY CHỌN FARM VIDEO
        # =========================================================================
        cfg_box = QGroupBox("⚡ Cấu Hình Chạy & Cắt Video Tự Động")
        cfg_grid = QGridLayout(cfg_box)
        cfg_grid.setSpacing(10)

        cfg_grid.addWidget(QLabel("Chạy cùng lúc:"), 0, 0)
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 20)
        self.concurrency_spin.setValue(int(getattr(self.settings, "zsky_concurrency", 1) or 1))
        self.concurrency_spin.setMinimumHeight(32)
        self.concurrency_spin.valueChanged.connect(self._save_settings)
        cfg_grid.addWidget(self.concurrency_spin, 0, 1)
        cfg_grid.addWidget(QLabel("tài khoản song song (= số tài khoản đã chọn). Mỗi tài khoản gửi và tải lần lượt 1 prompt."), 0, 2)

        self.headless_check = QCheckBox("🔒 Chạy ẩn (Headless - không hiện cửa sổ trình duyệt)")
        self.headless_check.setStyleSheet("font-weight: 600; color: #1e293b;")
        self.headless_check.setChecked(bool(getattr(self.settings, "zsky_headless", False)))
        self.headless_check.toggled.connect(self._save_settings)
        cfg_grid.addWidget(self.headless_check, 0, 3)

        cfg_grid.addWidget(QLabel("✂ Cắt xóa cuối video:"), 1, 0)
        self.trim_spin = QDoubleSpinBox()
        self.trim_spin.setRange(0.0, 10.0)
        self.trim_spin.setSingleStep(0.5)
        self.trim_spin.setDecimals(1)
        self.trim_spin.setValue(float(getattr(self.settings, "zsky_trim_seconds", 1.5) or 1.5))
        self.trim_spin.setMinimumHeight(32)
        self.trim_spin.valueChanged.connect(self._save_settings)
        cfg_grid.addWidget(self.trim_spin, 1, 1)
        cfg_grid.addWidget(QLabel("giây (mặc định 1.5s theo yêu cầu để bỏ watermark/outro)"), 1, 2, 1, 2)

        cfg_grid.addWidget(QLabel("Mô tả âm thanh (tuỳ chọn):"), 2, 0)
        self.audio_prompt_edit = QLineEdit()
        self.audio_prompt_edit.setPlaceholderText("Để trống AI sẽ tự sinh âm thanh phù hợp với video...")
        self.audio_prompt_edit.setMinimumHeight(32)
        cfg_grid.addWidget(self.audio_prompt_edit, 2, 1, 1, 3)

        root.addWidget(cfg_box)

        # =========================================================================
        # 3. DANH SÁCH PROMPT
        # =========================================================================
        prompt_box = QGroupBox("📝 Danh Sách Prompt (Mỗi dòng 1 prompt)")
        prompt_vbox = QVBoxLayout(prompt_box)
        prompt_vbox.setSpacing(8)

        p_toolbar = QHBoxLayout()
        self.btn_import_txt = QPushButton("📁 NHẬP FILE .TXT")
        self.btn_import_txt.setMinimumHeight(34)
        self.btn_import_txt.setStyleSheet("background: #1d4ed8; color: #ffffff; font-weight: 700; padding: 4px 14px;")
        self.btn_import_txt.clicked.connect(self._import_txt_file)
        p_toolbar.addWidget(self.btn_import_txt)

        self.prompt_count_label = QLabel("📊 Tổng số: 0 prompt")
        self.prompt_count_label.setStyleSheet("font-weight: 700; color: #1e293b; background: #f1f5f9; padding: 4px 8px; border-radius: 4px;")
        p_toolbar.addWidget(self.prompt_count_label)

        p_toolbar.addStretch()

        self.btn_clear_prompts = QPushButton("🗑 Xóa danh sách")
        self.btn_clear_prompts.setMinimumHeight(34)
        self.btn_clear_prompts.clicked.connect(self._clear_prompts)
        p_toolbar.addWidget(self.btn_clear_prompts)
        prompt_vbox.addLayout(p_toolbar)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Nhập hoặc dán các prompt vào đây, mỗi dòng là một prompt riêng biệt...\n"
            "Ví dụ:\n"
            "A breathtaking photorealistic wide landscape of Kinkaku-ji's golden pavilion beside a mirror pond in autumn\n"
            "A majestic wolf in snowy winter forest, cinematic lighting, 4k ultra detailed\n"
            "Cyberpunk street with neon lights at rainy night, reflections on wet asphalt"
        )
        self.prompt_edit.setMinimumHeight(140)
        self.prompt_edit.textChanged.connect(self._update_prompt_count)
        prompt_vbox.addWidget(self.prompt_edit)

        root.addWidget(prompt_box)

        # =========================================================================
        # 4. THƯ MỤC LƯU VIDEO OUTPUT
        # =========================================================================
        out_box = QGroupBox("📁 Thư Mục Xuất Video Hoàn Thiện")
        out_layout = QHBoxLayout(out_box)
        out_layout.setSpacing(8)

        default_out = getattr(self.settings, "zsky_last_output_folder", "") or getattr(self.settings, "last_output_folder", "") or str(Path.home() / "Videos")
        self.output_edit = QLineEdit(default_out)
        self.output_edit.setMinimumHeight(34)
        self.output_edit.editingFinished.connect(self._save_settings)
        out_layout.addWidget(self.output_edit, 1)

        btn_choose_out = QPushButton("📂 Chọn thư mục lưu")
        btn_choose_out.setMinimumHeight(34)
        btn_choose_out.clicked.connect(self._choose_output_folder)
        out_layout.addWidget(btn_choose_out)

        root.addWidget(out_box)

        # =========================================================================
        # 5. ĐIỀU KHIỂN & TIẾN ĐỘ CHẠY BATCH
        # =========================================================================
        action_box = QGroupBox("🎬 Bảng Điều Khiển Tác Vụ")
        action_vbox = QVBoxLayout(action_box)
        action_vbox.setSpacing(10)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.btn_start = QPushButton("▶ BẮT ĐẦU FARM VIDEO")
        self.btn_start.setMinimumHeight(44)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setStyleSheet(
            "QPushButton { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #16a34a, stop:1 #15803d); "
            "color: #ffffff; font-size: 15px; font-weight: 800; border-radius: 8px; padding: 0 24px; } "
            "QPushButton:hover { background: #15803d; }"
        )
        self.btn_start.clicked.connect(self._start_batch)
        btn_row.addWidget(self.btn_start, 2)

        self.btn_pause = QPushButton("⏸ Tạm dừng")
        self.btn_pause.setMinimumHeight(44)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        btn_row.addWidget(self.btn_pause, 1)

        self.btn_stop = QPushButton("⏹ Dừng lại")
        self.btn_stop.setMinimumHeight(44)
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #ef4444; color: #b91c1c; font-weight: 700; border-radius: 8px; } "
            "QPushButton:hover { background: #fecaca; }"
        )
        self.btn_stop.clicked.connect(self._stop_batch)
        btn_row.addWidget(self.btn_stop, 1)

        action_vbox.addLayout(btn_row)

        # Stats badges
        stats_layout = QHBoxLayout()
        self.stat_total = QLabel("Tổng: 0")
        self.stat_done = QLabel("✔ Thành công: 0")
        self.stat_failed = QLabel("✖ Lỗi: 0")
        self.stat_rem = QLabel("⏳ Còn lại: 0")

        for lbl, bg, fg in (
            (self.stat_total, "#f1f5f9", "#334155"),
            (self.stat_done, "#dcfce7", "#15803d"),
            (self.stat_failed, "#fee2e2", "#b91c1c"),
            (self.stat_rem, "#fef3c7", "#b45309"),
        ):
            lbl.setStyleSheet(f"font-weight: 700; padding: 6px 12px; border-radius: 6px; background: {bg}; color: {fg}; font-size: 13px;")
            stats_layout.addWidget(lbl)
        stats_layout.addStretch()
        action_vbox.addLayout(stats_layout)

        # Thanh tiến độ
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setMinimumHeight(22)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; font-weight: bold; } "
            "QProgressBar::chunk { background: #2563eb; border-radius: 5px; }"
        )
        action_vbox.addWidget(self.progress_bar)

        self.status_label = QLabel("Sẵn sàng.")
        self.status_label.setStyleSheet("color: #64748b; font-weight: 500;")
        action_vbox.addWidget(self.status_label)

        root.addWidget(action_box)

        # =========================================================================
        # 6. LOG CONSOLE THỜI GIAN THỰC
        # =========================================================================
        log_box = QGroupBox("📜 Nhật Ký Hoạt Động (Logs)")
        log_vbox = QVBoxLayout(log_box)
        log_vbox.setSpacing(6)

        log_tb = QHBoxLayout()
        log_tb.addStretch()
        btn_copy_log = QPushButton("📋 Copy Log")
        btn_copy_log.clicked.connect(self._copy_log)
        log_tb.addWidget(btn_copy_log)
        btn_clear_log = QPushButton("🗑 Xóa Log")
        btn_clear_log.clicked.connect(self._clear_log)
        log_tb.addWidget(btn_clear_log)
        log_vbox.addLayout(log_tb)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(160)
        self.log_view.setStyleSheet(
            "background: #0f172a; color: #e2e8f0; font-family: Consolas, monospace; font-size: 12px; border-radius: 6px; padding: 6px;"
        )
        log_vbox.addWidget(self.log_view)

        root.addWidget(log_box)

    def _append_log(self, text: str) -> None:
        t_str = time.strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{t_str}] {text}")
        self.log_view.ensureCursorVisible()

    def _clear_log(self) -> None:
        self.log_view.clear()

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self.log_view.toPlainText())
        QMessageBox.information(self, "Thông báo", "Đã copy toàn bộ log vào Clipboard!")

    def _save_settings(self) -> None:
        setattr(self.settings, "zsky_concurrency", self.concurrency_spin.value())
        setattr(self.settings, "zsky_headless", self.headless_check.isChecked())
        setattr(self.settings, "zsky_trim_seconds", self.trim_spin.value())
        setattr(self.settings, "zsky_last_output_folder", self.output_edit.text().strip())
        self.settings_changed.emit()

    def _choose_output_folder(self) -> None:
        dir_path = QFileDialog.getExistingDirectory(self, "Chọn thư mục lưu video", self.output_edit.text())
        if dir_path:
            self.output_edit.setText(dir_path)
            self._save_settings()

    def _update_prompt_count(self) -> None:
        lines = [line.strip() for line in self.prompt_edit.toPlainText().splitlines() if line.strip()]
        self.prompt_count_label.setText(f"📊 Tổng số: {len(lines)} prompt")

    def _import_txt_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "Chọn file prompt .txt", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            try:
                content = Path(file_path).read_text(encoding="utf-8", errors="replace")
                lines = [l.strip() for l in content.splitlines() if l.strip()]
                existing = self.prompt_edit.toPlainText().strip()
                if existing:
                    all_lines = existing + "\n" + "\n".join(lines)
                else:
                    all_lines = "\n".join(lines)
                self.prompt_edit.setPlainText(all_lines)
                self._update_prompt_count()
                self._append_log(f"✔ Đã nạp {len(lines)} prompt từ file {Path(file_path).name}")
            except Exception as e:
                QMessageBox.critical(self, "Lỗi", f"Không thể đọc file: {e}")

    def _clear_prompts(self) -> None:
        self.prompt_edit.clear()
        self._update_prompt_count()

    def _reload_accounts_table(self) -> None:
        self.accounts_table.blockSignals(True)
        self.accounts_table.setRowCount(0)
        self.account_combo.blockSignals(True)
        self.account_combo.clear()

        accounts = self.account_store.all()
        for row, acc in enumerate(accounts):
            self.accounts_table.insertRow(row)

            # Checkbox enable
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk_item.setCheckState(Qt.Checked if acc.enabled else Qt.Unchecked)
            chk_item.setData(Qt.UserRole, acc.account_id)
            self.accounts_table.setItem(row, 0, chk_item)

            # Name
            self.accounts_table.setItem(row, 1, QTableWidgetItem(acc.name))

            # Email
            self.accounts_table.setItem(row, 2, QTableWidgetItem(acc.email or "(Chưa có email)"))

            # Status
            st_item = QTableWidgetItem(acc.status_display)
            if acc.status == "active":
                st_item.setForeground(QColor("#15803d"))
            elif acc.status == "need_login":
                st_item.setForeground(QColor("#b45309"))
            elif acc.status == "error":
                st_item.setForeground(QColor("#b91c1c"))
            self.accounts_table.setItem(row, 3, st_item)

            # Tasks completed
            self.accounts_table.setItem(row, 4, QTableWidgetItem(str(acc.tasks_completed)))

            self.account_combo.addItem(acc.combo_label, acc.account_id)

        self.accounts_table.blockSignals(False)
        self.account_combo.blockSignals(False)
        self._on_account_combo_changed()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            acc_id = item.data(Qt.UserRole)
            if acc_id:
                acc = self.account_store.get(acc_id)
                if acc:
                    acc.enabled = (item.checkState() == Qt.Checked)
                    self.account_store.save(acc)
                    enabled_count = len([a for a in self.account_store.all() if a.enabled])
                    if enabled_count > 0:
                        self.concurrency_spin.setValue(enabled_count)

    def _on_account_combo_changed(self) -> None:
        acc_id = self.account_combo.currentData()
        if not acc_id:
            self.status_badge.setText("⚪ Chưa có tài khoản")
            return
        acc = self.account_store.get(acc_id)
        if acc:
            self.status_badge.setText(acc.status_display)

    def _open_add_account_dialog(self) -> None:
        count = len(self.account_store.all()) + 1
        dlg = AddZskyAccountDialog(f"Zsky {count}", self)
        if dlg.exec() == QDialog.Accepted:
            name, email = dlg.get_data()
            acc = self.account_store.create_account(name, email)
            self._reload_accounts_table()
            self._append_log(f"✔ Đã tạo tài khoản: {acc.name}")
            # Hỏi người dùng có muốn mở trình duyệt đăng nhập ngay không
            res = QMessageBox.question(
                self,
                "Đăng nhập Google",
                f"Đã tạo tài khoản '{acc.name}'. Bạn có muốn mở Google Chrome để đăng nhập tài khoản Zsky ngay bây giờ không?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if res == QMessageBox.Yes:
                self._open_browser_for_account(acc)

    def _open_browser_for_current_account(self) -> None:
        acc_id = self.account_combo.currentData()
        if not acc_id:
            QMessageBox.warning(self, "Chú ý", "Vui lòng chọn hoặc tạo một tài khoản trước.")
            return
        acc = self.account_store.get(acc_id)
        if acc:
            self._open_browser_for_account(acc)

    def _open_browser_for_account(self, account: ZskyAccount) -> None:
        if account.account_id in self._manual_drivers:
            QMessageBox.information(
                self,
                "Chrome đang mở",
                f"Trình duyệt Chrome cho tài khoản '{account.name}' đã đang mở rồi.\n\n"
                f"Hãy đăng nhập Google trên cửa sổ đó, sau đó ĐÓNG cửa sổ lại trước khi bấm bắt đầu farm.",
            )
            return

        self._append_log(f"🌐 Đang mở Chrome cho tài khoản [{account.name}] để bạn đăng nhập Google...")

        def run_browser():
            driver = None
            try:
                profile_dir = Path(account.profile_dir).resolve()
                clean_chrome_profile_locks(profile_dir)
                driver = create_zsky_chrome_driver(profile_dir, headless=False)
                self._manual_drivers[account.account_id] = driver
                if account.cookies:
                    inject_cookies_to_driver(driver, account.cookies, target_url=ZSKY_START_URL)
                else:
                    driver.get(ZSKY_START_URL)

                # Giữ cửa sổ mở để người dùng đăng nhập
                while True:
                    try:
                        time.sleep(2.0)
                        handles = driver.window_handles
                        if not handles:
                            break
                        is_ok, label = check_zsky_login_status(driver)
                        if is_ok and account.status != "active":
                            account.status = "active"
                            account.status_message = "Đã đăng nhập"
                            account.last_checked = time.strftime("%d/%m %H:%M")
                            if label and "@" in label:
                                account.email = label
                            self.account_store.save(account)
                    except Exception:
                        break
            except Exception as exc:
                self._append_log(f"❌ Không thể mở Chrome: {exc}")
            finally:
                self._manual_drivers.pop(account.account_id, None)
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                clean_chrome_profile_locks(account.profile_dir)
                self.account_store.save(account)

        t = threading.Thread(target=run_browser, daemon=True)
        t.start()
        QMessageBox.information(
            self,
            "Trình duyệt Chrome đang mở",
            f"Trình duyệt Chrome đã mở với Profile riêng của tài khoản '{account.name}'.\n\n"
            f"1. Hãy bấm 'Sign in with Google' trên trang web và đăng nhập tài khoản của bạn.\n"
            f"2. Sau khi thấy đã đăng nhập, HÃY ĐÓNG CỬA SỔ CHROME lại để lưu phiên!\n\n"
            f"⚠ LƯU Ý: Phải đóng cửa sổ Chrome này trước khi bấm 'Bắt đầu Farm Video' "
            f"để tránh xung đột khóa Profile của Chrome.",
        )

    def _check_current_account(self) -> None:
        acc_id = self.account_combo.currentData()
        if not acc_id:
            return
        acc = self.account_store.get(acc_id)
        if not acc:
            return
        self._append_log(f"🔍 Đang kiểm tra đăng nhập tài khoản [{acc.name}]...")
        self.status_badge.setText("⏳ Đang kiểm tra...")
        self._active_checker = ZskyCheckAccountThread(acc, self)
        self._active_checker.result_signal.connect(self._on_check_result)
        self._active_checker.start()

    def _on_check_result(self, account_id: str, is_logged_in: bool, label: str) -> None:
        acc = self.account_store.get(account_id)
        if acc:
            acc.last_checked = time.strftime("%d/%m %H:%M")
            if is_logged_in:
                acc.status = "active"
                acc.status_message = "Đã đăng nhập"
                if label and "@" in label:
                    acc.email = label
                self._append_log(f"✔ Tài khoản [{acc.name}] đã đăng nhập Google thành công ({label})")
            else:
                acc.status = "need_login"
                acc.status_message = "Chưa đăng nhập"
                self._append_log(f"⚠ Tài khoản [{acc.name}] chưa đăng nhập Google trên Zsky.ai.")
            self.account_store.save(acc)
            self._reload_accounts_table()

    def _open_paste_cookie_dialog(self) -> None:
        """Mở hộp thoại dán Cookie để thêm trực tiếp tài khoản mới (giống Muse)."""
        default_name = self.account_store.next_name()
        dlg = PasteCookieDialog(default_name, self)
        if dlg.exec() == QDialog.Accepted:
            raw, name, email = dlg.get_data()
            if not raw:
                QMessageBox.warning(self, "Thiếu dữ liệu", "Vui lòng dán chuỗi cookie vào khung.")
                return
            try:
                acc = self.account_store.import_from_json(raw, default_name=name, default_email=email)
                QMessageBox.information(
                    self,
                    "Thêm Cookie thành công",
                    f"Đã thêm tài khoản: {acc.combo_label}\n"
                    f"Tổng số cookies nhận diện: {len(acc.cookies)}.",
                )
                self._reload_accounts_table()
                idx = self.account_combo.findData(acc.account_id)
                if idx >= 0:
                    self.account_combo.setCurrentIndex(idx)
                self._append_log(f"✔ Đã thêm tài khoản Zsky qua Cookie: {acc.combo_label} ({len(acc.cookies)} cookies)")
            except Exception as exc:
                QMessageBox.critical(self, "Lỗi Cookie", f"Không thể nhận diện cookie: {exc}")

    def _delete_current_account(self) -> None:
        acc_id = self.account_combo.currentData()
        if not acc_id:
            return
        acc = self.account_store.get(acc_id)
        if not acc:
            return
        res = QMessageBox.question(
            self,
            "Xác nhận xóa",
            f"Bạn có chắc muốn xóa tài khoản '{acc.name}' và toàn bộ Chrome Profile của nó không?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if res == QMessageBox.Yes:
            self.account_store.delete(acc_id)
            self._reload_accounts_table()
            self._append_log(f"🗑 Đã xóa tài khoản: {acc.name}")

    def _start_batch(self) -> None:
        prompts = [line.strip() for line in self.prompt_edit.toPlainText().splitlines() if line.strip()]
        if not prompts:
            QMessageBox.warning(self, "Chưa có prompt", "Vui lòng nhập ít nhất một prompt vào danh sách.")
            return

        enabled_accounts = [acc for acc in self.account_store.all() if acc.enabled]
        if not enabled_accounts:
            QMessageBox.warning(self, "Chưa chọn tài khoản", "Hãy tích chọn ít nhất 1 tài khoản trong bảng tài khoản để chạy.")
            return

        # Luồng đồng thời = số tài khoản đã chọn
        concurrency = len(enabled_accounts)
        self.concurrency_spin.setValue(concurrency)

        # Tự động đóng các cửa sổ Chrome đăng nhập thủ công nếu còn mở để giải phóng Profile
        if self._manual_drivers:
            for acc_id, d in list(self._manual_drivers.items()):
                try:
                    d.quit()
                except Exception:
                    pass
            self._manual_drivers.clear()
            self._append_log("ℹ Đã tự động đóng cửa sổ Chrome đăng nhập để giải phóng Profile cho luồng farm.")
            time.sleep(1.0)

        out_dir = Path(self.output_edit.text().strip()).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        self._save_settings()

        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setText("⏸ Tạm dừng")

        self._active_runner = ZskyVideoBatchRunnerThread(
            accounts=enabled_accounts,
            prompts=prompts,
            output_dir=out_dir,
            audio_prompt=self.audio_prompt_edit.text().strip(),
            concurrency=self.concurrency_spin.value(),
            headless=self.headless_check.isChecked(),
            trim_seconds=self.trim_spin.value(),
            ffmpeg_path=self.settings.ffmpeg_path,
            ffprobe_path=self.settings.ffprobe_path,
            parent=self,
        )

        self._active_runner.log_signal.connect(self._append_log)
        self._active_runner.progress_signal.connect(self._on_progress)
        self._active_runner.stats_signal.connect(self._on_stats)
        self._active_runner.account_status_signal.connect(self._on_account_status_update)
        self._active_runner.batch_finished_signal.connect(self._on_batch_finished)

        self._active_runner.start()

    def _toggle_pause(self) -> None:
        if self._active_runner:
            is_paused = self._active_runner.toggle_pause()
            self.btn_pause.setText("▶ Tiếp tục" if is_paused else "⏸ Tạm dừng")
            self.status_label.setText("Đang tạm dừng..." if is_paused else "Đang chạy...")

    def _stop_batch(self) -> None:
        if self._active_runner:
            self._append_log("🛑 Đang gửi lệnh dừng tới các worker...")
            self._active_runner.stop()
            self.btn_stop.setEnabled(False)

    def _on_progress(self, pct: int, msg: str) -> None:
        self.progress_bar.setValue(pct)
        self.status_label.setText(msg)

    def _on_stats(self, total: int, done: int, failed: int, rem: int) -> None:
        self.stat_total.setText(f"Tổng: {total}")
        self.stat_done.setText(f"✔ Thành công: {done}")
        self.stat_failed.setText(f"✖ Lỗi: {failed}")
        self.stat_rem.setText(f"⏳ Còn lại: {rem}")

    def _on_account_status_update(self, account_id: str, status: str, msg: str) -> None:
        acc = self.account_store.get(account_id)
        if acc:
            acc.status = status
            acc.status_message = msg
            self.account_store.save(acc)
            self._reload_accounts_table()

    def _on_batch_finished(self, success: bool, msg: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.status_label.setText(msg)
        if success:
            QMessageBox.information(self, "Hoàn tất Farm Video Zsky AI", f"Đã hoàn thành farm video!\n{msg}")
        else:
            QMessageBox.warning(self, "Tạm dừng hoặc Có lỗi", f"Quá trình farm video đã dừng:\n{msg}")
