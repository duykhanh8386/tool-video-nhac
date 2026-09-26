from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from models.visual_project import ElementLayout, VisualProject, default_element_layouts
from utils.media import is_still_image
from utils.paths import CACHE_DIR, ensure_app_dirs, ffmpeg_filter_path
from visual.animation import background_filter
from visual.effects import ffmpeg_effect_filters
from visual.filters import ffmpeg_color_filters, ffmpeg_manual_filters
from visual.layout import to_top_left_rect
from visual.lut import lut_filter
from visual.waveform import waveform_filter


@dataclass
class VisualCommand:
    inputs: list[str]
    filter_complex: str
    video_map: str
    audio_map: str | None


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:")
        .replace("%", r"\%").replace("\r\n", r"\\n").replace("\n", r"\\n")
    )


def _text_file(value: str) -> str:
    ensure_app_dirs()
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    path = CACHE_DIR / f"text_{digest}.txt"
    if not path.exists():
        path.write_text(value, encoding="utf-8")
    return ffmpeg_filter_path(str(path))


def _font_option(custom_font: str = "") -> str:
    candidates = ([Path(custom_font)] if custom_font else []) + [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    font = next((item for item in candidates if item.is_file()), None)
    return f"fontfile='{ffmpeg_filter_path(str(font))}':" if font else ""


def _kind(element_id: str) -> str:
    return element_id.split("_copy_", 1)[0]


def _layout_for(project: VisualProject, element_id: str) -> ElementLayout:
    return project.elements.get(element_id) or default_element_layouts().get(_kind(element_id), ElementLayout(.1, .1, .2, .1))


def _pixel_rect(layout: ElementLayout, width: int, height: int) -> tuple[int, int, int, int]:
    x, y, item_width, item_height = to_top_left_rect(layout)
    return (
        round(x * width), round(y * height),
        max(8, round(item_width * width)), max(8, round(item_height * height)),
    )


def _rotate_filter(layout: ElementLayout) -> str:
    angle = layout.rotation % 360
    if abs(angle) < .01:
        return ""
    return f",rotate={angle:.3f}*PI/180:c=none:ow=rotw(iw):oh=roth(ih)"


def _element_text(project: VisualProject, kind: str) -> str:
    return {
        "title": project.title, "subtitle": project.subtitle, "artist": project.artist,
        "custom_text": project.custom_text, "playlist": project.playlist,
    }.get(kind, "")


def _font_size(project: VisualProject, kind: str, canvas_width: int, canvas_height: int) -> int:
    base = project.font_size or max(20, round(canvas_width * .115))
    ratios = {"title": 1.0, "subtitle": .60, "artist": .52, "custom_text": .48, "playlist": .38}
    return max(12, min(round(canvas_height * .72), round(base * ratios.get(kind, .5))))


def build_visual_graph(project: VisualProject, width: int, height: int) -> VisualCommand:
    inputs: list[str] = []
    still = is_still_image(project.background)
    if still:
        inputs += ["-loop", "1", "-framerate", str(project.fps), "-i", project.background]
    else:
        inputs += ["-stream_loop", "-1", "-i", project.background]
    filters: list[str] = [f"[0:v]{background_filter(project.animation, width, height, project.fps, still)},format=rgba[base]"]
    input_index = 1
    sources: list[tuple[int, str, str, ElementLayout]] = []
    output_audio_index: int | None = None

    visible_elements = sorted(
        ((element_id, layout) for element_id, layout in project.elements.items() if layout.visible and layout.opacity > 0),
        key=lambda item: item[1].z_order,
    )
    for serial, (element_id, layout) in enumerate(visible_elements):
        kind = _kind(element_id)
        x, y, item_width, item_height = _pixel_rect(layout, width, height)
        source_label = f"src{serial}"
        if kind in {"artwork", "logo", "platform_icons"}:
            source_path = getattr(project, kind, "")
            if not source_path:
                continue
            inputs += ["-loop", "1", "-framerate", str(project.fps), "-i", source_path]
            chain = (
                f"[{input_index}:v]scale={item_width}:{item_height}:force_original_aspect_ratio=decrease,"
                f"format=rgba,colorchannelmixer=aa={layout.opacity:.4f}{_rotate_filter(layout)}[{source_label}]"
            )
            filters.append(chain)
            input_index += 1
        elif kind == "waveform":
            if project.waveform == "NONE":
                continue
            audio_index = input_index
            if project.audio:
                inputs += ["-stream_loop", "-1", "-i", project.audio]
                if output_audio_index is None:
                    output_audio_index = audio_index
            else:
                inputs += ["-f", "lavfi", "-i", "sine=frequency=110:sample_rate=44100"]
            input_index += 1
            filters.append(
                f"[{audio_index}:a]{waveform_filter(project.waveform, item_width, item_height)},"
                f"colorchannelmixer=aa={layout.opacity:.4f}{_rotate_filter(layout)}[{source_label}]"
            )
        elif kind in {"title", "subtitle", "artist", "custom_text", "playlist"}:
            text = _element_text(project, kind)
            if not text:
                continue
            font_size = _font_size(project, kind, item_width, item_height)
            color = project.text_color.strip().lstrip("#") or "FFFFFF"
            opacity = max(0, min(100, project.text_opacity)) / 100 * layout.opacity
            border = max(0, project.stroke_width)
            shadow = ":shadowx=2:shadowy=2:shadowcolor=black@.65" if project.text_shadow else ""
            align = "center" if layout.anchor in {"center", "top_center", "bottom_center"} else "left"
            text_x = "(w-text_w)/2" if align == "center" else "4"
            filters.append(
                f"color=c=black@0.0:s={item_width}x{item_height}:r={project.fps}:d=60,format=rgba,"
                f"drawtext={_font_option(project.font_file)}textfile='{_text_file(text)}':reload=0:x={text_x}:y=4:"
                f"fontsize={font_size}:fontcolor=0x{color}@{opacity:.4f}:borderw={border}:"
                f"bordercolor=black@.7:line_spacing={max(2, font_size // 4)}{shadow}"
                f"{_rotate_filter(layout)}[{source_label}]"
            )
        else:
            continue
        overlay_x, overlay_y = str(x), str(y)
        if kind == "artwork" and project.artwork_motion.upper() in {"FLOAT", "BREATH", "BEAT_PULSE"}:
            overlay_y = f"{y}+{max(2, round(height * .0075))}*sin(2*PI*t/5)"
        sources.append((layout.z_order, source_label, element_id, layout))

    current = "base"
    for serial, (_z, source_label, element_id, layout) in enumerate(sorted(sources, key=lambda item: item[0])):
        x, y, _item_width, _item_height = _pixel_rect(layout, width, height)
        y_expression = str(y)
        if _kind(element_id) == "artwork" and project.artwork_motion.upper() in {"FLOAT", "BREATH", "BEAT_PULSE"}:
            y_expression = f"{y}+{max(2, round(height * .0075))}*sin(2*PI*t/5)"
        next_label = f"layer{serial}"
        filters.append(f"[{current}][{source_label}]overlay=x='{x}':y='{y_expression}':eval=frame:format=auto[{next_label}]")
        current = next_label

    post = ffmpeg_effect_filters(project.effects) + ffmpeg_color_filters(project.color_filter) + ffmpeg_manual_filters(project.manual_color) + lut_filter(project.lut)
    post += ["format=yuv420p"]
    filters.append(f"[{current}]{','.join(post)}[vout]")
    return VisualCommand(inputs, ";".join(filters), "[vout]", f"{output_audio_index}:a" if project.audio and output_audio_index is not None else None)
