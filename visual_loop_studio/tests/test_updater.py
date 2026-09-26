from __future__ import annotations

import unittest

from utils.updater import is_newer_version, parse_release, version_tuple


class UpdaterTests(unittest.TestCase):
    def test_numeric_version_comparison(self):
        self.assertTrue(is_newer_version("1.0.24.1", "1.0.23.9"))
        self.assertFalse(is_newer_version("v1.0.24.1", "1.0.24.1"))
        self.assertEqual(version_tuple("v2.7.3-beta"), (2, 7, 3))

    def test_parse_release_finds_exe_and_checksum(self):
        payload = {
            "tag_name": "v1.0.9.1",
            "name": "Visual Loop Studio 1.0.9.1",
            "body": "New release",
            "html_url": "https://example.test/release",
            "assets": [
                {"name": "VisualLoopStudio-Windows-x64.exe", "browser_download_url": "https://example.test/app.exe", "size": 123},
                {"name": "VisualLoopStudio-Windows-x64.exe.sha256", "browser_download_url": "https://example.test/app.sha256", "size": 80},
            ],
        }
        release = parse_release(payload)
        self.assertEqual(release.version, "1.0.9.1")
        self.assertEqual(release.executable.size, 123)
        self.assertIsNotNone(release.checksum)

    def test_release_without_windows_exe_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_release({"tag_name": "v2.0.0", "assets": []})


if __name__ == "__main__":
    unittest.main()
