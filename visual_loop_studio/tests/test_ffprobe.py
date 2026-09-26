from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from render.ffprobe import format_duration, probe_media


class FFprobeTests(unittest.TestCase):
    def test_parses_streams_and_fractional_fps(self):
        payload = {
            "format": {"duration": "12.345", "bit_rate": "800000"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001"},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / "sample.bin"
            media.write_bytes(b"x")
            result = Mock(returncode=0, stdout=json.dumps(payload), stderr="")
            with patch("render.ffprobe.subprocess.run", return_value=result):
                info = probe_media(str(media))
        self.assertTrue(info.has_video)
        self.assertTrue(info.has_audio)
        self.assertAlmostEqual(info.fps, 29.970, places=3)
        self.assertEqual(info.duration, 12.345)

    def test_duration_format(self):
        self.assertEqual(format_duration(3661.125), "01:01:01.125")


if __name__ == "__main__":
    unittest.main()
