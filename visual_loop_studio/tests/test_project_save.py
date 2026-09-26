from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from models.loop_project import LoopProject
from models.visual_project import EffectItem, TextStyle, VisualProject
from utils.paths import unique_output


class ProjectModelTests(unittest.TestCase):
    def test_visual_round_trip(self):
        original = VisualProject(title="Healing", effects=[EffectItem("SNOW", .4)])
        original.elements["logo"].locked = True
        original.elements["logo"].x = .82
        original.elements["logo"].blend_mode = "lighten"
        original.text_styles["title"] = TextStyle("Arial", 112, True, True)
        original.background_folder = "C:/backgrounds"
        original.batch_recursive = True
        original.ai_engine = "LOCAL"
        original.ai_source_mode = "MULTI"
        original.ai_source_folder = "C:/ai-images"
        original.ai_source_images = ["C:/one.png", "C:/two.jpg"]
        original.ai_source_recursive = True
        original.local_resolution = "1280x704"
        original.local_frames = 81
        original.local_steps = 20
        original.local_cfg = 5.0
        original.local_seed = 42
        restored = VisualProject.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_loop_round_trip_ignores_future_fields(self):
        payload = LoopProject(main_volume=.8).to_dict()
        payload["future_option"] = True
        restored = LoopProject.from_dict(payload)
        self.assertEqual(restored.main_volume, .8)

    def test_output_is_mp4_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            first = unique_output(folder, "demo.mkv", "visual", ".mp4")
            self.assertEqual(first.name, "demo.mp4")
            first.write_bytes(b"existing")
            second = unique_output(folder, "demo.mkv", "visual", ".mp4")
            self.assertEqual(second.name, "demo_2.mp4")


if __name__ == "__main__":
    unittest.main()
