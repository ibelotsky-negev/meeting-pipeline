"""
Fireflies GraphQL client (recent + by-id transcript fetch).
Extracted verbatim from app.py (Phase 2 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.
"""

from datetime import datetime, timedelta, timezone

import requests

from config import FIREFLIES_API_KEY

FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"

def fireflies_query(query: str, variables: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {FIREFLIES_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(FIREFLIES_GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise Exception(f"Fireflies API error: {data['errors']}")
    return data["data"]

def get_recent_transcripts(since_minutes: int = 30) -> list:
    query = """
    query { transcripts {
        id title dateString: date duration organizer_email participants
        summary { short_summary action_items keywords overview }
        sentences { speaker_name text }
    }}
    """
    data = fireflies_query(query)
    transcripts = data.get("transcripts", [])
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    recent = []
    for t in transcripts:
        try:
            ds = t.get("dateString", "")
            if isinstance(ds, (int, float)):
                t_date = datetime.fromtimestamp(ds / 1000 if ds > 1e12 else ds, tz=timezone.utc)
            else:
                t_date = datetime.fromisoformat(str(ds).replace("Z", "+00:00"))
            if t_date >= cutoff:
                recent.append(t)
        except (ValueError, TypeError, OSError):
            continue
    return recent

def get_transcript_by_id(transcript_id: str) -> dict:
    query = """
    query GetTranscript($id: String!) { transcript(id: $id) {
        id title dateString: date duration organizer_email participants
        summary { shorthand_bullet short_summary action_items keywords overview notes }
        sentences { speaker_name text }
    }}
    """
    data = fireflies_query(query, {"id": transcript_id})
    return data.get("transcript")
