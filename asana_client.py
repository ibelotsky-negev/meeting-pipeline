"""
Asana API client (tasks, user lookup).
Extracted verbatim from app.py (Phase 2 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.
"""

from typing import Optional

import requests

from config import ASANA_API_KEY, ASANA_WORKSPACE_GID, ASANA_PROJECT_GID

ASANA_BASE = "https://app.asana.com/api/1.0"

def asana_request(method: str, endpoint: str, data: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {ASANA_API_KEY}", "Content-Type": "application/json"}
    url = f"{ASANA_BASE}{endpoint}"
    resp = requests.request(method, url, json={"data": data} if data else None, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", {})

def create_asana_task(name: str, notes: str, due_on: str = None, assignee_gid: str = None) -> dict:
    task_data = {"name": name, "notes": notes, "workspace": ASANA_WORKSPACE_GID}
    if due_on:
        task_data["due_on"] = due_on
    if assignee_gid:
        task_data["assignee"] = assignee_gid
    if ASANA_PROJECT_GID:
        task_data["projects"] = [ASANA_PROJECT_GID]
    return asana_request("POST", "/tasks", task_data)

def find_asana_user_by_email(email: str) -> Optional[str]:
    """Look up Asana user GID by email. Returns GID or None."""
    if not email or "placeholder" in email or "unknown" in email:
        return None
    try:
        result = asana_request("GET", f"/users/{email}")
        return result.get("gid")
    except Exception:
        return None
