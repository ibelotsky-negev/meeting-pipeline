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
| `/corrections` | List active standing corrections (`?all=true` incl. inactive) |
| `/corrections/ingest` | Scan Sara's mailbox now for reply-corrections |
| `/corrections/add` | Add a standing correction (`?text=`) |
| `/corrections/delete` | Deactivate a correction (`?id=`) |

## Architecture Notes

- **Phase 1 (auto):** Fireflies/Teams triggers -> Claude extracts intelligence -> notification email sent with review link
- **Phase 2 (manual):** Organizer opens review page -> edits tasks/email -> clicks Approve -> HubSpot + Asana + Outlook actions created
- **Weekly Pulse:** Sunday 22:00 IST, scans all team emails/Teams/meetings, 4-pass Claude analysis, Green/Yellow/Red report emailed to Ken
- **Biweekly Business Update:** every other Monday 07:15 IST, distills the trailing ~2 weeks of pulse archives into a team-facing, technical-detail-free business update emailed to Ken to forward (`biweekly_business_update.py`; weekly Monday cron gated to every other week by `should_run_biweekly`)
- **Standing Corrections:** Ken replies to a pulse/biweekly email with a correction; `sara_corrections.py` ingests it from Sara's mailbox (scheduled every 20 min) and injects it as an authoritative override into both the pulse synthesis and the biweekly distill. Baseline correction (Ariadne fundraising structure) is always applied. Store: `/data/sara_corrections.json`
- **Pending approvals** stored in `/data/pending_approvals.json` (Railway persistent volume at `/data/`)
- **Pulse archives** stored in `/data/pulse/{YYYY}-W{WW}.json`
- **Microsoft Graph** uses app-only auth (team-wide), refresh token persisted at `/data/refresh_token.txt`
- **Teams transcripts** via Graph webhook subscription, renewed every 50 min via APScheduler
- **To-Do sync** polls Asana tasks and creates matching Microsoft To-Do tasks for @negevlabs.com users

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
- Daily scheduling: wire into APScheduler only AFTER backfill validation

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

## Environment Variables (Railway)

Key vars (do not log values): `FIREFLIES_API_KEY`, `CLAUDE_API_KEY`, `HUBSPOT_API_KEY`, `ASANA_API_KEY`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET`, `MS_GRAPH_TENANT_ID`, `HUBSPOT_OWNER_MAP`, `BOT_SENDER_EMAIL=sara@negevlabs.com`, `ASANA_PROJECT_GID=1213263339592202`, `ASANA_WORKSPACE_GID=597593980065511`

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
