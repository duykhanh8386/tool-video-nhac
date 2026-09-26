from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai.local_runtime import (
    MODEL_DOWNLOADS,
    RUNTIME_BACKENDS,
    build_runtime_command,
    detect_runtime_backend,
    extract_7z_archive,
    installed_runtime_backend,
    is_runtime_installed,
    missing_runtime_files,
    runtime_paths,
)


class LocalRuntimeTests(unittest.TestCase):
    def test_runtime_layout_and_hidden_server_command(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = runtime_paths(folder)
            self.assertEqual(paths["seven_zip"].name, "7zr.exe")
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

    def test_gpu_vendor_detection_selects_matching_portable_backend(self):
        cases = (
            (["NVIDIA GeForce RTX 3060"], "nvidia"),
            (["Intel(R) Arc(TM) A770 Graphics"], "intel"),
            (["AMD Radeon RX 7800 XT"], "amd"),
            (["Intel(R) UHD Graphics 770"], "cpu"),
        )
        for adapters, expected in cases:
            with self.subTest(adapters=adapters):
                self.assertEqual(detect_runtime_backend(adapters).key, expected)

    def test_backend_specific_archive_and_launch_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            intel_paths = runtime_paths(folder, "intel")
            self.assertEqual(intel_paths["archive"].name, "ComfyUI_windows_portable_intel.7z")
            command, _cwd = build_runtime_command(folder, "http://127.0.0.1:8188", "intel")
            self.assertNotIn("--cpu", command)
            cpu_command, _cwd = build_runtime_command(folder, "http://127.0.0.1:8188", "cpu")
            self.assertIn("--cpu", cpu_command)

    def test_installed_backend_mismatch_requires_reinstall_but_keeps_models(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = runtime_paths(folder, "intel")
            paths["python"].parent.mkdir(parents=True)
            paths["python"].write_bytes(b"exe")
            paths["main"].parent.mkdir(parents=True)
            paths["main"].write_text("# comfy", encoding="utf-8")
            paths["portable_marker"].write_text("nvidia", encoding="ascii")
            for item in MODEL_DOWNLOADS:
                model = paths["models"] / item.folder / item.filename
                model.parent.mkdir(parents=True, exist_ok=True)
                model.write_bytes(b"0" * 1024 * 1024)
            missing = missing_runtime_files(folder, RUNTIME_BACKENDS["intel"])
            self.assertTrue(any("NVIDIA CUDA" in item and "Intel Arc XPU" in item for item in missing))
            self.assertFalse(is_runtime_installed(folder, "intel"))

    def test_legacy_marker_can_identify_xpu_torch_build(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = runtime_paths(folder, "intel")
            paths["portable_marker"].parent.mkdir(parents=True, exist_ok=True)
            paths["portable_marker"].write_text("ok", encoding="ascii")
            version_file = paths["portable"] / "python_embeded/Lib/site-packages/torch/version.py"
            version_file.parent.mkdir(parents=True, exist_ok=True)
            version_file.write_text("xpu: str = '2026.1'\ncuda = None\n", encoding="utf-8")
            self.assertEqual(installed_runtime_backend(paths), "intel")

    def test_managed_runtime_rejects_remote_url(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "localhost"):
                build_runtime_command(folder, "http://192.168.1.4:8188")

    def test_7zr_extract_command_supports_native_comfy_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "ComfyUI.7z"
            archive.write_bytes(b"archive")
            seven_zip = root / "7zr.exe"
            seven_zip.write_bytes(b"exe")
            with patch(
                "ai.local_runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="Everything is Ok"),
            ) as run:
                extract_7z_archive(archive, root / "output", seven_zip)
            command = run.call_args.args[0]
            self.assertEqual(command[:3], [str(seven_zip.resolve()), "x", "-y"])
            self.assertIn(f"-o{(root / 'output').resolve()}", command)


if __name__ == "__main__":
    unittest.main()
