from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QFile

from utils import shortcuts


@unittest.skipUnless(os.name == "nt", "Windows Shell Links are only available on Windows")
class DesktopShortcutTests(unittest.TestCase):
    def test_create_desktop_shortcut_points_to_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "VisualLoopStudio-Windows-x64.exe"
            target.write_bytes(b"test executable")
            desktop = root / "Desktop"
            desktop.mkdir()

            shortcut = shortcuts.create_desktop_shortcut(target, desktop)

            self.assertEqual(shortcut, desktop / shortcuts.SHORTCUT_NAME)
            self.assertTrue(shortcut.is_file())
            self.assertEqual(Path(QFile.symLinkTarget(str(shortcut))), target.resolve())

    def test_existing_shortcut_is_refreshed_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.exe"
            second = root / "second.exe"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            desktop = root / "Desktop"
            desktop.mkdir()

            shortcuts.create_desktop_shortcut(first, desktop)
            shortcut = shortcuts.create_desktop_shortcut(second, desktop)

            self.assertEqual(Path(QFile.symLinkTarget(str(shortcut))), second.resolve())
            self.assertFalse(any(desktop.glob(".*.tmp.lnk")))

    def test_fallback_update_does_not_point_shortcut_to_temp_exe(self) -> None:
        with (
            patch.object(shortcuts.sys, "frozen", True, create=True),
            patch.object(shortcuts, "create_desktop_shortcut") as create,
        ):
            self.assertIsNone(shortcuts.ensure_desktop_shortcut(["app.exe", "--update-fallback"]))
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
