from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Tuple

from playlist_ai.core.track_scanner import TrackInfo


DEFAULT_PROMPT_TEMPLATE = '{prompt}, mood theo bài hát "{title}", cinematic, smooth motion, high detail, 4k'


@dataclass
class MappedItem:
    index: int
    track: TrackInfo
    raw_prompt: str
    final_prompt: str
    is_looped: bool = False


PromptMismatchMode = Literal["loop", "last"]


def parse_prompts_from_text(raw_text: str) -> List[str]:
    """Tách prompt theo từng dòng, tự động loại bỏ các dòng trống hoặc chỉ có khoảng trắng."""
    if not raw_text:
        return []
    lines = [line.strip() for line in raw_text.splitlines()]
    return [line for line in lines if line]


def apply_title_template(raw_prompt: str, title: str, template: str = DEFAULT_PROMPT_TEMPLATE) -> str:
    """Gắn tên bài hát (title) vào prompt theo template."""
    template = template or DEFAULT_PROMPT_TEMPLATE
    formatted = template.replace("{prompt}", raw_prompt).replace("{title}", title)
    return formatted.strip()


def map_prompts_to_tracks(
    tracks: List[TrackInfo],
    prompts: List[str],
    mismatch_mode: PromptMismatchMode = "loop",
    use_title_template: bool = True,
    template: str = DEFAULT_PROMPT_TEMPLATE,
) -> Tuple[List[MappedItem], str]:
    """
    Ghép danh sách prompt với danh sách bài hát MP3 theo thứ tự.
    Xử lý lệch số lượng:
    - Prompt ít hơn bài: lặp vòng (loop) hoặc giữ prompt cuối (last).
    - Prompt nhiều hơn bài: cảnh báo và cắt bỏ phần thừa.
    """
    if not tracks:
        return [], "Chưa có file MP3 nào được nạp."
    if not prompts:
        return [], "Chưa có prompt nào được nhập."

    total_tracks = len(tracks)
    total_prompts = len(prompts)
    warning_msg = ""

    if total_prompts > total_tracks:
        excess = total_prompts - total_tracks
        warning_msg = f"Cảnh báo: Có {total_prompts} prompt nhưng chỉ có {total_tracks} bài hát. Đã tự động bỏ {excess} prompt thừa."
    elif total_prompts < total_tracks:
        missing = total_tracks - total_prompts
        if mismatch_mode == "loop":
            warning_msg = f"Có {total_prompts} prompt cho {total_tracks} bài. Đã tự động lặp vòng lại prompt cho {missing} bài còn lại."
        else:
            warning_msg = f"Có {total_prompts} prompt cho {total_tracks} bài. Đã dùng prompt cuối cho {missing} bài còn lại."

    mapped_items: List[MappedItem] = []

    for i, track in enumerate(tracks):
        is_looped = False
        if i < total_prompts:
            raw_p = prompts[i]
        else:
            is_looped = True
            if mismatch_mode == "loop":
                raw_p = prompts[i % total_prompts]
            else:
                raw_p = prompts[-1]

        if use_title_template:
            final_p = apply_title_template(raw_p, track.title, template)
        else:
            final_p = raw_p

        mapped_items.append(
            MappedItem(
                index=track.index,
                track=track,
                raw_prompt=raw_p,
                final_prompt=final_p,
                is_looped=is_looped,
            )
        )

    return mapped_items, warning_msg


def parse_and_map_prompts(
    prompt_text: str,
    tracks: List[TrackInfo],
    template: str = DEFAULT_PROMPT_TEMPLATE,
    use_template: bool = True,
    mismatch_mode: PromptMismatchMode = "loop",
) -> Tuple[List[MappedItem], int, int, str | None]:
    """Helper phân tích text prompt và ghép nhanh với tracks, trả về (mapped_items, p_count, t_count, warn)."""
    prompts = parse_prompts_from_text(prompt_text)
    p_count = len(prompts)
    t_count = len(tracks)
    mapped, warn = map_prompts_to_tracks(
        tracks=tracks,
        prompts=prompts,
        mismatch_mode=mismatch_mode,
        use_title_template=use_template,
        template=template,
    )
    return mapped, p_count, t_count, warn if warn else None

