from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QFile, QStandardPaths


SHORTCUT_NAME = "Visual Loop Studio.lnk"


def desktop_directory() -> Path:
    """Return the user's real Windows Desktop, including redirected desktops."""
    value = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    if not value:
        raise OSError("Windows không trả về được đường dẫn Desktop.")
    desktop = Path(value)
    if not desktop.is_dir():
        raise OSError(f"Không tìm thấy thư mục Desktop: {desktop}")
    return desktop


def create_desktop_shortcut(
    executable: str | Path | None = None,
    desktop: str | Path | None = None,
) -> Path:
    """Create or atomically refresh the app's Windows .lnk shortcut."""
    target = Path(executable or sys.executable).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"Không tìm thấy file EXE để tạo shortcut: {target}")

    desktop_path = Path(desktop) if desktop is not None else desktop_directory()
    if not desktop_path.is_dir():
        raise OSError(f"Không tìm thấy thư mục Desktop: {desktop_path}")

    shortcut = desktop_path / SHORTCUT_NAME
    temporary = desktop_path / f".{shortcut.stem}.{os.getpid()}.tmp.lnk"
    QFile.remove(str(temporary))
    try:
        # On Windows, QFile.link creates a native Shell Link (.lnk). Windows
        # automatically uses the target EXE's working folder and embedded icon.
        if not QFile.link(str(target), str(temporary)):
            raise OSError(f"Không thể tạo shortcut tạm trên Desktop: {temporary}")
        os.replace(temporary, shortcut)
    finally:
        QFile.remove(str(temporary))
    return shortcut


def ensure_desktop_shortcut(argv: list[str] | None = None) -> Path | None:
    """Create the shortcut for packaged Windows builds and skip source runs."""
    arguments = sys.argv if argv is None else argv
    if os.name != "nt" or not bool(getattr(sys, "frozen", False)):
        return None
    # The updater can temporarily launch a downloaded EXE when the installed
    # file is locked. Never leave a permanent shortcut pointing into TEMP.
    if "--update-fallback" in arguments:
        return None
    return create_desktop_shortcut()
