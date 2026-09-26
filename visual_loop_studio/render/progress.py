from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RenderProgress:
    percent: float = 0.0
    current_seconds: float = 0.0
    fps: float = 0.0
    speed: float = 0.0
    total_size: int = 0


class ProgressParser:
    def __init__(self, duration: float):
        self.duration = max(duration, 0.001)
        self.data: dict[str, str] = {}

    def feed_line(self, line: str) -> RenderProgress | None:
        if "=" not in line:
            return None
        key, value = line.strip().split("=", 1)
        self.data[key] = value
        if key not in {"progress", "out_time", "out_time_ms", "out_time_us"}:
            return None
        current = 0.0
        if self.data.get("out_time_us", "").isdigit():
            current = int(self.data["out_time_us"]) / 1_000_000
        elif self.data.get("out_time_ms", "").isdigit():
            # FFmpeg historically names this out_time_ms even though the unit is microseconds.
            current = int(self.data["out_time_ms"]) / 1_000_000
        elif self.data.get("out_time"):
            parts = self.data["out_time"].split(":")
            if len(parts) == 3:
                current = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        speed_text = self.data.get("speed", "0x").rstrip("x")
        return RenderProgress(
            percent=min(100.0, current / self.duration * 100),
            current_seconds=current,
            fps=_float(self.data.get("fps")), speed=_float(speed_text),
            total_size=_int(self.data.get("total_size")),
        )


def _float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def parse_ffmpeg_time(text: str) -> float:
    match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    return (int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))) if match else 0.0
