from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from models.settings_model import AppSettings
from utils import config


class SecretConfigTests(unittest.TestCase):
    def test_save_settings_never_writes_legacy_gemini_key(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = AppSettings(gemini_api_key="must-not-be-written")
            with patch.object(config, "SETTINGS_FILE", path):
                config.save_settings(settings)
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("gemini_api_key", payload)
        self.assertNotIn("must-not-be-written", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
