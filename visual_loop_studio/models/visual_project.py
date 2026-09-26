from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EffectItem:
    name: str
    intensity: float = 0.35
    enabled: bool = True


@dataclass
class TextStyle:
    font_family: str = "Segoe UI"
    font_size: int = 48
    bold: bool = False
    italic: bool = False

    def normalized(self) -> "TextStyle":
        self.font_family = str(self.font_family or "Segoe UI")
        self.font_size = min(300, max(8, int(self.font_size)))
        self.bold = bool(self.bold)
        self.italic = bool(self.italic)
        return self


def default_text_styles() -> dict[str, TextStyle]:
    # Sizes are logical pixels at 1080p and scale with the output resolution.
    return {
        "title": TextStyle("Segoe UI", 96, True),
        "subtitle": TextStyle("Segoe UI", 56),
        "artist": TextStyle("Segoe UI", 48, True),
        "custom_text": TextStyle("Segoe UI", 44),
        "playlist": TextStyle("Segoe UI", 34),
    }


@dataclass
class ElementLayout:
    x: float
    y: float
    width: float
    height: float
    anchor: str = "top_left"
    rotation: float = 0.0
    opacity: float = 1.0
    z_order: int = 30
    locked: bool = False
    visible: bool = True
    blend_mode: str = "normal"

    def normalized(self) -> "ElementLayout":
        self.width = min(0.95, max(0.03, float(self.width)))
        self.height = min(0.95, max(0.03, float(self.height)))
        self.x = min(0.98, max(0.02, float(self.x)))
        self.y = min(0.98, max(0.02, float(self.y)))
        self.opacity = min(1.0, max(0.0, float(self.opacity)))
        self.rotation = float(self.rotation) % 360
        self.blend_mode = "lighten" if str(self.blend_mode).lower() == "lighten" else "normal"
        return self


def default_element_layouts() -> dict[str, ElementLayout]:
    return {
        "artwork": ElementLayout(.66, .24, .27, .42, z_order=20),
        "waveform": ElementLayout(.54, .80, .40, .13, z_order=35),
        "title": ElementLayout(.07, .10, .43, .14, z_order=40),
        "subtitle": ElementLayout(.07, .26, .43, .07, z_order=41),
        "artist": ElementLayout(.07, .35, .36, .06, z_order=42),
        "custom_text": ElementLayout(.07, .43, .42, .10, z_order=43),
        "playlist": ElementLayout(.63, .38, .31, .30, z_order=44),
        "platform_icons": ElementLayout(.05, .86, .15, .07, z_order=55),
        "logo": ElementLayout(.05, .05, .13, .11, z_order=60),
    }


@dataclass
class VisualProject:
    title: str = ""
    subtitle: str = ""
    artist: str = ""
    custom_text: str = ""
    playlist: str = ""
    font_file: str = ""
    font_size: int = 0
    text_styles: dict[str, TextStyle] = field(default_factory=default_text_styles)
    text_position: str = "Top Left"
    text_color: str = "#FFFFFF"
    text_opacity: int = 100
    stroke_width: int = 2
    text_shadow: bool = True
    background: str = ""
    logo: str = ""
    artwork: str = ""
    platform_icons: str = ""
    audio: str = ""
    waveform_media: str = ""
    waveform_remove_white: bool = True
    output_folder: str = ""
    background_folder: str = ""
    batch_recursive: bool = False
    output_name: str = ""
    resolution: str = "1920x1080"
    fps: int = 30
    encoder: str = "Auto"
    animation: str = "STATIC"
    artwork_motion: str = "FLOAT"
    waveform: str = "FILE"
    ai_prompt: str = ""
    ai_model: str = "veo-3.1-fast-generate-preview"
    ai_duration: int = 8
    ai_resolution: str = "720p"
    ai_aspect_ratio: str = "16:9"
    layout_mode: str = "AUTO"
    layout_template: str = "Minimal"
    layout_variant: int = 0
    elements: dict[str, ElementLayout] = field(default_factory=default_element_layouts)
    color_filter: str = "NONE"
    manual_color: dict[str, float] = field(default_factory=lambda: {
        "brightness": 0, "contrast": 100, "saturation": 100, "temperature": 0,
        "tint": 0, "gamma": 100, "highlights": 0, "shadows": 0,
        "sharpness": 0, "vignette": 0, "bloom": 0,
    })
    lut: str = ""
    effects: list[EffectItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VisualProject":
        data = dict(value or {})
        data["effects"] = [EffectItem(**item) for item in data.get("effects", [])]
        text_defaults = default_text_styles()
        loaded_styles: dict[str, TextStyle] = {}
        for name, item in (data.get("text_styles") or {}).items():
            if isinstance(item, TextStyle):
                loaded_styles[name] = item.normalized()
            elif isinstance(item, dict):
                base = asdict(text_defaults.get(name, TextStyle()))
                base.update(item)
                allowed_style = TextStyle.__dataclass_fields__.keys()
                loaded_styles[name] = TextStyle(**{k: v for k, v in base.items() if k in allowed_style}).normalized()
        text_defaults.update(loaded_styles)
        if not data.get("text_styles") and int(data.get("font_size") or 0) > 0:
            base_size = int(data["font_size"])
            ratios = {"title": 1.0, "subtitle": .60, "artist": .52, "custom_text": .48, "playlist": .38}
            for name, ratio in ratios.items():
                text_defaults[name].font_size = max(8, round(base_size * ratio))
        data["text_styles"] = text_defaults
        defaults = default_element_layouts()
        loaded: dict[str, ElementLayout] = {}
        for name, item in (data.get("elements") or {}).items():
            if isinstance(item, ElementLayout):
                loaded[name] = item.normalized()
            elif isinstance(item, dict):
                base = asdict(defaults.get(name, ElementLayout(.1, .1, .2, .1)))
                base.update(item)
                allowed_layout = ElementLayout.__dataclass_fields__.keys()
                loaded[name] = ElementLayout(**{k: v for k, v in base.items() if k in allowed_layout}).normalized()
        defaults.update(loaded)
        data["elements"] = defaults
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in data.items() if k in allowed})
