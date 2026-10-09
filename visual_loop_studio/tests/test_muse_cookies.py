from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from auth.muse_cookies import (
    DEFAULT_MUSE_STYLE_SUFFIX,
    MuseCookieAccount,
    MuseCookieAccountStore,
    build_muse_chunk_prompt,
    extract_email_from_muse_page,
    extract_email_from_text_or_cookies,
    inject_cookies_to_driver,
    parse_muse_cookies,
)


class TestMuseCookies(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store_path = Path(self.temp_dir.name) / "muse_cookie_accounts.json"
        self.store = MuseCookieAccountStore(self.store_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parse_json_array_cookies(self):
        raw = json.dumps([
            {
                "name": "session",
                "value": "xyz123",
                "domain": ".muse.ai",
                "path": "/",
                "secure": True,
                "expirationDate": 1893456000,
            },
            {
                "name": "token",
                "value": "abc456",
                "domain": "muse.ai",
                "path": "/app",
                "secure": False,
            },
        ])
        cookies = parse_muse_cookies(raw)
        self.assertEqual(len(cookies), 2)
        self.assertEqual(cookies[0]["name"], "session")
        self.assertEqual(cookies[0]["value"], "xyz123")
        self.assertEqual(cookies[0]["domain"], ".muse.ai")
        self.assertEqual(cookies[0]["expiry"], 1893456000)

        self.assertEqual(cookies[1]["name"], "token")
        self.assertEqual(cookies[1]["domain"], ".muse.ai")

    def test_parse_netscape_format(self):
        raw = (
            "# Netscape HTTP Cookie File\n"
            ".muse.ai\tTRUE\t/\tTRUE\t1893456000\tsession\tnetscape123\n"
            "muse.ai\tFALSE\t/api\tFALSE\t1893456000\ttoken\tnetscape456\n"
        )
        cookies = parse_muse_cookies(raw)
        self.assertEqual(len(cookies), 2)
        self.assertEqual(cookies[0]["name"], "session")
        self.assertEqual(cookies[0]["value"], "netscape123")
        self.assertEqual(cookies[1]["name"], "token")
        self.assertEqual(cookies[1]["value"], "netscape456")

    def test_parse_header_string(self):
        raw = "session=header123; token=header456; other_val=test"
        cookies = parse_muse_cookies(raw)
        self.assertEqual(len(cookies), 3)
        names = {c["name"] for c in cookies}
        self.assertEqual(names, {"session", "token", "other_val"})

    def test_extract_email_from_filename(self):
        email = extract_email_from_text_or_cookies("{}", [], filename="user123@gmail.com.json")
        self.assertEqual(email, "user123@gmail.com")

    def test_extract_email_from_raw_text(self):
        raw = "Logged in as cuongbinhha41609kick5@huta.pro with cookies"
        email = extract_email_from_text_or_cookies(raw, [])
        self.assertEqual(email, "cuongbinhha41609kick5@huta.pro")

    def test_store_crud(self):
        self.assertEqual(len(self.store.all()), 0)

        raw = json.dumps([{"name": "session", "value": "test"}])
        acc = self.store.import_from_json(raw, default_name="Muse 1", default_email="test@example.com")
        self.assertEqual(acc.name, "Muse 1")
        self.assertEqual(acc.email, "test@example.com")
        self.assertEqual(len(self.store.all()), 1)

        # Update email
        self.store.update_email(acc.account_id, "updated@example.com")
        loaded = self.store.get(acc.account_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.email, "updated@example.com")

        # Update status
        self.store.update_status(acc.account_id, "active", "Đang hoạt động", last_checked="09/10 11:00")
        loaded = self.store.get(acc.account_id)
        self.assertEqual(loaded.status, "active")
        self.assertEqual(loaded.last_checked, "09/10 11:00")
        self.assertTrue("🟢 Đang hoạt động" in loaded.status_display)

        # Toggle enabled
        self.store.set_enabled(acc.account_id, False)
        loaded = self.store.get(acc.account_id)
        self.assertFalse(loaded.enabled)

        # Delete
        self.assertTrue(self.store.delete(acc.account_id))
        self.assertEqual(len(self.store.all()), 0)

    def test_import_from_folder(self):
        folder = Path(self.temp_dir.name) / "cookie_folder"
        folder.mkdir()
        (folder / "user1@example.com.json").write_text(
            json.dumps([{"name": "sess1", "value": "val1"}]),
            encoding="utf-8",
        )
        (folder / "user2@example.com.txt").write_text(
            "sess2=val2; token=val3",
            encoding="utf-8",
        )

        added = self.store.import_from_folder(folder)
        self.assertEqual(len(added), 2)
        all_accounts = self.store.all()
        self.assertEqual(len(all_accounts), 2)
        emails = {a.email for a in all_accounts}
        self.assertEqual(emails, {"user1@example.com", "user2@example.com"})

    def test_inject_cookies_to_driver(self):
        mock_driver = MagicMock()
        cookies = [
            {"name": "session", "value": "val1", "domain": ".muse.ai", "path": "/"},
            {"name": "token", "value": "val2", "domain": ".muse.ai", "path": "/"},
        ]
        inject_cookies_to_driver(mock_driver, cookies, target_url="https://muse.ai/")
        self.assertTrue(mock_driver.get.called)
        self.assertTrue(mock_driver.delete_all_cookies.called)
        self.assertEqual(mock_driver.add_cookie.call_count, 2)
        self.assertTrue(mock_driver.refresh.called)


    def test_build_muse_chunk_prompt_format(self):
        prompts = [
            (1, "slow pan of a smartphone in Paris street"),
            (2, "static shot of a laptop in Berlin tech lab"),
            (3, "top-down view of a mechanical keyboard in Amsterdam"),
            (4, "macro close-up of a gaming mouse in Zurich"),
            (5, "cinematic dolly of a smartwatch in Stockholm"),
        ]
        result = build_muse_chunk_prompt(
            prompts,
            action_type="video",
            aspect_ratio="16:9",
            duration="11s",
        )
        self.assertIn("Tạo 5 video riêng biệt, mỗi dòng dưới đây là 1 video.", result)
        self.assertIn("Chỉ gửi video sau khi hoàn thành cả 5.", result)
        self.assertIn("Đặt tên file mỗi video bắt đầu bằng mã ở đầu dòng (ví dụ: Prompt0001-ten-canh.mp4).", result)
        self.assertIn("Prompt0001: slow pan of a smartphone in Paris street", result)
        self.assertIn("Prompt0002: static shot of a laptop in Berlin tech lab", result)
        self.assertIn("Prompt0003: top-down view of a mechanical keyboard in Amsterdam", result)
        self.assertIn("Prompt0004: macro close-up of a gaming mouse in Zurich", result)
        self.assertIn("Prompt0005: cinematic dolly of a smartwatch in Stockholm", result)
        self.assertIn("Áp dụng cho tất cả video: Tỉ lệ 16:9, dài 11s, phong cách: " + DEFAULT_MUSE_STYLE_SUFFIX, result)

    def test_build_muse_chunk_prompt_custom_style(self):
        prompts = [(10, "sunset ocean wave")]
        result = build_muse_chunk_prompt(
            prompts,
            action_type="image",
            aspect_ratio="9:16",
            style_suffix="Cyberpunk neon style",
        )
        self.assertIn("Tạo 1 ảnh riêng biệt, mỗi dòng dưới đây là 1 ảnh.", result)
        self.assertIn("Prompt0010: sunset ocean wave", result)
        self.assertIn("Áp dụng cho tất cả ảnh: Tỉ lệ 9:16, phong cách: Cyberpunk neon style", result)

    def test_extract_email_from_muse_page(self):
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = "myuser@gmail.com"
        email = extract_email_from_muse_page(mock_driver)
        self.assertEqual(email, "myuser@gmail.com")

    def test_extract_email_from_muse_page_fallback(self):
        mock_driver = MagicMock()
        mock_driver.execute_script.return_value = ""
        mock_driver.page_source = "<html><body>Welcome user_test99@gmail.com to Muse!</body></html>"
        email = extract_email_from_muse_page(mock_driver)
        self.assertEqual(email, "user_test99@gmail.com")


if __name__ == "__main__":
    unittest.main()
