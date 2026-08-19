"""Follow-Up Engine (pilot) -- watch an email thread for a reply; on silence
past a deadline, place a ready-to-send reminder draft in the thread owner's
Outlook Drafts folder and report by email. The machine acts only on silence
and NEVER sends to a counterparty (no auto-send exists in this module).

Spec: followup-engine-spec.md. Lazily imported by app.py (routes + cron only).
Reuses: email_pipeline_sync (Graph), x_transcribe_email (inbox reply,
auto-reply detection), learn_digest (_call_claude_text), config (identity).
"""
import os
import re          # noqa: F401 -- consumed by later tasks (intake parsing)
import json
import html         # noqa: F401 -- consumed by later tasks (draft/report body escaping)
import uuid
import logging
from datetime import datetime, timedelta, timezone, date

import email_pipeline_sync as eps      # noqa: F401 -- consumed by later tasks (Graph reuse)
import x_transcribe_email as xte       # noqa: F401 -- consumed by later tasks (reply/auto-reply reuse)
import learn_digest as ld              # noqa: F401 -- consumed by later tasks (_call_claude_text reuse)
import config                          # noqa: F401 -- consumed by later tasks (identity helpers)

logger = logging.getLogger("followup_engine")

SARA_MAILBOX = os.environ.get("BOT_SENDER_EMAIL", "sara@palomar-labs.com")

_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(_DATA_DIR, "followups.json")
PROCESSED_PATH = os.path.join(_DATA_DIR, "followup_processed.json")
STATUS_PATH = os.path.join(_DATA_DIR, "followup_status.json")

FOLLOWUP_DEFAULT_BUSINESS_DAYS = int(os.environ.get("FOLLOWUP_DEFAULT_BUSINESS_DAYS", "2"))
FOLLOWUP_MAX_NUDGES = int(os.environ.get("FOLLOWUP_MAX_NUDGES", "3"))
FOLLOWUP_MAX_WATCHES = int(os.environ.get("FOLLOWUP_MAX_WATCHES", "100"))
FOLLOWUP_INTAKE_MAX_MESSAGES = int(os.environ.get("FOLLOWUP_INTAKE_MAX_MESSAGES", "25"))
PARSE_MODEL = os.environ.get("FOLLOWUP_PARSE_MODEL", "claude-sonnet-4-6")
VERDICT_MODEL = os.environ.get("FOLLOWUP_VERDICT_MODEL", "claude-haiku-4-5-20251001")
DRAFT_MODEL = os.environ.get("FOLLOWUP_DRAFT_MODEL", "claude-sonnet-4-6")
ALERT_CC = os.environ.get("FOLLOWUP_ALERT_CC", "bk@negevlabs.com")
TZ_NAME = "Asia/Jerusalem"


def _live() -> bool:
    # Read at call time (house rule) so flipping FOLLOWUP_LIVE on Railway
    # arms draft creation without a code change. Unset = report-only.
    return os.environ.get("FOLLOWUP_LIVE", "") == "1"


def _today_il() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TZ_NAME)).date()


def add_business_days(d: date, n: int) -> date:
    # Mon-Fri calendar (counterparty convention; matches the spec example
    # Wed Aug 5 + 2 -> Fri Aug 7). Israel's Sun-Thu week is deliberately
    # NOT modeled in the pilot.
    out = d
    for _ in range(max(0, n)):
        out = out + timedelta(days=1)
        while out.weekday() >= 5:
            out = out + timedelta(days=1)
    return out


# ----------------------------------------------------------------------
#  State stores (x_transcribe_email patterns)
# ----------------------------------------------------------------------


def _load_registry() -> dict:
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"watches": []}
    except Exception as e:
        logger.warning(f"[followup] could not read registry ({e}); starting empty")
        return {"watches": []}
    data.setdefault("watches", [])
    return data


def _save_registry(reg: dict):
    try:
        os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
        with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(reg, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"[followup] could not write registry: {e}")


def _load_processed() -> set:
    try:
        with open(PROCESSED_PATH, encoding="utf-8") as f:
            return set((json.load(f) or {}).get("processed_ids") or [])
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _persist_processed(processed: set, dry_run: bool = False):
    # Written after EACH handled message, not once per scan -- a restart
    # mid-scan must never re-reply to mail already answered. Dry writes nothing.
    if dry_run:
        return
    try:
        os.makedirs(os.path.dirname(PROCESSED_PATH), exist_ok=True)
        with open(PROCESSED_PATH, "w", encoding="utf-8") as f:
            json.dump({"processed_ids": sorted(processed)[-1000:]}, f, indent=2)
    except Exception as e:
        logger.warning(f"[followup] could not write processed store: {e}")


def _write_status(result: dict):
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"[followup] could not write status: {e}")


def read_status() -> dict:
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ----------------------------------------------------------------------
#  Watch factory
# ----------------------------------------------------------------------


def new_watch(owner: str, mailbox: str, conversation_id: str, anchor_message_id: str,
              anchor_received: str, subject: str, ask: str, recipients: list,
              interval_days: int, deadline: date, intake_conversation_id: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": f"fw_{uuid.uuid4().hex[:8]}",
        "owner": owner,
        "mailbox": mailbox,
        "conversation_id": conversation_id,
        "anchor_message_id": anchor_message_id,
        "anchor_received": anchor_received,
        "subject": subject,
        "ask": ask,
        "recipients": recipients or [],
        "interval_days": int(interval_days or FOLLOWUP_DEFAULT_BUSINESS_DAYS),
        "deadline": deadline.isoformat(),
        "max_nudges": FOLLOWUP_MAX_NUDGES,
        "nudges_sent": 0,
        "status": "active",
        "last_checked": None,
        "latest_message_id": anchor_message_id,
        "drafts": [],
        "intake_conversation_id": intake_conversation_id or "",
        "notes": [],
        "created": now,
        "updated": now,
    }


def _note(watch: dict, text: str):
    watch.setdefault("notes", []).append(
        {"ts": datetime.now(timezone.utc).isoformat(), "text": text})
    watch["updated"] = datetime.now(timezone.utc).isoformat()
