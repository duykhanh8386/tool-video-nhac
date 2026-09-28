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
        original.effect_overlay = "C:/effects/light.mov"
        original.effect_overlay_blend = "screen"
        original.effect_overlay_opacity = 72
        original.effect_overlay_remove_white = True
        original.background_folder = "C:/backgrounds"
        original.batch_recursive = True
        original.ai_engine = "LOCAL"
        original.ai_source_mode = "MULTI"
        original.ai_source_folder = "C:/ai-images"
        original.ai_source_images = ["C:/one.png", "C:/two.jpg"]
        original.ai_source_recursive = True
        original.ai_generated_backgrounds = ["C:/outputs/one_ai.mp4", "C:/outputs/two_ai.mp4"]
        original.vids_show_browser = False
        original.local_wan_variant = "dmd4"
        original.local_resolution = "1280x704"
        original.local_frames = 81
        original.local_steps = 20
        original.local_cfg = 5.0
        original.local_seed = 42
        restored = VisualProject.from_dict(original.to_dict())
        self.assertEqual(restored, original)

    def test_legacy_turbo_project_migrates_to_commercial_dmd(self):
        restored = VisualProject.from_dict({"local_wan_variant": "turbo"})
        self.assertEqual(restored.local_wan_variant, "dmd4")

    def test_new_local_model_choices_round_trip(self):
        for variant in ("ltx2b", "hunyuan15"):
            with self.subTest(variant=variant):
                restored = VisualProject.from_dict({"local_wan_variant": variant})
                self.assertEqual(restored.local_wan_variant, variant)

    def test_invalid_overlay_values_are_normalized(self):
        restored = VisualProject.from_dict({
            "effect_overlay_blend": "unknown",
            "effect_overlay_opacity": 140,
            "elements": {"logo": {"x": .1, "y": .1, "width": .2, "height": .2, "blend_mode": "screen"}},
        })
        self.assertEqual(restored.effect_overlay_blend, "lighten")
        self.assertEqual(restored.effect_overlay_opacity, 100)
        self.assertEqual(restored.elements["logo"].blend_mode, "screen")

    def test_background_copy_preserves_full_composition_and_sets_ai_playback(self):
        original = VisualProject(
            title="Saved title",
            logo="C:/assets/logo.png",
            artwork="C:/assets/artwork.png",
            waveform_media="C:/assets/wave.mov",
            effect_overlay="C:/assets/effect.mov",
            animation="Fog drift",
            fps=60,
        )
        original.elements["logo"].x = .81
        copied = original.copy_for_background("C:/clips/generated.mp4", ai_video=True)
        self.assertEqual(copied.background, "C:/clips/generated.mp4")
        self.assertEqual(copied.title, original.title)
        self.assertEqual(copied.logo, original.logo)
        self.assertEqual(copied.artwork, original.artwork)
        self.assertEqual(copied.waveform_media, original.waveform_media)
        self.assertEqual(copied.effect_overlay, original.effect_overlay)
        self.assertEqual(copied.elements["logo"].x, .81)
        self.assertIsNot(copied.elements["logo"], original.elements["logo"])
        self.assertEqual(copied.animation, "STATIC")
        self.assertEqual(copied.fps, 24)
        self.assertEqual(original.animation, "Fog drift")
        self.assertEqual(original.fps, 60)

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
