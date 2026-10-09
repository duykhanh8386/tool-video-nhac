from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from auth.muse_cookies import (
    MuseCookieAccount,
    MuseCookieAccountStore,
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


if __name__ == "__main__":
    unittest.main()
