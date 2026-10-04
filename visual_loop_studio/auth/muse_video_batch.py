from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import threading
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from auth.muse_generation import QUOTA_MARKERS, _attr, _click_by_text, _find, _hostname, _safe_url, _text, _video_fingerprint, _visible
from auth.muse_login import MUSE_ALLOWED_HOSTS, MUSE_START_URL
from auth.muse_sessions import (
    MUSE_SESSION_COUNT,
    MUSE_SESSION_DOWNLOADS_DIR,
    MuseSession,
    MuseSessionManager,
    MuseSessionState,
    get_muse_session_manager,
)
from utils.config import read_json, write_json
from utils.paths import DATA_DIR


MUSE_VIDEO_BATCH_CHECKPOINT = DATA_DIR / "muse_video_batch.json"
SUPPORTED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
TRANSIENT_NETWORK_MARKERS = (
    "network error",
    "connection lost",
    "temporarily unavailable",
    "please try again",
    "something went wrong",
)


class MuseVideoJobState(str, Enum):
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    READY_TO_GENERATE = "READY_TO_GENERATE"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    GENERATING = "GENERATING"
    DOWNLOADING = "DOWNLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class MuseVideoWorkerState(str, Enum):
    IDLE = "IDLE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class MuseVideoBatchError(RuntimeError):
    """A safe batch error which must not expose browser session details."""


class MuseVideoStopped(MuseVideoBatchError):
    pass


class MuseVideoLoginRequired(MuseVideoBatchError):
    pass


class MuseVideoQuotaExhausted(MuseVideoBatchError):
    pass


class MuseVideoTimeout(MuseVideoBatchError):
    pass


class MuseVideoDownloadError(MuseVideoBatchError):
    pass


class MuseVideoDriverError(MuseVideoBatchError):
    pass


@dataclass(frozen=True)
class MuseVideoSelectors:
    """Stable-first Muse selectors kept together for DOM maintenance."""

    new_task: tuple[str, ...] = (
        "button[data-testid*='new' i]",
        "[role='button'][aria-label*='new video' i]",
        "[role='button'][aria-label*='create video' i]",
    )
    upload_inputs: tuple[str, ...] = (
        "input[type='file'][accept*='image' i]",
        "input[type='file'][accept*='.png' i]",
        "input[type='file']",
    )
    upload_buttons: tuple[str, ...] = (
        "button[data-testid*='upload' i]",
        "button[aria-label*='upload' i]",
        "[role='button'][aria-label*='attach' i]",
    )
    previews: tuple[str, ...] = (
        "[data-testid*='preview' i] img",
        "[data-testid*='attachment' i] img",
        "img[alt*='preview' i]",
        "img[src^='blob:']",
    )
    prompt_inputs: tuple[str, ...] = (
        "[data-testid='prompt-input']",
        "[data-testid*='composer' i] textarea",
        "textarea[aria-label*='prompt' i]",
        "textarea[placeholder*='prompt' i]",
        "[contenteditable='true'][role='textbox'][aria-label*='prompt' i]",
        "textarea",
        "[contenteditable='true'][role='textbox']",
    )
    generate_buttons: tuple[str, ...] = (
        "button[data-testid*='generate' i]",
        "button[data-testid*='create' i]",
        "button[aria-label*='generate' i]",
        "button[aria-label*='create video' i]",
        "button[type='submit']",
    )
    processing: tuple[str, ...] = (
        "[data-testid*='processing' i]",
        "[data-testid*='progress' i]",
        "[aria-busy='true']",
        "button[aria-label*='stop generating' i]",
    )
    video_results: tuple[str, ...] = (
        "[data-testid*='video-result' i] video",
        "[data-testid*='result' i] video",
        "video",
    )
    download_buttons: tuple[str, ...] = (
        "button[data-testid*='download' i]",
        "button[aria-label*='download' i]",
        "[role='button'][aria-label*='download' i]",
    )
    model_controls: tuple[str, ...] = (
        "select[data-testid*='model' i]",
        "[role='combobox'][aria-label*='model' i]",
    )
    aspect_controls: tuple[str, ...] = (
        "select[data-testid*='aspect' i]",
        "[role='combobox'][aria-label*='aspect' i]",
        "[role='combobox'][aria-label*='ratio' i]",
    )
    duration_controls: tuple[str, ...] = (
        "select[data-testid*='duration' i]",
        "[role='combobox'][aria-label*='duration' i]",
        "[role='combobox'][aria-label*='length' i]",
    )
    resolution_controls: tuple[str, ...] = (
        "select[data-testid*='resolution' i]",
        "[role='combobox'][aria-label*='resolution' i]",
        "[role='combobox'][aria-label*='quality' i]",
    )


@dataclass(frozen=True)
class MuseVideoSettings:
    model: str = ""
    aspect_ratio: str = ""
    duration: str = ""
    resolution: str = ""
    quantity: int = 1

    def normalized(self) -> "MuseVideoSettings":
        quantity = int(self.quantity or 1)
        if quantity != 1:
            raise ValueError("Batch Muse hiện chỉ cho phép đúng một video cho mỗi ảnh.")
        return MuseVideoSettings(
            model=str(self.model or "").strip(),
            aspect_ratio=str(self.aspect_ratio or "").strip(),
            duration=str(self.duration or "").strip(),
            resolution=str(self.resolution or "").strip(),
            quantity=1,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MuseVideoSettings":
        return cls(
            model=str(value.get("model") or ""),
            aspect_ratio=str(value.get("aspect_ratio") or ""),
            duration=str(value.get("duration") or ""),
            resolution=str(value.get("resolution") or ""),
            quantity=int(value.get("quantity") or 1),
        ).normalized()


@dataclass
class MuseVideoJob:
    job_id: str
    source_path: str
    worker_id: int
    account_id: str
    email: str
    prompt: str
    settings: MuseVideoSettings
    output_path: str
    state: MuseVideoJobState = MuseVideoJobState.PENDING
    submitted: bool = False
    submission_attempted: bool = False
    baseline_videos: list[str] = field(default_factory=list)
    result_fingerprint: str = ""
    started_at: str = ""
    submitted_at: str = ""
    completed_at: str = ""
    error: str = ""
    attempts: int = 0
    download_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["settings"] = asdict(self.settings)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MuseVideoJob":
        raw_state = str(value.get("state") or MuseVideoJobState.PENDING.value)
        try:
            state = MuseVideoJobState(raw_state)
        except ValueError:
            state = MuseVideoJobState.FAILED
        submitted = bool(value.get("submitted"))
        attempted = bool(value.get("submission_attempted"))
        if state in {MuseVideoJobState.SUBMITTING, MuseVideoJobState.SUBMITTED}:
            attempted = True
            state = MuseVideoJobState.SUBMITTED
        elif state in {MuseVideoJobState.GENERATING, MuseVideoJobState.DOWNLOADING}:
            attempted = True
            submitted = True
        elif state in {
            MuseVideoJobState.UPLOADING,
            MuseVideoJobState.READY_TO_GENERATE,
        } and not attempted:
            state = MuseVideoJobState.PENDING
        return cls(
            job_id=str(value.get("job_id") or ""),
            source_path=str(value.get("source_path") or ""),
            worker_id=int(value.get("worker_id") or 1),
            account_id=str(value.get("account_id") or ""),
            email=str(value.get("email") or ""),
            prompt=str(value.get("prompt") or ""),
            settings=MuseVideoSettings.from_dict(value.get("settings") or {}),
            output_path=str(value.get("output_path") or ""),
            state=state,
            submitted=submitted,
            submission_attempted=attempted,
            baseline_videos=[str(item) for item in value.get("baseline_videos", [])],
            result_fingerprint=str(value.get("result_fingerprint") or ""),
            started_at=str(value.get("started_at") or ""),
            submitted_at=str(value.get("submitted_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            error=str(value.get("error") or ""),
            attempts=int(value.get("attempts") or 0),
            download_attempts=int(value.get("download_attempts") or 0),
        )


@dataclass
class MuseVideoWorker:
    worker_id: int
    session_id: int
    profile_dir: Path
    download_dir: Path
    account_id: str = ""
    email: str = ""
    driver: Any = None
    queue: list[str] = field(default_factory=list)
    assigned_sources: list[str] = field(default_factory=list)
    task: asyncio.Task[Any] | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state: MuseVideoWorkerState = MuseVideoWorkerState.IDLE
    current_job_id: str = ""
    progress: int = 0
    error: str = ""
    logs: list[str] = field(default_factory=list)
    active: bool = False


@dataclass(frozen=True)
class MuseVideoWorkerSnapshot:
    worker_id: int
    account_id: str
    email: str
    profile_dir: str
    state: MuseVideoWorkerState
    assigned: tuple[str, ...]
    current_image: str
    progress: int
    pending: int
    completed: int
    failed: int
    quota: int
    error: str
    logs: tuple[str, ...]
    task_running: bool


@dataclass(frozen=True)
class MuseVideoBatchSnapshot:
    source_paths: tuple[str, ...]
    prompt: str
    output_dir: str
    settings: MuseVideoSettings
    workers: tuple[MuseVideoWorkerSnapshot, ...]
    jobs: tuple[MuseVideoJob, ...]
    running: bool


@dataclass
class MuseVideoRunContext:
    worker_id: int
    download_dir: Path
    stopped: Callable[[], bool]
    transition: Callable[..., None]
    log: Callable[[str], None]
    retry_limit: int
    backoff_base: float


class MuseVideoAutomation:
    """Run one image-to-video job on an already authenticated owned driver."""

    def __init__(
        self,
        *,
        start_url: str = MUSE_START_URL,
        selectors: MuseVideoSelectors | None = None,
        timeout: float = 15 * 60,
        upload_timeout: float = 90,
        download_timeout: float = 180,
        poll_interval: float = 0.5,
    ) -> None:
        self.start_url = start_url
        self.selectors = selectors or MuseVideoSelectors()
        self.timeout = max(0.1, float(timeout))
        self.upload_timeout = max(0.1, float(upload_timeout))
        self.download_timeout = max(0.1, float(download_timeout))
        self.poll_interval = max(0.02, float(poll_interval))

    def process(self, driver: Any, job: MuseVideoJob, context: MuseVideoRunContext) -> Path:
        existing = Path(job.output_path)
        if existing.is_file() and _valid_mp4(existing):
            context.log(f"Đã có MP4 hợp lệ cho job {job.job_id[:12]}; không gửi lại.")
            return existing
        if job.submitted or job.submission_attempted:
            return self._recover_submitted(driver, job, context)
        source = Path(job.source_path)
        if not source.is_file():
            raise MuseVideoBatchError("Ảnh nguồn không còn tồn tại.")
        self._check(driver, context)
        self._navigate_new_task(driver, context)
        baseline_previews = self._element_fingerprints(driver, self.selectors.previews)
        context.transition(MuseVideoJobState.UPLOADING, progress=8, error="")
        self._upload(driver, source)
        self._wait(
            driver,
            lambda: self._new_visible_element(driver, self.selectors.previews, baseline_previews),
            self.upload_timeout,
            context,
            "Muse không hiển thị preview ảnh đã upload trước thời hạn.",
        )
        self._fill_prompt(driver, job.prompt)
        self._apply_settings(driver, job.settings)
        baseline_videos = sorted(self._video_fingerprints(driver))
        context.transition(
            MuseVideoJobState.READY_TO_GENERATE,
            progress=18,
            baseline_videos=baseline_videos,
        )
        context.transition(
            MuseVideoJobState.SUBMITTING,
            progress=20,
            submission_attempted=True,
        )
        self._click_generate_once(driver)
        context.transition(
            MuseVideoJobState.SUBMITTED,
            progress=22,
            submitted=True,
            submitted_at=_utc_now(),
        )
        return self._wait_and_download(driver, job, context)

    def retry_download(self, driver: Any, job: MuseVideoJob, context: MuseVideoRunContext) -> Path:
        if not (job.submitted or job.submission_attempted):
            raise MuseVideoDownloadError("Job chưa từng được gửi; không thể chỉ retry download.")
        return self._recover_submitted(driver, job, context)

    def _recover_submitted(self, driver: Any, job: MuseVideoJob, context: MuseVideoRunContext) -> Path:
        context.log(f"Khôi phục job đã gửi {job.job_id[:12]}; không bấm Generate lần hai.")
        self._check(driver, context)
        return self._wait_and_download(driver, job, context)

    def _wait_and_download(self, driver: Any, job: MuseVideoJob, context: MuseVideoRunContext) -> Path:
        target = Path(job.output_path)
        if target.is_file() and _valid_mp4(target):
            return target
        baseline = set(job.baseline_videos)
        context.transition(MuseVideoJobState.GENERATING, progress=25)
        transient_retries = 0

        def find_result():
            nonlocal transient_retries
            self._check(driver, context)
            body = " ".join(_text(item) for item in _find(driver, "tag name", "body")).casefold()
            if any(marker in body for marker in QUOTA_MARKERS):
                raise MuseVideoQuotaExhausted(
                    "Tài khoản Muse đã chạm quota/rate limit; ảnh chưa gửi được giữ nguyên."
                )
            if any(marker in body for marker in TRANSIENT_NETWORK_MARKERS):
                if transient_retries >= context.retry_limit:
                    raise MuseVideoBatchError("Muse liên tục báo lỗi mạng sau số lần retry giới hạn.")
                delay = context.backoff_base * (2 ** transient_retries)
                transient_retries += 1
                if self._wait_stop(context, delay):
                    raise MuseVideoStopped("Đã dừng worker Muse.")
                return False
            videos = self._videos(driver)
            if job.result_fingerprint:
                matched = next(
                    (
                        video
                        for video in videos
                        if _video_fingerprint(video) == job.result_fingerprint
                        and self._video_ready(driver, video)
                    ),
                    None,
                )
                if matched is not None:
                    return matched
            return next(
                (
                    video
                    for video in reversed(videos)
                    if _video_fingerprint(video) not in baseline and self._video_ready(driver, video)
                ),
                False,
            )

        video = self._wait(
            driver,
            find_result,
            self.timeout,
            context,
            "Muse chưa trả về video mới trước thời hạn; job đã gửi sẽ không được gửi lại tự động.",
        )
        fingerprint = _video_fingerprint(video)
        context.transition(
            MuseVideoJobState.DOWNLOADING,
            progress=90,
            result_fingerprint=fingerprint,
            download_attempts=job.download_attempts + 1,
        )
        downloaded = self._download(driver, video, target, context)
        return downloaded

    def _navigate_new_task(self, driver: Any, context: MuseVideoRunContext) -> None:
        self._retry_network(lambda: driver.get(self.start_url), context)
        self._check(driver, context)
        for selector in self.selectors.new_task:
            for element in _find(driver, "css selector", selector):
                if _clickable(element):
                    try:
                        element.click()
                        return
                    except Exception:
                        continue

    def _upload(self, driver: Any, source: Path) -> None:
        fields = self._elements(driver, self.selectors.upload_inputs)
        if not fields:
            self._click_selector(driver, self.selectors.upload_buttons)
            fields = self._elements(driver, self.selectors.upload_inputs)
        for field in reversed(fields):
            try:
                accept = _attr(field, "accept").casefold()
                if accept and "image" not in accept and source.suffix.casefold() not in accept:
                    continue
                field.send_keys(str(source.resolve()))
                return
            except Exception:
                continue
        raise MuseVideoBatchError("Không tìm thấy input upload ảnh ổn định trên Muse.")

    def _fill_prompt(self, driver: Any, prompt: str) -> None:
        for field in reversed(self._elements(driver, self.selectors.prompt_inputs)):
            if not _clickable(field):
                continue
            try:
                field.click()
                tag = str(getattr(field, "tag_name", "") or "").casefold()
                if tag in {"textarea", "input"}:
                    field.clear()
                else:
                    field.send_keys("\ue009", "a")
                field.send_keys(prompt)
                return
            except Exception:
                continue
        raise MuseVideoBatchError("Không tìm thấy ô prompt ổn định trên Muse.")

    def _apply_settings(self, driver: Any, settings: MuseVideoSettings) -> None:
        for selectors, value, label in (
            (self.selectors.model_controls, settings.model, "model"),
            (self.selectors.aspect_controls, settings.aspect_ratio, "tỷ lệ"),
            (self.selectors.duration_controls, settings.duration, "độ dài"),
            (self.selectors.resolution_controls, settings.resolution, "độ phân giải"),
        ):
            if value:
                self._choose_control(driver, selectors, value, label)

    def _choose_control(self, driver: Any, selectors: tuple[str, ...], value: str, label: str) -> None:
        control = next((item for item in self._elements(driver, selectors) if _clickable(item)), None)
        if control is None:
            raise MuseVideoBatchError(f"Muse không hiển thị cài đặt {label} đã chọn.")
        try:
            if str(getattr(control, "tag_name", "")).casefold() == "select":
                from selenium.webdriver.support.ui import Select

                Select(control).select_by_visible_text(value)
                return
            control.click()
            options = _find(driver, "css selector", "[role='option'], [data-testid*='option' i]")
            target = value.casefold()
            for option in options:
                if _clickable(option) and _text(option).strip().casefold() == target:
                    option.click()
                    return
        except Exception:
            pass
        raise MuseVideoBatchError(f"Không áp dụng được cài đặt {label} trên giao diện Muse hiện tại.")

    def _click_generate_once(self, driver: Any) -> None:
        if self._click_selector(driver, self.selectors.generate_buttons):
            return
        if _click_by_text(driver, ("generate", "create video", "create", "send"), reverse=True):
            return
        raise MuseVideoBatchError("Không tìm thấy nút Generate/Send ổn định trên Muse.")

    def _download(self, driver: Any, video: Any, target: Path, context: MuseVideoRunContext) -> Path:
        context.download_dir.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = {
            (item.resolve(), item.stat().st_mtime_ns)
            for item in context.download_dir.iterdir()
            if item.is_file()
        }
        clicked = False
        try:
            clicked = bool(
                driver.execute_script(
                    "const v=arguments[0];let n=v;"
                    "while(n&&n!==document.body){const b=n.querySelector&&n.querySelector("
                    "\"button[aria-label*='download' i],[data-testid*='download' i]\");"
                    "if(b){b.click();return true;}n=n.parentElement;}return false;",
                    video,
                )
            )
        except Exception:
            clicked = False
        if not clicked:
            clicked = self._click_selector(driver, self.selectors.download_buttons, reverse=True)
        if not clicked:
            try:
                driver.execute_script(
                    "const v=arguments[0],a=document.createElement('a');"
                    "a.href=v.currentSrc||v.src;a.download='muse-video.mp4';"
                    "document.body.appendChild(a);a.click();a.remove();",
                    video,
                )
                clicked = True
            except Exception:
                pass
        if not clicked:
            raise MuseVideoDownloadError("Không kích hoạt được Download cho đúng video mới.")

        previous: tuple[Path, int] | None = None
        stable = 0

        def completed_download():
            nonlocal previous, stable
            self._check(driver, context)
            partials = [
                item for item in context.download_dir.iterdir()
                if item.is_file() and item.name.casefold().endswith((".crdownload", ".tmp", ".part"))
                and (item.resolve(), item.stat().st_mtime_ns) not in before
            ]
            candidates = []
            for item in context.download_dir.iterdir():
                if not item.is_file() or item.suffix.casefold() != ".mp4":
                    continue
                marker = (item.resolve(), item.stat().st_mtime_ns)
                if marker not in before and item.stat().st_size > 0:
                    candidates.append(item)
            if not candidates or partials:
                return False
            newest = max(candidates, key=lambda item: item.stat().st_mtime_ns)
            current = (newest, newest.stat().st_size)
            stable = stable + 1 if current == previous else 0
            previous = current
            return newest if stable >= 2 else False

        downloaded = Path(
            self._wait(
                driver,
                completed_download,
                self.download_timeout,
                context,
                "Video Muse đã hoàn tất nhưng download chưa xong; chỉ bước download có thể chạy lại.",
            )
        )
        if not _valid_mp4(downloaded):
            raise MuseVideoDownloadError("File tải từ Muse không phải MP4 hợp lệ hoặc có dung lượng bằng 0.")
        if target.exists():
            if _valid_mp4(target):
                downloaded.unlink(missing_ok=True)
                return target
            raise MuseVideoDownloadError("File output đích đã tồn tại nhưng không hợp lệ; tool không ghi đè.")
        shutil.move(str(downloaded), str(target))
        if not _valid_mp4(target):
            raise MuseVideoDownloadError("File output MP4 không hợp lệ sau khi di chuyển.")
        return target

    def _wait(self, driver: Any, condition, timeout: float, context: MuseVideoRunContext, message: str):
        try:
            from selenium.common.exceptions import TimeoutException
            from selenium.webdriver.support.ui import WebDriverWait
        except ModuleNotFoundError:
            raise MuseVideoBatchError("Thiếu Selenium. Hãy cài lại ứng dụng.") from None
        try:
            return WebDriverWait(
                driver,
                max(0.05, timeout),
                poll_frequency=self.poll_interval,
            ).until(lambda _driver: condition())
        except TimeoutException:
            raise MuseVideoTimeout(message) from None

    def _retry_network(self, callback: Callable[[], Any], context: MuseVideoRunContext) -> Any:
        for attempt in range(context.retry_limit + 1):
            if context.stopped():
                raise MuseVideoStopped("Đã dừng worker Muse.")
            try:
                return callback()
            except Exception:
                if attempt >= context.retry_limit:
                    raise MuseVideoBatchError("Không tải được giao diện Muse sau số lần retry giới hạn.") from None
                delay = context.backoff_base * (2 ** attempt)
                if self._wait_stop(context, delay):
                    raise MuseVideoStopped("Đã dừng worker Muse.")

    @staticmethod
    def _wait_stop(context: MuseVideoRunContext, seconds: float) -> bool:
        event = threading.Event()
        # The worker callback is checked in short intervals without a fixed sleep.
        remaining = max(0.0, seconds)
        while remaining > 0:
            interval = min(0.1, remaining)
            if context.stopped() or event.wait(interval):
                return True
            remaining -= interval
        return context.stopped()

    def _check(self, driver: Any, context: MuseVideoRunContext) -> None:
        if context.stopped():
            raise MuseVideoStopped("Đã dừng worker Muse.")
        current_url = _safe_url(driver)
        host = _hostname(current_url)
        if not current_url or not host:
            raise MuseVideoDriverError("Chrome/driver của worker Muse đã đóng hoặc không phản hồi.")
        if host in {"auth.muse.ai", "accounts.google.com"}:
            raise MuseVideoLoginRequired("Phiên Muse đã hết hạn; hãy đăng nhập thủ công cho riêng tài khoản này.")
        if host not in MUSE_ALLOWED_HOSTS or host != "muse.ai":
            raise MuseVideoBatchError("Muse chuyển tới hostname không được phép; worker đã dừng tương tác.")

    def _videos(self, driver: Any) -> list[Any]:
        return [item for item in self._elements(driver, self.selectors.video_results) if _visible(item)]

    def _video_fingerprints(self, driver: Any) -> set[str]:
        return {_video_fingerprint(item) for item in self._videos(driver)}

    @staticmethod
    def _video_ready(driver: Any, video: Any) -> bool:
        try:
            ready = bool(driver.execute_script("return arguments[0].readyState >= 2", video))
            if ready:
                return True
        except Exception:
            pass
        # Some fake/remote elements do not expose readyState through WebDriver;
        # a concrete media URL is the safe fallback for downloadability.
        return bool(_attr(video, "src") or _attr(video, "currentSrc"))

    def _new_visible_element(self, driver: Any, selectors: tuple[str, ...], baseline: set[str]):
        return next(
            (
                item
                for item in self._elements(driver, selectors)
                if _visible(item) and self._element_fingerprint(item) not in baseline
            ),
            False,
        )

    def _element_fingerprints(self, driver: Any, selectors: tuple[str, ...]) -> set[str]:
        return {self._element_fingerprint(item) for item in self._elements(driver, selectors)}

    @staticmethod
    def _element_fingerprint(element: Any) -> str:
        raw = "|".join(
            (
                str(getattr(element, "id", "") or ""),
                _attr(element, "src"),
                _attr(element, "alt"),
            )
        )
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _elements(driver: Any, selectors: tuple[str, ...]) -> list[Any]:
        values: list[Any] = []
        seen: set[str] = set()
        for selector in selectors:
            for item in _find(driver, "css selector", selector):
                key = str(getattr(item, "id", "") or f"object:{id(item)}")
                if key not in seen:
                    seen.add(key)
                    values.append(item)
        return values

    @staticmethod
    def _click_selector(driver: Any, selectors: tuple[str, ...], *, reverse: bool = False) -> bool:
        for selector in selectors:
            values = _find(driver, "css selector", selector)
            items = reversed(values) if reverse else iter(values)
            for item in items:
                if _clickable(item):
                    try:
                        item.click()
                        return True
                    except Exception:
                        continue
        return False


class MuseVideoBatchManager:
    """Coordinate three isolated sequential Muse image-to-video workers."""

    def __init__(
        self,
        *,
        session_manager: MuseSessionManager | None = None,
        checkpoint_path: str | Path = MUSE_VIDEO_BATCH_CHECKPOINT,
        automation: MuseVideoAutomation | None = None,
        retry_limit: int = 2,
        backoff_base: float = 0.5,
    ) -> None:
        self.session_manager = session_manager or get_muse_session_manager()
        self.checkpoint_path = Path(checkpoint_path)
        self.automation = automation or MuseVideoAutomation(start_url=self.session_manager.start_url)
        self.retry_limit = max(0, int(retry_limit))
        self.backoff_base = max(0.0, float(backoff_base))
        self._state_lock = threading.RLock()
        self._manager_lock: asyncio.Lock | None = None
        self._closed = False
        self.source_paths: list[str] = []
        self.current_job_ids: list[str] = []
        self.prompt = ""
        self.output_dir = ""
        self.settings = MuseVideoSettings()
        self.jobs: dict[str, MuseVideoJob] = {}
        self.workers: dict[int, MuseVideoWorker] = {
            worker_id: MuseVideoWorker(
                worker_id=worker_id,
                session_id=worker_id,
                profile_dir=self.session_manager.sessions[worker_id].profile_dir,
                download_dir=MUSE_SESSION_DOWNLOADS_DIR / f"account_{worker_id}",
            )
            for worker_id in range(1, MUSE_SESSION_COUNT + 1)
        }
        self._load()

    @property
    def busy(self) -> bool:
        with self._state_lock:
            return any(worker.active for worker in self.workers.values())

    @staticmethod
    def scan_images(folder: str | Path, *, recursive: bool = False) -> list[Path]:
        if not str(folder or "").strip():
            raise ValueError("Hãy chọn thư mục ảnh.")
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Thư mục ảnh không tồn tại.")
        iterator = root.rglob("*") if recursive else root.glob("*")
        unique: dict[str, Path] = {}
        for item in iterator:
            if not item.is_file() or item.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
                continue
            resolved = item.resolve()
            unique[str(resolved).casefold()] = resolved
        return sorted(
            unique.values(),
            key=lambda item: (item.name.casefold(), str(item).casefold()),
        )

    def allocate_images(self, paths: Iterable[str | Path]) -> tuple[tuple[str, ...], ...]:
        normalized: dict[str, Path] = {}
        for value in paths:
            path = Path(value).expanduser().resolve()
            if path.is_file() and path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES:
                normalized[str(path).casefold()] = path
        ordered = sorted(
            normalized.values(),
            key=lambda item: (item.name.casefold(), str(item).casefold()),
        )
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Không thể phân bổ khi batch Muse đang chạy.")
            self.source_paths = [str(item) for item in ordered]
            self.current_job_ids = []
            for worker in self.workers.values():
                worker.assigned_sources = []
                worker.queue = []
                worker.current_job_id = ""
                worker.progress = 0
                worker.error = ""
                worker.state = MuseVideoWorkerState.IDLE
            for index, path in enumerate(ordered):
                worker_id = index % MUSE_SESSION_COUNT + 1
                self.workers[worker_id].assigned_sources.append(str(path))
            self._persist_locked()
            return tuple(tuple(worker.assigned_sources) for worker in self.workers.values())

    def start_all(
        self,
        prompt: str,
        settings: MuseVideoSettings,
        output_dir: str | Path,
    ) -> Future[Any]:
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("Prompt tạo video chung không được để trống.")
        normalized_settings = settings.normalized()
        if not str(output_dir or "").strip():
            raise ValueError("Hãy chọn thư mục lưu video đầu ra.")
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        self._require_all_ready()
        with self._state_lock:
            if not self.source_paths:
                raise ValueError("Hãy chọn và phân bổ ảnh trước khi bắt đầu.")
            if self.busy:
                raise MuseVideoBatchError("Batch Muse đang chạy.")
            self.prompt = text
            self.settings = normalized_settings
            self.output_dir = str(output)
            self._build_jobs_locked()
            self._persist_locked()
        return self._schedule(self._launch_workers(resume=False))

    def resume(self) -> Future[Any]:
        self._require_all_ready()
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Batch Muse đang chạy.")
            if not self.current_job_ids:
                raise MuseVideoBatchError("Không có batch Muse để tiếp tục.")
        return self._schedule(self._launch_workers(resume=True))

    def stop_worker(self, worker_id: int) -> bool:
        with self._state_lock:
            worker = self._require_worker(worker_id)
            if not worker.active:
                return False
            worker.stop_event.set()
            worker.state = MuseVideoWorkerState.STOPPING
            self._log_locked(worker, "Đang dừng worker theo yêu cầu…")
            self._persist_locked()
            return True

    def stop_all(self) -> int:
        return sum(1 for worker_id in self.workers if self.stop_worker(worker_id))

    def retry_failed(self, worker_id: int) -> int:
        with self._state_lock:
            worker = self._require_worker(worker_id)
            if worker.active:
                raise MuseVideoBatchError("Hãy dừng worker trước khi chạy lại ảnh lỗi.")
            count = 0
            for job_id in worker.queue:
                job = self.jobs.get(job_id)
                if job and job.state == MuseVideoJobState.FAILED:
                    job.state = MuseVideoJobState.PENDING
                    job.error = ""
                    count += 1
            if count:
                worker.state = MuseVideoWorkerState.READY
                worker.error = ""
                self._log_locked(worker, f"Đã đưa {count} ảnh lỗi về hàng chờ thủ công.")
                self._persist_locked()
            return count

    def redistribute_unsubmitted(self) -> tuple[tuple[str, ...], ...]:
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Hãy dừng toàn bộ worker trước khi phân bổ lại.")
            movable = [
                self.jobs[job_id]
                for job_id in self.current_job_ids
                if job_id in self.jobs
                and not self.jobs[job_id].submitted
                and not self.jobs[job_id].submission_attempted
                and self.jobs[job_id].state in {
                    MuseVideoJobState.PENDING,
                    MuseVideoJobState.PAUSED,
                    MuseVideoJobState.STOPPED,
                }
            ]
            movable.sort(key=lambda job: job.source_path.casefold())
            movable_ids = {job.job_id for job in movable}
            for worker in self.workers.values():
                worker.queue = [job_id for job_id in worker.queue if job_id not in movable_ids]
            for index, job in enumerate(movable):
                worker_id = index % MUSE_SESSION_COUNT + 1
                job.worker_id = worker_id
                session = self.session_manager.sessions[worker_id]
                job.account_id = session.account_id
                job.email = session.email
                job.state = MuseVideoJobState.PENDING
                job.output_path = str(
                    Path(self.output_dir)
                    / _output_filename(Path(job.source_path), session.account_id, job.job_id)
                )
                self.workers[worker_id].queue.append(job.job_id)
            self._sync_assigned_from_queues_locked()
            self._persist_locked()
            return tuple(tuple(worker.assigned_sources) for worker in self.workers.values())

    def clear_ui_data(self) -> None:
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Hãy dừng batch trước khi xóa dữ liệu UI.")
            self.source_paths = []
            self.current_job_ids = []
            self.prompt = ""
            self.output_dir = ""
            self.settings = MuseVideoSettings()
            for worker in self.workers.values():
                worker.queue = []
                worker.assigned_sources = []
                worker.current_job_id = ""
                worker.progress = 0
                worker.error = ""
                worker.logs = []
                worker.state = MuseVideoWorkerState.IDLE
            # Historical jobs remain in the checkpoint to preserve de-duplication.
            self._persist_locked()

    def snapshot(self) -> MuseVideoBatchSnapshot:
        with self._state_lock:
            self._sync_sessions_locked()
            job_values = tuple(
                self.jobs[job_id]
                for job_id in self.current_job_ids
                if job_id in self.jobs
            )
            return MuseVideoBatchSnapshot(
                source_paths=tuple(self.source_paths),
                prompt=self.prompt,
                output_dir=self.output_dir,
                settings=self.settings,
                workers=tuple(self._worker_snapshot(worker) for worker in self.workers.values()),
                jobs=job_values,
                running=any(worker.active for worker in self.workers.values()),
            )

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self.stop_all()
        tasks = [worker.task for worker in self.workers.values() if worker.task and not worker.task.done()]
        if tasks:
            try:
                future = self._schedule_for_shutdown(self._wait_tasks(tasks, timeout))
                future.result(timeout=max(1.0, timeout) + 2)
            except Exception:
                pass

    async def _wait_tasks(self, tasks: list[asyncio.Task[Any]], timeout: float) -> None:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=max(0.5, timeout))
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()

    async def _launch_workers(self, *, resume: bool) -> dict[int, Any]:
        if self._manager_lock is None:
            self._manager_lock = asyncio.Lock()
        claimed: list[tuple[MuseVideoWorker, asyncio.Task[Any]]] = []
        async with self._manager_lock:
            for worker in self.workers.values():
                if worker.active:
                    continue
                worker.stop_event.clear()
                task = asyncio.create_task(
                    self._run_worker(worker, resume=resume),
                    name=f"muse-video-worker-{worker.worker_id}",
                )
                with self._state_lock:
                    worker.task = task
                    worker.active = True
                    self._persist_locked()
                claimed.append((worker, task))
        try:
            results = await asyncio.gather(*(task for _worker, task in claimed), return_exceptions=True)
        finally:
            with self._state_lock:
                for worker, task in claimed:
                    if worker.task is task:
                        worker.task = None
                        worker.active = False
                        worker.stop_event.clear()
                self._persist_locked()
        return {worker.worker_id: result for (worker, _task), result in zip(claimed, results)}

    async def _run_worker(self, worker: MuseVideoWorker, *, resume: bool) -> None:
        session = self.session_manager.sessions[worker.session_id]
        # Reuse the session lock as well as the worker lock so login/relogin and
        # batch generation can never queue overlapping access to one driver.
        async with worker.lock, session.lock:
            with self._state_lock:
                worker.driver = session.driver
                worker.account_id = session.account_id
                worker.email = session.email
                worker.state = MuseVideoWorkerState.RUNNING
                worker.error = ""
                self._log_locked(worker, "Worker bắt đầu/tiếp tục hàng đợi.")
                self._persist_locked()
            for job_id in list(worker.queue):
                if worker.stop_event.is_set():
                    break
                with self._state_lock:
                    job = self.jobs.get(job_id)
                    if job is None or job.state in {
                        MuseVideoJobState.COMPLETED,
                        MuseVideoJobState.QUOTA_EXHAUSTED,
                    }:
                        continue
                    # FAILED requires the explicit retry_failed action. Resume never
                    # converts failures (and especially quota failures) implicitly.
                    if job.state == MuseVideoJobState.FAILED:
                        continue
                    if job.state == MuseVideoJobState.LOGIN_REQUIRED:
                        job.state = MuseVideoJobState.PAUSED
                    if (
                        (job.submitted or job.submission_attempted)
                        and job.account_id
                        and job.account_id != session.account_id
                    ):
                        job.state = MuseVideoJobState.FAILED
                        job.error = (
                            "Job đã gửi thuộc tài khoản khác; tool từ chối gửi lại hoặc khôi phục bằng profile hiện tại."
                        )
                        worker.state = MuseVideoWorkerState.FAILED
                        worker.error = job.error
                        self._log_locked(worker, job.error)
                        self._persist_locked()
                        continue
                    worker.current_job_id = job_id
                    worker.progress = 0
                    job.attempts += 1
                    if not job.started_at:
                        job.started_at = _utc_now()
                    self._log_locked(worker, f"Đang xử lý {Path(job.source_path).name}")
                    self._persist_locked()
                context = MuseVideoRunContext(
                    worker_id=worker.worker_id,
                    download_dir=worker.download_dir,
                    stopped=worker.stop_event.is_set,
                    transition=lambda state, _job_id=job_id, **changes: self._transition(
                        worker.worker_id, _job_id, state, **changes
                    ),
                    log=lambda message, _worker=worker: self._log(_worker, message),
                    retry_limit=self.retry_limit,
                    backoff_base=self.backoff_base,
                )
                try:
                    output = await self.session_manager._loop.run_in_executor(
                        session.executor,
                        self.automation.process,
                        session.driver,
                        job,
                        context,
                    )
                except MuseVideoStopped:
                    with self._state_lock:
                        if job.state != MuseVideoJobState.COMPLETED:
                            job.state = MuseVideoJobState.PAUSED
                        worker.state = MuseVideoWorkerState.PAUSED
                        self._log_locked(worker, "Worker đã dừng; dữ liệu và hàng đợi được giữ nguyên.")
                        self._persist_locked()
                    break
                except MuseVideoLoginRequired as exc:
                    with self._state_lock:
                        job.state = MuseVideoJobState.LOGIN_REQUIRED
                        job.error = str(exc)
                        worker.state = MuseVideoWorkerState.LOGIN_REQUIRED
                        worker.error = str(exc)
                        self.session_manager._set_state(
                            session,
                            MuseSessionState.LOGIN_REQUIRED,
                            progress=0,
                            status_message=str(exc),
                        )
                        self._persist_locked()
                    break
                except MuseVideoQuotaExhausted as exc:
                    with self._state_lock:
                        job.state = MuseVideoJobState.QUOTA_EXHAUSTED
                        job.error = str(exc)
                        worker.state = MuseVideoWorkerState.QUOTA_EXHAUSTED
                        worker.error = str(exc)
                        self._log_locked(worker, "Quota/rate limit: không chuyển job sang tài khoản khác.")
                        self._persist_locked()
                    break
                except MuseVideoDriverError as exc:
                    with self._state_lock:
                        job.state = MuseVideoJobState.FAILED
                        job.error = str(exc)
                        worker.state = MuseVideoWorkerState.FAILED
                        worker.error = str(exc)
                        self.session_manager._set_state(
                            session,
                            MuseSessionState.FAILED,
                            progress=0,
                            status_message="Chrome/driver Muse đã dừng; có thể đăng nhập lại riêng tài khoản này.",
                            error=str(exc),
                        )
                        self._persist_locked()
                    break
                except Exception as exc:
                    message = str(exc) if isinstance(exc, MuseVideoBatchError) else (
                        "Muse/Chrome không phản hồi; không có cookie hoặc token nào được ghi log."
                    )
                    with self._state_lock:
                        job.state = MuseVideoJobState.FAILED
                        job.error = message
                        worker.error = message
                        self._log_locked(worker, f"Lỗi {Path(job.source_path).name}: {message}")
                        self._persist_locked()
                    continue
                else:
                    with self._state_lock:
                        job.output_path = str(output)
                        job.state = MuseVideoJobState.COMPLETED
                        job.completed_at = _utc_now()
                        job.error = ""
                        worker.progress = 100
                        self._log_locked(worker, f"Hoàn tất {Path(job.source_path).name}")
                        self._persist_locked()
            with self._state_lock:
                worker.current_job_id = ""
                if worker.state == MuseVideoWorkerState.RUNNING:
                    pending = self._worker_pending(worker)
                    has_failed = any(
                        self.jobs[job_id].state == MuseVideoJobState.FAILED
                        for job_id in worker.queue
                        if job_id in self.jobs
                    )
                    if has_failed:
                        worker.state = MuseVideoWorkerState.FAILED
                    else:
                        worker.state = MuseVideoWorkerState.READY if pending else MuseVideoWorkerState.COMPLETED
                self._persist_locked()

    def _transition(
        self,
        worker_id: int,
        job_id: str,
        state: MuseVideoJobState,
        **changes: Any,
    ) -> None:
        with self._state_lock:
            worker = self.workers[worker_id]
            job = self.jobs[job_id]
            job.state = state
            for key, value in changes.items():
                if key == "progress":
                    worker.progress = max(0, min(100, int(value)))
                elif hasattr(job, key):
                    setattr(job, key, value)
            self._persist_locked()

    def _build_jobs_locked(self) -> None:
        self.current_job_ids = []
        for worker in self.workers.values():
            worker.queue = []
            worker.state = MuseVideoWorkerState.READY
            worker.error = ""
            session = self.session_manager.sessions[worker.session_id]
            worker.account_id = session.account_id
            worker.email = session.email
        for index, source_value in enumerate(self.source_paths):
            worker_id = index % MUSE_SESSION_COUNT + 1
            source = Path(source_value)
            session = self.session_manager.sessions[worker_id]
            job_id = create_muse_video_job_id(source, self.prompt, self.settings)
            output = Path(self.output_dir) / _output_filename(source, session.account_id, job_id)
            job = self.jobs.get(job_id)
            if job is None:
                job = MuseVideoJob(
                    job_id=job_id,
                    source_path=str(source),
                    worker_id=worker_id,
                    account_id=session.account_id,
                    email=session.email,
                    prompt=self.prompt,
                    settings=self.settings,
                    output_path=str(output),
                )
                self.jobs[job_id] = job
            elif not job.submitted and not job.submission_attempted and job.state != MuseVideoJobState.COMPLETED:
                job.worker_id = worker_id
                job.account_id = session.account_id
                job.email = session.email
                job.output_path = str(output)
            self.current_job_ids.append(job_id)
            assigned_worker = self.workers[job.worker_id]
            assigned_worker.queue.append(job_id)
        self._sync_assigned_from_queues_locked()

    def _require_all_ready(self) -> None:
        snapshots = self.session_manager.snapshots()
        not_ready = [item.session_id for item in snapshots if item.state != MuseSessionState.READY or not item.driver_open]
        if not_ready:
            joined = ", ".join(f"Muse {value}" for value in not_ready)
            raise MuseVideoBatchError(f"Cả 3 tài khoản phải READY trước khi chạy; chưa sẵn sàng: {joined}.")
        emails = [item.email.casefold() for item in snapshots]
        if len(set(emails)) != MUSE_SESSION_COUNT:
            raise MuseVideoBatchError("Ba worker phải dùng ba tài khoản Google khác nhau.")

    def _worker_snapshot(self, worker: MuseVideoWorker) -> MuseVideoWorkerSnapshot:
        jobs = [self.jobs[job_id] for job_id in worker.queue if job_id in self.jobs]
        current = self.jobs.get(worker.current_job_id)
        pending_states = {
            MuseVideoJobState.PENDING,
            MuseVideoJobState.UPLOADING,
            MuseVideoJobState.READY_TO_GENERATE,
            MuseVideoJobState.SUBMITTING,
            MuseVideoJobState.SUBMITTED,
            MuseVideoJobState.GENERATING,
            MuseVideoJobState.DOWNLOADING,
            MuseVideoJobState.PAUSED,
            MuseVideoJobState.STOPPED,
            MuseVideoJobState.LOGIN_REQUIRED,
        }
        return MuseVideoWorkerSnapshot(
            worker_id=worker.worker_id,
            account_id=worker.account_id,
            email=worker.email,
            profile_dir=str(worker.profile_dir),
            state=worker.state,
            assigned=tuple(worker.assigned_sources),
            current_image=current.source_path if current else "",
            progress=worker.progress,
            pending=sum(job.state in pending_states for job in jobs),
            completed=sum(job.state == MuseVideoJobState.COMPLETED for job in jobs),
            failed=sum(job.state == MuseVideoJobState.FAILED for job in jobs),
            quota=sum(job.state == MuseVideoJobState.QUOTA_EXHAUSTED for job in jobs),
            error=worker.error,
            logs=tuple(worker.logs[-100:]),
            task_running=worker.active,
        )

    def _sync_sessions_locked(self) -> None:
        for worker in self.workers.values():
            session = self.session_manager.sessions[worker.session_id]
            worker.account_id = session.account_id
            worker.email = session.email
            worker.driver = session.driver
            if not worker.active and worker.state == MuseVideoWorkerState.IDLE and session.state == MuseSessionState.READY:
                worker.state = MuseVideoWorkerState.READY

    def _sync_assigned_from_queues_locked(self) -> None:
        for worker in self.workers.values():
            worker.assigned_sources = [
                self.jobs[job_id].source_path
                for job_id in worker.queue
                if job_id in self.jobs
            ]

    def _worker_pending(self, worker: MuseVideoWorker) -> int:
        terminal = {
            MuseVideoJobState.COMPLETED,
            MuseVideoJobState.FAILED,
            MuseVideoJobState.QUOTA_EXHAUSTED,
        }
        return sum(
            self.jobs[job_id].state not in terminal
            for job_id in worker.queue
            if job_id in self.jobs
        )

    def _log(self, worker: MuseVideoWorker, message: str) -> None:
        with self._state_lock:
            self._log_locked(worker, message)
            self._persist_locked()

    @staticmethod
    def _log_locked(worker: MuseVideoWorker, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        worker.logs.append(f"[{timestamp}] {message}")
        worker.logs = worker.logs[-100:]

    def _schedule(self, coroutine) -> Future[Any]:
        with self._state_lock:
            if self._closed:
                coroutine.close()
                raise MuseVideoBatchError("MuseVideoBatchManager đã đóng.")
        return self.session_manager._schedule(coroutine)

    def _schedule_for_shutdown(self, coroutine) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coroutine, self.session_manager._loop)

    def _require_worker(self, worker_id: int) -> MuseVideoWorker:
        try:
            return self.workers[int(worker_id)]
        except (KeyError, TypeError, ValueError):
            raise ValueError("Worker Muse phải là 1, 2 hoặc 3.") from None

    def _load(self) -> None:
        raw = read_json(self.checkpoint_path, {}) or {}
        if not isinstance(raw, dict):
            return
        with self._state_lock:
            self.source_paths = [str(item) for item in raw.get("source_paths", [])]
            self.current_job_ids = [str(item) for item in raw.get("current_job_ids", [])]
            self.prompt = str(raw.get("prompt") or "")
            self.output_dir = str(raw.get("output_dir") or "")
            self.settings = MuseVideoSettings.from_dict(raw.get("settings") or {})
            for value in raw.get("jobs", []):
                if isinstance(value, dict):
                    job = MuseVideoJob.from_dict(value)
                    if job.job_id:
                        self.jobs[job.job_id] = job
            worker_values = {
                int(value.get("worker_id")): value
                for value in raw.get("workers", [])
                if isinstance(value, dict) and str(value.get("worker_id", "")).isdigit()
            }
            for worker_id, worker in self.workers.items():
                value = worker_values.get(worker_id, {})
                worker.queue = [str(item) for item in value.get("queue", []) if str(item) in self.jobs]
                worker.assigned_sources = [str(item) for item in value.get("assigned_sources", [])]
                worker.logs = [str(item) for item in value.get("logs", [])][-100:]
                previous = str(value.get("state") or MuseVideoWorkerState.IDLE.value)
                if previous in {
                    MuseVideoWorkerState.QUOTA_EXHAUSTED.value,
                    MuseVideoWorkerState.FAILED.value,
                    MuseVideoWorkerState.COMPLETED.value,
                }:
                    worker.state = MuseVideoWorkerState(previous)
                elif worker.queue:
                    worker.state = MuseVideoWorkerState.PAUSED
            self._sync_assigned_from_queues_locked()
            self._persist_locked()

    def _persist_locked(self) -> None:
        write_json(
            self.checkpoint_path,
            {
                "version": 1,
                "source_paths": self.source_paths,
                "current_job_ids": self.current_job_ids,
                "prompt": self.prompt,
                "output_dir": self.output_dir,
                "settings": asdict(self.settings),
                "jobs": [job.to_dict() for job in self.jobs.values()],
                "workers": [
                    {
                        "worker_id": worker.worker_id,
                        "queue": worker.queue,
                        "assigned_sources": worker.assigned_sources,
                        "state": worker.state.value,
                        "logs": worker.logs,
                    }
                    for worker in self.workers.values()
                ],
            },
        )


def create_muse_video_job_id(source: str | Path, prompt: str, settings: MuseVideoSettings) -> str:
    path = Path(source).expanduser().resolve()
    stat = path.stat()
    file_hash = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            file_hash.update(chunk)
    payload = {
        "path": str(path).casefold(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "image_sha256": file_hash.hexdigest(),
        "prompt_sha256": hashlib.sha256(str(prompt).encode("utf-8")).hexdigest(),
        "settings": asdict(settings.normalized()),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _output_filename(source: Path, account_id: str, job_id: str) -> str:
    stem = re.sub(r'[^A-Za-z0-9._-]+', "_", source.stem).strip("._") or "image"
    safe_account = re.sub(r"[^A-Za-z0-9_-]+", "_", account_id).strip("_") or "account"
    return f"{stem}__{safe_account}__{job_id[:16]}.mp4"


def _valid_mp4(path: Path) -> bool:
    try:
        if not path.is_file() or path.suffix.casefold() != ".mp4" or path.stat().st_size <= 0:
            return False
        with path.open("rb") as stream:
            header = stream.read(64)
        return b"ftyp" in header
    except OSError:
        return False


def _clickable(element: Any) -> bool:
    try:
        return bool(element.is_displayed() and element.is_enabled())
    except Exception:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_batch_singleton: MuseVideoBatchManager | None = None
_batch_singleton_lock = threading.Lock()


def get_muse_video_batch_manager(
    *,
    session_manager: MuseSessionManager | None = None,
) -> MuseVideoBatchManager:
    global _batch_singleton
    with _batch_singleton_lock:
        if _batch_singleton is None or _batch_singleton._closed:
            _batch_singleton = MuseVideoBatchManager(session_manager=session_manager)
        return _batch_singleton
