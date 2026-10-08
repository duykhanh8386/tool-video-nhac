from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from auth.license_manager import LicenseManager, get_license_manager


class ValidationWorker(QThread):
    finished_signal = Signal(bool, str, dict)

    def __init__(self, manager: LicenseManager, key: str):
        super().__init__()
        self.manager = manager
        self.key = key

    def run(self):
        try:
            ok, msg, data = self.manager.validate_online(self.key)
            self.finished_signal.emit(ok, msg, data)
        except Exception as exc:
            self.finished_signal.emit(False, str(exc), {})


class LicenseDialog(QDialog):
    def __init__(self, manager: LicenseManager | None = None, view_mode: bool = False, parent=None):
        super().__init__(parent)
        self.manager = manager or get_license_manager()
        self.view_mode = view_mode
        self._worker: ValidationWorker | None = None

        self.setWindowTitle("Xác thực Bản Quyền — Keygen.sh")
        self.setFixedSize(540, 520)
        self.setModal(True)
        self._build_ui()
        self._load_data()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # Header
        header = QHBoxLayout()
        header.setSpacing(12)
        icon_lbl = QLabel("🛡️")
        icon_lbl.setStyleSheet("font-size: 32px;")
        header.addWidget(icon_lbl)

        title_box = QVBoxLayout()
        title_box.setSpacing(4)
        title_lbl = QLabel("KÍCH HOẠT BẢN QUYỀN PHẦN MỀM")
        title_lbl.setStyleSheet("font-size: 16px; font-weight: bold; color: #0f172a;")
        sub_lbl = QLabel("Quản lý bản quyền an toàn qua nền tảng Keygen.sh")
        sub_lbl.setStyleSheet("font-size: 12px; color: #64748b;")
        title_box.addWidget(title_lbl)
        title_box.addWidget(sub_lbl)
        header.addLayout(title_box)
        header.addStretch()
        root.addLayout(header)

        # Divider line
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color: #e2e8f0;")
        root.addWidget(line)

        # 1. Mã phần cứng (Hardware ID)
        hwid_group = QGroupBox("Mã Nhận Diện Máy Tính (Hardware ID / Fingerprint)")
        hwid_group.setStyleSheet("QGroupBox { font-weight: 600; color: #334155; }")
        hwid_layout = QHBoxLayout(hwid_group)
        hwid_layout.setSpacing(8)

        self.hwid_input = QLineEdit(self.manager.fingerprint)
        self.hwid_input.setReadOnly(True)
        self.hwid_input.setStyleSheet(
            "QLineEdit { font-family: 'Consolas', monospace; font-size: 13px; font-weight: bold; "
            "color: #1e293b; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; }"
        )
        self.copy_btn = QPushButton("📋 Copy")
        self.copy_btn.setMinimumHeight(34)
        self.copy_btn.setToolTip("Sao chép mã máy để gửi cho Admin cấp bản quyền")
        self.copy_btn.clicked.connect(self._copy_hwid)
        hwid_layout.addWidget(self.hwid_input, 1)
        hwid_layout.addWidget(self.copy_btn)
        root.addWidget(hwid_group)

        # 2. Keygen Account ID
        acc_group = QGroupBox("Keygen Account ID (Mã tài khoản nhà phát triển)")
        acc_group.setStyleSheet("QGroupBox { font-weight: 600; color: #334155; }")
        acc_layout = QVBoxLayout(acc_group)
        acc_layout.setSpacing(6)

        self.acc_input = QLineEdit()
        self.acc_input.setPlaceholderText("Nhập Account ID (lấy từ mục Settings trên keygen.sh)...")
        self.acc_input.setStyleSheet(
            "QLineEdit { font-size: 13px; color: #1e293b; background: #ffffff; "
            "border: 1.5px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; }"
        )
        acc_hint = QLabel("💡 Account ID dạng: <code>xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx</code>")
        acc_hint.setStyleSheet("font-size: 11px; color: #64748b;")
        acc_layout.addWidget(self.acc_input)
        acc_layout.addWidget(acc_hint)
        root.addWidget(acc_group)

        # 3. License Key
        key_group = QGroupBox("Mã Bản Quyền (License Key)")
        key_group.setStyleSheet("QGroupBox { font-weight: 600; color: #334155; }")
        key_layout = QVBoxLayout(key_group)
        key_layout.setSpacing(6)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("XXXX-XXXX-XXXX-XXXX-XXXX-XXXX")
        self.key_input.setStyleSheet(
            "QLineEdit { font-family: 'Consolas', monospace; font-size: 13px; font-weight: bold; "
            "color: #2563eb; background: #ffffff; border: 1.5px solid #cbd5e1; border-radius: 6px; padding: 8px 10px; }"
        )
        key_layout.addWidget(self.key_input)
        root.addWidget(key_group)

        # Status Label
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("font-size: 12px; font-weight: 600; padding: 2px;")
        root.addWidget(self.status_lbl)

        root.addStretch()

        # Action Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.deactivate_btn = QPushButton("🔓 Hủy kích hoạt máy này")
        self.deactivate_btn.setMinimumHeight(38)
        self.deactivate_btn.setStyleSheet(
            "QPushButton { background: #fee2e2; border: 1.5px solid #f87171; color: #991b1b; "
            "font-weight: 600; border-radius: 6px; padding: 0 14px; } "
            "QPushButton:hover { background: #fecaca; }"
        )
        self.deactivate_btn.clicked.connect(self._deactivate)
        self.deactivate_btn.setVisible(False)
        btn_row.addWidget(self.deactivate_btn)

        btn_row.addStretch()

        self.exit_btn = QPushButton("Đóng" if self.view_mode else "Thoát")
        self.exit_btn.setMinimumHeight(38)
        self.exit_btn.setMinimumWidth(85)
        self.exit_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.exit_btn)

        self.activate_btn = QPushButton("▶ Kích Hoạt Ngay")
        self.activate_btn.setObjectName("primary")
        self.activate_btn.setMinimumHeight(38)
        self.activate_btn.setMinimumWidth(140)
        self.activate_btn.setStyleSheet(
            "QPushButton { background: #2563eb; border: 1.5px solid #1d4ed8; color: #ffffff; "
            "font-size: 13px; font-weight: bold; border-radius: 6px; padding: 0 18px; } "
            "QPushButton:hover { background: #1d4ed8; } "
            "QPushButton:disabled { background: #cbd5e1; border-color: #cbd5e1; color: #64748b; }"
        )
        self.activate_btn.clicked.connect(self._activate)
        btn_row.addWidget(self.activate_btn)

        root.addLayout(btn_row)

    def _load_data(self) -> None:
        self.acc_input.setText(self.manager.get_account_id())
        info = self.manager.get_license_info()
        cached_key = info.get("key", "")
        if cached_key:
            self.key_input.setText(cached_key)

        if info.get("valid"):
            expiry = info.get("expiry")
            exp_text = f"Hết hạn: {expiry[:10]}" if expiry else "Thời hạn: Vĩnh viễn (Perpetual)"
            cust_name = info.get("customer_name")
            owner_text = f" • Khách hàng: {cust_name}" if cust_name else ""
            self.status_lbl.setText(f"✅ ĐÃ KÍCH HOẠT HỢP LỆ ({exp_text}{owner_text})")
            self.status_lbl.setStyleSheet("color: #16a34a; font-weight: bold;")
            self.deactivate_btn.setVisible(True)
            self.activate_btn.setText("🔄 Kiểm tra lại")
        else:
            self.status_lbl.setText("⚠️ Chưa kích hoạt. Vui lòng nhập License Key.")
            self.status_lbl.setStyleSheet("color: #d97706; font-weight: 500;")

    def _copy_hwid(self) -> None:
        clipboard = QApplication.clipboard()
        clipboard.setText(self.hwid_input.text().strip())
        QMessageBox.information(
            self,
            "Đã sao chép",
            f"Đã sao chép mã máy tính (Hardware ID):\n\n{self.hwid_input.text().strip()}\n\n"
            "Hãy gửi mã này cho Admin để gán bản quyền máy nếu cần.",
        )

    def _activate(self) -> None:
        acc_id = self.acc_input.text().strip()
        key = self.key_input.text().strip()

        if not acc_id:
            QMessageBox.warning(self, "Thiếu Account ID", "Vui lòng nhập Keygen Account ID để kết nối hệ thống.")
            self.acc_input.setFocus()
            return

        if not key:
            QMessageBox.warning(self, "Thiếu License Key", "Vui lòng nhập mã License Key được cấp.")
            self.key_input.setFocus()
            return

        # Lưu Account ID
        self.manager.set_account_id(acc_id)

        # Chạy kiểm tra qua worker
        self.activate_btn.setEnabled(False)
        self.activate_btn.setText("⏳ Đang xác thực...")
        self.status_lbl.setText("⏳ Đang kết nối tới máy chủ Keygen.sh để xác thực...")
        self.status_lbl.setStyleSheet("color: #2563eb; font-weight: 600;")

        self._worker = ValidationWorker(self.manager, key)
        self._worker.finished_signal.connect(self._on_validation_finished)
        self._worker.start()

    def _on_validation_finished(self, ok: bool, msg: str, _data: dict) -> None:
        self.activate_btn.setEnabled(True)
        self.activate_btn.setText("▶ Kích Hoạt Ngay" if not ok else "🔄 Kiểm tra lại")

        if ok:
            self.status_lbl.setText(f"✅ {msg}")
            self.status_lbl.setStyleSheet("color: #16a34a; font-weight: bold;")
            self.deactivate_btn.setVisible(True)
            QMessageBox.information(self, "Thành công", f"Chúc mừng! {msg}\nỨng dụng đã sẵn sàng sử dụng.")
            self.accept()
        else:
            self.status_lbl.setText(f"❌ {msg}")
            self.status_lbl.setStyleSheet("color: #dc2626; font-weight: bold;")
            QMessageBox.critical(self, "Kích hoạt thất bại", msg)

    def _deactivate(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Xác nhận hủy kích hoạt",
            "Bạn có chắc muốn hủy bản quyền trên máy tính này?\n"
            "Hành động này sẽ gỡ máy khỏi hệ thống Keygen để bạn có thể mang key sang máy khác.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            ok, msg = self.manager.deactivate_current_machine()
            QMessageBox.information(self, "Thông báo", msg)
            self._load_data()
            if not self.view_mode:
                self.reject()
