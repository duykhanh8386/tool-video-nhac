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

from auth.zsky_auth import (
    ZSKY_MY_CREATIONS_URL,
    ZSKY_START_URL,
    ZskyAccount,
    check_zsky_login_status,
    clean_chrome_profile_locks,
    create_zsky_chrome_driver,
    inject_cookies_to_driver,
)
from render.ffprobe import probe_media
from utils.paths import CACHE_DIR, safe_filename
from utils.process import hidden_process_kwargs

PROMPT_INPUT_SELECTORS = (
    "#promptInput",
    "input#promptInput",
    "input[aria-label*='prompt' i]",
    "input[placeholder*='Describe the scene' i]",
)

AUDIO_PROMPT_INPUT_SELECTORS = (
    "#audioPromptInput",
    "input#audioPromptInput",
    "input.audio-prompt-input",
    "input[placeholder*='Describe sound' i]",
)

GENERATE_BTN_SELECTORS = (
    "#generateBtn",
    "button#generateBtn",
    "button.prompt-generate",
    "button[aria-label='Generate']",
)

DOWNLOAD_BTN_SELECTORS = (
    "button.card-action-btn[title='Download']",
    "button[title='Download']",
    "button.card-action-btn[title*='download' i]",
    "button[title*='download' i]",
    "button[aria-label*='download' i]",
)


def ensure_video_mode(driver) -> bool:
    """Đảm bảo Zsky đang chọn chế độ Video (Image-to-Video / Text-to-Video)."""
    script = """
        const btns = Array.from(document.querySelectorAll(
            '.prompt-toolbar button, .prompt-toolbar div[role="button"], button'
        ));
        const videoBtn = btns.find(b => {
            const t = (b.innerText || '').trim().toLowerCase();
            return t === 'video' || t.includes('video');
        });
        if (videoBtn) {
            const isSelected = videoBtn.classList.contains('active') ||
                               videoBtn.classList.contains('selected') ||
                               videoBtn.getAttribute('aria-pressed') === 'true';
            if (!isSelected) {
                videoBtn.click();
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


def set_input_value_native_and_js(driver, selector: str, value: str) -> bool:
    """Điền text vào input đồng thời kích hoạt sự kiện input, change, keydown để framework nhận diện."""
    script = """
        const sel = arguments[0];
        const val = arguments[1];
        const el = document.querySelector(sel);
        if (!el) return false;
        el.focus();
        el.value = val;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    """
    try:
        res = driver.execute_script(script, selector, value)
        if res:
            return True
    except Exception:
        pass

    try:
        from selenium.webdriver.common.by import By
        el = driver.find_element(By.CSS_SELECTOR, selector)
        if el and el.is_displayed():
            el.click()
            el.clear()
            el.send_keys(value)
            return True
    except Exception:
        pass
    return False


def submit_zsky_prompt(driver, prompt: str, audio_prompt: str = "") -> bool:
    """Nhập prompt vào input#promptInput và bấm nút generateBtn."""
    ensure_video_mode(driver)
    time.sleep(0.5)

    # 1. Điền Prompt chính
    prompt_filled = False
    for sel in PROMPT_INPUT_SELECTORS:
        if set_input_value_native_and_js(driver, sel, prompt):
            prompt_filled = True
            break
    if not prompt_filled:
        raise RuntimeError("Không tìm thấy ô nhập prompt (#promptInput) trên Zsky.ai.")

    # 2. Điền Audio prompt (nếu có)
    if audio_prompt.strip():
        for sel in AUDIO_PROMPT_INPUT_SELECTORS:
            if set_input_value_native_and_js(driver, sel, audio_prompt.strip()):
                break

    time.sleep(0.5)

    # 3. Kích hoạt nút Generate
    click_script = """
        const btn = document.getElementById('generateBtn') ||
                    document.querySelector('button.prompt-generate') ||
                    document.querySelector('button[aria-label="Generate"]');
        if (btn) {
            btn.scrollIntoView({ block: 'center' });
            btn.click();
            return true;
        }
        return false;
    """
    clicked = False
    try:
        clicked = bool(driver.execute_script(click_script))
    except Exception:
        pass

    if not clicked:
        try:
            from selenium.webdriver.common.by import By
            for sel in GENERATE_BTN_SELECTORS:
                btn = driver.find_element(By.CSS_SELECTOR, sel)
                if btn and btn.is_displayed():
                    btn.click()
                    clicked = True
                    break
        except Exception:
            pass

    if not clicked:
        raise RuntimeError("Không thể click nút Generate (#generateBtn).")

    return True


def wait_for_video_generation(
    driver,
    timeout: float = 240.0,
    poll_interval: float = 2.0,
    cancel_check: Callable[[], bool] | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> bool:
    """Chờ Zsky hoàn thành sinh video sau khi đã bấm Generate."""
    start_time = time.monotonic()
    cancel_check = cancel_check or (lambda: False)
    log_cb = log_cb or (lambda _m: None)

    # Đợi 3 giây đầu để server kích hoạt tiến trình tạo video
    time.sleep(3.0)

    log_cb("⏳ Đang chờ Zsky xử lý và tạo video...")
    while time.monotonic() - start_time < timeout:
        if cancel_check():
            return False

        # Kiểm tra xem Zsky đã hiển thị preview video hoặc hoàn thành
        status_script = """
            // 1. Kiểm tra nếu có video tag xuất hiện với thời lượng > 0 hoặc src hợp lệ
            const videos = Array.from(document.querySelectorAll('video'));
            const activeVideo = videos.find(v => v.src && v.src.length > 5 || v.querySelector('source'));
            if (activeVideo) return 'video_ready';

            // 2. Kiểm tra nếu có màn hình editor "Close the editor and return to your creations"
            const bodyText = (document.body.innerText || '').toLowerCase();
            if (bodyText.includes('close the editor and return to your creations') || bodyText.includes('my creations')) {
                // Kiểm tra xem nút generate có đang loading không
                const genBtn = document.getElementById('generateBtn');
                const isGenerating = genBtn && (
                    genBtn.classList.contains('generating') ||
                    genBtn.classList.contains('loading') ||
                    genBtn.disabled
                );
                if (!isGenerating) return 'editor_ready';
            }

            // 3. Kiểm tra nút generate đã trở lại trạng thái sẵn sàng (không còn class loading/generating)
            const btn = document.getElementById('generateBtn');
            if (btn) {
                const isBusy = btn.disabled ||
                               btn.classList.contains('generating') ||
                               btn.classList.contains('loading') ||
                               Boolean(document.querySelector('.cloud-bolt, .cloud-rain'));
                // Nếu sau 15 giây mà nút không còn busy -> xem như đã sinh xong
                return isBusy ? 'generating' : 'idle_ready';
            }
            return 'waiting';
        """
        try:
            status = driver.execute_script(status_script)
            elapsed = int(time.monotonic() - start_time)
            if status in ("video_ready", "editor_ready"):
                log_cb(f"✔ Zsky đã hoàn thành tạo video (sau {elapsed}s)!")
                return True
            if status == "idle_ready" and elapsed >= 15:
                log_cb(f"✔ Tiến trình tạo video hoàn tất (sau {elapsed}s)!")
                return True
        except Exception:
            pass

        time.sleep(poll_interval)

    log_cb("⚠ Hết thời gian chờ tạo video trên giao diện create. Sẽ kiểm tra trang My Creations...")
    return True


def download_video_from_my_creations(
    driver,
    download_dir: Path,
    timeout: float = 120.0,
    cancel_check: Callable[[], bool] | None = None,
    log_cb: Callable[[str], None] | None = None,
) -> Path:
    """Truy cập zsky.ai/my-creations, tìm video mới nhất, bấm nút Download và chờ file tải về xong."""
    cancel_check = cancel_check or (lambda: False)
    log_cb = log_cb or (lambda _m: None)
    download_dir = Path(download_dir).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    # Ghi nhận danh sách file hiện có trước khi bấm tải
    existing_files = set(download_dir.glob("*"))

    log_cb("🌐 Đang chuyển sang trang My Creations (https://zsky.ai/my-creations)...")
    driver.get(ZSKY_MY_CREATIONS_URL)
    time.sleep(4.0)

    # Chờ danh sách video tải lên
    find_and_click_download_script = """
        // 1. Thử click tab VIDEOS nếu có
        const tabs = Array.from(document.querySelectorAll('button, a, div[role="tab"]'));
        const videoTab = tabs.find(t => (t.innerText || '').trim().toUpperCase().startsWith('VIDEOS'));
        if (videoTab) {
            videoTab.click();
        }

        // 2. Tìm nút Download: button.card-action-btn[title="Download"] hoặc button[title="Download"]
        const dlBtns = Array.from(document.querySelectorAll(
            'button.card-action-btn[title="Download"], button[title="Download"], button[title*="Download" i], button.card-action-btn'
        ));

        // Ưu tiên nút có icon download hoặc title="Download"
        const targetBtn = dlBtns.find(b => {
            const title = (b.getAttribute('title') || '').toLowerCase();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            return title.includes('download') || aria.includes('download') || Boolean(b.querySelector('svg path[d*="M12 5v14"]'));
        }) || dlBtns[0];

        if (targetBtn) {
            // Hover nhẹ lên parent card để overlay hiển thị nếu bị ẩn
            const card = targetBtn.closest('.card, .plate, div') || targetBtn.parentElement;
            if (card) {
                card.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                card.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
            }
            targetBtn.click();
            return true;
        }

        // 3. Fallback: Nếu có thẻ <video src="..."> trực tiếp thì trả về link
        const firstVideo = document.querySelector('video[src], video source[src]');
        if (firstVideo) {
            return firstVideo.src || firstVideo.getAttribute('src');
        }

        return false;
    """

    clicked = False
    start_time = time.monotonic()
    while time.monotonic() - start_time < 30.0:
        if cancel_check():
            raise RuntimeError("Người dùng đã hủy tác vụ tải video.")
        try:
            res = driver.execute_script(find_and_click_download_script)
            if res is True:
                clicked = True
                log_cb("✔ Đã bấm nút Download (<button class='card-action-btn' title='Download'>)!")
                break
            elif isinstance(res, str) and res.startswith("http"):
                log_cb("✔ Tìm thấy đường dẫn video trực tiếp, đang tải về...")
                # Tải file trực tiếp qua URL
                target_file = download_dir / f"zsky_video_{int(time.time())}.mp4"
                import urllib.request
                urllib.request.urlretrieve(res, str(target_file))
                return target_file
        except Exception:
            pass
        time.sleep(2.0)

    if not clicked:
        raise RuntimeError("Không tìm thấy nút Download trên trang My Creations (https://zsky.ai/my-creations).")

    # Chờ file tải về hoàn tất trong download_dir
    log_cb("📥 Đang chờ trình duyệt tải file video về thư mục tạm...")
    download_start = time.monotonic()
    while time.monotonic() - download_start < timeout:
        if cancel_check():
            raise RuntimeError("Người dùng đã dừng tác vụ.")

        current_files = set(download_dir.glob("*"))
        new_files = current_files - existing_files

        # Kiểm tra file mới
        for f in new_files:
            # Bỏ qua file tạm của Chrome
            if f.suffix in (".crdownload", ".tmp"):
                continue
            if f.suffix.lower() in (".mp4", ".webm", ".mov", ".mkv"):
                # Đảm bảo dung lượng file ổn định (đã tải xong hoàn toàn)
                try:
                    size1 = f.stat().st_size
                    time.sleep(1.2)
                    size2 = f.stat().st_size
                    if size1 > 1024 and size1 == size2:
                        log_cb(f"✔ Đã tải xong video: {f.name} ({round(size1 / (1024 * 1024), 2)} MB)")
                        return f
                except Exception:
                    pass

        time.sleep(1.0)

    raise TimeoutError("Hết thời gian chờ tải file video từ trình duyệt Chrome.")


def trim_video_end(
    input_video: Path,
    output_video: Path,
    trim_seconds: float = 1.5,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    log_cb: Callable[[str], None] | None = None,
) -> Path:
    """Cắt và xóa bỏ số giây cuối (mặc định 1.5s) của video bằng FFmpeg theo yêu cầu."""
    log_cb = log_cb or (lambda _m: None)
    input_path = Path(input_video).resolve()
    output_path = Path(output_video).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy video đầu vào để cắt: {input_path}")

    # 1. Đo thời lượng gốc qua FFprobe
    info = probe_media(str(input_path), ffprobe_path)
    orig_duration = float(info.duration)
    log_cb(f"📊 Thời lượng gốc của video: {orig_duration:.2f}s")

    if orig_duration <= trim_seconds:
        log_cb(f"⚠ Video chỉ dài {orig_duration:.2f}s (nhỏ hơn hoặc bằng {trim_seconds}s), giữ nguyên thời lượng tối thiểu 0.5s.")
        target_duration = 0.5
    else:
        target_duration = max(0.5, orig_duration - trim_seconds)

    log_cb(f"✂ Đang cắt bỏ {trim_seconds:.2f}s cuối video -> Thời lượng mới: {target_duration:.2f}s...")

    # 2. Re-encode nhanh bằng libx264 để đảm bảo cắt chính xác từng frame không bị đơ keyframe
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
        # Nếu lỗi libx264, thử stream copy fallback
        log_cb("⚠ Lỗi re-encode, thử lại với phương thức stream copy...")
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

    log_cb(f"✔ Đã cắt và lưu video thành công: {output_path.name}")
    return output_path


class ZskyVideoBatchRunnerThread(QThread):
    """Luồng chạy hàng loạt tạo video và tải về từ Zsky AI với đa tài khoản / concurrency."""

    log_signal = Signal(str)
    progress_signal = Signal(int, str)
    stats_signal = Signal(int, int, int, int)  # total, completed, failed, remaining
    worker_status_signal = Signal(int, str)
    account_status_signal = Signal(str, str, str)  # account_id, status, msg
    video_completed_signal = Signal(str, str)  # prompt, output_path
    batch_finished_signal = Signal(bool, str)

    def __init__(
        self,
        accounts: list[ZskyAccount],
        prompts: list[str],
        output_dir: Path,
        *,
        audio_prompt: str = "",
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
        self.audio_prompt = audio_prompt
        self.concurrency = max(1, min(20, concurrency))
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

    def is_paused(self) -> bool:
        return self._paused

    def run(self) -> None:
        total_items = len(self.prompts)
        if not self.accounts:
            self.log_signal.emit("❌ Chưa có tài khoản Zsky nào được tick chọn để chạy!")
            self.batch_finished_signal.emit(False, "Chưa chọn tài khoản.")
            return

        if not self.prompts:
            self.log_signal.emit("❌ Danh sách prompt trống!")
            self.batch_finished_signal.emit(False, "Danh sách prompt trống.")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_signal.emit(
            f"🚀 Bắt đầu Farm Video Zsky AI: {total_items} prompt | "
            f"{len(self.accounts)} tài khoản | Cùng lúc: {self.concurrency} | "
            f"Tự cắt xóa {self.trim_seconds}s cuối video."
        )

        prompt_queue = list(enumerate(self.prompts, start=1))  # [(index, prompt), ...]
        queue_lock = threading.Lock()
        stats_lock = threading.Lock()

        completed_count = 0
        failed_count = 0

        self.stats_signal.emit(total_items, 0, 0, total_items)
        self.progress_signal.emit(0, f"Đang bắt đầu... (0/{total_items})")

        effective_concurrency = min(self.concurrency, len(self.accounts))

        def worker_task(worker_id: int, account: ZskyAccount) -> None:
            nonlocal completed_count, failed_count
            worker_tag = f"Luồng {worker_id} [{account.name}]"
            self.worker_status_signal.emit(worker_id, f"Đang mở Chrome...")
            self.log_signal.emit(f"⚙ [{worker_tag}]: Khởi tạo Chrome Profile ({account.profile_dir})")

            profile_dir = Path(account.profile_dir).resolve()
            temp_dl_dir = Path(tempfile.mkdtemp(prefix=f"zsky_w{worker_id}_", dir=CACHE_DIR))

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
                driver = create_zsky_chrome_driver(
                    worker_profile, download_dir=temp_dl_dir, headless=self.headless
                )
                driver.set_page_load_timeout(45)

                if account.cookies:
                    self.worker_status_signal.emit(worker_id, "Nạp cookies...")
                    inject_cookies_to_driver(driver, account.cookies, target_url=ZSKY_START_URL)
                else:
                    self.worker_status_signal.emit(worker_id, "Đang vào zsky.ai/create...")
                    driver.get(ZSKY_START_URL)
                    time.sleep(3.0)

                # Kiểm tra trạng thái đăng nhập
                is_logged_in, user_label = check_zsky_login_status(driver)
                if is_logged_in:
                    self.account_status_signal.emit(account.account_id, "active", "🟢 Đã đăng nhập")
                    self.log_signal.emit(f"✔ [{worker_tag}]: Đã đăng nhập ({user_label})")
                else:
                    self.account_status_signal.emit(account.account_id, "need_login", "🟡 Chưa đăng nhập")
                    self.log_signal.emit(
                        f"⚠ [{worker_tag}]: Chưa đăng nhập Google trên zsky.ai! "
                        f"Hãy đảm bảo bạn đã mở trình duyệt đăng nhập tài khoản này trước đó."
                    )

                # Lặp lấy prompt từ hàng đợi chung
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

                    # Gửi prompt & submit
                    try:
                        # Nếu đang không ở trang /create thì về lại /create
                        if "zsky.ai/create" not in driver.current_url.lower():
                            driver.get(ZSKY_START_URL)
                            time.sleep(3.0)

                        submit_zsky_prompt(driver, prompt_text, audio_prompt=self.audio_prompt)
                        self.log_signal.emit(f"🚀 [{worker_tag}] [{task_label}]: Đã bấm nút Generate! Đang chờ tạo video...")

                        # Chờ video tạo xong
                        gen_ok = wait_for_video_generation(
                            driver,
                            timeout=240.0,
                            cancel_check=lambda: self._stopped,
                            log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                        )
                        if not gen_ok:
                            raise RuntimeError("Tạo video bị hủy hoặc quá thời gian chờ.")

                        # Tải video từ trang My Creations
                        downloaded_file = download_video_from_my_creations(
                            driver,
                            temp_dl_dir,
                            timeout=120.0,
                            cancel_check=lambda: self._stopped,
                            log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                        )

                        # Tự động cắt bỏ số giây cuối (mặc định 1.5s)
                        stem_name = safe_filename(f"zsky_{p_idx:03d}_{prompt_text[:30]}", default_prefix="zsky")
                        final_output = self.output_dir / f"{Path(stem_name).stem}_cut.mp4"

                        trimmed_file = trim_video_end(
                            downloaded_file,
                            final_output,
                            trim_seconds=self.trim_seconds,
                            ffmpeg_path=self.ffmpeg_path,
                            ffprobe_path=self.ffprobe_path,
                            log_cb=lambda m: self.log_signal.emit(f"[{worker_tag}]: {m}"),
                        )

                        with stats_lock:
                            completed_count += 1
                            rem = max(0, total_items - (completed_count + failed_count))
                            self.stats_signal.emit(total_items, completed_count, failed_count, rem)
                            pct = int((completed_count / total_items) * 100)
                            self.progress_signal.emit(pct, f"Đã hoàn thành {completed_count}/{total_items}")

                        self.video_completed_signal.emit(prompt_text, str(trimmed_file))
                        self.log_signal.emit(f"🎉 [{worker_tag}] [{task_label}]: Thành công -> {trimmed_file.name}")

                        # Xóa file tạm
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

        # Khởi chạy các worker song song
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
            self.log_signal.emit(f"🏁 ĐÃ HOÀN TẤT TOÀN BỘ BATCH: {completed_count} thành công, {failed_count} lỗi.")
            self.batch_finished_signal.emit(True, f"Thành công {completed_count}/{total_items} video.")
