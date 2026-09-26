from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import utils.updater as updater
from utils.updater import build_self_update_script
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

    def test_self_update_script_retries_verifies_and_relaunches(self):
        script = build_self_update_script()
        self.assertIn("for ($attempt = 1; $attempt -le 30", script)
        self.assertIn("Get-FileHash -Algorithm SHA256", script)
        self.assertIn("Start-Process -FilePath $Old", script)
        self.assertIn("--updated-to=$TargetVersion", script)
        self.assertIn("--update-fallback", script)

    def test_schedule_self_update_uses_hidden_powershell_and_unicode_script(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "Thư mục tiếng Việt" / "VisualLoopStudio.exe"
            current.parent.mkdir()
            current.write_bytes(b"old")
            downloaded = root / "new.exe"
            downloaded.write_bytes(b"new")
            with (
                patch.object(updater.sys, "frozen", True, create=True),
                patch.object(updater.sys, "executable", str(current)),
                patch.object(updater.tempfile, "gettempdir", return_value=folder),
                patch.object(updater.subprocess, "Popen") as popen,
            ):
                script_path = updater.schedule_self_update(downloaded, "1.2.3.4")

            command = popen.call_args.args[0]
            self.assertIn("-WindowStyle", command)
            self.assertIn("Hidden", command)
            self.assertIn("-TargetVersion", command)
            self.assertIn("1.2.3.4", command)
            self.assertIn(str(current.resolve()), command)
            self.assertEqual(script_path.read_bytes()[:3], b"\xef\xbb\xbf")


if __name__ == "__main__":
    unittest.main()
