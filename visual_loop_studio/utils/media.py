from __future__ import annotations

from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def is_still_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def existing_file(path: str, label: str) -> str:
    if not path:
        raise ValueError(f"Chưa chọn {label}.")
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"Không tìm thấy {label}: {path}")
    return str(target.resolve())
