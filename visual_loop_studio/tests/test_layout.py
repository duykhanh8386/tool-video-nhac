from __future__ import annotations

import unittest

from models.visual_project import ElementLayout, default_element_layouts
from visual.layout import BackgroundAnalysis, ZoneMetric, compose_layout, to_top_left_rect, update_from_top_left


class SmartLayoutTests(unittest.TestCase):
    def _analysis(self, side: str) -> BackgroundAnalysis:
        subject = {"LEFT": (.02, .10, .38, .80), "CENTER": (.31, .10, .38, .80), "RIGHT": (.60, .10, .38, .80)}[side]
        zones = [ZoneMetric(column, row, .45, .20) for row in range(4) for column in range(6)]
        return BackgroundAnalysis(subject_side=side, subject_bbox=subject, zones=zones)

    def test_auto_layout_avoids_left_subject_for_title(self):
        result = compose_layout(self._analysis("LEFT"), template="Minimal")
        title = to_top_left_rect(result.elements["title"])
        self.assertGreater(title[0], .40)

    def test_locked_element_never_moves(self):
        elements = default_element_layouts()
        elements["logo"].x = .77
        elements["logo"].y = .81
        elements["logo"].locked = True
        result = compose_layout(self._analysis("RIGHT"), elements, "Reggae", variant=2)
        self.assertEqual((result.elements["logo"].x, result.elements["logo"].y), (.77, .81))

    def test_try_another_produces_a_different_valid_variant(self):
        active = {"title", "logo", "waveform"}
        first = compose_layout(self._analysis("LEFT"), template="Reggae", variant=0, active_elements=active)
        second = compose_layout(self._analysis("LEFT"), template="Reggae", variant=1, active_elements=active)
        positions_a = {(first.elements[name].x, first.elements[name].y) for name in active}
        positions_b = {(second.elements[name].x, second.elements[name].y) for name in active}
        self.assertNotEqual(positions_a, positions_b)
        for name in active:
            x, y, width, height = to_top_left_rect(second.elements[name])
            self.assertGreaterEqual(x, .02)
            self.assertGreaterEqual(y, .02)
            self.assertLessEqual(x + width, .98)
            self.assertLessEqual(y + height, .98)

    def test_anchor_conversion_round_trip(self):
        layout = ElementLayout(.5, .5, .2, .1, anchor="center")
        rect = to_top_left_rect(layout)
        self.assertEqual(rect, (.4, .45, .2, .1))
        update_from_top_left(layout, rect)
        self.assertAlmostEqual(layout.x, .5)
        self.assertAlmostEqual(layout.y, .5)


if __name__ == "__main__":
    unittest.main()
