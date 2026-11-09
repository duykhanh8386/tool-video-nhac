from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtGui import QColor, QImage

from ai.comfyui import (
    WAN_VARIANT_DMD,
    WAN_VARIANT_QUALITY,
    check_comfyui,
    free_comfyui_memory,
    generate_local_image_to_video,
)
from ai.local_runtime import server_ready
from auth.local_generation import conform_video_duration, create_neutral_canvas_image
from playlist_ai.providers.base import IProvider, ProviderError
from utils.paths import CACHE_DIR


def snap_dim_to_32(dim: int, default: int = 704) -> int:
    """
    Chuẩn hóa kích thước khung hình:
    Wan 2.2 Local / ComfyUI bắt buộc chiều rộng và chiều cao phải >= 256 px và chia hết cho 32.
    Ví dụ: 1080 -> 1088, 1920 -> 1920, 720 -> 704.
    """
    try:
        val = int(dim)
    except (ValueError, TypeError):
        val = default
    return max(256, round(val / 32) * 32)


class WanProvider(IProvider):
    """Provider AI Local chạy qua ComfyUI localhost (Wan 2.2 TI2V / DMD)."""

    def __init__(
        self,
        comfyui_url: str = "http://127.0.0.1:8188",
        model_variant: str = WAN_VARIANT_DMD,
        ffmpeg_path: str = "ffmpeg",
    ):
        self.comfyui_url = comfyui_url.strip() or "http://127.0.0.1:8188"
        self.model_variant = model_variant or WAN_VARIANT_DMD
        self.ffmpeg_path = ffmpeg_path or "ffmpeg"

    @property
    def key(self) -> str:
        return "wan"

    @property
    def display_name(self) -> str:
        variant_title = "DMD 4 bước" if self.model_variant == WAN_VARIANT_DMD else "Chất lượng 20 bước"
        return f"Wan 2.2 Local ({variant_title})"

    def check_health(self) -> tuple[bool, str]:
        """Kiểm tra xem ComfyUI có đang chạy và sẵn sàng nhận job không."""
        try:
            if not server_ready(self.comfyui_url):
                return False, f"Không thể kết nối ComfyUI tại {self.comfyui_url}. Hãy khởi động ComfyUI trước."
            stats = check_comfyui(self.comfyui_url, require_default_models=False, wan_variant=self.model_variant)
            devices = stats.get("devices") or []
            dev_str = "GPU"
            if devices and isinstance(devices, list):
                d = devices[0]
                dev_str = str(d.get("name") or d.get("type") or "GPU")
            return True, f"ComfyUI sẵn sàng ({dev_str})"
        except Exception as exc:
            return False, f"Lỗi ComfyUI: {exc}"

    def text_to_image(
        self,
        prompt: str,
        width: int = 1280,
        height: int = 704,
        seed: Optional[int] = None,
        cache_path: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """
        Bước 1: Chuẩn bị ảnh nền nguồn.
        Với Wan Image-to-Video local, sinh canvas trung tính chuẩn (18% photographic gray)
        hoặc lưu ảnh cache để bước 2 tiếp tục tạo chuyển động.
        """
        if progress:
            progress(15, "Đang khởi tạo khung hình nguồn cho video...")

        if cache_path is None:
            cache_dir = Path(tempfile.mkdtemp(prefix="wan_t2i_", dir=CACHE_DIR))
            cache_path = cache_dir / "initial_frame.png"
        else:
            cache_path = Path(cache_path).resolve()
            cache_path.parent.mkdir(parents=True, exist_ok=True)

        render_w = snap_dim_to_32(width, 1280)
        render_h = snap_dim_to_32(height, 704)

        if not cache_path.is_file():
            create_neutral_canvas_image(render_w, render_h, cache_path)

        if progress:
            progress(100, "Đã chuẩn bị xong khung hình gốc.")

        return cache_path

    def image_to_video(
        self,
        image_path: Path,
        prompt: str,
        duration_seconds: int = 5,
        width: int = 1280,
        height: int = 704,
        output_path: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Path:
        """
        Bước 2: Gọi workflow ComfyUI Wan 2.2 để tạo video chuyển động.
        """
        if not Path(image_path).is_file():
            raise ProviderError(f"Không tìm thấy ảnh nguồn: {image_path}")

        render_w = snap_dim_to_32(width, 1280)
        render_h = snap_dim_to_32(height, 704)

        # Tính toán frame count: 4n + 1 (49 cho ~2s, 81 cho ~3.4s, 121 cho ~5s)
        if duration_seconds <= 2:
            length = 49
        elif duration_seconds <= 4:
            length = 81
        else:
            length = 121

        steps = 4 if self.model_variant == WAN_VARIANT_DMD else 20
        cfg = 1.0 if self.model_variant == WAN_VARIANT_DMD else 5.0

        temp_raw = Path(tempfile.mktemp(prefix="wan_raw_", suffix=".mp4", dir=CACHE_DIR))

        def on_comfy_progress(pct: int, msg: str):
            if progress:
                progress(pct, msg)

        try:
            generated = generate_local_image_to_video(
                comfyui_url=self.comfyui_url,
                image_path=str(image_path),
                prompt=prompt,
                output_path=str(temp_raw),
                width=render_w,
                height=render_h,
                length=length,
                steps=steps,
                cfg=cfg,
                seed=None,
                wan_variant=self.model_variant,
                progress=on_comfy_progress,
                cancelled=cancelled,
                cleanup_history=True,
            )

            final_dest = Path(output_path) if output_path else temp_raw.with_name(f"wan_final_{int(time.time())}.mp4")
            final_dest.parent.mkdir(parents=True, exist_ok=True)

            # Cắt chuẩn xác thời lượng và khử frame đen đầu (0.16s)
            conform_video_duration(
                Path(generated),
                final_dest,
                target_seconds=float(duration_seconds),
                ffmpeg_path=self.ffmpeg_path,
                trim_start_seconds=0.16,
            )
            free_comfyui_memory(self.comfyui_url, unload_models=False, free_memory=True)

            return final_dest
        except Exception as exc:
            free_comfyui_memory(self.comfyui_url, unload_models=True, free_memory=True)
            raise ProviderError(f"Wan Local Render Error: {exc}") from exc
        finally:
            if temp_raw.exists():
                try:
                    temp_raw.unlink(missing_ok=True)
                except OSError:
                    pass
