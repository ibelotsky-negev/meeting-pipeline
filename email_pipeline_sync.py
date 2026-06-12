#!/usr/bin/env python3
"""
email-pipeline-sync -- Sara module

Scans Outlook mailboxes (Microsoft Graph, app-only auth) for correspondence
with contacts associated to deals in the NL 2026 Fundraise HubSpot pipeline,
classifies each email as deal-relevant via Claude, and logs relevant emails
to HubSpot as email engagements -- only if not already logged.

Compensates for HubSpot's native Outlook logging, which fires inconsistently.

Usage:
    python email_pipeline_sync.py                      # daily window (last 3 days)
    python email_pipeline_sync.py --since 2026-05-01   # backfill
    python email_pipeline_sync.py --dry-run            # no HubSpot writes, no report email
    python email_pipeline_sync.py --mailbox bk@negevlabs.com   # restrict mailboxes

Spec: email-pipeline-sync-spec.md in this repo.
Shares Railway env credentials with app.py (HUBSPOT_API_KEY, CLAUDE_API_KEY,
MS_GRAPH_CLIENT_ID/SECRET/TENANT_ID, BOT_SENDER_EMAIL). No new dependencies.

Author: Negev Labs
"""

import os
import re
import json
import html
import time
import sqlite3
import logging
import argparse
import uuid
from datetime import datetime, timedelta, timezone

import requests

# Local testing convenience: load credentials from .env in the project dir
# (Railway injects real env vars in production; .env is gitignored)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("email-pipeline-sync")

# ======================================================================
#  CONFIG
# ======================================================================

HUBSPOT_BASE = "https://api.hubapi.com"
MS_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# NL 2026 Fundraise pipeline (all stages, including Closed Lost -- re-engagement
# history is part of the record)
PIPELINE_ID = os.environ.get("EMAIL_SYNC_PIPELINE_ID", "3760999624")

# Internal senders/recipients -- an email is in scope only when a roster contact
# appears on the thread AND at least one of these addresses is present.
INTERNAL_TEAM_EMAILS = [
    "bk@negevlabs.com",
    "bk@negevcap.com",
    "ak@negevcap.com",
    "vu@negevcap.com",
    "shlomi@negevlabs.com",
    "shlomi@ariadnebio.com",
]

# Mailboxes scanned each run. shlomi@ariadnebio.com is a separate tenant and may
# fail with 403/404 until access is arranged -- the run degrades gracefully and
# reports which mailboxes were skipped.
DEFAULT_MAILBOXES = list(INTERNAL_TEAM_EMAILS)

REPORT_RECIPIENT = os.environ.get("EMAIL_SYNC_REPORT_TO", "bk@negevlabs.com")
CLASSIFIER_MODEL = os.environ.get("EMAIL_SYNC_MODEL", "claude-haiku-4-5-20251001")

# Ledger lives on the Railway persistent volume when available, else project dir
LEDGER_PATH = (
    "/data/email_pipeline_sync.db"
    if os.path.isdir("/data")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_pipeline_sync.db")
)

# Per-(mailbox, address) page cap. Not a silent cap -- a warning is logged and
# the run report flags it when hit.
MAX_MESSAGES_PER_SEARCH = int(os.environ.get("EMAIL_SYNC_MAX_PER_SEARCH", "500"))

# Fundraise context injected into the classifier prompt
PIPELINE_CONTEXT = (
    "NL 2026 Fundraise: Negev Labs Class B funding round. Topics that signal deal "
    "relevance include: investment amounts and allocations, re-ups by existing "
    "investors, KYC and AML documentation, UPA (Unit Purchase Agreement) drafts and "
    "negotiation, wire instructions and wire confirmations, shareholder letters, "
    "subscription documents, data room access, cap table, valuation, MJFF "
    "(Michael J. Fox Foundation) grant co-funding, term discussions, closing "
    "timelines, and scheduling of calls explicitly about the investment. "
    "Correspondence may be in English or Russian."
)

# ======================================================================
#  HTTP HELPERS (retry on 429/5xx, shared by HubSpot and Graph)
# ======================================================================


def _request_with_retry(method: str, url: str, headers: dict, json_body: dict = None,
                        params: dict = None, ok_statuses=()) -> requests.Response:
    """HTTP request with 3x retry on 429/5xx (5s/10s/15s backoff)."""
    last_exc = None
    for attempt, wait in enumerate([0, 5, 10, 15]):
        if wait:
            time.sleep(wait)
        try:
            resp = requests.request(method, url, json=json_body, params=params,
                                    headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                logger.warning(f"{method} {url} -> {resp.status_code}, retrying ({attempt + 1}/3)")
                continue
            if resp.status_code in ok_statuses:
                return resp
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            logger.warning(f"{method} {url} attempt {attempt + 1} failed: {e}")
    raise last_exc


def hubspot_request(method: str, endpoint: str, data: dict = None, params: dict = None) -> dict:
    api_key = os.environ.get("HUBSPOT_API_KEY", "")
    if not api_key:
        raise RuntimeError("HUBSPOT_API_KEY not set")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    resp = _request_with_retry(method, f"{HUBSPOT_BASE}{endpoint}", headers, data, params)
    return resp.json() if resp.content else {}


_graph_token_cache = {"token": None, "expires_at": 0}


def get_graph_token() -> str:
    """App-only token (client_credentials) -- required for multi-mailbox access."""
    now = time.time()
    if _graph_token_cache["token"] and _graph_token_cache["expires_at"] > now + 60:
        return _graph_token_cache["token"]
    tenant = os.environ.get("MS_GRAPH_TENANT_ID", "")
    data = {
        "client_id": os.environ.get("MS_GRAPH_CLIENT_ID", ""),
        "client_secret": os.environ.get("MS_GRAPH_CLIENT_SECRET", ""),
        "grant_type": "client_credentials",
        "scope": "https://graph.microsoft.com/.default",
    }
    if not (tenant and data["client_id"] and data["client_secret"]):
        raise RuntimeError("MS_GRAPH_CLIENT_ID / MS_GRAPH_CLIENT_SECRET / MS_GRAPH_TENANT_ID not set")
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=data, timeout=15
    )
    resp.raise_for_status()
    token_data = resp.json()
    _graph_token_cache["token"] = token_data["access_token"]
    _graph_token_cache["expires_at"] = now + token_data.get("expires_in", 3600)
    return _graph_token_cache["token"]


def graph_get(url: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {get_graph_token()}"}
    resp = _request_with_retry("GET", url, headers, params=params)
    return resp.json() if resp.content else {}


def graph_post(url: str, json_body: dict) -> dict:
    headers = {"Authorization": f"Bearer {get_graph_token()}", "Content-Type": "application/json"}
    resp = _request_with_retry("POST", url, headers, json_body)
    return resp.json() if resp.content else {}


# ======================================================================
#  RUN LEDGER (SQLite)
# ======================================================================


def ledger_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(LEDGER_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS processed_messages (
            internet_message_id TEXT PRIMARY KEY,
            mailbox TEXT,
            contact_emails TEXT,
            subject TEXT,
            outcome TEXT,
            detail TEXT,
            run_id TEXT,
            received_at TEXT,
            processed_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            window_since TEXT,
            window_until TEXT,
            scanned INTEGER,
            logged INTEGER,
            duplicates INTEGER,
            irrelevant INTEGER,
            uncertain INTEGER,
            errors INTEGER,
            dry_run INTEGER
        )"""
    )
    conn.commit()
    return conn


def ledger_get(conn: sqlite3.Connection, internet_message_id: str):
    """(outcome, detail) for a processed message, or None if never processed."""
    return conn.execute(
        "SELECT outcome, detail FROM processed_messages WHERE internet_message_id = ?",
        (internet_message_id,),
    ).fetchone()


def ledger_seen(conn: sqlite3.Connection, internet_message_id: str) -> bool:
    return ledger_get(conn, internet_message_id) is not None


def ledger_update_outcome(conn: sqlite3.Connection, internet_message_id: str,
                          outcome: str, detail: str):
    conn.execute(
        "UPDATE processed_messages SET outcome = ?, detail = ? WHERE internet_message_id = ?",
        (outcome, (detail or "")[:500], internet_message_id),
    )
    conn.commit()


def ledger_record(conn: sqlite3.Connection, msg: dict, outcome: str, detail: str, run_id: str,
                  dry_run: bool = False):
    # Dry runs never write message rows -- otherwise the next real run would
    # treat everything as already processed and skip it
    if dry_run:
        return
    conn.execute(
        "INSERT OR REPLACE INTO processed_messages VALUES (?,?,?,?,?,?,?,?,?)",
        (
            msg["internet_message_id"],
            msg.get("mailbox", ""),
            ",".join(sorted(msg.get("matched_contacts", {}).keys())),
            (msg.get("subject") or "")[:300],
            outcome,
            (detail or "")[:500],
            run_id,
            msg.get("received", ""),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


# ======================================================================
#  PIPELINE ROSTER BUILDER
# ======================================================================


def get_stage_labels() -> dict:
    """Map dealstage internal IDs to human labels for the classifier prompt."""
    labels = {}
    try:
        data = hubspot_request("GET", f"/crm/v3/pipelines/deals/{PIPELINE_ID}")
        for stage in data.get("stages") or []:
            labels[stage.get("id", "")] = stage.get("label", "")
    except Exception as e:
        logger.warning(f"Could not fetch stage labels: {e}")
    return labels


def get_pipeline_deals() -> list:
    """All deals in the pipeline, every stage included."""
    deals, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_ID}
            ]}],
            "properties": ["dealname", "dealstage", "pipeline"],
            "limit": 100,
        }
        if after:
            body["after"] = after
        result = hubspot_request("POST", "/crm/v3/objects/deals/search", body)
        deals.extend(result.get("results") or [])
        after = ((result.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return deals


def get_associated_ids(from_type: str, from_id: str, to_type: str) -> list:
    """v4 associations, paginated. Returns toObjectId strings."""
    ids, url = [], f"{HUBSPOT_BASE}/crm/v4/objects/{from_type}/{from_id}/associations/{to_type}"
    params = {"limit": 500}
    api_key = os.environ.get("HUBSPOT_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"}
    while url:
        resp = _request_with_retry("GET", url, headers, params=params)
        data = resp.json() if resp.content else {}
        for r in data.get("results") or []:
            ids.append(str(r.get("toObjectId", "")))
        url = ((data.get("paging") or {}).get("next") or {}).get("link")
        params = None
    return [i for i in ids if i]


def batch_read_contacts(contact_ids: list) -> list:
    results = []
    for i in range(0, len(contact_ids), 100):
        chunk = contact_ids[i:i + 100]
        body = {
            "properties": ["email", "hs_additional_emails", "firstname", "lastname", "company"],
            "inputs": [{"id": cid} for cid in chunk],
        }
        result = hubspot_request("POST", "/crm/v3/objects/contacts/batch/read", body)
        results.extend(result.get("results") or [])
    return results


def contact_email_addresses(props: dict) -> list:
    """Primary email plus hs_additional_emails (semicolon-separated).
    Lowercased, deduped, order preserved (primary first)."""
    addresses = []
    primary = (props.get("email") or "").strip().lower()
    if primary:
        addresses.append(primary)
    for extra in (props.get("hs_additional_emails") or "").split(";"):
        extra = extra.strip().lower()
        if extra and extra not in addresses:
            addresses.append(extra)
    return addresses


def build_roster() -> dict:
    """contact email -> {contact_id, name, deals: [{id, name, stage_label}]}"""
    stage_labels = get_stage_labels()
    deals = get_pipeline_deals()
    logger.info(f"Pipeline {PIPELINE_ID}: {len(deals)} deals")

    contact_to_deals = {}
    deal_info = {}
    for deal in deals:
        deal_id = str(deal.get("id", ""))
        props = deal.get("properties") or {}
        deal_info[deal_id] = {
            "id": deal_id,
            "name": props.get("dealname") or "",
            "stage_label": stage_labels.get(props.get("dealstage") or "", props.get("dealstage") or ""),
        }
        for cid in get_associated_ids("deals", deal_id, "contacts"):
            contact_to_deals.setdefault(cid, []).append(deal_id)

    roster = {}
    contact_count = 0
    internal = set(INTERNAL_TEAM_EMAILS)
    for contact in batch_read_contacts(list(contact_to_deals.keys())):
        cid = str(contact.get("id", ""))
        props = contact.get("properties") or {}
        # Internal team members associated to deals are not roster contacts
        addresses = [a for a in contact_email_addresses(props) if a not in internal]
        if not addresses:
            continue
        name = " ".join(p for p in [props.get("firstname"), props.get("lastname")] if p) or addresses[0]
        entry = {
            "contact_id": cid,
            "name": name,
            "deals": [deal_info[d] for d in contact_to_deals.get(cid, []) if d in deal_info],
        }
        contact_count += 1
        # Aliases (hs_additional_emails) share the contact entry so mail from
        # any of the contact's addresses is scanned and attributed correctly
        for address in addresses:
            roster[address] = entry
    logger.info(f"Roster: {contact_count} contacts, {len(roster)} email addresses")
    return roster


# ======================================================================
#  MAILBOX SCANNER (Microsoft Graph, app-only)
# ======================================================================


def html_to_text(content: str) -> str:
    if not content:
        return ""
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", content, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def extract_addresses(msg: dict) -> tuple:
    """(from_address, all_participant_addresses) lowercased."""
    frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    participants = {frm.lower()} if frm else set()
    for field in ("toRecipients", "ccRecipients"):
        for r in msg.get(field) or []:
            addr = ((r.get("emailAddress") or {}).get("address") or "").lower()
            if addr:
                participants.add(addr)
    return frm.lower(), participants


def scan_mailbox(mailbox: str, roster: dict, since: str, until: str, caps_hit: list) -> dict:
    """Search one mailbox for messages involving any roster address.
    Returns {internetMessageId: message_record}. Raises on auth/access errors
    (caller handles graceful degradation)."""
    found = {}
    select = ("id,internetMessageId,subject,from,toRecipients,ccRecipients,"
              "receivedDateTime,body")
    for address in roster:
        search = f'"participants:{address} AND received>={since} AND received<{until}"'
        url = f"{MS_GRAPH_BASE}/users/{mailbox}/messages"
        params = {"$search": search, "$select": select, "$top": 50}
        count = 0
        while url:
            data = graph_get(url, params=params)
            params = None
            for msg in data.get("value") or []:
                imid = msg.get("internetMessageId") or ""
                if not imid:
                    continue
                # Calendar responses (Accepted/Declined/Tentative) are timeline
                # noise -- excluded. Meeting invites themselves stay in scope.
                if (msg.get("@odata.type") or "").endswith("eventMessageResponse"):
                    continue
                frm, participants = extract_addresses(msg)
                # Graph $search can fuzzy-match; require the address literally present
                if address not in participants:
                    continue
                record = found.get(imid)
                if not record:
                    record = {
                        "internet_message_id": imid,
                        "graph_id": msg.get("id", ""),
                        "mailbox": mailbox,
                        "subject": msg.get("subject") or "",
                        "from": frm,
                        "participants": participants,
                        "received": msg.get("receivedDateTime") or "",
                        "body_text": html_to_text(((msg.get("body") or {}).get("content")) or ""),
                        "matched_contacts": {},
                    }
                    found[imid] = record
                record["matched_contacts"][address] = roster[address]
                count += 1
            url = data.get("@odata.nextLink")
            if count >= MAX_MESSAGES_PER_SEARCH and url:
                logger.warning(f"Cap {MAX_MESSAGES_PER_SEARCH} hit for {mailbox} x {address}; remaining pages dropped")
                caps_hit.append(f"{mailbox} x {address}")
                url = None
    return found


# ======================================================================
#  HUBSPOT DEDUPE CHECKER
# ======================================================================

_contact_engagements_cache = {}


def get_contact_email_engagements(contact_id: str) -> list:
    """Existing email engagements on a contact: [{message_id, subject, ts_ms, from}]."""
    if contact_id in _contact_engagements_cache:
        return _contact_engagements_cache[contact_id]
    email_ids = get_associated_ids("contacts", contact_id, "emails")
    engagements = []
    for i in range(0, len(email_ids), 100):
        chunk = email_ids[i:i + 100]
        body = {
            "properties": ["hs_email_message_id", "hs_email_subject", "hs_timestamp", "hs_email_headers"],
            "inputs": [{"id": eid} for eid in chunk],
        }
        result = hubspot_request("POST", "/crm/v3/objects/emails/batch/read", body)
        for item in result.get("results") or []:
            props = item.get("properties") or {}
            from_addr = ""
            try:
                headers = json.loads(props.get("hs_email_headers") or "{}")
                from_addr = ((headers.get("from") or {}).get("email") or "").lower()
            except (json.JSONDecodeError, TypeError):
                pass
            ts_ms = 0
            ts_raw = props.get("hs_timestamp") or ""
            try:
                ts_ms = int(ts_raw)
            except (ValueError, TypeError):
                try:
                    ts_ms = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp() * 1000)
                except (ValueError, TypeError):
                    pass
            engagements.append({
                "id": str(item.get("id", "")),
                "message_id": (props.get("hs_email_message_id") or "").strip(),
                "subject": (props.get("hs_email_subject") or "").strip().lower(),
                "ts_ms": ts_ms,
                "from": from_addr,
            })
    _contact_engagements_cache[contact_id] = engagements
    return engagements


def find_logged_engagement(msg: dict, contact_id: str) -> str:
    """Engagement id of an already-logged copy of this email, or empty string.
    Primary: internetMessageId match. Fallback: subject + timestamp +-2 min + sender."""
    existing = get_contact_email_engagements(contact_id)
    imid = msg["internet_message_id"]
    for e in existing:
        if e["message_id"] and e["message_id"] == imid:
            return e["id"]
    try:
        msg_ts = int(datetime.fromisoformat(msg["received"].replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return ""
    subject = (msg.get("subject") or "").strip().lower()
    for e in existing:
        if e["message_id"]:
            continue  # had a message id and it did not match
        if e["subject"] == subject and abs(e["ts_ms"] - msg_ts) <= 120_000:
            if not e["from"] or e["from"] == msg.get("from", ""):
                return e["id"]
    return ""


# ======================================================================
#  RELEVANCE CLASSIFIER (Claude)
# ======================================================================


def classify_email(msg: dict) -> tuple:
    """Returns (label, reason). label in DEAL_RELEVANT / NOT_RELEVANT / UNCERTAIN."""
    import anthropic

    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")

    deal_lines = []
    for entry in msg["matched_contacts"].values():
        for deal in entry["deals"]:
            deal_lines.append(f"- Contact {entry['name']}: deal \"{deal['name']}\" (stage: {deal['stage_label']})")
    deals_block = "\n".join(deal_lines) or "- (no deal context resolved)"

    body_excerpt = (msg.get("body_text") or "")[:8000]
    prompt = f"""You are classifying whether an email belongs on a fundraising deal timeline in a CRM.

Pipeline context: {PIPELINE_CONTEXT}

Deal context for the contact(s) on this email:
{deals_block}

Email:
From: {msg.get('from', '')}
Subject: {msg.get('subject', '')}
Date: {msg.get('received', '')}
Body:
{body_excerpt}

Classify the email:
- DEAL_RELEVANT: relates to the contact's deal in this fundraise (terms, documents, KYC, wires, scheduling investment calls, diligence, updates about the round).
- NOT_RELEVANT: personal topics, other ventures or deals outside this pipeline, newsletters, unrelated business.
- UNCERTAIN: genuinely ambiguous; a human should review.

Stage matters: when the contact's deal is in an early stage (Outreach, Discovery,
Engaged), outreach from the team to the contact -- proposing an update call,
scheduling time, re-engaging the relationship -- IS the deal activity and counts
as DEAL_RELEVANT even when the round is not mentioned explicitly. Only treat
team-to-contact outreach as NOT_RELEVANT when it is clearly about an unrelated
matter (a personal topic, the contact's own other business, logistics of another
engagement).

Negev Labs is a venture studio: updates to fundraise contacts about its portfolio
companies (e.g., Ariadne Bio) are investor-relations content for this round and
count as DEAL_RELEVANT. Introductions connecting prospective investors or their
advisors with Negev Labs also count as DEAL_RELEVANT. Only ventures unrelated to
Negev Labs (e.g., the contact's own separate deals) are NOT_RELEVANT.

Respond with ONLY a JSON object: {{"classification": "DEAL_RELEVANT|NOT_RELEVANT|UNCERTAIN", "reason": "<one sentence>"}}"""

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return "UNCERTAIN", f"classifier returned non-JSON output: {text[:120]}"
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "UNCERTAIN", f"classifier JSON parse failed: {text[:120]}"
    label = (parsed.get("classification") or "").strip().upper()
    reason = (parsed.get("reason") or "").strip()
    if label not in ("DEAL_RELEVANT", "NOT_RELEVANT", "UNCERTAIN"):
        return "UNCERTAIN", f"unexpected label '{label}': {reason}"
    return label, reason


# ======================================================================
#  HUBSPOT LOGGER
# ======================================================================


def create_default_association(email_id: str, to_type: str, to_id: str):
    """v4 default association -- avoids hardcoding association type IDs."""
    api_key = os.environ.get("HUBSPOT_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{HUBSPOT_BASE}/crm/v4/objects/emails/{email_id}/associations/default/{to_type}/{to_id}"
    _request_with_retry("PUT", url, headers)


def ensure_engagement_associations(email_id: str, msg: dict) -> list:
    """Associate an engagement to matched contacts + their deals + companies.
    PUT default association is idempotent, so this doubles as the repair path
    for partial writes. Returns failed targets as 'type:id' strings."""
    failed = []
    associated_companies = set()
    for entry in msg["matched_contacts"].values():
        contact_id = entry["contact_id"]
        targets = [("contacts", contact_id)]
        targets += [("deals", deal["id"]) for deal in entry["deals"]]
        try:
            for company_id in get_associated_ids("contacts", contact_id, "companies")[:1]:
                if company_id not in associated_companies:
                    targets.append(("companies", company_id))
                    associated_companies.add(company_id)
        except Exception as e:
            logger.warning(f"Company lookup failed for contact {contact_id}: {e}")
            failed.append(f"companies:?contact={contact_id}")
        for to_type, to_id in targets:
            try:
                create_default_association(email_id, to_type, to_id)
            except Exception as e:
                logger.warning(f"Association {to_type}:{to_id} failed for engagement {email_id}: {e}")
                failed.append(f"{to_type}:{to_id}")
    return failed


def log_email_to_hubspot(msg: dict) -> tuple:
    """Create email engagement, associate to matched contacts + their deals + companies.
    Returns (engagement_id, failed_association_targets). Association failures do
    not abort the write -- they are recorded for retry on the next run."""
    try:
        ts_ms = int(datetime.fromisoformat(msg["received"].replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    direction = "EMAIL" if msg.get("from", "") in INTERNAL_TEAM_EMAILS else "INCOMING_EMAIL"
    to_addrs = sorted(a for a in msg.get("participants") or set() if a != msg.get("from", ""))
    headers_json = json.dumps({
        "from": {"email": msg.get("from", "")},
        "to": [{"email": a} for a in to_addrs],
        "cc": [],
        "bcc": [],
    })

    properties = {
        "hs_timestamp": str(ts_ms),
        "hs_email_direction": direction,
        "hs_email_status": "SENT",
        "hs_email_subject": (msg.get("subject") or "")[:1000],
        "hs_email_text": (msg.get("body_text") or "")[:60000],
        "hs_email_message_id": msg["internet_message_id"],
        "hs_email_headers": headers_json,
    }
    created = hubspot_request("POST", "/crm/v3/objects/emails", {"properties": properties})
    email_id = str(created.get("id", ""))
    failed = ensure_engagement_associations(email_id, msg)
    return email_id, failed


# ======================================================================
#  RUN REPORT
# ======================================================================


def build_report(stats: dict, uncertain: list, skipped_mailboxes: list, caps_hit: list,
                 window: str, dry_run: bool) -> str:
    lines = [
        f"email-pipeline-sync run report {'(DRY RUN) ' if dry_run else ''}-- window {window}",
        "",
        f"Scanned:    {stats['scanned']}",
        f"Logged:     {stats['logged']}",
        f"Duplicates: {stats['duplicates']}",
        f"Irrelevant: {stats['irrelevant']}",
        f"Uncertain:  {stats['uncertain']}",
        f"Repaired:   {stats.get('repaired', 0)}",
        f"Errors:     {stats['errors']}",
    ]
    if stats.get("partial"):
        lines.append(f"Partial:    {stats['partial']} (associations failed, auto-retry next run)")
    if skipped_mailboxes:
        lines += ["", "Mailboxes skipped (no access):"]
        lines += [f"  - {m}" for m in skipped_mailboxes]
    if caps_hit:
        lines += ["", f"Search cap ({MAX_MESSAGES_PER_SEARCH}) hit -- some messages were not scanned:"]
        lines += [f"  - {c}" for c in caps_hit]
    if uncertain:
        lines += ["", "UNCERTAIN -- human review needed (not logged):"]
        for u in uncertain:
            contacts = ", ".join(sorted(u["matched_contacts"].keys()))
            deals = ", ".join(d["name"] for e in u["matched_contacts"].values() for d in e["deals"])
            lines.append(f"  - [{u.get('received', '')[:10]}] \"{u.get('subject', '')}\"")
            lines.append(f"    contact(s): {contacts} | deal(s): {deals}")
            lines.append(f"    reason: {u.get('uncertain_reason', '')}")
    return "\n".join(lines)


def send_report_email(report: str, window: str):
    sender = os.environ.get("BOT_SENDER_EMAIL", "")
    if not sender:
        logger.warning("BOT_SENDER_EMAIL not set -- report not emailed")
        return
    body = {
        "message": {
            "subject": f"[email-pipeline-sync] Run report {window}",
            "body": {"contentType": "text", "content": report},
            "toRecipients": [{"emailAddress": {"address": REPORT_RECIPIENT}}],
        },
        "saveToSentItems": False,
    }
    graph_post(f"{MS_GRAPH_BASE}/users/{sender}/sendMail", body)
    logger.info(f"Run report emailed to {REPORT_RECIPIENT}")


# ======================================================================
#  RUN ORCHESTRATION
# ======================================================================


def run_sync(since: str, until: str, mailboxes: list, dry_run: bool, send_report: bool) -> dict:
    run_id = uuid.uuid4().hex[:12]
    started_at = datetime.now(timezone.utc).isoformat()
    window = f"{since} .. {until}"
    logger.info(f"Run {run_id} starting: window {window}, mailboxes {mailboxes}, dry_run={dry_run}")

    conn = ledger_connect()
    roster = build_roster()
    if not roster:
        logger.warning("Empty roster -- nothing to scan")

    # Scan all mailboxes; merge on internetMessageId so cross-mailbox copies of
    # the same email are processed once with all matched contacts attached.
    messages = {}
    skipped_mailboxes, caps_hit = [], []
    for mailbox in mailboxes:
        try:
            found = scan_mailbox(mailbox, roster, since, until, caps_hit)
            logger.info(f"{mailbox}: {len(found)} candidate messages")
            for imid, record in found.items():
                if imid in messages:
                    messages[imid]["matched_contacts"].update(record["matched_contacts"])
                else:
                    messages[imid] = record
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in (403, 404):
                logger.warning(f"{mailbox}: no access ({status}) -- skipped")
                skipped_mailboxes.append(mailbox)
            else:
                raise

    stats = {"scanned": 0, "logged": 0, "duplicates": 0, "irrelevant": 0,
             "uncertain": 0, "repaired": 0, "partial": 0, "errors": 0}
    uncertain_msgs = []
    internal = set(INTERNAL_TEAM_EMAILS)

    # Date-ordered processing (oldest first) for backfill sanity
    ordered = sorted(messages.values(), key=lambda m: m.get("received", ""))
    for msg in ordered:
        stats["scanned"] += 1
        imid = msg["internet_message_id"]

        prior = ledger_get(conn, imid)
        if prior:
            outcome, detail = prior
            # Self-heal: engagement was created but some associations failed
            # in a previous run -- retry them (PUT default assoc is idempotent)
            if outcome == "logged-partial" and not dry_run:
                m = re.search(r"engagement=(\d+)", detail or "")
                if m:
                    still_failed = ensure_engagement_associations(m.group(1), msg)
                    if still_failed:
                        stats["partial"] += 1
                        ledger_update_outcome(conn, imid, "logged-partial",
                                              f"engagement={m.group(1)}; failed={','.join(still_failed)}")
                    else:
                        stats["repaired"] += 1
                        ledger_update_outcome(conn, imid, "logged",
                                              f"engagement={m.group(1)}; associations repaired")
                        logger.info(f"Repaired associations on engagement {m.group(1)}: \"{msg.get('subject', '')}\"")
            continue  # processed in a previous run; not counted as duplicate

        # Scope check: at least one internal party on the thread
        if not (msg.get("participants") or set()) & internal:
            ledger_record(conn, msg, "skipped-irrelevant", "no internal party on thread", run_id, dry_run)
            stats["irrelevant"] += 1
            continue

        try:
            # Dedupe against HubSpot: already logged on any matched contact
            # means skip -- re-logging would duplicate it on that timeline.
            # But verify the deal associations first: a partial write of ours
            # (or native HubSpot logging) may have linked the contact only.
            existing_id = ""
            for entry in msg["matched_contacts"].values():
                existing_id = find_logged_engagement(msg, entry["contact_id"])
                if existing_id:
                    break
            if existing_id:
                expected_deals = {d["id"] for e in msg["matched_contacts"].values() for d in e["deals"]}
                linked_deals = set(get_associated_ids("emails", existing_id, "deals"))
                if expected_deals - linked_deals:
                    label, reason = classify_email(msg)
                    if label == "DEAL_RELEVANT":
                        if dry_run:
                            logger.info(f"[dry-run] would repair deal links on engagement {existing_id}: \"{msg.get('subject', '')}\"")
                        else:
                            ensure_engagement_associations(existing_id, msg)
                            logger.info(f"Repaired deal links on engagement {existing_id}: \"{msg.get('subject', '')}\"")
                        ledger_record(conn, msg, "repaired-associations",
                                      f"engagement={existing_id}; {reason}", run_id, dry_run)
                        stats["repaired"] += 1
                        continue
                ledger_record(conn, msg, "skipped-duplicate",
                              f"engagement={existing_id} already in HubSpot", run_id, dry_run)
                stats["duplicates"] += 1
                continue

            label, reason = classify_email(msg)
            if label == "NOT_RELEVANT":
                ledger_record(conn, msg, "skipped-irrelevant", reason, run_id, dry_run)
                stats["irrelevant"] += 1
            elif label == "UNCERTAIN":
                msg["uncertain_reason"] = reason
                uncertain_msgs.append(msg)
                ledger_record(conn, msg, "flagged-uncertain", reason, run_id, dry_run)
                stats["uncertain"] += 1
            else:
                if dry_run:
                    logger.info(f"[dry-run] would log: \"{msg.get('subject', '')}\" ({reason})")
                    ledger_record(conn, msg, "logged", reason, run_id, dry_run)
                else:
                    engagement_id, failed = log_email_to_hubspot(msg)
                    if failed:
                        # Engagement exists; failed associations retry next run
                        logger.warning(f"Logged engagement {engagement_id} with failed associations {failed}: \"{msg.get('subject', '')}\"")
                        ledger_record(conn, msg, "logged-partial",
                                      f"engagement={engagement_id}; failed={','.join(failed)}", run_id, dry_run)
                        stats["partial"] += 1
                    else:
                        logger.info(f"Logged engagement {engagement_id}: \"{msg.get('subject', '')}\"")
                        ledger_record(conn, msg, "logged",
                                      f"engagement={engagement_id}; {reason}", run_id, dry_run)
                stats["logged"] += 1
        except Exception as e:
            logger.error(f"Error processing \"{msg.get('subject', '')}\": {e}")
            stats["errors"] += 1
            # Not recorded in ledger -- will be retried next run

    conn.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, started_at, datetime.now(timezone.utc).isoformat(), since, until,
         stats["scanned"], stats["logged"], stats["duplicates"], stats["irrelevant"],
         stats["uncertain"], stats["errors"], 1 if dry_run else 0),
    )
    conn.commit()
    conn.close()

    report = build_report(stats, uncertain_msgs, skipped_mailboxes, caps_hit, window, dry_run)
    print("\n" + report + "\n")
    if send_report and not dry_run:
        try:
            send_report_email(report, window)
        except Exception as e:
            logger.error(f"Report email failed: {e}")
    return stats


def main():
    parser = argparse.ArgumentParser(description="Sync deal emails from Outlook to HubSpot")
    parser.add_argument("--since", help="Window start YYYY-MM-DD (backfill mode). Default: 3 days ago")
    parser.add_argument("--until", help="Window end YYYY-MM-DD exclusive. Default: tomorrow")
    parser.add_argument("--mailbox", action="append", dest="mailboxes",
                        help="Restrict to specific mailbox(es); repeatable")
    parser.add_argument("--dry-run", action="store_true",
                        help="No HubSpot writes, no report email, no ledger message rows")
    parser.add_argument("--no-report", action="store_true", help="Skip the report email")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    since = args.since or (today - timedelta(days=3)).isoformat()
    until = args.until or (today + timedelta(days=1)).isoformat()
    mailboxes = args.mailboxes or DEFAULT_MAILBOXES

    run_sync(since, until, mailboxes, dry_run=args.dry_run, send_report=not args.no_report)


if __name__ == "__main__":
    main()
