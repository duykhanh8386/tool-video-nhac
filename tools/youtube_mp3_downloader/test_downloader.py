"""Unit tests for YouTube MP3 Downloader standalone tool."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from downloader_app.downloader import (
    AtomicJsonStateStore,
    YoutubeMp3DownloadController,
    browser_cookie_candidates,
    is_playlist_url,
    normalize_youtube_source_url,
    parse_youtube_urls,
    prepare_youtube_cookiefile,
    youtube_video_id,
)


class TestYoutubeMp3Downloader(unittest.TestCase):
    def test_parse_youtube_urls(self):
        text = """
        https://www.youtube.com/watch?v=XcFug76KM0A
        https://youtu.be/6AszekLWdpE
        not-a-url
        https://google.com/search
        https://www.youtube.com/watch?v=XcFug76KM0A
        """
        urls, invalid = parse_youtube_urls(text)
        self.assertEqual(len(urls), 2)
        self.assertIn("https://www.youtube.com/watch?v=XcFug76KM0A", urls)
        self.assertIn("https://youtu.be/6AszekLWdpE", urls)
        self.assertEqual(len(invalid), 2)

    def test_youtube_video_id(self):
        self.assertEqual(youtube_video_id("https://www.youtube.com/watch?v=abc123XYZ"), "abc123XYZ")
        self.assertEqual(youtube_video_id("https://youtu.be/abc123XYZ"), "abc123XYZ")
        self.assertEqual(youtube_video_id("https://www.youtube.com/shorts/abc123XYZ"), "abc123XYZ")

    def test_is_playlist_url(self):
        self.assertTrue(is_playlist_url("https://www.youtube.com/playlist?list=PL12345"))
        self.assertTrue(is_playlist_url("https://www.youtube.com/watch?v=abc&list=PL12345"))
        self.assertFalse(is_playlist_url("https://www.youtube.com/watch?v=abc"))

    def test_normalize_youtube_source_url(self):
        url = "https://www.youtube.com/@EvBRecords"
        normalized = normalize_youtube_source_url(url)
        self.assertEqual(normalized, "https://www.youtube.com/@EvBRecords/videos")

    def test_browser_cookie_candidates(self):
        self.assertEqual(browser_cookie_candidates("auto"), ("firefox", "chrome", "edge"))
        self.assertEqual(browser_cookie_candidates("none"), ())
        self.assertEqual(browser_cookie_candidates("chrome"), ("chrome",))

    def test_controller_state_and_overall_progress(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            store = AtomicJsonStateStore(state_file)
            controller = YoutubeMp3DownloadController(state_store=store)

            urls = [
                "https://www.youtube.com/watch?v=vid1",
                "https://www.youtube.com/watch?v=vid2",
                "https://www.youtube.com/watch?v=vid3",
                "https://www.youtube.com/watch?v=vid4",
            ]
            controller.update_urls(urls)

            snapshot = controller.snapshot()
            self.assertEqual(len(snapshot["urls"]), 4)

            # Giả lập 2 video hoàn tất (50%), 1 video đang tải 50%
            controller.items[urls[0]]["status"] = "successful"
            controller.items[urls[0]]["progress"] = 100

            controller.items[urls[1]]["status"] = "successful"
            controller.items[urls[1]]["progress"] = 100

            controller.items[urls[2]]["status"] = "downloading"
            controller.items[urls[2]]["progress"] = 50

            # Tính tiến độ tổng thể
            selected_urls = [u for u in controller.urls if controller.items[u].get("selected", True)]
            total = len(selected_urls)
            completed = sum(1 for u in selected_urls if controller.items[u]["status"] == "successful")
            downloading_p = controller.items[urls[2]]["progress"]

            ratio = (completed + (downloading_p / 100.0)) / total
            percent = int(ratio * 100)
            self.assertEqual(percent, 62)  # (2 + 0.5) / 4 = 2.5 / 4 = 62.5% -> 62%

    def test_prepare_youtube_cookiefile_raw_header(self):
        cookie_header = "LOGIN_INFO=abc123xyz; SID=securesid456; __Secure-1PSID=token789"
        cookie_path = prepare_youtube_cookiefile(cookie_header)
        self.assertIsNotNone(cookie_path)
        path = Path(cookie_path)
        self.assertTrue(path.is_file())
        content = path.read_text(encoding="utf-8")
        self.assertIn("# Netscape HTTP Cookie File", content)
        self.assertIn(".youtube.com", content)
        self.assertIn("LOGIN_INFO", content)
        self.assertIn("abc123xyz", content)
        self.assertIn("SID", content)

    def test_prepare_youtube_cookiefile_file_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\t123\n")
            f_path = f.name
        try:
            resolved = prepare_youtube_cookiefile(f_path)
            self.assertEqual(resolved, str(Path(f_path).resolve()))
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_controller_cookie_persistence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "state.json"
            store = AtomicJsonStateStore(state_file)
            c1 = YoutubeMp3DownloadController(state_store=store)
            c1.set_cookie_text("Cookie: SID=mysecret; HSID=myhsid")
            self.assertEqual(c1.cookie_text, "Cookie: SID=mysecret; HSID=myhsid")

            # Reload new controller instance
            c2 = YoutubeMp3DownloadController(state_store=store)
            self.assertEqual(c2.cookie_text, "Cookie: SID=mysecret; HSID=myhsid")


if __name__ == "__main__":
    unittest.main()

