from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from utils.media import IMAGE_EXTENSIONS


@dataclass(frozen=True)
class BatchPrompt:
    prompt: str
    image_path: str = ""


def parse_prompt_file(path: str | Path) -> list[BatchPrompt]:
    source = Path(path)
    if not source.is_file():
        raise ValueError(f"Không tìm thấy file prompt: {source}")
    if source.suffix.casefold() == ".csv":
        return _parse_csv(source)
    rows = []
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            rows.append(BatchPrompt(value))
    if not rows:
        raise ValueError("File TXT không có prompt hợp lệ.")
    return rows


def _parse_csv(path: Path) -> list[BatchPrompt]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        field_map = {str(name or "").strip().casefold(): name for name in (reader.fieldnames or [])}
        prompt_field = field_map.get("prompt")
        image_field = field_map.get("image") or field_map.get("image_path")
        if not prompt_field:
            raise ValueError("CSV phải có cột 'prompt'.")
        rows: list[BatchPrompt] = []
        for item in reader:
            prompt = str(item.get(prompt_field) or "").strip()
            if not prompt:
                continue
            image = str(item.get(image_field) or "").strip() if image_field else ""
            if image and not Path(image).is_absolute():
                image = str((path.parent / image).resolve())
            rows.append(BatchPrompt(prompt, image))
    if not rows:
        raise ValueError("CSV không có prompt hợp lệ.")
    return rows


def image_files(folder: str | Path, recursive: bool = False) -> list[str]:
    root = Path(folder)
    if not root.is_dir():
        raise ValueError(f"Thư mục ảnh không tồn tại: {root}")
    iterator = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        (str(item.resolve()) for item in iterator if item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS),
        key=str.casefold,
    )


def expand_batch_inputs(
    prompts: list[BatchPrompt],
    selected_images: list[str],
) -> list[BatchPrompt]:
    if not prompts:
        raise ValueError("Chưa có prompt.")
    explicit = [item for item in prompts if item.image_path]
    if explicit:
        if len(explicit) != len(prompts):
            raise ValueError("CSV phải khai báo image cho tất cả dòng hoặc không dòng nào.")
        return prompts
    images = [str(Path(item).resolve()) for item in selected_images]
    if not images:
        return prompts
    if len(prompts) == 1:
        return [BatchPrompt(prompts[0].prompt, image) for image in images]
    if len(prompts) == len(images):
        return [BatchPrompt(prompt.prompt, image) for prompt, image in zip(prompts, images)]
    # Deliberately avoid a silent Cartesian product, which can create a large
    # bill unexpectedly. The operator must make the mapping explicit.
    raise ValueError(
        "Khi có nhiều prompt và nhiều ảnh, số prompt phải bằng số ảnh. "
        "Dùng một prompt chung nếu muốn áp dụng cho toàn bộ ảnh."
    )


def normalize_prompt(value: str) -> str:
    lines = [" ".join(line.strip().split()) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()
