from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


@dataclass
class MediaInfo:
    path: str
    duration: float
    has_video: bool
    has_audio: bool
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bitrate: int = 0
    file_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any, cast, default=0):
    try:
        return cast(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return default


def probe_media(path: str, ffprobe: str = "ffprobe") -> MediaInfo:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(path)
    command = [
        ffprobe, "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("Không tìm thấy FFprobe. Kiểm tra Settings > FFmpeg/FFprobe.") from exc
    if result.returncode:
        raise ValueError(result.stderr.strip() or "FFprobe không thể đọc media.")
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    fmt = payload.get("format", {})
    duration = _number(fmt.get("duration"), float)
    if duration <= 0:
        duration = max((_number(item.get("duration"), float) for item in streams), default=0.0)
    fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    fps = _number(Fraction(fps_text), float)
    return MediaInfo(
        path=str(target.resolve()), duration=duration,
        has_video=bool(video), has_audio=bool(audio),
        video_codec=video.get("codec_name", ""), audio_codec=audio.get("codec_name", ""),
        width=_number(video.get("width"), int), height=_number(video.get("height"), int), fps=fps,
        sample_rate=_number(audio.get("sample_rate"), int), channels=_number(audio.get("channels"), int),
        bitrate=_number(fmt.get("bit_rate"), int), file_size=target.stat().st_size,
    )


def format_duration(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs = remainder / 1000
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"
