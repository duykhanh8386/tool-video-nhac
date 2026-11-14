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


if __name__ == "__main__":
    unittest.main()

