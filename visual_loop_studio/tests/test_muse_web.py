from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from ai.muse_web import (
    CHECKPOINT_PROTOCOL,
    MuseJobCheckpoint,
    _is_transient_page,
    _wait_for_video_after_message,
    clear_checkpoint,
    is_valid_mp4,
    load_checkpoint,
    migrate_outputs,
    save_checkpoint,
)


class MuseWebTests(unittest.TestCase):
    def test_is_valid_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            
            # Non-existent
            self.assertFalse(is_valid_mp4(root / "missing.mp4"))

            # Too small
            small = root / "small.mp4"
            small.write_bytes(b"\x00\x00\x00\x1cftypisom" + b"\x00" * 100)
            self.assertFalse(is_valid_mp4(small))

            # Corrupted header (no ftyp)
            bad_header = root / "bad.mp4"
            bad_header.write_bytes(b"\x00" * 60_000)
            self.assertFalse(is_valid_mp4(bad_header))

            # Valid MP4
            valid = root / "valid.mp4"
            valid.write_bytes(b"\x00\x00\x00\x1cftypisom" + b"\x00" * 60_000)
            self.assertTrue(is_valid_mp4(valid))

    def test_migrate_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dst_dir:
            src = Path(src_dir)
            dst = Path(dst_dir)

            valid_mp4 = src / "test1.mp4"
            valid_mp4.write_bytes(b"\x00\x00\x00\x1cftypisom" + b"\x00" * 60_000)

            invalid_mp4 = src / "test2.mp4"
            invalid_mp4.write_bytes(b"too small")

            copied = migrate_outputs(str(src), str(dst))
            self.assertEqual(copied, 1)
            self.assertTrue((dst / "test1.mp4").is_file())
            self.assertFalse((dst / "test2.mp4").is_file())

    def test_checkpoint_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ckpt = MuseJobCheckpoint(
                protocol=CHECKPOINT_PROTOCOL,
                image_path=str(Path(folder) / "sample.png"),
                prompt="motion test",
                output_path=str(Path(folder) / "sample.mp4"),
                account_index=1,
                sent_ts="2026-10-06T07:00:00Z",
                message_ids_before=["msg-1", "msg-2"],
                locked_message_id="msg-3",
                download_done=False,
                mp4_valid=False,
            )
            save_checkpoint(ckpt)
            loaded = load_checkpoint(1, ckpt.image_path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.prompt, "motion test")
            self.assertEqual(loaded.message_ids_before, ["msg-1", "msg-2"])
            self.assertEqual(loaded.account_index, 1)

            clear_checkpoint(1, ckpt.image_path)
            self.assertIsNone(load_checkpoint(1, ckpt.image_path))

    def test_transient_page_detection(self) -> None:
        self.assertTrue(_is_transient_page("https://accounts.google.com/signin/v2"))
        self.assertTrue(_is_transient_page("https://consent.google.com/m"))
        self.assertFalse(_is_transient_page("https://muse.ai/chat"))

    def test_wait_for_video_ignores_old_videos(self) -> None:
        page = MagicMock()
        page.url = "https://muse.ai/chat"

        # Mock evaluate calls
        call_count = [0]
        def mock_evaluate(script, *args):
            call_count[0] += 1
            # Step 1: collecting message IDs
            if "querySelectorAll('[data-message-id]')" in script:
                return ["msg-old-1", "msg-new-bot"]
            # Step 2: checking specific message
            if "querySelector(`[data-message-id=\"${msgId}\"]`)" in script:
                mid = args[0] if args else ""
                if mid == "msg-new-bot":
                    return "https://cdn.muse.ai/videos/output-123.mp4"
                return ""
            # Step 3: all video srcs
            if "querySelectorAll('video')" in script:
                return ["https://cdn.muse.ai/videos/old-output.mp4"]
            return ""

        page.evaluate = mock_evaluate

        result = _wait_for_video_after_message(
            page=page,
            msg_ids_before=["msg-old-1"],
            video_srcs_before={"https://cdn.muse.ai/videos/old-output.mp4"},
            progress=lambda _p, _m: None,
            cancelled=lambda: False,
            timeout_minutes=1,
        )

        self.assertEqual(result, "https://cdn.muse.ai/videos/output-123.mp4")


if __name__ == "__main__":
    unittest.main()
