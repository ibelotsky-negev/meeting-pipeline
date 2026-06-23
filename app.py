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

import anthropic
import requests
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

# ======================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# Config and config-domain primitives live in config.py. Re-exported here
# so existing references and tests (app_module.X) keep resolving. ASCII-only.
from config import (FIREFLIES_API_KEY, CLAUDE_API_KEY, HUBSPOT_API_KEY, ASANA_API_KEY, MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, MS_GRAPH_TENANT_ID, MS_GRAPH_REFRESH_TOKEN, MS_GRAPH_AUTH_MODE, ASANA_WORKSPACE_GID, ASANA_PROJECT_GID, HUBSPOT_OWNER_ID, POLL_INTERVAL_MINUTES, APP_BASE_URL, NOTIFY_VIA, SLACK_WEBHOOK_URL, TEAMS_WEBHOOK_URL, BOT_SENDER_EMAIL, BOT_SENDER_NAME, INTERNAL_DOMAINS, HUBSPOT_OWNER_MAP_RAW, HUBSPOT_OWNER_MAP, TEAM_MEMBER_NAMES_RAW, TEAM_MEMBER_NAMES, TEAM_MEMBERS_LIST, EMAIL_ALIAS_MAP, EMAIL_ALIAS_MAP_RAW, DATA_DIR, PROCESSED_FILE, PENDING_FILE, SYNC_MAP_FILE, TODO_LIST_NAME, TODO_POLL_INTERVAL, RAILWAY_PUBLIC_URL, TEAMS_WEBHOOK_SECRET, TEAMS_TRANSCRIPT_ENABLED, TEAMS_ORGANIZER_USER_ID, TEAMS_POLL_USER_IDS, TEAMS_POLL_INTERVAL, SUBSCRIPTION_FILE, PULSE_RECIPIENTS, PULSE_SENDER, PULSE_DOMAINS, PULSE_ARCHIVE_DIR, PULSE_LOOKBACK_DAYS, BRIEFING_BOOK_PATH, BRIEFING_BOOK_REPO, PULSE_SKIP_SENDERS, PULSE_SKIP_DOMAINS, PULSE_SKIP_SUBJECTS, normalize_team_email, load_briefing_book, is_internal_email, resolve_internal_organizer)  # noqa: F401
from prompts import (PULSE_SCOPE, PULSE_ANTI_HALLUCINATION, PULSE_EMAIL_PROMPT, PULSE_TEAMS_PROMPT, PULSE_MEETINGS_PROMPT, PULSE_SYNTHESIS_PROMPT, PULSE_BRIEFING_UPDATE_PROMPT)  # noqa: F401
from templates import REVIEW_TEMPLATE, RESULT_TEMPLATE  # noqa: F401
from datetime_utils import to_hubspot_ms, to_graph_datetime, resolve_due_date  # noqa: F401
from stores import load_pending, save_pending, load_processed, save_processed, load_sync_map, save_sync_map  # noqa: F401
from fireflies_client import fireflies_query, get_recent_transcripts, get_transcript_by_id  # noqa: F401
from hubspot_client import (hubspot_request, find_hubspot_contact, _hubspot_owner_cache, resolve_hubspot_owner, create_hubspot_contact, get_contact_associations, log_hubspot_meeting, create_hubspot_task)  # noqa: F401
from asana_client import asana_request, create_asana_task, find_asana_user_by_email  # noqa: F401


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
# WEEKLY PULSE -> DATA COLLECTION
# ======================================================================

def pulse_get_team_users():
    """Get all users across negevlabs.com and ariadnebio.com domains."""
    token = get_ms_graph_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "ConsistencyLevel": "eventual"}
    users = []
    for domain in PULSE_DOMAINS:
        url = (f"{MS_GRAPH_BASE}/users?$filter=endsWith(mail,'@{domain}')"
               f"&$select=id,displayName,mail&$count=true&$top=999")
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for u in (data.get("value") or []):
                if u.get("mail"):
                    users.append({"id": u["id"], "displayName": u.get("displayName", ""),
                                  "mail": u["mail"]})
        except Exception as e:
            logger.warning(f"[pulse] Failed to enumerate users for {domain}: {e}")
    logger.info(f"[pulse] Found {len(users)} team members across {PULSE_DOMAINS}")
    return users


def pulse_should_skip_email(msg):
    """Pre-filter: skip automated, notification, calendar, and social emails."""
    subject = (msg.get("subject") or "").lower()
    from_addr = (msg.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
    # Skip by sender pattern
    if any(p in from_addr for p in PULSE_SKIP_SENDERS):
        return True
    # Skip by sender domain
    if any(d in from_addr for d in PULSE_SKIP_DOMAINS):
        return True
    # Skip by subject pattern
    if any(p in subject for p in PULSE_SKIP_SUBJECTS):
        return True
    # Skip very short subjects (likely automated)
    if len(subject.strip()) < 3:
        return True
    return False


def _pulse_has_team_in_from_or_to(msg):
    """Check if at least one team member (PULSE_DOMAINS) is in From or To fields.
    Emails where team is only in CC/BCC are excluded."""
    from_addr = (msg.get("from", {}).get("emailAddress", {}).get("address") or "").lower()
    if any(from_addr.endswith(f"@{d}") for d in PULSE_DOMAINS):
        return True
    for recip in (msg.get("toRecipients") or []):
        addr = (recip.get("emailAddress", {}).get("address") or "").lower()
        if any(addr.endswith(f"@{d}") for d in PULSE_DOMAINS):
            return True
    return False


def _pulse_fetch_user_emails(user, start_iso, end_iso, headers):
    """Fetch emails for a single user. Returns (emails_list, scanned_count, cc_skip_count)."""
    user_id = user["id"]
    url = (f"{MS_GRAPH_BASE}/users/{user_id}/messages"
           f"?$filter=receivedDateTime ge {start_iso} and receivedDateTime le {end_iso}"
           f"&$select=subject,bodyPreview,from,toRecipients,receivedDateTime,isRead"
           f"&$top=200&$orderby=receivedDateTime desc")
    emails = []
    scanned = 0
    cc_skipped = 0
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 403:
            logger.warning(f"[pulse] No Mail.Read permission for {user['mail']}, skipping")
            return emails, scanned, cc_skipped
        resp.raise_for_status()
        messages = resp.json().get("value") or []
        scanned = len(messages)
        for msg in messages:
            if pulse_should_skip_email(msg):
                continue
            if not _pulse_has_team_in_from_or_to(msg):
                cc_skipped += 1
                continue
            from_info = msg.get("from", {}).get("emailAddress", {})
            emails.append({
                "subject": msg.get("subject", ""),
                "bodyPreview": msg.get("bodyPreview", ""),
                "from_name": from_info.get("name", ""),
                "from_addr": from_info.get("address", ""),
                "date": msg.get("receivedDateTime", ""),
                "to_count": len(msg.get("toRecipients") or []),
            })
    except Exception as e:
        logger.warning(f"[pulse] Failed to fetch emails for {user['mail']}: {e}")
    return emails, scanned, cc_skipped


def pulse_collect_emails(start_dt, end_dt):
    """Collect business emails from all team mailboxes IN PARALLEL.
    Only bodyPreview is used -- no attachments are read or processed.
    Only includes emails where a team member is in From or To (not CC/BCC only)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    users = pulse_get_team_users()
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    token = get_ms_graph_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    all_emails = []
    total_scanned = 0
    skipped_cc_only = 0

    with ThreadPoolExecutor(max_workers=len(users)) as pool:
        futures = {pool.submit(_pulse_fetch_user_emails, u, start_iso, end_iso, headers): u for u in users}
        for future in as_completed(futures):
            emails, scanned, cc_skipped = future.result()
            all_emails.extend(emails)
            total_scanned += scanned
            skipped_cc_only += cc_skipped

    logger.info(f"[pulse] Emails: {total_scanned} scanned, {skipped_cc_only} skipped (CC/BCC only), {len(all_emails)} after filtering")
    return all_emails


def pulse_should_skip_teams_msg(msg):
    """Pre-filter: skip system messages, short messages, emoji-only."""
    if msg.get("messageType") != "message":
        return True
    import re
    body = (msg.get("body", {}).get("content") or "").strip()
    text = re.sub(r'<[^>]+>', '', body).strip()
    if len(text) < 20:
        return True
    return False


def _pulse_fetch_channel_messages(team_id, team_name, channel, start_iso, headers):
    """Fetch messages from a single channel. Returns list of message dicts."""
    import re
    channel_id = channel["id"]
    channel_name = channel.get("displayName", "")
    results = []
    try:
        msg_url = (f"{MS_GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages?$top=50")
        msg_resp = requests.get(msg_url, headers=headers, timeout=30)
        if msg_resp.status_code != 200:
            return results
        for msg in (msg_resp.json().get("value") or []):
            msg_date = msg.get("createdDateTime", "")
            if msg_date and msg_date < start_iso:
                continue
            if pulse_should_skip_teams_msg(msg):
                continue
            body = (msg.get("body", {}).get("content") or "").strip()
            text = re.sub(r'<[^>]+>', '', body).strip()
            results.append({
                "content_preview": text[:300],
                "chat_type": "channel",
                "channel_name": f"{team_name}/{channel_name}",
                "date": msg_date,
            })
    except Exception as e:
        logger.warning(f"[pulse] Channel msgs failed {team_name}/{channel_name}: {e}")
    return results


def _pulse_fetch_team_channels(team, start_iso, headers, pool):
    """Fetch all channel messages for a single team. Submits channel fetches to pool."""
    team_id = team["id"]
    team_name = team.get("displayName", "")
    futures = []
    try:
        ch_resp = requests.get(
            f"{MS_GRAPH_BASE}/teams/{team_id}/channels?$select=id,displayName",
            headers=headers, timeout=30)
        if ch_resp.status_code != 200:
            logger.warning(f"[pulse] Channels returned {ch_resp.status_code} for {team_name}")
            return futures
        channels = ch_resp.json().get("value") or []
        for channel in channels:
            futures.append(pool.submit(
                _pulse_fetch_channel_messages, team_id, team_name, channel, start_iso, headers))
    except Exception as e:
        logger.warning(f"[pulse] Channels list failed for {team_name}: {e}")
    return futures


def _pulse_fetch_user_chats(user, start_iso, headers, seen_chat_ids, seen_lock):
    """Fetch chat messages for a single user. Returns list of message dicts."""
    import re
    user_id = user["id"]
    results = []
    try:
        chats_url = (f"{MS_GRAPH_BASE}/users/{user_id}/chats"
                     f"?$select=id,chatType&$top=50")
        chats_resp = requests.get(chats_url, headers=headers, timeout=30)
        if chats_resp.status_code != 200:
            logger.warning(f"[pulse] Chats returned {chats_resp.status_code} for {user['mail']}")
            return results
        chats = chats_resp.json().get("value") or []
        for chat in chats:
            chat_id = chat["id"]
            # Thread-safe dedup
            with seen_lock:
                if chat_id in seen_chat_ids:
                    continue
                seen_chat_ids.add(chat_id)
            chat_type = chat.get("chatType", "unknown")
            if chat_type == "meeting":
                continue
            try:
                msg_url = f"{MS_GRAPH_BASE}/chats/{chat_id}/messages?$top=50"
                msg_resp = requests.get(msg_url, headers=headers, timeout=30)
                if msg_resp.status_code != 200:
                    continue
                for msg in (msg_resp.json().get("value") or []):
                    msg_date = msg.get("createdDateTime", "")
                    if msg_date and msg_date < start_iso:
                        continue
                    if pulse_should_skip_teams_msg(msg):
                        continue
                    body = (msg.get("body", {}).get("content") or "").strip()
                    text = re.sub(r'<[^>]+>', '', body).strip()
                    results.append({
                        "content_preview": text[:300],
                        "chat_type": chat_type,
                        "channel_name": "",
                        "date": msg_date,
                    })
            except Exception as e:
                logger.warning(f"[pulse] Chat messages failed {chat_id}: {e}")
    except Exception as e:
        logger.warning(f"[pulse] Chats list failed for {user['mail']}: {e}")
    return results


def pulse_collect_teams(start_dt, end_dt):
    """Collect Teams messages: channels + chats IN PARALLEL. Uses app-only compatible endpoints."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    token = get_ms_graph_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "ConsistencyLevel": "eventual"}
    all_messages = []
    channel_count = 0
    chat_count = 0

    # Use a shared pool for all concurrent Graph API calls (limit to 8 to avoid throttling)
    with ThreadPoolExecutor(max_workers=8) as pool:
        channel_futures = []
        chat_futures = []

        # 1. Channel messages: fetch groups list, then fan out channels in parallel
        try:
            groups_url = (f"{MS_GRAPH_BASE}/groups"
                          f"?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')"
                          f"&$select=id,displayName&$top=999")
            groups_resp = requests.get(groups_url, headers=headers, timeout=30)
            if groups_resp.status_code == 200:
                teams = groups_resp.json().get("value") or []
                logger.info(f"[pulse] Found {len(teams)} Teams via /groups")
                # Fan out: get channels for each team, then messages for each channel
                for team in teams:
                    team_futures = _pulse_fetch_team_channels(team, start_iso, headers, pool)
                    channel_futures.extend(team_futures)
            else:
                logger.warning(f"[pulse] Groups list returned {groups_resp.status_code}: "
                               f"{groups_resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[pulse] Teams channel collection failed: {e}")

        # 2. Chat messages per user -- fan out all users in parallel
        try:
            users = pulse_get_team_users()
            seen_chat_ids = set()
            seen_lock = threading.Lock()
            for user in users:
                chat_futures.append(pool.submit(
                    _pulse_fetch_user_chats, user, start_iso, headers, seen_chat_ids, seen_lock))
        except Exception as e:
            logger.warning(f"[pulse] Teams chat collection failed: {e}")

        # Collect all channel results
        for future in as_completed(channel_futures):
            msgs = future.result()
            all_messages.extend(msgs)
            channel_count += len(msgs)

        # Collect all chat results
        for future in as_completed(chat_futures):
            msgs = future.result()
            all_messages.extend(msgs)
            chat_count += len(msgs)

    logger.info(f"[pulse] Teams: {len(all_messages)} messages "
                f"({channel_count} channel, {chat_count} chat)")
    return all_messages


PULSE_TEAM_DOMAINS = {"negevlabs.com", "ariadnebio.com", "zirmania.com"}
# Note: negevcap.com excluded -- Negev Capital is out of pulse scope


def _pulse_is_team_meeting(participants):
    """Check if any participant email belongs to a team domain."""
    for p in (participants or []):
        email = (p if isinstance(p, str) else "").lower()
        domain = email.split("@")[-1] if "@" in email else ""
        if domain in PULSE_TEAM_DOMAINS:
            return True
    return False


def pulse_collect_meetings(start_dt, end_dt):
    """Collect meeting intelligence from Fireflies for the pulse period."""
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    meetings = []
    try:
        query = """
        query PulseMeetings($fromDate: DateTime, $toDate: DateTime, $limit: Int) {
            transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit) {
                id title date duration
                organizer_email
                participants
                summary { overview action_items shorthand_bullet }
            }
        }
        """
        data = fireflies_query(query, {
            "fromDate": start_iso,
            "toDate": end_iso,
            "limit": 50,
        })
        all_transcripts = data.get("transcripts") or []
        logger.info(f"[pulse] Fireflies returned {len(all_transcripts)} meetings for period {start_iso} - {end_iso}")
        for t in all_transcripts:
            # Filter: only include meetings where a team member participated
            organizer = (t.get("organizer_email") or "").lower()
            participants = t.get("participants") or []
            org_domain = organizer.split("@")[-1] if "@" in organizer else ""
            is_team = org_domain in PULSE_TEAM_DOMAINS or _pulse_is_team_meeting(participants)
            if not is_team:
                logger.debug(f"[pulse] Skipping meeting '{t.get('title')}' -- no team participants")
                continue

            summary = t.get("summary") or {}
            duration = t.get("duration")
            # Fireflies returns date as Unix timestamp (int) -- convert to ISO string
            raw_date = t.get("date", "")
            if isinstance(raw_date, (int, float)) and raw_date > 0:
                raw_date = datetime.fromtimestamp(raw_date / 1000, tz=timezone.utc).isoformat()
            ff_id = t.get("id", "")
            meetings.append({
                "title": t.get("title", ""),
                "date": str(raw_date),
                "duration_minutes": round(duration / 60) if duration else 0,
                "summary": summary.get("overview") or summary.get("shorthand_bullet") or "",
                "action_items": summary.get("action_items") or "",
                "fireflies_id": ff_id,
                "fireflies_url": f"https://app.fireflies.ai/view/{ff_id}" if ff_id else "",
            })
    except Exception as e:
        logger.warning(f"[pulse] Fireflies collection failed: {e}")
    logger.info(f"[pulse] Meetings: {len(meetings)} team transcripts collected (filtered from Fireflies results)")
    return meetings


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

    # Business context: briefing book (preferred) -> file -> env var -> default
    business_context = load_briefing_book()
    if not business_context:
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
    "internal_lead_email": "email@negevlabs.com -- the most active internal team member on this call (by speaking volume/engagement). Use this when the organizer is external to route ownership. Leave empty string if organizer is internal.",
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
- Internal domains are: {', '.join(INTERNAL_DOMAINS)}. Treat any participant whose email domain is in this list as internal; everyone else is external.
- For investor/BD meetings: capture interest signals, objections, and next steps that matter for deal flow
- For portfolio company meetings: capture strategic decisions, blockers, and deliverables
- Action items should be specific, actionable, and reflect what was actually committed to in the conversation   not generic tasks

FOLLOW-UP EMAIL RULES:
- ALWAYS return a non-empty follow_up_email object. Never return an empty object or omit the key, regardless of whether the meeting is internal-only or has external contacts.
- The follow-up email is FROM the organizer ({transcript.get('organizer_email', '')}) TO the other meeting participants (NEVER include the organizer in to_recipients).
- from_email must be the meeting organizer's email.
- body_html should use simple HTML (<p>, <br>, <strong>, <ul>, <li>) for Outlook rendering. body_text is the plain-text equivalent.
- Choose ONE of the two modes below based on whether any external participant is present:

  EXTERNAL MODE (at least one participant is external):
    * to_recipients = the key external participant(s) identified from the transcript speakers and discussion.
    * subject = professional follow-up subject, e.g. "Following up -- <topic>".
    * body = warm, professional follow-up addressed to the external recipient(s) by name (e.g. "Hi Sam"). Recap what was discussed, restate next steps, and include a clear CTA.
    * If a participant email is not known, use "unknown@placeholder.com" with their name so the organizer can fix it in the review UI.

  INTERNAL MODE (all participants are internal):
    * to_recipients = EVERY meeting participant whose email is not the organizer's. Include all internal teammates on the call.
    * subject = "{transcript.get('title', 'Meeting')} -- recap and action items" (literal two-hyphen separator, ASCII only).
    * body = terse, action-oriented recap for teammates. Open with the recap (no "Hi team" pleasantry). Format: one short paragraph summarizing key decisions, then a bulleted list of action items each prefixed with the owner's name (e.g. "- Shlomi: finalize DD memo by Fri"). Direct tone. Active voice. Banned-phrase list still applies.

- Identify ALL contacts (non-organizer attendees) from speaker names in the transcript, internal or external.
- internal_lead_email: When the organizer is external, identify which internal team member was MOST ACTIVE on the call (spoke most, drove the discussion). Set their email as internal_lead_email. If organizer is internal, set to empty string "".
- Rate interest level based on language, engagement, and commitments made
- Action items should be specific and assignable
- For each action item, set owner_email to the person's email if known from participants or organizer info. If the owner is the organizer, use their email. If unknown, leave as empty string.
- For due_days: convert relative time references from the conversation into integer days from the meeting date. Use: "ASAP"/"urgent"/"today"   1, "tomorrow"   1, "this week"/"few days"   3, "next week"   7, "couple weeks"   14, "end of month"   21, "next month"   30. If a specific date is mentioned, calculate the days difference from the meeting date. Default to 7 if unclear.
- For create_in: Route each task to the RIGHT system. Use "hubspot" for external/investor-facing tasks (follow-up emails, calls, scheduling meetings, sending materials to external contacts). Use "asana" for internal operational tasks (preparing documents, data rooms, reports, internal reviews, research). Most tasks should go to ONE system, not both.
- Return ONLY valid JSON, no markdown
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system="""You are drafting on behalf of a senior partner or team member at Negev Labs, a biotech venture studio. The sender communicates as a peer to investors, founders, and executives - never as someone asking for a favor. The tone is direct, warm, and authoritative regardless of who the sender is.

EMAIL TONE RULES - NON-NEGOTIABLE:
BANNED (never use):
- "Just checking in" -> use "Following up"
- "I just wanted to..." -> delete; open with the point
- "I hope this finds you well" -> delete; start with substance
- "Sorry to bother you" -> never apologize for outreach
- "Whenever you get a chance" -> use "By [date]" or omit
- "Would it be possible to..." -> use "I would like to" or "Lets"
- "I was wondering if..." -> state the ask directly
- "Any help would be greatly appreciated" -> state what you need
- "Please do not hesitate to reach out" -> use "Happy to discuss"
- "Looking forward to hearing from you" -> omit or replace with a clear CTA

REQUIRED:
- Open with the point, not a pleasantry
- Direct asks: "Can you send X by Friday?" not "Would you mind..."
- Active voice throughout
- Close with a clear next step or nothing at all
- Short sentences signal confidence""",
        messages=[{"role": "user", "content": prompt}],
    )
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0]
    intelligence = json.loads(response_text)

    # Safety net: always ensure follow_up_email is populated so the review
    # page has a draft to show. Fires when Claude returns empty/missing data.
    organizer_email = transcript.get("organizer_email", "") or ""
    title = transcript.get("title", "Meeting") or "Meeting"
    participants = transcript.get("participants") or []
    follow_up = intelligence.get("follow_up_email") or {}

    others = [
        p for p in participants
        if p and "@" in p and p.lower() != organizer_email.lower()
    ]

    if not follow_up.get("to_recipients"):
        follow_up["to_recipients"] = [
            {"name": p.split("@")[0], "email": p} for p in others
        ]
    if not follow_up.get("from_email"):
        follow_up["from_email"] = organizer_email
    if not follow_up.get("subject"):
        follow_up["subject"] = f"{title} -- recap and action items"
    if not follow_up.get("body_text"):
        short_summary = (transcript.get("summary") or {}).get("short_summary", "") or ""
        action_items = intelligence.get("action_items") or []
        bullet_lines = []
        for item in action_items:
            owner = item.get("owner") or item.get("owner_email") or "Unassigned"
            task_text = item.get("task", "")
            if task_text:
                bullet_lines.append(f"- {owner}: {task_text}")
        body_lines = []
        if short_summary:
            body_lines.append(short_summary.strip())
        if bullet_lines:
            if body_lines:
                body_lines.append("")
            body_lines.append("Action items:")
            body_lines.extend(bullet_lines)
        follow_up["body_text"] = "\n".join(body_lines) if body_lines else f"Recap of {title}."
    if not follow_up.get("body_html"):
        html_lines = []
        text_lines = follow_up["body_text"].split("\n")
        in_list = False
        for line in text_lines:
            if line.startswith("- "):
                if not in_list:
                    html_lines.append("<ul>")
                    in_list = True
                html_lines.append(f"<li>{line[2:]}</li>")
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                if line.strip():
                    html_lines.append(f"<p>{line}</p>")
                else:
                    html_lines.append("<br>")
        if in_list:
            html_lines.append("</ul>")
        follow_up["body_html"] = "".join(html_lines)

    intelligence["follow_up_email"] = follow_up
    return intelligence


# ======================================================================
# WEEKLY PULSE -> ANALYSIS PIPELINE (MULTI-PASS CLAUDE)
# ======================================================================

def _pulse_format_emails(email_data):
    """Format email data for Claude prompt."""
    lines = []
    for i, e in enumerate(email_data, 1):
        lines.append(f"{i}. [{e['date'][:10]}] Subject: {e['subject']}")
        if e.get("bodyPreview"):
            lines.append(f"   Preview: {e['bodyPreview'][:255]}")
        lines.append(f"   From: {e['from_addr']} | To count: {e['to_count']}")
    return "\n".join(lines) if lines else "(No emails collected)"


def _pulse_format_teams(teams_data):
    """Format Teams data for Claude prompt."""
    lines = []
    for i, m in enumerate(teams_data, 1):
        source = m.get("channel_name") or m.get("chat_type", "chat")
        lines.append(f"{i}. [{m['date'][:10]}] ({source}): {m['content_preview']}")
    return "\n".join(lines) if lines else "(No Teams messages collected)"


def _pulse_format_meetings(meeting_data):
    """Format meeting data for Claude prompt, including Fireflies links."""
    lines = []
    for i, m in enumerate(meeting_data, 1):
        date_str = str(m.get('date', ''))[:10] or 'unknown'
        ff_url = m.get("fireflies_url", "")
        link_part = f" | Recording: {ff_url}" if ff_url else ""
        lines.append(f"{i}. [{date_str}] {m['title']} ({m['duration_minutes']}min){link_part}")
        if m.get("summary"):
            lines.append(f"   Summary: {m['summary']}")
        if m.get("action_items"):
            lines.append(f"   Action items: {m['action_items']}")
    return "\n".join(lines) if lines else "(No meetings collected)"


def _pulse_build_meeting_links(meeting_data):
    """Build a meeting title -> Fireflies URL reference for the synthesis prompt."""
    links = []
    for m in meeting_data:
        ff_url = m.get("fireflies_url", "")
        if ff_url:
            date_str = str(m.get('date', ''))[:10] or 'unknown'
            links.append(f"- [{m['title']}]({ff_url}) ({date_str})")
    return "\n".join(links) if links else "(No meeting recordings available)"


PULSE_MAX_INPUT_CHARS = 80000  # ~20K tokens at ~4 chars/token


def _pulse_truncate_input(text, max_chars=PULSE_MAX_INPUT_CHARS):
    """Truncate text to stay under ~20K token limit for a single analysis pass."""
    if len(text) <= max_chars:
        return text
    logger.warning(f"[pulse] Truncating input from {len(text)} to {max_chars} chars (~20K tokens)")
    return text[:max_chars] + "\n\n[... TRUNCATED -- input exceeded 20K token limit ...]"


PULSE_MODEL_EXTRACT = "claude-sonnet-4-6"    # Passes 1-3: signal extraction
PULSE_MODEL_SYNTHESIZE = "claude-opus-4-8"   # Pass 4: synthesis (stronger reasoning)


def _pulse_call_claude(prompt_text, model=None, use_briefing=True):
    """Call Claude API for pulse analysis. Returns raw response text.
    Injects briefing book as system prompt for company context."""
    prompt_text = _pulse_truncate_input(prompt_text)
    use_model = model or PULSE_MODEL_EXTRACT
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    logger.info(f"[pulse] Calling Claude model={use_model}, input_len={len(prompt_text)}")

    # Build system prompt with briefing book context
    system_parts = []
    if use_briefing:
        briefing = load_briefing_book()
        if briefing:
            system_parts.append(f"COMPANY CONTEXT:\n{briefing}")

    kwargs = {
        "model": use_model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt_text}],
    }
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)

    response = client.messages.create(**kwargs)
    return response.content[0].text


def _pulse_parse_json(raw_text):
    """Parse JSON from Claude response, stripping markdown fences if present."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("[pulse] Failed to parse Claude JSON, returning raw text")
        return {"green": [], "yellow": [], "red": [], "key_entities": [], "_raw": text}


def pulse_analyze(email_data, teams_data, meeting_data, period_start, period_end):
    """Run 4-pass Claude analysis. Returns (report_markdown, raw_signals_dict)."""
    logger.info("[pulse] Starting 4-pass analysis pipeline")

    rate_limit_delay = 65  # seconds between Claude calls to stay under 30K TPM

    # Pass 1: Email signals
    logger.info(f"[pulse] Pass 1/4: Analyzing {len(email_data)} emails")
    email_prompt = PULSE_EMAIL_PROMPT.replace("{emails_text}", _pulse_format_emails(email_data))
    email_signals = _pulse_parse_json(_pulse_call_claude(email_prompt))
    logger.info(f"[pulse] Pass 1 complete: {len(email_signals.get('green', []))}G "
                f"{len(email_signals.get('yellow', []))}Y {len(email_signals.get('red', []))}R")

    # Rate-limit pause
    logger.info(f"[pulse] Waiting {rate_limit_delay}s for rate limit...")
    import time
    time.sleep(rate_limit_delay)

    # Pass 2: Teams signals
    logger.info(f"[pulse] Pass 2/4: Analyzing {len(teams_data)} Teams messages")
    teams_prompt = PULSE_TEAMS_PROMPT.replace("{teams_text}", _pulse_format_teams(teams_data))
    teams_signals = _pulse_parse_json(_pulse_call_claude(teams_prompt))
    logger.info(f"[pulse] Pass 2 complete: {len(teams_signals.get('green', []))}G "
                f"{len(teams_signals.get('yellow', []))}Y {len(teams_signals.get('red', []))}R")

    # Rate-limit pause
    logger.info(f"[pulse] Waiting {rate_limit_delay}s for rate limit...")
    time.sleep(rate_limit_delay)

    # Pass 3: Meeting signals
    logger.info(f"[pulse] Pass 3/4: Analyzing {len(meeting_data)} meetings")
    meetings_prompt = PULSE_MEETINGS_PROMPT.replace("{meetings_text}", _pulse_format_meetings(meeting_data))
    meeting_signals = _pulse_parse_json(_pulse_call_claude(meetings_prompt))
    logger.info(f"[pulse] Pass 3 complete: {len(meeting_signals.get('green', []))}G "
                f"{len(meeting_signals.get('yellow', []))}Y {len(meeting_signals.get('red', []))}R")

    # Collect all key entities for synthesis
    all_entities = set()
    for signals in [email_signals, teams_signals, meeting_signals]:
        all_entities.update(signals.get("key_entities") or [])

    # Rate-limit pause
    logger.info(f"[pulse] Waiting {rate_limit_delay}s for rate limit...")
    time.sleep(rate_limit_delay)

    # Pass 4: Synthesis
    logger.info("[pulse] Pass 4/4: Synthesizing final report")
    date_range = f"{period_start.strftime('%b %d')} - {period_end.strftime('%b %d, %Y')}"
    synthesis_prompt = (PULSE_SYNTHESIS_PROMPT
        .replace("{date_range}", date_range)
        .replace("{email_count}", str(len(email_data)))
        .replace("{teams_count}", str(len(teams_data)))
        .replace("{meetings_count}", str(len(meeting_data)))
        .replace("{entities}", ", ".join(sorted(all_entities)) if all_entities else "none detected")
        .replace("{meeting_links}", _pulse_build_meeting_links(meeting_data))
        .replace("{email_json}", json.dumps(email_signals, indent=2))
        .replace("{teams_json}", json.dumps(teams_signals, indent=2))
        .replace("{meetings_json}", json.dumps(meeting_signals, indent=2))
    )
    # Append Ken's standing corrections (e.g. the Ariadne fundraising structure)
    # as authoritative overrides so the synthesis does not repeat known mistakes.
    try:
        import sara_corrections
        synthesis_prompt += "\n\n" + sara_corrections.corrections_block()
    except Exception as e:
        logger.warning(f"[pulse] Could not load standing corrections (non-fatal): {e}")
    report = _pulse_call_claude(synthesis_prompt, model=PULSE_MODEL_SYNTHESIZE)
    logger.info("[pulse] Synthesis complete")

    # Pass 5: Briefing book update proposals
    all_signals = {
        "email_signals": email_signals,
        "teams_signals": teams_signals,
        "meeting_signals": meeting_signals,
    }
    briefing_updates = []
    try:
        logger.info(f"[pulse] Waiting {rate_limit_delay}s for rate limit...")
        time.sleep(rate_limit_delay)
        logger.info("[pulse] Pass 5/5: Proposing briefing book updates")
        briefing_updates = _pulse_propose_briefing_updates(all_signals)
    except Exception as e:
        logger.warning(f"[pulse] Briefing update pass failed (non-fatal): {e}")

    logger.info("[pulse] Analysis pipeline complete")

    return report, all_signals, briefing_updates


def _pulse_propose_briefing_updates(all_signals):
    """Pass 5: Propose briefing book updates based on this week's signals."""
    briefing = load_briefing_book()
    if not briefing:
        logger.warning("[pulse] No briefing book to update")
        return []

    prompt = (PULSE_BRIEFING_UPDATE_PROMPT
        .replace("{briefing_book}", briefing)
        .replace("{all_signals_json}", json.dumps(all_signals, indent=2))
    )
    raw = _pulse_call_claude(prompt, use_briefing=False)  # don't inject briefing as system too
    parsed = _pulse_parse_json(raw)
    updates = parsed.get("proposed_updates") or []
    logger.info(f"[pulse] Briefing update proposals: {len(updates)} "
                f"({sum(1 for u in updates if u.get('confidence') == 'high')} high, "
                f"{sum(1 for u in updates if u.get('confidence') == 'medium')} medium)")
    return updates


def pulse_update_briefing_book(proposed_updates):
    """Apply high-confidence updates to the briefing book. Returns list of applied updates."""
    import re as _re
    if not os.path.exists(BRIEFING_BOOK_PATH):
        logger.warning("[pulse] No briefing book file to update")
        return []

    with open(BRIEFING_BOOK_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    applied = []
    for update in (proposed_updates or []):
        if update.get("confidence") != "high":
            continue
        old = update.get("current", "")
        new = update.get("proposed", "")
        if old and old in content:
            content = content.replace(old, new, 1)
            applied.append(update)
            logger.info(f"[pulse] Briefing book updated: {update.get('section', 'unknown section')}")
        else:
            logger.warning(f"[pulse] Briefing update skipped -- exact text not found: {old[:80]}...")

    if applied:
        # Update the "Last Updated" date
        today = datetime.now(timezone.utc).strftime("%B %d, %Y")
        content = _re.sub(
            r"## Last Updated:.*",
            f"## Last Updated: {today}",
            content
        )
        with open(BRIEFING_BOOK_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[pulse] Applied {len(applied)} briefing book updates")

    return applied


# ======================================================================
# WEEKLY PULSE -> DELIVERY (EMAIL + ARCHIVE)
# ======================================================================

def _pulse_markdown_to_html(md_text):
    """Convert pulse markdown to styled HTML email body."""
    import re
    lines = md_text.split("\n")
    html_parts = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append("<br>")
            continue
        # H2 headers
        if stripped.startswith("## "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[3:]
            html_parts.append(f'<h2 style="color:#1a1a1a;font-size:22px;margin:24px 0 8px 0;">{text}</h2>')
            continue
        # H3 headers with color coding
        if stripped.startswith("### "):
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = stripped[4:]
            color = "#1a1a1a"
            if "green" in text.lower() or "wins" in text.lower():
                color = "#2e7d32"
            elif "yellow" in text.lower() or "watch" in text.lower():
                color = "#f9a825"
            elif "red" in text.lower() or "critical" in text.lower():
                color = "#c62828"
            html_parts.append(f'<h3 style="color:{color};font-size:17px;margin:20px 0 6px 0;">{text}</h3>')
            continue
        # Numbered list items
        num_match = re.match(r'^(\d+)\.\s+(.+)', stripped)
        if num_match:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            text = num_match.group(2)
            text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            html_parts.append(f'<p style="margin:4px 0 4px 16px;">{num_match.group(1)}. {text}</p>')
            continue
        # Bullet list items
        if stripped.startswith("- "):
            if not in_list:
                html_parts.append('<ul style="margin:4px 0;padding-left:24px;">')
                in_list = True
            text = stripped[2:]
            text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            html_parts.append(f'<li style="margin:3px 0;">{text}</li>')
            continue
        # Horizontal rule
        if stripped == "---":
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            html_parts.append('<hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">')
            continue
        # Plain text
        if in_list:
            html_parts.append("</ul>")
            in_list = False
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', stripped)
        html_parts.append(f'<p style="margin:4px 0;">{text}</p>')
    if in_list:
        html_parts.append("</ul>")

    body = "\n".join(html_parts)
    return (
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;'
        'line-height:1.6;color:#1a1a1a;max-width:680px;margin:0 auto;padding:16px;">'
        f'{body}'
        '</div>'
    )


def pulse_send_email(report_markdown, period_start, period_end):
    """Send pulse report via Microsoft Graph to all PULSE_RECIPIENTS."""
    subject = f"Weekly Pulse: {period_start.strftime('%b %d')} - {period_end.strftime('%b %d')}"
    html_body = _pulse_markdown_to_html(report_markdown)
    html_body = strip_emojis(html_body)

    token = get_ms_graph_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    to_list = [{"emailAddress": {"address": r}} for r in PULSE_RECIPIENTS]
    send_payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": to_list,
            "from": {"emailAddress": {"name": BOT_SENDER_NAME, "address": PULSE_SENDER}},
        },
    }
    url = f"{MS_GRAPH_BASE}/users/{PULSE_SENDER}/sendMail"
    resp = requests.post(url, json=send_payload, headers=headers, timeout=30)
    resp.raise_for_status()
    logger.info(f"[pulse] Email sent to {PULSE_RECIPIENTS} from {PULSE_SENDER}")


def pulse_archive(report_markdown, raw_signals, period_start, period_end, stats):
    """Save pulse to /data/pulse/ for trend tracking."""
    os.makedirs(PULSE_ARCHIVE_DIR, exist_ok=True)
    iso_year, iso_week, _ = period_end.isocalendar()
    filename = f"{iso_year}-W{iso_week:02d}.json"
    filepath = os.path.join(PULSE_ARCHIVE_DIR, filename)
    archive = {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "1.0",
        "stats": stats,
        "signals": raw_signals,
        "report_markdown": report_markdown,
    }
    with open(filepath, "w") as f:
        json.dump(archive, f, indent=2, default=str)
    logger.info(f"[pulse] Archived to {filepath}")
    return filepath


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
#  ASANA <-> MICROSOFT TO-DO BIDIRECTIONAL SYNC
# ======================================================================

import threading as _threading

_todo_poller_running = False
_todo_last_poll_time = None


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
                logger.warning(f"[todo-sync] Graph {method} {url} -> {resp.status_code}, retrying (attempt {attempt+1}/3)")
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
        graph_dt = to_graph_datetime(due_date)
        logger.info(f"[todo_task] due_date input='{due_date}' graph_dt='{graph_dt}'")
        if graph_dt:
            body["dueDateTime"] = {"dateTime": graph_dt, "timeZone": "UTC"}
        else:
            logger.warning(f"[todo_task] invalid due_date='{due_date}', skipping date")
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
        graph_dt = to_graph_datetime(due_date)
        logger.info(f"[todo_task] due_date input='{due_date}' graph_dt='{graph_dt}'")
        if graph_dt:
            body["dueDateTime"] = {"dateTime": graph_dt, "timeZone": "UTC"}
        else:
            logger.warning(f"[todo_task] invalid due_date='{due_date}', skipping date")
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
    # Safety: never send notification emails outside internal domains
    recipients = [r for r in recipients if is_internal_email(r)]
    if not recipients:
        logger.warning(f"[notify] No internal recipients after filtering -- skipping notification for {meeting_title}")
        return
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
                    f"{n_tasks} action items | {n_contacts} contacts"
                    f"{' | email draft ready' if has_email else ''}\n"
                    f"<{review_url}|Review & Approve>"
                )
            }, timeout=10)
            logger.info(f"Slack notification sent for {clean_title}")
        except Exception as e:
            logger.warning(f"Slack notification failed: {e}")

# ======================================================================
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
    raw_organizer = transcript.get("organizer_email", "")
    participants = transcript.get("participants") or []

    logger.info(f"=== Phase 1: Extracting intelligence for '{title}' ===")

    # Extract intelligence via Claude
    intelligence = extract_meeting_intelligence(transcript)

    # Resolve internal organizer (handles external organizer -> most active internal speaker)
    internal_lead = intelligence.get("internal_lead_email", "")
    organizer_email = resolve_internal_organizer(raw_organizer, participants, internal_lead)
    if organizer_email != raw_organizer:
        logger.info(f"[phase1] Organizer resolved: {raw_organizer} -> {organizer_email}")

    # Normalize owner emails in action items to canonical team emails
    # e.g. shlomi@ariadnebio.com -> shlomi@negevlabs.com
    for item in intelligence.get("action_items", []):
        raw_email = item.get("owner_email", "")
        if raw_email:
            normalized = normalize_team_email(raw_email)
            if normalized != raw_email:
                logger.info(f"[phase1] Normalized owner_email: {raw_email} -> {normalized}")
            item["owner_email"] = normalized

    # Create approval record
    approval_id = str(uuid.uuid4())[:8]
    approval = {
        "id": approval_id,
        "transcript_id": transcript_id,
        "title": title,
        "meeting_date": meeting_date,
        "organizer_email": organizer_email,
        "raw_organizer_email": raw_organizer,
        "participants": participants,
        "intelligence": intelligence,
        "status": "pending",  # pending -> approved -> executed
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    pending = load_pending()
    pending[approval_id] = approval
    save_pending(pending)

    # Notify ONLY the resolved internal organizer (not all participants)
    # Sara is a tool for the meeting owner, not a broadcast system
    logger.info(f"[phase1] Notifying organizer only: {organizer_email}")
    notify_organizer([organizer_email], approval_id, title, intelligence, meeting_date)

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
        # Normalize in case approve handler or manual edit reintroduced an alias
        owner_email = normalize_team_email(owner_email)
        logger.info(f"[execute] Task '{item.get('task', '')[:50]}' owner_email={owner_email}")

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
                    if not assignee_gid:
                        logger.warning(f"[execute] Asana user lookup FAILED for '{owner_email}', falling back to organizer")
                if not assignee_gid and organizer_email:
                    assignee_gid = find_asana_user_by_email(organizer_email)
                    logger.info(f"[execute] Asana fallback to organizer: {organizer_email} -> gid={assignee_gid}")
                else:
                    logger.info(f"[execute] Asana assigned to owner: {owner_email} -> gid={assignee_gid}")
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

# ======================================================================
#  WEEKLY PULSE ENDPOINTS
# ======================================================================

PULSE_STATUS_FILE = os.path.join(DATA_DIR, "pulse_status.json")
PULSE_LOCK_FILE = os.path.join(DATA_DIR, "pulse_lock.json")
PULSE_RUNNING_LOCK_FILE = os.path.join(DATA_DIR, "pulse_running.lock")
PULSE_LOCK_DURATION = 6 * 3600  # 6 hours -- prevents a second run within 6h of a completed one
PULSE_RUNNING_LOCK_MAX_AGE = 90 * 60  # treat running lock older than 90min as orphaned (max run ~5min)


def _pulse_save_status(status_data):
    """Save pulse run status to disk for polling."""
    try:
        with open(PULSE_STATUS_FILE, "w") as f:
            json.dump(status_data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"[pulse] Failed to save status: {e}")


def _read_pulse_lock():
    """Return the current pulse lock contents, or None if absent/corrupt."""
    try:
        with open(PULSE_LOCK_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def pulse_can_run():
    """Check if enough time has passed since the last completed pulse run.

    Persistent file-based guard (/data/pulse_lock.json survives container restarts).
    Prevents duplicate runs caused by APScheduler misfire_grace_time + restart,
    heartbeat re-registration, or stale browser tabs hitting /pulse/trigger.
    """
    lock_data = _read_pulse_lock()
    if lock_data:
        last_completed = lock_data.get("completed_at", 0)
        elapsed = time.time() - last_completed
        if elapsed < PULSE_LOCK_DURATION:
            logger.info(
                f"[pulse] Skipping -- last run completed {elapsed/60:.0f}min ago "
                f"(lock: {PULSE_LOCK_DURATION/3600:.0f}h)"
            )
            return False
    return True


def pulse_set_lock():
    """Record that a pulse run just completed, to block duplicates for PULSE_LOCK_DURATION."""
    try:
        with open(PULSE_LOCK_FILE, "w") as f:
            json.dump({
                "completed_at": time.time(),
                "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
            }, f)
    except Exception as e:
        logger.error(f"[pulse] Failed to write lock file: {e}")


_pulse_lock = _threading.Lock()


def _acquire_running_lock():
    """Atomically claim the cross-process running lock.

    Uses O_CREAT|O_EXCL so only one process succeeds even if 2 gunicorn workers
    fire pulse_weekly_run at the same instant. Stale locks (older than
    PULSE_RUNNING_LOCK_MAX_AGE) are reclaimed automatically.

    Returns True if acquired, False if another run is already in progress.
    """
    try:
        existing_age = time.time() - os.path.getmtime(PULSE_RUNNING_LOCK_FILE)
        if existing_age > PULSE_RUNNING_LOCK_MAX_AGE:
            logger.warning(f"[pulse] Removing stale running lock (age {existing_age/60:.0f}min)")
            try:
                os.remove(PULSE_RUNNING_LOCK_FILE)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    try:
        fd = os.open(PULSE_RUNNING_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        payload = json.dumps({
            "pid": os.getpid(),
            "started_at": time.time(),
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        })
        os.write(fd, payload.encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_running_lock():
    try:
        os.remove(PULSE_RUNNING_LOCK_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"[pulse] Failed to release running lock: {e}")


def _pulse_run_background(days, dry_run):
    """Run the full pulse pipeline in a background thread. Only one run at a time.
    Returns True if the run executed, False if it was skipped."""
    # Cross-process atomic lock -- prevents duplicate runs when 2 gunicorn workers
    # fire pulse_weekly_run simultaneously. The in-process _pulse_lock alone is
    # not enough because each worker process has its own.
    if not _acquire_running_lock():
        logger.warning("[pulse] Skipping -- another pulse run already in progress (cross-process)")
        return False

    # In-process lock for manual trigger + scheduler in the same process.
    if not _pulse_lock.acquire(blocking=False):
        logger.warning("[pulse] Skipping -- another pulse run is already in progress (in-process)")
        _release_running_lock()
        return False

    try:
        _pulse_run_inner(days, dry_run)
        return True
    finally:
        _pulse_lock.release()
        _release_running_lock()


def _pulse_run_inner(days, dry_run):
    """Inner pulse runner (called with lock held)."""
    import traceback as tb
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    status = {"run_id": run_id, "phase": "starting", "dry_run": dry_run, "days": days,
              "started_at": datetime.now(timezone.utc).isoformat()}
    _pulse_save_status(status)

    try:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)

        # Phase 1: collect ALL sources in parallel
        from concurrent.futures import ThreadPoolExecutor
        status["phase"] = "collecting (emails + Teams + meetings in parallel)"
        _pulse_save_status(status)

        with ThreadPoolExecutor(max_workers=3) as collector_pool:
            email_future = collector_pool.submit(pulse_collect_emails, start_dt, end_dt)
            teams_future = collector_pool.submit(pulse_collect_teams, start_dt, end_dt)
            meetings_future = collector_pool.submit(pulse_collect_meetings, start_dt, end_dt)

            emails = email_future.result()
            status["emails_count"] = len(emails)
            _pulse_save_status(status)

            teams = teams_future.result()
            status["teams_count"] = len(teams)
            _pulse_save_status(status)

            meetings = meetings_future.result()
            status["meetings_count"] = len(meetings)
            _pulse_save_status(status)

        stats = {
            "emails_scanned": len(emails),
            "teams_messages_scanned": len(teams),
            "meetings_analyzed": len(meetings),
        }
        status["stats"] = stats

        # Phase 2: analyze (5 Claude passes with rate-limit sleeps)
        status["phase"] = "analyzing (5 Claude passes, ~5 min)"
        _pulse_save_status(status)
        report, raw_signals, briefing_updates = pulse_analyze(emails, teams, meetings, start_dt, end_dt)
        status["report_preview"] = report[:1000] if report else ""
        status["briefing_updates_proposed"] = len(briefing_updates)

        # Phase 2b: apply briefing book updates (high confidence only)
        applied_updates = []
        if not dry_run and briefing_updates:
            status["phase"] = "updating briefing book"
            _pulse_save_status(status)
            try:
                applied_updates = pulse_update_briefing_book(briefing_updates)
                status["briefing_updates_applied"] = len(applied_updates)
            except Exception as e:
                logger.warning(f"[pulse] Briefing book update failed (non-fatal): {e}")

        # Append briefing update section to report if any updates were proposed
        if briefing_updates:
            report += "\n\n### Briefing Book Updates"
            high = [u for u in briefing_updates if u.get("confidence") == "high"]
            medium = [u for u in briefing_updates if u.get("confidence") == "medium"]
            if applied_updates and not dry_run:
                report += "\n**Applied this week:**"
                for u in applied_updates:
                    report += f"\n- {u.get('section', '?')}: {u.get('current', '')!r} -> {u.get('proposed', '')!r}"
            elif high:
                report += "\n**Proposed (dry run -- not applied):**"
                for u in high:
                    report += f"\n- {u.get('section', '?')}: {u.get('current', '')!r} -> {u.get('proposed', '')!r}"
            if medium:
                report += "\n**Flagged for review (medium confidence):**"
                for u in medium:
                    report += f"\n- {u.get('section', '?')}: {u.get('evidence', '')}"

        # Phase 3: deliver
        email_sent = False
        archived = False
        if not dry_run:
            status["phase"] = "sending email"
            _pulse_save_status(status)
            # Guard: even within a single run, ensure pulse_send_email is reached
            # exactly once. Belt-and-braces alongside the cross-process lock.
            if not email_sent:
                try:
                    pulse_send_email(report, start_dt, end_dt)
                    email_sent = True
                except Exception as e:
                    logger.error(f"[pulse] Email send failed: {e}", exc_info=True)
                    status["email_error"] = str(e)

            status["phase"] = "archiving"
            _pulse_save_status(status)
            try:
                # Include briefing updates in archive
                raw_signals["briefing_updates"] = briefing_updates
                raw_signals["briefing_updates_applied"] = [u for u in applied_updates]
                pulse_archive(report, raw_signals, start_dt, end_dt, stats)
                archived = True
            except Exception as e:
                logger.error(f"[pulse] Archive failed: {e}", exc_info=True)
                status["archive_error"] = str(e)

        status.update({
            "phase": "complete",
            "email_sent": email_sent,
            "archived": archived,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
        _pulse_save_status(status)
        logger.info(f"[pulse] Background run {run_id} complete")

    except Exception as e:
        logger.error(f"[pulse] Background run failed: {e}", exc_info=True)
        status.update({
            "phase": "error",
            "error": str(e),
            "traceback": tb.format_exc(),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        })
        _pulse_save_status(status)


@app.route("/pulse/trigger", methods=["GET"])
def pulse_trigger():
    """Trigger a weekly pulse. Runs in background thread, poll /pulse/status for progress."""
    import threading
    days = int(request.args.get("days", PULSE_LOOKBACK_DAYS))
    dry_run = request.args.get("dry_run", "").lower() in ("true", "1", "yes")
    force = request.args.get("force", "").lower() in ("true", "1", "yes")

    # Dedupe: refuse if a completed run is still within the lock window.
    # dry_run bypasses the lock (no email sent, so no duplicate-email risk).
    if not dry_run and not force and not pulse_can_run():
        return jsonify({
            "status": "skipped",
            "reason": "Pulse already ran recently. Use ?force=true to override.",
            "lock": _read_pulse_lock(),
        }), 200

    # Check if already running (thread lock + status file)
    if _pulse_lock.locked():
        try:
            with open(PULSE_STATUS_FILE) as f:
                current = json.load(f)
        except Exception:
            current = {"phase": "running"}
        return jsonify({"status": "already_running", "current": current}), 409
    try:
        if os.path.exists(PULSE_STATUS_FILE):
            with open(PULSE_STATUS_FILE) as f:
                current = json.load(f)
            if current.get("phase") not in ("complete", "error", None):
                return jsonify({"status": "already_running", "current": current}), 409
    except Exception:
        pass

    logger.info(f"[pulse] Trigger: days={days}, dry_run={dry_run} -- launching background thread")
    t = threading.Thread(target=_pulse_run_background, args=(days, dry_run), daemon=True)
    t.start()

    return jsonify({
        "status": "started",
        "message": "Pulse running in background. Poll /pulse/status for progress.",
        "days": days,
        "dry_run": dry_run,
    })


@app.route("/pulse/status", methods=["GET"])
def pulse_status():
    """Poll pulse run status. Returns current phase, stats, and result when complete."""
    try:
        if not os.path.exists(PULSE_STATUS_FILE):
            return jsonify({"status": "no_runs", "message": "No pulse run has been triggered yet."})
        with open(PULSE_STATUS_FILE) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/pulse/debug", methods=["GET"])
def pulse_debug():
    """Debug: test data collection only (no Claude calls). Returns in <60s."""
    import traceback as tb
    days = int(request.args.get("days", 1))
    results = {"days": days}
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    try:
        emails = pulse_collect_emails(start_dt, end_dt)
        results["emails"] = {"count": len(emails), "sample": emails[:2] if emails else []}
    except Exception as e:
        results["emails"] = {"error": str(e), "traceback": tb.format_exc()}

    try:
        teams = pulse_collect_teams(start_dt, end_dt)
        results["teams"] = {"count": len(teams), "sample": teams[:2] if teams else []}
    except Exception as e:
        results["teams"] = {"error": str(e), "traceback": tb.format_exc()}

    try:
        meetings = pulse_collect_meetings(start_dt, end_dt)
        results["meetings"] = {"count": len(meetings), "sample": meetings[:2] if meetings else []}
    except Exception as e:
        results["meetings"] = {"error": str(e), "traceback": tb.format_exc()}

    return jsonify(results)


@app.route("/pulse/check", methods=["GET"])
def pulse_check_permissions():
    """Verify all required Graph permissions for Weekly Pulse."""
    results = {
        "Mail.Read": False,
        "Chat.Read.All": False,
        "ChannelMessage.Read.All": False,
        "Mail.Send": True,  # Already confirmed working (Sara sends emails)
    }
    team_users = 0

    token = get_ms_graph_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    diagnostics = {}

    # Test Mail.Read (also satisfied by Mail.ReadWrite): try reading one message
    # Some users may have inactive/on-prem mailboxes, so try multiple users
    try:
        users = pulse_get_team_users()
        team_users = len(users)
        mail_errors = []
        for u in users[:5]:  # Try up to 5 users
            test_url = f"{MS_GRAPH_BASE}/users/{u['id']}/messages?$top=1&$select=id"
            resp = requests.get(test_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                results["Mail.Read"] = True
                break
            mail_errors.append(f"{u.get('mail','?')}: HTTP {resp.status_code}")
        if not results["Mail.Read"]:
            diagnostics["Mail.Read"] = "; ".join(mail_errors) if mail_errors else "No team users found"
    except Exception as e:
        logger.warning(f"[pulse] Mail.Read check failed: {e}")
        diagnostics["Mail.Read"] = str(e)

    # Test Chat.Read.All: use /users/{id}/chats (app-only auth compatible)
    # Note: /chats is delegated-only and fails with app-only tokens
    try:
        if team_users > 0:
            chat_url = f"{MS_GRAPH_BASE}/users/{users[0]['id']}/chats?$top=1"
            resp = requests.get(chat_url, headers=headers, timeout=15)
            results["Chat.Read.All"] = resp.status_code == 200
            if resp.status_code != 200:
                diagnostics["Chat.Read.All"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
        else:
            diagnostics["Chat.Read.All"] = "No team users to test with"
    except Exception as e:
        logger.warning(f"[pulse] Chat.Read.All check failed: {e}")
        diagnostics["Chat.Read.All"] = str(e)

    # Test ChannelMessage.Read.All: list joined teams for a user
    # Note: /teams requires Group.Read.All; use /users/{id}/joinedTeams instead
    try:
        if team_users > 0:
            teams_url = f"{MS_GRAPH_BASE}/users/{users[0]['id']}/joinedTeams"
            resp = requests.get(teams_url, headers=headers, timeout=15)
            results["ChannelMessage.Read.All"] = resp.status_code == 200
            if resp.status_code != 200:
                diagnostics["ChannelMessage.Read.All"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
        else:
            diagnostics["ChannelMessage.Read.All"] = "No team users to test with"
    except Exception as e:
        logger.warning(f"[pulse] ChannelMessage.Read.All check failed: {e}")
        diagnostics["ChannelMessage.Read.All"] = str(e)

    ready = all(results.values()) and team_users > 0
    response = {
        "permissions": results,
        "team_users_found": team_users,
        "ready": ready,
    }
    if diagnostics:
        response["diagnostics"] = diagnostics
    return jsonify(response)


@app.route("/pulse/history", methods=["GET"])
def pulse_history():
    """List archived pulse reports, or fetch a specific week's full content."""
    specific_week = request.args.get("week")  # e.g. 2026-W11

    if specific_week:
        filepath = os.path.join(PULSE_ARCHIVE_DIR, f"{specific_week}.json")
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                return jsonify(json.load(f))
        return jsonify({"error": f"No pulse found for {specific_week}"}), 404

    # List all archived pulses
    reports = []
    if os.path.exists(PULSE_ARCHIVE_DIR):
        for fname in sorted(os.listdir(PULSE_ARCHIVE_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            filepath = os.path.join(PULSE_ARCHIVE_DIR, fname)
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                reports.append({
                    "filename": fname,
                    "period_start": data.get("period_start"),
                    "period_end": data.get("period_end"),
                    "generated_at": data.get("generated_at"),
                })
            except (json.JSONDecodeError, KeyError):
                continue
    return jsonify({"reports": reports, "count": len(reports)})


@app.route("/pulse/scheduler", methods=["GET"])
def pulse_scheduler_info():
    """Diagnostic: show scheduler state and registered jobs."""
    if _scheduler is None:
        return jsonify({"error": "Scheduler not initialized"}), 503
    jobs = _scheduler.get_jobs()
    return jsonify({
        "scheduler_running": _scheduler.running,
        "jobs": [
            {
                "id": j.id,
                "next_run": str(j.next_run_time),
                "trigger": str(j.trigger),
            }
            for j in jobs
        ],
        "workers": os.environ.get("WEB_CONCURRENCY", "1"),
        "pid": os.getpid(),
    })


@app.route("/pulse/heartbeat", methods=["GET"])
def pulse_heartbeat():
    """Report-only health check. Does NOT re-register the job -- that path
    could cause double-fires by recreating a job that APScheduler then treats
    as having a missed run within misfire_grace_time. If the job is genuinely
    missing, restart the container; start_scheduler() re-registers on boot.
    """
    if _scheduler is None or not _scheduler.running:
        return jsonify({"status": "scheduler_down", "scheduler_running": False}), 503

    job = _scheduler.get_job("weekly_pulse")
    return jsonify({
        "status": "healthy" if job else "job_missing",
        "scheduler_running": _scheduler.running,
        "next_run": str(job.next_run_time) if job else None,
        "now_utc": datetime.utcnow().isoformat(),
        "last_pulse_lock": _read_pulse_lock(),
    })


@app.route("/briefing", methods=["GET"])
def view_briefing():
    """View current briefing book content."""
    try:
        content = load_briefing_book()
        if not content:
            return jsonify({"error": "No briefing book found"}), 404
        return jsonify({"content": content, "length": len(content), "path": BRIEFING_BOOK_PATH})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/briefing", methods=["POST"])
def update_briefing():
    """Manually update briefing book content."""
    data = request.get_json()
    if not data or "content" not in data:
        return jsonify({"error": "JSON body with 'content' field required"}), 400
    content = data["content"]
    try:
        with open(BRIEFING_BOOK_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[briefing] Manual update: {len(content)} chars written")
        return jsonify({"status": "updated", "length": len(content)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================================================================
#  FLASK ROUTES
# ======================================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})





# ======================================================================
# TEAMS TRANSCRIPT INTEGRATION
# ======================================================================

import re as _re
import threading as _teams_threading

_teams_subscription_id = None
_teams_subscription_expiry = None   # ISO string from Graph, None if unknown
_teams_subscription_lock = _teams_threading.Lock()

TEAMS_SUBSCRIPTION_FILE = os.path.join(DATA_DIR, "teams_subscription.json")


def _load_teams_subscription_state():
    """Read persisted subscription id + expiry from disk. Returns (id, expiry_str) or (None, None)."""
    try:
        with open(TEAMS_SUBSCRIPTION_FILE) as f:
            d = json.load(f)
        return d.get("subscription_id"), d.get("expiry")
    except (OSError, ValueError, KeyError):
        return None, None


def _save_teams_subscription_state(sub_id, expiry):
    """Persist subscription id + expiry to disk so state survives restarts."""
    try:
        with open(TEAMS_SUBSCRIPTION_FILE, "w") as f:
            json.dump({"subscription_id": sub_id, "expiry": expiry}, f)
    except OSError as exc:
        logger.warning(f"[teams] Could not save subscription state: {exc}")


def get_graph_app_only_token() -> str:
    now = time.time()
    if _ms_token_cache.get("app_only_token") and _ms_token_cache.get("app_only_expires", 0) > now + 60:
        return _ms_token_cache["app_only_token"]
    data = {
        "client_id": MS_GRAPH_CLIENT_ID,
        "client_secret": MS_GRAPH_CLIENT_SECRET,
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    resp = requests.post(
        f"https://login.microsoftonline.com/{MS_GRAPH_TENANT_ID}/oauth2/v2.0/token",
        data=data, timeout=15
    )
    resp.raise_for_status()
    td = resp.json()
    _ms_token_cache["app_only_token"] = td["access_token"]
    _ms_token_cache["app_only_expires"] = now + td.get("expires_in", 3600)
    logger.info("[teams] Got app-only Graph token")
    return _ms_token_cache["app_only_token"]


def parse_vtt_to_sentences(vtt_content: str) -> list:
    sentences = []
    lines = vtt_content.split("\n")
    current_speaker = "Unknown"
    current_text = []
    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.startswith("NOTE"):
            if current_text:
                sentences.append({"speaker_name": current_speaker, "text": " ".join(current_text)})
                current_text = []
            continue
        v_match = _re.match(r"<v\s+([^>]+)>(.+?)(?:</v>)?$", line)
        if v_match:
            if current_text:
                sentences.append({"speaker_name": current_speaker, "text": " ".join(current_text)})
                current_text = []
            current_speaker = v_match.group(1).strip()
            text = _re.sub(r"<[^>]+>", "", v_match.group(2)).strip()
            if text:
                current_text.append(text)
            continue
        colon_match = _re.match(r"^([A-Za-z][A-Za-z .\x27-]+):\s+(.+)$", line)
        if colon_match and not _re.match(r"^\d", line):
            if current_text:
                sentences.append({"speaker_name": current_speaker, "text": " ".join(current_text)})
                current_text = []
            current_speaker = colon_match.group(1).strip()
            current_text.append(colon_match.group(2).strip())
            continue
        if line and not _re.match(r"^\d+$", line):
            clean = _re.sub(r"<[^>]+>", "", line).strip()
            if clean:
                current_text.append(clean)
    if current_text:
        sentences.append({"speaker_name": current_speaker, "text": " ".join(current_text)})
    logger.info(f"[teams] Parsed VTT: {len(sentences)} sentences")
    return sentences


def get_teams_meeting_details(user_id: str, meeting_id: str) -> dict:
    token = get_graph_app_only_token()
    url = f"https://graph.microsoft.com/v1.0/users/{user_id}/onlineMeetings/{meeting_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    logger.warning(f"[teams] Meeting details failed: {resp.status_code} {resp.text[:200]}")
    return {}


def get_teams_transcript_content(user_id: str, meeting_id: str, transcript_id: str) -> str:
    token = get_graph_app_only_token()
    url = (f"https://graph.microsoft.com/v1.0/users/{user_id}"
           f"/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content")
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "text/vtt"}, timeout=60)
    if resp.status_code == 200:
        return resp.text
    logger.error(f"[teams] Transcript content failed: {resp.status_code} {resp.text[:200]}")
    return ""


def process_teams_transcript_background(user_id, meeting_id, transcript_id):
    try:
        logger.info(f"[teams] Processing: user={user_id} meeting={meeting_id}")
        teams_tid = f"teams-{transcript_id}"
        processed = load_processed()
        if teams_tid in processed:
            logger.info(f"[teams] Already processed {teams_tid}")
            return
        time.sleep(30)
        meeting = get_teams_meeting_details(user_id, meeting_id)
        subject = meeting.get("subject") or "Teams Meeting"
        join_url = meeting.get("joinWebUrl", "")
        start_time = meeting.get("startDateTime", "")
        end_time = meeting.get("endDateTime", "")
        duration = 0
        if start_time and end_time:
            try:
                s = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                e = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                duration = int((e - s).total_seconds() / 60)
            except (ValueError, TypeError):
                pass
        organizer_info = (meeting.get("participants") or {}).get("organizer") or {}
        organizer_identity = (organizer_info.get("identity") or {}).get("user") or {}
        organizer_email = organizer_info.get("upn", "")
        if not organizer_email and organizer_identity.get("id"):
            try:
                tk = get_graph_app_only_token()
                ur = requests.get(f"https://graph.microsoft.com/v1.0/users/{organizer_identity['id']}",
                    headers={"Authorization": f"Bearer {tk}"}, timeout=15)
                if ur.status_code == 200:
                    organizer_email = ur.json().get("mail") or ur.json().get("userPrincipalName", "")
            except Exception:
                pass
        attendees = (meeting.get("participants") or {}).get("attendees") or []
        participant_names = []
        for att in attendees:
            identity = (att.get("identity") or {}).get("user") or {}
            upn = att.get("upn", "")
            name = identity.get("displayName", "")
            participant_names.append(upn if upn else name)
        if organizer_email and organizer_email not in participant_names:
            participant_names.insert(0, organizer_email)
        logger.info(f"[teams] '{subject}' | org={organizer_email} | {len(participant_names)} participants | {duration}min")
        vtt = get_teams_transcript_content(user_id, meeting_id, transcript_id)
        if not vtt:
            logger.error("[teams] Empty transcript, aborting")
            return
        sentences = parse_vtt_to_sentences(vtt)
        if not sentences:
            logger.warning("[teams] No sentences parsed")
            return
        transcript_dict = {
            "id": teams_tid,
            "title": subject,
            "dateString": start_time or datetime.now(timezone.utc).isoformat(),
            "duration": duration,
            "organizer_email": organizer_email,
            "participants": participant_names,
            "summary": {
                "short_summary": f"Teams meeting: {subject}",
                "action_items": "",
                "keywords": "",
                "overview": f"Auto-transcribed Teams meeting with {len(participant_names)} participants.",
                "notes": ""
            },
            "sentences": sentences,
            "_source": "teams",
            "_join_url": join_url,
        }
        logger.info(f"[teams] Feeding into pipeline: '{subject}' ({len(sentences)} sentences)")
        process_transcript_phase1(transcript_dict)
        processed = load_processed()
        processed.add(teams_tid)
        save_processed(processed)
        logger.info(f"[teams] Done: {teams_tid}")
    except Exception as e:
        logger.error(f"[teams] Error: {e}", exc_info=True)


def _teams_new_expiry_str(minutes=59):
    """Return an expirationDateTime 59min from now.

    Graph requires lifecycleNotificationUrl when expiry exceeds 1h for this resource type.
    Keep expiry under 1h to avoid that requirement; the renewal job runs every 45min.
    """
    return (datetime.utcnow() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _teams_create_subscription(token):
    """POST to create a new subscription. Returns the Graph response object."""
    expiry = _teams_new_expiry_str()
    body = {
        "changeType": "created",
        "notificationUrl": f"{RAILWAY_PUBLIC_URL}/webhook/teams-transcript",
        "resource": "communications/onlineMeetings/getAllTranscripts",
        "expirationDateTime": expiry,
        "clientState": TEAMS_WEBHOOK_SECRET,
    }
    logger.info(f"[teams] POST /subscriptions body={body}")
    resp = requests.post(
        "https://graph.microsoft.com/v1.0/subscriptions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    logger.info(f"[teams] POST /subscriptions -> {resp.status_code} {resp.text[:400]}")
    return resp


def ensure_teams_subscription():
    """Idempotent: create or renew the Graph subscription as needed.

    - No existing id in memory or file: create new.
    - Existing id, expiry within 24h: PATCH to extend.
    - PATCH returns 404 (Graph purged it): fall back to create.
    - Expiry still >24h away: no-op.

    Persists state to TEAMS_SUBSCRIPTION_FILE so restarts resume correctly.
    """
    global _teams_subscription_id, _teams_subscription_expiry

    if not TEAMS_TRANSCRIPT_ENABLED or not MS_GRAPH_CLIENT_ID or not MS_GRAPH_TENANT_ID:
        return

    with _teams_subscription_lock:
        sub_id = _teams_subscription_id
        sub_expiry = _teams_subscription_expiry

    # If nothing in memory, try disk
    if not sub_id:
        sub_id, sub_expiry = _load_teams_subscription_state()
        if sub_id:
            with _teams_subscription_lock:
                _teams_subscription_id = sub_id
                _teams_subscription_expiry = sub_expiry

    # Determine whether renewal is needed
    needs_renew = True
    if sub_id and sub_expiry:
        try:
            exp_dt = datetime.fromisoformat(sub_expiry.replace("Z", "+00:00").replace(".0000000", ""))
            now_utc = datetime.now(exp_dt.tzinfo)
            hours_left = (exp_dt - now_utc).total_seconds() / 3600
            if hours_left > 0.5:   # > 30 min remaining -- no-op
                logger.info(f"[teams] Subscription {sub_id} valid for {hours_left*60:.0f}min, no action needed")
                needs_renew = False
        except (ValueError, TypeError) as exc:
            logger.warning(f"[teams] Could not parse expiry '{sub_expiry}': {exc}")

    if not needs_renew:
        return

    try:
        token = get_graph_app_only_token()

        if sub_id:
            # Try PATCH first
            new_expiry = _teams_new_expiry_str()
            logger.info(f"[teams] PATCH /subscriptions/{sub_id} expirationDateTime={new_expiry}")
            resp = requests.patch(
                f"https://graph.microsoft.com/v1.0/subscriptions/{sub_id}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"expirationDateTime": new_expiry}, timeout=30,
            )
            logger.info(f"[teams] PATCH -> {resp.status_code} {resp.text[:400]}")
            if resp.status_code == 200:
                d = resp.json()
                actual_expiry = d.get("expirationDateTime", new_expiry)
                with _teams_subscription_lock:
                    _teams_subscription_expiry = actual_expiry
                _save_teams_subscription_state(sub_id, actual_expiry)
                logger.info(f"[teams] Subscription renewed: {sub_id} expires {actual_expiry}")
                return
            if resp.status_code == 404:
                logger.warning(f"[teams] Subscription {sub_id} gone (404), will recreate")
                with _teams_subscription_lock:
                    _teams_subscription_id = None
                    _teams_subscription_expiry = None
                sub_id = None
            else:
                logger.error(f"[teams] Renewal failed {resp.status_code}, will recreate")
                with _teams_subscription_lock:
                    _teams_subscription_id = None
                    _teams_subscription_expiry = None
                sub_id = None

        # Create new subscription
        resp = _teams_create_subscription(token)
        if resp.status_code in (200, 201):
            d = resp.json()
            new_id = d.get("id")
            new_expiry = d.get("expirationDateTime")
            with _teams_subscription_lock:
                _teams_subscription_id = new_id
                _teams_subscription_expiry = new_expiry
            _save_teams_subscription_state(new_id, new_expiry)
            logger.info(f"[teams] Subscription created: {new_id} expires {new_expiry}")
        else:
            logger.error(f"[teams] Subscription creation failed: {resp.status_code}")
    except Exception as exc:
        logger.error(f"[teams] ensure_teams_subscription error: {exc}", exc_info=True)


# Keep for backwards compatibility (called by scheduler boot job)
create_teams_transcript_subscription = ensure_teams_subscription


@app.route("/teams/subscribe", methods=["POST", "GET"])
def teams_subscribe_trigger():
    """Manually trigger Teams subscription creation. Returns status:ok + real id on success."""
    if not TEAMS_TRANSCRIPT_ENABLED or not MS_GRAPH_CLIENT_ID or not MS_GRAPH_TENANT_ID:
        return jsonify({"status": "error", "error": "Teams transcript not configured"}), 400
    try:
        token = get_graph_app_only_token()
        resp = _teams_create_subscription(token)
        if resp.status_code in (200, 201):
            d = resp.json()
            new_id = d.get("id")
            new_expiry = d.get("expirationDateTime")
            with _teams_subscription_lock:
                global _teams_subscription_id, _teams_subscription_expiry
                _teams_subscription_id = new_id
                _teams_subscription_expiry = new_expiry
            _save_teams_subscription_state(new_id, new_expiry)
            return jsonify({"status": "ok", "subscription_id": new_id, "expires": new_expiry})
        # Graph returned an error -- surface it honestly
        try:
            graph_error = resp.json()
        except Exception:
            graph_error = {"raw": resp.text[:500]}
        logger.error(f"[teams] /teams/subscribe Graph error: {resp.status_code} {graph_error}")
        return jsonify({"status": "error", "http_status": resp.status_code, "graph_error": graph_error}), 502
    except Exception as exc:
        logger.error(f"[teams] /teams/subscribe exception: {exc}", exc_info=True)
        return jsonify({"status": "error", "error": str(exc)}), 500

@app.route("/teams/debug", methods=["GET"])
def teams_debug():
    """Debug Teams transcript integration."""
    results = {"subscription_id": _teams_subscription_id}
    try:
        token = get_graph_app_only_token()
        results["token"] = "ok"
        # List active subscriptions
        sr = requests.get("https://graph.microsoft.com/v1.0/subscriptions",
            headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if sr.status_code == 200:
            subs = sr.json().get("value", [])
            results["graph_subscriptions"] = [{"id": s["id"], "resource": s["resource"],
                "expiry": s.get("expirationDateTime")} for s in subs]
        else:
            results["graph_subscriptions_error"] = f"{sr.status_code}: {sr.text[:200]}"
        # Try to list recent meetings for the user
        user_id = TEAMS_ORGANIZER_USER_ID
        mr = requests.get(f"https://graph.microsoft.com/v1.0/users/{user_id}/onlineMeetings",
            headers={"Authorization": f"Bearer {token}"}, timeout=15)
        results["meetings_status"] = mr.status_code
        if mr.status_code == 200:
            meetings = mr.json().get("value", [])
            results["recent_meetings"] = [{"id": m["id"], "subject": m.get("subject"),
                "start": m.get("startDateTime")} for m in meetings[:5]]
        else:
            results["meetings_error"] = mr.text[:200]
    except Exception as e:
        results["error"] = str(e)
    return jsonify(results)

@app.route("/teams/fetch-recent", methods=["GET"])
def teams_fetch_recent():
    """Manually check for recent Teams transcripts via Graph API."""
    results = {"checked_users": []}
    try:
        token = get_graph_app_only_token()
        # Check recent calendar events for the organizer
        user_id = TEAMS_ORGANIZER_USER_ID
        # Get recent events with online meeting info
        now = datetime.utcnow()
        start = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        cal_url = (f"https://graph.microsoft.com/v1.0/users/{user_id}/calendarView"
                   f"?startDateTime={start}&endDateTime={end}"
                   f"&$select=id,subject,start,end,onlineMeeting")
        cr = requests.get(cal_url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if cr.status_code == 200:
            events = cr.json().get("value", [])
            results["calendar_events"] = len(events)
            for ev in events:
                om = ev.get("onlineMeeting") or {}
                join_url = om.get("joinUrl", "")
                ev_info = {"subject": ev.get("subject"), "join_url": join_url[:80] if join_url else "none"}
                if join_url:
                    # Try to get meeting ID from joinUrl
                    meet_r = requests.get(
                        f"https://graph.microsoft.com/v1.0/users/{user_id}/onlineMeetings"
                        f"?$filter=JoinWebUrl eq '{join_url}'",
                        headers={"Authorization": f"Bearer {token}"}, timeout=15)
                    if meet_r.status_code == 200:
                        meetings = meet_r.json().get("value", [])
                        if meetings:
                            mid = meetings[0]["id"]
                            ev_info["meeting_id"] = mid[:30] + "..."
                            # Try to list transcripts
                            tr = requests.get(
                                f"https://graph.microsoft.com/v1.0/users/{user_id}/onlineMeetings/{mid}/transcripts",
                                headers={"Authorization": f"Bearer {token}"}, timeout=15)
                            if tr.status_code == 200:
                                transcripts = tr.json().get("value", [])
                                ev_info["transcripts"] = len(transcripts)
                                if transcripts:
                                    ev_info["transcript_ids"] = [t["id"][:20] for t in transcripts]
                            else:
                                ev_info["transcripts_error"] = f"{tr.status_code}"
                    else:
                        ev_info["meeting_lookup"] = f"{meet_r.status_code}: {meet_r.text[:100]}"
                results["checked_users"].append(ev_info)
        else:
            results["calendar_error"] = f"{cr.status_code}: {cr.text[:200]}"
    except Exception as e:
        results["error"] = str(e)
    return jsonify(results)


def poll_teams_transcripts():
    """Poll Graph API for new Teams transcripts. Runs on a schedule."""
    if not TEAMS_TRANSCRIPT_ENABLED or not MS_GRAPH_CLIENT_ID:
        return
    logger.info(f"[teams-poll] Checking {len(TEAMS_POLL_USER_IDS)} user(s) for new transcripts...")
    try:
        token = get_graph_app_only_token()
    except Exception as e:
        logger.error(f"[teams-poll] Token error: {e}")
        return
    total_new = 0
    for user_id in TEAMS_POLL_USER_IDS:
        try:
            url = (f"https://graph.microsoft.com/v1.0/users/{user_id}"
                   f"/onlineMeetings/getAllTranscripts(meetingOrganizerUserId='{user_id}')")
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"[teams-poll] User {user_id[:8]}: {resp.status_code}")
                continue
            transcripts = resp.json().get("value") or []
            processed = load_processed()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
            for t in transcripts:
                tid = f"teams-{t['id']}"
                if tid in processed:
                    continue
                created = t.get("createdDateTime", "")
                try:
                    t_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if t_date < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue
                meeting_id = t.get("meetingId", "")
                transcript_id = t.get("id", "")
                logger.info(f"[teams-poll] New transcript: {transcript_id[:30]}... (created {created})")
                total_new += 1
                thread = _teams_threading.Thread(
                    target=process_teams_transcript_background,
                    args=(user_id, meeting_id, transcript_id),
                    daemon=True)
                thread.start()
        except Exception as e:
            logger.error(f"[teams-poll] Error for user {user_id[:8]}: {e}", exc_info=True)
    logger.info(f"[teams-poll] Done. {total_new} new transcript(s) found.")


@app.route("/teams/poll-now", methods=["GET", "POST"])
def teams_poll_now():
    """Manually trigger Teams transcript polling."""
    try:
        thread = _teams_threading.Thread(target=poll_teams_transcripts, daemon=True)
        thread.start()
        return jsonify({"status": "polling_started"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

_digest_lock = _threading.Lock()
_email_sync_lock = _threading.Lock()


@app.route("/digest/trigger", methods=["GET"])
def digest_trigger():
    """Manually trigger the daily pipeline digest.
    ?dry_run=true  -- compose but send no email; does not advance the window.
    ?sync=true     -- run inline and return the result (or full traceback) as
                      JSON. Diagnostic: surfaces collection/compose/send errors
                      that a background run would only write to the logs."""
    import traceback as _digest_tb
    dry_run = request.args.get("dry_run", "").lower() in ("true", "1", "yes")
    sync = request.args.get("sync", "").lower() in ("true", "1", "yes")
    if not _digest_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409

    if sync:
        try:
            import daily_pipeline_digest
            result = daily_pipeline_digest.run_digest(dry_run=dry_run)
            logger.info("[digest] Sync digest run complete")
            return jsonify(result)
        except Exception as e:
            logger.error(f"[digest] Sync digest run failed: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e),
                            "traceback": _digest_tb.format_exc()}), 500
        finally:
            _digest_lock.release()

    def _run():
        try:
            import daily_pipeline_digest
            daily_pipeline_digest.run_digest(dry_run=dry_run)
            logger.info("[digest] Manual digest run complete")
        except Exception as e:
            logger.error(f"[digest] Manual digest run failed: {e}", exc_info=True)
        finally:
            _digest_lock.release()

    logger.info(f"[digest] Trigger: dry_run={dry_run} -- launching background thread")
    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "dry_run": dry_run})


@app.route("/digest/status", methods=["GET"])
def digest_status():
    """Last digest run outcome (counts, sent/quiet flags, or error traceback).
    Lets scheduled-run failures be inspected without Railway log access."""
    import daily_pipeline_digest
    return jsonify(daily_pipeline_digest.read_status())


_learn_trigger_lock = _threading.Lock()


@app.route("/learn/run", methods=["GET", "POST"])
def learn_run():
    """Manually trigger the Read/Learn digest (learn_digest module).
    ?dry_run=true  -- resolve+cluster+curate, send no email, create no tasks, move nothing.
    ?backlog=1     -- force the full-backlog first run (clustering sees the whole set).
    ?sync=true     -- run inline and return the result/traceback as JSON (diagnostic).
    ?force=1       -- clear an orphaned run lock before starting (operator override;
                      do NOT use on the real send run -- it defeats the single-send guard)."""
    import traceback as _learn_tb
    dry_run = request.args.get("dry_run", "").lower() in ("true", "1", "yes")
    backlog = request.args.get("backlog", "").lower() in ("true", "1", "yes")
    sync = request.args.get("sync", "").lower() in ("true", "1", "yes")
    force = request.args.get("force", "").lower() in ("true", "1", "yes")
    limit = request.args.get("limit", type=int)  # diagnostic: process only the first N unread
    if not _learn_trigger_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409

    if sync:
        try:
            import learn_digest
            result = learn_digest.run_learn(dry_run=dry_run, backlog=backlog, force=force, limit=limit)
            return jsonify(result)
        except Exception as e:
            logger.error(f"[learn] Sync run failed: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e),
                            "traceback": _learn_tb.format_exc()}), 500
        finally:
            _learn_trigger_lock.release()

    def _run():
        try:
            import learn_digest
            learn_digest.run_learn(dry_run=dry_run, backlog=backlog, force=force, limit=limit)
            logger.info("[learn] Manual run complete")
        except Exception as e:
            logger.error(f"[learn] Manual run failed: {e}", exc_info=True)
        finally:
            _learn_trigger_lock.release()

    logger.info(f"[learn] Trigger: dry_run={dry_run} backlog={backlog} force={force} -- launching background thread")
    t = _threading.Thread(target=_run, daemon=True)
    try:
        t.start()
    except Exception as e:
        _learn_trigger_lock.release()  # never orphan the trigger lock if the thread won't start
        logger.error(f"[learn] Failed to start run thread: {e}", exc_info=True)
        return jsonify({"status": "error", "error": f"could not start run: {e}"}), 500
    return jsonify({"status": "started", "dry_run": dry_run, "backlog": backlog, "force": force, "limit": limit})


@app.route("/learn/status", methods=["GET"])
def learn_status():
    """Last Read/Learn run outcome (counts, sent flag, or error traceback)."""
    import learn_digest
    return jsonify(learn_digest.read_status())


_fyi_trigger_lock = _threading.Lock()


@app.route("/fyi/run", methods=["GET", "POST"])
def fyi_run():
    """Manually trigger FYI Triage (fyi_triage module). DRY-RUN by default.
    ?days=N     -- lookback window in days (7 = calibration dry-run, 30 = backfill).
                   Absent -> the cron default FYI_LOOKBACK_HOURS (24h).
    ?live=1     -- request a real move. A move ALSO requires env FYI_LIVE=1; absent
                   either gate the run is dry regardless of window.
    ?backlog=1  -- re-process everything in the window, ignoring the processed-id store.
    ?sync=true  -- run inline and return the result/traceback as JSON (diagnostic).
    ?force=1    -- clear an orphaned run lock first (operator override; not for a live run).
    ?email=1    -- also email a short run summary."""
    import traceback as _fyi_tb
    days = request.args.get("days", type=int)
    live = request.args.get("live", "").lower() in ("true", "1", "yes")
    backlog = request.args.get("backlog", "").lower() in ("true", "1", "yes")
    sync = request.args.get("sync", "").lower() in ("true", "1", "yes")
    force = request.args.get("force", "").lower() in ("true", "1", "yes")
    email = request.args.get("email", "").lower() in ("true", "1", "yes")
    limit = request.args.get("limit", type=int)  # diagnostic: cap messages per folder
    if not _fyi_trigger_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409

    if sync:
        try:
            import fyi_triage
            result = fyi_triage.run_fyi(days=days, live=live, backlog=backlog, force=force,
                                        limit=limit, send_summary=email)
            return jsonify(result)
        except Exception as e:
            logger.error(f"[fyi] Sync run failed: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e),
                            "traceback": _fyi_tb.format_exc()}), 500
        finally:
            _fyi_trigger_lock.release()

    def _run():
        try:
            import fyi_triage
            fyi_triage.run_fyi(days=days, live=live, backlog=backlog, force=force,
                               limit=limit, send_summary=email)
            logger.info("[fyi] Manual run complete")
        except Exception as e:
            logger.error(f"[fyi] Manual run failed: {e}", exc_info=True)
        finally:
            _fyi_trigger_lock.release()

    logger.info(f"[fyi] Trigger: days={days} live={live} backlog={backlog} force={force} "
                "-- launching background thread")
    t = _threading.Thread(target=_run, daemon=True)
    try:
        t.start()
    except Exception as e:
        _fyi_trigger_lock.release()  # never orphan the trigger lock if the thread won't start
        logger.error(f"[fyi] Failed to start run thread: {e}", exc_info=True)
        return jsonify({"status": "error", "error": f"could not start run: {e}"}), 500
    return jsonify({"status": "started", "days": days, "live": live, "backlog": backlog, "force": force})


@app.route("/fyi/status", methods=["GET"])
def fyi_status():
    """Last FYI Triage run outcome (scanned/important/moved counts, per-message
    decisions with reasons, or error traceback) + live heartbeat + FYI_LIVE state."""
    import fyi_triage
    return jsonify(fyi_triage.read_status())


_biweekly_lock = _threading.Lock()


@app.route("/biweekly/trigger", methods=["GET"])
def biweekly_trigger():
    """Manually trigger the biweekly business update (distilled from weekly pulse
    archives, emailed to Ken to forward to the team).
    ?dry_run=true                    -- compose but send no email.
    ?sync=true                       -- run inline, return result/traceback JSON.
    ?force=true                      -- ignore the every-other-week cadence gate.
    ?start=YYYY-MM-DD&end=YYYY-MM-DD -- explicit window (e.g. a backfill)."""
    import traceback as _bw_tb
    dry_run = request.args.get("dry_run", "").lower() in ("true", "1", "yes")
    sync = request.args.get("sync", "").lower() in ("true", "1", "yes")
    force = request.args.get("force", "").lower() in ("true", "1", "yes")
    start_arg = request.args.get("start", "")
    end_arg = request.args.get("end", "")
    try:
        start_override = (datetime.fromisoformat(start_arg).replace(tzinfo=timezone.utc)
                          if start_arg else None)
        end_override = (datetime.fromisoformat(end_arg).replace(tzinfo=timezone.utc)
                        if end_arg else None)
    except ValueError:
        return jsonify({"status": "error", "error": "start/end must be YYYY-MM-DD"}), 400

    if not _biweekly_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409

    if sync:
        try:
            import biweekly_business_update
            result = biweekly_business_update.run_biweekly(
                dry_run=dry_run, start_override=start_override,
                end_override=end_override, force=force)
            logger.info("[biweekly] Sync run complete")
            return jsonify(result)
        except Exception as e:
            logger.error(f"[biweekly] Sync run failed: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e),
                            "traceback": _bw_tb.format_exc()}), 500
        finally:
            _biweekly_lock.release()

    def _run():
        try:
            import biweekly_business_update
            biweekly_business_update.run_biweekly(
                dry_run=dry_run, start_override=start_override,
                end_override=end_override, force=force)
            logger.info("[biweekly] Manual run complete")
        except Exception as e:
            logger.error(f"[biweekly] Manual run failed: {e}", exc_info=True)
        finally:
            _biweekly_lock.release()

    logger.info(f"[biweekly] Trigger: dry_run={dry_run} -- launching background thread")
    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "dry_run": dry_run})


@app.route("/biweekly/status", methods=["GET"])
def biweekly_status():
    """Last biweekly business update run outcome (or error traceback)."""
    import biweekly_business_update
    return jsonify(biweekly_business_update.read_status())


_corrections_lock = _threading.Lock()


@app.route("/corrections", methods=["GET"])
def corrections_list():
    """Active standing corrections that Sara applies to the weekly pulse and the
    biweekly business update. `?all=true` includes deactivated ones."""
    import sara_corrections
    include = request.args.get("all", "").lower() in ("true", "1", "yes")
    items = sara_corrections.list_corrections(include_inactive=include)
    return jsonify({"count": len(items), "corrections": items,
                    "baseline_count": len(sara_corrections.BASELINE_CORRECTIONS)})


@app.route("/corrections/ingest", methods=["GET"])
def corrections_ingest():
    """Scan Sara's mailbox now for reply-corrections and store them. Manual
    counterpart to the scheduled poll."""
    import traceback as _c_tb
    import sara_corrections
    if not _corrections_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409
    try:
        result = sara_corrections.ingest_replies()
        return jsonify({"status": "ok", **result})
    except Exception as e:
        logger.error(f"[corrections] Ingest failed: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e),
                        "traceback": _c_tb.format_exc()}), 500
    finally:
        _corrections_lock.release()


@app.route("/corrections/add", methods=["GET", "POST"])
def corrections_add():
    """Add a standing correction directly (without an email reply)."""
    import sara_corrections
    text = request.args.get("text") or (request.get_json(silent=True) or {}).get("text", "")
    try:
        entry = sara_corrections.add_correction(text, source="endpoint")
    except ValueError as e:
        return jsonify({"status": "error", "error": str(e)}), 400
    return jsonify({"status": "ok", "correction": entry})


@app.route("/corrections/delete", methods=["GET", "POST"])
def corrections_delete():
    """Deactivate a standing correction by id so Sara stops applying it."""
    import sara_corrections
    cid = request.args.get("id", "")
    if not cid:
        return jsonify({"status": "error", "error": "id required"}), 400
    found = sara_corrections.deactivate_correction(cid)
    return (jsonify({"status": "ok", "deactivated": cid}) if found
            else (jsonify({"status": "error", "error": "no active correction with that id"}), 404))


@app.route("/version", methods=["GET"])
def version():
    return jsonify({"version": "2.21.0-learn-finance-priority", "deployed": "2026-06-23"})


@app.route("/config", methods=["GET"])
def config_check():
    """Diagnostic: verify team-wide config (no secrets exposed)."""
    return jsonify({
        "graph_auth_mode": "app-only (team-wide)" if is_app_only_mode() else "delegated (single-user)",
        "graph_client_id_set": bool(MS_GRAPH_CLIENT_ID), "graph_client_secret_set": bool(MS_GRAPH_CLIENT_SECRET), "refresh_token_len": len(MS_GRAPH_REFRESH_TOKEN),
        "graph_tenant_id_set": bool(MS_GRAPH_TENANT_ID),
        "bot_sender": BOT_SENDER_EMAIL or "(not set)",
        "teams_transcript_enabled": TEAMS_TRANSCRIPT_ENABLED,
        "teams_organizer_user_id": TEAMS_ORGANIZER_USER_ID[:8] + "..." if TEAMS_ORGANIZER_USER_ID else "(not set)",
        "teams_subscription_active": bool(_teams_subscription_id),
        "teams_subscription_expires": _teams_subscription_expiry,
        "internal_domains_count": len(INTERNAL_DOMAINS),
        "hubspot_owner_map_raw": HUBSPOT_OWNER_MAP_RAW[:200] if HUBSPOT_OWNER_MAP_RAW else "(empty)",
        "hubspot_owner_map_entries": len(HUBSPOT_OWNER_MAP),
        "hubspot_owner_map_emails": list(HUBSPOT_OWNER_MAP.keys()),
        "hubspot_fallback_owner": HUBSPOT_OWNER_ID or "(not set)",
        "notify_via": NOTIFY_VIA,
        "todo_list_name": TODO_LIST_NAME,
        "todo_poll_interval": TODO_POLL_INTERVAL,
        "todo_poller_running": _todo_poller_running,
        # Read/Learn optional resolver keys -- presence only, never the values.
        "xai_key_loaded": bool(os.environ.get("XAI_API_KEY")),
        "spoken_key_loaded": bool(os.environ.get("SPOKEN_API_KEY")),
        "jina_key_loaded": bool(os.environ.get("JINA_API_KEY")),
    })




@app.route("/test", methods=["GET"])
def test_pipeline():
    """Dry-run: fetch transcript, extract intelligence, test To-Do API, report pass/fail."""
    import time as _time
    import traceback as _tb
    results = {"version": "2.21.0-learn-finance-priority", "steps": {}}
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
                get_ms_graph_token()  # token acquisition probe
                test_user = "bk@negevlabs.com"
                url = f"{MS_GRAPH_BASE}/users/{test_user}/todo/lists"
                todo_resp = _graph_request_with_retry("GET", url)
                lists = todo_resp.get("value") or []
                results["steps"]["todo_api"] = {"status": "ok", "lists_count": len(lists), "ms": int((_time.time()-t0)*1000)}
            except Exception as te:
                results["steps"]["todo_api"] = {"status": "fail", "error": str(te)}
        else:
            results["steps"]["todo_api"] = {"status": "skip", "reason": "MS_GRAPH_CLIENT_ID not set"}

        # Step 5: Email alias normalization
        alias_tests = [
            ("shlomi@ariadnebio.com", "shlomi@negevlabs.com"),
            ("ka@ariadnebio.com", "ka@negevlabs.com"),
            ("dan@ariadnebio.com", "dan@negevlabs.com"),
            ("bk@negevlabs.com", "bk@negevlabs.com"),
            ("unknown@external.com", "unknown@external.com"),
        ]
        alias_ok = True
        for test_in, expected in alias_tests:
            actual = normalize_team_email(test_in)
            if actual != expected:
                results["steps"]["email_aliases"] = {"status": "fail", "input": test_in, "expected": expected, "got": actual}
                alias_ok = False
                break
        if alias_ok:
            results["steps"]["email_aliases"] = {"status": "ok", "tested": len(alias_tests)}

        results["status"] = "pass"
        return jsonify(results), 200
    except Exception as e:
        results["status"] = "fail"
        results["error"] = str(e)
        results["traceback"] = _tb.format_exc()
        return jsonify(results), 500



# ======================================================================
# TEAMS TRANSCRIPT WEBHOOK
# ======================================================================

@app.route("/webhook/teams-transcript", methods=["POST"])
def webhook_teams_transcript():
    validation_token = request.args.get("validationToken")
    if validation_token:
        logger.info(f"[teams-webhook] Validation: {validation_token[:20]}...")
        return validation_token, 200, {"Content-Type": "text/plain"}
    data = request.get_json(silent=True) or {}
    notifications = data.get("value") or []
    logger.info(f"[teams-webhook] Received {len(notifications)} notification(s)")
    for notif in notifications:
        if notif.get("clientState") != TEAMS_WEBHOOK_SECRET:
            logger.warning("[teams-webhook] Invalid clientState")
            continue
        resource = notif.get("resource", "")
        logger.info(f"[teams-webhook] Resource: {resource}")
        parts = resource.split("/")
        if len(parts) >= 6 and "transcripts" in parts:
            user_id = parts[1]
            meeting_id = parts[3]
            transcript_id = parts[5]
            logger.info(f"[teams-webhook] Processing: {transcript_id}")
            thread = _teams_threading.Thread(
                target=process_teams_transcript_background,
                args=(user_id, meeting_id, transcript_id),
                daemon=True)
            thread.start()
        else:
            logger.warning(f"[teams-webhook] Unexpected resource: {resource}")
    return "", 202

@app.route("/webhook/asana", methods=["POST"])
def asana_webhook():
    """Asana webhook handler -- handshake + event processing."""
    import threading

    # Handshake: echo X-Hook-Secret back
    hook_secret = request.headers.get("X-Hook-Secret")
    if hook_secret:
        logger.info("[todo-sync] Asana webhook handshake received, echoing X-Hook-Secret")
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
                    # New task added to project -> create in To-Do
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
                    # change.new_value is intentionally ignored -- Asana sends
                    # None for most fields; actual state is fetched from the API

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
                    # Task removed -> mark To-Do complete (best effort)
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


    force = payload.get("force", False)
    if transcript_id in processed and not force:
        logger.info(f"Webhook: {transcript_id} already processed, skipping")
        return jsonify({"status": "already_processed"})
    if force:
        logger.info(f"Webhook: FORCE reprocess requested for {transcript_id}")

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
    return render_template_string(REVIEW_TEMPLATE, data=data, team_members=TEAM_MEMBERS_LIST)


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


def pulse_weekly_run():
    """Scheduled weekly pulse. Uses same background runner as /pulse/trigger."""
    # Persistent dedupe guard -- survives container restarts (the in-memory
    # _pulse_lock does not). This blocks double-fires from misfire_grace_time,
    # heartbeat re-registration, or external health checks.
    if not pulse_can_run():
        return
    try:
        logger.info("[pulse] Starting scheduled weekly pulse")
        ran = _pulse_run_background(days=PULSE_LOOKBACK_DAYS, dry_run=False)
        # Only set the completion lock when this worker actually ran the
        # pipeline -- a worker that lost the run-lock race must not extend
        # the dedupe window for the winner.
        if ran:
            pulse_set_lock()
            logger.info("[pulse] Weekly pulse complete -- lock set")
    except Exception as e:
        logger.error(f"[pulse] Failed: {e}", exc_info=True)
        # Send failure notification
        try:
            token = get_ms_graph_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            error_payload = {
                "message": {
                    "subject": "[Sara] Weekly Pulse FAILED",
                    "body": {
                        "contentType": "Text",
                        "content": f"Weekly pulse generation failed.\n\nError: {str(e)}\n\nCheck Railway logs for details.",
                    },
                    "toRecipients": [{"emailAddress": {"address": r}} for r in PULSE_RECIPIENTS],
                    "from": {"emailAddress": {"name": BOT_SENDER_NAME, "address": PULSE_SENDER}},
                },
            }
            requests.post(
                f"{MS_GRAPH_BASE}/users/{PULSE_SENDER}/sendMail",
                json=error_payload, headers=headers, timeout=30)
        except Exception:
            logger.error("[pulse] Even the error notification email failed", exc_info=True)


def daily_digest_run():
    """Scheduled daily pipeline digest (daily_pipeline_digest module).

    The module records its own run ledger; a failed run is covered by the
    next run's resilient window, so no retry logic is needed here."""
    if not _digest_lock.acquire(blocking=False):
        logger.warning("[digest] Skipped scheduled run -- digest already running")
        return
    try:
        logger.info("[digest] Starting scheduled daily pipeline digest")
        import daily_pipeline_digest
        daily_pipeline_digest.run_digest()
        logger.info("[digest] Daily pipeline digest complete")
    except Exception as e:
        logger.error(f"[digest] Failed: {e}", exc_info=True)
    finally:
        _digest_lock.release()


def email_sync_run():
    """Scheduled email-pipeline-sync run (email_pipeline_sync module).

    Scans team mailboxes for deal correspondence, logs new deal-relevant
    emails to HubSpot, and emails the run report. The module keeps its own
    run ledger, so a failed run is covered by the next run's lookback window;
    no retry logic is needed here."""
    if not _email_sync_lock.acquire(blocking=False):
        logger.warning("[email-sync] Skipped scheduled run -- email sync already running")
        return
    try:
        logger.info("[email-sync] Starting scheduled email-pipeline-sync")
        import email_pipeline_sync
        email_pipeline_sync.run_daily()
        logger.info("[email-sync] Email-pipeline-sync complete")
    except Exception as e:
        logger.error(f"[email-sync] Failed: {e}", exc_info=True)
    finally:
        _email_sync_lock.release()


def biweekly_update_run():
    """Scheduled biweekly business update (biweekly_business_update module).

    Registered as a weekly Monday cron; the module's cadence gate
    (should_run_biweekly) no-ops on the in-between weeks so this fires every
    other week without ISO-week parity edge cases."""
    import biweekly_business_update
    if not biweekly_business_update.should_run_biweekly():
        logger.info("[biweekly] Off week -- skipping scheduled run")
        return
    if not _biweekly_lock.acquire(blocking=False):
        logger.warning("[biweekly] Skipped scheduled run -- already running")
        return
    try:
        logger.info("[biweekly] Starting scheduled biweekly business update")
        biweekly_business_update.run_biweekly()
        logger.info("[biweekly] Biweekly business update complete")
    except Exception as e:
        logger.error(f"[biweekly] Failed: {e}", exc_info=True)
    finally:
        _biweekly_lock.release()


def corrections_ingest_run():
    """Scheduled scan of Sara's mailbox for reply-corrections (sara_corrections
    module). Stores any new corrections so future reports honor them."""
    if not _corrections_lock.acquire(blocking=False):
        logger.warning("[corrections] Skipped scheduled ingest -- already running")
        return
    try:
        import sara_corrections
        result = sara_corrections.ingest_replies()
        if result.get("added_count"):
            logger.info(f"[corrections] Ingested {result['added_count']} new correction(s)")
    except Exception as e:
        logger.error(f"[corrections] Scheduled ingest failed: {e}", exc_info=True)
    finally:
        _corrections_lock.release()


def learn_weekly_run():
    """Scheduled weekly Read/Learn digest (learn_digest module).

    Dedup + single-send is enforced inside learn_digest.run_learn via its atomic
    O_CREAT|O_EXCL run lock, so no extra lock is needed here."""
    try:
        logger.info("[learn] Starting scheduled weekly Read/Learn digest")
        import learn_digest
        learn_digest.run_learn()
        logger.info("[learn] Weekly Read/Learn digest complete")
    except Exception as e:
        logger.error(f"[learn] Failed: {e}", exc_info=True)


def fyi_daily_run():
    """Scheduled daily FYI Triage (fyi_triage module).

    Passes live=True so the run auto-promotes to real moves the moment Ken sets
    FYI_LIVE=1 (STATE C/D) -- no code change needed. Until then the dual gate
    keeps it DRY (FYI_LIVE unset at ship = STATE A), and a short would-move
    summary is emailed each morning. Uses FYI_LOOKBACK_HOURS (24h). Single-run
    dedup is enforced inside run_fyi via its atomic O_CREAT|O_EXCL lock."""
    try:
        logger.info("[fyi] Starting scheduled daily FYI Triage")
        import fyi_triage
        fyi_triage.run_fyi(live=True, send_summary=True)
        logger.info("[fyi] Daily FYI Triage complete")
    except Exception as e:
        logger.error(f"[fyi] Failed: {e}", exc_info=True)


_scheduler = None  # module-level so diagnostic endpoints can inspect it


def start_scheduler():
    global _scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(poll_and_process, "interval", minutes=POLL_INTERVAL_MINUTES)
    _scheduler.add_job(
        pulse_weekly_run,
        trigger="cron",
        day_of_week="sun",
        hour=19,            # 19:00 UTC = 22:00 Israel
        minute=0,
        id="weekly_pulse",
        replace_existing=True,
        misfire_grace_time=3600,  # 1-hour window to fire if exact time was missed
    )
    _scheduler.add_job(
        email_sync_run,
        trigger="cron",
        hour=3,             # 03:15 UTC = 06:15 Israel (IDT), before the digest reads HubSpot
        minute=15,
        id="email_pipeline_sync",
        replace_existing=True,
        misfire_grace_time=3600,  # 1-hour window to fire if exact time was missed
    )
    _scheduler.add_job(
        daily_digest_run,
        trigger="cron",
        hour=3,             # 03:45 UTC = 06:45 Israel (IDT), after the overnight email sync
        minute=45,
        id="daily_pipeline_digest",
        replace_existing=True,
        misfire_grace_time=3600,  # 1-hour window to fire if exact time was missed
    )
    # Biweekly business update: weekly Monday cron, gated to every other week by
    # should_run_biweekly(). 04:15 UTC = 07:15 Israel (IDT), after the daily digest.
    _scheduler.add_job(
        biweekly_update_run,
        trigger="cron",
        day_of_week="mon",
        hour=4,
        minute=15,
        id="biweekly_business_update",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Corrections ingest: scan Sara's mailbox every 20min for reply-corrections
    # so future pulse/biweekly reports honor Ken's feedback promptly.
    _scheduler.add_job(
        corrections_ingest_run,
        trigger="interval",
        minutes=20,
        id="corrections_ingest",
        replace_existing=True,
    )
    # Read/Learn digest: weekly Friday 06:00 Asia/Jerusalem. tz-aware cron so
    # DST is handled automatically (06:00 Israel = 03:00 UTC summer / 04:00 winter).
    _scheduler.add_job(
        learn_weekly_run,
        trigger="cron",
        day_of_week="fri",
        hour=6,
        minute=0,
        timezone="Asia/Jerusalem",
        id="learn_digest_weekly",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # FYI Triage: daily 06:00 Asia/Jerusalem. tz-aware cron so DST is handled
    # automatically (06:00 Israel = 03:00 UTC summer / 04:00 winter). Ships DRY
    # (FYI_LIVE unset); auto-promotes to live moves once Ken sets FYI_LIVE=1.
    _scheduler.add_job(
        fyi_daily_run,
        trigger="cron",
        hour=6,
        minute=0,
        timezone="Asia/Jerusalem",
        id="fyi_triage_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Renewal job: every 45min, renews if expiry < 30min away (Graph max is 59min for this resource)
    _scheduler.add_job(
        ensure_teams_subscription,
        trigger="interval",
        minutes=45,
        id="teams_subscription_renewal",
        replace_existing=True,
    )
    # Boot job: ensure subscription ~60s after startup so web server is ready for Graph validation
    from apscheduler.triggers.date import DateTrigger as _DateTrigger
    import datetime as _datetime_mod
    boot_run_time = _datetime_mod.datetime.now() + _datetime_mod.timedelta(seconds=60)
    _scheduler.add_job(
        ensure_teams_subscription,
        trigger=_DateTrigger(run_date=boot_run_time),
        id="teams_subscription_boot",
        replace_existing=True,
    )
    _scheduler.start()
    pulse_job = _scheduler.get_job("weekly_pulse")
    next_run = pulse_job.next_run_time if pulse_job else "NOT REGISTERED"
    logger.info(f"[scheduler] Started: polling every {POLL_INTERVAL_MINUTES}m, weekly pulse Sunday 19:00 UTC, email sync daily 03:15 UTC")
    logger.info(f"[scheduler] Weekly pulse job registered: next run = {next_run}")
    logger.info(f"[scheduler] Teams subscription renewal every 12h; boot ensure at {boot_run_time}")


# Start background services for gunicorn (module-level, not just __main__)
# With --workers 1 in Procfile, a threading lock is sufficient to prevent
# double-init.  The old file-based lock on /data/ caused failures because
# Railway's persistent volume retained stale PIDs across container restarts.

_start_lock = _threading.Lock()
_started = False


def _start_background_services():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        start_scheduler()
        if ASANA_PROJECT_GID and MS_GRAPH_CLIENT_ID:
            start_todo_poller()


def _should_start_background_services():
    """Decide whether to start the scheduler and pollers at import time.

    RUN_SCHEDULER=1 forces on (set this in the Railway service variables).
    RUN_SCHEDULER=0 forces off. When unset, autodetect gunicorn so that a
    plain 'import app' (tests, scripts) never starts background threads.
    """
    flag = os.environ.get("RUN_SCHEDULER")
    if flag is not None:
        return flag == "1"
    import sys as _sys
    return "gunicorn" in os.path.basename(_sys.argv[0] or "").lower()


if _should_start_background_services():
    _start_background_services()

# ======================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting Post-Meeting Intelligence Pipeline v2 on port {port}")
    _start_background_services()
    app.run(host="0.0.0.0", port=port)







