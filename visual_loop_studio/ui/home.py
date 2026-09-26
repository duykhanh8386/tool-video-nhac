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
            ("Tạo Visual — 60 giây", "Ảnh nền • chữ • logo • ảnh bìa • file sóng • hiệu ứng • Veo AI", 1),
            ("Lặp Video + Nhạc", "Nhạc chính quyết định thời lượng; hỗ trợ video nhiều giờ", 2),
            ("Trộn âm thanh", "Trộn nhạc chính, nhạc nền, thiên nhiên, nước, mưa và tiếng ồn trắng", 3),
            ("Render hàng loạt", "Xếp hàng nhiều tác vụ với tiến độ riêng", 4),
        ]
        for index, (name, detail, page) in enumerate(cards):
            button = QPushButton(f"{name}\n{detail}")
            button.setObjectName("homeCard")
            button.setMinimumHeight(112)
            button.clicked.connect(lambda _checked=False, page=page: self.navigate.emit(page))
            grid.addWidget(button, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch()
