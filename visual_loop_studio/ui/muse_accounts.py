from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
    DEFAULT_MUSE_STYLE_SUFFIX,
    MUSE_COOKIE_PROFILES_DIR,
    MUSE_DEFAULT_URL,
    MuseCookieAccount,
    MuseCookieAccountStore,
    build_muse_chunk_prompt,
    check_single_cookie_account,
    extract_email_from_muse_page,
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
    _dismiss_muse_popups,
    _download_muse_video_file,
    _find,
    _hostname,
    _query_muse_videos,
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

    account_checked = Signal(str, bool, str, str, str)  # account_id, is_active, message, time_str, detected_email
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
                is_active, detected_email, msg = check_single_cookie_account(acc, headless=self.headless)
            except Exception as exc:
                is_active = False
                detected_email = ""
                msg = f"Lỗi: {exc}"

            if is_active:
                active_count += 1
            self.account_checked.emit(acc.account_id, is_active, msg, now_str, detected_email or "")

        self.all_finished.emit(active_count, len(self.accounts))


@dataclass
class MuseBatchChunk:
    chunk_index: int
    prompt_items: list[tuple[int, str]]  # list of (1-based prompt_id, prompt_text)
    images: list[Path] = field(default_factory=list)


class MuseCookieBatchRunnerThread(QThread):
    """Luồng chạy batch tạo video/ảnh qua danh sách tài khoản Cookie (gom 5 prompt/lần, Concurrency 1-5, tự F5 ngầm)."""

    progress_signal = Signal(int, str)
    stats_signal = Signal(int, int, int, int)  # total, completed, failed, remaining
    log_signal = Signal(str)
    worker_status_signal = Signal(int, str)  # worker_idx (1-5), status_text
    account_completed_signal = Signal(str, int)  # account_id, count
    account_email_found_signal = Signal(str, str)  # account_id, detected_email
    batch_finished_signal = Signal(bool, str)

    def __init__(
        self,
        accounts: list[MuseCookieAccount],
        concurrency: int,
        headless: bool,
        task_mode: str,
        chunks: list[MuseBatchChunk],
        total_prompts: int,
        settings: MuseVideoSettings,
        output_dir: Path,
        auto_refresh_minutes: int = 60,
        style_suffix: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.accounts = accounts
        self.concurrency = max(1, min(5, concurrency))
        self.headless = headless
        self.task_mode = task_mode
        self.chunks = chunks
        self.total_prompts = total_prompts
        self.settings = settings
        self.output_dir = output_dir
        self.auto_refresh_minutes = max(0, auto_refresh_minutes)
        self.style_suffix = style_suffix
        self._stopped = False

    @staticmethod
    def _fill_chat_prompt(driver: Any, text: str) -> bool:
        """Safely fill multi-line prompt into Muse chat input as 1 single message without submitting on Enter,
        ensuring React internal state is updated so the Send button becomes enabled."""
        from selenium.webdriver.common.keys import Keys

        selectors = (
            "textarea[aria-label='Nhắn tin']",
            "textarea[placeholder*='nhắn tin' i]",
            "textarea[aria-label='Message']",
            "textarea[placeholder='Message']",
            "[data-testid='prompt-input']",
            "[data-testid*='composer' i] textarea",
            "textarea[aria-label*='prompt' i]",
            "textarea[placeholder*='prompt' i]",
            "[contenteditable='true'][role='textbox'][aria-label*='prompt' i]",
            "textarea",
            "[contenteditable='true'][role='textbox']",
            "[contenteditable='true']",
        )

        for selector in selectors:
            fields = _find(driver, "css selector", selector)
            valid_fields = []
            for f in reversed(fields):
                if not _visible(f):
                    continue
                try:
                    is_sidebar = driver.execute_script(
                        "return !!arguments[0].closest('aside,nav,[data-testid*=\"sidebar\" i],[data-testid*=\"timeline\" i],[data-testid*=\"history\" i]');",
                        f,
                    )
                    if not is_sidebar:
                        valid_fields.append(f)
                except Exception:
                    valid_fields.append(f)

            for field in valid_fields:
                try:
                    field.click()
                    time.sleep(0.15)

                    # Cách 1: Đưa text vào qua JavaScript với React native setter + execCommand
                    js_res = driver.execute_script(
                        """
                        const el = arguments[0];
                        const val = arguments[1];
                        el.focus();

                        // 1. Thử execCommand('insertText') để mô phỏng gõ/dán thật mà không kích hoạt phím Enter
                        let inserted = false;
                        try {
                            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                                el.select();
                            } else {
                                document.execCommand('selectAll', false, null);
                            }
                            inserted = document.execCommand('insertText', false, val);
                        } catch(e) {}

                        // 2. Prototype descriptor setter để kích hoạt React onChange
                        const cur = (el.value || el.innerText || '').trim();
                        if (!inserted || cur.length < 10) {
                            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                                const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                                const setMethod = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                                if (setMethod) {
                                    setMethod.call(el, val);
                                } else {
                                    el.value = val;
                                }
                                el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
                            } else {
                                el.innerText = val;
                                el.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
                            }
                        }

                        // Kiểm tra lại xem nội dung đã vào chưa
                        const finalVal = (el.value || el.innerText || '').trim();
                        return finalVal.length > 10;
                        """,
                        field,
                        text,
                    )
                    if js_res:
                        return True

                    # Cách 2: Dự phòng Shift+Enter
                    tag = str(field.tag_name or "").lower()
                    if tag in {"textarea", "input"}:
                        field.clear()
                    else:
                        field.send_keys("\ue009", "a")

                    lines = text.split("\n")
                    for i, line in enumerate(lines):
                        if line:
                            field.send_keys(line)
                        if i < len(lines) - 1:
                            field.send_keys(Keys.SHIFT, Keys.ENTER)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _submit_chat_prompt(driver: Any) -> bool:
        """Gửi prompt trong chat Muse bằng cách bấm nút Send / Generate (hỗ trợ icon SVG) hoặc phím Enter trong composer."""
        from selenium.webdriver.common.keys import Keys

        # 1. Thử click nút Send / Generate qua JavaScript chuyên sâu (hỗ trợ cả nút SVG và composer buttons)
        try:
            clicked = bool(
                driver.execute_script(
                    """
                    const isInsideSidebar = el => !!el.closest('aside,nav,[data-testid*="sidebar" i],[data-testid*="timeline" i],[data-testid*="history" i]');
                    const isInsideChat = el => !!el.closest('[data-message-item="true"],[data-message-role],[data-message-group-id]');
                    const isStopOrCancel = el => {
                        if (!el) return false;
                        const label = ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '') + ' ' + (el.getAttribute('title') || '')).toLowerCase();
                        return /stop|dừng|cancel|hủy|abort|delete|xóa/i.test(label);
                    };

                    const triggerClick = el => {
                        if (!el || isStopOrCancel(el)) return false;
                        try { el.focus(); } catch(e) {}
                        try { el.click(); } catch(e) {}
                        return true;
                    };

                    // 1. Selector trực tiếp nút Send / Generate
                    const selectors = [
                        'button[aria-label="Send" i]',
                        'button[aria-label*="send message" i]',
                        'button[aria-label*="send" i]',
                        'button[aria-label*="tạo" i]',
                        'button[aria-label*="gửi" i]',
                        'button[aria-label*="create" i]',
                        'button[aria-label*="generate" i]',
                        'button[type="submit"][aria-label*="send" i]',
                        'button[type="submit"]',
                        'button[data-testid*="send" i]',
                        'button[data-testid*="generate" i]',
                        'button[data-testid*="create" i]'
                    ];
                    for (const sel of selectors) {
                        const btns = [...document.querySelectorAll(sel)].filter(b => {
                            if (isInsideSidebar(b) || isInsideChat(b) || isStopOrCancel(b)) return false;
                            const r = b.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
                        });
                        if (btns.length) {
                            return triggerClick(btns[btns.length - 1]);
                        }
                    }

                    // 2. Tìm nút trong container của textarea composer (kể cả nút chỉ có icon SVG)
                    const textareas = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(t => {
                        if (isInsideSidebar(t) || isInsideChat(t)) return false;
                        const r = t.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    if (textareas.length) {
                        const ta = textareas[textareas.length - 1];
                        const container = ta.closest('form,[data-testid*="composer" i],div.relative,div') || ta.parentElement;
                        if (container) {
                            const btns = [...container.querySelectorAll('button,[role="button"]')].filter(b => {
                                if (b === ta || isInsideSidebar(b) || isInsideChat(b) || isStopOrCancel(b)) return false;
                                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                                if (/attach|upload|file|image|add|close|dismiss|preview|stop|dừng|cancel|hủy|xóa/i.test(aria)) return false;
                                const r = b.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
                            });
                            const sendBtn = btns.find(b => {
                                if (isStopOrCancel(b)) return false;
                                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                                const type = (b.getAttribute('type') || '').toLowerCase();
                                return /send|submit|create|generate|tạo|gửi/i.test(aria) || type === 'submit' || Boolean(b.querySelector('svg path,svg'));
                            });
                            if (sendBtn && !isStopOrCancel(sendBtn)) {
                                return triggerClick(sendBtn);
                            }
                        }
                        // 3. Kích hoạt phím Enter trong JS
                        try {
                            ta.focus();
                            const evt = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true });
                            ta.dispatchEvent(evt);
                            return true;
                        } catch(e) {}
                    }
                    return false;
                    """
                )
            )
            if clicked:
                return True
        except Exception:
            pass

        # 2. Thử _click_by_text với tập nhãn mở rộng (tránh tuyệt đối nút dừng/hủy)
        extended_submit_texts = (
            "send", "generate", "create video", "create", "gửi", "tạo video", "tạo"
        )
        if _click_by_text(driver, extended_submit_texts):
            return True

        # 3. Thử qua Selenium find elements
        for sel in (
            "button[aria-label*='send' i]",
            "button[data-testid*='send' i]",
            "button[type='submit']",
        ):
            for btn in reversed(_find(driver, "css selector", sel)):
                if _clickable(btn):
                    lbl = (btn.get_attribute("aria-label") or "") + " " + (btn.text or "")
                    if re.search(r"stop|dừng|cancel|hủy|xóa", lbl, re.I):
                        continue
                    try:
                        btn.click()
                        return True
                    except Exception:
                        try:
                            driver.execute_script("arguments[0].click();", btn)
                            return True
                        except Exception:
                            pass

        # 4. Fallback tối hậu: Focus vào ô textarea và gửi Keys.RETURN / Keys.ENTER bằng Selenium
        try:
            for sel in (
                "textarea[aria-label='Message']",
                "textarea[placeholder='Message']",
                "[data-testid*='composer' i] textarea",
                "textarea",
                "[contenteditable='true']",
            ):
                for ta in reversed(_find(driver, "css selector", sel)):
                    if _clickable(ta):
                        try:
                            ta.send_keys(Keys.RETURN)
                            return True
                        except Exception:
                            pass
        except Exception:
            pass

        return False

    def stop(self) -> None:
        self._stopped = True

    def run(self) -> None:
        if not self.accounts:
            self.batch_finished_signal.emit(False, "Không có tài khoản nào được tick để chạy.")
            return

        if not self.chunks or self.total_prompts == 0:
            self.batch_finished_signal.emit(False, "Không có prompt nào để xử lý.")
            return

        action_name = "ảnh" if self.task_mode == "image" else "video"

        # 1. Sắp xếp tài khoản theo thứ tự tự nhiên (Muse 1 luôn đứng trước Muse 2)
        def _natural_key(s: str) -> list[int | str]:
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

        sorted_accounts = sorted(self.accounts, key=lambda a: _natural_key(a.name or a.account_id))

        # 2. Hàng đợi chung các đợt prompt (Dynamic Work Queue)
        # Các luồng tài khoản song song tự động nhận đợt kế tiếp ngay khi hoàn thành đợt hiện tại
        shared_chunks = list(self.chunks)
        chunks_lock = threading.Lock()
        total_chunks = len(self.chunks)

        total_items = self.total_prompts
        completed_total = 0
        failed_total = 0
        remaining_total = total_items
        stats_lock = threading.Lock()
        self.stats_signal.emit(total_items, completed_total, failed_total, remaining_total)

        effective_concurrency = max(1, min(self.concurrency, len(sorted_accounts)))
        account_pool = list(sorted_accounts)
        account_pool_lock = threading.Lock()

        self.log_signal.emit(
            f"🚀 Bắt đầu batch {action_name}: {self.total_prompts} prompt ({total_chunks} đợt gom 5 prompt/lần) "
            f"trên {len(sorted_accounts)} tài khoản (Chạy song song: {effective_concurrency} luồng, Chạy ẩn: {self.headless}, "
            f"F5 ngầm mỗi: {self.auto_refresh_minutes} phút)."
        )

        def worker_task(worker_id: int) -> None:
            nonlocal completed_total, failed_total, remaining_total
            while not self._stopped:
                current_acc = None
                with account_pool_lock:
                    if account_pool:
                        current_acc = account_pool.pop(0)
                if not current_acc:
                    self.worker_status_signal.emit(worker_id, "Sẵn sàng (Đã hết tài khoản)")
                    break

                acc_label = current_acc.combo_label
                worker_tag = f"Luồng {worker_id}: {acc_label}"
                self.worker_status_signal.emit(worker_id, f"Khởi tạo Chrome [{acc_label}]…")
                self.log_signal.emit(f"⚙ [{worker_tag}]: Bắt đầu phiên làm việc trên Muse.")

                profile_dir = MUSE_COOKIE_PROFILES_DIR / current_acc.account_id
                profile_dir.mkdir(parents=True, exist_ok=True)
                download_dir = Path(tempfile.mkdtemp(prefix=f"muse_w{worker_id}_", dir=CACHE_DIR))

                driver = None
                acc_completed = 0
                last_f5_time = time.monotonic()
                try:
                    driver = create_muse_chrome_driver(profile_dir, download_dir=download_dir, headless=self.headless)
                    driver.set_page_load_timeout(35)
                    self.worker_status_signal.emit(worker_id, f"Nạp cookie [{acc_label}]…")
                    inject_cookies_to_driver(driver, current_acc.cookies, target_url=MUSE_DEFAULT_URL)
                    time.sleep(3.0)
                    _dismiss_muse_popups(driver)

                    # Cuộn nhẹ để DOM hydrate
                    try:
                        driver.execute_script("""
                            const w = [...document.querySelectorAll('[data-hatch-video-wrapper],[data-hatch-video-src],video')].pop();
                            if (w) w.scrollIntoView({block: 'center'});
                        """)
                    except Exception:
                        pass
                    time.sleep(1.0)

                    # Bắt email nếu chưa có
                    if not current_acc.email:
                        found_em = extract_email_from_muse_page(driver)
                        if found_em:
                            current_acc.email = found_em
                            self.account_email_found_signal.emit(current_acc.account_id, found_em)

                    _dismiss_muse_popups(driver)

                    # Kiểm tra xem tài khoản có bị hết credit/token ngay từ đầu không
                    init_body = _body_text(driver).lower()
                    if any(m in init_body for m in QUOTA_MARKERS):
                        self.log_signal.emit(
                            f"⚠ [{worker_tag}]: Tài khoản đã hết token/credit ngay từ đầu. "
                            f"Tự động chuyển sang tài khoản tiếp theo trong danh sách…"
                        )
                        continue

                    # Đánh dấu các video cũ đã có sẵn trên trang để tránh trùng lặp
                    initial_vids = _query_muse_videos(driver)
                    seen_video_srcs: set[str] = {v.get("src") for v in initial_vids if v.get("src")}
                    seen_video_ids: set[str] = {v.get("id") for v in initial_vids if v.get("id")}
                    if initial_vids:
                        self.log_signal.emit(f"ℹ [{worker_tag}]: Đã đánh dấu {len(initial_vids)} video cũ có sẵn trên trang (để không tải nhầm).")

                    # Vòng lặp lấy đợt prompt từ hàng đợi chung (Dynamic Work Queue)
                    while not self._stopped:
                        chunk = None
                        with chunks_lock:
                            if shared_chunks:
                                chunk = shared_chunks.pop(0)
                        if not chunk:
                            # Hết prompt trong toàn bộ batch
                            break

                        n_prompts = len(chunk.prompt_items)
                        first_id = chunk.prompt_items[0][0]
                        last_id = chunk.prompt_items[-1][0]
                        chunk_label = f"Đợt {chunk.chunk_index}/{total_chunks} (Prompt #{first_id:04d} - #{last_id:04d})"

                        # Kiểm tra xem toàn bộ prompt trong đợt này đã có file hoàn tất trong output_dir chưa
                        all_chunk_done = True
                        for p_id, _ in chunk.prompt_items:
                            if not any(f.name.startswith(f"Prompt{p_id:04d}") and f.stat().st_size > 10240 for f in self.output_dir.iterdir() if f.is_file()):
                                all_chunk_done = False
                                break
                        if all_chunk_done:
                            with stats_lock:
                                completed_total += n_prompts
                                acc_completed += n_prompts
                                remaining_total = max(0, total_items - (completed_total + failed_total))
                                self.stats_signal.emit(total_items, completed_total, failed_total, remaining_total)
                                pct = int(((completed_total + failed_total) / max(1, total_items)) * 100)
                                self.progress_signal.emit(pct, f"Đang xử lý: {completed_total}/{total_items} hoàn tất")
                            self.log_signal.emit(f"⏩ [{worker_tag}]: Đã có sẵn đủ {n_prompts} video trong thư mục ({chunk_label}), chuyển đợt tiếp theo.")
                            continue

                        # Kiểm tra chu kỳ tự động F5 ngầm
                        if self.auto_refresh_minutes > 0 and (time.monotonic() - last_f5_time) >= (self.auto_refresh_minutes * 60):
                            self.worker_status_signal.emit(worker_id, f"F5 ngầm [{acc_label}]…")
                            self.log_signal.emit(f"🔄 [{worker_tag}]: F5 Muse ngầm theo chu kỳ {self.auto_refresh_minutes} phút…")
                            try:
                                driver.refresh()
                                time.sleep(2.0)
                                _dismiss_muse_popups(driver)
                            except Exception:
                                pass
                            last_f5_time = time.monotonic()

                        self.worker_status_signal.emit(worker_id, f"[{acc_label}] {chunk_label}…")
                        self.log_signal.emit(f"🎬 [{worker_tag}]: Bắt đầu gửi nhóm {n_prompts} prompt ({chunk_label})…")

                        try:
                            _dismiss_muse_popups(driver)

                            # Upload ảnh nếu có
                            if chunk.images:
                                valid_imgs = [p for p in chunk.images if p and p.is_file()]
                                if valid_imgs:
                                    self.worker_status_signal.emit(worker_id, f"[{acc_label}] Đang tải {len(valid_imgs)} ảnh mẫu lên Muse…")
                                    inputs = _find(driver, "css selector", "input[type='file']")
                                    if not inputs:
                                        _click_by_text(driver, ATTACH_TEXTS)
                                        inputs = _find(driver, "css selector", "input[type='file']")
                                    for field in reversed(inputs):
                                        try:
                                            joined = "\n".join(str(p.resolve()) for p in valid_imgs)
                                            field.send_keys(joined)
                                            break
                                        except Exception:
                                            for p in valid_imgs:
                                                try:
                                                    field.send_keys(str(p.resolve()))
                                                    time.sleep(0.3)
                                                except Exception:
                                                    pass
                                            break
                                    time.sleep(1.5)

                            chunk_prompt_text = build_muse_chunk_prompt(
                                chunk.prompt_items,
                                action_type=self.task_mode,
                                aspect_ratio=self.settings.aspect_ratio or "16:9",
                                duration=self.settings.duration or "11s",
                                style_suffix=self.style_suffix,
                            )

                            pre_chunk_vids = _query_muse_videos(driver)
                            for pv in pre_chunk_vids:
                                if pv.get("src"):
                                    seen_video_srcs.add(pv.get("src"))
                                if pv.get("id"):
                                    seen_video_ids.add(pv.get("id"))

                            self.worker_status_signal.emit(worker_id, f"[{acc_label}] Đang điền prompt vào Muse…")
                            filled = self._fill_chat_prompt(driver, chunk_prompt_text)
                            if not filled:
                                self.log_signal.emit(f"⚠ [{worker_tag}]: Không thể điền prompt tự động vào selector chuẩn.")
                            time.sleep(1.0)

                            self.worker_status_signal.emit(worker_id, f"[{acc_label}] Đang bấm gửi prompt ({chunk_label})…")
                            submitted = self._submit_chat_prompt(driver)
                            time.sleep(1.5)

                            still_in_composer = False
                            try:
                                still_in_composer = bool(
                                    driver.execute_script(
                                        """
                                        const prompt = (arguments[0] || '').slice(0, 30).trim().toLowerCase();
                                        if (!prompt) return false;
                                        const inChat = [...document.querySelectorAll('[data-message-item="true"],[data-message-role],[data-message-group-id],[class*="message" i]')].some(m => {
                                            return (m.innerText || '').toLowerCase().includes(prompt);
                                        });
                                        if (inChat) return false;
                                        const tas = [...document.querySelectorAll('textarea,[contenteditable="true"]')].filter(
                                            t => !t.closest('aside,nav,[data-testid*="sidebar" i],[data-testid*="history" i]')
                                        );
                                        return tas.some(t => {
                                            const val = (t.value || t.innerText || '').trim().toLowerCase();
                                            return val.length > 20 && val.includes(prompt);
                                        });
                                        """,
                                        chunk_prompt_text,
                                    )
                                )
                            except Exception:
                                pass

                            if still_in_composer:
                                self.log_signal.emit(f"🔄 [{worker_tag}]: Prompt còn trong ô nhập, gửi lại bằng Enter…")
                                try:
                                    from selenium.webdriver.common.keys import Keys
                                    for ta in reversed(_find(driver, "css selector", "textarea,[contenteditable='true']")):
                                        if _clickable(ta):
                                            ta.send_keys(Keys.RETURN)
                                            submitted = True
                                            break
                                except Exception:
                                    pass
                                time.sleep(1.0)

                            if submitted:
                                self.log_signal.emit(f"🚀 [{worker_tag}]: Đã gửi thành công nhóm prompt ({chunk_label}) tới Muse!")
                            else:
                                self.log_signal.emit(f"⚠ [{worker_tag}]: Đã kích hoạt lệnh gửi ({chunk_label}), đang theo dõi tạo video…")

                            # Kiểm tra ngay sau khi gửi xem có thông báo hết quota xuất hiện không
                            time.sleep(1.0)
                            post_submit_body = _body_text(driver).lower()
                            if any(m in post_submit_body for m in QUOTA_MARKERS):
                                raise RuntimeError(f"Hết quota/credit trên tài khoản {acc_label}.")

                            self.worker_status_signal.emit(worker_id, f"[{acc_label}] Đang theo dõi và tải {n_prompts} video ({chunk_label})…")
                            start_wait = time.monotonic()
                            unassigned_prompts = list(chunk.prompt_items)
                            batch_success_count = 0
                            stable_idle_cycles = 0
                            last_heartbeat = time.monotonic()

                            while time.monotonic() - start_wait < 300.0:
                                if self._stopped:
                                    break
                                body = _body_text(driver).lower()
                                if any(m in body for m in QUOTA_MARKERS):
                                    raise RuntimeError(f"Hết quota/credit trên tài khoản {acc_label}.")

                                try:
                                    driver.execute_script("""
                                        const w = [...document.querySelectorAll('[data-hatch-video-wrapper],[data-hatch-video-src],video')].pop();
                                        if (w) w.scrollIntoView({block: 'center'});
                                    """)
                                except Exception:
                                    pass

                                current_vids = _query_muse_videos(driver)
                                fresh_vids = [
                                    v for v in current_vids
                                    if v.get("src")
                                    and v.get("id") not in seen_video_ids
                                    and v.get("src") not in seen_video_srcs
                                ]

                                if fresh_vids and unassigned_prompts:
                                    stable_idle_cycles = 0
                                    for v_item in fresh_vids:
                                        if self._stopped or not unassigned_prompts:
                                            break

                                        v_id = v_item.get("id")
                                        v_src = (v_item.get("src") or "").strip()
                                        if v_id:
                                            seen_video_ids.add(v_id)
                                        if v_src:
                                            seen_video_srcs.add(v_src)

                                        v_info_str = (v_item.get("name") or "") + " " + (v_item.get("label") or "") + " " + (v_item.get("text") or "")
                                        m = re.search(r'(?<![a-z0-9])(?:v|prompt[-_]?)?0*(\d{1,6})(?!\d)', v_info_str, re.I)
                                        matched_p = None
                                        if m:
                                            target_id = int(m.group(1))
                                            matched_p = next((p for p in unassigned_prompts if p[0] == target_id), None)

                                        if not matched_p:
                                            matched_p = unassigned_prompts[0]

                                        unassigned_prompts.remove(matched_p)
                                        p_id, p_text = matched_p

                                        slug_snippet = re.sub(r'[^\w\-\.]+', '_', p_text[:35].strip()).strip('_')
                                        if not slug_snippet:
                                            slug_snippet = "video"
                                        target_filename = f"Prompt{p_id:04d}_{slug_snippet}.mp4"
                                        target_dest = self.output_dir / target_filename
                                        if target_dest.exists():
                                            target_dest = self.output_dir / f"Prompt{p_id:04d}_{int(time.time())}_{slug_snippet}.mp4"

                                        self.worker_status_signal.emit(worker_id, f"[{acc_label}] Tải ngay Prompt #{p_id:04d}…")
                                        dl_ok = _download_muse_video_file(driver, v_item, target_dest, download_dir)
                                        if dl_ok and target_dest.exists() and target_dest.stat().st_size > 10240:
                                            batch_success_count += 1
                                            with stats_lock:
                                                completed_total += 1
                                                acc_completed += 1
                                                remaining_total = max(0, total_items - (completed_total + failed_total))
                                                self.stats_signal.emit(total_items, completed_total, failed_total, remaining_total)
                                                pct = int(((completed_total + failed_total) / max(1, total_items)) * 100)
                                                self.progress_signal.emit(pct, f"Đang xử lý: {completed_total}/{total_items} hoàn tất")
                                            mb_size = target_dest.stat().st_size / (1024 * 1024)
                                            self.log_signal.emit(f"✔ [{worker_tag}]: Đã tải xong Prompt #{p_id:04d} -> {target_dest.name} ({mb_size:.2f} MB)")
                                        else:
                                            self.log_signal.emit(f"⚠ [{worker_tag}]: Tải không thành công Prompt #{p_id:04d}")

                                if not unassigned_prompts:
                                    self.log_signal.emit(f"🎉 [{worker_tag}]: Đã tải đủ toàn bộ {n_prompts} video của {chunk_label}! Chuyển sang gửi prompt đợt tiếp theo ngay lập tức…")
                                    break

                                if time.monotonic() - last_heartbeat >= 20.0:
                                    elapsed_sec = int(time.monotonic() - start_wait)
                                    self.worker_status_signal.emit(worker_id, f"[{acc_label}] Chờ video ({batch_success_count}/{n_prompts} xong, {elapsed_sec}s)…")
                                    last_heartbeat = time.monotonic()

                                is_rendering = False
                                try:
                                    is_rendering = bool(driver.execute_script("""
                                        const indicators = [
                                            '[data-testid*="processing" i]',
                                            '[data-testid*="progress" i]',
                                            '[data-testid*="generating" i]',
                                            '[aria-busy="true"]',
                                            'button[aria-label*="stop" i]',
                                            '[class*="spinner" i]',
                                            '[class*="loading" i]',
                                            '[class*="generating" i]'
                                        ];
                                        return indicators.some(sel => {
                                            const els = [...document.querySelectorAll(sel)];
                                            return els.some(e => {
                                                const r = e.getBoundingClientRect();
                                                return r.width > 0 && r.height > 0;
                                            });
                                        });
                                    """))
                                except Exception:
                                    pass

                                if not is_rendering:
                                    stable_idle_cycles += 1
                                    if batch_success_count > 0 and stable_idle_cycles >= 6:
                                        self.log_signal.emit(f"ℹ [{worker_tag}]: Muse đã ngừng tạo video ({batch_success_count}/{n_prompts} hoàn tất). Chuyển sang đợt tiếp theo.")
                                        break
                                    if batch_success_count == 0 and stable_idle_cycles >= 20 and (time.monotonic() - start_wait) > 50.0:
                                        self.log_signal.emit(f"⚠ [{worker_tag}]: Không thấy tiến trình tạo video trên trang sau 50s. Tiếp tục đợt sau…")
                                        break
                                else:
                                    stable_idle_cycles = 0

                                time.sleep(2.0)

                            if unassigned_prompts:
                                missed_count = len(unassigned_prompts)
                                with stats_lock:
                                    failed_total += missed_count
                                    remaining_total = max(0, total_items - (completed_total + failed_total))
                                    self.stats_signal.emit(total_items, completed_total, failed_total, remaining_total)
                                    pct = int(((completed_total + failed_total) / max(1, total_items)) * 100)
                                    self.progress_signal.emit(pct, f"Đang xử lý: {completed_total}/{total_items} hoàn tất")
                                missed_ids_str = ", ".join(f"#{p[0]:04d}" for p in unassigned_prompts)
                                self.log_signal.emit(f"⚠ [{worker_tag}]: {missed_count} prompt chưa có video ({missed_ids_str}).")

                            if batch_success_count > 0:
                                self.log_signal.emit(f"✔ [{worker_tag}]: Hoàn tất đợt ({chunk_label}): Đã lưu {batch_success_count}/{n_prompts} video.")
                            else:
                                self.log_signal.emit(f"❌ [{worker_tag}]: Không lưu được video nào trong ({chunk_label}).")

                            time.sleep(1.0)

                        except Exception as chunk_err:
                            err_str = str(chunk_err).lower()
                            is_quota = any(
                                marker in err_str
                                for marker in (
                                    "quota",
                                    "credit",
                                    "token",
                                    "limit",
                                    "giới hạn",
                                    "hết lượt",
                                    "hết token",
                                    "hết credit",
                                    "upgrade",
                                )
                            )

                            if is_quota:
                                # Lọc ra các prompt trong chunk mà CHƯA có file video hoàn tất trên ổ đĩa
                                uncompleted_items = []
                                for p_id, p_text in chunk.prompt_items:
                                    has_file = any(
                                        f.name.startswith(f"Prompt{p_id:04d}") and f.stat().st_size > 10240
                                        for f in self.output_dir.iterdir()
                                        if f.is_file()
                                    )
                                    if not has_file:
                                        uncompleted_items.append((p_id, p_text))

                                if uncompleted_items:
                                    retry_chunk = MuseBatchChunk(
                                        chunk_index=chunk.chunk_index,
                                        prompt_items=uncompleted_items,
                                        images=chunk.images[:len(uncompleted_items)] if chunk.images else [],
                                    )
                                    with chunks_lock:
                                        shared_chunks.insert(0, retry_chunk)

                                self.log_signal.emit(
                                    f"🔄 [{worker_tag}]: Tài khoản đã hết token/credit! "
                                    f"Đã tự động hoàn trả {len(uncompleted_items)} prompt chưa hoàn thành về hàng đợi. "
                                    f"Đang tự động chuyển sang tài khoản tiếp theo trong danh sách…"
                                )
                                self.worker_status_signal.emit(worker_id, f"[{acc_label}] Hết token -> Đổi tài khoản…")
                                break
                            else:
                                with stats_lock:
                                    failed_total += n_prompts
                                    remaining_total = max(0, total_items - (completed_total + failed_total))
                                    self.stats_signal.emit(total_items, completed_total, failed_total, remaining_total)
                                    pct = int(((completed_total + failed_total) / max(1, total_items)) * 100)
                                    self.progress_signal.emit(pct, f"Lỗi nhóm {chunk_label}: {chunk_err}")
                                self.log_signal.emit(f"❌ [{worker_tag}] lỗi nhóm ({chunk_label}): {chunk_err}")

                    self.account_completed_signal.emit(current_acc.account_id, acc_completed)
                except Exception as acc_err:
                    self.log_signal.emit(f"❌ [{worker_tag}] Lỗi tài khoản: {acc_err}")
                finally:
                    if driver:
                        try:
                            driver.quit()
                        except Exception:
                            pass
                    shutil.rmtree(download_dir, ignore_errors=True)

            self.worker_status_signal.emit(worker_id, "Đã hoàn thành")

        # Khởi chạy các worker song song (1 - 5)
        threads = []
        for w_idx in range(1, effective_concurrency + 1):
            t = threading.Thread(target=worker_task, args=(w_idx,), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Nếu các tài khoản đã dừng hết nhưng vẫn còn prompt trong hàng đợi chung (do tất cả tài khoản đều hết token)
        if shared_chunks:
            with chunks_lock:
                leftover_count = sum(len(c.prompt_items) for c in shared_chunks)
                shared_chunks.clear()
            with stats_lock:
                failed_total += leftover_count
                remaining_total = max(0, total_items - (completed_total + failed_total))
                self.stats_signal.emit(total_items, completed_total, failed_total, remaining_total)
            self.log_signal.emit(
                f"⚠ Tất cả các tài khoản đã chọn đều đã hết token/credit hoặc đã dừng! "
                f"Còn {leftover_count} prompt chưa thể hoàn thành."
            )

        self.progress_signal.emit(100, f"Hoàn tất: {completed_total} thành công, {failed_total} lỗi")
        self.log_signal.emit(f"🏁 Đã hoàn tất toàn bộ batch: {completed_total}/{total_items} thành công, {failed_total} lỗi.")
        self.batch_finished_signal.emit(True, f"Đã xử lý xong: {completed_total} thành công, {failed_total} lỗi.")


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

        # F5 Muse chạy ngầm 60 phút hoàn toàn, ẩn khỏi UI theo yêu cầu của người dùng
        self.f5_spin = QSpinBox()
        self.f5_spin.setRange(0, 1440)
        self.f5_spin.setValue(int(getattr(self.settings, "muse_auto_refresh_minutes", 60) or 60))
        self.f5_spin.valueChanged.connect(self._on_f5_interval_changed)

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
        # PROMPT BOX (KHỚP ẢNH 3 & 4)
        # =========================================================================
        self.prompt_card = QGroupBox("Prompt (mỗi dòng 1 prompt)")
        prompt_vbox = QVBoxLayout(self.prompt_card)
        prompt_vbox.setSpacing(10)

        # Thanh trên cùng: Nút NHẬP FILE .TXT + 4 badge thống kê
        top_prompt_bar = QHBoxLayout()
        top_prompt_bar.setSpacing(10)

        self.btn_import_txt = QPushButton("📁 NHẬP FILE .TXT")
        self.btn_import_txt.setCursor(Qt.PointingHandCursor)
        self.btn_import_txt.setMinimumHeight(38)
        self.btn_import_txt.setStyleSheet(
            "QPushButton { "
            "background: #1d4ed8; color: #ffffff; font-weight: 800; font-size: 13px; "
            "border: none; border-radius: 6px; padding: 6px 18px; "
            "} "
            "QPushButton:hover { background: #1e40af; }"
        )
        self.btn_import_txt.clicked.connect(self._import_prompts_file)
        top_prompt_bar.addWidget(self.btn_import_txt)

        # 4 Badge Thống kê (Khớp ảnh 4):
        # [ 📋 Tổng 0 ]   [ ✔ Done 0 ]   [ ✖ Lỗi 0 ]   [ ⏳ Còn 0 ]
        self.badge_total = QLabel("📋 Tổng 0")
        self.badge_total.setStyleSheet(
            "background: #f8fafc; color: #1e293b; border: 1.5px solid #cbd5e1; border-radius: 6px; "
            "padding: 5px 12px; font-weight: 800; font-size: 13px;"
        )

        self.badge_done = QLabel("✔ Done 0")
        self.badge_done.setStyleSheet(
            "background: #ecfdf5; color: #047857; border: 1.5px solid #a7f3d0; border-radius: 6px; "
            "padding: 5px 12px; font-weight: 800; font-size: 13px;"
        )

        self.badge_error = QLabel("✖ Lỗi 0")
        self.badge_error.setStyleSheet(
            "background: #fef2f2; color: #b91c1c; border: 1.5px solid #fecaca; border-radius: 6px; "
            "padding: 5px 12px; font-weight: 800; font-size: 13px;"
        )

        self.badge_remaining = QLabel("⏳ Còn 0")
        self.badge_remaining.setStyleSheet(
            "background: #f0f9ff; color: #0369a1; border: 1.5px solid #bae6fd; border-radius: 6px; "
            "padding: 5px 12px; font-weight: 800; font-size: 13px;"
        )

        top_prompt_bar.addWidget(self.badge_total)
        top_prompt_bar.addWidget(self.badge_done)
        top_prompt_bar.addWidget(self.badge_error)
        top_prompt_bar.addWidget(self.badge_remaining)
        top_prompt_bar.addStretch()

        prompt_vbox.addLayout(top_prompt_bar)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Nhập hoặc dán các prompt tại đây (mỗi dòng 1 prompt), hoặc bấm [📁 NHẬP FILE .TXT]...")
        self.prompt.setMinimumHeight(120)
        self.prompt.setStyleSheet(
            "QPlainTextEdit { background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 6px; padding: 8px; font-size: 13px; font-family: monospace; }"
        )
        self.prompt.textChanged.connect(self._on_prompt_text_changed)
        prompt_vbox.addWidget(self.prompt)

        # Style Footer / Phong cách áp dụng chung
        style_box = QHBoxLayout()
        style_box.setSpacing(8)
        lbl_style = QLabel("🎨 Phong cách chân khung mặc định:")
        lbl_style.setStyleSheet("color: #475569; font-size: 12px; font-weight: 600;")
        self.style_suffix_edit = QLineEdit(getattr(self.settings, "muse_style_suffix", "") or DEFAULT_MUSE_STYLE_SUFFIX)
        self.style_suffix_edit.setMinimumHeight(32)
        self.style_suffix_edit.setStyleSheet(
            "QLineEdit { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 5px; padding: 4px 8px; font-size: 12px; }"
        )
        self.style_suffix_edit.editingFinished.connect(self._save_style_suffix)
        style_box.addWidget(lbl_style)
        style_box.addWidget(self.style_suffix_edit, 1)
        prompt_vbox.addLayout(style_box)

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
        self.quantity_label = QLabel("🎬 Số lượng video:")
        qty_box.addWidget(self.quantity_label)
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
        self.overall_progress.setMinimumHeight(24)
        self.overall_progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overall_progress.setTextVisible(True)
        self.overall_progress.setFormat("0 / 0 (0.0%)")
        self.overall_progress.setStyleSheet(
            "QProgressBar { background-color: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; text-align: center; font-weight: 700; font-size: 11px; color: #0f172a; height: 24px; }"
            "QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3b82f6, stop:1 #10b981); border-radius: 5px; }"
        )
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
            self.image_folder.setPlaceholderText("(Tùy chọn) Chọn ảnh mẫu để tạo Image-to-Image, hoặc để trống để tạo thuần từ Prompt...")
            self.prompt_card.setTitle("Prompt (mỗi dòng 1 prompt)")
            self.prompt.setPlaceholderText("Nhập hoặc dán các prompt tạo ảnh (mỗi dòng 1 prompt), hoặc bấm [📁 NHẬP FILE .TXT]...")
            self.duration_box_widget.setVisible(False)
            self.image_quantity_widget.setVisible(True)
            self.quantity_label.setText("🖼 Số lượng ảnh:")
            self.start_all.setText("▶ Bắt đầu tạo ảnh")
        else:
            self.btn_mode_video.setStyleSheet(ACTIVE_MODE_STYLE)
            self.btn_mode_image.setStyleSheet(INACTIVE_MODE_STYLE)
            self.source_group.setTitle("📁 Nguồn Ảnh Mẫu (Tùy chọn) & Thư Mục Xuất Video")
            self.source_label.setText("Thư mục ảnh mẫu:")
            self.image_folder.setPlaceholderText("(Tùy chọn) Chọn ảnh để tạo Video từ ảnh, hoặc để trống để tạo thuần từ Prompt...")
            self.prompt_card.setTitle("Prompt (mỗi dòng 1 prompt)")
            self.prompt.setPlaceholderText("Nhập hoặc dán các prompt tạo video (mỗi dòng 1 prompt), hoặc bấm [📁 NHẬP FILE .TXT]...")
            self.duration_box_widget.setVisible(True)
            self.image_quantity_widget.setVisible(True)
            self.quantity_label.setText("🎬 Số lượng video:")
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

    def _on_account_checked(self, account_id: str, is_active: bool, message: str, time_str: str, detected_email: str = "") -> None:
        status_key = "active" if is_active else "expired"
        self.cookie_store.update_status(account_id, status_key, message, last_checked=time_str)
        if detected_email:
            self.cookie_store.update_email(account_id, detected_email)
        self._reload_accounts_table()

    def _on_account_email_found(self, account_id: str, detected_email: str) -> None:
        if detected_email:
            self.cookie_store.update_email(account_id, detected_email)
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
    # PROMPT FILE IMPORT & BADGE UPDATES
    # =========================================================================

    def _import_prompts_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn file text chứa các prompt (.txt)",
            "",
            "Text files (*.txt);;All files (*.*)",
        )
        if not file_path:
            return

        try:
            content = ""
            for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1258", "latin-1"):
                try:
                    content = Path(file_path).read_text(encoding=encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if not content.strip():
                QMessageBox.warning(self, "File rỗng", "File .txt đã chọn không có nội dung.")
                return

            self.prompt.setPlainText(content)
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            self._update_prompt_badges(len(lines), 0, 0, len(lines))
            self.global_status.setText(f"Đã nhập {len(lines):,} prompt từ file: {Path(file_path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Lỗi đọc file", f"Không thể đọc file .txt: {exc}")

    def _on_prompt_text_changed(self) -> None:
        if self._active_runner and self._active_runner.isRunning():
            return
        lines = [line.strip() for line in self.prompt.toPlainText().splitlines() if line.strip()]
        total = len(lines)
        self._update_prompt_badges(total, 0, 0, total)
        self.overall_progress.setRange(0, max(1, total))
        self.overall_progress.setValue(0)
        self.overall_progress.setFormat(f"0 / {total:,} video (0.0%)" if total > 0 else "0 / 0 (0.0%)")

    def _update_prompt_badges(self, total: int, done: int, error: int, remaining: int) -> None:
        self.badge_total.setText(f"📋 Tổng {total:,}")
        self.badge_done.setText(f"✔ Done {done:,}")
        self.badge_error.setText(f"✖ Lỗi {error:,}")
        self.badge_remaining.setText(f"⏳ Còn {remaining:,}")

    def _on_f5_interval_changed(self, value: int) -> None:
        self.settings.muse_auto_refresh_minutes = value
        self.settings_changed.emit()

    def _show_f5_help_dialog(self) -> None:
        QMessageBox.information(
            self,
            "Tác dụng của Tự Động F5 Muse Ngầm",
            "🔄 <b>Tác dụng của tính năng F5 Muse định kỳ:</b><br><br>"
            "1. <b>Tránh treo phiên (Session Timeout):</b> Muse AI và Google session có thể hết hạn hoặc bị đơ cache sau một thời gian dài tạo liên tục hàng trăm/nghìn video.<br>"
            "2. <b>Giải phóng RAM trình duyệt:</b> Mỗi lần F5 ngầm sẽ dọn dẹp bộ nhớ đệm (garbage collection) của trang web Muse.<br>"
            "3. <b>Chạy hoàn toàn ngầm:</b> Quá trình làm mới diễn ra bên trong phiên Chrome ẩn, không làm hiện cửa sổ ra màn hình hay gián đoạn trải nghiệm của bạn.<br><br>"
            "<i>Mặc định là 60 phút. Bạn có thể tăng/giảm hoặc nhập 0 để tắt.</i>",
        )

    def _save_style_suffix(self) -> None:
        text = self.style_suffix_edit.text().strip()
        self.settings.muse_style_suffix = text
        self.settings_changed.emit()

    # =========================================================================
    # BATCH ALLOCATION & EXECUTION (GOM 5 PROMPT/LẦN, CONCURRENCY 1-5)
    # =========================================================================

    def _get_enabled_accounts(self) -> list[MuseCookieAccount]:
        return [acc for acc in self.cookie_store.all() if acc.enabled]

    def _allocate_jobs(self) -> None:
        active_accounts = self._get_enabled_accounts()
        if not active_accounts:
            QMessageBox.warning(self, "Chưa chọn tài khoản", "Hãy tick ít nhất một tài khoản Muse trong bảng để chạy.")
            return

        lines = [line.strip() for line in self.prompt.toPlainText().splitlines() if line.strip()]
        total_prompts = len(lines)
        if total_prompts == 0:
            QMessageBox.warning(self, "Thiếu Prompt", "Vui lòng nhập ít nhất 1 prompt hoặc nạp file .txt.")
            return

        num_chunks = (total_prompts + 4) // 5
        is_image = (self._current_task_mode() == "image")
        type_name = "ảnh" if is_image else "video"
        folder_text = self.image_folder.text().strip()

        imgs: list[Path] = []
        if folder_text:
            imgs = self._scan_images()

        if imgs:
            self.global_status.setText(
                f"Đã phân bổ {total_prompts:,} prompt ({len(imgs)} ảnh mẫu, chia thành {num_chunks:,} đợt gom 5 prompt/lần) "
                f"qua {len(active_accounts)} tài khoản được tick (Trung bình ~{max(1, num_chunks // len(active_accounts))} đợt/tài khoản)."
            )
        else:
            self.global_status.setText(
                f"Đã phân bổ {total_prompts:,} prompt thuần {type_name} (chia thành {num_chunks:,} đợt gom 5 prompt/lần) "
                f"qua {len(active_accounts)} tài khoản được tick (Trung bình ~{max(1, num_chunks // len(active_accounts))} đợt/tài khoản)."
            )

    def _start_batch(self) -> None:
        active_accounts = self._get_enabled_accounts()
        if not active_accounts:
            QMessageBox.warning(self, "Chưa chọn tài khoản", "Hãy tick ít nhất một tài khoản Muse trong bảng để chạy.")
            return

        lines = [line.strip() for line in self.prompt.toPlainText().splitlines() if line.strip()]
        if not lines:
            QMessageBox.warning(self, "Thiếu Prompt", "Prompt không được để trống (nhập mỗi dòng 1 prompt hoặc bấm [📁 NHẬP FILE .TXT]).")
            return

        output_dir = Path(self.output_folder.text().strip())
        if not str(output_dir).strip():
            QMessageBox.warning(self, "Thiếu Thư Mục Output", "Hãy chọn thư mục lưu kết quả đầu ra.")
            return

        is_image = (self._current_task_mode() == "image")
        folder_text = self.image_folder.text().strip()
        all_images: list[Path] = []
        if folder_text:
            all_images = self._scan_images()

        # Chia thành các chunk gom 5 prompt/lần (và 5 ảnh tương ứng nếu có)
        chunks: list[MuseBatchChunk] = []
        chunk_size = 5
        prompt_tuples = list(enumerate(lines, start=1))  # (1-based index, text)
        for i in range(0, len(prompt_tuples), chunk_size):
            chunk_items = prompt_tuples[i : i + chunk_size]
            chunk_imgs = all_images[i : i + chunk_size] if all_images else []
            chunks.append(
                MuseBatchChunk(
                    chunk_index=len(chunks) + 1,
                    prompt_items=chunk_items,
                    images=chunk_imgs,
                )
            )

        # Cài đặt Muse
        settings = MuseVideoSettings(
            task_mode="image" if is_image else "video",
            model="",
            aspect_ratio=self.aspect_ratio.currentText().strip() if self.aspect_ratio.currentText() != "Muse mặc định" else "16:9",
            duration=self.duration.currentText().strip() if self.duration.currentText() != "Muse mặc định" else "11s",
            resolution=self.resolution.currentText().strip() if self.resolution.currentText() != "Muse mặc định" else "",
            quantity=self.image_quantity.value() if is_image else 1,
        )

        concurrency = self.concurrency_spin.value()
        headless = self.headless_check.isChecked()
        auto_f5_mins = self.f5_spin.value()
        style_suffix = self.style_suffix_edit.text().strip()

        # Update UI state
        self.start_all.setEnabled(False)
        self.stop_all.setEnabled(True)
        total_prompts_count = len(lines)
        self.overall_progress.setRange(0, max(1, total_prompts_count))
        self.overall_progress.setValue(0)
        self.overall_progress.setFormat(f"0 / {total_prompts_count:,} video (0.0%)")
        self.logs_edit.clear()
        self._update_prompt_badges(len(lines), 0, 0, len(lines))

        # Start Runner Thread
        self._active_runner = MuseCookieBatchRunnerThread(
            accounts=active_accounts,
            concurrency=concurrency,
            headless=headless,
            task_mode="image" if is_image else "video",
            chunks=chunks,
            total_prompts=len(lines),
            settings=settings,
            output_dir=output_dir,
            auto_refresh_minutes=auto_f5_mins,
            style_suffix=style_suffix,
            parent=self,
        )
        self._active_runner.progress_signal.connect(self._on_batch_progress)
        self._active_runner.stats_signal.connect(self._on_batch_stats)
        self._active_runner.log_signal.connect(self._on_batch_log)
        self._active_runner.worker_status_signal.connect(self._on_worker_status)
        self._active_runner.account_completed_signal.connect(self._on_account_task_completed)
        self._active_runner.account_email_found_signal.connect(self._on_account_email_found)
        self._active_runner.batch_finished_signal.connect(self._on_batch_finished)
        self._active_runner.start()

    def _stop_batch(self) -> None:
        if self._active_runner and self._active_runner.isRunning():
            self._active_runner.stop()
            self.global_status.setText("Đang yêu cầu dừng tất cả các luồng…")
            self.stop_all.setEnabled(False)

    def _on_batch_progress(self, percent: int, text: str) -> None:
        self.global_status.setText(text)
        if percent >= 100:
            max_v = self.overall_progress.maximum()
            self.overall_progress.setValue(max_v)
            self.overall_progress.setFormat(f"{max_v:,} / {max_v:,} video (100.0%)")

    def _on_batch_stats(self, total: int, completed: int, failed: int, remaining: int) -> None:
        self._update_prompt_badges(total, completed, failed, remaining)
        self.stats_label.setText(
            f"Tổng: {total:,} • Đã hoàn thành: {completed:,} • Lỗi: {failed:,} • Còn lại: {remaining:,}"
        )
        done_count = completed + failed
        max_total = max(1, total)
        self.overall_progress.setRange(0, max_total)
        self.overall_progress.setValue(min(done_count, max_total))
        pct_float = (done_count / max_total) * 100
        self.overall_progress.setFormat(f"{done_count:,} / {total:,} video ({pct_float:.1f}%)")

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
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_progress.setFormat("0 / 0 (0.0%)")
        self._update_prompt_badges(0, 0, 0, 0)
        self.stats_label.setText("Tổng: 0 • Đã hoàn thành: 0 • Lỗi: 0 • Còn lại: 0")
        self.global_status.setText("Đã xóa dữ liệu UI.")
