from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai.local_runtime import (
    MODEL_DOWNLOADS,
    build_runtime_command,
    is_runtime_installed,
    missing_runtime_files,
    runtime_paths,
)


class LocalRuntimeTests(unittest.TestCase):
    def test_runtime_layout_and_hidden_server_command(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = runtime_paths(folder)
            paths["python"].parent.mkdir(parents=True)
            paths["python"].write_bytes(b"exe")
            paths["main"].parent.mkdir(parents=True)
            paths["main"].write_text("# comfy", encoding="utf-8")
            paths["portable_marker"].write_text("ok", encoding="ascii")
            command, cwd = build_runtime_command(folder, "http://127.0.0.1:8188")

            self.assertEqual(Path(command[0]), paths["python"])
            self.assertIn("--disable-auto-launch", command)
            self.assertEqual(command[command.index("--port") + 1], "8188")
            self.assertEqual(cwd, paths["portable"])

    def test_runtime_requires_all_three_models(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = runtime_paths(folder)
            paths["python"].parent.mkdir(parents=True)
            paths["python"].write_bytes(b"exe")
            paths["main"].parent.mkdir(parents=True)
            paths["main"].write_text("# comfy", encoding="utf-8")
            self.assertFalse(is_runtime_installed(folder))
            paths["portable_marker"].write_text("ok", encoding="ascii")
            for item in MODEL_DOWNLOADS:
                model = paths["models"] / item.folder / item.filename
                model.parent.mkdir(parents=True, exist_ok=True)
                model.write_bytes(b"0" * 1024 * 1024)
            self.assertEqual(missing_runtime_files(folder), [])
            self.assertTrue(is_runtime_installed(folder))

    def test_model_sources_are_https_and_have_sha256(self):
        self.assertEqual(len(MODEL_DOWNLOADS), 3)
        for item in MODEL_DOWNLOADS:
            self.assertTrue(item.url.startswith("https://"))
            self.assertEqual(len(item.sha256), 64)
            int(item.sha256, 16)

    def test_managed_runtime_rejects_remote_url(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "localhost"):
                build_runtime_command(folder, "http://192.168.1.4:8188")


if __name__ == "__main__":
    unittest.main()
