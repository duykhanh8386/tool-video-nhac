from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from utils.paths import LOG_DIR, USER_DATA_ROOT


GOOGLE_VIDS_URL = "https://vids.new"
GOOGLE_VIDS_PROFILE_DIR = USER_DATA_ROOT / "GoogleVidsChromeProfile"


class GoogleVidsCancelled(RuntimeError):
    pass


class GoogleVidsAutomationError(RuntimeError):
    pass


def google_vids_profile_ready(profile_dir: str | Path = GOOGLE_VIDS_PROFILE_DIR) -> bool:
    root = Path(profile_dir)
    cookie_files = (
        root / "Default" / "Network" / "Cookies",
        root / "Default" / "Cookies",
    )
    return (root / "Local State").is_file() and any(path.is_file() for path in cookie_files)


def open_google_vids_login(
    profile_dir: str | Path = GOOGLE_VIDS_PROFILE_DIR,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Open a normal browser with a dedicated profile for an interactive Google login."""
    progress = progress or (lambda _value, _message: None)
    cancelled = cancelled or (lambda: False)
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    progress(5, "Đang mở Chrome/Edge bình thường cho Google Vids…")
    browser_process = _launch_login_browser(profile)
    progress(
        50,
        "Hãy đăng nhập Google, mở được Google Vids rồi đóng toàn bộ cửa sổ trình duyệt này.",
    )
    deadline = time.monotonic() + 30 * 60
    while browser_process.poll() is None:
        if cancelled():
            _stop_login_browser(browser_process)
            raise GoogleVidsCancelled("Đã hủy đăng nhập Google Vids.")
        if time.monotonic() >= deadline:
            _stop_login_browser(browser_process)
            raise GoogleVidsAutomationError("Đăng nhập Google Vids quá 30 phút nên đã hết thời gian chờ.")
        time.sleep(0.5)
    if not google_vids_profile_ready(profile):
        raise GoogleVidsAutomationError(
            "Chrome chưa lưu được phiên đăng nhập. Hãy bấm Đăng nhập Google Vids và đăng nhập lại."
        )
    progress(100, "Đã lưu phiên Google Vids trong profile riêng của Visual Loop Studio.")
    return str(profile)


def generate_google_vids_clip(
    image_path: str | Path,
    prompt: str,
    output_path: str | Path,
    profile_dir: str | Path = GOOGLE_VIDS_PROFILE_DIR,
    show_browser: bool = True,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Drive the public Google Vids UI. This is intentionally isolated as a beta adapter."""
    progress = progress or (lambda _value, _message: None)
    cancelled = cancelled or (lambda: False)
    source = Path(image_path).resolve()
    destination = Path(output_path).resolve()
    profile = Path(profile_dir).resolve()
    if not source.is_file():
        raise ValueError(f"Không tìm thấy ảnh nguồn: {source}")
    if not prompt.strip():
        raise ValueError("Chưa nhập prompt cho Google Vids.")
    if not google_vids_profile_ready(profile):
        raise GoogleVidsAutomationError(
            "Chưa đăng nhập Google Vids. Hãy bấm ‘Đăng nhập Google Vids (chỉ lần đầu)’ trước."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    sync_playwright = _sync_playwright()
    progress(2, "Đang mở Google Vids bằng phiên đăng nhập đã lưu…")
    with sync_playwright() as playwright:
        context = _launch_context(playwright, profile, headless=not show_browser)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(GOOGLE_VIDS_URL, wait_until="domcontentloaded", timeout=120_000)
            _check_cancel(cancelled)
            _assert_logged_in(page)
            progress(8, "Đang mở công cụ Video AI trong Google Vids…")
            _prepare_ai_video_panel(page, cancelled)
            progress(12, f"Đang tải {source.name} lên Google Vids…")
            _upload_ingredient(page, source, cancelled)
            progress(16, "Đang điền prompt chung…")
            _fill_prompt(page, prompt.strip(), cancelled)
            progress(20, "Đang gửi yêu cầu Google Vids (Omni 720p, 10 giây)…")
            before = _visible_video_count(page)
            _submit_generation(page, cancelled)
            _wait_for_generated_clip(page, before, progress, cancelled)
            progress(78, "Đã tạo clip; đang chèn clip vào cảnh…")
            _insert_generated_clip(page, cancelled)
            progress(84, "Đang yêu cầu Google Vids xuất MP4…")
            _download_project_mp4(page, destination, progress, cancelled)
        except GoogleVidsCancelled:
            raise
        except Exception as exc:
            screenshot = _save_error_screenshot(page)
            detail = f" Ảnh lỗi: {screenshot}" if screenshot else ""
            if isinstance(exc, GoogleVidsAutomationError):
                raise GoogleVidsAutomationError(str(exc) + detail) from exc
            raise GoogleVidsAutomationError(
                "Google Vids Web đã thay đổi giao diện hoặc không phản hồi. "
                f"Hãy bật ‘Hiện Chrome khi chạy’ để kiểm tra.{detail} Lỗi gốc: {exc}"
            ) from exc
        finally:
            try:
                context.close()
            except Exception:
                pass
    if not destination.is_file() or destination.stat().st_size == 0:
        raise GoogleVidsAutomationError("Google Vids báo tải xong nhưng không tìm thấy file MP4 đầu ra.")
    progress(100, f"Đã tải clip Google Vids: {destination.name}")
    return destination


def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise GoogleVidsAutomationError(
            "Bản ứng dụng này chưa có bộ điều khiển Chrome Playwright. Hãy cập nhật lên bản EXE mới nhất."
        ) from exc
    return sync_playwright


def _browser_candidates() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []

    def add(name: str, path: str | Path | None) -> None:
        if not path:
            return
        candidate = Path(path)
        if not candidate.is_file():
            return
        key = str(candidate.resolve()).casefold()
        if any(str(existing.resolve()).casefold() == key for _label, existing in candidates):
            return
        candidates.append((name, candidate))

    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if program_files:
        add("Google Chrome", Path(program_files) / "Google/Chrome/Application/chrome.exe")
    if program_files_x86:
        add("Google Chrome", Path(program_files_x86) / "Google/Chrome/Application/chrome.exe")
    if local_app_data:
        add("Google Chrome", Path(local_app_data) / "Google/Chrome/Application/chrome.exe")
    if program_files_x86:
        add("Microsoft Edge", Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe")
    if program_files:
        add("Microsoft Edge", Path(program_files) / "Microsoft/Edge/Application/msedge.exe")
    if local_app_data:
        add("Microsoft Edge", Path(local_app_data) / "Microsoft/Edge/Application/msedge.exe")
    for name, command in (
        ("Google Chrome", "chrome"),
        ("Google Chrome", "google-chrome"),
        ("Microsoft Edge", "msedge"),
    ):
        add(name, shutil.which(command))
    return candidates


def _launch_login_browser(profile: Path) -> subprocess.Popen:
    errors: list[str] = []
    for name, executable in _browser_candidates():
        command = [
            str(executable),
            f"--user-data-dir={profile.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            GOOGLE_VIDS_URL,
        ]
        try:
            return subprocess.Popen(command)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
    detail = " | ".join(errors)
    if detail:
        detail = " " + detail
    raise GoogleVidsAutomationError(
        "Không mở được Google Chrome hoặc Microsoft Edge bình thường. "
        f"Hãy cài/cập nhật một trong hai trình duyệt.{detail}"
    )


def _stop_login_browser(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _launch_context(playwright, profile: Path, headless: bool):
    profile.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for channel in ("chrome", "msedge"):
        try:
            return playwright.chromium.launch_persistent_context(
                str(profile),
                channel=channel,
                headless=headless,
                accept_downloads=True,
                no_viewport=True,
                locale="vi-VN",
                chromium_sandbox=True,
                args=["--start-maximized"],
            )
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
    if any("user data directory is already in use" in error.lower() for error in errors):
        raise GoogleVidsAutomationError(
            "Profile Google Vids đang được một cửa sổ Chrome/Edge khác sử dụng. "
            "Hãy đóng cửa sổ đăng nhập Google Vids cũ rồi thử lại."
        )
    raise GoogleVidsAutomationError(
        "Không mở được Google Chrome hoặc Microsoft Edge. Hãy cài/cập nhật một trong hai trình duyệt. "
        + " | ".join(errors)
    )


def _assert_logged_in(page) -> None:
    url = str(page.url or "").lower()
    if "accounts.google.com" in url or "signin" in url:
        raise GoogleVidsAutomationError(
            "Phiên Google đã hết hạn. Hãy đăng nhập Google Vids lại trong ứng dụng."
        )


def _prepare_ai_video_panel(page, cancelled) -> None:
    _click_named(
        page,
        ("Create AI videos", "Tạo video AI", "Tạo video bằng AI"),
        cancelled,
        required=False,
        timeout=12_000,
    )
    _click_named(
        page,
        ("AI video", "Video AI", "Đoạn video do AI tạo"),
        cancelled,
        required=True,
        timeout=30_000,
    )
    _click_named(
        page,
        ("Create", "Tạo"),
        cancelled,
        required=False,
        timeout=8_000,
        roles=("tab", "button"),
    )


def _upload_ingredient(page, source: Path, cancelled) -> None:
    _check_cancel(cancelled)
    _click_named(
        page,
        ("Ingredients", "Ingredient", "Thành phần"),
        cancelled,
        required=False,
        timeout=8_000,
    )
    page.wait_for_timeout(800)
    for frame in page.frames:
        inputs = frame.locator("input[type=file]")
        try:
            count = inputs.count()
        except Exception:
            continue
        for index in range(count - 1, -1, -1):
            try:
                inputs.nth(index).set_input_files(str(source), timeout=8_000)
                page.wait_for_timeout(1_000)
                return
            except Exception:
                continue
    try:
        with page.expect_file_chooser(timeout=10_000) as chooser_info:
            _click_named(
                page,
                ("Upload", "Tải lên", "Add image", "Thêm ảnh", "Ingredients", "Thành phần"),
                cancelled,
                required=True,
                timeout=10_000,
            )
        chooser_info.value.set_files(str(source))
    except Exception as exc:
        raise GoogleVidsAutomationError(
            "Không tìm thấy ô tải ảnh ‘Thành phần’ của Google Vids."
        ) from exc


def _fill_prompt(page, prompt: str, cancelled) -> None:
    _check_cancel(cancelled)
    selectors = (
        "textarea[placeholder*='Describe' i]",
        "textarea[placeholder*='prompt' i]",
        "textarea[placeholder*='Mô tả' i]",
        "[contenteditable=true][aria-label*='prompt' i]",
        "[contenteditable=true][aria-label*='Mô tả' i]",
        "textarea",
        "[contenteditable=true][role=textbox]",
    )
    for frame in page.frames:
        for selector in selectors:
            candidates = frame.locator(selector)
            try:
                count = candidates.count()
            except Exception:
                continue
            for index in range(count - 1, -1, -1):
                field = candidates.nth(index)
                try:
                    if not field.is_visible():
                        continue
                    field.click()
                    try:
                        field.fill(prompt)
                    except Exception:
                        field.press("Control+A")
                        field.type(prompt)
                    return
                except Exception:
                    continue
    raise GoogleVidsAutomationError("Không tìm thấy ô nhập prompt của Google Vids.")


def _submit_generation(page, cancelled) -> None:
    if _click_named(
        page,
        ("Generate video", "Create video", "Tạo video", "Generate", "Tạo", "Submit", "Gửi"),
        cancelled,
        required=False,
        timeout=12_000,
        prefer_last=True,
    ):
        return
    selectors = (
        "button[aria-label*='Generate' i]",
        "button[aria-label*='Create video' i]",
        "button[aria-label*='Tạo video' i]",
        "button[aria-label*='Gửi' i]",
    )
    for selector in selectors:
        button = page.locator(selector).last
        try:
            button.wait_for(state="visible", timeout=5_000)
            button.click()
            return
        except Exception:
            continue
    raise GoogleVidsAutomationError("Không tìm thấy nút gửi yêu cầu tạo video của Google Vids.")


def _wait_for_generated_clip(page, before: int, progress, cancelled) -> None:
    started = time.monotonic()
    deadline = started + 20 * 60
    while time.monotonic() < deadline:
        _check_cancel(cancelled)
        elapsed = time.monotonic() - started
        value = min(74, 22 + round(elapsed / (20 * 60) * 52))
        progress(value, f"Google Vids đang tạo clip… đã chờ {int(elapsed // 60)} phút {int(elapsed % 60):02d} giây")
        if _visible_video_count(page) > before:
            return
        if _has_named(page, ("Insert", "Chèn", "Add to scene", "Thêm vào cảnh")):
            return
        body_text = _body_text(page)
        if re.search(r"(generation failed|couldn.?t generate|không thể tạo|tạo video thất bại)", body_text, re.I):
            raise GoogleVidsAutomationError("Google Vids báo tạo clip thất bại hoặc đã hết quota.")
        page.wait_for_timeout(2_000)
    raise GoogleVidsAutomationError("Google Vids tạo clip quá 20 phút nên tool đã dừng chờ.")


def _insert_generated_clip(page, cancelled) -> None:
    if _click_named(
        page,
        ("Insert", "Chèn", "Add to scene", "Thêm vào cảnh", "Use video", "Dùng video"),
        cancelled,
        required=False,
        timeout=15_000,
        prefer_last=True,
    ):
        page.wait_for_timeout(2_000)
        return
    for frame in page.frames:
        videos = frame.locator("video")
        try:
            for index in range(videos.count() - 1, -1, -1):
                video = videos.nth(index)
                if video.is_visible():
                    video.click()
                    if _click_named(
                        page,
                        ("Insert", "Chèn", "Add to scene", "Thêm vào cảnh"),
                        cancelled,
                        required=False,
                        timeout=8_000,
                        prefer_last=True,
                    ):
                        page.wait_for_timeout(2_000)
                        return
        except Exception:
            continue
    raise GoogleVidsAutomationError("Clip đã tạo nhưng không tìm thấy nút chèn vào cảnh.")


def _download_project_mp4(page, destination: Path, progress, cancelled) -> None:
    downloads: list = []
    page.on("download", lambda download: downloads.append(download))
    _click_named(page, ("File", "Tệp"), cancelled, required=True, timeout=15_000)
    page.wait_for_timeout(500)
    _click_named(
        page,
        ("Download", "Download as", "Tải xuống", "Tải xuống dưới dạng"),
        cancelled,
        required=True,
        timeout=12_000,
    )
    page.wait_for_timeout(500)
    if not downloads:
        _click_named(
            page,
            ("MP4", "MP4 (.mp4)", "Download as MP4", "Tải xuống dưới dạng MP4"),
            cancelled,
            required=False,
            timeout=12_000,
        )
    started = time.monotonic()
    deadline = started + 10 * 60
    while time.monotonic() < deadline:
        _check_cancel(cancelled)
        if downloads:
            download = downloads[-1]
            failure = download.failure()
            if failure:
                raise GoogleVidsAutomationError(f"Google Vids tải MP4 thất bại: {failure}")
            download.save_as(str(destination))
            return
        elapsed = time.monotonic() - started
        progress(min(97, 86 + round(elapsed / 600 * 11)), "Google Vids đang xử lý và tải MP4…")
        page.wait_for_timeout(1_000)
    raise GoogleVidsAutomationError("Không nhận được file MP4 từ Google Vids sau 10 phút.")


def _click_named(
    page,
    names: tuple[str, ...],
    cancelled,
    *,
    required: bool,
    timeout: int,
    prefer_last: bool = False,
    roles: tuple[str, ...] = ("button", "menuitem", "tab", "link"),
) -> bool:
    pattern = re.compile("|".join(re.escape(name) for name in names), re.I)
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        _check_cancel(cancelled)
        for frame in page.frames:
            for role in roles:
                try:
                    locator = frame.get_by_role(role, name=pattern)
                    count = min(locator.count(), 30)
                    indexes = range(count - 1, -1, -1) if prefer_last else range(count)
                    for index in indexes:
                        item = locator.nth(index)
                        if item.is_visible() and item.is_enabled():
                            item.click()
                            return True
                except Exception:
                    continue
        page.wait_for_timeout(300)
    if required:
        raise GoogleVidsAutomationError(f"Không tìm thấy nút Google Vids: {' / '.join(names)}")
    return False


def _has_named(page, names: tuple[str, ...]) -> bool:
    pattern = re.compile("|".join(re.escape(name) for name in names), re.I)
    for frame in page.frames:
        for role in ("button", "menuitem", "tab"):
            try:
                locator = frame.get_by_role(role, name=pattern)
                for index in range(min(locator.count(), 30)):
                    if locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
    return False


def _visible_video_count(page) -> int:
    count = 0
    for frame in page.frames:
        try:
            videos = frame.locator("video")
            count += sum(videos.nth(index).is_visible() for index in range(videos.count()))
        except Exception:
            continue
    return count


def _body_text(page) -> str:
    values: list[str] = []
    for frame in page.frames:
        try:
            values.append(frame.locator("body").inner_text(timeout=2_000))
        except Exception:
            continue
    return "\n".join(values)


def _check_cancel(cancelled) -> None:
    if cancelled():
        raise GoogleVidsCancelled("Đã hủy tác vụ Google Vids.")


def _save_error_screenshot(page) -> str:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"google_vids_error_{datetime.now():%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return ""
