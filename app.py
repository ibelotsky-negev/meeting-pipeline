"""
Post-Meeting Intelligence Pipeline v2
Fireflies → Claude AI → Approval UI → HubSpot + Asana + Outlook Draft

Flow:
1. Fireflies triggers (webhook or poll) when transcript is ready
2. Claude extracts intelligence (signals, action items, contacts, email draft)
3. Organizer receives approval link (email or Slack)
4. Organizer reviews tasks & email draft in a web UI — can edit, delete, or approve
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

# ─── Configuration ───────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# API Keys (set via environment variables)
FIREFLIES_API_KEY = os.environ["FIREFLIES_API_KEY"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]
ASANA_API_KEY = os.environ["ASANA_API_KEY"]

# Microsoft Graph (Outlook) — OAuth2 client credentials or delegated
MS_GRAPH_CLIENT_ID = os.environ.get("MS_GRAPH_CLIENT_ID", "")
MS_GRAPH_CLIENT_SECRET = os.environ.get("MS_GRAPH_CLIENT_SECRET", "")
MS_GRAPH_TENANT_ID = os.environ.get("MS_GRAPH_TENANT_ID", "")
MS_GRAPH_REFRESH_TOKEN = os.environ.get("MS_GRAPH_REFRESH_TOKEN", "")  # For delegated flow

# Configuration
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "")
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "")
HUBSPOT_OWNER_ID = os.environ.get("HUBSPOT_OWNER_ID", "")
POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8080")  # Your deployed URL
NOTIFY_VIA = os.environ.get("NOTIFY_VIA", "email")  # "email" (per-organizer), "teams" (shared ops channel), or "email,teams" for both
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "")  # Optional: shared ops channel for admin visibility
BOT_SENDER_EMAIL = os.environ.get("BOT_SENDER_EMAIL", "")  # e.g. sara@negevlabs.com (shared mailbox)
BOT_SENDER_NAME = os.environ.get("BOT_SENDER_NAME", "Sara - Negev Chief of Staff")

# Track processed transcripts and pending approvals
PROCESSED_FILE = "processed_transcripts.json"
PENDING_FILE = "pending_approvals.json"

app = Flask(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  PENDING APPROVALS STORE
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
#  FIREFLIES API
# ═══════════════════════════════════════════════════════════════════════════

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
            t_date = datetime.fromisoformat(t["dateString"].replace("Z", "+00:00"))
            if t_date >= cutoff:
                recent.append(t)
        except (ValueError, TypeError):
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


# ═══════════════════════════════════════════════════════════════════════════
#  CLAUDE AI — INTELLIGENCE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

def extract_meeting_intelligence(transcript: dict) -> dict:
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    summary = transcript.get("summary", {})
    sentences = transcript.get("sentences", [])
    transcript_text = "\n".join(
        [f"{s.get('speaker_name', 'Unknown')}: {s.get('text', '')}" for s in sentences]
    )

    # Business context — loaded from file if available, else env var, else default
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
- Participants: {', '.join(transcript.get('participants', []))}
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
            "due_context": "ASAP / next week / specific date if mentioned",
            "create_in": "both"
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
- Action items should be specific, actionable, and reflect what was actually committed to in the conversation — not generic tasks
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


# ═══════════════════════════════════════════════════════════════════════════
#  MICROSOFT GRAPH — OUTLOOK DRAFT CREATION
# ═══════════════════════════════════════════════════════════════════════════

MS_GRAPH_TOKEN_URL = f"https://login.microsoftonline.com/{MS_GRAPH_TENANT_ID}/oauth2/v2.0/token"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_ms_token_cache = {"token": None, "expires_at": 0}


def get_ms_graph_token() -> str:
    """Get Microsoft Graph access token (supports both delegated and app-only)."""
    now = time.time()
    if _ms_token_cache["token"] and _ms_token_cache["expires_at"] > now + 60:
        return _ms_token_cache["token"]

    if MS_GRAPH_REFRESH_TOKEN:
        # Delegated flow (send as specific user)
        data = {
            "client_id": MS_GRAPH_CLIENT_ID,
            "client_secret": MS_GRAPH_CLIENT_SECRET,
            "refresh_token": MS_GRAPH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
            "scope": "https://graph.microsoft.com/Mail.ReadWrite",
        }
    else:
        # App-only flow (requires Mail.ReadWrite application permission)
        data = {
            "client_id": MS_GRAPH_CLIENT_ID,
            "client_secret": MS_GRAPH_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }

    resp = requests.post(MS_GRAPH_TOKEN_URL, data=data, timeout=15)
    resp.raise_for_status()
    token_data = resp.json()

    _ms_token_cache["token"] = token_data["access_token"]
    _ms_token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _ms_token_cache["token"]


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
    # App-only: /users/{email}/messages
    # Delegated: /me/messages (if refresh token belongs to sender)
    if MS_GRAPH_REFRESH_TOKEN:
        url = f"{MS_GRAPH_BASE}/me/messages"
    else:
        url = f"{MS_GRAPH_BASE}/users/{sender_email}/messages"

    resp = requests.post(url, json=message_payload, headers=headers, timeout=30)
    resp.raise_for_status()
    draft = resp.json()

    logger.info(f"Created Outlook draft: '{subject}' in {sender_email}'s Drafts (ID: {draft.get('id', 'unknown')})")
    return draft


# ═══════════════════════════════════════════════════════════════════════════
#  HUBSPOT API
# ═══════════════════════════════════════════════════════════════════════════

HUBSPOT_BASE = "https://api.hubapi.com"


def hubspot_request(method: str, endpoint: str, data: dict = None) -> dict:
    headers = {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{HUBSPOT_BASE}{endpoint}"
    resp = requests.request(method, url, json=data, headers=headers, timeout=30)
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


def create_hubspot_contact(contact_info: dict) -> dict:
    properties = {
        "firstname": contact_info.get("name", "").split()[0] if contact_info.get("name") else "",
        "lastname": " ".join(contact_info.get("name", "").split()[1:]) if contact_info.get("name") else "",
        "email": contact_info.get("email", ""),
        "company": contact_info.get("company", ""),
        "jobtitle": contact_info.get("role", ""),
    }
    if HUBSPOT_OWNER_ID:
        properties["hubspot_owner_id"] = HUBSPOT_OWNER_ID
    properties = {k: v for k, v in properties.items() if v}
    result = hubspot_request("POST", "/crm/v3/objects/contacts", {"properties": properties})
    logger.info(f"Created HubSpot contact: {contact_info.get('name')} ({result.get('id')})")
    return result


def log_hubspot_note(contact_id: str, note_body: str, meeting_date: str) -> dict:
    data = {
        "properties": {"hs_timestamp": meeting_date, "hs_note_body": note_body},
        "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}],
    }
    return hubspot_request("POST", "/crm/v3/objects/notes", data)


def create_hubspot_task(contact_id: str, subject: str, body: str, due_date: str) -> dict:
    data = {
        "properties": {
            "hs_task_subject": subject, "hs_task_body": body,
            "hs_task_status": "NOT_STARTED", "hs_task_priority": "HIGH",
            "hs_timestamp": due_date,
        },
        "associations": [{"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 204}]}],
    }
    if HUBSPOT_OWNER_ID:
        data["properties"]["hubspot_owner_id"] = HUBSPOT_OWNER_ID
    return hubspot_request("POST", "/crm/v3/objects/tasks", data)


# ═══════════════════════════════════════════════════════════════════════════
#  ASANA API
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
#  NOTIFICATION — Alert organizer to review
# ═══════════════════════════════════════════════════════════════════════════

def notify_organizer(organizer_email: str, approval_id: str, meeting_title: str):
    """Send organizer a link to review and approve tasks + email draft.
    Supports multiple channels: email, slack, teams (comma-separated in NOTIFY_VIA)."""
    review_url = f"{APP_BASE_URL}/review/{approval_id}"
    channels = [c.strip().lower() for c in NOTIFY_VIA.split(",")]

    # ── Slack ──
    if "slack" in channels and SLACK_WEBHOOK_URL:
        try:
            requests.post(SLACK_WEBHOOK_URL, json={
                "text": (
                    f"📞 *Meeting processed: {meeting_title}*\n"
                    f"Review tasks & follow-up email before they're created:\n"
                    f"👉 <{review_url}|Review & Approve>"
                )
            }, timeout=10)
            logger.info(f"Slack notification sent for {meeting_title}")
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")

    # ── Teams (Incoming Webhook — posts to a channel) ──
    if "teams" in channels and TEAMS_WEBHOOK_URL:
        try:
            card_payload = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "contentUrl": None,
                        "content": {
                            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                            "type": "AdaptiveCard",
                            "version": "1.4",
                            "body": [
                                {
                                    "type": "TextBlock",
                                    "size": "medium",
                                    "weight": "bolder",
                                    "text": f"📞 Meeting Processed: {meeting_title}",
                                    "wrap": True,
                                },
                                {
                                    "type": "TextBlock",
                                    "text": "Claude extracted action items and a follow-up email draft. Review, edit, or delete before they're created in HubSpot, Asana, and Outlook.",
                                    "wrap": True,
                                    "spacing": "small",
                                },
                                {
                                    "type": "FactSet",
                                    "facts": [
                                        {"title": "Organizer", "value": organizer_email},
                                        {"title": "Status", "value": "⏳ Awaiting your review"},
                                    ],
                                },
                            ],
                            "actions": [
                                {
                                    "type": "Action.OpenUrl",
                                    "title": "✅ Review & Approve Tasks",
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
            logger.info(f"Teams webhook notification sent for {meeting_title}")
        except Exception as e:
            logger.warning(f"Teams webhook notification failed: {e}")

    # ── Email (via Outlook / Graph API) ──
    if "email" in channels and MS_GRAPH_CLIENT_ID:
        try:
            token = get_ms_graph_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            send_payload = {
                "message": {
                    "subject": f"✅ Review: Post-meeting tasks — {meeting_title}",
                    "body": {
                        "contentType": "HTML",
                        "content": (
                            f"<h3>📞 Meeting processed: {meeting_title}</h3>"
                            f"<p>Hi! I've extracted action items and drafted a follow-up email from your meeting.</p>"
                            f"<p><strong>Review, edit, or delete before they're created in HubSpot, Asana, and Outlook:</strong></p>"
                            f'<p><a href="{review_url}" style="background:#2563eb;color:white;padding:12px 24px;'
                            f'text-decoration:none;border-radius:6px;font-weight:bold;">Review & Approve Tasks</a></p>'
                            f"<p style='color:#666;font-size:12px;'>— {BOT_SENDER_NAME}</p>"
                        ),
                    },
                    "toRecipients": [{"emailAddress": {"address": organizer_email}}],
                },
            }
            # Send from Sara's shared mailbox if configured
            if BOT_SENDER_EMAIL:
                send_payload["message"]["from"] = {
                    "emailAddress": {"name": BOT_SENDER_NAME, "address": BOT_SENDER_EMAIL}
                }
            if MS_GRAPH_REFRESH_TOKEN:
                url = f"{MS_GRAPH_BASE}/me/sendMail"
            else:
                url = f"{MS_GRAPH_BASE}/users/{BOT_SENDER_EMAIL or organizer_email}/sendMail"
            requests.post(url, json=send_payload, headers=headers, timeout=15)
            logger.info(f"Email notification sent to {organizer_email} from {BOT_SENDER_EMAIL or 'self'}")
        except Exception as e:
            logger.warning(f"Email notification failed: {e}")

    logger.info(f"Review URL for '{meeting_title}': {review_url}")


# ═══════════════════════════════════════════════════════════════════════════
#  PIPELINE — Phase 1: Extract & Queue for Approval
# ═══════════════════════════════════════════════════════════════════════════

def process_transcript_phase1(transcript: dict) -> str:
    """
    Phase 1: Extract intelligence and queue for organizer approval.
    Returns the approval_id.
    """
    transcript_id = transcript["id"]
    title = transcript.get("title", "Unknown Meeting")
    meeting_date = transcript.get("dateString", datetime.now(timezone.utc).isoformat())
    organizer_email = transcript.get("organizer_email", "")

    logger.info(f"═══ Phase 1: Extracting intelligence for '{title}' ═══")

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
        "participants": transcript.get("participants", []),
        "intelligence": intelligence,
        "status": "pending",  # pending → approved → executed
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    pending = load_pending()
    pending[approval_id] = approval
    save_pending(pending)

    # Notify organizer
    notify_organizer(organizer_email, approval_id, title)

    logger.info(f"Phase 1 complete. Approval ID: {approval_id} — awaiting organizer review.")
    return approval_id


# ═══════════════════════════════════════════════════════════════════════════
#  PIPELINE — Phase 2: Execute Approved Actions
# ═══════════════════════════════════════════════════════════════════════════

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

    # ── HubSpot: Contacts + Notes ──
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
                results["actions"].append(f"✅ Found HubSpot contact: {contact['name']}")
            else:
                new_contact = create_hubspot_contact(contact)
                contact_ids[email] = new_contact["id"]
                results["actions"].append(f"✅ Created HubSpot contact: {contact['name']}")
        except Exception as e:
            results["actions"].append(f"❌ HubSpot contact failed ({email}): {e}")

    # Log meeting notes
    note_body = intelligence.get("hubspot_note", "Meeting processed via pipeline.")
    for email, cid in contact_ids.items():
        try:
            log_hubspot_note(cid, note_body, meeting_date)
            results["actions"].append(f"✅ Meeting note logged on {email}")
        except Exception as e:
            results["actions"].append(f"❌ Note failed for {email}: {e}")

    # ── Tasks (HubSpot + Asana) ──
    action_items = intelligence.get("action_items", [])
    for item in action_items:
        due = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%dT17:00:00Z")
        due_date = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")

        # HubSpot task
        for email, cid in contact_ids.items():
            try:
                create_hubspot_task(cid, item["task"], item.get("task", ""), due)
                results["actions"].append(f"✅ HubSpot task: {item['task'][:60]}")
            except Exception as e:
                results["actions"].append(f"❌ HubSpot task failed: {e}")
            break

        # Asana task
        try:
            notes = f"From meeting: {title}\nOwner: {item.get('owner', 'TBD')}\nPriority: {item.get('priority', 'medium')}\nDue: {item.get('due_context', 'TBD')}"
            # Look up Asana user by owner email, fallback to organizer
            assignee_gid = None
            owner_email = item.get("owner_email", "")
            if owner_email:
                assignee_gid = find_asana_user_by_email(owner_email)
            if not assignee_gid and organizer_email:
                assignee_gid = find_asana_user_by_email(organizer_email)
            create_asana_task(item["task"], notes, due_date, assignee_gid)
            results["actions"].append(f"✅ Asana task: {item['task'][:60]}")
        except Exception as e:
            results["actions"].append(f"❌ Asana task failed: {e}")

    # ── Outlook Draft ──
    follow_up = intelligence.get("follow_up_email", {})
    if follow_up and follow_up.get("to_recipients") and MS_GRAPH_CLIENT_ID:
        try:
            create_outlook_draft(
                sender_email=follow_up.get("from_email", organizer_email),
                to_recipients=follow_up["to_recipients"],
                subject=follow_up.get("subject", f"Following up — {title}"),
                body_html=follow_up.get("body_html", follow_up.get("body_text", "")),
            )
            results["actions"].append(f"✅ Outlook draft created in {organizer_email}'s Drafts")
        except Exception as e:
            results["actions"].append(f"❌ Outlook draft failed: {e}")

    # Mark as executed
    pending = load_pending()
    if approval_id in pending:
        pending[approval_id]["status"] = "executed"
        pending[approval_id]["results"] = results
        save_pending(pending)

    logger.info(f"Phase 2 complete for approval {approval_id}: {len(results['actions'])} actions")
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  REVIEW & APPROVAL WEB UI
# ═══════════════════════════════════════════════════════════════════════════

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
        .signals-list li::before { content: "→ "; color: #3b82f6; font-weight: bold; }
        .status-banner { padding: 16px; border-radius: 8px; text-align: center; font-weight: 600; }
        .status-executed { background: #dcfce7; color: #166534; }
        .status-pending { background: #dbeafe; color: #1e40af; }
    </style>
</head>
<body>
    <div class="container">
        {% if data.status == 'executed' %}
        <div class="card status-banner status-executed">
            ✅ Actions already executed for this meeting.
        </div>
        {% endif %}

        <div class="card">
            <h1>📞 {{ data.title }}</h1>
            <p class="subtitle">{{ (data.meeting_date|string)[:10] }} · Organizer: {{ data.organizer_email }}</p>

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
                <h2>📋 Action Items</h2>
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
                                </div>
                                <div style="flex:0.5;">
                                    <label>PRIORITY</label>
                                    <select name="task_priority_{{ loop.index0 }}" style="width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;">
                                        <option value="high" {{ 'selected' if item.get('priority') == 'high' }}>High</option>
                                        <option value="medium" {{ 'selected' if item.get('priority', 'medium') == 'medium' }}>Medium</option>
                                        <option value="low" {{ 'selected' if item.get('priority') == 'low' }}>Low</option>
                                    </select>
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
                <h2>✉️ Follow-Up Email Draft</h2>
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
                    <span style="font-size:13px; color:#64748b;">Skip email draft — don't create in Outlook</span>
                </label>
            </div>

            <!-- APPROVE / CANCEL -->
            {% if data.status == 'pending' %}
            <div class="card">
                <div class="actions-bar" style="border-top:none; margin-top:0; padding-top:0;">
                    <a href="/review/{{ data.id }}/cancel" class="btn btn-ghost">Cancel — Don't Create Anything</a>
                    <button type="submit" class="btn btn-primary">✅ Approve & Create Tasks + Draft</button>
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


# ═══════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


@app.route("/webhook/fireflies", methods=["POST"])
def fireflies_webhook():
    """Fireflies webhook — triggers Phase 1 (extract + queue for approval)."""
    import threading
    payload = request.get_json(force=True)
    logger.info(f"Webhook received: {json.dumps(payload)[:200]}")

    transcript_id = payload.get("transcriptId") or payload.get("data", {}).get("transcriptId")
    if not transcript_id:
        return jsonify({"error": "No transcript ID"}), 400

    processed = load_processed()
    if transcript_id in processed:
        return jsonify({"status": "already_processed"})

    def _do_webhook_process():
        try:
            transcript = get_transcript_by_id(transcript_id)
            if not transcript:
                logger.error(f"Transcript not found: {transcript_id}")
                return
            approval_id = process_transcript_phase1(transcript)
            proc = load_processed()
            proc.add(transcript_id)
            save_processed(proc)
            logger.info(f"Webhook Phase 1 complete: approval_id={approval_id}")
        except Exception as e:
            logger.error(f"Webhook background error: {e}", exc_info=True)

    thread = threading.Thread(target=_do_webhook_process)
    thread.start()
    return jsonify({"status": "processing", "message": "Phase 1 started in background."})


@app.route("/review/<approval_id>", methods=["GET"])
def review_page(approval_id: str):
    """Approval review page — organizer sees tasks + email draft and can edit/delete."""
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
                                      status_title="Already Processed", status_emoji="ℹ️", actions=[])

    # ── Parse edited tasks from form ──
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
            "priority": request.form.get(f"task_priority_{i}", "medium"),
            "due_context": data["intelligence"].get("action_items", [{}])[i].get("due_context", "1 week") if i < len(data["intelligence"].get("action_items", [])) else "1 week",
        })
    data["intelligence"]["action_items"] = approved_tasks

    # ── Parse edited email from form ──
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

    # ── Execute ──
    try:
        results = execute_approved_actions(approval_id, data)
        return render_template_string(RESULT_TEMPLATE, data=data,
                                      status_title="Actions Created Successfully",
                                      status_emoji="✅", actions=results.get("actions", []))
    except Exception as e:
        logger.error(f"Execution error: {e}", exc_info=True)
        return render_template_string(RESULT_TEMPLATE, data=data,
                                      status_title="Error During Execution",
                                      status_emoji="❌", actions=[str(e)])


@app.route("/review/<approval_id>/cancel", methods=["GET"])
def cancel_actions(approval_id: str):
    """Cancel — don't create anything."""
    pending = load_pending()
    if approval_id in pending:
        pending[approval_id]["status"] = "cancelled"
        save_pending(pending)
    return render_template_string(RESULT_TEMPLATE,
                                  data=pending.get(approval_id, {"title": "Unknown"}),
                                  status_title="Cancelled — No Actions Created",
                                  status_emoji="🚫", actions=[])


@app.route("/process/<transcript_id>", methods=["POST"])
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


# ─── Polling (Fallback) ─────────────────────────────────────────────────────

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


# ─── Startup ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    start_scheduler()
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting Post-Meeting Intelligence Pipeline v2 on port {port}")
    app.run(host="0.0.0.0", port=port)
