from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


ProgressCallback = Callable[[int, str], None]
CancelledCallback = Callable[[], bool]
ExternalIdCallback = Callable[[str], None]


@dataclass(frozen=True)
class ModelCapability:
    provider_id: str
    model_id: str
    display_name: str
    supports_text: bool = True
    supports_image: bool = True
    min_images: int = 0
    max_images: int = 1
    durations: tuple[int, ...] = ()
    duration_min: int = 0
    duration_max: int = 0
    aspect_ratios: tuple[str, ...] = ("16:9", "9:16")
    resolutions: tuple[str, ...] = ("720p",)
    supports_audio: bool = False
    max_concurrency: int = 1

    def supports_duration(self, value: int) -> bool:
        if self.durations:
            return value in self.durations
        if self.duration_min and value < self.duration_min:
            return False
        if self.duration_max and value > self.duration_max:
            return False
        return value > 0

    def duration_choices(self) -> tuple[int, ...]:
        if self.durations:
            return self.durations
        if self.duration_min and self.duration_max:
            span = self.duration_max - self.duration_min
            if span <= 30:
                return tuple(range(self.duration_min, self.duration_max + 1))
        return ()


@dataclass
class GenerationRequest:
    provider_id: str
    model_id: str
    prompt: str
    output_path: str
    source_images: list[str] = field(default_factory=list)
    duration: int = 8
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    generate_audio: bool = False
    seed: int = -1
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GenerationRequest":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: item for key, item in (value or {}).items() if key in allowed})


@dataclass(frozen=True)
class ProviderHealth:
    available: bool
    message: str


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        auth_error: bool = False,
        quota_error: bool = False,
        policy_blocked: bool = False,
        retry_after: float = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.auth_error = auth_error
        self.quota_error = quota_error
        self.policy_blocked = policy_blocked
        self.retry_after = max(0.0, float(retry_after or 0))


class ProviderCancelled(ProviderError):
    def __init__(self, message: str = "Đã hủy tác vụ AI.") -> None:
        super().__init__(message, code="cancelled")


@dataclass
class ProviderExecutionContext:
    progress: ProgressCallback = lambda _value, _message: None
    cancelled: CancelledCallback = lambda: False
    external_id: str = ""
    save_external_id: ExternalIdCallback = lambda _value: None

    def set_external_id(self, value: str) -> None:
        normalized = str(value or "").strip()
        if not normalized:
            return
        self.external_id = normalized
        self.save_external_id(normalized)

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise ProviderCancelled()


class VideoProvider(ABC):
    provider_id: str
    display_name: str
    supports_resume: bool = False

    @abstractmethod
    def capabilities(self) -> tuple[ModelCapability, ...]:
        raise NotImplementedError

    def capability(self, model_id: str) -> ModelCapability:
        for item in self.capabilities():
            if item.model_id == model_id:
                return item
        raise ValueError(f"Model không thuộc provider {self.display_name}: {model_id}")

    def validate(self, request: GenerationRequest) -> ModelCapability:
        if request.provider_id != self.provider_id:
            raise ValueError(
                f"Request dành cho provider {request.provider_id}, không phải {self.provider_id}."
            )
        capability = self.capability(request.model_id)
        if not request.prompt.strip():
            raise ValueError("Prompt không được để trống.")
        image_count = len(request.source_images)
        if image_count and not capability.supports_image:
            raise ValueError(f"{capability.display_name} không hỗ trợ ảnh đầu vào.")
        if not image_count and not capability.supports_text:
            raise ValueError(f"{capability.display_name} yêu cầu ảnh đầu vào.")
        if image_count < capability.min_images:
            raise ValueError(
                f"{capability.display_name} yêu cầu tối thiểu {capability.min_images} ảnh."
            )
        if image_count > capability.max_images:
            raise ValueError(
                f"{capability.display_name} chỉ nhận tối đa {capability.max_images} ảnh."
            )
        for image in request.source_images:
            if not Path(image).is_file():
                raise ValueError(f"Không tìm thấy ảnh đầu vào: {image}")
        if not capability.supports_duration(int(request.duration)):
            raise ValueError(
                f"{capability.display_name} không hỗ trợ thời lượng {request.duration} giây."
            )
        if request.aspect_ratio not in capability.aspect_ratios:
            raise ValueError(
                f"{capability.display_name} không hỗ trợ tỉ lệ {request.aspect_ratio}."
            )
        if request.resolution not in capability.resolutions:
            raise ValueError(
                f"{capability.display_name} không hỗ trợ độ phân giải {request.resolution}."
            )
        if request.generate_audio and not capability.supports_audio:
            raise ValueError(f"{capability.display_name} không hỗ trợ tạo âm thanh đồng bộ.")
        if not request.output_path.strip():
            raise ValueError("Chưa có đường dẫn video đầu ra.")
        return capability

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        raise NotImplementedError

    @abstractmethod
    def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> str:
        raise NotImplementedError


def classify_provider_exception(exc: Exception, provider_name: str) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    message = str(exc)
    lowered = message.casefold()
    if any(token in lowered for token in ("cancel", "hủy", "huỷ")):
        return ProviderCancelled(message)
    if any(token in lowered for token in ("401", "403", "api key", "authentication", "unauthorized")):
        return ProviderError(
            message, code="authentication", auth_error=True,
        )
    if any(token in lowered for token in ("429", "quota", "rate limit", "credit", "insufficient balance")):
        return ProviderError(
            message, code="quota", quota_error=True,
        )
    if any(token in lowered for token in ("policy", "safety", "moderation", "blocked", "nội dung bị chặn")):
        return ProviderError(
            message, code="policy_blocked", policy_blocked=True,
        )
    retryable = any(token in lowered for token in ("timeout", "timed out", "5xx", " 500", " 502", " 503", "network"))
    return ProviderError(
        f"{provider_name}: {message}", code="temporary" if retryable else "provider_error", retryable=retryable,
    )
