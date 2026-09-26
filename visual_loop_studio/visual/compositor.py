from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from models.visual_project import ElementLayout, TextStyle, VisualProject, default_element_layouts, default_text_styles
from utils.media import is_still_image
from utils.paths import CACHE_DIR, ensure_app_dirs, ffmpeg_filter_path
from visual.animation import background_filter
from visual.effects import ffmpeg_effect_filters
from visual.filters import ffmpeg_color_filters, ffmpeg_manual_filters
from visual.fonts import windows_font_file
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


def _font_option(custom_font: str = "", family: str = "Segoe UI", bold: bool = False, italic: bool = False) -> str:
    selected = windows_font_file(family, bold, italic)
    candidates = ([Path(custom_font)] if custom_font else []) + ([Path(selected)] if selected else []) + [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    font = next((item for item in candidates if item.is_file()), None)
    if font:
        return f"fontfile='{ffmpeg_filter_path(str(font))}':"
    escaped_family = family.replace("\\", r"\\").replace("'", r"\'").replace(":", r"\:")
    return f"font='{escaped_family}':" if escaped_family else ""


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


def _text_style(project: VisualProject, kind: str) -> TextStyle:
    style = project.text_styles.get(kind) if project.text_styles else None
    return style if isinstance(style, TextStyle) else default_text_styles().get(kind, TextStyle())


def _ffmpeg_color(value: str, fallback: str = "FFFFFF") -> str:
    color = str(value or "").strip()
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", color):
        return "0x" + color.lstrip("#").upper()
    return color or f"0x{fallback}"


def _gradient_points(direction: str, width: int, height: int) -> tuple[int, int, int, int]:
    right = max(1, width - 1)
    bottom = max(1, height - 1)
    points = {
        "left_to_right": (0, height // 2, right, height // 2),
        "right_to_left": (right, height // 2, 0, height // 2),
        "top_to_bottom": (width // 2, 0, width // 2, bottom),
        "bottom_to_top": (width // 2, bottom, width // 2, 0),
        "diagonal_down": (0, 0, right, bottom),
        "diagonal_up": (0, bottom, right, 0),
    }
    return points.get(str(direction or "").lower(), points["left_to_right"])
def _font_size(project: VisualProject, kind: str, output_height: int, item_height: int) -> int:
    ratios = {"title": 1.0, "subtitle": .60, "artist": .52, "custom_text": .48, "playlist": .38}
    style = _text_style(project, kind)
    logical_size = round(project.font_size * ratios.get(kind, .5)) if project.font_size else style.font_size
    scaled_size = round(logical_size * output_height / 1080)
    return max(12, min(round(item_height * .88), scaled_size))


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
    if project.audio:
        inputs += ["-stream_loop", "-1", "-i", project.audio]
        output_audio_index = input_index
        input_index += 1

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
            if project.waveform_media:
                wave_still = is_still_image(project.waveform_media)
                if wave_still:
                    inputs += ["-loop", "1", "-framerate", str(project.fps), "-i", project.waveform_media]
                else:
                    inputs += ["-stream_loop", "-1", "-i", project.waveform_media]
                key_white = ",colorkey=0xFFFFFF:0.18:0.08" if project.waveform_remove_white else ""
                filters.append(
                    f"[{input_index}:v]setpts=PTS-STARTPTS,fps={project.fps},"
                    f"scale={item_width}:{item_height}:force_original_aspect_ratio=decrease,format=rgba{key_white},"
                    f"pad={item_width}:{item_height}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
                    f"colorchannelmixer=aa={layout.opacity:.4f}{_rotate_filter(layout)}[{source_label}]"
                )
                input_index += 1
            elif project.waveform not in {"FILE", "NONE"}:
                audio_index = output_audio_index
                if audio_index is None:
                    audio_index = input_index
                    inputs += ["-f", "lavfi", "-i", "sine=frequency=110:sample_rate=44100"]
                    input_index += 1
                filters.append(
                    f"[{audio_index}:a]{waveform_filter(project.waveform, item_width, item_height)},"
                    f"colorchannelmixer=aa={layout.opacity:.4f}{_rotate_filter(layout)}[{source_label}]"
                )
            else:
                continue
        elif kind in {"title", "subtitle", "artist", "custom_text", "playlist"}:
            text = _element_text(project, kind)
            if not text:
                continue
            style = _text_style(project, kind)
            font_size = _font_size(project, kind, height, item_height)
            color = _ffmpeg_color(style.color_start or project.text_color)
            opacity = max(0, min(100, project.text_opacity)) / 100 * layout.opacity
            border = max(0, project.stroke_width)
            shadow = ":shadowx=2:shadowy=2:shadowcolor=black@.65" if project.text_shadow else ""
            align = "center" if layout.anchor in {"center", "top_center", "bottom_center"} else "left"
            text_x = "(w-text_w)/2" if align == "center" else "4"
            text_options = (
                f"{_font_option(project.font_file, style.font_family, style.bold, style.italic)}"
                f"textfile='{_text_file(text)}':reload=0:x={text_x}:y=4:"
                f"fontsize={font_size}:line_spacing={max(2, font_size // 4)}"
            )
            if style.color_mode == "linear_gradient":
                x0, y0, x1, y1 = _gradient_points(style.gradient_direction, item_width, item_height)
                shadow_label = f"textshadow{serial}"
                mask_source_label = f"textmasksource{serial}"
                mask_label = f"textmask{serial}"
                gradient_label = f"textgradient{serial}"
                gradient_fill_label = f"textgradientfill{serial}"
                filters.append(
                    f"color=c=black@0.0:s={item_width}x{item_height}:r={project.fps}:d=60,format=rgba,"
                    f"drawtext={text_options}:fontcolor=black@0.0:borderw={border}:bordercolor=black@.7{shadow}[{shadow_label}]"
                )
                filters.append(
                    f"color=c=black@0.0:s={item_width}x{item_height}:r={project.fps}:d=60,format=rgba,"
                    f"drawtext={text_options}:fontcolor=white@{opacity:.4f}:borderw=0[{mask_source_label}]"
                )
                filters.append(f"[{mask_source_label}]alphaextract[{mask_label}]")
                filters.append(
                    f"gradients=s={item_width}x{item_height}:r={project.fps}:d=60:c0={_ffmpeg_color(style.color_start)}:"
                    f"c1={_ffmpeg_color(style.color_end)}:n=2:x0={x0}:y0={y0}:x1={x1}:y1={y1}:t=linear:speed=0,format=rgba[{gradient_label}]"
                )
                filters.append(f"[{gradient_label}][{mask_label}]alphamerge,format=rgba[{gradient_fill_label}]")
                filters.append(
                    f"[{shadow_label}][{gradient_fill_label}]overlay=x=0:y=0:format=auto{_rotate_filter(layout)}[{source_label}]"
                )
                sources.append((layout.z_order, source_label, element_id, layout))
                continue
            filters.append(
                f"color=c=black@0.0:s={item_width}x{item_height}:r={project.fps}:d=60,format=rgba,"
                f"drawtext={text_options}:fontcolor={color}@{opacity:.4f}:borderw={border}:"
                f"bordercolor=black@.7{shadow}{_rotate_filter(layout)}[{source_label}]"
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
        kind = _kind(element_id)
        if layout.blend_mode in {"lighten", "screen", "addition"} and kind in {"artwork", "logo", "platform_icons", "waveform"}:
            dark_label = f"dark{serial}"
            positioned_label = f"positioned{serial}"
            filters.append(f"color=c=black:s={width}x{height}:r={project.fps}:d=60,format=rgba[{dark_label}]")
            filters.append(
                f"[{dark_label}][{source_label}]overlay=x='{x}':y='{y_expression}':"
                f"eval=frame:format=auto[{positioned_label}]"
            )
            filters.append(
                f"[{current}][{positioned_label}]blend=all_mode={layout.blend_mode}:shortest=1[{next_label}]"
            )
        else:
            filters.append(f"[{current}][{source_label}]overlay=x='{x}':y='{y_expression}':eval=frame:format=auto[{next_label}]")
        current = next_label

    if project.effect_overlay:
        overlay_still = is_still_image(project.effect_overlay)
        if overlay_still:
            inputs += ["-loop", "1", "-framerate", str(project.fps), "-i", project.effect_overlay]
        else:
            inputs += ["-stream_loop", "-1", "-i", project.effect_overlay]
        opacity = min(1.0, max(0.0, project.effect_overlay_opacity / 100))
        blend = str(project.effect_overlay_blend or "lighten").lower()
        overlay_label = "effectoverlay"
        key_white = ",colorkey=0xFFFFFF:0.18:0.08" if project.effect_overlay_remove_white else ""
        base_chain = (
            f"[{input_index}:v]setpts=PTS-STARTPTS,fps={project.fps},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},format=rgba{key_white}"
        )
        if blend == "normal":
            filters.append(f"{base_chain},colorchannelmixer=aa={opacity:.4f}[{overlay_label}]")
            filters.append(
                f"[{current}][{overlay_label}]overlay=x=0:y=0:eval=frame:format=auto:shortest=1[effectlayer]"
            )
        else:
            blend = blend if blend in {"lighten", "screen", "addition"} else "lighten"
            # Blend filters operate on RGB even where alpha is zero. Premultiply
            # first so transparent MOV pixels cannot wash the whole frame white.
            filters.append(f"{base_chain},premultiply=inplace=1[{overlay_label}]")
            filters.append(
                f"[{current}][{overlay_label}]blend=all_mode={blend}:all_opacity={opacity:.4f}:"
                "shortest=1[effectlayer]"
            )
        current = "effectlayer"
        input_index += 1

    post = ffmpeg_effect_filters(project.effects) + ffmpeg_color_filters(project.color_filter) + ffmpeg_manual_filters(project.manual_color) + lut_filter(project.lut)
    post += ["format=yuv420p"]
    filters.append(f"[{current}]{','.join(post)}[vout]")
    return VisualCommand(inputs, ";".join(filters), "[vout]", f"{output_audio_index}:a" if output_audio_index is not None else None)
