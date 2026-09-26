from __future__ import annotations

from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".gif", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mpeg", ".mpg", ".ts"}
BACKGROUND_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def is_still_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def existing_file(path: str, label: str) -> str:
    if not path:
        raise ValueError(f"Chưa chọn {label}.")
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"Không tìm thấy {label}: {path}")
    return str(target.resolve())


def background_files(folder: str, recursive: bool = False) -> list[str]:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError("Thư mục background không tồn tại.")
    iterator = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (str(path.resolve()) for path in iterator if path.is_file() and path.suffix.lower() in BACKGROUND_EXTENSIONS),
        key=str.lower,
    )
