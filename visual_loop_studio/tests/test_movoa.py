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


def test_movoa_rendering_wait_logic():
    """Kiểm tra logic đợi render Movoa với driver giả lập."""
    from unittest.mock import MagicMock
    from auth.movoa_generation import is_movoa_rendering, wait_for_movoa_clip_ready

    mock_driver = MagicMock()

    # 1. Test is_movoa_rendering returns True when script says True
    mock_driver.execute_script.return_value = True
    assert is_movoa_rendering(mock_driver) is True

    # 2. Test is_movoa_rendering returns False when script says False
    mock_driver.execute_script.return_value = False
    assert is_movoa_rendering(mock_driver) is False

    # 3. Test wait_for_movoa_clip_ready with state progression:
    # Phase 1: render start (True)
    # Phase 2: rendering for 2 ticks (True, True, then False)
    # Phase 3: has_dl_btn (True)
    script_responses = [
        [False, ""],  # check_error
        True,         # is_movoa_rendering -> started
        [False, ""],  # check_error
        True,         # is_rendering (tick 1)
        [False, ""],  # check_error
        False,        # is_rendering (finished)
        True,         # has_dl_btn
    ]

    def mock_exec(script, *args):
        if script_responses:
            return script_responses.pop(0)
        return False

    mock_driver.execute_script.side_effect = mock_exec
    logs = []
    res = wait_for_movoa_clip_ready(
        mock_driver,
        initial_video_src="old_clip.mp4",
        timeout=10.0,
        poll_interval=0.1,
        log_cb=logs.append
    )
    assert res is True
    assert any("đang trong hàng đợi" in l.lower() or "tiếp nhận" in l.lower() for l in logs)
    assert any("kết thúc" in l.lower() for l in logs)


def test_check_movoa_render_error_no_false_positive():
    """Đảm bảo chữ 'No Daily Cap' trên giao diện Movoa không bao giờ bị nhận nhầm là lỗi chạm giới hạn."""
    from unittest.mock import MagicMock
    from auth.movoa_generation import check_movoa_render_error, set_prompt_text

    mock_driver = MagicMock()

    # 1. Giả lập khi trang web có chữ No Daily Cap (khẩu hiệu web Movoa)
    mock_driver.execute_script.return_value = [False, ""]
    err, msg = check_movoa_render_error(mock_driver)
    assert err is False
    assert msg == ""

    # 2. Giả lập khi có lỗi thật từ Movoa
    mock_driver.execute_script.return_value = [True, "Tài khoản chạm giới hạn tạo video hàng ngày của Movoa."]
    err, msg = check_movoa_render_error(mock_driver)
    assert err is True
    assert "giới hạn" in msg

    # 3. Test set_prompt_text thành công khi JS setter ăn
    mock_driver.execute_script.return_value = True
    assert set_prompt_text(mock_driver, "A beautiful cinematic landscape") is True


