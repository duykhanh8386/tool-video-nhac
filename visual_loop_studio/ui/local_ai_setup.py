from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ai.local_runtime import RuntimeInstallCancelled, install_local_runtime


class LocalAiSetupWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)
    canceled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.running = False
        self._cancel = threading.Event()

    def start(self, install_root: str, wan_variant: str = "quality") -> None:
        if self.running:
            raise RuntimeError("Cài đặt AI Local đang chạy.")
        self.running = True
        self._cancel.clear()
        threading.Thread(target=self._run, args=(install_root, wan_variant), daemon=True).start()

    def cancel(self) -> None:
        if self.running:
            self._cancel.set()
            self.progress.emit(0, "Đang dừng tải; lần sau có thể tiếp tục từ phần đã tải…")

    def _run(self, install_root: str, wan_variant: str) -> None:
        try:
            root = install_local_runtime(
                install_root,
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
                wan_variant=wan_variant,
            )
        except RuntimeInstallCancelled:
            self.running = False
            self.canceled.emit()
        except Exception as exc:
            self.running = False
            self.failed.emit(str(exc))
        else:
            self.running = False
            self.finished.emit(root)
