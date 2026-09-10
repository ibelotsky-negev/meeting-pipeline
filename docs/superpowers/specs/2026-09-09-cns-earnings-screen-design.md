# CNS / Rare-Neuro Earnings-Call Screen -- Design

**Date:** 2026-09-09
**Status:** approved (design), pending implementation
**Owner:** Ken Belotsky
**Module home:** Sara meeting-pipeline repo (`ibelotsky-negev/meeting-pipeline`)

## Problem

Ariadne Bio develops a non-hallucinogenic 5-HT2A/2C agonist for apathy in
Parkinson's disease, plus serotonergic work in Prader-Willi syndrome. Large-pharma
earnings calls are the highest-signal public venue for three things Ariadne needs
to know: which companies are hunting external CNS assets, what competitor CNS
programs are doing, and who is entering or exiting neuroscience as a therapeutic
area. Nobody reads 240 transcripts a quarter by hand.

Two design artifacts already exist and are the substance of this system:

- `cns_earnings_screen_prompt.md` -- the screening system prompt (three signal
  types, HIGH/MEDIUM/LOW priority, zone weighting, disambiguation rules,
  JSON-only output with a verbatim `quote` field).
- `cns_earnings_screen_keywords.yaml` -- the keyword prefilter (domain gate,
  BD-intent vocabulary, pipeline events, TA-strategy language, watchlist
  entities, exclusions, ambiguity rules, high-signal rare terms).

Both are checked in as data files and loaded at runtime. Tuning the screen must
never require a Python edit.

## Decision record

| Decision | Choice | Why |
|---|---|---|
| Transcript source | Financial Modeling Prep, Ultimate plan | Verified live 2026-09-09: full transcript, per-symbol dates, global latest feed, earnings calendar all return 200. $139/mo month-to-month. FMP's own FAQ: transcripts need "at least the Ultimate plan" |
| Universe | Drug Manufacturers General + Specialty & Generic + Biotechnology, market cap > $1B, US exchanges, UNION a force-include roster | Verified: the industry screener returns 222 rows but MISSES Roche, Otsuka, Lundbeck, UCB, Eisai, Astellas, Daiichi Sankyo entirely. A screener-only universe would silently omit the most CNS-relevant foreign majors |
| Host | Sara meeting-pipeline, two new modules | Ken's explicit choice. Reuses the Graph app-only send path, the Claude client, the atomic-lock and status-file patterns, the `/data` volume and the single-worker scheduler |
| Delivery | Daily digest during earnings season + quarterly season wrap-up | Ken's explicit choice |
| Recipients | bk@negevlabs.com, dan@negevlabs.com | Ken's explicit choice |
| Prefilter role | Skip gate + recall check, NOT a token cut | Approved. Cost at this volume is trivial, so the model reads the whole transcript with both zones labeled. The prefilter decides whether to call at all, and separately catches HIGH-signal terms the model failed to report |
| Screening model | `claude-opus-5` | The task is judgment-heavy: PD-vs-pharmacodynamics disambiguation, generic-BD-vs-neuro-BD gating, precision over recall. Env-overridable |

## Verified API facts (probed live 2026-09-09)

These are load-bearing and each one shaped the design.

**Endpoints, all `https://financialmodelingprep.com/stable/`:**

| Endpoint | Params | Returns |
|---|---|---|
| `earning-call-transcript` | `symbol`, `year`, `quarter` | `[{symbol, period, year, date, content}]` -- AbbVie Q2 2026 was 56,663 chars |
| `earning-call-transcript-dates` | `symbol` | `[{quarter, fiscalYear, date}]` |
| `earning-call-transcript-latest` | `page`, `limit` | `[{symbol, period, fiscalYear, date}]` global feed |
| `earnings-calendar` | `from`, `to` | `[{symbol, date, epsActual, epsEstimated, ...}]` |
| `company-screener` | `industry`, `marketCapMoreThan`, `isActivelyTrading`, `limit` | company rows with `exchangeShortName`, `marketCap`, `country` |
| `earnings-transcript-list` | none | 11,236 rows `{symbol, companyName, noOfTranscripts}` |

**Gotchas, all confirmed by probe:**

1. **The three period shapes disagree.** The transcript endpoint returns
   `year` + `period` (`"Q2"`); the dates endpoint returns `fiscalYear` +
   `quarter` (int `2`); the latest feed returns `fiscalYear` + `period`
   (`"Q2"`). Every ledger key must go through one normalizer or the same call
   gets screened twice under two keys.
2. **`earnings-transcript-list` is NOT authoritative.** Lundbeck (HLUYY) shows
   `noOfTranscripts: 0` there, but `earning-call-transcript-dates` returns 4
   with the latest dated 2026-08-19. Use the list as a diagnostic only; never
   as an availability gate.
3. **The latest feed is not reliably ordered and contains future dates.** FMP's
   FAQ says late-added transcripts are inserted by call date, not at the top.
   The probe's first row was `7011.T` dated 2026-11-07, two months in the
   future. So: bounded paging, filter to the date window, drop future dates,
   and never early-exit on the first out-of-window row.
4. **Cross-listings duplicate companies.** `LLY.TO` and `LLY`, `BHC.TO` and
   `BHC`, `CRON.TO` and `CRON` all appear. Filtering to NASDAQ/NYSE/AMEX
   removes all three pairs and costs nothing else -- TSX was the only non-US
   exchange present.
5. **Fiscal labels do not align across companies.** Takeda's FY2026 Q1 call and
   AbbVie's FY2026 Q2 call are both late July 2026. Season windows are therefore
   defined on the CALL DATE, never on the fiscal label.
6. **Several watchlist names are acquired and hold no calls.** Cerevel (last
   call 2023-11-01), Karuna (2023-11-02), Intra-Cellular (2024-10-30), Sage
   (2025-04-29), Alector (2025-08-07), Astellas (2025-04-25), Ono, Shionogi.
   Otsuka has no transcripts at all under either ADR ticker. These are
   entity-only: names the prompt should detect inside someone else's
   transcript, never companies to fetch.
7. **`year` in the fetch equals `fiscalYear` from the dates endpoint, and both
   can differ from the calendar year of the call.** Axsome's FY2025 Q4 call
   happened on 2026-02-23. So the fetch key is `(fiscalYear, quarter)` while
   the season window is decided by `date`. Conflating them misfiles a quarter
   of the calls every year.
8. **A listed transcript can come back with empty content, and the endpoint
   sometimes returns JSON `null`.** Axsome FY2026 Q2 is listed by the dates
   endpoint with `date: 2026-08-10`, and the fetch returns a well-formed row
   whose `content` is zero characters; a repeat call returned bare `null`. The
   client must tolerate `null`, a non-list, an empty list, and a row with
   missing or too-short content. Critically, an empty-content period must NOT
   be marked processed -- it is a coverage gap that retries (bounded by
   `CNS_MAX_FETCH_ATTEMPTS`) until FMP backfills it.
9. **There are two transcript formats and neither has paragraphs.** Verified
   across twelve transcripts. Format A (AbbVie) is a single unbroken line with
   **zero newlines** and inline `Analyst (Name).` labels. Format B (the other
   eleven) is newline-delimited `Speaker Name: text` turns, 39-126 lines, and
   Roche writes `Name : text` with a space before the colon. Neither format has
   blank-line paragraphs, and format B's turns run up to 25,525 characters --
   Roche has a single 25K-character turn. This invalidates the keyword YAML's
   "same paragraph" co-occurrence rule as written; see Proximity gating below.

**Force-include roster, evidence-based** (each has a 2026-dated transcript, verified):
`RHHBY, HLUYY, UCBJY, ESALY, DSNKY, NVO, GSK, IPSEY, MKKGY, BAYRY, SMMNY, NVS,
TAK, SNY, AZN, TEVA, BIIB, NBIX, ACAD, ALKS, SUPN, HRMY, SLNO, AXSM, DNLI`

## Architecture

Two new modules, both lazily imported by `app.py` (route and cron handlers only,
never at module load), following the FYI Triage and Read/Learn precedent.

```
cns_fmp.py       FMP data client. Universe, discovery, transcript fetch,
                 earnings calendar. Pure I/O + normalization, no business logic.

cns_screen.py    Everything else: prefilter, zone split, Claude screening,
                 quote verification, ledger, digest render, season wrap-up,
                 run orchestration.

cns_screen_keywords.yaml   Ken's keyword list (checked in, runtime-loaded)
cns_screen_prompt.md       Ken's system prompt (checked in, runtime-loaded)
cns_screen_universe.yaml   Force-include roster + entity-only names
```

The split is deliberate: the FMP client is the only part that touches the
network in normal operation, so isolating it keeps the screening logic fully
testable offline.

**Screening is SEQUENTIAL, deliberately.** There is no concurrency knob, and
adding one is ruled out rather than merely unimplemented: `process_one` mutates
the shared `ledger` and `findings_store` dicts, and the per-item persistence
ORDER that guarantees durability (findings written before the ledger, so a crash
between the two can only cause a harmless re-screen and never a lost finding)
depends on those writes happening one at a time. Threading it would mean
restructuring persistence, which buys nothing at this volume. A long run is safe
because the `O_CREAT|O_EXCL` run lock carries an mtime heartbeat, so a run that
outlives `CNS_LOCK_MAX_AGE` is not mistaken for a stale lock and reclaimed
underneath itself.

### Daily flow

1. **Universe** (`cns_fmp.build_universe`) -- three industry screens, market cap
   over $1B, filtered to NASDAQ/NYSE/AMEX, unioned with the force-include
   roster. Cached at `/data/cns_universe.json`, refreshed when older than
   `CNS_UNIVERSE_TTL_DAYS` (default 7).
2. **Discovery** (`cns_fmp.discover_new`) -- page the global latest feed up to
   `CNS_FEED_PAGES` (default 40) pages of 100, keep rows whose symbol is in the
   universe and whose call date falls in the window, drop future dates, then
   diff against the ledger.
3. **Weekly reconciliation** -- on the configured weekday, additionally sweep
   `earning-call-transcript-dates` for every universe symbol. This is the
   backstop for anything the unstably-ordered feed missed. About 250 calls,
   trivial at 3,000 requests/minute.
4. **Fetch and store** -- `earning-call-transcript` per new (symbol, fy, quarter);
   the raw text is written to `/data/cns_transcripts/<SYMBOL>-<FY>Q<Q>.txt`.
   Storage is required, not incidental: quote verification, the season wrap-up,
   and replay all read it back.
5. **Prepare** (`cns_screen.prepare_transcript`) -- strip the safe-harbor
   section, locate the Q&A boundary, label the two zones, then run the keyword
   prefilter with proximity gating.

### Zone detection (evidence-driven)

Naive splitting on "Question-and-Answer Session" would corrupt every zone label
on every transcript, because both formats contain that phrase inside the
operator's OPENING boilerplate. AbbVie's only occurrence is at character 176
of 56,663; Biogen's is at 354 and its "Q&A" at 1,606, both inside prepared
remarks.

The detector is therefore **position-banded and multi-signal**: search an
ordered list of handoff patterns, keep only matches falling between 15% and 85%
of the document, and take the earliest survivor. The band is what rejects the
boilerplate traps. Validated against twelve transcripts, boundaries confirmed
correct by eye in all eleven that matched:

| Company | Boundary | Matched on |
|---|---|---|
| AbbVie | 39.8% | "We will now open the call for questions" |
| Biogen | 43.7% | "open us up for questions" |
| Neurocrine | 23.4% | "jump into Q&A" |
| Roche | 57.1% | "open the Q&A session" |
| Takeda | 51.4% | "[Operator Instructions]" |
| Acadia | 31.9% | "[Operator Instructions]" |
| Eli Lilly | 44.3% | "[Operator Instructions]" |
| AstraZeneca | 48.9% | "move to the Q&A" |
| Pfizer | 59.6% | "move to Q&A" |
| Sanofi | 32.8% | "open the call to questions" |
| Harmony | 44.5% | "[Operator Instructions]" |

When nothing lands in the band, the zone is recorded as `UNKNOWN` for that
transcript and the model's own zone label is left standing rather than
overridden. Guessing a boundary is worse than admitting there isn't one,
because zone weighting feeds the prompt's priority rules.

`zone` on a finding is recomputed in code from the quote's character offset
relative to the boundary. `speaker` is NOT recomputed -- with inline speaker
names and no line structure in format A, the model reads attribution better
than a regex, and the prompt already allows null.

### Proximity gating (replaces "same paragraph")

The keyword YAML's central rule is that a generic BD term counts only with a
domain term "in the same paragraph". Neither transcript format has paragraphs,
and format B's speaker turns reach 25,525 characters, so treating a turn as a
paragraph would let a BD term co-occur with a domain term 25,000 characters
away. That is not a gate.

Co-occurrence is therefore defined as **within `CNS_GATE_WINDOW` characters
(default 600) of the other term**. This applies to the BD gate and to every
ambiguity rule that says "in the same paragraph": PD needs a Parkinson's term
nearby and no `PK/PD` / `PD-1` / `PD-L1` / `pharmacodynamic` nearby; MDS and
MSA need a neuro term nearby; AES needs an epilepsy or seizure term nearby.

This is a deliberate, documented deviation from the YAML's wording. The YAML's
`ambiguity_rules` block is human-readable prose, so the code implements each
rule explicitly and a test asserts each one's behavior. Adding a new rule to
the YAML does NOT auto-apply it; that requires a code change, and CLAUDE.md
records this.
6. **Screen** -- a transcript with zero domain-gate hits is recorded as
   `no_cns_content` and never sent to Claude. Everything else goes to Opus 5
   with Ken's prompt as the system prompt and a JSON schema enforcing the output
   shape.
7. **Verify** (`cns_screen.verify_findings`) -- every `quote` must appear
   verbatim in the stored transcript after normalizing whitespace and smart
   quotes. A finding that fails is dropped and counted. `zone` is recomputed
   from where the quote actually sits, overriding whatever the model said.
8. **Persist** -- findings written per company per period; the ledger is written
   after EACH transcript, not at the end of the run, so a restart mid-run never
   re-emails a delivered finding.
9. **Digest** -- emailed only on days with new transcripts.

### Digest contents

In order:

1. HIGH findings, grouped by company: quote, speaker, zone, why it matters.
2. MEDIUM then LOW findings.
3. One line listing companies screened with nothing relevant. Silence must be
   visible, or an empty digest is indistinguishable from a broken pipeline.
4. **Reported but no transcript** -- universe companies whose `earnings-calendar`
   date passed more than `CNS_TRANSCRIPT_GRACE_DAYS` (default 3) ago with no
   transcript fetched. This is what makes a Sanofi-style coverage gap visible.
   The calendar is read over `CNS_GAP_WINDOW_DAYS` (default 30), which is
   DELIBERATELY a different size from the discovery window: the grace filter
   trims the recent end of it, so reading the calendar over the 3-day discovery
   window left the eligible set exactly one date wide, and empty for any
   configuration with grace >= lookback. Discovery drives fetches and stays
   narrow; the calendar read is one cheap call and stays wide.
5. **Screened with errors** -- per-transcript `failed`, `gap` and `truncated`
   outcomes, with the reason. Outcomes here are: failed (screen error), gap (no transcript), or truncated (too long). A `failed` or `truncated` outcome is not in findings or nothing-relevant, so this section is the only path to the reader. A `gap` outcome can appear in both sections (calendar-driven and non-terminal), which is acceptable -- both statements are true, and this section adds the specific failure reason. The overlap is rare because a discovered gap usually has a call date within the grace period.
6. **Unconfirmed high-signal keyword hits** -- paragraphs matching the YAML's
   `high_signal_rare_terms` that the model did NOT report. A model miss on
   apathy, Prader-Willi, or 5-HT2C is the single most expensive failure this
   screen can have, so the keyword layer reports it independently.
7. Dropped-quote count, if any.

### Season wrap-up

Windows are defined on call date, not fiscal label:

| Season label | Call-date window | Cron |
|---|---|---|
| Q4 / annual | Jan 1 -- Mar 15 | Mar 16 |
| Q1 | Apr 1 -- Jun 15 | Jun 16 |
| Q2 | Jul 1 -- Sep 15 | Sep 16 |
| Q3 | Oct 1 -- Dec 15 | Dec 16 |

The cron runs the day AFTER the window closes, not on the closing day. The
season job shares the single `_cns_trigger_lock` with the daily 07:00 scan
(they write the same ledger and findings store), screening is sequential, and a
peak-season run of up to `CNS_MAX_TRANSCRIPTS_PER_RUN` Opus transcripts can
still be running at 08:00. A season job that finds the lock held logs "already
running" and never retries, so a collision would lose that quarter's wrap-up
outright. Running on the 16th makes the collision much less likely, because mid-month has few earnings calls and a small pending queue -- but the daily job runs that morning too and shares the lock, so a long run can still cause the wrap-up to skip.

One Opus 5 call reads every stored finding in the window and writes a narrative:
BD appetite across pharma, pipeline moves by area, TA-posture shifts, watchlist
company by company, and the read-through for Ariadne. Streamed, because the
output is long. Emailed as "CNS Earnings Season Wrap: Q2 2026".

A wrap-up run reads stored findings only. It never re-screens and never mutates
the ledger, so it is safe to re-run for comparison.

### First run

The Q2 2026 backfill is the first real run and doubles as the calibration pass
the prompt's usage notes call for: screen the most recent transcript for every
universe company with a call dated on or after 2026-07-01, then build the Q2
wrap-up.

## Safety and correctness properties

- **Never fabricates a quote.** Verbatim verification against the stored
  transcript is mandatory and unconditional. A finding whose quote cannot be
  located is dropped, never reported, and the drop is counted in the digest.
- **Idempotent.** Ledger keyed on normalized `SYMBOL:FY:Qn`, written after each
  transcript. A restart, a crash, or an overlapping trigger cannot re-email.
- **One run at a time.** Atomic `O_CREAT|O_EXCL` lock at `/data/cns_lock.json`
  with stale reclaim and an mtime heartbeat, matching the Weekly Pulse and FYI
  Triage pattern. A single in-process trigger lock in `app.py` gates both routes
  and both cron jobs, because they share one ledger file.
- **Read-only against everything but its own state.** No mail is moved, no
  transcript is deleted, nothing outside `/data/cns_*` is written.
- **Untrusted input.** Transcript text is counterparty-authored. It is fenced
  in the prompt and any fence marker it tries to emit is stripped, following
  the followup-engine precedent.
- **Degrades honestly.** A missing `FMP_API_KEY` disables the module with a
  logged warning rather than crashing the scheduler. A per-transcript failure
  is recorded and reported; it never aborts the run.
- **Cost-bounded.** `CNS_MAX_TRANSCRIPTS_PER_RUN` (default 60) caps a single
  run. Hitting the cap is logged and surfaced in the digest, never silent.

## Configuration

New env vars:

| Var | Default | Purpose |
|---|---|---|
| `FMP_API_KEY` | unset | FMP Ultimate key. Unset = module disabled |
| `CNS_RECIPIENTS` | `bk@negevlabs.com,dan@negevlabs.com` | Digest recipients |
| `CNS_SCREEN_MODEL` | `claude-opus-5` | Per-transcript screener |
| `CNS_SEASON_MODEL` | `claude-opus-5` | Season wrap-up |
| `CNS_MIN_MARKET_CAP` | `1000000000` | Universe floor |
| `CNS_UNIVERSE_TTL_DAYS` | `7` | Universe cache lifetime |
| `CNS_FEED_PAGES` | `40` | Max pages of the latest feed per run |
| `CNS_LOOKBACK_DAYS` | `3` | Daily DISCOVERY window (drives transcript fetches) |
| `CNS_GAP_WINDOW_DAYS` | `30` | Earnings-CALENDAR window for the coverage-gap section. Deliberately wider than the discovery window -- see Digest contents |
| `CNS_MAX_TRANSCRIPTS_PER_RUN` | `60` | Per-run cost bound |
| `CNS_TRANSCRIPT_GRACE_DAYS` | `3` | Reported-but-missing threshold |
| `CNS_RECONCILE_WEEKDAY` | `6` | Weekly per-symbol sweep (0=Mon, 6=Sun) |
| `CNS_SEASON_MAX_PAYLOAD_CHARS` | `400000` | Size cap on the season wrap-up's single Opus payload. Over it, LOW-priority findings are dropped first, then `why_it_matters`, and what was trimmed is logged |
| `CNS_ANTHROPIC_TIMEOUT` | `300` | Per-call timeout, seconds |
| `CNS_GATE_WINDOW` | `600` | Proximity window, chars, for BD and ambiguity gating |
| `CNS_MIN_TRANSCRIPT_CHARS` | `2000` | Below this the content is unusable -> coverage gap |
| `CNS_MAX_FETCH_ATTEMPTS` | `4` | Retries for an empty-content period before giving up |
| `CNS_ZONE_BAND` | `0.15,0.85` | Position band for Q&A boundary candidates |

Reused: `CLAUDE_API_KEY`, `BOT_SENDER_EMAIL`, `DATA_DIR`.

Dependency changes in `requirements.txt`: add `PyYAML==6.0.2` (not currently
present), raise `anthropic>=0.43.0` to `anthropic>=0.116.0` (the floor that
supports `output_config.format` structured outputs).

State files on `/data`: `cns_universe.json`, `cns_ledger.json`,
`cns_findings.json`, `cns_lock.json`, `cns_status.json`, and the
`cns_transcripts/` directory.

## Endpoints

| Route | Purpose |
|---|---|
| `/cns/run` | Daily scan. `?dry_run=&sync=&days=N&limit=N&backlog=1&force=1` |
| `/cns/season` | Season wrap-up. `?season=Q2-2026&start=&end=&dry_run=&sync=` |
| `/cns/status` | Last run outcome, per-company decisions, dropped-quote count, heartbeat |

Cron: daily 07:00 Asia/Jerusalem; season wrap-up on the 16th of March, June,
September and December at 08:00 Asia/Jerusalem (the day after the window
closes -- see Season wrap-up for why not the 15th).

`?dry_run=1` on `/cns/run` writes NO state, but it still screens (real Opus
spend) and still emails the digest with a `[DRY] ` subject prefix. It bounds
state, not cost. The CLI mirrors this: `python cns_screen.py` without `--live`
is the dry form, and `--no-email` is what suppresses the send.

## Claude API usage

Per the current API reference:

- `claude-opus-5`, thinking on by default. Do NOT send `budget_tokens`,
  `temperature`, `top_p`, or `top_k` -- all rejected with a 400 on this model.
  No assistant prefill.
- Structured output via `output_config: {"effort": "high", "format":
  {"type": "json_schema", "schema": FINDINGS_SCHEMA}}` with
  `additionalProperties: false`.
- Server-side refusal fallbacks enabled by default:
  `betas=["server-side-fallback-2026-07-01"]` with `fallbacks="default"` on
  `client.beta.messages.create`. If that call returns a `BadRequestError`
  naming the fallback or beta parameter, the caller retries once on the plain
  `client.messages.create` path and logs a warning, so a beta-surface change
  cannot take the screen down.
- `stop_reason` is checked BEFORE reading content. `refusal` and `max_tokens`
  both mark that transcript failed and continue the run.
- The season wrap-up streams (`client.messages.stream` +
  `get_final_message()`), because its output is long.

## Cost

| Item | Estimate |
|---|---|
| FMP Ultimate | $139/mo month-to-month, or $99/mo billed annually |
| Screening, Opus 5 | ~240 transcripts/quarter, ~15K input tokens each. Prefilter skips the ones with no CNS content. Roughly $12-20/quarter |
| Season wrap-up, Opus 5 | ~$1-3 per run |

## Testing

Offline only, per the repo's standing rule. No test calls a live API.

Covered: period normalization across all three FMP shapes; universe build
including the US-exchange filter, the cross-listing dedup, and the
force-include union; feed paging with unstable order and future dates;
safe-harbor stripping; every YAML gating rule (BD-needs-domain,
PD/MSA/MDS/AES ambiguity, stem expansion, word boundaries, exclusions);
quote verification including smart quotes and whitespace normalization; zone
recomputation; ledger idempotency and per-transcript persistence; season window
assignment across all four quarters and boundary dates; digest rendering with
and without findings; the reported-but-missing calculation; the unconfirmed
high-signal-hit path; FMP error paths returning empty rather than raising; the
Claude beta-to-plain fallback retry; and the three routes' success and failure
paths.

Three test groups exist specifically because a live probe caught the bug:

- **Zone detection.** Boilerplate traps must NOT match: a transcript whose only
  "question-and-answer" is at 0.3% must resolve to the later real handoff, and
  one with no in-band candidate must return `UNKNOWN` rather than a guess. Both
  transcript formats are fixtured, including Roche's `Name : ` spacing.
- **Proximity gating.** A BD term 25,000 characters from a domain term in the
  same speaker turn must NOT count; the same pair 200 characters apart must.
- **Empty-content handling.** `null`, `[]`, a non-list, and a row with
  zero-length or too-short content must each be treated as a coverage gap that
  leaves the ledger untouched so the period retries.

Fixtures are trimmed excerpts of real transcripts, stored under
`tests/fixtures/cns/`, with no API key and no network access.

## Out of scope

- Non-US-listed companies with no ADR. Otsuka in particular cannot be screened
  from FMP and stays entity-only.
- Uncaptioned or unpublished transcripts. FMP publishes about four hours after
  a call; a company that never publishes is reported as a coverage gap, not
  worked around.
- Any write to HubSpot or Asana. This screen reports; it does not create tasks.
  If that turns out to be wanted, it is a separate change.
