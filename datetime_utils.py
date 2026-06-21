"""
Date/time conversion helpers (HubSpot ms, Graph dateTime, due-date math).
Extracted verbatim from app.py (Phase 2 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.
"""

from datetime import datetime, timedelta, timezone

def to_hubspot_ms(date_str):
    """Convert date or ISO datetime string to HubSpot Unix ms (as string).
    Handles: '2026-04-28', '2026-04-28T17:00:00Z',
             '2026-04-28T17:00:00+00:00', None, empty strings.
    Returns None if input is invalid or empty.
    HubSpot requires timestamps as Unix ms strings (e.g. '1772631000000').
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    # Normalize trailing Z to +00:00 for fromisoformat compatibility
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1000))

def to_graph_datetime(date_str):
    """Convert date or ISO datetime string to Microsoft Graph dateTime
    format (ISO 8601 with seconds, no timezone suffix -- Graph pairs it
    with a separate timeZone field).

    Handles: '2026-04-28', '2026-04-28T17:00:00Z',
             '2026-04-28T17:00:00+00:00', None, empty strings.
    Returns None if input is invalid or empty.

    Output format: 'YYYY-MM-DDTHH:MM:SS' (no Z, no offset).
    Date-only inputs default to T00:00:00.
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
    # Graph wants naive ISO format paired with a timeZone field
    return dt.strftime("%Y-%m-%dT%H:%M:%S")

def resolve_due_date(item: dict, meeting_date_str: str) -> tuple:
    """Convert due_days/due_context into actual dates for HubSpot and Asana.
    Returns (hubspot_due: str ISO datetime, asana_due: str YYYY-MM-DD, display: str)"""
    try:
        meeting_dt = datetime.fromisoformat(meeting_date_str.replace("Z", "+00:00"))
    except Exception:
        meeting_dt = datetime.now(timezone.utc)

    # Get due_days from Claude extraction, fallback to parsing due_context
    due_days = item.get("due_days")
    if due_days is None or not isinstance(due_days, (int, float)):
        # Fallback: parse due_context string
        ctx = (item.get("due_context") or "").lower()
        if any(w in ctx for w in ["asap", "urgent", "today", "immediate"]):
            due_days = 1
        elif "tomorrow" in ctx:
            due_days = 1
        elif any(w in ctx for w in ["this week", "few days", "couple days"]):
            due_days = 3
        elif "next week" in ctx:
            due_days = 7
        elif any(w in ctx for w in ["two week", "couple week", "2 week"]):
            due_days = 14
        elif any(w in ctx for w in ["end of month", "month end"]):
            due_days = 21
        elif "next month" in ctx:
            due_days = 30
        else:
            due_days = 7  # default

    due_days = max(1, int(due_days))
    due_dt = meeting_dt + timedelta(days=due_days)
    hubspot_due = due_dt.strftime("%Y-%m-%dT17:00:00Z")
    asana_due = due_dt.strftime("%Y-%m-%d")
    display = due_dt.strftime("%b %d, %Y")
    return hubspot_due, asana_due, display
