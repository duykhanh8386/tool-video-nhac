from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class HomePage(QWidget):
    navigate = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Visual Loop Studio")
        title.setObjectName("hero")
        subtitle = QLabel("Tạo visual 60 giây, kiểm tra, rồi render video dài đúng bằng thời lượng nhạc.")
        subtitle.setObjectName("muted")
        layout.addStretch()
        layout.addWidget(title)
        layout.addWidget(subtitle)
        grid = QGridLayout()
        cards = [
            ("Visual Creator — 60 Seconds", "Background • text • logo • artwork • waveform • effects", 1),
            ("Loop Video + Music", "Main audio là master clock; hỗ trợ video nhiều giờ", 2),
            ("Audio Mixer", "Mix main, background, nature, water, rain, white noise", 3),
            ("Batch Render", "Xếp hàng nhiều tác vụ với progress riêng", 4),
        ]
        for index, (name, detail, page) in enumerate(cards):
            button = QPushButton(f"{name}\n{detail}")
            button.setObjectName("homeCard")
            button.setMinimumHeight(112)
            button.clicked.connect(lambda _checked=False, page=page: self.navigate.emit(page))
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch()
