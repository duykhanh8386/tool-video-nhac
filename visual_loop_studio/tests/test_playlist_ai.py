from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playlist_ai.core.file_namer import resolve_output_file_path, sanitize_title_for_filename
from playlist_ai.core.job_queue import JobQueueManager, JobState, PlaylistBatchRunner
from playlist_ai.core.prompt_parser import parse_and_map_prompts
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


if __name__ == "__main__":
    unittest.main()
