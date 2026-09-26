from __future__ import annotations

import unittest
from unittest.mock import patch

from audio.duration import main_audio_duration
from render.ffprobe import MediaInfo


class AudioDurationTests(unittest.TestCase):
    def test_main_audio_is_master_clock(self):
        info = MediaInfo("song.wav", 9876.543, False, True)
        with patch("audio.duration.probe_media", return_value=info):
            self.assertEqual(main_audio_duration("song.wav"), 9876.543)

    def test_rejects_media_without_audio(self):
        info = MediaInfo("image.png", 0, True, False)
        with patch("audio.duration.probe_media", return_value=info):
            with self.assertRaises(ValueError):
                main_audio_duration("image.png")


if __name__ == "__main__":
    unittest.main()
