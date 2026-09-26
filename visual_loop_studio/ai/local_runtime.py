from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ai.comfyui import WAN_MODEL, WAN_TEXT_ENCODER, WAN_VAE
from utils.paths import LOG_DIR, USER_DATA_ROOT
from utils.process import hidden_process_kwargs


COMFY_PORTABLE_URL = (
    "https://github.com/Comfy-Org/ComfyUI/releases/latest/download/"
    "ComfyUI_windows_portable_nvidia.7z"
)
MIN_FREE_BYTES = 32 * 1024**3
COMFY_ARCHIVE_BYTES = 1_925_204_508
COMFY_EXTRACT_RESERVE_BYTES = 8 * 1024**3
INSTALL_MARGIN_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class ModelDownload:
    filename: str
    folder: str
    url: str
    sha256: str
    size_bytes: int


MODEL_DOWNLOADS = (
    ModelDownload(
        WAN_MODEL,
        "diffusion_models",
        "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/"
        "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors",
        "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
        9_999_658_848,
    ),
    ModelDownload(
        WAN_TEXT_ENCODER,
        "text_encoders",
        "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/"
        "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
        6_735_906_897,
    ),
    ModelDownload(
        WAN_VAE,
        "vae",
        "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/"
        "split_files/vae/wan2.2_vae.safetensors",
        "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
        1_409_400_960,
    ),
)


class RuntimeInstallCancelled(RuntimeError):
    pass


def default_runtime_root() -> Path:
    return USER_DATA_ROOT / "ai_runtime"


def portable_dir(install_root: str | Path) -> Path:
    root = Path(install_root).expanduser().resolve()
    candidates = (root / "ComfyUI_windows_portable", root)
    for candidate in candidates:
        if (candidate / "python_embeded" / "python.exe").is_file() or (candidate / "ComfyUI" / "main.py").is_file():
            return candidate
    try:
        nested = next(root.glob("*/python_embeded/python.exe"))
    except StopIteration:
        return candidates[0]
    return nested.parent.parent


def runtime_paths(install_root: str | Path) -> dict[str, Path]:
    root = Path(install_root).expanduser().resolve()
    portable = portable_dir(root)
    comfy = portable / "ComfyUI"
    return {
        "root": root,
        "portable": portable,
        "python": portable / "python_embeded" / "python.exe",
        "main": comfy / "main.py",
        "models": comfy / "models",
        "archive": root / "downloads" / "ComfyUI_windows_portable_nvidia.7z",
        "portable_marker": root / ".comfy-portable-ok",
    }


def missing_runtime_files(install_root: str | Path) -> list[str]:
    paths = runtime_paths(install_root)
    missing: list[str] = []
    for key, label in (("python", "Python ComfyUI"), ("main", "ComfyUI")):
        if not paths[key].is_file():
            missing.append(label)
    if not paths["portable_marker"].is_file():
        missing.append("xác nhận cài đặt ComfyUI hoàn chỉnh")
    for item in MODEL_DOWNLOADS:
        model = paths["models"] / item.folder / item.filename
        if not model.is_file() or model.stat().st_size < 1024 * 1024:
            missing.append(item.filename)
    return missing


def is_runtime_installed(install_root: str | Path) -> bool:
    if not str(install_root or "").strip():
        return False
    try:
        return not missing_runtime_files(install_root)
    except OSError:
        return False


def install_local_runtime(
    install_root: str | Path,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    progress = progress or (lambda _value, _message: None)
    cancelled = cancelled or (lambda: False)
    root = Path(install_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    required = required_free_bytes(root)
    if free < required and not is_runtime_installed(root):
        raise RuntimeError(
            f"Ổ đĩa chỉ còn {free / 1024**3:.1f} GB. Cần thêm khoảng {required / 1024**3:.1f} GB trống "
            "để tải/tiếp tục cài ComfyUI + Wan 2.2."
        )
    _raise_if_cancelled(cancelled)
    paths = runtime_paths(root)
    if not paths["python"].is_file() or not paths["main"].is_file() or not paths["portable_marker"].is_file():
        if not paths["archive"].is_file() or paths["archive"].stat().st_size < COMFY_ARCHIVE_BYTES:
            progress(1, "Đang tải ComfyUI Portable NVIDIA (có thể tiếp tục nếu mạng bị ngắt)…")
            _download(
                COMFY_PORTABLE_URL,
                paths["archive"],
                lambda value, message: progress(1 + round(value * .17), message),
                cancelled,
            )
        _raise_if_cancelled(cancelled)
        progress(19, "Đang giải nén ComfyUI Portable; bước này có thể mất vài phút…")
        try:
            import py7zr
        except ModuleNotFoundError as exc:
            raise RuntimeError("Thiếu thư viện py7zr. Hãy cập nhật/cài lại Visual Loop Studio.") from exc
        with py7zr.SevenZipFile(paths["archive"], mode="r") as archive:
            archive.extractall(path=root)
        paths = runtime_paths(root)
        if not paths["python"].is_file() or not paths["main"].is_file():
            raise RuntimeError("Đã giải nén nhưng không tìm thấy ComfyUI_windows_portable hợp lệ.")
        paths["portable_marker"].write_text("ok\n", encoding="ascii")
        paths["archive"].unlink(missing_ok=True)
    else:
        progress(20, "Đã có ComfyUI Portable; bỏ qua phần tải chương trình.")

    ranges = ((22, 62), (62, 91), (91, 99))
    for item, (start, end) in zip(MODEL_DOWNLOADS, ranges):
        _raise_if_cancelled(cancelled)
        destination = paths["models"] / item.folder / item.filename
        if _matches_sha256(destination, item.sha256):
            progress(end, f"Đã có model hợp lệ: {item.filename}")
            continue
        progress(start, f"Đang tải {item.filename}…")
        _download(
            item.url,
            destination,
            lambda value, message, start=start, end=end: progress(
                start + round(value * (end - start) / 100), message
            ),
            cancelled,
            item.sha256,
        )
    marker = root / "installed.json"
    marker.write_text(
        json.dumps({"runtime": str(paths["portable"]), "models": [item.filename for item in MODEL_DOWNLOADS]}, indent=2),
        encoding="utf-8",
    )
    progress(100, "Đã cài xong ComfyUI + Wan 2.2 Native.")
    return str(root)


def build_runtime_command(install_root: str | Path, base_url: str) -> tuple[list[str], Path]:
    paths = runtime_paths(install_root)
    parsed = urlparse(_normalized_url(base_url))
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Chỉ có thể tự chạy ComfyUI trên localhost.")
    port = parsed.port or 8188
    command = [
        str(paths["python"]), "-s", str(paths["main"]),
        "--windows-standalone-build", "--listen", "127.0.0.1",
        "--port", str(port), "--disable-auto-launch",
    ]
    return command, paths["portable"]


def required_free_bytes(install_root: str | Path) -> int:
    paths = runtime_paths(install_root)
    has_progress = paths["archive"].exists() or paths["archive"].with_suffix(".7z.part").exists()
    remaining = INSTALL_MARGIN_BYTES
    if not paths["python"].is_file() or not paths["main"].is_file() or not paths["portable_marker"].is_file():
        archive_partial = paths["archive"].with_suffix(paths["archive"].suffix + ".part")
        archive_have = paths["archive"].stat().st_size if paths["archive"].is_file() else (
            archive_partial.stat().st_size if archive_partial.is_file() else 0
        )
        remaining += max(0, COMFY_ARCHIVE_BYTES - archive_have) + COMFY_EXTRACT_RESERVE_BYTES
    for item in MODEL_DOWNLOADS:
        destination = paths["models"] / item.folder / item.filename
        partial = destination.with_suffix(destination.suffix + ".part")
        have = destination.stat().st_size if destination.is_file() else (partial.stat().st_size if partial.is_file() else 0)
        has_progress = has_progress or have > 0
        remaining += max(0, item.size_bytes - have)
    return max(remaining, 8 * 1024**3 if has_progress else MIN_FREE_BYTES)


class LocalRuntimeManager:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self._log_stream = None
        self._lock = threading.Lock()
        self._stopping = False

    def ensure_running(
        self,
        install_root: str,
        base_url: str,
        progress: Callable[[int, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: int = 240,
    ) -> None:
        progress = progress or (lambda _value, _message: None)
        cancelled = cancelled or (lambda: False)
        if server_ready(base_url):
            return
        if not is_runtime_installed(install_root):
            missing = ", ".join(missing_runtime_files(install_root)) if install_root else "ComfyUI + model Wan 2.2"
            raise RuntimeError(
                "AI Local chưa được cài đầy đủ (thiếu: " + missing + "). "
                "Bấm ‘Cài AI Local tự động’ trong phần Chuyển động AI."
            )
        with self._lock:
            if self._stopping:
                raise RuntimeInstallCancelled("Ứng dụng đang đóng; không khởi động ComfyUI.")
            if not self.process or self.process.poll() is not None:
                command, cwd = build_runtime_command(install_root, base_url)
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                self._close_log()
                self._log_stream = open(LOG_DIR / "comfyui.log", "ab", buffering=0)
                self.process = subprocess.Popen(
                    command,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=self._log_stream,
                    stderr=subprocess.STDOUT,
                    **hidden_process_kwargs(),
                )
        progress(1, "Đang tự khởi động ComfyUI ẩn trong nền…")
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            _raise_if_cancelled(cancelled)
            if server_ready(base_url):
                progress(3, "ComfyUI đã khởi động; đang nạp workflow Wan 2.2…")
                return
            process = self.process
            if process and process.poll() is not None:
                raise RuntimeError(
                    f"ComfyUI đã dừng với mã {process.returncode}. Xem log: {LOG_DIR / 'comfyui.log'}"
                )
            elapsed = int(time.monotonic() - started)
            progress(1, f"Đang chờ ComfyUI sẵn sàng… {elapsed} giây")
            time.sleep(1)
        raise TimeoutError(f"ComfyUI không sẵn sàng sau {timeout} giây. Xem log: {LOG_DIR / 'comfyui.log'}")

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            process = self.process
            self.process = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._close_log()

    def _close_log(self) -> None:
        if self._log_stream:
            try:
                self._log_stream.close()
            except OSError:
                pass
            self._log_stream = None


def server_ready(base_url: str, timeout: float = 1.5) -> bool:
    request = Request(
        _normalized_url(base_url) + "/system_stats",
        headers={"User-Agent": "VisualLoopStudio/1", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return False


def _download(
    url: str,
    destination: Path,
    progress: Callable[[int, str], None],
    cancelled: Callable[[], bool],
    expected_sha256: str = "",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    existing = partial.stat().st_size if partial.is_file() else 0
    headers = {"User-Agent": "VisualLoopStudio/1", "Accept": "application/octet-stream"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    request = Request(url, headers=headers)
    try:
        response = urlopen(request, timeout=60)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Không tải được {destination.name}: {exc}") from exc
    with response:
        resumed = existing > 0 and int(getattr(response, "status", 200)) == 206
        if not resumed:
            existing = 0
        length = int(response.headers.get("Content-Length") or 0)
        total = existing + length if length else 0
        mode = "ab" if resumed else "wb"
        downloaded = existing
        with open(partial, mode) as stream:
            while True:
                _raise_if_cancelled(cancelled)
                chunk = response.read(4 * 1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
                downloaded += len(chunk)
                percent = min(100, round(downloaded * 100 / total)) if total else 0
                progress(percent, f"Đang tải {destination.name}: {downloaded / 1024**3:.2f} GB")
    if expected_sha256:
        progress(100, f"Đang kiểm tra SHA-256 của {destination.name}…")
        actual = _sha256(partial, cancelled)
        if actual.lower() != expected_sha256.lower():
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"SHA-256 không khớp cho {destination.name}; file tải đã bị xóa.")
    partial.replace(destination)
    if expected_sha256:
        destination.with_suffix(destination.suffix + ".sha256-ok").write_text(
            expected_sha256.lower(), encoding="ascii"
        )


def _matches_sha256(path: Path, expected: str) -> bool:
    if not path.is_file() or path.stat().st_size < 1024 * 1024:
        return False
    marker = path.with_suffix(path.suffix + ".sha256-ok")
    try:
        if marker.is_file() and marker.read_text(encoding="ascii").strip().lower() == expected.lower():
            return True
        if _sha256(path, lambda: False) == expected.lower():
            marker.write_text(expected.lower(), encoding="ascii")
            return True
    except OSError:
        return False
    return False


def _sha256(path: Path, cancelled: Callable[[], bool]) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _raise_if_cancelled(cancelled)
            chunk = stream.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest().lower()


def _normalized_url(value: str) -> str:
    value = (value or "http://127.0.0.1:8188").strip().rstrip("/")
    return value if value.startswith(("http://", "https://")) else "http://" + value


def _raise_if_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise RuntimeInstallCancelled("Đã hủy cài đặt AI Local.")
