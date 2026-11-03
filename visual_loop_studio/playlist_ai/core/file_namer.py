from __future__ import annotations

import re
from pathlib import Path
from typing import Literal


ConflictResolution = Literal["suffix", "overwrite", "skip"]

# Ký tự cấm trong tên file trên Windows và Unix: \ / : * ? " < > |
INVALID_CHARS_PATTERN = re.compile(r'[\\/:*?"<>|]')


def sanitize_title_for_filename(title: str, max_length: int = 150) -> str:
    """
    Làm sạch title để đặt tên file:
    - Loại bỏ ký tự cấm (\\ / : * ? " < > |).
    - Giữ nguyên tiếng Việt có dấu.
    - Cắt gọn độ dài nếu quá dài.
    """
    if not title:
        return "Untitled"

    # Thay thế ký tự cấm bằng khoảng trắng hoặc gạch ngang
    cleaned = INVALID_CHARS_PATTERN.sub("", title)

    # Loại bỏ khoảng trắng thừa
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Loại bỏ dấu chấm hoặc khoảng trắng ở cuối (Windows không cho phép file kết thúc bằng '.')
    cleaned = cleaned.rstrip(". ")

    if not cleaned:
        cleaned = "Untitled"

    # Giới hạn độ dài an toàn cho file name
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")

    return cleaned


def resolve_output_file_path(
    output_dir: str | Path,
    title: str,
    extension: str = ".mp4",
    conflict_mode: ConflictResolution = "suffix",
) -> tuple[Path, bool]:
    """
    Xác định đường dẫn file đầu ra:
    - conflict_mode == 'overwrite': Ghi đè file nếu đã tồn tại.
    - conflict_mode == 'skip': Nếu đã tồn tại file, trả về (path, True) để bỏ qua.
    - conflict_mode == 'suffix': Thêm hậu tố _2, _3 nếu file đã tồn tại.
    Trả về (Path, is_skip).
    """
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = sanitize_title_for_filename(title)
    if not extension.startswith("."):
        extension = "." + extension

    candidate = out_dir / f"{base_name}{extension}"

    if not candidate.exists():
        return candidate, False

    if conflict_mode == "overwrite":
        return candidate, False

    if conflict_mode == "skip":
        return candidate, True

    # Suffix mode (_2, _3, ...)
    counter = 2
    while candidate.exists():
        candidate = out_dir / f"{base_name}_{counter}{extension}"
        counter += 1

    return candidate, False
