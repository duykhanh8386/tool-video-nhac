from __future__ import annotations

import struct
import unittest
from pathlib import Path

from PySide6.QtGui import QImage


ASSET_DIR = Path(__file__).resolve().parents[1] / "assets"


class AppBrandingTests(unittest.TestCase):
    def test_logo_is_a_large_transparent_png(self) -> None:
        image = QImage(str(ASSET_DIR / "app_logo.png"))
        self.assertFalse(image.isNull())
        self.assertGreaterEqual(image.width(), 512)
        self.assertEqual(image.width(), image.height())
        self.assertTrue(image.hasAlphaChannel())

    def test_windows_icon_contains_multiple_sizes(self) -> None:
        payload = (ASSET_DIR / "app_icon.ico").read_bytes()
        reserved, image_type, count = struct.unpack("<HHH", payload[:6])
        self.assertEqual(reserved, 0)
        self.assertEqual(image_type, 1)
        self.assertGreaterEqual(count, 8)


if __name__ == "__main__":
    unittest.main()
