"""
HubSpot CRM client (contacts, owners, meetings, tasks).
Extracted verbatim from app.py (Phase 2 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config import HUBSPOT_API_KEY, HUBSPOT_OWNER_ID, HUBSPOT_OWNER_MAP, normalize_team_email
from datetime_utils import to_hubspot_ms

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"

def hubspot_request(method: str, endpoint: str, data: dict = None, params: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{HUBSPOT_BASE}{endpoint}"
    resp = requests.request(method, url, json=data, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json() if resp.content else {}

def find_hubspot_contact(email: str) -> Optional[dict]:
    data = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["firstname", "lastname", "email", "company", "jobtitle", "hubspot_owner_id"],
    }
    result = hubspot_request("POST", "/crm/v3/objects/contacts/search", data)
    results = result.get("results", [])
    return results[0] if results else None

_hubspot_owner_cache = {}  # email   owner_id cache

def resolve_hubspot_owner(organizer_email: str) -> str:
    """Resolve organizer email to HubSpot owner ID.
    Priority: HUBSPOT_OWNER_MAP   HubSpot API lookup   HUBSPOT_OWNER_ID fallback."""
    if not organizer_email:
        return HUBSPOT_OWNER_ID
    # Normalize alias emails to canonical @negevlabs.com
    organizer_email = normalize_team_email(organizer_email)

    # Check static map first (fast, no API call)
    if organizer_email in HUBSPOT_OWNER_MAP:
        return HUBSPOT_OWNER_MAP[organizer_email]

    # Check cache
    if organizer_email in _hubspot_owner_cache:
        return _hubspot_owner_cache[organizer_email]

    # Try HubSpot owners API lookup by email
    try:
        result = hubspot_request("GET", "/crm/v3/owners", params={"email": organizer_email, "limit": 1})
        owners = result.get("results", [])
        if owners:
            owner_id = str(owners[0].get("id", ""))
            _hubspot_owner_cache[organizer_email] = owner_id
            logger.info(f"Resolved HubSpot owner: {organizer_email}   {owner_id}")
            return owner_id
    except Exception as e:
        logger.warning(f"HubSpot owner lookup failed for {organizer_email}: {e}")

    # Fallback to default
    _hubspot_owner_cache[organizer_email] = HUBSPOT_OWNER_ID
    return HUBSPOT_OWNER_ID

def create_hubspot_contact(contact_info: dict, organizer_email: str = "") -> dict:
    properties = {
        "firstname": contact_info.get("name", "").split()[0] if contact_info.get("name") else "",
        "lastname": " ".join(contact_info.get("name", "").split()[1:]) if contact_info.get("name") else "",
        "email": contact_info.get("email", ""),
        "company": contact_info.get("company", ""),
        "jobtitle": contact_info.get("role", ""),
    }
    owner_id = resolve_hubspot_owner(organizer_email)
    if owner_id:
        properties["hubspot_owner_id"] = owner_id
    properties = {k: v for k, v in properties.items() if v}
    result = hubspot_request("POST", "/crm/v3/objects/contacts", {"properties": properties})
    logger.info(f"Created HubSpot contact: {contact_info.get('name')} ({result.get('id')})   owner: {owner_id or 'none'}")
    return result

def get_contact_associations(contact_id: str) -> dict:
    """Look up a contact's associated companies and deals."""
    assoc = {"companies": [], "deals": []}
    for obj_type in ("companies", "deals"):
        try:
            result = hubspot_request("GET", f"/crm/v4/objects/contacts/{contact_id}/associations/{obj_type}")
            for item in (result.get("results") or []):
                to_id = item.get("toObjectId")
                if to_id:
                    assoc[obj_type].append(str(to_id))
        except Exception as e:
            logger.warning(f"Association lookup {obj_type} for contact {contact_id}: {e}")
    return assoc

def log_hubspot_meeting(contact_id: str, meeting_body: str, meeting_date: str,
                        title: str = "", transcript_id: str = "", duration_min: int = 30) -> dict:
    """Create a Meeting engagement in HubSpot, associated with Contact + Company + Deal."""
    # Build Fireflies link
    fireflies_url = f"https://app.fireflies.ai/view/{transcript_id}" if transcript_id else ""
    body_with_link = meeting_body
    if fireflies_url:
        body_with_link += f"\n\n---\nRecording: {fireflies_url}"

    # Calculate meeting times
    try:
        from dateutil import parser as dtparser
        start_dt = dtparser.parse(meeting_date)
    except Exception:
        start_dt = datetime.now(timezone.utc)
    end_dt = start_dt + timedelta(minutes=duration_min)

    properties = {
        "hs_timestamp": int(start_dt.timestamp() * 1000),
        "hs_meeting_title": title or "Meeting",
        "hs_meeting_body": body_with_link,
        "hs_meeting_start_time": int(start_dt.timestamp() * 1000),
        "hs_meeting_end_time": int(end_dt.timestamp() * 1000),
        "hs_meeting_outcome": "COMPLETED",
    }

    # Build associations: contact + company + deal
    # Association type IDs: meeting->contact=200, meeting->company=188, meeting->deal=206
    associations = [
        {"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 200}]}
    ]

    # Look up related companies and deals
    related = get_contact_associations(contact_id)
    for company_id in related["companies"]:
        associations.append(
            {"to": {"id": company_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 188}]}
        )
    for deal_id in related["deals"]:
        associations.append(
            {"to": {"id": deal_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 206}]}
        )

    logger.info(f"Creating HubSpot meeting: contact={contact_id}, companies={related['companies']}, deals={related['deals']}")
    data = {"properties": properties, "associations": associations}
    return hubspot_request("POST", "/crm/v3/objects/meetings", data)

def create_hubspot_task(contact_id: str, subject: str, body: str, due_date: str, organizer_email: str = "") -> dict:
    ms = to_hubspot_ms(due_date) or str(int(datetime.now(timezone.utc).timestamp() * 1000))
    logger.info(f"[hubspot_task] due_date input='{due_date}' converted='{ms}'")
    data = {
        "properties": {
            "hs_task_subject": subject, "hs_task_body": body,
            "hs_task_status": "NOT_STARTED", "hs_task_priority": "HIGH",
            "hs_timestamp": ms,
        },
        "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}],
    }
    owner_id = resolve_hubspot_owner(organizer_email)
    if owner_id:
        data["properties"]["hubspot_owner_id"] = owner_id
    return hubspot_request("POST", "/crm/v3/objects/tasks", data)
