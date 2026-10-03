from __future__ import annotations

from ai.providers.base import (
    GenerationRequest,
    ModelCapability,
    ProviderExecutionContext,
    ProviderHealth,
    VideoProvider,
    classify_provider_exception,
)
from ai.veo import GenerationCancelled, generate_image_to_video
from utils.secret_store import resolve_secret


class VeoProvider(VideoProvider):
    provider_id = "veo"
    display_name = "Veo — Gemini API"
    supports_resume = True

    _CAPABILITIES = (
        ModelCapability(
            provider_id=provider_id,
            model_id="veo-3.1-fast-generate-preview",
            display_name="Veo 3.1 Fast",
            supports_text=False,
            min_images=1,
            max_images=1,
            durations=(4, 6, 8),
            resolutions=("720p", "1080p"),
            max_concurrency=2,
        ),
        ModelCapability(
            provider_id=provider_id,
            model_id="veo-3.1-generate-preview",
            display_name="Veo 3.1",
            supports_text=False,
            min_images=1,
            max_images=1,
            durations=(4, 6, 8),
            resolutions=("720p", "1080p"),
            max_concurrency=2,
        ),
        ModelCapability(
            provider_id=provider_id,
            model_id="veo-3.1-lite-generate-preview",
            display_name="Veo 3.1 Lite",
            supports_text=False,
            min_images=1,
            max_images=1,
            durations=(4, 6, 8),
            resolutions=("720p", "1080p"),
            max_concurrency=2,
        ),
    )

    def __init__(self, legacy_api_key: str = "") -> None:
        self.legacy_api_key = legacy_api_key

    def _api_key(self) -> str:
        return resolve_secret("gemini_api_key", "GEMINI_API_KEY", self.legacy_api_key)

    def capabilities(self) -> tuple[ModelCapability, ...]:
        return self._CAPABILITIES

    def validate(self, request: GenerationRequest) -> ModelCapability:
        capability = super().validate(request)
        if request.resolution in {"1080p", "4k"} and request.duration != 8:
            raise ValueError("Veo yêu cầu thời lượng 8 giây khi chọn 1080p hoặc 4K.")
        return capability

    def health_check(self) -> ProviderHealth:
        if self._api_key():
            return ProviderHealth(True, "Đã cấu hình Gemini API key.")
        return ProviderHealth(False, "Chưa có Gemini API key trong Credential Manager hoặc GEMINI_API_KEY.")

    def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> str:
        self.validate(request)
        key = self._api_key()
        if not key:
            raise ValueError("Chưa cấu hình Gemini API key cho Veo.")
        try:
            output = generate_image_to_video(
                api_key=key,
                image_path=request.source_images[0],
                prompt=request.prompt,
                output_path=request.output_path,
                model=request.model_id,
                aspect_ratio=request.aspect_ratio,
                duration=request.duration,
                resolution=request.resolution,
                progress=context.progress,
                cancelled=context.cancelled,
                operation_name=context.external_id,
                submitted=context.set_external_id,
            )
            return str(output)
        except GenerationCancelled as exc:
            context.raise_if_cancelled()
            raise classify_provider_exception(exc, self.display_name) from exc
        except Exception as exc:
            raise classify_provider_exception(exc, self.display_name) from exc
