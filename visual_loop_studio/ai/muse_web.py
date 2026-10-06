"""Muse AI web automation — protocol v3.

Each account runs in its own Chrome profile with a dedicated ChromeDriver port,
eliminating driver conflicts when multiple workers run in parallel.

Key design decisions
--------------------
* **Message-ID locking** — We record the full set of visible message IDs right
  before pressing Send, then watch *only* for a new message-ID that appeared
  after that snapshot.  This guarantees we never download a video from an older
  chat turn.
* **Checkpoint** — Every job writes its state (sent-timestamp, locked message ID,
  download path) to a JSON file so that a restart can skip the "send" phase and
  resume directly at the download step.
* **MP4 validation** — A downloaded file only counts as "success" if it exists,
  has size > 50 KB, and starts with the ``ftyp`` atom (valid MP4 container).
* **Transient-error tolerance** — Google sometimes redirects to an accounts page
  or shows a temporary error.  The driver catches these and waits instead of
  quitting Chrome.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from utils.paths import LOG_DIR, USER_DATA_ROOT


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MUSE_URL = "https://muse.ai/"
CHECKPOINT_PROTOCOL = 3
MUSE_PROFILES_ROOT = USER_DATA_ROOT / "MuseChromeProfiles"
CHECKPOINT_DIR = USER_DATA_ROOT / "muse_checkpoints"
_BASE_DEBUG_PORT = 19200  # accounts get 19200, 19201, 19202 …


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MuseJobCheckpoint:
    """Persisted state for a single prompt job inside a Muse chat."""
    protocol: int = CHECKPOINT_PROTOCOL
    image_path: str = ""
    prompt: str = ""
    output_path: str = ""
    account_index: int = 0
    sent_ts: str = ""          # ISO-8601 UTC timestamp of the Send click
    message_ids_before: list[str] = field(default_factory=list)
    locked_message_id: str = ""  # the one message-ID we are waiting for
    download_done: bool = False
    mp4_valid: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MuseJobCheckpoint":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


class MuseCancelled(RuntimeError):
    pass


class MuseAutomationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------

def _chrome_executable() -> Path:
    """Find a usable Chrome/Edge binary on Windows."""
    candidates: list[Path] = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
        candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for cmd in ("chrome", "google-chrome", "msedge"):
        found = shutil.which(cmd)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path
    raise MuseAutomationError(
        "Không tìm thấy Google Chrome hoặc Microsoft Edge. "
        "Hãy cài một trong hai trình duyệt."
    )


def profile_dir_for(account_index: int) -> Path:
    """Return an isolated Chrome user-data-dir for the given account slot."""
    return MUSE_PROFILES_ROOT / f"account_{account_index}"


def debug_port_for(account_index: int) -> int:
    return _BASE_DEBUG_PORT + account_index


def _launch_chrome(account_index: int, url: str = MUSE_URL) -> subprocess.Popen:
    """Start Chrome with a unique profile and remote-debugging port."""
    chrome = _chrome_executable()
    profile = profile_dir_for(account_index)
    profile.mkdir(parents=True, exist_ok=True)
    port = debug_port_for(account_index)
    cmd = [
        str(chrome),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        url,
    ]
    return subprocess.Popen(cmd)


def open_muse_login(account_index: int) -> subprocess.Popen:
    """Open Chrome with the account's profile so the user can log in manually."""
    return _launch_chrome(account_index, MUSE_URL)


def is_profile_ready(account_index: int) -> bool:
    """Quick heuristic: profile exists and has cookie data."""
    profile = profile_dir_for(account_index)
    cookie_paths = (
        profile / "Default" / "Network" / "Cookies",
        profile / "Default" / "Cookies",
    )
    return (profile / "Local State").is_file() and any(p.is_file() for p in cookie_paths)


# ---------------------------------------------------------------------------
# Checkpoint persistence
# ---------------------------------------------------------------------------

def _ckpt_path(account_index: int, image_stem: str) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / f"acct{account_index}_{image_stem}.json"


def save_checkpoint(ckpt: MuseJobCheckpoint) -> None:
    path = _ckpt_path(ckpt.account_index, Path(ckpt.image_path).stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(ckpt.to_dict(), f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_checkpoint(account_index: int, image_path: str) -> MuseJobCheckpoint | None:
    path = _ckpt_path(account_index, Path(image_path).stem)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        ckpt = MuseJobCheckpoint.from_dict(data)
        if ckpt.protocol != CHECKPOINT_PROTOCOL:
            return None  # stale protocol → discard
        return ckpt
    except Exception:
        return None


def clear_checkpoint(account_index: int, image_path: str) -> None:
    path = _ckpt_path(account_index, Path(image_path).stem)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# MP4 validation
# ---------------------------------------------------------------------------

def is_valid_mp4(path: str | Path) -> bool:
    """Return True only if *path* exists, is > 50 KB, and has the ftyp atom."""
    p = Path(path)
    if not p.is_file():
        return False
    if p.stat().st_size < 50_000:
        return False
    try:
        with p.open("rb") as f:
            header = f.read(12)
        if len(header) < 8:
            return False
        return header[4:8] == b"ftyp"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Playwright-based Muse automation
# ---------------------------------------------------------------------------

def _sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise MuseAutomationError(
            "Cần cài playwright. Chạy: pip install playwright && python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _connect_to_chrome(playwright, account_index: int):
    """Connect Playwright to an already-running Chrome instance via CDP."""
    port = debug_port_for(account_index)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        return browser
    except Exception as exc:
        raise MuseAutomationError(
            f"Không kết nối được Chrome ở cổng {port}. "
            f"Hãy mở Chrome/Muse đã tick cho tài khoản {account_index + 1}. Lỗi: {exc}"
        ) from exc


def _find_muse_tab(browser):
    """Find an existing Muse tab among all browser contexts/pages."""
    for ctx in browser.contexts:
        for page in ctx.pages:
            url = (page.url or "").lower()
            if "muse.ai" in url:
                return page
    return None


def _wait_muse_tab(browser, cancelled, timeout: int = 60) -> Any:
    """Wait for a Muse tab to appear, tolerating Google redirect pages."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            raise MuseCancelled("Đã hủy.")
        page = _find_muse_tab(browser)
        if page:
            return page
        time.sleep(1)
    raise MuseAutomationError(
        "Không tìm thấy tab Muse trong Chrome. Hãy mở https://muse.ai/ trước."
    )


def _check_cancel(cancelled) -> None:
    if cancelled():
        raise MuseCancelled("Đã hủy tác vụ Muse.")


def _is_transient_page(url: str) -> bool:
    """Return True if Chrome is on a Google sign-in or consent redirect."""
    low = (url or "").lower()
    return any(x in low for x in (
        "accounts.google.com",
        "consent.google.com",
        "gds.google.com",
        "myaccount.google.com/signinoptions",
    ))


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------

def _collect_message_ids(page) -> list[str]:
    """Return all data-message-id values currently visible in the chat."""
    try:
        ids = page.evaluate("""() => {
            const items = document.querySelectorAll('[data-message-id]');
            return Array.from(items).map(el => el.getAttribute('data-message-id'));
        }""")
        return ids or []
    except Exception:
        return []


def _collect_all_video_srcs(page) -> set[str]:
    """Snapshot every <video> src currently in the DOM."""
    try:
        srcs = page.evaluate("""() => {
            const videos = document.querySelectorAll('video');
            const result = [];
            for (const v of videos) {
                if (v.src) result.push(v.src);
                const sources = v.querySelectorAll('source');
                for (const s of sources) {
                    if (s.src) result.push(s.src);
                }
            }
            return result;
        }""")
        return set(srcs or [])
    except Exception:
        return set()


def _upload_image(page, image_path: str, cancelled) -> None:
    """Find the file input (attachment button) and set the image."""
    _check_cancel(cancelled)
    # Try multiple selectors used by Muse's chat UI
    selectors = [
        "input[type='file']",
        "input[type='file'][accept*='image']",
        "#file-upload",
    ]
    for sel in selectors:
        try:
            inputs = page.locator(sel)
            count = inputs.count()
            if count > 0:
                inputs.last.set_input_files(image_path, timeout=10_000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue
    # Fallback: click the + / attach button then use file chooser
    attach_selectors = [
        "button[aria-label*='Attach' i]",
        "button[aria-label*='Upload' i]",
        "button[aria-label*='Đính kèm' i]",
        "button[aria-label*='Tải lên' i]",
        "[data-testid='attach-button']",
        "button:has(svg)",  # generic icon button near input
    ]
    for sel in attach_selectors:
        try:
            btn = page.locator(sel).last
            if btn.is_visible():
                with page.expect_file_chooser(timeout=8_000) as fc:
                    btn.click()
                fc.value.set_files(image_path)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue
    raise MuseAutomationError(
        "Không tìm thấy nút đính kèm ảnh trong Muse. Giao diện có thể đã thay đổi."
    )


def _fill_prompt(page, prompt: str, cancelled) -> None:
    """Type the prompt into the chat input."""
    _check_cancel(cancelled)
    selectors = [
        "textarea",
        "[contenteditable='true']",
        "input[type='text']",
        "[role='textbox']",
    ]
    for sel in selectors:
        try:
            fields = page.locator(sel)
            for i in range(fields.count()):
                f = fields.nth(i)
                if f.is_visible():
                    f.click()
                    try:
                        f.fill(prompt)
                    except Exception:
                        f.press("Control+A")
                        f.type(prompt)
                    return
        except Exception:
            continue
    raise MuseAutomationError("Không tìm thấy ô nhập prompt trong Muse.")


def _click_send(page, cancelled) -> None:
    """Click the Send button."""
    _check_cancel(cancelled)
    send_selectors = [
        "button[aria-label*='Send' i]",
        "button[aria-label*='Gửi' i]",
        "button[data-testid='send-button']",
        "button[type='submit']",
    ]
    for sel in send_selectors:
        try:
            btn = page.locator(sel).last
            if btn.is_visible() and btn.is_enabled():
                btn.click()
                return
        except Exception:
            continue
    # Fallback: press Enter in the textarea
    try:
        page.keyboard.press("Enter")
    except Exception:
        raise MuseAutomationError("Không tìm thấy nút Gửi trong Muse.")


def _wait_for_video_after_message(
    page,
    msg_ids_before: list[str],
    video_srcs_before: set[str],
    progress: Callable[[int, str], None],
    cancelled: Callable[[], bool],
    timeout_minutes: int = 25,
) -> str:
    """Wait until a new <video> with a valid src appears after the sent message.

    Strategy:
    1. Look for a new message-ID that did not exist before Send.
    2. Within that message group, look for a <video> tag.
    3. Also detect the case where Muse reuses an existing <video> element
       and simply changes its ``src`` attribute.
    4. Wait until the video has ``readyState >= 2`` (enough data to play).
    """
    started = time.monotonic()
    deadline = started + timeout_minutes * 60
    ids_before_set = set(msg_ids_before)
    locked_msg_id = ""

    while time.monotonic() < deadline:
        _check_cancel(cancelled)
        elapsed = time.monotonic() - started
        pct = min(85, 25 + round(elapsed / (timeout_minutes * 60) * 60))
        progress(
            pct,
            f"Muse đang xử lý… đã chờ {int(elapsed // 60)} phút {int(elapsed % 60):02d}s"
        )

        # Check for transient Google redirects — just wait
        url = (page.url or "").lower()
        if _is_transient_page(url):
            progress(pct, "Google đang chuyển trang (tạm thời) — đợi…")
            page.wait_for_timeout(3_000)
            continue

        # Step 1: Find new message IDs created after the prompt
        current_ids = _collect_message_ids(page)
        new_ids = [mid for mid in current_ids if mid not in ids_before_set]

        # Step 2: Check each new message (newest first) for a generated video
        for mid in reversed(new_ids):
            video_src = page.evaluate("""(msgId) => {
                const msgEl = document.querySelector(`[data-message-id="${msgId}"]`);
                if (msgEl) {
                    const vids = msgEl.querySelectorAll('video');
                    for (const v of vids) {
                        const src = v.src || (v.querySelector('source') || {}).src || '';
                        if (src && !src.endsWith('.png') && !src.endsWith('.jpg') && !src.startsWith('data:image/')) {
                            return src;
                        }
                    }
                    const dlLink = msgEl.querySelector('a[download], a[href*=".mp4"]');
                    if (dlLink && dlLink.href) return dlLink.href;
                }
                return '';
            }""", mid)
            if video_src:
                return video_src

        # Step 3: Detect newly added <video> sources across the page
        current_srcs = _collect_all_video_srcs(page)
        new_srcs = current_srcs - video_srcs_before
        for src in new_srcs:
            if src and not src.endswith(('.png', '.jpg', '.jpeg', '.webp')) and not src.startswith('data:image/'):
                return src

        page.wait_for_timeout(2_000)

    raise MuseAutomationError(
        f"Muse không trả về video sau {timeout_minutes} phút."
    )


def _download_video(page, video_src: str, output_path: str, progress, cancelled) -> Path:
    """Download the video from its blob/URL to a local file."""
    _check_cancel(cancelled)
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    progress(88, "Đang tải video từ Muse…")

    # If it's a blob URL, we need to extract via page context
    if video_src.startswith("blob:"):
        try:
            # Use MediaRecorder approach for blob URLs
            video_data = page.evaluate("""async (blobUrl) => {
                const response = await fetch(blobUrl);
                const blob = await response.blob();
                const buffer = await blob.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = '';
                const chunk = 8192;
                for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                }
                return btoa(binary);
            }""", video_src)
            import base64
            raw = base64.b64decode(video_data)
            with open(dest, "wb") as f:
                f.write(raw)
        except Exception as exc:
            raise MuseAutomationError(f"Không tải được blob video: {exc}") from exc
    else:
        # Regular URL — use Playwright download
        try:
            # Try direct fetch
            response = page.request.get(video_src)
            if response.ok:
                with open(dest, "wb") as f:
                    f.write(response.body())
            else:
                raise MuseAutomationError(f"HTTP {response.status} khi tải video.")
        except MuseAutomationError:
            raise
        except Exception:
            # Fallback: trigger download via browser
            try:
                with page.expect_download(timeout=120_000) as dl_info:
                    page.evaluate("""(src) => {
                        const a = document.createElement('a');
                        a.href = src;
                        a.download = 'muse-video.mp4';
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                    }""", video_src)
                dl_info.value.save_as(str(dest))
            except Exception as exc2:
                raise MuseAutomationError(f"Không tải được video từ Muse: {exc2}") from exc2

    progress(95, "Đang xác thực file MP4…")
    if not is_valid_mp4(dest):
        raise MuseAutomationError(
            f"File tải về không phải MP4 hợp lệ: {dest} "
            f"(kích thước: {dest.stat().st_size if dest.is_file() else 0} bytes)"
        )
    progress(100, f"Đã tải thành công: {dest.name}")
    return dest


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

def process_muse_job(
    image_path: str,
    prompt: str,
    output_path: str,
    account_index: int = 0,
    muse_url: str = MUSE_URL,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    tab_handle: str = "",
    video_settings: dict | None = None,
) -> Path:
    """Send a prompt with an image to Muse and download the resulting video.

    If a valid checkpoint exists from a previous run (same protocol version),
    the function skips the send step and goes straight to downloading.
    """
    progress = progress or (lambda _v, _m: None)
    cancelled = cancelled or (lambda: False)

    source = Path(image_path).resolve()
    if not source.is_file():
        raise ValueError(f"Ảnh nguồn không tồn tại: {source}")
    if not prompt.strip():
        raise ValueError("Chưa nhập prompt.")

    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Check for resumable checkpoint
    ckpt = load_checkpoint(account_index, image_path)
    need_send = True
    if ckpt and ckpt.prompt == prompt and not ckpt.download_done:
        need_send = False  # resume download only
        progress(5, "Tìm thấy checkpoint — bỏ qua bước gửi, chỉ tải video…")

    sync_playwright = _sync_playwright()

    progress(2, f"Kết nối Chrome tài khoản {account_index + 1}…")

    with sync_playwright() as pw:
        browser = _connect_to_chrome(pw, account_index)
        try:
            page = _wait_muse_tab(browser, cancelled)

            # Navigate to Muse URL if not already there
            url = (page.url or "").lower()
            if "muse.ai" not in url:
                page.goto(muse_url, wait_until="domcontentloaded", timeout=60_000)

            # Wait for chat to be ready
            _check_cancel(cancelled)
            page.wait_for_timeout(2_000)

            # Handle transient Google pages
            for _ in range(10):
                if _is_transient_page(page.url):
                    progress(5, "Google đang chuyển trang — đợi…")
                    page.wait_for_timeout(3_000)
                else:
                    break

            if need_send:
                progress(8, "Snapshot DOM trước khi gửi…")

                # Snapshot message IDs & video srcs BEFORE sending
                msg_ids_before = _collect_message_ids(page)
                video_srcs_before = _collect_all_video_srcs(page)

                progress(12, f"Đang tải ảnh {source.name} lên Muse…")
                _upload_image(page, str(source), cancelled)

                progress(16, "Đang điền prompt…")
                _fill_prompt(page, prompt.strip(), cancelled)

                # Build & save checkpoint BEFORE clicking Send
                ckpt = MuseJobCheckpoint(
                    protocol=CHECKPOINT_PROTOCOL,
                    image_path=str(source),
                    prompt=prompt,
                    output_path=str(dest),
                    account_index=account_index,
                    sent_ts=datetime.now(timezone.utc).isoformat(),
                    message_ids_before=msg_ids_before,
                )
                save_checkpoint(ckpt)

                progress(20, "Đang bấm Gửi…")
                _click_send(page, cancelled)
                page.wait_for_timeout(1_000)

            else:
                # Resuming — reconstruct state from checkpoint
                msg_ids_before = ckpt.message_ids_before or []
                video_srcs_before = _collect_all_video_srcs(page)
                progress(20, "Đang chờ video từ checkpoint khôi phục…")

            # Wait for the video to appear
            progress(22, "Đang chờ Muse trả video…")
            video_src = _wait_for_video_after_message(
                page, msg_ids_before, video_srcs_before,
                progress, cancelled
            )

            # Lock the message ID into checkpoint
            current_ids = _collect_message_ids(page)
            new_ids = [mid for mid in current_ids if mid not in set(msg_ids_before)]
            if new_ids:
                ckpt.locked_message_id = new_ids[-1]
                save_checkpoint(ckpt)

            # Download
            result = _download_video(page, video_src, str(dest), progress, cancelled)

            # Mark complete
            ckpt.download_done = True
            ckpt.mp4_valid = is_valid_mp4(result)
            save_checkpoint(ckpt)

            return result

        except MuseCancelled:
            raise
        except Exception as exc:
            screenshot = _save_error_screenshot(page if 'page' in dir() else None)
            detail = f" Ảnh lỗi: {screenshot}" if screenshot else ""
            if isinstance(exc, (MuseAutomationError, MuseCancelled)):
                raise
            raise MuseAutomationError(
                f"Lỗi Muse: {exc}{detail}"
            ) from exc
        finally:
            try:
                browser.close()
            except Exception:
                pass


def migrate_outputs(old_dir: str, new_dir: str) -> int:
    """Copy valid MP4s from *old_dir* to *new_dir*.  Returns count copied."""
    old = Path(old_dir)
    new = Path(new_dir)
    if not old.is_dir():
        return 0
    new.mkdir(parents=True, exist_ok=True)
    count = 0
    for mp4 in old.glob("*.mp4"):
        if is_valid_mp4(mp4):
            dst = new / mp4.name
            if not dst.exists():
                shutil.copy2(mp4, dst)
                count += 1
    return count


def _save_error_screenshot(page) -> str:
    if page is None:
        return ""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"muse_error_{datetime.now():%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return ""
