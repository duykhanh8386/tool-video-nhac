from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ai.comfyui import (
    HUNYUAN_BYT5_ENCODER,
    HUNYUAN_CLIP_VISION,
    HUNYUAN_MODEL,
    HUNYUAN_TEXT_ENCODER,
    HUNYUAN_VAE,
    LOCAL_MODEL_HUNYUAN,
    LOCAL_MODEL_LTX,
    LTX_MODEL,
    LTX_TEXT_ENCODER,
    WAN_DMD_LORA,
    WAN_MODEL,
    WAN_TEXT_ENCODER,
    WAN_VAE,
    WAN_VARIANT_DMD,
    WAN_VARIANT_QUALITY,
    local_model_label,
    normalize_wan_variant,
)
from utils.paths import LOG_DIR, USER_DATA_ROOT
from utils.process import hidden_process_kwargs


COMFY_RELEASE_BASE_URL = "https://github.com/Comfy-Org/ComfyUI/releases/latest/download/"
SEVEN_ZIP_URL = "https://github.com/ip7z/7zip/releases/download/26.03/7zr.exe"
SEVEN_ZIP_SHA256 = "ad4c82fadcbdf93c03b4fc440f300509c7d60c5c2f4d183e35d9d70d6957037d"
MIN_FREE_BYTES = 32 * 1024**3
COMFY_ARCHIVE_BYTES = 1_925_204_508
COMFY_EXTRACT_RESERVE_BYTES = 8 * 1024**3
INSTALL_MARGIN_BYTES = 2 * 1024**3


@dataclass(frozen=True)
class RuntimeBackend:
    key: str
    label: str
    archive_name: str
    launch_args: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return COMFY_RELEASE_BASE_URL + self.archive_name


RUNTIME_BACKENDS = {
    "nvidia": RuntimeBackend("nvidia", "NVIDIA CUDA", "ComfyUI_windows_portable_nvidia.7z"),
    "intel": RuntimeBackend("intel", "Intel Arc XPU", "ComfyUI_windows_portable_intel.7z"),
    "amd": RuntimeBackend("amd", "AMD", "ComfyUI_windows_portable_amd.7z"),
    # The NVIDIA portable package can also run without CUDA when --cpu is used.
    "cpu": RuntimeBackend("cpu", "CPU", "ComfyUI_windows_portable_nvidia.7z", ("--cpu",)),
}


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

DMD_LORA_DOWNLOAD = ModelDownload(
    WAN_DMD_LORA,
    "loras",
    "https://huggingface.co/Perflow-Shuai/"
    "Wan2.2-5B-NonAR-DMD-4Step-LoRA-r64-iter1600/resolve/main/adapter_model.safetensors",
    "da4a75094b4afdf5fbdf47a07530151b9ef47f2fffebc4c8675683ad049c32dc",
    644_949_280,
)
DMD_LORA_SOURCE_FILENAME = "wan2.2_5b_nonar_dmd_4step_lora_r64_peft.safetensors"
DMD_LORA_TENSOR_COUNT = 600

LTX_DOWNLOADS = (
    ModelDownload(
        LTX_MODEL,
        "checkpoints",
        "https://huggingface.co/Lightricks/LTX-Video/resolve/main/"
        "ltxv-2b-0.9.6-distilled-04-25.safetensors",
        "94891bd4bd08de30d484befbfc54fdcffe6d1596a131baad700b9baa5e1de86b",
        6_340_744_028,
    ),
    ModelDownload(
        LTX_TEXT_ENCODER,
        "text_encoders",
        "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/"
        "t5xxl_fp8_e4m3fn_scaled.safetensors",
        "a498f0485dc9536735258018417c3fd7758dc3bccc0a645feaa472b34955557a",
        5_157_348_688,
    ),
)

HUNYUAN_DOWNLOADS = (
    ModelDownload(
        HUNYUAN_MODEL,
        "diffusion_models",
        "https://huggingface.co/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/"
        "split_files/diffusion_models/hunyuanvideo1.5_480p_i2v_step_distilled_fp8_scaled.safetensors",
        "302636263ad01e2659a18b78e96e95f44433b92def7cae1dab29b5105eeb63b1",
        8_335_127_098,
    ),
    ModelDownload(
        HUNYUAN_TEXT_ENCODER,
        "text_encoders",
        "https://huggingface.co/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/"
        "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4",
        9_384_670_680,
    ),
    ModelDownload(
        HUNYUAN_BYT5_ENCODER,
        "text_encoders",
        "https://huggingface.co/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/"
        "split_files/text_encoders/byt5_small_glyphxl_fp16.safetensors",
        "516910bb4c9b225370290e40585d1b0e6c8cd3583690f7eec2f7fb593990fb48",
        438_643_184,
    ),
    ModelDownload(
        HUNYUAN_VAE,
        "vae",
        "https://huggingface.co/Comfy-Org/HunyuanVideo_1.5_repackaged/resolve/main/"
        "split_files/vae/hunyuanvideo15_vae_fp16.safetensors",
        "e7c3091949c27e2d55ae6d5df917b99dadfebbf308e5a50d0ade0d16c90297ae",
        2_521_292_758,
    ),
    ModelDownload(
        HUNYUAN_CLIP_VISION,
        "clip_vision",
        "https://huggingface.co/Comfy-Org/sigclip_vision_384/resolve/main/"
        "sigclip_vision_patch14_384.safetensors",
        "1fee501deabac72f0ed17610307d7131e3e9d1e838d0363aa3c2b97a6e03fb33",
        856_505_640,
    ),
)


def model_downloads_for_variant(wan_variant: str = WAN_VARIANT_QUALITY) -> tuple[ModelDownload, ...]:
    variant = normalize_wan_variant(wan_variant)
    if variant == WAN_VARIANT_DMD:
        return (*MODEL_DOWNLOADS, DMD_LORA_DOWNLOAD)
    if variant == LOCAL_MODEL_LTX:
        return LTX_DOWNLOADS
    if variant == LOCAL_MODEL_HUNYUAN:
        return HUNYUAN_DOWNLOADS
    return MODEL_DOWNLOADS


def _install_model_download(
    item: ModelDownload,
    destination: Path,
    install_root: Path,
    progress: Callable[[int, str], None],
    cancelled: Callable[[], bool],
) -> None:
    if item != DMD_LORA_DOWNLOAD:
        _download(item.url, destination, progress, cancelled, item.sha256)
        return

    source = install_root / "downloads" / DMD_LORA_SOURCE_FILENAME
    if not _matches_sha256(source, item.sha256):
        _download(item.url, source, progress, cancelled, item.sha256)
    _raise_if_cancelled(cancelled)
    progress(97, "Đang chuyển key LoRA DMD sang định dạng ComfyUI…")
    _convert_dmd_lora_for_comfyui(source, destination, cancelled)
    if not _is_converted_dmd_lora(destination):
        raise RuntimeError("Đã chuyển LoRA DMD nhưng file kết quả không tương thích ComfyUI.")
    source.unlink(missing_ok=True)
    progress(100, f"Đã kiểm tra tương thích ComfyUI: {destination.name}")


def _model_download_ready(path: Path, item: ModelDownload) -> bool:
    if item == DMD_LORA_DOWNLOAD:
        return _is_converted_dmd_lora(path)
    return _matches_sha256(path, item.sha256)


def _read_safetensors_header(path: Path) -> tuple[dict, int]:
    if not path.is_file() or path.stat().st_size < 8:
        raise ValueError("File safetensors không tồn tại hoặc bị thiếu dữ liệu.")
    with path.open("rb") as stream:
        raw_length = stream.read(8)
        header_length = struct.unpack("<Q", raw_length)[0]
        if not 2 <= header_length <= 16 * 1024 * 1024:
            raise ValueError("Header safetensors không hợp lệ.")
        header = json.loads(stream.read(header_length).decode("utf-8"))
    if not isinstance(header, dict):
        raise ValueError("Header safetensors không phải object JSON.")
    return header, header_length


def _is_converted_dmd_lora(path: Path) -> bool:
    try:
        if path.stat().st_size < 600 * 1024**2:
            return False
        header, _header_length = _read_safetensors_header(path)
    except (OSError, ValueError, json.JSONDecodeError, struct.error, UnicodeDecodeError):
        return False
    tensor_keys = [key for key in header if key != "__metadata__"]
    return (
        len(tensor_keys) == DMD_LORA_TENSOR_COUNT
        and all(key.startswith("diffusion_model.blocks.") for key in tensor_keys)
        and all(key.endswith((".lora_A.weight", ".lora_B.weight")) for key in tensor_keys)
    )


def _convert_dmd_lora_for_comfyui(
    source: Path,
    destination: Path,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Rewrite PEFT key names without loading or changing the tensor data."""
    cancelled = cancelled or (lambda: False)
    header, old_header_length = _read_safetensors_header(source)
    converted: dict = {}
    converted_count = 0
    prefix = "base_model.model."
    for key, value in header.items():
        if key == "__metadata__":
            new_key = key
        elif key.startswith(prefix) and key.endswith((".lora_A.weight", ".lora_B.weight")):
            new_key = "diffusion_model." + key[len(prefix):]
            converted_count += 1
        else:
            raise RuntimeError(f"LoRA DMD có tensor không hỗ trợ: {key}")
        if new_key in converted:
            raise RuntimeError(f"LoRA DMD có tensor trùng sau khi chuyển đổi: {new_key}")
        converted[new_key] = value
    if converted_count != DMD_LORA_TENSOR_COUNT:
        raise RuntimeError(
            f"LoRA DMD cần {DMD_LORA_TENSOR_COUNT} tensor nhưng tìm thấy {converted_count}; "
            "nguồn model có thể đã thay đổi."
        )

    header_bytes = json.dumps(converted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header_bytes += b" " * (-len(header_bytes) % 8)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".converting")
    try:
        with source.open("rb") as source_stream, temporary.open("wb") as output_stream:
            source_stream.seek(8 + old_header_length)
            output_stream.write(struct.pack("<Q", len(header_bytes)))
            output_stream.write(header_bytes)
            while True:
                _raise_if_cancelled(cancelled)
                chunk = source_stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                output_stream.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class RuntimeInstallCancelled(RuntimeError):
    pass


def windows_video_adapters() -> tuple[str, ...]:
    """Return display adapter names without spawning a visible shell."""
    if os.name != "nt":
        return ()

    class DisplayDevice(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("DeviceName", ctypes.c_wchar * 32),
            ("DeviceString", ctypes.c_wchar * 128),
            ("StateFlags", ctypes.c_ulong),
            ("DeviceID", ctypes.c_wchar * 128),
            ("DeviceKey", ctypes.c_wchar * 128),
        ]

    try:
        enum_display_devices = ctypes.windll.user32.EnumDisplayDevicesW
    except (AttributeError, OSError):
        return ()

    names: list[str] = []
    index = 0
    while True:
        device = DisplayDevice()
        device.cb = ctypes.sizeof(device)
        try:
            found = enum_display_devices(None, index, ctypes.byref(device), 0)
        except (AttributeError, OSError):
            break
        if not found:
            break
        name = str(device.DeviceString or "").strip()
        if name and name not in names:
            names.append(name)
        index += 1
    return tuple(names)


def detect_runtime_backend(adapters: tuple[str, ...] | list[str] | None = None) -> RuntimeBackend:
    names = tuple(adapters) if adapters is not None else windows_video_adapters()
    normalized = " | ".join(names).casefold()
    if "nvidia" in normalized or "geforce" in normalized or "quadro" in normalized:
        return RUNTIME_BACKENDS["nvidia"]
    if "amd" in normalized or "radeon" in normalized or "advanced micro devices" in normalized:
        return RUNTIME_BACKENDS["amd"]
    if "intel" in normalized and "arc" in normalized:
        return RUNTIME_BACKENDS["intel"]
    return RUNTIME_BACKENDS["cpu"]


def validate_local_model_backend(
    wan_variant: str,
    backend: RuntimeBackend | str | None = None,
) -> RuntimeBackend:
    selected = RUNTIME_BACKENDS.get(backend, None) if isinstance(backend, str) else backend
    selected = selected or detect_runtime_backend()
    if normalize_wan_variant(wan_variant) == LOCAL_MODEL_HUNYUAN and selected.key != "nvidia":
        raise RuntimeError(
            "HunyuanVideo 1.5 trong tool chỉ hỗ trợ NVIDIA CUDA và khuyến nghị từ 16 GB VRAM. "
            f"Máy hiện được nhận diện là {selected.label}; hãy chọn Wan hoặc LTX-Video cho máy này."
        )
    return selected


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


def runtime_paths(
    install_root: str | Path,
    backend: RuntimeBackend | str | None = None,
) -> dict[str, Path | RuntimeBackend]:
    root = Path(install_root).expanduser().resolve()
    selected = RUNTIME_BACKENDS.get(backend, None) if isinstance(backend, str) else backend
    selected = selected or detect_runtime_backend()
    portable = portable_dir(root)
    comfy = portable / "ComfyUI"
    return {
        "root": root,
        "portable": portable,
        "backend": selected,
        "python": portable / "python_embeded" / "python.exe",
        "main": comfy / "main.py",
        "models": comfy / "models",
        "archive": root / "downloads" / selected.archive_name,
        "seven_zip": root / "tools" / "7zr.exe",
        "portable_marker": root / ".comfy-portable-ok",
    }


def installed_runtime_backend(paths: dict[str, Path | RuntimeBackend]) -> str:
    marker = Path(paths["portable_marker"])
    try:
        marker_value = marker.read_text(encoding="utf-8").strip().casefold()
    except OSError:
        marker_value = ""
    if marker_value in RUNTIME_BACKENDS:
        return marker_value

    version_file = Path(paths["portable"]) / "python_embeded" / "Lib" / "site-packages" / "torch" / "version.py"
    try:
        version_text = version_file.read_text(encoding="utf-8", errors="ignore").casefold()
    except OSError:
        return ""
    for attribute, backend_key in (("xpu", "intel"), ("hip", "amd"), ("cuda", "nvidia")):
        match = re.search(rf"(?m)^{attribute}[^\n=]*=\s*(['\"])([^'\"]+)\1", version_text)
        if match and match.group(2).strip().casefold() not in {"none", "null"}:
            return backend_key
    return ""


def missing_runtime_files(
    install_root: str | Path,
    expected_backend: RuntimeBackend | str | None = None,
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> list[str]:
    selected = RUNTIME_BACKENDS.get(expected_backend, None) if isinstance(expected_backend, str) else expected_backend
    selected = selected or detect_runtime_backend()
    paths = runtime_paths(install_root, selected)
    missing: list[str] = []
    try:
        validate_local_model_backend(wan_variant, selected)
    except RuntimeError as exc:
        missing.append(str(exc))
    if paths["portable_marker"].is_file():
        installed = installed_runtime_backend(paths)
        if installed and installed != selected.key:
            installed_label = RUNTIME_BACKENDS.get(installed, RUNTIME_BACKENDS["cpu"]).label
            missing.append(
                f"backend {installed_label}; m\u00e1y n\u00e0y c\u1ea7n {selected.label}"
            )
    for key, label in (("python", "Python ComfyUI"), ("main", "ComfyUI")):
        if not paths[key].is_file():
            missing.append(label)
    if not paths["portable_marker"].is_file():
        missing.append("xác nhận cài đặt ComfyUI hoàn chỉnh")
    for item in model_downloads_for_variant(wan_variant):
        model = paths["models"] / item.folder / item.filename
        installed = (
            _is_converted_dmd_lora(model)
            if item == DMD_LORA_DOWNLOAD
            else model.is_file() and model.stat().st_size >= 1024 * 1024
        )
        if not installed:
            missing.append(item.filename)
    return missing


def is_runtime_installed(
    install_root: str | Path,
    expected_backend: RuntimeBackend | str | None = None,
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> bool:
    if not str(install_root or "").strip():
        return False
    try:
        return not missing_runtime_files(install_root, expected_backend, wan_variant)
    except OSError:
        return False


def install_local_runtime(
    install_root: str | Path,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> str:
    progress = progress or (lambda _value, _message: None)
    cancelled = cancelled or (lambda: False)
    wan_variant = normalize_wan_variant(wan_variant)
    downloads = model_downloads_for_variant(wan_variant)
    root = Path(install_root).expanduser().resolve()
    backend = validate_local_model_backend(wan_variant)
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    required = required_free_bytes(root, backend, wan_variant)
    if free < required and not is_runtime_installed(root, backend, wan_variant):
        raise RuntimeError(
            f"Ổ đĩa chỉ còn {free / 1024**3:.1f} GB. Cần thêm khoảng {required / 1024**3:.1f} GB trống "
            f"để tải/tiếp tục cài ComfyUI + {local_model_label(wan_variant)}."
        )
    _raise_if_cancelled(cancelled)
    paths = runtime_paths(root, backend)
    installed_backend = installed_runtime_backend(paths)
    switching_backend = bool(installed_backend and installed_backend != backend.key)
    portable_ready = (
        paths["python"].is_file()
        and paths["main"].is_file()
        and paths["portable_marker"].is_file()
        and not switching_backend
    )
    if not portable_ready:
        if not paths["archive"].is_file() or paths["archive"].stat().st_size < 256 * 1024**2:
            progress(1, f"Đã nhận diện {backend.label}. Đang tải đúng bản ComfyUI Portable…")
            _download(
                backend.url,
                paths["archive"],
                lambda value, message: progress(1 + round(value * .17), message),
                cancelled,
            )
        _raise_if_cancelled(cancelled)
        if not _matches_sha256(paths["seven_zip"], SEVEN_ZIP_SHA256, minimum_bytes=100_000):
            progress(18, "Đang tải 7-Zip chính thức để giải nén ComfyUI…")
            _download(
                SEVEN_ZIP_URL,
                paths["seven_zip"],
                lambda _value, message: progress(18, message),
                cancelled,
                SEVEN_ZIP_SHA256,
            )
        _raise_if_cancelled(cancelled)
        if switching_backend:
            progress(19, f"Đang chuyển backend {installed_backend} sang {backend.label}; giữ nguyên các model đã tải…")
            _prepare_backend_switch(paths)
        progress(19, f"Đang giải nén ComfyUI Portable {backend.label}; bước này có thể mất vài phút…")
        extract_7z_archive(paths["archive"], root, paths["seven_zip"])
        paths = runtime_paths(root, backend)
        if not paths["python"].is_file() or not paths["main"].is_file():
            raise RuntimeError("Đã giải nén nhưng không tìm thấy ComfyUI_windows_portable hợp lệ.")
        paths["portable_marker"].write_text(backend.key + "\n", encoding="ascii")
        paths["archive"].unlink(missing_ok=True)
    else:
        progress(20, f"Đã có ComfyUI Portable {backend.label}; bỏ qua phần tải chương trình.")

    total_bytes = sum(item.size_bytes for item in downloads)
    consumed_bytes = 0
    for item in downloads:
        start = 22 + round(77 * consumed_bytes / total_bytes)
        consumed_bytes += item.size_bytes
        end = 22 + round(77 * consumed_bytes / total_bytes)
        _raise_if_cancelled(cancelled)
        destination = paths["models"] / item.folder / item.filename
        if _model_download_ready(destination, item):
            progress(end, f"Đã có model hợp lệ: {item.filename}")
            continue
        progress(start, f"Đang tải {item.filename}…")
        _install_model_download(
            item, destination, root,
            lambda value, message, start=start, end=end: progress(
                start + round(value * (end - start) / 100), message
            ), cancelled,
        )
    marker = root / "installed.json"
    marker.write_text(
        json.dumps(
            {
                "runtime": str(paths["portable"]),
                "backend": backend.key,
                "backend_label": backend.label,
                "wan_variant": wan_variant,
                "local_model": wan_variant,
                "models": [item.filename for item in downloads],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    model_label = local_model_label(wan_variant)
    progress(100, f"Đã cài xong ComfyUI {backend.label} + {model_label}.")
    return str(root)


def build_runtime_command(
    install_root: str | Path,
    base_url: str,
    backend: RuntimeBackend | str | None = None,
) -> tuple[list[str], Path]:
    selected = RUNTIME_BACKENDS.get(backend, None) if isinstance(backend, str) else backend
    selected = selected or detect_runtime_backend()
    paths = runtime_paths(install_root, selected)
    parsed = urlparse(_normalized_url(base_url))
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Chỉ có thể tự chạy ComfyUI trên localhost.")
    port = parsed.port or 8188
    command = [
        str(paths["python"]), "-s", str(paths["main"]),
        *selected.launch_args,
        "--windows-standalone-build", "--listen", "127.0.0.1",
        "--port", str(port), "--disable-auto-launch",
    ]
    return command, paths["portable"]


def required_free_bytes(
    install_root: str | Path,
    backend: RuntimeBackend | str | None = None,
    wan_variant: str = WAN_VARIANT_QUALITY,
) -> int:
    selected = RUNTIME_BACKENDS.get(backend, None) if isinstance(backend, str) else backend
    selected = selected or detect_runtime_backend()
    paths = runtime_paths(install_root, selected)
    archive_partial = paths["archive"].with_suffix(paths["archive"].suffix + ".part")
    has_progress = paths["archive"].exists() or archive_partial.exists()
    remaining = INSTALL_MARGIN_BYTES
    installed = installed_runtime_backend(paths)
    runtime_missing = not paths["python"].is_file() or not paths["main"].is_file()
    backend_mismatch = bool(installed and installed != selected.key)
    if runtime_missing or not paths["portable_marker"].is_file() or backend_mismatch:
        archive_have = paths["archive"].stat().st_size if paths["archive"].is_file() else (
            archive_partial.stat().st_size if archive_partial.is_file() else 0
        )
        remaining += max(0, COMFY_ARCHIVE_BYTES - archive_have) + COMFY_EXTRACT_RESERVE_BYTES
    for item in model_downloads_for_variant(wan_variant):
        destination = paths["models"] / item.folder / item.filename
        partial = destination.with_suffix(destination.suffix + ".part")
        have = destination.stat().st_size if destination.is_file() else (partial.stat().st_size if partial.is_file() else 0)
        has_progress = has_progress or have > 0
        remaining += max(0, item.size_bytes - have)
    return max(remaining, 8 * 1024**3 if has_progress else MIN_FREE_BYTES)


def _prepare_backend_switch(paths: dict[str, Path | RuntimeBackend]) -> None:
    root = Path(paths["root"]).resolve()
    python_dir = (Path(paths["portable"]) / "python_embeded").resolve()
    try:
        python_dir.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Thư mục Python ComfyUI nằm ngoài thư mục AI Local; không thể chuyển backend an toàn.") from exc

    if python_dir.is_symlink() or python_dir.is_file():
        python_dir.unlink(missing_ok=True)
    elif python_dir.is_dir():
        shutil.rmtree(python_dir)
    Path(paths["portable_marker"]).unlink(missing_ok=True)


def extract_7z_archive(archive: str | Path, destination: str | Path, seven_zip: str | Path) -> None:
    archive_path = Path(archive).resolve()
    destination_path = Path(destination).resolve()
    seven_zip_path = Path(seven_zip).resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(seven_zip_path), "x", "-y", f"-o{destination_path}", str(archive_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **hidden_process_kwargs(),
    )
    if result.returncode != 0:
        details = (result.stdout or "").strip()[-2000:]
        raise RuntimeError(
            f"7-Zip không giải nén được ComfyUI (mã {result.returncode}). "
            f"File tải vẫn được giữ để thử lại.\n{details}"
        )


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
        wan_variant: str = WAN_VARIANT_QUALITY,
    ) -> None:
        progress = progress or (lambda _value, _message: None)
        cancelled = cancelled or (lambda: False)
        validate_local_model_backend(wan_variant)
        if server_ready(base_url):
            return
        if not is_runtime_installed(install_root, wan_variant=wan_variant):
            missing = ", ".join(missing_runtime_files(install_root, wan_variant=wan_variant)) if install_root else f"ComfyUI + {local_model_label(wan_variant)}"
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
                progress(3, f"ComfyUI đã khởi động; đang nạp {local_model_label(wan_variant)}…")
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


def _matches_sha256(path: Path, expected: str, minimum_bytes: int = 1024 * 1024) -> bool:
    if not path.is_file() or path.stat().st_size < minimum_bytes:
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
