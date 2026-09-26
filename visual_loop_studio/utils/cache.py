from __future__ import annotations

import hashlib
from pathlib import Path

from utils.paths import CACHE_DIR, ensure_app_dirs


def cache_path(source: str, suffix: str = ".json") -> Path:
    ensure_app_dirs()
    target = Path(source)
    stamp = f"{target.resolve()}:{target.stat().st_size}:{target.stat().st_mtime_ns}"
    return CACHE_DIR / f"{hashlib.sha256(stamp.encode()).hexdigest()}{suffix}"
