from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from auth.local_generation import create_neutral_canvas_image
from auth.muse_cookies import (
    MUSE_COOKIE_PROFILES_DIR,
    MUSE_DEFAULT_URL,
    MuseCookieAccount,
    MuseCookieAccountStore,
    inject_cookies_to_driver,
    parse_muse_cookies,
)
from auth.muse_generation import (
    ATTACH_TEXTS,
    _click_by_text,
    _dismiss_muse_popups,
    _download_muse_video_file,
    _find,
    _get_muse_recent_text,
    _query_muse_videos,
    fill_muse_chat_prompt,
    is_muse_disconnected_text,
    is_muse_quota_exhausted,
    submit_muse_chat_prompt,
)
from auth.muse_login import create_muse_chrome_driver
from playlist_ai.providers.base import CookieExpiredError, IProvider, ProviderError
from utils.paths import CACHE_DIR


class MuseProvider(IProvider):
    """
    Provider kết nối Muse AI qua Cookie xác thực:
    - Tự động mở trình duyệt Chrome, nạp Cookie Muse AI và gửi Prompt thật vào chat.
    - Theo dõi tiến trình tạo video trực tiếp trên giao diện Muse AI.
    - Tải video .mp4 thật về máy (kiểm tra dung lượng > 10 KB).
    - Tự động phát hiện khi cookie hết hạn hoặc hết quota để kích hoạt CookieExpiredError,
      giúp UI tự động tạm dừng mà không mất danh sách đang chờ.
    """

    def __init__(
        self,
        cookie_raw: str = "",
        account_id: Optional[str] = None,
        store: Optional[MuseCookieAccountStore] = None,
        headless: bool = True,
    ):
        self._cookie_raw = cookie_raw.strip()
        self._account_id = account_id
        self._store = store or MuseCookieAccountStore()
        self.headless = headless
        self._parsed_cookies: list[dict[str, Any]] = []

        self._driver: Any = None
        self._download_dir: Optional[Path] = None
        self._profile_dir: Optional[Path] = None

        if self._cookie_raw:
            self._parsed_cookies = parse_muse_cookies(self._cookie_raw)
        elif self._account_id:
            acc = self._store.get(self._account_id)
            if acc:
                self._cookie_raw = acc.cookie_raw
                self._parsed_cookies = acc.cookies or parse_muse_cookies(acc.cookie_raw)

    @property
    def key(self) -> str:
        return "muse"

    @property
    def display_name(self) -> str:
        return "Muse AI (Cookie)"

    def close(self) -> None:
        """Đóng phiên trình duyệt Chrome khi hàng đợi kết thúc hoặc người dùng dừng tác vụ."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

        if self._download_dir is not None and self._download_dir.exists():
            try:
                shutil.rmtree(self._download_dir, ignore_errors=True)
            except Exception:
                pass
            self._download_dir = None

    def update_cookie(self, new_cookie_raw: str) -> None:
        """Cập nhật cookie mới khi cookie cũ hết hạn."""
        self._cookie_raw = new_cookie_raw.strip()
        self._parsed_cookies = parse_muse_cookies(self._cookie_raw)
        if self._account_id:
            acc = self._store.get(self._account_id)
            if acc:
                acc.cookie_raw = self._cookie_raw
                acc.cookies = self._parsed_cookies
                acc.status = "active"
                acc.status_message = "Đã cập nhật cookie mới"
                self._store.save(acc)

        # Nếu đang có session browser mở, nạp ngay cookie mới vào
        if self._driver is not None and self._parsed_cookies:
            try:
                inject_cookies_to_driver(self._driver, self._parsed_cookies, target_url=MUSE_DEFAULT_URL)
            except Exception:
                self.close()

    def check_health(self) -> tuple[bool, str]:
        """Kiểm tra xem cookie có hợp lệ hay không."""
        if not self._cookie_raw and not self._parsed_cookies:
            return False, "Chưa nhập Cookie Muse AI. Hãy dán cookie để tiếp tục."

        if not self._parsed_cookies:
            return False, "Định dạng cookie không hợp lệ. Hãy copy lại từ Cookie-Editor (JSON hoặc Header)."

        cookie_names = {str(c.get("name") or "").lower() for c in self._parsed_cookies}
        session_indicators = {"session", "token", "auth", "jwt", "sid", "connect.sid", "remember", "__session"}
        has_session = any(any(ind in name for ind in session_indicators) for name in cookie_names)

        if not has_session and len(self._parsed_cookies) < 2:
            return False, "Cookie thiếu thông tin phiên đăng nhập (session/token). Hãy xuất lại cookie đầy đủ."

        return True, f"Cookie hợp lệ ({len(self._parsed_cookies)} cookie keys)"

    def _ensure_driver(
        self,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Any:
        """Đảm bảo có phiên Chrome WebDriver đang chạy với Cookie Muse đã nạp."""
        if self._driver is not None:
            try:
                _ = self._driver.current_window_handle
                return self._driver
            except Exception:
                self.close()

        if cancelled and cancelled():
            raise ProviderError("Tác vụ đã bị dừng bởi người dùng.")

        if progress:
            progress(5, "Đang khởi động trình duyệt Chrome cho Muse AI...")

        profile_dir = MUSE_COOKIE_PROFILES_DIR / "playlist_muse"
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir = profile_dir

        download_dir = CACHE_DIR / "MuseDownloads" / f"playlist_{int(time.time())}"
        download_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir = download_dir

        try:
            driver = create_muse_chrome_driver(profile_dir, download_dir=download_dir, headless=self.headless)
            driver.set_page_load_timeout(35)
        except Exception as exc:
            raise ProviderError(f"Không thể mở Chrome cho Muse AI: {exc}") from exc

        try:
            if progress:
                progress(10, "Đang nạp Cookie Muse AI vào trình duyệt...")
            inject_cookies_to_driver(driver, self._parsed_cookies, target_url=MUSE_DEFAULT_URL)
            time.sleep(2.5)
            _dismiss_muse_popups(driver)

            # Kiểm tra trạng thái đăng nhập
            init_text = _get_muse_recent_text(driver)
            if is_muse_disconnected_text(init_text):
                driver.refresh()
                time.sleep(3.0)
                _dismiss_muse_popups(driver)
                init_text = _get_muse_recent_text(driver)
                if is_muse_disconnected_text(init_text):
                    raise CookieExpiredError("Muse báo 'Đã ngắt kết nối'. Vui lòng dán cookie mới để kết nối lại.")

            if any(b in init_text.lower() for b in ("account suspended", "bị tạm khóa", "tài khoản bị khóa", "account blocked")):
                raise CookieExpiredError("Tài khoản Muse đã bị khóa hoặc chặn.")

            self._driver = driver
            return self._driver
        except CookieExpiredError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ProviderError(f"Lỗi khởi tạo phiên Muse AI: {exc}") from exc

    def text_to_image(
        self,
        prompt: str,
        width: int = 1280,
        height: int = 704,
        seed: Optional[int] = None,
        cache_path: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """Bước 1: Chuẩn bị ảnh nguồn tĩnh nếu cần."""
        ok, msg = self.check_health()
        if not ok:
            raise CookieExpiredError(msg)

        if progress:
            progress(20, "Đang chuẩn bị phiên làm việc Muse AI...")

        if cache_path is None:
            cache_dir = CACHE_DIR / "playlist_t2i"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / f"muse_init_{int(time.time() * 1000)}.png"
        else:
            cache_path = Path(cache_path).resolve()
            cache_path.parent.mkdir(parents=True, exist_ok=True)

        if not cache_path.is_file():
            create_neutral_canvas_image(width, height, cache_path)

        if progress:
            progress(100, "Đã sẵn sàng tạo video với Muse.")

        return cache_path

    def _upload_image(self, driver: Any, source: Path) -> bool:
        """Tải ảnh tham chiếu / thumbnail lên Muse AI."""
        resolved = Path(source).resolve()
        if not resolved.is_file() or resolved.stat().st_size <= 0:
            return False

        inputs = _find(driver, "css selector", "input[type='file']")
        if not inputs:
            _click_by_text(driver, ATTACH_TEXTS)
            time.sleep(1.0)
            inputs = _find(driver, "css selector", "input[type='file']")

        for field in reversed(inputs):
            try:
                accept = str(field.get_attribute("accept") or "").casefold()
                if accept and "image" not in accept and not any(ext in accept for ext in (".png", ".jpg", ".jpeg", ".webp")):
                    continue
                field.send_keys(str(resolved))
                time.sleep(2.0)
                return True
            except Exception:
                continue
        return False

    def image_to_video(
        self,
        image_path: Path,
        prompt: str,
        duration_seconds: int = 5,
        width: int = 1280,
        height: int = 704,
        output_path: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """
        Bước 2: Gửi prompt lên Muse AI qua trình duyệt Chrome có Cookie,
        theo dõi tạo video và tải file .mp4 thật về máy.
        """
        ok, msg = self.check_health()
        if not ok:
            raise CookieExpiredError(msg)

        final_dest = Path(output_path).resolve() if output_path else Path(tempfile.mktemp(prefix="muse_out_", suffix=".mp4", dir=CACHE_DIR))
        final_dest.parent.mkdir(parents=True, exist_ok=True)

        driver = self._ensure_driver(progress=progress, cancelled=cancelled)

        if cancelled and cancelled():
            raise ProviderError("Tác vụ đã bị dừng bởi người dùng.")

        _dismiss_muse_popups(driver)

        # 1. Ghi nhận danh sách video hiện có trước khi gửi prompt
        baseline_vids = _query_muse_videos(driver)
        seen_ids = {v.get("id") for v in baseline_vids if v.get("id")}
        seen_srcs = {v.get("src") for v in baseline_vids if v.get("src")}

        # 2. Nếu có ảnh tham chiếu (thumbnail), tải lên Muse trước
        if image_path and Path(image_path).is_file() and Path(image_path).stat().st_size > 0:
            if progress:
                progress(15, f"Đang tải ảnh '{Path(image_path).name}' lên Muse AI...")
            self._upload_image(driver, Path(image_path))
            time.sleep(2.0)

        # 3. Điền prompt vào ô chat Muse
        if progress:
            progress(20, f"Đang gửi prompt lên Muse AI...")

        filled = fill_muse_chat_prompt(driver, prompt)
        if not filled:
            # Thử refresh nếu trang bị mất kết nối
            if is_muse_disconnected_text(_get_muse_recent_text(driver)):
                driver.refresh()
                time.sleep(3.0)
                _dismiss_muse_popups(driver)
                filled = fill_muse_chat_prompt(driver, prompt)
            if not filled:
                raise ProviderError("Không thể điền prompt vào ô chat Muse AI.")

        time.sleep(1.5)

        # 3. Bấm gửi prompt
        if progress:
            progress(25, "Đang gửi yêu cầu tạo video tới Muse AI...")

        submitted = submit_muse_chat_prompt(driver)
        time.sleep(2.0)

        # Kiểm tra xem có thông báo lỗi hạn mức hoặc tài khoản không
        post_text = _get_muse_recent_text(driver)
        if is_muse_quota_exhausted(post_text):
            raise CookieExpiredError("Tài khoản Muse đã hết lượt tạo video (quota/credit). Hãy dán cookie tài khoản khác.")

        if is_muse_disconnected_text(post_text):
            raise CookieExpiredError("Muse đã ngắt kết nối phiên làm việc. Hãy dán cookie mới để tiếp tục.")

        if any(b in post_text.lower() for b in ("account suspended", "bị tạm khóa", "tài khoản bị khóa", "account blocked")):
            raise CookieExpiredError("Tài khoản Muse đã bị khóa hoặc chặn.")

        # 4. Theo dõi và chờ video hoàn thành (tối đa 300 giây / 5 phút)
        start_wait = time.monotonic()
        timeout_seconds = 300.0
        target_vid: Optional[dict[str, str]] = None

        while time.monotonic() - start_wait < timeout_seconds:
            if cancelled and cancelled():
                raise ProviderError("Tác vụ đã bị dừng bởi người dùng.")

            elapsed = time.monotonic() - start_wait
            pct = min(88, int(25 + (elapsed / 120.0) * 63))
            if progress:
                progress(pct, f"Muse AI đang tạo video ({int(elapsed)}s)...")

            _dismiss_muse_popups(driver)

            # Cuộn nhẹ để DOM cập nhật video mới
            try:
                driver.execute_script("""
                    window.scrollTo(0, document.body.scrollHeight);
                    const w = [...document.querySelectorAll('[data-hatch-video-wrapper],[data-hatch-video-src],video')].pop();
                    if (w) w.scrollIntoView({block: 'center'});
                """)
            except Exception:
                pass

            poll_text = _get_muse_recent_text(driver)
            if is_muse_disconnected_text(poll_text):
                raise CookieExpiredError("Muse báo 'Đã ngắt kết nối'. Vui lòng dán cookie mới.")
            if any(b in poll_text.lower() for b in ("account suspended", "bị tạm khóa", "tài khoản bị khóa", "account blocked")):
                raise CookieExpiredError("Tài khoản Muse đã bị khóa hoặc chặn.")

            current_vids = _query_muse_videos(driver)
            fresh_vids = [
                v for v in current_vids
                if v.get("src")
                and v.get("id") not in seen_ids
                and v.get("src") not in seen_srcs
            ]

            if fresh_vids:
                target_vid = fresh_vids[-1]
                break

            time.sleep(2.0)

        if not target_vid:
            raise ProviderError(f"Hết thời gian chờ Muse AI tạo video (quá {int(timeout_seconds)}s).")

        # 5. Tải file video hoàn thiện về máy
        if progress:
            progress(90, "Muse AI đã hoàn thành video. Đang tải về máy...")

        download_ok = _download_muse_video_file(driver, target_vid, final_dest, self._download_dir)

        # Fallback kiểm tra download folder nếu browser tải về bằng Native Chrome download
        if not download_ok or not final_dest.exists() or final_dest.stat().st_size <= 10240:
            if self._download_dir and self._download_dir.exists():
                st = time.monotonic()
                while time.monotonic() - st < 20.0:
                    candidates = [
                        f for f in self._download_dir.iterdir()
                        if f.is_file() and not f.name.endswith((".crdownload", ".tmp")) and f.stat().st_size > 10240
                    ]
                    if candidates:
                        candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
                        shutil.move(str(candidates[0]), str(final_dest))
                        download_ok = True
                        break
                    time.sleep(1.0)

        if not (final_dest.exists() and final_dest.stat().st_size > 10240):
            raise ProviderError("Không thể tải file video từ Muse AI về máy.")

        # Đánh dấu đã thấy video này để không nhầm với bài tiếp theo
        if target_vid.get("id"):
            seen_ids.add(target_vid["id"])
        if target_vid.get("src"):
            seen_srcs.add(target_vid["src"])

        if progress:
            progress(100, f"Hoàn tất tải video Muse ({final_dest.stat().st_size // 1024} KB).")

        return final_dest
