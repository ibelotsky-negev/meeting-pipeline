#!/usr/bin/env python3
"""
sara-corrections -- standing corrections store for Sara's reports.

Ken corrects Sara by REPLYING to one of her report emails (the biweekly business
update or the weekly pulse). Those replies are ingested and stored as standing
corrections, then injected as AUTHORITATIVE context into future report prompts so
the same mistake is not repeated.

Two report generators read this store:
- the weekly pulse synthesis (app.py)
- the biweekly business update distillation (biweekly_business_update.py)

A baseline correction (Ariadne Bio fundraising structure) is always included so
both reports honor it even before any reply is ingested.

Shares Graph auth + HTTP + html_to_text helpers with email_pipeline_sync.py.
No new dependencies. Reading the mailbox is the only network action and happens
only inside ingest_replies().

Author: Negev Labs
"""

import os
import json
import uuid
import logging
from datetime import datetime, timezone

import email_pipeline_sync as eps

logger = logging.getLogger("sara-corrections")

# ======================================================================
#  CONFIG
# ======================================================================

CORRECTIONS_PATH = (
    "/data/sara_corrections.json"
    if os.path.isdir("/data")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)), "sara_corrections.json")
)

# Mailbox Sara sends from and receives replies in.
SARA_MAILBOX = os.environ.get("BOT_SENDER_EMAIL", "sara@palomar-labs.com")

# Only these senders may submit corrections (prompt-injection guard -- Sara's
# mailbox can receive external mail). Comma-separated; defaults to Ken.
CORRECTIONS_ALLOW = {
    a.strip().lower()
    for a in os.environ.get(
        "CORRECTIONS_ALLOW", "bk@negevlabs.com,ibelotsky@gmail.com").split(",")
    if a.strip()
}

# Subjects that mark a message as a reply to one of Sara's reports.
CORRECTION_SUBJECT_MARKERS = ("business update", "weekly pulse")

# Send Ken a short confirmation reply when a correction is logged.
CORRECTIONS_ACK = os.environ.get("CORRECTIONS_ACK", "true").lower() in ("true", "1", "yes")

# Always-on baseline corrections (authoritative). Kept here, not in the prompts,
# so both reports stay consistent and there is a single source of truth.
BASELINE_CORRECTIONS = [
    ("Ariadne Bio fundraising: Ariadne is NOT seeking a lead biotech investor and "
     "there is NO \"$8-12M lead-investor gap\" -- never frame the raise as at risk for "
     "lack of a lead. Ariadne is funded by a combination of the MJFF grant, Negev Labs "
     "funding ($2M already provided), and new investor commitments (for example Tetrad "
     "VC, $2M). Remaining funding is being raised via European non-dilutive grants "
     "(FFG, EIC) and/or additional funding at the Negev Labs level, which is ongoing "
     "and on-plan. Flag a fundraising risk only on a concrete, specific problem (a "
     "named grant rejected or a committed tranche delayed)."),
]


# ======================================================================
#  STORE
# ======================================================================


def _load() -> dict:
    try:
        with open(CORRECTIONS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"corrections": [], "processed_ids": []}
    except Exception as e:
        logger.warning(f"Could not read corrections store ({e}); starting empty")
        return {"corrections": [], "processed_ids": []}
    data.setdefault("corrections", [])
    data.setdefault("processed_ids", [])
    return data


def _save(data: dict):
    try:
        with open(CORRECTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"Could not write corrections store: {e}")


def list_corrections(include_inactive: bool = False) -> list:
    """User-submitted corrections (newest first). Baseline is not included here --
    it is added by corrections_block()."""
    items = _load()["corrections"]
    if not include_inactive:
        items = [c for c in items if c.get("active", True)]
    return sorted(items, key=lambda c: c.get("added_at", ""), reverse=True)


def add_correction(text: str, source: str = "manual", from_addr: str = "") -> dict:
    """Add a standing correction. Returns the stored entry."""
    text = (text or "").strip()
    if not text:
        raise ValueError("correction text is empty")
    data = _load()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "added_at": datetime.now(timezone.utc).isoformat(),
        "from": from_addr,
        "source": source,
        "text": text,
        "active": True,
    }
    data["corrections"].append(entry)
    _save(data)
    logger.info(f"Correction {entry['id']} added from {from_addr or source}")
    return entry


def deactivate_correction(correction_id: str) -> bool:
    """Mark a user correction inactive so it stops being injected. Returns True
    if a matching active correction was found."""
    data = _load()
    found = False
    for c in data["corrections"]:
        if c.get("id") == correction_id and c.get("active", True):
            c["active"] = False
            found = True
    if found:
        _save(data)
        logger.info(f"Correction {correction_id} deactivated")
    return found


# ======================================================================
#  PROMPT INJECTION
# ======================================================================


def corrections_block() -> str:
    """Authoritative block appended to report prompts. Always contains the
    baseline corrections plus any active user-submitted ones."""
    texts = list(BASELINE_CORRECTIONS) + [c["text"] for c in list_corrections()]
    bullets = "\n".join(f"- {t}" for t in texts)
    return (
        "STANDING CORRECTIONS FROM KEN -- these are authoritative and OVERRIDE "
        "anything in the source material that conflicts with them:\n" + bullets
    )


# ======================================================================
#  EMAIL INGESTION (reply -> correction)
# ======================================================================


def _is_correction_reply(subject: str) -> bool:
    s = (subject or "").lower()
    return any(marker in s for marker in CORRECTION_SUBJECT_MARKERS)


def _sender_allowed(addr: str) -> bool:
    return (addr or "").strip().lower() in CORRECTIONS_ALLOW


def _send_ack(message_id: str, to_addr: str):
    """Best-effort threaded confirmation reply. Never fatal."""
    if not CORRECTIONS_ACK:
        return
    try:
        eps.graph_post(
            f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/messages/{message_id}/reply",
            {"comment": "Logged your correction -- I'll apply it to future updates. -- Sara"},
        )
    except Exception as e:
        logger.warning(f"Correction ack failed for {to_addr}: {e}")


def ingest_replies(limit: int = 25) -> dict:
    """Scan Sara's inbox for replies to her reports, store new ones as standing
    corrections, and acknowledge them. Idempotent via processed message ids.
    Returns a summary dict. Skipped/unreadable messages are non-fatal."""
    data = _load()
    processed = set(data.get("processed_ids") or [])
    added, skipped = [], 0

    url = f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/mailFolders/inbox/messages"
    params = {
        "$select": "id,subject,from,receivedDateTime,uniqueBody,internetMessageId",
        "$top": str(limit),
        "$orderby": "receivedDateTime desc",
    }
    resp = eps.graph_get(url, params=params)
    messages = resp.get("value") or []

    for m in messages:
        mid = m.get("internetMessageId") or m.get("id") or ""
        if not mid or mid in processed:
            continue
        subject = m.get("subject") or ""
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        if not _is_correction_reply(subject):
            continue
        if not _sender_allowed(sender):
            logger.info(f"Ignoring correction reply from non-allowed sender {sender}")
            processed.add(mid)  # do not reconsider every run
            skipped += 1
            continue
        body_html = (m.get("uniqueBody") or {}).get("content", "")
        text = eps.html_to_text(body_html).strip()
        if not text:
            processed.add(mid)
            skipped += 1
            continue
        # add_correction reloads/saves the store; re-load processed afterwards
        add_correction(text=text, source=subject, from_addr=sender)
        processed.add(mid)
        added.append({"from": sender, "subject": subject, "text": text[:200]})
        _send_ack(m.get("id"), sender)

    # Persist processed-id set (merge with whatever add_correction wrote)
    final = _load()
    final["processed_ids"] = sorted(processed)
    _save(final)

    logger.info(f"Correction ingest: {len(added)} added, {skipped} skipped, "
                f"{len(messages)} scanned")
    return {"added": added, "added_count": len(added), "skipped": skipped,
            "scanned": len(messages)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sara corrections store / ingest")
    parser.add_argument("--ingest", action="store_true", help="Scan Sara mailbox for reply corrections")
    parser.add_argument("--add", help="Add a correction manually")
    parser.add_argument("--list", action="store_true", help="List active corrections")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if args.add:
        print(add_correction(args.add, source="cli"))
    if args.ingest:
        print(json.dumps(ingest_replies(), indent=2, default=str))
    if args.list or not (args.add or args.ingest):
        print(corrections_block())


if __name__ == "__main__":
    main()
