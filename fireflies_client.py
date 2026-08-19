"""
Fireflies GraphQL client (recent + by-id transcript fetch).
Extracted verbatim from app.py (Phase 2 refactor). Re-exported there so
existing references and tests (app_module.X) keep resolving. ASCII-only.

Quota model (load-bearing -- see CLAUDE.md Common Failure Modes):
Fireflies enforces a hard daily request quota (~50/day, resetting 00:00 UTC)
that is shared workspace-wide with anything else using the same key,
including the Fireflies MCP. Exhausting it takes down BOTH of Sara's
ingestion paths at once -- the poller raises on every run and webhook
transcript fetches fail -- so a quota error is modelled as its own
exception type and remembered, rather than retried blindly.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from config import DATA_DIR, FIREFLIES_API_KEY

logger = logging.getLogger(__name__)

FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"

# Where the "quota is spent until X" note lives. Persisted rather than kept
# in memory so a container restart does not start re-burning requests
# against a quota that will not reset until midnight UTC.
QUOTA_STATE_FILE = os.path.join(DATA_DIR, "fireflies_quota.json")

# Longest plausible real meeting, in minutes. Doubles as the clamp that stops
# a mis-scaled duration from widening the poll window without bound.
MAX_MEETING_MINUTES = 600

# Upper bound on records asked for in one list query.
LIST_QUERY_LIMIT = 50


class FirefliesQuotaExceeded(Exception):
    """The daily Fireflies request quota is spent.

    Distinct from a generic API error because the correct response is
    different: stop calling until `retry_after` instead of retrying. A quota
    that resets at midnight UTC cannot be beaten by a backoff loop measured
    in seconds.
    """

    def __init__(self, message: str, retry_after: datetime = None):
        super().__init__(message)
        self.retry_after = retry_after


def _next_utc_midnight(now: datetime = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC so comparisons never raise."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _load_quota_block() -> datetime:
    try:
        with open(QUOTA_STATE_FILE, "r") as f:
            raw = (json.load(f) or {}).get("blocked_until")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(raw)))
    except (TypeError, ValueError):
        return None


def _save_quota_block(when: datetime) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(QUOTA_STATE_FILE, "w") as f:
            json.dump({"blocked_until": when.isoformat() if when else None}, f)
    except OSError as e:
        # Losing the note only costs extra 429s -- never fail the caller.
        logger.warning(f"[fireflies] Could not persist quota block: {e}")


def quota_blocked_until() -> datetime:
    """Datetime the quota frees up, or None if requests are allowed now."""
    when = _load_quota_block()
    if when and when > datetime.now(timezone.utc):
        return when
    return None


def clear_quota_block() -> None:
    """Forget any recorded quota block (ops / test helper)."""
    _save_quota_block(None)


def _parse_quota_error(errors: list):
    """Return (is_quota_error, retry_after). Fireflies puts a machine-readable
    reset timestamp in extensions.metadata.retryAfter (epoch ms)."""
    is_quota = False
    retry_after = None
    for err in errors or []:
        ext = err.get("extensions") or {}
        code = err.get("code") or ext.get("code")
        status = ext.get("status")
        if code == "too_many_requests" or status == 429:
            is_quota = True
            raw = (ext.get("metadata") or {}).get("retryAfter")
            if raw and not retry_after:
                try:
                    value = float(raw)
                    retry_after = datetime.fromtimestamp(
                        value / 1000 if value > 1e12 else value, tz=timezone.utc
                    )
                except (TypeError, ValueError, OSError, OverflowError):
                    retry_after = None
    return is_quota, retry_after


def fireflies_query(query: str, variables: dict = None) -> dict:
    # Short-circuit while the quota is known-spent. This is the difference
    # between one wasted request per reset window and hundreds.
    blocked = quota_blocked_until()
    if blocked:
        raise FirefliesQuotaExceeded(
            f"Fireflies daily quota spent; request not sent. Frees up {blocked.isoformat()}",
            retry_after=blocked,
        )

    headers = {
        "Authorization": f"Bearer {FIREFLIES_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(FIREFLIES_GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        is_quota, retry_after = _parse_quota_error(data["errors"])
        if is_quota:
            retry_after = _as_utc(retry_after) or _next_utc_midnight()
            _save_quota_block(retry_after)
            logger.warning(
                f"[fireflies] Daily quota exhausted -- suspending calls until {retry_after.isoformat()}"
            )
            raise FirefliesQuotaExceeded(
                f"Fireflies daily quota exhausted until {retry_after.isoformat()}",
                retry_after=retry_after,
            )
        raise Exception(f"Fireflies API error: {data['errors']}")
    return data["data"]


def duration_to_minutes(raw) -> float:
    """Normalize the Fireflies 'duration' field to minutes.

    The units are ambiguous in this codebase's history: this module treated
    the field as MINUTES while the Weekly Pulse collector in app.py divided
    it by 60 as SECONDS. Only one can be right, and neither was ever checked
    against a real response -- the Pulse value only ever reached an LLM
    prompt, where a 60x error is invisible.

    Rather than guess, normalize so both readings agree for any realistic
    meeting: a value too large to be a plausible meeting length in minutes
    can only be seconds, so it is converted. Small values stay minutes,
    which at worst widens the poll window slightly -- harmless, because
    already-processed transcripts are skipped by id. The result is clamped so
    a mis-scaled value can never widen the window without bound.
    """
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    if value > MAX_MEETING_MINUTES:
        value = value / 60.0
    return min(value, float(MAX_MEETING_MINUTES))


def get_recent_transcripts(since_minutes: int = 30) -> list:
    """Transcripts whose meeting ENDED within the trailing window.

    Server-side date bounds keep the response small: asking for every
    transcript in the workspace (with sentences attached) on every poll is
    what made this query expensive enough to matter against a ~50/day quota.
    The requested range is widened by MAX_MEETING_MINUTES so a long meeting
    whose START predates the window is still returned, then the end time is
    checked client-side.

    Deliberately does NOT request `sentences` or `summary`: callers that need
    the body fetch it per-id via get_transcript_by_id, so pulling it for
    every candidate was pure waste.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=since_minutes)
    range_start = cutoff - timedelta(minutes=MAX_MEETING_MINUTES)
    query = """
    query RecentTranscripts($fromDate: DateTime, $toDate: DateTime, $limit: Int) {
        transcripts(fromDate: $fromDate, toDate: $toDate, limit: $limit) {
            id title dateString: date duration organizer_email participants
        }
    }
    """
    data = fireflies_query(query, {
        "fromDate": range_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": LIST_QUERY_LIMIT,
    })
    transcripts = data.get("transcripts") or []
    recent = []
    for t in transcripts:
        try:
            ds = t.get("dateString", "")
            if isinstance(ds, (int, float)):
                t_start = datetime.fromtimestamp(ds / 1000 if ds > 1e12 else ds, tz=timezone.utc)
            else:
                t_start = datetime.fromisoformat(str(ds).replace("Z", "+00:00"))
            # Gate on meeting END time (start + duration), not start time.
            # A poll window is meant to catch "transcript became available
            # recently" -- filtering on start alone silently drops any
            # meeting longer than the window itself, because by the time
            # Fireflies finishes processing it the start timestamp has
            # already scrolled out of the lookback (bit an 86-min meeting
            # on 2026-08-05: never picked up by webhook OR poll).
            t_end = t_start + timedelta(minutes=duration_to_minutes(t.get("duration")))
            if t_end >= cutoff:
                recent.append(t)
        except (ValueError, TypeError, OSError):
            continue
    return recent


def get_transcript_by_id(transcript_id: str) -> dict:
    query = """
    query GetTranscript($id: String!) { transcript(id: $id) {
        id title dateString: date duration organizer_email participants
        summary { shorthand_bullet short_summary action_items keywords overview notes }
        sentences { speaker_name text }
    }}
    """
    data = fireflies_query(query, {"id": transcript_id})
    return data.get("transcript")
