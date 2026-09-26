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


if __name__ == "__main__":
    unittest.main()
