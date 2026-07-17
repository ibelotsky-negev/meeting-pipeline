---
description: Fresh-research sweep on any topic across web news, Reddit, and X (Twitter), filtered to the last 30 days (window override in the argument).
argument-hint: <topic> [, last N days]
allowed-tools: WebSearch, WebFetch, Task, Bash
---

# Last 30 Days -- fresh-signal research

You are running the **"Last 30 Days"** research workflow. The goal is to make
the user an instant domain expert on a topic using only **fresh** signals --
recent web news, Reddit threads, and X (Twitter) chatter -- and to ruthlessly
filter out stale content.

## Topic

`$ARGUMENTS`

If no topic was given, ask the user for one and stop.

## Window

Default window is the **last 30 days** relative to today's date. If the
argument names a different window (e.g. "last 90 days", "past week", "since
May"), use that instead and state the window you used.

## Method

Work in phases. Prefer running the independent searches in parallel (multiple
tool calls in one turn).

1. **Scope.** Restate the topic and the exact date window (compute the cutoff
   date from today). List the 3-6 angles you will search (news, funding/deals,
   product/launches, hiring/people, controversy/risk, competitor moves) --
   pick the angles that fit the topic.

2. **Aggregate signals (parallel).** For each angle, run `WebSearch` queries
   that force recency. Techniques:
   - Add explicit recency to the query: the year, the month, "last 30 days",
     "this week", or an explicit `after:YYYY-MM-DD` cutoff.
   - Hit each platform deliberately:
     - **Web / news:** general queries + `site:` for relevant outlets.
     - **Reddit:** `site:reddit.com <topic>` (and specific subreddits if known).
     - **X / Twitter (PRIMARY -- true timeline depth):** call the Grok
       `x_search` helper for real X-timeline access (not web-scoped snippets):

       ```bash
       python x_search_cli.py "<topic>" --days <N>
       ```

       This reads X directly via Grok (`XAI_API_KEY`), date-scoped to the
       window, and returns dated posts + handles + a signal summary + citations.
       If it errors (e.g. `XAI_API_KEY not set` or an xAI outage), report the X
       section as unavailable and FALL BACK to web-scoped X search
       (`site:x.com OR site:twitter.com <topic>`) -- never fabricate posts.
   - `WebFetch` the most promising 3-8 web/news/Reddit results to read the
     actual content, not just snippets.

3. **Freshness filter (mandatory).** Discard anything you cannot confirm falls
   inside the window. For each kept item, note its **publication/post date**.
   If a source has no verifiable date, treat it as stale and drop it (or flag
   it explicitly as undated). Do not let an evergreen/older article pad the
   result -- freshness is the whole point.

4. **Verify.** Cross-check any surprising or high-stakes claim against a second
   independent source before reporting it as fact. Separate confirmed facts
   from rumor/speculation (common on X/Reddit).

   For a heavy topic, you may delegate parallel angle-searches to `Task`
   subagents (Explore/general-purpose) and synthesize their findings -- but you
   still own the freshness filter and verification.

## Output

Produce a tight briefing (skimmable, dated, cited):

- **TL;DR** -- 3-5 bullets: what someone needs to know from the last N days.
- **What's new (dated).** Chronological or by-theme; every item carries its
  date and a source link. Mark `[confirmed]` vs `[rumor/unverified]`.
- **Signals by platform.** What web/news vs Reddit vs X are each saying, and
  where they diverge (sentiment, what the crowd cares about). For X, use the
  `x_search` helper output (dated posts + handles), not just web snippets.
- **So what.** 2-4 bullets of implications / trends / what to watch next.
- **Gaps.** What you could NOT find fresh coverage on (say so plainly; never
  fabricate to fill a gap).

Rules:
- Never present undated or out-of-window material as current.
- Cite every non-obvious claim with a link.
- If searches come back thin, report that honestly rather than padding with
  stale or generic background.
