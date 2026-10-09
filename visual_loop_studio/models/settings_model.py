from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class AppSettings:
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    last_input_folder: str = ""
    last_output_folder: str = ""
    encoder: str = "Auto"
    resolution: str = "1920x1080"
    fps: int = 30
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_workflow: str = ""
    local_ai_root: str = ""
    local_ai_auto_start: bool = True
    # Legacy only. New values are stored in Windows Credential Manager and
    # this field is removed when settings.json is written.
    gemini_api_key: str = ""
    byteplus_las_base_url: str = "https://operator.las.ap-southeast-1.bytepluses.com/api/v1"
    ai_batch_concurrency: int = 2
    ai_retry_limit: int = 3
    ai_daily_budget: float = 0.0
    ai_batch_budget: float = 0.0
    ai_estimated_cloud_cost_per_second: float = 0.0
    google_oauth_client_json: str = ""
    muse_start_url: str = "https://muse.ai/"
    muse_connection_mode: str = "manual_browser"
    muse_task_mode: str = "video"
    muse_headless: bool = False
    muse_concurrency: int = 1
    muse_auto_refresh_minutes: int = 60
    muse_style_suffix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AppSettings":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (value or {}).items() if k in allowed})
