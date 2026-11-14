"""Durable download queue for the standalone YouTube MP3 application."""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse, urlunparse

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadCancelled as YtdlpDownloadCancelled
except ImportError:
    YoutubeDL = None
    YtdlpDownloadCancelled = Exception

APP_NAME = "YouTubeMp3Downloader"
STATE_VERSION = 3
BROWSER_SESSION_SOURCES = {"auto", "edge", "none", "chrome", "firefox"}
AUTO_COOKIE_BROWSERS = ("firefox", "chrome", "edge")
_YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "www.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


def app_data_dir() -> Path:
    """Return a per-Windows-user folder that survives app upgrades."""
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".config" / APP_NAME


def default_output_dir() -> Path:
    return Path.home() / "Music" / "YouTube MP3"


class StateStore(Protocol):
    def load(self) -> dict[str, Any] | None:
        ...

    def save(self, value: dict[str, Any]) -> bool:
        ...


class AtomicJsonStateStore:
    """Crash-resistant JSON state persisted independently on every machine."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else app_data_dir() / "state.json"
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            try:
                if not self.path.is_file():
                    return None
                with self.path.open("r", encoding="utf-8") as handle:
                    value = json.load(handle)
                return value if isinstance(value, dict) else None
            except (OSError, json.JSONDecodeError):
                return None

    def save(self, value: dict[str, Any]) -> bool:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(value, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                return True
            except OSError:
                return False


class DownloadStopped(Exception):
    """Raised inside yt-dlp when the user requests a stop."""
    pass


class ListingStopped(Exception):
    """Raised when channel/playlist scanning is cancelled by the user."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        super().__init__("Đã dừng lấy video từ YouTube.")
        self.entries = list(entries) if entries else []


class YoutubeSessionRequired(RuntimeError):
    """Raised when YouTube requires a signed-in browser session."""

    def __init__(self, browser_source: str = "auto") -> None:
        if browser_source == "none":
            message = (
                "YouTube yêu cầu xác minh. Hãy chọn Tự động, Firefox, Chrome hoặc Edge "
                "ở mục Phiên YouTube rồi thử lại."
            )
        elif browser_source == "auto":
            message = (
                "YouTube yêu cầu xác minh nhưng chưa đọc được phiên đăng nhập hợp lệ. "
                "Hãy đăng nhập YouTube trong Firefox, Chrome hoặc Edge; nếu vẫn lỗi, "
                "hãy đóng hẳn trình duyệt và chọn đúng trình duyệt ở mục Phiên YouTube."
            )
        else:
            browser_title = browser_source.title()
            message = (
                f"YouTube yêu cầu xác minh qua phiên {browser_title}. Hãy đăng nhập "
                f"YouTube trên {browser_title}, đóng hẳn trình duyệt rồi thử lại."
            )
        super().__init__(message)


class QuietYtdlpLogger:
    """Avoid writes to a missing console in the windowed executable."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


class AuthTrackingYtdlpLogger(QuietYtdlpLogger):
    """Remember auth errors that yt-dlp suppresses with ignoreerrors enabled."""

    def __init__(self) -> None:
        self.auth_error = ""

    def error(self, message: str) -> None:
        if is_youtube_auth_error(RuntimeError(message)):
            self.auth_error = message


def browser_cookie_candidates(browser_source: str = "auto") -> tuple[str, ...]:
    """Return browser cookie stores to try without exporting cookies to disk."""
    source = str(browser_source or "auto").lower()
    if source not in BROWSER_SESSION_SOURCES:
        source = "auto"
    if source == "auto":
        return AUTO_COOKIE_BROWSERS
    if source == "none":
        return ()
    return (source,)


def is_youtube_auth_error(exc: BaseException) -> bool:
    detail = str(exc).lower().replace("’", "'")
    markers = (
        "sign in to confirm",
        "not a bot",
        "login_required",
        "login required",
        "please sign in",
        "use --cookies-from-browser",
        "use --cookies",
    )
    return any(marker in detail for marker in markers)


def is_browser_cookie_error(exc: BaseException) -> bool:
    detail = str(exc).lower()
    markers = (
        "failed to load cookies",
        "failed to decrypt",
        "could not copy",
        "cookie database",
        "cookies database",
        "browser cookies",
        "browser profile",
    )
    return any(marker in detail for marker in markers)


def parse_youtube_urls(text: str | None) -> tuple[list[str], list[str]]:
    """Parse one YouTube URL per line and remove duplicates."""
    urls: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").replace(",", "\n").splitlines():
        url = raw.strip()
        if not url:
            continue
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":", 1)[0]
        if parsed.scheme not in {"http", "https"} or host not in _YOUTUBE_HOSTS:
            invalid.append(url)
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls, invalid


def is_playlist_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/playlist"):
        return True
    return bool(parse_qs(parsed.query).get("list"))


def normalize_youtube_source_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.startswith("/@") and "/" not in path[2:]:
        return urlunparse(parsed._replace(path=f"{path}/videos"))
    return url


def youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.strip("/")
    if host in {"youtu.be", "www.youtu.be"}:
        return path.split("/")[0] if path else None
    if "youtube.com" in host:
        if path == "watch":
            params = parse_qs(parsed.query)
            return params.get("v", [None])[0]
        if path.startswith("shorts/"):
            return path.split("/")[1] if len(path.split("/")) > 1 else None
        if path.startswith("embed/"):
            return path.split("/")[1] if len(path.split("/")) > 1 else None
    return None


def thumbnail_url_from_video_url(url: str) -> str:
    vid = youtube_video_id(url)
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    return ""


def _entry_url(entry: dict[str, Any]) -> str:
    url = str(entry.get("webpage_url") or entry.get("url") or "").strip()
    if url and url.startswith("http"):
        return url
    vid = str(entry.get("id") or "").strip()
    if vid:
        return f"https://www.youtube.com/watch?v=f{vid}"
    return url


def _best_thumbnail_url(entry: dict[str, Any]) -> str:
    thumbnails = entry.get("thumbnails") or []
    candidates = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]
    if candidates:
        best = max(
            candidates,
            key=lambda t: (int(t.get("width") or 0), int(t.get("height") or 0)),
        )
        return str(best["url"])
    video_id = str(entry.get("id") or "").strip()
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


def extract_youtube_entries(
    urls: list[str],
    limit: int = 0,
    browser_source: str = "auto",
    stop_event: threading.Event | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """Scan channels, playlists, or video URLs and return ranked metadata."""
    if not YoutubeDL:
        raise RuntimeError("yt-dlp chưa sẵn sàng.")
    collected: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def ranked_results() -> list[dict[str, Any]]:
        with lock:
            results = list(collected.values())
        results.sort(key=lambda it: int(it.get("view_count") or 0), reverse=True)
        if limit > 0:
            results = results[:limit]
        return results

    def ensure_not_stopped() -> None:
        if stop_event and stop_event.is_set():
            raise ListingStopped(ranked_results())

    def add_entry(entry: dict[str, Any]) -> None:
        canonical_url = _entry_url(entry)
        if not canonical_url:
            return
        title = str(entry.get("title") or "").strip()
        views = int(entry.get("view_count") or 0)
        thumb = _best_thumbnail_url(entry)
        with lock:
            existing = collected.get(canonical_url)
            if existing:
                if not existing.get("title") and title:
                    existing["title"] = title
                if views > int(existing.get("view_count") or 0):
                    existing["view_count"] = views
                if not existing.get("thumbnail_url") and thumb:
                    existing["thumbnail_url"] = thumb
                return
            collected[canonical_url] = {
                "url": canonical_url,
                "title": title,
                "view_count": views,
                "thumbnail_url": thumb,
                "duration": int(entry.get("duration") or 0),
            }
            count = len(collected)
        if on_progress:
            on_progress(count)

    def stop_filter(info_dict: dict[str, Any], *, incomplete: bool = False) -> str | None:
        if stop_event and stop_event.is_set():
            return "stopped by user"
        return None

    def scan(browser_name: str | None) -> None:
        logger = AuthTrackingYtdlpLogger()
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "ignoreerrors": True,
            "logger": logger,
            "match_filter": stop_filter,
        }
        if browser_name:
            options["cookiesfrombrowser"] = (browser_name,)
        js_runtime = resolve_js_runtime()
        if js_runtime:
            runtime_name, runtime_path = js_runtime
            options["js_runtimes"] = {runtime_name: {"path": runtime_path}}

        with YoutubeDL(options) as ydl:
            for source_url in urls:
                ensure_not_stopped()
                info = ydl.extract_info(
                    normalize_youtube_source_url(source_url),
                    download=False,
                )
                ensure_not_stopped()
                if not info:
                    if logger.auth_error:
                        raise RuntimeError(logger.auth_error)
                    continue
                raw_entries = info.get("entries")
                entries = raw_entries if raw_entries is not None else [info]
                for raw_entry in entries:
                    ensure_not_stopped()
                    if isinstance(raw_entry, dict):
                        add_entry(raw_entry)

    candidates = browser_cookie_candidates(browser_source)
    if not candidates:
        scan(None)
        return ranked_results()

    last_auth_error: Exception | None = None
    for candidate in candidates:
        try:
            scan(candidate)
            return ranked_results()
        except ListingStopped:
            raise
        except Exception as exc:
            if is_youtube_auth_error(exc):
                last_auth_error = exc
                continue
            if is_browser_cookie_error(exc):
                continue
            raise

    if last_auth_error:
        raise YoutubeSessionRequired(browser_source)
    # Fallback to no cookies
    scan(None)
    return ranked_results()


def _bundle_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def resolve_ffmpeg_location() -> str | None:
    """Find FFmpeg both in packaged builds and local developer installations."""
    candidates: list[Path] = []
    bundle_root = _bundle_root()
    candidates.append(bundle_root / "_internal" / "tools" / "ffmpeg")
    candidates.append(bundle_root / "tools" / "ffmpeg")
    candidates.append(bundle_root / "_internal")

    # In standalone installation:
    installed_app_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / APP_NAME / "_internal" / "tools" / "ffmpeg"
    if installed_app_root.is_dir():
        candidates.append(installed_app_root)

    for directory in candidates:
        executable = directory / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
        if executable.is_file():
            return str(directory)

    located = shutil.which("ffmpeg")
    return str(Path(located).parent) if located else None


def resolve_js_runtime() -> tuple[str, str] | None:
    """Use an installed JS runtime when available for newer YouTube players."""
    candidates = ["deno", "node", "bun"]
    bundle_node = _bundle_root() / "_internal" / "tools" / "node" / "node.exe"
    if bundle_node.is_file():
        return "node", str(bundle_node)

    for name in candidates:
        executable = shutil.which(name)
        if executable:
            return name, executable
    return None


def safe_error_message(exc: BaseException) -> str:
    detail = str(exc).replace("\n", " ").strip()
    lowered = detail.lower()
    if is_youtube_auth_error(exc):
        return (
            "YouTube yêu cầu xác minh. Hãy chọn đúng trình duyệt tại mục "
            "'Phiên YouTube' và đảm bảo đã đăng nhập YouTube trên trình duyệt đó."
        )
    if "ffmpeg" in lowered and ("not found" in lowered or "not installed" in lowered):
        return "Không tìm thấy FFmpeg để chuyển đổi sang MP3. Hãy cài lại tool."
    return detail[:400] or exc.__class__.__name__


class YoutubeMp3DownloadController:
    """One durable background download queue for the application."""

    def __init__(
        self,
        state_store: StateStore | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.state_store = state_store or AtomicJsonStateStore()
        self.on_change = on_change
        self.urls: list[str] = []
        self.items: dict[str, dict[str, Any]] = {}
        self.output_dir = default_output_dir()
        self.audio_quality = "192"
        self.browser_source = "auto"
        self.status_text = "Dán link YouTube rồi bấm Tải MP3."
        self.version = 0
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._last_saved_progress: dict[str, int] = {}
        self._active_cookie_browser: str | None = None
        self._restore_state()

    def _default_item(self, url: str) -> dict[str, Any]:
        return {
            "url": url,
            "selected": True,
            "status": "pending",
            "progress": 0,
            "title": "",
            "view_count": 0,
            "thumbnail_url": thumbnail_url_from_video_url(url),
            "output_path": "",
            "error": "",
        }

    def is_running(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": self.version,
                "urls": list(self.urls),
                "items": {url: dict(item) for url, item in self.items.items()},
                "output_dir": str(self.output_dir),
                "audio_quality": self.audio_quality,
                "browser_source": self.browser_source,
                "status_text": self.status_text,
                "running": self.is_running(),
            }

    def _state_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state_version": STATE_VERSION,
                "urls": list(self.urls),
                "items": {url: dict(item) for url, item in self.items.items()},
                "output_dir": str(self.output_dir),
                "audio_quality": self.audio_quality,
                "browser_source": self.browser_source,
            }

    def _save_state(self) -> bool:
        return self.state_store.save(self._state_payload())

    def _changed(self, persist: bool = True) -> None:
        with self._lock:
            if persist:
                self._save_state()
            self.version += 1
            callback = self.on_change
        if callback:
            try:
                callback()
            except Exception:
                pass

    def _restore_state(self) -> None:
        state = self.state_store.load() or {}
        saved_urls = state.get("urls") or []
        self.urls = [url for url in saved_urls if isinstance(url, str)]
        saved_items = state.get("items") or {}
        self.items = {
            url: {**self._default_item(url), **dict(saved_items.get(url) or {})}
            for url in self.urls
        }
        output_dir = state.get("output_dir")
        if output_dir:
            self.output_dir = Path(output_dir)
        self.audio_quality = str(state.get("audio_quality") or "192")
        self.browser_source = str(state.get("browser_source") or "auto")
        if self.browser_source not in BROWSER_SESSION_SOURCES:
            self.browser_source = "auto"
        # Reset any interrupted downloading state to pending
        for item in self.items.values():
            if item.get("status") == "downloading":
                item["status"] = "pending"
                item["progress"] = 0

    def update_urls(self, urls: list[str]) -> None:
        with self._lock:
            self.urls = list(urls)
            self.items = {
                url: self.items.get(url) or self._default_item(url)
                for url in self.urls
            }
        self._changed(persist=True)

    def update_entries(self, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            new_urls: list[str] = []
            for entry in entries:
                url = _entry_url(entry)
                if not url:
                    continue
                new_urls.append(url)
                item = self.items.get(url) or self._default_item(url)
                item["title"] = str(entry.get("title") or item.get("title") or "").strip()
                item["view_count"] = int(entry.get("view_count") or item.get("view_count") or 0)
                thumb = str(entry.get("thumbnail_url") or item.get("thumbnail_url") or "").strip()
                if thumb:
                    item["thumbnail_url"] = thumb
                item["duration"] = int(entry.get("duration") or item.get("duration") or 0)
                self.items[url] = item
            self.urls = new_urls
        self._changed(persist=True)

    def set_selected_urls(self, selected_urls: set[str]) -> None:
        with self._lock:
            for url, item in self.items.items():
                item["selected"] = url in selected_urls
        self._changed(persist=True)

    def set_output_dir(self, output_dir: Path | str) -> None:
        with self._lock:
            self.output_dir = Path(output_dir)
        self._changed(persist=True)

    def set_browser_source(self, source: str) -> None:
        with self._lock:
            self.browser_source = source if source in BROWSER_SESSION_SOURCES else "auto"
            self._active_cookie_browser = None
        self._changed(persist=True)

    def start(self) -> None:
        if not YoutubeDL:
            raise RuntimeError("yt-dlp chưa được cài đặt trong môi trường.")
        ffmpeg_location = resolve_ffmpeg_location()
        if not ffmpeg_location:
            raise RuntimeError(
                "Không tìm thấy FFmpeg để chuyển đổi sang file MP3. "
                "Hãy kiểm tra thư mục cài đặt hoặc cài FFmpeg."
            )
        with self._lock:
            if self.is_running():
                return
            selected_urls = [
                url for url in self.urls if self.items.get(url, {}).get("selected", True)
            ]
            if not selected_urls:
                raise RuntimeError("Chưa chọn video nào để tải.")
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run_loop,
                args=(ffmpeg_location, selected_urls),
                daemon=True,
            )
            self._worker.start()

    def request_stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self.is_running():
                self.status_text = "Đang dừng tải..."
        self._changed(persist=False)

    def wait(self, timeout: float | None = None) -> None:
        worker = self._worker
        if worker:
            worker.join(timeout=timeout)

    def _run_loop(self, ffmpeg_location: str, selected_urls: list[str]) -> None:
        stopped = False
        try:
            for url in selected_urls:
                if self._stop_event.is_set():
                    stopped = True
                    break
                with self._lock:
                    item = self.items[url]
                    if item.get("status") == "successful":
                        continue
                    item.update(status="downloading", progress=0, error="")
                    self.status_text = f"Đang tải: {url}"
                self._changed(persist=False)

                try:
                    result = self._download_one(url, ffmpeg_location)
                    with self._lock:
                        self.items[url].update(
                            status="successful",
                            progress=100,
                            title=result.get("title") or self.items[url].get("title", ""),
                            output_path=result.get("output_path") or "",
                            error="",
                        )
                    self._changed(persist=True)
                except DownloadStopped:
                    stopped = True
                    with self._lock:
                        self.items[url].update(status="pending", progress=0)
                    self._changed(persist=True)
                    break
                except Exception as exc:
                    err_msg = safe_error_message(exc)
                    with self._lock:
                        self.items[url].update(status="error", progress=0, error=err_msg)
                    self._changed(persist=True)
                    if is_youtube_auth_error(exc):
                        break
        finally:
            with self._lock:
                if stopped:
                    self.status_text = "Đã dừng. Bấm Tải MP3 để tiếp tục các link còn lại."
                elif all(self.items.get(u, {}).get("status") == "successful" for u in selected_urls):
                    self.status_text = f"Đã tải xong {len(selected_urls)} video đã chọn."
                else:
                    self.status_text = "Đã hoàn tất; các link lỗi có thể tải lại."
            self._changed(persist=True)

    def _record_progress(self, url: str, downloaded: int, total: int) -> None:
        with self._lock:
            item = self.items.get(url)
            if not item or item.get("status") != "downloading":
                return
            percent = min(99, int((downloaded * 100) / total)) if total else 0
            item["progress"] = percent
            last_saved = self._last_saved_progress.get(url, -5)
            should_persist = (percent - last_saved) >= 5
            if should_persist:
                self._last_saved_progress[url] = percent
        self._changed(persist=should_persist)

    def _progress_hook(self, url: str, data: dict[str, Any]) -> None:
        if self._stop_event.is_set():
            raise DownloadStopped()
        if data.get("status") != "downloading":
            return
        downloaded = int(data.get("downloaded_bytes") or 0)
        total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
        self._record_progress(url, downloaded, total)

    def _download_one(self, url: str, ffmpeg_location: str) -> dict[str, str]:
        attempted_browsers: set[str] = set()
        active_browser = self._active_cookie_browser
        allowed_browsers = browser_cookie_candidates(self.browser_source)
        if active_browser and active_browser in allowed_browsers:
            attempted_browsers.add(active_browser)
            try:
                return self._download_one_attempt(url, ffmpeg_location, active_browser)
            except DownloadStopped:
                raise
            except Exception as exc:
                if not is_youtube_auth_error(exc) and not is_browser_cookie_error(exc):
                    raise
                self._active_cookie_browser = None

        if self.browser_source == "auto" or not allowed_browsers:
            try:
                return self._download_one_attempt(url, ffmpeg_location, None)
            except DownloadStopped:
                raise
            except Exception as exc:
                if not is_youtube_auth_error(exc):
                    raise

        last_auth_error: Exception | None = None
        for browser_name in allowed_browsers:
            if browser_name in attempted_browsers:
                continue
            try:
                result = self._download_one_attempt(url, ffmpeg_location, browser_name)
                self._active_cookie_browser = browser_name
                return result
            except DownloadStopped:
                raise
            except Exception as exc:
                if is_youtube_auth_error(exc):
                    last_auth_error = exc
                    continue
                if is_browser_cookie_error(exc):
                    continue
                raise

        if last_auth_error:
            raise YoutubeSessionRequired(self.browser_source)
        return self._download_one_attempt(url, ffmpeg_location, None)

    def _download_one_attempt(
        self,
        url: str,
        ffmpeg_location: str,
        browser_name: str | None,
    ) -> dict[str, str]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "outtmpl": str(self.output_dir / "%(title)s.%(ext)s"),
            "ffmpeg_location": ffmpeg_location,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": self.audio_quality,
                }
            ],
            "quiet": True,
            "no_warnings": True,
            "logger": QuietYtdlpLogger(),
            "progress_hooks": [lambda data: self._progress_hook(url, data)],
        }
        if browser_name:
            options["cookiesfrombrowser"] = (browser_name,)
        js_runtime = resolve_js_runtime()
        if js_runtime:
            runtime_name, runtime_path = js_runtime
            options["js_runtimes"] = {runtime_name: {"path": runtime_path}}

        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            if self._stop_event.is_set():
                raise DownloadStopped()
            if not info:
                raise RuntimeError("yt-dlp không trả về thông tin video.")
            source_path = Path(ydl.prepare_filename(info))
            return {
                "title": str(info.get("title") or ""),
                "output_path": str(source_path.with_suffix(".mp3")),
            }


def standalone_smoke_test() -> list[str]:
    problems: list[str] = []
    if not YoutubeDL:
        problems.append("yt-dlp missing")
    if not resolve_ffmpeg_location():
        problems.append("ffmpeg missing")
    return problems

