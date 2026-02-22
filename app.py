"""
Post-Meeting Intelligence Pipeline v2
# Fireflies -> Claude AI -> Approval UI -> HubSpot/Asana/Outlook

Flow:
1. Fireflies triggers (webhook or poll) when transcript is ready
2. Claude extracts intelligence (signals, action items, contacts, email draft)
3. Organizer receives approval link (email or Slack)
# 4. Organizer reviews tasks & email draft in a web UI -- can edit, delete, or approve
5. On approval: HubSpot updated, Asana tasks created, Outlook draft saved

Author: Negev Labs
"""

import os
import json
import uuid
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import anthropic
import requests
from flask import Flask, request, jsonify, render_template_string, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler

# ======================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# API Keys (set via environment variables)
FIREFLIES_API_KEY = os.environ["FIREFLIES_API_KEY"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]
ASANA_API_KEY = os.environ["ASANA_API_KEY"]

# Microsoft Graph (Outlook) -> OAuth2 client credentials or delegated
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
MS_GRAPH_CLIENT_SECRET = os.environ.get("MS_GRAPH_CLIENT_SECRET", "")
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "")
MS_GRAPH_REFRESH_TOKEN = os.environ.get("MS_GRAPH_REFRESH_TOKEN", "")  # For delegated flow
# Load refresh token from persistent storage if env var not set or to get latest rotated token
try:
    if os.path.exists("/data/refresh_token.txt"):
        with open("/data/refresh_token.txt") as f: _file_token = f.read().strip()
        if _file_token: MS_GRAPH_REFRESH_TOKEN = _file_token; import sys; print("[startup] Loaded refresh token from /data/refresh_token.txt", file=sys.stderr)
except Exception as _e:
    import sys; print(f"[startup] Could not load refresh token from file: {_e}", file=sys.stderr)
# Auth mode: "delegated" (uses refresh_token, /me/ endpoints -> single user only)
# "app" (uses client_credentials, /users/{email} endpoints -> team-wide)
# Auto-detected: if refresh_token set -- delegated, else -- app
MS_GRAPH_AUTH_MODE = os.environ.get("MS_GRAPH_AUTH_MODE", "auto")

# Configuration
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "")
HUBSPOT_OWNER_ID = os.environ.get("HUBSPOT_OWNER_ID", "")  # Fallback default owner
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8080")  # Your deployed URL
NOTIFY_VIA = os.environ.get("NOTIFY_VIA", "email")  # "email" (per-organizer), "teams" (shared ops channel), or "email,teams" for both
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")  # Optional: shared ops channel for admin visibility
BOT_SENDER_EMAIL = os.environ.get("BOT_SENDER_EMAIL", "")  # e.g. sara@negevlabs.com (shared mailbox)
BOT_SENDER_NAME = os.environ.get("BOT_SENDER_NAME", "Sara - Negev Chief of Staff")
# HubSpot owner map: maps organizer email -- HubSpot owner ID
# Supports two formats:
#   JSON:   {"bk@negevlabs.com":"241153249","shlomi@negevlabs.com":"241153250"}
#   Simple: bk@negevlabs.com:241153249,shlomi@negevlabs.com:241153250,dan@negevlabs.com:31299775
HUBSPOT_OWNER_MAP_RAW = os.environ.get("HUBSPOT_OWNER_MAP", "")
HUBSPOT_OWNER_MAP = {}
if HUBSPOT_OWNER_MAP_RAW:
    try:
        HUBSPOT_OWNER_MAP = json.loads(HUBSPOT_OWNER_MAP_RAW)
    except (json.JSONDecodeError, TypeError):
        # Parse simple format: email:id,email:id
        for pair in HUBSPOT_OWNER_MAP_RAW.split(","):
            pair = pair.strip()
            if ":" in pair:
                email, owner_id = pair.rsplit(":", 1)
                HUBSPOT_OWNER_MAP[email.strip()] = owner_id.strip()
    logger.info(f"Parsed HUBSPOT_OWNER_MAP: {HUBSPOT_OWNER_MAP}")

# Track processed transcripts and pending approvals
# Railway volume mount: attach a volume at /data for persistence across deploys
DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
PROCESSED_FILE = os.path.join(DATA_DIR, "processed_transcripts.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending_approvals.json")
SYNC_MAP_FILE = os.path.join(DATA_DIR, "asana_todo_map.json")

# To-Do sync config
TODO_LIST_NAME = os.environ.get("TODO_LIST_NAME", "Asana Tasks")
# TODO_SYNC_USER_EMAIL removed - app-only mode syncs all @negevlabs.com users automatically
TODO_POLL_INTERVAL = int(os.environ.get("TODO_POLL_INTERVAL", "300"))
RAILWAY_PUBLIC_URL = os.environ.get("RAILWAY_PUBLIC_URL", "https://meeting-pipeline-production.up.railway.app")


def strip_emojis(text: str) -> str:
    """Remove emoji characters from text to prevent encoding issues in emails."""
    import re as _re
    # Remove emoji Unicode ranges
    emoji_pattern = _re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002702-\U000027B0"  # dingbats
        "\U000024C2-\U0001F251"  # misc
        "\U0001f926-\U0001f937"  # supplemental
        "\U00010000-\U0010ffff"  # supplemental
        "\u200d"                   # zero width joiner
        "\u2640-\u2642"          # gender symbols
        "\ufe0f"                   # variation selector
        "\u2600-\u26FF"          # misc symbols
        "\u2700-\u27BF"          # dingbats
        "]+", flags=_re.UNICODE)
    return emoji_pattern.sub("", text)

app = Flask(__name__)

# Startup config summary
_app_only = MS_GRAPH_AUTH_MODE == "app" or (MS_GRAPH_AUTH_MODE == "auto" and not MS_GRAPH_REFRESH_TOKEN)
logger.info(f"Graph auth mode: {'app-only (team-wide)' if _app_only else 'delegated (single-user)'}")
logger.info(f"HubSpot owner map: {len(HUBSPOT_OWNER_MAP)} entries | fallback: {HUBSPOT_OWNER_ID or 'none'}")


# ======================================================================
#  PENDING APPROVALS STORE
# ======================================================================

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


# ======================================================================
#  FIREFLIES API
# ======================================================================

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


# ======================================================================
# CLAUDE AI -> INTELLIGENCE EXTRACTION
# ======================================================================

def extract_meeting_intelligence(transcript: dict) -> dict:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    summary = transcript.get("summary") or {}
    sentences = transcript.get("sentences") or []
    transcript_text = "\n".join(
        [f"{s.get('speaker_name', 'Unknown')}: {s.get('text', '')}" for s in sentences]
    )

 # Business context -> loaded from file if available, else env var, else default
    business_context = ""
    context_file = os.environ.get("BUSINESS_CONTEXT_FILE", "business_context.md")
    if os.path.exists(context_file):
        with open(context_file, "r") as f:
            business_context = f.read()
        logger.info(f"Loaded business context from {context_file} ({len(business_context)} chars)")
    elif os.environ.get("BUSINESS_CONTEXT"):
        business_context = os.environ["BUSINESS_CONTEXT"]
    else:
        business_context = "No specific business context provided. Extract general meeting intelligence."

    prompt = f"""You are an expert biotech venture capital analyst and chief of staff.

BUSINESS CONTEXT:
{business_context}

Analyze this meeting transcript and extract structured intelligence.

MEETING INFO:
- Title: {transcript.get('title', 'Unknown')}
- Date: {transcript.get('dateString', 'Unknown')}
- Duration: {transcript.get('duration', 'Unknown')} minutes
- Participants: {', '.join(transcript.get('participants') or [])}
- Organizer: {transcript.get('organizer_email', 'Unknown')}

SUMMARY: {summary.get('short_summary', 'No summary available')}
ACTION ITEMS (from Fireflies): {summary.get('action_items', 'None extracted')}
KEY TOPICS: {summary.get('overview', '')}

FULL TRANSCRIPT:
{transcript_text}

---

Return a JSON object with exactly this structure:
{{
    "contacts": [
        {{
            "name": "Full Name",
            "email": "email@domain.com",
            "company": "Company Name",
            "role": "Their role/title if mentioned",
            "is_internal": false
        }}
    ],
    "signals": {{
        "interest_level": "high/medium/low",
        "relationship_type": "investor/advisor/partner/customer/other",
        "key_signals": ["signal 1", "signal 2"],
        "objections_or_concerns": ["concern 1"],
        "topics_discussed": ["topic 1", "topic 2"],
        "next_steps_discussed": ["step 1", "step 2"]
    }},
    "action_items": [
        {{
            "owner": "Person Name",
            "owner_email": "person@email.com or empty string if unknown",
            "task": "Task description",
            "priority": "high/medium/low",
            "due_context": "ASAP / tomorrow / this week / next week / end of month / specific date if mentioned",
            "due_days": 7,
            "create_in": "hubspot or asana"
        }}
    ],
    "hubspot_note": "Comprehensive meeting note for HubSpot CRM",
    "follow_up_email": {{
        "to_recipients": [
            {{"name": "Recipient Name", "email": "recipient@email.com"}}
        ],
        "from_email": "{transcript.get('organizer_email', '')}",
        "from_name": "Organizer Name",
        "subject": "Email subject line",
        "body_html": "<p>Full email body in HTML format, warm and professional</p>",
        "body_text": "Plain text version of the email"
    }}
}}

RULES:
- Use the BUSINESS CONTEXT above to understand what matters in this meeting and extract high-value action items
- Distinguish internal team members (@negevlabs.com, @ariadnebio.com, etc.) from external contacts
- For investor/BD meetings: capture interest signals, objections, and next steps that matter for deal flow
- For portfolio company meetings: capture strategic decisions, blockers, and deliverables
- Action items should be specific, actionable, and reflect what was actually committed to in the conversation   not generic tasks
- The follow-up email is FROM the organizer TO the other meeting participants (NOT to the organizer themselves)
- to_recipients must NEVER include the organizer ({transcript.get('organizer_email', '')}). The email is sent BY the organizer, not TO them.
- to_recipients should include the key external participants identified from the transcript speakers and discussion
- If participant emails are not known, use "unknown@placeholder.com" and include their name so the organizer can fix it in the review UI
- The email greeting should address the recipient(s) by name (e.g., "Hi Sam"), NOT the organizer
- from_email must be the meeting organizer's email
- body_html should use simple HTML (<p>, <br>, <strong>) for Outlook rendering
- Identify ALL external contacts (non-organizer attendees) from speaker names in the transcript
- Rate interest level based on language, engagement, and commitments made
- Action items should be specific and assignable
- For each action item, set owner_email to the person's email if known from participants or organizer info. If the owner is the organizer, use their email. If unknown, leave as empty string.
- For due_days: convert relative time references from the conversation into integer days from the meeting date. Use: "ASAP"/"urgent"/"today"   1, "tomorrow"   1, "this week"/"few days"   3, "next week"   7, "couple weeks"   14, "end of month"   21, "next month"   30. If a specific date is mentioned, calculate the days difference from the meeting date. Default to 7 if unclear.
- For create_in: Route each task to the RIGHT system. Use "hubspot" for external/investor-facing tasks (follow-up emails, calls, scheduling meetings, sending materials to external contacts). Use "asana" for internal operational tasks (preparing documents, data rooms, reports, internal reviews, research). Most tasks should go to ONE system, not both.
- Return ONLY valid JSON, no markdown
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0]
    return json.loads(response_text)


# ======================================================================
# MICROSOFT GRAPH -> OUTLOOK DRAFT CREATION
# ======================================================================

MS_GRAPH_TOKEN_URL = f"https://login.microsoftonline.com/{MS_GRAPH_TENANT_ID}/oauth2/v2.0/token"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_ms_token_cache = {"token": None, "expires_at": 0}
_ms_delegated_token_cache = {"token": None, "expires_at": 0}


def is_app_only_mode() -> bool:
    """Determine if we're using app-only (team-wide) or delegated (single-user) auth."""
    if MS_GRAPH_AUTH_MODE == "app":
        return True
    if MS_GRAPH_AUTH_MODE == "delegated":
        return False
    # Auto-detect: delegated if refresh token exists, else app-only
    return not bool(MS_GRAPH_REFRESH_TOKEN)


def get_ms_graph_token() -> str:
    """Get Microsoft Graph access token (supports both delegated and app-only)."""
    now = time.time()
    if _ms_token_cache["token"] and _ms_token_cache["expires_at"] > now + 60:
        return _ms_token_cache["token"]

    if not is_app_only_mode():
 # Delegated flow (send as specific user -> single user only)
        data = {
            "client_id": MS_GRAPH_CLIENT_ID,
            "client_secret": MS_GRAPH_CLIENT_SECRET,
            "refresh_token": MS_GRAPH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
            "scope": "https://graph.microsoft.com/Mail.ReadWrite",
        }
        logger.info("Using delegated auth (single-user mode)")
    else:
 # App-only flow (team-wide -> requires Mail.ReadWrite application permission)
        data = {
            "client_id": MS_GRAPH_CLIENT_ID,
            "client_secret": MS_GRAPH_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }
        logger.info("Using app-only auth (team-wide mode)")

    resp = requests.post(MS_GRAPH_TOKEN_URL, data=data, timeout=15)
    resp.raise_for_status()
    token_data = resp.json()

    _ms_token_cache["token"] = token_data["access_token"]
    _ms_token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _ms_token_cache["token"]


def get_delegated_graph_token() -> str:
    global MS_GRAPH_REFRESH_TOKEN
    """Get delegated token via refresh token (for Mail.ReadWrite delegated flow)."""
    now = time.time()
    if _ms_delegated_token_cache["token"] and _ms_delegated_token_cache["expires_at"] > now + 60:
        return _ms_delegated_token_cache["token"]
    # Disk re-read removed -- startup handles env-vs-disk priority
    if not MS_GRAPH_REFRESH_TOKEN:
        raise RuntimeError("MS_GRAPH_REFRESH_TOKEN not set -- required for To-Do API (delegated auth)")
    data = {
        "client_id": MS_GRAPH_CLIENT_ID,
        "client_secret": MS_GRAPH_CLIENT_SECRET,
        "refresh_token": MS_GRAPH_REFRESH_TOKEN,
        "grant_type": "refresh_token",
        "scope": "https://graph.microsoft.com/Tasks.ReadWrite Mail.ReadWrite",
    }
    resp = requests.post(MS_GRAPH_TOKEN_URL, data=data, timeout=30)
    if resp.status_code != 200:
        logger.error(f"[todo-sync] Token request FAILED {resp.status_code}: {resp.text}")
        logger.error(f"[todo-sync] client_id present: {bool(data.get('client_id'))}, client_secret present: {bool(data.get('client_secret'))}, refresh_token length: {len(data.get('refresh_token',''))}")
        resp.raise_for_status()
    token_data = resp.json()
    _ms_delegated_token_cache["token"] = token_data["access_token"]
    _ms_delegated_token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    if token_data.get("refresh_token"):
        MS_GRAPH_REFRESH_TOKEN = token_data["refresh_token"]
        logger.info("[todo-sync] Refresh token rotated, updated in memory")
        try:
            os.makedirs("/data", exist_ok=True)
            with open("/data/refresh_token.txt", "w") as f: f.write(MS_GRAPH_REFRESH_TOKEN)
            logger.info("[todo-sync] Refresh token persisted to /data/refresh_token.txt")
        except Exception as e:
            logger.warning(f"[todo-sync] Could not persist refresh token: {e}")
    return _ms_delegated_token_cache["token"]


def create_outlook_draft(
    sender_email: str,
    to_recipients: list,
    subject: str,
    body_html: str,
) -> dict:
    """
    Create a draft email in the sender's Outlook mailbox.

    Args:
        sender_email: The organizer's email (draft appears in their Drafts folder)
        to_recipients: List of {"name": "...", "email": "..."} dicts
        subject: Email subject
        body_html: HTML body content
    """
    token = get_ms_graph_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Build recipient list for Graph API
    to_list = [
        {
            "emailAddress": {
                "name": r.get("name", r.get("email", "")),
                "address": r.get("email", ""),
            }
        }
        for r in to_recipients
        if r.get("email")
    ]

    message_payload = {
        "subject": subject,
        "body": {
            "contentType": "HTML",
            "content": body_html,
        },
        "toRecipients": to_list,
        "importance": "normal",
    }

    # Create draft in sender's mailbox
 # App-only: /users/{email}/messages (team-wide -> any user's mailbox)
 # Delegated: /me/messages (single-user -> only authenticated user)
    if is_app_only_mode():
        url = f"{MS_GRAPH_BASE}/users/{sender_email}/messages"
    else:
        url = f"{MS_GRAPH_BASE}/me/messages"

    resp = requests.post(url, json=message_payload, headers=headers, timeout=30)
    resp.raise_for_status()
    draft = resp.json()

    logger.info(f"Created Outlook draft: '{subject}' in {sender_email}'s Drafts (ID: {draft.get('id', 'unknown')})")
    return draft


# ======================================================================
#  HUBSPOT API
# ======================================================================

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


# ======================================================================

_hubspot_owner_cache = {}  # email   owner_id cache


def resolve_hubspot_owner(organizer_email: str) -> str:
    """Resolve organizer email to HubSpot owner ID.
    Priority: HUBSPOT_OWNER_MAP   HubSpot API lookup   HUBSPOT_OWNER_ID fallback."""
    if not organizer_email:
        return HUBSPOT_OWNER_ID

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
        "hs_timestamp": start_dt.isoformat(),
        "hs_meeting_title": title or "Meeting",
        "hs_meeting_body": body_with_link,
        "hs_meeting_start_time": start_dt.isoformat(),
        "hs_meeting_end_time": end_dt.isoformat(),
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
    data = {
        "properties": {
            "hs_task_subject": subject, "hs_task_body": body,
            "hs_task_status": "NOT_STARTED", "hs_task_priority": "HIGH",
            "hs_timestamp": due_date,
        },
        "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}],
    }
    owner_id = resolve_hubspot_owner(organizer_email)
    if owner_id:
        data["properties"]["hubspot_owner_id"] = owner_id
    return hubspot_request("POST", "/crm/v3/objects/tasks", data)


# ======================================================================
#  ASANA API
# ======================================================================

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


# ======================================================================
#  ASANA <-> MICROSOFT TO-DO BIDIRECTIONAL SYNC
# ======================================================================

import threading as _threading

_todo_poller_running = False
_todo_last_poll_time = None


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


def _graph_request_with_retry(method: str, url: str, json_body: dict = None, headers: dict = None, force_delegated: bool = False) -> dict:
    """Microsoft Graph request with 3x retry on 429/5xx (5s/10s/15s backoff).
    force_delegated=True: use refresh token instead of client_credentials.
    """
    token = get_delegated_graph_token() if force_delegated else get_ms_graph_token()
    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    last_exc = None
    for attempt, wait in enumerate([0, 5, 10, 15]):
        if wait:
            time.sleep(wait)
        try:
            resp = requests.request(method, url, json=json_body, headers=hdrs, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                logger.warning(f"[todo-sync] Graph {method} {url} â†’ {resp.status_code}, retrying (attempt {attempt+1}/3)")
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except Exception as e:
            last_exc = e
            logger.warning(f"[todo-sync] Graph request attempt {attempt+1} failed: {e}")
    raise last_exc


def get_or_create_todo_list(user_email: str, list_name: str = None) -> str:
    """Find or create a Microsoft To-Do list for a specific user. App-only auth."""
    list_name = list_name or TODO_LIST_NAME
    url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists"
    data = _graph_request_with_retry("GET", url)  # app-only auth
    for lst in (data.get("value") or []):
        if lst.get("displayName", "").lower() == list_name.lower():
            logger.info(f"[todo-sync] Found To-Do list '{list_name}' for {user_email}: {lst['id']}")
            return lst["id"]
    # Create new list
    created = _graph_request_with_retry("POST", url, json_body={"displayName": list_name})
    list_id = created["id"]
    logger.info(f"[todo-sync] Created To-Do list '{list_name}' for {user_email}: {list_id}")
    return list_id


def create_todo_task(title: str, user_email: str, notes: str = None, due_date: str = None,
                     asana_gid: str = None, asana_project_gid: str = None) -> str:
    """Create a To-Do task for a specific user (app-only auth). Returns todo_task_id."""
    sync_map = load_sync_map()
    # Get or create per-user list
    user_lists = sync_map.get("user_lists") or {}
    list_id = user_lists.get(user_email)
    if not list_id:
        list_id = get_or_create_todo_list(user_email)
        sync_map.setdefault("user_lists", {})[user_email] = list_id
        save_sync_map(sync_map)

    body: dict = {"title": title}
    # Build body with Asana link + notes
    body_parts = []
    if asana_gid:
        _proj = asana_project_gid or ASANA_PROJECT_GID
        _asana_url = f"https://app.asana.com/0/{_proj}/{asana_gid}" if _proj else f"https://app.asana.com/0/0/{asana_gid}"
        body_parts.append(f"Asana: {_asana_url}")
    if notes:
        body_parts.append(notes)
    if body_parts:
        body["body"] = {"content": "\n\n".join(body_parts), "contentType": "text"}
    if due_date:
        body["dueDateTime"] = {"dateTime": f"{due_date}T00:00:00", "timeZone": "UTC"}
    body["status"] = "notStarted"

    url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks"
    logger.info(f"[todo-sync] Creating To-Do task for {user_email}, Asana {asana_gid}: '{title}'")
    task = _graph_request_with_retry("POST", url, json_body=body)
    todo_task_id = task["id"]

    # Add linked resource back to Asana
    if asana_gid:
        project_gid = asana_project_gid or ASANA_PROJECT_GID
        linked_url = f"https://app.asana.com/0/{project_gid}/{asana_gid}" if project_gid else f"https://app.asana.com/0/0/{asana_gid}"
        try:
            _graph_request_with_retry("POST",
                f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks/{todo_task_id}/linkedResources",
                json_body={"webUrl": linked_url, "applicationName": "Asana", "displayName": "View in Asana"})
        except Exception as e:
            logger.warning(f"[todo-sync] Could not add linked resource: {e}")

        # Store mapping
        sync_map = load_sync_map()
        sync_map.setdefault("user_lists", {})[user_email] = list_id
        sync_map["mappings"][asana_gid] = {
            "todo_task_id": todo_task_id,
            "user_email": user_email,
            "todo_list_id": list_id,
            "last_synced": datetime.now(timezone.utc).isoformat(),
            "completed_by": None,
        }
        save_sync_map(sync_map)

    logger.info(f"[todo-sync] Created To-Do task {todo_task_id} for {user_email}, Asana {asana_gid}")
    return todo_task_id



def update_todo_task(todo_task_id: str, asana_gid: str = None, title: str = None, notes: str = None, due_date: str = None):
    """Update an existing To-Do task using per-user app-only auth."""
    sync_map = load_sync_map()
    mapping = sync_map.get("mappings", {}).get(asana_gid or "", {})
    user_email = mapping.get("user_email", "")
    list_id = mapping.get("todo_list_id", "")
    if not user_email or not list_id:
        logger.warning(f"[todo-sync] update_todo_task: no user_email/list_id for {asana_gid}")
        return
    body = {}
    if title:
        body["title"] = title
    if notes:
        body["body"] = {"content": notes, "contentType": "text"}
    if due_date:
        body["dueDateTime"] = {"dateTime": f"{due_date}T00:00:00", "timeZone": "UTC"}
    if not body:
        return
    url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks/{todo_task_id}"
    logger.info(f"[todo-sync] Updating To-Do task {todo_task_id} for {user_email}")
    _graph_request_with_retry("PATCH", url, json_body=body)



def complete_todo_task(todo_task_id: str, asana_gid: str = None):
    """Mark To-Do task complete using per-user app-only auth."""
    sync_map = load_sync_map()
    mapping = sync_map.get("mappings", {}).get(asana_gid or "", {})
    user_email = mapping.get("user_email", "")
    list_id = mapping.get("todo_list_id", "")
    if not user_email or not list_id:
        logger.warning(f"[todo-sync] complete_todo_task: no user_email/list_id for {asana_gid}")
        return
    url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks/{todo_task_id}"
    logger.info(f"[todo-sync] Completing To-Do task {todo_task_id} for {user_email} (from Asana {asana_gid})")
    _graph_request_with_retry("PATCH", url, json_body={"status": "completed"})
    if asana_gid and asana_gid in sync_map["mappings"]:
        sync_map["mappings"][asana_gid]["completed_by"] = "asana"
        sync_map["mappings"][asana_gid]["last_synced"] = datetime.now(timezone.utc).isoformat()
        save_sync_map(sync_map)


def reopen_todo_task(todo_task_id: str, asana_gid: str = None):
    """Mark To-Do task as not started (reopen) when re-opened in Asana."""
    sync_map = load_sync_map()
    mapping = sync_map.get("mappings", {}).get(asana_gid or "", {})
    user_email = mapping.get("user_email", "")
    list_id = mapping.get("todo_list_id", "")
    if not user_email or not list_id:
        logger.warning(f"[todo-sync] reopen_todo_task: no user_email/list_id for {asana_gid}")
        return
    url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks/{todo_task_id}"
    logger.info(f"[todo-sync] Reopening To-Do task {todo_task_id} for {user_email} (from Asana {asana_gid})")
    _graph_request_with_retry("PATCH", url, json_body={"status": "notStarted"})
    if asana_gid and asana_gid in sync_map["mappings"]:
        sync_map["mappings"][asana_gid].pop("completed_by", None)
        sync_map["mappings"][asana_gid]["last_synced"] = datetime.now(timezone.utc).isoformat()
        save_sync_map(sync_map)

def complete_asana_task(asana_gid: str, todo_task_id: str = None):
    """Mark Asana task complete. Sets completed_by='todo' in mapping."""
    logger.info(f"[todo-sync] Completing Asana task {asana_gid} (triggered by To-Do)")
    try:
        asana_request("PUT", f"/tasks/{asana_gid}", {"completed": True})
    except Exception as e:
        logger.error(f"[todo-sync] Failed to complete Asana task {asana_gid}: {e}", exc_info=True)
        return
    sync_map = load_sync_map()
    if asana_gid in sync_map["mappings"]:
        sync_map["mappings"][asana_gid]["completed_by"] = "todo"
        sync_map["mappings"][asana_gid]["last_synced"] = datetime.now(timezone.utc).isoformat()
        save_sync_map(sync_map)


def poll_todo_completions():
    """Check all user To-Do lists for newly completed tasks and sync back to Asana."""
    global _todo_last_poll_time
    _todo_last_poll_time = datetime.now(timezone.utc).isoformat()
    sync_map = load_sync_map()
    user_lists = sync_map.get("user_lists") or {}
    if not user_lists:
        logger.info("[todo-sync] Poller: no user_lists configured, skipping")
        return
    # Build reverse mapping: todo_task_id -> asana_gid
    reverse = {v["todo_task_id"]: k for k, v in sync_map.get("mappings", {}).items() if v.get("todo_task_id")}
    total_checked = 0
    newly_synced = 0
    for user_email, list_id in user_lists.items():
        try:
            url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks?$filter=status eq 'completed'&$top=100"
            data = _graph_request_with_retry("GET", url)
            completed_tasks = data.get("value") or []
            total_checked += len(completed_tasks)
            for task in completed_tasks:
                tid = task.get("id")
                asana_gid = reverse.get(tid)
                if not asana_gid:
                    continue
                mapping = sync_map["mappings"][asana_gid]
                if mapping.get("completed_by"):
                    continue
                complete_asana_task(asana_gid, todo_task_id=tid)
                newly_synced += 1
        except Exception as e:
            logger.error(f"[todo-sync] Poller error for {user_email}: {e}", exc_info=True)
    logger.info(f"[todo-sync] Poller: {len(user_lists)} users, {total_checked} completed tasks, {newly_synced} newly synced to Asana")



def start_todo_poller():
    """Start background polling thread for To-Do -> Asana sync."""
    global _todo_poller_running
    if _todo_poller_running:
        return
    _todo_poller_running = True

    def _run():
        logger.info(f"[todo-sync] Poller started (interval: {TODO_POLL_INTERVAL}s)")
        while _todo_poller_running:
            try:
                poll_todo_completions()
            except Exception as e:
                logger.error(f"[todo-sync] Poller loop error: {e}", exc_info=True)
            time.sleep(TODO_POLL_INTERVAL)

    t = _threading.Thread(target=_run, daemon=True, name="todo-poller")
    t.start()
    logger.info("[todo-sync] Poller thread launched")



# ======================================================================

def notify_organizer(recipients, approval_id: str, meeting_title: str, intelligence: dict = None, meeting_date_str: str = ""):
    """Send all internal participants a rich notification with meeting intelligence summary.
    recipients: list of email addresses or single email string.
    Supports multiple channels: email, slack, teams (comma-separated in NOTIFY_VIA)."""
    # Normalize to list
    if isinstance(recipients, str):
        recipients = [recipients]
    organizer_email = recipients[0] if recipients else ""
    review_url = f"{APP_BASE_URL}/review/{approval_id}"
    # Ensure URL has protocol for mobile auto-linking
    if not review_url.startswith("http"):
        review_url = f"https://{review_url}"
    channels = [c.strip().lower() for c in NOTIFY_VIA.split(",")]

# ======================================================================
    intel = intelligence or {}
    summary = intel.get("meeting_summary", "Meeting processed  --  review details below.")
    meeting_type = intel.get("meeting_type", "Meeting")
    action_items = intel.get("action_items", [])
    contacts = intel.get("contacts", [])
    email_draft = intel.get("follow_up_email", {})
    key_insights = intel.get("key_insights", [])

    # Clean meeting title: use Claude's extraction or trim raw Fireflies filename
    clean_title = meeting_title
    if len(meeting_title) > 80 or "organisations_" in meeting_title or "meetingAssistant" in meeting_title:
        participant_names = [c.get("name", "") for c in contacts if c.get("name")]
        if participant_names:
            clean_title = f"{meeting_type} with {', '.join(participant_names[:2])}"
        else:
            clean_title = meeting_type or "Meeting"

    # Counts
    n_tasks = len(action_items)
    n_contacts = len(contacts)
    has_email = bool(email_draft.get("body"))

    # Build action items HTML
    tasks_html = ""
    for item in action_items[:5]:
        owner = item.get("owner", "Unassigned")
        task = item.get("task", "")
        priority = item.get("priority", "medium")
        priority_badge = {"high": "!!!", "medium": "!!", "low": "."}.get(priority, "")
        # Resolve due date for display
        _, _, due_display = resolve_due_date(item, meeting_date_str)
        tasks_html += (
            f'<tr>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;">{priority_badge} {task}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#666;white-space:nowrap;">{owner}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #eee;color:#888;white-space:nowrap;font-size:12px;">{due_display}</td>'
            f'</tr>'
        )

    # Build insights HTML (top 3)
    insights_html = ""
    for insight in key_insights[:3]:
        insights_html += f'<li style="margin-bottom:6px;color:#444;">{strip_emojis(insight)}</li>'

# ======================================================================
    if "slack" in channels and SLACK_WEBHOOK_URL:
        try:
            requests.post(SLACK_WEBHOOK_URL, json={
                "text": (
                    f"*Meeting processed: {clean_title}*\n"
                    f"_{summary[:200]}_\n"
                    f"{n_tasks} action items | {n_signals} key signals{email_note}\n"
                    f"<{review_url}|Review & Approve>"
                )
            }, timeout=10)
            logger.info(f"Slack notification sent for {clean_title}")
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")

# ======================================================================
    TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if "teams" in channels and TEAMS_WEBHOOK_URL:
        try:
            card_payload = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {"type": "TextBlock", "text": f"{clean_title}", "weight": "Bolder", "size": "Medium"},
                                {"type": "TextBlock", "text": summary[:200], "wrap": True},
                                {
                                    "type": "FactSet",
                                    "facts": [
                                        {"title": "Organizer", "value": organizer_email},
                                        {"title": "Action Items", "value": str(n_tasks)},
                        {"title": "Status", "value": "Awaiting your review"},
                                    ],
                                },
                            ],
                            "actions": [
                                {
                                    "type": "Action.OpenUrl",
                    "title": "Review & Approve Tasks",
                                    "url": review_url,
                                    "style": "positive",
                                },
                            ],
                        },
                    }
                ],
            }
            resp = requests.post(TEAMS_WEBHOOK_URL, json=card_payload, timeout=15)
            resp.raise_for_status()
            logger.info(f"Teams webhook notification sent for {clean_title}")
        except Exception as e:
            logger.warning(f"Teams webhook notification failed: {e}")

# ======================================================================
    if "email" in channels and MS_GRAPH_CLIENT_ID:
        try:
            token = get_ms_graph_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            # Build tasks table
            tasks_table = ""
            if tasks_html:
                tasks_table = (
                    '<div style="padding:20px 28px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">'
                    '<div style="font-size:12px;text-transform:uppercase;color:#64748b;font-weight:600;letter-spacing:0.5px;margin-bottom:12px;">Action Items</div>'
                    '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
                    '<tr style="background:#f1f5f9;"><th style="padding:8px 12px;text-align:left;font-weight:600;color:#475569;">Task</th><th style="padding:8px 12px;text-align:left;font-weight:600;color:#475569;">Owner</th><th style="padding:8px 12px;text-align:left;font-weight:600;color:#475569;">Due</th></tr>'
                    f'{tasks_html}'
                    '</table></div>'
                )

            # Build insights section
            insights_section = ""
            if insights_html:
                insights_section = (
                    '<div style="padding:20px 28px;background:#fffbeb;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">'
                    '<div style="font-size:12px;text-transform:uppercase;color:#92400e;font-weight:600;letter-spacing:0.5px;margin-bottom:8px;">  Key Insights</div>'
                    f'<ul style="margin:0;padding-left:20px;font-size:14px;line-height:1.6;">{insights_html}</ul></div>'
                )

            email_html = (
                '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;max-width:640px;margin:0 auto;color:#1a1a1a;">'
                # Header
                '<div style="background:linear-gradient(135deg,#1e3a5f,#2563eb);padding:24px 28px;border-radius:12px 12px 0 0;">'
                '<div style="color:white;font-size:13px;text-transform:uppercase;letter-spacing:1px;opacity:0.85;">Meeting Intelligence Report</div>'
                f'<div style="color:white;font-size:22px;font-weight:600;margin-top:8px;">{clean_title}</div>'
                f'<div style="color:rgba(255,255,255,0.75);font-size:13px;margin-top:4px;">{meeting_type} &bull; {n_tasks} action items &bull; {n_contacts} contacts{"  &bull; Draft email ready" if has_email else ""}</div>'
                '</div>'
                # Summary
                '<div style="background:#f8fafc;padding:20px 28px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">'
                '<div style="font-size:12px;text-transform:uppercase;color:#64748b;font-weight:600;letter-spacing:0.5px;margin-bottom:8px;">Summary</div>'
                f'<div style="font-size:14px;line-height:1.6;color:#334155;">{strip_emojis(summary)}</div>'
                '</div>'
                # Action Items
                f'{tasks_table}'
                # Key Insights
                f'{insights_section}'
                # CTA - raw URL (mobile Outlook strips <a> tags, raw https:// URLs auto-link)
                '<div style="padding:28px;text-align:center;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">'
                '<p style="margin:0 0 8px 0;font-weight:600;font-size:15px;"> Review &amp; Approve Tasks</p>'
                f'<p style="margin:0;font-size:14px;">{review_url}</p>'
                '<p style="margin:12px 0 0 0;font-size:12px;color:#94a3b8;">Review, edit, or delete items before they are created in HubSpot, Asana, and Outlook</p>'
                '</div>'
                # Footer
                '<div style="background:#f1f5f9;padding:16px 28px;border-radius:0 0 12px 12px;border:1px solid #e2e8f0;border-top:none;">'
                '<div style="font-size:12px;color:#64748b;">'
                f'&mdash; {BOT_SENDER_NAME}<br>'
                '<span style="color:#94a3b8;">Automated meeting intelligence by Negev Labs</span>'
                '</div></div></div>'
            )

            send_payload = {
                "message": {
                    "subject": f"{clean_title} -- {n_tasks} action items ready for review",
                    "body": {
                        "contentType": "HTML",
                        "content": email_html,
                    },
                    "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
                },
            }
            if BOT_SENDER_EMAIL:
                send_payload["message"]["from"] = {
                    "emailAddress": {"name": BOT_SENDER_NAME, "address": BOT_SENDER_EMAIL}
                }
            if is_app_only_mode():
                # App-only: send from Sara's mailbox (or organizer's)
                url = f"{MS_GRAPH_BASE}/users/{BOT_SENDER_EMAIL or organizer_email}/sendMail"
            else:
                # Delegated: send as authenticated user
                url = f"{MS_GRAPH_BASE}/me/sendMail"
            requests.post(url, json=send_payload, headers=headers, timeout=15)
            logger.info(f"Email notification sent to {recipients} from {BOT_SENDER_EMAIL or 'self'}")
        except Exception as e:
            logger.warning(f"Email notification failed: {e}")

    logger.info(f"Review URL for '{clean_title}': {review_url}")




# ======================================================================
# PIPELINE -> Phase 1: Extract & Queue for Approval
# ======================================================================

def process_transcript_phase1(transcript: dict) -> str:
    """
    Phase 1: Extract intelligence and queue for organizer approval.
    Returns the approval_id.
    """
    transcript_id = transcript["id"]
    title = transcript.get("title", "Unknown Meeting")
    meeting_date = transcript.get("dateString", datetime.now(timezone.utc).isoformat())
    organizer_email = transcript.get("organizer_email", "")

    logger.info(f"=== Phase 1: Extracting intelligence for '{title}' ===")

    # Extract intelligence via Claude
    intelligence = extract_meeting_intelligence(transcript)

    # Create approval record
    approval_id = str(uuid.uuid4())[:8]
    approval = {
        "id": approval_id,
        "transcript_id": transcript_id,
        "title": title,
        "meeting_date": meeting_date,
        "organizer_email": organizer_email,
        "participants": transcript.get("participants") or [],
        "intelligence": intelligence,
        "status": "pending",  # pending -> approved -> executed
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    pending = load_pending()
    pending[approval_id] = approval
    save_pending(pending)

    # Collect all internal recipient emails (organizer + internal contacts from transcript)
    internal_emails = set()
    if organizer_email:
        internal_emails.add(organizer_email.lower())
    for c in intelligence.get("contacts", []):
        if c.get("is_internal") and c.get("email"):
            email = c["email"].lower()
            if email and "@" in email and "placeholder" not in email:
                internal_emails.add(email)
    notify_recipients = list(internal_emails) or [organizer_email]
    logger.info(f"Notifying internal team: {notify_recipients}")

    # Notify all internal participants
    notify_organizer(notify_recipients, approval_id, title, intelligence, meeting_date)

    logger.info(f"Phase 1 complete. Approval ID: {approval_id}  --  awaiting organizer review.")
    return approval_id


# ======================================================================
# PIPELINE -> Phase 2: Execute Approved Actions
# ======================================================================

def execute_approved_actions(approval_id: str, approved_data: dict) -> dict:
    """
    Phase 2: Execute all approved actions (HubSpot, Asana, Outlook draft).
    Called after organizer reviews and approves.
    """
    results = {"actions": []}
    intelligence = approved_data["intelligence"]
    meeting_date = approved_data["meeting_date"]
    title = approved_data["title"]
    organizer_email = approved_data["organizer_email"]

# ======================================================================
    contact_ids = {}
    for contact in intelligence.get("contacts", []):
        if contact.get("is_internal"):
            continue
        email = contact.get("email", "")
        if not email or "placeholder" in email or "unknown" in email or "@" not in email:
            continue
        try:
            existing = find_hubspot_contact(email)
            if existing:
                contact_ids[email] = existing["id"]
                results["actions"].append(f"[OK] Found HubSpot contact: {contact['name']}")
            else:
                new_contact = create_hubspot_contact(contact, organizer_email)
                contact_ids[email] = new_contact["id"]
                results["actions"].append(f"[OK] Created HubSpot contact: {contact['name']}")
        except Exception as e:
            results["actions"].append(f"[ERR] HubSpot contact failed ({email}): {e}")

    # Log meeting in HubSpot (Meeting engagement, not Note)
    note_body = intelligence.get("hubspot_note", "Meeting processed via pipeline.")
    transcript_id = approved_data.get("transcript_id", "")
    for email, cid in contact_ids.items():
        try:
            log_hubspot_meeting(cid, note_body, meeting_date, title=title, transcript_id=transcript_id)
            results["actions"].append(f"[OK] Meeting logged on {email}")
        except Exception as e:
            results["actions"].append(f"[ERR] Meeting log failed for {email}: {e}")

# ======================================================================
    action_items = intelligence.get("action_items", [])
    for item in action_items:
        hubspot_due, asana_due, due_display = resolve_due_date(item, meeting_date)
        create_in = item.get("create_in", "both").lower()
        owner_email = item.get("owner_email", "") or organizer_email

        # HubSpot task (for external/investor-facing tasks)
        if create_in in ("hubspot", "both"):
            for email, cid in contact_ids.items():
                try:
                    create_hubspot_task(cid, item["task"], item.get("task", ""), hubspot_due, owner_email)
                    results["actions"].append(f"[OK] HubSpot task: {item['task'][:60]} (due {due_display})")
                except Exception as e:
                    results["actions"].append(f"[ERR] HubSpot task failed: {e}")
                break

        # Asana task (for internal operational tasks)
        if create_in in ("asana", "both"):
            try:
                notes = f"From meeting: {title}\nOwner: {item.get('owner', 'TBD')}\nPriority: {item.get('priority', 'medium')}\nDue: {due_display} ({item.get('due_context', 'TBD')})"
                # Look up Asana user by owner email, fallback to organizer
                assignee_gid = None
                if owner_email:
                    assignee_gid = find_asana_user_by_email(owner_email)
                if not assignee_gid and organizer_email:
                    assignee_gid = find_asana_user_by_email(organizer_email)
                asana_task = create_asana_task(item["task"], notes, asana_due, assignee_gid)
                results["actions"].append(f"[OK] Asana task: {item['task'][:60]} (due {due_display})")
                # Sync new Asana task to Microsoft To-Do
                if asana_task and asana_task.get("gid") and MS_GRAPH_CLIENT_ID:
                    try:
                        todo_user = (owner_email or organizer_email or '').lower()
                        if todo_user and todo_user.endswith('@negevlabs.com'):
                            create_todo_task(
                                title=item['task'],
                                user_email=todo_user,
                                notes=notes,
                                due_date=asana_due,
                                asana_gid=asana_task['gid'],
                                asana_project_gid=ASANA_PROJECT_GID,
                            )
                        results["actions"].append(f"[OK] To-Do task synced for: {item['task'][:60]}")
                    except Exception as te:
                        logger.warning(f"[todo-sync] Failed to create To-Do task for Asana {asana_task.get('gid')}: {te}")
                        results["actions"].append(f"[WARN] To-Do sync failed: {te}")
            except Exception as e:
                results["actions"].append(f"[ERR] Asana task failed: {e}")

# ======================================================================
    follow_up = intelligence.get("follow_up_email", {})
    if follow_up and follow_up.get("to_recipients") and MS_GRAPH_CLIENT_ID:
        try:
            create_outlook_draft(
                sender_email=follow_up.get("from_email", organizer_email),
                to_recipients=follow_up["to_recipients"],
                subject=follow_up.get("subject", f"Following up  --  {title}"),
                body_html=follow_up.get("body_html", follow_up.get("body_text", "")),
            )
            results["actions"].append(f"[OK] Outlook draft created in {organizer_email}'s Drafts")
        except Exception as e:
            results["actions"].append(f"[ERR] Outlook draft failed: {e}")

    # Mark as executed
    pending = load_pending()
    if approval_id in pending:
        pending[approval_id]["status"] = "executed"
        pending[approval_id]["results"] = results
        save_pending(pending)

    logger.info(f"Phase 2 complete for approval {approval_id}: {len(results['actions'])} actions")
    return results


# ======================================================================
#  REVIEW & APPROVAL WEB UI
# ======================================================================

REVIEW_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Review: {{ data.title }}</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               background: #f1f5f9; color: #1e293b; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        h1 { font-size: 24px; margin-bottom: 4px; }
        .subtitle { color: #64748b; font-size: 14px; margin-bottom: 20px; }
        h2 { font-size: 18px; margin-bottom: 16px; color: #334155; }
        .signal-badge { display: inline-block; padding: 4px 12px; border-radius: 20px;
                        font-size: 12px; font-weight: 600; margin-right: 6px; margin-bottom: 6px; }
        .high { background: #dcfce7; color: #166534; }
        .medium { background: #fef3c7; color: #92400e; }
        .low { background: #fee2e2; color: #991b1b; }
        .task-item { border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px;
                     margin-bottom: 12px; position: relative; }
        .task-item.deleted { opacity: 0.4; text-decoration: line-through; }
        .task-header { display: flex; justify-content: space-between; align-items: flex-start; }
        textarea { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; padding: 10px;
                   font-family: inherit; font-size: 14px; resize: vertical; min-height: 60px; }
        textarea:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
        input[type=text] { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px;
                           padding: 8px 10px; font-family: inherit; font-size: 14px; }
        input[type=text]:focus { outline: none; border-color: #3b82f6; }
        label { font-size: 12px; font-weight: 600; color: #64748b; display: block; margin-bottom: 4px; margin-top: 10px; }
        .btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px;
               font-weight: 600; cursor: pointer; transition: all 0.2s; }
        .btn-primary { background: #2563eb; color: white; }
        .btn-primary:hover { background: #1d4ed8; }
        .btn-danger { background: #fee2e2; color: #dc2626; }
        .btn-danger:hover { background: #fecaca; }
        .btn-ghost { background: transparent; color: #64748b; border: 1px solid #e2e8f0; }
        .btn-ghost:hover { background: #f8fafc; }
        .btn-sm { padding: 6px 12px; font-size: 12px; }
        .actions-bar { display: flex; justify-content: space-between; align-items: center;
                       margin-top: 20px; padding-top: 20px; border-top: 1px solid #e2e8f0; }
        .email-preview { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
                         padding: 16px; margin-top: 12px; }
        .meta-row { display: flex; gap: 8px; margin-bottom: 6px; font-size: 13px; }
        .meta-label { font-weight: 600; color: #64748b; min-width: 70px; }
        .signals-list { list-style: none; padding: 0; }
        .signals-list li { padding: 6px 0; font-size: 14px; }
            .signals-list li::before { content: "-> "; color: #3b82f6; font-weight: bold; }
        .status-banner { padding: 16px; border-radius: 8px; text-align: center; font-weight: 600; }
        .status-executed { background: #dcfce7; color: #166534; }
        .status-pending { background: #dbeafe; color: #1e40af; }
    </style>
</head>
<body>
    <div class="container">
        {% if data.status == 'executed' %}
        <div class="card status-banner status-executed">
              Actions already executed for this meeting.
        </div>
        {% endif %}

        <div class="card">
        <h1>{{ data.title }}</h1>
        <p class="subtitle">{{ (data.meeting_date|string)[:10] }} | Organizer: {{ data.organizer_email }}</p>

            {% set signals = data.intelligence.get('signals', {}) %}
            <div style="margin-top: 12px;">
                <span class="signal-badge {{ signals.get('interest_level', 'medium') }}">
                    {{ signals.get('interest_level', 'medium')|upper }} INTEREST
                </span>
                <span class="signal-badge" style="background:#ede9fe;color:#5b21b6;">
                    {{ signals.get('relationship_type', 'other')|upper }}
                </span>
            </div>

            {% if signals.get('key_signals') %}
            <div style="margin-top: 16px;">
                <label>KEY SIGNALS</label>
                <ul class="signals-list">
                    {% for s in signals.key_signals %}
                    <li>{{ s }}</li>
                    {% endfor %}
                </ul>
            </div>
            {% endif %}
        </div>

        <form method="POST" action="/review/{{ data.id }}/approve">
            <!-- ACTION ITEMS -->
            <div class="card">
        <h2>Action Items</h2>
                <p style="color:#64748b; font-size:13px; margin-bottom:16px;">
                    Edit task text, change owners, or delete tasks you don't need. Only approved tasks will be created.
                </p>

                {% for item in data.intelligence.get('action_items', []) %}
                <div class="task-item" id="task-{{ loop.index0 }}">
                    <div class="task-header">
                        <div style="flex:1;">
                            <label>TASK</label>
                            <textarea name="task_text_{{ loop.index0 }}" rows="2">{{ item.task }}</textarea>

                            <div style="display:flex; gap:12px;">
                                <div style="flex:1;">
                                    <label>OWNER</label>
                                    <input type="text" name="task_owner_{{ loop.index0 }}" value="{{ item.get('owner', '') }}">
                                    <input type="hidden" name="task_owner_email_{{ loop.index0 }}" value="{{ item.get('owner_email', '') }}">
                                </div>
                                <div style="flex:0.5;">
                                    <label>CREATE IN</label>
                                    <select name="task_create_in_{{ loop.index0 }}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                        <option value="hubspot" {{ 'selected' if item.get('create_in') == 'hubspot' }}>HubSpot (external)</option>
                                        <option value="asana" {{ 'selected' if item.get('create_in', 'asana') == 'asana' }}>Asana (internal)</option>
                                        <option value="both" {{ 'selected' if item.get('create_in') == 'both' }}>Both</option>
                                    </select>
                                </div>
                                <div style="flex:0.5;">
                                    <label>PRIORITY</label>
                                    <select name="task_priority_{{ loop.index0 }}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                        <option value="high" {{ 'selected' if item.get('priority') == 'high' }}>High</option>
                                        <option value="medium" {{ 'selected' if item.get('priority', 'medium') == 'medium' }}>Medium</option>
                                        <option value="low" {{ 'selected' if item.get('priority') == 'low' }}>Low</option>
                                    </select>
                                </div>
                                <div style="flex:0.5;">
                                    <label>DUE (days)</label>
                                    <input type="number" name="task_due_days_{{ loop.index0 }}" value="{{ item.get('due_days', 7) }}" min="1" max="90"
                                           style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                    <span style="font-size:11px;color:#94a3b8;">{{ item.get('due_context', '') }}</span>
                                </div>
                            </div>
                        </div>
                        <div style="margin-left:12px; padding-top:20px;">
                            <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
                                <input type="checkbox" name="task_delete_{{ loop.index0 }}" value="1"
                                       onchange="this.closest('.task-item').classList.toggle('deleted')">
                                <span style="font-size:12px; color:#dc2626;">Delete</span>
                            </label>
                        </div>
                    </div>
                </div>
                {% endfor %}

                <input type="hidden" name="task_count" value="{{ data.intelligence.get('action_items', [])|length }}">
            </div>

            <!-- FOLLOW-UP EMAIL -->
            <div class="card">
        <h2>Follow-Up Email Draft</h2>
                <p style="color:#64748b; font-size:13px; margin-bottom:16px;">
                    This will be saved as a draft in Outlook ({{ data.organizer_email }}). Edit before approving.
                </p>

                {% set email = data.intelligence.get('follow_up_email', {}) %}
                <label>TO</label>
                <input type="text" name="email_to" value="{% for r in email.get('to_recipients', []) %}{{ r.get('name', '') }} &lt;{{ r.get('email', '') }}&gt;{% if not loop.last %}, {% endif %}{% endfor %}">

                <label>SUBJECT</label>
                <input type="text" name="email_subject" value="{{ email.get('subject', '') }}">

                <label>BODY</label>
                <textarea name="email_body" rows="10">{{ email.get('body_text', '') }}</textarea>

                <label style="display:flex; align-items:center; gap:6px; margin-top:12px; cursor:pointer;">
                    <input type="checkbox" name="skip_email" value="1">
                    <span style="font-size:13px; color:#64748b;">Skip email draft  --  don't create in Outlook</span>
                </label>
            </div>

            <!-- APPROVE / CANCEL -->
            {% if data.status == 'pending' %}
            <div class="card">
                <div class="actions-bar" style="border-top:none; margin-top:0; padding-top:0;">
                    <a href="/review/{{ data.id }}/cancel" class="btn btn-ghost">Cancel  --  Don't Create Anything</a>
                    <button type="submit" class="btn btn-primary"> Approve & Create Tasks + Draft</button>
                </div>
            </div>
            {% endif %}
        </form>
    </div>
</body>
</html>
"""

RESULT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ status_title }}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f1f5f9;
               color: #1e293b; padding: 20px; }
        .container { max-width: 600px; margin: 40px auto; }
        .card { background: white; border-radius: 12px; padding: 32px; text-align: center;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        h1 { font-size: 24px; margin-bottom: 12px; }
        .action-item { text-align: left; padding: 8px 0; font-size: 14px; border-bottom: 1px solid #f1f5f9; }
        .action-item:last-child { border-bottom: none; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>{{ status_emoji }} {{ status_title }}</h1>
            <p style="color:#64748b; margin-bottom:24px;">{{ data.title }}</p>
            {% if actions %}
            <div style="text-align:left; margin-top:20px;">
                {% for action in actions %}
                <div class="action-item">{{ action }}</div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


# ======================================================================
#  FLASK ROUTES
# ======================================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/version", methods=["GET"])
def version():
    return jsonify({"version": "2.7.6-fetch-completed", "deployed": "2026-02-23"})


@app.route("/config", methods=["GET"])
def config_check():
    """Diagnostic: verify team-wide config (no secrets exposed)."""
    return jsonify({
        "graph_auth_mode": "app-only (team-wide)" if is_app_only_mode() else "delegated (single-user)",
        "graph_client_id_set": bool(MS_GRAPH_CLIENT_ID), "graph_client_secret_set": bool(MS_GRAPH_CLIENT_SECRET), "refresh_token_len": len(MS_GRAPH_REFRESH_TOKEN),
        "graph_tenant_id_set": bool(MS_GRAPH_TENANT_ID),
        "bot_sender": BOT_SENDER_EMAIL or "(not set)",
        "hubspot_owner_map_raw": HUBSPOT_OWNER_MAP_RAW[:200] if HUBSPOT_OWNER_MAP_RAW else "(empty)",
        "hubspot_owner_map_entries": len(HUBSPOT_OWNER_MAP),
        "hubspot_owner_map_emails": list(HUBSPOT_OWNER_MAP.keys()),
        "hubspot_fallback_owner": HUBSPOT_OWNER_ID or "(not set)",
        "notify_via": NOTIFY_VIA,
        "todo_list_name": TODO_LIST_NAME,
        "todo_poll_interval": TODO_POLL_INTERVAL,
        "todo_poller_running": _todo_poller_running,
    })




@app.route("/test", methods=["GET"])
def test_pipeline():
    """Dry-run: fetch transcript, extract intelligence, test To-Do API, report pass/fail."""
    import time as _time
    import traceback as _tb
    results = {"version": "2.7.6-fetch-completed", "steps": {}}
    try:
        # Step 1: Fetch recent transcript
        t0 = _time.time()
        recent = get_recent_transcripts(since_minutes=10080)
        if not recent:
            return jsonify({"status": "skip", "reason": "No transcripts in last 7 days"}), 200
        transcript = recent[0]
        results["steps"]["fetch"] = {"status": "ok", "title": transcript.get("title","?"), "ms": int((_time.time()-t0)*1000)}

        # Step 2: Null-safety check
        summary = transcript.get("summary") or {}
        participants = transcript.get("participants") or []
        sentences = transcript.get("sentences") or []
        _ = summary.get("short_summary", "N/A")
        results["steps"]["null_safe"] = {"status": "ok", "summary_none": transcript.get("summary") is None, "parts": len(participants), "sents": len(sentences)}

        # Step 3: Claude extraction
        t0 = _time.time()
        intelligence = extract_meeting_intelligence(transcript)
        results["steps"]["claude"] = {"status": "ok", "tasks": len(intelligence.get("tasks",[])), "contacts": len(intelligence.get("contacts",[])), "ms": int((_time.time()-t0)*1000)}

        # Step 4: Microsoft To-Do API connectivity
        if MS_GRAPH_CLIENT_ID:
            try:
                t0 = _time.time()
                token = get_ms_graph_token()
                test_user = "bk@negevlabs.com"
                url = f"{MS_GRAPH_BASE}/users/{test_user}/todo/lists"
                todo_resp = _graph_request_with_retry("GET", url)
                lists = todo_resp.get("value") or []
                results["steps"]["todo_api"] = {"status": "ok", "lists_count": len(lists), "ms": int((_time.time()-t0)*1000)}
            except Exception as te:
                results["steps"]["todo_api"] = {"status": "fail", "error": str(te)}
        else:
            results["steps"]["todo_api"] = {"status": "skip", "reason": "MS_GRAPH_CLIENT_ID not set"}

        results["status"] = "pass"
        return jsonify(results), 200
    except Exception as e:
        results["status"] = "fail"
        results["error"] = str(e)
        results["traceback"] = _tb.format_exc()
        return jsonify(results), 500

@app.route("/webhook/asana", methods=["POST"])
def asana_webhook():
    """Asana webhook handler â€” handshake + event processing."""
    import threading

    # Handshake: echo X-Hook-Secret back
    hook_secret = request.headers.get("X-Hook-Secret")
    if hook_secret:
        logger.info(f"[todo-sync] Asana webhook handshake received, echoing X-Hook-Secret")
        return jsonify({}), 200, {"X-Hook-Secret": hook_secret}

    payload = request.get_json(force=True, silent=True) or {}
    events = payload.get("events") or []
    logger.info(f"[todo-sync] Asana webhook: {len(events)} events")

    def _log_event_debug(ev):
        a = ev.get("action")
        r = ev.get("resource") or {}
        c = ev.get("change") or {}
        logger.info(f"[todo-sync] EVENT: action={a} type={r.get('resource_type')} gid={r.get('gid')} field={c.get('field')} new_value={c.get('new_value')} new_value_type={type(c.get('new_value')).__name__}")
    for _ev in events:
        _log_event_debug(_ev)

    def _process_events():
        try:
            for event in events:
                action = event.get("action")
                resource = event.get("resource") or {}
                change = event.get("change") or {}
                if resource.get("resource_type") != "task":
                    continue
                task_gid = resource.get("gid")
                if not task_gid:
                    continue

                sync_map = load_sync_map()
                mapping = sync_map["mappings"].get(task_gid)

                if action == "added":
                    # New task added to project â€” create in To-Do
                    logger.info(f"[todo-sync] Asana webhook: task added {task_gid}")
                    try:
                        task_data = asana_request("GET", f"/tasks/{task_gid}?opt_fields=name,notes,due_on,assignee.email")
                        if not task_data:
                            continue
                        name = task_data.get("name") or "Asana Task"
                        notes = task_data.get("notes") or ""
                        due_on = task_data.get("due_on")
                        assignee = task_data.get("assignee") or {}
                        assignee_email = (assignee.get("email") or "").lower()
                        if not mapping and assignee_email and assignee_email.endswith("@negevlabs.com"):
                            create_todo_task(name, user_email=assignee_email, notes=notes, due_date=due_on, asana_gid=task_gid)
                    except Exception as e:
                        logger.error(f"[todo-sync] Failed to create To-Do for new Asana task {task_gid}: {e}", exc_info=True)

                elif action == "changed":
                    field = change.get("field")
                    new_val = change.get("new_value")

                    if field == "completed" and mapping:
                        # Asana sends new_value=None for completed field - must fetch actual status
                        td = asana_request("GET", f"/tasks/{task_gid}?opt_fields=completed")
                        if td:
                            is_completed = td.get("completed", False)
                            logger.info(f"[todo-sync] Asana webhook: completed field changed for {task_gid}, actual completed={is_completed}")
                            if is_completed and not mapping.get("completed_by"):
                                logger.info(f"[todo-sync] Completing To-Do for {task_gid}")
                                try:
                                    complete_todo_task(mapping["todo_task_id"], asana_gid=task_gid)
                                except Exception as e:
                                    logger.error(f"[todo-sync] complete_todo_task failed for {task_gid}: {e}", exc_info=True)
                            elif not is_completed:
                                logger.info(f"[todo-sync] Reopening To-Do for {task_gid}")
                                try:
                                    reopen_todo_task(mapping["todo_task_id"], asana_gid=task_gid)
                                except Exception as e:
                                    logger.error(f"[todo-sync] reopen_todo_task failed for {task_gid}: {e}", exc_info=True)


                    elif field in ("name", "due_on", "notes") and mapping:
                        # Name, due date, or notes changed -> fetch current values from Asana then update To-Do
                        # NOTE: Asana webhooks do NOT include new_value for most fields - must fetch
                        logger.info(f"[todo-sync] Asana webhook: {field} changed for {task_gid}, fetching current values")
                        try:
                            td = asana_request("GET", f"/tasks/{task_gid}?opt_fields=name,notes,due_on")
                            if td:
                                _title = td.get("name")
                                _notes_raw = td.get("notes") or ""
                                _due = td.get("due_on")
                                _proj = ASANA_PROJECT_GID
                                _link = f"Asana: https://app.asana.com/0/{_proj}/{task_gid}" if _proj else ""
                                _body = f"{_link}\n\n{_notes_raw}".strip() if _link else _notes_raw
                                logger.info(f"[todo-sync] Fetched: name='{_title}', due={_due}, notes_len={len(_notes_raw)}")
                                update_todo_task(mapping["todo_task_id"], asana_gid=task_gid, title=_title, notes=_body, due_date=_due)
                            else:
                                logger.warning(f"[todo-sync] Could not fetch task {task_gid} from Asana")
                        except Exception as e:
                            logger.error(f"[todo-sync] update_todo_task failed for {task_gid}: {e}", exc_info=True)
                elif action in ("deleted", "removed") and mapping:
                    # Task removed â€” mark To-Do complete (best effort)
                    try:
                        complete_todo_task(mapping["todo_task_id"], asana_gid=task_gid)
                    except Exception as e:
                        logger.warning(f"[todo-sync] Could not complete To-Do on Asana delete {task_gid}: {e}")

        except Exception as e:
            logger.error(f"[todo-sync] Asana webhook processing error: {e}", exc_info=True)

    threading.Thread(target=_process_events, daemon=True).start()
    return jsonify({"status": "processing"}), 200


@app.route("/sync/webhook-verify", methods=["GET", "POST"])
def sync_webhook_verify():
    """Check if Asana webhook is active. POST to force-recreate if dead."""
    sync_map = load_sync_map()
    wh_id = sync_map.get("asana_webhook_id")
    result = {"webhook_id": wh_id, "status": "unknown"}

    if not wh_id:
        result["status"] = "not_registered"
        result["fix"] = "Call /sync/setup to register"
        return jsonify(result), 200

    # Query Asana for webhook status
    try:
        wh_data = asana_request("GET", f"/webhooks/{wh_id}")
        if wh_data:
            result["active"] = wh_data.get("active", False)
            result["target"] = wh_data.get("target")
            result["last_failure_at"] = wh_data.get("last_failure_at")
            result["last_failure_content"] = wh_data.get("last_failure_content")
            result["status"] = "active" if wh_data.get("active") else "failed"
        else:
            result["status"] = "not_found"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    # If POST and webhook is dead, force recreate
    if request.method == "POST" and result["status"] in ("failed", "not_found", "error"):
        try:
            # Delete old
            try:
                asana_request("DELETE", f"/webhooks/{wh_id}")
            except Exception:
                pass
            # Create new
            webhook_target = f"{RAILWAY_PUBLIC_URL}/webhook/asana"
            wh = asana_request("POST", "/webhooks", {
                "resource": ASANA_PROJECT_GID,
                "target": webhook_target,
            })
            new_id = (wh or {}).get("gid", "pending_handshake")
            sync_map = load_sync_map()
            sync_map["asana_webhook_id"] = new_id
            save_sync_map(sync_map)
            result["action"] = "recreated"
            result["new_webhook_id"] = new_id
            result["status"] = "recreated"
        except Exception as re_err:
            result["action"] = "recreate_failed"
            result["recreate_error"] = str(re_err)

    return jsonify(result), 200


@app.route("/todo/setup", methods=["GET", "POST"])
def todo_setup():
    """Setup: create To-Do list for a user. Pass ?user=email@negevlabs.com"""
    user_email = request.args.get("user", "bk@negevlabs.com")
    try:
        list_id = get_or_create_todo_list(user_email)
        sync_map = load_sync_map()
        sync_map.setdefault("user_lists", {})[user_email] = list_id
        save_sync_map(sync_map)
        return jsonify({"status": "ok", "user": user_email, "todo_list_id": list_id, "list_name": TODO_LIST_NAME}), 200
    except Exception as e:
        logger.error(f"[todo/setup] Failed for {user_email}: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/sync/setup", methods=["POST"])
def sync_setup():
    """One-time setup: create To-Do list, register Asana webhook, do initial full sync."""
    results = {}
    force = request.args.get("force", "").lower() in ("true", "1", "yes")
    try:
        # Per-user lists created automatically during sync
        sync_map = load_sync_map()

        # 2. Register Asana webhook
        if ASANA_PROJECT_GID:
            webhook_target = f"{RAILWAY_PUBLIC_URL}/webhook/asana"
            try:
                existing_wh_id = sync_map.get("asana_webhook_id")
                if existing_wh_id and force:
                    logger.info(f"[todo-sync] Force mode: deleting webhook {existing_wh_id}")
                    try:
                        asana_request("DELETE", f"/webhooks/{existing_wh_id}")
                    except Exception:
                        pass
                    sync_map["asana_webhook_id"] = None
                    save_sync_map(sync_map)
                    existing_wh_id = None
                    results["webhook_note"] = "Old webhook deleted, creating new"
                if existing_wh_id:
                    results["asana_webhook_id"] = existing_wh_id
                    results["webhook_note"] = "Already registered"
                else:
                    try:
                        wh = asana_request("POST", "/webhooks", {
                            "resource": ASANA_PROJECT_GID,
                            "target": webhook_target,
                        })
                        wh_id = (wh or {}).get("gid", "pending_handshake")
                    except Exception as wh_err:
                        # 403 = webhook already exists, list and reuse
                        logger.info(f"[todo-sync] Webhook create failed ({wh_err}), listing existing...")
                        wh_id = None
                        try:
                            existing = asana_request("GET", f"/webhooks?workspace={ASANA_WORKSPACE_GID}&resource={ASANA_PROJECT_GID}")
                            for wh_item in (existing if isinstance(existing, list) else []):
                                if webhook_target in (wh_item.get("target") or ""):
                                    wh_id = wh_item.get("gid")
                                    logger.info(f"[todo-sync] Found existing webhook: {wh_id}")
                                    break
                        except Exception as list_err:
                            logger.warning(f"[todo-sync] Could not list webhooks: {list_err}")
                        if not wh_id:
                            raise wh_err
                    sync_map = load_sync_map()
                    sync_map["asana_webhook_id"] = wh_id
                    save_sync_map(sync_map)
                    results["asana_webhook_id"] = wh_id
                    results["webhook_target"] = webhook_target
            except Exception as e:
                results["webhook_error"] = str(e)
                logger.warning(f"[todo-sync] Asana webhook registration failed: {e}")

        # 3. Full sync of all incomplete Asana tasks
        tasks_synced = 0
        if ASANA_PROJECT_GID:
            try:
                project_tasks = asana_request("GET", f"/projects/{ASANA_PROJECT_GID}/tasks?opt_fields=gid,name,notes,due_on,completed,assignee.email")
                task_list = project_tasks if isinstance(project_tasks, list) else []
                sync_map = load_sync_map()
                for task in task_list:
                    if task.get("completed"):
                        continue
                    gid = task.get("gid")
                    if not gid or gid in sync_map.get("mappings", {}):
                        continue
                    assignee = task.get("assignee") or {}
                    assignee_email = (assignee.get("email") or "").lower()
                    if not assignee_email or not assignee_email.endswith("@negevlabs.com"):
                        continue
                    try:
                        create_todo_task(
                            title=task.get("name") or "Asana Task",
                            user_email=assignee_email,
                            notes=task.get("notes") or "",
                            due_date=task.get("due_on"),
                            asana_gid=gid,
                        )
                        tasks_synced += 1
                        tasks_synced += 1
                    except Exception as e:
                        logger.warning(f"[todo-sync] Initial sync failed for {gid}: {e}")
            except Exception as e:
                results["initial_sync_error"] = str(e)
                logger.error(f"[todo-sync] Initial sync error: {e}", exc_info=True)

        results["tasks_synced"] = tasks_synced
        results["status"] = "ok"
        return jsonify(results), 200
    except Exception as e:
        logger.error(f"[todo-sync] Setup error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/sync/status", methods=["GET"])
def sync_status():
    """Return current sync status and mapping stats."""
    sync_map = load_sync_map()
    return jsonify({
        "todo_list_id": sync_map.get("todo_list_id"),
        "asana_webhook_id": sync_map.get("asana_webhook_id"),
        "total_mapped_tasks": len(sync_map.get("mappings", {})),
        "poller_running": _todo_poller_running,
        "last_poll_time": _todo_last_poll_time,
        "poll_interval_seconds": TODO_POLL_INTERVAL,
        "todo_list_name": TODO_LIST_NAME,
    })


@app.route("/sync/full", methods=["POST"])
def sync_full():
    """Full re-sync: create To-Do tasks for all assigned, unmapped Asana tasks (per-user)."""
    if not ASANA_PROJECT_GID:
        return jsonify({"status": "error", "reason": "ASANA_PROJECT_GID not set"}), 400
    tasks_synced = 0
    tasks_skipped = 0
    users_synced = set()
    errors = []
    try:
        project_tasks = asana_request("GET", f"/projects/{ASANA_PROJECT_GID}/tasks?opt_fields=gid,name,notes,due_on,completed,assignee.email")
        task_list = project_tasks if isinstance(project_tasks, list) else []
        for task in task_list:
            if task.get("completed"):
                tasks_skipped += 1
                continue
            gid = task.get("gid")
            if not gid:
                continue
            assignee = task.get("assignee") or {}
            assignee_email = (assignee.get("email") or "").lower().strip()
            if not assignee_email or not assignee_email.endswith("@negevlabs.com"):
                tasks_skipped += 1
                continue
            sync_map = load_sync_map()
            if gid in sync_map.get("mappings", {}):
                tasks_skipped += 1
                continue
            try:
                create_todo_task(
                    title=task.get("name") or "Asana Task",
                    user_email=assignee_email,
                    notes=task.get("notes") or "",
                    due_date=task.get("due_on"),
                    asana_gid=gid,
                )
                tasks_synced += 1
                users_synced.add(assignee_email)
            except Exception as e:
                errors.append(f"{gid} ({assignee_email}): {e}")
                logger.warning(f"[todo-sync] Full sync failed for {gid} ({assignee_email}): {e}")
        return jsonify({"status": "ok", "tasks_synced": tasks_synced, "tasks_skipped": tasks_skipped, "users": sorted(users_synced), "errors": errors})
    except Exception as e:
        logger.error(f"[todo-sync] Full sync error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500




@app.route("/sync/reset", methods=["POST"])
def sync_reset():
    """Clear sync map and delete all tasks from all user To-Do lists. Start fresh."""
    try:
        sync_map = load_sync_map()
        user_lists = sync_map.get("user_lists") or {}
        deleted = 0
        for user_email, list_id in user_lists.items():
            try:
                url = f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks"
                data = _graph_request_with_retry("GET", url)
                for task in (data.get("value") or []):
                    try:
                        _graph_request_with_retry("DELETE",
                            f"{MS_GRAPH_BASE}/users/{user_email}/todo/lists/{list_id}/tasks/{task['id']}")
                        deleted += 1
                    except Exception as e:
                        logger.warning(f"[sync-reset] Could not delete task {task['id']} for {user_email}: {e}")
            except Exception as e:
                logger.warning(f"[sync-reset] Could not list tasks for {user_email}: {e}")
        new_map = {"user_lists": {}, "mappings": {}}
        save_sync_map(new_map)
        return jsonify({"status": "ok", "tasks_deleted": deleted, "users_cleared": list(user_lists.keys()), "mappings_cleared": len(sync_map.get("mappings", {}))})
    except Exception as e:
        logger.error(f"[sync-reset] Error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500
@app.route("/webhook/fireflies", methods=["POST"])
def fireflies_webhook():
    """Fireflies webhook  --  triggers Phase 1 (extract + queue for approval)."""
    import threading
    payload = request.get_json(force=True)
    logger.info(f"Webhook received: {json.dumps(payload)[:500]}")

    transcript_id = payload.get("meetingId") or payload.get("transcriptId") or payload.get("data", {}).get("transcriptId")
    logger.info(f"Webhook extracted transcript_id={transcript_id}")
    if not transcript_id:
        logger.warning("Webhook: No transcript ID found in payload")
        return jsonify({"error": "No transcript ID"}), 400

    try:
        processed = load_processed()
        logger.info(f"Webhook: loaded {len(processed)} processed transcripts")
    except Exception as e:
        logger.error(f"Webhook: failed to load processed list: {e}", exc_info=True)
        processed = set()

    if transcript_id in processed:
        logger.info(f"Webhook: {transcript_id} already processed, skipping")
        return jsonify({"status": "already_processed"})

    logger.info(f"Webhook: starting background processing for {transcript_id}")

    def _do_webhook_process():
        try:
            logger.info(f"Webhook thread: fetching transcript {transcript_id}")
            transcript = None
 # Retry up to 3 times with increasing delay -> webhook may fire before transcript is ready
            for attempt in range(1, 4):
                try:
                    transcript = get_transcript_by_id(transcript_id)
                except Exception as fetch_err:
                    logger.warning(f"Webhook thread: fetch attempt {attempt}/3 failed: {fetch_err}")
                    transcript = None
                if transcript:
                    break
                wait_seconds = attempt * 15  # 15s, 30s, 45s
                logger.info(f"Webhook thread: transcript not available, retry {attempt}/3 in {wait_seconds}s...")
                import time
                time.sleep(wait_seconds)
            if not transcript:
                logger.error(f"Webhook thread: transcript not found after 3 attempts: {transcript_id}")
                return
            logger.info(f"Webhook thread: got transcript '{transcript.get('title', '?')}', starting Phase 1")
            approval_id = process_transcript_phase1(transcript)
            proc = load_processed()
            proc.add(transcript_id)
            save_processed(proc)
            logger.info(f"Webhook Phase 1 complete: approval_id={approval_id}")
        except Exception as e:
            logger.error(f"Webhook background error: {e}", exc_info=True)

    thread = threading.Thread(target=_do_webhook_process)
    thread.start()
    logger.info(f"Webhook: background thread started for {transcript_id}")
    return jsonify({"status": "processing", "message": "Phase 1 started in background."})


@app.route("/review/<approval_id>", methods=["GET"])
def review_page(approval_id: str):
    """Approval review page  --  organizer sees tasks + email draft and can edit/delete."""
    pending = load_pending()
    data = pending.get(approval_id)
    if not data:
        return "Approval not found or expired.", 404
    return render_template_string(REVIEW_TEMPLATE, data=data)


@app.route("/review/<approval_id>/approve", methods=["POST"])
def approve_actions(approval_id: str):
    """Process the approved (and possibly edited) actions."""
    pending = load_pending()
    data = pending.get(approval_id)
    if not data:
        return "Approval not found.", 404
    if data["status"] != "pending":
        return render_template_string(RESULT_TEMPLATE, data=data,
                                      status_title="Already Processed", status_emoji="&#9989;", actions=[])

# ======================================================================
    task_count = int(request.form.get("task_count", 0))
    approved_tasks = []
    for i in range(task_count):
        if request.form.get(f"task_delete_{i}"):
            continue  # Deleted by organizer
        task_text = request.form.get(f"task_text_{i}", "").strip()
        if not task_text:
            continue
        approved_tasks.append({
            "task": task_text,
            "owner": request.form.get(f"task_owner_{i}", ""),
            "owner_email": request.form.get(f"task_owner_email_{i}", ""),
            "priority": request.form.get(f"task_priority_{i}", "medium"),
            "due_days": int(request.form.get(f"task_due_days_{i}", 7)),
            "due_context": data["intelligence"].get("action_items", [{}])[i].get("due_context", "1 week") if i < len(data["intelligence"].get("action_items", [])) else "1 week",
            "create_in": request.form.get(f"task_create_in_{i}", "both"),
        })
    data["intelligence"]["action_items"] = approved_tasks

# ======================================================================
    skip_email = request.form.get("skip_email")
    if skip_email:
        data["intelligence"]["follow_up_email"] = {}
    else:
        email_subject = request.form.get("email_subject", "")
        email_body = request.form.get("email_body", "")
        email_to_raw = request.form.get("email_to", "")

        # Parse "Name <email>, Name2 <email2>" format
        to_recipients = []
        for part in email_to_raw.split(","):
            part = part.strip()
            if "<" in part and ">" in part:
                name = part[:part.index("<")].strip()
                email = part[part.index("<")+1:part.index(">")].strip()
                to_recipients.append({"name": name, "email": email})
            elif "@" in part:
                to_recipients.append({"name": part, "email": part})

        # Convert plain text body to HTML
        body_html = email_body.replace("\n", "<br>")
        body_html = f"<p>{body_html}</p>"

        data["intelligence"]["follow_up_email"] = {
            "to_recipients": to_recipients,
            "from_email": data.get("organizer_email", ""),
            "subject": email_subject,
            "body_html": body_html,
            "body_text": email_body,
        }

# ======================================================================
    try:
        results = execute_approved_actions(approval_id, data)
        return render_template_string(RESULT_TEMPLATE, data=data,
                                      status_title="Actions Created Successfully",
            status_emoji="&#9989;", actions=results.get("actions", []))
    except Exception as e:
        logger.error(f"Execution error: {e}", exc_info=True)
        return render_template_string(RESULT_TEMPLATE, data=data,
                                      status_title="Error During Execution",
            status_emoji="&#10060;", actions=[str(e)])


@app.route("/review/<approval_id>/cancel", methods=["GET"])
def cancel_actions(approval_id: str):
    """Cancel  --  don't create anything."""
    pending = load_pending()
    if approval_id in pending:
        pending[approval_id]["status"] = "cancelled"
        save_pending(pending)
    return render_template_string(RESULT_TEMPLATE,
                                  data=pending.get(approval_id, {"title": "Unknown"}),
                                  status_title="Cancelled  --  No Actions Created",
        status_emoji="&#128683;", actions=[])


@app.route("/process/<transcript_id>", methods=["GET", "POST"])
def manual_process(transcript_id: str):
    """Manually trigger Phase 1 for a specific transcript (async)."""
    import threading

    def _do_process():
        try:
            transcript = get_transcript_by_id(transcript_id)
            if not transcript:
                logger.error(f"Transcript not found: {transcript_id}")
                return
            approval_id = process_transcript_phase1(transcript)
            logger.info(f"Phase 1 complete: approval_id={approval_id}")
        except Exception as e:
            logger.error(f"Background processing failed for {transcript_id}: {e}", exc_info=True)

    thread = threading.Thread(target=_do_process)
    thread.start()
    return jsonify({"status": "processing", "message": "Phase 1 started in background. Check your email for the approval link."})


# ======================================================================

def poll_and_process():
    logger.info("Polling Fireflies for new transcripts...")
    processed = load_processed()
    try:
        transcripts = get_recent_transcripts(since_minutes=POLL_INTERVAL_MINUTES + 10)
        for transcript in transcripts:
            tid = transcript["id"]
            if tid in processed:
                continue
            full_transcript = get_transcript_by_id(tid)
            if not full_transcript:
                continue
            process_transcript_phase1(full_transcript)
            processed.add(tid)
            save_processed(processed)
    except Exception as e:
        logger.error(f"Polling error: {e}", exc_info=True)


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(poll_and_process, "interval", minutes=POLL_INTERVAL_MINUTES)
    scheduler.start()
    logger.info(f"Scheduler started: polling every {POLL_INTERVAL_MINUTES} minutes")


# Start background services for gunicorn (module-level, not just __main__)
_start_lock = _threading.Lock()
def _start_background_services():
    with _start_lock:
        start_scheduler()
        if ASANA_PROJECT_GID and MS_GRAPH_CLIENT_ID:
            start_todo_poller()

_start_background_services()

# ======================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting Post-Meeting Intelligence Pipeline v2 on port {port}")
    app.run(host="0.0.0.0", port=port)





