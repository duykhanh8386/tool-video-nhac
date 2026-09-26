from __future__ import annotations

import json
import urllib.request


def queue_workflow(base_url: str, workflow_path: str, client_id: str = "visual-loop-studio") -> dict:
    with open(workflow_path, "r", encoding="utf-8") as stream:
        workflow = json.load(stream)
    body = json.dumps({"prompt": workflow, "client_id": client_id}).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/prompt", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)
