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
    LocalGenerationCancelled,
    clear_comfyui_history,
    free_comfyui_memory,
    generate_local_image_to_video,
    get_comfyui_queue,
    local_model_label,
    normalize_wan_variant,
)
from ai.local_runtime import (
    LocalRuntimeManager,
    default_runtime_root,
    is_runtime_installed,
    server_ready,
)
from utils.paths import CACHE_DIR, safe_filename
from utils.process import hidden_process_kwargs


def create_neutral_canvas_image(width: int, height: int, output_path: Path) -> Path:
    """Tạo ảnh canvas trung tính để làm nền đầu vào cho Text-to-Video nếu người dùng không chọn ảnh."""
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(int(width), int(height), QImage.Format.Format_RGB888)
    # Tô màu xám trung tính (128, 128, 128 - 18% photographic gray) để latent noise hội tụ tự nhiên, không bị hắc đen
    img.fill(QColor(128, 128, 128))
    img.save(str(output_path), "PNG")
    return output_path


def conform_video_duration(
    input_path: Path,
    output_path: Path,
    target_seconds: float = 5.0,
    ffmpeg_path: str = "ffmpeg",
    trim_start_seconds: float = 0.0,
) -> Path:
    """Cắt chuẩn xác thời lượng video về target_seconds bằng FFmpeg, tự động loại bỏ frame canvas đen đầu nếu có."""
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.is_file():
        raise FileNotFoundError(f"Không tìm thấy video nguồn: {input_path}")

    start_offset = max(0.0, float(trim_start_seconds))

    # Nếu có yêu cầu cắt bỏ frame đầu (Text-to-Video canvas), re-encode nhanh để cắt chuẩn xác từng frame
    if start_offset > 0.0:
        cmd_trim = [
            ffmpeg_path,
            "-hide_banner",
            "-y",
            "-ss", f"{start_offset:.3f}",
            "-i", str(input_path),
            "-t", f"{target_seconds:.3f}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "17",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]
        res = subprocess.run(
            cmd_trim,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **hidden_process_kwargs(),
        )
        if res.returncode == 0 and output_path.exists():
            return output_path

    # Trường hợp thông thường (chọn ảnh nguồn thật), ưu tiên stream copy nhanh
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
            "-ss", f"{start_offset:.3f}",
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
        runtime_root: str = "",
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
        self.runtime_root = str(runtime_root or "").strip()

        self._stopped = False
        self._paused = False

    def stop(self) -> None:
        self._stopped = True

    def toggle_pause(self) -> bool:
        self._paused = not self._paused
        return self._paused

    def _emit_log(self, text: str) -> None:
        stamped = f"[{time.strftime('%H:%M:%S')}] {text}" if not re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s", text) else text
        self.log_signal.emit(stamped)

    def _effective_runtime_root(self) -> str:
        if self.runtime_root:
            return self.runtime_root
        default_root = default_runtime_root()
        if is_runtime_installed(default_root, wan_variant=self.model_variant):
            return str(default_root)
        return ""

    def _recover_comfyui(self, task_label: str, effective_root: str) -> None:
        """Clear wedged queue/memory and restart managed ComfyUI if GPU worker stalled."""
        self.worker_status_signal.emit(f"[{task_label}] Đang giải phóng VRAM & phục hồi ComfyUI...")
        clear_comfyui_history(self.comfyui_url, clear_all=True)
        free_comfyui_memory(self.comfyui_url, unload_models=True, free_memory=True)

        queue_data = get_comfyui_queue(self.comfyui_url, timeout=8)
        stuck_running = bool(
            isinstance(queue_data, dict)
            and isinstance(queue_data.get("queue_running"), list)
            and len(queue_data.get("queue_running") or []) > 0
        )
        alive = server_ready(self.comfyui_url, timeout=2.0)

        if (not alive or stuck_running) and effective_root:
            try:
                restarted = self.runtime_manager.restart(
                    effective_root,
                    self.comfyui_url,
                    progress=lambda v, m: self._emit_log(f"⚙ [{v}%] {m}"),
                    cancelled=lambda: self._stopped,
                    wan_variant=self.model_variant,
                )
                if restarted:
                    self._emit_log(f"🔄 [{task_label}] Đã khởi động lại ComfyUI thành công để giải phóng GPU bị treo.")
            except Exception as exc:
                self._emit_log(f"⚠ [{task_label}] Không thể khởi động lại ComfyUI tự động: {exc}")
        elif not alive:
            self._emit_log(f"⚠ [{task_label}] ComfyUI không phản hồi; đang chờ phục hồi trước khi thử lại...")

        if not self._stopped:
            time.sleep(2.0)

    def run(self) -> None:
        total_items = len(self.prompts)
        if not self.prompts:
            self._emit_log("❌ Danh sách prompt trống!")
            self.batch_finished_signal.emit(False, "Danh sách prompt trống.")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        model_label_str = local_model_label(self.model_variant)
        effective_root = self._effective_runtime_root()

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

        self._emit_log(
            f"🚀 Bắt đầu Farm Video AI Local: {total_items} prompt | "
            f"Model: {model_label_str} | Kích thước: {width}×{height} ({self.aspect_ratio}) | "
            f"Thời lượng: {self.duration_seconds}s ({length} frames) | Số bước: {steps} steps (CFG {cfg})"
        )
        if not (self.source_image_path and Path(self.source_image_path).is_file()):
            self._emit_log("✨ Đang bật tự động khử frame đen: Video sẽ bắt đầu ngay từ khung hình sắc nét.")

        completed_count = 0
        failed_count = 0
        self.stats_signal.emit(total_items, 0, 0, total_items)
        self.progress_signal.emit(0, f"Đang khởi động AI Local... (0/{total_items})")

        # 1. Kiểm tra kết nối ComfyUI
        self.worker_status_signal.emit("Kiểm tra ComfyUI...")
        if not server_ready(self.comfyui_url):
            self._emit_log(f"⚙ ComfyUI chưa phản hồi tại {self.comfyui_url}. Đang cố gắng khởi chạy tự động...")
            try:
                self.runtime_manager.ensure_running(
                    effective_root,
                    self.comfyui_url,
                    progress=lambda v, m: self._emit_log(f"⚙ [{v}%] {m}"),
                    cancelled=lambda: self._stopped,
                    wan_variant=self.model_variant,
                )
            except Exception as e:
                self._emit_log(f"❌ Không thể khởi chạy ComfyUI tự động: {e}")
                self.batch_finished_signal.emit(False, f"Lỗi ComfyUI: {e}")
                return

        # 2. Tạo thư mục tạm và chuẩn bị ảnh nền dùng chung cho prompt_only
        temp_work_dir = Path(tempfile.mkdtemp(prefix="local_farm_", dir=CACHE_DIR))
        shared_canvas_path: str | None = None
        batch_verified = False

        try:
            for idx, prompt_text in enumerate(self.prompts, start=1):
                if self._stopped:
                    self._emit_log("⏹ Người dùng đã yêu cầu dừng tác vụ.")
                    break

                while self._paused and not self._stopped:
                    self.worker_status_signal.emit("Tạm dừng")
                    time.sleep(1.0)

                if self._stopped:
                    self._emit_log("⏹ Người dùng đã yêu cầu dừng tác vụ.")
                    break

                task_label = f"#{idx}/{total_items}"
                self.worker_status_signal.emit(f"Đang tạo {task_label}...")
                self._emit_log(f"📝 [{task_label}] Bắt đầu render prompt: '{prompt_text[:70]}...'")

                start_task_time = time.monotonic()
                is_canvas_fallback = not (self.source_image_path and Path(self.source_image_path).is_file())
                raw_output = temp_work_dir / f"raw_output_{idx}.mp4"

                try:
                    if not is_canvas_fallback:
                        active_image = self.source_image_path
                    else:
                        if not shared_canvas_path or not Path(shared_canvas_path).is_file():
                            canvas_file = temp_work_dir / f"canvas_neutral_{width}x{height}.png"
                            shared_canvas_path = str(create_neutral_canvas_image(width, height, canvas_file))
                        active_image = shared_canvas_path

                    generated_file: str | None = None
                    last_error: Exception | None = None
                    max_attempts = 2

                    for attempt in range(1, max_attempts + 1):
                        if self._stopped:
                            raise LocalGenerationCancelled()

                        last_log_ts = 0.0
                        last_milestone = -1
                        last_stage_sig = ""

                        def on_render_progress(val: int, msg: str):
                            nonlocal last_log_ts, last_milestone, last_stage_sig
                            self.worker_status_signal.emit(f"[{task_label}] {val}%: {msg}")
                            now = time.monotonic()
                            stage_sig = re.sub(r"\d+(?:\.\d+)?\s*phút|\(\d+/\d+\)", "", msg).strip()
                            milestone = val // 25
                            important_msg = any(
                                kw in msg
                                for kw in (
                                    "Hoàn tất",
                                    "giải mã video",
                                    "đóng gói",
                                    "hàng đợi",
                                    "thử kết nối",
                                )
                            )
                            stage_changed = stage_sig != last_stage_sig
                            should_log = (
                                val == 100
                                or important_msg and stage_changed
                                or (milestone > last_milestone and val > 0)
                                or (now - last_log_ts >= 20.0)
                            )
                            if should_log:
                                last_log_ts = now
                                last_milestone = max(last_milestone, milestone)
                                last_stage_sig = stage_sig
                                prefix = f"⏳ [{task_label}]" if attempt == 1 else f"⏳ [{task_label} • Lần {attempt}]"
                                self._emit_log(f"{prefix} ({val}%): {msg}")

                        try:
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
                                verify_server=(not batch_verified or attempt > 1),
                                cleanup_history=True,
                            )
                            batch_verified = True
                            break
                        except LocalGenerationCancelled:
                            raise
                        except ValueError:
                            raise
                        except Exception as exc:
                            last_error = exc
                            if attempt < max_attempts and not self._stopped:
                                self._emit_log(
                                    f"⚠ [{task_label}] Lần {attempt} gặp sự cố: {exc}. "
                                    "Đang dọn VRAM ComfyUI và thử lại tự động..."
                                )
                                self._recover_comfyui(task_label, effective_root)
                                batch_verified = False
                                while self._paused and not self._stopped:
                                    self.worker_status_signal.emit("Tạm dừng")
                                    time.sleep(1.0)
                                continue
                            raise

                    if not generated_file:
                        raise last_error or RuntimeError("Không tạo được file video local.")

                    # Cắt chuẩn xác thời lượng và loại bỏ frame đen đầu nếu là canvas trống
                    stem_name = safe_filename(f"local_{idx:03d}_{prompt_text[:35]}", default_prefix="local")
                    final_output = self.output_dir / f"{Path(stem_name).stem}.mp4"

                    self.worker_status_signal.emit(f"[{task_label}] Cắt chuẩn {self.duration_seconds}s...")
                    trim_offset = 0.16 if is_canvas_fallback else 0.0
                    conform_video_duration(
                        Path(generated_file),
                        final_output,
                        target_seconds=float(self.duration_seconds),
                        ffmpeg_path=self.ffmpeg_path,
                        trim_start_seconds=trim_offset,
                    )

                    render_duration = int(time.monotonic() - start_task_time)
                    completed_count += 1
                    remaining = total_items - (completed_count + failed_count)
                    pct = int((completed_count + failed_count) / total_items * 100)

                    self.stats_signal.emit(total_items, completed_count, failed_count, remaining)
                    self.progress_signal.emit(pct, f"Hoàn thành {completed_count}/{total_items} video")
                    self.video_completed_signal.emit(prompt_text, str(final_output))

                    self._emit_log(
                        f"✔ [{task_label}] Thành công ({render_duration}s)! Đã lưu: {final_output.name}"
                    )

                    # Bảo trì VRAM ComfyUI giữa các clip để chạy dài 600+ video không bị tràn sang RAM/Pagefile
                    if render_duration >= 240 or (completed_count % 30 == 0):
                        self._emit_log(
                            "🧹 Dọn bộ nhớ đệm & sắp xếp lại VRAM ComfyUI để tránh treo GPU khi farm dài..."
                        )
                        clear_comfyui_history(self.comfyui_url, clear_all=True)
                        free_comfyui_memory(self.comfyui_url, unload_models=True, free_memory=True)
                    else:
                        free_comfyui_memory(self.comfyui_url, unload_models=False, free_memory=True)

                except LocalGenerationCancelled:
                    self._emit_log(f"⏹ [{task_label}] Đã hủy tác vụ hiện tại theo yêu cầu.")
                    break
                except Exception as e:
                    failed_count += 1
                    remaining = total_items - (completed_count + failed_count)
                    pct = int((completed_count + failed_count) / total_items * 100)
                    self.stats_signal.emit(total_items, completed_count, failed_count, remaining)
                    self.progress_signal.emit(pct, f"Hoàn thành {completed_count}/{total_items} video (Lỗi: {failed_count})")
                    self._emit_log(f"❌ [{task_label}] Lỗi khi render video: {e}")
                    free_comfyui_memory(self.comfyui_url, unload_models=True, free_memory=True)
                    batch_verified = False
                finally:
                    raw_output.unlink(missing_ok=True)

                time.sleep(1.0)

        finally:
            shutil.rmtree(temp_work_dir, ignore_errors=True)

        if self._stopped:
            self.batch_finished_signal.emit(False, f"Đã dừng tác vụ. Hoàn thành {completed_count}/{total_items}.")
        else:
            self.batch_finished_signal.emit(
                True, f"Đã hoàn thành farm video AI Local! Thành công: {completed_count}/{total_items}."
            )
