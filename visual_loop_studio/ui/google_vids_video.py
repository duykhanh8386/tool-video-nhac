from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ai.google_vids_web import (
    GOOGLE_VIDS_PROFILE_DIR,
    GoogleVidsCancelled,
    generate_google_vids_clip,
    open_google_vids_login,
)


class GoogleVidsWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)
    canceled = Signal()
    login_finished = Signal(str)
    login_failed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.running = False
        self.login_running = False
        self._cancel = threading.Event()

    @property
    def busy(self) -> bool:
        return self.running or self.login_running

    def login(self, profile_dir: str = str(GOOGLE_VIDS_PROFILE_DIR)) -> None:
        if self.busy:
            raise RuntimeError("Google Vids đang chạy một tác vụ khác.")
        self.login_running = True
        self._cancel.clear()
        threading.Thread(target=self._login, args=(profile_dir,), daemon=True).start()

    def start(self, **kwargs) -> None:
        if self.busy:
            raise RuntimeError("Google Vids đang chạy một tác vụ khác.")
        self.running = True
        self._cancel.clear()
        threading.Thread(target=self._run, args=(kwargs,), daemon=True).start()

    def cancel(self) -> None:
        if self.busy:
            self._cancel.set()
            self.progress.emit(0, "Đang yêu cầu dừng Google Vids…")

    def _login(self, profile_dir: str) -> None:
        try:
            profile = open_google_vids_login(
                profile_dir,
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
            )
        except GoogleVidsCancelled:
            self.login_running = False
            self.canceled.emit()
        except Exception as exc:
            self.login_running = False
            self.login_failed.emit(str(exc))
        else:
            self.login_running = False
            self.login_finished.emit(profile)

    def _run(self, kwargs: dict) -> None:
        try:
            output = generate_google_vids_clip(
                **kwargs,
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
            )
        except GoogleVidsCancelled:
            self.running = False
            self.canceled.emit()
        except Exception as exc:
            self.running = False
            self.failed.emit(str(exc))
        else:
            self.running = False
            self.finished.emit(str(output))
