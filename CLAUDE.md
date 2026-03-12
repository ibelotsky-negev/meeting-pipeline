# CLAUDE.md — Post-Meeting Intelligence Pipeline

## Project Overview

Single-file Flask app (`app.py`) that processes Fireflies meeting transcripts through Claude, queues extracted actions for human review, then executes approved items across HubSpot, Asana, Outlook, and Microsoft To-Do. Deployed on Railway.

**Stack:** Python 3.12 / Flask 3.1 / Gunicorn / APScheduler / Anthropic SDK
**Architecture:** Monolithic `app.py` (~2300 lines), JSON file persistence, human-in-the-loop approval

## Deploy Rules

### How to deploy
```powershell
.\deploy.ps1
```
The script: copies `app.py` to repo, extracts version from code, commits, pushes, polls `/version` for up to 4 minutes, then runs `/test` dry-run.

### Version format
Version lives inline in `app.py` at the `/version` endpoint:
```python
return jsonify({"version": "X.Y.Z-description", "deployed": "YYYY-MM-DD"})
```
- Semantic: `MAJOR.MINOR.PATCH-description` (e.g. `2.7.6-fetch-completed`)
- Bump PATCH for fixes, MINOR for features, MAJOR for breaking changes
- Description tag should be 2-3 word summary of what changed

### Commit format
```
deploy: {version} [{YYYYMMDDHHmmss}]
```
Only `app.py` and `CACHEBUST` are committed per deploy.

### Post-deploy verification
- `/version` — must return the new version string
- `/test` — dry-run integration test (Fireflies fetch, Claude extraction, Graph API); must return `"status": "pass"`
- `/health` — liveness check
- `/config` — diagnostic (auth mode, API key presence, no secrets)

### Railway config
- Base URL: `https://meeting-pipeline-production.up.railway.app`
- Port: 8080 (Gunicorn, 2 workers, 120s timeout)
- Persistent volume at `/data/` for JSON state files
- Environment variables configured in Railway dashboard (never committed)

## Code Patterns

### File organization
`app.py` is sectioned with `# ======` comment blocks:
1. Imports + config loading (~1-100)
2. JSON persistence helpers (~130-160)
3. Fireflies API wrappers (~160-220)
4. Claude extraction (~220-344)
5. Microsoft Graph token management (~346-400)
6. Outlook draft creation (~400-500)
7. HubSpot API wrappers (~500-630)
8. Asana API + To-Do sync (~690-900+)
9. Notification engine (~1000-1200)
10. Phase 1 & Phase 2 execution (~1200-1350)
11. Jinja2 HTML templates (~1350-1600)
12. Flask routes (~1600-2100)
13. Scheduler init (~2237-2280)

### Naming conventions
- **Functions:** `snake_case` — e.g. `fireflies_query()`, `hubspot_request()`
- **Private/internal:** underscore prefix — e.g. `_do_webhook_process()`, `_graph_request_with_retry()`
- **Constants:** `UPPER_CASE` — e.g. `FIREFLIES_API_KEY`, `REVIEW_TEMPLATE`
- **Log prefixes:** bracket tags — `[todo-sync]`, `[webhook]`, `[deploy]`

### API wrapper pattern
Each external API has a wrapper function that handles auth, headers, retries, and error logging:
```python
def hubspot_request(method, endpoint, **kwargs):
    # base URL, auth header, error handling, response parsing
```
Always use the wrapper — never call `requests.get/post` directly for external APIs.

### State persistence
Three JSON files in `DATA_DIR` (defaults to `/data/` on Railway):
- `processed_transcripts.json` — deduplication tracker
- `pending_approvals.json` — review queue
- `asana_todo_map.json` — bidirectional sync mappings

Load/save via `load_pending()`, `save_pending()`, `load_processed()`, `load_sync_map()`, `save_sync_map()`.

### Claude model
Currently using `claude-sonnet-4-20250514` with 8000 max tokens. Business context from `business_context.md` is injected into the system prompt.

### Error handling
- Defensive try/except throughout with `logging.error()` + traceback
- Graph API calls use `_graph_request_with_retry()` with exponential backoff
- Webhook endpoints return 200 even on internal errors (to prevent retry storms)
- `strip_emojis()` on all text before sending to Outlook (encoding issues)

### Auth modes (Microsoft Graph)
- **App-only:** client credentials flow, sends from shared mailbox
- **Delegated:** refresh token flow, sends as authenticated user
- Auto-detect: `MS_GRAPH_REFRESH_TOKEN` present → delegated, else → app-only
- Token cache: `_ms_token_cache` / `_ms_delegated_token_cache` (in-memory with expiry)

## Environment Variables

### Required
```
FIREFLIES_API_KEY        # Fireflies API
CLAUDE_API_KEY           # Anthropic API
HUBSPOT_API_KEY          # HubSpot CRM
ASANA_API_KEY            # Asana project management
MS_GRAPH_CLIENT_ID       # Azure app registration
MS_GRAPH_CLIENT_SECRET   # Azure app secret
MS_GRAPH_TENANT_ID       # Azure AD tenant
```

### Optional
```
MS_GRAPH_REFRESH_TOKEN   # Enables delegated auth mode
MS_GRAPH_AUTH_MODE       # "auto" | "delegated" | "app"
NOTIFY_VIA               # "email" | "teams" | "slack" (comma-separated)
TEAMS_WEBHOOK_URL        # Teams incoming webhook
SLACK_WEBHOOK_URL        # Slack incoming webhook
BOT_SENDER_EMAIL         # Shared mailbox address
BOT_SENDER_NAME          # Bot display name
HUBSPOT_OWNER_ID         # Default fallback owner
HUBSPOT_OWNER_MAP        # JSON or "email:id,email:id" for owner routing
POLL_INTERVAL_MINUTES    # Fireflies polling interval (default: 5)
TODO_POLL_INTERVAL       # To-Do sync interval in seconds (default: 300)
TODO_LIST_NAME           # Microsoft To-Do list name
APP_BASE_URL             # Deployed URL for review links
DATA_DIR                 # Persistent storage path (default: /data)
BUSINESS_CONTEXT_FILE    # Path to business_context.md
```

## Pipeline Flow

```
Fireflies webhook → fetch transcript → Claude extracts actions
    → save to pending_approvals.json → notify organizer with review link
    → organizer edits/approves → create HubSpot contacts/tasks + Asana tasks
    → log meeting in HubSpot → create Outlook draft → mark executed
```

## Key Routes

| Route | Purpose |
|---|---|
| `POST /webhook/fireflies` | Fireflies auto-trigger (Phase 1) |
| `GET /review/<id>` | Human review UI |
| `POST /review/<id>/approve` | Execute approved actions (Phase 2) |
| `GET /test` | Dry-run integration test |
| `POST /sync/setup` | One-time To-Do + Asana webhook setup |
| `POST /sync/full` | Force full Asana ↔ To-Do re-sync |

## Do NOT

- Split `app.py` into multiple modules (deliberate monolith)
- Commit `.env` files or secrets
- Add ORM or database — JSON persistence is intentional
- Add a test framework — `/test` endpoint is the test suite
- Change the deploy script's version extraction regex without updating `app.py` format
- Return non-200 from webhook endpoints (causes retry storms)
- Call external APIs without using the existing wrapper functions
