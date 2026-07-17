#!/usr/bin/env python3
"""
x_search_cli -- date-scoped X (Twitter) timeline sweep via the xAI Grok
Agent Tools API (x_search + web_search), for the /last-30-days slash command.

This is a DEV / RESEARCH helper, NOT part of the deployed Flask app. It gives
the "Last 30 Days" workflow true X-timeline depth (Grok reads X directly via
XAI_API_KEY -- no X bearer token) instead of web-scoped site:x.com searches.

It deliberately DUPLICATES the tiny xAI call/parse pattern from
learn_digest.py (`_grok_responses_call` / `_parse_grok_responses`) rather than
importing it: learn_digest pulls in email_pipeline_sync + asana_client (Graph /
HubSpot), which a slash-command helper must not drag in. The duplicated surface
is ~40 lines and matches the proven module.

Usage:
    python x_search_cli.py "Saronic Technologies" --days 90
    python x_search_cli.py "Saronic Technologies" --days 30 --max 30 --json

Env:
    XAI_API_KEY   (required)  xAI key -- read at call time; absent -> honest error
    LEARN_X_MODEL (optional)  model override (default grok-4.20-non-reasoning)

ASCII-only comments and non-user-facing strings.
Author: Negev Labs
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone, timedelta

import requests

DEFAULT_MODEL = os.environ.get("LEARN_X_MODEL", "grok-4.20-non-reasoning")
DEFAULT_DAYS = 30
DEFAULT_MAX_ITEMS = 25
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"


def default_window(days: int, now: datetime = None):
    """Return (from_date, to_date) as YYYY-MM-DD strings for a trailing window
    of `days` ending today (UTC). `now` is injectable for deterministic tests."""
    if days < 1:
        raise ValueError("days must be >= 1")
    now = now or datetime.now(timezone.utc)
    frm = now - timedelta(days=days)
    return frm.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def build_prompt(topic: str, from_date: str, to_date: str, max_items: int) -> str:
    """Build the x_search sweep prompt. The date window is enforced IN the
    prompt (the /v1/responses x_search tool is invoked without date-key config,
    matching the proven learn_digest call): Grok is told to restrict to the
    window, stamp each post's date, and drop anything it cannot date. Pure ->
    unit-testable without any network."""
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("topic must not be empty")
    return (
        f"Search X (Twitter) for posts about: {topic}\n\n"
        f"STRICT DATE WINDOW: only posts published between {from_date} and "
        f"{to_date} (inclusive). Discard anything outside this window and "
        f"anything whose date you cannot confirm.\n\n"
        f"Return up to {max_items} of the most relevant/high-signal posts. For "
        f"each, give: the post date (YYYY-MM-DD), the author handle, a faithful "
        f"1-2 sentence summary of what it says, engagement if visible, and the "
        f"canonical x.com URL. Prefer original/high-signal posts over "
        f"low-effort reposts. Then add a short SIGNAL SUMMARY: dominant themes, "
        f"notable accounts, and overall sentiment across the window.\n\n"
        f"Report only what you actually find on X. If coverage is thin, say so "
        f"plainly -- never fabricate posts, dates, or handles."
    )


def x_search(topic: str, days: int = DEFAULT_DAYS, max_items: int = DEFAULT_MAX_ITEMS,
             model: str = None, timeout: int = 120, now: datetime = None) -> dict:
    """Run a date-scoped X timeline sweep. Returns
    {topic, from_date, to_date, model, text, citations}. Raises RuntimeError on
    a missing key or an HTTP/network failure after one transient retry."""
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY not set")
    model = model or DEFAULT_MODEL
    from_date, to_date = default_window(days, now=now)
    prompt = build_prompt(topic, from_date, to_date, max_items)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model,
            "tools": [{"type": "x_search"}, {"type": "web_search"}],
            "input": [{"role": "user", "content": prompt}]}
    last = None
    for attempt in range(2):
        try:
            resp = requests.post(XAI_RESPONSES_URL, headers=headers, json=body,
                                 timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 1:
                last = f"status {resp.status_code}"
                time.sleep(3)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status in (429, 500, 502, 503, 504)
            if attempt < 1 and retryable:
                time.sleep(3)
                continue
            raise RuntimeError(last)
    else:
        raise RuntimeError(last or "unknown")
    text, citations = parse_response(data)
    return {"topic": topic, "from_date": from_date, "to_date": to_date,
            "model": model, "text": text, "citations": citations}


def parse_response(data: dict):
    """Extract assistant text + url citations from an xAI /v1/responses payload.
    Mirrors learn_digest._parse_grok_responses: the top-level output_text is
    frequently null, so walk output[] for the assistant message item only, and
    never iterate a string content as dicts. Returns (text, citations)."""
    data = data or {}
    citations = []
    txt = (data.get("output_text") or "").strip()
    for item in (data.get("output") or []):
        if (not isinstance(item, dict) or item.get("type") != "message"
                or item.get("role") != "assistant"):
            continue
        parts = []
        for c in (item.get("content") or []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "output_text" and c.get("text"):
                parts.append(c["text"])
            for a in (c.get("annotations") or []):
                if isinstance(a, dict) and a.get("type") == "url_citation" and a.get("url"):
                    citations.append(a["url"])
        if not txt and parts:
            txt = "\n".join(parts).strip()
    return txt, citations


def _format_human(result: dict) -> str:
    lines = [
        f"# X timeline sweep: {result['topic']}",
        f"# Window: {result['from_date']} -> {result['to_date']}  (model: {result['model']})",
        "",
        result.get("text") or "(no text returned)",
    ]
    cits = result.get("citations") or []
    if cits:
        lines.append("")
        lines.append("Citations:")
        lines.extend(f"- {u}" for u in cits)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Date-scoped X (Twitter) timeline sweep via xAI Grok x_search.")
    parser.add_argument("topic", nargs="+", help="Topic to search X for.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Trailing window in days (default {DEFAULT_DAYS}).")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_ITEMS,
                        dest="max_items", help="Max posts to return.")
    parser.add_argument("--model", default=None, help="xAI model override.")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout (s).")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit raw JSON instead of formatted text.")
    args = parser.parse_args(argv)
    topic = " ".join(args.topic).strip()
    try:
        result = x_search(topic, days=args.days, max_items=args.max_items,
                          model=args.model, timeout=args.timeout)
    except Exception as e:
        print(f"x_search failed: {e}", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(_format_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
