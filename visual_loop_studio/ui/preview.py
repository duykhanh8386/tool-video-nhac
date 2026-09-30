from __future__ import annotations

import math
import subprocess
import unicodedata
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QCloseEvent, QFont, QFontMetricsF, QImage, QLinearGradient, QMouseEvent, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient, QWheelEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from models.visual_project import ElementLayout, TextStyle, default_element_layouts, default_text_styles
from utils.media import is_still_image
from visual.layout import to_top_left_rect, update_from_top_left
from utils.process import hidden_process_kwargs
from visual.particles import particle_positions


def _preview_graphemes(value: str) -> list[str]:
    result: list[str] = []
    current = ""
    for char in value:
        attached = bool(current) and (
            bool(unicodedata.combining(char))
            or char in {"\ufe0e", "\ufe0f", "\u200d"}
            or current.endswith("\u200d")
        )
        if attached:
            current += char
        else:
            if current:
                result.append(current)
            current = char
    if current:
        result.append(current)
    return result


class CompositionPreview(QWidget):
    element_selected = Signal(str)
    element_activated = Signal(str)
    layout_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.background = ""
        self.background_motion = "STATIC"
        self.ffmpeg_path = "ffmpeg"
        self.artwork = ""
        self.logo = ""
        self.platform_icons = ""
        self.title = "Visual Loop Studio"
        self.subtitle = "Drag elements directly in this preview"
        self.artist = ""
        self.custom_text = ""
        self.playlist = ""
        self.text_color = "#FFFFFF"
        self.text_styles: dict[str, TextStyle] = default_text_styles()
        self.waveform = "Smooth sine waveform"
        self.waveform_media = ""
        self.waveform_remove_white = True
        self.effect_overlay = ""
        self.effect_overlay_blend = "lighten"
        self.effect_overlay_opacity = 1.0
        self.effect_overlay_remove_white = False
        self.effects: list[object] = []
        self.color_filter = "NONE"
        self.manual_color: dict[str, float] = {}
        self.elements: dict[str, ElementLayout] = default_element_layouts()
        self.selected_element = "title"
        self.subject_bbox: tuple[float, float, float, float] | None = None
        self.show_guides = True
        self.snap_center = True
        self.snap_safe_margin = True
        self.snap_elements = True
        self.snap_grid = False
        self._time = 0.0
        self._playing = True
        self._images: dict[str, QPixmap] = {}
        self._video_readers: dict[str, _VideoPreviewReader] = {}
        self._hit_rects: dict[str, QRectF] = {}
        self._canvas = QRectF()
        self._action = ""
        self._drag_start = QPointF()
        self._start_rect = (0.0, 0.0, 0.0, 0.0)
        self._press_element = ""
        self._drag_moved = False
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def set_source(self, kind: str, path: str) -> None:
        previous = str(getattr(self, kind, "") or "")
        setattr(self, kind, path)
        if previous and previous != path:
            self._release_video_if_unused(previous)
        if path and Path(path).is_file():
            if is_still_image(path):
                pixmap = QPixmap(path)
            else:
                reader = self._video_readers.get(path)
                if reader is None:
                    reader = _VideoPreviewReader(path, self.ffmpeg_path)
                    self._video_readers[path] = reader
                pixmap = reader.frame_at(self._time)
                if pixmap is None or pixmap.isNull():
                    pixmap = _video_thumbnail(path, self.ffmpeg_path)
            if not pixmap.isNull():
                self._images[path] = pixmap
        self.update()

    def set_layouts(self, layouts: dict[str, ElementLayout]) -> None:
        self.elements = layouts
        if self.selected_element not in layouts and layouts:
            self.selected_element = next(iter(layouts))
        self.update()

    def select_element(self, element_id: str) -> None:
        if element_id in self.elements:
            self.selected_element = element_id
            self.element_selected.emit(element_id)
            self.update()

    def duplicate_selected(self) -> str | None:
        if self.selected_element not in self.elements:
            return None
        base = self.selected_element.split("_copy_", 1)[0]
        index = 1
        while f"{base}_copy_{index}" in self.elements:
            index += 1
        copy_id = f"{base}_copy_{index}"
        layout = ElementLayout(**asdict(self.elements[self.selected_element]))
        layout.x = min(.95, layout.x + .035)
        layout.y = min(.95, layout.y + .035)
        layout.locked = False
        layout.z_order += index
        self.elements[copy_id] = layout
        self.select_element(copy_id)
        self.layout_changed.emit()
        return copy_id

    def delete_selected_copy(self) -> bool:
        if "_copy_" not in self.selected_element:
            return False
        del self.elements[self.selected_element]
        self.selected_element = next(iter(self.elements), "")
        self.element_selected.emit(self.selected_element)
        self.layout_changed.emit()
        self.update()
        return True

    def align_selected(self, axis: str) -> None:
        layout = self.elements.get(self.selected_element)
        if not layout or layout.locked:
            return
        rect = list(to_top_left_rect(layout))
        if axis == "horizontal":
            rect[0] = .5 - rect[2] / 2
        else:
            rect[1] = .5 - rect[3] / 2
        update_from_top_left(layout, tuple(rect))
        self.layout_changed.emit()
        self.update()

    def play(self) -> None:
        self._playing = True
        self.timer.start()

    def pause(self) -> None:
        self._playing = False

    def stop(self) -> None:
        self._playing = False
        self._time = 0
        self._refresh_video_frames()
        self.update()

    def restart(self) -> None:
        self._time = 0
        self._refresh_video_frames()
        self.play()

    def _tick(self) -> None:
        if self._playing:
            self._time = (self._time + .033) % 3600
            if self.isVisible():
                self._refresh_video_frames()
            self.update()

    def _refresh_video_frames(self) -> None:
        active = {
            str(getattr(self, name, "") or "")
            for name in ("background", "artwork", "logo", "platform_icons", "waveform_media", "effect_overlay")
        }
        for path in active:
            reader = self._video_readers.get(path)
            if not reader:
                continue
            pixmap = reader.frame_at(self._time)
            if pixmap is not None and not pixmap.isNull():
                self._images[path] = pixmap

    def _release_video_if_unused(self, path: str) -> None:
        active = {
            str(getattr(self, name, "") or "")
            for name in ("background", "artwork", "logo", "platform_icons", "waveform_media", "effect_overlay")
        }
        if path in active:
            return
        reader = self._video_readers.pop(path, None)
        if reader:
            reader.close()
        self._images.pop(path, None)

    def closeEvent(self, event: QCloseEvent) -> None:
        for reader in self._video_readers.values():
            reader.close()
        self._video_readers.clear()
        super().closeEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        canvas = _fit_16_9(rect.width(), rect.height())
        self._canvas = canvas
        painter.fillRect(rect, QColor("#090d18"))
        painter.setClipRect(canvas)
        phase = (self._time % 10) / 10
        self._paint_background(painter, canvas, phase)
        self._paint_filter(painter, canvas)
        self._hit_rects.clear()
        for element_id, layout in sorted(self.elements.items(), key=lambda item: item[1].z_order):
            if layout.visible and layout.opacity > 0:
                self._paint_element(painter, canvas, element_id, layout)
        self._paint_effect_overlay(painter, canvas)
        self._paint_effects(painter, canvas, phase)
        painter.setClipping(False)
        if self.show_guides:
            self._paint_guides(painter, canvas)
        self._paint_selection(painter)

    def _paint_background(self, painter: QPainter, rect: QRectF, phase: float) -> None:
        animate_frame = is_still_image(self.background) and self.background_motion.upper() not in {"NONE", "STATIC"}
        zoom = 1 + .02 * (1 - math.cos(math.tau * phase)) if animate_frame else 1.0
        bg = self._images.get(self.background)
        if bg and not bg.isNull():
            scaled = bg.scaled(round(rect.width() * zoom), round(rect.height() * zoom), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            x = rect.center().x() - scaled.width() / 2 + (math.sin(math.tau * phase) * 4 if animate_frame else 0)
            y = rect.center().y() - scaled.height() / 2 + (math.sin(math.tau * phase) * 3 if animate_frame else 0)
            painter.drawPixmap(round(x), round(y), scaled)
        else:
            painter.fillRect(rect, QColor("#14213d"))
            painter.setPen(QColor("#64748b"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Hãy chọn ảnh hoặc video nền")

    def _paint_effect_overlay(self, painter: QPainter, rect: QRectF) -> None:
        pixmap = self._images.get(self.effect_overlay)
        if pixmap is None or pixmap.isNull() or self.effect_overlay_opacity <= 0:
            return
        if self.effect_overlay_remove_white:
            pixmap = _white_key_pixmap(pixmap)
        scaled = pixmap.scaled(
            round(rect.width()),
            round(rect.height()),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        source = QRectF(
            max(0, (scaled.width() - rect.width()) / 2),
            max(0, (scaled.height() - rect.height()) / 2),
            min(scaled.width(), rect.width()),
            min(scaled.height(), rect.height()),
        )
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, float(self.effect_overlay_opacity))))
        _set_painter_blend(painter, self.effect_overlay_blend)
        painter.drawPixmap(rect, scaled, source)
        painter.restore()

    def _paint_element(self, painter: QPainter, canvas: QRectF, element_id: str, layout: ElementLayout) -> None:
        x, y, width, height = to_top_left_rect(layout)
        item_rect = QRectF(canvas.left() + x * canvas.width(), canvas.top() + y * canvas.height(), width * canvas.width(), height * canvas.height())
        if element_id.split("_copy_", 1)[0] == "artwork" and self._playing:
            item_rect.translate(0, math.sin(math.tau * self._time / 5) * 7)
        self._hit_rects[element_id] = item_rect
        painter.save()
        painter.setOpacity(layout.opacity)
        painter.translate(item_rect.center())
        painter.rotate(layout.rotation)
        local = QRectF(-item_rect.width() / 2, -item_rect.height() / 2, item_rect.width(), item_rect.height())
        kind = element_id.split("_copy_", 1)[0]
        if kind in {"artwork", "logo", "platform_icons", "waveform"}:
            _set_painter_blend(painter, layout.blend_mode)
        if kind in {"artwork", "logo", "platform_icons"}:
            path = getattr(self, kind, "")
            pixmap = self._images.get(path)
            if pixmap and not pixmap.isNull():
                scaled = pixmap.scaled(round(local.width()), round(local.height()), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(round(-scaled.width() / 2), round(-scaled.height() / 2), scaled)
            else:
                _placeholder(painter, local, kind.replace("_", " ").title())
        elif kind == "waveform":
            pixmap = self._images.get(self.waveform_media)
            if pixmap and not pixmap.isNull():
                if self.waveform_remove_white:
                    pixmap = _white_key_pixmap(pixmap)
                scaled = pixmap.scaled(round(local.width()), round(local.height()), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(round(-scaled.width() / 2), round(-scaled.height() / 2), scaled)
            elif self.waveform != "FILE":
                self._paint_wave(painter, local)
        else:
            self._paint_text_element(painter, local, kind)
        painter.restore()

    def _paint_text_element(self, painter: QPainter, rect: QRectF, kind: str) -> None:
        text = {
            "title": self.title, "subtitle": self.subtitle, "artist": self.artist,
            "custom_text": self.custom_text, "playlist": self.playlist,
        }.get(kind, "")
        if not text:
            return
        style = self.text_styles.get(kind) or default_text_styles().get(kind, TextStyle())
        size = max(6, min(round(rect.height() * .88), round(style.font_size * self._canvas.height() / 1080)))
        font = QFont(style.font_family or "Segoe UI")
        font.setPixelSize(size)
        font.setBold(style.bold)
        font.setItalic(style.italic)
        painter.setFont(font)
        color = QColor(style.color_start or self.text_color)
        if not color.isValid():
            color = QColor("white")
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
        if style.color_mode == "linear_gradient":
            points = {
                "left_to_right": (rect.left(), rect.center().y(), rect.right(), rect.center().y()),
                "right_to_left": (rect.right(), rect.center().y(), rect.left(), rect.center().y()),
                "top_to_bottom": (rect.center().x(), rect.top(), rect.center().x(), rect.bottom()),
                "bottom_to_top": (rect.center().x(), rect.bottom(), rect.center().x(), rect.top()),
                "diagonal_down": (rect.left(), rect.top(), rect.right(), rect.bottom()),
                "diagonal_up": (rect.left(), rect.bottom(), rect.right(), rect.top()),
            }
            x0, y0, x1, y1 = points.get(style.gradient_direction, points["left_to_right"])
            gradient = QLinearGradient(x0, y0, x1, y1)
            gradient.setColorAt(0, color)
            end_color = QColor(style.color_end)
            gradient.setColorAt(1, end_color if end_color.isValid() else color)
            fill_pen = QPen(QBrush(gradient), 1)
        else:
            fill_pen = QPen(color)
        if str(style.animation or "none").lower() != "none":
            self._paint_animated_text(painter, rect, text, font, fill_pen, style.animation)
            return
        painter.setPen(QPen(QColor(0, 0, 0, 150), 3))
        painter.drawText(rect.adjusted(2, 2, -2, -2), flags, text)
        painter.setPen(fill_pen)
        painter.drawText(rect, flags, text)

    def _paint_animated_text(
        self,
        painter: QPainter,
        rect: QRectF,
        text: str,
        font: QFont,
        fill_pen: QPen,
        animation: str,
    ) -> None:
        metrics = QFontMetricsF(font)
        padding = 2.0
        available = max(8.0, rect.width() - padding * 2)
        rows: list[list[tuple[str, float, int]]] = []
        sequence = 0
        for source_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            row: list[tuple[str, float, int]] = []
            row_width = 0.0
            for glyph in _preview_graphemes(source_line):
                advance = max(1.0, metrics.horizontalAdvance(glyph))
                if row and row_width + advance > available:
                    rows.append(row)
                    row = []
                    row_width = 0.0
                row.append((glyph, advance, sequence))
                row_width += advance
                sequence += 1
            rows.append(row)
            sequence += 1

        amplitude = max(2.0, font.pixelSize() * .13)
        line_height = max(metrics.height(), font.pixelSize() * 1.22)
        base_opacity = painter.opacity()
        name = str(animation or "none").lower()
        for row_index, row in enumerate(rows):
            cursor = rect.left() + padding
            baseline = rect.top() + padding + metrics.ascent() + row_index * line_height
            for glyph, advance, index in row:
                if glyph.isspace():
                    cursor += advance
                    continue
                phase = index * .62
                dx = 0.0
                dy = 0.0
                alpha = 1.0
                if name == "wave":
                    dy = amplitude * math.sin(math.tau * self._time * .90 + phase)
                elif name == "bounce":
                    dy = -amplitude * abs(math.sin(math.pi * self._time * 1.70 + phase))
                elif name == "float":
                    dx = max(1.0, amplitude / 3) * math.sin(math.tau * self._time * .45 + index * 1.17)
                    dy = amplitude * math.sin(math.tau * self._time * .55 + phase)
                elif name == "jitter":
                    shake = max(1.0, font.pixelSize() * .025)
                    dx = shake * math.sin(math.tau * self._time * 7.0 + index * 2.10)
                    dy = shake * math.sin(math.tau * self._time * 9.0 + index * 1.30)
                elif name == "typewriter":
                    reveal_time = self._time % 8
                    if reveal_time < min(4.5, index * .075) or reveal_time >= 7.5:
                        cursor += advance
                        continue
                elif name == "neon":
                    alpha = .70 + .30 * math.sin(math.tau * self._time * 2.2 + index * 1.70)
                point = QPointF(cursor + dx, baseline + dy)
                painter.save()
                painter.setOpacity(base_opacity * alpha)
                painter.setPen(QPen(QColor(0, 0, 0, 150), 3))
                painter.drawText(point + QPointF(2, 2), glyph)
                painter.setPen(fill_pen)
                painter.drawText(point, glyph)
                painter.restore()
                cursor += advance

    def _paint_wave(self, painter: QPainter, rect: QRectF) -> None:
        if self.waveform == "NONE":
            return
        path = QPainterPath()
        center = rect.center().y()
        for index in range(121):
            x = rect.left() + rect.width() * index / 120
            amp = rect.height() * .30 * (.45 + .55 * math.sin(index * .17 + self._time * 1.3) ** 2)
            y = center + math.sin(index * .31 + self._time * 3.2) * amp
            path.moveTo(x, center) if index == 0 else path.lineTo(x, y)
        painter.setPen(QPen(QColor("#63e6ff"), max(2, rect.width() / 190)))
        painter.drawPath(path)

    def _paint_filter(self, painter: QPainter, rect: QRectF) -> None:
        colors = {"WARM": (255, 120, 50, 24), "COOL": (50, 140, 255, 25), "PURPLE": (170, 70, 255, 28), "BLUE": (35, 100, 255, 28), "HEALING": (50, 220, 180, 18), "CHRISTMAS": (220, 30, 40, 17), "VINTAGE": (170, 110, 45, 26), "LOFI": (120, 70, 90, 20)}
        if self.color_filter in colors:
            painter.fillRect(rect, QColor(*colors[self.color_filter]))
        if self.color_filter == "BLACK_AND_WHITE":
            painter.fillRect(rect, QColor(80, 80, 80, 70))
        brightness = float(self.manual_color.get("brightness", 0))
        if brightness:
            painter.fillRect(rect, QColor(255, 255, 255, min(100, round(brightness * .8))) if brightness > 0 else QColor(0, 0, 0, min(100, round(-brightness * .8))))
        temperature = float(self.manual_color.get("temperature", 0))
        if temperature:
            painter.fillRect(rect, QColor(255, 95, 35, min(55, round(temperature * .45))) if temperature > 0 else QColor(40, 110, 255, min(55, round(-temperature * .45))))

    def _paint_effects(self, painter: QPainter, rect: QRectF, phase: float) -> None:
        amounts: dict[str, float] = {}
        for item in self.effects:
            name = str(getattr(item, "name", item)).upper()
            amount = max(0.0, min(1.0, float(getattr(item, "intensity", .35))))
            amounts[name] = max(amounts.get(name, 0.0), amount)
        upper = set(amounts)
        painter.save()
        if upper & {"SNOW", "SPARKLES", "DUST", "STAR_FIELD", "FLOATING_LIGHTS"}:
            amount = max(amounts.get(name, 0) for name in upper & {"SNOW", "SPARKLES", "DUST", "STAR_FIELD", "FLOATING_LIGHTS"})
            painter.setPen(Qt.PenStyle.NoPen)
            count = round(35 + amount * 130)
            for x, y, size in particle_positions(count, int(rect.width()), int(rect.height()), phase):
                alpha = round((70 if "SNOW" in upper else 45) + amount * 120)
                color = QColor(255, 245, 190, alpha) if "FLOATING_LIGHTS" in upper else QColor(255, 255, 255, alpha)
                painter.setBrush(color)
                radius = size * (1 + amount * 1.5)
                painter.drawEllipse(QRectF(rect.left() + x, rect.top() + y, radius, radius))
        if "RAIN" in upper:
            amount = amounts["RAIN"]
            painter.setPen(QPen(QColor(190, 220, 255, round(55 + amount * 150)), max(1, round(1 + amount * 2))))
            for x, y, _ in particle_positions(round(50 + amount * 160), int(rect.width()), int(rect.height()), phase * 3):
                length = round(9 + amount * 24)
                painter.drawLine(round(rect.left() + x), round(rect.top() + y), round(rect.left() + x - length * .3), round(rect.top() + y + length))
        if upper & {"FILM_GRAIN", "TV_STATIC", "VHS_NOISE", "PIXEL_NOISE"}:
            amount = max(amounts.get(name, 0) for name in upper & {"FILM_GRAIN", "TV_STATIC", "VHS_NOISE", "PIXEL_NOISE"})
            painter.setPen(QColor(255, 255, 255, round(12 + amount * 70)))
            for x, y, _ in particle_positions(round(100 + amount * 500), int(rect.width()), int(rect.height()), (phase * 31) % 1):
                painter.drawPoint(round(rect.left() + x), round(rect.top() + y))
        if "SCANLINES" in upper:
            amount = amounts["SCANLINES"]
            painter.setPen(QColor(0, 0, 0, round(20 + amount * 95)))
            y = int(rect.top())
            while y < rect.bottom():
                painter.drawLine(int(rect.left()), y, int(rect.right()), y)
                y += 4
        if upper & {"LIGHT_LEAK", "BLOOM", "SOFT_GLOW"}:
            amount = max(amounts.get(name, 0) for name in upper & {"LIGHT_LEAK", "BLOOM", "SOFT_GLOW"})
            center = QPointF(rect.left() + rect.width() * (.15 + .7 * phase), rect.top() + rect.height() * .28)
            gradient = QRadialGradient(center, rect.width() * (.35 + amount * .35))
            gradient.setColorAt(0, QColor(255, 225, 155, round(70 + amount * 100)))
            gradient.setColorAt(.45, QColor(255, 95, 45, round(30 + amount * 65)))
            gradient.setColorAt(1, QColor(255, 60, 20, 0))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
            painter.fillRect(rect, QBrush(gradient))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if upper & {"BOKEH", "FOG"}:
            amount = max(amounts.get(name, 0) for name in upper & {"BOKEH", "FOG"})
            painter.setPen(Qt.PenStyle.NoPen)
            for x, y, size in particle_positions(round(16 + amount * 42), int(rect.width()), int(rect.height()), (phase * .35) % 1):
                radius = (8 + size * 7) * (1 + amount)
                alpha = round(18 + amount * (55 if "FOG" in upper else 95))
                painter.setBrush(QColor(235, 245, 255, alpha))
                painter.drawEllipse(QRectF(rect.left() + x - radius, rect.top() + y - radius, radius * 2, radius * 2))
        if upper & {"RGB_GLITCH", "CHROMATIC_ABERRATION"}:
            amount = max(amounts.get(name, 0) for name in upper & {"RGB_GLITCH", "CHROMATIC_ABERRATION"})
            shift = max(2, round(rect.width() * (.002 + amount * .008)))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Screen)
            painter.fillRect(QRectF(rect.left() + shift, rect.top(), shift, rect.height()), QColor(255, 20, 60, round(30 + amount * 80)))
            painter.fillRect(QRectF(rect.right() - shift * 2, rect.top(), shift, rect.height()), QColor(20, 160, 255, round(30 + amount * 80)))
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        if upper & {"OLD_FILM", "FILM_SCRATCHES"}:
            amount = max(amounts.get(name, 0) for name in upper & {"OLD_FILM", "FILM_SCRATCHES"})
            if "OLD_FILM" in upper:
                painter.fillRect(rect, QColor(145, 90, 25, round(15 + amount * 55)))
            painter.setPen(QPen(QColor(255, 245, 220, round(30 + amount * 90)), 1))
            for index in range(round(2 + amount * 9)):
                x = rect.left() + ((index * 0.173 + phase * .41) % 1) * rect.width()
                painter.drawLine(QPointF(x, rect.top()), QPointF(x + math.sin(index) * 3, rect.bottom()))
        if "VIGNETTE" in upper:
            amount = amounts["VIGNETTE"]
            painter.setPen(QPen(QColor(0, 0, 0, round(55 + amount * 170)), max(12, rect.width() * (.025 + amount * .09))))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
        painter.restore()

    def _paint_guides(self, painter: QPainter, canvas: QRectF) -> None:
        painter.save()
        painter.setPen(QPen(QColor(99, 230, 255, 75), 1, Qt.PenStyle.DashLine))
        painter.drawRect(canvas.adjusted(canvas.width() * .05, canvas.height() * .05, -canvas.width() * .05, -canvas.height() * .05))
        painter.drawLine(QPointF(canvas.center().x(), canvas.top()), QPointF(canvas.center().x(), canvas.bottom()))
        painter.drawLine(QPointF(canvas.left(), canvas.center().y()), QPointF(canvas.right(), canvas.center().y()))
        if self.subject_bbox:
            x, y, width, height = self.subject_bbox
            subject = QRectF(canvas.left() + x * canvas.width(), canvas.top() + y * canvas.height(), width * canvas.width(), height * canvas.height())
            painter.setPen(QPen(QColor(251, 113, 133, 130), 2, Qt.PenStyle.DashLine))
            painter.drawRect(subject)
            painter.drawText(subject.topLeft() + QPointF(4, 16), "IMPORTANT REGION")
        painter.restore()

    def _paint_selection(self, painter: QPainter) -> None:
        rect = self._hit_rects.get(self.selected_element)
        if not rect:
            return
        locked = self.elements[self.selected_element].locked
        painter.setPen(QPen(QColor("#facc15") if locked else QColor("#22d3ee"), 2, Qt.PenStyle.DashLine if locked else Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        handle = QRectF(rect.right() - 6, rect.bottom() - 6, 12, 12)
        painter.fillRect(handle, QColor("#facc15") if locked else QColor("#22d3ee"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._canvas.contains(event.position()):
            return
        point = event.position()
        selected = next((name for name, _ in sorted(self.elements.items(), key=lambda item: item[1].z_order, reverse=True) if self._hit_rects.get(name, QRectF()).contains(point)), "")
        if not selected:
            return
        self.select_element(selected)
        self._press_element = selected
        self._drag_moved = False
        layout = self.elements[selected]
        if layout.locked:
            return
        rect = self._hit_rects[selected]
        handle = QRectF(rect.right() - 14, rect.bottom() - 14, 20, 20)
        self._action = "resize" if handle.contains(point) else "drag"
        self._drag_start = point
        self._start_rect = to_top_left_rect(layout)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor if self._action == "resize" else Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._action or self.selected_element not in self.elements:
            return
        if (event.position() - self._drag_start).manhattanLength() > 4:
            self._drag_moved = True
        dx = (event.position().x() - self._drag_start.x()) / max(1, self._canvas.width())
        dy = (event.position().y() - self._drag_start.y()) / max(1, self._canvas.height())
        x, y, width, height = self._start_rect
        if self._action == "resize":
            width, height = max(.03, width + dx), max(.03, height + dy)
        else:
            x, y = x + dx, y + dy
        rect = self._snap_rect((x, y, width, height), self.selected_element)
        update_from_top_left(self.elements[self.selected_element], rect)
        self.layout_changed.emit()
        self.update()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        activated = self._press_element if self._press_element and not self._drag_moved else ""
        self._action = ""
        self._press_element = ""
        self._drag_moved = False
        self.unsetCursor()
        if activated:
            self.element_activated.emit(activated)

    def wheelEvent(self, event: QWheelEvent) -> None:
        layout = self.elements.get(self.selected_element)
        if not layout or layout.locked or not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return super().wheelEvent(event)
        layout.rotation = (layout.rotation + (2 if event.angleDelta().y() > 0 else -2)) % 360
        self.layout_changed.emit()
        self.update()
        event.accept()

    def _snap_rect(self, rect: tuple[float, float, float, float], selected: str) -> tuple[float, float, float, float]:
        x, y, width, height = rect
        width, height = min(.94, width), min(.94, height)
        threshold = .014
        if self.snap_grid:
            step = .025
            x, y, width, height = (round(value / step) * step for value in (x, y, width, height))
        if self.snap_center:
            if abs(x + width / 2 - .5) < threshold:
                x = .5 - width / 2
            if abs(y + height / 2 - .5) < threshold:
                y = .5 - height / 2
        if self.snap_safe_margin:
            if abs(x - .05) < threshold:
                x = .05
            if abs(y - .05) < threshold:
                y = .05
            if abs(x + width - .95) < threshold:
                x = .95 - width
            if abs(y + height - .95) < threshold:
                y = .95 - height
        if self.snap_elements:
            for name, layout in self.elements.items():
                if name == selected or not layout.visible:
                    continue
                ox, oy, ow, oh = to_top_left_rect(layout)
                pairs_x = ((x, ox), (x, ox + ow), (x + width, ox), (x + width, ox + ow), (x + width / 2, ox + ow / 2))
                pairs_y = ((y, oy), (y, oy + oh), (y + height, oy), (y + height, oy + oh), (y + height / 2, oy + oh / 2))
                for source, target in pairs_x:
                    if abs(source - target) < threshold:
                        x += target - source
                        break
                for source, target in pairs_y:
                    if abs(source - target) < threshold:
                        y += target - source
                        break
        x = min(.98 - width, max(.02, x))
        y = min(.98 - height, max(.02, y))
        return x, y, width, height


def _placeholder(painter: QPainter, rect: QRectF, label: str) -> None:
    painter.setPen(QPen(QColor(148, 163, 184, 180), 1, Qt.PenStyle.DashLine))
    painter.setBrush(QColor(15, 23, 42, 90))
    painter.drawRoundedRect(rect, 5, 5)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)


def _set_painter_blend(painter: QPainter, mode: str) -> None:
    modes = {
        "lighten": QPainter.CompositionMode.CompositionMode_Lighten,
        "screen": QPainter.CompositionMode.CompositionMode_Screen,
        "addition": QPainter.CompositionMode.CompositionMode_Plus,
    }
    painter.setCompositionMode(modes.get(str(mode or "normal").lower(), QPainter.CompositionMode.CompositionMode_SourceOver))


class _VideoPreviewReader:
    """Decode preview frames through FFmpeg so MOV alpha is not discarded."""

    def __init__(self, path: str, ffmpeg: str = "ffmpeg"):
        self.path = path
        self.ffmpeg = ffmpeg
        self.capture = cv2.VideoCapture(path)
        self.fps = float(self.capture.get(cv2.CAP_PROP_FPS) or 0) or 30.0
        self.frame_count = max(0, int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
        source_width = max(1, int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 960))
        source_height = max(1, int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 540))
        self.width = min(960, source_width)
        self.height = max(1, round(source_height * self.width / source_width))
        self.current_index = -1
        self.fallback_index = -1
        self.process: subprocess.Popen | None = None
        self.last_pixmap: QPixmap | None = None
        self.ffmpeg_failed = False

    def frame_at(self, seconds: float) -> QPixmap | None:
        target = max(0, int(seconds * self.fps))
        if self.frame_count > 0:
            target %= self.frame_count
        if not self.ffmpeg_failed:
            pixmap = self._ffmpeg_frame(target)
            if pixmap is not None and not pixmap.isNull():
                return pixmap
            self.ffmpeg_failed = True
            self._stop_ffmpeg()
        return self._opencv_frame(target)

    def _ffmpeg_frame(self, target: int) -> QPixmap | None:
        if target == self.current_index and self.last_pixmap is not None:
            return self.last_pixmap
        restart_gap = max(6, round(self.fps * .5))
        if self.process is None or target < self.current_index or target - self.current_index > restart_gap:
            self._start_ffmpeg(target / self.fps)
            self.current_index = target - 1
        if self.process is None or self.process.stdout is None:
            return None
        latest: QPixmap | None = None
        while self.current_index < target:
            payload = _read_exact(self.process.stdout, self.width * self.height * 4)
            if payload is None:
                return None
            image = QImage(
                payload,
                self.width,
                self.height,
                self.width * 4,
                QImage.Format.Format_RGBA8888,
            )
            latest = QPixmap.fromImage(image.copy())
            self.current_index += 1
        if latest is not None:
            self.last_pixmap = latest
        return self.last_pixmap

    def _start_ffmpeg(self, seconds: float) -> None:
        self._stop_ffmpeg()
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-stream_loop", "-1",
            "-ss", f"{max(0.0, seconds):.6f}", "-i", self.path,
            "-an", "-sn", "-dn",
            "-vf", f"fps={self.fps:.6f},scale={self.width}:{self.height}:flags=fast_bilinear",
            "-pix_fmt", "rgba", "-f", "rawvideo", "pipe:1",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=self.width * self.height * 8,
                **hidden_process_kwargs(),
            )
        except OSError:
            self.process = None

    def _opencv_frame(self, target: int) -> QPixmap | None:
        if not self.capture.isOpened():
            return None
        if target <= self.fallback_index or target - self.fallback_index > 4:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            self.fallback_index = target - 1
        while self.fallback_index + 1 < target:
            if not self.capture.grab():
                self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.fallback_index = -1
                break
            self.fallback_index += 1
        ok, frame = self.capture.read()
        if not ok or frame is None:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self.fallback_index = -1
            ok, frame = self.capture.read()
            if not ok or frame is None:
                return None
        self.fallback_index += 1
        height, width = frame.shape[:2]
        maximum_width = 960
        if width > maximum_width:
            scale = maximum_width / width
            frame = cv2.resize(frame, (maximum_width, max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
            height, width = frame.shape[:2]
        image = QImage(frame.data, width, height, int(frame.strides[0]), QImage.Format.Format_BGR888)
        return QPixmap.fromImage(image.copy())

    def close(self) -> None:
        self._stop_ffmpeg()
        self.capture.release()

    def _stop_ffmpeg(self) -> None:
        process = self.process
        self.process = None
        if not process:
            return
        if process.stdout:
            process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


def _read_exact(stream, size: int) -> bytes | None:
    payload = bytearray()
    while len(payload) < size:
        chunk = stream.read(size - len(payload))
        if not chunk:
            return None
        payload.extend(chunk)
    return bytes(payload)


def _white_key_pixmap(pixmap: QPixmap) -> QPixmap:
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return pixmap
    rows = np.frombuffer(image.bits(), dtype=np.uint8, count=image.sizeInBytes()).reshape(
        height, image.bytesPerLine()
    )
    pixels = rows[:, :width * 4].reshape(height, width, 4).copy()
    distance = 255 - pixels[:, :, :3].min(axis=2).astype(np.int16)
    alpha_factor = np.clip((distance - 12) / 42, 0.0, 1.0)
    pixels[:, :, 3] = np.rint(pixels[:, :, 3].astype(np.float32) * alpha_factor).astype(np.uint8)
    keyed = QImage(pixels.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(keyed)


def _video_thumbnail(path: str, ffmpeg: str = "ffmpeg") -> QPixmap:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path, "-frames:v", "1", "-vf", "scale=960:540:force_original_aspect_ratio=decrease", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            capture_output=True, timeout=15, check=False, **hidden_process_kwargs(),
        )
        pixmap = QPixmap()
        pixmap.loadFromData(result.stdout)
        return pixmap
    except (OSError, subprocess.TimeoutExpired):
        return QPixmap()


def _fit_16_9(width: int, height: int) -> QRectF:
    target_width = min(width, height * 16 / 9)
    target_height = target_width * 9 / 16
    return QRectF((width - target_width) / 2, (height - target_height) / 2, target_width, target_height)
