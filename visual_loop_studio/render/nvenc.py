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
    all_names = ("h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_amf", "h264_mf", "libx264")
    return {name for name in all_names if name in result.stdout}


@lru_cache(maxsize=16)
def encoder_usable(encoder: str, ffmpeg: str = "ffmpeg") -> bool:
    if encoder not in available_encoders(ffmpeg):
        return False
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=black:s=64x64:d=0.05", "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        return subprocess.run(
            command, capture_output=True, timeout=12, check=False,
            **hidden_process_kwargs(),
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def nvenc_usable(ffmpeg: str = "ffmpeg") -> bool:
    return encoder_usable("h264_nvenc", ffmpeg)


def resolve_encoder(choice: str, ffmpeg: str = "ffmpeg") -> str:
    mapping = {
        "H264 NVENC": "h264_nvenc",
        "HEVC NVENC": "hevc_nvenc",
        "Intel QSV": "h264_qsv",
        "AMD AMF": "h264_amf",
        "Windows Media Foundation": "h264_mf",
        "libx264": "libx264",
        "Auto": "Auto",
    }
    available = available_encoders(ffmpeg)
    canonical = mapping.get(choice, choice)
    if canonical == "Auto" or choice == "Auto":
        # Prioritize fastest hardware encoder
        for hw in ("h264_nvenc", "h264_qsv", "h264_mf", "h264_amf"):
            if hw in available and encoder_usable(hw, ffmpeg):
                return hw
        return "libx264"
    if canonical in available and encoder_usable(canonical, ffmpeg):
        return canonical
    # Fallback to auto if selected hardware encoder is unavailable
    for hw in ("h264_nvenc", "h264_qsv", "h264_mf", "h264_amf"):
        if hw in available and encoder_usable(hw, ffmpeg):
            return hw
    return "libx264"
