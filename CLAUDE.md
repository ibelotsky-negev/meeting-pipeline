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

Negev Labs -> Palomar Labs rename (2026-08-06): the team also sends/organizes from `ken@`, `shlomi@`, `dan@`, `kostia@palomar-labs.com` now. Canonical identity everywhere downstream (HubSpot owner map, Asana, TEAM_MEMBER_NAMES) stays the `@negevlabs.com` addresses above -- `EMAIL_ALIAS_MAP` in [config.py](config.py) resolves the new addresses to them.

Internal domains: `negevlabs.com`, `negevcap.com`, `ariadnebio.com`, `adres.bio`, `zirmania.onmicrosoft.com`, `palomar-labs.com` (must match INTERNAL_DOMAINS env var on Railway)

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
| `/fireflies/status` | Fireflies quota state + parked-transcript queue (read-only, makes no API call) |
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
| `/learn/stt-replay` | Manual X-video STT replay (`?dry_run=&sync=&send_email=`); reads `/data/learn_pending_stt.json` |
| `/fyi/run` | Manual FYI Triage run; DRY by default (`?days=N&live=1&backlog=&force=&sync=&email=`) |
| `/fyi/status` | Last FYI Triage run outcome (scanned/important/moved + per-message decisions) + heartbeat + `fyi_live_env` |
| `/transcribe-email/run` | Email-to-transcript: scan Sara's inbox for team mail with x.com/YouTube links, reply with transcript+summary (`?dry_run=&sync=&limit=`) |
| `/transcribe-email/status` | Last x-transcribe-email scan outcome (scanned/replied + per-message links) |

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
- **X-transcribe-email:** any internal teammate emails Sara (`sara@palomar-labs.com`) an x.com/twitter.com or youtube.com/youtu.be link to a single post/video (a podcast episode link is detected too and reported unsupported; a container URL -- channel, profile, playlist, show -- is not a request at all); a 15-min inbox scan transcribes each video (YouTube captions first, falling back to yt-dlp + Grok STT; X always via yt-dlp + Grok STT), summarizes with Claude, and REPLIES in-thread (Graph `createReply`) with a structured summary in the body + the full transcript as a `.md` attachment per link. A question asked alongside the link, or a link-free follow-up reply in an already-transcribed conversation, is answered from the cached transcript (`x_transcribe_email.py`). See the x-transcribe-email Module section.

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
- Endpoints: `/learn/run` (`?dry_run=&backlog=&force=&limit=`), `/learn/status`,
  `/learn/stt-replay` (`?dry_run=&sync=&send_email=`). `backlog=1` is the manual
  "reprocess everything" switch (ignores the processed-ids store AND the recency
  window); default/cron runs process items received within the trailing
  `LEARN_LOOKBACK_DAYS` window (default 14) **regardless of read/unread state**,
  skipping already-seen ids. (Was unread-only through 2.22.x -- saved items are
  forwarded-to-self and arrive READ, so the unread filter silently skipped the
  whole queue and produced empty digests; dedup is the processed-id store +
  move-to-Processed, not the read flag. Fixed @2.23.0.)
- **X-video capture/replay (two-step):** weekly digest **captures** untranscribable
  X videos to `/data/learn_pending_stt.json` during `resolve_x` (Grok x_search gives
  visual summary only; `needs_stt` flag). **Replay** runs AUTOMATICALLY after the
  weekly digest (`learn_weekly_run` chains `run_stt_replay` @2.25.0 -- drains this
  week's captures plus any stranded from prior weeks; no-op on an empty queue) and is
  ALSO available manually via `/learn/stt-replay` (`?sync=1` runs inline + returns per-
  entry outcomes; a `dry_run` list-only call REWRITES the status file, clobbering the
  last live result). Replay: yt-dlp + ffmpeg extracts audio from the X post URL, Grok
  STT (`POST api.x.ai/v1/stt` multipart file upload, `XAI_API_KEY`) transcribes,
  success emails a supplementary transcript digest. Idempotent pending store;
  3-attempt cap then `failed`. Status at `/data/learn_stt_status.json`. Requires
  `yt-dlp` + `ffmpeg` in the container (Dockerfile). LIMIT (not a bug): Grok's
  `VIDEO_WITH_AUDIO` detection over-fires -- it flags posts as video-with-audio that
  have no NATIVE downloadable video (yt-dlp: "No video could be found in this tweet"
  / "No video formats found") or exceed the 60-min `LEARN_STT_MAX_DURATION_SEC` cap.
  Since 2.26.0 `resolve_x` gates `needs_stt` behind a cheap yt-dlp metadata probe
  (`_probe_x_native_video`, `LEARN_STT_PROBE_TIMEOUT` default 45s): only a post with
  a fetchable native clip within the cap is queued for STT; a post with no native
  video surfaces Grok's summary (needs_stt False, reason "not natively downloadable")
  instead of being stranded. A queued clip that still fails cycles to `failed`. Since @2.25.0 the digest no longer
  DISCARDS the Grok visual/text summary for such posts -- `summarize_item` keeps it,
  prefixed `[x-video audio pending STT replay]`, instead of "content not retrieved"
  (gated on a new `content_retrieved` flag, honored by `render_digest_html`).
- X content via the Grok Agent Tools API (`x_search`, `XAI_API_KEY`) -- NO X
  bearer token. Optional resolver keys (XAI_API_KEY, SPOKEN_API_KEY, JINA_API_KEY)
  are read at CALL TIME inside resolvers, never at module scope; an absent key
  degrades that item to "content not retrieved" and never fabricates.
- **Asana output** -> "Read/Learn Triage" project `1215897524719950`. Two task
  paths (2.26.0): (a) `has_action` keepers -> a topic section **deterministically** via
  `route_section()` -- a FIRST-MATCH-WINS keyword table (`_ROUTING_RULES`) keyed
  off the cluster topic + keeper subject/title/summary, with NO extra LLM call
  (the LLM `bucket` tag is now informational only). Order: Health -> Negev Labs
  (biotech/life-sci investing) -> Zirmania Family Office (general/non-biotech
  investing, incl. financial deal-analysis / investment-workflow automation
  tooling by default) -> Travel Relay -> Ariadne Website -> Sara Pipeline ->
  General/Reference (default). Biotech-vs-general tie-breaker IS the ordering
  (Negev tested before Zirmania); a biotech deal-analysis tool still lands Negev,
  and only a meeting-pipeline-parallel build goes to Sara. Sara Pipeline = convention (b): only items paralleling
  the Sara meeting-pipeline (OpenClaw/post-meeting/transcript/CoS builds + self-
  referential Sara infra); general Claude tooling (Cowork/Obsidian/PKM) falls
  through to General/Reference. `route_section()` itself NEVER targets the manual
  sections (Untitled, Video to watch, Drop -- Not important). (b) Since 2.26.0 an
  actionless video keeper that is important -- `_is_watchable_video`: type x/youtube
  AND priority High/Medium -- is filed as an explicit "Watch: ..." task in the
  **Video to watch** section (GID `1215909647377755`, hardcoded, verified live
  2026-07-19) so a clip worth Ken's time is never left in the digest email only;
  Low-priority / non-video keepers with no action create no task. This is the ONLY
  automated path into a manual section.
- **Priority** custom field set at creation: the Opus curate output emits a
  per-keeper High/Med/Low (rubric in the prompt); single-item/fallback keepers
  default Medium (Low if content-not-retrieved). A DETERMINISTIC floor
  (`_apply_priority_floor` -> `_is_financial_workflow_tool`, run in
  `curate_cluster`) then overrides any lower verdict to **High** for financial
  deal-analysis / investment-workflow automation tooling (valuation/DCF/LBO
  modeling, DD/deal-data automation, Bloomberg/FactSet/PitchBook-class agents) --
  Zirmania core + Ken's own builds; it OUTRANKS the generic "AI tooling = Medium"
  default. Discriminator = finance-domain signal (word-boundary regex, so
  "valuation" != "evaluation") AND a tooling signal both present; generic
  dev/coding tooling stays Medium. Field GID **1199941453034656** (High `...657`
  / Med `...658` / Low `...659`). GUARD: never use the duplicate workspace
  Priority field `1206810235510187`.
- **Currency check** (`currency_check`, fast-moving keepers) assesses TWO
  INDEPENDENT axes -- (1) superseded? (2) is the SPECIFIC claim verifiable? -- and
  keeps them separate: an unverifiable sub-claim (e.g. a named repo cannot be
  web-confirmed) is an informational caveat ONLY; it never frames the item as
  superseded/fabricated and never downgrades priority. Relevance is curation's
  call, not the currency check's.

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
  a source). Moved messages are flagged UNREAD so they resurface in "2: FYI" at their original received date; nothing else is deleted or modified; idempotent. State files on
  `/data`: `fyi_processed.json`, `fyi_lock.json`, `fyi_status.json`.

## x-transcribe-email Module

Standalone module (`x_transcribe_email.py`) -- lets any internal teammate get an X or YouTube
video transcript, and ask follow-up questions about it, by emailing Sara a link. Imported
lazily by app.py (route + cron handlers only), never at module load. Reuses deployed
machinery, no duplication: `email_pipeline_sync` Graph helpers (app-only token, `graph_get` /
`graph_post` / `graph_patch` / `graph_delete`, `html_to_text`); `learn_digest`
`extract_x_post_audio` + `_grok_stt_from_file` (the 2.22.0 STT path) + `_fetch_youtube_transcript`
+ `_call_claude_text` + `SUMMARY_MODEL`; `config.is_internal_email` (the team allow-list).

- **Trigger:** any inbox message from an INTERNAL sender (or `XTE_TEAM_EXTRA`) carrying an
  x.com/twitter.com or youtube.com/youtu.be link to a single ITEM. A **container** URL --
  one naming a channel, profile, playlist or show rather than one item -- is NOT a request
  and yields no entry at all, exactly like an article URL (`_is_container_url`): an X path
  naming no item (profiles, the bare domain, nav pages), a YouTube URL with neither a
  parseable video id nor a `/clip/<id>` (`@handle`, `/c/`, `/user/`, `/channel/`,
  `/playlist?list=`), and a podcast `/show/` or `/playlist/` page. That REVERSES the original
  "any X link earns an honest no-video reply" rule -- ordinary mail and follow-up questions
  pass through this same gate, Sara's inbox is SHARED with `sara_corrections.py`, and a
  container URL can never be cached, so a signature link dropped the question and sent an
  unsolicited reply. ITEM is deliberately wider than "post or watch id": `/status/<id>`,
  `/i/spaces/<id>` and `/i/broadcasts/<id>` are X items (`_x_item_id`), a YouTube
  `/clip/<id>` is an item (`_youtube_clip_id` -- a clip URL carries no watch id, so its id is
  namespaced `youtube:clip:` in `_link_key` and `clip_` in the attachment name), and a
  podcast `/episode/` is an item: all reach the resolvers and produce a transcript or an
  honest failure, never silence. The podcast container regex is Spotify path-shaped, so a
  SHOW page on Apple / anchor.fm / pod.link / overcast / castbox / podbean is still detected
  as an item and answered "not supported yet" -- accepted deliberately (a per-host regex
  would rot). Links are read from `uniqueBody` ONLY, so
  a link quoted in a reply's thread history never re-fires. An ITEM link with no downloadable
  video still earns an honest "no video found" reply. A reply in a conversation Sara already
  transcribed is treated as a follow-up question unless it adds a **transcribable** (`x` /
  `youtube`) link that conversation has NOT already transcribed (`_has_new_link`, compared on
  the normalized `_link_key` so `youtu.be/ID` matches a cached `youtube.com/watch?v=ID`) --
  `uniqueBody` strips quoted history but KEEPS the sender's SIGNATURE, so a signature link
  would otherwise hijack the follow-up path: the question dropped, an unsolicited failure
  reply sent (see State). The container rule covers the common signature link (channel /
  profile / Spotify show); `_has_new_link` covers the rest -- a signature link to a real ITEM
  this conversation already transcribed, and ANY podcast link (a podcast never transcribes,
  so it can never be cached; letting it count would drop every follow-up in that conversation
  forever). With no cached conversation the follow-up branch is not taken at all, so a
  podcast-only email still gets its honest unsupported reply.
- **Flow:** per link, `transcribe_link` dispatches by kind -- YouTube tries captions first
  (`_fetch_youtube_transcript`, fast and free) and falls back to the same yt-dlp + Grok STT
  path as X only when there are none, but a YouTube URL naming no single video (channel /
  playlist / `@handle`) is refused up front and NEVER reaches yt-dlp (a channel has no
  duration for the cap to bound, and the download thread is a daemon that outlives its join)
  -- defense in depth, since detection now drops those before the scan gets here. A
  `/clip/<id>` is NOT refused: yt-dlp can fetch a clip, so it skips straight to the audio path
  (captions are keyed by watch id, which a clip URL does not carry, so that lookup returns
  None). Every YouTube WATCH id in this module comes from ONE local resolver, `_youtube_id`:
  it delegates to the shared `ld._youtube_video_id` and additionally recognizes `/live/<id>`
  (the address-bar form for livestreams and premieres) and the legacy `/v/<id>`, which that
  regex does not cover; a clip id comes from `_youtube_clip_id` and is kept separate so it can
  never collide with a watch id.
  `learn_digest` is shared with the Read/Learn digest and is deliberately NOT widened;
  podcast returns `PODCAST_UNSUPPORTED` without calling
  any resolver -- then a Claude summary (TITLE/TL;DR/KEY POINTS/NOTABLE QUOTES) goes out via
  `send_threaded_reply` with the transcript(s) attached as `.md` (the attachment header records
  the real source, "YouTube captions" or "xAI Grok STT"). A note asked alongside the link
  (`extract_note`) is answered first on an `ANSWER:` line grounded only in the transcript, when
  the model judges it is actually a question -- no heuristic decides this. A later link-free
  reply in that conversation is answered from the cached transcript (`answer_question`, one
  Claude call, no re-transcription) unless the model judges the note is not a question either
  (see Safety). A link that cannot be transcribed is reported honestly in the reply (specific
  reason), never faked.
- **Safety:** Sara's own outbound is skipped (loop guard); external senders are ignored (and
  marked processed so they are not reconsidered); per-email link cap `XTE_MAX_LINKS` (default
  5). Replies go out via Graph `createReply` (never `sendMail`) so the reply inherits
  `conversationId` -- the match the follow-up path depends on. Two loop breakers guard BOTH
  paths: `is_auto_reply` skips a message carrying an autoresponder header (`Auto-Submitted`,
  `X-Autoreply`/`X-Autorespond`/`X-Autoresponder`, or
  `Precedence: bulk/auto_reply/auto-reply/junk`) -- checked on the LINK path too, where such a
  message is marked processed so it is not re-evaluated every scan, since an autoresponder
  whose template carries a media link would otherwise bypass both breakers -- and
  `XTE_THREAD_MAX_QUESTIONS` (default 20) caps follow-ups per conversation -- at the cap Sara
  goes silent on purpose, since a "limit reached" reply would itself feed the loop. A follow-up
  the model judges is not actually a question (returns the `NO_QUESTION` marker -- a thank-you,
  acknowledgement, or forwarded boilerplate) gets no reply either, but the conversation's
  `questions` counter still increments, so the same cap bounds Claude spend as well as replies.
  Idempotent via processed message-ids at `/data/x_transcribe_email.json`; a reply-send failure
  is NOT marked processed so it retries. That retry rule cuts both ways, so NOTHING after the
  send may raise: the `remember_thread` cache write is wrapped, persisted counters are read
  through `_as_int`, and ids are persisted after EACH reply (`_persist_processed`), not only at
  the end of the scan -- otherwise one post-send exception, or a restart mid-scan, re-sends a
  delivered reply every 15 minutes. Status at `/data/x_transcribe_email_status.json`.
- **State:** per-conversation transcript cache at `/data/x_transcribe_threads.json`, keyed on
  `conversationId`, written by `remember_thread` only when at least one link in the message
  transcribed successfully (nothing cached means nothing to answer a follow-up from). Evicted
  by `XTE_THREAD_TTL_DAYS` (default 30), then capped at `XTE_THREAD_MAX` (default 200)
  newest-by-`updated_at` entries.
- **Scheduler:** every 15 min (`XTE_INTERVAL_MINUTES`) via APScheduler (`x_transcribe_email_run`),
  sharing `_xte_trigger_lock` with the manual route so a scheduled scan never overlaps
  `/transcribe-email/run`. CLI: `python x_transcribe_email.py [--dry-run] [--limit N]`.

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
| Team stops getting ANY meeting update after Zoom/Fireflies calls; logs show `Polling error: ... too_many_requests` on every poll and `Webhook thread: transcript not found after 3 attempts` | Fireflies enforces a hard **~50 requests/day** quota (resets 00:00 UTC, shared workspace-wide with anything else on the same key, including the Fireflies MCP). At `POLL_INTERVAL_MINUTES=5` the poller alone wanted 288 calls/day, so the quota died daily by ~04:13 UTC -- before the Israeli workday. After that BOTH ingestion paths failed at once: the poll raised every run, and the webhook's 3 retries (15/30/45s) could not outlast a quota resetting at midnight, so the transcript was DROPPED. The miss was permanent -- the poll window is only `POLL_INTERVAL + 10` min, so by the next reset the meeting had scrolled out of range. Broke 2026-08-06 (emails stopped 05:09Z, ~35 min before 2.27.0 went live), diagnosed 2026-08-19: 195/245 polls 429'd, 0 Phase-1 runs, 0 notification emails | Fixed @2.29.0 on three levels. (1) `POLL_INTERVAL_MINUTES=60` on Railway -- 24 calls/day, leaving headroom for webhook fetches. (2) [fireflies_client.py](fireflies_client.py) now raises `FirefliesQuotaExceeded` on the 429, honors the machine-readable `extensions.metadata.retryAfter`, and **short-circuits every call until the reset** (persisted at `/data/fireflies_quota.json`, so a restart cannot re-burn). (3) A transcript that cannot be fetched is PARKED at `/data/fireflies_deferred.json` and retried by `drain_deferred_transcripts` on the next poll, so a meeting arriving in a dead window is no longer lost. For a historical backlog use `recover_missed_meetings.py` |
| Meeting never got a Phase-1 email (no webhook log line for its transcript id) | Poll's fallback window used to gate on meeting START time only ([fireflies_client.py](fireflies_client.py) `get_recent_transcripts`); a meeting longer than the window (default 15 min) had its start timestamp scroll out of the cutoff before the transcript was ready, and if the webhook also missed it the meeting was silently never processed | Fixed @2.27.0 -- window now gates on end time (start + duration). If a meeting still slips through, replay it manually via `/process/<transcript_id>` |
| X-video STT replay fails immediately | `ffmpeg` missing in container or `XAI_API_KEY` unset | Dockerfile installs ffmpeg; STT uses `XAI_API_KEY` (not `SPOKEN_API_KEY`) |
| Every YouTube link fails: log shows `youtube transcript failed <vid>: no element found: line 1, column 0`, then yt-dlp `HTTP Error 403` | `youtube-transcript-api` 0.6.2 broke against YouTube's current response format (empty body -> XML parse error), so the captions path returned nothing and everything fell through to yt-dlp | Fixed @2.28.1 -- pin raised to `1.2.4` and `_fetch_youtube_transcript` rewritten for the 1.x API. **The API changed incompatibly at 1.0**: static `get_transcript` -> instance `fetch`, dict chunks -> snippet objects with `.text`. A bare version bump without the call-site change fails SILENTLY (AttributeError swallowed -> None). The resolver now handles both generations |
| A non-English YouTube video fails with `NoTranscriptFound` even though it HAS captions | `api.fetch(vid)` defaults to ENGLISH and raises when the only track is another language -- so a Russian-captioned video was indistinguishable from one with no captions and fell through to yt-dlp for nothing | Fixed @2.28.2 -- `_fetch_transcript_any_language` lists the tracks and takes a manually-created one if present, else whatever exists, in the ORIGINAL language (the summarizer reads any language; translating first loses fidelity). Verified live: a ru-only video that returned 0 chars now returns 122k |
| YouTube video with NO captions still fails in prod (`403 Forbidden`) | yt-dlp needs a JS runtime for YouTube signature deciphering ("No supported JavaScript runtime could be found", "YouTube is forcing SABR streaming"); the container has none | KNOWN, not fixed. Captioned videos (the large majority, in ANY language since 2.28.2) work via `youtube-transcript-api` and never touch yt-dlp. To also cover uncaptioned ones, add `deno` to the Dockerfile. X is unaffected -- these are YouTube-specific extraction requirements |
| A long YouTube video fails with `duration NNNNs exceeds cap (3600s)` | Only the yt-dlp/STT path is duration-capped (`LEARN_STT_MAX_DURATION_SEC`); reaching it at all means the captions path found nothing | Captions have NO duration cap -- a 2h video transcribes fine when it has any caption track. Since 2.28.3 a TRANSIENT captions failure no longer surfaces as this message (see the row below); if you still see it, the video genuinely has no captions and the real limit is the STT cap |
| A captioned YouTube video fails, then the same link works minutes later | YouTube rate-limits / blocks the datacenter IP for a window. Pre-2.28.3 that was indistinguishable from "no captions": the caller fell through to yt-dlp and reported ITS error, usually `duration exceeds cap` -- which sent a real user chasing an imaginary hour limit | Fixed @2.28.3 -- `fetch_youtube_transcript` returns `(text, transient_error)`, retries only TRANSIENT errors (`RequestBlocked` / `IpBlocked` / `YouTubeRequestFailed` / `YouTubeDataUnparsable` / network) `LEARN_YT_ATTEMPTS` times with `LEARN_YT_RETRY_WAIT` backoff, and the transient reason OVERRIDES the downstream yt-dlp error so the reply names the real cause. Permanent errors (captions disabled, video unavailable, bad id) still fail fast, unretried. If it recurs often, `youtube-transcript-api` supports a proxy natively |
| Read/Learn digest silently empty ("no unread items") | Saved items are forwarded-to-self and arrive READ; pre-2.23.0 runs were unread-only and skipped them | Fixed @2.23.0: normal runs use a trailing `LEARN_LOOKBACK_DAYS` window, read/unread agnostic. For older-than-window backlog use `/learn/run?backlog=1` |
| Read/Learn X-video shows "content not retrieved" / STT never arrives | Post has no NATIVE downloadable video (Grok VIDEO_WITH_AUDIO over-fired), or video > 60-min cap; yt-dlp can't fetch it | Expected for those posts -- @2.25.0 the digest now surfaces Grok's visual/text summary (prefixed `[x-video audio pending STT replay]`) instead of discarding it; unfetchable entries cycle to `failed` after 3 attempts. Only genuinely-short native X clips transcribe |
| Email-to-transcript never replies | Sender not on an internal domain, or no x.com/YouTube ITEM link in the new (unquoted) body -- a container URL (channel / profile / playlist / Spotify show) is not a request | Send from an `INTERNAL_DOMAINS` address with a link to one post, Space, broadcast, video or clip in the body (not just quoted); an ITEM link with no video still gets a "no video found" reply. Check `/transcribe-email/status` |
| Follow-up question gets no reply | The reply landed in a different Exchange conversation, or the first run's transcripts were never cached (every link failed) | Replies must be sent via `createReply` so `conversationId` is inherited; check `/data/x_transcribe_threads.json` for the conversation |
| Spotify link replies "not supported yet" | By design -- podcast audio is DRM'd, yt-dlp cannot fetch it and `_fetch_spoken` has no key and an unvalidated request shape | Expected. Deferred work, not a bug |
| Sara keeps replying to an autoresponder | Autoresponder sends no `Auto-Submitted`/`Precedence` header | `is_auto_reply` guards BOTH paths (a media link in the autoresponder's template no longer bypasses it), and the per-conversation cap `XTE_THREAD_MAX_QUESTIONS` stops a header-less one after 20 and goes silent; lower it if needed |
| Follow-up question gets a transcription reply instead of an answer | A link in the sender's signature made the message look like a new request -- `uniqueBody` strips quoted history but KEEPS signatures | Two guards. A CONTAINER link (channel / profile / Spotify show) is not a request at all, so a signature carrying one is inert; and no podcast link of any host ever counts as a new request. What still reads as new is a signature link to a real TRANSCRIBABLE ITEM (X post / Space / broadcast, YouTube video or clip) Sara has never transcribed -- transcribe it once, or drop it from the signature |
| Channel / profile / playlist / Spotify-show link gets no reply at all | By design -- a container URL is not a transcription request (`_is_container_url`), so the message is left for other handlers exactly like an article link | Send the link to ONE post or video. Reversed the old "any X link earns an honest no-video reply" rule: ordinary mail and follow-ups share this gate, and a container can never be cached, so it could never self-heal. NOT containers (these do get a reply): `/i/spaces/<id>`, `/i/broadcasts/<id>`, `youtube.com/clip/<id>`. A podcast `/episode/` or a show page on a non-Spotify host is ALSO detected and answered -- but ONLY in a conversation Sara has not already cached: once this conversation has a cached transcript, a podcast link no longer counts as a request at all (`_has_new_link`), so a podcast-only message gets no reply, and a podcast link plus words answers the PREVIOUSLY transcribed video, never mentioning that podcasts are unsupported |

## Environment Variables (Railway)

Key vars (do not log values): `FIREFLIES_API_KEY`, `CLAUDE_API_KEY`, `HUBSPOT_API_KEY`, `ASANA_API_KEY`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`, `MS_GRAPH_TENANT_ID`, `HUBSPOT_OWNER_MAP`, `BOT_SENDER_EMAIL=sara@palomar-labs.com`, `ASANA_PROJECT_GID=1213263339592202`, `ASANA_WORKSPACE_GID=597593980065511`

Fireflies polling: `POLL_INTERVAL_MINUTES` (default 5 in code, **set to 60 on Railway**). This is a quota-critical value, not a latency knob -- Fireflies allows ~50 API requests/day workspace-wide and each poll costs one, plus one per new transcript fetched. Never lower it without recounting the daily budget (webhook fetches and any Fireflies MCP use draw on the same 50). State files: `/data/fireflies_quota.json` (the "quota spent until X" note that gates every call) and `/data/fireflies_deferred.json` (transcript ids parked for retry). Both are safe to delete -- worst case is one wasted request or a re-fetch.

Read/Learn: optional `LEARN_LOOKBACK_DAYS` (trailing window for normal/cron runs, default 14; read/unread agnostic), `LEARN_CONCURRENCY`, `LEARN_CURRENCY_CHECK`, `LEARN_YT_ATTEMPTS` (YouTube caption attempts on a TRANSIENT error, default 3) / `LEARN_YT_RETRY_WAIT` (backoff seconds, default 2), resolver keys `XAI_API_KEY` / `SPOKEN_API_KEY` / `JINA_API_KEY` (read at call time; absent = degrade, never fabricate).

FYI Triage: `FYI_LIVE` (set to `1` to arm real moves -- the second of the two gates; UNSET at ship = dry), `FYI_LOOKBACK_HOURS` (cron window, default 24), `FYI_RECIPIENTS` (summary email, default bk@negevlabs.com), optional `FYI_CLASSIFIER_MODEL` / `FYI_MAX_DAYS` / `FYI_MAX_PER_FOLDER` / `FYI_CONCURRENCY` / `FYI_BROADCAST_DOMAINS` (broker/ESP blast domains -> deterministic NOISE) / `FYI_HELD_DOMAINS` + `FYI_HELD_NAMES` (tracked holdings -> material IR is deterministic IMPORTANT) and `INTERNAL_DOMAINS` (own-outbound -> deterministic NOISE).

x-transcribe-email: reuses `BOT_SENDER_EMAIL` (Sara's mailbox), `XAI_API_KEY` (STT), `CLAUDE_API_KEY` (summary), `INTERNAL_DOMAINS` (sender allow-list); optional `XTE_TEAM_EXTRA` (extra served addresses beyond INTERNAL_DOMAINS, comma-separated, default empty), `XTE_INTERVAL_MINUTES` (scan cadence, default 15), `XTE_MAX_MESSAGES` (scan window, default 25), `XTE_MAX_LINKS` (per-email cap, default 5), `XTE_SUMMARY_MODEL`, `XTE_THREAD_TTL_DAYS` (follow-up transcript cache eviction, default 30), `XTE_THREAD_MAX` (cached-conversation cap, default 200), `XTE_THREAD_MAX_QUESTIONS` (per-conversation follow-up loop breaker, default 20).

## Current Known Issues

- **Fireflies `duration` units are still formally unconfirmed** (harmless since @2.29.0). The codebase used to contradict itself: [app.py](app.py) Weekly Pulse computed `round(duration / 60)` (SECONDS) while [fireflies_client.py](fireflies_client.py) added it as `timedelta(minutes=duration)` (MINUTES). Neither was ever validated against a real response -- the Pulse value only ever reached an LLM prompt, where a 60x error is invisible, and `create_hubspot_meeting` (the one place a wrong duration would have been visible) is dead code, never called. Both call sites now go through the single `duration_to_minutes` normalizer, which is correct under EITHER reading for any realistic meeting (a value too large to be plausible minutes can only be seconds, so it is converted; the result is clamped to `MAX_MEETING_MINUTES` so a mis-scaled value can never widen the poll window without bound). Confirm the real units by reading a live value (`railway run python recover_missed_meetings.py --dry-run` prints a sample) and simplify if desired -- but nothing depends on the answer any more.
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
