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

_TRIGGER_RE = re.compile(r"\b(follow(?:s|ed|ing)?[\s-]?up|remind(?:er)?|chase)\b", re.I)
# Media links belong to x_transcribe_email; a follow-up request never needs one.
_MEDIA_RE = re.compile(r"(?<!\w)(?:x\.com|twitter\.com|youtube\.com|youtu\.be)/", re.I)
_WATCH_ID_RE = re.compile(r"\bfw_[0-9a-f]{8}\b")
_CANCEL_RE = re.compile(r"\b(stop|cancel|done)\b", re.I)
_RESUME_RE = re.compile(r"\b(resume|continue|keep|re-?arm)\b", re.I)
# Command word must LEAD the body or one of its lines (after an optional
# short greeting) UNLESS an explicit fw_ id is present -- see _parse_command.
_GREETING_RE = re.compile(r"^\s*(?:hi|hello|hey|thanks?|dear)\b[^,\n]{0,20},\s*", re.I)
_CANCEL_LEAD_RE = re.compile(r"^\s*(stop|cancel|done)\b", re.I)
_RESUME_LEAD_RE = re.compile(r"^\s*(resume|continue|keep|re-?arm)\b", re.I)


def _parse_command(body_text: str) -> str:
    """Deterministic, no LLM. Cancel wins over resume on a conflicting
    message. Human-approved deviation from spec line 53's literal "a reply
    CONTAINING stop|cancel|done": an ordinary reply can contain "done" /
    "continue" / "keep" as plain English nowhere related to a command (e.g.
    "once the report's done, loop in Legal"), and Sara's own confirmation
    reply invites replies in the very thread these words show up in -- so
    absent an explicit fw_ id, the command word must actually LEAD the
    message or one of its lines (after an optional short greeting), not
    merely appear anywhere. An explicit fw_ id is unambiguous enough that
    the original anywhere-in-body match still applies."""
    text = body_text or ""
    if _WATCH_ID_RE.search(text):
        if _CANCEL_RE.search(text):
            return "cancel"
        if _RESUME_RE.search(text):
            return "resume"
        return None
    lines = text.splitlines() or [text]
    stripped = [_GREETING_RE.sub("", ln, count=1) for ln in lines]
    if any(_CANCEL_LEAD_RE.match(ln) for ln in stripped):
        return "cancel"
    if any(_RESUME_LEAD_RE.match(ln) for ln in stripped):
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


# ----------------------------------------------------------------------
#  Thread resolution -- the forward Sara received is a NEW conversation;
#  the real thread lives in the SENDER's mailbox and is found by subject.
# ----------------------------------------------------------------------

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.I)
# A leading tenant/gateway tag, e.g. "[EXTERNAL]" -- many Exchange tenants
# prepend this to exactly the externally-sourced mail this feature watches.
# Anchored at the start only: a bracket appearing mid-subject is untouched.
_SUBJECT_TAG_RE = re.compile(r"^\s*\[[^\[\]]+\]\s*")


def _normalize_subject(s: str) -> str:
    s = (s or "").strip()
    while True:
        stripped = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        stripped = _SUBJECT_TAG_RE.sub("", stripped, count=1)
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

    if not convs:
        # Every message lacked a usable conversationId -- nothing to
        # score. max() on an empty sequence would raise; refuse instead.
        return None
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
            # is None (not falsy) check -- same contract as new_watch's
            # interval_days=0 (Task 1): an explicit same-day-chase cadence
            # must survive a resume, not get silently upgraded to the
            # default by an `x or default` truthiness check on 0.
            stored_interval = w.get("interval_days")
            interval = (FOLLOWUP_DEFAULT_BUSINESS_DAYS if stored_interval is None
                        else int(stored_interval))
            w["deadline"] = add_business_days(_today_il(), interval).isoformat()
            _note(w, "re-armed by owner reply")
            changed.append(w["id"])
    return changed


def _find_existing_watch(reg: dict, new: list, intake_conversation_id: str, ask_text: str):
    """An equivalent watch already exists for this intake conversation + ask
    text. Guards against duplicate registration on a retry -- if
    send_threaded_reply raised on a prior scan, mid never got marked
    processed (the registry save already landed, before the failed reply),
    so the SAME instruction gets re-parsed from scratch and would otherwise
    call new_watch() again with a fresh random id. A CANCELLED prior watch
    does not block re-registration -- the owner may deliberately cancel and
    re-ask for the same thing. Compares only fields already in the watch
    schema (intake_conversation_id, ask, status); adds no new field."""
    for w in (reg.get("watches") or []) + new:
        if (w.get("intake_conversation_id") == intake_conversation_id
                and w.get("ask") == ask_text and w.get("status") != "cancelled"):
            return w
    return None


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

        if cmd and ids_in_body and not cmd_watches:
            # A command word plus an explicit fw_ id that matches no known
            # watch -- almost certainly a typo. "Honest failure, never
            # silence" per the module's own rule; same reply-then-mark
            # ordering as the resolve_thread failure path below.
            failures += 1
            if not dry_run:
                xte.send_threaded_reply(m.get("id"), _failure_html(
                    f"I do not recognize watch id(s) {', '.join(sorted(ids_in_body))}."))
            outcomes.append({"from": sender, "kind": "unknown_watch_id"})
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
        matched_existing = []
        for ask in parsed["asks"]:
            ask_text = ask["ask"].strip()
            existing = _find_existing_watch(reg, new, conv, ask_text)
            if existing:
                # Already registered -- e.g. a retry after send_threaded_reply
                # raised on a prior scan (the registry save happens before the
                # reply is sent, so mid never got marked processed and this
                # instruction is being re-parsed from scratch). Skipping keeps
                # the registry idempotent without adding a new watch field;
                # collected so the confirmation that failed to send earlier
                # can still go out below, instead of being lost for good.
                matched_existing.append(existing)
                continue
            if len(reg["watches"]) + len(new) >= FOLLOWUP_MAX_WATCHES:
                logger.warning("[followup] watch cap reached; skipping remaining asks")
                break
            days = ask.get("days")
            # is None (not falsy) check -- an explicit days=0 ("chase the
            # same day") must not be silently upgraded to the default by an
            # `x or default` truthiness check; parse_instruction already
            # preserves 0 (Task 2), this is where it would otherwise be lost.
            interval = FOLLOWUP_DEFAULT_BUSINESS_DAYS if days is None else int(days)
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
                subject=thread["subject"], ask=ask_text,
                recipients=[r.strip() for r in (ask.get("recipients") or []) if r] or externals,
                interval_days=interval, deadline=deadline,
                intake_conversation_id=conv))
        registered += len(new)
        if new:
            outcomes.append({"from": sender, "kind": "registered",
                             "watches": [w["id"] for w in new]})
            if not dry_run:
                reg["watches"].extend(new)
                _save_registry(reg)
                xte.send_threaded_reply(m.get("id"), _confirmation_html(new))
        elif matched_existing:
            # Every ask already had a watch -- the confirmation for them
            # never went out (that is exactly why mid was never marked
            # processed). Re-sending it now completes the interrupted
            # operation; it is not a duplicate since nothing new is created.
            outcomes.append({"from": sender, "kind": "reconfirmed",
                             "watches": [w["id"] for w in matched_existing]})
            if not dry_run:
                xte.send_threaded_reply(m.get("id"), _confirmation_html(matched_existing))
        processed.add(mid)
        _persist_processed(processed, dry_run)

    result = {"started": started.isoformat(), "dry_run": dry_run,
              "scanned": len(messages), "registered": registered,
              "commands": commands, "failures": failures, "outcomes": outcomes,
              "finished": datetime.now(timezone.utc).isoformat()}
    logger.info(f"[followup] intake: {registered} registered, {commands} commands, "
                f"{failures} failures of {len(messages)} scanned (dry={dry_run})")
    return result


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
    # Graph's own Z-suffixed format (no microseconds, no +00:00) -- NOT
    # .isoformat(), which on a tz-aware UTC datetime yields "+00:00" plus
    # microseconds. This value feeds straight into _fetch_new_messages'
    # "receivedDateTime gt {since}" $filter on the NEXT check, and Graph
    # rejects that shape. Same convention as learn_digest.py and
    # fyi_triage.py's own receivedDateTime filters.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            # is None (not falsy) check -- an explicit interval_days=0 must
            # survive a deadline reset too; `x or default` would silently
            # upgrade a same-day-chase watch to the 2-day default. Same fix
            # as new_watch (Task 1) and _apply_command's resume (Task 4) --
            # standing rule, do not reintroduce.
            stored_interval = w.get("interval_days")
            interval = (FOLLOWUP_DEFAULT_BUSINESS_DAYS if stored_interval is None
                        else int(stored_interval))
            w["deadline"] = add_business_days(today, interval).isoformat()
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
        # is None check -- see comment above; same anti-pattern, same fix.
        # Computed once, shared by the LIVE and REPORT-ONLY branches below
        # -- DRY RUN never uses it (it mutates nothing), but the
        # computation itself is pure (no I/O, no mutation) so precomputing
        # it here is harmless and avoids a third copy of the interval logic.
        stored_interval = w.get("interval_days")
        interval = (FOLLOWUP_DEFAULT_BUSINESS_DAYS if stored_interval is None
                    else int(stored_interval))
        next_deadline = add_business_days(today, interval).isoformat()

        if dry_run:
            # CONTROLLER RULING (dry-run budget fix): dry_run=True must be
            # safe to invoke at any time, whether or not FOLLOWUP_LIVE is
            # set -- report what WOULD happen and mutate NOTHING (no
            # nudges_sent, no deadline, no note).
            events.append({"type": "would_draft", "owner": w["owner"], "watch_id": w["id"],
                           "ask": w["ask"], "recipients": w["recipients"],
                           "body": body_html, "escalation": escalation})
            continue

        if _live():
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
            w["nudges_sent"] = escalation
            w["deadline"] = next_deadline
        else:
            # CONTROLLER RULING (dry-run budget fix): REPORT-ONLY is the
            # ship state (FOLLOWUP_LIVE unset). Advance the deadline so the
            # watch re-surfaces on the same cadence a live run would, but
            # leave nudges_sent UNTOUCHED -- a run that drafted nothing in
            # anyone's mailbox must never consume the nudge budget, or
            # every watch silently exhausts itself within max_nudges
            # report-only days with zero real reminders ever sent (the
            # defect: report-only used to advance nudges_sent
            # unconditionally, so setting FOLLOWUP_LIVE=1 later found every
            # pilot watch already dead). The exhaustion check above
            # therefore never fires from repeated report-only runs alone --
            # no separate guard needed, since nudges_sent structurally
            # cannot reach max_nudges this way.
            _note(w, f"reminder {escalation} would be drafted (report-only)")
            events.append({"type": "would_draft", "owner": w["owner"], "watch_id": w["id"],
                           "ask": w["ask"], "recipients": w["recipients"],
                           "body": body_html, "escalation": escalation})
            w["deadline"] = next_deadline
    return events


def sweep_unsent(reg: dict) -> list:
    """A draft that no longer exists (404) or is no longer isDraft was sent or
    deleted by the owner -- stop listing it. Anything still isDraft is UNSENT
    and gets re-reported daily until acted on.

    MANDATED CORRECTION (pre-flight Ruling C): a CANCELLED watch is skipped
    entirely, even if Graph still reports isDraft=True on its draft. Spec
    line 47 requires an unsent draft be re-reported "daily until sent or
    cancelled" -- the brief's literal code here only ever looked at Graph's
    isDraft/404 state and never read w["status"], so a watch cancelled AFTER
    its draft was created would otherwise nag forever, since
    _apply_command's cancel path (Task 4) only flips status and never
    touches w["drafts"]. Only "cancelled" is skipped -- paused, answered,
    and exhausted watches still get their stale drafts reported, unchanged
    from the brief."""
    unsent = []
    for w in reg.get("watches") or []:
        if w.get("status") == "cancelled":
            continue
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
