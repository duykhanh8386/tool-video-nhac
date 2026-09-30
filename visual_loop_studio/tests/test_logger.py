from __future__ import annotations

import unittest

from utils.logger import explain_ffmpeg_error


class LoggerTests(unittest.TestCase):
    def test_invalid_ffmpeg_text_color_has_an_actionable_message(self) -> None:
        output = """
[Parsed_drawtext_17] Invalid 0xRRGGBB[AA] color string: '67894'
[Parsed_drawtext_17] Unable to parse \"fontcolor\" option value \"#67894@1.0000\" as color
Error: Invalid argument
"""
        self.assertEqual(
            explain_ffmpeg_error(output),
            "Màu chữ không hợp lệ. Hãy dùng mã HEX gồm đúng 6 ký tự, ví dụ #067894.",
        )


if __name__ == "__main__":
    unittest.main()
