from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


root = Path.cwd()
app_dir = root / "visual_loop_studio"
vendor_dir = app_dir / "vendor" / "bin"
version_file = root / "build" / "version_info.txt"

binaries = []
for executable in ("ffmpeg.exe", "ffprobe.exe"):
    path = vendor_dir / executable
    if not path.is_file():
        raise SystemExit(f"Missing bundled binary: {path}")
    binaries.append((str(path), "vendor/bin"))

a = Analysis(
    [str(app_dir / "main.py")],
    pathex=[str(app_dir)],
    binaries=binaries,
    datas=[],
    hiddenimports=["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "cv2", "numpy"] + collect_submodules("py7zr"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VisualLoopStudio-Windows-x64",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(version_file) if version_file.is_file() else None,
)
