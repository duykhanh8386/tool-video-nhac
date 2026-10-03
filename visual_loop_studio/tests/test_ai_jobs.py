from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.job_manager import AiJobManager
from ai.providers.base import (
    GenerationRequest,
    ModelCapability,
    ProviderError,
    ProviderExecutionContext,
    ProviderHealth,
    VideoProvider,
)
from models.ai_job import AiGenerationJob, AiJobStore, JobStatus


class FakeProvider(VideoProvider):
    provider_id = "fake"
    display_name = "Fake Provider"
    supports_resume = True

    def __init__(self, failures: list[ProviderError] | None = None):
        self.failures = list(failures or [])
        self.seen_external_ids: list[str] = []

    def capabilities(self):
        return (
            ModelCapability(
                provider_id="fake",
                model_id="fake-video",
                display_name="Fake Video",
                durations=(4,),
                resolutions=("720p",),
                max_images=1,
            ),
        )

    def health_check(self):
        return ProviderHealth(True, "ready")

    def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> str:
        self.seen_external_ids.append(context.external_id)
        if not context.external_id:
            context.set_external_id("external-1")
        if self.failures:
            raise self.failures.pop(0)
        context.progress(50, "rendering")
        Path(request.output_path).write_bytes(b"video")
        context.progress(100, "done")
        return request.output_path


class BrokenAuditLog:
    def record(self, *_args, **_kwargs) -> None:
        raise OSError("disk is read-only")


def wait_for_status(manager: AiJobManager, status: JobStatus, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = manager.snapshot()
        if jobs and jobs[0].status == status:
            return jobs[0]
        time.sleep(0.02)
    jobs = manager.snapshot()
    raise AssertionError(f"Expected {status}; got {[job.status for job in jobs]}")


class AiJobManagerTests(unittest.TestCase):
    def request(self, folder: str) -> GenerationRequest:
        return GenerationRequest(
            provider_id="fake",
            model_id="fake-video",
            prompt="make video",
            output_path=str(Path(folder) / "result.mp4"),
            duration=4,
            resolution="720p",
        )

    def test_job_completes_and_persists_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = AiJobStore(Path(folder) / "jobs.json")
            manager = AiJobManager({"fake": FakeProvider()}, store=store, concurrency=1)
            with patch.object(manager, "_verify_output", return_value="sha256"):
                manager.add_batch([self.request(folder)])
                job = wait_for_status(manager, JobStatus.COMPLETED)
            manager.shutdown(wait=True)
            self.assertEqual(job.output_checksum, "sha256")
            saved, _batches = store.load()
            self.assertEqual(saved[0].status, JobStatus.COMPLETED)

    def test_temporary_error_is_retried_with_limit(self) -> None:
        failure = ProviderError("network", code="network", retryable=True, retry_after=0.01)
        provider = FakeProvider([failure])
        with tempfile.TemporaryDirectory() as folder:
            manager = AiJobManager(
                {"fake": provider},
                store=AiJobStore(Path(folder) / "jobs.json"),
                concurrency=1,
                retry_limit=2,
            )
            with patch.object(manager, "_verify_output", return_value="sha256"):
                manager.add_batch([self.request(folder)])
                job = wait_for_status(manager, JobStatus.COMPLETED)
            manager.shutdown(wait=True)
        self.assertEqual(job.attempt_count, 2)
        self.assertEqual(provider.seen_external_ids, ["", "external-1"])

    def test_policy_error_is_blocked_without_retry(self) -> None:
        provider = FakeProvider([ProviderError("blocked", code="policy", policy_blocked=True)])
        with tempfile.TemporaryDirectory() as folder:
            manager = AiJobManager(
                {"fake": provider},
                store=AiJobStore(Path(folder) / "jobs.json"),
                concurrency=1,
            )
            manager.add_batch([self.request(folder)])
            job = wait_for_status(manager, JobStatus.BLOCKED)
            manager.shutdown(wait=True)
        self.assertEqual(job.attempt_count, 1)
        self.assertEqual(len(provider.seen_external_ids), 1)

    def test_saved_external_task_resumes_after_restart(self) -> None:
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as folder:
            store = AiJobStore(Path(folder) / "jobs.json")
            request = self.request(folder)
            job = AiGenerationJob(
                request=request,
                status=JobStatus.RENDERING,
                external_id="external-saved",
            )
            store.save([job], [])
            manager = AiJobManager({"fake": provider}, store=store, concurrency=1)
            with patch.object(manager, "_verify_output", return_value="sha256"):
                manager.start()
                resumed = wait_for_status(manager, JobStatus.COMPLETED)
            manager.shutdown(wait=True)
        self.assertEqual(resumed.external_id, "external-saved")
        self.assertEqual(provider.seen_external_ids, ["external-saved"])

    def test_quota_error_stops_queued_siblings_without_submitting_them(self) -> None:
        provider = FakeProvider([
            ProviderError("quota exceeded", code="quota", quota_error=True, retryable=True),
        ])
        with tempfile.TemporaryDirectory() as folder:
            manager = AiJobManager(
                {"fake": provider},
                store=AiJobStore(Path(folder) / "jobs.json"),
                concurrency=1,
            )
            manager.add_batch([self.request(folder), self.request(folder)])
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                jobs = manager.snapshot()
                if jobs and all(job.status == JobStatus.FAILED for job in jobs):
                    break
                time.sleep(0.02)
            manager.shutdown(wait=True)
        self.assertEqual([job.status for job in jobs], [JobStatus.FAILED, JobStatus.FAILED])
        self.assertEqual(len(provider.seen_external_ids), 1)

    def test_audit_failure_does_not_change_job_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manager = AiJobManager(
                {"fake": FakeProvider()},
                store=AiJobStore(Path(folder) / "jobs.json"),
                concurrency=1,
                audit_log=BrokenAuditLog(),
            )
            with patch.object(manager, "_verify_output", return_value="sha256"):
                manager.add_batch([self.request(folder)])
                job = wait_for_status(manager, JobStatus.COMPLETED)
            manager.shutdown(wait=True)
        self.assertEqual(job.output_checksum, "sha256")

    def test_estimated_cost_is_persisted_for_job_and_batch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = AiJobStore(Path(folder) / "jobs.json")
            manager = AiJobManager({"fake": FakeProvider()}, store=store, concurrency=1)
            request = self.request(folder)
            request.options["estimated_cost_usd"] = 1.25
            batch = manager.add_batch([request, request], auto_start=False)
            self.assertEqual(batch.estimated_cost, 2.5)
            self.assertEqual(manager.estimated_cost_today(), 2.5)
            manager.shutdown(wait=True)
            jobs, batches = store.load()
        self.assertEqual([job.estimated_cost for job in jobs], [1.25, 1.25])
        self.assertEqual(batches[0].estimated_cost, 2.5)


if __name__ == "__main__":
    unittest.main()
