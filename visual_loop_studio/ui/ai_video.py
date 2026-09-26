from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ai.veo import GenerationCancelled, generate_image_to_video


class AiVideoWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)
    canceled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.running = False
        self._cancel = threading.Event()

    def start(self, **kwargs) -> None:
        if self.running:
            raise RuntimeError("Một tác vụ tạo video AI đang chạy.")
        self.running = True
        self._cancel.clear()
        threading.Thread(target=self._run, args=(kwargs,), daemon=True).start()

    def cancel(self) -> None:
        if self.running:
            self._cancel.set()
            self.progress.emit(0, "Đang yêu cầu hủy tác vụ AI…")

    def _run(self, kwargs: dict) -> None:
        try:
            output = generate_image_to_video(
                **kwargs,
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
            )
        except GenerationCancelled:
            self.running = False
            self.canceled.emit()
        except Exception as exc:
            self.running = False
            self.failed.emit(str(exc))
        else:
            self.running = False
            self.finished.emit(str(output))
