from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QGridLayout, QLabel, QSlider, QVBoxLayout, QWidget

from ui.common import FileField
from visual.filters import COLOR_FILTERS


class ColorFilterPanel(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.preset = QComboBox()
        self.preset.addItems(COLOR_FILTERS)
        self.preset.currentTextChanged.connect(lambda _text: self.changed.emit())
        grid = QGridLayout()
        self.sliders: dict[str, QSlider] = {}
        definitions = (
            ("brightness", "Độ sáng", -100, 100, 0), ("contrast", "Tương phản", 0, 200, 100),
            ("saturation", "Bão hòa", 0, 200, 100), ("temperature", "Nhiệt độ màu", -100, 100, 0),
            ("tint", "Sắc màu", -100, 100, 0), ("gamma", "Gamma", 10, 300, 100),
            ("highlights", "Vùng sáng", -100, 100, 0), ("shadows", "Vùng tối", -100, 100, 0),
            ("sharpness", "Độ nét", 0, 100, 0), ("vignette", "Tối góc", 0, 100, 0),
            ("bloom", "Quầng sáng", 0, 100, 0),
        )
        for index, (key, label, minimum, maximum, default) in enumerate(definitions):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(minimum, maximum)
            slider.setValue(default)
            value_label = QLabel(str(default))
            value_label.setMinimumWidth(30)
            slider.valueChanged.connect(lambda value, value_label=value_label: value_label.setText(str(value)))
            slider.valueChanged.connect(lambda _value: self.changed.emit())
            row, column = divmod(index, 2)
            grid.addWidget(QLabel(label), row, column * 3)
            grid.addWidget(slider, row, column * 3 + 1)
            grid.addWidget(value_label, row, column * 3 + 2)
            self.sliders[key] = slider
        self.lut = FileField("File màu LUT .cube", "Cube LUT (*.cube);;Tất cả file (*.*)", optional=True)
        self.lut.changed.connect(lambda _text: self.changed.emit())
        layout.addWidget(self.preset)
        layout.addLayout(grid)
        layout.addWidget(self.lut)

    def values(self) -> dict[str, float]:
        return {key: slider.value() for key, slider in self.sliders.items()}

    def set_values(self, values: dict[str, float]) -> None:
        for key, slider in self.sliders.items():
            if key in values:
                slider.setValue(round(values[key]))
