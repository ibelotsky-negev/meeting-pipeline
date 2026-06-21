"""
JSON persistence for pending approvals, processed transcripts, sync map.
Extracted verbatim from app.py (Phase 2 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.
"""

import json

from config import PENDING_FILE, PROCESSED_FILE, SYNC_MAP_FILE

def load_pending() -> dict:
    try:
        with open(PENDING_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_pending(pending: dict):
    with open(PENDING_FILE, "w") as f:
        json.dump(pending, f, indent=2, default=str)

def load_processed() -> set:
    try:
        with open(PROCESSED_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed), f)

def load_sync_map() -> dict:
    """Load Asana<->To-Do mapping from /data/asana_todo_map.json."""
    try:
        with open(SYNC_MAP_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"user_lists": {}, "mappings": {}, "asana_webhook_id": None}

def save_sync_map(sync_map: dict):
    """Persist mapping to /data/asana_todo_map.json."""
    with open(SYNC_MAP_FILE, "w") as f:
        json.dump(sync_map, f, indent=2, default=str)
