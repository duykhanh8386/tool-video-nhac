from __future__ import annotations

import base64
import json
import mimetypes
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ai.providers.base import (
    GenerationRequest,
    ModelCapability,
    ProviderCancelled,
    ProviderError,
    ProviderExecutionContext,
    ProviderHealth,
    VideoProvider,
)
from utils.secret_store import resolve_secret


DEFAULT_BASE_URL = "https://operator.las.ap-southeast-1.bytepluses.com/api/v1"


class BytePlusSeedanceProvider(VideoProvider):
    """Official BytePlus LAS adapter for Dreamina Seedance video models."""

    provider_id = "byteplus_seedance"
    display_name = "Seedance — BytePlus API chính thức"
    supports_resume = True

    _CAPABILITIES = (
        ModelCapability(
            provider_id=provider_id,
            model_id="dreamina-seedance-2-5-260628",
            display_name="Dreamina Seedance 2.5",
            min_images=0,
            max_images=30,
            duration_min=4,
            duration_max=30,
            aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"),
            resolutions=("480p", "720p"),
            supports_audio=True,
            max_concurrency=3,
        ),
        ModelCapability(
            provider_id=provider_id,
            model_id="dreamina-seedance-2-0-260128",
            display_name="Dreamina Seedance 2.0",
            min_images=0,
            max_images=9,
            duration_min=4,
            duration_max=15,
            aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"),
            resolutions=("480p", "720p", "1080p", "4k"),
            supports_audio=True,
            max_concurrency=3,
        ),
        ModelCapability(
            provider_id=provider_id,
            model_id="dreamina-seedance-2-0-fast-260128",
            display_name="Dreamina Seedance 2.0 Fast",
            min_images=0,
            max_images=9,
            duration_min=4,
            duration_max=15,
            aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"),
            resolutions=("480p", "720p"),
            supports_audio=True,
            max_concurrency=3,
        ),
        ModelCapability(
            provider_id=provider_id,
            model_id="dreamina-seedance-2-0-mini-260615",
            display_name="Dreamina Seedance 2.0 Mini",
            min_images=0,
            max_images=9,
            duration_min=4,
            duration_max=15,
            aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive"),
            resolutions=("480p", "720p"),
            supports_audio=True,
            max_concurrency=3,
        ),
    )

    def __init__(self, base_url: str = DEFAULT_BASE_URL) -> None:
        self.base_url = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")

    def _api_key(self) -> str:
        return resolve_secret("byteplus_las_api_key", "BYTEPLUS_LAS_API_KEY")

    def capabilities(self) -> tuple[ModelCapability, ...]:
        return self._CAPABILITIES

    def health_check(self) -> ProviderHealth:
        if not self._api_key():
            return ProviderHealth(
                False,
                "Chưa có BytePlus LAS API key trong Credential Manager hoặc BYTEPLUS_LAS_API_KEY.",
            )
        return ProviderHealth(True, "Đã cấu hình BytePlus LAS API key.")

    def generate(self, request: GenerationRequest, context: ProviderExecutionContext) -> str:
        self.validate(request)
        api_key = self._api_key()
        if not api_key:
            raise ProviderError(
                "Chưa cấu hình BytePlus LAS API key cho Seedance.",
                code="authentication",
                auth_error=True,
            )
        context.raise_if_cancelled()
        task_id = context.external_id
        if not task_id:
            context.progress(3, "Đang gửi tác vụ Seedance đến BytePlus LAS…")
            payload = self._build_payload(request)
            result = self._json_request("POST", f"{self.base_url}/contents/generations/tasks", api_key, payload)
            task_id = str(result.get("id") or "").strip()
            if not task_id:
                raise ProviderError(
                    f"BytePlus không trả về task ID: {result}", code="invalid_response",
                )
            context.set_external_id(task_id)
        else:
            context.progress(5, f"Đang tiếp tục theo dõi Seedance task {task_id}…")

        started = time.monotonic()
        while True:
            context.raise_if_cancelled()
            result = self._json_request(
                "GET", f"{self.base_url}/contents/generations/tasks/{task_id}", api_key,
            )
            status = str(result.get("status") or "").strip().casefold()
            if status == "succeeded":
                video_url = str((result.get("content") or {}).get("video_url") or "").strip()
                if not video_url:
                    raise ProviderError(
                        f"Seedance hoàn tất nhưng thiếu video_url: {result}", code="invalid_response",
                    )
                context.progress(92, "Seedance đã hoàn tất; đang tải MP4 về máy…")
                self._download(video_url, request.output_path, context)
                context.progress(100, "Đã tải video Seedance từ BytePlus.")
                return str(Path(request.output_path).resolve())
            if status in {"failed", "expired", "cancelled"}:
                error = result.get("error") or {}
                message = str(error.get("message") if isinstance(error, dict) else error).strip()
                lowered = message.casefold()
                raise ProviderError(
                    message or f"Seedance task {status}.",
                    code="policy_blocked" if any(word in lowered for word in ("policy", "safety", "moderation")) else status,
                    policy_blocked=any(word in lowered for word in ("policy", "safety", "moderation")),
                )
            elapsed = time.monotonic() - started
            if elapsed > 48 * 60 * 60:
                raise ProviderError("Seedance vượt quá thời gian chờ 48 giờ.", code="timeout", retryable=True)
            progress = min(88, 8 + int(elapsed / 60 * 2))
            label = "đang xếp hàng" if status == "queued" else "đang dựng"
            context.progress(progress, f"Seedance {label}… {int(elapsed // 60)} phút")
            for _ in range(5):
                if context.cancelled():
                    raise ProviderCancelled()
                time.sleep(1)

    def _build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt.strip()}]
        images = list(request.source_images)
        for index, image_path in enumerate(images):
            if len(images) == 1:
                role = "first_frame"
            elif index == 0 and request.options.get("first_last_frames"):
                role = "first_frame"
            elif index == len(images) - 1 and request.options.get("first_last_frames"):
                role = "last_frame"
            else:
                role = "reference_image"
            content.append({
                "type": "image_url",
                "image_url": {"url": self._image_data_url(Path(image_path))},
                "role": role,
            })
        payload: dict[str, Any] = {
            "model": request.model_id,
            "content": content,
            "duration": int(request.duration),
            "ratio": request.aspect_ratio,
            "resolution": request.resolution,
            "generate_audio": bool(request.generate_audio),
            "watermark": bool(request.options.get("watermark", False)),
            "return_last_frame": bool(request.options.get("return_last_frame", False)),
        }
        if request.seed >= 0:
            payload["seed"] = int(request.seed)
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 64 * 1024 * 1024:
            raise ValueError("Tổng request Seedance vượt giới hạn BytePlus 64 MB. Hãy giảm số lượng/kích thước ảnh.")
        return payload

    @staticmethod
    def _image_data_url(path: Path) -> str:
        if not path.is_file():
            raise ValueError(f"Không tìm thấy ảnh tham chiếu: {path}")
        if path.stat().st_size > 30 * 1024 * 1024:
            raise ValueError(f"Ảnh vượt quá giới hạn BytePlus 30 MB: {path.name}")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        if not mime_type.startswith("image/"):
            raise ValueError(f"File tham chiếu không phải ảnh: {path}")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type.lower()};base64,{encoded}"

    @staticmethod
    def _json_request(method: str, url: str, api_key: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "VisualLoopStudio/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                error = parsed.get("error") or parsed
                message = str(error.get("message") if isinstance(error, dict) else error)
                code = str(error.get("code") if isinstance(error, dict) else "")
            except (ValueError, TypeError):
                message, code = detail, ""
            lowered = f"{code} {message}".casefold()
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0
            raise ProviderError(
                f"BytePlus HTTP {exc.code}: {message or detail}",
                code=code or f"http_{exc.code}",
                retryable=exc.code >= 500 or exc.code == 429,
                auth_error=exc.code in {401, 403},
                quota_error=exc.code == 429 or any(word in lowered for word in ("quota", "rate", "balance")),
                policy_blocked=any(word in lowered for word in ("policy", "safety", "moderation")),
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"Không kết nối được BytePlus: {exc}", code="network", retryable=True,
            ) from exc

    @staticmethod
    def _download(url: str, output_path: str, context: ProviderExecutionContext) -> None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": "VisualLoopStudio/1"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response, partial.open("wb") as stream:
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                while True:
                    context.raise_if_cancelled()
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    downloaded += len(block)
                    if total:
                        context.progress(92 + min(7, round(downloaded / total * 7)), "Đang tải video Seedance…")
            if not partial.is_file() or partial.stat().st_size == 0:
                raise ProviderError("BytePlus trả về file video rỗng.", code="empty_output", retryable=True)
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
