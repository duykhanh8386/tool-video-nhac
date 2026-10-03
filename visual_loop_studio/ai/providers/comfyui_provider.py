from __future__ import annotations

from ai.comfyui import (
    LOCAL_MODEL_HUNYUAN,
    LOCAL_MODEL_LTX,
    WAN_VARIANT_DMD,
    WAN_VARIANT_QUALITY,
    generate_local_image_to_video,
)
from ai.local_runtime import server_ready
from ai.providers.base import (
    GenerationRequest,
    ModelCapability,
    ProviderExecutionContext,
    ProviderHealth,
    VideoProvider,
    classify_provider_exception,
)


class ComfyUiProvider(VideoProvider):
    provider_id = "comfyui"
    display_name = "AI Local — ComfyUI"
    supports_resume = False

    def __init__(self, comfyui_url: str, workflow_path: str = "") -> None:
        self.comfyui_url = comfyui_url
        self.workflow_path = workflow_path

    def capabilities(self) -> tuple[ModelCapability, ...]:
        common = {
            "provider_id": self.provider_id,
            "supports_text": False,
            "min_images": 1,
            "max_images": 1,
            "durations": (4, 6, 8),
            "aspect_ratios": ("16:9", "9:16"),
            "supports_audio": False,
            "max_concurrency": 1,
        }
        return (
            ModelCapability(
                model_id=WAN_VARIANT_QUALITY,
                display_name="Wan 2.2 5B Chất lượng",
                resolutions=("832x480", "1280x704"),
                **common,
            ),
            ModelCapability(
                model_id=WAN_VARIANT_DMD,
                display_name="Wan 2.2 5B DMD 4 bước",
                resolutions=("832x480", "1280x704"),
                **common,
            ),
            ModelCapability(
                model_id=LOCAL_MODEL_LTX,
                display_name="LTX-Video 2B Distilled",
                resolutions=("832x480", "1280x704"),
                **common,
            ),
            ModelCapability(
                model_id=LOCAL_MODEL_HUNYUAN,
                display_name="HunyuanVideo 1.5 480p",
                resolutions=("832x480",),
                **common,
            ),
        )

    def health_check(self) -> ProviderHealth:
        if server_ready(self.comfyui_url):
            return ProviderHealth(True, f"ComfyUI sẵn sàng tại {self.comfyui_url}.")
        return ProviderHealth(False, "ComfyUI chưa chạy. Có thể cài/khởi động từ màn hình Tạo Visual.")

    def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> str:
        self.validate(request)
        try:
            width_text, height_text = request.resolution.lower().split("x", 1)
            width, height = int(width_text), int(height_text)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Độ phân giải local không hợp lệ: {request.resolution}") from exc
        if request.aspect_ratio == "9:16" and width > height:
            width, height = height, width
        # 24 fps plus the first frame. This satisfies both 4n+1 (Wan/Hunyuan)
        # and 8n+1 (LTX) frame constraints.
        frame_count = int(request.duration) * 24 + 1
        try:
            return generate_local_image_to_video(
                comfyui_url=self.comfyui_url,
                workflow_path=self.workflow_path,
                image_path=request.source_images[0],
                prompt=request.prompt,
                output_path=request.output_path,
                width=width,
                height=height,
                length=frame_count,
                seed=None if request.seed < 0 else request.seed,
                negative_prompt=str(request.options.get("negative_prompt") or ""),
                wan_variant=request.model_id,
                progress=context.progress,
                cancelled=context.cancelled,
            )
        except Exception as exc:
            context.raise_if_cancelled()
            raise classify_provider_exception(exc, self.display_name) from exc
