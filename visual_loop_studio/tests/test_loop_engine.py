from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models.loop_project import LoopProject
from models.settings_model import AppSettings
from render.ffmpeg import build_loop_job
from render.ffprobe import MediaInfo


class LoopEngineTests(unittest.TestCase):
    def test_streams_video_and_uses_main_audio_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "short.mp4"
            audio = root / "long.wav"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            project = LoopProject(video=str(video), main_audio=str(audio), output_folder=str(root), encoder="libx264")
            info = MediaInfo(str(video), 60.0, True, False, width=1920, height=1080, fps=30)
            with patch("render.ffmpeg.probe_media", return_value=info), patch("render.ffmpeg.main_audio_duration", return_value=7200.125), patch("render.ffmpeg.resolve_encoder", return_value="libx264"):
                job = build_loop_job(project, AppSettings())
            self.assertIn("-stream_loop", job.command)
            self.assertEqual(job.duration, 7200.125)
            self.assertEqual(job.command[job.command.index("-t") + 1], "7200.125000")


if __name__ == "__main__":
    unittest.main()
