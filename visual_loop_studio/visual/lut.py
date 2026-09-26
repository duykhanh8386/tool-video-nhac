from __future__ import annotations

from utils.paths import ffmpeg_filter_path


def lut_filter(path: str, strength: float = 1.0) -> list[str]:
    if not path:
        return []
    # FFmpeg lut3d has no mix knob; strength is reserved for a future split/blend implementation.
    return [f"lut3d=file='{ffmpeg_filter_path(path)}'"]
