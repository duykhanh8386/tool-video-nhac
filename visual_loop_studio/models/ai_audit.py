from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.paths import DATA_DIR


SENSITIVE_MARKERS = ("key", "token", "secret", "password", "cookie", "session", "authorization")


def _redact(value: Any, key: str = "") -> Any:
    if any(marker in key.casefold() for marker in SENSITIVE_MARKERS):
        return "***"
    if isinstance(value, dict):
        return {str(item_key): _redact(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class AiAuditLog:
    def __init__(self, path: str | Path = DATA_DIR / "ai_audit.jsonl") -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def record(
        self,
        action: str,
        *,
        target_type: str,
        target_id: str,
        details: dict[str, Any] | None = None,
        actor: str = "local-user",
    ) -> None:
        event = {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": actor,
            "action": str(action),
            "target_type": str(target_type),
            "target_id": str(target_id),
            "details": _redact(details or {}),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
