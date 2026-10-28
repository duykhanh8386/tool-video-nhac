from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

from auth.movoa_auth import (
    MOVOA_START_URL,
    MovoaAccount,
    check_movoa_login_status,
    clean_chrome_profile_locks,
    create_movoa_chrome_driver,
    inject_cookies_to_driver,
)
from render.ffprobe import probe_media
from utils.paths import CACHE_DIR, safe_filename
from utils.process import hidden_process_kwargs

PROMPT_TEXTAREA_SELECTORS = (
    "#video-prompt",
    "textarea#video-prompt",
    "textarea[placeholder*='Who is in the shot' i]",
    "textarea[id*='prompt' i]",
)

GENERATE_BTN_SELECTORS = (
    "button.generate-cta",
    "button[data-slot='button'].generate-cta",
    "button.generate-cta span",
)


def ensure_text_to_video_tab(driver) -> bool:
    """Đảm bảo đang ở tab 'Text to video'."""
    script = """
        const tabs = Array.from(document.querySelectorAll('button[role="tab"], button[data-slot="tabs-trigger"]'));
        const textTab = tabs.find(t => (t.innerText || '').trim().toLowerCase().includes('text to video'));
        if (textTab) {
            const isActive = textTab.getAttribute('data-state') === 'active' ||
                             textTab.getAttribute('aria-selected') === 'true';
            if (!isActive) {
                textTab.click();
                return true;
            }
            return true;
        }
        return false;
    """
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def select_aspect_ratio(driver, ratio: str = "16:9") -> bool:
    """Chọn tỉ lệ khung hình (16:9, 9:16, 1:1)."""
    script = """
        const targetRatio = arguments[0].trim();
        const btns = Array.from(document.querySelectorAll('div.grid button, button'));
        const ratioBtn = btns.find(b => {
            const span = b.querySelector('span');
            const txt = (span ? span.innerText : b.innerText || '').trim();
            return txt === targetRatio;
        });
        if (ratioBtn) {
            ratioBtn.click();
            return true;
        }
        return false;
    """
    try:
        return bool(driver.execute_script(script, ratio))
    except Exception:
        return False


def set_prompt_text(driver, prompt: str) -> bool:
    """Điền prompt vào textarea#video-prompt."""
    script = """
        const val = arguments[0];
        const ta = document.getElementById('video-prompt') ||
                   document.querySelector('textarea#video-prompt') ||
                   document.querySelector('textarea[placeholder*="Who is in the shot" i]');
        if (!ta) return false;
        ta.focus();
        ta.value = val;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    """
    try:
        if driver.execute_script(script, prompt):
            return True
    except Exception:
        pass

    try:
        from selenium.webdriver.common.by import By
        for sel in PROMPT_TEXTAREA_SELECTORS:
            ta = driver.find_element(By.CSS_SELECTOR, sel)
            if ta and ta.is_displayed():
                ta.click()
                ta.clear()
                ta.send_keys(prompt)
                return True
    except Exception:
        pass
    return False


def get_movoa_current_video_src(driver) -> str:
    """Lấy src hoặc poster của thẻ video hiện tại trên Movoa (nếu có)."""
    script = """
        const v = document.querySelector('video');
        if (v) {
            return v.currentSrc || v.src || (v.querySelector('source') ? v.querySelector('source').src : '') || v.poster || '';
        }
        return '';
    """
    try:
        res = driver.execute_script(script)
        return str(res or "").strip()
    except Exception:
        return ""


def is_movoa_rendering(driver) -> bool:
    """Kiểm tra xem Movoa có đang trong trạng thái render clip hay không."""
    script = """
        const bodyText = document.body ? (document.body.innerText || '') : '';
        const lower = bodyText.toLowerCase();

        // 1. Kiểm tra text thông báo đang render trên card 'Your clips'
        if (lower.includes('the model is rendering your clip') ||
            lower.includes('clips rendering') ||
            lower.includes('rendering your clip') ||
            lower.includes('rendering...')) {
            return true;
        }

        // 2. Kiểm tra nếu nút chuyển sang trạng thái Queue / Spinner
        const allBtns = Array.from(document.querySelectorAll('button'));
        const queueBtn = allBtns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            return (t.includes('queue my next take') || t.includes('queue next take')) && b.offsetParent !== null;
        });
        if (queueBtn) return true;

        const genBtn = document.querySelector('button.generate-cta');
        if (genBtn && genBtn.offsetParent !== null) {
            const t = (genBtn.innerText || '').toLowerCase();
            if (t.includes('queue') || genBtn.disabled || genBtn.classList.contains('loading')) {
                return true;
            }
        }

        // 3. Kiểm tra spinner quay tròn
        const spinners = document.querySelectorAll('svg.animate-spin, .animate-spin, [class*="animate-spin"]');
        for (const s of spinners) {
            if (s.offsetParent !== null) {
                const parentText = (s.closest('div, section')?.innerText || '').toLowerCase();
                if (parentText.includes('rendering') || parentText.includes('queue')) {
                    return true;
                }
            }
        }

        return false;
    """
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def check_movoa_render_error(driver) -> tuple[bool, str]:
    """Kiểm tra xem Movoa có hiển thị lỗi kiểm duyệt, hết quota, hoặc render fail không."""
    script = """
        const bodyText = document.body ? (document.body.innerText || '') : '';
        const lower = bodyText.toLowerCase();

        if (lower.includes('violat') || lower.includes('moderation') || lower.includes('safety system')) {
            return [true, 'Prompt bị hệ thống kiểm duyệt an toàn của Movoa chặn.'];
        }

        if (lower.includes('failed to render') || lower.includes('rendering failed') || lower.includes('generation failed')) {
            return [true, 'Movoa báo lỗi render video thất bại.'];
        }

        if (lower.includes('rate limit') || lower.includes('too many requests') || lower.includes('daily cap') || lower.includes('reached your limit')) {
            return [true, 'Tài khoản chạm giới hạn tạo video của Movoa.'];
        }

        const toasts = Array.from(document.querySelectorAll('[role="alert"], [data-slot="toast"], .toast, .alert-error'));
        for (const t of toasts) {
            const txt = (t.innerText || '').trim();
            if (txt && (txt.toLowerCase().includes('error') || txt.toLowerCase().includes('failed') || txt.toLowerCase().includes('limit'))) {
                return [true, txt];
            }
        }

        return [false, ''];
    """
    try:
        res = driver.execute_script(script)
        if isinstance(res, list) and len(res) == 2:
            return bool(res[0]), str(res[1])
    except Exception:
        pass
    return False, ""


def click_generate_clip(driver) -> bool:
    """Bấm nút 'Make my clip' hoặc 'Make it free' (chỉ bấm khi sẵn sàng, KHÔNG bấm nút Queue)."""
    script = """
        const allBtns = Array.from(document.querySelectorAll('button'));

        // 1. Ưu tiên tìm nút có chữ 'Make my clip' hoặc 'Make it free'
        let btn = allBtns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            return (t.includes('make my clip') || t.includes('make it free')) && b.offsetParent !== null;
        });

        // 2. Fallback: button.generate-cta nhưng loại trừ nút Queue
        if (!btn) {
            const genBtn = document.querySelector('button.generate-cta');
            if (genBtn && genBtn.offsetParent !== null) {
                const t = (genBtn.innerText || '').toLowerCase();
                if (!t.includes('queue')) {
                    btn = genBtn;
                }
            }
        }

        if (btn) {
            btn.scrollIntoView({ block: 'center' });
            btn.click();
            return true;
        }
        return false;
    """
    try:
        if driver.execute_script(script):
            return True
    except Exception:
        pass

    try:
        from selenium.webdriver.common.by import By
        for sel in GENERATE_BTN_SELECTORS:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn and btn.is_displayed():
                txt = (btn.text or "").lower()
                if "queue" not in txt:
                    btn.click()
                    return True
    except Exception:
        pass
    return False


def wait_for_movoa_clip_ready(
    driver,
    initial_video_src: str = "",
    timeout: float = 360.0,
    poll_interval: float = 2.5,
    cancel_check: Callable[[], bool] | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> bool:
    """Chờ clip video tạo xong trên Movoa AI:
    1. Đợi Movoa tiếp nhận và hiển thị trạng thái đang render ('The model is rendering your clip...').
    2. Chờ cho tới khi quá trình render kết thúc hoàn toàn (thẻ rendering biến mất).
    3. Xác nhận video mới và nút 'Download my clip' đã sẵn sàng tải.
    """
    start_time = time.monotonic()
    cancel_check = cancel_check or (lambda: False)
    log_cb = log_cb or (lambda _m: None)

    # Bước 1: Đợi tối đa 15s để Movoa xác nhận render đã bắt đầu
    render_started = False
    log_cb("⏳ Đang gửi yêu cầu và chờ Movoa kích hoạt render...")

    for _ in range(15):
        if cancel_check():
            return False

        err_found, err_msg = check_movoa_render_error(driver)
        if err_found:
            raise RuntimeError(f"Movoa báo lỗi: {err_msg}")

        if is_movoa_rendering(driver):
            render_started = True
            log_cb("🚀 Movoa đã tiếp nhận! Đang trong hàng đợi render video...")
            break

        current_src = get_movoa_current_video_src(driver)
        if initial_video_src and current_src and current_src != initial_video_src:
            log_cb("✔ Clip video mới đã xuất hiện ngay!")
            return True

        time.sleep(1.0)

    # Bước 2: Vòng lặp chờ render hoàn tất
    last_log_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        if cancel_check():
            return False

        err_found, err_msg = check_movoa_render_error(driver)
        if err_found:
            raise RuntimeError(f"Movoa báo lỗi: {err_msg}")

        rendering = is_movoa_rendering(driver)
        elapsed = int(time.monotonic() - start_time)

        if rendering:
            # Vẫn đang render: KHÔNG ĐƯỢC return ready, bất kể có nút download cũ hay không!
            if time.monotonic() - last_log_time >= 10.0:
                log_cb(f"⏳ Movoa đang render video... (đã render {elapsed}s)")
                last_log_time = time.monotonic()
            time.sleep(poll_interval)
            continue

        # Nếu không còn rendering nữa (render_started đã qua hoặc render vừa xong)
        if render_started:
            log_cb(f"✔ Quá trình render kết thúc sau {elapsed}s! Đang chuẩn bị clip thành phẩm...")
            time.sleep(2.0)
            break
        else:
            # Nếu ban đầu không bắt được rendering, kiểm tra xem video src có thay đổi không
            current_src = get_movoa_current_video_src(driver)
            if not initial_video_src or (current_src and current_src != initial_video_src):
                break
            time.sleep(poll_interval)

    # Bước 3: Đợi nút 'Download my clip' xuất hiện và sẵn sàng
    confirm_start = time.monotonic()
    while time.monotonic() - confirm_start < 25.0:
        if cancel_check():
            return False

        check_script = """
            const allBtns = Array.from(document.querySelectorAll('button'));
            const dlBtn = allBtns.find(b => {
                const t = (b.innerText || '').toLowerCase();
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                return (t.includes('download my clip') || aria.includes('download my clip') || t.includes('download clip')) && b.offsetParent !== null;
            });
            return Boolean(dlBtn);
        """
        try:
            has_dl_btn = bool(driver.execute_script(check_script))
            if has_dl_btn:
                total_elapsed = int(time.monotonic() - start_time)
                log_cb(f"✔ Clip video mới đã hoàn tất và sẵn sàng tải (sau {total_elapsed}s)!")
                return True
        except Exception:
            pass

        time.sleep(1.5)

    log_cb("⚠ Đã hết thời gian chờ nút download. Sẽ thử quét và tải file...")
    return True


def download_movoa_clip(
    driver,
    download_dir: Path,
    timeout: float = 120.0,
    cancel_check: Callable[[], bool] | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> Path:
    """Bấm nút 'Download my clip' và chờ tải file MP4 về hoàn tất."""
    cancel_check = cancel_check or (lambda: False)
    log_cb = log_cb or (lambda _m: None)
    download_dir = Path(download_dir).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    # Dọn dẹp các file cũ trong thư mục tạm trước khi tải mới để tránh nhầm lẫn
    for old_file in download_dir.glob("*"):
        if old_file.is_file():
            try:
                old_file.unlink()
            except Exception:
                pass

    existing_files = set(download_dir.glob("*"))

    click_download_script = """
        const allBtns = Array.from(document.querySelectorAll('button'));
        const dlBtn = allBtns.find(b => {
            const t = (b.innerText || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return (t.includes('download my clip') || aria.includes('download my clip') || t.includes('download clip')) && b.offsetParent !== null;
        });
        if (dlBtn) {
            dlBtn.scrollIntoView({ block: 'center' });
            dlBtn.click();
            return true;
        }

        // Fallback: Tìm thẻ video trong Your clips để lấy src trực tiếp
        const v = document.querySelector('video[src], video source[src]');
        if (v) {
            return v.src || v.getAttribute('src');
        }
        return false;
    """

    clicked = False
    start_wait = time.monotonic()
    while time.monotonic() - start_wait < 30.0:
        if cancel_check():
            raise RuntimeError("Người dùng đã dừng tác vụ.")
        try:
            res = driver.execute_script(click_download_script)
            if res is True:
                clicked = True
                log_cb("✔ Đã kích hoạt nút 'Download my clip'!")
                break
            elif isinstance(res, str) and res.startswith("http"):
                log_cb("✔ Tìm thấy đường dẫn video trực tiếp, đang tải về...")
                import urllib.request
                target_file = download_dir / f"movoa_clip_{int(time.time())}.mp4"
                urllib.request.urlretrieve(res, str(target_file))
                return target_file
        except Exception:
            pass
        time.sleep(2.0)

    if not clicked:
        raise RuntimeError("Không tìm thấy nút 'Download my clip' trên giao diện Movoa.")

    log_cb("📥 Đang chờ Chrome hoàn tất tải video về thư mục tạm...")
    download_start = time.monotonic()
    while time.monotonic() - download_start < timeout:
        if cancel_check():
            raise RuntimeError("Người dùng đã dừng tác vụ.")

        current_files = set(download_dir.glob("*"))
        new_files = current_files - existing_files

        for f in new_files:
            if f.suffix in (".crdownload", ".tmp"):
                continue
            if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv"):
                try:
                    size1 = f.stat().st_size
                    time.sleep(1.2)
                    size2 = f.stat().st_size
                    if size1 > 1024 and size1 == size2:
                        log_cb(f"✔ Đã tải về: {f.name} ({round(size1 / (1024 * 1024), 2)} MB)")
                        return f
                except Exception:
                    pass

        time.sleep(1.0)

    raise TimeoutError("Hết thời gian chờ trình duyệt tải file video Movoa.")


def trim_video_end(
    input_video: Path,
    output_video: Path,
    trim_seconds: float = 1.5,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    log_cb: Callable[[str], None] | None = None,
) -> Path:
    """Cắt và xóa bỏ số giây cuối (mặc định 1.5s) bằng FFmpeg."""
    log_cb = log_cb or (lambda _m: None)
    input_path = Path(input_video).resolve()
    output_path = Path(output_video).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy file video: {input_path}")

    info = probe_media(str(input_path), ffprobe_path)
    orig_duration = float(info.duration)
    log_cb(f"📊 Thời lượng gốc: {orig_duration:.2f}s")

    if orig_duration <= trim_seconds:
        log_cb(f"⚠ Video ngắn hơn {trim_seconds}s, giữ nguyên tối thiểu 0.5s.")
        target_duration = 0.5
    else:
        target_duration = max(0.5, orig_duration - trim_seconds)

    log_cb(f"✂ Đang cắt bỏ {trim_seconds:.2f}s cuối -> Thời lượng mới: {target_duration:.2f}s...")

    command = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-ss", "0",
        "-i", str(input_path),
        "-t", f"{target_duration:.3f}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_process_kwargs(),
    )

    if result.returncode != 0:
        fallback_cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-y",
            "-ss", "0",
            "-i", str(input_path),
            "-t", f"{target_duration:.3f}",
            "-c", "copy",
            str(output_path),
        ]
        fb_result = subprocess.run(
            fallback_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_process_kwargs(),
        )
        if fb_result.returncode != 0 or not output_path.exists():
            raise RuntimeError(f"FFmpeg cắt video thất bại: {result.stderr or fb_result.stderr}")

    log_cb(f"✔ Đã cắt và lưu video: {output_path.name}")
    return output_path


class MovoaVideoBatchRunnerThread(QThread):
    """Luồng chạy hàng loạt tạo video và tải về từ Movoa AI (Text-to-Video)."""

    log_signal = Signal(str)
    progress_signal = Signal(int, str)
    stats_signal = Signal(int, int, int, int)  # total, completed, failed, remaining
    worker_status_signal = Signal(int, str)
    account_status_signal = Signal(str, str, str)  # account_id, status, msg
    video_completed_signal = Signal(str, str)  # prompt, output_path
    batch_finished_signal = Signal(bool, str)

    def __init__(
        self,
        accounts: list[MovoaAccount],
        prompts: list[str],
        output_dir: Path,
        *,
        aspect_ratio: str = "16:9",
        concurrency: int = 1,
        headless: bool = False,
        trim_seconds: float = 1.5,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.accounts = [acc for acc in accounts if acc.enabled]
        self.prompts = [p.strip() for p in prompts if p.strip()]
        self.output_dir = Path(output_dir).resolve()
        self.aspect_ratio = aspect_ratio
        self.concurrency = max(1, min(5, concurrency))
        self.headless = headless
        self.trim_seconds = float(trim_seconds)
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

        self._stopped = False
        self._paused = False

    def stop(self) -> None:
        self._stopped = True

    def toggle_pause(self) -> bool:
        self._paused = not self._paused
        return self._paused

    def run(self) -> None:
        total_items = len(self.prompts)
        if not self.accounts:
            self.log_signal.emit("❌ Chưa có tài khoản Movoa nào được chọn để chạy!")
            self.batch_finished_signal.emit(False, "Chưa chọn tài khoản.")
            return

        if not self.prompts:
            self.log_signal.emit("❌ Danh sách prompt trống!")
            self.batch_finished_signal.emit(False, "Danh sách prompt trống.")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_signal.emit(
            f"🚀 Bắt đầu Farm Video Movoa AI: {total_items} prompt | "
            f"{len(self.accounts)} tài khoản | Cùng lúc: {self.concurrency} | "
            f"Tỉ lệ: {self.aspect_ratio} | Tự cắt xóa {self.trim_seconds}s cuối."
        )

        prompt_queue = list(enumerate(self.prompts, start=1))
        queue_lock = threading.Lock()
        stats_lock = threading.Lock()

        completed_count = 0
        failed_count = 0

        self.stats_signal.emit(total_items, 0, 0, total_items)
        self.progress_signal.emit(0, f"Đang bắt đầu... (0/{total_items})")

        effective_concurrency = min(self.concurrency, len(self.accounts))

        def worker_task(worker_id: int, account: MovoaAccount) -> None:
            nonlocal completed_count, failed_count
            worker_tag = f"Luồng {worker_id} [{account.name}]"
            self.worker_status_signal.emit(worker_id, "Đang mở Chrome...")
            self.log_signal.emit(f"⚙ [{worker_tag}]: Khởi tạo Chrome Profile ({account.profile_dir})")

            profile_dir = Path(account.profile_dir).resolve()
            temp_dl_dir = Path(tempfile.mkdtemp(prefix=f"movoa_w{worker_id}_", dir=CACHE_DIR))

            # Nếu tài khoản chạy bằng cookies, dùng profile riêng biệt trong temp_dl_dir
            # để cookies chạy hoàn toàn cô lập, không bao giờ đụng độ lock với trình duyệt ngoài
            if account.cookies:
                worker_profile = temp_dl_dir / "cookie_profile"
                worker_profile.mkdir(parents=True, exist_ok=True)
            else:
                worker_profile = profile_dir
                clean_chrome_profile_locks(worker_profile)

            driver = None
            try:
                driver = create_movoa_chrome_driver(
                    worker_profile, download_dir=temp_dl_dir, headless=self.headless
                )
                driver.set_page_load_timeout(45)

                if account.cookies:
                    self.worker_status_signal.emit(worker_id, "Nạp cookies...")
                    inject_cookies_to_driver(driver, account.cookies, target_url=MOVOA_START_URL)
                else:
                    self.worker_status_signal.emit(worker_id, "Đang vào movoa.ai...")
                    driver.get(MOVOA_START_URL)
                    time.sleep(3.0)

                # Kiểm tra đăng nhập
                is_logged_in, user_label = check_movoa_login_status(driver)
                if is_logged_in:
                    self.account_status_signal.emit(account.account_id, "active", "🟢 Đã đăng nhập")
                    self.log_signal.emit(f"✔ [{worker_tag}]: Đã đăng nhập ({user_label})")
                else:
                    self.account_status_signal.emit(account.account_id, "need_login", "🟡 Chưa đăng nhập")
                    self.log_signal.emit(
                        f"⚠ [{worker_tag}]: Chưa đăng nhập Google trên Movoa. "
                        f"Hãy mở Chrome đăng nhập tài khoản này trước để không bị giới hạn!"
                    )

                while not self._stopped:
                    while self._paused and not self._stopped:
                        self.worker_status_signal.emit(worker_id, "Tạm dừng")
                        time.sleep(1.0)

                    item = None
                    with queue_lock:
                        if prompt_queue:
                            item = prompt_queue.pop(0)

                    if not item:
                        self.worker_status_signal.emit(worker_id, "Sẵn sàng (Hết việc)")
                        break

                    p_idx, prompt_text = item
                    task_label = f"#{p_idx}/{total_items}"

                    self.worker_status_signal.emit(worker_id, f"Đang tạo {task_label}...")
                    self.log_signal.emit(f"📝 [{worker_tag}] [{task_label}]: Gửi prompt: '{prompt_text[:60]}...'")

                    try:
                        if "movoa.ai/video/text-to-video" not in driver.current_url.lower():
                            driver.get(MOVOA_START_URL)
                            time.sleep(3.0)

                        ensure_text_to_video_tab(driver)
                        time.sleep(0.5)

                        # 1. Nếu có video trước đó đang render dở, chờ nó render xong hoàn toàn
                        if is_movoa_rendering(driver):
                            self.log_signal.emit(
                                f"⏳ [{worker_tag}] [{task_label}]: Phát hiện clip trước đó đang render dở dang. "
                                f"Đang chờ hoàn tất trước khi gửi prompt mới..."
                            )
                            wait_for_movoa_clip_ready(
                                driver,
                                timeout=360.0,
                                cancel_check=lambda: self._stopped,
                                log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                            )
                            time.sleep(2.0)

                        # 2. Ghi nhận video src hiện tại để đối chiếu với video mới
                        initial_src = get_movoa_current_video_src(driver)

                        # 3. Chọn tỉ lệ khung hình
                        select_aspect_ratio(driver, self.aspect_ratio)
                        time.sleep(0.5)

                        # 4. Nhập prompt
                        if not set_prompt_text(driver, prompt_text):
                            raise RuntimeError("Không thể điền prompt vào ô #video-prompt.")

                        time.sleep(1.0)

                        # 5. Bấm nút Make my clip (tuyệt đối không bấm nhầm nút Queue)
                        if not click_generate_clip(driver):
                            raise RuntimeError("Không thể click nút 'Make my clip' / 'Make it free'.")

                        self.log_signal.emit(f"🚀 [{worker_tag}] [{task_label}]: Đã bấm nút Make my clip! Đang chờ tạo video...")

                        # 6. Chờ video MỚI tạo xong hoàn tất (đợi render xong, không tải video cũ)
                        gen_ok = wait_for_movoa_clip_ready(
                            driver,
                            initial_video_src=initial_src,
                            timeout=420.0,
                            cancel_check=lambda: self._stopped,
                            log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                        )
                        if not gen_ok:
                            raise RuntimeError("Tạo clip bị hủy hoặc quá thời gian chờ render.")

                        # 7. Tải video MỚI vừa render xong qua nút 'Download my clip'
                        self.worker_status_signal.emit(worker_id, f"Đang tải {task_label}...")
                        downloaded_file = download_movoa_clip(
                            driver,
                            temp_dl_dir,
                            timeout=120.0,
                            cancel_check=lambda: self._stopped,
                            log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                        )

                        # 8. Cắt xóa 1.5s cuối (hoặc số giây cấu hình)
                        self.worker_status_signal.emit(worker_id, f"Cắt {self.trim_seconds}s cuối...")
                        stem_name = safe_filename(f"movoa_{p_idx:03d}_{prompt_text[:30]}", default_prefix="movoa")
                        final_output = self.output_dir / f"{Path(stem_name).stem}_cut.mp4"

                        trimmed_file = trim_video_end(
                            downloaded_file,
                            final_output,
                            trim_seconds=self.trim_seconds,
                            ffmpeg_path=self.ffmpeg_path,
                            ffprobe_path=self.ffprobe_path,
                            log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                        )

                        # Nghỉ 2.5s để Movoa cập nhật giao diện trước khi tiếp tục
                        time.sleep(2.5)

                        with stats_lock:
                            completed_count += 1
                            rem = max(0, total_items - (completed_count + failed_count))
                            self.stats_signal.emit(total_items, completed_count, failed_count, rem)
                            pct = int((completed_count / total_items) * 100)
                            self.progress_signal.emit(pct, f"Đã hoàn thành {completed_count}/{total_items}")

                        self.video_completed_signal.emit(prompt_text, str(trimmed_file))
                        self.log_signal.emit(f"🎉 [{worker_tag}] [{task_label}]: Thành công -> {trimmed_file.name}")

                        try:
                            downloaded_file.unlink(missing_ok=True)
                        except Exception:
                            pass

                    except Exception as exc:
                        with stats_lock:
                            failed_count += 1
                            rem = max(0, total_items - (completed_count + failed_count))
                            self.stats_signal.emit(total_items, completed_count, failed_count, rem)

                        self.log_signal.emit(f"❌ [{worker_tag}] [{task_label}] Lỗi: {exc}")
                        time.sleep(2.0)

            except Exception as e:
                self.log_signal.emit(f"❌ [{worker_tag}] Lỗi trình duyệt hoặc phiên: {e}")
            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                shutil.rmtree(temp_dl_dir, ignore_errors=True)
                self.worker_status_signal.emit(worker_id, "Đã đóng")

        threads = []
        for w_idx in range(effective_concurrency):
            acc = self.accounts[w_idx % len(self.accounts)]
            t = threading.Thread(target=worker_task, args=(w_idx + 1, acc), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        if self._stopped:
            self.log_signal.emit(f"🛑 Đã dừng tiến trình. Hoàn tất: {completed_count}/{total_items}, Lỗi: {failed_count}")
            self.batch_finished_signal.emit(False, "Đã dừng tiến trình.")
        else:
            self.progress_signal.emit(100, f"Xong toàn bộ: {completed_count}/{total_items} video.")
            self.log_signal.emit(f"🏁 ĐÃ HOÀN TẤT TOÀN BỘ BATCH MOVOA: {completed_count} thành công, {failed_count} lỗi.")
            self.batch_finished_signal.emit(True, f"Thành công {completed_count}/{total_items} video.")
