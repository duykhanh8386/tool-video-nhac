from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from version import __repository__, __version__


API_URL = f"https://api.github.com/repos/{__repository__}/releases/latest"
RELEASES_URL = f"https://github.com/{__repository__}/releases/latest"
USER_AGENT = f"VisualLoopStudio/{__version__}"
HELPER_START_TIMEOUT_SECONDS = 8.0


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


def update_state_dir() -> Path:
    """Return a durable updater folder so failures survive TEMP cleanup."""
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return base / "VisualLoopStudio" / "Updater"


def update_log_path() -> Path:
    return update_state_dir() / "update.log"


def _wait_for_helper_ready(process, ready_path: Path, log_path: Path) -> None:
    deadline = time.monotonic() + HELPER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if ready_path.is_file():
            return
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"Bộ cập nhật không khởi động được (mã {exit_code}). Xem log: {log_path}"
            )
        time.sleep(0.05)
    try:
        process.terminate()
    except Exception:
        pass
    raise RuntimeError(f"Bộ cập nhật không phản hồi. Xem log: {log_path}")


def build_self_update_script() -> str:
    return r'''param(
    [Parameter(Mandatory=$true)][string]$Old,
    [Parameter(Mandatory=$true)][string]$New,
    [Parameter(Mandatory=$true)][int]$AppPid,
    [Parameter(Mandatory=$true)][string]$TargetVersion,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][string]$ReadyPath
)

$ErrorActionPreference = "Stop"

function Write-UpdateLog([string]$Message) {
    $line = "{0:u} {1}" -f (Get-Date), $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}

function Start-UpdatedApplication([string]$Path, [bool]$Fallback) {
    $arguments = @("--updated-to=$TargetVersion")
    if ($Fallback) {
        $arguments += "--update-fallback"
    }
    $workingDirectory = Split-Path -Parent $Path
    # A one-file PyInstaller child otherwise reuses the old process's _MEI
    # directory. The old parent removes that directory while exiting, which
    # can make the relaunched EXE fail to load python312.dll.
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    $started = Start-Process -FilePath $Path -WorkingDirectory $workingDirectory -ArgumentList $arguments -PassThru -ErrorAction Stop
    Write-UpdateLog "Relaunch requested: $Path (PID $($started.Id), fallback=$Fallback)."
    Start-Sleep -Seconds 4
    if ($started.HasExited) {
        throw "The updated application exited immediately with code $($started.ExitCode)."
    }
}

try {
    Set-Content -LiteralPath $ReadyPath -Value $PID -Encoding ASCII -ErrorAction Stop
    Write-UpdateLog "Updater started. Waiting for PID $AppPid."
    $deadline = (Get-Date).AddSeconds(20)
    while (Get-Process -Id $AppPid -ErrorAction SilentlyContinue) {
        if ((Get-Date) -gt $deadline) {
            throw "Timed out waiting for the old application to exit."
        }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep -Milliseconds 750

    if (-not (Test-Path -LiteralPath $New -PathType Leaf)) {
        throw "The downloaded update no longer exists: $New"
    }
    $newHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $New).Hash
    $installed = $false
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            Copy-Item -LiteralPath $New -Destination $Old -Force -ErrorAction Stop
            $oldHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Old).Hash
            if ($oldHash -ne $newHash) {
                throw "The installed EXE hash does not match the downloaded update."
            }
            $installed = $true
            break
        }
        catch {
            Write-UpdateLog "Install attempt $attempt failed: $($_.Exception.Message)"
            Start-Sleep -Seconds 1
        }
    }

    if (-not $installed) {
        throw "Windows did not allow the old EXE to be replaced after 30 attempts."
    }

    Write-UpdateLog "Update installed successfully: $Old"
    Start-UpdatedApplication -Path $Old -Fallback $false
    Remove-Item -LiteralPath $New -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $ReadyPath -Force -ErrorAction SilentlyContinue
    exit 0
}
catch {
    Write-UpdateLog "Update failed: $($_.Exception.Message)"
    if (Test-Path -LiteralPath $New -PathType Leaf) {
        try {
            Write-UpdateLog "Launching the verified downloaded EXE as fallback."
            Start-UpdatedApplication -Path $New -Fallback $true
        }
        catch {
            Write-UpdateLog "Fallback launch failed: $($_.Exception.Message)"
        }
    }
    Remove-Item -LiteralPath $ReadyPath -Force -ErrorAction SilentlyContinue
    exit 1
}
'''


def schedule_self_update(downloaded_exe: str | Path, target_version: str = "") -> Path:
    if not can_self_update():
        raise RuntimeError("Self-update chỉ hoạt động trong bản EXE đã đóng gói.")
    current = Path(sys.executable).resolve()
    downloaded = Path(downloaded_exe).resolve()
    if not downloaded.is_file():
        raise ValueError(f"Không tìm thấy EXE cập nhật đã tải: {downloaded}")
    state_dir = update_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    script = state_dir / f"update_{os.getpid()}.ps1"
    ready_path = state_dir / f"update_{os.getpid()}.ready"
    log_path = update_log_path()
    ready_path.unlink(missing_ok=True)
    # UTF-8 BOM is required for Windows PowerShell 5.1 to parse Unicode paths reliably.
    script.write_text(build_self_update_script(), encoding="utf-8-sig")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    executable = str(powershell) if powershell.is_file() else "powershell.exe"
    flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    with log_path.open("a", encoding="utf-8") as log_stream:
        log_stream.write(
            f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} Scheduling update "
            f"from {current} to {target_version or __version__}.\n"
        )
        log_stream.flush()
        process = subprocess.Popen(
            [
                executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-File", str(script),
                "-Old", str(current), "-New", str(downloaded), "-AppPid", str(os.getpid()),
                "-TargetVersion", target_version or __version__, "-LogPath", str(log_path),
                "-ReadyPath", str(ready_path),
            ],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
        )
        _wait_for_helper_ready(process, ready_path, log_path)
    return script
