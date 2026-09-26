from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai.veo import build_request_payload, motion_prompt


class VeoTests(unittest.TestCase):
    def test_prompt_locks_camera_and_keeps_user_motion(self):
        value = motion_prompt("Người phụ nữ chớp mắt và bàn tay chuyển động nhẹ")
        self.assertIn("Locked-off tripod camera", value)
        self.assertIn("bàn tay chuyển động nhẹ", value)

    def test_payload_uses_same_first_and_last_frame_for_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "frame.png"
            image.write_bytes(b"fake-png-for-payload-test")
            payload = build_request_payload(image, "Nến và khói chuyển động", duration=8, resolution="1080p")
        instance = payload["instances"][0]
        self.assertEqual(instance["image"], instance["lastFrame"])
        self.assertEqual(payload["parameters"]["durationSeconds"], 8)
        self.assertEqual(payload["parameters"]["personGeneration"], "allow_adult")

    def test_high_resolution_requires_eight_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "frame.png"
            image.write_bytes(b"fake")
            with self.assertRaises(ValueError):
                build_request_payload(image, "Khói chuyển động", duration=6, resolution="1080p")


if __name__ == "__main__":
    unittest.main()
