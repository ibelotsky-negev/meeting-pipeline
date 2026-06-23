# Sara Meeting Pipeline - Claude Code Guide

## Project Overview

Sara is a post-meeting intelligence pipeline for Negev Labs (biotech venture studio). It processes meeting transcripts from Fireflies/Teams, extracts intelligence via Claude API, presents a review UI, then creates tasks in HubSpot/Asana and email drafts in Outlook.

- **Repo:** `ibelotsky-negev/meeting-pipeline` (branch: `main`)
- **Hosting:** Railway at `https://meeting-pipeline-production.up.railway.app`
- **Stack:** Flask, Claude API, Fireflies, Microsoft Graph, HubSpot, Asana, APScheduler
- **Module layout:** `app.py` (~4000 lines: Flask `app:app` entrypoint, all routes, Weekly Pulse, Teams, To-Do sync, Graph auth, Phase-1/2 core, scheduler) plus extracted helper modules it re-exports -- see [Module Layout](#module-layout)

## Team

| Name | Email | HubSpot Owner ID | Notes |
|------|-------|-------------------|-------|
| Ken Belotsky | bk@negevlabs.com | 241153249 | Lead, HUBSPOT_OWNER_ID fallback |
| Shlomi Raz | shlomi@negevlabs.com | 31267643 | Co-founder |
| Dan Jeffries | dan@negevlabs.com | 31299775 | |
| Kostia Adamsky | ka@negevlabs.com | N/A | No HubSpot seat |

Internal domains: `negevlabs.com`, `negevcap.com`, `ariadnebio.com`, `adres.bio`, `zirmania.onmicrosoft.com` (must match INTERNAL_DOMAINS env var on Railway)

## Module Layout

`app.py` was split out of a single-file god-file (refactor phases 0-2, shipped @2.17.2). It is still the Flask `app:app` entrypoint and the SOLE owner of: all `@app.route` handlers, the single APScheduler instance, Weekly Pulse, Teams transcripts, To-Do sync, Graph/Outlook token functions, the Phase-1/2 core (`extract_meeting_intelligence`, `notify_organizer`, `process_transcript_phase1`, `execute_approved_actions`), and `strip_emojis`.

Extracted modules (imported and **re-exported** by app.py):

| Module | Contents |
|--------|----------|
| `config.py` | All env reads + derived constants; helpers `normalize_team_email`, `is_internal_email`, `resolve_internal_organizer`, `load_briefing_book` |
| `prompts.py` | The 7 Weekly Pulse prompt strings (`PULSE_*`) |
| `templates.py` | `REVIEW_TEMPLATE`, `RESULT_TEMPLATE` (review/result HTML) |
| `datetime_utils.py` | `to_hubspot_ms`, `to_graph_datetime`, `resolve_due_date` |
| `stores.py` | pending/processed/sync-map JSON persistence |
| `fireflies_client.py` | Fireflies GraphQL client |
| `hubspot_client.py` | HubSpot contacts/owners/meetings/tasks + `_hubspot_owner_cache` |
| `asana_client.py` | Asana tasks + user lookup |

**Re-export pattern (load-bearing).** Near the top, app.py does `from <module> import (...)  # noqa: F401`, so `app_module.X` and every existing bare reference keep resolving. Rules for agents:
- To change a moved function/constant, edit it **in its module** -- NOT in app.py (app.py only re-exports it).
- A new shared helper goes in the right module and is added to app.py's re-export line (keep the `# noqa: F401`).
- When a test mocks a moved function, patch it in the function's **home module** (e.g. `monkeypatch.setattr(hubspot_client, "hubspot_request", ...)`), not `app_module` -- otherwise the patch won't intercept the function's internal calls. Functions still in app.py are patched on `app_module` as before.
- **NOT yet extracted (stay in app.py on purpose):** Graph/Outlook token functions (`get_ms_graph_token`, `get_delegated_graph_token`, `is_app_only_mode`, `create_outlook_draft`, `_graph_request_with_retry`, `get_graph_app_only_token`) -- `get_delegated_graph_token` rotates the `global MS_GRAPH_REFRESH_TOKEN`, so they were kept together. Routes, Pulse, Teams, and To-Do sync also remain in app.py.

**CRLF warning.** `app.py` is stored CRLF (repo has `autocrlf=true`, no `.gitattributes`). Edit it preserving CRLF or `git diff` shows a full-file rewrite (the Edit/Write tools and most replacement tools default to LF). For large scripted edits, read/write in binary keeping `\r\n`; confirm a clean diff with `git diff --cached --stat`, not `git -c core.autocrlf=false diff`.

## Session Start (MANDATORY)

Step 0 of every session: `git fetch && git rebase origin/main`. Origin is the source of truth.

## Deploy Rules (CRITICAL)

### Version String
- Format: `MAJOR.MINOR.PATCH-description` (e.g., `2.9.6-owner-dropdown`)
- Must be a **string literal** in both `/version` and `/test` endpoints
- Deploy scripts extract it with regex -- do NOT assemble from variables

### Deploy Steps
```bash
# 1. Edit app.py
# 2. Verify syntax
python -c "import ast; ast.parse(open('app.py').read()); print('OK')"
# 3. Update version string in /version AND /test endpoints
# 4. Commit and push
ts=$(date +%Y%m%d%H%M%S)
echo -n "$ts" > CACHEBUST
git add app.py CACHEBUST   # + any changed module (config.py, hubspot_client.py, ...)
git commit -m "deploy: VERSION_STRING [$ts]"
git push
# 5. Poll until live (Railway takes 60-180s)
for i in $(seq 1 12); do sleep 20; curl -s https://meeting-pipeline-production.up.railway.app/version; echo; done
# 6. Run /test
curl -s https://meeting-pipeline-production.up.railway.app/test | python -m json.tool
```

### CACHEBUST File
Always update `CACHEBUST` with a timestamp on every deploy. This invalidates Docker layer caching so Railway picks up the new code (app.py or any extracted module).

### Procfile
The repo has a `Procfile` that must say:
```
web: gunicorn --bind 0.0.0.0:8080 --workers 1 --worker-class gthread --threads 4 --timeout 120 app:app
```
Railway uses nixpacks which defaults to `main:app` without this -- causing `ModuleNotFoundError`.

**CRITICAL -- never increase workers above 1.** Single-process topology is load-bearing: one APScheduler instance, atomic `O_CREAT|O_EXCL` pulse lock, Teams subscription renewal job. Increasing workers to 2+ reintroduces duplicate Pulse emails (the 2.12.7 regression). Use `--threads` within the single process for concurrency instead.

## Code Rules

### ASCII-Only Comments (MANDATORY)
Never use Unicode in comments or non-user-facing strings. Use `->` not arrow, `--` not em-dash, `"` not smart quotes. PowerShell and replacement tools corrupt Unicode silently.

### Null-Safe API Responses
```python
# WRONG
summary = data.get("summary", {})
# RIGHT -- handles both missing AND null
summary = data.get("summary") or {}
```

### Webhook Handlers
- Always log raw payloads from day 1 (never deploy logging-only versions)
- Never trust webhook `new_value` -- always fetch from API
- Add retry with backoff for webhook-triggered processing

### HubSpot Specifics
- Timestamps must be Unix milliseconds as strings (e.g., `'1772631000000'`)
- Tasks are single-owner -- create separate task per owner
- `resolve_hubspot_owner()` chain: HUBSPOT_OWNER_MAP -> API lookup -> fallback

### Task Routing
- External-facing (follow-ups, investor emails) -> HubSpot
- Internal operational (doc prep, research) -> Asana

### Email Tone
Sara drafts emails with confident, direct tone. BANNED: "Just checking in", "I just wanted to", "Sorry to bother you", "I hope this finds you well". Open with the point, active voice, clear CTAs.

## Key Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/version` | Version check (deploy verification) |
| `/config` | Non-sensitive config summary |
| `/test` | Dry-run critical path, no side effects |
| `/webhook/fireflies` | Fireflies auto-trigger |
| `/webhook/teams-transcript` | Teams transcript webhook |
| `/review/<id>` | Approval review page |
| `/review/<id>/approve` | Execute approved actions |
| `/pulse/trigger` | Manual weekly pulse (supports `?dry_run=true&days=N`) |
| `/pulse/check` | Verify Graph permissions for pulse |
| `/pulse/history` | Browse archived pulse reports |
| `/biweekly/trigger` | Manual biweekly business update (`?dry_run=&sync=&force=&start=&end=`) |
| `/biweekly/status` | Last biweekly update run outcome |
| `/digest/trigger` | Manual daily pipeline (CRM activity) digest (`?dry_run=&sync=`) |
| `/digest/status` | Last daily pipeline digest run outcome |
| `/corrections` | List active standing corrections (`?all=true` incl. inactive) |
| `/corrections/ingest` | Scan Sara's mailbox now for reply-corrections |
| `/corrections/add` | Add a standing correction (`?text=`) |
| `/corrections/delete` | Deactivate a correction (`?id=`) |
| `/learn/run` | Manual Read/Learn digest run (`?dry_run=&backlog=&force=&limit=`) |
| `/learn/status` | Last Read/Learn run outcome + live heartbeat (`live_progress`, `cluster_diag`) |
| `/fyi/run` | Manual FYI Triage run; DRY by default (`?days=N&live=1&backlog=&force=&sync=&email=`) |
| `/fyi/status` | Last FYI Triage run outcome (scanned/important/moved + per-message decisions) + heartbeat + `fyi_live_env` |

## Architecture Notes

- **Phase 1 (auto):** Fireflies/Teams triggers -> Claude extracts intelligence -> notification email sent with review link
- **Phase 2 (manual):** Organizer opens review page -> edits tasks/email -> clicks Approve -> HubSpot + Asana + Outlook actions created
- **Weekly Pulse:** Sunday 22:00 IST, scans all team emails/Teams/meetings, 4-pass Claude analysis, Green/Yellow/Red report emailed to Ken
- **Biweekly Business Update:** every other Monday 07:15 IST, distills the trailing ~2 weeks of pulse archives into a team-facing, technical-detail-free business update written in Ken's first-person voice for him to forward (`biweekly_business_update.py`; weekly Monday cron gated to every other week by `should_run_biweekly`)
- **Standing Corrections:** Ken replies to a pulse/biweekly email with a correction; `sara_corrections.py` ingests it from Sara's mailbox (scheduled every 20 min) and injects it as an authoritative override into both the pulse synthesis and the biweekly distill. Baseline correction (Ariadne fundraising structure) is always applied. Store: `/data/sara_corrections.json`
- **Pending approvals** stored in `/data/pending_approvals.json` (Railway persistent volume at `/data/`)
- **Pulse archives** stored in `/data/pulse/{YYYY}-W{WW}.json`
- **Microsoft Graph** uses app-only auth (team-wide), refresh token persisted at `/data/refresh_token.txt`
- **Teams transcripts** via Graph webhook subscription, renewed every 50 min via APScheduler
- **To-Do sync** polls Asana tasks and creates matching Microsoft To-Do tasks for @negevlabs.com users
- **Daily Pipeline Digest:** daily 06:45 IST (03:45 UTC), compiles every change + new activity in the NL 2026 Fundraise HubSpot pipeline over the trailing window (default 24h, resilient to missed runs), narrates deltas rather than state, applies Negev operating rules (stale-deal / overdue-task / wire-watch flags), and emails Ken a single morning brief (`daily_pipeline_digest.py`). See the daily-pipeline-digest Module section.
- **Read/Learn Digest:** Friday 06:00 Asia/Jerusalem, drains Ken's Outlook "read/learn" folder, resolves each saved link, Opus cluster+curate against a Ken's-needs profile, emails one HTML digest + creates Asana keeper tasks (`learn_digest.py`). See the Read/Learn Digest Module section.
- **FYI Triage:** daily 06:00 Asia/Jerusalem, scans the two high-volume auto-filed folders "4: notification" + "8: marketing", classifies each message IMPORTANT vs NOISE with Sonnet (reading the body, not just the from-address), and MOVES the important ones to "2: FYI". Dual-gated (`?live=1` AND env `FYI_LIVE=1`) -- ships DRY, auto-promotes to live once Ken sets `FYI_LIVE` (`fyi_triage.py`). See the FYI Triage Module section + ROLLOUT.md.

## email-pipeline-sync Module

Standalone module (`email_pipeline_sync.py`) -- scans team Outlook mailboxes for
correspondence with NL 2026 Fundraise pipeline contacts, classifies deal relevance
via Claude (haiku), logs relevant emails to HubSpot as email engagements. Full spec:
`email-pipeline-sync-spec.md`.

- Runs headless via CLI, NOT imported by app.py (no Flask coupling)
- `python email_pipeline_sync.py --since 2026-05-01` -- backfill; no flags -- last 3 days
- `--dry-run` -- no HubSpot writes, no report email, no ledger message rows
- Reuses existing env vars: HUBSPOT_API_KEY, CLAUDE_API_KEY, MS_GRAPH_CLIENT_ID/SECRET/TENANT_ID, BOT_SENDER_EMAIL
- Requires app-only Graph auth (Mail.Read application permission) for multi-mailbox scan; inaccessible mailboxes (e.g. shlomi@ariadnebio.com, separate tenant) are skipped and reported, not fatal
- Ledger: SQLite at `/data/email_pipeline_sync.db` (Railway volume) or project dir locally
- UNCERTAIN classifications are never auto-logged -- they go to the run report for human review
- Run report emailed to bk@negevlabs.com from BOT_SENDER_EMAIL
- Scheduled daily at 03:15 UTC via APScheduler (`email_sync_run` -> `email_pipeline_sync.run_daily`), 3-day rolling lookback; runs before the 03:45 digest so logged engagements are visible to it. Manual: `railway run python email_pipeline_sync.py`

## daily-pipeline-digest Module

Standalone module (`daily_pipeline_digest.py`) -- compiles every change and new
activity in the NL 2026 Fundraise HubSpot pipeline over the previous window
(default 24h, resilient to missed runs via a persisted last-run watermark) and
emails Ken a single readable morning brief. Covers all deal owners, narrates
deltas rather than state, links deals/activities to their HubSpot records, and
applies Negev operating rules deterministically (stale-deal, overdue-task,
wire-watch flags). Composer model: haiku. Shares credentials, HTTP helpers, and
the SQLite ledger file with `email_pipeline_sync.py` (imported as `eps`).

- Scheduler: daily **06:45 Israel (03:45 UTC)** via APScheduler. Manual:
  `/digest/trigger` (`?dry_run=&sync=`), `/digest/status` for the last-run
  outcome. CLI: `python daily_pipeline_digest.py [--since YYYY-MM-DD] [--dry-run]`.
- Degrades gracefully when HubSpot read scopes are missing (reports the gap,
  does not crash). Status persisted at `/data/daily_pipeline_digest_status.json`.
- Recipients: `DIGEST_RECIPIENTS` (default bk@negevlabs.com); optional
  `DIGEST_CC`. Pipeline: `DIGEST_PIPELINE_ID` (defaults to the shared id).

## Read/Learn Digest Module

Standalone module (`learn_digest.py`) -- weekly drains Ken's Outlook "read/learn"
folder, resolves each saved link (article -> Jina/trafilatura, YouTube ->
transcript, podcast -> spoken.md, X -> Grok x_search), runs a per-item Sonnet
summary, an Opus cluster+curate pass (best 1-2 per topic vs a Ken's-needs
profile), a web_search currency check on fast-moving AI-tooling keepers, and
emits one Friday HTML digest + Asana keeper tasks. Full spec:
`read-learn-digest-spec.md`. Imported lazily by app.py (route handlers only), not
at module load. Reuses Sara infra: Graph app-only token (via `email_pipeline_sync`),
Claude client, send-email, the Pulse-style atomic lock pattern.

- Scheduler: weekly **Friday 06:00 Asia/Jerusalem** (tz-aware cron). Atomic
  `O_CREAT|O_EXCL` lock at `/data/learn_lock.json` (2h stale-reclaim);
  processed-id dedup at `/data/learn_processed.json`. Mail moved to a read/learn
  "Processed" childfolder, never deleted.
- Endpoints: `/learn/run` (`?dry_run=&backlog=&force=&limit=`), `/learn/status`.
  `backlog=1` is the manual "reprocess everything" switch (ignores processed-ids
  AND the unread filter); default/cron runs are unread-only + skip-already-seen.
- X content via the Grok Agent Tools API (`x_search`, `XAI_API_KEY`) -- NO X
  bearer token. Optional resolver keys (XAI_API_KEY, SPOKEN_API_KEY, JINA_API_KEY)
  are read at CALL TIME inside resolvers, never at module scope; an absent key
  degrades that item to "content not retrieved" and never fabricates.
- **Asana output** -> "Read/Learn Triage" project `1215897524719950`. Keeper
  tasks (only `has_action` keepers) route to a section **deterministically** via
  `route_section()` -- a FIRST-MATCH-WINS keyword table (`_ROUTING_RULES`) keyed
  off the cluster topic + keeper subject/title/summary, with NO extra LLM call
  (the LLM `bucket` tag is now informational only). Order: Health -> Negev Labs
  (biotech/life-sci investing) -> Zirmania Family Office (general/non-biotech
  investing) -> Travel Relay -> Ariadne Website -> Sara Pipeline -> General/
  Reference (default). Biotech-vs-general tie-breaker IS the ordering (Negev
  tested before Zirmania). Sara Pipeline = convention (b): only items paralleling
  the Sara meeting-pipeline (OpenClaw/post-meeting/transcript/CoS builds + self-
  referential Sara infra); general Claude tooling (Cowork/Obsidian/PKM) falls
  through to General/Reference. The router NEVER targets the manual-only sections
  (Untitled, Video to watch, Drop -- Not important).
- **Priority** custom field set at creation: the Opus curate output emits a
  per-keeper High/Med/Low (rubric in the prompt); single-item/fallback keepers
  default Medium (Low if content-not-retrieved). Field GID **1199941453034656**
  (High `...657` / Med `...658` / Low `...659`). GUARD: never use the duplicate
  workspace Priority field `1206810235510187`.

## FYI Triage Module

Standalone module (`fyi_triage.py`) -- surfaces important mail buried in two
high-volume auto-filed Outlook folders by MOVING the important messages into
"2: FYI". Sources: **"4: notification"** (Fireflies/Humantic/Zoom/Calendly/OTP
noise) and **"8: marketing"** (newsletters/IR blasts). Imported lazily by app.py
(route + cron handlers only), never at module load. Reuses Sara infra:
`email_pipeline_sync` Graph helpers (app-only token, retrying GET/POST,
html_to_text), the Claude client, the send-email path, the Pulse-style atomic
`O_CREAT|O_EXCL` run lock + single-worker topology. Full rollout: `ROLLOUT.md`.

- **Folders resolved LIVE by display name** via Graph at run time
  (`resolve_folder_map` -> `_walk_mail_folders`, recursive, cached). IDs are NEVER
  hardcoded for addressing; `EXPECTED_FOLDER_IDS` (confirmed live 2026-06-22) is a
  cross-check only (logs a warning on mismatch, still trusts the live value). The
  run aborts rather than guess if any folder cannot be resolved, or if the
  destination resolves equal to a source.
- **Classifier:** Sonnet (`FYI_CLASSIFIER_MODEL`, classification tier -- NOT Opus),
  one call per message, reads subject + sender + body excerpt. Rubric embedded in
  `FYI_RUBRIC` (the 50-email calibration: IMPORTANT exemplars + the NOISE list +
  "named individual inside a marketing email = IMPORTANT" + precision-over-recall
  tie-breaker). Unparseable/empty/errored -> NOISE (never move on uncertainty).
- **Deterministic precision guards (pure functions, NO LLM call, run before the
  classifier; from the STATE B precision pass):** (1) mail from an internal domain
  (`FYI_INTERNAL_DOMAINS`, default = the INTERNAL_DOMAINS set) -> NOISE (own
  outbound / sent-copy / reply thread); (2) bulk broadcast / broker blast --
  `FYI_BROADCAST_DOMAINS` (ccsend/vccross/rflafferty/iangels) or an `OFFER//`
  subject -> NOISE; (3) **recency guard** drops anything received before the window
  cutoff; (4) **dedup** collapses repeated invites/reminders (key = sender +
  subject stripped of RE:/FW:/Reminder:) so only one surfaces; (5) **held-company
  IR** (FIX A, round 3): material IR (AGM / clinical readout / financing / M&A,
  gated by `FYI_MATERIAL_IR_KEYWORDS`) from a tracked holding (`FYI_HELD_DOMAINS` /
  `FYI_HELD_NAMES`: Solvonis, Xylo Bio, Filament Health, Reset Pharma, PharmAla,
  BlaBlaCar, Estateguru, ...) -> IMPORTANT even from info@/no-reply. The nuanced
  1:1-vs-broadcast and automated-urgency-bait calls stay with the model, and so
  does the syndicate-platform allocation invite (FIX B): a SPECIFIC named-round
  invite is IMPORTANT even via an ESP/no-reply (Concentric Series C), but the ESP
  domain alone never auto-NOISEs and generic platform marketing stays NOISE. STATE
  D (round 5) sharpened this across LANGUAGE / RECIPIENTS / SENDER and BOTH theses:
  a specific allocation/round/secondary offer (named company + round + confirm-
  interest ask) is IMPORTANT even when non-English, sent to a "Dear investors"
  distribution list, or from an external VC -- spanning biotech AND the Zirmania
  general-tech thesis (e.g. a Russian "Replit. Series D"); only generic VC
  marketing/newsletters (any language) stay NOISE. The discriminator is "specific
  company + allocation + action ask", never the language/recipients/sender, and it
  stays with the model -- NO hardcoded sender.
- **Dual gate (load-bearing):** a real move requires BOTH `?live=1` AND env
  `FYI_LIVE=1`. Absent either, the run is DRY (classify + log would-move, write no
  ids, move nothing). The daily cron passes `live=True`, so it ships DRY while
  `FYI_LIVE` is unset and auto-promotes to live the moment Ken sets `FYI_LIVE=1`.
- **Windows (parameter, never fixed):** cron = 24h (`FYI_LOOKBACK_HOURS`); 7-day
  calibration dry-run = `/fyi/run?days=7`; 30-day backfill = `/fyi/run?days=30&live=1`.
  A window >30d (`FYI_MAX_DAYS`) must be passed explicitly; never scans the full
  ~13k backlog. Per-folder fetch cap `FYI_MAX_PER_FOLDER` (logged when hit, not silent).
- **Dedup:** processed-id store `/data/fyi_processed.json`, keyed by message id.
  LIVE runs record every confidently-classified id (moved + noise); DRY runs write
  nothing (so a later backfill still sees everything). `backlog=1` ignores the store.
- **Safety:** only ever moves FROM the two named sources TO "2: FYI"
  (`_assert_safe_move` guards every move: dest must be the resolved FYI id and not
  a source). Never deletes/modifies anything else; idempotent. State files on
  `/data`: `fyi_processed.json`, `fyi_lock.json`, `fyi_status.json`.

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Build succeeds but old code runs | Docker layer caching | Commit CACHEBUST file |
| ModuleNotFoundError: 'main' | Nixpacks default | Ensure Procfile has `app:app` |
| 502 on all endpoints | App crash on startup | Check Railway deploy logs |
| HubSpot task wrong owner | organizer_email used instead of owner_email | Use task-level owner_email |
| Webhook handler misses state change | Trusting new_value from payload | Fetch from API instead |
| /test returns 502 timeout | Sequential API calls too slow | Known issue, use /version + /config |
| /pulse/check shows false | Missing Azure AD permissions | Ken must grant + admin consent |
| Read/Learn task Priority blank/wrong | Duplicate Asana Priority field used | Use field 1199941453034656, NOT 1206810235510187 |
| Daily digest owner names blank / 403 | Missing HubSpot crm.objects.owners.read scope | Grant + reconnect HubSpot (granted 2026-06-14) |
| FYI Triage classifies but never moves | Dual gate not fully open | Set BOTH `?live=1` AND env `FYI_LIVE=1`; absent either it stays DRY by design |
| FYI Triage run aborts "could not resolve folder" | A source/dest folder was renamed | Folders are matched by display name -- restore "2: FYI" / "4: notification" / "8: marketing" or update the names in `fyi_triage.py` |

## Environment Variables (Railway)

Key vars (do not log values): `FIREFLIES_API_KEY`, `CLAUDE_API_KEY`, `HUBSPOT_API_KEY`, `ASANA_API_KEY`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`, `MS_GRAPH_TENANT_ID`, `HUBSPOT_OWNER_MAP`, `BOT_SENDER_EMAIL=sara@negevlabs.com`, `ASANA_PROJECT_GID=1213263339592202`, `ASANA_WORKSPACE_GID=597593980065511`

FYI Triage: `FYI_LIVE` (set to `1` to arm real moves -- the second of the two gates; UNSET at ship = dry), `FYI_LOOKBACK_HOURS` (cron window, default 24), `FYI_RECIPIENTS` (summary email, default bk@negevlabs.com), optional `FYI_CLASSIFIER_MODEL` / `FYI_MAX_DAYS` / `FYI_MAX_PER_FOLDER` / `FYI_CONCURRENCY` / `FYI_BROADCAST_DOMAINS` (broker/ESP blast domains -> deterministic NOISE) / `FYI_HELD_DOMAINS` + `FYI_HELD_NAMES` (tracked holdings -> material IR is deterministic IMPORTANT) and `INTERNAL_DOMAINS` (own-outbound -> deterministic NOISE).

## Current Known Issues

- Shlomi's HUBSPOT_OWNER_MAP entry had wrong ID (was `241153250`, fixed to `31267643`) -- verify in Railway env vars
- Teams live transcription requires separate enablement from Teams recording (MP4 to OneDrive != callTranscript object)
- Weekly Pulse requires Azure AD permissions: `Mail.Read`, `Chat.Read.All`, `ChannelMessage.Read.All` (must be granted + admin consented before `/pulse/trigger` will work)

## Loop protocol
1. Write the change. 2. Checks run automatically on stop (syntax, ruff, pytest).
3. On failure, read the error, fix the root cause. 4. Max 5 hook-enforced retries; same error twice in a row = invoke @fixer.
- Never weaken, skip, or delete a test to pass it. Fix the code.
- ASCII-only in comments and non-user-facing strings.
- Version string lives in exactly 2 places in app.py (/version and /test). Bump both on any deployed code change (app.py or an extracted module).
- Passing local checks does NOT mean deployed. Deploy = commit changed code (app.py and/or modules) + fresh-timestamp CACHEBUST, push to main, poll /version every 20s up to 4 min. Never confirm via /test.
- Poll loops must exit on terminal status and carry a max-iteration cap -- never poll unconditionally.
- Tests are offline-only. Never add a test that calls a live API.

## Test-with-code mandate
Tests are required for SIGNIFICANTLY NEW functionality only -- not for every edit.

Tests REQUIRED when adding or changing:
- New functions with real logic: parsing, routing, date/time math, locks, auth, dedupe, retry/error handling
- New endpoints or webhook handlers (minimum: success path + one failure path)
- Behavior changes that existing tests do not already cover

Tests NOT required for:
- Log lines, comments, docstrings, user-facing copy, prompt text
- Config/env plumbing, constant tweaks, version bumps
- Small refactors fully covered by the existing suite (a green hook is the proof)

When required, tests ship in the same commit as the code. If unsure whether a change is significant, write the test. Never weaken, skip, or delete an existing test to pass it (unchanged rule).

## CLAUDE.md self-maintenance (MANDATORY)

CLAUDE.md is the shared, checked-in map of this project -- keep it true. When a
change alters how the system is built, run, deployed, or reasoned about, update
CLAUDE.md in the SAME commit as the change (same discipline as the
Test-with-code mandate). A stale guide is worse than none.

Update REQUIRED when you:
- Add / remove / rename a module, endpoint, webhook, or scheduled job
- Change architecture, data flow, a deploy/build step, or the single-worker topology
- Add / rename an env var, persistent file path, external service, or a key ID a
  future session needs
- Hit a non-obvious gotcha worth a row in Common Failure Modes
- Change a documented behavior, owner ID, domain list, or routing/priority rule

Update NOT needed for:
- Version bumps, CACHEBUST, dependency bumps
- Bug fixes already covered by existing docs/tests
- One-off data operations, manual runs, or transient investigations (these live
  in git history / the run record, not the guide)
- Log lines, comments, user-facing copy tweaks

Rules:
- Map, not changelog (git log is the changelog); pointers, not a code mirror
  (link to the file/symbol instead of copying logic).
- If a change makes an existing line wrong, FIX or DELETE that line -- do not
  just append.
- ASCII-only; preserve CRLF (this file is CRLF, autocrlf=true).
