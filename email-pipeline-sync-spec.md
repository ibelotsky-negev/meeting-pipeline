# Sara Module: email-pipeline-sync — Specification

## PRD

**Project Name:** email-pipeline-sync (new module in `meeting-pipeline` project)

**Description:** A Sara module that scans Outlook mailboxes for email correspondence with contacts associated to deals in the NL 2026 Fundraise HubSpot pipeline, classifies whether each email relates to the contact's deal, and logs deal-relevant emails to HubSpot as email engagements — but only if they are not already logged. Compensates for HubSpot's native Outlook logging, which fires inconsistently.

**Target Audience:** Internal — Ken Belotsky (process owner), Alex Kubasov (CRM operations, primary consumer of complete deal timelines), Vadim Uzberg and Shlomi Raz (account owners whose correspondence gets captured).

**Key Problem:** HubSpot's native email logging (Outlook extension / BCC logging) does not work reliably. Deal-critical threads — e.g., the full Tetrad VC / Maxillon KYC and UPA negotiation thread spanning May 8 – June 11 — are absent from deal timelines. Alex maintains the pipeline weekly but has no reliable email history on deal records. Manual logging doesn't scale across 27 deals and 4 team members.

**Devices:** Server-side module, runs headless inside the meeting-pipeline project (Claude Code execution environment). No UI. Output is visible in HubSpot deal/contact timelines.

**Hosting Budget:** $0 incremental — runs in the existing meeting-pipeline infrastructure with existing Microsoft Graph and HubSpot credentials. LLM classification calls via existing Anthropic API key (estimated <$5/month at expected volume).

**MVP Deadline:** Backfill run (May 1, 2026 → present) within one week of module completion; ongoing scheduled runs immediately after backfill validation.

### System Architecture

| Component | What It Does | Where It Runs | Status |
|---|---|---|---|
| Pipeline Roster Builder | Pulls all deals from NL 2026 Fundraise pipeline (ID 3760999624), resolves associated contacts, builds contact→deal map with email addresses | meeting-pipeline / HubSpot API | built |
| Mailbox Scanner | Searches Outlook via Microsoft Graph for messages to/from roster email addresses within the run window | meeting-pipeline / MS Graph API | built |
| Dedupe Checker | For each candidate email, checks HubSpot for an existing email engagement with the same `internetMessageId` (fallback: subject + timestamp ±2 min + sender match) | meeting-pipeline / HubSpot Engagements API | built |
| Relevance Classifier | LLM call (Claude) classifying each email as DEAL_RELEVANT / NOT_RELEVANT / UNCERTAIN against the contact's deal context (deal name, stage, fundraise keywords) | meeting-pipeline / Anthropic API | built |
| HubSpot Logger | Creates email engagement (type EMAIL) with full headers, body text, timestamp; associates to contact + deal + company | meeting-pipeline / HubSpot API | built |
| Run Ledger | Local state file recording processed message IDs, run windows, and outcomes per message (logged / skipped-duplicate / skipped-irrelevant / flagged-uncertain) | meeting-pipeline / SQLite | built |
| Run Report | Summary posted at end of each run: N scanned, N logged, N duplicates, N irrelevant, N uncertain (with links) — emailed to Ken or appended to Sara Weekly Pulse | meeting-pipeline | built |

### Data Flow

1. Scheduled trigger (or manual backfill invocation with `--since 2026-05-01`).
2. Roster Builder queries HubSpot: deals in pipeline 3760999624 → associated contacts → email addresses. All stages included, including Closed Lost (re-engagement history is valuable; "Not Now" deals will return).
3. Mailbox Scanner queries MS Graph per roster address: messages where the address appears in from/to/cc, received within run window.
4. For each message: Run Ledger check (already processed?) → skip if yes.
5. Dedupe Checker queries HubSpot engagements on the contact: match `internetMessageId` → skip and record as duplicate if found.
6. Relevance Classifier: prompt includes deal name, investor name, pipeline context (NL 2026 Fundraise: Class B round, re-ups, KYC, UPA, wire, shareholder letter, MJFF, etc.) plus email subject/body. Returns DEAL_RELEVANT / NOT_RELEVANT / UNCERTAIN.
7. DEAL_RELEVANT → HubSpot Logger creates engagement, associates contact + deal + company. UNCERTAIN → flagged in Run Report for human review, not logged. NOT_RELEVANT → recorded in ledger, not logged.
8. Run Report generated and delivered.

### Internal Team Identification

Emails are in scope when a roster contact appears on the thread AND at least one internal sender/recipient is present: bk@negevlabs.com, bk@negevcap.com, ak@negevcap.com, vu@negevcap.com, shlomi@negevlabs.com, shlomi@ariadnebio.com. Internal-only emails (no roster contact) are out of scope.

### Evaluated and Rejected Approaches

| Approach | Why Considered | Why Rejected |
|---|---|---|
| Rely on HubSpot native Outlook logging | Zero build effort; already deployed | Demonstrated unreliable — fires inconsistently; entire Tetrad thread missing. The module exists because of this gap. |
| HubSpot Workflows / native automation | No-code; lives inside HubSpot | HubSpot workflows cannot read external mailboxes; they only act on data already in HubSpot. |
| Forward-to-BCC address discipline (team manually BCCs HubSpot) | Standard HubSpot pattern | Human-dependent; team already doesn't do it consistently. Vadim works from gmail aliases; enforcement unrealistic. |
| Log ALL emails with roster contacts (no relevance filter) | Simpler; no LLM cost | Roster contacts are long-term relationships — emails include personal topics, other ventures (e.g., s16vc threads with Vadim), unrelated business. Logging everything pollutes deal timelines and creates privacy exposure in CRM. |
| Keyword-only relevance filter (no LLM) | Cheaper, deterministic | Fundraise emails span Russian and English, legal jargon, KYC minutiae; keyword lists would miss or over-match. LLM classification is cheap at this volume. |

### Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| MS Graph requires delegated access per mailbox; shlomi@ariadnebio.com is a separate tenant | Module cannot run against a mailbox until access is granted; ariadnebio.com may need its own app registration or shared-mailbox arrangement | Access grants are a pre-build task (Ken/Alex to arrange). Module degrades gracefully: scans whatever mailboxes are accessible and reports which were skipped. Emails existing only in Vadim's gmail (vuzberg@gmail.com) remain invisible — accepted; deal threads run through vu@negevcap.com. |
| HubSpot engagement search by internetMessageId is not a first-class filter | Dedupe requires fetching contact's email engagements and matching client-side | Acceptable at this scale (27 deals, low engagement volume per contact). Ledger prevents re-processing. |
| Native HubSpot logging may log the same email AFTER the module does | Potential duplicates created by HubSpot, not the module | Module records what it logged; periodic dedupe report flags doubles. Low frequency expected given native logging unreliability. |
| LLM misclassification (false negative) | Deal email not logged | UNCERTAIN bucket + Run Report gives human review path; classifier prompt tuned on backfill results before ongoing runs. |
| WhatsApp/Telegram messages are out of scope | Significant relationship comms not captured | Accepted — known constraint of the entire CRM (per existing ops practice, these channels are not email-searchable). |

---

## Requirements

### MVP (required for the first version)

- [x] **Pipeline roster builder** — resolve all contacts + emails associated with NL 2026 Fundraise deals via HubSpot API. Foundation for everything else.
- [x] **Mailbox scanner (all team mailboxes)** — Graph search per roster address within run window across: bk@negevlabs.com, bk@negevcap.com, ak@negevcap.com, vu@negevcap.com, shlomi@negevlabs.com, shlomi@ariadnebio.com. *Prerequisite: Mail.Read application-permission access for ak@, vu@, and both shlomi@ mailboxes; shlomi@ariadnebio.com is a separate tenant — confirm access path before first full run.*
- [x] **Ledger-based idempotency** — never process the same internetMessageId twice across runs.
- [x] **HubSpot dedupe check** — skip emails already logged as engagements on the contact.
- [x] **LLM relevance classification** — three-way output (RELEVANT / NOT_RELEVANT / UNCERTAIN) with deal context in prompt.
- [x] **HubSpot email engagement logging** — full email (headers, body, timestamp) associated to contact + deal + company.
- [x] **Backfill mode** — `--since 2026-05-01` flag; processes history in date-ordered batches with rate-limit handling.
- [x] **Run report** — counts + UNCERTAIN list, delivered to Ken.

### Nice-to-have (add later)

- [ ] **Attachment logging** — attach files (UPAs, KYC docs) to the engagement. *Dependency: HubSpot files API quota.*
- [ ] **Thread-level grouping** — log one engagement per thread with consolidated body instead of per-message.
- [ ] **Auto-note on stage signals** — when classifier detects a stage-relevant event ("wire sent", "docs signed"), create a draft note for Alex to review.
- [ ] **Weekly Pulse integration** — append run summary to Sara's existing Weekly Pulse email.
- [ ] **Coverage of future pipelines** — parameterize pipeline ID to reuse for Ariadne Series A (`EMAIL_SYNC_PIPELINE_ID` env var already supported).
- [ ] **Scheduled daily runs** — wire into app.py APScheduler after backfill validation.

---

## Tech Stack

| Component | Technology | Why this technology for THIS project | Free? | AI codes it well? |
|---|---|---|---|---|
| Runtime | Python in meeting-pipeline project (Claude Code) | Sara modules already live here; shares credentials, scheduler, and logging conventions. New language/runtime would fragment the project. | Yes — existing infra | Yes — excellent |
| Email access | Microsoft Graph API (Mail.Read, app-only) | bk@ mailboxes are Exchange Online; Graph is the only sanctioned API. Already authenticated in the M365 integration used by the team. App-only (client_credentials) is required for multi-mailbox access. | Yes — included in M365 tenant | Yes — well-documented |
| CRM read/write | HubSpot CRM v3 API + Engagements API | The target system. Engagements API is the only way to create email records with custom timestamps (needed for backfill). Pipeline/deal/contact IDs already known (pipeline 3760999624). | Yes — included in existing HubSpot subscription | Yes — excellent |
| Relevance classification | Anthropic API (claude-haiku for cost, claude-sonnet if accuracy insufficient) | Bilingual (RU/EN) deal correspondence with legal jargon; needs semantic judgment, not keywords. Haiku at this volume is pennies. | No — but <$5/month at ~hundreds of emails | Yes — native |
| State / ledger | SQLite (single file, `/data/` volume on Railway or project dir locally) | Needs queryable idempotency state across runs; JSON file gets unwieldy with per-message records. No server DB justified for one consumer. | Yes | Yes — excellent |
| Scheduler | Existing meeting-pipeline cron/scheduler | Module slots into Sara's existing run cadence; daily run after backfill. No new scheduling infra. | Yes | Yes |

### What's NOT in the stack

- **HubSpot native Outlook logging** — the unreliability of this feature is the reason the module exists. The module treats it as a possible duplicate source, not a dependency.
- **Gmail API** — vuzberg@gmail.com is out of scope for MVP; Ken's Exchange mailbox catches CC'd threads. Revisit only if coverage gaps appear in backfill results.
- **Zapier / Make / n8n** — middleware adds a subscription and an external failure point for logic that is ~300 lines of Python in an environment we already run.
- **Vector DB / embeddings** — classification is per-email with deal context in the prompt; no retrieval needed at this scale.
- **Web UI** — output surface is HubSpot itself plus a run report email. A dashboard nobody asked for is scope creep.

### Confirmed Decisions

1. **Mailbox scope (MVP):** All four team members — bk@negevlabs.com, bk@negevcap.com, ak@negevcap.com, vu@negevcap.com, shlomi@negevlabs.com, shlomi@ariadnebio.com. Delegated access grants are a pre-build task.
2. **Run cadence after backfill:** Daily.
3. **Closed Lost deals:** Included in roster — re-engagement history is part of the record.
4. **UNCERTAIN handling:** Report-only. Flagged in the Run Report for human review; never auto-logged.
