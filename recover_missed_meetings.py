#!/usr/bin/env python3
"""
One-off recovery for meetings Sara never processed during the Fireflies
quota outage (2026-08-06 -> 2026-08-19).

Background: the Fireflies API enforces a hard ~50 requests/day quota that
resets at 00:00 UTC. Sara's poller ran every 5 minutes (288 calls/day), so
the quota was exhausted every morning by ~04:13 UTC. Both ingestion paths
then failed for the rest of the day -- the poll raised on every run, and
webhook transcript fetches 429'd through all 3 retries and were dropped.
A meeting missed that way is never recovered on its own: the poll window is
only POLL_INTERVAL + 10 minutes, so by the next quota reset the meeting has
long scrolled out of range.

This script replays those meetings through Sara's existing /process/<id>
endpoint, which runs Phase 1 (extract intelligence -> email the organizer a
review link).

Safety / correctness notes:
  - Sara's last Phase-1 notification email went out 2026-08-06T05:09:20Z.
    Zero notifications were sent after that, so every Fireflies transcript
    dated after the cutoff is by definition unprocessed. That is why no
    fuzzy title matching against the processed store is needed.
  - process_transcript_phase1 adds a transcript to the processed store only
    AFTER notify_organizer returns, so "in processed" implies "notified".
    Replaying a post-cutoff transcript therefore cannot duplicate an email.
  - Every Fireflies call costs one unit of the shared ~50/day quota. The
    live poller now needs ~24/day, so this script defaults to a small
    per-run budget and is safe to run across several days. Already-replayed
    ids are remembered locally so re-runs never repeat work.

Usage (Fireflies key comes from the Railway env):
    railway run python recover_missed_meetings.py --dry-run
    railway run python recover_missed_meetings.py --limit 15

ASCII-only per project convention.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

FIREFLIES_GRAPHQL_URL = "https://api.fireflies.ai/graphql"
APP_BASE_URL = os.environ.get(
    "RECOVER_APP_BASE_URL",
    "https://meeting-pipeline-production.up.railway.app",
)

# Last Phase-1 notification Sara actually sent (verified in Ken's mailbox).
# Everything after this is unprocessed.
DEFAULT_CUTOFF = "2026-08-06T05:09:20+00:00"

STATE_FILE = os.environ.get(
    "RECOVER_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".recover_state.json"),
)


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"replayed": [], "failed": []}
    data.setdefault("replayed", [])
    data.setdefault("failed", [])
    return data


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fireflies_transcripts(api_key: str) -> list:
    """Fetch transcript metadata only -- no sentences, no summary.

    Deliberately lean: the heavy fields are what make the poller's query
    expensive, and recovery only needs ids and dates.
    """
    query = """
    query { transcripts { id title date duration organizer_email } }
    """
    resp = requests.post(
        FIREFLIES_GRAPHQL_URL,
        json={"query": query},
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        errs = payload["errors"]
        codes = {e.get("code") or e.get("extensions", {}).get("code") for e in errs}
        if "too_many_requests" in codes:
            retry = None
            for e in errs:
                retry = (e.get("extensions", {}).get("metadata") or {}).get("retryAfter")
                if retry:
                    break
            when = ""
            if retry:
                when = datetime.fromtimestamp(retry / 1000, tz=timezone.utc).isoformat()
            raise SystemExit(
                f"Fireflies daily quota is exhausted. Retry after {when or 'the next 00:00 UTC reset'}."
            )
        raise SystemExit(f"Fireflies API error: {errs}")
    return payload.get("data", {}).get("transcripts") or []


def parse_ts(raw) -> datetime:
    """Fireflies 'date' is a Unix ms timestamp, sometimes an ISO string."""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 1e12 else raw, tz=timezone.utc)
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay meetings missed during the Fireflies quota outage.")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                    help="Only replay meetings dated after this ISO timestamp.")
    ap.add_argument("--limit", type=int, default=15,
                    help="Max meetings to replay this run (protects the shared daily quota).")
    ap.add_argument("--delay", type=float, default=20.0,
                    help="Seconds to wait between replays so Phase 1 finishes serially.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be replayed; make no /process calls.")
    args = ap.parse_args()

    api_key = os.environ.get("FIREFLIES_API_KEY", "")
    if not api_key:
        print("FIREFLIES_API_KEY is not set. Run under 'railway run' so the Railway env is injected.")
        return 2

    cutoff = datetime.fromisoformat(args.cutoff)
    state = load_state()
    already = set(state["replayed"])

    print("Listing Fireflies transcripts (1 quota unit)...")
    transcripts = fireflies_transcripts(api_key)
    print(f"Fireflies returned {len(transcripts)} transcripts total.")

    # Settle the duration-units question while we have a live response:
    # app.py:431 treats 'duration' as seconds, fireflies_client.py:57 treats
    # it as minutes. Only one can be right, and the poll window depends on it.
    sample = [t for t in transcripts if t.get("duration")][:5]
    if sample:
        print("\nduration field sample (for units check):")
        for t in sample:
            print(f"  {str(t.get('title'))[:45]:45} duration={t.get('duration')}")
        print("  -> values near 30-90 mean MINUTES; near 1800-5400 mean SECONDS.\n")

    candidates = []
    for t in transcripts:
        tid = t.get("id")
        if not tid or tid in already:
            continue
        try:
            when = parse_ts(t.get("date", ""))
        except (ValueError, TypeError, OSError):
            continue
        if when > cutoff:
            candidates.append((when, tid, t.get("title", "?")))

    candidates.sort()
    print(f"{len(candidates)} unprocessed meetings after {cutoff.isoformat()}")
    if not candidates:
        print("Nothing to recover.")
        return 0

    batch = candidates[: args.limit]
    remaining = len(candidates) - len(batch)
    for when, tid, title in batch:
        print(f"  {when.isoformat()}  {tid}  {str(title)[:60]}")
    if remaining:
        print(f"\n{remaining} more not attempted this run (per-run limit {args.limit}).")
        print("Re-run tomorrow after the 00:00 UTC quota reset to continue.")

    if args.dry_run:
        print("\nDRY RUN -- no /process calls made, state not modified.")
        return 0

    print(f"\nReplaying {len(batch)} meetings via {APP_BASE_URL}/process/<id> ...")
    ok = 0
    for i, (when, tid, title) in enumerate(batch, 1):
        try:
            r = requests.post(f"{APP_BASE_URL}/process/{tid}", timeout=60)
            if r.status_code == 200:
                ok += 1
                state["replayed"].append(tid)
                print(f"  [{i}/{len(batch)}] queued {tid} ({str(title)[:45]})")
            else:
                state["failed"].append({"id": tid, "status": r.status_code})
                print(f"  [{i}/{len(batch)}] FAILED {tid} -- HTTP {r.status_code}")
        except Exception as e:
            state["failed"].append({"id": tid, "error": str(e)})
            print(f"  [{i}/{len(batch)}] ERROR {tid} -- {e}")
        # Persist after each replay: a crash must not re-send delivered mail.
        save_state(state)
        if i < len(batch):
            time.sleep(args.delay)

    print(f"\nDone: {ok}/{len(batch)} queued. Phase 1 runs in the background;")
    print("each recovered meeting emails its organizer a review link.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
