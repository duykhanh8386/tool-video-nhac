from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtGui import QImage

from playlist_ai.core.file_namer import sanitize_title_for_filename
from utils.net_ssl import robust_urlopen


DEFAULT_MASTER_VISUALIZER_PROMPT = (
    "Animate the provided music playlist thumbnail into a beautiful, professional cinematic music visualizer video. "
    "Use the original image as the exact visual reference, preserving the singer's identity, facial features, pose, "
    "clothing, background composition, colors, playlist title, artist name, and all existing text exactly as shown. "
    "Keep every text element sharp, readable, correctly spelled, and in its original position throughout the entire video.\n\n"
    "CAMERA: 100% locked static camera, absolutely no zoom in or zoom out, no camera movement, no composition changes.\n\n"
    "AUDIO SPECTRUM / MUSIC EQUALIZER: Animate the existing audio spectrum or equalizer with gentle, smooth rhythmic movements. "
    "If no equalizer exists, add a small, elegant music equalizer along the bottom edge, matching the thumbnail's color palette. "
    "The bars softly rise and fall in a smooth, closed repeating cycle, without covering the singer, title, or playlist text.\n\n"
    "LIGHTING: Subtle cinematic glow and brightness pulses, gently increasing and decreasing in a smooth, closed repeating cycle. "
    "Preserve the original colors and lighting style.\n\n"
    "PERFECT SEAMLESS LOOP: The first frame and final frame must be visually identical. All light pulses and equalizer movements "
    "must return precisely to their original starting states at the final frame, creating a smooth, seamless infinite loop.\n\n"
    "High-quality cinematic music visualizer, clean professional details, original 16:9 composition, no duration badge, no watermark."
)

YOUTUBE_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}


def search_youtube_first_video(query: str, timeout: int = 12) -> Optional[Dict[str, Any]]:
    """
    Tìm kiếm trên YouTube theo tên bài hát và trả về thông tin video đầu tiên:
    - video_id: mã video 11 ký tự
    - title: tiêu đề video trên YouTube
    - thumbnails: danh sách URL ảnh thumbnail từ kết quả tìm kiếm
    """
    clean_query = query.strip()
    if not clean_query:
        return None

    search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(clean_query)}"
    req = urllib.request.Request(search_url, headers=YOUTUBE_SEARCH_HEADERS)

    try:
        with robust_urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        # Fallback direct request
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            return None

    # Cách 1: Phân tích JSON từ ytInitialData
    data_match = re.search(r"ytInitialData\s*=\s*({.+?});</script>", html)
    if data_match:
        try:
            data = json.loads(data_match.group(1))
            contents = (
                data.get("contents", {})
                .get("twoColumnSearchResultsRenderer", {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            for sec in contents:
                items = sec.get("itemSectionRenderer", {}).get("contents", [])
                for item in items:
                    v = item.get("videoRenderer")
                    if v and "videoId" in v:
                        vid_id = str(v["videoId"]).strip()
                        runs = v.get("title", {}).get("runs", [])
                        vid_title = "".join(r.get("text", "") for r in runs) or clean_query
                        thumbs = [
                            t.get("url")
                            for t in v.get("thumbnail", {}).get("thumbnails", [])
                            if t.get("url")
                        ]
                        return {
                            "video_id": vid_id,
                            "title": vid_title,
                            "thumbnails": thumbs,
                        }
        except Exception:
            pass

    # Cách 2: Regex dự phòng tìm videoId đầu tiên
    vids = re.findall(r"/watch\?v=([a-zA-Z0-9_-]{11})", html)
    if vids:
        # Loại trừ một số id đặc biệt nếu có
        for vid in vids:
            if len(vid) == 11 and vid != "00000000000":
                return {
                    "video_id": vid,
                    "title": clean_query,
                    "thumbnails": [],
                }

    # Cách 3: Regex tìm "videoId":"..."
    id_matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
    if id_matches:
        return {
            "video_id": id_matches[0],
            "title": clean_query,
            "thumbnails": [],
        }

    return None


def download_youtube_thumbnail(
    video_id: str,
    output_path: Path | str,
    extra_urls: Optional[List[str]] = None,
    timeout: int = 15,
) -> Optional[Path]:
    """
    Tải thumbnail YouTube với độ phân giải cao nhất có thể (ưu tiên maxresdefault 1280x720 / 1920x1080),
    lưu file dưới định dạng .png hoặc .jpg tương ứng với output_path.
    """
    vid = video_id.strip()
    if not vid:
        return None

    dest = Path(output_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    candidates: List[str] = [
        f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
    ]
    if extra_urls:
        for u in extra_urls:
            if u and u not in candidates:
                candidates.append(u)
    candidates.extend([
        f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
    ])

    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=YOUTUBE_SEARCH_HEADERS)
            try:
                resp = robust_urlopen(req, timeout=timeout)
            except Exception:
                resp = urllib.request.urlopen(req, timeout=timeout)

            with resp:
                data = resp.read()

            # YouTube trả về ảnh rỗng/1097 bytes khi maxres không tồn tại
            if len(data) <= 2048:
                continue

            # Sử dụng QImage để đảm bảo chuyển đổi thành file PNG/JPG chuẩn xác
            img = QImage()
            if img.loadFromData(data):
                if dest.suffix.lower() == ".png":
                    ok = img.save(str(dest), "PNG")
                else:
                    ok = img.save(str(dest), "JPG", 95)
                if ok and dest.is_file() and dest.stat().st_size > 0:
                    return dest

            # Fallback lưu dữ liệu nhị phân trực tiếp
            dest.write_bytes(data)
            if dest.is_file() and dest.stat().st_size > 2048:
                return dest
        except Exception:
            continue

    return None


def fetch_and_save_youtube_thumbnail(
    track_title: str,
    output_path: Path | str,
    timeout: int = 15,
) -> Optional[Path]:
    """
    Tìm kiếm video trên YouTube theo tên bài hát, tải thumbnail về và lưu vào output_path.
    """
    info = search_youtube_first_video(track_title, timeout=timeout)
    if not info or not info.get("video_id"):
        return None

    video_id = info["video_id"]
    thumbs = info.get("thumbnails") or []
    return download_youtube_thumbnail(video_id, output_path, extra_urls=thumbs, timeout=timeout)
