from __future__ import annotations

from render.ffprobe import probe_media


def main_audio_duration(path: str, ffprobe: str = "ffprobe") -> float:
    info = probe_media(path, ffprobe)
    if not info.has_audio:
        raise ValueError("File nhạc chính không có audio stream.")
    if info.duration <= 0:
        raise ValueError("Không đọc được thời lượng nhạc chính.")
    return info.duration
