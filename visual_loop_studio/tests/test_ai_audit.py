from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from models.ai_audit import AiAuditLog


class AiAuditTests(unittest.TestCase):
    def test_sensitive_values_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audit.jsonl"
            audit = AiAuditLog(path)
            audit.record(
                "settings.changed",
                target_type="settings",
                target_id="ai",
                details={
                    "api_key": "secret-value",
                    "nested": {"authorization": "Bearer secret", "budget": 12.5},
                },
            )
            event = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(event["details"]["api_key"], "***")
        self.assertEqual(event["details"]["nested"]["authorization"], "***")
        self.assertEqual(event["details"]["nested"]["budget"], 12.5)
        self.assertNotIn("secret-value", json.dumps(event))


if __name__ == "__main__":
    unittest.main()
