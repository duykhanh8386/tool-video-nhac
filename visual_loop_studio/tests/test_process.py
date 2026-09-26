from __future__ import annotations

import unittest
from unittest.mock import patch

import utils.process as process_utils


class _StartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 0


class HiddenProcessTests(unittest.TestCase):
    def test_non_windows_needs_no_special_flags(self):
        with patch.object(process_utils.os, "name", "posix"):
            self.assertEqual(process_utils.hidden_process_kwargs(), {})

    def test_windows_processes_are_created_without_console(self):
        with (
            patch.object(process_utils.os, "name", "nt"),
            patch.object(process_utils.subprocess, "STARTUPINFO", _StartupInfo, create=True),
            patch.object(process_utils.subprocess, "STARTF_USESHOWWINDOW", 1, create=True),
            patch.object(process_utils.subprocess, "SW_HIDE", 0, create=True),
            patch.object(process_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
        ):
            options = process_utils.hidden_process_kwargs()
        self.assertEqual(options["creationflags"], 0x08000000)
        self.assertEqual(options["startupinfo"].dwFlags & 1, 1)
        self.assertEqual(options["startupinfo"].wShowWindow, 0)


if __name__ == "__main__":
    unittest.main()
