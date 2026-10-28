from __future__ import annotations

import subprocess
from pathlib import Path

from auth.movoa_auth import MovoaAccount, MovoaAccountStore
from auth.movoa_generation import trim_video_end
from render.ffprobe import probe_media


def test_movoa_account_store(tmp_path: Path):
    store_file = tmp_path / "test_movoa_accounts.json"
    store = MovoaAccountStore(store_file)

    acc = store.create_account("Movoa Test 1", "movoa_user@gmail.com")
    assert acc.name == "Movoa Test 1"
    assert acc.email == "movoa_user@gmail.com"
    assert len(store.all()) == 1

    acc.status = "active"
    store.save(acc)

    loaded = store.get(acc.account_id)
    assert loaded is not None
    assert loaded.status == "active"

    # Test next_name
    assert store.next_name() == "Movoa 1"

    # Test import_from_json
    cookie_json = '[{"name": "session_id", "value": "xyz123", "domain": ".movoa.ai", "path": "/"}]'
    imported = store.import_from_json(cookie_json, default_name="Movoa Cookie", default_email="user_cookie@gmail.com")
    assert imported.name == "Movoa Cookie"
    assert imported.email == "user_cookie@gmail.com"
    assert len(imported.cookies) == 1
    assert imported.status == "active"

    store.delete(acc.account_id)
    store.delete(imported.account_id)
    assert len(store.all()) == 0


def test_movoa_trim_video_end(tmp_path: Path):
    raw_video = tmp_path / "movoa_sample_5s.mp4"
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
        "-t", "5.0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(raw_video)
    ]
    res = subprocess.run(cmd, capture_output=True, check=False)
    assert res.returncode == 0
    assert raw_video.exists()

    orig_info = probe_media(str(raw_video))
    assert 4.9 <= orig_info.duration <= 5.1

    trimmed_video = tmp_path / "movoa_sample_trimmed.mp4"
    out = trim_video_end(raw_video, trimmed_video, trim_seconds=1.5)
    assert out.exists()

    new_info = probe_media(str(trimmed_video))
    assert 3.4 <= new_info.duration <= 3.6
