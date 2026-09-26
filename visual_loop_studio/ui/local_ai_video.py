from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from ai.comfyui import (
    WAN_VARIANT_DMD,
    LocalGenerationCancelled,
    check_comfyui,
    generate_local_image_to_video,
    normalize_wan_variant,
)
from ai.local_runtime import LocalRuntimeManager, RuntimeInstallCancelled


class LocalAiVideoWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)
    canceled = Signal()
    checked = Signal(bool, str)

    def __init__(self, runtime_manager: LocalRuntimeManager, parent: QObject | None = None):
        super().__init__(parent)
        self.runtime_manager = runtime_manager
        self.running = False
        self.checking = False
        self._cancel = threading.Event()

    def start(self, **kwargs) -> None:
        if self.running:
            raise RuntimeError("Một tác vụ Wan 2.2 local đang chạy.")
        self.running = True
        self._cancel.clear()
        threading.Thread(target=self._run, args=(kwargs,), daemon=True).start()

    def check(self, comfyui_url: str, workflow_path: str = "", runtime_root: str = "", wan_variant: str = "quality") -> None:
        if self.checking or self.running:
            raise RuntimeError("ComfyUI đang được kiểm tra hoặc đang tạo video.")
        self.checking = True
        self._cancel.clear()
        threading.Thread(
            target=self._check,
            args=(comfyui_url, workflow_path, runtime_root, wan_variant),
            daemon=True,
        ).start()

    def cancel(self) -> None:
        if self.running or self.checking:
            self._cancel.set()
            self.progress.emit(0, "Đang yêu cầu ComfyUI hủy tác vụ…")

    def _run(self, kwargs: dict) -> None:
        try:
            runtime_root = str(kwargs.pop("runtime_root", "") or "")
            self.runtime_manager.ensure_running(
                runtime_root,
                str(kwargs.get("comfyui_url") or ""),
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
                wan_variant=str(kwargs.get("wan_variant") or "quality"),
            )
            output = generate_local_image_to_video(
                **kwargs,
                progress=lambda value, message: self.progress.emit(value, message),
                cancelled=self._cancel.is_set,
            )
        except (LocalGenerationCancelled, RuntimeInstallCancelled):
            self.running = False
            self.canceled.emit()
        except Exception as exc:
            self.running = False
            self.failed.emit(str(exc))
        else:
            self.running = False
            self.finished.emit(str(output))

    def _check(self, comfyui_url: str, workflow_path: str, runtime_root: str, wan_variant: str) -> None:
        try:
            if runtime_root:
                self.runtime_manager.ensure_running(
                    runtime_root,
                    comfyui_url,
                    progress=lambda value, message: self.progress.emit(value, message),
                    cancelled=self._cancel.is_set,
                    wan_variant=wan_variant,
                )
            stats = check_comfyui(
                comfyui_url, require_default_models=not bool(workflow_path), wan_variant=wan_variant
            )
            devices = stats.get("devices") or []
            device = devices[0] if devices else {}
            name = str(device.get("name") or device.get("type") or "GPU/CPU local")
            vram = device.get("vram_total")
            suffix = f" • VRAM {float(vram) / 1024**3:.1f} GB" if isinstance(vram, (int, float)) else ""
            mode = "DMD 4 bước" if normalize_wan_variant(wan_variant) == WAN_VARIANT_DMD else "Chất lượng 20 bước"
            message = f"ComfyUI sẵn sàng • {name}{suffix} • Wan 2.2 {mode} đã sẵn sàng."
        except Exception as exc:
            self.checked.emit(False, str(exc))
        else:
            self.checked.emit(True, message)
        finally:
            self.checking = False
