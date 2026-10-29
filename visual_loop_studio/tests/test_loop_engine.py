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

    def test_custom_duration_loops_audio_when_longer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "short.mp4"
            audio = root / "short.wav"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            project = LoopProject(
                video=str(video),
                main_audio=str(audio),
                output_folder=str(root),
                encoder="libx264",
                duration_mode="custom",
                custom_duration=600.0,
            )
            info = MediaInfo(str(video), 10.0, True, False, width=1920, height=1080, fps=30)
            with patch("render.ffmpeg.probe_media", return_value=info), patch(
                "render.ffmpeg.main_audio_duration", return_value=120.0
            ), patch("render.ffmpeg.resolve_encoder", return_value="libx264"):
                job = build_loop_job(project, AppSettings())
            self.assertEqual(job.duration, 600.0)
            self.assertEqual(job.command[job.command.index("-t") + 1], "600.000000")
            # Both video and audio should have -stream_loop -1 because 600 > 120
            self.assertEqual(job.command.count("-stream_loop"), 2)


    def test_trim_start_and_end_affects_duration_and_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "video.mp4"
            audio = root / "audio.wav"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            project = LoopProject(
                video=str(video),
                main_audio=str(audio),
                output_folder=str(root),
                encoder="libx264",
                duration_mode="audio",
                main_trim_start=5.0,
                main_trim_end=5.0,
                main_volume_db=0.0,
            )
            info = MediaInfo(str(video), 60.0, True, False, width=1920, height=1080, fps=30)
            with patch("render.ffmpeg.probe_media", return_value=info), patch(
                "render.ffmpeg.main_audio_duration", return_value=100.0
            ), patch("render.ffmpeg.resolve_encoder", return_value="libx264"):
                job = build_loop_job(project, AppSettings())
            # 100 - 5.0 - 5.0 = 90.0
            self.assertEqual(job.duration, 90.0)
            self.assertEqual(job.command[job.command.index("-t") + 1], "90.000000")
            filter_str = job.command[job.command.index("-filter_complex") + 1]
            self.assertIn("atrim=start=5.000:end=95.000", filter_str)
            self.assertIn("volume=0.0dB", filter_str)

    def test_main_and_background_volume_db(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "video.mp4"
            audio = root / "audio.wav"
            bg = root / "bg.mp3"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            bg.write_bytes(b"bg")
            project = LoopProject(
                video=str(video),
                main_audio=str(audio),
                background_audio=str(bg),
                output_folder=str(root),
                encoder="libx264",
                duration_mode="audio",
                main_volume_db=-2.5,
                background_volume_db=-18.0,
            )
            info = MediaInfo(str(video), 60.0, True, False, width=1920, height=1080, fps=30)
            bg_info = MediaInfo(str(bg), 60.0, False, True)
            def mock_probe(p, *args):
                return bg_info if Path(p).resolve() == bg.resolve() else info
            with patch("render.ffmpeg.probe_media", side_effect=mock_probe), patch(
                "render.ffmpeg.main_audio_duration", return_value=120.0
            ), patch("render.ffmpeg.resolve_encoder", return_value="libx264"):
                job = build_loop_job(project, AppSettings())
            filter_str = job.command[job.command.index("-filter_complex") + 1]
            self.assertIn("volume=-2.5dB", filter_str)
            self.assertIn("volume=-18.0dB", filter_str)
            self.assertIn("amix=inputs=2", filter_str)


if __name__ == "__main__":
    unittest.main()
