from __future__ import annotations

import re
import os
import sys
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else APP_DIR
if FROZEN:
    USER_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", INSTALL_DIR)) / "VisualLoopStudio"
else:
    USER_DATA_ROOT = APP_DIR
DATA_DIR = USER_DATA_ROOT / "data"
CACHE_DIR = USER_DATA_ROOT / "cache"
LOG_DIR = USER_DATA_ROOT / "logs"


def ensure_app_dirs() -> None:
    for path in (DATA_DIR, CACHE_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def safe_filename(name: str, default_prefix: str = "visual", default_suffix: str = ".mp4") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    name = name.rstrip(". ")
    if not name:
        name = f"{default_prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    if not Path(name).suffix:
        name += default_suffix
    return name


def unique_output(folder: str, name: str, prefix: str = "visual", required_suffix: str | None = None) -> Path:
    destination = Path(folder).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if required_suffix:
        required_suffix = required_suffix if required_suffix.startswith(".") else "." + required_suffix
        name = (Path(name).stem + required_suffix) if name else ""
    requested_suffix = Path(name).suffix if name else (required_suffix or ".mp4")
    stem_path = destination / safe_filename(name, prefix, requested_suffix)
    if not stem_path.exists():
        return stem_path
    index = 2
    while True:
        candidate = stem_path.with_name(f"{stem_path.stem}_{index}{stem_path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def ffmpeg_filter_path(path: str) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def bundled_binary(name: str) -> str:
    executable = name if name.lower().endswith(".exe") else name + ".exe"
    candidate = RESOURCE_DIR / "vendor" / "bin" / executable
    return str(candidate) if candidate.is_file() else name.removesuffix(".exe")


def portable_binary_setting(value: str, name: str) -> str:
    try:
        if Path(value).resolve() == Path(bundled_binary(name)).resolve():
            return name
    except OSError:
        pass
    return value
