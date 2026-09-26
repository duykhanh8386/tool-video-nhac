from __future__ import annotations

import random
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from models.visual_project import ElementLayout, default_element_layouts


PRESETS = ["Healing", "Sleep", "Christmas", "Reggae", "LoFi", "Retro", "Minimal", "Meditation", "Thai Music", "Brazil Music", "Salsa", "Gospel"]

ELEMENT_LABELS = {
    "title": "Tiêu đề", "subtitle": "Tiêu đề phụ", "artist": "Nghệ sĩ", "custom_text": "Nội dung thêm",
    "playlist": "Danh sách bài hát", "logo": "Logo", "platform_icons": "Biểu tượng nền tảng",
    "waveform": "File sóng", "artwork": "Ảnh bìa",
}

TEMPLATE_PREFERENCES: dict[str, dict[str, list[str]]] = {
    "REGGAE": {
        "logo": ["top_left", "top_right"], "title": ["top_right", "top_left"],
        "playlist": ["right_center", "left_center"], "platform_icons": ["bottom_left", "bottom_right"],
        "waveform": ["bottom_right", "bottom_left"], "artwork": ["left_center", "right_center"],
    },
    "HEALING": {
        "title": ["top_center", "top_left"], "subtitle": ["bottom_center", "bottom_left"],
        "waveform": ["bottom_center", "bottom_right"], "logo": ["top_left", "top_right"],
        "artwork": ["right_center", "left_center"], "playlist": ["left_center", "right_center"],
    },
    "CHRISTMAS": {
        "title": ["top_center", "top_right"], "playlist": ["right_center", "left_center"],
        "logo": ["top_left", "top_right"], "waveform": ["bottom_center", "bottom_right"],
        "platform_icons": ["bottom_left", "bottom_right"],
    },
    "LOFI": {
        "title": ["top_left", "top_right"], "playlist": ["left_center", "right_center"],
        "artwork": ["right_center", "left_center"], "waveform": ["bottom_left", "bottom_right"],
    },
    "MINIMAL": {
        "title": ["top_left", "top_right", "top_center"], "subtitle": ["bottom_center", "bottom_left"],
        "logo": ["top_right", "top_left"], "waveform": ["bottom_right", "bottom_left"],
    },
}


@dataclass
class ZoneMetric:
    column: int
    row: int
    brightness: float
    detail: float


@dataclass
class BackgroundAnalysis:
    width: int = 240
    height: int = 135
    mean_brightness: float = .5
    subject_side: str = "CENTER"
    subject_bbox: tuple[float, float, float, float] = (.34, .16, .32, .68)
    grid_columns: int = 6
    grid_rows: int = 4
    zones: list[ZoneMetric] | None = None
    face_count: int = 0
    human_count: int = 0
    method: str = "saliency"

    def region_metrics(self, rect: tuple[float, float, float, float]) -> tuple[float, float]:
        x, y, width, height = rect
        selected: list[ZoneMetric] = []
        for zone in self.zones or []:
            cx = (zone.column + .5) / self.grid_columns
            cy = (zone.row + .5) / self.grid_rows
            if x <= cx <= x + width and y <= cy <= y + height:
                selected.append(zone)
        if not selected:
            return self.mean_brightness, .5
        return (
            sum(item.brightness for item in selected) / len(selected),
            sum(item.detail for item in selected) / len(selected),
        )


@dataclass
class LayoutResult:
    elements: dict[str, ElementLayout]
    text_color: str
    analysis: BackgroundAnalysis


def analyze_background(path: str, ffmpeg: str = "ffmpeg") -> BackgroundAnalysis:
    if not path or not Path(path).is_file():
        return BackgroundAnalysis(zones=_neutral_zones())
    width, height = 240, 135
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path, "-frames:v", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return BackgroundAnalysis(zones=_neutral_zones(), method="fallback")
    expected = width * height * 3
    if result.returncode or len(result.stdout) < expected:
        return BackgroundAnalysis(zones=_neutral_zones(), method="fallback")
    pixels = result.stdout[:expected]
    luma = [0.0] * (width * height)
    for index in range(width * height):
        offset = index * 3
        luma[index] = (pixels[offset] * .2126 + pixels[offset + 1] * .7152 + pixels[offset + 2] * .0722) / 255
    mean = sum(luma) / len(luma)
    detail = [0.0] * len(luma)
    for y in range(1, height - 1):
        row = y * width
        for x in range(1, width - 1):
            index = row + x
            detail[index] = min(1.0, (abs(luma[index] - luma[index - 1]) + abs(luma[index] - luma[index - width])) * 2.5)
    zones = _zone_metrics(luma, detail, width, height)
    detected_subject = _detect_subjects(pixels, width, height)
    if detected_subject:
        subject_bbox, face_count, human_count = detected_subject
        subject_side = _side(subject_bbox[0] + subject_bbox[2] / 2)
        method = "vision+saliency"
    else:
        subject_side, subject_bbox = _saliency_subject(luma, detail, width, height, mean)
        face_count, human_count, method = 0, 0, "saliency"
    return BackgroundAnalysis(
        width=width, height=height, mean_brightness=mean, subject_side=subject_side,
        subject_bbox=subject_bbox, grid_columns=6, grid_rows=4, zones=zones,
        face_count=face_count, human_count=human_count, method=method,
    )


def compose_layout(
    analysis: BackgroundAnalysis,
    current: dict[str, ElementLayout] | None = None,
    template: str = "Minimal",
    variant: int = 0,
    active_elements: set[str] | None = None,
) -> LayoutResult:
    defaults = default_element_layouts()
    current = current or defaults
    result = {name: _clone(layout) for name, layout in defaults.items()}
    for name, layout in current.items():
        result[name] = _clone(layout).normalized()
    occupied: list[tuple[float, float, float, float]] = []
    for element_id, layout in result.items():
        base_kind = element_id.split("_copy_", 1)[0]
        if layout.locked and layout.visible and (active_elements is None or base_kind in active_elements):
            occupied.append(to_top_left_rect(layout))
    order = ["title", "playlist", "artwork", "waveform", "logo", "subtitle", "platform_icons", "artist", "custom_text"]
    for element_id in order + [name for name in result if name not in order]:
        layout = result[element_id]
        base_kind = element_id.split("_copy_", 1)[0]
        if layout.locked or not layout.visible or (active_elements is not None and base_kind not in active_elements):
            continue
        candidates = _candidate_rects(layout.width, layout.height)
        preferences = _preferences(base_kind, template, analysis.subject_side)
        if variant and preferences:
            shift = variant % len(preferences)
            preferences = preferences[shift:] + preferences[:shift]
        ranked: list[tuple[float, tuple[float, float, float, float], str]] = []
        rng = random.Random(f"{variant}:{element_id}:{analysis.subject_side}")
        for zone_name, rect in candidates.items():
            _, detail = analysis.region_metrics(rect)
            overlap = sum(_overlap_ratio(rect, item) for item in occupied)
            subject_overlap = _overlap_ratio(rect, analysis.subject_bbox)
            preferred_rank = preferences.index(zone_name) if zone_name in preferences else len(preferences) + 1
            jitter = rng.random() * .18
            score = detail * 2.2 + overlap * 12 + subject_overlap * 18 + preferred_rank * .32 + _safe_margin_penalty(rect) + jitter
            ranked.append((score, rect, zone_name))
        ranked.sort(key=lambda item: item[0])
        _, chosen, _ = ranked[0]
        layout.x, layout.y, layout.width, layout.height = chosen
        layout.anchor = "top_left"
        occupied.append(chosen)
    title_brightness, _ = analysis.region_metrics(to_top_left_rect(result["title"]))
    return LayoutResult(result, auto_text_color(title_brightness), analysis)


def reset_layout() -> dict[str, ElementLayout]:
    return {name: _clone(layout) for name, layout in default_element_layouts().items()}


def to_top_left_rect(layout: ElementLayout) -> tuple[float, float, float, float]:
    x, y, width, height = layout.x, layout.y, layout.width, layout.height
    if layout.anchor in {"center", "top_center", "bottom_center"}:
        x -= width / 2
    elif layout.anchor in {"top_right", "bottom_right"}:
        x -= width
    if layout.anchor == "center":
        y -= height / 2
    elif layout.anchor in {"bottom_left", "bottom_center", "bottom_right"}:
        y -= height
    return x, y, width, height


def update_from_top_left(layout: ElementLayout, rect: tuple[float, float, float, float]) -> None:
    x, y, width, height = rect
    layout.width, layout.height = width, height
    if layout.anchor in {"center", "top_center", "bottom_center"}:
        x += width / 2
    elif layout.anchor in {"top_right", "bottom_right"}:
        x += width
    if layout.anchor == "center":
        y += height / 2
    elif layout.anchor in {"bottom_left", "bottom_center", "bottom_right"}:
        y += height
    layout.x, layout.y = x, y
    layout.normalized()


def auto_text_color(brightness: float) -> str:
    return "#111827" if brightness > .62 else "#FFFFFF"


def _clone(layout: ElementLayout) -> ElementLayout:
    return ElementLayout(**asdict(layout))


def _neutral_zones() -> list[ZoneMetric]:
    return [ZoneMetric(column, row, .45, .35) for row in range(4) for column in range(6)]


def _zone_metrics(luma: list[float], detail: list[float], width: int, height: int) -> list[ZoneMetric]:
    zones: list[ZoneMetric] = []
    for row in range(4):
        for column in range(6):
            x0, x1 = column * width // 6, (column + 1) * width // 6
            y0, y1 = row * height // 4, (row + 1) * height // 4
            indexes = [y * width + x for y in range(y0, y1) for x in range(x0, x1)]
            zones.append(ZoneMetric(column, row, sum(luma[i] for i in indexes) / len(indexes), sum(detail[i] for i in indexes) / len(indexes)))
    return zones


def _saliency_subject(luma: list[float], detail: list[float], width: int, height: int, mean: float) -> tuple[str, tuple[float, float, float, float]]:
    scores = [0.0, 0.0, 0.0]
    counts = [0, 0, 0]
    for y in range(height // 10, height * 9 // 10):
        for x in range(width):
            index = y * width + x
            third = min(2, x * 3 // width)
            center_weight = 1.15 if height * .15 < y < height * .85 else .8
            scores[third] += (detail[index] * 1.7 + abs(luma[index] - mean) * .55) * center_weight
            counts[third] += 1
    scores = [score / max(1, count) for score, count in zip(scores, counts)]
    best = max(range(3), key=scores.__getitem__)
    side = ("LEFT", "CENTER", "RIGHT")[best]
    return side, ((.02, .12, .38, .76), (.31, .10, .38, .80), (.60, .12, .38, .76))[best]


def _detect_subjects(pixels: bytes, width: int, height: int) -> tuple[tuple[float, float, float, float], int, int] | None:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        image = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width, 3))
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        faces = cascade.detectMultiScale(gray, scaleFactor=1.08, minNeighbors=4, minSize=(18, 18))
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        humans, _weights = hog.detectMultiScale(image, winStride=(8, 8), padding=(8, 8), scale=1.05)
        boxes = list(faces) + list(humans)
        if not boxes:
            return None
        x0 = min(int(box[0]) for box in boxes)
        y0 = min(int(box[1]) for box in boxes)
        x1 = max(int(box[0] + box[2]) for box in boxes)
        y1 = max(int(box[1] + box[3]) for box in boxes)
        padding_x, padding_y = (x1 - x0) * .55, (y1 - y0) * 1.5
        left, top = max(0, (x0 - padding_x) / width), max(0, (y0 - padding_y * .35) / height)
        right, bottom = min(1, (x1 + padding_x) / width), min(1, (y1 + padding_y) / height)
        return (left, top, right - left, bottom - top), len(faces), len(humans)
    except Exception:
        return None


def _side(center_x: float) -> str:
    return "LEFT" if center_x < .4 else "RIGHT" if center_x > .6 else "CENTER"


def _preferences(element: str, template: str, subject_side: str) -> list[str]:
    template_map = TEMPLATE_PREFERENCES.get(template.upper(), TEMPLATE_PREFERENCES["MINIMAL"])
    preferred = list(template_map.get(element, []))
    opposite = {
        "LEFT": ["top_right", "right_center", "bottom_right", "top_center", "bottom_center", "top_left", "left_center", "bottom_left"],
        "RIGHT": ["top_left", "left_center", "bottom_left", "top_center", "bottom_center", "top_right", "right_center", "bottom_right"],
        "CENTER": ["top_left", "top_right", "bottom_left", "bottom_right", "top_center", "bottom_center", "left_center", "right_center"],
    }[subject_side]
    return preferred + [item for item in opposite if item not in preferred]


def _candidate_rects(width: float, height: float) -> dict[str, tuple[float, float, float, float]]:
    margin = .05
    return {
        "top_left": (margin, margin, width, height),
        "top_center": ((1 - width) / 2, margin, width, height),
        "top_right": (1 - margin - width, margin, width, height),
        "left_center": (margin, (1 - height) / 2, width, height),
        "right_center": (1 - margin - width, (1 - height) / 2, width, height),
        "bottom_left": (margin, 1 - margin - height, width, height),
        "bottom_center": ((1 - width) / 2, 1 - margin - height, width, height),
        "bottom_right": (1 - margin - width, 1 - margin - height, width, height),
    }


def _overlap_ratio(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    width = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    height = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return width * height / max(.0001, min(aw * ah, bw * bh))


def _safe_margin_penalty(rect: tuple[float, float, float, float]) -> float:
    x, y, width, height = rect
    return 20 if x < .035 or y < .035 or x + width > .965 or y + height > .965 else 0
