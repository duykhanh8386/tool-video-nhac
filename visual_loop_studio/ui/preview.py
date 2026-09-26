from __future__ import annotations

import math
import subprocess
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from models.visual_project import ElementLayout, default_element_layouts
from utils.media import is_still_image
from visual.layout import to_top_left_rect, update_from_top_left
from visual.particles import particle_positions


class CompositionPreview(QWidget):
    element_selected = Signal(str)
    layout_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.background = ""
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
        self.waveform = "Smooth sine waveform"
        self.effects: list[str] = []
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
        self._hit_rects: dict[str, QRectF] = {}
        self._canvas = QRectF()
        self._action = ""
        self._drag_start = QPointF()
        self._start_rect = (0.0, 0.0, 0.0, 0.0)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def set_source(self, kind: str, path: str) -> None:
        setattr(self, kind, path)
        if path and Path(path).is_file():
            pixmap = QPixmap(path) if is_still_image(path) else _video_thumbnail(path, self.ffmpeg_path)
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
        self.update()

    def restart(self) -> None:
        self._time = 0
        self.play()

    def _tick(self) -> None:
        if self._playing:
            self._time = (self._time + .033) % 10
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        canvas = _fit_16_9(rect.width(), rect.height())
        self._canvas = canvas
        painter.fillRect(rect, QColor("#090d18"))
        painter.setClipRect(canvas)
        phase = self._time / 10
        self._paint_background(painter, canvas, phase)
        self._paint_filter(painter, canvas)
        self._hit_rects.clear()
        for element_id, layout in sorted(self.elements.items(), key=lambda item: item[1].z_order):
            if layout.visible and layout.opacity > 0:
                self._paint_element(painter, canvas, element_id, layout)
        self._paint_effects(painter, canvas, phase)
        painter.setClipping(False)
        if self.show_guides:
            self._paint_guides(painter, canvas)
        self._paint_selection(painter)

    def _paint_background(self, painter: QPainter, rect: QRectF, phase: float) -> None:
        zoom = 1 + .02 * (1 - math.cos(math.tau * phase))
        bg = self._images.get(self.background)
        if bg and not bg.isNull():
            scaled = bg.scaled(round(rect.width() * zoom), round(rect.height() * zoom), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            x = rect.center().x() - scaled.width() / 2 + math.sin(math.tau * phase) * 4
            y = rect.center().y() - scaled.height() / 2 + math.sin(math.tau * phase) * 3
            painter.drawPixmap(round(x), round(y), scaled)
        else:
            painter.fillRect(rect, QColor("#14213d"))
            painter.setPen(QColor("#64748b"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Choose a background image or video")

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
        if kind in {"artwork", "logo", "platform_icons"}:
            path = getattr(self, kind, "")
            pixmap = self._images.get(path)
            if pixmap and not pixmap.isNull():
                scaled = pixmap.scaled(round(local.width()), round(local.height()), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(round(-scaled.width() / 2), round(-scaled.height() / 2), scaled)
            else:
                _placeholder(painter, local, kind.replace("_", " ").title())
        elif kind == "waveform":
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
        ratio = {"title": .34, "subtitle": .40, "artist": .42, "custom_text": .30, "playlist": .12}.get(kind, .25)
        size = max(9, min(54, round(rect.height() * ratio)))
        font = QFont("Segoe UI", size, QFont.Weight.Bold if kind == "title" else QFont.Weight.Normal)
        painter.setFont(font)
        color = QColor(self.text_color)
        if not color.isValid():
            color = QColor("white")
        painter.setPen(QPen(QColor(0, 0, 0, 150), 3))
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
        painter.drawText(rect.adjusted(2, 2, -2, -2), flags, text)
        painter.setPen(color)
        painter.drawText(rect, flags, text)

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
        upper = {item.upper() for item in self.effects}
        if upper & {"SNOW", "SPARKLES", "DUST", "STAR_FIELD", "FLOATING_LIGHTS"}:
            painter.setPen(Qt.PenStyle.NoPen)
            for x, y, size in particle_positions(80, int(rect.width()), int(rect.height()), phase):
                painter.setBrush(QColor(255, 255, 255, 120 if "SNOW" in upper else 80))
                painter.drawEllipse(QRectF(rect.left() + x, rect.top() + y, size, size))
        if "RAIN" in upper:
            painter.setPen(QPen(QColor(190, 220, 255, 85), 1))
            for x, y, _ in particle_positions(95, int(rect.width()), int(rect.height()), phase * 3):
                painter.drawLine(round(rect.left() + x), round(rect.top() + y), round(rect.left() + x - 4), round(rect.top() + y + 14))
        if upper & {"FILM_GRAIN", "TV_STATIC", "VHS_NOISE", "PIXEL_NOISE"}:
            painter.setPen(QColor(255, 255, 255, 22))
            for x, y, _ in particle_positions(180, int(rect.width()), int(rect.height()), (phase * 31) % 1):
                painter.drawPoint(round(rect.left() + x), round(rect.top() + y))
        if "SCANLINES" in upper:
            painter.setPen(QColor(0, 0, 0, 40))
            y = int(rect.top())
            while y < rect.bottom():
                painter.drawLine(int(rect.left()), y, int(rect.right()), y)
                y += 4
        if "VIGNETTE" in upper:
            painter.setPen(QPen(QColor(0, 0, 0, 120), max(12, rect.width() * .06)))
            painter.drawRect(rect.adjusted(2, 2, -2, -2))

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
        self._action = ""
        self.unsetCursor()

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


def _video_thumbnail(path: str, ffmpeg: str = "ffmpeg") -> QPixmap:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path, "-frames:v", "1", "-vf", "scale=960:540:force_original_aspect_ratio=decrease", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            capture_output=True, timeout=15, check=False,
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
