from __future__ import annotations

from auth.muse_generation import (
    MuseGenerationAuthError,
    MuseGenerationCancelled,
    MuseGenerationError,
    MuseGenerationQuotaError,
    MuseGenerationService,
    MuseGenerationTimeout,
)
from auth.muse_login import MuseAccountStore
from ai.providers.base import (
    GenerationRequest,
    ModelCapability,
    ProviderCancelled,
    ProviderError,
    ProviderExecutionContext,
    ProviderHealth,
    VideoProvider,
)


class MuseWebProvider(VideoProvider):
    provider_id = "muse_web"
    display_name = "Muse AI Web — tài khoản đã chọn"
    supports_resume = False

    _CAPABILITIES = (
        ModelCapability(
            provider_id=provider_id,
            model_id="muse-web-video",
            display_name="Muse Web Video",
            supports_text=True,
            supports_image=True,
            min_images=0,
            max_images=1,
            durations=(5, 10, 15),
            aspect_ratios=("16:9", "9:16", "1:1"),
            resolutions=("720p",),
            supports_audio=False,
            max_concurrency=1,
        ),
    )

    def __init__(self, start_url: str, store: MuseAccountStore | None = None) -> None:
        self.start_url = start_url
        self.store = store or MuseAccountStore()

    def capabilities(self) -> tuple[ModelCapability, ...]:
        return self._CAPABILITIES

    def health_check(self) -> ProviderHealth:
        accounts = [item for item in self.store.all() if item.status == "connected"]
        if accounts:
            return ProviderHealth(True, f"Có {len(accounts)} tài khoản Muse đã đăng nhập; mỗi batch chỉ dùng tài khoản được chọn.")
        return ProviderHealth(False, "Chưa có tài khoản Muse đã đăng nhập trong trang Đăng nhập Muse AI.")

    def validate(self, request: GenerationRequest) -> ModelCapability:
        capability = super().validate(request)
        account_id = str(request.options.get("muse_account_id") or "")
        account = self.store.get(account_id)
        if not account or account.status != "connected":
            raise ValueError("Hãy chọn một tài khoản Muse đang ở trạng thái Đã đăng nhập.")
        return capability

    def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> str:
        self.validate(request)
        account_id = str(request.options["muse_account_id"])
        prompt = self._effective_prompt(request)
        source = request.source_images[0] if request.source_images else ""
        service = MuseGenerationService(start_url=self.start_url, store=self.store)
        try:
            output = service.generate(
                account_id,
                prompt,
                source,
                request.output_path,
                timeout_seconds=float(request.options.get("muse_timeout_seconds") or 15 * 60),
                cancelled=context.cancelled,
                progress=context.progress,
            )
            return str(output)
        except MuseGenerationCancelled as exc:
            raise ProviderCancelled(str(exc)) from exc
        except MuseGenerationQuotaError as exc:
            raise ProviderError(str(exc), code="quota", quota_error=True) from exc
        except MuseGenerationAuthError as exc:
            raise ProviderError(str(exc), code="authentication", auth_error=True) from exc
        except MuseGenerationTimeout as exc:
            # A web timeout is ambiguous: Muse may already have consumed credit and
            # finished the generation after the browser stopped waiting. Replaying
            # the request automatically could create duplicate, billable videos.
            raise ProviderError(str(exc), code="timeout", retryable=False) from exc
        except MuseGenerationError as exc:
            raise ProviderError(str(exc), code="muse_web_ui_changed", retryable=False) from exc

    @staticmethod
    def _effective_prompt(request: GenerationRequest) -> str:
        audio = "Do not add audio." if not request.generate_audio else ""
        return (
            f"{request.prompt.strip()}\n\n"
            f"Create a {request.duration}-second video in {request.aspect_ratio} aspect ratio at {request.resolution}. "
            f"{audio} Return the finished video in this chat."
        ).strip()
