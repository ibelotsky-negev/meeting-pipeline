#!/usr/bin/env python3
"""
fyi-triage -- Sara module (FYI Triage)

Surfaces important mail buried in two high-volume auto-filed Outlook folders by
MOVING the important messages into Ken's "2: FYI" folder. The two sources are
"4: notification" (Fireflies/Humantic/Zoom/Calendly/OTP noise) and "8: marketing"
(newsletters/IR blasts) -- both fill faster than Ken can skim, so a real
signature request, a named-person reply, or an AGM notice gets lost.

What it does each run:
  1. Resolve the three folder IDs LIVE by display name via Graph (never hardcode).
  2. Fetch messages in the lookback window from the two source folders.
  3. Classify each with Sonnet (IMPORTANT vs NOISE) -- reading the BODY, not just
     the from-address (a named individual writing inside a marketing blast is
     IMPORTANT). Precision over recall: when unsure, do NOT move.
  4. DRY-RUN (default): log what it WOULD move, write no dedup ids, move nothing.
     LIVE (dual-gated): move each IMPORTANT message to "2: FYI", record its id.

Safety, by construction:
  - It ONLY ever moves FROM the two named sources TO "2: FYI". The destination is
    asserted to be the FYI folder and to differ from both sources before any move.
  - A real move requires BOTH the ?live=1 request flag AND env FYI_LIVE=1. Absent
    either, the run is dry regardless of window. The module cannot move mail until
    Ken sets FYI_LIVE.
  - Idempotent: a classified message is recorded by id (live runs only) and never
    re-touched. The module never deletes or modifies anything else.

Reuses Sara infra: Microsoft Graph app-only token + retrying GET/POST + html_to_text
(via email_pipeline_sync), the Claude client, the send-email path, the Weekly-Pulse
atomic O_CREAT|O_EXCL run-lock pattern, and the single-worker scheduler topology.

Usage:
    python fyi_triage.py                  # dry-run, last FYI_LOOKBACK_HOURS (24h)
    python fyi_triage.py --days 7         # dry-run, last 7 days (STATE B calibration)
    python fyi_triage.py --days 30 --live # 30-day backfill (needs FYI_LIVE=1 too)

ASCII-only comments and non-user-facing strings (PowerShell corrupts Unicode).
Author: Negev Labs
"""

import os
import json
import time
import uuid
import logging
import argparse
from datetime import datetime, timezone, timedelta

# Shared Graph helpers (app-only token, retrying GET/POST, html_to_text).
import email_pipeline_sync as eps

logger = logging.getLogger("fyi-triage")

# ======================================================================
#  CONFIG
# ======================================================================

MAILBOX = os.environ.get("FYI_MAILBOX", "bk@negevlabs.com")

# Folders are addressed by DISPLAY NAME, resolved live via Graph at run time
# (resolve_folder_map). IDs are NEVER hardcoded for addressing.
SOURCE_FOLDER_NAMES = ["4: notification", "8: marketing"]
DEST_FOLDER_NAME = "2: FYI"

# Cross-check ONLY: the folder IDs confirmed live via Graph on 2026-06-22. The
# module trusts the live lookup; if a resolved id does not match the expected one
# below, it logs a loud warning (a folder may have been renamed/recreated) but
# still uses the live value. These are NOT used for addressing and NOT a fallback.
# The "-" in the FYI id is the URL-safe base64 form of "/" (raw Graph id).
EXPECTED_FOLDER_IDS = {
    "2: FYI": "AAMkAGY0Nzc0N2Q0LWU2NWYtNDFlMi05MmM3LWI5ZWIwODY5ZDA4YwAuAAAAAAD2HZAEgE0dQ6DKSpP8o42sAQBxq2Btx8bBQbRoRXyUmqLCAAbaALi-AAA=",
    "4: notification": "AAMkAGY0Nzc0N2Q0LWU2NWYtNDFlMi05MmM3LWI5ZWIwODY5ZDA4YwAuAAAAAAD2HZAEgE0dQ6DKSpP8o42sAQBxq2Btx8bBQbRoRXyUmqLCAAbaALi9AAA=",
    "8: marketing": "AAMkAGY0Nzc0N2Q0LWU2NWYtNDFlMi05MmM3LWI5ZWIwODY5ZDA4YwAuAAAAAAD2HZAEgE0dQ6DKSpP8o42sAQBxq2Btx8bBQbRoRXyUmqLCAAbaALi5AAA=",
}

# Classification tier (Sonnet) -- NOT Opus. One call per message.
CLASSIFIER_MODEL = os.environ.get("FYI_CLASSIFIER_MODEL", "claude-sonnet-4-6")

# Lookback window. Steady-state cron uses HOURS (24h default). /fyi/run?days=N
# overrides for a single invocation (7 = calibration dry-run, 30 = backfill). A
# window beyond MAX_DAYS must be passed explicitly -- never silently scan the
# full ~13k backlog.
FYI_LOOKBACK_HOURS = int(os.environ.get("FYI_LOOKBACK_HOURS", "24"))
FYI_MAX_DAYS = int(os.environ.get("FYI_MAX_DAYS", "30"))

# Per-folder fetch cap (NOT silent -- a hit is logged and surfaced in status).
FYI_MAX_PER_FOLDER = int(os.environ.get("FYI_MAX_PER_FOLDER", "3000"))

# Bounded execution: classify concurrently (I/O-bound Anthropic calls release the
# GIL) with a hard per-call timeout so a stuck call can never hang the whole run.
FYI_CONCURRENCY = int(os.environ.get("FYI_CONCURRENCY", "8"))
FYI_ANTHROPIC_TIMEOUT = int(os.environ.get("FYI_ANTHROPIC_TIMEOUT", "60"))

# How much of each message the classifier reads (subject + sender always; body
# text truncated). Enough to catch a named person inside a marketing template.
FYI_BODY_CHARS = int(os.environ.get("FYI_BODY_CHARS", "2500"))

FYI_RECIPIENTS = [
    r.strip() for r in os.environ.get("FYI_RECIPIENTS", "bk@negevlabs.com").split(",") if r.strip()
]

# Fixed UTC offset only for the summary-email subject date (codebase convention;
# the scheduled cron itself is tz-aware via Asia/Jerusalem). 3 = IDT (summer).
ISRAEL_UTC_OFFSET_HOURS = int(os.environ.get("ISRAEL_UTC_OFFSET_HOURS", "3"))


def fyi_live_enabled() -> bool:
    """Second of the two gates: the FYI_LIVE env switch. Read at CALL TIME so a
    test (or Ken on Railway) can flip it without a re-import."""
    return os.environ.get("FYI_LIVE", "").strip() == "1"


# ----------------------------------------------------------------------
#  State files on /data (mirrors the Pulse/learn lock scale + pattern).
# ----------------------------------------------------------------------
_FYI_DATA_DIR = (
    os.environ.get("DATA_DIR")
    or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))
)
FYI_PROCESSED_FILE = os.path.join(_FYI_DATA_DIR, "fyi_processed.json")
FYI_LOCK_FILE = os.path.join(_FYI_DATA_DIR, "fyi_lock.json")
FYI_STATUS_FILE = os.path.join(_FYI_DATA_DIR, "fyi_status.json")
# A 30-day backfill classifies thousands of messages; allow a generous window
# before a held lock is treated as orphaned.
FYI_LOCK_MAX_AGE = int(os.environ.get("FYI_LOCK_MAX_AGE", str(2 * 3600)))

# In-process guard for manual trigger + scheduler in the same (single) worker.
import threading as _threading  # noqa: E402
_fyi_lock = _threading.Lock()


# ======================================================================
#  CLASSIFICATION RUBRIC (from Ken's 50-email calibration)
# ======================================================================

FYI_RUBRIC = """You triage one auto-filed email for Ken Belotsky (lead at Negev Labs, a biotech
venture studio, and Zirmania Family Office). The email was auto-sorted into a high-volume
"notification" or "marketing" folder. Decide whether it is IMPORTANT enough to surface to
Ken's "FYI" folder, or whether it is NOISE that should stay where it is.

READ THE BODY -- do NOT judge by the from-address alone. A bulk sender's address can carry a
real, personal message (e.g. a named individual writing inside an otherwise-marketing email);
that is IMPORTANT.

IMPORTANT (surface it) -- any of:
- A genuine action is required of Ken: signature / e-sign requests, document approvals,
  financial or banking actions (wires, account setup), KYC/subscription docs.
- A REAL PERSON opening or continuing a 1:1 conversation -- including a named individual
  writing inside an otherwise-marketing or form email. Read the body to catch this.
- Deal flow / co-invest / investor or partner outreach with real substance (a specific
  opportunity, a real intro, a term or allocation conversation).
- Portfolio / watchlist investor-relations or governance: an AGM notice, a shareholder
  letter, a material corporate action from a company Ken holds or tracks.

Confirmed IMPORTANT examples (positive exemplars):
  1. A YC SAFE signature request (e.g. Kinro via HelloSign / "Signature requested").
  2. A Webflow / website form submission from a named founder (e.g. "New form submission",
     a CEO reaching out).
  3. A public-company AGM notice (e.g. "Announces Result of Annual General Meeting").
  4. A conference-sponsorship proposal from a named sender (e.g. a psychedelics conference).
  5. A funding-round invitation with substance (e.g. "Series C invite", a real allocation ask).
  6. A banking / account invitation naming a specific person to coordinate with.

NOISE (leave it in place) -- do NOT surface:
- Meeting-automation echo: Fireflies, Humantic, Zoom bot join/recap/prep, "Notetaker has
  joined", "Meeting Prep", "Your meeting recap", "Catch up on yesterday".
- LinkedIn and social notifications (invitations, profile views, job listings, network digests).
- Newsletters, substacks, webinars and webinar invites, market-outlook blasts, promotional
  "database inside" or "save 20%" emails.
- OTPs / verification codes / login codes.
- Ken's own outbound or test sends, and automated delivery/confirmation receipts.
- Calendly booking notifications ("New Event: ... Zoom call").
- Receipts, invoices, credentials, and automated confirmations.

TIE-BREAKER: when you are not clearly sure it is important, classify NOISE. A missed surface is
cheap; a wrong move erodes trust. Precision over recall."""


# ======================================================================
#  ATOMIC RUN LOCK (mirrors the Weekly Pulse running lock)
# ======================================================================


def _acquire_run_lock() -> bool:
    """Atomically claim the cross-process run lock with O_CREAT|O_EXCL so only one
    run proceeds even if two workers fire at the same instant. Stale locks (older
    than FYI_LOCK_MAX_AGE) are reclaimed automatically.

    Returns True if acquired, False if a run is already in progress."""
    try:
        existing_age = time.time() - os.path.getmtime(FYI_LOCK_FILE)
        if existing_age > FYI_LOCK_MAX_AGE:
            logger.warning(f"[fyi] Removing stale run lock (age {existing_age/60:.0f}min)")
            try:
                os.remove(FYI_LOCK_FILE)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    try:
        fd = os.open(FYI_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
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


def _release_run_lock():
    try:
        os.remove(FYI_LOCK_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"[fyi] Failed to release run lock: {e}")


def _touch_run_lock():
    """Refresh the run-lock mtime so a long-but-LIVE run (e.g. a multi-hour
    30-day backfill that classifies thousands of messages) is never wrongly
    treated as orphaned and reclaimed by a concurrent worker. Without this, a
    backfill that outlives FYI_LOCK_MAX_AGE would let the daily cron reclaim the
    'stale' lock and start a SECOND live run against the same folders."""
    try:
        os.utime(FYI_LOCK_FILE, None)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"[fyi] Failed to refresh run lock mtime: {e}")


# ======================================================================
#  PROCESSED-ID STORE (dedup, keyed by message id)
# ======================================================================


def _load_processed_ids() -> set:
    try:
        with open(FYI_PROCESSED_FILE) as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else data.get("ids", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_processed_ids(ids):
    try:
        with open(FYI_PROCESSED_FILE, "w") as f:
            json.dump(sorted(ids), f)
    except Exception as e:
        logger.error(f"[fyi] Failed to write processed-id store: {e}")


# ======================================================================
#  FOLDER RESOLUTION (live, by display name; recursive; cached)
# ======================================================================

# Resolved {display_name: id} for this process. One Graph walk, then cached.
_folder_id_cache = {}


def _walk_mail_folders(max_depth: int = 4, max_folders: int = 600) -> dict:
    """Breadth-first walk of the mailbox's folders, returning {displayName: id}.
    Top-level mailFolders only list the root tier, so we descend childFolders for
    any folder that has them. Bounded by depth + total folders so a pathological
    tree can never run away. Last-writer-wins on duplicate display names (rare)."""
    out = {}
    base = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/mailFolders"
    # (url, params, depth) queue; root tier first.
    queue = [(base, {"$top": "100", "$select": "id,displayName,childFolderCount"}, 0)]
    scanned = 0
    while queue and scanned < max_folders:
        url, params, depth = queue.pop(0)
        data = eps.graph_get(url, params=params) or {}
        for f in (data.get("value") or []):
            name = (f.get("displayName") or "").strip()
            fid = f.get("id")
            if name and fid:
                out[name] = fid
            scanned += 1
            if depth < max_depth and (f.get("childFolderCount") or 0) > 0 and fid:
                child_url = f"{base}/{fid}/childFolders"
                queue.append((child_url, {"$top": "100", "$select": "id,displayName,childFolderCount"}, depth + 1))
        nxt = data.get("@odata.nextLink")
        if nxt:
            # nextLink already carries the query; do not re-pass params.
            queue.insert(0, (nxt, None, depth))
    logger.info(f"[fyi] Folder walk scanned {scanned} folders, matched {len(out)} names")
    return out


def resolve_folder_map(force: bool = False) -> dict:
    """Resolve the three folders by display name LIVE via Graph. Returns
    {display_name: id} for the dest + both sources. Trusts the live lookup;
    cross-checks each against EXPECTED_FOLDER_IDS and logs a loud warning on
    mismatch. Raises if any required folder cannot be resolved (the run must NOT
    guess a folder). Cached per process."""
    needed = [DEST_FOLDER_NAME] + SOURCE_FOLDER_NAMES
    if not force and all(n in _folder_id_cache for n in needed):
        return {n: _folder_id_cache[n] for n in needed}

    all_folders = _walk_mail_folders()
    resolved = {}
    missing = []
    for name in needed:
        fid = all_folders.get(name)
        if not fid:
            missing.append(name)
            continue
        resolved[name] = fid
        expected = EXPECTED_FOLDER_IDS.get(name)
        if expected and fid != expected:
            logger.warning(
                f"[fyi] Folder '{name}' resolved to a DIFFERENT id than expected "
                f"(live={fid[:48]}... expected={expected[:48]}...) -- trusting live value")
    if missing:
        raise RuntimeError(f"[fyi] Could not resolve folder(s) by display name: {missing}")

    # Safety: the destination must be distinct from both sources.
    dest_id = resolved[DEST_FOLDER_NAME]
    for sname in SOURCE_FOLDER_NAMES:
        if resolved[sname] == dest_id:
            raise RuntimeError(f"[fyi] Destination folder id equals source '{sname}' -- refusing to run")

    _folder_id_cache.update(resolved)
    return resolved


def _assert_safe_move(dest_id: str, source_ids: set):
    """Defense in depth before any move: the destination must be the resolved FYI
    folder id and must NOT be one of the source folder ids. Raises on violation."""
    fyi_id = _folder_id_cache.get(DEST_FOLDER_NAME)
    if not fyi_id or dest_id != fyi_id:
        raise RuntimeError("[fyi] Refusing move: destination is not the resolved '2: FYI' folder")
    if dest_id in source_ids:
        raise RuntimeError("[fyi] Refusing move: destination equals a source folder")


# ======================================================================
#  MAIL FETCH
# ======================================================================


def _cutoff_iso(days: int = None, hours: int = None) -> str:
    """UTC ISO8601 'Z' cutoff. days takes precedence over hours; falls back to
    FYI_LOOKBACK_HOURS. The window is always a parameter, never a fixed date."""
    if days is not None:
        delta = timedelta(days=days)
    elif hours is not None:
        delta = timedelta(hours=hours)
    else:
        delta = timedelta(hours=FYI_LOOKBACK_HOURS)
    cutoff = datetime.now(timezone.utc) - delta
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_messages(folder_id: str, since_iso: str, processed_ids: set = None,
                   backlog: bool = False, limit: int = None) -> list:
    """Fetch messages in the source folder received at/after since_iso. Skips ids
    already in the processed store (unless backlog=True, which re-processes
    everything). Paged + capped (FYI_MAX_PER_FOLDER); a cap hit is logged."""
    processed_ids = processed_ids or set()
    base = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/mailFolders/{folder_id}/messages"
    params = {
        "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,webLink,isRead",
        "$top": "50",
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
    }
    cap = limit or FYI_MAX_PER_FOLDER
    url, messages, pages, capped = base, [], 0, False
    while url and pages < 200:
        data = eps.graph_get(url, params=params if url == base else None) or {}
        for m in (data.get("value") or []):
            if not backlog and m.get("id") in processed_ids:
                continue
            messages.append(m)
            if len(messages) >= cap:
                capped = True
                break
        if capped:
            break
        url = data.get("@odata.nextLink")
        pages += 1
    if capped:
        logger.warning(f"[fyi] Folder fetch hit cap of {cap} messages (folder {folder_id[:32]}...) "
                       f"-- not all in-window mail was scanned this run")
    logger.info(f"[fyi] Fetched {len(messages)} messages since {since_iso} (folder {folder_id[:32]}..., "
                f"pages={pages}, backlog={backlog}, capped={capped})")
    return messages, capped


def _sender_address(msg: dict) -> str:
    return (((msg.get("from") or {}).get("emailAddress") or {}).get("address") or "").strip()


def _message_text(msg: dict) -> str:
    """Compact text the classifier reads: subject + sender + body excerpt. Body
    HTML is reduced to text and truncated to FYI_BODY_CHARS (enough to spot a real
    person inside a templated email). bodyPreview backstops an empty body."""
    subject = (msg.get("subject") or "").strip()
    sender = _sender_address(msg)
    body_html = ((msg.get("body") or {}).get("content")) or ""
    body_text = eps.html_to_text(body_html) if body_html else ""
    if not body_text:
        body_text = (msg.get("bodyPreview") or "").strip()
    body_text = body_text[:FYI_BODY_CHARS]
    return f"From: {sender}\nSubject: {subject}\n\n{body_text}"


# ======================================================================
#  CLASSIFIER (Sonnet -- one call per message)
# ======================================================================


def _call_claude_text(prompt: str, model: str, max_tokens: int = 400, timeout: int = None) -> str:
    import anthropic
    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")
    # Hard per-call timeout + single retry so a stuck call degrades fast instead
    # of blocking the run on the SDK's 10-minute default (x3 retries).
    client = anthropic.Anthropic(api_key=api_key).with_options(
        timeout=float(timeout or FYI_ANTHROPIC_TIMEOUT), max_retries=1)
    resp = client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _extract_json(text: str):
    """Tolerant JSON extraction from an LLM reply (handles ```json fences and
    surrounding prose). Returns the parsed object or None."""
    if not text:
        return None
    import re
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(candidate[start:end + 1])
    except Exception:
        return None


def classify_message(msg: dict, call_fn=None) -> tuple:
    """Classify one message as 'IMPORTANT' or 'NOISE' with a one-line reason.
    Reads the body (not just the from-address). Null-safe: an unparseable reply or
    an empty message degrades to NOISE (precision over recall -- never move on
    uncertainty). Returns (decision, reason). The caller treats an exception as a
    transient failure and does NOT record the id, so a later run can retry."""
    call_fn = call_fn or _call_claude_text
    content = _message_text(msg)
    prompt = (
        FYI_RUBRIC + "\n\n"
        'Return ONLY a JSON object: {"decision": "IMPORTANT" or "NOISE", "reason": "one short line"}.\n\n'
        "Email:\n" + content
    )
    raw = call_fn(prompt, CLASSIFIER_MODEL)
    parsed = _extract_json(raw) or {}
    decision = (parsed.get("decision") or "").strip().upper()
    reason = (parsed.get("reason") or "").strip()
    if decision not in ("IMPORTANT", "NOISE"):
        # Unparseable / unexpected -> the safe default is to NOT move it.
        return "NOISE", (reason or "unparseable classifier reply -- defaulting NOISE")
    return decision, (reason or "(no reason given)")


# ======================================================================
#  MOVE
# ======================================================================


def move_to_fyi(message_id: str, dest_id: str, source_ids: set) -> bool:
    """Move one message to the FYI folder via Graph POST /messages/{id}/move with
    {destinationId}. Asserts the destination is the resolved FYI folder (and not a
    source) first. Best effort -- returns True on success, False on failure (never
    raises, so one bad move does not abort the run)."""
    if not message_id:
        return False
    _assert_safe_move(dest_id, source_ids)
    url = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/messages/{message_id}/move"
    try:
        eps.graph_post(url, {"destinationId": dest_id})
        return True
    except Exception as e:
        logger.warning(f"[fyi] move failed {message_id[:24]}...: {e}")
        return False


# ======================================================================
#  SUMMARY EMAIL (optional -- the daily cron mails a short "would move N")
# ======================================================================


def _esc(value) -> str:
    import html
    return html.escape(str(value if value is not None else ""), quote=True)


def render_summary_html(decisions: list, dry_run: bool, window_desc: str) -> str:
    """Short HTML summary of a run: the IMPORTANT items (would-move in dry mode,
    moved in live mode) with sender + reason, then a count of NOISE left in place."""
    important = [d for d in decisions if d.get("decision") == "IMPORTANT"]
    noise = [d for d in decisions if d.get("decision") != "IMPORTANT"]
    verb = "Would move" if dry_run else "Moved"
    css = "font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;line-height:1.5;"
    parts = [f'<div style="{css}">']
    parts.append(
        f'<p style="color:#555;">FYI Triage ({"DRY-RUN" if dry_run else "LIVE"}), window {window_desc}. '
        f'<b>{verb} {len(important)}</b> to "2: FYI"; {len(noise)} left as noise.</p>')
    if important:
        parts.append('<table style="border-collapse:collapse;width:100%;">')
        parts.append('<tr style="text-align:left;color:#718096;font-size:12px;">'
                     '<th style="padding:4px 8px;">From</th><th style="padding:4px 8px;">Subject</th>'
                     '<th style="padding:4px 8px;">Why</th></tr>')
        for d in important:
            url = _esc(d.get("webLink"))
            subj = _esc(d.get("subject") or "(no subject)")
            link = f'<a href="{url}" style="color:#2b6cb0;text-decoration:none;">{subj}</a>' if url else subj
            parts.append(
                '<tr style="border-top:1px solid #edf2f7;">'
                f'<td style="padding:4px 8px;color:#444;">{_esc(d.get("sender"))}</td>'
                f'<td style="padding:4px 8px;">{link} '
                f'<span style="color:#a0aec0;">[{_esc(d.get("source"))}]</span></td>'
                f'<td style="padding:4px 8px;color:#2f855a;">{_esc(d.get("reason"))}</td></tr>')
        parts.append('</table>')
    else:
        parts.append('<p style="color:#718096;">Nothing important found in this window.</p>')
    parts.append('</div>')
    return "".join(parts)


def send_summary_email(subject: str, body: str):
    sender = os.environ.get("BOT_SENDER_EMAIL", "")
    if not sender:
        logger.warning("[fyi] BOT_SENDER_EMAIL not set -- summary not emailed")
        return
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body},
        "toRecipients": [{"emailAddress": {"address": r}} for r in FYI_RECIPIENTS],
    }
    eps.graph_post(f"{eps.MS_GRAPH_BASE}/users/{sender}/sendMail",
                   {"message": message, "saveToSentItems": False})
    logger.info(f"[fyi] Summary emailed to {', '.join(FYI_RECIPIENTS)}")


# ======================================================================
#  STATUS + HEARTBEAT
# ======================================================================


def write_status(status: dict):
    try:
        with open(FYI_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, default=str)
    except Exception as e:
        logger.warning(f"[fyi] Could not write status: {e}")


_PROGRESS_LOCK = _threading.Lock()
_FYI_PROGRESS = {"phase": "idle", "done": 0, "total": 0, "last": "", "run_id": None, "updated_at": None}


def _set_progress(**kw):
    with _PROGRESS_LOCK:
        _FYI_PROGRESS.update(kw)
        _FYI_PROGRESS["updated_at"] = datetime.now(timezone.utc).isoformat()


def _bump_progress(last: str):
    with _PROGRESS_LOCK:
        _FYI_PROGRESS["done"] = _FYI_PROGRESS.get("done", 0) + 1
        _FYI_PROGRESS["last"] = (last or "")[:120]
        _FYI_PROGRESS["updated_at"] = datetime.now(timezone.utc).isoformat()


def read_status() -> dict:
    try:
        with open(FYI_STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"status": "no_runs", "message": "No FYI triage run has completed yet."}
    except Exception as e:
        data = {"status": "error", "error": f"could not read status: {e}"}
    data["live_progress"] = dict(_FYI_PROGRESS)
    data["fyi_live_env"] = fyi_live_enabled()
    return data


# ======================================================================
#  CONCURRENCY
# ======================================================================


def _run_concurrent(items: list, fn, workers: int = None) -> list:
    """Run fn(index, item) over items with a bounded thread pool, preserving input
    order. A crash in one item yields None for that slot (caller filters) and never
    fails the run. workers<=1 runs sequentially."""
    workers = workers or FYI_CONCURRENCY
    n = len(items)
    results = [None] * n
    if workers <= 1 or n <= 1:
        for i, it in enumerate(items):
            try:
                results[i] = fn(i, it)
            except Exception as e:
                logger.error(f"[fyi] item {i} crashed: {e}")
        return results
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, i, it): i for i, it in enumerate(items)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                logger.error(f"[fyi] item {i} crashed: {e}")
    return results


# ======================================================================
#  RUN ORCHESTRATION
# ======================================================================


def _fyi_run_inner(dry_run: bool, days: int, backlog: bool, limit: int,
                   send_summary: bool) -> dict:
    """The pipeline, run with the lock held. Resolve folders -> fetch window ->
    classify -> (dry: log only) / (live: move IMPORTANT + record ids)."""
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc)
    _set_progress(phase="resolve-folders", done=0, total=0, last="", run_id=run_id)

    folders = resolve_folder_map()
    dest_id = folders[DEST_FOLDER_NAME]
    source_ids = {folders[n] for n in SOURCE_FOLDER_NAMES}
    processed_ids = _load_processed_ids()

    since_iso = _cutoff_iso(days=days)
    window_desc = f"{days}d" if days is not None else f"{FYI_LOOKBACK_HOURS}h"

    # Fetch the window from both source folders.
    _set_progress(phase="fetch", last=f"window {window_desc}")
    fetched, any_capped = [], False
    for sname in SOURCE_FOLDER_NAMES:
        msgs, capped = fetch_messages(folders[sname], since_iso, processed_ids, backlog, limit=limit)
        any_capped = any_capped or capped
        for m in msgs:
            fetched.append((sname, m))

    if not fetched:
        result = {"status": "ok", "run_id": run_id, "dry_run": dry_run, "backlog": backlog,
                  "window": window_desc, "scanned": 0, "important": 0, "moved": 0,
                  "noise": 0, "capped": any_capped, "decisions": [],
                  "finished_at": datetime.now(timezone.utc).isoformat()}
        _set_progress(phase="done", done=0, total=0, last="no messages in window")
        write_status(result)
        logger.info(f"[fyi] Run {run_id}: no messages in window {window_desc}")
        return result

    # Classify each message (concurrent, bounded). An exception is isolated and
    # marked ok=False so the caller does NOT record its id (allows a retry).
    _set_progress(phase="classify", done=0, total=len(fetched), last="")

    def _classify_one(i, pair):
        sname, m = pair
        rec = {
            "id": m.get("id"), "source": sname, "subject": (m.get("subject") or "").strip(),
            "sender": _sender_address(m), "webLink": m.get("webLink"),
            "received": m.get("receivedDateTime"), "decision": "NOISE", "reason": "", "ok": True,
        }
        try:
            decision, reason = classify_message(m)
            rec["decision"], rec["reason"] = decision, reason
        except Exception as e:
            rec["decision"], rec["reason"], rec["ok"] = "NOISE", f"classifier error: {e}", False
        # Raw-decision log from day 1.
        logger.info(f"[fyi] [{rec['decision']}] {rec['source']} | {rec['sender']} | "
                    f"{rec['subject'][:80]} -- {rec['reason'][:100]}")
        _bump_progress(f"[{rec['decision']}] {rec['subject'][:50]}")
        return rec

    decisions = [d for d in _run_concurrent(fetched, _classify_one) if d]
    important = [d for d in decisions if d.get("decision") == "IMPORTANT"]
    noise = [d for d in decisions if d.get("decision") != "IMPORTANT"]

    moved = 0
    if dry_run:
        logger.info(f"[fyi] [dry-run] {len(fetched)} scanned -> {len(important)} would move, "
                    f"{len(noise)} noise (no move, no ids written)")
        _set_progress(phase="done", last=f"would move {len(important)}")
    else:
        _set_progress(phase="move", done=0, total=len(important), last="")
        new_ids = set()
        for d in important:
            if move_to_fyi(d.get("id"), dest_id, source_ids):
                moved += 1
                if d.get("id"):
                    new_ids.add(d["id"])
                _bump_progress(f"moved: {d.get('subject', '')[:50]}")
        # Record EVERY confidently-classified id (important moved + noise left) so
        # we never re-classify them. An errored classification (ok=False) is left
        # unrecorded so a future run retries it.
        for d in noise:
            if d.get("ok") and d.get("id"):
                new_ids.add(d["id"])
        _save_processed_ids(processed_ids | new_ids)
        logger.info(f"[fyi] [live] moved {moved}/{len(important)} important to '2: FYI'; "
                    f"recorded {len(new_ids)} processed ids")
        _set_progress(phase="done", last=f"moved {moved}")

    local_date = (started + timedelta(hours=ISRAEL_UTC_OFFSET_HOURS)).strftime("%a %d %b %Y")
    subject = f"[Sara] FYI Triage {'dry-run' if dry_run else 'live'} -- {local_date}: " \
              f"{'would move' if dry_run else 'moved'} {len(important) if dry_run else moved}"

    if send_summary:
        try:
            send_summary_email(subject, render_summary_html(decisions, dry_run, window_desc))
        except Exception as e:
            logger.warning(f"[fyi] summary email failed: {e}")

    result = {
        "status": "ok", "run_id": run_id, "dry_run": dry_run, "backlog": backlog,
        "window": window_desc, "since": since_iso, "scanned": len(fetched),
        "important": len(important), "moved": moved, "noise": len(noise),
        "capped": any_capped, "subject": subject,
        # The reviewable artifact (STATE B / STATE C): every decision with reason.
        "decisions": decisions,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    write_status(result)
    logger.info(f"[fyi] Run {run_id} done: scanned={len(fetched)} important={len(important)} "
                f"moved={moved} dry_run={dry_run}")
    return result


def run_fyi(dry_run: bool = None, days: int = None, live: bool = False, backlog: bool = False,
            force: bool = False, limit: int = None, send_summary: bool = False) -> dict:
    """Public entry. Acquires the cross-process file lock + the in-process lock,
    runs the pipeline, releases both. Guarantees exactly one run even under a
    two-worker race.

    DUAL GATE: a real move happens only when BOTH live=True AND env FYI_LIVE=1. If
    dry_run is left as None it is DERIVED from the gate (dry unless both gates are
    on). Passing dry_run=True forces dry regardless of the gates.

    force=True clears a pre-existing run lock first (operator override for an
    orphaned lock); do NOT use it on a real live run -- it defeats the single-run
    guard."""
    gate_open = bool(live) and fyi_live_enabled()
    if dry_run is None:
        dry_run = not gate_open
    elif not dry_run and not gate_open:
        # Caller asked for a real run but the dual gate is not open -> force dry.
        logger.warning("[fyi] Live requested but dual gate not open (need ?live=1 AND FYI_LIVE=1) "
                       "-- running DRY")
        dry_run = True

    if force:
        logger.warning("[fyi] force=1 -- clearing any existing run lock before starting")
        _release_run_lock()
    if not _acquire_run_lock():
        logger.warning("[fyi] Skipping -- another run already in progress (cross-process)")
        return {"status": "skipped", "reason": "run already in progress"}
    if not _fyi_lock.acquire(blocking=False):
        logger.warning("[fyi] Skipping -- another run already in progress (in-process)")
        _release_run_lock()
        return {"status": "skipped", "reason": "run already in progress"}
    # Heartbeat: keep the lock mtime fresh while this (possibly hours-long)
    # run is alive so a concurrent worker never reclaims it as stale. If this
    # process dies, the heartbeat stops and the lock correctly becomes
    # reclaimable after FYI_LOCK_MAX_AGE.
    _hb_stop = _threading.Event()

    def _heartbeat():
        interval = max(60, FYI_LOCK_MAX_AGE // 4)
        while not _hb_stop.wait(interval):
            _touch_run_lock()

    _hb_thread = _threading.Thread(target=_heartbeat, daemon=True)
    _hb_thread.start()
    try:
        return _fyi_run_inner(dry_run, days, backlog, limit, send_summary)
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        logger.error(f"[fyi] Run failed: {tb}")
        write_status({"status": "error", "error": str(e), "traceback": tb,
                      "finished_at": datetime.now(timezone.utc).isoformat()})
        raise
    finally:
        _hb_stop.set()
        _hb_thread.join(timeout=2)
        _fyi_lock.release()
        _release_run_lock()


def main():
    parser = argparse.ArgumentParser(description="FYI Triage (Sara module)")
    parser.add_argument("--days", type=int, default=None,
                        help="Lookback window in days (default: FYI_LOOKBACK_HOURS, 24h)")
    parser.add_argument("--live", action="store_true",
                        help="Request a real move (still requires env FYI_LIVE=1)")
    parser.add_argument("--backlog", action="store_true",
                        help="Re-process everything in the window, ignoring the processed-id store")
    parser.add_argument("--email", action="store_true", help="Email a short run summary")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.days is not None and args.days > FYI_MAX_DAYS:
        logger.warning(f"[fyi] --days {args.days} exceeds FYI_MAX_DAYS ({FYI_MAX_DAYS}); "
                       "this is a large explicit window")
    run_fyi(days=args.days, live=args.live, backlog=args.backlog, send_summary=args.email)


if __name__ == "__main__":
    main()
