from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from utils.paths import USER_DATA_ROOT


YOUTUBE_STUDIO_URL = "https://studio.youtube.com/"
YOUTUBE_STUDIO_PROFILES_DIR = USER_DATA_ROOT / "YouTubeStudioProfiles"


class YouTubeStudioBrowserError(RuntimeError):
    pass


def youtube_studio_profile_dir(account_id: str) -> Path:
    safe_id = "".join(character for character in str(account_id) if character.isalnum() or character in "-_")
    if not safe_id:
        raise ValueError("Account ID không hợp lệ.")
    return YOUTUBE_STUDIO_PROFILES_DIR / safe_id


def open_youtube_studio(account_id: str, profile_dir: str | Path = "") -> tuple[subprocess.Popen, Path]:
    """Open Studio in a normal, account-specific browser profile for manual authentication."""
    profile = Path(profile_dir) if profile_dir else youtube_studio_profile_dir(account_id)
    profile.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for name, executable in _browser_candidates():
        command = [
            str(executable),
            f"--user-data-dir={profile.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            YOUTUBE_STUDIO_URL,
        ]
        try:
            return subprocess.Popen(command), profile
        except OSError:
            errors.append(name)
    suffix = f" ({', '.join(errors)})" if errors else ""
    raise YouTubeStudioBrowserError(
        "Không mở được Google Chrome hoặc Microsoft Edge. Hãy cài/cập nhật một trong hai trình duyệt." + suffix
    )


def _browser_candidates() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []

    def add(name: str, value: str | Path | None) -> None:
        if not value:
            return
        path = Path(value)
        if not path.is_file():
            return
        key = str(path.resolve()).casefold()
        if all(str(existing.resolve()).casefold() != key for _label, existing in candidates):
            candidates.append((name, path))

    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    local_app_data = os.environ.get("LOCALAPPDATA")
    for name, root, relative in (
        ("Google Chrome", program_files, "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", program_files_x86, "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", local_app_data, "Google/Chrome/Application/chrome.exe"),
        ("Microsoft Edge", program_files_x86, "Microsoft/Edge/Application/msedge.exe"),
        ("Microsoft Edge", program_files, "Microsoft/Edge/Application/msedge.exe"),
        ("Microsoft Edge", local_app_data, "Microsoft/Edge/Application/msedge.exe"),
    ):
        add(name, Path(root) / relative if root else None)
    for name, command in (
        ("Google Chrome", "chrome"),
        ("Google Chrome", "google-chrome"),
        ("Microsoft Edge", "msedge"),
    ):
        add(name, shutil.which(command))
    return candidates
