# Follow-Up Engine (pilot) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an external counterparty goes silent on a registered email thread, place a ready-to-send reminder draft in the thread owner's Outlook Drafts and report it by email — never sending anything itself.

**Architecture:** One new standalone module `followup_engine.py` (lazily imported by `app.py`), reusing deployed machinery: `email_pipeline_sync` Graph helpers, `x_transcribe_email` reply/auto-reply helpers, `learn_digest._call_claude_text`, `config` identity helpers. Two scheduled jobs (15-min intake scan of Sara's inbox; daily 17:00 Asia/Jerusalem check), three Flask routes, JSON state on `/data`. Full behavior spec: `followup-engine-spec.md` (repo root) — read it before starting.

**Tech Stack:** Python 3.12, Flask, APScheduler, Microsoft Graph (app-only), Anthropic SDK (via `learn_digest._call_claude_text`), pytest (offline only).

**Goal prompt for the executing session (<=400 chars):**
`/goal Build the Sara Follow-Up Engine pilot exactly per followup-engine-spec.md + docs/superpowers/plans/2026-08-18-followup-engine.md. Subagent-driven: fresh subagent per task, review between tasks. TDD, offline tests, ASCII comments, preserve CRLF in app.py/CLAUDE.md. Ship via PR (main is protected), poll /version for 2.29.0-followup-pilot. Report-only until FOLLOWUP_LIVE=1. No auto-send.`

## Global Constraints

- ASCII-only in comments and non-user-facing strings (`->` not arrows, `--` not em-dash).
- Tests are offline-only; conftest's autouse `no_network` fixture fails real HTTP. Never weaken/skip/delete a test.
- Null-safe API reads: `data.get("x") or {}`, never `data.get("x", {})`.
- `app.py` and `CLAUDE.md` are CRLF — multi-line insertions there MUST go through the byte-level python scripts given in Tasks 8-9; single-line replacements may use exact-string edit. Confirm with `git diff --numstat` that only intended files changed by sane line counts.
- Version string `2.29.0-followup-pilot` must appear in exactly 2 places in app.py (`/version` and `/test`) — Task 8.
- Single worker topology: never touch the Procfile; new jobs go in the existing single APScheduler instance.
- Mock moved/reused functions in their HOME module (`eps.graph_get` on `email_pipeline_sync`, `ld._call_claude_text` on `learn_digest`, `xte.send_threaded_reply` on `x_transcribe_email`).
- The machine NEVER sends to a counterparty: no `/send` call on any owner-mailbox draft anywhere in this module. `FOLLOWUP_LIVE` unset ⇒ report-only (no drafts created either).
- `main` is branch-protected (required check "Offline test suite") — all work on branch `feat/followup-engine`, ship via PR (Task 9).
- Every commit message ends with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Module skeleton, stores, business-day math, watch factory

**Files:**
- Create: `followup_engine.py`
- Create: `tests/test_followup_engine.py`

**Interfaces:**
- Consumes: `email_pipeline_sync as eps`, `x_transcribe_email as xte`, `learn_digest as ld`, `config` (import only here; used in later tasks).
- Produces (later tasks rely on these exact names): module constants `SARA_MAILBOX, REGISTRY_PATH, PROCESSED_PATH, STATUS_PATH, FOLLOWUP_DEFAULT_BUSINESS_DAYS, FOLLOWUP_MAX_NUDGES, FOLLOWUP_MAX_WATCHES, FOLLOWUP_INTAKE_MAX_MESSAGES, PARSE_MODEL, VERDICT_MODEL, DRAFT_MODEL, ALERT_CC`; functions `_live() -> bool`, `_today_il() -> date`, `add_business_days(d: date, n: int) -> date`, `_load_registry() -> dict`, `_save_registry(reg: dict)`, `_load_processed() -> set`, `_persist_processed(processed: set, dry_run: bool = False)`, `_write_status(result: dict)`, `read_status() -> dict`, `new_watch(...) -> dict`, `_note(watch: dict, text: str)`.

- [ ] **Step 1: Create branch**

```bash
git checkout -b feat/followup-engine
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_followup_engine.py`:

```python
"""Offline tests for followup_engine (silent-thread reminder drafts).

Reuse points are mocked in their HOME modules:
- Graph read/write  -> email_pipeline_sync.graph_get/graph_post/graph_patch
- Claude            -> learn_digest._call_claude_text
- Sara inbox reply  -> x_transcribe_email.send_threaded_reply
The autouse no_network fixture (conftest) fails any real HTTP.
"""
import json
from datetime import date

import pytest

import followup_engine as fue
import email_pipeline_sync as eps
import learn_digest as ld
import x_transcribe_email as xte
import config


@pytest.fixture
def fue_files(monkeypatch, tmp_path):
    monkeypatch.setattr(fue, "REGISTRY_PATH", str(tmp_path / "followups.json"))
    monkeypatch.setattr(fue, "PROCESSED_PATH", str(tmp_path / "processed.json"))
    monkeypatch.setattr(fue, "STATUS_PATH", str(tmp_path / "status.json"))
    return tmp_path


def test_add_business_days_skips_weekend():
    # Wed Aug 5 2026 + 2 business days = Fri Aug 7 (the doc's example)
    assert fue.add_business_days(date(2026, 8, 5), 2) == date(2026, 8, 7)
    # Fri + 2 business days = Tue
    assert fue.add_business_days(date(2026, 8, 7), 2) == date(2026, 8, 11)
    assert fue.add_business_days(date(2026, 8, 5), 0) == date(2026, 8, 5)


def test_registry_roundtrip(fue_files):
    reg = fue._load_registry()
    assert reg == {"watches": []}
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z",
        subject="Negev_28-Day dog tox", ask="status of the investigation",
        recipients=["salim.tamboli@vimta.com"], interval_days=2,
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake",
    )
    assert w["id"].startswith("fw_") and len(w["id"]) == 11
    assert w["status"] == "active" and w["nudges_sent"] == 0
    assert w["deadline"] == "2026-08-07"
    reg["watches"].append(w)
    fue._save_registry(reg)
    again = fue._load_registry()
    assert again["watches"][0]["ask"] == "status of the investigation"


def test_registry_corrupt_file_degrades_empty(fue_files, tmp_path):
    (tmp_path / "followups.json").write_text("{not json", encoding="utf-8")
    assert fue._load_registry() == {"watches": []}


def test_processed_persist_and_dry_run(fue_files):
    fue._persist_processed({"a", "b"})
    assert fue._load_processed() == {"a", "b"}
    fue._persist_processed({"a", "b", "c"}, dry_run=True)
    assert fue._load_processed() == {"a", "b"}


def test_live_gate_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    assert fue._live() is False
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    assert fue._live() is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'followup_engine'`

- [ ] **Step 4: Write the module skeleton**

Create `followup_engine.py`:

```python
"""Follow-Up Engine (pilot) -- watch an email thread for a reply; on silence
past a deadline, place a ready-to-send reminder draft in the thread owner's
Outlook Drafts folder and report by email. The machine acts only on silence
and NEVER sends to a counterparty (no auto-send exists in this module).

Spec: followup-engine-spec.md. Lazily imported by app.py (routes + cron only).
Reuses: email_pipeline_sync (Graph), x_transcribe_email (inbox reply,
auto-reply detection), learn_digest (_call_claude_text), config (identity).
"""
import os
import re
import json
import html
import uuid
import logging
from datetime import datetime, timedelta, timezone, date

import email_pipeline_sync as eps
import x_transcribe_email as xte
import learn_digest as ld
import config

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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: 5 PASS

- [ ] **Step 6: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py followup-engine-spec.md docs/superpowers/plans/2026-08-18-followup-engine.md
git commit -m "feat: followup engine skeleton -- stores, business days, watch factory

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Intake gating, instruction parsing, deterministic commands

**Files:**
- Modify: `followup_engine.py` (append after `_note`)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: `ld._call_claude_text(prompt, model, max_tokens=...)` (Task 1 imports).
- Produces: `_TRIGGER_RE`, `_MEDIA_RE`, `_WATCH_ID_RE`, `_parse_command(body_text: str) -> str | None` (returns `"cancel"`, `"resume"`, or `None`), `_extract_json(text: str) -> dict | None`, `parse_instruction(subject: str, body_text: str) -> dict` (always returns a dict with key `is_request: bool`; when true also `thread_subject: str`, `counterparties: list[str]`, `asks: list[{ask, recipients, days, date}]`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_followup_engine.py`)

```python
def test_trigger_and_media_regexes():
    assert fue._TRIGGER_RE.search("please follow up if no reply")
    assert fue._TRIGGER_RE.search("send a REMINDER on Thursday")
    assert fue._TRIGGER_RE.search("chase Vimta on both points")
    assert not fue._TRIGGER_RE.search("here are the meeting notes")
    assert fue._MEDIA_RE.search("watch https://youtu.be/abc123")
    assert not fue._MEDIA_RE.search("see https://vimta.com/about")


def test_parse_command_words():
    assert fue._parse_command("stop") == "cancel"
    assert fue._parse_command("Please CANCEL fw_1234abcd") == "cancel"
    assert fue._parse_command("done, they replied") == "cancel"
    assert fue._parse_command("resume chasing") == "resume"
    assert fue._parse_command("keep going") == "resume"
    assert fue._parse_command("thanks!") is None


def test_extract_json_fenced_and_bare():
    assert fue._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert fue._extract_json('noise {"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    assert fue._extract_json("no json here") is None


def test_parse_instruction_happy(monkeypatch):
    canned = json.dumps({
        "is_request": True,
        "thread_subject": "Negev_28-Day repeated dose toxicity study in dogs",
        "counterparties": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"],
        "asks": [
            {"ask": "status of the investigation", 
             "recipients": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"],
             "days": 2, "date": None},
            {"ask": "summary report for the 28-day dog study",
             "recipients": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"],
             "days": 2, "date": None},
        ],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    out = fue.parse_instruction("FW: Negev_28-Day dog tox", "if Vimta does not reply within 2 days ...")
    assert out["is_request"] is True and len(out["asks"]) == 2


def test_parse_instruction_degrades_on_garbage(monkeypatch):
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: "NOT JSON")
    assert fue.parse_instruction("s", "b") == {"is_request": False}
    def _boom(p, m, max_tokens=2000, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(ld, "_call_claude_text", _boom)
    assert fue.parse_instruction("s", "b") == {"is_request": False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_followup_engine.py -v -k "trigger or command or extract_json or parse_instruction"`
Expected: FAIL with `AttributeError: ... has no attribute '_TRIGGER_RE'`

- [ ] **Step 3: Implement** (append to `followup_engine.py`)

```python
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
```

- [ ] **Step 4: Run the full test file**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py
git commit -m "feat: followup intake gates, command words, instruction parser

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Thread resolution in the owner's mailbox

**Files:**
- Modify: `followup_engine.py` (append)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: `eps.graph_get(url, params=None)`, `eps.MS_GRAPH_BASE`, `config.is_internal_email(addr)`.
- Produces: `_normalize_subject(s: str) -> str`, `_participants(msg: dict) -> set[str]`, `resolve_thread(mailbox: str, subject_hint: str, counterparties: list) -> dict | None` returning `{"conversation_id", "anchor_id", "anchor_received", "subject", "participants": set}` where anchor is the NEWEST message of the best conversation.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _graph_msg(mid, cid, subject, sender, received, to=None, cc=None):
    return {
        "id": mid, "conversationId": cid, "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": received,
        "toRecipients": [{"emailAddress": {"address": a}} for a in (to or [])],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
    }


def test_normalize_subject_strips_prefixes():
    assert fue._normalize_subject("FW: RE: Fwd: Dog tox study") == "Dog tox study"
    assert fue._normalize_subject("  Re:Re: x  ") == "x"


def test_resolve_thread_picks_counterparty_conversation(monkeypatch):
    wrong = _graph_msg("m9", "cid-wrong", "Dog tox study invoice",
                       "billing@other.com", "2026-08-04T10:00:00Z")
    older = _graph_msg("m1", "cid-right", "Dog tox study",
                       "upendra.kumar@adgyllifesciences.com", "2026-08-01T10:00:00Z",
                       to=["salim.tamboli@vimta.com"], cc=["dan@negevlabs.com"])
    newest = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                        "dan@negevlabs.com", "2026-08-05T07:23:00Z",
                        to=["salim.tamboli@vimta.com"], cc=["habibur.khan@vimta.in"])
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: {"value": [wrong, older, newest]})
    out = fue.resolve_thread("dan@negevlabs.com", "FW: Dog tox study",
                             ["salim.tamboli@vimta.com"])
    assert out["conversation_id"] == "cid-right"
    assert out["anchor_id"] == "m2"
    assert out["anchor_received"] == "2026-08-05T07:23:00Z"
    assert "habibur.khan@vimta.in" in out["participants"]


def test_resolve_thread_none_on_no_results(monkeypatch):
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    assert fue.resolve_thread("dan@negevlabs.com", "Nothing", []) is None
    assert fue.resolve_thread("dan@negevlabs.com", "", []) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_followup_engine.py -v -k "normalize or resolve"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement** (append)

```python
# ----------------------------------------------------------------------
#  Thread resolution -- the forward Sara received is a NEW conversation;
#  the real thread lives in the SENDER's mailbox and is found by subject.
# ----------------------------------------------------------------------

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.I)


def _normalize_subject(s: str) -> str:
    s = (s or "").strip()
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        if stripped == s:
            return s.strip()
        s = stripped


def _participants(msg: dict) -> set:
    out = set()
    frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address")
    if frm:
        out.add(frm.strip().lower())
    for key in ("toRecipients", "ccRecipients"):
        for r in (msg.get(key) or []):
            addr = ((r or {}).get("emailAddress") or {}).get("address")
            if addr:
                out.add(addr.strip().lower())
    return out


def resolve_thread(mailbox: str, subject_hint: str, counterparties: list):
    q = _normalize_subject(subject_hint)
    if not q:
        return None
    url = f"{eps.MS_GRAPH_BASE}/users/{mailbox}/messages"
    params = {
        "$search": f'"subject:{q}"',
        "$top": "25",
        "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,conversationId",
    }
    resp = eps.graph_get(url, params=params) or {}
    msgs = resp.get("value") or []
    if not msgs:
        return None

    wanted = {c.strip().lower() for c in (counterparties or []) if c and "@" in c}
    convs = {}
    for m in msgs:
        cid = m.get("conversationId") or ""
        if not cid:
            continue
        entry = convs.setdefault(cid, {"messages": [], "participants": set()})
        entry["messages"].append(m)
        entry["participants"] |= _participants(m)

    def _score(item):
        cid, entry = item
        overlap = len(wanted & entry["participants"]) if wanted else 0
        newest = max(m.get("receivedDateTime") or "" for m in entry["messages"])
        return (overlap, newest)

    cid, entry = max(convs.items(), key=_score)
    if wanted and not (wanted & entry["participants"]):
        # Counterparties were named but no candidate conversation contains
        # them -- guessing here would watch the wrong thread. Refuse instead.
        return None
    anchor = max(entry["messages"], key=lambda m: m.get("receivedDateTime") or "")
    return {
        "conversation_id": cid,
        "anchor_id": anchor.get("id"),
        "anchor_received": anchor.get("receivedDateTime"),
        "subject": anchor.get("subject") or q,
        "participants": entry["participants"],
    }
```

- [ ] **Step 4: Run the full test file**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py
git commit -m "feat: followup thread resolution by subject + counterparty match

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Intake scan (`run_intake`)

**Files:**
- Modify: `followup_engine.py` (append)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-3; `xte.is_auto_reply(m)`, `xte.send_threaded_reply(source_message_id, html_body, attachments=None)`, `eps.html_to_text(content)`, `config.is_internal_email`, `config.normalize_team_email`.
- Produces: `run_intake(dry_run: bool = False, limit: int = None) -> dict` (summary with `registered`, `commands`, `failures`, `scanned`); helpers `_confirmation_html(watches: list) -> str`, `_failure_html(reason: str) -> str`, `_apply_command(reg, watches, cmd) -> list`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _intake_msg(mid, sender, subject, body, cid="conv-intake-1"):
    return {
        "id": mid, "internetMessageId": mid, "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": "2026-08-18T08:00:00Z",
        "conversationId": cid,
        "uniqueBody": {"content": f"<html><body>{body}</body></html>"},
        "internetMessageHeaders": [],
    }


@pytest.fixture
def intake_world(monkeypatch, fue_files):
    """Sara inbox has one forward-with-instruction; owner mailbox search
    resolves one conversation. Claude parse returns two asks. Captures the
    confirmation reply."""
    instruction = _intake_msg(
        "im1", "dan@negevlabs.com", "FW: Dog tox study",
        "Sara: if Vimta does not reply within 2 days, follow up on the "
        "investigation status and the summary report.")
    thread_newest = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                               "upendra.kumar@adgyllifesciences.com",
                               "2026-08-05T07:23:00Z",
                               to=["salim.tamboli@vimta.com"],
                               cc=["dan@negevlabs.com", "habibur.khan@vimta.in"])

    def _graph_get(url, params=None):
        if f"/users/{fue.SARA_MAILBOX}/mailFolders/inbox/messages" in url:
            return {"value": [instruction]}
        if "/users/dan@negevlabs.com/messages" in url:
            return {"value": [thread_newest]}
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    canned = json.dumps({
        "is_request": True, "thread_subject": "Dog tox study",
        "counterparties": ["salim.tamboli@vimta.com"],
        "asks": [
            {"ask": "investigation status", "recipients": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"], "days": 2, "date": None},
            {"ask": "summary report", "recipients": [], "days": 2, "date": None},
        ],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append((mid, html_body)))
    return {"replies": replies}


def test_run_intake_registers_watch_per_ask(intake_world):
    out = fue.run_intake()
    assert out["registered"] == 2
    reg = fue._load_registry()
    assert len(reg["watches"]) == 2
    w = reg["watches"][0]
    assert w["owner"] == "dan@negevlabs.com"
    assert w["mailbox"] == "dan@negevlabs.com"
    assert w["conversation_id"] == "cid-right"
    assert w["anchor_message_id"] == "m2"
    assert w["recipients"] == ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"]
    # Ask 2 gave no explicit recipients -> external thread participants.
    w2 = reg["watches"][1]
    assert set(w2["recipients"]) == {"salim.tamboli@vimta.com",
                                     "habibur.khan@vimta.in",
                                     "upendra.kumar@adgyllifesciences.com"}
    # Confirmation reply went out on the instruction email and names both ids.
    replies = intake_world["replies"]
    assert len(replies) == 1 and replies[0][0] == "im1"
    assert w["id"] in replies[0][1] and w2["id"] in replies[0][1]
    # Idempotent: second scan does nothing.
    assert fue.run_intake()["registered"] == 0


def test_run_intake_dry_run_writes_nothing(intake_world):
    out = fue.run_intake(dry_run=True)
    assert out["registered"] == 2
    assert fue._load_registry()["watches"] == []
    assert intake_world["replies"] == []


def test_run_intake_ignores_external_and_media(monkeypatch, fue_files):
    ext = _intake_msg("x1", "spam@evil.com", "follow up", "follow up please")
    media = _intake_msg("x2", "dan@negevlabs.com", "fyi",
                        "follow up on https://youtu.be/abc123 later")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [ext, media]})
    called = []
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: called.append(1) or '{"is_request": false}')
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda *a, **kw: pytest.fail("must not reply"))
    out = fue.run_intake()
    assert out["registered"] == 0
    assert called == []  # media-link mail never reaches the parser


def test_run_intake_cancel_command_by_watch_id(monkeypatch, fue_files):
    reg = {"watches": [fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-right", anchor_message_id="m2",
        anchor_received="2026-08-05T07:23:00Z", subject="Dog tox",
        ask="investigation status", recipients=[], interval_days=2,
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake-1")]}
    wid = reg["watches"][0]["id"]
    fue._save_registry(reg)
    cmd = _intake_msg("c1", "dan@negevlabs.com", "RE: registered",
                      f"stop {wid} please", cid="conv-other")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [cmd]})
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append(mid))
    out = fue.run_intake()
    assert out["commands"] == 1
    assert fue._load_registry()["watches"][0]["status"] == "cancelled"
    assert replies == ["c1"]


def test_run_intake_unresolvable_thread_replies_honestly(monkeypatch, fue_files):
    instruction = _intake_msg("im2", "dan@negevlabs.com", "FW: Mystery",
                              "please follow up if they do not reply")
    def _graph_get(url, params=None):
        if "/mailFolders/inbox/" in url:
            return {"value": [instruction]}
        return {"value": []}  # owner-mailbox search finds nothing
    monkeypatch.setattr(eps, "graph_get", _graph_get)
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: json.dumps({
        "is_request": True, "thread_subject": "Mystery", "counterparties": [],
        "asks": [{"ask": "an answer", "recipients": [], "days": None, "date": None}]}))
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append((mid, html_body)))
    out = fue.run_intake()
    assert out["failures"] == 1 and fue._load_registry()["watches"] == []
    assert replies and "could not" in replies[0][1].lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_followup_engine.py -v -k intake`
Expected: FAIL with `AttributeError: ... 'run_intake'`

- [ ] **Step 3: Implement** (append)

```python
# ----------------------------------------------------------------------
#  Intake scan
# ----------------------------------------------------------------------


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _confirmation_html(watches: list) -> str:
    rows = "".join(
        f"<li><code>{_esc(w['id'])}</code> -- {_esc(w['ask'])} -> "
        f"{_esc(', '.join(w['recipients']))}; reminder drafted if silent past "
        f"<b>{_esc(w['deadline'])}</b> (max {w['max_nudges']} reminders)</li>"
        for w in watches)
    return (f"<p>Registered {len(watches)} follow-up watch(es) on "
            f"<b>{_esc(watches[0]['subject'])}</b>:</p><ul>{rows}</ul>"
            "<p>I check once a day at 17:00 and place ready-to-send drafts in your "
            "Drafts folder; you will get a report email whenever there is anything "
            "new or still unsent. Reply <b>stop</b> (optionally with watch ids) to "
            "cancel, <b>resume</b> to re-arm a paused watch.</p>")


def _failure_html(reason: str) -> str:
    return (f"<p>I could not register that follow-up: {_esc(reason)}</p>"
            "<p>Forward the thread again (keeping its subject line), or name the "
            "exact subject and who should reply.</p>")


def _apply_command(reg: dict, watches: list, cmd: str) -> list:
    changed = []
    for w in watches:
        if cmd == "cancel" and w.get("status") in ("active", "paused"):
            w["status"] = "cancelled"
            _note(w, "cancelled by owner reply")
            changed.append(w["id"])
        elif cmd == "resume" and w.get("status") == "paused":
            w["status"] = "active"
            w["deadline"] = add_business_days(
                _today_il(), int(w.get("interval_days") or FOLLOWUP_DEFAULT_BUSINESS_DAYS)).isoformat()
            _note(w, "re-armed by owner reply")
            changed.append(w["id"])
    return changed


def run_intake(dry_run: bool = False, limit: int = None) -> dict:
    """Scan Sara's inbox for follow-up registrations and owner commands.
    Idempotent via processed internetMessageIds, persisted after each
    handled message. Dry run: parse + resolve, but write and reply nothing."""
    started = datetime.now(timezone.utc)
    processed = _load_processed()
    reg = _load_registry()
    limit = limit or FOLLOWUP_INTAKE_MAX_MESSAGES

    url = f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/mailFolders/inbox/messages"
    params = {
        "$select": "id,subject,from,receivedDateTime,uniqueBody,internetMessageId,conversationId,internetMessageHeaders",
        "$top": str(limit),
        "$orderby": "receivedDateTime desc",
    }
    messages = (eps.graph_get(url, params=params) or {}).get("value") or []

    registered, commands, failures, outcomes = 0, 0, 0, []
    for m in messages:
        mid = m.get("internetMessageId") or m.get("id") or ""
        if not mid or mid in processed:
            continue
        sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").strip()
        if not sender or sender.lower() == SARA_MAILBOX.lower():
            continue
        if not config.is_internal_email(sender):
            processed.add(mid)
            _persist_processed(processed, dry_run)
            continue
        if xte.is_auto_reply(m):
            processed.add(mid)
            _persist_processed(processed, dry_run)
            continue

        body_text = eps.html_to_text((m.get("uniqueBody") or {}).get("content") or "")

        # Owner commands: explicit watch ids anywhere, or the intake thread.
        cmd = _parse_command(body_text)
        ids_in_body = set(_WATCH_ID_RE.findall(body_text))
        conv = m.get("conversationId") or ""
        cmd_watches = [w for w in reg["watches"]
                       if (w["id"] in ids_in_body)
                       or (not ids_in_body and conv and w.get("intake_conversation_id") == conv)]
        if cmd and cmd_watches:
            changed = [] if dry_run else _apply_command(reg, cmd_watches, cmd)
            if not dry_run:
                _save_registry(reg)
                xte.send_threaded_reply(
                    m.get("id"),
                    f"<p>Done: {cmd} applied to {_esc(', '.join(changed) or 'no eligible watches')}.</p>")
            commands += 1
            outcomes.append({"from": sender, "kind": "command", "cmd": cmd})
            processed.add(mid)
            _persist_processed(processed, dry_run)
            continue

        if not _TRIGGER_RE.search(body_text) or _MEDIA_RE.search(body_text):
            continue  # not ours; leave for other handlers, do not mark

        parsed = parse_instruction(m.get("subject") or "", body_text)
        if not parsed.get("is_request"):
            processed.add(mid)
            _persist_processed(processed, dry_run)
            continue

        owner = config.normalize_team_email(sender)
        thread = resolve_thread(sender, parsed.get("thread_subject") or m.get("subject") or "",
                                parsed.get("counterparties") or [])
        if not thread:
            failures += 1
            if not dry_run:
                xte.send_threaded_reply(m.get("id"), _failure_html(
                    "I could not find that conversation in your mailbox by its subject."))
            outcomes.append({"from": sender, "kind": "resolve_failed"})
            processed.add(mid)
            _persist_processed(processed, dry_run)
            continue

        externals = sorted(p for p in thread["participants"] if not config.is_internal_email(p))
        today = _today_il()
        new = []
        for ask in parsed["asks"]:
            if len(reg["watches"]) + len(new) >= FOLLOWUP_MAX_WATCHES:
                logger.warning("[followup] watch cap reached; skipping remaining asks")
                break
            days = ask.get("days")
            interval = int(days) if days else FOLLOWUP_DEFAULT_BUSINESS_DAYS
            if ask.get("date"):
                try:
                    deadline = date.fromisoformat(ask["date"])
                except ValueError:
                    deadline = add_business_days(today, interval)
            else:
                deadline = add_business_days(today, interval)
            new.append(new_watch(
                owner=owner, mailbox=sender,
                conversation_id=thread["conversation_id"],
                anchor_message_id=thread["anchor_id"],
                anchor_received=thread["anchor_received"],
                subject=thread["subject"], ask=ask["ask"].strip(),
                recipients=[r.strip() for r in (ask.get("recipients") or []) if r] or externals,
                interval_days=interval, deadline=deadline,
                intake_conversation_id=conv))
        registered += len(new)
        outcomes.append({"from": sender, "kind": "registered",
                         "watches": [w["id"] for w in new]})
        if not dry_run and new:
            reg["watches"].extend(new)
            _save_registry(reg)
            xte.send_threaded_reply(m.get("id"), _confirmation_html(new))
        processed.add(mid)
        _persist_processed(processed, dry_run)

    result = {"started": started.isoformat(), "dry_run": dry_run,
              "scanned": len(messages), "registered": registered,
              "commands": commands, "failures": failures, "outcomes": outcomes,
              "finished": datetime.now(timezone.utc).isoformat()}
    logger.info(f"[followup] intake: {registered} registered, {commands} commands, "
                f"{failures} failures of {len(messages)} scanned (dry={dry_run})")
    return result
```

- [ ] **Step 4: Run the full test file**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py
git commit -m "feat: followup intake scan -- register watches, commands, honest failures

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Reply detection and verdicts (`check_replies`)

**Files:**
- Modify: `followup_engine.py` (append)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: Task 1-4 names; `xte.is_auto_reply`, `eps.html_to_text`, `ld._call_claude_text`, `config.is_internal_email`.
- Produces: `_fetch_new_messages(watch: dict) -> list` (sorted ascending; updates `watch["latest_message_id"]`), `_verdict(watch: dict, msgs: list) -> str` (`"ANSWERED"` or `"NOT_ANSWERED"`), `check_replies(reg: dict) -> list[dict]` (events `{"type": "reply_answered"|"reply_paused", "owner", "watch_id", "ask", "who", "when"}`; mutates watch statuses and `last_checked`).

- [ ] **Step 1: Write the failing tests** (append)

```python
def _watch_in_registry(**over):
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-right", anchor_message_id="m2",
        anchor_received="2026-08-05T07:23:00Z", subject="Dog tox",
        ask="investigation status", recipients=["salim.tamboli@vimta.com"],
        interval_days=2, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-intake-1")
    w.update(over)
    return w


def _thread_reply(mid, sender, body, received="2026-08-06T09:00:00Z", headers=None):
    return {"id": mid, "conversationId": "cid-right",
            "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": received,
            "uniqueBody": {"content": f"<html><body>{body}</body></html>"},
            "internetMessageHeaders": headers or []}


def test_check_replies_answered_closes_watch(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [
        _thread_reply("r1", "salim.tamboli@vimta.com",
                      "Investigation complete, root cause was a pipetting error.")]})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "ANSWERED")
    events = fue.check_replies(reg)
    assert reg["watches"][0]["status"] == "answered"
    assert events[0]["type"] == "reply_answered"
    assert reg["watches"][0]["last_checked"] is not None
    assert reg["watches"][0]["latest_message_id"] == "r1"


def test_check_replies_human_nonanswer_pauses(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [
        _thread_reply("r2", "salim.tamboli@vimta.com", "We will get back to you next week.")]})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "NOT_ANSWERED")
    events = fue.check_replies(reg)
    assert reg["watches"][0]["status"] == "paused"
    assert events[0]["type"] == "reply_paused"


def test_check_replies_autoreply_and_internal_ignored(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    ooo = _thread_reply("r3", "salim.tamboli@vimta.com", "Out of office",
                        headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])
    own = _thread_reply("r4", "dan@negevlabs.com", "bumping this myself")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [ooo, own]})
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("no verdict for auto/internal"))
    events = fue.check_replies(reg)
    assert events == [] and reg["watches"][0]["status"] == "active"


def test_check_replies_no_new_messages(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    assert fue.check_replies(reg) == []
    assert reg["watches"][0]["status"] == "active"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_followup_engine.py -v -k check_replies`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement** (append)

```python
# ----------------------------------------------------------------------
#  Daily check part 1: reply detection
# ----------------------------------------------------------------------


def _fetch_new_messages(watch: dict) -> list:
    since = watch.get("last_checked") or watch.get("anchor_received") or ""
    url = f"{eps.MS_GRAPH_BASE}/users/{watch['mailbox']}/messages"
    params = {
        "$filter": f"conversationId eq '{watch['conversation_id']}' "
                   f"and receivedDateTime gt {since}",
        "$select": "id,subject,from,receivedDateTime,uniqueBody,internetMessageHeaders,conversationId",
        "$top": "50",
    }
    msgs = (eps.graph_get(url, params=params) or {}).get("value") or []
    msgs.sort(key=lambda m: m.get("receivedDateTime") or "")
    if msgs:
        watch["latest_message_id"] = msgs[-1].get("id") or watch.get("latest_message_id")
    return msgs


_VERDICT_PROMPT = """A follow-up watch awaits this from an email thread:
ASK: {ask}

New messages on the thread:
{messages}

Does any of these messages substantively answer the ASK? A promise to answer
later ("we will get back to you") is NOT an answer. Reply with exactly one
word: ANSWERED or NOT_ANSWERED."""


def _verdict(watch: dict, msgs: list) -> str:
    blocks = []
    for m in msgs:
        frm = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "?")
        text = eps.html_to_text((m.get("uniqueBody") or {}).get("content") or "")[:2000]
        blocks.append(f"From {frm} at {m.get('receivedDateTime')}:\n{text}")
    try:
        raw = ld._call_claude_text(
            _VERDICT_PROMPT.format(ask=watch["ask"], messages="\n---\n".join(blocks)),
            VERDICT_MODEL, max_tokens=10)
    except Exception as e:
        logger.warning(f"[followup] verdict failed for {watch['id']}: {e}")
        return "NOT_ANSWERED"  # degrade to pause, never to a silent close
    return "ANSWERED" if (raw or "").strip().upper().startswith("ANSWERED") else "NOT_ANSWERED"


def check_replies(reg: dict) -> list:
    events = []
    now = datetime.now(timezone.utc).isoformat()
    for w in reg.get("watches") or []:
        if w.get("status") != "active":
            continue
        try:
            msgs = _fetch_new_messages(w)
        except Exception as e:
            logger.warning(f"[followup] fetch failed for {w['id']}: {e}")
            continue
        w["last_checked"] = now
        ext = [m for m in msgs
               if not config.is_internal_email(
                   (((m.get("from") or {}).get("emailAddress") or {}).get("address") or ""))
               and not xte.is_auto_reply(m)]
        if not ext:
            continue
        who = (((ext[-1].get("from") or {}).get("emailAddress") or {}).get("address") or "")
        when = ext[-1].get("receivedDateTime") or ""
        if _verdict(w, ext) == "ANSWERED":
            w["status"] = "answered"
            _note(w, f"answered by {who} at {when}")
            events.append({"type": "reply_answered", "owner": w["owner"],
                           "watch_id": w["id"], "ask": w["ask"], "who": who, "when": when})
        else:
            w["status"] = "paused"
            _note(w, f"human reply from {who} at {when} did not answer; paused")
            events.append({"type": "reply_paused", "owner": w["owner"],
                           "watch_id": w["id"], "ask": w["ask"], "who": who, "when": when})
    return events
```

- [ ] **Step 4: Run the full test file**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py
git commit -m "feat: followup reply detection with per-ask verdicts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Deadline drafting, exhaustion, unsent sweep

**Files:**
- Modify: `followup_engine.py` (append)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: Task 1-5 names; `eps.graph_post/graph_patch/graph_delete/graph_get`.
- Produces: `_compose_draft(watch: dict) -> str` (HTML body), `_create_draft(watch: dict, body_html: str) -> dict` (`{"message_id", "web_link", "created", "sent": False}`; createReplyAll + PATCH, orphan-delete on failure, NEVER calls `/send`), `process_deadlines(reg: dict, today: date, dry_run: bool) -> list[dict]` (events `draft` / `would_draft` / `exhausted` with `owner, watch_id, ask, recipients, body, web_link?, escalation`), `sweep_unsent(reg: dict) -> list[dict]` (`{"owner", "watch_id", "ask", "web_link", "created"}`, marks `sent`).

- [ ] **Step 1: Write the failing tests** (append)

```python
def _capture_writes(monkeypatch, draft_id="d1", web_link="https://outlook.example/d1"):
    calls = []
    def _post(url, json_body):
        calls.append({"method": "POST", "url": url, "body": json_body})
        if url.endswith("/createReplyAll"):
            return {"id": draft_id, "webLink": web_link}
        return {}
    def _patch(url, json_body):
        calls.append({"method": "PATCH", "url": url, "body": json_body})
        return {}
    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", _patch)
    monkeypatch.setattr(eps, "graph_delete", lambda url: calls.append({"method": "DELETE", "url": url}))
    return calls


def test_process_deadlines_creates_draft_when_live(monkeypatch, fue_files):
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    calls = _capture_writes(monkeypatch)
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: "Dear Dr. Salim,\n\nA gentle reminder on the investigation status.\n\nBest regards")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    w = reg["watches"][0]
    assert events[0]["type"] == "draft" and events[0]["web_link"]
    assert w["nudges_sent"] == 1 and w["deadline"] == "2026-08-11"  # Fri + 2bd -> Tue
    assert w["drafts"][0] == {"message_id": "d1",
                              "web_link": "https://outlook.example/d1",
                              "created": w["drafts"][0]["created"], "sent": False}
    post = [c for c in calls if c["method"] == "POST"]
    assert post[0]["url"].endswith("/users/dan@negevlabs.com/messages/m2/createReplyAll")
    assert not any(c["url"].endswith("/send") for c in post)  # NEVER sends
    patch = [c for c in calls if c["method"] == "PATCH"][0]
    assert patch["body"]["toRecipients"] == [
        {"emailAddress": {"address": "salim.tamboli@vimta.com"}}]
    assert "reminder" in patch["body"]["body"]["content"].lower()


def test_process_deadlines_report_only_without_live(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    monkeypatch.setattr(eps, "graph_post", lambda *a, **kw: pytest.fail("no Graph writes in report-only"))
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events[0]["type"] == "would_draft" and events[0]["body"] == "Reminder body"
    assert reg["watches"][0]["nudges_sent"] == 1


def test_process_deadlines_before_deadline_noop(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    assert fue.process_deadlines(reg, date(2026, 8, 6), dry_run=False) == []
    assert reg["watches"][0]["nudges_sent"] == 0


def test_process_deadlines_exhaustion(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", nudges_sent=3)]}
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events[0]["type"] == "exhausted"
    assert reg["watches"][0]["status"] == "exhausted"


def test_sweep_unsent_marks_sent_on_404(monkeypatch, fue_files):
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "d1", "web_link": "L1", "created": "c", "sent": False},
                   {"message_id": "d2", "web_link": "L2", "created": "c", "sent": False}]
    reg = {"watches": [w]}
    def _get(url, params=None):
        if "/messages/d1" in url:
            raise RuntimeError("404 Not Found")   # sent or deleted -> gone
        return {"id": "d2", "isDraft": True}
    monkeypatch.setattr(eps, "graph_get", _get)
    unsent = fue.sweep_unsent(reg)
    assert w["drafts"][0]["sent"] is True
    assert [u["web_link"] for u in unsent] == ["L2"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_followup_engine.py -v -k "deadlines or sweep"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement** (append)

```python
# ----------------------------------------------------------------------
#  Daily check part 2: drafting on silence -- never sending
# ----------------------------------------------------------------------

_DRAFT_PROMPT = """Write the body of a follow-up reminder email on an existing thread.

Context: we are awaiting "{ask}" on the thread "{subject}". This is reminder
number {escalation} of at most {max_nudges} (1 = gentle nudge, {max_nudges} =
firm but professional, referencing prior reminders).

Hard rules: request a status update ONLY. Do not invent facts, commitments,
deadlines, or consequences. Do not mention automation. No subject line, no
placeholders -- ready to send as-is. Address the recipients generically
(e.g. "Dear colleagues") unless names are obvious from the ask. 80-150 words,
plain paragraphs separated by blank lines."""


def _compose_draft(watch: dict) -> str:
    text = ld._call_claude_text(
        _DRAFT_PROMPT.format(ask=watch["ask"], subject=watch["subject"],
                             escalation=watch.get("nudges_sent", 0) + 1,
                             max_nudges=watch.get("max_nudges", FOLLOWUP_MAX_NUDGES)),
        DRAFT_MODEL, max_tokens=1200)
    paras = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return "".join(f"<p>{_esc(p)}</p>" for p in paras) or "<p></p>"


def _create_draft(watch: dict, body_html: str) -> dict:
    """createReplyAll on the newest known thread message in the OWNER's
    mailbox, then PATCH body (+ explicit recipients, keeping inherited CC so
    the prime CRO stays in the loop). Leaves the message AS A DRAFT -- this
    module has no code path that sends it. Orphan cleanup mirrors
    x_transcribe_email.send_threaded_reply."""
    base = f"{eps.MS_GRAPH_BASE}/users/{watch['mailbox']}/messages"
    target = watch.get("latest_message_id") or watch["anchor_message_id"]
    draft = eps.graph_post(f"{base}/{target}/createReplyAll", {}) or {}
    draft_id = draft.get("id")
    if not draft_id:
        raise RuntimeError("createReplyAll returned no draft id")
    patch_body = {"body": {"contentType": "HTML", "content": body_html}}
    if watch.get("recipients"):
        patch_body["toRecipients"] = [
            {"emailAddress": {"address": r}} for r in watch["recipients"]]
    try:
        eps.graph_patch(f"{base}/{draft_id}", patch_body)
    except Exception:
        try:
            eps.graph_delete(f"{base}/{draft_id}")
        except Exception as cleanup_err:
            logger.warning(f"[followup] could not delete orphaned draft {draft_id}: {cleanup_err}")
        raise
    web_link = draft.get("webLink") or ""
    if not web_link:
        try:
            web_link = (eps.graph_get(f"{base}/{draft_id}", params={"$select": "webLink"}) or {}).get("webLink") or ""
        except Exception:
            web_link = ""
    return {"message_id": draft_id, "web_link": web_link,
            "created": datetime.now(timezone.utc).isoformat(), "sent": False}


def process_deadlines(reg: dict, today: date, dry_run: bool) -> list:
    events = []
    for w in reg.get("watches") or []:
        if w.get("status") != "active":
            continue
        try:
            deadline = date.fromisoformat(w.get("deadline") or "")
        except ValueError:
            _note(w, f"unparseable deadline {w.get('deadline')!r}; resetting")
            w["deadline"] = add_business_days(today, w.get("interval_days") or
                                              FOLLOWUP_DEFAULT_BUSINESS_DAYS).isoformat()
            continue
        if today < deadline:
            continue
        if w.get("nudges_sent", 0) >= w.get("max_nudges", FOLLOWUP_MAX_NUDGES):
            w["status"] = "exhausted"
            _note(w, "max reminders reached; escalated to owner")
            events.append({"type": "exhausted", "owner": w["owner"], "watch_id": w["id"],
                           "ask": w["ask"], "recipients": w["recipients"],
                           "escalation": w.get("nudges_sent", 0)})
            continue
        try:
            body_html = _compose_draft(w)
        except Exception as e:
            logger.warning(f"[followup] compose failed for {w['id']}: {e}")
            continue  # try again next run; deadline unchanged
        escalation = w.get("nudges_sent", 0) + 1
        if _live() and not dry_run:
            try:
                d = _create_draft(w, body_html)
            except Exception as e:
                logger.warning(f"[followup] draft creation failed for {w['id']}: {e}")
                continue  # try again next run
            w.setdefault("drafts", []).append(d)
            _note(w, f"reminder {escalation} drafted ({d['message_id']})")
            events.append({"type": "draft", "owner": w["owner"], "watch_id": w["id"],
                           "ask": w["ask"], "recipients": w["recipients"],
                           "body": body_html, "web_link": d["web_link"],
                           "escalation": escalation})
        else:
            _note(w, f"reminder {escalation} would be drafted (report-only)")
            events.append({"type": "would_draft", "owner": w["owner"], "watch_id": w["id"],
                           "ask": w["ask"], "recipients": w["recipients"],
                           "body": body_html, "escalation": escalation})
        w["nudges_sent"] = escalation
        w["deadline"] = add_business_days(
            today, int(w.get("interval_days") or FOLLOWUP_DEFAULT_BUSINESS_DAYS)).isoformat()
    return events


def sweep_unsent(reg: dict) -> list:
    """A draft that no longer exists (404) or is no longer isDraft was sent or
    deleted by the owner -- stop listing it. Anything still isDraft is UNSENT
    and gets re-reported daily until acted on."""
    unsent = []
    for w in reg.get("watches") or []:
        base = f"{eps.MS_GRAPH_BASE}/users/{w['mailbox']}/messages"
        for d in (w.get("drafts") or []):
            if d.get("sent"):
                continue
            try:
                msg = eps.graph_get(f"{base}/{d['message_id']}", params={"$select": "isDraft"}) or {}
                still_draft = bool(msg.get("isDraft"))
            except Exception:
                still_draft = False  # gone -> sent or deleted either way
            if still_draft:
                unsent.append({"owner": w["owner"], "watch_id": w["id"], "ask": w["ask"],
                               "web_link": d.get("web_link") or "", "created": d.get("created") or ""})
            else:
                d["sent"] = True
                _note(w, f"draft {d['message_id']} left the Drafts folder")
    return unsent
```

- [ ] **Step 4: Run the full test file**

Run: `python -m pytest tests/test_followup_engine.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py
git commit -m "feat: followup drafting on silence, exhaustion, unsent-draft sweep

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Daily report email, orchestration (`run_daily`), status, CLI

**Files:**
- Modify: `followup_engine.py` (append)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: Tasks 1-6; `eps.graph_post` (sendMail), `eps.MS_GRAPH_BASE`.
- Produces: `build_report(owner: str, events: list, unsent: list) -> str`, `_send_email(to_list: list, subject: str, html_body: str)`, `run_daily(dry_run: bool = False) -> dict` (summary: `watches, events, unsent, reports`), `status_summary() -> dict` (for the route), `main()` CLI (`--intake | --check`, `--dry-run`).

- [ ] **Step 1: Write the failing tests** (append)

```python
def _sendmail_capture(monkeypatch):
    sent = []
    def _post(url, json_body):
        if url.endswith("/sendMail"):
            sent.append(json_body)
            return {}
        if url.endswith("/createReplyAll"):
            return {"id": "d1", "webLink": "https://outlook.example/d1"}
        return {}
    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", lambda url, json_body: {})
    monkeypatch.setattr(eps, "graph_delete", lambda url: {})
    return sent


def test_run_daily_reports_would_draft_per_owner(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [
        _watch_in_registry(deadline="2026-08-07"),
        _watch_in_registry(owner="ka@negevlabs.com", mailbox="ka@negevlabs.com",
                           deadline="2026-08-07"),
    ]}
    fue._save_registry(reg)
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily()
    assert out["reports"] == 2 and len(sent) == 2
    recipients = {s["message"]["toRecipients"][0]["emailAddress"]["address"] for s in sent}
    assert recipients == {"dan@negevlabs.com", "ka@negevlabs.com"}
    body = sent[0]["message"]["body"]["content"]
    assert "Reminder body" in body and "report-only" in body.lower()
    saved = fue._load_registry()
    assert saved["watches"][0]["nudges_sent"] == 1  # persisted


def test_run_daily_quiet_day_sends_nothing(monkeypatch, fue_files):
    fue._save_registry({"watches": [_watch_in_registry(deadline="2026-12-31")]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily()
    assert out["reports"] == 0 and sent == []
    assert fue.read_status()["reports"] == 0


def test_run_daily_escalation_ccs_alert(monkeypatch, fue_files):
    fue._save_registry({"watches": [
        _watch_in_registry(deadline="2026-08-07", nudges_sent=3)]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    sent = _sendmail_capture(monkeypatch)
    fue.run_daily()
    addrs = [r["emailAddress"]["address"]
             for r in sent[0]["message"]["toRecipients"]]
    assert addrs == ["dan@negevlabs.com", fue.ALERT_CC]


def test_run_daily_dry_run_persists_nothing(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    fue._save_registry({"watches": [_watch_in_registry(deadline="2026-08-07")]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily(dry_run=True)
    assert out["reports"] == 1 and sent == []           # counted, not sent
    assert fue._load_registry()["watches"][0]["nudges_sent"] == 0  # not persisted


def test_status_summary_shape(fue_files):
    fue._save_registry({"watches": [_watch_in_registry()]})
    fue._write_status({"reports": 0})
    s = fue.status_summary()
    assert s["last_run"] == {"reports": 0}
    assert s["watches"][0]["ask"] == "investigation status"
    assert "conversation_id" not in s["watches"][0]  # trimmed view
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_followup_engine.py -v -k "run_daily or status_summary"`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Implement** (append)

```python
# ----------------------------------------------------------------------
#  Daily report + orchestration
# ----------------------------------------------------------------------


def _send_email(to_list: list, subject: str, html_body: str):
    body = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to_list],
        },
        "saveToSentItems": False,
    }
    eps.graph_post(f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/sendMail", body)


def build_report(owner: str, events: list, unsent: list) -> str:
    parts = []
    drafts = [e for e in events if e["type"] == "draft"]
    woulds = [e for e in events if e["type"] == "would_draft"]
    replies = [e for e in events if e["type"].startswith("reply_")]
    exhausted = [e for e in events if e["type"] == "exhausted"]

    if drafts:
        parts.append("<h3>New reminder drafts in your Drafts folder</h3>")
        for e in drafts:
            link = (f'<p><a href="{_esc(e["web_link"])}">Open draft in Outlook</a></p>'
                    if e.get("web_link") else "")
            parts.append(
                f"<p><b>{_esc(e['ask'])}</b> (watch <code>{_esc(e['watch_id'])}</code>, "
                f"reminder {e['escalation']}) -> {_esc(', '.join(e['recipients']))}</p>"
                f"<blockquote>{e['body']}</blockquote>{link}")
    if woulds:
        parts.append("<h3>Report-only mode: I WOULD have drafted these "
                     "(set FOLLOWUP_LIVE=1 to arm)</h3>")
        for e in woulds:
            parts.append(
                f"<p><b>{_esc(e['ask'])}</b> (watch <code>{_esc(e['watch_id'])}</code>, "
                f"reminder {e['escalation']}) -> {_esc(', '.join(e['recipients']))}</p>"
                f"<blockquote>{e['body']}</blockquote>")
    if replies:
        parts.append("<h3>Replies detected</h3><ul>")
        for e in replies:
            state = "answered -- watch closed" if e["type"] == "reply_answered" \
                else "did not answer -- watch paused (reply 'resume' to keep chasing)"
            parts.append(f"<li><b>{_esc(e['ask'])}</b>: {_esc(e['who'])} replied "
                         f"at {_esc(e['when'])} -- {state} "
                         f"(<code>{_esc(e['watch_id'])}</code>)</li>")
        parts.append("</ul>")
    if exhausted:
        parts.append("<h3>Escalations -- max reminders reached, over to you</h3><ul>")
        for e in exhausted:
            parts.append(f"<li><b>{_esc(e['ask'])}</b> "
                         f"(<code>{_esc(e['watch_id'])}</code>): "
                         f"{e['escalation']} reminders went unanswered.</li>")
        parts.append("</ul>")
    if unsent:
        parts.append("<h3>Still unsent from earlier days</h3><ul>")
        for u in unsent:
            link = (f' -- <a href="{_esc(u["web_link"])}">open draft</a>'
                    if u.get("web_link") else "")
            parts.append(f"<li><b>{_esc(u['ask'])}</b> (drafted {_esc(u['created'][:10])}, "
                         f"<code>{_esc(u['watch_id'])}</code>){link}</li>")
        parts.append("</ul>")
    parts.append("<p>Reply <b>stop</b> or <b>resume</b> with a watch id to control "
                 "a watch. -- Sara Follow-Up Engine</p>")
    return "".join(parts)


def run_daily(dry_run: bool = False) -> dict:
    started = datetime.now(timezone.utc)
    reg = _load_registry()
    events = check_replies(reg)
    today = _today_il()
    events += process_deadlines(reg, today, dry_run)
    unsent = [] if dry_run else sweep_unsent(reg)

    owners = sorted({e["owner"] for e in events} | {u["owner"] for u in unsent})
    reports = 0
    for owner in owners:
        ev = [e for e in events if e["owner"] == owner]
        un = [u for u in unsent if u["owner"] == owner]
        if not ev and not un:
            continue
        n_new = sum(1 for e in ev if e["type"] in ("draft", "would_draft"))
        subject = (f"[follow-up] {n_new} draft(s) ready, "
                   f"{len(un)} still unsent -- {today.isoformat()}")
        html_body = build_report(owner, ev, un)
        if not dry_run:
            to = [owner]
            if any(e["type"] == "exhausted" for e in ev) and ALERT_CC.lower() != owner.lower():
                to.append(ALERT_CC)
            try:
                _send_email(to, subject, html_body)
            except Exception as e:
                logger.error(f"[followup] report email to {owner} failed: {e}")
                continue
        reports += 1
    if not dry_run:
        _save_registry(reg)
    result = {"started": started.isoformat(), "dry_run": dry_run,
              "live": _live(), "watches": len(reg.get("watches") or []),
              "events": events, "unsent": len(unsent), "reports": reports,
              "finished": datetime.now(timezone.utc).isoformat()}
    _write_status(result)
    logger.info(f"[followup] daily: {len(events)} events, {len(unsent)} unsent, "
                f"{reports} reports (dry={dry_run}, live={_live()})")
    return result


def status_summary() -> dict:
    reg = _load_registry()
    keep = ("id", "owner", "subject", "ask", "recipients", "status",
            "deadline", "nudges_sent", "max_nudges", "last_checked", "created")
    return {"last_run": read_status(),
            "live": _live(),
            "watches": [{k: w.get(k) for k in keep} for w in reg.get("watches") or []]}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Follow-Up Engine (pilot)")
    ap.add_argument("--intake", action="store_true", help="run the inbox intake scan")
    ap.add_argument("--check", action="store_true", help="run the daily thread check")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.intake:
        print(json.dumps(run_intake(dry_run=args.dry_run), indent=2, default=str))
    if args.check or not args.intake:
        print(json.dumps(run_daily(dry_run=args.dry_run), indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the FULL suite (not just this file)**

Run: `python -m pytest -q`
Expected: all PASS (existing suite untouched)

- [ ] **Step 5: Commit**

```bash
git add followup_engine.py tests/test_followup_engine.py
git commit -m "feat: followup daily report email, orchestration, status, CLI

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: app.py wiring — routes, scheduler jobs, version bump (CRLF-safe)

**Files:**
- Modify: `app.py` (CRLF — use the scripts below verbatim, do NOT hand-edit multi-line blocks)
- Test: `tests/test_followup_engine.py` (append)

**Interfaces:**
- Consumes: `followup_engine.run_intake / run_daily / status_summary` (Task 4/7).
- Produces: routes `/followup/run`, `/followup/intake`, `/followup/status`; scheduler jobs `followup_daily` (cron 17:00 Asia/Jerusalem) and `followup_intake` (interval); version `2.29.0-followup-pilot`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_followup_engine.py`)

```python
def test_app_routes_registered():
    import app as app_module
    rules = {r.rule for r in app_module.app.url_map.iter_rules()}
    assert {"/followup/run", "/followup/intake", "/followup/status"} <= rules


def test_followup_status_route(fue_files):
    import app as app_module
    fue._write_status({"reports": 1})
    client = app_module.app.test_client()
    resp = client.get("/followup/status")
    assert resp.status_code == 200
    assert resp.get_json()["last_run"] == {"reports": 1}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_followup_engine.py -v -k app_routes`
Expected: FAIL (`assert ... <= rules` fails)

- [ ] **Step 3: Insert the route/wrapper block (byte-level, CRLF-preserving)**

Save as `scratch_wire_routes.py`, run `python scratch_wire_routes.py`, then delete it:

```python
import io

NEW = """\
# ======================================================================
#  FOLLOW-UP ENGINE (pilot) -- silent-thread reminder drafts
#  Spec: followup-engine-spec.md. Module imported lazily; report-only
#  until FOLLOWUP_LIVE=1. The engine NEVER sends to a counterparty.
# ======================================================================

_followup_run_lock = _threading.Lock()
_followup_intake_lock = _threading.Lock()


def followup_daily_run():
    if not _followup_run_lock.acquire(blocking=False):
        logger.info("[followup] daily run already in progress; skipping")
        return
    try:
        import followup_engine
        followup_engine.run_daily()
    except Exception as e:
        logger.error(f"[followup] daily run failed: {e}", exc_info=True)
    finally:
        _followup_run_lock.release()


def followup_intake_run():
    if not _followup_intake_lock.acquire(blocking=False):
        logger.info("[followup] intake already in progress; skipping")
        return
    try:
        import followup_engine
        followup_engine.run_intake()
    except Exception as e:
        logger.error(f"[followup] intake run failed: {e}", exc_info=True)
    finally:
        _followup_intake_lock.release()


def _followup_route(lock, fn_name, dry_run, sync):
    def _invoke():
        import followup_engine
        return getattr(followup_engine, fn_name)(dry_run=dry_run)
    if sync:
        if not lock.acquire(blocking=False):
            return jsonify({"status": "already-running"}), 409
        try:
            return jsonify(_invoke())
        except Exception as e:
            logger.error(f"[followup] {fn_name} failed: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            lock.release()

    def _bg():
        if not lock.acquire(blocking=False):
            return
        try:
            _invoke()
        except Exception as e:
            logger.error(f"[followup] {fn_name} failed: {e}", exc_info=True)
        finally:
            lock.release()
    _threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"status": "started", "dry_run": dry_run})


@app.route("/followup/run", methods=["GET", "POST"])
def followup_run_route():
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true")
    sync = request.args.get("sync", "").lower() in ("1", "true")
    return _followup_route(_followup_run_lock, "run_daily", dry_run, sync)


@app.route("/followup/intake", methods=["GET", "POST"])
def followup_intake_route():
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true")
    sync = request.args.get("sync", "").lower() in ("1", "true")
    return _followup_route(_followup_intake_lock, "run_intake", dry_run, sync)


@app.route("/followup/status")
def followup_status_route():
    import followup_engine
    return jsonify(followup_engine.status_summary())


"""

ANCHOR = '@app.route("/transcribe-email/run", methods=["GET", "POST"])'

with io.open("app.py", "r", encoding="utf-8", newline="") as f:
    src = f.read()
assert src.count(ANCHOR) == 1, "anchor not unique"
block = NEW.replace("\n", "\r\n")
src = src.replace(ANCHOR, block + ANCHOR)
with io.open("app.py", "w", encoding="utf-8", newline="") as f:
    f.write(src)
print("routes inserted OK")
```

Note: `_threading`, `jsonify`, `request`, `logger` all already exist at that point in app.py (the x-transcribe block right below uses them).

- [ ] **Step 4: Insert the scheduler jobs (byte-level)**

Save as `scratch_wire_jobs.py`, run, delete:

```python
import io

NEW = """\
    # Follow-Up Engine: intake scan every 15min (registrations + commands);
    # daily thread check 17:00 Asia/Jerusalem -- drafts land in time for an
    # early-evening send that tops a CRO's next-morning inbox in India.
    _scheduler.add_job(
        followup_intake_run,
        trigger="interval",
        minutes=int(os.environ.get("FOLLOWUP_INTAKE_MINUTES", "15")),
        id="followup_intake",
        replace_existing=True,
    )
    _scheduler.add_job(
        followup_daily_run,
        trigger="cron",
        hour=int(os.environ.get("FOLLOWUP_HOUR", "17")),
        minute=0,
        timezone="Asia/Jerusalem",
        id="followup_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
"""

ANCHOR = "    # Renewal job: every 45min"

with io.open("app.py", "r", encoding="utf-8", newline="") as f:
    src = f.read()
assert src.count(ANCHOR) == 1, "anchor not unique"
block = NEW.replace("\n", "\r\n")
src = src.replace(ANCHOR, block + ANCHOR)
with io.open("app.py", "w", encoding="utf-8", newline="") as f:
    f.write(src)
print("jobs inserted OK")
```

- [ ] **Step 5: Bump the version string (both places — single-line exact replacements, CRLF-safe)**

In app.py replace:
- `"version": "2.28.3-yt-retry", "deployed": "2026-08-17"` → `"version": "2.29.0-followup-pilot", "deployed": "<today YYYY-MM-DD>"`
- `results = {"version": "2.28.3-yt-retry"` → `results = {"version": "2.29.0-followup-pilot"`

(If the current version string differs — main moved — use whatever the two literals currently are; there are exactly two.)

- [ ] **Step 6: Verify syntax, CRLF integrity, and tests**

```bash
python -c "import ast; ast.parse(open('app.py', encoding='utf-8').read()); print('OK')"
git diff --numstat app.py
python -m pytest -q
```

Expected: `OK`; numstat shows roughly +120/-2 on app.py alone (a full-file rewrite means CRLF was mangled — revert and redo via the scripts); full suite PASS including the two new route tests.

- [ ] **Step 7: Commit**

```bash
git add app.py tests/test_followup_engine.py
git commit -m "feat: 2.29.0-followup-pilot -- wire followup engine routes + schedule

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: CLAUDE.md docs + ship via PR

**Files:**
- Modify: `CLAUDE.md` (CRLF — byte-level script)
- Modify: `CACHEBUST`

**Interfaces:**
- Consumes: everything shipped in Tasks 1-8.
- Produces: deployed `2.29.0-followup-pilot` on Railway.

- [ ] **Step 1: Update CLAUDE.md (byte-level, CRLF-preserving)**

Save as `scratch_docs.py`, run, delete. It makes 3 insertions — a module section before `## Common Failure Modes`, endpoint rows appended directly after the `/transcribe-email/status` table row, and an env-vars paragraph before the FYI Triage env paragraph:

```python
import io

with io.open("CLAUDE.md", "r", encoding="utf-8", newline="") as f:
    src = f.read()

def insert_before(anchor, text):
    global src
    assert src.count(anchor) == 1, f"anchor not unique: {anchor[:40]}"
    src = src.replace(anchor, text.replace("\n", "\r\n") + anchor)

def insert_after(anchor, text):
    global src
    assert src.count(anchor) == 1, f"anchor not unique: {anchor[:40]}"
    src = src.replace(anchor, anchor + text.replace("\n", "\r\n"))

MODULE = """\
## followup-engine Module (pilot)

Standalone module (`followup_engine.py`) -- watches registered email threads
for a counterparty reply; on silence past a per-ask deadline, places a
ready-to-send reminder draft in the thread OWNER's Outlook Drafts (app-only
`createReplyAll` + PATCH; explicit recipients when the instruction named
them, inherited CC preserved) and sends the owner ONE report email per run:
new drafts (full text + Graph webLink), replies detected, escalations
(CC `FOLLOWUP_ALERT_CC`), and every STILL UNSENT draft repeated daily until
sent or cancelled (unsent = message still `isDraft`; 404 = sent/deleted).
The machine NEVER sends to a counterparty -- there is no auto-send code path
(parked by team decision); `FOLLOWUP_LIVE` unset ships REPORT-ONLY (no drafts
created, report shows would-draft text). Full spec: `followup-engine-spec.md`.
Imported lazily by app.py. Reuses eps Graph helpers, xte `is_auto_reply` +
`send_threaded_reply`, ld `_call_claude_text`, config identity helpers.

- Intake (15-min scan of Sara's inbox): internal sender forwards a thread with
  an instruction ("if Vimta does not reply within 2 days, draft a reminder for
  me to send"). Deterministic gate (trigger keyword, no media links -- those
  belong to x-transcribe) -> Claude parse (one WATCH PER ASK) -> thread
  resolved in the SENDER's mailbox by normalized-subject `$search` +
  counterparty overlap (the forward itself is a NEW conversation) ->
  confirmation reply with watch ids; unresolvable -> honest failure reply.
  Owner commands, deterministic, no LLM: reply stop/cancel/done (cancels) or
  resume/continue/keep (re-arms), targeting `fw_` ids in the body else the
  intake conversation's watches.
- Daily check (17:00 Asia/Jerusalem): per active watch, new thread messages
  since last check; external non-auto-reply messages get a per-ask Haiku
  verdict -- ANSWERED closes, a human non-answer PAUSES (owner re-arms);
  internal/own messages ignored (pilot limitation: an owner's manual chase
  does not reset the clock). Active past deadline -> Sonnet drafts an
  escalating status-request-only reminder (nudge N of max 3), deadline
  advances by the watch's business-day interval (Mon-Fri); at max ->
  `exhausted` + escalation. Quiet day (no events, nothing unsent) -> no email.
- State on /data: `followups.json` (registry), `followup_processed.json`
  (intake dedup, persisted after EACH handled message), `followup_status.json`.
- Endpoints: `/followup/run`, `/followup/intake` (both `?dry_run=&sync=`),
  `/followup/status`. CLI: `python followup_engine.py --intake|--check [--dry-run]`.

"""

ENDPOINTS = """\
| `/followup/run` | Manual Follow-Up Engine daily check (`?dry_run=&sync=`) |
| `/followup/intake` | Manual Follow-Up Engine inbox intake scan (`?dry_run=&sync=`) |
| `/followup/status` | Follow-Up Engine last run + active watch summaries |
"""

ENV = """\
followup-engine: `FOLLOWUP_LIVE` (set `1` to arm draft creation; UNSET at ship
= report-only), `FOLLOWUP_HOUR` (daily check hour Asia/Jerusalem, default 17),
`FOLLOWUP_INTAKE_MINUTES` (15), `FOLLOWUP_DEFAULT_BUSINESS_DAYS` (2),
`FOLLOWUP_MAX_NUDGES` (3), `FOLLOWUP_MAX_WATCHES` (100),
`FOLLOWUP_INTAKE_MAX_MESSAGES` (25), `FOLLOWUP_PARSE_MODEL` /
`FOLLOWUP_DRAFT_MODEL` (sonnet) / `FOLLOWUP_VERDICT_MODEL` (haiku),
`FOLLOWUP_ALERT_CC` (bk@negevlabs.com). Reuses `BOT_SENDER_EMAIL`,
`CLAUDE_API_KEY`, `MS_GRAPH_*`, `INTERNAL_DOMAINS`.

"""

insert_before("## Common Failure Modes", MODULE)
insert_after("| `/transcribe-email/status` | Last x-transcribe-email scan outcome (scanned/replied + per-message links) |\r\n",
             ENDPOINTS)
insert_before("FYI Triage: `FYI_LIVE`", ENV)

with io.open("CLAUDE.md", "w", encoding="utf-8", newline="") as f:
    f.write(src)
print("CLAUDE.md updated OK")
```

Then verify: `git diff --numstat CLAUDE.md` shows only additions (~55/0). If an anchor assert fires, find the actual anchor text with `grep -n` and adjust the script — do not hand-paste multi-line text into CLAUDE.md.

- [ ] **Step 2: Full suite one last time**

Run: `python -m pytest -q`
Expected: all PASS

- [ ] **Step 3: CACHEBUST + commit + PR**

```bash
ts=$(date +%Y%m%d%H%M%S)
echo -n "$ts" > CACHEBUST
git add CLAUDE.md CACHEBUST
git commit -m "docs: CLAUDE.md followup-engine section + CACHEBUST [$ts]

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push -u origin feat/followup-engine
gh pr create --title "feat: 2.29.0-followup-pilot -- Follow-Up Engine (silent-thread reminder drafts)" --body "Pilot per followup-engine-spec.md: intake scan (forward-to-Sara), daily 17:00 check, reminder drafts in the owner's Drafts, daily report email with unsent carryover. Report-only until FOLLOWUP_LIVE=1. No auto-send code path. No Asana (team decision).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 4: Wait for the required CI check, then merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

Expected: "Offline test suite" green before merge. If it fails, fix on the branch — never bypass.

- [ ] **Step 5: Poll the deploy (Railway takes 60-180s; up to 4 min)**

```bash
for i in $(seq 1 12); do sleep 20; curl -s https://meeting-pipeline-production.up.railway.app/version; echo; done
```

Expected: `"version": "2.29.0-followup-pilot"` appears. "Pushed" is not "deployed" — poll for THIS exact string.

- [ ] **Step 6: Live smoke (read-only)**

```bash
curl -s https://meeting-pipeline-production.up.railway.app/followup/status | python -m json.tool
curl -s "https://meeting-pipeline-production.up.railway.app/followup/intake?dry_run=1&sync=1" | python -m json.tool
```

Expected: status returns `{"last_run": {...}, "live": false, "watches": []}`; the dry intake scans Sara's real inbox and registers/replies to NOTHING (`"dry_run": true`). Report the outputs to Ken. Do NOT set `FOLLOWUP_LIVE` — that is Ken's call after the report-only days.

---

## Self-Review (performed while writing)

- **Spec coverage:** intake/registration incl. failure reply (T4), commands (T2/T4), thread resolution (T3), daily verdicts + pause/close (T5), drafting/escalation/exhaustion + live gate + never-send (T6), report email + unsent carryover + quiet-day skip + CC on escalation (T7), endpoints/scheduler/CLI (T7/T8), state files (T1), docs (T9). Out-of-scope items (auto-send, Asana) have no tasks — by design.
- **Type consistency:** watch dict keys fixed in T1 `new_watch` and used verbatim in T4-T7; event dict shapes fixed in T5/T6 and consumed in T7 `build_report`; `resolve_thread` return keys match T4's usage; test helper names (`_watch_in_registry`, `_graph_msg`, `_thread_reply`) defined before first use.
- **Placeholders:** none — every step carries runnable code or an exact command. One deliberate variable: the Task 8 version-string replacement notes what to do if main's current literal moved.
