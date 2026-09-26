from __future__ import annotations

import unittest

from models.visual_project import VisualProject
from visual.compositor import build_visual_graph


class CompositorTests(unittest.TestCase):
    def test_waveform_media_loops_without_audio_reactivity(self):
        project = VisualProject(background="background.png", waveform_media="wave.mov", waveform="FILE")
        graph = build_visual_graph(project, 1920, 1080)
        self.assertIn("wave.mov", graph.inputs)
        self.assertIn("-stream_loop", graph.inputs)
        self.assertIn("colorkey=0xFFFFFF", graph.filter_complex)
        self.assertNotIn("showwaves", graph.filter_complex)
        self.assertNotIn("showspectrum", graph.filter_complex)
        self.assertIsNone(graph.audio_map)

    def test_text_size_scales_from_same_1080p_design_size(self):
        project = VisualProject(background="background.png", title="Tiêu đề lớn")
        graph_1080 = build_visual_graph(project, 1920, 1080)
        graph_4k = build_visual_graph(project, 3840, 2160)
        self.assertIn("fontsize=96", graph_1080.filter_complex)
        self.assertIn("fontsize=192", graph_4k.filter_complex)

    def test_image_element_supports_lighten_blend(self):
        project = VisualProject(background="background.png", logo="logo.png")
        project.elements["logo"].blend_mode = "lighten"
        graph = build_visual_graph(project, 1920, 1080)
        self.assertIn("blend=all_mode=lighten", graph.filter_complex)
        self.assertIn("color=c=black:s=1920x1080", graph.filter_complex)


if __name__ == "__main__":
    unittest.main()
