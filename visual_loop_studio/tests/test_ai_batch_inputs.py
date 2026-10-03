from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai.batch_inputs import BatchPrompt, expand_batch_inputs, parse_prompt_file


class AiBatchInputTests(unittest.TestCase):
    def test_txt_ignores_blank_and_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompts.txt"
            path.write_text("# comment\nfirst prompt\n\nsecond prompt\n", encoding="utf-8")
            rows = parse_prompt_file(path)
        self.assertEqual([item.prompt for item in rows], ["first prompt", "second prompt"])

    def test_csv_resolves_relative_image_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prompts.csv"
            path.write_text("prompt,image\nhello,images/a.png\n", encoding="utf-8")
            rows = parse_prompt_file(path)
            self.assertEqual(rows[0].image_path, str((Path(folder) / "images/a.png").resolve()))

    def test_one_prompt_expands_across_all_selected_images(self) -> None:
        rows = expand_batch_inputs([BatchPrompt("hello")], ["one.png", "two.png"])
        self.assertEqual(len(rows), 2)
        self.assertEqual({Path(item.image_path).name for item in rows}, {"one.png", "two.png"})

    def test_mismatched_prompt_and_image_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            expand_batch_inputs(
                [BatchPrompt("one"), BatchPrompt("two")],
                ["one.png", "two.png", "three.png"],
            )


if __name__ == "__main__":
    unittest.main()
