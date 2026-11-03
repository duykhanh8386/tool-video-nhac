from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from playlist_ai.core.file_namer import ConflictResolution, resolve_output_file_path
from playlist_ai.core.prompt_parser import MappedItem
from playlist_ai.providers.base import CookieExpiredError, IProvider, ProviderError
from utils.config import read_json, write_json
from utils.paths import CACHE_DIR, DATA_DIR


JOBS_STATE_FILE = DATA_DIR / "playlist_jobs_state.json"


@dataclass
class JobState:
    id: str
    index: int
    track_file: str
    track_title: str
    raw_prompt: str
    final_prompt: str
    provider_key: str  # "wan" | "muse"
    status: str = "Chờ"  # "Chờ", "Đang tạo ảnh", "Đang tạo chuyển động", "Hoàn thành", "Lỗi", "Bỏ qua", "Tạm dừng"
    error_message: str = ""
    cached_image_path: str = ""
    output_video_path: str = ""
    retry_count: int = 0
    duration_seconds: int = 5
    width: int = 1280
    height: int = 704
    keep_thumbnail: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> JobState:
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


class JobQueueManager:
    """Quản lý trạng thái, lưu trữ bền vững (persistence) và cache của hàng đợi Job."""

    def __init__(self, state_file: Path = JOBS_STATE_FILE):
        self.state_file = state_file
        self.jobs: List[JobState] = []
        self.load()

    def load(self) -> None:
        """Đọc danh sách job đã lưu từ phiên làm việc trước."""
        if not self.state_file.is_file():
            self.jobs = []
            return
        try:
            data = read_json(self.state_file, {"jobs": []}) or {}
            raw_list = data.get("jobs", [])
            self.jobs = [JobState.from_dict(item) for item in raw_list if isinstance(item, dict)]
        except Exception:
            self.jobs = []

    def save(self) -> None:
        """Lưu trạng thái hàng đợi ra file JSON để tiếp tục khi khởi động lại."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"jobs": [j.to_dict() for j in self.jobs], "updated_at": time.time()}
            write_json(self.state_file, payload)
        except Exception:
            pass

    def clear(self) -> None:
        self.jobs = []
        self.save()

    def set_jobs_from_mapped_items(
        self,
        mapped_items: List[MappedItem],
        provider_key: str = "wan",
        duration_seconds: int = 5,
        width: int = 1280,
        height: int = 704,
        keep_thumbnail: bool = True,
    ) -> None:
        """Khởi tạo danh sách jobs mới từ kết quả ghép prompt."""
        self.jobs = []
        for item in mapped_items:
            job_id = f"job_{item.index:03d}_{int(time.time() * 1000) % 100000}"
            self.jobs.append(
                JobState(
                    id=job_id,
                    index=item.index,
                    track_file=str(item.track.file_path),
                    track_title=item.track.title,
                    raw_prompt=item.raw_prompt,
                    final_prompt=item.final_prompt,
                    provider_key=provider_key,
                    duration_seconds=duration_seconds,
                    width=width,
                    height=height,
                    keep_thumbnail=keep_thumbnail,
                )
            )
        self.save()

    def get_job(self, job_id: str) -> Optional[JobState]:
        for j in self.jobs:
            if j.id == job_id:
                return j
        return None

    def update_pending_provider(self, new_provider_key: str) -> int:
        """Đổi AI giữa chừng cho tất cả các bài chưa chạy (đang ở trạng thái 'Chờ' hoặc 'Lỗi')."""
        updated = 0
        for j in self.jobs:
            if j.status in {"Chờ", "Lỗi", "Tạm dừng"}:
                j.provider_key = new_provider_key
                updated += 1
        self.save()
        return updated


class PlaylistBatchRunner(QThread):
    """Worker Thread chạy hàng đợi từng job, hỗ trợ Pause/Resume, Retry, và bắt Cookie Expired."""

    job_status_changed = Signal(dict)
    log_signal = Signal(str)
    progress_signal = Signal(int, str)
    cookie_expired_signal = Signal(str)  # Báo cho UI khi cookie hết hạn để người dùng dán cookie mới
    all_finished_signal = Signal(bool, str)

    def __init__(
        self,
        queue_manager: JobQueueManager,
        providers: Dict[str, IProvider],
        output_dir: str | Path,
        conflict_mode: ConflictResolution = "suffix",
    ):
        super().__init__()
        self.queue_manager = queue_manager
        self.providers = providers
        self.output_dir = Path(output_dir).resolve()
        self.conflict_mode = conflict_mode
        self._paused = False
        self._stopped = False
        self._single_job_retry_id: Optional[str] = None

    def pause(self) -> None:
        self._paused = True
        self.log_signal.emit("⏸ Đã yêu cầu tạm dừng tác vụ sau bài hiện tại.")

    def resume(self) -> None:
        self._paused = False
        self.log_signal.emit("▶ Tiếp tục chạy hàng đợi tác vụ.")

    def stop(self) -> None:
        self._stopped = True
        self._paused = False
        self.log_signal.emit("⏹ Người dùng yêu cầu dừng hẳn hàng đợi.")

    def retry_single_job(self, job_id: str) -> None:
        """Chạy lại riêng 1 bài được chọn."""
        job = self.queue_manager.get_job(job_id)
        if job:
            job.status = "Chờ"
            job.error_message = ""
            self.queue_manager.save()
            self.job_status_changed.emit(job.to_dict())
            if not self.isRunning():
                self._single_job_retry_id = job_id
                self.start()

    def run(self) -> None:
        self._stopped = False
        jobs_to_run = self.queue_manager.jobs

        if self._single_job_retry_id:
            job = self.queue_manager.get_job(self._single_job_retry_id)
            jobs_to_run = [job] if job else []
            self._single_job_retry_id = None

        total = len(jobs_to_run)
        if total == 0:
            self.all_finished_signal.emit(True, "Không có tác vụ nào cần xử lý.")
            return

        self.log_signal.emit(f"🚀 Bắt đầu xử lý {total} bài hát trong hàng đợi...")

        completed = 0
        failed = 0

        for idx, job in enumerate(jobs_to_run, start=1):
            if self._stopped:
                self.log_signal.emit("⏹ Hàng đợi đã dừng.")
                break

            # Nếu bài này đã hoàn thành từ trước (resume), bỏ qua để không tốn lượt
            if job.status == "Hoàn thành" and Path(job.output_video_path).is_file():
                completed += 1
                continue

            while self._paused and not self._stopped:
                time.sleep(1.0)

            if self._stopped:
                break

            # Lấy provider tương ứng cho bài này
            provider = self.providers.get(job.provider_key) or self.providers.get("wan")
            if not provider:
                job.status = "Lỗi"
                job.error_message = f"Không tìm thấy provider '{job.provider_key}'"
                self.queue_manager.save()
                self.job_status_changed.emit(job.to_dict())
                failed += 1
                continue

            self.log_signal.emit(f"🎬 [{idx}/{total}] Bắt đầu bài '{job.track_title}' ({provider.display_name})...")

            # Xác định đường dẫn file video đầu ra (chuẩn hóa tên file theo title)
            target_video_file, is_skip = resolve_output_file_path(
                self.output_dir, job.track_title, extension=".mp4", conflict_mode=self.conflict_mode
            )

            if is_skip:
                job.status = "Bỏ qua"
                job.output_video_path = str(target_video_file)
                self.queue_manager.save()
                self.job_status_changed.emit(job.to_dict())
                self.log_signal.emit(f"⏭ Đã tồn tại file '{target_video_file.name}', bỏ qua theo cài đặt.")
                completed += 1
                continue

            # Bước 1: Tạo ảnh (hoặc dùng ảnh cache nếu đã tạo trước đó để không tốn lượt)
            cached_img = Path(job.cached_image_path) if job.cached_image_path else None
            if not (cached_img and cached_img.is_file()):
                job.status = "Đang tạo ảnh"
                self.queue_manager.save()
                self.job_status_changed.emit(job.to_dict())

                cache_file = self.output_dir / f"{target_video_file.stem}.png" if job.keep_thumbnail else None

                try:
                    img_path = provider.text_to_image(
                        prompt=job.final_prompt,
                        width=job.width,
                        height=job.height,
                        cache_path=cache_file,
                        progress=lambda p, m: self.progress_signal.emit(p, f"[{idx}/{total}] {m}"),
                        cancelled=lambda: self._stopped,
                    )
                    job.cached_image_path = str(img_path)
                    self.queue_manager.save()
                except CookieExpiredError as e:
                    job.status = "Tạm dừng"
                    job.error_message = str(e)
                    self.queue_manager.save()
                    self.job_status_changed.emit(job.to_dict())
                    self.log_signal.emit(f"⚠ Cookie hết hạn: {e}")
                    self.cookie_expired_signal.emit(str(e))
                    self._paused = True
                    # Chờ dán cookie mới
                    while self._paused and not self._stopped:
                        time.sleep(1.0)
                    if self._stopped:
                        break
                    continue
                except Exception as e:
                    job.status = "Lỗi"
                    job.error_message = f"Lỗi tạo ảnh: {e}"
                    self.queue_manager.save()
                    self.job_status_changed.emit(job.to_dict())
                    self.log_signal.emit(f"❌ [{idx}/{total}] {job.error_message}")
                    failed += 1
                    continue
            else:
                img_path = cached_img

            # Bước 2: Tạo chuyển động (clip vài giây)
            job.status = "Đang tạo chuyển động"
            self.queue_manager.save()
            self.job_status_changed.emit(job.to_dict())

            try:
                vid_path = provider.image_to_video(
                    image_path=img_path,
                    prompt=job.final_prompt,
                    duration_seconds=job.duration_seconds,
                    width=job.width,
                    height=job.height,
                    output_path=target_video_file,
                    progress=lambda p, m: self.progress_signal.emit(p, f"[{idx}/{total}] {m}"),
                    cancelled=lambda: self._stopped,
                )
                job.output_video_path = str(vid_path)
                job.status = "Hoàn thành"
                job.error_message = ""
                self.queue_manager.save()
                self.job_status_changed.emit(job.to_dict())
                self.log_signal.emit(f"✔ [{idx}/{total}] Thành công! Đã lưu: {target_video_file.name}")
                completed += 1
            except CookieExpiredError as e:
                job.status = "Tạm dừng"
                job.error_message = str(e)
                self.queue_manager.save()
                self.job_status_changed.emit(job.to_dict())
                self.log_signal.emit(f"⚠ Cookie hết hạn: {e}")
                self.cookie_expired_signal.emit(str(e))
                self._paused = True
                while self._paused and not self._stopped:
                    time.sleep(1.0)
                if self._stopped:
                    break
                continue
            except Exception as e:
                job.status = "Lỗi"
                job.error_message = f"Lỗi tạo video: {e}"
                self.queue_manager.save()
                self.job_status_changed.emit(job.to_dict())
                self.log_signal.emit(f"❌ [{idx}/{total}] {job.error_message}")
                failed += 1

            pct = int((completed + failed) / total * 100)
            self.progress_signal.emit(pct, f"Đã xử lý {completed}/{total} bài hát")

        if self._stopped:
            self.all_finished_signal.emit(False, f"Đã dừng tác vụ. Hoàn thành {completed}/{total} bài.")
        else:
            self.all_finished_signal.emit(True, f"Đã hoàn tất! Thành công: {completed}/{total} bài.")
