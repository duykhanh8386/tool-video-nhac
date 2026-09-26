from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ai.comfyui import LocalGenerationCancelled, check_comfyui, generate_local_image_to_video


class LocalAiVideoWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)
    canceled = Signal()
    checked = Signal(bool, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.running = False
        self.checking = False
        self._cancel = threading.Event()

    def start(self, **kwargs) -> None:
        if self.running:
            raise RuntimeError("Một tác vụ Wan 2.2 local đang chạy.")
        self.running = True
        self._cancel.clear()
        threading.Thread(target=self._run, args=(kwargs,), daemon=True).start()

    def check(self, comfyui_url: str, workflow_path: str = "") -> None:
        if self.checking or self.running:
            raise RuntimeError("ComfyUI đang được kiểm tra hoặc đang tạo video.")
        self.checking = True
        threading.Thread(
            target=self._check,
            args=(comfyui_url, workflow_path),
            daemon=True,
        ).start()

    def cancel(self) -> None:
        if self.running:
            self._cancel.set()
            self.progress.emit(0, "Đang yêu cầu ComfyUI hủy tác vụ…")

    def _run(self, kwargs: dict) -> None:
        try:
            output = generate_local_image_to_video(
                **kwargs,
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
            )
        except LocalGenerationCancelled:
            self.running = False
            self.canceled.emit()
        except Exception as exc:
            self.running = False
            self.failed.emit(str(exc))
        else:
            self.running = False
            self.finished.emit(str(output))

    def _check(self, comfyui_url: str, workflow_path: str) -> None:
        try:
            stats = check_comfyui(comfyui_url, require_default_models=not bool(workflow_path))
            devices = stats.get("devices") or []
            device = devices[0] if devices else {}
            name = str(device.get("name") or device.get("type") or "GPU/CPU local")
            vram = device.get("vram_total")
            suffix = f" • VRAM {float(vram) / 1024**3:.1f} GB" if isinstance(vram, (int, float)) else ""
            message = f"ComfyUI sẵn sàng • {name}{suffix} • đã có node/model Wan 2.2."
        except Exception as exc:
            self.checked.emit(False, str(exc))
        else:
            self.checked.emit(True, message)
        finally:
            self.checking = False
