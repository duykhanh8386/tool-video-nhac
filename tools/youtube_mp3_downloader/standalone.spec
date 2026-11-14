# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

block_cipher = None
root_dir = Path.cwd()
app_dir = root_dir / "tools" / "youtube_mp3_downloader"
if not app_dir.is_dir():
    app_dir = root_dir

icon_path = str(app_dir / "icon.ico")

# Collect yt_dlp, certifi, mutagen, PIL
ytdlp_datas, ytdlp_binaries, ytdlp_hiddenimports = collect_all("yt_dlp")
certifi_datas, certifi_binaries, certifi_hiddenimports = collect_all("certifi")
mutagen_datas, mutagen_binaries, mutagen_hiddenimports = collect_all("mutagen")
pil_datas, pil_binaries, pil_hiddenimports = collect_all("PIL")

binaries = []
tools_dir = Path(r"C:\Users\Mixi World\AppData\Local\Programs\YouTube MP3 Downloader\_internal\tools")
if not tools_dir.is_dir():
    tools_dir = app_dir / "dist" / "YouTube MP3 Downloader" / "_internal" / "tools"

for bin_rel in [("ffmpeg/ffmpeg.exe", "tools/ffmpeg"), ("ffmpeg/ffprobe.exe", "tools/ffmpeg"), ("node/node.EXE", "tools/node")]:
    src = tools_dir / bin_rel[0]
    if src.is_file():
        binaries.append((str(src), bin_rel[1]))

binaries += ytdlp_binaries + certifi_binaries + mutagen_binaries + pil_binaries

datas = [
    (str(app_dir / "icon.ico"), "downloader_app"),
]
datas += ytdlp_datas + certifi_datas + mutagen_datas + pil_datas

hiddenimports = [
    "downloader_app",
    "downloader_app.app",
    "downloader_app.downloader",
    "ctypes",
    "ctypes.wintypes",
    "urllib.request",
    "urllib.parse",
    "json",
    "sqlite3",
] + ytdlp_hiddenimports + certifi_hiddenimports + mutagen_hiddenimports + pil_hiddenimports

a = Analysis(
    [str(app_dir / "launcher.py")],
    pathex=[str(app_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "PyQt5", "tkinter", "matplotlib", "scipy", "pandas"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="YouTube MP3 Downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if Path(icon_path).is_file() else None,
)

