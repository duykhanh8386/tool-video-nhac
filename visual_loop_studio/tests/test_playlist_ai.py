from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playlist_ai.core.file_namer import resolve_output_file_path, sanitize_title_for_filename
from playlist_ai.core.job_queue import JobQueueManager, JobState, PlaylistBatchRunner
from playlist_ai.core.prompt_parser import MappedItem, parse_and_map_prompts
from playlist_ai.core.track_scanner import TrackInfo, natural_sort_key, scan_mp3_folder
from playlist_ai.providers.base import CookieExpiredError, IProvider, ProviderError
from playlist_ai.providers.muse_provider import MuseProvider
from playlist_ai.providers.wan_provider import WanProvider


class TestPlaylistAI(unittest.TestCase):
    def test_natural_sort_key(self):
        p1 = Path("1. Bài hát A.mp3")
        p2 = Path("2. Bài hát B.mp3")
        p10 = Path("10. Bài hát C.mp3")

        items = [p10, p1, p2]
        sorted_items = sorted(items, key=natural_sort_key)
        self.assertEqual(sorted_items, [p1, p2, p10])

    def test_sanitize_title_for_filename(self):
        # Giữ nguyên tiếng Việt có dấu, loại bỏ ký tự cấm: \ / : * ? " < > |
        raw = 'Lời Xin Lỗi Vụng Về: "Phần 1" / 2026? * <Hot> |'
        sanitized = sanitize_title_for_filename(raw)
        self.assertEqual(sanitized, "Lời Xin Lỗi Vụng Về Phần 1 2026 Hot")

        # Cắt ngắn nếu quá dài
        very_long = "A" * 200
        self.assertLessEqual(len(sanitize_title_for_filename(very_long, max_length=50)), 50)

        # Fallback nếu rỗng
        self.assertEqual(sanitize_title_for_filename("???"), "Untitled")

    def test_resolve_output_file_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = Path(tmp_dir)
            title = "Bài Hát Hay"

            # 1. Chưa có file nào
            p1, is_skip = resolve_output_file_path(out_dir, title, conflict_mode="suffix")
            self.assertFalse(is_skip)
            self.assertEqual(p1.name, "Bài Hát Hay.mp4")

            # Tạo file giả lập
            p1.touch()

            # 2. Xung đột với mode "suffix"
            p2, is_skip2 = resolve_output_file_path(out_dir, title, conflict_mode="suffix")
            self.assertFalse(is_skip2)
            self.assertEqual(p2.name, "Bài Hát Hay_2.mp4")
            p2.touch()

            p3, _ = resolve_output_file_path(out_dir, title, conflict_mode="suffix")
            self.assertEqual(p3.name, "Bài Hát Hay_3.mp4")

            # 3. Xung đột với mode "overwrite"
            p_ov, is_skip_ov = resolve_output_file_path(out_dir, title, conflict_mode="overwrite")
            self.assertFalse(is_skip_ov)
            self.assertEqual(p_ov.name, "Bài Hát Hay.mp4")

            # 4. Xung đột với mode "skip"
            p_sk, is_skip_sk = resolve_output_file_path(out_dir, title, conflict_mode="skip")
            self.assertTrue(is_skip_sk)
            self.assertEqual(p_sk.name, "Bài Hát Hay.mp4")

    def test_prompt_parser_equal(self):
        tracks = [
            TrackInfo(file_path=Path("track1.mp3"), title="Tình Đơn Phương"),
            TrackInfo(file_path=Path("track2.mp3"), title="Mưa Bão"),
        ]
        prompts = "lofi anime room, rainy window\nsunset coastline, warm breeze"

        mapped, p_count, t_count, warn = parse_and_map_prompts(
            prompt_text=prompts,
            tracks=tracks,
            template='{prompt}, mood theo bài hát "{title}"',
            use_template=True,
        )

        self.assertEqual(p_count, 2)
        self.assertEqual(t_count, 2)
        self.assertIsNone(warn)
        self.assertEqual(len(mapped), 2)
        self.assertEqual(mapped[0].final_prompt, 'lofi anime room, rainy window, mood theo bài hát "Tình Đơn Phương"')
        self.assertEqual(mapped[1].final_prompt, 'sunset coastline, warm breeze, mood theo bài hát "Mưa Bão"')

    def test_prompt_parser_mismatch_loop(self):
        # 1 prompt cho 3 bài -> Lặp vòng
        tracks = [
            TrackInfo(file_path=Path("1.mp3"), title="Bài 1"),
            TrackInfo(file_path=Path("2.mp3"), title="Bài 2"),
            TrackInfo(file_path=Path("3.mp3"), title="Bài 3"),
        ]
        prompts = "prompt duy nhat"

        mapped, p_count, t_count, warn = parse_and_map_prompts(
            prompt_text=prompts,
            tracks=tracks,
            use_template=False,
            mismatch_mode="loop",
        )
        self.assertEqual(p_count, 1)
        self.assertEqual(t_count, 3)
        self.assertEqual(len(mapped), 3)
        self.assertEqual(mapped[0].final_prompt, "prompt duy nhat")
        self.assertEqual(mapped[1].final_prompt, "prompt duy nhat")
        self.assertEqual(mapped[2].final_prompt, "prompt duy nhat")

    def test_prompt_parser_mismatch_last(self):
        # 2 prompt cho 3 bài -> Bài 3 dùng prompt cuối (prompt 2)
        tracks = [
            TrackInfo(file_path=Path("1.mp3"), title="Bài 1"),
            TrackInfo(file_path=Path("2.mp3"), title="Bài 2"),
            TrackInfo(file_path=Path("3.mp3"), title="Bài 3"),
        ]
        prompts = "prompt A\nprompt B"

        mapped, p_count, t_count, _ = parse_and_map_prompts(
            prompt_text=prompts,
            tracks=tracks,
            use_template=False,
            mismatch_mode="last",
        )
        self.assertEqual(mapped[0].final_prompt, "prompt A")
        self.assertEqual(mapped[1].final_prompt, "prompt B")
        self.assertEqual(mapped[2].final_prompt, "prompt B")

    def test_prompt_parser_excess_prompts(self):
        # 3 prompt cho 2 bài -> Bỏ prompt 3 và cảnh báo
        tracks = [
            TrackInfo(file_path=Path("1.mp3"), title="Bài 1"),
            TrackInfo(file_path=Path("2.mp3"), title="Bài 2"),
        ]
        prompts = "prompt 1\nprompt 2\nprompt 3"

        mapped, p_count, t_count, warn = parse_and_map_prompts(
            prompt_text=prompts,
            tracks=tracks,
            use_template=False,
        )
        self.assertEqual(p_count, 3)
        self.assertEqual(t_count, 2)
        self.assertEqual(len(mapped), 2)
        self.assertIn("1 prompt thừa", warn or "")

    def test_job_queue_persistence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "test_jobs.json"
            qm = JobQueueManager(state_file=state_file)

            tracks = [TrackInfo(file_path=Path("t1.mp3"), title="Song 1")]
            mapped, _, _, _ = parse_and_map_prompts("prompt 1", tracks, use_template=False)

            qm.set_jobs_from_mapped_items(mapped, provider_key="wan")
            self.assertEqual(len(qm.jobs), 1)
            self.assertEqual(qm.jobs[0].track_title, "Song 1")
            self.assertEqual(qm.jobs[0].provider_key, "wan")

            # Test đổi provider cho các bài pending
            qm.update_pending_provider("muse")
            self.assertEqual(qm.jobs[0].provider_key, "muse")

            # Test load lại từ file (resume)
            qm2 = JobQueueManager(state_file=state_file)
            self.assertEqual(len(qm2.jobs), 1)
            self.assertEqual(qm2.jobs[0].provider_key, "muse")

    def test_wan_provider_interface(self):
        wan = WanProvider(comfyui_url="http://127.0.0.1:8188")
        self.assertEqual(wan.key, "wan")
        self.assertIn("Wan 2.2 Local", wan.display_name)

    def test_muse_provider_interface(self):
        cookie = '[{"name": "session", "value": "12345", "domain": ".muse.ai"}]'
        muse = MuseProvider(cookie_raw=cookie)
        self.assertEqual(muse.key, "muse")
        self.assertEqual(muse.display_name, "Muse AI (Cookie)")

        muse.update_cookie("new_cookie_val")
        self.assertEqual(muse._cookie_raw, "new_cookie_val")

    def test_wan_dimension_snap(self):
        from playlist_ai.providers.wan_provider import snap_dim_to_32

        # 1080 -> 1088 (1088 % 32 == 0)
        self.assertEqual(snap_dim_to_32(1080), 1088)
        self.assertEqual(snap_dim_to_32(1088) % 32, 0)

        # 1920 -> 1920 (1920 % 32 == 0)
        self.assertEqual(snap_dim_to_32(1920), 1920)
        self.assertEqual(snap_dim_to_32(1920) % 32, 0)

        # 720 -> 704 (704 % 32 == 0)
        self.assertEqual(snap_dim_to_32(720), 704)
        self.assertEqual(snap_dim_to_32(720) % 32, 0)

        # Dưới 256 px -> tự động nâng lên tối thiểu 256 px
        self.assertEqual(snap_dim_to_32(100), 256)

    def test_extract_video_thumbnail_mock(self):
        from playlist_ai.core.job_queue import extract_video_thumbnail

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            vid = tmp / "test.mp4"
            vid.write_bytes(b"x" * 20000)
            thumb = tmp / "test.png"

            with patch("subprocess.run") as mock_run:
                def fake_run(cmd, **kwargs):
                    thumb.write_bytes(b"png_data")
                    m = MagicMock()
                    m.returncode = 0
                    return m

                mock_run.side_effect = fake_run
                ok = extract_video_thumbnail(vid, thumb, timestamp_sec=1.0)
                self.assertTrue(ok)
                self.assertTrue(thumb.is_file())

    def test_search_youtube_first_video_parses_json(self):
        from playlist_ai.core.youtube_thumb import search_youtube_first_video

        fake_html = (
            '<html><script>var ytInitialData = {"contents":{"twoColumnSearchResultsRenderer":'
            '{"primaryContents":{"sectionListRenderer":{"contents":[{"itemSectionRenderer":{"contents":['
            '{"videoRenderer":{"videoId":"testVid12345","title":{"runs":[{"text":"Bài Hát Test"}]},'
            '"thumbnail":{"thumbnails":[{"url":"https://i.ytimg.com/vi/testVid12345/hqdefault.jpg"}]}}}]}}]}}}}};'
            '</script></html>'
        )

        with patch("playlist_ai.core.youtube_thumb.robust_urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = fake_html.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            res = search_youtube_first_video("Bài Hát Test")
            self.assertIsNotNone(res)
            self.assertEqual(res["video_id"], "testVid12345")
            self.assertEqual(res["title"], "Bài Hát Test")
            self.assertEqual(len(res["thumbnails"]), 1)

    def test_search_youtube_first_video_regex_fallback(self):
        from playlist_ai.core.youtube_thumb import search_youtube_first_video

        fake_html = '<html><a href="/watch?v=regexVid999">Watch here</a></html>'
        with patch("playlist_ai.core.youtube_thumb.robust_urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.read.return_value = fake_html.encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            res = search_youtube_first_video("Fallback Search")
            self.assertIsNotNone(res)
            self.assertEqual(res["video_id"], "regexVid999")

    def test_download_youtube_thumbnail_mock(self):
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QColor, QImage
        from playlist_ai.core.youtube_thumb import download_youtube_thumbnail

        test_img = QImage(1280, 720, QImage.Format.Format_RGB32)
        test_img.fill(QColor(100, 150, 200))
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        test_img.save(buf, "PNG")
        raw_img_bytes = bytes(ba.data())

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "thumb.png"
            with patch("playlist_ai.core.youtube_thumb.robust_urlopen") as mock_url:
                mock_resp = MagicMock()
                mock_resp.read.return_value = raw_img_bytes
                mock_resp.__enter__.return_value = mock_resp
                mock_url.return_value = mock_resp

                res = download_youtube_thumbnail("testVid12345", out_file)
                self.assertIsNotNone(res)
                self.assertTrue(out_file.is_file())
                self.assertGreater(out_file.stat().st_size, 2048)

    def test_job_state_source_type(self):
        job = JobState(
            id="job_001",
            index=1,
            track_file="song.mp3",
            track_title="Song A",
            raw_prompt="prompt",
            final_prompt="final",
            provider_key="muse",
            source_type="youtube",
            thumbnail_url="https://youtube.com/thumb.jpg",
        )
        d = job.to_dict()
        self.assertEqual(d["source_type"], "youtube")
        self.assertEqual(d["thumbnail_url"], "https://youtube.com/thumb.jpg")

        job2 = JobState.from_dict(d)
        self.assertEqual(job2.source_type, "youtube")
        self.assertEqual(job2.thumbnail_url, "https://youtube.com/thumb.jpg")

    def test_muse_provider_uploads_image_before_submit(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_img = Path(tmp_dir) / "test.png"
            test_img.write_bytes(b"x" * 5000)

            muse = MuseProvider(cookie_raw='[{"name":"session","value":"val"}]')
            mock_driver = MagicMock()
            mock_driver.current_window_handle = "main_window"
            muse._driver = mock_driver

            with patch.object(muse, "_upload_image") as mock_upload, \
                 patch("playlist_ai.providers.muse_provider._query_muse_videos") as mock_q, \
                 patch("playlist_ai.providers.muse_provider.fill_muse_chat_prompt") as mock_fill, \
                 patch("playlist_ai.providers.muse_provider.submit_muse_chat_prompt") as mock_sub, \
                 patch("playlist_ai.providers.muse_provider._download_muse_video_file") as mock_dl:

                mock_upload.return_value = True
                mock_q.side_effect = [[], [{"id": "new_vid_1", "src": "http://muse/vid.mp4"}]]
                mock_fill.return_value = True
                mock_sub.return_value = True

                def fake_dl(driver, vid, dest, down_dir):
                    Path(dest).write_bytes(b"v" * 20000)
                    return True
                mock_dl.side_effect = fake_dl

                out_mp4 = Path(tmp_dir) / "output.mp4"
                res = muse.image_to_video(
                    image_path=test_img,
                    prompt="Master Prompt",
                    output_path=out_mp4,
                )
                self.assertEqual(Path(res).resolve(), out_mp4.resolve())
                mock_upload.assert_called_once_with(mock_driver, test_img)
                mock_fill.assert_called_once_with(mock_driver, "Master Prompt")

    def test_batch_runner_fetches_youtube_thumbnail(self):
        from playlist_ai.core.youtube_thumb import DEFAULT_MASTER_VISUALIZER_PROMPT

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            out_dir = tmp / "output"
            out_dir.mkdir(parents=True, exist_ok=True)

            state_file = tmp / "state.json"
            qm = JobQueueManager(state_file=state_file)

            track = TrackInfo(file_path=tmp / "song.mp3", title="Bài Hát Test")
            mapped = [
                MappedItem(
                    index=1,
                    track=track,
                    raw_prompt="[YouTube Thumbnail Auto]",
                    final_prompt=DEFAULT_MASTER_VISUALIZER_PROMPT,
                )
            ]
            qm.set_jobs_from_mapped_items(mapped, provider_key="wan", source_type="youtube")

            mock_provider = MagicMock()
            mock_provider.display_name = "Wan Test"
            mock_provider.key = "wan"

            def fake_i2v(image_path, prompt, **kwargs):
                target = kwargs.get("output_path") or (out_dir / "out.mp4")
                Path(target).write_bytes(b"v" * 15000)
                return target

            mock_provider.image_to_video.side_effect = fake_i2v

            fake_thumb = out_dir / "Bài Hát Test.png"

            def fake_fetch_thumb(title, output_path, **kwargs):
                Path(output_path).write_bytes(b"p" * 5000)
                return Path(output_path)

            with patch("playlist_ai.core.job_queue.fetch_and_save_youtube_thumbnail", side_effect=fake_fetch_thumb):
                runner = PlaylistBatchRunner(
                    queue_manager=qm,
                    providers={"wan": mock_provider},
                    output_dir=out_dir,
                )
                runner.run()

                # Kiểm tra kết quả: job hoàn thành, thumbnail được tạo và lưu với tên bài hát
                job = qm.jobs[0]
                self.assertEqual(job.status, "Hoàn thành")
                self.assertTrue(fake_thumb.is_file())
                self.assertTrue(Path(job.output_video_path).is_file())
                mock_provider.image_to_video.assert_called_once()
                call_args = mock_provider.image_to_video.call_args
                self.assertEqual(Path(call_args.kwargs["image_path"]).resolve(), fake_thumb.resolve())
                self.assertEqual(call_args.kwargs["prompt"], DEFAULT_MASTER_VISUALIZER_PROMPT)


if __name__ == "__main__":
    unittest.main()
