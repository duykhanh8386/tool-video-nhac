from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from version import __repository__, __version__


API_URL = f"https://api.github.com/repos/{__repository__}/releases/latest"
RELEASES_URL = f"https://github.com/{__repository__}/releases/latest"
USER_AGENT = f"VisualLoopStudio/{__version__}"


@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: int = 0


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    name: str
    notes: str
    page_url: str
    executable: ReleaseAsset
    checksum: ReleaseAsset | None = None


def version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.split("+", 1)[0])
    return tuple(int(item) for item in numbers[:4]) or (0,)


def is_newer_version(latest: str, current: str = __version__) -> bool:
    left, right = version_tuple(latest), version_tuple(current)
    length = max(len(left), len(right))
    return left + (0,) * (length - len(left)) > right + (0,) * (length - len(right))


def parse_release(payload: dict) -> ReleaseInfo:
    assets = [ReleaseAsset(item.get("name", ""), item.get("browser_download_url", ""), int(item.get("size", 0))) for item in payload.get("assets", [])]
    executable = next((item for item in assets if item.name.lower() == "visualloopstudio-windows-x64.exe"), None)
    if not executable:
        executable = next((item for item in assets if item.name.lower().endswith(".exe") and "setup" not in item.name.lower()), None)
    if not executable or not executable.url:
        raise ValueError("Release mới không có file EXE Windows.")
    checksum = next((item for item in assets if item.name.lower() in {executable.name.lower() + ".sha256", "sha256sums.txt"}), None)
    tag = str(payload.get("tag_name", ""))
    return ReleaseInfo(
        version=tag.lstrip("vV") or str(payload.get("name", "0")), tag=tag,
        name=str(payload.get("name") or tag), notes=str(payload.get("body") or ""),
        page_url=str(payload.get("html_url") or RELEASES_URL), executable=executable, checksum=checksum,
    )


def check_latest_release(timeout: int = 20) -> ReleaseInfo:
    request = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_release(json.load(response))


def download_release(
    release: ReleaseInfo,
    destination: str | Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    target = Path(destination) if destination else Path(tempfile.gettempdir()) / f"VisualLoopStudio-{release.version}-update.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    if not release.checksum:
        raise ValueError("Release không có file SHA-256; từ chối cập nhật để bảo vệ an toàn.")
    request = urllib.request.Request(release.executable.url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as stream:
        total = int(response.headers.get("Content-Length") or release.executable.size or 0)
        downloaded = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            stream.write(block)
            digest.update(block)
            downloaded += len(block)
            if progress:
                progress(downloaded, total)
    checksum_request = urllib.request.Request(release.checksum.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(checksum_request, timeout=20) as response:
        expected = response.read().decode("utf-8", errors="replace").strip().split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest.hexdigest().lower() != expected:
        partial.unlink(missing_ok=True)
        raise ValueError("SHA-256 của bản cập nhật không hợp lệ. Đã hủy cài đặt.")
    partial.replace(target)
    return target


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and Path(sys.executable).name.lower().endswith(".exe")


def schedule_self_update(downloaded_exe: str | Path) -> Path:
    if not can_self_update():
        raise RuntimeError("Self-update chỉ hoạt động trong bản EXE đã đóng gói.")
    current = Path(sys.executable).resolve()
    downloaded = Path(downloaded_exe).resolve()
    script = Path(tempfile.gettempdir()) / f"visual_loop_studio_update_{os.getpid()}.cmd"
    content = f"""@echo off
setlocal
set "OLD={current}"
set "NEW={downloaded}"
set "APP_PID={os.getpid()}"
:wait_for_exit
tasklist /FI "PID eq %APP_PID%" 2>NUL | find "%APP_PID%" >NUL
if not errorlevel 1 (
  timeout /t 1 /nobreak >NUL
  goto wait_for_exit
)
copy /Y "%NEW%" "%OLD%" >NUL
if errorlevel 1 (
  start "" "%NEW%"
  exit /b 1
)
start "" "%OLD%"
del /Q "%NEW%" >NUL 2>NUL
(goto) 2>NUL & del /Q "%~f0"
"""
    script.write_text(content, encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=flags, close_fds=True)
    return script
