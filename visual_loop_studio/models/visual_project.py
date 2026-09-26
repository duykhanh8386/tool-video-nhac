from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EffectItem:
    name: str
    intensity: float = 0.35
    enabled: bool = True


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

    def normalized(self) -> "ElementLayout":
        self.width = min(0.95, max(0.03, float(self.width)))
        self.height = min(0.95, max(0.03, float(self.height)))
        self.x = min(0.98, max(0.02, float(self.x)))
        self.y = min(0.98, max(0.02, float(self.y)))
        self.opacity = min(1.0, max(0.0, float(self.opacity)))
        self.rotation = float(self.rotation) % 360
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
    output_folder: str = ""
    output_name: str = ""
    resolution: str = "1920x1080"
    fps: int = 30
    encoder: str = "Auto"
    animation: str = "VEO STYLE GENTLE"
    artwork_motion: str = "FLOAT"
    waveform: str = "Smooth sine waveform"
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
