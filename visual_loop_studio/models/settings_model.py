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
    gemini_api_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AppSettings":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (value or {}).items() if k in allowed})
