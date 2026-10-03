from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models.settings_model import AppSettings
from utils.paths import DATA_DIR, bundled_binary, ensure_app_dirs, portable_binary_setting
from utils.secret_store import set_secret


SETTINGS_FILE = DATA_DIR / "settings.json"


def read_json(path: str | Path, default: Any = None) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(target)


def load_settings() -> AppSettings:
    ensure_app_dirs()
    raw = read_json(SETTINGS_FILE, {}) or {}
    legacy_key = str(raw.get("gemini_api_key") or "").strip()
    if legacy_key:
        try:
            if set_secret("gemini_api_key", legacy_key):
                raw.pop("gemini_api_key", None)
                write_json(SETTINGS_FILE, raw)
        except OSError:
            # Keep the in-memory legacy value for this run if Credential
            # Manager is unavailable. save_settings still refuses plaintext.
            pass
    settings = AppSettings.from_dict(raw)
    if settings.ffmpeg_path in {"", "ffmpeg"} or not Path(settings.ffmpeg_path).exists():
        settings.ffmpeg_path = bundled_binary("ffmpeg")
    if settings.ffprobe_path in {"", "ffprobe"} or not Path(settings.ffprobe_path).exists():
        settings.ffprobe_path = bundled_binary("ffprobe")
    return settings


def save_settings(settings: AppSettings) -> None:
    data = settings.to_dict()
    # API keys are secrets, not ordinary application preferences.
    data.pop("gemini_api_key", None)
    data["ffmpeg_path"] = portable_binary_setting(settings.ffmpeg_path, "ffmpeg")
    data["ffprobe_path"] = portable_binary_setting(settings.ffprobe_path, "ffprobe")
    write_json(SETTINGS_FILE, data)
