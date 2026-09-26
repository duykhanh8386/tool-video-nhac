from __future__ import annotations

import subprocess
from functools import lru_cache

from utils.process import hidden_process_kwargs


def available_encoders(ffmpeg: str = "ffmpeg") -> set[str]:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, errors="replace", check=False,
            **hidden_process_kwargs(),
        )
    except FileNotFoundError:
        return set()
    return {name for name in ("h264_nvenc", "hevc_nvenc", "libx264") if name in result.stdout}


def resolve_encoder(choice: str, ffmpeg: str = "ffmpeg") -> str:
    mapping = {"H264 NVENC": "h264_nvenc", "HEVC NVENC": "hevc_nvenc", "libx264": "libx264"}
    available = available_encoders(ffmpeg)
    if choice == "Auto":
        return "h264_nvenc" if "h264_nvenc" in available and nvenc_usable(ffmpeg) else "libx264"
    selected = mapping.get(choice, "libx264")
    if selected not in available:
        raise RuntimeError(f"Encoder {selected} không có trong FFmpeg hiện tại.")
    return selected


@lru_cache(maxsize=4)
def nvenc_usable(ffmpeg: str = "ffmpeg") -> bool:
    if "h264_nvenc" not in available_encoders(ffmpeg):
        return False
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=black:s=64x64:d=.05", "-c:v", "h264_nvenc", "-f", "null", "-",
    ]
    try:
        return subprocess.run(
            command, capture_output=True, timeout=12, check=False,
            **hidden_process_kwargs(),
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
