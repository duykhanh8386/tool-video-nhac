from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from auth.license_manager import LicenseManager, get_hardware_fingerprint


class LicenseManagerTests(unittest.TestCase):
    def test_hardware_fingerprint(self):
        hwid = get_hardware_fingerprint()
        self.assertTrue(hwid.startswith("HWID-"))
        self.assertGreater(len(hwid), 10)

    def test_account_id_storage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_cfg = Path(td) / "license_config.json"
            with patch("auth.license_manager.KEYGEN_CONFIG_FILE", tmp_cfg):
                mgr = LicenseManager()
                self.assertEqual(mgr.get_account_id(), "")
                mgr.set_account_id("my-test-account-id-1234")
                self.assertEqual(mgr.get_account_id(), "my-test-account-id-1234")
                self.assertTrue(tmp_cfg.exists())

    def test_check_license_empty(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_lic = Path(td) / "license.json"
            tmp_cfg = Path(td) / "license_config.json"
            with patch("auth.license_manager.LICENSE_FILE", tmp_lic), \
                 patch("auth.license_manager.KEYGEN_CONFIG_FILE", tmp_cfg):
                mgr = LicenseManager()
                valid, msg = mgr.check_license()
                self.assertFalse(valid)
                self.assertIn("Chưa cấu hình", msg)

    def test_validate_online_valid_response(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_lic = Path(td) / "license.json"
            tmp_cfg = Path(td) / "license_config.json"
            with patch("auth.license_manager.LICENSE_FILE", tmp_lic), \
                 patch("auth.license_manager.KEYGEN_CONFIG_FILE", tmp_cfg):
                mgr = LicenseManager()
                mgr.set_account_id("acc-uuid-1234")

                mock_response = MagicMock()
                mock_response.read.return_value = json.dumps({
                    "meta": {
                        "constant": "VALID",
                        "valid": True,
                        "code": "VALID",
                        "detail": "is valid"
                    },
                    "data": {
                        "id": "lic-1234",
                        "type": "licenses",
                        "attributes": {
                            "name": "Khách hàng A",
                            "expiry": "2030-01-01T00:00:00.000Z",
                            "status": "ACTIVE"
                        }
                    }
                }).encode("utf-8")
                mock_response.__enter__.return_value = mock_response

                with patch("urllib.request.urlopen", return_value=mock_response):
                    ok, msg, data = mgr.validate_online("TEST-KEY-1111-2222")
                    self.assertTrue(ok)
                    self.assertIn("thành công", msg)
                    self.assertTrue(mgr.is_cached_valid())
                    self.assertTrue(tmp_lic.exists())


if __name__ == "__main__":
    unittest.main()
