from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from utils.media import background_files


class BackgroundFolderTests(unittest.TestCase):
    def test_lists_supported_backgrounds_and_can_recurse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "b.mp4").write_bytes(b"video")
            (root / "a.png").write_bytes(b"image")
            (root / "ignore.txt").write_text("no", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "c.jpg").write_bytes(b"image")

            shallow = background_files(folder, recursive=False)
            recursive = background_files(folder, recursive=True)

            self.assertEqual([Path(path).name for path in shallow], ["a.png", "b.mp4"])
            self.assertEqual([Path(path).name for path in recursive], ["a.png", "b.mp4", "c.jpg"])


if __name__ == "__main__":
    unittest.main()
