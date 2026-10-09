from __future__ import annotations

import base64
import hashlib
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import requests

from auth.muse_login import (
    CLICKABLE_SELECTOR,
    MUSE_ALLOWED_HOSTS,
    MUSE_APP_SELECTORS,
    MUSE_START_URL,
    MuseAccount,
    MuseAccountStore,
    MuseLoginService,
    create_muse_chrome_driver,
    validate_muse_start_url,
)
from utils.paths import CACHE_DIR


PROMPT_SELECTORS = (
    "textarea",
    "[contenteditable='true'][role='textbox']",
    "[contenteditable='true'][aria-label*='message' i]",
    "[contenteditable='true'][aria-label*='prompt' i]",
)
SUBMIT_TEXTS = ("send", "generate", "create video", "create")
DOWNLOAD_TEXTS = ("download", "download video", "tải xuống", "tải video")
ATTACH_TEXTS = ("attach", "upload", "add image", "thêm ảnh", "tải ảnh lên")
QUOTA_MARKERS = (
    "usage limit reached",
    "you've reached your limit",
    "you have reached your limit",
    "not enough credits",
    "insufficient credits",
    "out of credits",
    "weekly limit reached",
    "upgrade to continue",
)


class MuseGenerationError(RuntimeError):
    pass


class MuseGenerationCancelled(MuseGenerationError):
    pass


class MuseGenerationTimeout(MuseGenerationError):
    pass


class MuseGenerationAuthError(MuseGenerationError):
    pass


class MuseGenerationQuotaError(MuseGenerationError):
    pass


DriverFactory = Callable[[Path, Path], Any]
ProgressCallback = Callable[[int, str], None]


class MuseGenerationService:
    """Submit one Muse chat generation using one explicitly selected Chrome profile."""

    def __init__(
        self,
        *,
        start_url: str = MUSE_START_URL,
        store: MuseAccountStore | None = None,
        driver_factory: DriverFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        poll_interval: float = 1.0,
    ):
        self.start_url = validate_muse_start_url(start_url)
        self.store = store or MuseAccountStore()
        self._driver_factory = driver_factory or create_muse_chrome_driver
        self._clock = clock
        self._sleep = sleeper
        self.poll_interval = max(0.1, float(poll_interval))

    def generate(
        self,
        account_id: str,
        prompt: str,
        image_path: str | Path,
        output_path: str | Path,
        *,
        timeout_seconds: float = 15 * 60,
        cancelled: Callable[[], bool] | None = None,
        progress: ProgressCallback | None = None,
    ) -> Path:
        cancelled = cancelled or (lambda: False)
        progress = progress or (lambda _value, _message: None)
        account = self.store.get(str(account_id or ""))
        if not account:
            raise MuseGenerationAuthError("Không tìm thấy tài khoản Muse đã chọn.")
        if account.status != "connected":
            raise MuseGenerationAuthError("Tài khoản Muse chưa được xác nhận đăng nhập. Hãy đăng nhập Muse trước.")
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("Prompt Muse không được để trống.")
        source = Path(image_path).expanduser().resolve() if image_path else None
        if source is not None and not source.is_file():
            raise ValueError("Không tìm thấy ảnh đầu vào cho Muse.")
        target = Path(output_path).expanduser().resolve()
        if target.exists():
            raise ValueError("File output Muse đã tồn tại; tool không tự ghi đè.")
        target.parent.mkdir(parents=True, exist_ok=True)
        cache_root = CACHE_DIR / "MuseDownloads"
        cache_root.mkdir(parents=True, exist_ok=True)
        download_dir = Path(tempfile.mkdtemp(prefix="job_", dir=cache_root))
        profile = Path(account.profile_dir).resolve()
        profile_key = str(profile).casefold()
        MuseLoginService._acquire_profile(profile_key)
        driver = None
        owned_handles: set[str] = set()
        known_handles: set[str] = set()
        started = self._clock()
        try:
            self._check_stop(started, timeout_seconds, cancelled)
            progress(2, "Đang mở đúng Chrome profile Muse đã chọn…")
            try:
                driver = self._driver_factory(profile, download_dir)
                known_handles.update(str(item) for item in driver.window_handles)
                owned_handles.add(str(driver.current_window_handle))
                driver.set_page_load_timeout(30)
                driver.get(self.start_url)
            except Exception:
                raise MuseGenerationError(
                    "Không thể mở Chrome profile Muse. Hãy đóng cửa sổ đang dùng cùng profile rồi thử lại."
                ) from None
            self._wait_for_muse_main(driver, started, timeout_seconds, cancelled)
            baseline = self._video_fingerprints(driver)
            if source is not None:
                progress(8, f"Đang tải ảnh {source.name} lên Muse…")
                self._upload_image(driver, source)
            progress(12, "Đang điền prompt vào Muse…")
            self._fill_prompt(driver, prompt)
            progress(15, "Đang gửi yêu cầu tạo video tới Muse…")
            self._submit(driver)
            video = self._wait_for_new_video(
                driver,
                baseline,
                started,
                timeout_seconds,
                cancelled,
                progress,
            )
            progress(90, "Muse đã tạo video; đang yêu cầu tải xuống…")
            if not self._click_download(driver):
                self._download_video_element(driver, video)
            downloaded = self._wait_for_download(
                download_dir,
                started,
                timeout_seconds,
                cancelled,
                progress,
            )
            shutil.move(str(downloaded), str(target))
            progress(100, f"Đã tải video Muse: {target.name}")
            return target
        except MuseGenerationAuthError:
            account.status = "manual_required"
            self.store.save(account)
            raise
        except (MuseGenerationCancelled, MuseGenerationTimeout, MuseGenerationQuotaError):
            raise
        except MuseGenerationError:
            raise
        except Exception:
            # Selenium exceptions can contain current URLs/session identifiers.
            raise MuseGenerationError(
                "Giao diện Muse đã thay đổi hoặc không phản hồi; không có cookie/token nào được ghi log."
            ) from None
        finally:
            if driver is not None:
                try:
                    owned_handles.update({str(item) for item in driver.window_handles} - known_handles)
                except Exception:
                    pass
                _close_owned_driver(driver, owned_handles)
            MuseLoginService._release_profile(profile_key)
            shutil.rmtree(download_dir, ignore_errors=True)

    def _wait_for_muse_main(self, driver: Any, started: float, timeout: float, cancelled) -> None:
        deadline = min(timeout, 45.0)
        while self._clock() - started < deadline:
            self._check_stop(started, timeout, cancelled)
            host = _hostname(_safe_url(driver))
            if host not in MUSE_ALLOWED_HOSTS:
                raise MuseGenerationError("Muse chuyển tới hostname không được phép; tác vụ đã dừng.")
            if host in {"auth.muse.ai", "accounts.google.com"}:
                raise MuseGenerationAuthError("Phiên Muse đã hết hạn. Hãy đăng nhập lại trong trang Đăng nhập Muse AI.")
            if host == "muse.ai" and _has_muse_main_marker(driver):
                return
            self._sleep(self.poll_interval)
        raise MuseGenerationAuthError("Không xác nhận được giao diện chính Muse bằng profile đã chọn.")

    def _upload_image(self, driver: Any, source: Path) -> None:
        inputs = _find(driver, "css selector", "input[type='file']")
        if not inputs:
            _click_by_text(driver, ATTACH_TEXTS)
            inputs = _find(driver, "css selector", "input[type='file']")
        for field in reversed(inputs):
            try:
                accept = str(field.get_attribute("accept") or "").casefold()
                if accept and "image" not in accept and not any(ext in accept for ext in (".png", ".jpg", ".jpeg", ".webp")):
                    continue
                field.send_keys(str(source))
                return
            except Exception:
                continue
        raise MuseGenerationError("Không tìm thấy ô tải ảnh ổn định trên giao diện Muse.")

    def _fill_prompt(self, driver: Any, prompt: str) -> None:
        for selector in PROMPT_SELECTORS:
            for field in reversed(_find(driver, "css selector", selector)):
                if not _visible(field):
                    continue
                try:
                    field.click()
                    tag = str(field.tag_name or "").casefold()
                    if tag == "textarea":
                        field.clear()
                    else:
                        field.send_keys("\ue009", "a")
                    field.send_keys(prompt)
                    return
                except Exception:
                    continue
        raise MuseGenerationError("Không tìm thấy ô nhập prompt ổn định trên Muse.")

    def _submit(self, driver: Any) -> None:
        if _click_by_text(driver, SUBMIT_TEXTS):
            return
        for button in reversed(_find(driver, "css selector", "button[type='submit']")):
            if _clickable(button):
                button.click()
                return
        raise MuseGenerationError("Không tìm thấy nút gửi/tạo video trên Muse.")

    def _wait_for_new_video(self, driver, baseline, started, timeout, cancelled, progress):
        while True:
            self._check_stop(started, timeout, cancelled)
            host = _hostname(_safe_url(driver))
            if host != "muse.ai":
                if host in {"auth.muse.ai", "accounts.google.com"}:
                    raise MuseGenerationAuthError("Phiên Muse hết hạn trong lúc tạo video. Hãy đăng nhập lại.")
                raise MuseGenerationError("Muse chuyển tới hostname không được phép; tác vụ đã dừng.")
            body = _body_text(driver).casefold()
            if any(marker in body for marker in QUOTA_MARKERS):
                raise MuseGenerationQuotaError("Muse đã hết credit/quota hoặc chạm usage limit; batch đã dừng.")
            for video in reversed(_find(driver, "css selector", "video")):
                if _visible(video) and _video_fingerprint(video) not in baseline:
                    return video
            elapsed = self._clock() - started
            percent = min(86, 18 + round(elapsed / max(1.0, timeout) * 68))
            progress(percent, "Muse đang tạo video…")
            self._sleep(self.poll_interval)

    def _click_download(self, driver: Any) -> bool:
        return _click_by_text(driver, DOWNLOAD_TEXTS, reverse=True)

    @staticmethod
    def _download_video_element(driver: Any, video: Any) -> None:
        try:
            driver.execute_script(
                "const v=arguments[0],a=document.createElement('a');"
                "a.href=v.currentSrc||v.src;a.download='muse-video.mp4';"
                "document.body.appendChild(a);a.click();a.remove();",
                video,
            )
        except Exception:
            raise MuseGenerationError("Muse đã tạo video nhưng không tìm thấy nút tải xuống.") from None

    def _wait_for_download(self, folder, started, timeout, cancelled, progress) -> Path:
        previous: tuple[Path, int] | None = None
        stable_polls = 0
        while True:
            self._check_stop(started, timeout, cancelled)
            candidates = [
                item for item in Path(folder).iterdir()
                if item.is_file() and item.suffix.casefold() in {".mp4", ".mov", ".webm"}
                and not item.name.casefold().endswith((".crdownload", ".tmp"))
            ]
            if candidates:
                newest = max(candidates, key=lambda item: item.stat().st_mtime_ns)
                current = (newest, newest.stat().st_size)
                stable_polls = stable_polls + 1 if current == previous and current[1] > 0 else 0
                previous = current
                if stable_polls >= 2:
                    return newest
            progress(94, "Đang chờ Chrome tải video Muse hoàn tất…")
            self._sleep(self.poll_interval)

    @staticmethod
    def _video_fingerprints(driver: Any) -> set[str]:
        return {_video_fingerprint(item) for item in _find(driver, "css selector", "video")}

    def _check_stop(self, started: float, timeout: float, cancelled) -> None:
        if cancelled():
            raise MuseGenerationCancelled("Đã hủy tác vụ Muse.")
        if self._clock() - started >= max(1.0, float(timeout)):
            raise MuseGenerationTimeout("Muse tạo/tải video quá thời gian chờ.")


def _has_muse_main_marker(driver: Any) -> bool:
    return any(_visible(item) for selector in MUSE_APP_SELECTORS for item in _find(driver, "css selector", selector))


def _click_by_text(driver: Any, texts: tuple[str, ...], *, reverse: bool = False) -> bool:
    accepted = {value.casefold() for value in texts}
    elements = _find(driver, "css selector", CLICKABLE_SELECTOR)
    iterator = reversed(elements) if reverse else iter(elements)
    for element in iterator:
        if not _clickable(element):
            continue
        labels = (
            _text(element),
            _attr(element, "aria-label"),
            _attr(element, "title"),
        )
        normalized = {" ".join(label.split()).casefold() for label in labels if label}
        if any(label == value or label.startswith(value + " ") for label in normalized for value in accepted):
            try:
                element.click()
                return True
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", element)
                    return True
                except Exception:
                    continue
    return False


def _dismiss_muse_popups(driver: Any) -> None:
    """Dong hop thoai/dialog/pop-up/overlay tren trang Muse tuong tu co che cua MuseStudio."""
    try:
        driver.execute_script("""
            for (const d of document.querySelectorAll('[role=dialog],[role=alertdialog],[data-slot=dialog-overlay],[data-state=open]')) {
                const b = [...d.querySelectorAll('button')].find(x => 
                    /đóng|close|bỏ qua|skip|để sau|not now|ok|đã hiểu|got it/i.test(
                        (x.getAttribute('aria-label') || '') + ' ' + (x.innerText || '')
                    )
                );
                if (b) {
                    try { b.click(); } catch(e) {}
                }
            }
        """)
    except Exception:
        pass
    try:
        from selenium.webdriver.common.keys import Keys
        body = driver.find_element("css selector", "body")
        body.send_keys(Keys.ESCAPE)
    except Exception:
        pass


def _query_muse_videos(driver: Any) -> list[dict[str, str]]:
    """Trich xuat danh sach video tu DOM trang Muse theo co che chuan cua MuseStudio."""
    try:
        return driver.execute_script("""
            window.__mavSeq = window.__mavSeq || 0;
            const results = [];
            // 1. Selector uu tien [data-hatch-video-src] cua Muse AI
            document.querySelectorAll('[data-hatch-video-src]').forEach(w => {
                if (!w.dataset.mavId) w.dataset.mavId = String(++window.__mavSeq);
                const label = (w.getAttribute('aria-label') || '').trim();
                const m = label.match(/[^\\s/\\\\]+\\.(mp4|webm|mov)/i);
                const poster = (w.getAttribute('data-hatch-video-poster') || '').split('?')[0];
                const src = (w.getAttribute('data-hatch-video-src') || '').trim();
                if (src) {
                    results.push({ id: w.dataset.mavId, src: src, name: m ? m[0] : '', label: label, poster: poster });
                }
            });
            // 2. The <video> thong thuong tren trang
            document.querySelectorAll('video').forEach(v => {
                const src = (v.currentSrc || v.src || '').trim();
                if (src && !results.some(r => r.src === src)) {
                    if (!v.dataset.mavId) v.dataset.mavId = 'vid_' + (++window.__mavSeq);
                    const parent = v.closest('[aria-label]');
                    const parentLabel = (parent && parent.getAttribute) ? (parent.getAttribute('aria-label') || '') : '';
                    const m = parentLabel.match(/[^\\s/\\\\]+\\.(mp4|webm|mov)/i);
                    results.push({ id: v.dataset.mavId, src: src, name: m ? m[0] : '', label: parentLabel, poster: '' });
                }
            });
            return results;
        """) or []
    except Exception:
        return []


def _download_muse_video_file(
    driver: Any,
    video_info: dict[str, str],
    target_dest: Path,
    download_dir: Path | None = None,
) -> bool:
    """Tai file video Muse ve may bang da tang: JS Blob Fetch (chuan MuseStudio) -> Requests CDN -> Dom Link -> UI Click."""
    target_dest.parent.mkdir(parents=True, exist_ok=True)
    video_src = (video_info.get("src") or "").strip()
    if not video_src:
        return False

    # Tang 1: Fetch Base64 truc tiep qua JavaScript trong session cua browser (Cực kỳ mạnh, không lo overlay hay download bar)
    try:
        b64_data = driver.execute_async_script("""
            var url = arguments[0];
            var done = arguments[arguments.length - 1];
            fetch(url)
                .then(function(res) { return res.blob(); })
                .then(function(blob) {
                    var reader = new FileReader();
                    reader.onload = function() { done(String(reader.result).split(',')[1]); };
                    reader.onerror = function() { done(null); };
                    reader.readAsDataURL(blob);
                })
                .catch(function(err) { done(null); });
        """, video_src)
        if b64_data:
            import base64
            raw_bytes = base64.b64decode(b64_data)
            if len(raw_bytes) > 10240:
                with open(target_dest, "wb") as f:
                    f.write(raw_bytes)
                return True
    except Exception:
        pass

    # Tang 2: Direct HTTP download voi cookies cua browser neu la link https
    if video_src.startswith("http"):
        try:
            import requests
            session = requests.Session()
            for cookie in driver.get_cookies():
                session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""))
            resp = session.get(video_src, stream=True, timeout=60)
            if resp.status_code == 200 and len(resp.content) > 10240:
                with open(target_dest, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception:
            pass

    # Tang 3: DOM <a download> trigger (tao the a roi click trong DOM)
    try:
        driver.execute_script("""
            var a = document.createElement('a');
            a.href = arguments[0];
            a.download = arguments[1];
            document.body.appendChild(a);
            a.click();
            a.remove();
        """, video_src, target_dest.name)
        if download_dir and download_dir.exists():
            import time
            st = time.monotonic()
            while time.monotonic() - st < 25.0:
                time.sleep(1.0)
                candidates = [
                    f for f in download_dir.iterdir()
                    if f.is_file() and not f.name.endswith((".crdownload", ".tmp")) and f.stat().st_size > 10240
                ]
                if candidates:
                    newest = max(candidates, key=lambda x: x.stat().st_mtime)
                    import shutil
                    shutil.move(str(newest), str(target_dest))
                    return True
    except Exception:
        pass

    # Tang 4: UI Download Button click bang JS (bo qua overlay)
    try:
        _dismiss_muse_popups(driver)
        buttons = _find(driver, "css selector", "button[aria-label*='download' i], button[data-testid*='download' i], [role='button'][aria-label*='download' i], a[download]")
        for b in reversed(buttons):
            try:
                driver.execute_script("arguments[0].click();", b)
            except Exception:
                pass
        if download_dir and download_dir.exists():
            import time
            st = time.monotonic()
            while time.monotonic() - st < 25.0:
                time.sleep(1.0)
                candidates = [
                    f for f in download_dir.iterdir()
                    if f.is_file() and not f.name.endswith((".crdownload", ".tmp")) and f.stat().st_size > 10240
                ]
                if candidates:
                    newest = max(candidates, key=lambda x: x.stat().st_mtime)
                    import shutil
                    shutil.move(str(newest), str(target_dest))
                    return True
    except Exception:
        pass

    return False


def _video_fingerprint(element: Any) -> str:
    stable_media = _attr(element, "currentSrc") or _attr(element, "src") or _attr(element, "poster")
    # WebElement ids change whenever the DOM reloads, so use one only when Muse
    # has not exposed any media URL yet. Persisted baselines must survive reload.
    raw = stable_media or str(getattr(element, "id", ""))
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _body_text(driver: Any) -> str:
    return " ".join(_text(item) for item in _find(driver, "tag name", "body"))


def _find(driver: Any, by: str, selector: str) -> list[Any]:
    try:
        return list(driver.find_elements(by, selector))
    except Exception:
        return []


def _text(element: Any) -> str:
    try:
        return str(element.text or "")
    except Exception:
        return ""


def _attr(element: Any, name: str) -> str:
    try:
        return str(element.get_attribute(name) or "")
    except Exception:
        return ""


def _visible(element: Any) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:
        return False


def _clickable(element: Any) -> bool:
    try:
        return bool(element.is_displayed() and element.is_enabled())
    except Exception:
        return False


def _safe_url(driver: Any) -> str:
    try:
        return str(driver.current_url or "")
    except Exception:
        return ""


def _hostname(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").casefold().rstrip(".")


def _close_owned_driver(driver: Any, owned_handles: set[str]) -> None:
    for handle in list(owned_handles):
        try:
            if handle in set(driver.window_handles):
                driver.switch_to.window(handle)
                driver.close()
        except Exception:
            continue
    try:
        if not driver.window_handles:
            driver.quit()
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
