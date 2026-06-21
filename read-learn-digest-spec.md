# Read/Learn Digest — Specification

> v0.2 — drafted 2026-06-21 for Claude Code Phase 1. Module of the Sara meeting-pipeline (repo `ibelotsky-negev/meeting-pipeline`, `meeting-pipeline-production.up.railway.app`). Phase 2 (X bookmarks) is scoped here but deferred. **v0.2 change:** synthesis is a cluster-and-curate pass (consolidate to the best item per topic, drop redundant/outdated), not a flat per-item digest.

## PRD

**Project Name:** Read/Learn Digest (Sara module)

**Description:** A scheduled module inside the Sara meeting-pipeline that weekly drains Ken's Outlook "read/learn" folder, resolves each saved link (X posts, articles, YouTube, podcasts, X video), then **clusters the items by topic and curates each cluster down to the single most useful, most current item for Ken's needs** — dropping redundant and superseded saves. It emits a Friday digest organized by topic cluster plus Asana tasks for the keepers. Phase 2 adds X bookmarks as a second capture source. The module reuses Sara's existing Microsoft Graph auth, `/data` volume, Claude pipeline, atomic run-lock, email path, and self-verifying-loop (SVL) test harness — it is **not** a new service.

**Target Audience:** Ken Belotsky, single user. The folder is his personal "save to read later" queue — 117 messages, 76 unread at spec time. ~80% are self-sent with a terse subject that doubles as Ken's own topic tag ("Burry", "Clinical unmet need", "Google flights api") and a body of one link + Outlook mobile signature; ~20% are newsletters (bench2bio, Psychedelic Alpha); a few are personal. The set is heavily **clustered and redundant** (e.g. ~6 Claude Code / skills items, several Cowork items, a Gut/Digestion/Health/Huberman group) and contains **outdated** ideas/tools saved months ago.

**Key Problem:** Ken captures dozens of links he intends to read — to Outlook "read/learn", and increasingly to X bookmarks — and never processes them. The queue is redundant (many near-duplicate saves on the same topic) and partly stale (tools/approaches superseded since saving). A flat summary of all 76 would not help. Ken needs the queue **consolidated to the few items genuinely worth his time**, with the best-of-each-topic surfaced and the rest visibly set aside.

**Devices:** Server-side only (Railway container). Output consumed in Outlook (Friday email) and Asana (tasks). No UI.

**Hosting Budget:** $0 infrastructure — runs inside the existing Sara container. Incremental API spend only, ~a few USD/month at Ken's volume: X post reads ~$0.005 each, X owned-reads/bookmarks ~$0.001, Grok STT $0.10/hr of video, podcast transcripts ~$0.10 each, plus per-item Claude tokens and a small `web_search` cost on fast-moving keepers.

**MVP Deadline:** One Claude Code session (Phase 1, Outlook folder, including the first-run backlog consolidation). Phase 2 (X bookmarks) a follow-up session after the X developer app is provisioned.

### System Architecture

| Component | Responsibility | Where it lives | Status |
|---|---|---|---|
| Folder fetch | Pull unread from "read/learn" by cached folder ID via Graph (app perms) | Sara `app.py` | New |
| URL extractor + classifier | Strip signature/HTML, extract + dedup links, classify x / youtube / podcast / article / x-video | Sara `app.py` | New |
| Content resolvers | Per-type fetch: X API, Jina Reader, youtube-transcript-api, Grok STT, spoken.md | Sara `app.py` | New |
| Per-item summarizer (Sonnet) | Title, source, date-if-detectable, named tools/approaches, key specifics, 2–3 sentence summary, confidence | Sara `app.py` | New |
| Cluster + curate synthesizer (Opus) | Group items into topic clusters; pick the best 1–2 per cluster for Ken's needs; mark the rest superseded/redundant with a reason | Sara `app.py` | New |
| Currency checker (Claude `web_search`) | For keepers in fast-moving AI-tooling clusters, confirm still-current; annotate "superseded by X" | Sara `app.py` (Claude tool) | New |
| Bucket tagger | Tag each keeper with 1+ of 6 relevance buckets for routing | Sara `app.py` | New |
| Digest emitter | Friday email grouped by topic cluster, "N saved → best" + skipped list; atomic lock = exactly one send | reuses Sara `send_email` + lock | New (reuses) |
| Asana router | Keepers with an action → tasks in "Read/Learn Triage", placed in matching bucket section | Asana API | New |
| Idempotency + post-process | Mark ALL processed (keepers + skipped) read, move to `/Processed` subfolder, record IDs | Sara `app.py` + `/data` | New |
| Scheduler | APScheduler weekly **Friday 06:00 Asia/Jerusalem** (tz-aware cron; DST automatic), `RUN_SCHEDULER` guard, `/learn/run` manual | reuses Sara scheduler | New (reuses) |
| Egress probe | Throwaway `/egress-check` reachability test | Sara `app.py` | New, temporary |

**Shared state (`/data`):**
- `learn_folder_id.json` — cached folder ID (seeded with the known ID below)
- `learn_processed.json` — processed message IDs (belt-and-suspenders dedup independent of mark-as-read)
- `learn_lock.json` — atomic run-lock (`O_CREAT|O_EXCL`), mirrors the Weekly Pulse lock

**Known IDs (seed these — do not look up by name):**
- Outlook folder "read/learn" ID: `AAMkAGY0Nzc0N2Q0LWU2NWYtNDFlMi05MmM3LWI5ZWIwODY5ZDA4YwAuAAAAAAD2HZAEgE0dQ6DKSpP8o42sAQBxq2Btx8bBQbRoRXyUmqLCAAlms0PtAAA=`
- Mailbox: `bk@negevlabs.com`
- Asana project "Read/Learn Triage": `1215897524719950` (team Zirmania Deal Team `597593980065513`)
- Asana section GIDs: Negev Labs `1215897524642810` · Zirmania Family Office `1215886226827868` · Sara Pipeline `1215899505871771` · Travel Relay `1215886226830719` · Ariadne Website `1215898379087843` · General/Reference `1215899505871835`

**Data flow (one run):**
0. `/egress-check` (one-time during build) confirms outbound hosts reachable.
1. Trigger (weekly **Friday 06:00 Asia/Jerusalem** cron — set `timezone="Asia/Jerusalem"` so DST is automatic — or manual `/learn/run`). Acquire `learn_lock` atomically; abort if held.
2. Fetch unread by folder ID (skip IDs already in `learn_processed.json`). **First production run: fetch the full backlog, do not chunk the clustering.**
3. Per message: strip the Outlook mobile signature + HTML boilerplate, extract + dedup anchor and bare URLs, classify by host.
4. Resolve content per type (resolver table below); on failure set `partial=true` with a reason — never fabricate.
5. **Summarize each item (Sonnet 4.6):** title, type, url, detected content date, named tools/approaches, key specifics, 2–3 sentence summary, confidence. These summaries feed clustering and curation.
6. **Cluster (Opus):** group all items in the batch into topic clusters from the summaries (the whole batch must be seen at once for dedup to work).
7. **Curate within each cluster (Opus, against the Ken's-needs profile below):** rank by usefulness-to-Ken × currency; select the best 1–2 as **keepers**; mark the rest **superseded/redundant** with a one-line reason. Within a cluster the newest credible item usually wins; an older item is kept only if it adds unique complementary value.
8. **Currency check (Claude `web_search`, keepers in fast-moving AI-tooling clusters only):** confirm the approach/tool is still current as of today; if superseded, annotate "likely superseded by X" and surface the newer canonical resource. Toggle: `LEARN_CURRENCY_CHECK` = `fast-moving` (default) | `off` | `all`.
9. **Tag buckets:** tag each keeper with 1+ of the 6 relevance buckets; flag whether it carries an action.
10. **Send ONE Friday digest** grouped by topic cluster (ordered by importance), each cluster showing "N saved → recommended best", the why-this-one rationale, the currency note, and the action; a collapsed "Skipped as redundant or outdated (M items)" list at the end with a one-line reason + link each. Create Asana tasks for keepers that carry an action, into the matching bucket section.
11. **Post-process:** mark every processed message (keepers and skipped) read + move to "read/learn/Processed" childfolder (create if missing — items are moved, never deleted); append IDs to `learn_processed.json`; release the lock.

### Curation logic & Ken's-needs profile
The curator judges "best for Ken" against this encoded profile (keep it in the prompt, not hardcoded magic):
- **Active builds:** Sara meeting-pipeline, Travel Relay (TAS), Ariadne Bio website; plus Negev Labs (venture studio) and Zirmania Family Office.
- **Stack in use:** Claude Code (Sonnet for dev, Opus for hard problems), Railway/Flask, Anthropic Managed Agents, MCP connectors, Google Apps Script, HubSpot, Asana, Microsoft Graph/Teams, Telegram bots; SVL discipline; one-chat-one-deployable-unit; spec-in-claude.ai-implement-in-Claude-Code.
- **"Best" = most applicable to how Ken actually works + most current**, not most popular. For a Cowork-setup cluster, the keeper is the one closest to his real Cowork usage (e.g. the TAS daily monitor, agentic workflows) and most up to date.
- **Currency sensitivity is highest for AI-tooling topics** (Claude Code, Cowork, MCP, skills, agent frameworks, models) — these change monthly and are the only clusters that get the live web check. Biotech, investing, and health age slowly and are judged on intra-cluster recency + content only.

**Anti-inflation guards (mirroring the Company Briefing Book):** do not invent a project connection; do not promote "interesting" to "action required"; a `partial`/unfetched item is labeled "content not retrieved — from title/sender only" and never fabricated; if a cluster has no clearly useful item, say so rather than manufacture a keeper.

### Content resolution by type
| Link type | Primary resolver | Fallback | Notes |
|---|---|---|---|
| Article / blog | Jina Reader (`r.jina.ai/{url}`) | trafilatura + requests | clean markdown; handles most JS/paywalls |
| YouTube | youtube-transcript-api | spoken.md | free, no key |
| Podcast (Spotify/Apple/etc.) | spoken.md (pass the URL) | — | Spotify has no transcript API — do not use it |
| X post (text) | X API `/2/tweets/{id}` | — | pay-per-use; expand author + created_at + note_tweet |
| X post with video | X API media variants → mp4 → Grok STT (`/v1/stt` URL mode) | — | transcribe, then summarize |
| Bare / unknown | Jina Reader | skip + flag | |

### Evaluated and Rejected Approaches
| Approach | Why considered | Why rejected |
|---|---|---|
| Flat per-item digest (summarize every link) | Simplest synthesis | The folder is redundant and partly stale; Ken needs consolidation, not 76 summaries. Cluster + curate instead. |
| Web-currency-check every item | Catches all obsolescence | Wasteful on slow-moving topics (biotech/health/investing). Check keepers in fast-moving AI-tooling clusters only. |
| Chunk the first backlog run | Bound cost/time | Clustering needs the whole set at once or near-duplicates split across chunks and never collapse. First run processes all unread in one curation pass. |
| Folder lookup by display name | Natural | The "/" in "read/learn" breaks Graph name lookup (returns NOT_FOUND). Use the hardcoded folder ID. |
| Spotify Web API for transcripts | Obvious first thought | No transcript endpoint; only 30-second audio previews; developer access tightened in 2026. Use spoken.md from the URL. |
| Grok chat / Files video upload | Ken's "use Grok" idea | Files API capped at 48 MB, not built for video containers, variable quality. Use **Grok STT URL mode** instead — same instinct, correct endpoint. |
| Own search infra for currency checks | More control | Claude's server-side `web_search` tool runs inside the existing API call — no new egress, no key, nothing to host. |
| X bookmarks in v1 | Ken's preferred capture | Needs OAuth2-PKCE + refresh-token lifecycle — a meaningful chunk that would delay the core digest. Deferred to Phase 2. |
| Real-time Graph mail webhook | Sara already runs Teams webhooks | A weekly reading queue doesn't need real-time; batch is simpler and cheaper. |
| New repo | Clean separation | Shares all of Sara's infra. A new repo duplicates everything. |

### Known Limitations
| Limitation | Impact | Mitigation |
|---|---|---|
| Obsolescence detection limited by training cutoff | Opus alone may miss post-cutoff changes | Intra-cluster recency + a live `web_search` check on fast-moving keepers |
| Clustering quality depends on whole-batch view | Split batches under-dedup | First run processes all unread at once; weekly runs are small |
| X video transcription quality is variable | Some video summaries weaker | Grok STT is best-in-class WER; flag low-confidence |
| X bookmarks need OAuth setup | Phase 2 blocked until X app live | Scoped separately; folder digest ships first |
| Spotify only via third-party + per-transcript cost | Small $ per podcast | spoken.md $0.10; only resolve podcasts actually linked |
| Reader misses some paywalled/JS-heavy pages | Occasional empty fetch | trafilatura fallback; flag `partial` |
| Railway egress to new hosts unverified | Module could fail to fetch | `/egress-check` probe is Step 0; resolve before wiring resolvers |
| Fetch failures | Risk of hallucinated summaries | Hard rule: `partial=true` → "content not retrieved"; never fabricate |

---

## Requirements

### MVP — Phase 1 (Outlook folder, incl. backlog consolidation)

- [ ] **Folder fetch by cached ID** — unread only, via `/users/bk@negevlabs.com/mailFolders/{id}/messages`; seed the known folder ID; skip IDs already in `learn_processed.json`. First run fetches the full backlog.
- [ ] **URL extraction + classification** — parse the HTML body, strip the Outlook mobile signature, extract + dedup anchor and bare URLs, classify by host.
- [ ] **Content resolvers** — article → Jina (+ trafilatura), YouTube → youtube-transcript-api, podcast → spoken.md, X text → X API, X video → X API media → Grok STT. All null-safe; all set `partial=true` + reason on failure.
- [ ] **Per-item summary (Sonnet 4.6)** — title, type, url, detected date, named tools/approaches, key specifics, 2–3 sentence summary, confidence.
- [ ] **Cluster + curate (Opus)** — cluster the whole batch by topic; per cluster select best 1–2 keepers for Ken's needs and mark the rest superseded/redundant with a reason. Subject line is a primary relevance signal. Anti-inflation rules enforced. **This is the core of the module.**
- [ ] **Currency check (`web_search`)** — on keepers in fast-moving AI-tooling clusters; annotate superseded/current; gated by `LEARN_CURRENCY_CHECK` (default `fast-moving`).
- [ ] **Friday digest email** — grouped by topic cluster, "N saved → best" + why-this-one + currency note + action, with a collapsed "Skipped (M)" list; atomic `learn_lock` guarantees exactly one send (reuse the Pulse lock fix).
- [ ] **Asana tasks for keepers with an action** — into project `1215897524719950`, placed in the matching section by bucket; task body carries summary + link + why-this-one.
- [ ] **First-run backlog consolidation** — process all 76 unread in a single clustering/curation pass; this is the headline value, so it ships in v1.
- [ ] **Post-process + idempotency** — mark every processed message (keepers + skipped) read, move to "read/learn/Processed" (create if missing; never delete), append IDs to `learn_processed.json`.
- [ ] **`/egress-check` (throwaway)** — pings each outbound host from the container, reports reachable/blocked; remove after egress confirmed.
- [ ] **Tests (SVL, offline, mocked)** — URL extraction + signature stripping; host classification; null-safe resolver on empty/None; **clustering groups near-duplicate items**; **curation selects the most-recent/most-relevant keeper and marks others superseded** (fixture: 5 mock "Cowork setup" summaries with varying dates); currency-check annotates a stale keeper (mock `web_search`); exactly-one-email lock race; processed-ID dedup. New logic ships with tests per the test-with-code mandate.
- [ ] **Deploy parity** — bump the version string in both `/version` and `/test`; fresh CACHEBUST; poll `/version` 20s×12; never confirm via `/test`.

### Nice-to-have — Phase 2 and later

- [ ] **X bookmarks ingestion** — `GET /2/users/:id/bookmarks` via OAuth2-PKCE user token (`bookmark.read`, `tweet.read`, `users.read`, `offline.access`); refresh token stored on `/data`, read-from-disk per request (mirrors the Microsoft token rotation). Post-process: remove the bookmark or track processed tweet IDs. *Needs the X developer app provisioned first.*
- [ ] **Cross-source dedup** — same link arriving via both email and bookmark collapses to one item.
- [ ] **Grok direct multimodal summarization** for X video (vs STT) if quality/cost proves better.
- [ ] **Cluster-memory across runs** — remember prior keepers so a later save on the same topic is compared against what was already surfaced.
- [ ] **Per-cluster scoring tuning** — thresholds for "skip the trivial", optional importance weighting per bucket.
- [ ] **Spotify metadata enrichment** — only if spoken.md misses an episode (fallback to show/episode description).

### Open setup items (manual, before/with the sessions)

- [ ] **X developer app** (Phase 2 prerequisite): create app at developer.x.com, OAuth2 enabled, scopes above, pay-per-use billing on; run the one-time PKCE authorize; store the refresh token on `/data`.
- [ ] **Egress confirmation**: run `/egress-check` once; if any host blocked, permit it before wiring resolvers.
- [ ] **Env vars on Railway**: `XAI_API_KEY` (Grok STT), `SPOKEN_API_KEY` (podcasts), optional `JINA_API_KEY`, `LEARN_CURRENCY_CHECK` (default `fast-moving`); Phase 2: `X_CLIENT_ID`, `X_CLIENT_SECRET`; confirm `INTERNAL_DOMAINS` lists all 5 domains; confirm `RUN_SCHEDULER=1`.
- [x] **Asana project**: "Read/Learn Triage" created — GID `1215897524719950`, 6 bucket sections (GIDs in System Architecture).

---

## Tech Stack

| Component | Technology | Why this technology for THIS project | Free? | AI codes it well? |
|---|---|---|---|---|
| Email source | Microsoft Graph (existing Sara app `43ed6271`, `Mail.ReadWrite`) | The folder lives in Outlook; Sara already authenticates to Graph and runs as a daemon. Address by cached folder ID (the "/" defeats name lookup). | Included (existing tenant) | Yes — same patterns Sara already uses. |
| Article reader | Jina Reader (`r.jina.ai`) | Prepend the URL, get clean markdown; handles JS/paywalls, no scraping stack to maintain. | Free tier (key raises limits) | Excellent — trivial HTTP. |
| YouTube transcripts | youtube-transcript-api | Free, no key, returns the transcript directly. | Yes | Excellent. |
| Podcast transcripts | spoken.md | Spotify has no transcript API and blocks audio download; spoken.md takes the URL and returns a transcript. Also covers YouTube fallback. | ~$0.10/transcript | Excellent — one GET. |
| X posts + bookmarks | X API v2 (pay-per-use) | Most saved links are X posts. Pay-per-use reads are $0.005, owned-reads/bookmarks $0.001 — pennies at Ken's volume. Bookmarks need OAuth2 user context (Phase 2). | Pay-per-use, ~$1–2/mo | Excellent — REST; OAuth2-PKCE is the fiddly part. |
| X video transcription | Grok Speech-to-Text (`api.x.ai/v1/stt`) | Ken's "use Grok" call via the right endpoint: URL-mode STT, best-in-class WER, $0.10/hr. | $0.10/hr | Excellent — POST a URL. |
| Per-item summaries | Claude Sonnet 4.6 | Extraction tier per Sara's model rule; cheap/fast across many items. | API usage | Native. |
| Cluster + curate | Claude Opus | Judgement-heavy: grouping, best-of selection, supersede calls — same tier as Pulse synthesis. | API usage | Native. |
| Currency check | Claude `web_search` tool (server-side) | Confirms fast-moving tooling is still current; runs inside the API call, so no new Railway egress, no search key, nothing to host. | Small per-search | Native — set the tool, read results. |
| Action items | Asana API (project `1215897524719950`) | Keepers-with-action become tasks; sections pre-created per bucket for one-call routing. | Included | Yes. |
| Digest delivery | Existing Sara `send_email` + atomic lock | Reuse the exact Pulse mechanism (incl. the `O_CREAT|O_EXCL` lock that fixed the double-send). | Included | Yes. |
| Scheduling | APScheduler + `RUN_SCHEDULER` guard | Reuse Sara's single-worker/guard topology; weekly Friday 06:00 Asia/Jerusalem (tz-aware) + `/learn/run`. | Included | Yes. |
| State | JSON files on `/data` | Folder-ID cache, processed IDs, run-lock — same scale/pattern as the Pulse lock. | Yes | Trivial. |
| Tests | pytest (existing SVL harness) | The repo already has check.py exit-2 + ~65 tests; add offline mocked tests for the new logic. | Yes | Excellent. |

### What's NOT in the stack
| Skipped | Why not for THIS project |
|---|---|
| Spotify Web API | No transcript endpoint, 30s-preview-only audio, tightened 2026 access. spoken.md does the job from the URL. |
| Grok chat / Files video upload | 48 MB cap, not built for video containers, variable quality. STT URL mode is correct. |
| Own search infrastructure | Claude's `web_search` tool covers the currency check inside the existing API call. |
| New repo / new server | Everything reuses Sara's Graph auth, `/data`, Claude, lock, `send_email`, SVL. |
| Database | Dozens of items and a few JSON files. No DB warranted. |
| Real-time Graph mail webhook | A weekly reading queue doesn't need push; scheduled batch is simpler. |
| Duffel / flight APIs | Out of scope — that's Travel Relay. |
