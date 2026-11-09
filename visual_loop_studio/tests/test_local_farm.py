from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
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

    # Test trim frame đen đầu
    conformed_trimmed = tmp_path / "conformed_trimmed.mp4"
    out_trimmed = conform_video_duration(raw_video, conformed_trimmed, target_seconds=5.0, trim_start_seconds=0.16)
    assert out_trimmed.exists()
    info_trimmed = probe_media(str(conformed_trimmed))
    assert 4.8 <= info_trimmed.duration <= 5.2


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


class LocalFarmBatchStabilityTests(unittest.TestCase):
    def test_batch_runner_recovers_and_retries_once_on_timeout_then_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "out"
            dummy_video = tmp_path / "dummy.mp4"
            dummy_video.write_bytes(b"fake_mp4")

            runtime_mgr = MagicMock()
            runtime_mgr.restart.return_value = True

            runner = LocalVideoBatchRunnerThread(
                prompts=["Prompt one", "Prompt two"],
                output_dir=out_dir,
                comfyui_url="http://127.0.0.1:8188",
                model_variant="dmd4",
                resolution="832x480",
                duration_seconds=5,
                runtime_manager=runtime_mgr,
                runtime_root=str(tmp_path / "rt"),
            )

            seen_calls = []

            def fake_generate(**kwargs):
                seen_calls.append(kwargs)
                kwargs["progress"](12, "Wan 2.2 DMD 4 bước đang chạy trên GPU… 0.0 phút")
                kwargs["progress"](13, "Wan 2.2 DMD 4 bước đang chạy trên GPU… 0.1 phút")
                kwargs["progress"](14, "Wan 2.2 DMD 4 bước đang chạy trên GPU… 0.2 phút")
                if len(seen_calls) == 1:
                    raise TimeoutError("ComfyUI bị treo quá 10 phút")
                return str(dummy_video)

            stats_list = []
            logs = []
            runner.stats_signal.connect(lambda t, c, f, r: stats_list.append((t, c, f, r)))
            runner.log_signal.connect(logs.append)

            with (
                patch("auth.local_generation.server_ready", side_effect=[True, False, True, True]),
                patch("auth.local_generation.generate_local_image_to_video", side_effect=fake_generate),
                patch("auth.local_generation.conform_video_duration", side_effect=lambda src, dst, **kw: dst.write_bytes(b"final") or dst),
                patch("auth.local_generation.free_comfyui_memory", return_value=True) as mock_free,
                patch("auth.local_generation.clear_comfyui_history", return_value=True) as mock_hist,
                patch("auth.local_generation.get_comfyui_queue", return_value={"queue_running": [], "queue_pending": []}),
                patch("auth.local_generation.time.sleep"),
            ):
                runner.run()

            # 1 failure recovered + 1 retry + 1 second prompt = 3 calls
            self.assertEqual(len(seen_calls), 3)
            # Canvas image filename reused across items
            self.assertEqual(
                Path(seen_calls[0]["image_path"]).name,
                "canvas_neutral_832x480.png",
            )
            self.assertEqual(
                Path(seen_calls[2]["image_path"]).name,
                "canvas_neutral_832x480.png",
            )
            # First and retry verified; subsequent prompt skips redundant /object_info check
            self.assertTrue(seen_calls[0]["verify_server"])
            self.assertTrue(seen_calls[1]["verify_server"])
            self.assertFalse(seen_calls[2]["verify_server"])
            # Completed all 2 with 0 failed
            self.assertEqual(stats_list[-1], (2, 2, 0, 0))
            runtime_mgr.restart.assert_called_once()
            self.assertTrue(mock_free.called)
            self.assertTrue(mock_hist.called)
            # All worker log lines carry real timestamp and throttled GPU spam
            self.assertTrue(all(re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s", line) for line in logs))
            gpu_lines = [line for line in logs if "đang chạy trên GPU" in line]
            # First tick per attempt logs once, rapid 0.1/0.2 min duplicate ticks are throttled
            self.assertLessEqual(len(gpu_lines), 3)

    def test_log_view_caps_maximum_blocks_and_avoids_double_timestamp(self):
        from PySide6.QtWidgets import QApplication
        from ui.local_studio import LocalStudioPage

        app = QApplication.instance() or QApplication([])
        self.assertIsNotNone(app)
        with patch.object(LocalStudioPage, "_check_comfyui_status"):
            page = LocalStudioPage(AppSettings())
        try:
            self.assertEqual(page.log_view.maximumBlockCount(), 1500)
            page._append_log("[08:12:34] 📝 Bắt đầu")
            page._append_log("✔ Không có timestamp sẵn")
            lines = page.log_view.toPlainText().splitlines()
            self.assertEqual(lines[0], "[08:12:34] 📝 Bắt đầu")
            self.assertRegex(lines[1], r"^\[\d{2}:\d{2}:\d{2}\] ✔ Không có timestamp sẵn$")
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
