#!/usr/bin/env python3
"""
daily-pipeline-digest -- Sara module

Compiles every change and new activity in the NL 2026 Fundraise pipeline
over the previous window (default 24h, resilient to missed runs) and emails
Ken a single readable morning brief. Covers all deal owners, narrates deltas
rather than state, and applies Negev operating rules deterministically
(stale-deal, overdue-task, and wire-watch flags).

Usage:
    python daily_pipeline_digest.py                    # window since last successful run
    python daily_pipeline_digest.py --since 2026-06-09 # explicit window start
    python daily_pipeline_digest.py --dry-run          # no email, no ledger run row

Spec: daily-pipeline-digest spec (CRM-activity-summary branch).
Shares credentials, HTTP helpers, and the SQLite ledger file with
email_pipeline_sync.py. No new dependencies.

Author: Negev Labs
"""

import os
import json
import sqlite3
import logging
import argparse
import uuid
from datetime import datetime, timedelta, timezone

# Shared HTTP helpers (retry, auth), pipeline id, ledger path, html_to_text
import email_pipeline_sync as eps

logger = logging.getLogger("daily-pipeline-digest")

# ======================================================================
#  CONFIG
# ======================================================================

PIPELINE_ID = os.environ.get("DIGEST_PIPELINE_ID", eps.PIPELINE_ID)

DIGEST_RECIPIENTS = [
    r.strip() for r in os.environ.get("DIGEST_RECIPIENTS", "bk@negevlabs.com").split(",") if r.strip()
]
# CC list (e.g. Alex Kubasov) -- empty by default, flip via env when Ken decides
DIGEST_CC = [r.strip() for r in os.environ.get("DIGEST_CC", "").split(",") if r.strip()]

COMPOSER_MODEL = os.environ.get("DIGEST_MODEL", "claude-haiku-4-5-20251001")

# Negev operating rules (ops manual): 1+ week stuck -> flag; Closing 5+ days -> wire watch
STALE_DAYS = int(os.environ.get("DIGEST_STALE_DAYS", "7"))
CLOSING_WATCH_DAYS = int(os.environ.get("DIGEST_CLOSING_WATCH_DAYS", "5"))
DEFAULT_WINDOW_HOURS = 24
# Cap the resilient window so a long outage does not produce a giant digest
MAX_WINDOW_DAYS = 7

# Quiet days: send the one-liner (silence is ambiguous -- did it run?)
QUIET_DAY_SEND = os.environ.get("DIGEST_QUIET_SEND", "true").lower() in ("true", "1", "yes")

# Fixed offset for "today" boundaries and the subject line date. The codebase
# convention is fixed UTC offsets (see weekly pulse cron); 3 = IDT (summer).
ISRAEL_UTC_OFFSET_HOURS = int(os.environ.get("ISRAEL_UTC_OFFSET_HOURS", "3"))

# Stage whose label contains this substring is the wire-watch stage
CLOSING_STAGE_LABEL_MATCH = os.environ.get("DIGEST_CLOSING_STAGE_LABEL", "closing").lower()

TRACKED_PROPERTIES = ("dealstage", "amount", "closedate")

ENGAGEMENT_SPECS = {
    "notes": ["hs_note_body", "hs_createdate", "hubspot_owner_id"],
    "emails": ["hs_email_subject", "hs_email_direction", "hs_createdate", "hubspot_owner_id"],
    "meetings": ["hs_meeting_title", "hs_meeting_start_time", "hs_createdate", "hubspot_owner_id"],
    "calls": ["hs_call_title", "hs_call_body", "hs_createdate", "hubspot_owner_id"],
    "tasks": ["hs_task_subject", "hs_task_status", "hs_timestamp",
              "hs_task_completion_date", "hs_createdate", "hubspot_owner_id"],
}


# ======================================================================
#  TIME HELPERS
# ======================================================================


def _parse_ts(value):
    """HubSpot timestamp (ISO8601 with Z, or epoch-ms string/int) -> aware UTC
    datetime, or None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _ms(dt: datetime) -> str:
    return str(int(dt.timestamp() * 1000))


def israel_day_bounds(now_utc: datetime, offset_hours: int = None) -> tuple:
    """(start_utc, end_utc) of 'today' in Israel local time."""
    offset = ISRAEL_UTC_OFFSET_HOURS if offset_hours is None else offset_hours
    local = now_utc.astimezone(timezone(timedelta(hours=offset)))
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return day_start.astimezone(timezone.utc), day_end.astimezone(timezone.utc)


# ======================================================================
#  RUN LEDGER (shared SQLite file with email-pipeline-sync, own table)
# ======================================================================


def ledger_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(eps.LEDGER_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digest_runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            window_since TEXT,
            window_until TEXT,
            stage_moves INTEGER,
            activities INTEGER,
            flags INTEGER,
            quiet INTEGER,
            sent INTEGER,
            dry_run INTEGER,
            status TEXT
        )"""
    )
    conn.commit()
    return conn


def get_window_start(conn: sqlite3.Connection, now: datetime) -> datetime:
    """Window starts at the last successful real run's window end, so a failed
    or skipped day is covered by the next run. Falls back to 24h; capped at
    MAX_WINDOW_DAYS so an extended outage cannot produce an unbounded digest."""
    default_start = now - timedelta(hours=DEFAULT_WINDOW_HOURS)
    row = conn.execute(
        "SELECT window_until FROM digest_runs WHERE status = 'ok' AND dry_run = 0 "
        "ORDER BY window_until DESC LIMIT 1"
    ).fetchone()
    last = _parse_ts(row[0]) if row and row[0] else None
    if last is None:
        return default_start
    earliest = now - timedelta(days=MAX_WINDOW_DAYS)
    return max(min(last, now), earliest)


def record_run(conn, run_id, started_at, since, until, data, quiet, sent, dry_run, status):
    counts = _data_counts(data)
    conn.execute(
        "INSERT OR REPLACE INTO digest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, started_at, datetime.now(timezone.utc).isoformat(),
         since.isoformat(), until.isoformat(),
         counts["stage_moves"], counts["activities"], counts["flags"],
         1 if quiet else 0, 1 if sent else 0, 1 if dry_run else 0, status),
    )
    conn.commit()


def _data_counts(data) -> dict:
    if not data:
        return {"stage_moves": 0, "activities": 0, "flags": 0}
    return {
        "stage_moves": len(data.get("stage_moves") or []),
        "activities": sum(len(v) for v in (data.get("activity") or {}).values()),
        "flags": sum(len(v) for v in (data.get("flags") or {}).values()),
    }


# ======================================================================
#  HUBSPOT COLLECTORS
# ======================================================================


def search_objects(object_type: str, filters: list, properties: list) -> list:
    """CRM v3 search, paginated."""
    results, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": properties, "limit": 100}
        if after:
            body["after"] = after
        data = eps.hubspot_request("POST", f"/crm/v3/objects/{object_type}/search", body)
        results.extend(data.get("results") or [])
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return results


def get_pipeline_stages() -> list:
    """[{id, label, probability, is_closed, order}] for the fundraise pipeline."""
    data = eps.hubspot_request("GET", f"/crm/v3/pipelines/deals/{PIPELINE_ID}")
    stages = []
    for s in data.get("stages") or []:
        meta = s.get("metadata") or {}
        try:
            prob = float(meta.get("probability") or 0)
        except (TypeError, ValueError):
            prob = 0.0
        stages.append({
            "id": str(s.get("id", "")),
            "label": s.get("label", ""),
            "probability": prob,
            "is_closed": str(meta.get("isClosed", "")).lower() == "true",
            "order": s.get("displayOrder", 0),
        })
    stages.sort(key=lambda s: s["order"])
    return stages


def find_closing_stage(stages: list):
    for s in stages:
        if CLOSING_STAGE_LABEL_MATCH in (s.get("label") or "").lower() and not s.get("is_closed"):
            return s
    return None


def get_pipeline_deals(closing_stage_id: str = "") -> list:
    """All deals in the pipeline with the properties the digest needs."""
    properties = ["dealname", "dealstage", "amount", "closedate", "hubspot_owner_id",
                  "hs_lastmodifieddate", "notes_last_updated", "createdate"]
    if closing_stage_id:
        properties.append(f"hs_date_entered_{closing_stage_id}")
    filters = [{"propertyName": "pipeline", "operator": "EQ", "value": PIPELINE_ID}]
    return search_objects("deals", filters, properties)


def get_owner_maps() -> tuple:
    """(owner_id -> name, user_id -> name). User ids attribute property-history
    changes; owner ids attribute deals and engagements."""
    by_owner, by_user = {}, {}
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        data = eps.hubspot_request("GET", "/crm/v3/owners", params=params)
        for o in data.get("results") or []:
            name = " ".join(p for p in [o.get("firstName"), o.get("lastName")] if p)
            name = name or o.get("email") or str(o.get("id", ""))
            by_owner[str(o.get("id", ""))] = name
            if o.get("userId") is not None:
                by_user[str(o.get("userId"))] = name
        after = ((data.get("paging") or {}).get("next") or {}).get("after")
        if not after:
            break
    return by_owner, by_user


def normalize_deal(raw: dict, stages: list, by_owner: dict, closing_stage_id: str) -> dict:
    props = raw.get("properties") or {}
    stage_id = str(props.get("dealstage") or "")
    stage = next((s for s in stages if s["id"] == stage_id), None)
    try:
        amount = float(props.get("amount"))
    except (TypeError, ValueError):
        amount = None
    return {
        "id": str(raw.get("id", "")),
        "name": props.get("dealname") or "(unnamed deal)",
        "stage_id": stage_id,
        "stage_label": stage["label"] if stage else stage_id,
        "is_closed": stage["is_closed"] if stage else False,
        "probability": stage["probability"] if stage else 0.0,
        "in_closing": bool(closing_stage_id) and stage_id == closing_stage_id,
        "owner": by_owner.get(str(props.get("hubspot_owner_id") or ""), "Unassigned"),
        "amount": amount,
        "last_activity": _parse_ts(props.get("notes_last_updated")),
        "created": _parse_ts(props.get("createdate")),
        "modified": _parse_ts(props.get("hs_lastmodifieddate")),
        "entered_closing": _parse_ts(props.get(f"hs_date_entered_{closing_stage_id}"))
        if closing_stage_id else None,
    }


# ----------------------------------------------------------------------
#  Delta collector (property history)
# ----------------------------------------------------------------------


def fetch_deal_history(deal_id: str) -> dict:
    """propertiesWithHistory for the tracked properties: {prop: [versions]}."""
    data = eps.hubspot_request(
        "GET", f"/crm/v3/objects/deals/{deal_id}",
        params={"propertiesWithHistory": ",".join(TRACKED_PROPERTIES)},
    )
    return data.get("propertiesWithHistory") or {}


def extract_property_changes(versions: list, since: datetime, until: datetime) -> list:
    """Versions (any order) -> in-window changes oldest first:
    [{from, to, at, by_user_id}]. The first-ever version inside the window
    reports from=None (deal/property created in window)."""
    parsed = []
    for v in versions or []:
        ts = _parse_ts(v.get("timestamp"))
        if ts is not None:
            parsed.append((ts, v))
    parsed.sort(key=lambda p: p[0])
    changes = []
    for i, (ts, v) in enumerate(parsed):
        if not (since <= ts < until):
            continue
        prev = parsed[i - 1][1].get("value") if i > 0 else None
        if prev == v.get("value"):
            continue
        changes.append({
            "from": prev,
            "to": v.get("value"),
            "at": ts.isoformat(),
            "by_user_id": str(v.get("updatedByUserId") or ""),
        })
    return changes


def collect_deltas(changed_deal_ids, deals_by_id, since, until, by_user, stage_labels) -> tuple:
    """(stage_moves, property_changes) across all changed deals."""
    stage_moves, property_changes = [], []
    prop_labels = {"amount": "amount", "closedate": "close date"}
    for deal_id in changed_deal_ids:
        deal = deals_by_id[deal_id]
        try:
            history = fetch_deal_history(deal_id)
        except Exception as e:
            logger.warning(f"History fetch failed for deal {deal_id}: {e}")
            continue
        for prop in TRACKED_PROPERTIES:
            for ch in extract_property_changes(history.get(prop) or [], since, until):
                actor = by_user.get(ch["by_user_id"], "") or "system"
                entry = {"deal": deal["name"], "owner": deal["owner"],
                         "by": actor, "at": ch["at"]}
                if prop == "dealstage":
                    entry["from"] = stage_labels.get(ch["from"], ch["from"]) if ch["from"] else "(new deal)"
                    entry["to"] = stage_labels.get(ch["to"], ch["to"]) or ""
                    stage_moves.append(entry)
                else:
                    entry["property"] = prop_labels.get(prop, prop)
                    entry["from"] = ch["from"]
                    entry["to"] = ch["to"]
                    property_changes.append(entry)
    return stage_moves, property_changes


# ----------------------------------------------------------------------
#  Activity collector (engagements)
# ----------------------------------------------------------------------


def _summarize_engagement(object_type: str, props: dict) -> str:
    """Short raw excerpt; the composer turns these into 1-liners."""
    if object_type == "notes":
        return eps.html_to_text(props.get("hs_note_body") or "")[:400] or "(empty note)"
    if object_type == "emails":
        direction = "sent" if (props.get("hs_email_direction") or "") == "EMAIL" else "received"
        return f"email {direction}: {props.get('hs_email_subject') or '(no subject)'}"
    if object_type == "meetings":
        return props.get("hs_meeting_title") or "(meeting)"
    if object_type == "calls":
        title = props.get("hs_call_title") or "(call)"
        body = eps.html_to_text(props.get("hs_call_body") or "")[:200]
        return f"{title}: {body}" if body else title
    if object_type == "tasks":
        return props.get("hs_task_subject") or "(task)"
    return ""


def _window_filters(property_name: str, since: datetime, until: datetime) -> list:
    return [
        {"propertyName": property_name, "operator": "GTE", "value": _ms(since)},
        {"propertyName": property_name, "operator": "LT", "value": _ms(until)},
    ]


def collect_activity(since, until, deals_by_id, by_owner) -> list:
    """Engagements created (and tasks completed) in the window, attributed to
    pipeline deals. [{type, deal, owner, summary, author, at}]."""
    items = []
    for object_type, properties in ENGAGEMENT_SPECS.items():
        found = {}  # id -> (record, kind); completed wins over created for tasks
        for r in search_objects(object_type, _window_filters("hs_createdate", since, until), properties):
            kind = "task created" if object_type == "tasks" else object_type.rstrip("s") + " logged"
            found[str(r.get("id", ""))] = (r, kind)
        if object_type == "tasks":
            completed = search_objects(
                "tasks", _window_filters("hs_task_completion_date", since, until), properties)
            for r in completed:
                found[str(r.get("id", ""))] = (r, "task completed")
        for object_id, (r, kind) in found.items():
            try:
                deal_ids = [d for d in eps.get_associated_ids(object_type, object_id, "deals")
                            if d in deals_by_id]
            except Exception as e:
                logger.warning(f"Association lookup failed for {object_type} {object_id}: {e}")
                continue
            if not deal_ids:
                continue
            props = r.get("properties") or {}
            author = by_owner.get(str(props.get("hubspot_owner_id") or ""), "")
            for deal_id in deal_ids:
                deal = deals_by_id[deal_id]
                items.append({
                    "type": kind,
                    "deal": deal["name"],
                    "owner": deal["owner"],
                    "summary": _summarize_engagement(object_type, props),
                    "author": author,
                    "at": props.get("hs_createdate") or "",
                })
    items.sort(key=lambda i: (i["owner"], i["deal"], i["at"]))
    return items


def collect_open_tasks(deals_by_id, by_owner, due_before: datetime) -> list:
    """Open tasks due before due_before, attributed to pipeline deals."""
    filters = [
        {"propertyName": "hs_task_status", "operator": "NEQ", "value": "COMPLETED"},
        {"propertyName": "hs_timestamp", "operator": "LT", "value": _ms(due_before)},
    ]
    rows = search_objects("tasks", filters, ["hs_task_subject", "hs_task_status",
                                             "hs_timestamp", "hubspot_owner_id"])
    tasks = []
    for r in rows:
        object_id = str(r.get("id", ""))
        try:
            deal_ids = [d for d in eps.get_associated_ids("tasks", object_id, "deals")
                        if d in deals_by_id]
        except Exception as e:
            logger.warning(f"Association lookup failed for task {object_id}: {e}")
            continue
        if not deal_ids:
            continue
        props = r.get("properties") or {}
        tasks.append({
            "id": object_id,
            "subject": props.get("hs_task_subject") or "(task)",
            "due": _parse_ts(props.get("hs_timestamp")),
            "owner": by_owner.get(str(props.get("hubspot_owner_id") or ""), "Unassigned"),
            "deals": [deals_by_id[d]["name"] for d in deal_ids],
        })
    return tasks


# ======================================================================
#  RULES ENGINE (deterministic, no LLM)
# ======================================================================


def flag_stale_deals(deals: list, now: datetime, stale_days: int = None) -> list:
    """Open deals with no logged activity for stale_days+ (ops manual: 1+ week
    stuck -> flag). Falls back to deal creation date when nothing was ever logged."""
    stale_days = STALE_DAYS if stale_days is None else stale_days
    cutoff = now - timedelta(days=stale_days)
    flags = []
    for d in deals:
        if d.get("is_closed"):
            continue
        last = d.get("last_activity") or d.get("created")
        if last is None:
            flags.append({"type": "stale", "deal": d["name"], "owner": d["owner"],
                          "detail": "no activity ever logged"})
        elif last < cutoff:
            flags.append({"type": "stale", "deal": d["name"], "owner": d["owner"],
                          "detail": f"no activity for {(now - last).days} days"})
    return flags


def flag_closing_watch(deals: list, now: datetime, watch_days: int = None) -> list:
    """Deals sitting in the Closing stage for watch_days+ (wire watch)."""
    watch_days = CLOSING_WATCH_DAYS if watch_days is None else watch_days
    cutoff = now - timedelta(days=watch_days)
    flags = []
    for d in deals:
        if not d.get("in_closing") or d.get("is_closed"):
            continue
        entered = d.get("entered_closing")
        if entered and entered <= cutoff:
            flags.append({"type": "wire-watch", "deal": d["name"], "owner": d["owner"],
                          "detail": f"in Closing for {(now - entered).days} days"})
    return flags


def flag_overdue_tasks(overdue_tasks: list) -> list:
    flags = []
    for t in overdue_tasks:
        due = t["due"].date().isoformat() if t.get("due") else "?"
        flags.append({"type": "overdue task", "deal": ", ".join(t.get("deals") or []),
                      "owner": t["owner"],
                      "detail": f"\"{t['subject']}\" due {due}"})
    return flags


def split_overdue_and_due_today(tasks: list, now: datetime, offset_hours: int = None) -> tuple:
    """(overdue, due_today). Overdue = due before today (Israel local); a task
    due later today is not overdue in a 07:00 digest."""
    day_start, day_end = israel_day_bounds(now, offset_hours)
    overdue = [t for t in tasks if t.get("due") and t["due"] < day_start]
    due_today = [t for t in tasks if t.get("due") and day_start <= t["due"] < day_end]
    return overdue, due_today


# ======================================================================
#  DIGEST ASSEMBLY + COMPOSER
# ======================================================================


def group_by_owner(items: list) -> dict:
    grouped = {}
    for item in items:
        grouped.setdefault(item.get("owner") or "Unassigned", []).append(item)
    return grouped


def pipeline_totals(deals: list, stages: list) -> dict:
    by_stage = {}
    for s in stages:
        count = sum(1 for d in deals if d["stage_id"] == s["id"])
        if count:
            by_stage[s["label"]] = count
    weighted = sum((d["amount"] or 0) * d["probability"]
                   for d in deals if not d["is_closed"] and d.get("amount"))
    return {"deals": len(deals), "by_stage": by_stage, "weighted_open_value": round(weighted)}


def build_digest_data(since, until, stage_moves, property_changes, activity,
                      flags, due_today, deals, stages) -> dict:
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "stage_moves": stage_moves,
        "property_changes": property_changes,
        "activity": group_by_owner(activity),
        "flags": group_by_owner(flags),
        "due_today": group_by_owner([
            {"owner": t["owner"], "task": t["subject"], "deal": ", ".join(t.get("deals") or []),
             "due": t["due"].isoformat() if t.get("due") else ""}
            for t in due_today
        ]),
        "totals": pipeline_totals(deals, stages),
    }


def is_quiet(data: dict) -> bool:
    """True when there is nothing to report (footer totals do not count)."""
    return not (data.get("stage_moves") or data.get("property_changes")
                or data.get("activity") or data.get("flags") or data.get("due_today"))


def _fmt_value(value) -> str:
    if value in (None, ""):
        return "(none)"
    return str(value)


def render_fallback(data: dict) -> str:
    """Deterministic plain-text rendering used when the LLM composer fails.
    Same fixed section order; empty sections suppressed."""
    lines = []
    if data.get("stage_moves") or data.get("property_changes"):
        lines.append("STAGE MOVES")
        for m in data.get("stage_moves") or []:
            lines.append(f"- {m['deal']}: {m['from']} -> {m['to']} ({m['by']})")
        for c in data.get("property_changes") or []:
            lines.append(f"- {c['deal']}: {c['property']} {_fmt_value(c['from'])} -> {_fmt_value(c['to'])} ({c['by']})")
        lines.append("")
    if data.get("activity"):
        lines.append("NEW ACTIVITY")
        for owner, items in data["activity"].items():
            lines.append(f"{owner}:")
            for it in items:
                author = f" ({it['author']})" if it.get("author") else ""
                lines.append(f"- {it['deal']} -- {it['type']}: {(it.get('summary') or '')[:120]}{author}")
        lines.append("")
    if data.get("flags"):
        lines.append("FLAGS")
        for owner, items in data["flags"].items():
            lines.append(f"{owner}:")
            for f in items:
                lines.append(f"- [{f['type']}] {f['deal']}: {f['detail']}")
        lines.append("")
    if data.get("due_today"):
        lines.append("DUE TODAY")
        for owner, items in data["due_today"].items():
            lines.append(f"{owner}:")
            for t in items:
                lines.append(f"- {t['task']} ({t['deal']})")
        lines.append("")
    totals = data.get("totals") or {}
    stage_summary = ", ".join(f"{label}: {count}" for label, count in (totals.get("by_stage") or {}).items())
    lines.append(f"Pipeline: {totals.get('deals', 0)} deals ({stage_summary}); "
                 f"weighted open value ${totals.get('weighted_open_value', 0):,}")
    return "\n".join(lines).strip()


def compose_digest(data: dict) -> str:
    """One LLM call turning collected deltas + flags into the morning brief.
    Rules logic stays deterministic -- the LLM only composes prose."""
    import anthropic

    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")

    prompt = f"""You are Sara, Negev Labs' operations assistant. Compose Ken's daily fundraise pipeline digest email from the JSON data below.

Format rules:
- Plain text only. No HTML, no markdown syntax.
- Fixed section order, each with an ALL-CAPS header. OMIT a section entirely if it has no items:
  1. STAGE MOVES -- one line per move: deal, old stage -> new stage, who moved it. Include amount/close-date changes here too.
  2. NEW ACTIVITY -- grouped by owner (the JSON is pre-grouped), then by deal. Summarize each note/email/call into ONE short line. Name the author when known.
  3. FLAGS -- grouped by owner: stale deals, overdue tasks, wire-watch. One line each, keep the detail text.
  4. DUE TODAY -- tasks due today, by owner.
- End with the one-line pipeline totals footer from "totals" (deal counts by stage, weighted open value in USD).
- Direct, confident tone. No greetings, no sign-off, no filler. Facts only -- never invent or embellish beyond the data.

Data (JSON):
{json.dumps(data, ensure_ascii=False, default=str)}

Return ONLY the email body text."""

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=COMPOSER_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in response.content if hasattr(block, "text")).strip()
    if not text:
        raise RuntimeError("composer returned empty body")
    return text


# ======================================================================
#  MAILER (same mechanism as Weekly Pulse / email-pipeline-sync reports)
# ======================================================================


def send_digest_email(subject: str, body: str):
    sender = os.environ.get("BOT_SENDER_EMAIL", "")
    if not sender:
        raise RuntimeError("BOT_SENDER_EMAIL not set -- digest not emailed")
    message = {
        "subject": subject,
        "body": {"contentType": "text", "content": body},
        "toRecipients": [{"emailAddress": {"address": r}} for r in DIGEST_RECIPIENTS],
    }
    if DIGEST_CC:
        message["ccRecipients"] = [{"emailAddress": {"address": r}} for r in DIGEST_CC]
    eps.graph_post(f"{eps.MS_GRAPH_BASE}/users/{sender}/sendMail",
                   {"message": message, "saveToSentItems": False})
    logger.info(f"Digest emailed to {', '.join(DIGEST_RECIPIENTS)}")


# ======================================================================
#  RUN ORCHESTRATION
# ======================================================================


def run_digest(dry_run: bool = False, since_override: datetime = None) -> dict:
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    started_at = now.isoformat()
    conn = ledger_connect()
    since = since_override or get_window_start(conn, now)
    until = now
    logger.info(f"Digest run {run_id}: window {since.isoformat()} .. {until.isoformat()}, dry_run={dry_run}")

    data, quiet, sent = None, False, False
    try:
        stages = get_pipeline_stages()
        stage_labels = {s["id"]: s["label"] for s in stages}
        closing = find_closing_stage(stages)
        closing_id = closing["id"] if closing else ""
        if not closing:
            logger.warning("No Closing stage matched -- wire-watch flags disabled this run")

        by_owner, by_user = get_owner_maps()
        deals = [normalize_deal(r, stages, by_owner, closing_id)
                 for r in get_pipeline_deals(closing_id)]
        deals_by_id = {d["id"]: d for d in deals}
        logger.info(f"Pipeline {PIPELINE_ID}: {len(deals)} deals")

        changed_ids = [d["id"] for d in deals
                       if d.get("modified") and since <= d["modified"] < until]
        stage_moves, property_changes = collect_deltas(
            changed_ids, deals_by_id, since, until, by_user, stage_labels)
        activity = collect_activity(since, until, deals_by_id, by_owner)

        _, day_end = israel_day_bounds(now)
        open_tasks = collect_open_tasks(deals_by_id, by_owner, day_end)
        overdue, due_today = split_overdue_and_due_today(open_tasks, now)
        flags = (flag_stale_deals(deals, now)
                 + flag_overdue_tasks(overdue)
                 + flag_closing_watch(deals, now))

        data = build_digest_data(since, until, stage_moves, property_changes,
                                 activity, flags, due_today, deals, stages)

        local_date = (now + timedelta(hours=ISRAEL_UTC_OFFSET_HOURS)).strftime("%a %d %b %Y")
        subject = f"[Sara] Pipeline digest -- {local_date}"
        quiet = is_quiet(data)
        if quiet:
            body = "No pipeline changes yesterday. Nothing flagged, nothing due today."
        else:
            try:
                body = compose_digest(data)
            except Exception as e:
                logger.error(f"Composer failed, sending fallback rendering: {e}")
                body = render_fallback(data)

        if dry_run:
            logger.info(f"[dry-run] digest body:\n{body}")
            print(f"\nSubject: {subject}\n\n{body}\n")
        elif quiet and not QUIET_DAY_SEND:
            logger.info("Quiet day and DIGEST_QUIET_SEND disabled -- skipping send")
        else:
            send_digest_email(subject, body)
            sent = True

        record_run(conn, run_id, started_at, since, until, data, quiet, sent, dry_run, "ok")
        return data
    except Exception:
        record_run(conn, run_id, started_at, since, until, data, quiet, sent, dry_run, "error")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Daily NL 2026 Fundraise pipeline digest")
    parser.add_argument("--since", help="Window start YYYY-MM-DD (overrides ledger window)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Collect and compose but send no email; run not recorded as window anchor")
    args = parser.parse_args()

    since_override = None
    if args.since:
        since_override = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_digest(dry_run=args.dry_run, since_override=since_override)


if __name__ == "__main__":
    main()
