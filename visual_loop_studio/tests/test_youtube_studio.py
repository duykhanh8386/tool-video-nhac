from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from auth import youtube_studio


class YouTubeStudioBrowserTests(unittest.TestCase):
    @patch("auth.youtube_studio.subprocess.Popen")
    @patch("auth.youtube_studio._browser_candidates")
    def test_uses_separate_native_profile_without_login_automation(self, candidates, popen) -> None:
        candidates.return_value = [("Google Chrome", Path("C:/Chrome/chrome.exe"))]
        process = MagicMock()
        popen.return_value = process
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder) / "channel-profile"
            returned_process, returned_profile = youtube_studio.open_youtube_studio("account-one", profile)

        self.assertIs(returned_process, process)
        self.assertEqual(returned_profile, profile)
        command = popen.call_args.args[0]
        self.assertIn(youtube_studio.YOUTUBE_STUDIO_URL, command)
        self.assertTrue(any(item.startswith("--user-data-dir=") for item in command))
        self.assertNotIn("--headless", command)
        self.assertFalse(any("password" in item.casefold() for item in command))
        self.assertFalse(any("email" in item.casefold() for item in command))


if __name__ == "__main__":
    unittest.main()
