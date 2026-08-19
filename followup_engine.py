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
        "interval_days": FOLLOWUP_DEFAULT_BUSINESS_DAYS if interval_days is None else int(interval_days),
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


# ----------------------------------------------------------------------
#  Intake: gates, commands, instruction parsing
# ----------------------------------------------------------------------

_TRIGGER_RE = re.compile(r"\b(follow[\s-]?up|remind(?:er)?|chase)\b", re.I)
# Media links belong to x_transcribe_email; a follow-up request never needs one.
_MEDIA_RE = re.compile(r"(?:x\.com|twitter\.com|youtube\.com|youtu\.be)/", re.I)
_WATCH_ID_RE = re.compile(r"\bfw_[0-9a-f]{8}\b")
_CANCEL_RE = re.compile(r"\b(stop|cancel|done)\b", re.I)
_RESUME_RE = re.compile(r"\b(resume|continue|keep|re-?arm)\b", re.I)


def _parse_command(body_text: str) -> str:
    # Deterministic, no LLM: cancel wins over resume on a conflicting message.
    if _CANCEL_RE.search(body_text or ""):
        return "cancel"
    if _RESUME_RE.search(body_text or ""):
        return "resume"
    return None


def _extract_json(text: str):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return None


_PARSE_PROMPT = """You extract follow-up watch requests from a team member's email to Sara, \
an email assistant. The member forwards an email thread and asks Sara to remind/chase \
someone if they do not reply.

Email subject: {subject}

Email body (new text only, may include forwarded headers):
---
{body}
---

Reply with JSON ONLY, no prose:
{{"is_request": true/false,
  "thread_subject": "subject of the thread to watch, without FW:/RE: prefixes",
  "counterparties": ["email addresses or names of who should reply"],
  "asks": [{{"ask": "one specific thing awaited, in plain words",
             "recipients": ["explicit reminder recipients if the sender named any, else []"],
             "days": <integer business days to wait, or null>,
             "date": "YYYY-MM-DD explicit deadline, or null"}}]}}

Rules: is_request is false unless the sender clearly asks to follow up / remind / chase \
if someone does not reply. Split independent awaited items into SEPARATE asks. Never \
invent recipients or deadlines the sender did not give."""


def parse_instruction(subject: str, body_text: str) -> dict:
    try:
        raw = ld._call_claude_text(
            _PARSE_PROMPT.format(subject=subject or "", body=(body_text or "")[:6000]),
            PARSE_MODEL, max_tokens=1000)
    except Exception as e:
        logger.warning(f"[followup] instruction parse failed: {e}")
        return {"is_request": False}
    out = _extract_json(raw)
    if not out or not isinstance(out, dict) or not out.get("is_request"):
        return {"is_request": False}
    asks = [a for a in (out.get("asks") or []) if isinstance(a, dict) and (a.get("ask") or "").strip()]
    if not asks:
        return {"is_request": False}
    out["asks"] = asks
    out["thread_subject"] = (out.get("thread_subject") or "").strip()
    out["counterparties"] = [c for c in (out.get("counterparties") or []) if c]
    return out
