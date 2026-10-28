from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor, QImage

from ai.comfyui import (
    LOCAL_MODEL_HUNYUAN,
    LOCAL_MODEL_LTX,
    WAN_VARIANT_DMD,
    WAN_VARIANT_QUALITY,
    check_comfyui,
    generate_local_image_to_video,
    local_model_label,
    normalize_wan_variant,
)
from ai.local_runtime import LocalRuntimeManager, server_ready
from utils.paths import CACHE_DIR, safe_filename
from utils.process import hidden_process_kwargs


def create_neutral_canvas_image(width: int, height: int, output_path: Path) -> Path:
    """Tạo ảnh canvas trung tính để làm nền đầu vào cho Text-to-Video nếu người dùng không chọn ảnh."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(int(width), int(height), QImage.Format.Format_RGB888)
    # Tô màu xám đậm trung tính (dark neutral) để latent noise hội tụ tự nhiên theo prompt
    img.fill(QColor(22, 22, 26))
    img.save(str(output_path), "PNG")
    return output_path


def conform_video_duration(
    input_path: Path,
    output_path: Path,
    target_seconds: float = 5.0,
    ffmpeg_path: str = "ffmpeg",
) -> Path:
    """Cắt chuẩn xác thời lượng video về target_seconds bằng FFmpeg."""
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy video nguồn: {input_path}")

    # Cắt chuẩn xác thời lượng video
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-y",
        "-ss", "0",
        "-i", str(input_path),
        "-t", f"{target_seconds:.3f}",
        "-c", "copy",
        str(output_path),
    ]

    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_process_kwargs(),
    )

    if res.returncode != 0 or not output_path.exists():
        # Fallback re-encode nếu stream copy gặp lỗi keyframe
        cmd_reencode = [
            ffmpeg_path,
            "-hide_banner",
            "-y",
            "-ss", "0",
            "-i", str(input_path),
            "-t", f"{target_seconds:.3f}",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]
        res_re = subprocess.run(
            cmd_reencode,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_process_kwargs(),
        )
        if res_re.returncode != 0 or not output_path.exists():
            shutil.copy2(input_path, output_path)

    return output_path


class LocalVideoBatchRunnerThread(QThread):
    """Luồng xử lý farm hàng loạt video AI Local chạy trực tiếp qua ComfyUI (Wan / LTX / Hunyuan).
    Không cần đăng nhập tài khoản hay cookie, tối ưu tốc độ nhanh nhất có thể.
    """

    log_signal = Signal(str)
    progress_signal = Signal(int, str)
    stats_signal = Signal(int, int, int, int)  # total, completed, failed, remaining
    worker_status_signal = Signal(str)
    video_completed_signal = Signal(str, str)  # prompt, output_path
    batch_finished_signal = Signal(bool, str)

    def __init__(
        self,
        prompts: list[str],
        output_dir: Path,
        *,
        comfyui_url: str = "http://127.0.0.1:8188",
        model_variant: str = WAN_VARIANT_DMD,
        resolution: str = "832x480",
        aspect_ratio: str = "16:9",
        duration_seconds: int = 5,
        source_image_path: str = "",
        negative_prompt: str = "",
        seed: int = -1,
        ffmpeg_path: str = "ffmpeg",
        runtime_manager: LocalRuntimeManager | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.prompts = [p.strip() for p in prompts if p.strip()]
        self.output_dir = Path(output_dir).resolve()
        self.comfyui_url = comfyui_url.strip().rstrip("/")
        self.model_variant = normalize_wan_variant(model_variant)
        self.resolution = resolution
        self.aspect_ratio = aspect_ratio
        self.duration_seconds = max(2, min(10, duration_seconds))
        self.source_image_path = source_image_path.strip()
        self.negative_prompt = negative_prompt.strip()
        self.seed = seed
        self.ffmpeg_path = ffmpeg_path
        self.runtime_manager = runtime_manager or LocalRuntimeManager()

        self._stopped = False
        self._paused = False

    def stop(self) -> None:
        self._stopped = True

    def toggle_pause(self) -> bool:
        self._paused = not self._paused
        return self._paused

    def run(self) -> None:
        total_items = len(self.prompts)
        if not self.prompts:
            self.log_signal.emit("❌ Danh sách prompt trống!")
            self.batch_finished_signal.emit(False, "Danh sách prompt trống.")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        model_label_str = local_model_label(self.model_variant)

        # Tính toán độ phân giải chuẩn chia hết cho 32
        res_parts = self.resolution.lower().split("x", 1)
        try:
            base_w = int(res_parts[0])
            base_h = int(res_parts[1])
        except (ValueError, IndexError):
            base_w, base_h = 832, 480

        if self.aspect_ratio == "9:16":
            width, height = min(base_w, base_h), max(base_w, base_h)
        else:
            width, height = max(base_w, base_h), min(base_w, base_h)

        # Số frame: 121 frame cho 5 giây @ 24fps (thỏa mãn cả 4n+1 của Wan/Hunyuan và 8n+1 của LTX)
        if self.duration_seconds <= 2:
            length = 49  # ~2s
        elif self.duration_seconds <= 4:
            length = 81  # ~3.4s
        else:
            length = 121  # ~5s

        # Tối ưu hóa số bước và CFG theo từng model
        if self.model_variant == WAN_VARIANT_DMD:
            # 4 bước, CFG 1.0 (Nhanh nhất thế giới cho Wan 2.2)
            steps = 4
            cfg = 1.0
        elif self.model_variant in {LOCAL_MODEL_LTX, LOCAL_MODEL_HUNYUAN}:
            steps = 8
            cfg = 1.0
        else:
            steps = 20
            cfg = 5.0

        self.log_signal.emit(
            f"🚀 Bắt đầu Farm Video AI Local: {total_items} prompt | "
            f"Model: {model_label_str} | Kích thước: {width}×{height} ({self.aspect_ratio}) | "
            f"Thời lượng: {self.duration_seconds}s ({length} frames) | Số bước: {steps} steps (CFG {cfg})"
        )

        completed_count = 0
        failed_count = 0
        self.stats_signal.emit(total_items, 0, 0, total_items)
        self.progress_signal.emit(0, f"Đang khởi động AI Local... (0/{total_items})")

        # 1. Kiểm tra kết nối ComfyUI
        self.worker_status_signal.emit("Kiểm tra ComfyUI...")
        if not server_ready(self.comfyui_url):
            self.log_signal.emit(f"⚙ ComfyUI chưa phản hồi tại {self.comfyui_url}. Đang cố gắng khởi chạy tự động...")
            try:
                self.runtime_manager.ensure_running(
                    "",
                    self.comfyui_url,
                    progress=lambda v, m: self.log_signal.emit(f"⚙ [{v}%] {m}"),
                    cancelled=lambda: self._stopped,
                    wan_variant=self.model_variant,
                )
            except Exception as e:
                self.log_signal.emit(f"❌ Không thể khởi chạy ComfyUI tự động: {e}")
                self.batch_finished_signal.emit(False, f"Lỗi ComfyUI: {e}")
                return

        # 2. Tạo thư mục tạm
        temp_work_dir = Path(tempfile.mkdtemp(prefix="local_farm_", dir=CACHE_DIR))

        try:
            for idx, prompt_text in enumerate(self.prompts, start=1):
                if self._stopped:
                    self.log_signal.emit("⏹ Người dùng đã yêu cầu dừng tác vụ.")
                    break

                while self._paused and not self._stopped:
                    self.worker_status_signal.emit("Tạm dừng")
                    time.sleep(1.0)

                task_label = f"#{idx}/{total_items}"
                self.worker_status_signal.emit(f"Đang tạo {task_label}...")
                self.log_signal.emit(f"📝 [{task_label}] Bắt đầu render prompt: '{prompt_text[:70]}...'")

                start_task_time = time.monotonic()

                try:
                    # Chuẩn bị ảnh đầu vào
                    if self.source_image_path and Path(self.source_image_path).is_file():
                        active_image = self.source_image_path
                    else:
                        canvas_file = temp_work_dir / f"canvas_{idx}.png"
                        active_image = str(create_neutral_canvas_image(width, height, canvas_file))

                    raw_output = temp_work_dir / f"raw_output_{idx}.mp4"

                    # Tiến hành tạo video qua ComfyUI
                    def on_render_progress(val: int, msg: str):
                        self.worker_status_signal.emit(f"[{task_label}] {val}%: {msg}")
                        if val % 25 == 0 or "đang chạy trên GPU" in msg:
                            self.log_signal.emit(f"⏳ [{task_label}] ({val}%): {msg}")

                    generated_file = generate_local_image_to_video(
                        comfyui_url=self.comfyui_url,
                        image_path=active_image,
                        prompt=prompt_text,
                        output_path=str(raw_output),
                        width=width,
                        height=height,
                        length=length,
                        steps=steps,
                        cfg=cfg,
                        seed=None if self.seed < 0 else self.seed,
                        negative_prompt=self.negative_prompt,
                        wan_variant=self.model_variant,
                        progress=on_render_progress,
                        cancelled=lambda: self._stopped,
                    )

                    # Cắt chuẩn xác 5 giây nếu cần
                    stem_name = safe_filename(f"local_{idx:03d}_{prompt_text[:35]}", default_prefix="local")
                    final_output = self.output_dir / f"{Path(stem_name).stem}.mp4"

                    self.worker_status_signal.emit(f"[{task_label}] Cắt chuẩn {self.duration_seconds}s...")
                    conform_video_duration(
                        Path(generated_file),
                        final_output,
                        target_seconds=float(self.duration_seconds),
                        ffmpeg_path=self.ffmpeg_path,
                    )

                    render_duration = int(time.monotonic() - start_task_time)
                    completed_count += 1
                    remaining = total_items - (completed_count + failed_count)
                    pct = int((completed_count + failed_count) / total_items * 100)

                    self.stats_signal.emit(total_items, completed_count, failed_count, remaining)
                    self.progress_signal.emit(pct, f"Hoàn thành {completed_count}/{total_items} video")
                    self.video_completed_signal.emit(prompt_text, str(final_output))

                    self.log_signal.emit(
                        f"✔ [{task_label}] Thành công ({render_duration}s)! Đã lưu: {final_output.name}"
                    )

                except Exception as e:
                    failed_count += 1
                    remaining = total_items - (completed_count + failed_count)
                    self.stats_signal.emit(total_items, completed_count, failed_count, remaining)
                    self.log_signal.emit(f"❌ [{task_label}] Lỗi khi render video: {e}")

                time.sleep(1.0)

        finally:
            shutil.rmtree(temp_work_dir, ignore_errors=True)

        if self._stopped:
            self.batch_finished_signal.emit(False, f"Đã dừng tác vụ. Hoàn thành {completed_count}/{total_items}.")
        else:
            self.batch_finished_signal.emit(
                True, f"Đã hoàn thành farm video AI Local! Thành công: {completed_count}/{total_items}."
            )
