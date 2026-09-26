from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

from utils.updater import (
    RELEASES_URL, can_self_update, check_latest_release, download_release,
    is_newer_version, schedule_self_update,
)
from version import __version__


class UpdateController(QObject):
    check_completed = Signal(object, bool)
    operation_failed = Signal(str, bool)
    download_progress = Signal(int, int)
    download_completed = Signal(str)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.parent_window = parent
        self.busy = False
        self.progress_dialog: QProgressDialog | None = None
        self.pending_release = None
        self.check_completed.connect(self._handle_check)
        self.operation_failed.connect(self._handle_error)
        self.download_progress.connect(self._handle_progress)
        self.download_completed.connect(self._handle_download)

    def check(self, silent: bool = False) -> None:
        if self.busy:
            if not silent:
                QMessageBox.information(self.parent_window, "Update", "Đang kiểm tra hoặc tải bản cập nhật.")
            return
        self.busy = True
        threading.Thread(target=self._check_worker, args=(silent,), daemon=True).start()

    def _check_worker(self, silent: bool) -> None:
        try:
            release = check_latest_release()
            self.check_completed.emit(release, silent)
        except Exception as exc:
            self.operation_failed.emit(str(exc), silent)

    def _handle_check(self, release, silent: bool) -> None:
        self.busy = False
        if not is_newer_version(release.version, __version__):
            if not silent:
                QMessageBox.information(self.parent_window, "Update", f"Bạn đang dùng phiên bản mới nhất: v{__version__}")
            return
        notes = release.notes.strip()
        if len(notes) > 1200:
            notes = notes[:1200] + "…"
        message = f"Có phiên bản mới v{release.version}.\nPhiên bản hiện tại: v{__version__}."
        if notes:
            message += f"\n\n{notes}"
        message += "\n\nTải bản cập nhật ngay?"
        answer = QMessageBox.question(self.parent_window, "Visual Loop Studio Update", message)
        if answer == QMessageBox.StandardButton.Yes:
            if not can_self_update():
                QDesktopServices.openUrl(QUrl(release.page_url or RELEASES_URL))
                QMessageBox.information(self.parent_window, "Developer mode", "Ứng dụng đang chạy từ source. Trang Release đã được mở để bạn tải EXE.")
                return
            self._download(release)

    def _download(self, release) -> None:
        self.busy = True
        self.pending_release = release
        self.progress_dialog = QProgressDialog("Đang tải bản cập nhật…", "Ẩn", 0, 1000, self.parent_window)
        self.progress_dialog.setWindowTitle("Visual Loop Studio Update")
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setMinimumDuration(0)
        self.progress_dialog.show()
        threading.Thread(target=self._download_worker, args=(release,), daemon=True).start()

    def _download_worker(self, release) -> None:
        try:
            path = download_release(release, progress=lambda done, total: self.download_progress.emit(done, total))
            self.download_completed.emit(str(path))
        except Exception as exc:
            self.operation_failed.emit(str(exc), False)

    def _handle_progress(self, downloaded: int, total: int) -> None:
        if not self.progress_dialog:
            return
        if total > 0:
            self.progress_dialog.setValue(min(1000, round(downloaded / total * 1000)))
            self.progress_dialog.setLabelText(f"Đang tải bản cập nhật… {downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB")
        else:
            self.progress_dialog.setRange(0, 0)

    def _handle_download(self, path: str) -> None:
        self.busy = False
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        try:
            version = str(getattr(self.pending_release, "version", "") or "")
            schedule_self_update(path, version)
        except Exception as exc:
            QMessageBox.critical(self.parent_window, "Update failed", str(exc))
            return
        QMessageBox.information(
            self.parent_window,
            "Sẵn sàng cập nhật",
            "Đã tải và xác minh SHA-256. Ứng dụng sẽ đóng, thay EXE và tự mở lại. "
            "Sau khi mở lại sẽ có thông báo Cập nhật hoàn tất.",
        )
        QApplication.quit()

    def _handle_error(self, message: str, silent: bool) -> None:
        self.busy = False
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        if not silent:
            QMessageBox.critical(self.parent_window, "Không thể cập nhật", message)
