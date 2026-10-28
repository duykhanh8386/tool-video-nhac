from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from auth.zsky_auth import ZskyAccount, ZskyAccountStore
from auth.zsky_generation import trim_video_end
from render.ffprobe import probe_media


def test_zsky_account_store(tmp_path: Path):
    store_file = tmp_path / "test_zsky_accounts.json"
    store = ZskyAccountStore(store_file)

    acc = store.create_account("Test Account 1", "test@gmail.com")
    assert acc.name == "Test Account 1"
    assert acc.email == "test@gmail.com"
    assert len(store.all()) == 1

    acc.status = "active"
    store.save(acc)

    loaded = store.get(acc.account_id)
    assert loaded is not None
    assert loaded.status == "active"

    # Test next_name
    assert store.next_name() == "Zsky 1"

    # Test import_from_json
    cookie_json = '[{"name": "session_id", "value": "xyz123", "domain": ".zsky.ai", "path": "/"}]'
    imported = store.import_from_json(cookie_json, default_name="Zsky Cookie", default_email="zsky_user@gmail.com")
    assert imported.name == "Zsky Cookie"
    assert imported.email == "zsky_user@gmail.com"
    assert len(imported.cookies) == 1
    assert imported.status == "active"

    store.delete(acc.account_id)
    store.delete(imported.account_id)
    assert len(store.all()) == 0


def test_trim_video_end(tmp_path: Path):
    # Tạo video test dài 5 giây bằng ffmpeg lavfi testsrc
    raw_video = tmp_path / "sample_5s.mp4"
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

    # Cắt bỏ 1.5s cuối
    trimmed_video = tmp_path / "sample_trimmed.mp4"
    out = trim_video_end(raw_video, trimmed_video, trim_seconds=1.5)
    assert out.exists()

    new_info = probe_media(str(trimmed_video))
    # 5.0 - 1.5 = 3.5s
    assert 3.4 <= new_info.duration <= 3.6
