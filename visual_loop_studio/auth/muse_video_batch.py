from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import threading
import time
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
    MuseSessionAuthenticationError,
    MuseSessionError,
    MuseSessionManager,
    MuseSessionState,
    get_muse_session_manager,
)
from utils.config import read_json, write_json
from utils.paths import DATA_DIR


MUSE_VIDEO_BATCH_CHECKPOINT = DATA_DIR / "muse_video_batch.json"
MUSE_IMAGES_PER_REQUEST = 3
MUSE_JOB_PROTOCOL = "single-image-exact-video-v3"
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
    image_results: tuple[str, ...] = (
        "[data-testid*='image-result' i] img",
        "[data-testid*='result' i] img",
        "img[src^='blob:']",
        "img",
    )
    mode_switch_image: tuple[str, ...] = (
        "button[data-testid*='image' i]",
        "[role='tab'][aria-label*='image' i]",
        "[role='button'][aria-label*='image' i]",
        "button[aria-label*='image' i]",
    )
    mode_switch_video: tuple[str, ...] = (
        "button[data-testid*='video' i]",
        "[role='tab'][aria-label*='video' i]",
        "[role='button'][aria-label*='video' i]",
        "button[aria-label*='video' i]",
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
    task_mode: str = "video"  # "video" | "image"

    def normalized(self) -> "MuseVideoSettings":
        raw_mode = str(self.task_mode or "video").strip().casefold()
        task_mode = "image" if raw_mode == "image" else "video"
        quantity = int(self.quantity or 1)
        if task_mode == "video" and quantity != 1:
            raise ValueError("Batch Muse hiện chỉ cho phép đúng một video cho mỗi ảnh.")
        quantity = max(1, quantity)
        return MuseVideoSettings(
            model=str(self.model or "").strip(),
            aspect_ratio=str(self.aspect_ratio or "").strip(),
            duration="" if task_mode == "image" else str(self.duration or "").strip(),
            resolution=str(self.resolution or "").strip(),
            quantity=quantity,
            task_mode=task_mode,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MuseVideoSettings":
        return cls(
            model=str(value.get("model") or ""),
            aspect_ratio=str(value.get("aspect_ratio") or ""),
            duration=str(value.get("duration") or ""),
            resolution=str(value.get("resolution") or ""),
            quantity=int(value.get("quantity") or 1),
            task_mode=str(value.get("task_mode") or "video"),
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
    task_mode: str = "video"
    submitted: bool = False
    submission_attempted: bool = False
    baseline_videos: list[str] = field(default_factory=list)
    baseline_images: list[str] = field(default_factory=list)
    submission_group_id: str = ""
    submission_index: int = 0
    submission_size: int = 1
    baseline_message_ids: list[str] = field(default_factory=list)
    submission_message_floor: int = 0
    submission_message_id: str = ""
    submission_message_group_id: str = ""
    submission_message_index: int = -1
    submission_watch_token: str = ""
    submission_started_epoch_ms: int = 0
    baseline_session_fingerprints: list[str] = field(default_factory=list)
    result_session_fingerprint: str = ""
    result_session_text: str = ""
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
        value["task_mode"] = self.task_mode
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
        parsed_settings = MuseVideoSettings.from_dict(value.get("settings") or {})
        task_mode = str(value.get("task_mode") or parsed_settings.task_mode or "video")
        return cls(
            job_id=str(value.get("job_id") or ""),
            source_path=str(value.get("source_path") or ""),
            worker_id=int(value.get("worker_id") or 1),
            account_id=str(value.get("account_id") or ""),
            email=str(value.get("email") or ""),
            prompt=str(value.get("prompt") or ""),
            settings=parsed_settings,
            output_path=str(value.get("output_path") or ""),
            state=state,
            task_mode=task_mode,
            submitted=submitted,
            submission_attempted=attempted,
            baseline_videos=[str(item) for item in value.get("baseline_videos", [])],
            baseline_images=[str(item) for item in value.get("baseline_images", [])],
            submission_group_id=str(value.get("submission_group_id") or ""),
            submission_index=max(0, int(value.get("submission_index") or 0)),
            submission_size=max(1, int(value.get("submission_size") or 1)),
            baseline_message_ids=[str(item) for item in value.get("baseline_message_ids", []) if str(item)],
            submission_message_floor=max(0, int(value.get("submission_message_floor") or 0)),
            submission_message_id=str(value.get("submission_message_id") or ""),
            submission_message_group_id=str(value.get("submission_message_group_id") or ""),
            submission_message_index=int(
                value.get("submission_message_index")
                if value.get("submission_message_index") is not None
                else -1
            ),
            submission_watch_token=str(value.get("submission_watch_token") or ""),
            submission_started_epoch_ms=max(0, int(value.get("submission_started_epoch_ms") or 0)),
            baseline_session_fingerprints=[
                str(item) for item in value.get("baseline_session_fingerprints", []) if str(item)
            ],
            result_session_fingerprint=str(value.get("result_session_fingerprint") or ""),
            result_session_text=str(value.get("result_session_text") or ""),
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


@dataclass(frozen=True)
class MuseSubmissionAnchor:
    """Stable boundary for the exact user message created by one Send click."""

    element: Any = None
    message_id: str = ""
    message_group_id: str = ""
    dom_index: int = -1
    prompt: str = ""
    baseline_message_ids: tuple[str, ...] = ()
    message_floor: int = 0
    watch_token: str = ""
    started_epoch_ms: int = 0


@dataclass(frozen=True)
class MuseCompletedSession:
    element: Any
    fingerprint: str
    text: str
    completed_epoch_ms: int = 0
    visual_index: int = 0


@dataclass(frozen=True)
class MuseSummaryArtifact:
    options_button: Any
    fingerprint: str
    text: str
    index: int = 0


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
        self._summary_locks_guard = threading.Lock()
        self._summary_locks: dict[int, threading.Lock] = {}

    def process(self, driver: Any, job: MuseVideoJob, context: MuseVideoRunContext) -> Path:
        existing = Path(job.output_path)
        if existing.is_file() and _valid_artifact(existing, job.settings.task_mode):
            kind = "ảnh" if job.settings.task_mode == "image" else "MP4"
            context.log(f"Đã có {kind} hợp lệ cho job {job.job_id[:12]}; không gửi lại.")
            return existing
        if job.submitted or job.submission_attempted:
            if job.settings.task_mode == "image":
                result = self.recover_image_batch(driver, [job], [context])[job.job_id]
                if isinstance(result, BaseException):
                    raise result
                return result
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
        """Submit up to three inputs, then download ordered artifacts (MP4 or images)."""
        if not jobs or len(jobs) != len(contexts):
            raise MuseVideoBatchError("Nhóm Muse không có đủ job/context để xử lý.")
        if len(jobs) > MUSE_IMAGES_PER_REQUEST:
            raise MuseVideoBatchError("Mỗi lượt Muse chỉ được gửi tối đa 3 tác vụ.")
        if any(job.submitted or job.submission_attempted for job in jobs):
            raise MuseVideoBatchError("Job đã gửi phải được khôi phục riêng, không được gửi lại theo nhóm.")
        prompt = jobs[0].prompt
        settings = jobs[0].settings
        is_image_mode = (settings.task_mode == "image")
        if any(job.prompt != prompt or job.settings != settings for job in jobs):
            raise MuseVideoBatchError("Các tác vụ trong cùng lượt phải dùng chung prompt và cài đặt.")
        sources = [Path(job.source_path) for job in jobs if job.source_path]
        if not is_image_mode and len(sources) != len(jobs):
            raise MuseVideoBatchError("Tác vụ video bắt buộc phải có đủ ảnh nguồn.")
        if any(not source.is_file() for source in sources):
            raise MuseVideoBatchError("Một hoặc nhiều ảnh nguồn không còn tồn tại.")

        primary = contexts[0]
        self._check(driver, primary)
        action_name = "ảnh" if is_image_mode else "video"
        primary.log(f"Đang chuẩn bị một lượt tạo {action_name} gồm {len(jobs)} tác vụ trên Muse…")
        self._navigate_new_task(driver, primary)
        self._switch_mode_if_needed(driver, "image" if is_image_mode else "video")

        if sources:
            baseline_previews = self._element_fingerprints(driver, self.selectors.previews)
            for index, (source, context) in enumerate(zip(sources, contexts), start=1):
                context.transition(MuseVideoJobState.UPLOADING, progress=5 + index * 3, error="")
                context.log(f"Đang đính kèm ảnh {index}/{len(sources)}: {source.name}")
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
            primary.log(f"Muse đã nhận đủ {len(sources)} ảnh tham chiếu; đang điền prompt…")
        else:
            primary.log("Tạo ảnh từ Prompt thuần (không kèm ảnh mẫu); đang điền prompt…")

        self._fill_prompt(driver, prompt, primary)
        self._apply_settings(driver, settings)

        if is_image_mode:
            baseline_images = sorted(self._image_fingerprints(driver))
        else:
            baseline_videos = sorted(self._video_fingerprints(driver))

        message_snapshot = self._submission_messages(driver)
        baseline_message_ids = sorted(
            {
                str(item.get("message_id") or "")
                for item in message_snapshot
                if str(item.get("message_id") or "")
            }
        )
        message_floor = max(
            (int(item.get("dom_index", -1)) + 1 for item in message_snapshot),
            default=0,
        )
        baseline_session_fingerprints = sorted(
            session.fingerprint for session in self._completed_sessions(driver)
        )
        group_id = hashlib.sha256("|".join(job.job_id for job in jobs).encode("utf-8")).hexdigest()[:24]
        watch_token = f"visual-loop-{group_id}"
        if is_image_mode:
            watch_started_epoch_ms = self._install_image_watch(driver, watch_token)
        else:
            watch_started_epoch_ms = self._install_video_watch(driver, watch_token)
        submission_started_epoch_ms = watch_started_epoch_ms or self._browser_epoch_ms(driver)

        for index, context in enumerate(contexts):
            trans_kwargs = {
                "progress": 18,
                "submission_group_id": group_id,
                "submission_index": index,
                "submission_size": len(jobs),
                "baseline_message_ids": baseline_message_ids,
                "submission_message_floor": message_floor,
                "baseline_session_fingerprints": baseline_session_fingerprints,
            }
            if is_image_mode:
                trans_kwargs["baseline_images"] = baseline_images
            else:
                trans_kwargs["baseline_videos"] = baseline_videos

            context.transition(
                MuseVideoJobState.READY_TO_GENERATE,
                **trans_kwargs,
            )
            context.transition(
                MuseVideoJobState.SUBMITTING,
                progress=20,
                submission_attempted=True,
                submission_watch_token=watch_token,
                submission_started_epoch_ms=submission_started_epoch_ms,
            )

        images: list[Any] = []
        videos: list[Any] = []
        try:
            self._click_generate_once(driver, primary)
            submitted_at = _utc_now()
            for context in contexts:
                context.transition(
                    MuseVideoJobState.SUBMITTED,
                    progress=22,
                    submitted=True,
                    submitted_at=submitted_at,
                )
            found_anchor = self._wait_for_submission_message(
                driver,
                prompt=prompt,
                baseline_message_ids=set(baseline_message_ids),
                message_floor=message_floor,
                context=primary,
            )
            anchor = MuseSubmissionAnchor(
                element=found_anchor.element,
                message_id=found_anchor.message_id,
                message_group_id=found_anchor.message_group_id,
                dom_index=found_anchor.dom_index,
                prompt=found_anchor.prompt,
                baseline_message_ids=found_anchor.baseline_message_ids,
                message_floor=found_anchor.message_floor,
                watch_token=watch_token,
                started_epoch_ms=submission_started_epoch_ms,
            )
            for context in contexts:
                context.transition(
                    MuseVideoJobState.GENERATING,
                    progress=24,
                    submission_message_id=anchor.message_id,
                    submission_message_group_id=anchor.message_group_id,
                    submission_message_index=anchor.dom_index,
                )
            primary.log(
                f"Đã gửi lúc {submitted_at}; đang chờ đủ {len(jobs)} {action_name} của đúng tin nhắn hoàn tất."
            )
            if is_image_mode:
                images = self._wait_for_distinct_images(
                    driver,
                    expected=len(jobs),
                    baseline=set(baseline_images),
                    anchor=anchor,
                    context=primary,
                    baseline_sessions=set(baseline_session_fingerprints),
                )
                for context in contexts:
                    context.transition(MuseVideoJobState.GENERATING, progress=78)
            else:
                videos = self._wait_for_distinct_videos(
                    driver,
                    expected=len(jobs),
                    baseline=set(baseline_videos),
                    anchor=anchor,
                    context=primary,
                    baseline_sessions=set(baseline_session_fingerprints),
                )
                for context in contexts:
                    context.transition(MuseVideoJobState.GENERATING, progress=78)
        finally:
            if is_image_mode:
                self._stop_image_watch(driver, watch_token)
            else:
                self._stop_video_watch(driver, watch_token)

        if is_image_mode:
            if not images:
                primary.log("Chưa thấy ảnh trực tiếp trong chat; đang chuyển sang kiểm tra khôi phục ảnh...")
                return self.recover_image_batch(driver, jobs, contexts)
            primary.log("Ảnh đã xuất hiện dưới đúng prompt; đang tải trực tiếp toàn bộ ảnh...")
            direct_results = self._download_chat_images(driver, jobs, contexts, images)
            if all(not isinstance(result, BaseException) for result in direct_results.values()):
                primary.log("Đã tải đủ toàn bộ ảnh thành công!")
                return direct_results
            return self.recover_image_batch(driver, jobs, contexts)

        if not videos:
            primary.log(
                "Đã thấy Chat Session mới có tick Complete sau thời điểm Send; "
                "đang mở đúng session đó để tải toàn bộ MP4."
            )
            return self.recover_batch(driver, jobs, contexts)

        primary.log(
            "Video đã xuất hiện dưới đúng prompt; đang tải trực tiếp toàn bộ MP4 trong đoạn chat."
        )
        direct_results = self._download_chat_videos(driver, jobs, contexts, videos)
        if all(not isinstance(result, BaseException) for result in direct_results.values()):
            primary.log("Đã tải đủ toàn bộ video nằm dưới prompt; không cần mở Chat Session.")
            return direct_results
        primary.log(
            "Muse không cho tải trực tiếp ít nhất một video; chỉ lúc này mới dùng Chat Session "
            "Complete có thời gian thuộc phút sau thời điểm Send."
        )
        return self.recover_batch(driver, jobs, contexts)

    def _download_chat_videos(
        self,
        driver: Any,
        jobs: list[MuseVideoJob],
        contexts: list[MuseVideoRunContext],
        videos: list[Any],
    ) -> dict[str, Path | BaseException]:
        """Download only videos found below the exact user message created by Send."""
        results: dict[str, Path | BaseException] = {}
        for index, (job, context, video) in enumerate(zip(jobs, contexts, videos), start=1):
            fingerprint = _video_fingerprint(video)
            context.log(f"Đang tải video {index}/{len(jobs)} nằm dưới đúng prompt vừa gửi…")
            context.transition(
                MuseVideoJobState.DOWNLOADING,
                progress=80 + round(index / len(jobs) * 15),
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
            except (
                MuseVideoStopped,
                MuseVideoLoginRequired,
                MuseVideoQuotaExhausted,
                MuseVideoDriverError,
            ):
                raise
            except BaseException as exc:
                results[job.job_id] = exc
        return results

    def _download_chat_images(
        self,
        driver: Any,
        jobs: list[MuseVideoJob],
        contexts: list[MuseVideoRunContext],
        images: list[Any],
    ) -> dict[str, Path | BaseException]:
        """Download only images found below the exact user message created by Send."""
        results: dict[str, Path | BaseException] = {}
        for index, (job, context, image) in enumerate(zip(jobs, contexts, images), start=1):
            fingerprint = self._image_fingerprint(image)
            context.log(f"Đang tải ảnh {index}/{len(jobs)} nằm dưới đúng prompt vừa gửi…")
            context.transition(
                MuseVideoJobState.DOWNLOADING,
                progress=80 + round(index / len(jobs) * 15),
                result_fingerprint=fingerprint,
                download_attempts=job.download_attempts + 1,
            )
            try:
                results[job.job_id] = self._download_image(
                    driver,
                    image,
                    Path(job.output_path),
                    context,
                )
            except (
                MuseVideoStopped,
                MuseVideoLoginRequired,
                MuseVideoQuotaExhausted,
                MuseVideoDriverError,
            ):
                raise
            except BaseException as exc:
                results[job.job_id] = exc
        return results

    def _download_image(
        self,
        driver: Any,
        image: Any,
        target: Path,
        context: MuseVideoRunContext,
    ) -> Path:
        if target.is_file() and _valid_image(target):
            return target
        context.download_dir.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)

        # 1. Direct canvas/base64 extraction (fastest & reliable)
        try:
            b64_data = driver.execute_script(
                "/* VISUAL_LOOP_EXTRACT_IMAGE_DATA */"
                "const img=arguments[0];"
                "if(!img)return null;"
                "try{"
                "  const canvas=document.createElement('canvas');"
                "  canvas.width=img.naturalWidth||img.width||512;"
                "  canvas.height=img.naturalHeight||img.height||512;"
                "  const ctx=canvas.getContext('2d');"
                "  ctx.drawImage(img,0,0);"
                "  return canvas.toDataURL('image/png');"
                "}catch(e){return null;}"
            , image)
            if b64_data and isinstance(b64_data, str) and "," in b64_data:
                import base64
                _header, encoded = b64_data.split(",", 1)
                data = base64.b64decode(encoded)
                if len(data) > 256:
                    target.write_bytes(data)
                    if _valid_image(target):
                        return target
                    target.unlink(missing_ok=True)
        except Exception:
            pass

        # 2. Browser download anchor or card button
        before = {
            (item.resolve(), item.stat().st_mtime_ns)
            for item in context.download_dir.iterdir()
            if item.is_file()
        }
        clicked = False
        try:
            clicked = bool(
                driver.execute_script(
                    "/* VISUAL_LOOP_CLICK_IMAGE_DOWNLOAD */"
                    "const img=arguments[0];"
                    "if(!img)return false;"
                    "const src=String(img.currentSrc||img.src||'').trim();"
                    "if(src&&/^(blob:|https?:|data:)/i.test(src)){"
                    "  const a=document.createElement('a');"
                    "  a.href=src;"
                    "  a.download='muse-image.png';"
                    "  a.style.display='none';"
                    "  document.body.appendChild(a);"
                    "  a.click();"
                    "  a.remove();"
                    "  return true;"
                    "}"
                    "const card=img.closest('[data-testid*=\"card\" i],div.relative,div');"
                    "if(card){"
                    "  const btn=card.querySelector('button[aria-label*=\"download\" i],button[title*=\"download\" i],[data-testid*=\"download\" i]');"
                    "  if(btn){btn.click();return true;}"
                    "}"
                    "return false;"
                , image)
            )
        except Exception:
            clicked = False

        if not clicked:
            raise MuseVideoDownloadError("Không thể tải ảnh: không trích xuất được URL ảnh từ giao diện Muse.")

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
                if not item.is_file() or item.suffix.casefold() not in SUPPORTED_IMAGE_SUFFIXES:
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
                "Ảnh Muse đã hoàn tất nhưng download chưa xong.",
            )
        )
        if not _valid_image(downloaded):
            raise MuseVideoDownloadError("File tải từ Muse không phải ảnh hợp lệ hoặc rỗng.")
        if target.exists():
            if _valid_image(target):
                downloaded.unlink(missing_ok=True)
                return target
            raise MuseVideoDownloadError("File output đích đã tồn tại nhưng không hợp lệ; tool không ghi đè.")
        shutil.move(str(downloaded), str(target))
        if not _valid_image(target):
            raise MuseVideoDownloadError("File output ảnh không hợp lệ sau khi di chuyển.")
        return target

    def recover_image_batch(
        self,
        driver: Any,
        jobs: list[MuseVideoJob],
        contexts: list[MuseVideoRunContext],
        *,
        claimed_fingerprints: set[str] | None = None,
        claimed_sessions: set[str] | None = None,
    ) -> dict[str, Path | BaseException]:
        """Recover a submitted image group without sending the prompt again."""
        if not jobs or len(jobs) != len(contexts):
            raise MuseVideoDownloadError("Nhóm khôi phục ảnh Muse không hợp lệ.")
        primary = contexts[0]
        self._check(driver, primary)
        expected = max(job.submission_size for job in jobs)
        primary.log(f"Khôi phục lượt ảnh đã gửi gồm {expected} ảnh; không bấm Send lần hai.")
        results: dict[str, Path | BaseException] = {}
        pending: list[tuple[MuseVideoJob, MuseVideoRunContext]] = []
        for job, context in zip(jobs, contexts):
            target = Path(job.output_path)
            if target.is_file() and _valid_image(target):
                results[job.job_id] = target
            else:
                pending.append((job, context))
        if not pending:
            return results

        claimed_fps = set(claimed_fingerprints or set())
        images = []
        try:
            images = self._wait_for_distinct_images(
                driver,
                expected=expected,
                baseline=set(jobs[0].baseline_images),
                anchor=self._anchor_from_job(jobs[0]),
                context=primary,
                claimed_fingerprints=claimed_fps,
            )
        except Exception:
            images = []
        if isinstance(images, list) and images:
            if len(jobs) == 1 and len(images) > 1:
                target_idx = min(max(0, jobs[0].submission_index), len(images) - 1)
                img_by_job_id = {jobs[0].job_id: images[target_idx]}
            else:
                img_by_job_id = {
                    job.job_id: img for job, img in zip(jobs, images)
                }
            direct_pairs = [
                (job, context, img_by_job_id[job.job_id])
                for job, context in pending
                if job.job_id in img_by_job_id
            ]
            if direct_pairs:
                direct_results = self._download_chat_images(
                    driver,
                    [job for job, _context, _img in direct_pairs],
                    [context for _job, context, _img in direct_pairs],
                    [img for _job, _context, img in direct_pairs],
                )
                results.update(direct_results)
                pending = [
                    (job, context)
                    for job, context in pending
                    if not (Path(job.output_path).is_file() and _valid_image(Path(job.output_path)))
                ]
                if not pending:
                    primary.log("Đã tải đủ ảnh trực tiếp từ đoạn chat.")
                    return results

        for job, context in pending:
            results[job.job_id] = MuseVideoDownloadError(
                "Không khôi phục được ảnh Muse; ảnh chưa hoàn tất hoặc giao diện đã thay đổi."
            )
        return results

    def _summary_lock_for(self, driver: Any) -> threading.Lock:
        """Serialize one browser tab only; different Muse tabs remain concurrent."""
        key = id(driver)
        with self._summary_locks_guard:
            return self._summary_locks.setdefault(key, threading.Lock())

    def recover_batch(
        self,
        driver: Any,
        jobs: list[MuseVideoJob],
        contexts: list[MuseVideoRunContext],
        *,
        claimed_fingerprints: set[str] | None = None,
        claimed_sessions: set[str] | None = None,
    ) -> dict[str, Path | BaseException]:
        """Recover a submitted group without sending the prompt again."""
        if not jobs or len(jobs) != len(contexts):
            raise MuseVideoDownloadError("Nhóm khôi phục Muse không hợp lệ.")
        primary = contexts[0]
        self._check(driver, primary)
        expected = max(job.submission_size for job in jobs)
        primary.log(f"Khôi phục lượt đã gửi gồm {expected} MP4; không bấm Send lần hai.")
        results: dict[str, Path | BaseException] = {}
        pending: list[tuple[MuseVideoJob, MuseVideoRunContext]] = []
        for job, context in zip(jobs, contexts):
            target = Path(job.output_path)
            if target.is_file() and _valid_mp4(target):
                results[job.job_id] = target
            else:
                pending.append((job, context))
        if not pending:
            return results

        claimed_fps = set(claimed_fingerprints or set())
        claimed_sess = set(claimed_sessions or set())

        primary.log(
            f"Đang tìm đủ {expected} video nằm dưới đúng prompt để tải trực tiếp trước."
        )
        videos = []
        try:
            videos = self._wait_for_distinct_videos(
                driver,
                expected=expected,
                baseline=set(jobs[0].baseline_videos),
                anchor=self._anchor_from_job(jobs[0]),
                context=primary,
                baseline_sessions=set(jobs[0].baseline_session_fingerprints),
                claimed_fingerprints=claimed_fps,
            )
        except Exception:
            videos = []
        if isinstance(videos, list) and videos:
            if len(jobs) == 1 and len(videos) > 1:
                target_idx = min(max(0, jobs[0].submission_index), len(videos) - 1)
                video_by_job_id = {jobs[0].job_id: videos[target_idx]}
            else:
                video_by_job_id = {
                    job.job_id: video for job, video in zip(jobs, videos)
                }
            direct_pairs = [
                (job, context, video_by_job_id[job.job_id])
                for job, context in pending
                if job.job_id in video_by_job_id
            ]
            if direct_pairs:
                direct_results = self._download_chat_videos(
                    driver,
                    [job for job, _context, _video in direct_pairs],
                    [context for _job, context, _video in direct_pairs],
                    [video for _job, _context, video in direct_pairs],
                )
                results.update(direct_results)
                pending = [
                    (job, context)
                    for job, context in pending
                    if not (Path(job.output_path).is_file() and _valid_mp4(Path(job.output_path)))
                ]
                if not pending:
                    primary.log("Đã tải đủ MP4 trực tiếp từ các video dưới prompt.")
                    return results

        primary.log(
            "Tải trực tiếp chưa thành công; đang chờ Chat Session tick Complete ở phút sau thời điểm Send."
        )

        baseline = set(jobs[0].baseline_session_fingerprints)
        summary_lock = self._summary_lock_for(driver)
        summary_lock.acquire()
        try:
            while pending:
                first_job, _first_context = pending[0]
                wanted_fingerprint = first_job.result_session_fingerprint
                wanted_text = first_job.result_session_text
                session = self._wait_for_completed_session(
                    driver,
                    baseline=baseline,
                    wanted_fingerprint=wanted_fingerprint,
                    wanted_text=wanted_text,
                    started_epoch_ms=first_job.submission_started_epoch_ms,
                    context=primary,
                    claimed=claimed_sess,
                )
                artifacts = self._open_summary_and_wait_for_all_mp4(
                    driver,
                    session=session,
                    context=primary,
                )
                claimed_artifacts: set[str] = set()
                processed: list[tuple[MuseVideoJob, MuseVideoRunContext]] = []
                if wanted_fingerprint:
                    candidates = [
                        pair for pair in pending if pair[0].result_session_fingerprint == wanted_fingerprint
                    ]
                elif wanted_text:
                    candidates = [pair for pair in pending if pair[0].result_session_text == wanted_text]
                else:
                    candidates = [pair for pair in pending if not pair[0].result_session_fingerprint]
                try:
                    for job, context in candidates:
                        artifact = next(
                            (
                                item
                                for item in artifacts
                                if item.fingerprint == job.result_fingerprint
                                and item.fingerprint not in claimed_artifacts
                                and item.fingerprint not in claimed_fps
                            ),
                            None,
                        )
                        if artifact is None:
                            unclaimed = [
                                item for item in artifacts
                                if item.fingerprint not in claimed_artifacts
                                and item.fingerprint not in claimed_fps
                            ]
                            if unclaimed:
                                if 0 <= job.submission_index < len(unclaimed):
                                    artifact = unclaimed[job.submission_index]
                                else:
                                    artifact = unclaimed[0]
                        if artifact is None:
                            if wanted_fingerprint or wanted_text:
                                results[job.job_id] = MuseVideoDownloadError(
                                    "Không ghép được MP4 trong đúng Chat Session Complete của lượt cũ."
                                )
                                processed.append((job, context))
                            break
                        claimed_artifacts.add(artifact.fingerprint)
                        claimed_fps.add(artifact.fingerprint)
                        context.transition(
                            MuseVideoJobState.DOWNLOADING,
                            progress=90,
                            result_session_fingerprint=session.fingerprint,
                            result_session_text=session.text,
                            result_fingerprint=artifact.fingerprint,
                            download_attempts=job.download_attempts + 1,
                        )
                        try:
                            results[job.job_id] = self._download_summary_artifact(
                                driver,
                                artifact,
                                Path(job.output_path),
                                context,
                            )
                        except (
                            MuseVideoStopped,
                            MuseVideoLoginRequired,
                            MuseVideoQuotaExhausted,
                            MuseVideoDriverError,
                        ):
                            raise
                        except BaseException as exc:
                            results[job.job_id] = exc
                        processed.append((job, context))
                finally:
                    self._close_summary(driver)
                if not processed:
                    primary.log(
                        f"Session {session.text[:40]} không có MP4 mới chưa tải; bỏ qua để kiểm tra session khác."
                    )
                    claimed_sess.add(session.fingerprint)
                    continue
                if all(
                    item.fingerprint in claimed_artifacts or item.fingerprint in claimed_fps
                    for item in artifacts
                ):
                    claimed_sess.add(session.fingerprint)
                processed_ids = {job.job_id for job, _context in processed}
                pending = [pair for pair in pending if pair[0].job_id not in processed_ids]
            primary.log("Đã khôi phục và tải đủ MP4 theo từng Chat Session Complete.")
            return results
        finally:
            summary_lock.release()

    def _wait_for_distinct_videos(
        self,
        driver: Any,
        *,
        expected: int,
        baseline: set[str],
        anchor: MuseSubmissionAnchor,
        context: MuseVideoRunContext,
        baseline_sessions: set[str] | None = None,
        claimed_fingerprints: set[str] | None = None,
    ) -> list[Any]:
        transient_retries = 0
        session_ready = object()
        session_seen_at: float | None = None
        claimed = set(claimed_fingerprints or set())

        def find_results():
            nonlocal transient_retries, session_seen_at
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
            # Bind results to the exact message created after this Send click,
            # not merely to matching prompt text (which is often repeated).
            for video in self._videos_after_submission(driver, anchor):
                fingerprint = _video_fingerprint(video)
                if (
                    fingerprint in baseline
                    or fingerprint in seen
                    or fingerprint in claimed
                    or not self._video_ready(driver, video)
                ):
                    continue
                seen.add(fingerprint)
                values.append(video)
            if len(values) >= expected:
                return values
            if baseline_sessions is not None:
                sessions = self._completed_sessions(driver)
                if anchor.started_epoch_ms > 0:
                    cutoff = (
                        anchor.started_epoch_ms
                        - (anchor.started_epoch_ms % 60_000)
                        + 60_000
                    )
                    sessions = [
                        session
                        for session in sessions
                        if session.completed_epoch_ms >= cutoff
                    ]
                if any(
                    session.fingerprint not in baseline_sessions
                    for session in sessions
                ):
                    if session_seen_at is None:
                        session_seen_at = time.monotonic()
                    grace_time = min(2.0, self.timeout * 0.2)
                    if time.monotonic() - session_seen_at >= grace_time:
                        return session_ready
            return False

        result = self._wait(
            driver,
            find_results,
            self.timeout,
            context,
            f"Muse chưa trả về đủ {expected} video mới; lượt đã gửi sẽ không tự gửi lại.",
        )
        return [] if result is session_ready else list(result)

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
        if target.is_file() and _valid_artifact(target, job.settings.task_mode):
            return target
        if job.settings.task_mode == "image":
            result = self.recover_image_batch(driver, [job], [context])[job.job_id]
        else:
            result = self.recover_batch(driver, [job], [context])[job.job_id]
        if isinstance(result, BaseException):
            raise result
        return result

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

    def _fill_prompt(
        self,
        driver: Any,
        prompt: str,
        context: MuseVideoRunContext,
    ) -> None:
        def fill_when_ready():
            self._check(driver, context)
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
                    return True
                except Exception:
                    continue
            return False

        self._wait(
            driver,
            fill_when_ready,
            self.upload_timeout,
            context,
            "Muse chưa hiển thị lại ô prompt ổn định sau khi upload ảnh.",
        )

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

    def _prompt_still_in_composer(self, driver: Any, prompt: str) -> bool:
        """Kiểm tra xem prompt có còn nằm trong ô nhập của composer hay không."""
        try:
            return bool(
                driver.execute_script(
                    "/* VISUAL_LOOP_CHECK_COMPOSER_PROMPT */"
                    "const prompt=(arguments[0]||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                    "if(!prompt)return false;"
                    "const isInsideSidebar=el=>!!el.closest('aside,nav,[data-testid*=\"sidebar\" i],[data-testid*=\"timeline\" i],[data-testid*=\"history\" i]');"
                    "const isInsideChat=el=>!!el.closest('[data-message-item=\"true\"],[data-message-role],[data-message-group-id]');"
                    "const textareas=[...document.querySelectorAll('textarea,input[type=\"text\"],[contenteditable=\"true\"]')].filter(t=>!isInsideSidebar(t)&&!isInsideChat(t));"
                    "for(const ta of textareas){"
                    "const val=((ta.value||ta.innerText||ta.textContent||'')).replace(/\\s+/g,' ').trim().toLowerCase();"
                    "if(val&&(val.includes(prompt.slice(0,15))||prompt.includes(val.slice(0,15))))return true;"
                    "}"
                    "return false;",
                    prompt,
                )
            )
        except Exception:
            return False

    def _click_generate_once(self, driver: Any, context: MuseVideoRunContext | None = None) -> None:
        """Click the Send / Generate button in the composer, waiting briefly if initially disabled."""
        def try_click():
            if context and context.stopped():
                raise MuseVideoStopped("Đã dừng worker Muse.")
            # 1. Try standard generate buttons (first check clickable items)
            for selector in self.selectors.generate_buttons:
                for item in _find(driver, "css selector", selector):
                    if _clickable(item):
                        try:
                            item.click()
                            return True
                        except Exception:
                            continue
            # 2. Try JavaScript composer send button (strictly excluding sidebar and history)
            try:
                clicked = bool(
                    driver.execute_script(
                        "/* VISUAL_LOOP_CLICK_COMPOSER_SEND */"
                        "const isInsideSidebar=el=>!!el.closest('aside,nav,[data-testid*=\"sidebar\" i],[data-testid*=\"timeline\" i],[data-testid*=\"history\" i]');"
                        "const isInsideChat=el=>!!el.closest('[data-message-item=\"true\"],[data-message-role],[data-message-group-id]');"
                        "const triggerClick=el=>{"
                        "if(!el)return false;"
                        "try{el.focus();}catch(e){}"
                        "const opts={bubbles:true,cancelable:true,view:window};"
                        "try{el.dispatchEvent(new PointerEvent('pointerdown',opts));}catch(e){}"
                        "try{el.dispatchEvent(new MouseEvent('mousedown',opts));}catch(e){}"
                        "try{el.dispatchEvent(new PointerEvent('pointerup',opts));}catch(e){}"
                        "try{el.dispatchEvent(new MouseEvent('mouseup',opts));}catch(e){}"
                        "el.click();"
                        "return true;"
                        "};"
                        "const selectors=["
                        "'button[aria-label=\"Send\" i]',"
                        "'button[aria-label*=\"send message\" i]',"
                        "'button[aria-label*=\"send\" i]',"
                        "'button[type=\"submit\"][aria-label*=\"send\" i]',"
                        "'button[type=\"submit\"]',"
                        "'button[data-testid*=\"send\" i]',"
                        "'button[data-testid*=\"generate\" i]'"
                        "];"
                        "for(const sel of selectors){"
                        "const btns=[...document.querySelectorAll(sel)].filter(b=>{"
                        "if(isInsideSidebar(b)||isInsideChat(b))return false;"
                        "const r=b.getBoundingClientRect();return r.width>0&&r.height>0&&!b.disabled&&b.getAttribute('aria-disabled')!=='true';"
                        "});"
                        "if(btns.length){return triggerClick(btns[btns.length-1]);}"
                        "}"
                        "const textareas=[...document.querySelectorAll('textarea,[contenteditable=\"true\"]')].filter(t=>{"
                        "if(isInsideSidebar(t)||isInsideChat(t))return false;"
                        "const r=t.getBoundingClientRect();return r.width>0&&r.height>0;"
                        "});"
                        "if(textareas.length){"
                        "const ta=textareas[textareas.length-1];"
                        "const container=ta.closest('form,[data-testid*=\"composer\" i],div.relative,div')||ta.parentElement;"
                        "if(container){"
                        "const btns=[...container.querySelectorAll('button,[role=\"button\"]')].filter(b=>{"
                        "if(b===ta||isInsideSidebar(b)||isInsideChat(b))return false;"
                        "const aria=(b.getAttribute('aria-label')||'').toLowerCase();"
                        "if(/attach|upload|file|image|add|close|dismiss|preview/i.test(aria))return false;"
                        "const r=b.getBoundingClientRect();return r.width>0&&r.height>0&&!b.disabled&&b.getAttribute('aria-disabled')!=='true';"
                        "});"
                        "const sendBtn=btns.find(b=>{"
                        "const aria=(b.getAttribute('aria-label')||'').toLowerCase();"
                        "const type=(b.getAttribute('type')||'').toLowerCase();"
                        "return /send|submit/i.test(aria)||type==='submit'||Boolean(b.querySelector('svg path,svg'));"
                        "})||btns.sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right)[0];"
                        "if(sendBtn){return triggerClick(sendBtn);}"
                        "}"
                        "try{"
                        "ta.focus();"
                        "const evt=new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true});"
                        "ta.dispatchEvent(evt);"
                        "return true;"
                        "}catch(e){}"
                        "}"
                        "return false;"
                    )
                )
                if clicked:
                    return True
            except Exception:
                pass
            return False

        if try_click():
            return

        # If not clickable immediately (e.g. button was briefly disabled while input processed),
        # wait up to 3 seconds with polling instead of clicking random sidebar items!
        deadline = time.monotonic() + min(3.0, max(0.2, self.upload_timeout * 0.1))
        while time.monotonic() < deadline:
            time.sleep(min(0.05, self.poll_interval))
            if try_click():
                return

        # 3. Fallback: Try pressing Enter in the prompt field directly
        try:
            from selenium.webdriver.common.keys import Keys

            for field in reversed(self._elements(driver, self.selectors.prompt_inputs)):
                if _clickable(field):
                    field.send_keys(Keys.RETURN)
                    return
        except Exception:
            pass

        raise MuseVideoBatchError("Không tìm thấy nút Generate/Send ổn định trên Muse.")

    def _download(self, driver: Any, video: Any, target: Path, context: MuseVideoRunContext) -> Path:
        context.download_dir.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = {
            (item.resolve(), item.stat().st_mtime_ns)
            for item in context.download_dir.iterdir()
            if item.is_file()
        }
        # Download only the media URL owned by the exact <video> returned for
        # this prompt.  A Muse result card can also contain the uploaded image;
        # clicking the card's generic Download button can therefore save the
        # image instead of the generated video.
        clicked = False
        try:
            clicked = bool(
                driver.execute_script(
                    "const v=arguments[0];"
                    "if(!v || String(v.tagName||'').toLowerCase()!=='video')return false;"
                    "const mediaSrc=v=>String(v.currentSrc||v.src||(v.querySelector&&v.querySelector('source')&&v.querySelector('source').src)||'').trim();"
                    "const src=mediaSrc(v);"
                    "if(!src || /^data:image[/]/i.test(src) || /^https?:.*\\.(png|jpe?g|webp|gif)(?:[?#]|$)/i.test(src))return false;"
                    "if(!/^(blob:|https?:)/i.test(src))return false;"
                    "const s=src.toLowerCase();"
                    "if(/(avatar|mascot|muse[-_]?bot|icon|reaction|subagent|status[-_]?anim|thinking|loading[-_]anim|placeholders?|animations?|assets?\\/static)/i.test(s))return false;"
                    "const bad='header,nav,aside,[data-testid*=\"avatar\" i],[class*=\"avatar\" i],[data-testid*=\"mascot\" i],[class*=\"mascot\" i],[data-testid*=\"loading\" i],[class*=\"loading\" i],[class*=\"thinking\" i],[class*=\"reaction\" i],[class*=\"badge\" i],[class*=\"status\" i],[class*=\"indicator\" i],[data-testid*=\"subagent\" i],[class*=\"subagent\" i],[data-testid*=\"placeholder\" i],[class*=\"placeholder\" i]';"
                    "if(v.matches&&v.matches(bad))return false;"
                    "if(v.closest&&v.closest(bad))return false;"
                    "const r=v.getBoundingClientRect?v.getBoundingClientRect():null;"
                    "if(r&&r.width>0&&r.height>0&&(r.width<160||r.height<160))return false;"
                    "const a=document.createElement('a');a.href=src;a.download='muse-video.mp4';"
                    "a.style.display='none';document.body.appendChild(a);a.click();a.remove();return true;",
                    video,
                )
            )
        except Exception:
            clicked = False
        if not clicked:
            raise MuseVideoDownloadError(
                "Không lấy được URL video MP4/blob của đúng kết quả mới; tool đã từ chối bấm nút tải ảnh/card."
            )

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

    @staticmethod
    def _browser_epoch_ms(driver: Any) -> int:
        try:
            return max(0, int(driver.execute_script("/* VISUAL_LOOP_BROWSER_EPOCH */ return Date.now();") or 0))
        except Exception:
            return 0

    def _completed_sessions(self, driver: Any) -> list[MuseCompletedSession]:
        """Return visible completed timeline sessions, newest first."""
        try:
            values = driver.execute_script(
                "/* VISUAL_LOOP_COMPLETED_SESSION_SNAPSHOT */"
                "const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);"
                "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
                "const norm=s=>(s||'').replace(/\\s+/g,' ').trim();"
                "const media=/(video|videos|clip|clips|mp4)/i;"
                "const clock=/\\b\\d{1,2}:\\d{2}(?:\\s*[ap]m)?\\b/i;"
                "const completedTick='M15.79 7.66C16.08 7.27 16.63 7.18 17.02 7.47C17.41 7.75 17.49 8.3 17.21 8.69L11.13 17.02C10.97 17.23 10.72 17.36 10.45 17.37C10.18 17.38 9.93 17.27 9.75 17.06L6.58 13.3C6.27 12.94 6.32 12.38 6.69 12.07C7.05 11.76 7.61 11.81 7.92 12.18L10.37 15.08L15.79 7.66Z';"
                "return [...document.querySelectorAll('button')].filter(button=>{"
                "if(!visible(button)||button.closest('[role=\\\"dialog\\\"]'))return false;"
                "const text=norm(button.innerText||button.textContent);"
                "const hasCompletedTick=[...button.querySelectorAll('svg path')].some(path=>"
                "norm(path.getAttribute('d'))===completedTick);"
                "return hasCompletedTick&&media.test(text)&&clock.test(text);"
                "}).map((element,index)=>{"
                "const text=norm(element.innerText||element.textContent),match=text.match(clock);let epochMs=0;"
                "if(match){let hour=Number(match[0].match(/^\\d{1,2}/)[0]),minute=Number(match[0].match(/:(\\d{2})/)[1]);"
                "const suffix=(match[0].match(/[ap]m/i)||[''])[0].toLowerCase();"
                "if(suffix==='pm'&&hour<12)hour+=12;if(suffix==='am'&&hour===12)hour=0;"
                "const date=new Date();date.setHours(hour,minute,0,0);if(date.getTime()>Date.now()+21600000)date.setDate(date.getDate()-1);"
                "epochMs=date.getTime();}return {element,index,epoch_ms:epochMs,"
                "top:element.getBoundingClientRect().top,text,"
                "key:[element.getAttribute('data-testid')||'',"
                "element.getAttribute('data-pel-impression')||'',element.getAttribute('aria-label')||'',"
                "element.getAttribute('title')||'',element.getAttribute('href')||''].join('|')};})"
                ".sort((a,b)=>a.top-b.top||a.index-b.index);"
            )
        except Exception:
            return []
        if not isinstance(values, (list, tuple)):
            return []
        sessions: list[MuseCompletedSession] = []
        for index, value in enumerate(values):
            if not isinstance(value, dict) or value.get("element") is None:
                continue
            text = " ".join(str(value.get("text") or "").split())
            key = str(value.get("key") or "")
            fingerprint = hashlib.sha256(
                f"{key}|{text}".encode("utf-8", errors="ignore")
            ).hexdigest()
            sessions.append(
                MuseCompletedSession(
                    element=value.get("element"),
                    fingerprint=fingerprint,
                    text=text,
                    completed_epoch_ms=max(0, int(value.get("epoch_ms") or 0)),
                    visual_index=index,
                )
            )
        return sessions

    def _wait_for_completed_session(
        self,
        driver: Any,
        *,
        baseline: set[str],
        wanted_fingerprint: str,
        wanted_text: str,
        started_epoch_ms: int,
        context: MuseVideoRunContext,
        claimed: set[str] | None = None,
    ) -> MuseCompletedSession:
        """Wait for the earliest unclaimed tick-complete session after Send."""

        claimed_fingerprints = set(claimed) if claimed is not None else set()

        def find_session():
            self._check(driver, context)
            sessions = self._completed_sessions(driver)
            if started_epoch_ms > 0:
                # Sidebar timestamps have minute precision. Requiring the next
                # minute is the only safe way to reject an older Complete card
                # from the same displayed minute as the Send click.
                cutoff = started_epoch_ms - (started_epoch_ms % 60_000) + 60_000
                sessions = [
                    session
                    for session in sessions
                    if session.completed_epoch_ms >= cutoff
                ]
            if wanted_fingerprint:
                matched = next(
                    (session for session in sessions if session.fingerprint == wanted_fingerprint),
                    None,
                )
                if matched is not None:
                    return matched
                if wanted_text:
                    matched = next((session for session in sessions if session.text == wanted_text), None)
                    if matched is not None:
                        return matched
                    clock_match = re.search(r"\b\d{1,2}:\d{2}(?:\s*[ap]m)?\b", wanted_text, re.IGNORECASE)
                    if clock_match:
                        time_str = clock_match.group(0).lower()
                        matched = next(
                            (
                                s for s in sessions
                                if time_str in s.text.lower()
                                and s.fingerprint not in claimed_fingerprints
                            ),
                            None,
                        )
                        if matched is not None:
                            return matched
                unclaimed = [
                    s for s in sessions
                    if s.fingerprint not in baseline
                    and s.fingerprint not in claimed_fingerprints
                ]
                if unclaimed:
                    unclaimed.sort(
                        key=lambda s: (
                            s.completed_epoch_ms or (2 ** 63 - 1),
                            -s.visual_index,
                        )
                    )
                    return unclaimed[0]
                return False
            sessions = [
                session
                for session in sessions
                if session.fingerprint not in baseline
                and session.fingerprint not in claimed_fingerprints
            ]
            sessions.sort(
                key=lambda session: (
                    session.completed_epoch_ms or (2 ** 63 - 1),
                    -session.visual_index,
                )
            )
            return sessions[0] if sessions else False

        return self._wait(
            driver,
            find_session,
            self.timeout,
            context,
            "Muse chưa xuất hiện Chat Session mới có đúng dấu tick Complete sau lúc gửi; tool không tải video cũ.",
        )

    @staticmethod
    def _generation_in_progress(driver: Any) -> bool:
        try:
            return bool(
                driver.execute_script(
                    "/* VISUAL_LOOP_GENERATION_IN_PROGRESS */"
                    "const visible=e=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);"
                    "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
                    "const norm=s=>(s||'').replace(/\\s+/g,' ').trim();"
                    "const active=/(generating|creating|rendering|processing|đang tạo|đang xử lý)/i;"
                    "const complete=/(generated|rendered|delivered|finished|ready|created|đã tạo|hoàn tất)/i;"
                    "const media=/(video|videos|clip|clips|mp4)/i;"
                    "const clock=/\\b\\d{1,2}:\\d{2}(?:\\s*[ap]m)?\\b/i;"
                    "const sessions=[...document.querySelectorAll('button')].filter(button=>{"
                    "if(!visible(button)||button.closest('[role=\\\"dialog\\\"]'))return false;"
                    "const text=norm(button.innerText||button.textContent);"
                    "return media.test(text)&&clock.test(text)&&(active.test(text)||complete.test(text));"
                    "}).sort((a,b)=>a.getBoundingClientRect().top-b.getBoundingClientRect().top);"
                    "return sessions.length?active.test(norm(sessions[0].innerText||sessions[0].textContent)):false;"
                )
            )
        except Exception:
            return False

    def _summary_mp4_artifacts(self, driver: Any) -> list[MuseSummaryArtifact]:
        """Read only MP4 artifact cards from the currently open Summary dialog."""
        try:
            values = driver.execute_script(
                "/* VISUAL_LOOP_SUMMARY_MP4_ARTIFACTS */"
                "const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);"
                "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
                "const norm=s=>(s||'').replace(/\\s+/g,' ').trim();"
                "const headings=[...document.querySelectorAll('h1,h2,h3,h4,[role=\"heading\"],div,span')]"
                ".filter(e=>visible(e)&&norm(e.innerText||e.textContent).toLowerCase()==='summary');"
                "let root=null;"
                "if(headings.length){"
                "const heading=headings[headings.length-1];"
                "root=heading.closest('[role=\"dialog\"],[aria-modal=\"true\"],dialog,div[class*=\"modal\" i],div[class*=\"dialog\" i]');"
                "if(!root){let cur=heading.parentElement;while(cur&&cur!==document.body){"
                "const r=cur.getBoundingClientRect();if(r.width>=320&&r.height>=240){root=cur;break;}cur=cur.parentElement;}}"
                "}"
                "if(!root){root=document.querySelector('[role=\"dialog\"],[aria-modal=\"true\"],dialog')||document.body;}"
                "const candidates=[];"
                "const seenButtons=new Set();"
                "const addCandidate=(btn,text)=>{"
                "if(!btn||seenButtons.has(btn))return;seenButtons.add(btn);"
                "candidates.push({element:btn,text:text||'media-generation MP4'});"
                "};"
                "const knownButtons=[...root.querySelectorAll("
                "'button[data-testid=\\\"hatch-sandbox-file-card-options\\\"],button[data-testid=\\\"sandbox-file-card-options\\\"],'+"
                "'button[data-testid*=\\\"sandbox-file-card-options\\\" i],button[data-testid*=\\\"file-card-options\\\" i],button[data-testid*=\\\"options\\\" i],'+"
                "'button[data-pel-click*=\\\"artifact_options\\\" i],button[data-slot=\\\"dropdown-menu-trigger\\\"],'+"
                "'button[aria-label=\\\"More options\\\" i],button[aria-label*=\\\"options\\\" i],button[aria-label*=\\\"action\\\" i]'"
                ")].filter(visible);"
                "for(const btn of knownButtons){"
                "let card=btn.closest('[data-pel-impression=\"sandbox_file_card_impression\"]');"
                "if(!card){card=btn.parentElement;while(card&&card!==root&&!/\\b(MP4|JSON)\\b/i.test(norm(card.innerText||card.textContent)))card=card.parentElement;}"
                "const text=norm(card&&(card.innerText||card.textContent)||'');"
                "if(/\\bMP4\\b/i.test(text)&&!/\\bJSON\\b/i.test(text)){addCandidate(btn,text);}"
                "}"
                "if(!candidates.length){"
                "const mp4Cards=[...root.querySelectorAll('div,li,[data-pel-impression]')].filter(el=>{"
                "if(!visible(el))return false;const t=norm(el.innerText||el.textContent);"
                "if(!/\\bMP4\\b/i.test(t)||/\\bJSON\\b/i.test(t))return false;"
                "const r=el.getBoundingClientRect();return r.height>=20&&r.height<=250&&r.width>=100;"
                "});"
                "mp4Cards.sort((a,b)=>(a.getBoundingClientRect().width*a.getBoundingClientRect().height)-(b.getBoundingClientRect().width*b.getBoundingClientRect().height));"
                "const claimedCards=new Set();"
                "for(const card of mp4Cards){"
                "if([...claimedCards].some(c=>c.contains(card)))continue;"
                "const btns=[...card.querySelectorAll('button,[role=\"button\"]')].filter(visible);"
                "if(btns.length){"
                "const moreBtn=btns.find(b=>{"
                "const aria=(b.getAttribute('aria-label')||'').toLowerCase();"
                "const title=(b.getAttribute('title')||'').toLowerCase();"
                "const t=norm(b.innerText||b.textContent);"
                "return /option|more|menu|action/i.test(aria+' '+title)||/\\.{2,}|…|···/.test(t)||"
                "b.getAttribute('aria-haspopup')==='menu'||Boolean(b.querySelector('svg circle,svg path,[data-testid*=\"more\" i]'));"
                "})||btns.sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right)[0];"
                "if(moreBtn){claimedCards.add(card);addCandidate(moreBtn,norm(card.innerText||card.textContent)||'media-generation MP4');}"
                "}"
                "}"
                "}"
                "return candidates.map((item,index)=>({element:item.element,index:index,text:item.text}));"
            )
        except Exception:
            return []
        if not isinstance(values, (list, tuple)):
            return []
        artifacts: list[MuseSummaryArtifact] = []
        for index, value in enumerate(values):
            if not isinstance(value, dict) or value.get("element") is None:
                continue
            text = " ".join(str(value.get("text") or "").split())
            if not re.search(r"\bMP4\b", text, flags=re.IGNORECASE) or re.search(
                r"\bJSON\b", text, flags=re.IGNORECASE
            ):
                continue
            fingerprint = hashlib.sha256(
                f"{text}|{int(value.get('index', index) or 0)}".encode("utf-8", errors="ignore")
            ).hexdigest()
            artifacts.append(
                MuseSummaryArtifact(
                    options_button=value.get("element"),
                    fingerprint=fingerprint,
                    text=text,
                    index=int(value.get("index", index) or 0),
                )
            )
        return artifacts

    def _open_summary_and_wait_for_mp4(
        self,
        driver: Any,
        *,
        session: MuseCompletedSession,
        expected: int,
        context: MuseVideoRunContext,
    ) -> list[MuseSummaryArtifact]:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});arguments[0].click();",
                session.element,
            )
        except Exception:
            try:
                session.element.click()
            except Exception as exc:
                raise MuseVideoDownloadError("Không mở được session Muse vừa hoàn tất để đọc Summary.") from exc

        def find_artifacts():
            self._check(driver, context)
            artifacts = self._summary_mp4_artifacts(driver)
            return artifacts[:expected] if len(artifacts) >= expected else False

        return list(
            self._wait(
                driver,
                find_artifacts,
                min(self.timeout, 180.0),
                context,
                f"Summary của session mới chưa có đủ {expected} card MP4; JSON và session cũ đều bị bỏ qua.",
            )
        )

    def _open_summary_and_wait_for_all_mp4(
        self,
        driver: Any,
        *,
        session: MuseCompletedSession,
        context: MuseVideoRunContext,
    ) -> list[MuseSummaryArtifact]:
        """Open one tick-complete session and wait until its MP4 card list is stable."""
        try:
            self._close_summary(driver)
        except Exception:
            pass
        try:
            driver.execute_script(
                "const btn=arguments[0];"
                "btn.scrollIntoView({block:'center',inline:'nearest'});"
                "try{btn.focus();}catch(e){}"
                "const opts={bubbles:true,cancelable:true,view:window};"
                "try{btn.dispatchEvent(new PointerEvent('pointerdown',opts));}catch(e){}"
                "try{btn.dispatchEvent(new MouseEvent('mousedown',opts));}catch(e){}"
                "try{btn.dispatchEvent(new PointerEvent('pointerup',opts));}catch(e){}"
                "try{btn.dispatchEvent(new MouseEvent('mouseup',opts));}catch(e){}"
                "btn.click();",
                session.element,
            )
        except Exception:
            try:
                session.element.click()
            except Exception as exc:
                raise MuseVideoDownloadError(
                    "Không mở được Chat Session có dấu tick Complete để đọc Summary."
                ) from exc

        previous: tuple[str, ...] = ()
        stable_reads = 0

        def find_all_artifacts():
            nonlocal previous, stable_reads
            self._check(driver, context)
            artifacts = self._summary_mp4_artifacts(driver)
            if not artifacts:
                previous = ()
                stable_reads = 0
                return False
            current = tuple(item.fingerprint for item in artifacts)
            stable_reads = stable_reads + 1 if current == previous else 0
            previous = current
            return artifacts if stable_reads >= 2 else False

        return list(
            self._wait(
                driver,
                find_all_artifacts,
                min(self.timeout, 180.0),
                context,
                "Chat Session đã Complete nhưng Summary chưa hiển thị ổn định danh sách MP4.",
            )
        )

    @staticmethod
    def _summary_download_action(driver: Any) -> Any:
        try:
            return driver.execute_script(
                "/* VISUAL_LOOP_SUMMARY_DOWNLOAD_ACTION */"
                "const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);"
                "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
                "const norm=s=>(s||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                "const reg=/download|tải\\s*xuống|save/i;"
                "const isDownload=e=>{"
                "if(!visible(e))return false;"
                "const t=norm(e.innerText||e.textContent);"
                "const aria=norm(e.getAttribute('aria-label')||'');"
                "const title=norm(e.getAttribute('title')||'');"
                "return reg.test(t)||reg.test(aria)||reg.test(title)||t==='download'||aria==='download';"
                "};"
                "const items=[...document.querySelectorAll('[role=\"menuitem\"],[data-slot=\"dropdown-menu-item\"],[role=\"option\"]')].filter(isDownload);"
                "if(items.length)return items[items.length-1];"
                "const openRoots=[...document.querySelectorAll('[role=\"menu\"],[data-state=\"open\"],[data-radix-popper-content-wrapper],[data-radix-menu-content],[data-testid*=\"menu\" i],div[class*=\"menu\" i],div[class*=\"dropdown\" i],div[class*=\"popover\" i]')].filter(visible);"
                "for(let i=openRoots.length-1;i>=0;i--){"
                "const found=[...openRoots[i].querySelectorAll('button,[role=\"button\"],a,div[role=\"menuitem\"],[data-slot=\"dropdown-menu-item\"]')].find(isDownload);"
                "if(found)return found;"
                "}"
                "const fallback=[...document.querySelectorAll('button,[role=\"button\"],a,[role=\"menuitem\"]')].filter(isDownload);"
                "return fallback.length?fallback[fallback.length-1]:null;"
            )
        except Exception:
            return None

    def _fresh_summary_artifact(
        self,
        driver: Any,
        artifact: MuseSummaryArtifact,
        context: MuseVideoRunContext,
    ) -> MuseSummaryArtifact:
        """Resolve the card again because every completed download can rerender Summary."""

        def find_artifact():
            self._check(driver, context)
            artifacts = self._summary_mp4_artifacts(driver)
            exact = next(
                (item for item in artifacts if item.fingerprint == artifact.fingerprint),
                None,
            )
            if exact is not None:
                return exact
            return next(
                (
                    item
                    for item in artifacts
                    if item.index == artifact.index and item.text == artifact.text
                ),
                False,
            )

        return self._wait(
            driver,
            find_artifact,
            min(30.0, self.download_timeout),
            context,
            "Summary đã render lại nhưng không tìm thấy đúng card MP4 cần tải.",
        )

    def _download_summary_artifact(
        self,
        driver: Any,
        artifact: MuseSummaryArtifact,
        target: Path,
        context: MuseVideoRunContext,
    ) -> Path:
        if target.is_file() and _valid_mp4(target):
            return target
        context.download_dir.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        before = {
            (item.resolve(), item.stat().st_mtime_ns)
            for item in context.download_dir.iterdir()
            if item.is_file()
        }

        action = None
        for attempt in range(3):
            self._check(driver, context)
            current_art = self._fresh_summary_artifact(driver, artifact, context)
            btn = current_art.options_button
            try:
                driver.execute_script(
                    "const btn=arguments[0];"
                    "btn.scrollIntoView({block:'center',inline:'nearest'});"
                    "try{btn.focus();}catch(e){}"
                    "const opts={bubbles:true,cancelable:true,view:window};"
                    "try{btn.dispatchEvent(new PointerEvent('pointerdown',opts));}catch(e){}"
                    "try{btn.dispatchEvent(new MouseEvent('mousedown',opts));}catch(e){}"
                    "try{btn.dispatchEvent(new PointerEvent('pointerup',opts));}catch(e){}"
                    "try{btn.dispatchEvent(new MouseEvent('mouseup',opts));}catch(e){}"
                    "btn.click();",
                    btn,
                )
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", btn)
                except Exception:
                    try:
                        btn.click()
                    except Exception:
                        pass

            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                self._check(driver, context)
                act = self._summary_download_action(driver)
                if act is not None:
                    action = act
                    break
                time.sleep(0.3)
            if action is not None:
                break

        if action is None:
            raise MuseVideoDownloadError(
                f"Đã mở dấu ba chấm của card '{artifact.text[:40]}' nhưng không tìm thấy nút Download trong Summary popup."
            )

        clicked = False
        try:
            action.click()
            clicked = True
        except Exception:
            pass
        try:
            driver.execute_script(
                "const el=arguments[0];"
                "try{el.focus();}catch(e){}"
                "const opts={bubbles:true,cancelable:true,view:window};"
                "try{el.dispatchEvent(new PointerEvent('pointerdown',opts));}catch(e){}"
                "try{el.dispatchEvent(new MouseEvent('mousedown',opts));}catch(e){}"
                "try{el.dispatchEvent(new PointerEvent('pointerup',opts));}catch(e){}"
                "try{el.dispatchEvent(new MouseEvent('mouseup',opts));}catch(e){}"
                "el.click();",
                action,
            )
            clicked = True
        except Exception:
            pass
        if not clicked:
            raise MuseVideoDownloadError("Không click được nút Download của card MP4 trong Summary popup.")

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
            candidates = [
                item for item in context.download_dir.iterdir()
                if item.is_file() and item.suffix.casefold() == ".mp4"
                and (item.resolve(), item.stat().st_mtime_ns) not in before
                and item.stat().st_size > 0
            ]
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
                "Đã click Download trong Summary nhưng file MP4 chưa tải xong.",
            )
        )
        if not _valid_mp4(downloaded):
            raise MuseVideoDownloadError("Artifact tải từ Summary không phải MP4 hợp lệ.")
        if target.exists():
            if _valid_mp4(target):
                downloaded.unlink(missing_ok=True)
                return target
            raise MuseVideoDownloadError("File output đích đã tồn tại nhưng không hợp lệ; tool không ghi đè.")
        shutil.move(str(downloaded), str(target))
        if not _valid_mp4(target):
            raise MuseVideoDownloadError("File MP4 từ Summary không hợp lệ sau khi di chuyển.")
        return target

    @staticmethod
    def _close_summary(driver: Any) -> None:
        try:
            driver.execute_script(
                "/* VISUAL_LOOP_CLOSE_SUMMARY */"
                "const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();const s=getComputedStyle(e);"
                "return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};"
                "const norm=s=>(s||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                "const heading=[...document.querySelectorAll('h1,h2,h3,h4,[role=\\\"heading\\\"],div,span')]"
                ".find(e=>visible(e)&&norm(e.innerText||e.textContent)==='summary');"
                "if(heading){"
                "let root=heading.closest('[role=\\\"dialog\\\"],[aria-modal=\\\"true\\\"],dialog');"
                "if(!root){root=heading.parentElement;"
                "while(root&&root!==document.body&&!root.querySelector('button[data-testid*=\"sandbox-file-card-options\" i],button[data-testid*=\"options\" i],button[aria-label*=\"options\" i]'))"
                "root=root.parentElement;}"
                "if(root){"
                "const buttons=[...root.querySelectorAll('button,[role=\"button\"]')].filter(visible);"
                "const close=buttons.find(e=>{"
                "const aria=(e.getAttribute('aria-label')||'').toLowerCase();"
                "const title=(e.getAttribute('title')||'').toLowerCase();"
                "const cls=(e.className||'').toString().toLowerCase();"
                "if(/close|dismiss|cancel|exit|đóng|x/i.test(aria+' '+title+' '+cls))return true;"
                "const svg=e.querySelector('svg');"
                "if(svg){const d=(svg.querySelector('path')?.getAttribute('d')||'').toLowerCase();"
                "if(/close|cross|x/i.test(svg.getAttribute('class')||'')||d.includes('m18 6')||d.includes('m6 18'))return true;}"
                "return false;"
                "});"
                "if(close){close.click();}"
                "}"
                "}"
                "try{document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',code:'Escape',keyCode:27,which:27,bubbles:true}));}catch(e){}"
                "try{document.dispatchEvent(new KeyboardEvent('keyup',{key:'Escape',code:'Escape',keyCode:27,which:27,bubbles:true}));}catch(e){}"
                "const backdrop=document.querySelector('[data-radix-portal] > [data-state=\"open\"],div[class*=\"backdrop\" i],div[class*=\"overlay\" i]');"
                "if(backdrop&&visible(backdrop)){try{backdrop.click();}catch(e){}}"
            )
        except Exception:
            pass

    def _videos(self, driver: Any) -> list[Any]:
        return [item for item in self._elements(driver, self.selectors.video_results) if _visible(item)]

    def _video_fingerprints(self, driver: Any) -> set[str]:
        # Include hidden/off-screen cards. Muse can lazy-show an old card after
        # a new prompt; excluding it here made that old media look newly added.
        return {_video_fingerprint(item) for item in self._all_videos(driver)}

    def _all_videos(self, driver: Any) -> list[Any]:
        return self._elements(driver, self.selectors.video_results)

    @staticmethod
    def _install_video_watch(driver: Any, token: str) -> int:
        """Start a page-local clocked watch immediately before Send is clicked."""
        try:
            value = driver.execute_script(
                "/* VISUAL_LOOP_INSTALL_VIDEO_WATCH */"
                "const token=arguments[0];"
                "window.__visualLoopVideoWatches=window.__visualLoopVideoWatches||{};"
                "const previous=window.__visualLoopVideoWatches[token];"
                "if(previous){try{previous.observer.disconnect();}catch(e){}"
                "try{document.removeEventListener('loadedmetadata',previous.onMedia,true);}catch(e){}}"
                "const source=video=>String(video.currentSrc||video.src||"
                "(video.querySelector&&video.querySelector('source')&&video.querySelector('source').src)||'');"
                "const baseline=new Set(document.querySelectorAll('video'));"
                "const baselineSources=new Map([...baseline].map(video=>[video,source(video)]));"
                "const touched=new Map(),startedAt=Date.now();"
                "const mark=node=>{if(!node||node.nodeType!==1)return;"
                "const video=String(node.tagName||'').toLowerCase()==='video'?node:"
                "(String(node.tagName||'').toLowerCase()==='source'?node.closest('video'):null);"
                "if(video)touched.set(video,Date.now());"
                "if(node.querySelectorAll)for(const item of node.querySelectorAll('video'))touched.set(item,Date.now());};"
                "const observer=new MutationObserver(records=>{for(const record of records){"
                "mark(record.target);for(const node of record.addedNodes||[])mark(node);}});"
                "observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['src']});"
                "const onMedia=event=>mark(event.target);document.addEventListener('loadedmetadata',onMedia,true);"
                "window.__visualLoopVideoWatches[token]={startedAt,baseline,baselineSources,touched,observer,onMedia,source};"
                "return startedAt;",
                token,
            )
            return max(0, int(value or 0))
        except Exception:
            return 0

    @staticmethod
    def _stop_video_watch(driver: Any, token: str) -> None:
        if not token:
            return
        try:
            driver.execute_script(
                "/* VISUAL_LOOP_STOP_VIDEO_WATCH */"
                "const root=window.__visualLoopVideoWatches||{},state=root[arguments[0]];"
                "if(!state)return;try{state.observer.disconnect();}catch(e){}"
                "try{document.removeEventListener('loadedmetadata',state.onMedia,true);}catch(e){}"
                "delete root[arguments[0]];",
                token,
            )
        except Exception:
            pass

    def _images(self, driver: Any) -> list[Any]:
        return [item for item in self._elements(driver, self.selectors.image_results) if _visible(item)]

    def _image_fingerprints(self, driver: Any) -> set[str]:
        return {self._image_fingerprint(item) for item in self._all_images(driver)}

    def _all_images(self, driver: Any) -> list[Any]:
        return self._elements(driver, self.selectors.image_results)

    @staticmethod
    def _image_fingerprint(element: Any) -> str:
        raw = "|".join(
            (
                str(getattr(element, "id", "") or ""),
                _attr(element, "src"),
                _attr(element, "currentSrc"),
                _attr(element, "alt"),
            )
        )
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _install_image_watch(driver: Any, token: str) -> int:
        """Start a page-local clocked watch for new images immediately before Send is clicked."""
        try:
            value = driver.execute_script(
                "/* VISUAL_LOOP_INSTALL_IMAGE_WATCH */"
                "const token=arguments[0];"
                "window.__visualLoopImageWatches=window.__visualLoopImageWatches||{};"
                "const previous=window.__visualLoopImageWatches[token];"
                "if(previous){try{previous.observer.disconnect();}catch(e){}"
                "try{document.removeEventListener('load',previous.onMedia,true);}catch(e){}}"
                "const source=img=>String(img.currentSrc||img.src||'');"
                "const baseline=new Set(document.querySelectorAll('img'));"
                "const baselineSources=new Map([...baseline].map(img=>[img,source(img)]));"
                "const touched=new Map(),startedAt=Date.now();"
                "const mark=node=>{if(!node||node.nodeType!==1)return;"
                "const img=String(node.tagName||'').toLowerCase()==='img'?node:null;"
                "if(img)touched.set(img,Date.now());"
                "if(node.querySelectorAll)for(const item of node.querySelectorAll('img'))touched.set(item,Date.now());};"
                "const observer=new MutationObserver(records=>{for(const record of records){"
                "mark(record.target);for(const node of record.addedNodes||[])mark(node);}});"
                "observer.observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:['src']});"
                "const onMedia=event=>mark(event.target);document.addEventListener('load',onMedia,true);"
                "window.__visualLoopImageWatches[token]={startedAt,baseline,baselineSources,touched,observer,onMedia,source};"
                "return startedAt;",
                token,
            )
            return max(0, int(value or 0))
        except Exception:
            return 0

    @staticmethod
    def _stop_image_watch(driver: Any, token: str) -> None:
        if not token:
            return
        try:
            driver.execute_script(
                "/* VISUAL_LOOP_STOP_IMAGE_WATCH */"
                "const root=window.__visualLoopImageWatches||{},state=root[arguments[0]];"
                "if(!state)return;try{state.observer.disconnect();}catch(e){}"
                "try{document.removeEventListener('load',state.onMedia,true);}catch(e){}"
                "delete root[arguments[0]];",
                token,
            )
        except Exception:
            pass

    def _switch_mode_if_needed(self, driver: Any, target_mode: str) -> None:
        """Switch between Video and Image tabs/modes on Muse web interface if supported."""
        try:
            driver.execute_script(
                "/* VISUAL_LOOP_SWITCH_MODE */"
                "const targetMode=String(arguments[0]||'video').toLowerCase();"
                "const visible=e=>{if(!e)return false;const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};"
                "const norm=s=>(s||'').replace(/\\s+/g,' ').trim().toLowerCase();"
                "const tabs=[...document.querySelectorAll('[role=\"tab\"],button,[data-testid*=\"mode\" i],[data-testid*=\"tab\" i]')].filter(visible);"
                "for(const tab of tabs){"
                "  const t=norm(tab.innerText||tab.textContent);"
                "  const aria=norm(tab.getAttribute('aria-label')||'');"
                "  const val=norm(tab.getAttribute('data-value')||'');"
                "  const isMatch=(targetMode==='image'&&(/\\bimage\\b|\\bảnh\\b/i.test(t)||/image/i.test(aria)||val==='image'))||"
                "                (targetMode==='video'&&(/\\bvideo\\b/i.test(t)||/video/i.test(aria)||val==='video'));"
                "  if(isMatch){"
                "    const isSelected=tab.getAttribute('aria-selected')==='true'||tab.getAttribute('data-state')==='active'||tab.classList.contains('active');"
                "    if(!isSelected){try{tab.click();}catch(e){}}"
                "    break;"
                "  }"
                "}",
                target_mode,
            )
        except Exception:
            pass

    def _submission_messages(self, driver: Any) -> list[dict[str, Any]]:
        """Read stable identities for user messages currently present in the chat DOM."""
        try:
            values = driver.execute_script(
                "/* VISUAL_LOOP_USER_MESSAGE_SNAPSHOT */"
                "const nodes=[...document.querySelectorAll("
                "'[data-message-role=\\\"user\\\"],[data-role=\\\"user\\\"],"
                "[data-author=\\\"user\\\"],[data-testid*=\\\"user-message\\\" i]')];"
                "return nodes.map((element,domIndex)=>({"
                "element,dom_index:domIndex,"
                "message_id:element.getAttribute('data-message-id')||'',"
                "message_group_id:element.getAttribute('data-message-group-id')||'',"
                "text:(element.innerText||element.textContent||'').replace(/\\s+/g,' ').trim()"
                "}));"
            )
        except Exception:
            return []
        if not isinstance(values, (list, tuple)):
            return []
        records: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict) or value.get("element") is None:
                continue
            try:
                dom_index = int(value.get("dom_index", -1))
            except (TypeError, ValueError):
                dom_index = -1
            records.append(
                {
                    "element": value.get("element"),
                    "dom_index": dom_index,
                    "message_id": str(value.get("message_id") or ""),
                    "message_group_id": str(value.get("message_group_id") or ""),
                    "text": " ".join(str(value.get("text") or "").split()),
                }
            )
        return records

    def _wait_for_submission_message(
        self,
        driver: Any,
        *,
        prompt: str,
        baseline_message_ids: set[str],
        message_floor: int,
        context: MuseVideoRunContext,
    ) -> MuseSubmissionAnchor:
        """Wait for the one user message created after the current Send click."""
        wanted = " ".join(str(prompt or "").split())

        def find_anchor():
            self._check(driver, context)
            candidates: list[dict[str, Any]] = []
            for record in self._submission_messages(driver):
                message_id = str(record.get("message_id") or "")
                dom_index = int(record.get("dom_index", -1))
                text = " ".join(str(record.get("text") or "").split())
                if dom_index < message_floor:
                    continue
                if message_id and message_id in baseline_message_ids:
                    continue
                if wanted and wanted not in text:
                    continue
                candidates.append(record)
            if not candidates:
                return False
            record = min(candidates, key=lambda item: int(item.get("dom_index", -1)))
            return MuseSubmissionAnchor(
                element=record.get("element"),
                message_id=str(record.get("message_id") or ""),
                message_group_id=str(record.get("message_group_id") or ""),
                dom_index=int(record.get("dom_index", -1)),
                prompt=wanted,
                baseline_message_ids=tuple(sorted(baseline_message_ids)),
                message_floor=message_floor,
            )

        deadline = time.monotonic() + min(20.0, self.upload_timeout * 0.3)
        retries = 0
        while time.monotonic() < deadline:
            anchor = find_anchor()
            if anchor:
                return anchor
            if self._wait_stop(context, min(0.1, self.poll_interval)):
                raise MuseVideoStopped("Đã dừng worker Muse.")
            if self._prompt_still_in_composer(driver, wanted):
                retries += 1
                if retries <= 5:
                    context.log(f"Prompt vẫn còn trong ô nhập (lần {retries}); đang thử ấn lại nút Send/Enter…")
                    try:
                        self._click_generate_once(driver, context)
                    except Exception:
                        pass
                    if self._wait_stop(context, min(0.4, self.poll_interval * 2)):
                        raise MuseVideoStopped("Đã dừng worker Muse.")
            elif retries > 0:
                for _ in range(5):
                    anchor = find_anchor()
                    if anchor:
                        return anchor
                    if self._wait_stop(context, min(0.1, self.poll_interval)):
                        raise MuseVideoStopped("Đã dừng worker Muse.")
                break
            else:
                if time.monotonic() - (deadline - min(20.0, self.upload_timeout * 0.3)) > 0.4:
                    break

        if self._prompt_still_in_composer(driver, wanted):
            raise MuseVideoBatchError(
                "Muse chưa gửi được prompt; nội dung vẫn còn nằm trong ô nhập sau nhiều lần bấm Send. "
                "Tool đã dừng lại để tránh gửi nhầm hoặc tải nhầm video cũ."
            )

        context.log(
            "Muse không công khai ID tin nhắn mới trong DOM; "
            "đang nhận video bằng mốc theo dõi được cài ngay trước lúc Send."
        )
        return MuseSubmissionAnchor(
            prompt=wanted,
            baseline_message_ids=tuple(sorted(baseline_message_ids)),
            message_floor=message_floor,
        )

    @staticmethod
    def _anchor_from_job(job: MuseVideoJob) -> MuseSubmissionAnchor:
        return MuseSubmissionAnchor(
            message_id=job.submission_message_id,
            message_group_id=job.submission_message_group_id,
            dom_index=job.submission_message_index,
            prompt=" ".join(str(job.prompt or "").split()),
            baseline_message_ids=tuple(job.baseline_message_ids),
            message_floor=job.submission_message_floor,
            watch_token=job.submission_watch_token,
            started_epoch_ms=job.submission_started_epoch_ms,
        )

    def _videos_after_submission(self, driver: Any, anchor: MuseSubmissionAnchor) -> list[Any]:
        """Return videos observed after Send, prioritising the exact message/group."""
        try:
            values = driver.execute_script(
                "/* VISUAL_LOOP_VIDEOS_AFTER_MESSAGE */"
                "const live=arguments[0],wantedId=arguments[1],wantedGroup=arguments[2],"
                "wantedIndex=arguments[3],wantedText=(arguments[4]||'').replace(/\\s+/g,' ').trim(),"
                "baselineIds=new Set(arguments[5]||[]),floor=arguments[6]||0,"
                "watchToken=arguments[7]||'',sentAt=Number(arguments[8]||0);"
                "const isImageSrc=s=>!s||/^data:image[/]/i.test(s)||/^https?:.*\\.(png|jpe?g|webp|gif)(?:[?#]|$)/i.test(s);"
                "const mediaSrc=v=>String(v.currentSrc||v.src||(v.querySelector&&v.querySelector('source')&&v.querySelector('source').src)||'').trim();"
                "const isBotOrAvatar=v=>{"
                "if(!v)return true;"
                "const s=mediaSrc(v).toLowerCase();"
                "if(/(avatar|mascot|muse[-_]?bot|icon|reaction|subagent|status[-_]?anim|thinking|loading[-_]anim|placeholders?|animations?|assets?\\/static)/i.test(s))return true;"
                "const bad='header,nav,aside,[data-testid*=\"avatar\" i],[class*=\"avatar\" i],[data-testid*=\"mascot\" i],[class*=\"mascot\" i],[data-testid*=\"loading\" i],[class*=\"loading\" i],[class*=\"thinking\" i],[class*=\"reaction\" i],[class*=\"badge\" i],[class*=\"status\" i],[class*=\"indicator\" i],[data-testid*=\"subagent\" i],[class*=\"subagent\" i],[data-testid*=\"placeholder\" i],[class*=\"placeholder\" i]';"
                "if(v.matches&&v.matches(bad))return true;"
                "if(v.closest&&v.closest(bad))return true;"
                "const r=v.getBoundingClientRect?v.getBoundingClientRect():null;"
                "if(r&&r.width>0&&r.height>0&&(r.width<160||r.height<160))return true;"
                "if(v.videoWidth>0&&v.videoHeight>0&&(v.videoWidth<280&&v.videoHeight<280))return true;"
                "if(v.videoWidth>0&&v.videoHeight>0&&v.videoWidth===v.videoHeight&&v.duration>0&&v.duration<6.5)return true;"
                "if(v.duration>0&&Math.abs(v.duration-5.04)<0.25)return true;"
                "if(v.loop&&!v.controls)return true;"
                "if(v.autoplay&&v.muted&&!v.controls)return true;"
                "return false;};"
                "const isMediaVideo=v=>{"
                "const s=mediaSrc(v);return Boolean(s&&!isImageSrc(s)&&/^(blob:|https?:)/i.test(s)&&!isBotOrAvatar(v));};"
                "const users=[...document.querySelectorAll("
                "'[data-message-role=\\\"user\\\"],[data-role=\\\"user\\\"],"
                "[data-author=\\\"user\\\"],[data-message-author=\\\"user\\\"],[data-testid*=\\\"user-message\\\" i]')];"
                "const norm=e=>(e&&(e.innerText||e.textContent)||'').replace(/\\s+/g,' ').trim();"
                "const all=[...document.querySelectorAll('video')].filter(isMediaVideo);"
                "const root=window.__visualLoopVideoWatches||{},watch=watchToken?root[watchToken]:null;"
                "const appearedAt=video=>{"
                "if(!watch)return 0;const touched=Number(watch.touched.get(video)||0);if(touched)return touched;"
                "if(!watch.baseline.has(video))return Date.now();"
                "const currentSrc=watch.source(video);const oldSrc=watch.baselineSources.get(video);"
                "return (currentSrc&&currentSrc!==oldSrc)?Date.now():0;};"
                "const videos=watch?all.filter(video=>appearedAt(video)>=Math.max(0,sentAt-1000)):all;"
                "let message=(live&&live.isConnected)?live:null;"
                "if(!message&&wantedId)message=users.find(e=>e.getAttribute('data-message-id')===wantedId)||"
                "document.querySelector(`[data-message-id=\"${wantedId}\"]`)||null;"
                "if(!message&&wantedGroup)message=users.find(e=>e.getAttribute('data-message-group-id')===wantedGroup&&(!wantedText||norm(e).includes(wantedText)))||null;"
                "if(!message&&wantedIndex>=0&&users[wantedIndex]&&(!wantedText||norm(users[wantedIndex]).includes(wantedText)))message=users[wantedIndex];"
                "if(!message)message=users.find((e,index)=>index>=floor&&!baselineIds.has(e.getAttribute('data-message-id')||'')&&(!wantedText||norm(e).includes(wantedText)))||null;"
                "if(!message)return watch?videos:[];"
                "const following=(a,b)=>!!(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING);"
                "const nextUser=users.find(e=>e!==message&&following(message,e))||null;"
                "const candidates=videos.filter(video=>following(message,video)&&(!nextUser||following(video,nextUser)));"
                "const group=message.getAttribute('data-message-group-id')||wantedGroup;"
                "const grouped=group?candidates.filter(video=>{"
                "const item=video.closest('[data-message-group-id]');return item&&item.getAttribute('data-message-group-id')===group;}):[];"
                "const result=[],seen=new Set();"
                "for(const video of [...grouped,...candidates]){"
                "if(!seen.has(video)){seen.add(video);result.push(video);}}return result;",
                anchor.element,
                anchor.message_id,
                anchor.message_group_id,
                anchor.dom_index,
                anchor.prompt,
                list(anchor.baseline_message_ids),
                anchor.message_floor,
                anchor.watch_token,
                anchor.started_epoch_ms,
            )
        except Exception:
            return []
        if not isinstance(values, (list, tuple)):
            return []
        return [item for item in values if item is not None]

    @staticmethod
    def _video_ready(driver: Any, video: Any) -> bool:
        try:
            return bool(
                driver.execute_script(
                    "const v=arguments[0];if(!v)return false;"
                    "const mediaSrc=v=>String(v.currentSrc||v.src||(v.querySelector&&v.querySelector('source')&&v.querySelector('source').src)||'').trim();"
                    "const src=mediaSrc(v);"
                    "if(!src||/^data:image[/]/i.test(src)||/^https?:.*\\.(png|jpe?g|webp|gif)(?:[?#]|$)/i.test(src))return false;"
                    "if(!/^(blob:|https?:)/i.test(src))return false;"
                    "const s=src.toLowerCase();"
                    "if(/(avatar|mascot|muse[-_]?bot|icon|reaction|subagent|status[-_]?anim|thinking|loading[-_]anim|placeholders?|animations?|assets?\\/static)/i.test(s))return false;"
                    "const bad='header,nav,aside,[data-testid*=\"avatar\" i],[class*=\"avatar\" i],[data-testid*=\"mascot\" i],[class*=\"mascot\" i],[data-testid*=\"loading\" i],[class*=\"loading\" i],[class*=\"thinking\" i],[class*=\"reaction\" i],[class*=\"badge\" i],[class*=\"status\" i],[class*=\"indicator\" i],[data-testid*=\"subagent\" i],[class*=\"subagent\" i],[data-testid*=\"placeholder\" i],[class*=\"placeholder\" i]';"
                    "if(v.matches&&v.matches(bad))return false;"
                    "if(v.closest&&v.closest(bad))return false;"
                    "const r=v.getBoundingClientRect?v.getBoundingClientRect():null;"
                    "if(r&&r.width>0&&r.height>0&&(r.width<160||r.height<160))return false;"
                    "if(v.videoWidth>0&&v.videoHeight>0&&(v.videoWidth<280&&v.videoHeight<280))return false;"
                    "if(v.videoWidth>0&&v.videoHeight>0&&v.videoWidth===v.videoHeight&&v.duration>0&&v.duration<6.5)return false;"
                    "if(v.duration>0&&Math.abs(v.duration-5.04)<0.25)return false;"
                    "if(v.loop&&!v.controls)return false;"
                    "if(v.autoplay&&v.muted&&!v.controls)return false;"
                    "return (v.readyState>=2)||(v.duration>0)||Boolean(src);",
                    video,
                )
            )
        except Exception:
            return False

    def _images_after_submission(self, driver: Any, anchor: MuseSubmissionAnchor) -> list[Any]:
        """Return images observed after Send, prioritising the exact message/group."""
        try:
            values = driver.execute_script(
                "/* VISUAL_LOOP_IMAGES_AFTER_MESSAGE */"
                "const live=arguments[0],wantedId=arguments[1],wantedGroup=arguments[2],"
                "wantedIndex=arguments[3],wantedText=(arguments[4]||'').replace(/\\s+/g,' ').trim(),"
                "baselineIds=new Set(arguments[5]||[]),floor=arguments[6]||0,"
                "watchToken=arguments[7]||'',sentAt=Number(arguments[8]||0);"
                "const mediaSrc=img=>String(img.currentSrc||img.src||'').trim();"
                "const isBotOrAvatar=img=>{"
                "if(!img)return true;"
                "const s=mediaSrc(img).toLowerCase();"
                "if(/(avatar|mascot|muse[-_]?bot|icon|reaction|subagent|status[-_]?anim|thinking|loading[-_]anim|placeholders?|animations?|logo|favicon)/i.test(s))return true;"
                "const bad='header,nav,aside,[data-testid*=\"avatar\" i],[class*=\"avatar\" i],[data-testid*=\"mascot\" i],[class*=\"mascot\" i],[data-testid*=\"loading\" i],[class*=\"loading\" i],[class*=\"thinking\" i],[class*=\"reaction\" i],[class*=\"badge\" i],[class*=\"status\" i],[class*=\"indicator\" i],[data-testid*=\"subagent\" i],[class*=\"subagent\" i],[data-testid*=\"placeholder\" i],[class*=\"placeholder\" i]';"
                "if(img.matches&&img.matches(bad))return true;"
                "if(img.closest&&img.closest(bad))return true;"
                "const r=img.getBoundingClientRect?img.getBoundingClientRect():null;"
                "if(r&&r.width>0&&r.height>0&&(r.width<80||r.height<80))return true;"
                "if(img.naturalWidth>0&&img.naturalHeight>0&&(img.naturalWidth<80||img.naturalHeight<80))return true;"
                "return false;};"
                "const isMediaImage=img=>{"
                "const s=mediaSrc(img);return Boolean(s&&/^(blob:|https?:|data:image)/i.test(s)&&!isBotOrAvatar(img));};"
                "const users=[...document.querySelectorAll("
                "'[data-message-role=\\\"user\\\"],[data-role=\\\"user\\\"],"
                "[data-author=\\\"user\\\"],[data-message-author=\\\"user\\\"],[data-testid*=\\\"user-message\\\" i]')];"
                "const norm=e=>(e&&(e.innerText||e.textContent)||'').replace(/\\s+/g,' ').trim();"
                "const all=[...document.querySelectorAll('img')].filter(isMediaImage);"
                "const root=window.__visualLoopImageWatches||{},watch=watchToken?root[watchToken]:null;"
                "const appearedAt=img=>{"
                "if(!watch)return 0;const touched=Number(watch.touched.get(img)||0);if(touched)return touched;"
                "if(!watch.baseline.has(img))return Date.now();"
                "const currentSrc=watch.source(img);const oldSrc=watch.baselineSources.get(img);"
                "return (currentSrc&&currentSrc!==oldSrc)?Date.now():0;};"
                "const images=watch?all.filter(img=>appearedAt(img)>=Math.max(0,sentAt-1000)):all;"
                "let message=(live&&live.isConnected)?live:null;"
                "if(!message&&wantedId)message=users.find(e=>e.getAttribute('data-message-id')===wantedId)||"
                "document.querySelector(`[data-message-id=\"${wantedId}\"]`)||null;"
                "if(!message&&wantedGroup)message=users.find(e=>e.getAttribute('data-message-group-id')===wantedGroup&&(!wantedText||norm(e).includes(wantedText)))||null;"
                "if(!message&&wantedIndex>=0&&users[wantedIndex]&&(!wantedText||norm(users[wantedIndex]).includes(wantedText)))message=users[wantedIndex];"
                "if(!message)message=users.find((e,index)=>index>=floor&&!baselineIds.has(e.getAttribute('data-message-id')||'')&&(!wantedText||norm(e).includes(wantedText)))||null;"
                "if(!message)return watch?images:[];"
                "const following=(a,b)=>!!(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING);"
                "const nextUser=users.find(e=>e!==message&&following(message,e))||null;"
                "const candidates=images.filter(img=>following(message,img)&&(!nextUser||following(img,nextUser)));"
                "const group=message.getAttribute('data-message-group-id')||wantedGroup;"
                "const grouped=group?candidates.filter(img=>{"
                "const item=img.closest('[data-message-group-id]');return item&&item.getAttribute('data-message-group-id')===group;}):[];"
                "const result=[],seen=new Set();"
                "for(const img of [...grouped,...candidates]){"
                "if(!seen.has(img)){seen.add(img);result.push(img);}}return result;",
                anchor.element,
                anchor.message_id,
                anchor.message_group_id,
                anchor.dom_index,
                anchor.prompt,
                list(anchor.baseline_message_ids),
                anchor.message_floor,
                anchor.watch_token,
                anchor.started_epoch_ms,
            )
        except Exception:
            return []
        if not isinstance(values, (list, tuple)):
            return []
        return [item for item in values if item is not None]

    @staticmethod
    def _image_ready(driver: Any, image: Any) -> bool:
        try:
            return bool(
                driver.execute_script(
                    "const img=arguments[0];if(!img)return false;"
                    "const s=String(img.currentSrc||img.src||'').trim();"
                    "if(!s||!/^(blob:|https?:|data:image)/i.test(s))return false;"
                    "if(img.complete&&img.naturalWidth>60&&img.naturalHeight>60)return true;"
                    "const r=img.getBoundingClientRect?img.getBoundingClientRect():null;"
                    "return (r&&r.width>60&&r.height>60);",
                    image,
                )
            )
        except Exception:
            return False

    def _wait_for_distinct_images(
        self,
        driver: Any,
        *,
        expected: int,
        baseline: set[str],
        anchor: MuseSubmissionAnchor,
        context: MuseVideoRunContext,
        baseline_sessions: set[str] | None = None,
        claimed_fingerprints: set[str] | None = None,
    ) -> list[Any]:
        transient_retries = 0
        claimed = set(claimed_fingerprints or set())

        def find_results():
            nonlocal transient_retries
            self._check(driver, context)
            body = " ".join(_text(item) for item in _find(driver, "tag name", "body")).casefold()
            if any(marker in body for marker in QUOTA_MARKERS):
                raise MuseVideoQuotaExhausted(
                    "Tài khoản Muse đã chạm quota/rate limit; tác vụ chưa gửi được giữ nguyên."
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
            for image in self._images_after_submission(driver, anchor):
                fingerprint = self._image_fingerprint(image)
                if (
                    fingerprint in baseline
                    or fingerprint in seen
                    or fingerprint in claimed
                    or not self._image_ready(driver, image)
                ):
                    continue
                seen.add(fingerprint)
                values.append(image)
            if len(values) >= expected:
                return values
            return False

        result = self._wait(
            driver,
            find_results,
            self.timeout,
            context,
            f"Muse chưa trả về đủ {expected} ảnh mới; lượt đã gửi sẽ không tự gửi lại.",
        )
        return list(result) if result else []

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

    def allocate_text_jobs(
        self,
        quantity: int = 1,
        worker_ids: Iterable[int] | None = None,
    ) -> tuple[tuple[str, ...], ...]:
        """Allocate text-only jobs (no input images) across workers for Text-to-Image generation."""
        quantity = max(1, int(quantity or 1))
        with self._state_lock:
            if self.busy:
                raise MuseVideoBatchError("Không thể phân bổ khi batch Muse đang chạy.")
            selected_ids = self._normalize_worker_ids(worker_ids)
            self.enabled_worker_ids = list(selected_ids)
            self.source_paths = []
            self.current_job_ids = []
            for worker in self.workers.values():
                worker.assigned_sources = []
                worker.queue = []
                worker.current_job_id = ""
                worker.progress = 0
                worker.error = ""
                worker.state = MuseVideoWorkerState.IDLE
            for index in range(quantity):
                worker_id = selected_ids[index % len(selected_ids)]
                self.workers[worker_id].assigned_sources.append(f"Prompt #{index + 1}")
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
        normalized_settings = settings.normalized()
        action_name = "ảnh" if normalized_settings.task_mode == "image" else "video"
        if not text:
            raise ValueError(f"Prompt tạo {action_name} chung không được để trống.")
        if not str(output_dir or "").strip():
            raise ValueError(f"Hãy chọn thư mục lưu {action_name} đầu ra.")
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        with self._state_lock:
            if not self.source_paths and normalized_settings.task_mode != "image":
                raise ValueError("Hãy chọn và phân bổ ảnh trước khi bắt đầu.")
            if self.busy:
                raise MuseVideoBatchError("Batch Muse đang chạy.")
            selected_ids = self._normalize_worker_ids(worker_ids)
            self.enabled_worker_ids = list(selected_ids)
            for worker in self.workers.values():
                worker.assigned_sources = []
            if self.source_paths:
                for index, source in enumerate(self.source_paths):
                    worker_id = selected_ids[index % len(selected_ids)]
                    self.workers[worker_id].assigned_sources.append(source)
            else:
                qty = max(1, int(normalized_settings.quantity or 1))
                for index in range(qty):
                    worker_id = selected_ids[index % len(selected_ids)]
                    self.workers[worker_id].assigned_sources.append(f"Prompt #{index + 1}")
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
                            jobs = [
                                candidate
                                for candidate in candidates
                                if (candidate.submitted or candidate.submission_attempted)
                                and (not candidate.account_id or candidate.account_id == session.account_id)
                            ][:MUSE_IMAGES_PER_REQUEST * 2]
                    else:
                        jobs = []
                        for candidate in candidates:
                            if candidate.submitted or candidate.submission_attempted:
                                break
                            if candidate.state == MuseVideoJobState.LOGIN_REQUIRED:
                                candidate.state = MuseVideoJobState.PAUSED
                            jobs.append(candidate)
                            # Gom tối đa MUSE_IMAGES_PER_REQUEST (3 ảnh) cho mỗi lượt prompt trên Muse
                            if len(jobs) >= MUSE_IMAGES_PER_REQUEST:
                                break
                    worker.current_job_id = jobs[0].job_id
                    worker.progress = 0
                    for job in jobs:
                        job.attempts += 1
                        if not job.started_at:
                            job.started_at = _utc_now()
                    names = ", ".join(Path(job.source_path).name if job.source_path else f"Prompt #{job.job_id[:6]}" for job in jobs)
                    action_noun = "ảnh" if jobs[0].settings.task_mode == "image" else "video"
                    self._log_locked(worker, f"Đang xử lý lượt {len(jobs)} tác vụ {action_noun}: {names}")
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
                    self.session_manager.activate_selected_muse_tab(session.session_id)
                    is_image_mode = (jobs[0].settings.task_mode == "image")
                    if jobs[0].submitted or jobs[0].submission_attempted:
                        if is_image_mode:
                            recover_image_batch = getattr(self.automation, "recover_image_batch", None)
                            if callable(recover_image_batch):
                                current_ids = {j.job_id for j in jobs}
                                other_fps = {
                                    j.result_fingerprint for j in self.jobs.values()
                                    if j.result_fingerprint and j.job_id not in current_ids
                                    and (j.state == MuseVideoJobState.COMPLETED or (Path(j.output_path).is_file() and _valid_image(Path(j.output_path))))
                                }
                                try:
                                    return recover_image_batch(
                                        session.driver,
                                        jobs,
                                        contexts,
                                        claimed_fingerprints=other_fps,
                                    )
                                except TypeError:
                                    return recover_image_batch(session.driver, jobs, contexts)
                        else:
                            recover_batch = getattr(self.automation, "recover_batch", None)
                            if callable(recover_batch):
                                current_ids = {j.job_id for j in jobs}
                                other_fps = {
                                    j.result_fingerprint for j in self.jobs.values()
                                    if j.result_fingerprint and j.job_id not in current_ids
                                    and (j.state == MuseVideoJobState.COMPLETED or (Path(j.output_path).is_file() and _valid_mp4(Path(j.output_path))))
                                }
                                try:
                                    return recover_batch(
                                        session.driver,
                                        jobs,
                                        contexts,
                                        claimed_fingerprints=other_fps,
                                    )
                                except TypeError:
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
                except MuseSessionAuthenticationError as exc:
                    message = str(exc)
                    with self._state_lock:
                        for job in jobs:
                            job.state = MuseVideoJobState.LOGIN_REQUIRED
                            job.error = message
                        worker.state = MuseVideoWorkerState.LOGIN_REQUIRED
                        worker.error = message
                        self.session_manager._set_state(
                            session,
                            MuseSessionState.LOGIN_REQUIRED,
                            progress=0,
                            status_message=message,
                            error=message,
                        )
                        self._log_locked(worker, f"Yêu cầu phiên/đăng nhập: {message}")
                        self._persist_locked()
                    break
                except MuseSessionError as exc:
                    message = str(exc)
                    with self._state_lock:
                        for job in jobs:
                            job.state = MuseVideoJobState.FAILED
                            job.error = message
                        worker.error = message
                        self._log_locked(worker, f"Lỗi phiên Muse: {message}")
                        self._persist_locked()
                    continue
                except Exception as exc:
                    message = str(exc) if str(exc).strip() else (
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
                                name_label = Path(job.source_path).name if job.source_path else f"Prompt #{job.job_id[:6]}"
                                kind = "ảnh" if job.settings.task_mode == "image" else "video"
                                message = str(result) if isinstance(result, MuseVideoBatchError) else (
                                    f"Muse/Chrome không phản hồi khi tải {kind}; có thể chạy lại bước download."
                                )
                                job.state = MuseVideoJobState.FAILED
                                job.error = message
                                worker.error = message
                                self._log_locked(worker, f"Lỗi {name_label}: {message}")
                                continue
                            try:
                                result_path = Path(result)
                            except (TypeError, ValueError):
                                result_path = Path()
                            if not _valid_artifact(result_path, job.settings.task_mode):
                                item_type = "ảnh" if job.settings.task_mode == "image" else "file MP4"
                                message = (
                                    f"Muse đã trả kết quả nhưng chưa có {item_type} hợp lệ; "
                                    "job chỉ được phép chạy lại bước download."
                                )
                                job.state = MuseVideoJobState.FAILED
                                job.error = message
                                worker.error = message
                                name_label = Path(job.source_path).name if job.source_path else f"Prompt #{job.job_id[:6]}"
                                self._log_locked(worker, f"Chưa tải xong {name_label}: {message}")
                                continue
                            job.output_path = str(result_path)
                            job.state = MuseVideoJobState.COMPLETED
                            job.completed_at = _utc_now()
                            job.error = ""
                            name_label = Path(job.source_path).name if job.source_path else f"Prompt #{job.job_id[:6]}"
                            self._log_locked(worker, f"Hoàn tất {name_label}")
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
        is_image_mode = (self.settings.task_mode == "image")
        items_to_generate: list[tuple[int, Path | None, int]] = []
        if self.source_paths:
            for index, source_value in enumerate(self.source_paths):
                worker_id = selected_ids[index % len(selected_ids)]
                items_to_generate.append((worker_id, Path(source_value), index))
        elif is_image_mode:
            qty = max(1, int(self.settings.quantity or 1))
            for index in range(qty):
                worker_id = selected_ids[index % len(selected_ids)]
                items_to_generate.append((worker_id, None, index))

        for worker_id, source, index in items_to_generate:
            session = self.session_manager.sessions[worker_id]
            job_id = create_muse_video_job_id(source, self.prompt, self.settings, index=index)
            output = Path(self.output_dir) / _output_filename(source, session.account_id, job_id, task_mode=self.settings.task_mode)
            if output.exists() and not _valid_artifact(output, self.settings.task_mode):
                output = _available_output_path(output)
            job = self.jobs.get(job_id)
            if job is None:
                job = MuseVideoJob(
                    job_id=job_id,
                    source_path=str(source) if source else "",
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
                    if _valid_artifact(output, self.settings.task_mode):
                        job.output_path = str(output)
                    elif _valid_artifact(current_output, self.settings.task_mode):
                        if _path_key(current_output) != _path_key(output):
                            try:
                                if output.exists():
                                    raise OSError("target exists")
                                output.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(current_output, output)
                            except OSError:
                                job.state = MuseVideoJobState.FAILED
                                job.error = (
                                    "Không thể chép file kết quả đã hoàn tất sang thư mục output hiện tại; "
                                    "tool không ghi đè file có sẵn."
                                )
                            else:
                                if _valid_artifact(output, self.settings.task_mode):
                                    job.output_path = str(output)
                                else:
                                    job.state = MuseVideoJobState.FAILED
                                    job.error = "Bản sao file trong thư mục output hiện tại không hợp lệ."
                    else:
                        job.output_path = str(output)
                        job.state = (
                            MuseVideoJobState.DOWNLOADING
                            if job.submitted or job.submission_attempted
                            else MuseVideoJobState.PENDING
                        )
                        job.completed_at = ""
                        kind = "ảnh" if self.settings.task_mode == "image" else "MP4"
                        job.error = f"File {kind} không còn tồn tại; đang khôi phục bước download."
                elif job.submitted or job.submission_attempted:
                    if not _valid_artifact(current_output, self.settings.task_mode):
                        job.output_path = str(output)
                    if job.state in {
                        MuseVideoJobState.FAILED,
                        MuseVideoJobState.PAUSED,
                        MuseVideoJobState.STOPPED,
                        MuseVideoJobState.LOGIN_REQUIRED,
                    }:
                        job.state = MuseVideoJobState.DOWNLOADING
                        job.error = ""
                else:
                    if not _valid_artifact(current_output, self.settings.task_mode):
                        job.output_path = str(output)
                    if job.state in {
                        MuseVideoJobState.FAILED,
                        MuseVideoJobState.PAUSED,
                        MuseVideoJobState.STOPPED,
                        MuseVideoJobState.LOGIN_REQUIRED,
                    }:
                        job.state = MuseVideoJobState.PENDING
                        job.error = ""

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
        driver_keys: list[tuple[str, object]] = []
        for session_id in ready:
            session = self.session_manager.sessions[session_id]
            driver = session.driver
            capabilities = getattr(driver, "capabilities", {}) or {}
            browser_options: dict[str, object] = {}
            if isinstance(capabilities, dict):
                for options_name in ("goog:chromeOptions", "ms:edgeOptions"):
                    value = capabilities.get(options_name, {})
                    if isinstance(value, dict) and value.get("debuggerAddress"):
                        browser_options = value
                        break
            debugger_address = str(browser_options.get("debuggerAddress", "")).strip().casefold()
            if not debugger_address:
                try:
                    port = int((session.profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
                except (OSError, ValueError, IndexError):
                    port = 0
                if 1 <= port <= 65535:
                    debugger_address = f"127.0.0.1:{port}"
            driver_keys.append(
                ("debugger", debugger_address) if debugger_address else ("driver", id(driver))
            )
        if len(set(driver_keys)) != len(driver_keys):
            raise MuseVideoBatchError(
                "Hai tài khoản đang trỏ vào cùng một Chrome/profile. Hãy mở lại từng profile, quét tab và chọn lại."
            )
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
                self.jobs[job_id].source_path or f"Prompt #{self.jobs[job_id].job_id[:6]}"
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
            self._sanitize_and_seed_claimed_artifacts_locked()
            self._persist_locked()

    def _sanitize_and_seed_claimed_artifacts_locked(self) -> None:
        """Sanitize any duplicate video assignments across completed jobs."""
        seen_fps: dict[str, MuseVideoJob] = {}
        for job in list(self.jobs.values()):
            if not job.result_fingerprint:
                continue
            has_valid_output = Path(job.output_path).is_file() and _valid_artifact(Path(job.output_path), job.settings.task_mode)
            if job.state != MuseVideoJobState.COMPLETED and not has_valid_output:
                continue
            fp = job.result_fingerprint
            if fp in seen_fps:
                out = Path(job.output_path)
                if out.is_file():
                    try:
                        out.unlink(missing_ok=True)
                    except Exception:
                        pass
                job.result_fingerprint = ""
                job.result_session_fingerprint = ""
                job.result_session_text = ""
                job.state = MuseVideoJobState.SUBMITTED if job.submitted else MuseVideoJobState.PENDING
                job.completed_at = ""
                job.error = ""
                worker = self.workers.get(job.worker_id)
                if worker and job.job_id not in worker.queue:
                    worker.queue.append(job.job_id)
                    if worker.state == MuseVideoWorkerState.COMPLETED:
                        worker.state = MuseVideoWorkerState.PAUSED
            else:
                seen_fps[fp] = job

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


def create_muse_video_job_id(
    source: str | Path,
    prompt: str,
    settings: MuseVideoSettings,
    index: int = 0,
) -> str:
    path_str = str(source or "").strip()
    norm_settings = settings.normalized()
    if path_str:
        path = Path(path_str).expanduser().resolve()
        if path.is_file():
            stat = path.stat()
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    file_hash.update(chunk)
            payload = {
                # This version deliberately invalidates checkpoints made by the old
                # multi-image/card-download flow.  From this protocol onward, reruns
                # with identical input reuse the verified MP4 and never submit again.
                "protocol": MUSE_JOB_PROTOCOL,
                "path": str(path).casefold(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "image_sha256": file_hash.hexdigest(),
                "prompt_sha256": hashlib.sha256(str(prompt).encode("utf-8")).hexdigest(),
                "settings": asdict(norm_settings),
                "task_mode": norm_settings.task_mode,
            }
            return hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
    # Prompt-only (Text-to-Image) job without reference image file
    payload = {
        "protocol": MUSE_JOB_PROTOCOL,
        "path": "",
        "index": index,
        "prompt_sha256": hashlib.sha256(str(prompt).encode("utf-8")).hexdigest(),
        "settings": asdict(norm_settings),
        "task_mode": norm_settings.task_mode,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _output_filename(source: Path | str, account_id: str, job_id: str, task_mode: str = "video") -> str:
    path_obj = Path(source) if source else None
    if path_obj and str(source).strip():
        stem = re.sub(r'[^A-Za-z0-9._-]+', "_", path_obj.stem).strip("._") or "image"
    else:
        stem = f"image_{job_id[:8]}"
    safe_account = re.sub(r"[^A-Za-z0-9_-]+", "_", account_id).strip("_") or "account"
    ext = "png" if task_mode == "image" else "mp4"
    return f"{stem}__{safe_account}__{job_id[:16]}.{ext}"


def _valid_image(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open("rb") as stream:
            header = stream.read(32)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return True
        if header.startswith(b"\xff\xd8\xff"):
            return True
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return True
        if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            return True
        if path.suffix.casefold() in SUPPORTED_IMAGE_SUFFIXES and path.stat().st_size > 256:
            return True
        return False
    except OSError:
        return False


def _valid_artifact(path: Path, task_mode: str = "video") -> bool:
    if task_mode == "image":
        return _valid_image(path)
    return _valid_mp4(path)


def _valid_mp4(path: Path) -> bool:
    try:
        if not path.is_file() or path.suffix.casefold() != ".mp4" or path.stat().st_size <= 0:
            return False
        with path.open("rb") as stream:
            header = stream.read(64)
        if b"ftyp" not in header:
            return False
        if path.stat().st_size < 500_000:
            try:
                import subprocess, json
                res = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "stream=width,height,duration", "-of", "json", str(path)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    for s in data.get("streams", []):
                        w = int(s.get("width") or 0)
                        h = int(s.get("height") or 0)
                        d = float(s.get("duration") or 0.0)
                        if w == 480 and h == 480 and abs(d - 5.04) < 0.25:
                            return False
            except Exception:
                pass
        return True
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
