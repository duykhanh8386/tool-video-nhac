from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai.google_vids_web import google_vids_profile_ready


class GoogleVidsProfileTests(unittest.TestCase):
    def test_empty_profile_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(google_vids_profile_ready(folder))

    def test_chrome_network_cookie_profile_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Local State").write_text("{}", encoding="utf-8")
            cookies = root / "Default" / "Network" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"sqlite")
            self.assertTrue(google_vids_profile_ready(root))

    def test_edge_legacy_cookie_location_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Local State").write_text("{}", encoding="utf-8")
            cookies = root / "Default" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"sqlite")
            self.assertTrue(google_vids_profile_ready(root))


if __name__ == "__main__":
    unittest.main()
