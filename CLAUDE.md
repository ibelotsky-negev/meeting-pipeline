# Sara Meeting Pipeline - Claude Code Guide

## Project Overview

Sara is a post-meeting intelligence pipeline for Negev Labs (biotech venture studio). It processes meeting transcripts from Fireflies/Teams, extracts intelligence via Claude API, presents a review UI, then creates tasks in HubSpot/Asana and email drafts in Outlook.

- **Repo:** `ibelotsky-negev/meeting-pipeline` (branch: `main`)
- **Hosting:** Railway at `https://meeting-pipeline-production.up.railway.app`
- **Stack:** Flask, Claude API, Fireflies, Microsoft Graph, HubSpot, Asana, APScheduler
- **Single-file app:** Everything is in `app.py` (~2800 lines)

## Team

| Name | Email | HubSpot Owner ID | Notes |
|------|-------|-------------------|-------|
| Ken Belotsky | bk@negevlabs.com | 241153249 | Lead, HUBSPOT_OWNER_ID fallback |
| Shlomi Raz | shlomi@negevlabs.com | 31267643 | Co-founder |
| Dan Jeffries | dan@negevlabs.com | 31299775 | |
| Kostia Adamsky | ka@negevlabs.com | N/A | No HubSpot seat |

Internal domains: `negevlabs.com`, `negevcap.com`, `ariadnebio.com`, `zirmania.com`

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
git add app.py CACHEBUST
git commit -m "deploy: VERSION_STRING [$ts]"
git push
# 5. Poll until live (Railway takes 60-180s)
for i in $(seq 1 12); do sleep 20; curl -s https://meeting-pipeline-production.up.railway.app/version; echo; done
# 6. Run /test
curl -s https://meeting-pipeline-production.up.railway.app/test | python -m json.tool
```

### CACHEBUST File
Always update `CACHEBUST` with a timestamp on every deploy. This invalidates Docker layer caching so Railway picks up the new `app.py`.

### Procfile
The repo has a `Procfile` that must say:
```
web: gunicorn --bind 0.0.0.0:8080 --workers 2 --timeout 120 app:app
```
Railway uses nixpacks which defaults to `main:app` without this -- causing `ModuleNotFoundError`.

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

## Architecture Notes

- **Phase 1 (auto):** Fireflies/Teams triggers -> Claude extracts intelligence -> notification email sent with review link
- **Phase 2 (manual):** Organizer opens review page -> edits tasks/email -> clicks Approve -> HubSpot + Asana + Outlook actions created
- **Weekly Pulse:** Sunday 22:00 IST, scans all team emails/Teams/meetings, 4-pass Claude analysis, Green/Yellow/Red report emailed to Ken
- **Pending approvals** stored in `/data/pending_approvals.json` (Railway persistent volume at `/data/`)
- **Pulse archives** stored in `/data/pulse/{YYYY}-W{WW}.json`
- **Microsoft Graph** uses app-only auth (team-wide), refresh token persisted at `/data/refresh_token.txt`
- **Teams transcripts** via Graph webhook subscription, renewed every 50 min via APScheduler
- **To-Do sync** polls Asana tasks and creates matching Microsoft To-Do tasks for @negevlabs.com users

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
- Version string lives in exactly 2 places in app.py (/version and /test). Bump both on any app.py change.
- Passing local checks does NOT mean deployed. Deploy = commit app.py + fresh-timestamp CACHEBUST, push to main, poll /version every 20s up to 4 min. Never confirm via /test.
- Tests are offline-only. Never add a test that calls a live API.
