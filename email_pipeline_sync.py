#!/usr/bin/env python3
"""
email-pipeline-sync -- Sara module

Scans Outlook mailboxes (Microsoft Graph, app-only auth) for correspondence
with contacts associated to deals in the fundraise HubSpot pipelines (PL 2027
Fundraise for Palomar Labs, Ariadne 2027 Fundraise for Ariadne Bio), classifies
each email as deal-relevant via Claude AND routes it to ONE of those pipelines,
and logs relevant emails to HubSpot as email engagements linked only to that
pipeline's deals -- only if not already logged.

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

# PL 2027 Fundraise pipeline (all stages, including Closed Lost -- re-engagement
# history is part of the record). Replaced PL 2026 Fundraise (3760999624) on
# 2026-10-25.
PIPELINE_ID = os.environ.get("EMAIL_SYNC_PIPELINE_ID", "4252440818")
# Ariadne 2027 Fundraise -- Ariadne Bio's own round, a separate pipeline.
ARIADNE_PIPELINE_ID = os.environ.get("EMAIL_SYNC_ARIADNE_PIPELINE_ID", "4283794618")

# The fundraise pipelines this module (and daily_pipeline_digest) cover. Each
# email is routed to exactly ONE of them by the classifier: an engagement is
# associated only to the matched contacts' deals IN THAT pipeline, never to a
# contact's deals in the other one. `key` is the stable routing token stored in
# the ledger and returned by the classifier -- never rename it.
PIPELINES = [
    {
        "key": "palomar",
        "label": "Palomar",
        "name": "PL 2027 Fundraise",
        "id": PIPELINE_ID,
        "context": (
            "Palomar Labs (formerly Negev Labs) studio-level funding round. Topics: "
            "investment amounts and allocations in Palomar Labs, re-ups by existing "
            "Palomar/Negev investors, KYC and AML documentation, UPA (Unit Purchase "
            "Agreement) drafts and negotiation, wire instructions and confirmations, "
            "shareholder letters, subscription documents, data room access, cap table, "
            "valuation, term discussions, closing timelines, scheduling of calls about "
            "investing in Palomar Labs, and studio/portfolio updates sent to Palomar "
            "investors as investor relations."
        ),
    },
    {
        "key": "ariadne",
        "label": "Ariadne",
        "name": "Ariadne 2027 Fundraise",
        "id": ARIADNE_PIPELINE_ID,
        "context": (
            "Ariadne Bio's own funding round (Ariadne Bio is a CNS biotech, a Palomar "
            "Labs portfolio company). Topics: investing directly in Ariadne Bio, its "
            "round terms, SAFE/equity documents, Ariadne data room and diligence "
            "(science, clinical plan, IP), Ariadne co-investors such as Tetrad VC, "
            "MJFF (Michael J. Fox Foundation) grant co-funding, European non-dilutive "
            "grants (FFG, EIC), Ariadne KYC/wires/closing, and scheduling of calls "
            "about investing in Ariadne Bio."
        ),
    },
]
PIPELINES_BY_KEY = {p["key"]: p for p in PIPELINES}

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


def graph_patch(url: str, json_body: dict) -> dict:
    headers = {"Authorization": f"Bearer {get_graph_token()}", "Content-Type": "application/json"}
    resp = _request_with_retry("PATCH", url, headers, json_body)
    return resp.json() if resp.content else {}


def graph_delete(url: str) -> dict:
    headers = {"Authorization": f"Bearer {get_graph_token()}"}
    resp = _request_with_retry("DELETE", url, headers)
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


def get_stage_labels(pipeline_id: str = None) -> dict:
    """Map dealstage internal IDs to human labels for the classifier prompt."""
    pipeline_id = pipeline_id or PIPELINE_ID
    labels = {}
    try:
        data = hubspot_request("GET", f"/crm/v3/pipelines/deals/{pipeline_id}")
        for stage in data.get("stages") or []:
            labels[stage.get("id", "")] = stage.get("label", "")
    except Exception as e:
        logger.warning(f"Could not fetch stage labels: {e}")
    return labels


def get_pipeline_deals(pipeline_id: str = None) -> list:
    """All deals in the pipeline, every stage included."""
    pipeline_id = pipeline_id or PIPELINE_ID
    deals, after = [], None
    while True:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id}
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
    """contact email -> {contact_id, name, deals: [{id, name, stage_label, pipeline}]}

    Built across EVERY pipeline in PIPELINES, so one contact can carry deals in
    both the Palomar and the Ariadne fundraise; routing to one of them happens
    per email, after classification (see deals_for_pipeline)."""
    contact_to_deals = {}
    deal_info = {}
    for pipeline in PIPELINES:
        stage_labels = get_stage_labels(pipeline["id"])
        deals = get_pipeline_deals(pipeline["id"])
        logger.info(f"Pipeline {pipeline['id']} ({pipeline['name']}): {len(deals)} deals")
        for deal in deals:
            deal_id = str(deal.get("id", ""))
            if deal_id in deal_info:
                continue
            props = deal.get("properties") or {}
            deal_info[deal_id] = {
                "id": deal_id,
                "name": props.get("dealname") or "",
                "stage_label": stage_labels.get(props.get("dealstage") or "", props.get("dealstage") or ""),
                "pipeline": pipeline["key"],
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


def _deal_pipeline(deal: dict) -> str:
    # A deal record without a pipeline key predates the Ariadne pipeline, when
    # the roster was Palomar-only.
    return deal.get("pipeline") or PIPELINES[0]["key"]


def pipelines_on_msg(msg: dict) -> list:
    """Pipeline keys (PIPELINES order) in which a matched contact has a deal."""
    present = {_deal_pipeline(d) for e in msg["matched_contacts"].values() for d in e["deals"]}
    return [p["key"] for p in PIPELINES if p["key"] in present]


def deals_for_pipeline(msg: dict, pipeline_key: str = None) -> list:
    """Matched contacts' deals in one pipeline, deduped by id. pipeline_key None
    means every pipeline (legacy ledger rows that carry no routing)."""
    out, seen = [], set()
    for entry in msg["matched_contacts"].values():
        for deal in entry["deals"]:
            if pipeline_key and _deal_pipeline(deal) != pipeline_key:
                continue
            if deal["id"] not in seen:
                seen.add(deal["id"])
                out.append(deal)
    return out


def parse_classification(text: str, msg: dict) -> tuple:
    """Classifier reply -> (label, reason, pipeline_key).

    label in DEAL_RELEVANT / NOT_RELEVANT / UNCERTAIN; pipeline_key is set only
    for DEAL_RELEVANT. Pure -- every routing rule lives here so it is testable:
    - no or unparseable JSON, or an unknown label -> UNCERTAIN;
    - DEAL_RELEVANT with no valid "fundraise" -> the one pipeline the contacts
      are in, else UNCERTAIN (never guess between two);
    - DEAL_RELEVANT for a fundraise in which NO matched contact has a deal ->
      UNCERTAIN for human review, never logged onto the other pipeline's deal."""
    match = re.search(r"\{.*\}", text or "", flags=re.S)
    if not match:
        return "UNCERTAIN", f"classifier returned non-JSON output: {(text or '')[:120]}", None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "UNCERTAIN", f"classifier JSON parse failed: {(text or '')[:120]}", None
    if not isinstance(parsed, dict):
        return "UNCERTAIN", f"classifier returned non-object JSON: {(text or '')[:120]}", None
    label = str(parsed.get("classification") or "").strip().upper()
    reason = str(parsed.get("reason") or "").strip()
    if label not in ("DEAL_RELEVANT", "NOT_RELEVANT", "UNCERTAIN"):
        return "UNCERTAIN", f"unexpected label '{label}': {reason}", None
    if label != "DEAL_RELEVANT":
        return label, reason, None

    present = pipelines_on_msg(msg)
    pipeline = str(parsed.get("fundraise") or "").strip().lower()
    if pipeline not in PIPELINES_BY_KEY:
        if len(present) == 1:
            pipeline = present[0]
        else:
            return "UNCERTAIN", f"deal-relevant but fundraise not identified: {reason}", None
    if pipeline not in present:
        name = PIPELINES_BY_KEY[pipeline]["name"]
        return ("UNCERTAIN",
                f"about the {name} but no contact on the email has a deal in that pipeline: {reason}",
                None)
    return "DEAL_RELEVANT", reason, pipeline


def classify_email(msg: dict) -> tuple:
    """Returns (label, reason, pipeline_key). label in DEAL_RELEVANT /
    NOT_RELEVANT / UNCERTAIN; pipeline_key names the fundraise a DEAL_RELEVANT
    email belongs to (see parse_classification), else None."""
    import anthropic

    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")

    deal_lines = []
    for entry in msg["matched_contacts"].values():
        for deal in entry["deals"]:
            pname = PIPELINES_BY_KEY.get(_deal_pipeline(deal), {}).get("name", "")
            deal_lines.append(f"- Contact {entry['name']}: deal \"{deal['name']}\" "
                              f"in {pname} (stage: {deal['stage_label']})")
    deals_block = "\n".join(deal_lines) or "- (no deal context resolved)"
    fundraise_block = "\n".join(f"- \"{p['key']}\" = {p['name']}: {p['context']}" for p in PIPELINES)
    keys = "|".join(p["key"] for p in PIPELINES)

    body_excerpt = (msg.get("body_text") or "")[:8000]
    prompt = f"""You are classifying whether an email belongs on a fundraising deal timeline in a CRM, and if so WHICH of two separate fundraises it belongs to.

The two fundraises (separate CRM pipelines):
{fundraise_block}

Correspondence may be in English or Russian.

Deal context for the contact(s) on this email:
{deals_block}

Email:
From: {msg.get('from', '')}
Subject: {msg.get('subject', '')}
Date: {msg.get('received', '')}
Body:
{body_excerpt}

Classify the email:
- DEAL_RELEVANT: relates to the contact's deal in one of these fundraises (terms, documents, KYC, wires, scheduling investment calls, diligence, updates about the round).
- NOT_RELEVANT: personal topics, other ventures or deals outside these pipelines, newsletters, unrelated business.
- UNCERTAIN: genuinely ambiguous; a human should review.

For DEAL_RELEVANT, also set "fundraise" to the ONE fundraise the email is about:
- "ariadne" when it is about investing in Ariadne Bio itself -- its round, terms,
  documents, diligence, grants, or calls about an Ariadne investment.
- "palomar" when it is about investing in Palomar Labs (the studio), including
  studio or portfolio updates (Ariadne news included) sent to a contact as
  Palomar investor relations, when that contact has no Ariadne deal.
- When the content fits both, prefer the fundraise in which the contact
  actually has a deal (see deal context).

Stage matters: when the contact's deal is in an early stage (Outreach, Discovery,
Engaged), outreach from the team to the contact -- proposing an update call,
scheduling time, re-engaging the relationship -- IS the deal activity and counts
as DEAL_RELEVANT even when the round is not mentioned explicitly; route it to
the fundraise of that early-stage deal. Only treat team-to-contact outreach as
NOT_RELEVANT when it is clearly about an unrelated matter (a personal topic, the
contact's own other business, logistics of another engagement).

Palomar Labs (formerly Negev Labs) is a venture studio. Introductions connecting
prospective investors or their advisors with Palomar Labs or Ariadne Bio count as
DEAL_RELEVANT. Only ventures unrelated to Palomar Labs and its portfolio (e.g.,
the contact's own separate deals) are NOT_RELEVANT.

Respond with ONLY a JSON object: {{"classification": "DEAL_RELEVANT|NOT_RELEVANT|UNCERTAIN", "fundraise": "{keys}|null", "reason": "<one sentence>"}}"""

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
    return parse_classification(text, msg)


# ======================================================================
#  HUBSPOT LOGGER
# ======================================================================


def create_default_association(email_id: str, to_type: str, to_id: str):
    """v4 default association -- avoids hardcoding association type IDs."""
    api_key = os.environ.get("HUBSPOT_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{HUBSPOT_BASE}/crm/v4/objects/emails/{email_id}/associations/default/{to_type}/{to_id}"
    _request_with_retry("PUT", url, headers)


def ensure_engagement_associations(email_id: str, msg: dict, pipeline_key: str = None) -> list:
    """Associate an engagement to matched contacts + their deals + companies.
    pipeline_key restricts the DEAL links to that one fundraise pipeline (an
    Ariadne email never lands on the contact's Palomar deal, and vice versa);
    None links every deal (legacy ledger rows, written before routing existed).
    PUT default association is idempotent, so this doubles as the repair path
    for partial writes. Returns failed targets as 'type:id' strings."""
    failed = []
    associated_companies = set()
    for entry in msg["matched_contacts"].values():
        contact_id = entry["contact_id"]
        targets = [("contacts", contact_id)]
        targets += [("deals", deal["id"]) for deal in entry["deals"]
                    if not pipeline_key or _deal_pipeline(deal) == pipeline_key]
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


def log_email_to_hubspot(msg: dict, pipeline_key: str = None) -> tuple:
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
    failed = ensure_engagement_associations(email_id, msg, pipeline_key)
    return email_id, failed


# ======================================================================
#  RUN REPORT
# ======================================================================


def build_report(stats: dict, uncertain: list, skipped_mailboxes: list, caps_hit: list,
                 window: str, dry_run: bool) -> str:
    by_pipeline = stats.get("logged_by_pipeline") or {}
    split = ", ".join(f"{p['label']} {by_pipeline.get(p['key'], 0)}" for p in PIPELINES)
    lines = [
        f"email-pipeline-sync run report {'(DRY RUN) ' if dry_run else ''}-- window {window}",
        "Pipelines: " + "; ".join(f"{p['name']} ({p['id']})" for p in PIPELINES),
        "",
        f"Scanned:    {stats['scanned']}",
        f"Logged:     {stats['logged']} ({split})",
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
            deals = ", ".join(
                f"{d['name']} [{PIPELINES_BY_KEY.get(_deal_pipeline(d), {}).get('label', '?')}]"
                for d in deals_for_pipeline(u))
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


def _pipeline_tag(pipeline_key) -> str:
    """'pipeline=<key>; ' for a ledger detail string, '' when unrouted."""
    return f"pipeline={pipeline_key}; " if pipeline_key else ""


def detail_pipeline(detail: str):
    """Routed pipeline key recorded in a ledger detail, or None for rows
    written before routing existed (their repair links every deal, as then)."""
    m = re.search(r"pipeline=(\w+)", detail or "")
    return m.group(1) if m and m.group(1) in PIPELINES_BY_KEY else None


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
             "uncertain": 0, "repaired": 0, "partial": 0, "errors": 0,
             "logged_by_pipeline": {}}
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
                    pipeline = detail_pipeline(detail)
                    still_failed = ensure_engagement_associations(m.group(1), msg, pipeline)
                    tag = _pipeline_tag(pipeline)
                    if still_failed:
                        stats["partial"] += 1
                        ledger_update_outcome(conn, imid, "logged-partial",
                                              f"engagement={m.group(1)}; {tag}failed={','.join(still_failed)}")
                    else:
                        stats["repaired"] += 1
                        ledger_update_outcome(conn, imid, "logged",
                                              f"engagement={m.group(1)}; {tag}associations repaired")
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
                all_deals = {d["id"] for d in deals_for_pipeline(msg)}
                linked_deals = set(get_associated_ids("emails", existing_id, "deals"))
                if all_deals - linked_deals:
                    label, reason, pipeline = classify_email(msg)
                    # Only the routed pipeline's deals are expected: a contact's
                    # deal in the OTHER fundraise being unlinked is correct.
                    expected = {d["id"] for d in deals_for_pipeline(msg, pipeline)}
                    if label == "DEAL_RELEVANT" and expected - linked_deals:
                        if dry_run:
                            logger.info(f"[dry-run] would repair {pipeline} deal links on engagement {existing_id}: \"{msg.get('subject', '')}\"")
                        else:
                            ensure_engagement_associations(existing_id, msg, pipeline)
                            logger.info(f"Repaired {pipeline} deal links on engagement {existing_id}: \"{msg.get('subject', '')}\"")
                        ledger_record(conn, msg, "repaired-associations",
                                      f"engagement={existing_id}; {_pipeline_tag(pipeline)}{reason}",
                                      run_id, dry_run)
                        stats["repaired"] += 1
                        continue
                ledger_record(conn, msg, "skipped-duplicate",
                              f"engagement={existing_id} already in HubSpot", run_id, dry_run)
                stats["duplicates"] += 1
                continue

            label, reason, pipeline = classify_email(msg)
            tag = _pipeline_tag(pipeline)
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
                    logger.info(f"[dry-run] would log to {pipeline}: \"{msg.get('subject', '')}\" ({reason})")
                    ledger_record(conn, msg, "logged", f"{tag}{reason}", run_id, dry_run)
                else:
                    engagement_id, failed = log_email_to_hubspot(msg, pipeline)
                    if failed:
                        # Engagement exists; failed associations retry next run
                        logger.warning(f"Logged engagement {engagement_id} ({pipeline}) with failed associations {failed}: \"{msg.get('subject', '')}\"")
                        ledger_record(conn, msg, "logged-partial",
                                      f"engagement={engagement_id}; {tag}failed={','.join(failed)}", run_id, dry_run)
                        stats["partial"] += 1
                    else:
                        logger.info(f"Logged engagement {engagement_id} ({pipeline}): \"{msg.get('subject', '')}\"")
                        ledger_record(conn, msg, "logged",
                                      f"engagement={engagement_id}; {tag}{reason}", run_id, dry_run)
                stats["logged"] += 1
                stats["logged_by_pipeline"][pipeline] = stats["logged_by_pipeline"].get(pipeline, 0) + 1
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


def run_daily(lookback_days: int = 3) -> dict:
    """No-argv entrypoint for the scheduler. Scans a rolling lookback window
    (default 3 days, so a missed run is covered by the next), logs to HubSpot,
    and emails the run report. Ledger + HubSpot dedupe make the overlap safe."""
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=lookback_days)).isoformat()
    until = (today + timedelta(days=1)).isoformat()
    return run_sync(since, until, DEFAULT_MAILBOXES, dry_run=False, send_report=True)


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
