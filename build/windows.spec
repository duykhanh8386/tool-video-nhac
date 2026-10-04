from pathlib import Path
from PyInstaller.utils.hooks import collect_all


root = Path.cwd()
app_dir = root / "visual_loop_studio"
vendor_dir = app_dir / "vendor" / "bin"
version_file = root / "build" / "version_info.txt"
icon_png = app_dir / "assets" / "app_logo.png"
icon_ico = app_dir / "assets" / "app_icon.ico"

for asset in (icon_png, icon_ico):
    if not asset.is_file():
        raise SystemExit(f"Missing app branding asset: {asset}")

binaries = []
for executable in ("ffmpeg.exe", "ffprobe.exe"):
    path = vendor_dir / executable
    if not path.is_file():
        raise SystemExit(f"Missing bundled binary: {path}")
    binaries.append((str(path), "vendor/bin"))

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all("playwright")
selenium_datas, selenium_binaries, selenium_hiddenimports = collect_all("selenium")
binaries += playwright_binaries
binaries += selenium_binaries

a = Analysis(
    [str(app_dir / "main.py")],
    pathex=[str(app_dir)],
    binaries=binaries,
    datas=[*playwright_datas, *selenium_datas, (str(icon_png), "assets")],
    hiddenimports=[
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "cv2", "numpy",
        "ai.providers.byteplus_seedance", "ai.providers.comfyui_provider", "ai.providers.veo_provider",
        "auth.google_youtube", "auth.youtube_studio", "ui.youtube_accounts",
        "auth.muse_login", "auth.muse_generation", "auth.muse_sessions", "auth.muse_video_batch",
        "ai.providers.muse_web_provider", "ui.muse_accounts",
        *playwright_hiddenimports, *selenium_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "build" / "windows_runtime_hook.py")],
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
    icon=str(icon_ico),
)
