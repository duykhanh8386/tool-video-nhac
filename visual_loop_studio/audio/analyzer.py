from __future__ import annotations

import json
import subprocess

from utils.process import hidden_process_kwargs
from pathlib import Path

from utils.cache import cache_path


def analyze_volume(path: str, ffmpeg: str = "ffmpeg") -> dict[str, float]:
    cached = cache_path(path, ".volume.json")
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    command = [ffmpeg, "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"]
    result = subprocess.run(
        command, capture_output=True, text=True, errors="replace", check=False,
        **hidden_process_kwargs(),
    )
    values = {"mean_volume": 0.0, "max_volume": 0.0}
    for line in result.stderr.splitlines():
        for key in tuple(values):
            if f"{key}:" in line:
                try:
                    values[key] = float(line.split(f"{key}:", 1)[1].split("dB", 1)[0].strip())
                except ValueError:
                    pass
    cached.write_text(json.dumps(values), encoding="utf-8")
    return values
