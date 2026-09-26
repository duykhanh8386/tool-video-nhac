from __future__ import annotations

import unittest

from models.visual_project import EffectItem
from visual.effects import ffmpeg_effect_filters
from visual.filters import ffmpeg_color_filters
from visual.seamless_loop import sinusoidal01


class EffectsTests(unittest.TestCase):
    def test_empty_stack_and_none_filter_do_nothing(self):
        self.assertEqual(ffmpeg_effect_filters([]), [])
        self.assertEqual(ffmpeg_color_filters("NONE"), [])

    def test_effect_stack_preserves_order(self):
        filters = ffmpeg_effect_filters([EffectItem("SCANLINES", .2), EffectItem("VIGNETTE", .5)])
        self.assertTrue(filters[0].startswith("drawgrid"))
        self.assertTrue(filters[1].startswith("vignette"))

    def test_animation_loop_matches_endpoints(self):
        self.assertAlmostEqual(sinusoidal01(0), sinusoidal01(10), places=7)
        self.assertAlmostEqual(sinusoidal01(10), sinusoidal01(60), places=7)


if __name__ == "__main__":
    unittest.main()
