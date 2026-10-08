from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui.batch_render import (
    natural_sort_key, scan_folder, VIDEO_EXTS, AUDIO_EXTS,
    parse_duration_string, format_seconds_to_hhmmss,
)


class BatchRenderTests(unittest.TestCase):
    def test_natural_sort_key(self):
        files = ["video10.mp4", "video2.mp4", "video1.mp4", "video20.mp4"]
        files.sort(key=natural_sort_key)
        self.assertEqual(files, ["video1.mp4", "video2.mp4", "video10.mp4", "video20.mp4"])

    def test_scan_folder_finds_and_sorts_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "track10.mp3").write_bytes(b"audio")
            (root / "track1.mp3").write_bytes(b"audio")
            (root / "track2.wav").write_bytes(b"audio")
            (root / "notes.txt").write_bytes(b"text")

            scanned = scan_folder(str(root), AUDIO_EXTS)
            names = [f.name for f in scanned]
            self.assertEqual(names, ["track1.mp3", "track2.wav", "track10.mp3"])

    def test_scan_folder_empty_or_invalid(self):
        self.assertEqual(scan_folder("non_existent_folder_12345", VIDEO_EXTS), [])

    def test_parse_duration_string(self):
        self.assertEqual(parse_duration_string("Theo nhạc"), ("audio", 0.0))
        self.assertEqual(parse_duration_string("audio"), ("audio", 0.0))
        self.assertEqual(parse_duration_string(""), ("audio", 0.0))

        # HH:MM:SS format
        self.assertEqual(parse_duration_string("00:30:00"), ("custom", 1800.0))
        self.assertEqual(parse_duration_string("01:00:00"), ("custom", 3600.0))
        self.assertEqual(parse_duration_string("00:01:30"), ("custom", 90.0))

        # MM:SS format
        self.assertEqual(parse_duration_string("30:00"), ("custom", 1800.0))
        self.assertEqual(parse_duration_string("05:00"), ("custom", 300.0))

        # Raw seconds
        self.assertEqual(parse_duration_string("60s"), ("custom", 60.0))
        self.assertEqual(parse_duration_string("120 giây"), ("custom", 120.0))
        self.assertEqual(parse_duration_string("1800"), ("custom", 1800.0))

    def test_format_seconds_to_hhmmss(self):
        self.assertEqual(format_seconds_to_hhmmss(1800.0), "00:30:00")
        self.assertEqual(format_seconds_to_hhmmss(3600.0), "01:00:00")
        self.assertEqual(format_seconds_to_hhmmss(30.0), "00:00:30")
        self.assertEqual(format_seconds_to_hhmmss(3661.0), "01:01:01")


if __name__ == "__main__":
    unittest.main()
