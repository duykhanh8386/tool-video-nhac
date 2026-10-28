from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtGui import QImage

from auth.local_generation import (
    conform_video_duration,
    create_neutral_canvas_image,
    LocalVideoBatchRunnerThread,
)
from models.settings_model import AppSettings
from render.ffprobe import probe_media


def test_create_neutral_canvas_image(tmp_path: Path):
    canvas_file = tmp_path / "test_canvas.png"
    out = create_neutral_canvas_image(832, 480, canvas_file)
    assert out.exists()
    img = QImage(str(out))
    assert img.width() == 832
    assert img.height() == 480


def test_conform_video_duration(tmp_path: Path):
    raw_video = tmp_path / "raw_sample_7s.mp4"
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
        "-t", "7.0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(raw_video)
    ]
    res = subprocess.run(cmd, capture_output=True, check=False)
    assert res.returncode == 0
    assert raw_video.exists()

    conformed = tmp_path / "conformed_5s.mp4"
    out = conform_video_duration(raw_video, conformed, target_seconds=5.0)
    assert out.exists()

    info = probe_media(str(conformed))
    assert 4.9 <= info.duration <= 5.2


def test_local_farm_thread_mock(tmp_path: Path):
    output_dir = tmp_path / "local_output"
    prompts = [
        "A peaceful drone shot of misty mountain lake",
        "Golden sunset over ocean waves",
    ]

    runner = LocalVideoBatchRunnerThread(
        prompts=prompts,
        output_dir=output_dir,
        comfyui_url="http://127.0.0.1:8188",
        model_variant="dmd4",
        resolution="832x480",
        duration_seconds=5,
    )

    # Mock server_ready and generate_local_image_to_video
    dummy_video = tmp_path / "dummy_comfy_out.mp4"
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
        "-t", "5.04",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(dummy_video)
    ]
    subprocess.run(cmd, capture_output=True, check=False)

    logs = []
    stats = []
    runner.log_signal.connect(logs.append)
    runner.stats_signal.connect(lambda t, c, f, r: stats.append((t, c, f, r)))

    with patch("auth.local_generation.server_ready", return_value=True):
        with patch("auth.local_generation.generate_local_image_to_video", return_value=str(dummy_video)):
            runner.run()

    assert any("Bắt đầu Farm Video AI Local" in l for l in logs)
    assert any("Thành công" in l for l in logs)
    assert len(list(output_dir.glob("*.mp4"))) == 2


def test_local_farm_settings_persistence():
    settings = AppSettings()
    assert settings.local_farm_model == "dmd4"
    assert settings.local_farm_duration == 5
    assert settings.local_farm_resolution == "832x480"

    data = settings.to_dict()
    assert data["local_farm_model"] == "dmd4"

    loaded = AppSettings.from_dict(data)
    assert loaded.local_farm_duration == 5
