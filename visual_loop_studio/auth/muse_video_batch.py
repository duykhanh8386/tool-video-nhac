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
MUSE_IMAGES_PER_REQUEST = 3
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
        "button[aria-label='Attach file']",
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
        "textarea[aria-label='Message']",
        "textarea[placeholder='Message']",
        "[data-testid='prompt-input']",
        "[data-testid*='composer' i] textarea",
        "textarea[aria-label*='prompt' i]",
        "textarea[placeholder*='prompt' i]",
        "[contenteditable='true'][role='textbox'][aria-label*='prompt' i]",
        "textarea",
        "[contenteditable='true'][role='textbox']",
    )
    generate_buttons: tuple[str, ...] = (
        "button[aria-label='Send']",
        "button[aria-label*='send message' i]",
        "button[type='submit'][aria-label*='send' i]",
        "button[data-testid*='generate' i]",
        "button[data-testid*='create' i]",
        "button[aria-label*='generate' i]",
        "button[aria-label*='create video' i]",
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
        "button[aria-label='Download']",
        "button[data-testid*='download' i]",
        "button[aria-label*='download' i]",
        "[role='button'][aria-label*='download' i]",
        "a[download]",
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
    submission_group_id: str = ""
    submission_index: int = 0
    submission_size: int = 1
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
            submission_group_id=str(value.get("submission_group_id") or ""),
            submission_index=max(0, int(value.get("submission_index") or 0)),
            submission_size=max(1, int(value.get("submission_size") or 1)),
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
    enabled_worker_ids: tuple[int, ...]
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
        result = self.process_batch(driver, [job], [context])[job.job_id]
        if isinstance(result, BaseException):
            raise result
        return result

    def process_batch(
        self,
        driver: Any,
        jobs: list[MuseVideoJob],
        contexts: list[MuseVideoRunContext],
    ) -> dict[str, Path | BaseException]:
        """Submit up to three images with one prompt, then bind distinct new videos in DOM order."""
        if not jobs or len(jobs) != len(contexts):
            raise MuseVideoBatchError("Nhóm Muse không có đủ job/context để xử lý.")
        if len(jobs) > MUSE_IMAGES_PER_REQUEST:
            raise MuseVideoBatchError("Mỗi lượt Muse chỉ được gửi tối đa 3 ảnh.")
        if any(job.submitted or job.submission_attempted for job in jobs):
            raise MuseVideoBatchError("Job đã gửi phải được khôi phục riêng, không được gửi lại theo nhóm.")
        prompt = jobs[0].prompt
        settings = jobs[0].settings
        if any(job.prompt != prompt or job.settings != settings for job in jobs):
            raise MuseVideoBatchError("Ba ảnh trong cùng lượt phải dùng chung prompt và cài đặt.")
        sources = [Path(job.source_path) for job in jobs]
        if any(not source.is_file() for source in sources):
            raise MuseVideoBatchError("Một hoặc nhiều ảnh nguồn không còn tồn tại.")
        primary = contexts[0]
        self._check(driver, primary)
        primary.log(f"Đang chuẩn bị một lượt gồm {len(jobs)} ảnh trên Muse…")
        self._navigate_new_task(driver, primary)
        baseline_previews = self._element_fingerprints(driver, self.selectors.previews)
        for index, (job, source, context) in enumerate(zip(jobs, sources, contexts), start=1):
            context.transition(MuseVideoJobState.UPLOADING, progress=5 + index * 3, error="")
            context.log(f"Đang đính kèm ảnh {index}/{len(jobs)}: {source.name}")
            self._upload(driver, source)
            self._wait(
                driver,
                lambda expected=index: len(
                    self._element_fingerprints(driver, self.selectors.previews) - baseline_previews
                ) >= expected,
                self.upload_timeout,
                context,
                f"Muse chưa hiển thị đủ preview cho ảnh thứ {index}.",
            )
        primary.log(f"Muse đã nhận đủ {len(jobs)} ảnh; đang điền một prompt chung…")
        self._fill_prompt(driver, prompt)
        self._apply_settings(driver, settings)
        baseline_videos = sorted(self._video_fingerprints(driver))
        group_id = hashlib.sha256("|".join(job.job_id for job in jobs).encode("utf-8")).hexdigest()[:24]
        for index, context in enumerate(contexts):
            context.transition(
                MuseVideoJobState.READY_TO_GENERATE,
                progress=18,
                baseline_videos=baseline_videos,
                submission_group_id=group_id,
                submission_index=index,
                submission_size=len(jobs),
            )
            context.transition(
                MuseVideoJobState.SUBMITTING,
                progress=20,
                submission_attempted=True,
            )
        self._click_generate_once(driver)
        submitted_at = _utc_now()
        for context in contexts:
            context.transition(
                MuseVideoJobState.SUBMITTED,
                progress=22,
                submitted=True,
                submitted_at=submitted_at,
            )
        primary.log(
            f"Đã gửi một prompt cho {len(jobs)} ảnh; đang chờ đủ {len(jobs)} video mới trước khi tải."
        )
        videos = self._wait_for_distinct_videos(
            driver,
            expected=len(jobs),
            baseline=set(baseline_videos),
            context=primary,
        )
        results: dict[str, Path | BaseException] = {}
        for index, (job, context, video) in enumerate(zip(jobs, contexts, videos), start=1):
            fingerprint = _video_fingerprint(video)
            context.log(f"Đang tải video {index}/{len(jobs)} đúng theo thứ tự ảnh đã gửi…")
            context.transition(
                MuseVideoJobState.DOWNLOADING,
                progress=85 + round(index / len(jobs) * 10),
                result_fingerprint=fingerprint,
                download_attempts=job.download_attempts + 1,
            )
            try:
                results[job.job_id] = self._download(
                    driver,
                    video,
                    Path(job.output_path),
                    context,
                )
            except (MuseVideoStopped, MuseVideoLoginRequired, MuseVideoQuotaExhausted, MuseVideoDriverError):
                raise
            except BaseException as exc:
                results[job.job_id] = exc
        return results

    def recover_batch(
        self,
        driver: Any,
        jobs: list[MuseVideoJob],
        contexts: list[MuseVideoRunContext],
    ) -> dict[str, Path | BaseException]:
        """Recover a submitted group without sending the prompt again."""
        if not jobs or len(jobs) != len(contexts):
            raise MuseVideoDownloadError("Nhóm khôi phục Muse không hợp lệ.")
        primary = contexts[0]
        self._check(driver, primary)
        expected = max(job.submission_size for job in jobs)
        baseline = set(jobs[0].baseline_videos)
        primary.log(f"Khôi phục lượt đã gửi gồm {expected} video; không bấm Send lần hai.")
        videos = self._wait_for_distinct_videos(
            driver,
            expected=expected,
            baseline=baseline,
            context=primary,
        )
        by_fingerprint = {_video_fingerprint(video): video for video in videos}
        results: dict[str, Path | BaseException] = {}
        claimed: set[str] = set()
        for job, context in zip(jobs, contexts):
            target = Path(job.output_path)
            if target.is_file() and _valid_mp4(target):
                results[job.job_id] = target
                continue
            video = by_fingerprint.get(job.result_fingerprint) if job.result_fingerprint else None
            if video is None and 0 <= job.submission_index < len(videos):
                video = videos[job.submission_index]
            if video is None:
                results[job.job_id] = MuseVideoDownloadError(
                    "Không ghép được video đã tạo với đúng ảnh trong nhóm cũ."
                )
                continue
            fingerprint = _video_fingerprint(video)
            if fingerprint in claimed:
                results[job.job_id] = MuseVideoDownloadError(
                    "Muse trả về video trùng cho hai ảnh; tool từ chối lưu sai file."
                )
                continue
            claimed.add(fingerprint)
            context.transition(
                MuseVideoJobState.DOWNLOADING,
                progress=90,
                result_fingerprint=fingerprint,
                download_attempts=job.download_attempts + 1,
            )
            try:
                results[job.job_id] = self._download(driver, video, target, context)
            except (MuseVideoStopped, MuseVideoLoginRequired, MuseVideoQuotaExhausted, MuseVideoDriverError):
                raise
            except BaseException as exc:
                results[job.job_id] = exc
        return results

    def _wait_for_distinct_videos(
        self,
        driver: Any,
        *,
        expected: int,
        baseline: set[str],
        context: MuseVideoRunContext,
    ) -> list[Any]:
        transient_retries = 0

        def find_results():
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
            values: list[Any] = []
            seen: set[str] = set()
            for video in self._videos(driver):
                fingerprint = _video_fingerprint(video)
                if fingerprint in baseline or fingerprint in seen or not self._video_ready(driver, video):
                    continue
                seen.add(fingerprint)
                values.append(video)
            return values[:expected] if len(values) >= expected else False

        return list(
            self._wait(
                driver,
                find_results,
                self.timeout,
                context,
                f"Muse chưa trả về đủ {expected} video mới; lượt đã gửi sẽ không tự gửi lại.",
            )
        )

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
        context.log("Đã phát hiện video mới hoàn tất; đang tải xuống…")
        context.transition(
            MuseVideoJobState.DOWNLOADING,
            progress=90,
            result_fingerprint=fingerprint,
            download_attempts=job.download_attempts + 1,
        )
        downloaded = self._download(driver, video, target, context)
        return downloaded

    def _navigate_new_task(self, driver: Any, context: MuseVideoRunContext) -> None:
        self._check(driver, context)

        def composer_ready():
            self._check(driver, context)
            has_prompt = any(_clickable(item) for item in self._elements(driver, self.selectors.prompt_inputs))
            has_upload = bool(self._elements(driver, self.selectors.upload_inputs))
            return has_prompt and has_upload

        if composer_ready():
            return
        self._click_selector(driver, self.selectors.new_task)
        if not composer_ready():
            self._retry_network(lambda: driver.get(self.start_url), context)
            self._check(driver, context)
            if not composer_ready():
                self._click_selector(driver, self.selectors.new_task)
        self._wait(
            driver,
            composer_ready,
            min(45.0, self.upload_timeout),
            context,
            "Muse đã đăng nhập nhưng chưa hiển thị ô Message và input đính kèm tệp.",
        )

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
                    "const v=arguments[0],sel=\"button[aria-label*='download' i],"
                    "[data-testid*='download' i],[role='button'][aria-label*='download' i],a[download]\";"
                    "const visible=e=>!!(e&&e.offsetParent!==null);"
                    "const center=e=>{const r=e.getBoundingClientRect();return [(r.left+r.right)/2,(r.top+r.bottom)/2]};"
                    "const dist=(a,b)=>Math.abs(a[0]-b[0])+Math.abs(a[1]-b[1]);"
                    "const vc=center(v),buttons=[...document.querySelectorAll(sel)].filter(visible);"
                    "const valid=buttons.filter(b=>{let n=b.parentElement,depth=0;"
                    "while(n&&n!==document.body&&depth++<10){const videos=[...n.querySelectorAll('video')].filter(visible);"
                    "if(videos.includes(v)){return videos.sort((a,c)=>dist(center(a),center(b))-dist(center(c),center(b)))[0]===v;}"
                    "n=n.parentElement;}return false;});"
                    "if(!valid.length)return false;valid.sort((a,b)=>dist(center(a),vc)-dist(center(b),vc));"
                    "valid[0].click();return true;",
                    video,
                )
            )
        except Exception:
            clicked = False
        if not clicked:
            try:
                clicked = bool(driver.execute_script(
                    "const v=arguments[0],a=document.createElement('a');"
                    "const src=v.currentSrc||v.src;if(!src)return false;"
                    "a.href=src;a.download='muse-video.mp4';"
                    "document.body.appendChild(a);a.click();a.remove();return true;",
                    video,
                ))
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
        self.enabled_worker_ids: list[int] = list(range(1, MUSE_SESSION_COUNT + 1))
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

    def set_enabled_workers(self, worker_ids: Iterable[int]) -> tuple[int, ...]:
        """Persist which account slots may receive new work."""
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Không thể đổi tài khoản khi batch Muse đang chạy.")
            selected = self._normalize_worker_ids(worker_ids)
            self.enabled_worker_ids = list(selected)
            self._persist_locked()
            return selected

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

    def allocate_images(
        self,
        paths: Iterable[str | Path],
        worker_ids: Iterable[int] | None = None,
    ) -> tuple[tuple[str, ...], ...]:
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
            selected_ids = self._normalize_worker_ids(worker_ids)
            self.enabled_worker_ids = list(selected_ids)
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
                worker_id = selected_ids[index % len(selected_ids)]
                self.workers[worker_id].assigned_sources.append(str(path))
            self._persist_locked()
            return tuple(tuple(worker.assigned_sources) for worker in self.workers.values())

    def start_all(
        self,
        prompt: str,
        settings: MuseVideoSettings,
        output_dir: str | Path,
        worker_ids: Iterable[int] | None = None,
    ) -> Future[Any]:
        self.prepare_start(prompt, settings, output_dir, worker_ids=worker_ids)
        ready_ids = self._ready_session_ids(self.enabled_worker_ids)
        with self._state_lock:
            self._build_jobs_locked(self.enabled_worker_ids)
            self._persist_locked()
        return self._schedule(self._launch_workers(resume=False, worker_ids=ready_ids))

    def prepare_start(
        self,
        prompt: str,
        settings: MuseVideoSettings,
        output_dir: str | Path,
        *,
        worker_ids: Iterable[int] | None = None,
    ) -> None:
        """Checkpoint the user's batch inputs before interactive login begins."""
        text = str(prompt or "").strip()
        if not text:
            raise ValueError("Prompt tạo video chung không được để trống.")
        normalized_settings = settings.normalized()
        if not str(output_dir or "").strip():
            raise ValueError("Hãy chọn thư mục lưu video đầu ra.")
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        with self._state_lock:
            if not self.source_paths:
                raise ValueError("Hãy chọn và phân bổ ảnh trước khi bắt đầu.")
            if self.busy:
                raise MuseVideoBatchError("Batch Muse đang chạy.")
            selected_ids = self._normalize_worker_ids(worker_ids)
            self.enabled_worker_ids = list(selected_ids)
            for worker in self.workers.values():
                worker.assigned_sources = []
            for index, source in enumerate(self.source_paths):
                worker_id = selected_ids[index % len(selected_ids)]
                self.workers[worker_id].assigned_sources.append(source)
            self.prompt = text
            self.settings = normalized_settings
            self.output_dir = str(output)
            self._persist_locked()

    def resume(self) -> Future[Any]:
        ready_ids = self._ready_session_ids(self.enabled_worker_ids)
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Batch Muse đang chạy.")
            if not self.current_job_ids:
                raise MuseVideoBatchError("Không có batch Muse để tiếp tục.")
        return self._schedule(self._launch_workers(resume=True, worker_ids=ready_ids))

    def start_ready_workers(self, worker_ids: Iterable[int] | None = None) -> Future[Any]:
        """Start newly READY workers without interrupting workers already running."""
        ready_ids = set(self._ready_session_ids(self.enabled_worker_ids))
        if worker_ids is not None:
            ready_ids.intersection_update(int(value) for value in worker_ids)
        with self._state_lock:
            if not self.current_job_ids:
                raise MuseVideoBatchError("Không có batch Muse đang chờ để chạy.")
        return self._schedule(self._launch_workers(resume=True, worker_ids=ready_ids))

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

    def redistribute_unsubmitted(
        self,
        worker_ids: Iterable[int] | None = None,
    ) -> tuple[tuple[str, ...], ...]:
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Hãy dừng toàn bộ worker trước khi phân bổ lại.")
            selected_ids = self._normalize_worker_ids(worker_ids)
            self.enabled_worker_ids = list(selected_ids)
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
                worker_id = selected_ids[index % len(selected_ids)]
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
                enabled_worker_ids=tuple(self.enabled_worker_ids),
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

    async def _launch_workers(
        self,
        *,
        resume: bool,
        worker_ids: Iterable[int] | None = None,
    ) -> dict[int, Any]:
        if self._manager_lock is None:
            self._manager_lock = asyncio.Lock()
        eligible_ids = set(self.workers) if worker_ids is None else {int(value) for value in worker_ids}
        claimed: list[tuple[MuseVideoWorker, asyncio.Task[Any]]] = []
        async with self._manager_lock:
            for worker in self.workers.values():
                session = self.session_manager.sessions[worker.session_id]
                if (
                    worker.worker_id not in eligible_ids
                    or worker.active
                    or session.state != MuseSessionState.READY
                    or not session.driver_open
                    or self._worker_pending(worker) == 0
                ):
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
            while True:
                if worker.stop_event.is_set():
                    break
                with self._state_lock:
                    candidates = [
                        self.jobs[job_id]
                        for job_id in worker.queue
                        if job_id in self.jobs
                        and self.jobs[job_id].state
                        not in {
                            MuseVideoJobState.COMPLETED,
                            MuseVideoJobState.QUOTA_EXHAUSTED,
                            MuseVideoJobState.FAILED,
                        }
                    ]
                    if not candidates:
                        break
                    first = candidates[0]
                    if first.state == MuseVideoJobState.LOGIN_REQUIRED:
                        first.state = MuseVideoJobState.PAUSED
                    if (
                        (first.submitted or first.submission_attempted)
                        and first.account_id
                        and first.account_id != session.account_id
                    ):
                        first.state = MuseVideoJobState.FAILED
                        first.error = (
                            "Job đã gửi thuộc tài khoản khác; tool từ chối gửi lại hoặc khôi phục bằng profile hiện tại."
                        )
                        worker.state = MuseVideoWorkerState.FAILED
                        worker.error = first.error
                        self._log_locked(worker, first.error)
                        self._persist_locked()
                        continue
                    if first.submitted or first.submission_attempted:
                        if first.submission_group_id:
                            jobs = [
                                candidate
                                for candidate in candidates
                                if candidate.submission_group_id == first.submission_group_id
                            ]
                        else:
                            jobs = [first]
                    else:
                        jobs = []
                        for candidate in candidates:
                            if candidate.submitted or candidate.submission_attempted:
                                break
                            if candidate.state == MuseVideoJobState.LOGIN_REQUIRED:
                                candidate.state = MuseVideoJobState.PAUSED
                            jobs.append(candidate)
                            if len(jobs) >= MUSE_IMAGES_PER_REQUEST:
                                break
                    worker.current_job_id = jobs[0].job_id
                    worker.progress = 0
                    for job in jobs:
                        job.attempts += 1
                        if not job.started_at:
                            job.started_at = _utc_now()
                    names = ", ".join(Path(job.source_path).name for job in jobs)
                    self._log_locked(worker, f"Đang xử lý lượt {len(jobs)} ảnh: {names}")
                    self._persist_locked()
                contexts = [
                    MuseVideoRunContext(
                        worker_id=worker.worker_id,
                        download_dir=worker.download_dir,
                        stopped=worker.stop_event.is_set,
                        transition=lambda state, _job_id=job.job_id, **changes: self._transition(
                            worker.worker_id, _job_id, state, **changes
                        ),
                        log=lambda message, _worker=worker: self._log(_worker, message),
                        retry_limit=self.retry_limit,
                        backoff_base=self.backoff_base,
                    )
                    for job in jobs
                ]

                def run_group() -> dict[str, Path | BaseException]:
                    if jobs[0].submitted or jobs[0].submission_attempted:
                        recover_batch = getattr(self.automation, "recover_batch", None)
                        if jobs[0].submission_group_id and callable(recover_batch):
                            return recover_batch(session.driver, jobs, contexts)
                        values: dict[str, Path | BaseException] = {}
                        for job, context in zip(jobs, contexts):
                            values[job.job_id] = self.automation.process(session.driver, job, context)
                        return values
                    process_batch = getattr(self.automation, "process_batch", None)
                    if callable(process_batch):
                        return process_batch(session.driver, jobs, contexts)
                    values: dict[str, Path | BaseException] = {}
                    for job, context in zip(jobs, contexts):
                        values[job.job_id] = self.automation.process(session.driver, job, context)
                    return values

                try:
                    results = await self.session_manager._loop.run_in_executor(
                        session.executor,
                        run_group,
                    )
                except MuseVideoStopped:
                    with self._state_lock:
                        for job in jobs:
                            if job.state != MuseVideoJobState.COMPLETED:
                                job.state = MuseVideoJobState.PAUSED
                        worker.state = MuseVideoWorkerState.PAUSED
                        self._log_locked(worker, "Worker đã dừng; dữ liệu và hàng đợi được giữ nguyên.")
                        self._persist_locked()
                    break
                except MuseVideoLoginRequired as exc:
                    with self._state_lock:
                        for job in jobs:
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
                        for job in jobs:
                            job.state = MuseVideoJobState.QUOTA_EXHAUSTED
                            job.error = str(exc)
                        worker.state = MuseVideoWorkerState.QUOTA_EXHAUSTED
                        worker.error = str(exc)
                        self._log_locked(worker, "Quota/rate limit: không chuyển job sang tài khoản khác.")
                        self._persist_locked()
                    break
                except MuseVideoDriverError as exc:
                    with self._state_lock:
                        for job in jobs:
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
                        for job in jobs:
                            job.state = MuseVideoJobState.FAILED
                            job.error = message
                        worker.error = message
                        self._log_locked(worker, f"Lỗi lượt {len(jobs)} ảnh: {message}")
                        self._persist_locked()
                    continue
                else:
                    with self._state_lock:
                        for job in jobs:
                            result = results.get(job.job_id)
                            if isinstance(result, BaseException) or result is None:
                                message = str(result) if isinstance(result, MuseVideoBatchError) else (
                                    "Muse/Chrome không phản hồi khi tải video; có thể chạy lại bước download."
                                )
                                job.state = MuseVideoJobState.FAILED
                                job.error = message
                                worker.error = message
                                self._log_locked(worker, f"Lỗi {Path(job.source_path).name}: {message}")
                                continue
                            try:
                                result_path = Path(result)
                            except (TypeError, ValueError):
                                result_path = Path()
                            if not _valid_mp4(result_path):
                                message = (
                                    "Muse đã trả kết quả nhưng chưa có file MP4 hợp lệ; "
                                    "job chỉ được phép chạy lại bước download."
                                )
                                job.state = MuseVideoJobState.FAILED
                                job.error = message
                                worker.error = message
                                self._log_locked(worker, f"Chưa tải xong {Path(job.source_path).name}: {message}")
                                continue
                            job.output_path = str(result_path)
                            job.state = MuseVideoJobState.COMPLETED
                            job.completed_at = _utc_now()
                            job.error = ""
                            self._log_locked(worker, f"Hoàn tất {Path(job.source_path).name}")
                        worker.progress = 100
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

    def _build_jobs_locked(self, worker_ids: Iterable[int] | None = None) -> None:
        selected_ids = self._normalize_worker_ids(worker_ids)
        self.current_job_ids = []
        for worker in self.workers.values():
            worker.queue = []
            session = self.session_manager.sessions[worker.session_id]
            if session.state == MuseSessionState.READY and session.driver_open:
                worker.state = MuseVideoWorkerState.READY
            elif session.state == MuseSessionState.LOGIN_REQUIRED:
                worker.state = MuseVideoWorkerState.LOGIN_REQUIRED
            else:
                worker.state = MuseVideoWorkerState.IDLE
            worker.error = ""
            worker.account_id = session.account_id
            worker.email = session.email
        for index, source_value in enumerate(self.source_paths):
            worker_id = selected_ids[index % len(selected_ids)]
            source = Path(source_value)
            session = self.session_manager.sessions[worker_id]
            job_id = create_muse_video_job_id(source, self.prompt, self.settings)
            output = Path(self.output_dir) / _output_filename(source, session.account_id, job_id)
            if output.exists() and not _valid_mp4(output):
                output = _available_output_path(output)
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
            else:
                current_output = Path(job.output_path)
                if job.state == MuseVideoJobState.COMPLETED:
                    if _valid_mp4(output):
                        # The requested output folder already contains the
                        # exact completed file, so keep the job terminal.
                        job.output_path = str(output)
                    elif _valid_mp4(current_output):
                        # A checkpoint can point at an older output folder.
                        # Copy the verified MP4 to the folder selected now;
                        # never submit the image to Muse a second time.
                        if _path_key(current_output) != _path_key(output):
                            try:
                                if output.exists():
                                    raise OSError("target exists")
                                output.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(current_output, output)
                            except OSError:
                                job.state = MuseVideoJobState.FAILED
                                job.error = (
                                    "Không thể chép MP4 đã hoàn tất sang thư mục output hiện tại; "
                                    "tool không ghi đè file có sẵn."
                                )
                            else:
                                if _valid_mp4(output):
                                    job.output_path = str(output)
                                else:
                                    job.state = MuseVideoJobState.FAILED
                                    job.error = "Bản sao MP4 trong thư mục output hiện tại không hợp lệ."
                    else:
                        # Do not trust a terminal checkpoint when its artifact
                        # has disappeared. A submitted job stays bound to its
                        # original account and resumes download only.
                        job.output_path = str(output)
                        job.state = (
                            MuseVideoJobState.DOWNLOADING
                            if job.submitted or job.submission_attempted
                            else MuseVideoJobState.PENDING
                        )
                        job.completed_at = ""
                        job.error = "File MP4 không còn tồn tại; đang khôi phục bước download."
                elif job.submitted or job.submission_attempted:
                    # A submitted result may safely be downloaded into a newly
                    # selected folder, but it must never change account/worker.
                    if not _valid_mp4(current_output):
                        job.output_path = str(output)

                if (
                    not job.submitted
                    and not job.submission_attempted
                    and job.state != MuseVideoJobState.COMPLETED
                ):
                    job.worker_id = worker_id
                    job.account_id = session.account_id
                    job.email = session.email
                    job.output_path = str(output)
            self.current_job_ids.append(job_id)
            assigned_worker = self.workers[job.worker_id]
            assigned_worker.queue.append(job_id)
        self._sync_assigned_from_queues_locked()

    def _ready_session_ids(self, worker_ids: Iterable[int] | None = None) -> tuple[int, ...]:
        selected_ids = set(self._normalize_worker_ids(worker_ids))
        snapshots = self.session_manager.snapshots()
        ready = tuple(
            item.session_id
            for item in snapshots
            if item.session_id in selected_ids
            and item.state == MuseSessionState.READY
            and item.driver_open
        )
        if not ready:
            raise MuseVideoBatchError("Chưa có tài khoản Muse nào READY để chạy batch.")
        emails = [item.email.casefold() for item in snapshots if item.session_id in ready]
        if len(set(emails)) != len(emails):
            raise MuseVideoBatchError("Các worker READY phải dùng các tài khoản khác nhau.")
        return ready

    def _normalize_worker_ids(self, worker_ids: Iterable[int] | None) -> tuple[int, ...]:
        values = self.enabled_worker_ids if worker_ids is None else worker_ids
        try:
            selected = tuple(sorted({int(value) for value in values}))
        except (TypeError, ValueError):
            raise ValueError("Danh sách tài khoản Muse được chọn không hợp lệ.") from None
        if not selected:
            raise ValueError("Hãy tick ít nhất một tài khoản Muse để chạy.")
        if any(worker_id not in self.workers for worker_id in selected):
            raise ValueError("Tài khoản Muse được chọn phải là 1, 2 hoặc 3.")
        return selected

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
            raw_enabled = raw.get("enabled_worker_ids", list(range(1, MUSE_SESSION_COUNT + 1)))
            try:
                self.enabled_worker_ids = list(self._normalize_worker_ids(raw_enabled))
            except ValueError:
                self.enabled_worker_ids = list(range(1, MUSE_SESSION_COUNT + 1))
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
                "version": 2,
                "source_paths": self.source_paths,
                "current_job_ids": self.current_job_ids,
                "enabled_worker_ids": self.enabled_worker_ids,
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


def _path_key(path: Path) -> str:
    try:
        return str(path.expanduser().resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _available_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}__retry_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise MuseVideoBatchError("Không tạo được tên file MP4 khôi phục không trùng trong thư mục output.")


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
