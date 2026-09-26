from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSlider, QVBoxLayout, QWidget

from models.visual_project import EffectItem
from visual.effects import EFFECT_NAMES


class EffectPanel(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Danh sách hiệu ứng toàn khung (mặc định: trống)"))
        row = QHBoxLayout()
        self.choice = QComboBox()
        self.choice.addItems(EFFECT_NAMES)
        add = QPushButton("Thêm")
        add.clicked.connect(self.add_current)
        row.addWidget(self.choice, 1)
        row.addWidget(add)
        layout.addLayout(row)
        self.list = QListWidget()
        self.list.setMaximumHeight(112)
        self.list.currentItemChanged.connect(self._selection)
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        layout.addWidget(self.list)
        controls = QHBoxLayout()
        remove = QPushButton("Xóa")
        up = QPushButton("↑")
        down = QPushButton("↓")
        remove.clicked.connect(self.remove_current)
        up.clicked.connect(lambda: self.move(-1))
        down.clicked.connect(lambda: self.move(1))
        self.intensity = QSlider(Qt.Orientation.Horizontal)
        self.intensity.setRange(0, 100)
        self.intensity.setValue(35)
        self.intensity.valueChanged.connect(self._intensity_changed)
        controls.addWidget(remove)
        controls.addWidget(up)
        controls.addWidget(down)
        controls.addWidget(QLabel("Cường độ"))
        controls.addWidget(self.intensity, 1)
        layout.addLayout(controls)

    def add_current(self) -> None:
        item = QListWidgetItem(self.choice.currentText())
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        item.setData(Qt.ItemDataRole.UserRole, .35)
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self.changed.emit()

    def remove_current(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self.changed.emit()

    def move(self, offset: int) -> None:
        row = self.list.currentRow()
        target = row + offset
        if row >= 0 and 0 <= target < self.list.count():
            item = self.list.takeItem(row)
            self.list.insertItem(target, item)
            self.list.setCurrentRow(target)
            self.changed.emit()

    def _selection(self, current, _previous) -> None:
        if current:
            self.intensity.blockSignals(True)
            self.intensity.setValue(round(float(current.data(Qt.ItemDataRole.UserRole) or .35) * 100))
            self.intensity.blockSignals(False)

    def _intensity_changed(self, value: int) -> None:
        item = self.list.currentItem()
        if item:
            item.setData(Qt.ItemDataRole.UserRole, value / 100)
            self.changed.emit()

    def values(self) -> list[EffectItem]:
        return [EffectItem(self.list.item(i).text(), float(self.list.item(i).data(Qt.ItemDataRole.UserRole) or .35), self.list.item(i).checkState() == Qt.CheckState.Checked) for i in range(self.list.count())]

    def set_values(self, effects: list[EffectItem]) -> None:
        self.list.clear()
        for effect in effects:
            item = QListWidgetItem(effect.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if effect.enabled else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, effect.intensity)
            self.list.addItem(item)
        self.changed.emit()
