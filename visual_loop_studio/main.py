from __future__ import annotations

import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from version import __build_commit__, __version__

if __name__ == "__main__" and "--version" in sys.argv:
    print(f"Visual Loop Studio {__version__} ({__build_commit__})")
    raise SystemExit(0)

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QAction, QCloseEvent, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication, QFileDialog, QHBoxLayout, QLabel, QListWidget, QMainWindow,
        QMessageBox, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
    )
except ModuleNotFoundError as exc:
    raise SystemExit("PySide6 is not installed. Run install.bat, then open the app with run.bat.") from exc

from models.loop_project import LoopProject
from models.visual_project import VisualProject
from ui.audio_mixer import AudioMixerPage
from ui.ai_batch import AiBatchPage
from ui.batch_render import BatchRenderPage
from ui.common import WheelValueGuard
from ui.home import HomePage
from ui.loop_music import LoopMusicPage
from ui.settings import SettingsDialog
from ui.updater import UpdateController
from ui.visual_creator import VisualCreatorPage
from utils.config import load_settings, save_settings, write_json
from utils.paths import RESOURCE_DIR, ensure_app_dirs
from utils.shortcuts import ensure_desktop_shortcut


APP_LOGO = RESOURCE_DIR / "assets" / "app_logo.png"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.updater = UpdateController(self)
        self.project_path = ""
        self.setWindowTitle(f"Visual Loop Studio v{__version__}")
        self.resize(1440, 900)
        self.setMinimumSize(1080, 700)
        self._build_ui()
        self._wheel_value_guard = WheelValueGuard(self)
        QApplication.instance().installEventFilter(self._wheel_value_guard)
        self._build_menu()
        QTimer.singleShot(2500, lambda: self.updater.check(silent=True))

    def _build_ui(self) -> None:
        shell = QWidget()
        root = QHBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        logo_pixmap = QPixmap(str(APP_LOGO))
        if not logo_pixmap.isNull():
            logo = QLabel()
            logo.setObjectName("brandLogo")
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            logo.setPixmap(logo_pixmap.scaled(
                96, 96,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            side.addWidget(logo)
        brand = QLabel("VISUAL\nLOOP STUDIO")
        brand.setObjectName("brand")
        side.addWidget(brand)
        self.navigation = QListWidget()
        self.navigation.addItems([
            "Trang chủ", "Tạo Visual — 60 giây", "Lặp Video + Nhạc", "Trộn âm thanh",
            "Render hàng loạt", "AI Video hàng loạt",
        ])
        self.navigation.setCurrentRow(0)
        self.navigation.currentRowChanged.connect(self._navigate)
        side.addWidget(self.navigation, 1)
        update_button = QPushButton("Kiểm tra cập nhật")
        update_button.clicked.connect(lambda: self.updater.check(silent=False))
        side.addWidget(update_button)
        version = QLabel(f"Local • FFmpeg • v{__version__}\n{__build_commit__[:10]}")
        version.setObjectName("muted")
        side.addWidget(version)
        root.addWidget(sidebar)
        self.pages = QStackedWidget()
        self.home = HomePage()
        self.visual = VisualCreatorPage(self.settings)
        self.loop = LoopMusicPage(self.settings)
        self.audio = AudioMixerPage(self.settings)
        self.batch = BatchRenderPage(self.settings)
        self.ai_batch = AiBatchPage(self.settings)
        for page in (self.home, self.visual, self.loop, self.audio, self.batch, self.ai_batch):
            self.pages.addWidget(page)
        self.home.navigate.connect(self.navigation.setCurrentRow)
        self.visual.settings_changed.connect(self._save_settings)
        self.loop.settings_changed.connect(self._save_settings)
        self.audio.settings_changed.connect(self._save_settings)
        self.ai_batch.settings_changed.connect(self._save_settings)
        root.addWidget(self.pages, 1)
        self.setCentralWidget(shell)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Tệp")
        for text, shortcut, callback in (
            ("Project mới", "Ctrl+N", self.new_project), ("Mở project", "Ctrl+O", self.open_project),
            ("Lưu project", "Ctrl+S", self.save_project), ("Lưu project thành…", "Ctrl+Shift+S", self.save_project_as),
        ):
            action = QAction(text, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            file_menu.addAction(action)
        file_menu.addSeparator()
        exit_action = QAction("Thoát", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        tools = self.menuBar().addMenu("Công cụ")
        for index, text in enumerate((
            "Tạo Visual — 60 giây", "Lặp Video + Nhạc", "Trộn âm thanh",
            "Render hàng loạt", "AI Video hàng loạt",
        ), start=1):
            action = QAction(text, self)
            action.triggered.connect(lambda _checked=False, index=index: self.navigation.setCurrentRow(index))
            tools.addAction(action)
        settings_menu = self.menuBar().addMenu("Cài đặt")
        action = QAction("FFmpeg, GPU, ComfyUI và Gemini/Veo…", self)
        action.triggered.connect(self.open_settings)
        settings_menu.addAction(action)
        help_menu = self.menuBar().addMenu("Trợ giúp")
        update_action = QAction("Kiểm tra cập nhật", self)
        update_action.triggered.connect(lambda: self.updater.check(silent=False))
        help_menu.addAction(update_action)

    def _navigate(self, index: int) -> None:
        if index >= 0:
            self.pages.setCurrentIndex(index)

    def new_project(self) -> None:
        self.project_path = ""
        self.visual.load(VisualProject(output_folder=self.settings.last_output_folder, encoder=self.settings.encoder))
        self.loop.load(LoopProject(output_folder=self.settings.last_output_folder, encoder=self.settings.encoder))
        self.setWindowTitle("Visual Loop Studio — Untitled")

    def project_data(self) -> dict:
        return {
            "format": "visual-loop-studio-project",
            "version": 1,
            "visual_creator": self.visual.collect().to_dict(),
            "loop_music": self.loop.collect().to_dict(),
        }

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", "Visual Loop Studio (*.vls.json *.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            self.visual.load(VisualProject.from_dict(data.get("visual_creator", {})))
            self.loop.load(LoopProject.from_dict(data.get("loop_music", {})))
            self.project_path = path
            self.setWindowTitle(f"Visual Loop Studio — {Path(path).name}")
        except Exception as exc:
            QMessageBox.critical(self, "Open project failed", str(exc))

    def save_project(self) -> None:
        if not self.project_path:
            self.save_project_as()
            return
        try:
            write_json(self.project_path, self.project_data())
            self.statusBar().showMessage(f"Saved {self.project_path}", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "Save project failed", str(exc))

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save project", self.project_path or "visual_project.vls.json", "Visual Loop Studio (*.vls.json)")
        if not path:
            return
        if not path.lower().endswith(".vls.json"):
            path += ".vls.json"
        self.project_path = path
        self.save_project()
        self.setWindowTitle(f"Visual Loop Studio — {Path(path).name}")

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            try:
                dialog.apply(self.settings)
                self.visual.preview.ffmpeg_path = self.settings.ffmpeg_path
                self.ai_batch.reload_settings(self.settings)
                self._save_settings()
            except Exception as exc:
                QMessageBox.critical(self, "Không thể lưu cài đặt", str(exc))

    def _save_settings(self) -> None:
        save_settings(self.settings)

    def closeEvent(self, event: QCloseEvent) -> None:
        workers = (self.visual.worker, self.loop.worker, self.audio.worker)
        if (
            any(worker.running for worker in workers)
            or self.batch.running
            or self.visual.ai_worker.running
            or self.visual.local_ai_worker.running
            or self.visual.local_ai_worker.checking
            or self.visual.local_setup_worker.running
            or self.visual.google_vids_worker.busy
            or self.ai_batch.busy
        ):
            answer = QMessageBox.question(self, "Render đang chạy", "Cancel render và thoát ứng dụng?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for worker in workers:
                worker.cancel()
            self.batch.cancel_all()
            self.visual.ai_worker.cancel()
            self.visual.local_ai_worker.cancel()
            self.visual.local_setup_worker.cancel()
            self.visual.google_vids_worker.cancel()
            self.ai_batch.cancel_all()
        self.visual.local_runtime.stop()
        self.ai_batch.shutdown()
        self._save_settings()
        event.accept()


STYLE = """
QWidget { background: #0b1020; color: #e5e7eb; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QMenuBar, QMenu { background: #0b1020; }
QMenuBar::item:selected, QMenu::item:selected { background: #24304a; }
#sidebar { background: #0f172a; border-right: 1px solid #26334d; }
#brandLogo { padding: 18px 12px 0; }
#brand { color: #67e8f9; font-size: 20px; font-weight: 800; padding: 20px 12px; letter-spacing: 2px; }
QListWidget { background: transparent; border: 0; outline: 0; }
QListWidget::item { padding: 12px; margin: 2px; border-radius: 6px; }
QListWidget::item:selected { background: #1e40af; color: white; }
#hero { font-size: 42px; font-weight: 800; color: white; }
#pageTitle { font-size: 25px; font-weight: 700; color: white; padding: 8px 0; }
#muted { color: #94a3b8; }
#notice { background: #102a43; color: #bae6fd; border: 1px solid #164e63; border-radius: 6px; padding: 10px; }
#homeCard { text-align: left; font-size: 15px; padding: 18px; background: #111c32; border: 1px solid #253452; border-radius: 10px; }
#homeCard:hover { border-color: #22d3ee; background: #152440; }
QGroupBox { border: 1px solid #29364f; border-radius: 8px; margin-top: 12px; padding: 13px 8px 8px; font-weight: 600; color: #a5f3fc; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QLineEdit, QComboBox, QSpinBox, QTableWidget { background: #111827; border: 1px solid #334155; border-radius: 5px; padding: 7px; color: #f8fafc; }
QPushButton { background: #1e293b; border: 1px solid #3b4b66; border-radius: 6px; padding: 8px 12px; }
QPushButton:hover { background: #293a55; border-color: #67e8f9; }
QPushButton:disabled { color: #64748b; background: #111827; }
QPushButton#primary { background: #0891b2; border-color: #22d3ee; color: white; font-weight: 700; }
QPushButton#primary:hover { background: #06b6d4; }
QProgressBar { background: #111827; border: 1px solid #334155; border-radius: 5px; text-align: center; height: 18px; }
QProgressBar::chunk { background: #06b6d4; border-radius: 4px; }
QScrollArea { border: 0; }
QToolTip { background: #1e293b; color: white; border: 1px solid #475569; }
"""


def main() -> int:
    if "--version" in sys.argv:
        print(f"Visual Loop Studio {__version__} ({__build_commit__})")
        return 0
    ensure_app_dirs()
    app = QApplication(sys.argv)
    app.setApplicationName("Visual Loop Studio")
    app.setOrganizationName("Visual Loop Studio")
    shortcut_error = ""
    try:
        ensure_desktop_shortcut()
    except Exception as exc:
        shortcut_error = str(exc)
    app_icon = QIcon(str(APP_LOGO))
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    if shortcut_error:
        QTimer.singleShot(500, lambda: QMessageBox.warning(
            window,
            "Không thể tạo shortcut",
            f"Ứng dụng không thể tạo shortcut trên Desktop.\n\n{shortcut_error}",
        ))
    updated_to = next((item.split("=", 1)[1] for item in sys.argv if item.startswith("--updated-to=")), "")
    if updated_to:
        fallback = "--update-fallback" in sys.argv
        title = "Cập nhật cần xác nhận" if fallback else "Cập nhật hoàn tất"
        message = (
            f"Đã mở Visual Loop Studio v{updated_to} từ file tải tạm, nhưng Windows chưa cho phép "
            "thay thế EXE cũ. Hãy tải/thay EXE thủ công từ trang Release."
            if fallback else f"Đã cập nhật thành công lên Visual Loop Studio v{updated_to}."
        )
        if fallback:
            callback = lambda: QMessageBox.warning(window, title, message)
        else:
            callback = lambda: QMessageBox.information(window, title, message)
        QTimer.singleShot(900, callback)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
