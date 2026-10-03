from __future__ import annotations

import copy
import hashlib
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ai.providers.base import (
    GenerationRequest,
    ProviderCancelled,
    ProviderError,
    ProviderExecutionContext,
    VideoProvider,
)
from models.ai_job import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AiGenerationBatch,
    AiGenerationJob,
    AiJobStore,
    JobStatus,
    utc_now,
)
from models.ai_audit import AiAuditLog
from render.ffprobe import probe_media


class AiJobManager:
    def __init__(
        self,
        providers: dict[str, VideoProvider],
        *,
        store: AiJobStore | None = None,
        concurrency: int = 2,
        retry_limit: int = 3,
        ffprobe_path: str = "ffprobe",
        audit_log: AiAuditLog | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.store = store or AiJobStore()
        self.concurrency = min(8, max(1, int(concurrency)))
        self.retry_limit = min(10, max(0, int(retry_limit)))
        self.ffprobe_path = ffprobe_path
        self.audit_log = audit_log or AiAuditLog(self.store.path.with_name("ai_audit.jsonl"))
        loaded_jobs, loaded_batches = self.store.load()
        self._jobs = {item.id: item for item in loaded_jobs}
        self._batches = {item.id: item for item in loaded_batches}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._queued_ids: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._started = False
        self._provider_slots = {
            provider_id: threading.BoundedSemaphore(max(
                1,
                max((capability.max_concurrency for capability in provider.capabilities()), default=1),
            ))
            for provider_id, provider in self.providers.items()
        }
        self._recover_interrupted_jobs()

    def _recover_interrupted_jobs(self) -> None:
        changed = False
        for job in self._jobs.values():
            if job.status not in ACTIVE_STATUSES:
                continue
            provider = self.providers.get(job.request.provider_id)
            can_resume = bool(provider and provider.supports_resume and job.external_id)
            if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAITING} or can_resume:
                job.status = JobStatus.QUEUED
                job.message = "Chờ tiếp tục sau khi mở lại ứng dụng."
            else:
                job.status = JobStatus.FAILED
                job.error_code = "interrupted"
                job.error_message = "Ứng dụng đã đóng trước khi tác vụ local hoàn tất; có thể chạy lại thủ công."
                job.message = job.error_message
                job.completed_at = utc_now()
            job.touch()
            changed = True
        if changed:
            self._save_locked()

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                self._stopping.clear()
                for index in range(self.concurrency):
                    thread = threading.Thread(
                        target=self._worker,
                        name=f"ai-job-worker-{index + 1}",
                        daemon=True,
                    )
                    self._threads.append(thread)
                    thread.start()
            for job in self._jobs.values():
                if job.status == JobStatus.QUEUED:
                    self._enqueue_locked(job.id)

    def add_batch(
        self,
        requests: Iterable[GenerationRequest],
        *,
        name: str = "",
        auto_start: bool = True,
    ) -> AiGenerationBatch:
        request_list = list(requests)
        if not request_list:
            raise ValueError("Batch không có job nào.")
        for request in request_list:
            provider = self.providers.get(request.provider_id)
            if provider is None:
                raise ValueError(f"Provider không tồn tại: {request.provider_id}")
            provider.validate(request)
        batch = AiGenerationBatch(name=name.strip() or f"AI Batch {utc_now()}")
        with self._lock:
            for request in request_list:
                job = AiGenerationJob(
                    request=copy.deepcopy(request),
                    batch_id=batch.id,
                    status=JobStatus.QUEUED,
                    max_attempts=max(1, self.retry_limit + 1),
                    message="Đang chờ xử lý.",
                    estimated_cost=max(0.0, float(request.options.get("estimated_cost_usd") or 0)),
                )
                self._jobs[job.id] = job
                batch.job_ids.append(job.id)
                batch.estimated_cost += job.estimated_cost
            self._batches[batch.id] = batch
            self._save_locked()
            self._audit(
                "batch.created",
                target_type="batch",
                target_id=batch.id,
                details={"name": batch.name, "job_count": len(batch.job_ids)},
            )
            if auto_start:
                self.start()
                for job_id in batch.job_ids:
                    self._enqueue_locked(job_id)
        return copy.deepcopy(batch)

    def retry(self, job_ids: Iterable[str]) -> None:
        with self._lock:
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if not job or job.status not in {JobStatus.FAILED, JobStatus.BLOCKED, JobStatus.CANCELLED}:
                    continue
                # A policy block must be corrected by changing the request;
                # blind retry would only repeat the same rejected content.
                if job.status == JobStatus.BLOCKED:
                    continue
                job.status = JobStatus.QUEUED
                job.progress = 0
                job.message = "Đã xếp hàng chạy lại."
                job.error_code = ""
                job.error_message = ""
                job.completed_at = ""
                job.external_id = ""
                job.attempt_count = 0
                job.touch()
                self._cancel_events.pop(job.id, None)
                self._enqueue_locked(job.id)
                self._audit(
                    "job.retry_requested", target_type="job", target_id=job.id,
                    details={"provider": job.request.provider_id, "model": job.request.model_id},
                )
            self._save_locked()
        self.start()

    def cancel(self, job_ids: Iterable[str]) -> None:
        with self._lock:
            for job_id in job_ids:
                job = self._jobs.get(job_id)
                if not job or job.status in TERMINAL_STATUSES:
                    continue
                self._cancel_events.setdefault(job_id, threading.Event()).set()
                if job.status in {JobStatus.QUEUED, JobStatus.RETRY_WAITING}:
                    self._transition_locked(job, JobStatus.CANCELLED, "Đã hủy trước khi gửi.")
                self._audit(
                    "job.cancel_requested", target_type="job", target_id=job.id,
                    details={"status": job.status.value},
                )
            self._save_locked()

    def cancel_all(self) -> None:
        self.cancel(job.id for job in self.snapshot() if job.status in ACTIVE_STATUSES)

    def remove_terminal(self, job_ids: Iterable[str] | None = None) -> None:
        requested = set(job_ids or [])
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status not in TERMINAL_STATUSES:
                    continue
                if requested and job_id not in requested:
                    continue
                self._jobs.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._queued_ids.discard(job_id)
            for batch_id, batch in list(self._batches.items()):
                batch.job_ids = [job_id for job_id in batch.job_ids if job_id in self._jobs]
                if not batch.job_ids:
                    self._batches.pop(batch_id, None)
            self._save_locked()

    def snapshot(self) -> list[AiGenerationJob]:
        with self._lock:
            return [copy.deepcopy(item) for item in sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)]

    def batches(self) -> list[AiGenerationBatch]:
        with self._lock:
            return [copy.deepcopy(item) for item in self._batches.values()]

    def estimated_cost_today(self) -> float:
        today = datetime.now(timezone.utc).date()
        with self._lock:
            total = 0.0
            for job in self._jobs.values():
                try:
                    created = datetime.fromisoformat(job.created_at).astimezone(timezone.utc).date()
                except (TypeError, ValueError):
                    continue
                if created == today:
                    total += max(0.0, float(job.estimated_cost))
            return total

    @property
    def busy(self) -> bool:
        return any(job.status in ACTIVE_STATUSES for job in self.snapshot())

    def shutdown(self, wait: bool = False) -> None:
        self._stopping.set()
        for _thread in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join(timeout=3)

    def _enqueue_locked(self, job_id: str) -> None:
        if job_id in self._queued_ids:
            return
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.QUEUED:
            return
        self._queued_ids.add(job_id)
        self._queue.put(job_id)

    def _worker(self) -> None:
        while not self._stopping.is_set():
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            with self._lock:
                self._queued_ids.discard(job_id)
            try:
                self._execute(job_id)
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str) -> None:
        with self._lock:
            pending = self._jobs.get(job_id)
            if not pending or pending.status != JobStatus.QUEUED:
                return
            provider_id = pending.request.provider_id
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
            slot = self._provider_slots.get(provider_id)
        if slot is None:
            with self._lock:
                pending = self._jobs.get(job_id)
                if pending:
                    self._fail_locked(pending, "provider_missing", f"Provider không tồn tại: {provider_id}")
            return
        while not slot.acquire(timeout=0.25):
            if cancel_event.is_set() or self._stopping.is_set():
                with self._lock:
                    pending = self._jobs.get(job_id)
                    if pending and pending.status not in TERMINAL_STATUSES:
                        self._transition_locked(pending, JobStatus.CANCELLED, "Đã hủy khi chờ lượt provider.")
                return
        try:
            self._run_job(job_id)
        finally:
            slot.release()

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.QUEUED:
                return
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
            provider = self.providers.get(job.request.provider_id)
            if provider is None:
                self._fail_locked(job, "provider_missing", f"Provider không tồn tại: {job.request.provider_id}")
                return
            job.attempt_count += 1
            self._transition_locked(job, JobStatus.SUBMITTING, "Đang gửi yêu cầu đến provider.")
            request = copy.deepcopy(job.request)
            external_id = job.external_id

        def progress(value: int, message: str) -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if not current or current.status in TERMINAL_STATUSES:
                    return
                current.progress = min(100, max(current.progress, int(value)))
                current.message = str(message or current.message)
                if current.progress >= 90:
                    current.status = JobStatus.DOWNLOADING
                elif current.external_id and current.progress >= 8:
                    current.status = JobStatus.RENDERING
                current.touch()
                self._save_locked()

        def save_external_id(value: str) -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if not current:
                    return
                current.external_id = value
                current.status = JobStatus.ACCEPTED
                current.message = f"Provider đã nhận job {value}."
                current.touch()
                self._save_locked()

        context = ProviderExecutionContext(
            progress=progress,
            cancelled=cancel_event.is_set,
            external_id=external_id,
            save_external_id=save_external_id,
        )
        try:
            provider.generate(request, context)
            checksum = self._verify_output(request.output_path)
        except ProviderCancelled as exc:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    self._transition_locked(current, JobStatus.CANCELLED, str(exc))
            return
        except ProviderError as exc:
            self._handle_provider_error(job_id, exc, cancel_event)
            return
        except Exception as exc:
            self._handle_provider_error(
                job_id,
                ProviderError(str(exc), code="unexpected", retryable=False),
                cancel_event,
            )
            return

        with self._lock:
            current = self._jobs.get(job_id)
            if not current:
                return
            current.progress = 100
            current.output_checksum = checksum
            current.error_code = ""
            current.error_message = ""
            self._transition_locked(current, JobStatus.COMPLETED, f"Hoàn tất: {request.output_path}")

    def _handle_provider_error(
        self,
        job_id: str,
        error: ProviderError,
        cancel_event: threading.Event,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            if cancel_event.is_set():
                self._transition_locked(job, JobStatus.CANCELLED, "Đã hủy tác vụ AI.")
                return
            if error.policy_blocked:
                job.error_code = error.code
                job.error_message = str(error)
                self._transition_locked(job, JobStatus.BLOCKED, str(error))
                return
            if error.auth_error or error.quota_error:
                self._fail_locked(job, error.code, str(error))
                self._stop_related_jobs_locked(job, error)
                return
            if error.retryable and not error.auth_error and not error.quota_error and job.attempt_count < job.max_attempts:
                if error.retry_after:
                    delay = error.retry_after
                else:
                    base_delay = min(60.0, 2.0 ** max(1, job.attempt_count))
                    delay = base_delay + random.uniform(0, min(1.0, base_delay * 0.25))
                job.error_code = error.code
                job.error_message = str(error)
                self._transition_locked(job, JobStatus.RETRY_WAITING, f"Lỗi tạm thời; thử lại sau {delay:.0f} giây.")
            else:
                self._fail_locked(job, error.code, str(error))
                return

        if cancel_event.wait(delay):
            with self._lock:
                job = self._jobs.get(job_id)
                if job and job.status == JobStatus.RETRY_WAITING:
                    self._transition_locked(job, JobStatus.CANCELLED, "Đã hủy trong khi chờ retry.")
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status != JobStatus.RETRY_WAITING:
                return
            job.status = JobStatus.QUEUED
            job.message = "Đã đến thời điểm retry."
            job.touch()
            self._save_locked()
            self._enqueue_locked(job.id)

    def _verify_output(self, output_path: str) -> str:
        target = Path(output_path)
        if not target.is_file() or target.stat().st_size <= 0:
            raise ProviderError("Provider không tạo file video hợp lệ.", code="missing_output", retryable=True)
        info = probe_media(str(target), self.ffprobe_path)
        if not info.has_video or info.duration <= 0:
            raise ProviderError("FFprobe không tìm thấy video hợp lệ trong file đầu ra.", code="invalid_output")
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    def _transition_locked(self, job: AiGenerationJob, status: JobStatus, message: str) -> None:
        previous = job.status
        job.status = status
        job.message = message
        if status in TERMINAL_STATUSES:
            job.completed_at = utc_now()
        job.touch()
        self._save_locked()
        if status in TERMINAL_STATUSES:
            self._audit(
                "job.finished",
                target_type="job",
                target_id=job.id,
                details={
                    "from": previous.value,
                    "status": status.value,
                    "provider": job.request.provider_id,
                    "model": job.request.model_id,
                    "error_code": job.error_code,
                    "output": job.request.output_path if status == JobStatus.COMPLETED else "",
                },
            )

    def _fail_locked(self, job: AiGenerationJob, code: str, message: str) -> None:
        job.error_code = code
        job.error_message = message
        self._transition_locked(job, JobStatus.FAILED, message)

    def _stop_related_jobs_locked(self, failed_job: AiGenerationJob, error: ProviderError) -> None:
        """Stop queued siblings after a provider-wide auth or quota failure.

        Continuing to submit the rest of a batch cannot succeed in this case and
        can amplify rate limiting or unexpected cloud spend.
        """
        reason = "quota" if error.quota_error else "authentication"
        message = (
            "Đã dừng vì provider hết quota/giới hạn thanh toán."
            if error.quota_error
            else "Đã dừng vì khóa API không hợp lệ hoặc không đủ quyền."
        )
        for sibling in self._jobs.values():
            if sibling.id == failed_job.id or sibling.batch_id != failed_job.batch_id:
                continue
            if sibling.request.provider_id != failed_job.request.provider_id:
                continue
            if sibling.status in TERMINAL_STATUSES:
                continue
            if sibling.status not in {JobStatus.QUEUED, JobStatus.RETRY_WAITING}:
                self._cancel_events.setdefault(sibling.id, threading.Event()).set()
                sibling.message = message + " Tác vụ đã gửi sẽ dừng theo dõi khi provider phản hồi."
                sibling.touch()
                continue
            sibling.error_code = reason
            sibling.error_message = message
            self._transition_locked(sibling, JobStatus.FAILED, message)
        self._save_locked()

    def _audit(self, event: str, **kwargs) -> None:
        # Audit logging is important, but a full/read-only disk must not change
        # the generation outcome or strand a job in an active state.
        try:
            self.audit_log.record(event, **kwargs)
        except OSError:
            pass

    def _save_locked(self) -> None:
        self.store.save(self._jobs.values(), self._batches.values())
