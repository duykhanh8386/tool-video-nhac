from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui.batch_render import natural_sort_key, scan_folder, VIDEO_EXTS, AUDIO_EXTS


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


if __name__ == "__main__":
    unittest.main()
