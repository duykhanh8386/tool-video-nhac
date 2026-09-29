from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai import google_vids_web
from ai.google_vids_web import GOOGLE_VIDS_URL, google_vids_profile_ready


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


class GoogleVidsBrowserTests(unittest.TestCase):
    @patch("ai.google_vids_web.subprocess.Popen")
    @patch("ai.google_vids_web._browser_candidates")
    def test_login_browser_is_native_and_does_not_disable_sandbox(self, candidates, popen) -> None:
        candidates.return_value = [("Google Chrome", Path("C:/Chrome/chrome.exe"))]
        process = MagicMock()
        popen.return_value = process

        self.assertIs(google_vids_web._launch_login_browser(Path("C:/Vids Profile")), process)

        command = popen.call_args.args[0]
        self.assertIn(GOOGLE_VIDS_URL, command)
        self.assertIn("--user-data-dir=C:\\Vids Profile", command)
        self.assertNotIn("--no-sandbox", command)
        self.assertNotIn("--remote-debugging-pipe", command)

    def test_playwright_generation_context_enables_chromium_sandbox(self) -> None:
        context = object()
        launch = MagicMock(return_value=context)
        playwright = SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=launch),
        )

        result = google_vids_web._launch_context(playwright, Path("C:/Vids Profile"), headless=False)

        self.assertIs(result, context)
        self.assertTrue(launch.call_args.kwargs["chromium_sandbox"])

    @patch("ai.google_vids_web.time.sleep")
    @patch("ai.google_vids_web.google_vids_profile_ready", return_value=True)
    @patch("ai.google_vids_web._launch_login_browser")
    def test_login_flow_waits_for_native_browser_to_close(self, launch, _ready, _sleep) -> None:
        process = MagicMock()
        process.poll.side_effect = [None, 0]
        launch.return_value = process

        with tempfile.TemporaryDirectory() as folder:
            result = google_vids_web.open_google_vids_login(folder)

        self.assertEqual(result, folder)
        launch.assert_called_once_with(Path(folder))


if __name__ == "__main__":
    unittest.main()
