# Post-Meeting Intelligence Pipeline v2

**Fireflies → Claude AI → Organizer Approval → HubSpot + Asana + Outlook Draft**

Automatically processes every meeting with human-in-the-loop approval before any actions are created.

---

## What's New in v2

| Feature | Description |
|---------|-------------|
| **Outlook Draft** | Follow-up email created as a draft in the organizer's Outlook (via Microsoft Graph) — with correct recipients and sender |
| **Approval Workflow** | Organizer gets a review link → edits task text, changes owners, deletes tasks, edits email → then approves |
| **Notification** | Organizer notified via **email**, **Teams**, and/or **Slack** with a one-click review link |

---

## Architecture

```
┌─────────────┐     ┌───────────────┐     ┌────────────────┐
│  Fireflies   │────▶│  Pipeline     │────▶│  Claude API    │
│  (webhook)   │     │  Server       │     │  (extraction)  │
└─────────────┘     └───────┬───────┘     └────────────────┘
                            │
                    ┌───────▼───────┐
                    │  PHASE 1       │
                    │  Extract +     │
                    │  Queue         │
                    └───────┬───────┘
                            │  Notify organizer (email / Teams / Slack)
                    ┌───────▼───────┐
                    │  REVIEW UI     │  ◀── Organizer edits / deletes / approves
                    │  /review/:id   │
                    └───────┬───────┘
                            │  On approve:
                    ┌───────▼───────┐
                    │  PHASE 2       │
                    │  Execute       │
                    └───────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         HubSpot        Asana        Outlook
         (contact +     (tasks)      (draft email
          notes +                    in organizer's
          tasks)                     mailbox)
```

---

## Flow Step-by-Step

| Step | What Happens |
|------|-------------|
| 1 | Meeting ends → Fireflies sends webhook (or pipeline polls) |
| 2 | Pipeline fetches full transcript from Fireflies |
| 3 | Claude extracts: contacts, signals, action items, follow-up email draft |
| 4 | Pipeline saves extraction and sends organizer a **review link** (via email, Teams, and/or Slack) |
| 5 | Organizer opens review page in browser |
| 6 | Organizer can **edit task text**, **change owner/priority**, **delete tasks**, **edit email subject/body**, or **skip email entirely** |
| 7 | Organizer clicks **"Approve & Create"** |
| 8 | Pipeline creates: HubSpot contact + notes + tasks, Asana tasks, Outlook draft |
| 9 | Organizer opens Outlook → draft is ready → review and hit Send |

---

## Setup Guide

### All API Keys Needed

| Service | Where | Permissions |
|---------|-------|-------------|
| **Fireflies** | Settings → API & Webhooks | API Key |
| **Anthropic** | console.anthropic.com | API Key |
| **HubSpot** | Settings → Private Apps | `crm.objects.contacts.read/write`, `crm.objects.custom.read/write` |
| **Asana** | Settings → Developer Apps | Personal Access Token |
| **Microsoft Azure** | portal.azure.com → App Registrations | See below |

### Microsoft Graph / Outlook Setup

This is the most involved step. You need an Azure AD app registration to create Outlook drafts.

**Step 1: Register App**
1. Go to [portal.azure.com](https://portal.azure.com) → Azure Active Directory → App Registrations
2. Click **"New Registration"**
3. Name: `Meeting Pipeline`
4. Supported account types: **Single tenant** (your org only)
5. Redirect URI: `http://localhost:8080/auth/callback` (for initial token setup)

**Step 2: Configure Permissions**
1. Go to **API Permissions → Add a permission → Microsoft Graph**
2. Choose **Delegated permissions** (recommended — sends as the user):
   - `Mail.ReadWrite` (create drafts)
   - `Mail.Send` (send notification emails)
   - `offline_access` (refresh token)
3. Click **Grant admin consent**

**Step 3: Get Client Secret**
1. Go to **Certificates & secrets → New client secret**
2. Copy the secret value immediately (shown only once)

**Step 4: Get Refresh Token (one-time)**
Run this in your browser to get the authorization code:
```
https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize?
  client_id={CLIENT_ID}&
  response_type=code&
  redirect_uri=http://localhost:8080/auth/callback&
  scope=Mail.ReadWrite Mail.Send offline_access&
  response_mode=query
```

Exchange the code for a refresh token:
```bash
curl -X POST https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token \
  -d "client_id={CLIENT_ID}" \
  -d "client_secret={CLIENT_SECRET}" \
  -d "code={AUTH_CODE}" \
  -d "redirect_uri=http://localhost:8080/auth/callback" \
  -d "grant_type=authorization_code" \
  -d "scope=Mail.ReadWrite Mail.Send offline_access"
```

Copy the `refresh_token` from the response → set as `MS_GRAPH_REFRESH_TOKEN` in your `.env`.

**Alternative: App-Only Flow**
If you prefer app-only (no user login required):
1. Use **Application permissions** instead of Delegated
2. Requires tenant admin consent
3. Leave `MS_GRAPH_REFRESH_TOKEN` blank
4. The pipeline will use `/users/{email}/messages` endpoint

---

### Microsoft Teams Notification Setup

Two options for delivering the review link to Teams:

**Option A: Incoming Webhook (simplest — posts to a channel)**

1. In Teams, go to the channel where you want notifications
2. Click **⋯ → Manage channel → Connectors (or Workflows)**
3. Find **Incoming Webhook** → Configure
4. Name it `Meeting Pipeline`, upload an icon if you want
5. Copy the webhook URL → set as `TEAMS_WEBHOOK_URL` in `.env`

This posts an **Adaptive Card** to the channel with a "Review & Approve" button.

**Option B: Graph API 1:1 Chat (sends DM to organizer)**

More powerful — sends a direct Teams chat message to the meeting organizer specifically.

1. Add these Graph API permissions to your Azure app registration:
   - `Chat.ReadWrite` (delegated) — to create/access 1:1 chats
   - `ChatMessage.Send` (delegated) — to send messages in chats
2. Re-consent permissions (re-run the OAuth flow from the Outlook setup)
3. Set `TEAMS_NOTIFY_VIA_GRAPH=true` in `.env`

**Combine channels:** Set `NOTIFY_VIA=email,teams` to send both an Outlook email AND a Teams message. Any combination works: `email`, `teams`, `slack`, or `email,teams,slack`.

---

## Deployment (Railway — Recommended)

```bash
# 1. Push to GitHub
git init && git add . && git commit -m "Meeting pipeline v2"
git remote add origin YOUR_GITHUB_REPO
git push -u origin main

# 2. Deploy on Railway
npm install -g @railway/cli
railway login
railway init
railway up

# 3. Set environment variables in Railway dashboard
# (copy from .env.example and fill in your values)

# 4. Set APP_BASE_URL to your Railway URL
# e.g., https://meeting-pipeline.up.railway.app

# 5. Configure Fireflies webhook
# URL: https://meeting-pipeline.up.railway.app/webhook/fireflies
# Event: Transcription Complete
```

---

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/webhook/fireflies` | POST | Fireflies auto-trigger (Phase 1) |
| `/process/<transcript_id>` | POST | Manual trigger for specific transcript |
| `/review/<approval_id>` | GET | Organizer review page (edit/delete/approve) |
| `/review/<approval_id>/approve` | POST | Execute approved actions (Phase 2) |
| `/review/<approval_id>/cancel` | GET | Cancel — create nothing |

---

## Review UI Features

The organizer sees a clean web page with:

- **Meeting signals** — interest level, relationship type, key signals extracted by Claude
- **Editable tasks** — change task text, owner, priority, or check "Delete" to remove
- **Editable email draft** — modify subject, recipients, body, or skip entirely
- **One-click approve** — creates everything in HubSpot + Asana + Outlook
- **Cancel option** — nothing gets created

---

## File Structure

```
pipeline-v2/
├── app.py                 # Main app (all logic in single file)
├── requirements.txt       # Python dependencies
├── Dockerfile             # Container config
├── .env.example           # Environment variable template
├── processed_transcripts.json  # Auto-created: dedup tracker
├── pending_approvals.json      # Auto-created: pending review queue
└── README.md              # This file
```

---

## Customization

| What | How |
|------|-----|
| Change Claude's extraction | Edit `extract_meeting_intelligence()` prompt |
| Add Slack notifications | Set `NOTIFY_VIA=slack` + `SLACK_WEBHOOK_URL` |
| Add Teams notifications | Set `NOTIFY_VIA=teams` + `TEAMS_WEBHOOK_URL` (webhook) or `TEAMS_NOTIFY_VIA_GRAPH=true` (DM) |
| Multi-channel notify | Set `NOTIFY_VIA=email,teams,slack` to send on all channels |
| Route tasks to specific people | Map names in `execute_approved_actions()` to Asana GIDs |
| Skip certain meetings | Add filters in `poll_and_process()` (e.g., min duration, specific attendees) |
| Custom approval expiry | Add TTL check in `review_page()` using `created_at` |
| Add CC/BCC to Outlook draft | Extend `create_outlook_draft()` with `ccRecipients` field |
