from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ai.providers.base import GenerationRequest
from utils.paths import DATA_DIR


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStatus(str, Enum):
    DRAFT = "Draft"
    QUEUED = "Queued"
    SUBMITTING = "Submitting"
    ACCEPTED = "Accepted"
    RENDERING = "Rendering"
    DOWNLOADING = "Downloading"
    RETRY_WAITING = "RetryWaiting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    BLOCKED = "Blocked"
    CANCELLED = "Cancelled"


TERMINAL_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.FAILED,
    JobStatus.BLOCKED,
    JobStatus.CANCELLED,
}


ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.SUBMITTING,
    JobStatus.ACCEPTED,
    JobStatus.RENDERING,
    JobStatus.DOWNLOADING,
    JobStatus.RETRY_WAITING,
}


@dataclass
class AiGenerationJob:
    request: GenerationRequest
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    batch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: JobStatus = JobStatus.DRAFT
    progress: int = 0
    message: str = ""
    external_id: str = ""
    attempt_count: int = 0
    max_attempts: int = 3
    error_code: str = ""
    error_message: str = ""
    output_checksum: str = ""
    estimated_cost: float = 0.0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["request"] = self.request.to_dict()
        return data

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AiGenerationJob":
        data = dict(value or {})
        data["request"] = GenerationRequest.from_dict(data.get("request") or {})
        try:
            data["status"] = JobStatus(data.get("status") or JobStatus.DRAFT.value)
        except ValueError:
            data["status"] = JobStatus.FAILED
            data["error_code"] = "unknown_saved_status"
            data["error_message"] = "Trạng thái job đã lưu không còn được hỗ trợ."
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: item for key, item in data.items() if key in allowed})

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass
class AiGenerationBatch:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    job_ids: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AiGenerationBatch":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: item for key, item in (value or {}).items() if key in allowed})


class AiJobStore:
    def __init__(self, path: str | Path = DATA_DIR / "ai_jobs.json") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> tuple[list[AiGenerationJob], list[AiGenerationBatch]]:
        with self._lock:
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    payload = json.load(stream)
            except FileNotFoundError:
                return [], []
            except (OSError, ValueError, TypeError):
                return [], []
            jobs = [AiGenerationJob.from_dict(item) for item in payload.get("jobs", []) if isinstance(item, dict)]
            batches = [AiGenerationBatch.from_dict(item) for item in payload.get("batches", []) if isinstance(item, dict)]
            return jobs, batches

    def save(
        self,
        jobs: Iterable[AiGenerationJob],
        batches: Iterable[AiGenerationBatch],
    ) -> None:
        payload = {
            "version": 1,
            "jobs": [item.to_dict() for item in jobs],
            "batches": [item.to_dict() for item in batches],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            temporary.replace(self.path)
