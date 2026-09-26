from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class LoopProject:
    video: str = ""
    main_audio: str = ""
    background_audio: str = ""
    output_folder: str = ""
    output_name: str = ""
    main_volume: float = 1.0
    background_volume: float = 0.15
    loop_background: bool = True
    background_crossfade: bool = True
    crossfade_ms: int = 1000
    normalize: bool = False
    seamless_video: bool = True
    encoder: str = "Auto"
    resolution: str = "Keep source"
    fps: str = "Keep source"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LoopProject":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in (value or {}).items() if k in allowed})
