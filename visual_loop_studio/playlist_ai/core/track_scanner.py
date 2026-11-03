from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class TrackInfo:
    index: int = 1
    file_path: Path = Path(".")
    filename: str = ""
    title: str = ""
    duration: float = 0.0

    def __post_init__(self) -> None:
        if not self.filename and self.file_path:
            self.filename = self.file_path.name

    @property
    def display_name(self) -> str:
        return f"{self.index:02d}. {self.title}"


def natural_sort_key(path: Path):
    """Sắp xếp file theo thứ tự số tự nhiên (1, 2, 10 thay vì 1, 10, 2)."""
    parts = re.split(r"(\d+)", path.name)
    return [int(p) if p.isdigit() else p.casefold() for p in parts]


_natural_sort_key = natural_sort_key


def _clean_title_from_filename(stem: str) -> str:
    """Loại bỏ tiền tố số thứ tự như '01. ', '01 - ', '01_', '1. ' khỏi tên file."""
    cleaned = re.sub(r"^\s*\d+[\s._\-–—]+\s*", "", stem).strip()
    return cleaned if cleaned else stem.strip()


def extract_track_title(file_path: Path) -> str:
    """Trích xuất title từ metadata ID3 tag của MP3, nếu không có thì lấy tên file đã làm sạch."""
    file_path = Path(file_path)
    stem = file_path.stem

    # 1. Thử đọc qua mutagen nếu có
    try:
        import mutagen  # type: ignore
        from mutagen.easyid3 import EasyID3  # type: ignore

        audio = mutagen.File(str(file_path))
        if audio is not None:
            # Thử EasyID3 title
            if hasattr(audio, "tags") and audio.tags:
                title = audio.tags.get("title") or audio.tags.get("TIT2")
                if title:
                    val = str(title[0] if isinstance(title, (list, tuple)) else title).strip()
                    if val:
                        return val
    except Exception:
        pass

    # 2. Fallback: Lấy từ tên file đã làm sạch số thứ tự
    return _clean_title_from_filename(stem)


def scan_mp3_folder(folder_path: str | Path) -> List[TrackInfo]:
    """Quét thư mục lấy danh sách các file MP3 và trích xuất title tương ứng."""
    folder = Path(folder_path).resolve()
    if not folder.is_dir():
        return []

    # Tìm tất cả file mp3 (case-insensitive)
    audio_files = [
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in {".mp3", ".wav", ".m4a", ".flac"}
    ]

    # Sắp xếp theo số tự nhiên
    audio_files.sort(key=_natural_sort_key)

    tracks: List[TrackInfo] = []
    for idx, f in enumerate(audio_files, start=1):
        title = extract_track_title(f)
        tracks.append(
            TrackInfo(
                index=idx,
                file_path=f,
                filename=f.name,
                title=title,
            )
        )
    return tracks
