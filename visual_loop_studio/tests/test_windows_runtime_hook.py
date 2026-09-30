from __future__ import annotations

import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


RUNTIME_HOOK = Path(__file__).resolve().parents[2] / "build" / "windows_runtime_hook.py"


class WindowsRuntimeHookTests(unittest.TestCase):
    def test_registers_numpy_dll_directory_and_keeps_handle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            numpy_libs = root / "numpy.libs"
            numpy_libs.mkdir()
            handle = object()
            add_dll_directory = MagicMock(return_value=handle)
            original_meipass = getattr(sys, "_MEIPASS", None)
            had_meipass = hasattr(sys, "_MEIPASS")
            original_handles = getattr(sys, "_visual_loop_dll_directory_handles", None)
            had_handles = hasattr(sys, "_visual_loop_dll_directory_handles")
            try:
                sys._MEIPASS = str(root)
                if had_handles:
                    del sys._visual_loop_dll_directory_handles
                with (
                    patch("os.name", "nt"),
                    patch("os.add_dll_directory", add_dll_directory, create=True),
                ):
                    runpy.run_path(str(RUNTIME_HOOK))
                add_dll_directory.assert_called_once_with(str(numpy_libs))
                self.assertEqual(sys._visual_loop_dll_directory_handles, [handle])
            finally:
                if had_meipass:
                    sys._MEIPASS = original_meipass
                elif hasattr(sys, "_MEIPASS"):
                    del sys._MEIPASS
                if had_handles:
                    sys._visual_loop_dll_directory_handles = original_handles
                elif hasattr(sys, "_visual_loop_dll_directory_handles"):
                    del sys._visual_loop_dll_directory_handles


if __name__ == "__main__":
    unittest.main()
