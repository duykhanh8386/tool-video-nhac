from __future__ import annotations

from datetime import datetime
from pathlib import Path

from utils.paths import LOG_DIR, ensure_app_dirs


def new_render_log(prefix: str = "render") -> Path:
    ensure_app_dirs()
    return LOG_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}.log"


def explain_ffmpeg_error(text: str) -> str:
    lowered = text.lower()
    matches = (
        (("no space left", "disk full"), "Ổ đĩa không còn đủ dung lượng."),
        (("permission denied",), "Không có quyền đọc/ghi đường dẫn đã chọn."),
        (("nvenc", "cannot load nvcuda"), "NVENC không khả dụng. Hãy chọn Auto hoặc libx264."),
        (("invalid data", "moov atom not found"), "Media không hợp lệ hoặc đã bị hỏng."),
        (("no such file",), "Không tìm thấy file đầu vào hoặc FFmpeg."),
        (("resource busy", "being used by another process"), "File output đang được chương trình khác sử dụng."),
    )
    for needles, message in matches:
        if any(needle in lowered for needle in needles):
            return message
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "FFmpeg không trả về thông tin lỗi."
