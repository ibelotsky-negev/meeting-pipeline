# CNS / Rare-Neuro Earnings-Call Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screen every public pharma/biotech earnings call over $1B market cap each quarter for CNS and rare-neuro BD intent, competitor pipeline moves, and therapeutic-area strategy shifts, and email Ken and Dan a daily digest plus a quarterly season wrap-up.

**Architecture:** Two new lazily-imported modules in the Sara meeting-pipeline repo. `cns_fmp.py` is the Financial Modeling Prep data client (universe, discovery, transcript fetch, earnings calendar) and is the only part that touches the network in normal operation. `cns_screen.py` holds everything else: keyword prefilter, Q&A zone detection, Claude screening with structured output, verbatim quote verification, ledger, digest rendering, season wrap-up, and run orchestration. Ken's prompt and keyword list ship as runtime-loaded data files so tuning never requires a Python edit.

**Tech Stack:** Python 3.12, Flask (existing `app:app`), `anthropic>=0.116.0` structured outputs, `PyYAML`, `requests`, APScheduler, Microsoft Graph app-only send via `email_pipeline_sync`, `/data` Railway volume.

**Spec:** `docs/superpowers/specs/2026-09-09-cns-earnings-screen-design.md`

## Global Constraints

- **ASCII-only** in comments and non-user-facing strings. Use `->` not an arrow, `--` not an em-dash, `"` not smart quotes.
- **CRLF preservation.** `app.py` and `CLAUDE.md` are stored CRLF (repo `autocrlf=true`, no `.gitattributes`). Edit them preserving CRLF or `git diff` shows a full-file rewrite. New files may be LF.
- **Null-safe API responses.** `data.get("x") or {}`, never `data.get("x", {})`.
- **Tests are offline-only.** No test may call a live API. `tests/conftest.py` installs an autouse `no_network` fixture that raises on any real `requests` call.
- **Version string lives in exactly 2 places** in `app.py`: `/version` (line 4030) and `/test` (line 4098). Both currently read `"2.34.0-prompt-integrity"`. Bump both to `"2.35.0-cns-earnings-screen"` in the deploy task. Before picking that literal, run `git fetch && git log origin/main -1 --format=%s` and confirm no collision (Ken merges his own PRs concurrently).
- **Session start:** `git fetch && git rebase origin/main` before any edit.
- **Model parameters.** `claude-opus-5` has thinking on by default. Never send `budget_tokens`, `temperature`, `top_p`, or `top_k` (all return 400 on this model). No assistant prefill. Check `stop_reason` BEFORE reading `response.content`.
- **Never weaken, skip, or delete a test to make it pass.** Fix the code.
- Every new state file lives under `_CNS_DATA_DIR`, resolved as `os.environ.get("DATA_DIR") or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))` -- copy this expression from `fyi_triage.py:175-178`.

## File Structure

| File | Responsibility |
|---|---|
| `cns_fmp.py` (new, ~320 lines) | FMP HTTP client, period normalization, universe build, discovery, transcript fetch, earnings calendar. No business logic, no Claude, no email. |
| `cns_screen.py` (new, ~950 lines) | YAML/prompt loading, keyword prefilter with proximity gating, Q&A zone detection, Claude screening, quote verification, ledger/lock/status, digest render + send, season wrap-up, `run_daily` / `run_season`. |
| `cns_screen_keywords.yaml` (new) | Ken's keyword list, copied verbatim from `C:/Users/ibelo/Downloads/cns_earnings_screen_keywords.yaml`. |
| `cns_screen_prompt.md` (new) | Ken's prompt, copied verbatim from `C:/Users/ibelo/Downloads/cns_earnings_screen_prompt.md`. |
| `cns_screen_universe.yaml` (new) | Force-include roster and entity-only names. |
| `tests/fixtures/cns/*.txt` (new) | Trimmed real-transcript excerpts, both formats. |
| `tests/test_cns_fmp.py` (new) | Task 1 tests. |
| `tests/test_cns_screen.py` (new) | Tasks 2-8 tests. |
| `app.py` (modify) | 3 routes, 2 cron jobs, 1 trigger lock, version bump. |
| `requirements.txt` (modify) | Add `PyYAML==6.0.2`, raise `anthropic` floor to `>=0.116.0`. |
| `CLAUDE.md` (modify) | Module section, env vars, endpoints, failure modes. |

---

### Task 1: FMP data client (`cns_fmp.py`)

**Files:**
- Create: `cns_fmp.py`
- Create: `tests/test_cns_fmp.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `fmp_enabled() -> bool`
  - `normalize_period(row: dict) -> tuple[int, int] | None` returning `(fiscal_year, quarter)`
  - `period_key(symbol: str, fiscal_year: int, quarter: int) -> str` returning `"ABBV:2026:Q2"`
  - `build_universe(force_include: list[str] | None = None, min_market_cap: int | None = None) -> dict[str, dict]`
  - `discover_from_feed(universe: dict, start_date: str, end_date: str, max_pages: int | None = None) -> list[dict]` where each dict is `{"symbol","fiscal_year","quarter","date"}`
  - `discover_from_dates(universe: dict, start_date: str, end_date: str) -> list[dict]` same shape
  - `fetch_transcript(symbol: str, fiscal_year: int, quarter: int) -> tuple[str | None, str]` returning `(content, call_date)` on success or `(None, reason)` on failure
  - `reported_symbols(universe: dict, start_date: str, end_date: str) -> dict[str, str]` mapping symbol to its latest reported call date
  - Module constants `INDUSTRIES`, `US_EXCHANGES`, `FMP_BASE`

- [ ] **Step 1: Write the failing tests for period normalization and period_key**

Create `tests/test_cns_fmp.py`:

```python
"""Offline tests for the FMP data client. Every FMP shape asserted here was
captured from a live probe on 2026-09-09; see the design spec's
"Verified API facts" table."""
import cns_fmp


def test_normalize_period_handles_all_three_fmp_shapes():
    # transcript endpoint: year + period("Q2")
    assert cns_fmp.normalize_period({"year": 2026, "period": "Q2"}) == (2026, 2)
    # dates endpoint: fiscalYear + quarter(int)
    assert cns_fmp.normalize_period({"fiscalYear": 2026, "quarter": 2}) == (2026, 2)
    # latest feed: fiscalYear + period("Q2")
    assert cns_fmp.normalize_period({"fiscalYear": 2026, "period": "Q2"}) == (2026, 2)


def test_normalize_period_rejects_garbage():
    assert cns_fmp.normalize_period({}) is None
    assert cns_fmp.normalize_period({"year": 2026}) is None
    assert cns_fmp.normalize_period({"year": "nope", "period": "Q2"}) is None
    assert cns_fmp.normalize_period({"year": 2026, "period": "Q9"}) is None
    assert cns_fmp.normalize_period({"year": 1500, "period": "Q1"}) is None


def test_period_key_is_stable_and_uppercase():
    assert cns_fmp.period_key("abbv", 2026, 2) == "ABBV:2026:Q2"
    # the same call discovered via two different endpoint shapes must collide
    a = cns_fmp.normalize_period({"year": 2026, "period": "Q2"})
    b = cns_fmp.normalize_period({"fiscalYear": 2026, "quarter": 2})
    assert cns_fmp.period_key("ABBV", *a) == cns_fmp.period_key("ABBV", *b)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cns_fmp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cns_fmp'`

- [ ] **Step 3: Create `cns_fmp.py` with the header, config, and HTTP helper**

```python
#!/usr/bin/env python3
"""
cns-fmp -- Financial Modeling Prep data client for the CNS earnings screen.

The ONLY module in the screen that touches the network in normal operation.
It fetches and normalizes; it holds no business logic, calls no LLM, and sends
no mail. Isolating it is what keeps cns_screen.py fully testable offline.

Requires the FMP Ultimate plan -- transcripts are gated to that tier (FMP's own
FAQ: "you will need at least the Ultimate plan"). An unset FMP_API_KEY disables
the whole screen with a logged warning rather than raising.

Every quirk handled below was observed on a live probe 2026-09-09:
  - Three endpoints report the same period in three different shapes.
  - The global "latest" feed is not reliably ordered and contains future dates.
  - The screener misses Roche, Otsuka, Lundbeck, UCB, Eisai and Astellas
    entirely, which is why a force-include roster exists.
  - Cross-listings (LLY.TO vs LLY) would double-screen one call.
  - A listed transcript can return empty content, or bare JSON null.

ASCII-only comments and non-user-facing strings.
Author: Negev Labs
"""

import os
import json
import logging
from datetime import date

import requests

logger = logging.getLogger("cns-fmp")

FMP_BASE = "https://financialmodelingprep.com/stable/"
FMP_TIMEOUT = int(os.environ.get("CNS_FMP_TIMEOUT", "60"))

# The three industry buckets that make up "public pharma". Biotechnology is
# included on Ken's instruction -- it is where the CNS mid-caps live
# (Neurocrine, Acadia, Axsome, Denali). Screener returns 17 + 29 + 176 rows
# at the $1B floor as of 2026-09-09.
INDUSTRIES = (
    "Drug Manufacturers - General",
    "Drug Manufacturers - Specialty & Generic",
    "Biotechnology",
)

# US listings only. This is a DEDUP mechanism, not a geography preference: the
# probe found TSX was the only non-US exchange present and every TSX row was a
# cross-listing of a US name (LLY.TO/LLY, BHC.TO/BHC, CRON.TO/CRON), so
# without this filter three companies get screened twice. Foreign majors reach
# the universe through the force-include roster instead, via their ADRs.
US_EXCHANGES = frozenset(("NASDAQ", "NYSE", "AMEX"))

SCREENER_LIMIT = int(os.environ.get("CNS_SCREENER_LIMIT", "3000"))
FEED_PAGE_SIZE = int(os.environ.get("CNS_FEED_PAGE_SIZE", "100"))
CNS_FEED_PAGES = int(os.environ.get("CNS_FEED_PAGES", "40"))
CNS_MIN_MARKET_CAP = int(os.environ.get("CNS_MIN_MARKET_CAP", "1000000000"))
CNS_MIN_TRANSCRIPT_CHARS = int(os.environ.get("CNS_MIN_TRANSCRIPT_CHARS", "2000"))


def _api_key() -> str:
    return os.environ.get("FMP_API_KEY", "").strip()


def fmp_enabled() -> bool:
    """-> True when an FMP key is configured. An absent key disables the screen
    rather than crashing the scheduler."""
    return bool(_api_key())


def _redact(text: str) -> str:
    """The key rides in the query string, so requests can echo it into an
    exception message. Never log or email an unredacted FMP error."""
    key = _api_key()
    if key and text:
        return text.replace(key, "***")
    return text or ""


def _fmp_get(path: str, **params):
    """-> parsed JSON (any type), or None on ANY failure. NEVER raises.

    Returning None rather than raising is deliberate: one bad FMP call must
    degrade that company, not abort a 240-company run."""
    key = _api_key()
    if not key:
        logger.warning("[cns] FMP_API_KEY not set -- FMP calls disabled")
        return None
    params["apikey"] = key
    try:
        resp = requests.get(FMP_BASE + path, params=params, timeout=FMP_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"[cns] FMP {path} request failed: {_redact(str(e))}")
        return None
    if resp.status_code != 200:
        logger.warning(f"[cns] FMP {path} HTTP {resp.status_code}")
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning(f"[cns] FMP {path} returned non-JSON")
        return None


def _rows(data) -> list:
    """-> a list of dict rows, whatever FMP actually returned.

    FMP success is a bare list; errors can be a dict ({"Error Message": ...}),
    and the transcript endpoint has been observed returning bare JSON null.
    Every caller goes through here so none of them can do len(None)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []
```

- [ ] **Step 4: Add `normalize_period` and `period_key`**

```python
def normalize_period(row: dict):
    """-> (fiscal_year, quarter) as ints, or None if unusable.

    The three endpoints disagree on field names AND types, and conflating them
    means the same call gets screened twice under two ledger keys:
        earning-call-transcript        {"year": 2026,       "period": "Q2"}
        earning-call-transcript-dates  {"fiscalYear": 2026, "quarter": 2}
        earning-call-transcript-latest {"fiscalYear": 2026, "period": "Q2"}

    NOTE fiscal_year is NOT the calendar year of the call: Axsome's FY2025 Q4
    call happened 2026-02-23. Fetching uses (fiscal_year, quarter); season
    windows use the separate `date` field. Never substitute one for the other.
    """
    row = row or {}
    fy = row.get("fiscalYear")
    if fy is None:
        fy = row.get("year")
    quarter = row.get("quarter")
    if quarter is None:
        quarter = row.get("period")
    if isinstance(quarter, str):
        quarter = quarter.strip().upper().lstrip("Q")
    try:
        fy = int(fy)
        quarter = int(quarter)
    except (TypeError, ValueError):
        return None
    if not 1 <= quarter <= 4:
        return None
    if not 1990 <= fy <= 2100:
        return None
    return fy, quarter


def period_key(symbol: str, fiscal_year: int, quarter: int) -> str:
    """The ledger key. One canonical form so a call discovered via the feed and
    the same call discovered via the dates sweep collide instead of double-
    screening."""
    return f"{(symbol or '').strip().upper()}:{int(fiscal_year)}:Q{int(quarter)}"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cns_fmp.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add cns_fmp.py tests/test_cns_fmp.py
git commit -m "feat(cns): FMP client scaffold with period normalization"
```

- [ ] **Step 7: Write the failing test for `build_universe`**

Append to `tests/test_cns_fmp.py`:

```python
import pytest


@pytest.fixture
def fake_fmp(monkeypatch):
    """Route every _fmp_get through a recorded script. Keyed by path; the value
    is a list consumed in call order so paging can be simulated."""
    calls = []
    script = {}

    def _fake(path, **params):
        calls.append((path, dict(params)))
        queue = script.get(path)
        if queue is None:
            return None
        if isinstance(queue, list) and queue and isinstance(queue[0], _Reply):
            return queue.pop(0).payload if queue else None
        return queue

    monkeypatch.setattr(cns_fmp, "_fmp_get", _fake)
    monkeypatch.setenv("FMP_API_KEY", "test-fmp-key")
    return script, calls


class _Reply:
    def __init__(self, payload):
        self.payload = payload


def test_build_universe_filters_non_us_cross_listings(fake_fmp):
    script, _ = fake_fmp
    script["company-screener"] = [
        {"symbol": "LLY", "companyName": "Eli Lilly and Company",
         "exchangeShortName": "NYSE", "marketCap": 1_065_000_000_000, "country": "US"},
        {"symbol": "LLY.TO", "companyName": "Eli Lilly and Company",
         "exchangeShortName": "TSX", "marketCap": 1_396_000_000_000, "country": "CA"},
    ]
    uni = cns_fmp.build_universe()
    assert "LLY" in uni
    assert "LLY.TO" not in uni, "TSX cross-listing would double-screen one call"


def test_build_universe_unions_force_include_roster(fake_fmp):
    script, _ = fake_fmp
    script["company-screener"] = [
        {"symbol": "BIIB", "companyName": "Biogen Inc.",
         "exchangeShortName": "NASDAQ", "marketCap": 20_000_000_000, "country": "US"},
    ]
    # Roche is absent from the industry screener entirely -- verified live.
    uni = cns_fmp.build_universe(force_include=["RHHBY", "biib"])
    assert uni["RHHBY"]["source"] == "roster"
    assert uni["BIIB"]["source"] == "screener", "roster must not clobber screener metadata"
    assert len(uni) == 2, "force-include is a union, not an append"


def test_build_universe_survives_total_fmp_failure(fake_fmp):
    script, _ = fake_fmp
    script["company-screener"] = None  # _fmp_get returned None
    uni = cns_fmp.build_universe(force_include=["RHHBY"])
    assert uni == {"RHHBY": uni["RHHBY"]}
    assert uni["RHHBY"]["source"] == "roster"
```

- [ ] **Step 8: Run to verify failure**

Run: `python -m pytest tests/test_cns_fmp.py -k universe -v`
Expected: FAIL with `AttributeError: module 'cns_fmp' has no attribute 'build_universe'`

- [ ] **Step 9: Implement `build_universe`**

```python
def build_universe(force_include=None, min_market_cap: int = None) -> dict:
    """-> {SYMBOL: {name, industry, market_cap, exchange, country, source}}

    Two sources, unioned:
      1. The three industry screens above the market-cap floor, US exchanges
         only (see US_EXCHANGES for why that filter is a dedup, not a policy).
      2. An explicit force-include roster. This is NOT belt-and-braces -- the
         screener genuinely returns none of Roche, Otsuka, Lundbeck, UCB, Eisai,
         Astellas or Daiichi Sankyo, so a screener-only universe silently omits
         the most CNS-relevant foreign majors. Verified live 2026-09-09.

    Screener metadata wins over a roster stub for a symbol in both.
    """
    floor = CNS_MIN_MARKET_CAP if min_market_cap is None else int(min_market_cap)
    universe = {}
    for industry in INDUSTRIES:
        rows = _rows(_fmp_get(
            "company-screener",
            industry=industry,
            marketCapMoreThan=floor,
            isActivelyTrading="true",
            limit=SCREENER_LIMIT,
        ))
        if not rows:
            logger.warning(f"[cns] screener returned nothing for industry {industry!r}")
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if (row.get("exchangeShortName") or "") not in US_EXCHANGES:
                continue
            universe[symbol] = {
                "name": row.get("companyName") or symbol,
                "industry": industry,
                "market_cap": row.get("marketCap"),
                "exchange": row.get("exchangeShortName"),
                "country": row.get("country"),
                "source": "screener",
            }
    for symbol in (force_include or []):
        symbol = (symbol or "").strip().upper()
        if not symbol or symbol in universe:
            continue
        universe[symbol] = {
            "name": symbol, "industry": None, "market_cap": None,
            "exchange": None, "country": None, "source": "roster",
        }
    logger.info(f"[cns] universe built: {len(universe)} symbols")
    return universe
```

- [ ] **Step 10: Run to verify pass**

Run: `python -m pytest tests/test_cns_fmp.py -v`
Expected: PASS (6 tests)

- [ ] **Step 11: Commit**

```bash
git add cns_fmp.py tests/test_cns_fmp.py
git commit -m "feat(cns): universe build with cross-listing dedup and force-include roster"
```

- [ ] **Step 12: Write the failing tests for discovery**

Append to `tests/test_cns_fmp.py`:

```python
def test_discover_from_feed_drops_future_dates_and_out_of_universe(fake_fmp):
    script, _ = fake_fmp
    # Row 1 is the real live observation: 7011.T dated two months in the future.
    script["earning-call-transcript-latest"] = [
        {"symbol": "7011.T", "period": "Q2", "fiscalYear": 2025, "date": "2099-11-07"},
        {"symbol": "FDEV.L", "period": "Q4", "fiscalYear": 2026, "date": "2026-09-09"},
        {"symbol": "ABBV", "period": "Q2", "fiscalYear": 2026, "date": "2026-07-31"},
    ]
    found = cns_fmp.discover_from_feed({"ABBV": {}}, "2026-07-01", "2026-09-15", max_pages=1)
    assert found == [{"symbol": "ABBV", "fiscal_year": 2026, "quarter": 2,
                      "date": "2026-07-31"}]


def test_discover_from_feed_does_not_early_exit_on_unsorted_rows(fake_fmp):
    """FMP's FAQ: late-added transcripts are inserted by call date, not at the
    top. An early exit on the first out-of-window row would drop the rest."""
    script, _ = fake_fmp
    script["earning-call-transcript-latest"] = [
        {"symbol": "ABBV", "period": "Q1", "fiscalYear": 2020, "date": "2020-04-01"},
        {"symbol": "BIIB", "period": "Q2", "fiscalYear": 2026, "date": "2026-07-29"},
    ]
    found = cns_fmp.discover_from_feed({"ABBV": {}, "BIIB": {}},
                                       "2026-07-01", "2026-09-15", max_pages=1)
    assert [f["symbol"] for f in found] == ["BIIB"]


def test_discover_from_feed_dedupes_repeated_period(fake_fmp):
    script, _ = fake_fmp
    script["earning-call-transcript-latest"] = [
        {"symbol": "ABBV", "period": "Q2", "fiscalYear": 2026, "date": "2026-07-31"},
        {"symbol": "abbv", "quarter": 2, "fiscalYear": 2026, "date": "2026-07-31"},
    ]
    found = cns_fmp.discover_from_feed({"ABBV": {}}, "2026-07-01", "2026-09-15", max_pages=1)
    assert len(found) == 1
```

- [ ] **Step 13: Run to verify failure**

Run: `python -m pytest tests/test_cns_fmp.py -k discover -v`
Expected: FAIL with `AttributeError: module 'cns_fmp' has no attribute 'discover_from_feed'`

- [ ] **Step 14: Implement both discovery functions**

```python
def discover_from_feed(universe: dict, start_date: str, end_date: str,
                       max_pages: int = None) -> list:
    """-> [{symbol, fiscal_year, quarter, date}] for universe companies whose
    call date falls in [start_date, end_date].

    Cheap primary discovery: one global feed instead of 240 per-symbol calls.

    Three properties are load-bearing and each one is a live observation:
      - The feed is NOT reliably ordered (FMP FAQ: a transcript added late is
        inserted at its call date, not at the top), so this NEVER early-exits
        on an out-of-window row -- it reads every row of every page it pulls.
      - The feed contains future dates (7011.T dated 2026-11-07 seen on
        2026-09-09), so anything after today is dropped.
      - Paging is bounded by max_pages so a peak-season day cannot run away.
    """
    pages = CNS_FEED_PAGES if max_pages is None else int(max_pages)
    today = date.today().isoformat()
    found, seen = [], set()
    for page in range(pages):
        rows = _rows(_fmp_get("earning-call-transcript-latest",
                              page=page, limit=FEED_PAGE_SIZE))
        if not rows:
            break
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if symbol not in universe:
                continue
            call_date = (row.get("date") or "")[:10]
            if not call_date or call_date > today:
                continue
            if not (start_date <= call_date <= end_date):
                continue
            parsed = normalize_period(row)
            if not parsed:
                continue
            fiscal_year, quarter = parsed
            key = period_key(symbol, fiscal_year, quarter)
            if key in seen:
                continue
            seen.add(key)
            found.append({"symbol": symbol, "fiscal_year": fiscal_year,
                          "quarter": quarter, "date": call_date})
    else:
        logger.warning(f"[cns] feed page limit {pages} reached -- "
                       "relying on the weekly dates sweep for the remainder")
    return found


def discover_from_dates(universe: dict, start_date: str, end_date: str) -> list:
    """-> same shape as discover_from_feed, via one call per universe symbol.

    The reconciliation backstop for anything the unstably-ordered feed missed.
    About 240 calls, trivial at the Ultimate tier's 3000 requests/minute, so it
    runs weekly rather than daily purely to keep the daily run fast."""
    found, seen = [], set()
    for symbol in sorted(universe):
        rows = _rows(_fmp_get("earning-call-transcript-dates", symbol=symbol))
        for row in rows:
            call_date = (row.get("date") or "")[:10]
            if not call_date or not (start_date <= call_date <= end_date):
                continue
            parsed = normalize_period(row)
            if not parsed:
                continue
            fiscal_year, quarter = parsed
            key = period_key(symbol, fiscal_year, quarter)
            if key in seen:
                continue
            seen.add(key)
            found.append({"symbol": symbol, "fiscal_year": fiscal_year,
                          "quarter": quarter, "date": call_date})
    return found
```

Note the `for ... else` on the paging loop: the warning fires only when the loop completes all `pages` iterations without breaking, which is exactly the "we hit the cap" case.

- [ ] **Step 15: Run to verify pass**

Run: `python -m pytest tests/test_cns_fmp.py -v`
Expected: PASS (9 tests)

- [ ] **Step 16: Write the failing tests for `fetch_transcript` and `reported_symbols`**

Append to `tests/test_cns_fmp.py`:

```python
def test_fetch_transcript_success(fake_fmp):
    script, _ = fake_fmp
    script["earning-call-transcript"] = [
        {"symbol": "ABBV", "period": "Q2", "year": 2026,
         "date": "2026-07-31", "content": "x" * 50_000},
    ]
    content, call_date = cns_fmp.fetch_transcript("ABBV", 2026, 2)
    assert len(content) == 50_000
    assert call_date == "2026-07-31"


def test_fetch_transcript_treats_empty_content_as_gap(fake_fmp):
    """AXSM FY2026Q2, verified live: the dates endpoint lists it with
    date 2026-08-10 and the fetch returns a well-formed row whose content is
    zero characters. This must NOT look like 'screened, no findings'."""
    script, _ = fake_fmp
    script["earning-call-transcript"] = [
        {"symbol": "AXSM", "period": "Q2", "year": 2026,
         "date": "2026-08-10", "content": ""},
    ]
    content, reason = cns_fmp.fetch_transcript("AXSM", 2026, 2)
    assert content is None
    assert "too_short" in reason


def test_fetch_transcript_tolerates_null_and_dict_and_empty(fake_fmp):
    """A repeat call for AXSM returned bare JSON null, which crashed the probe
    with len(None). Each of these must be a clean failure, not an exception."""
    script, _ = fake_fmp
    for payload in (None, [], {}, {"Error Message": "boom"}, "nope"):
        script["earning-call-transcript"] = payload
        content, reason = cns_fmp.fetch_transcript("AXSM", 2026, 2)
        assert content is None
        assert reason


def test_reported_symbols_ignores_scheduled_but_unreported(fake_fmp):
    script, _ = fake_fmp
    script["earnings-calendar"] = [
        {"symbol": "ABBV", "date": "2026-07-31", "epsActual": 3.1},
        {"symbol": "BIIB", "date": "2026-09-20", "epsActual": None},
        {"symbol": "NOPE", "date": "2026-07-15", "epsActual": 1.0},
    ]
    got = cns_fmp.reported_symbols({"ABBV": {}, "BIIB": {}}, "2026-07-01", "2026-09-15")
    assert got == {"ABBV": "2026-07-31"}
```

- [ ] **Step 17: Run to verify failure**

Run: `python -m pytest tests/test_cns_fmp.py -k "fetch or reported" -v`
Expected: FAIL with `AttributeError: module 'cns_fmp' has no attribute 'fetch_transcript'`

- [ ] **Step 18: Implement `fetch_transcript` and `reported_symbols`**

```python
def fetch_transcript(symbol: str, fiscal_year: int, quarter: int):
    """-> (content, call_date) on success, or (None, reason) on failure.

    `year` on this endpoint means FISCAL year and matches `fiscalYear` from the
    dates endpoint -- not the calendar year of the call.

    Four distinct failure shapes, all observed live, and none of them may be
    mistaken for a successful screen with no findings:
      - _fmp_get returned None (HTTP error, timeout, non-JSON)
      - bare JSON null, a dict, or a string instead of a list
      - an empty list
      - a well-formed row whose `content` is empty or implausibly short
    The caller records the last case as a retryable coverage gap so the period
    is re-fetched once FMP backfills it.
    """
    data = _fmp_get("earning-call-transcript",
                    symbol=symbol, year=fiscal_year, quarter=quarter)
    if data is None:
        return None, "fmp_request_failed"
    rows = _rows(data)
    if not rows:
        return None, "no_row_returned"
    content = rows[0].get("content") or ""
    call_date = (rows[0].get("date") or "")[:10]
    if len(content) < CNS_MIN_TRANSCRIPT_CHARS:
        return None, f"content_too_short:{len(content)}"
    return content, call_date


def reported_symbols(universe: dict, start_date: str, end_date: str) -> dict:
    """-> {SYMBOL: latest call date} for universe companies that have ACTUALLY
    reported in the window (epsActual present).

    Feeds the digest's "reported but no transcript" line, which is what makes a
    coverage gap visible instead of silent. A row with epsActual None is merely
    scheduled and must not count as reported."""
    rows = _rows(_fmp_get("earnings-calendar",
                          **{"from": start_date, "to": end_date}))
    reported = {}
    for row in rows:
        symbol = (row.get("symbol") or "").strip().upper()
        if symbol not in universe:
            continue
        if row.get("epsActual") is None:
            continue
        call_date = (row.get("date") or "")[:10]
        if not call_date:
            continue
        if symbol not in reported or call_date > reported[symbol]:
            reported[symbol] = call_date
    return reported
```

- [ ] **Step 19: Run the whole file to verify pass**

Run: `python -m pytest tests/test_cns_fmp.py -v`
Expected: PASS (13 tests)

- [ ] **Step 20: Verify syntax and commit**

```bash
python -c "import ast; ast.parse(open('cns_fmp.py').read()); print('OK')"
git add cns_fmp.py tests/test_cns_fmp.py
git commit -m "feat(cns): transcript fetch and earnings-calendar coverage check"
```

---

### Task 2: Data files and loaders

**Files:**
- Create: `cns_screen_keywords.yaml` (copy)
- Create: `cns_screen_prompt.md` (copy)
- Create: `cns_screen_universe.yaml` (new content)
- Create: `cns_screen.py` (header + loaders only)
- Create: `tests/test_cns_screen.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `load_keywords(path: str | None = None) -> dict` -- parsed YAML, cached
  - `load_prompt(path: str | None = None) -> str` -- the system prompt only
  - `load_universe_config(path: str | None = None) -> dict` with keys `force_include: list[str]` and `entity_only: list[str]`
  - Group constants `DOMAIN_GROUPS`, `BD_GROUP`, `STANDALONE_GROUPS`, `ASSET_GROUP`

- [ ] **Step 1: Copy Ken's two artifacts into the repo verbatim**

```bash
cp "C:/Users/ibelo/Downloads/cns_earnings_screen_keywords.yaml" cns_screen_keywords.yaml
cp "C:/Users/ibelo/Downloads/cns_earnings_screen_prompt.md" cns_screen_prompt.md
python -c "import yaml; d=yaml.safe_load(open('cns_screen_keywords.yaml',encoding='utf-8')); print(sorted(d.keys()))"
```

Expected output includes: `ambiguity_rules`, `bd_intent`, `conference_mentions`, `domain_cns_modalities`, `domain_general`, `domain_neuropsych_symptoms`, `domain_parkinsons_and_synuclein`, `domain_rare_neuro`, `domain_serotonergic_and_neuroplastogen`, `exclusions`, `high_signal_rare_terms`, `pipeline_events`, `ta_strategy`, `watchlist_assets`, `watchlist_companies`

Do not edit either file's content in this task. They are Ken's to tune.

- [ ] **Step 2: Create `cns_screen_universe.yaml`**

```yaml
# CNS earnings screen -- universe overrides.
#
# force_include: companies the FMP industry screener does NOT return but that
# must be screened anyway. This list is not belt-and-braces: a live probe on
# 2026-09-09 confirmed the screener returns NONE of Roche, Otsuka, Lundbeck,
# UCB, Eisai, Astellas or Daiichi Sankyo, because their US listings are ADRs
# that carry no industry classification or market cap in the screener. Every
# ticker below was verified to return a 2026-dated transcript.
force_include:
  - RHHBY      # Roche -- prasinezumab, trontinemab. 60 transcripts, latest 2026-07-23
  - HLUYY      # Lundbeck -- pure-play CNS. Latest 2026-08-19
  - UCBJY      # UCB -- epilepsy franchise. Latest 2026-07-30
  - ESALY      # Eisai -- Leqembi. Latest 2026-08-03
  - DSNKY      # Daiichi Sankyo. Latest 2026-01-30
  - NVO        # Novo Nordisk
  - GSK
  - IPSEY      # Ipsen
  - MKKGY      # Merck KGaA
  - BAYRY      # Bayer
  - SMMNY      # Sumitomo Pharma
  - NVS        # Novartis
  - TAK        # Takeda
  - SNY        # Sanofi
  - AZN        # AstraZeneca
  - TEVA
  - BIIB       # Biogen
  - NBIX       # Neurocrine
  - ACAD       # Acadia -- pimavanserin/Nuplazid, Daybue
  - ALKS       # Alkermes -- active BD counterparty
  - SUPN       # Supernus
  - HRMY       # Harmony Biosciences
  - SLNO       # Soleno -- VYKAT, Prader-Willi
  - AXSM       # Axsome
  - DNLI       # Denali

# entity_only: names that belong in the prompt's high-precision entity list but
# hold NO earnings call, so they must never be fetched. Acquired or delisted,
# with the last observed transcript date.
entity_only:
  - Otsuka         # no transcripts under OTSKY or OTSKF at all
  - Cerevel        # acquired by AbbVie; last call 2023-11-01
  - Karuna         # acquired by BMS; last call 2023-11-02
  - Intra-Cellular # acquired by J&J; last call 2024-10-30
  - Sage           # last call 2025-04-29
  - Alector        # last call 2025-08-07
  - Astellas       # last ADR call 2025-04-25
  - Ono            # last call 2025-08-01
  - Shionogi       # last call 2025-07-28
  - Boehringer     # private, holds no earnings call
```

- [ ] **Step 3: Write the failing tests for the loaders**

Create `tests/test_cns_screen.py`:

```python
"""Offline tests for the CNS screen. Fixtures under tests/fixtures/cns/ are
trimmed excerpts of real FMP transcripts captured 2026-09-09."""
import os

import pytest

import cns_screen


def test_load_keywords_exposes_every_group_the_code_relies_on():
    kw = cns_screen.load_keywords()
    for group in cns_screen.DOMAIN_GROUPS:
        assert kw.get(group), f"{group} missing or empty"
    assert kw.get(cns_screen.BD_GROUP)
    assert kw.get(cns_screen.ASSET_GROUP)
    for group in cns_screen.STANDALONE_GROUPS:
        assert kw.get(group), f"{group} missing or empty"
    assert kw.get("exclusions")
    assert kw.get("high_signal_rare_terms")


def test_load_prompt_returns_only_the_system_prompt():
    prompt = cns_screen.load_prompt()
    # the substance
    assert "BD_INTENT" in prompt
    assert "PIPELINE_MOVE" in prompt
    assert "TA_STRATEGY" in prompt
    # NOT the surrounding document: the paste instruction above the first rule
    # and the usage notes below the second must both be stripped
    assert "Paste everything between" not in prompt
    assert "Usage notes" not in prompt
    assert "Calibration" not in prompt


def test_load_universe_config_splits_screenable_from_entity_only():
    cfg = cns_screen.load_universe_config()
    assert "RHHBY" in cfg["force_include"]
    assert "Otsuka" in cfg["entity_only"]
    # an acquired company must never end up in the fetch list
    assert not any(t.upper() in ("CERE", "KRTX", "ITCI", "SAGE")
                   for t in cfg["force_include"])
```

- [ ] **Step 4: Run to verify failure**

Run: `python -m pytest tests/test_cns_screen.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cns_screen'`

- [ ] **Step 5: Create `cns_screen.py` with header, config and loaders**

```python
#!/usr/bin/env python3
"""
cns-screen -- Sara module (CNS / rare-neuro earnings-call screen).

Reads large-pharma earnings-call transcripts and flags passages relevant to
Ariadne Bio's agenda: appetite for external CNS assets (BD_INTENT), competitor
CNS pipeline movement (PIPELINE_MOVE), and therapeutic-area posture shifts
(TA_STRATEGY). It is a FILTER, not a summarizer -- most transcripts contain
nothing relevant and saying so is the correct answer.

What it does each run:
  1. Build/refresh the universe (cns_fmp): three pharma/biotech industry screens
     above $1B, US listings only, unioned with a force-include roster of ADRs
     the screener misses.
  2. Discover new transcripts from the global latest feed; reconcile weekly with
     a per-symbol sweep.
  3. Fetch and STORE each new transcript. Storage is required, not incidental:
     quote verification, the season wrap-up and replay all read it back.
  4. Prepare: mask the safe harbor, locate the Q&A boundary, run the keyword
     prefilter with proximity gating.
  5. Screen with Opus 5 under Ken's prompt and a JSON schema. A transcript with
     zero domain hits is recorded as no_cns_content and never sent to the model.
  6. VERIFY every quote appears verbatim in the stored transcript. A finding
     whose quote cannot be located is DROPPED -- never reported.
  7. Email a digest, only on days with new transcripts.

Two structural facts about FMP transcripts drive the design, both verified
across twelve real transcripts on 2026-09-09:
  - Neither transcript format has paragraphs, and format B's speaker turns run
    to 25,525 characters. So the keyword YAML's "same paragraph" co-occurrence
    rule is implemented as a CHARACTER PROXIMITY window instead.
  - Both formats contain "question-and-answer" and "Q&A" inside the operator's
    OPENING boilerplate (AbbVie at char 176 of 56663). So Q&A detection is
    position-banded, and when nothing lands in the band the zone is UNKNOWN
    rather than guessed.

Reuses Sara infra: cns_fmp for data, the Weekly-Pulse atomic O_CREAT|O_EXCL
lock pattern, email_pipeline_sync's Graph app-only send, the single-worker
scheduler topology.

Usage:
    python cns_screen.py                        # daily scan, dry-run
    python cns_screen.py --live --days 3        # real run
    python cns_screen.py --season Q2-2026       # season wrap-up

ASCII-only comments and non-user-facing strings.
Author: Negev Labs
"""

import os
import re
import json
import time
import logging
import argparse
import threading as _threading
from datetime import datetime, timezone, date

import yaml

import cns_fmp

logger = logging.getLogger("cns-screen")

_HERE = os.path.dirname(os.path.abspath(__file__))

KEYWORDS_PATH = os.environ.get("CNS_KEYWORDS_PATH", os.path.join(_HERE, "cns_screen_keywords.yaml"))
PROMPT_PATH = os.environ.get("CNS_PROMPT_PATH", os.path.join(_HERE, "cns_screen_prompt.md"))
UNIVERSE_CONFIG_PATH = os.environ.get("CNS_UNIVERSE_CONFIG_PATH",
                                      os.path.join(_HERE, "cns_screen_universe.yaml"))

# ======================================================================
#  KEYWORD GROUP ROLES
# ======================================================================
# The YAML is a flat set of named lists; these constants assign each list its
# gating role. Two assignments are judgment calls worth stating:
#
#   watchlist_assets  -> DOMAIN. An asset name like Cobenfy or trofinetide is
#                        inherently CNS, so it opens the domain gate by itself.
#   watchlist_companies -> STANDALONE signal, but NOT domain. Pharma companies
#                        name each other on every call ("unlike Pfizer, we..."),
#                        so a bare company name is not evidence of CNS content.
DOMAIN_GROUPS = (
    "domain_general",
    "domain_parkinsons_and_synuclein",
    "domain_neuropsych_symptoms",
    "domain_serotonergic_and_neuroplastogen",
    "domain_rare_neuro",
    "domain_cns_modalities",
)
ASSET_GROUP = "watchlist_assets"
BD_GROUP = "bd_intent"
STANDALONE_GROUPS = (
    "pipeline_events",
    "conference_mentions",
    "ta_strategy",
    "watchlist_companies",
)
PARKINSONS_GROUP = "domain_parkinsons_and_synuclein"

_keywords_cache = {}
_prompt_cache = {}
_universe_config_cache = {}


def load_keywords(path: str = None) -> dict:
    """-> the parsed keyword YAML, cached per path.

    Ken tunes this file directly; the code must never need editing to change a
    term. Raises on a malformed file: a silently-empty keyword set would make
    every transcript look CNS-free."""
    path = path or KEYWORDS_PATH
    if path not in _keywords_cache:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict) or not data:
            raise ValueError(f"keyword file {path} did not parse to a non-empty mapping")
        _keywords_cache[path] = data
    return _keywords_cache[path]


def load_prompt(path: str = None) -> str:
    """-> the system prompt only, extracted from Ken's markdown.

    The file wraps the prompt in horizontal rules: a paste instruction above the
    first `---`, the prompt between the rules, and usage notes (message format,
    verification advice, cost estimates) below the second. Sending the whole
    file would feed the model instructions addressed to the implementer."""
    path = path or PROMPT_PATH
    if path not in _prompt_cache:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        parts = re.split(r"(?m)^---[ \t]*$", raw)
        if len(parts) < 3:
            raise ValueError(f"prompt file {path} has no ----delimited prompt body")
        body = parts[1].strip()
        if "BD_INTENT" not in body:
            raise ValueError(f"prompt body extracted from {path} looks wrong "
                             "(no BD_INTENT); check the --- rules")
        _prompt_cache[path] = body
    return _prompt_cache[path]


def load_universe_config(path: str = None) -> dict:
    """-> {"force_include": [SYMBOL, ...], "entity_only": [name, ...]}

    force_include are screened. entity_only are names the model should
    recognize inside someone else's transcript but that hold no call of their
    own -- they must never reach cns_fmp.fetch_transcript."""
    path = path or UNIVERSE_CONFIG_PATH
    if path not in _universe_config_cache:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        _universe_config_cache[path] = {
            "force_include": [str(s).strip().upper()
                              for s in (data.get("force_include") or []) if str(s).strip()],
            "entity_only": [str(s).strip()
                            for s in (data.get("entity_only") or []) if str(s).strip()],
        }
    return _universe_config_cache[path]
```

- [ ] **Step 6: Run to verify pass**

Run: `python -m pytest tests/test_cns_screen.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
python -c "import ast; ast.parse(open('cns_screen.py').read()); print('OK')"
git add cns_screen.py cns_screen_keywords.yaml cns_screen_prompt.md cns_screen_universe.yaml tests/test_cns_screen.py
git commit -m "feat(cns): keyword, prompt and universe data files with loaders"
```

---

### Task 3: Text normalization, exclusion masking, and the keyword prefilter

**Files:**
- Modify: `cns_screen.py`
- Modify: `tests/test_cns_screen.py`

**Interfaces:**
- Consumes: `load_keywords`, group constants from Task 2.
- Produces:
  - `normalize_text(s: str) -> str` -- smart quotes and dashes folded, length preserved
  - `term_pattern(term: str) -> re.Pattern`
  - `compile_group(kw: dict, group: str) -> list[tuple[str, re.Pattern]]`
  - `mask_spans(text: str, patterns) -> str` -- equal-length masking
  - `mask_safe_harbor(text: str) -> str`
  - `PrefilterResult` dataclass with fields `domain_hits`, `bd_hits`, `standalone_hits`, `high_signal_hits`, `screen` (bool), `masked`
  - `prefilter(text: str, kw: dict | None = None, window: int | None = None) -> PrefilterResult`
  - `CNS_GATE_WINDOW` constant

Every hit is a `(term, offset)` pair. Offsets index the ORIGINAL normalized text and stay valid because all masking is equal-length.

- [ ] **Step 1: Write the failing tests for normalization and term patterns**

Append to `tests/test_cns_screen.py`:

```python
def test_normalize_text_preserves_length():
    """Offsets from the prefilter, the zone boundary and quote verification all
    index the same string, so normalization must never change its length."""
    raw = "Parkinson\u2019s disease \u2014 the \u201cnon-motor\u201d symptoms\u00a0here"
    out = cns_screen.normalize_text(raw)
    assert len(out) == len(raw)
    assert "Parkinson's disease" in out
    assert '"non-motor"' in out


def test_term_pattern_word_boundaries_and_stems():
    # strict boundary on a short acronym: ALS must not match inside "also"
    als = cns_screen.term_pattern("ALS")
    assert als.search("diagnosed with ALS last year")
    assert not als.search("we also expect growth")
    # tau must not match "taught"
    tau = cns_screen.term_pattern("tau")
    assert tau.search("tau pathology")
    assert not tau.search("he taught us")
    # a trailing * is a prefix stem
    disc = cns_screen.term_pattern("discontinu*")
    assert disc.search("we discontinued the program")
    assert disc.search("the discontinuation was announced")
    # a multi-word term matches across a line break, because format B is
    # newline-delimited and a phrase can straddle a turn boundary
    bbb = cns_screen.term_pattern("blood-brain barrier")
    assert bbb.search("crosses the blood-brain\nbarrier reliably")


def test_mask_spans_is_equal_length_and_blocks_the_substring():
    text = "Our PD-1 asset and our PD program"
    patterns = [cns_screen.term_pattern("PD-1")]
    masked = cns_screen.mask_spans(text, patterns)
    assert len(masked) == len(text)
    assert "PD-1" not in masked
    # the real PD mention survives
    assert masked.endswith("our PD program")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_cns_screen.py -k "normalize or term_pattern or mask_spans" -v`
Expected: FAIL with `AttributeError: module 'cns_screen' has no attribute 'normalize_text'`

- [ ] **Step 3: Implement normalization, term patterns and masking**

Append to `cns_screen.py`:

```python
# ======================================================================
#  TEXT NORMALIZATION
# ======================================================================
# EVERY replacement below is length-preserving on purpose. The prefilter's hit
# offsets, the Q&A boundary offset and quote verification all index the same
# normalized string, so a normalizer that changed length would silently
# misalign zone labels. The one multi-char case (an ellipsis) is deliberately
# NOT folded for that reason.
_CHAR_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2011": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u200b": " ",
}
_FOLD_TABLE = str.maketrans(_CHAR_FOLD)


def normalize_text(text: str) -> str:
    """-> text with smart quotes, dashes and exotic spaces folded to ASCII.

    LENGTH-PRESERVING. See _CHAR_FOLD."""
    return (text or "").translate(_FOLD_TABLE)


def term_pattern(term: str) -> re.Pattern:
    """-> a compiled, case-insensitive, word-boundaried pattern for one YAML term.

    Per the keyword file's own matching notes:
      - case-insensitive
      - \\b on every term, which is what stops ALS matching inside "also" and
        tau inside "taught"
      - a trailing '*' is a prefix stem (discontinu* -> discontinued/-ation)
    Interior whitespace becomes \\s+ so a multi-word term still matches when the
    phrase straddles a newline, which happens in the newline-delimited format.
    """
    term = normalize_text(term).strip()
    is_stem = term.endswith("*")
    if is_stem:
        term = term[:-1].strip()
    body = r"\s+".join(re.escape(part) for part in term.split())
    suffix = r"\w*" if is_stem else r"\b"
    return re.compile(r"\b" + body + suffix, re.IGNORECASE)


def compile_group(kw: dict, group: str):
    """-> [(term, pattern)] for one YAML group. Non-string entries are skipped
    so a malformed line degrades one term, not the whole group."""
    out = []
    for entry in (kw.get(group) or []):
        if not isinstance(entry, str) or not entry.strip():
            continue
        out.append((entry.strip(), term_pattern(entry)))
    return out


def mask_spans(text: str, patterns) -> str:
    """-> text with every match of every pattern replaced by '#' of EQUAL length.

    Equal length is the whole point: it neutralizes a phrase for term matching
    while keeping every downstream character offset valid.

    This is how the exclusion list does its work. Masking 'PD-1' means the 'PD'
    term regex can never match inside it, which is a stronger guarantee than
    checking for a blocker after the fact."""
    masked = text or ""
    for pattern in patterns:
        masked = pattern.sub(lambda m: "#" * len(m.group(0)), masked)
    return masked


_SAFE_HARBOR_CUE = re.compile(
    r"forward[-\s]looking statements?|safe harbor|private securities litigation",
    re.IGNORECASE)
CNS_SAFE_HARBOR_SPAN = int(os.environ.get("CNS_SAFE_HARBOR_SPAN", "4000"))


def mask_safe_harbor(text: str, span: int = None) -> str:
    """-> text with each safe-harbor disclaimer masked, EQUAL LENGTH.

    Ken's prompt says to ignore the safe-harbor section; masking enforces it
    mechanically for the KEYWORD layer rather than trusting an instruction.

    Bounded to `span` characters from each cue because the disclaimer has no
    reliable end marker. Frequently a no-op -- AbbVie's transcript contains zero
    occurrences of 'forward-looking' -- so this must never be load-bearing."""
    span = CNS_SAFE_HARBOR_SPAN if span is None else int(span)
    text = text or ""
    out = text
    for match in _SAFE_HARBOR_CUE.finditer(text):
        start = match.start()
        end = min(len(text), start + span)
        out = out[:start] + ("#" * (end - start)) + out[end:]
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_cns_screen.py -k "normalize or term_pattern or mask_spans" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add cns_screen.py tests/test_cns_screen.py
git commit -m "feat(cns): length-preserving normalization, term patterns, equal-length masking"
```

- [ ] **Step 6: Write the failing tests for the prefilter and its gating rules**

Append to `tests/test_cns_screen.py`:

```python
def test_prefilter_skips_a_transcript_with_no_cns_content():
    text = ("We had a strong quarter in oncology. Our PD-L1 asset met its "
            "primary endpoint and we have significant capacity for business "
            "development going forward.")
    res = cns_screen.prefilter(text)
    assert res.screen is False, "no domain term -> must not reach the model"
    assert res.domain_hits == []


def test_prefilter_asset_name_alone_opens_the_domain_gate():
    """A named CNS asset is inherently in-domain even with no other vocabulary."""
    res = cns_screen.prefilter("Cobenfy uptake continues to exceed our plan.")
    assert res.screen is True
    assert any(term.lower() == "cobenfy" for term, _ in res.domain_hits)


def test_prefilter_company_name_alone_does_not_open_the_gate():
    """Pharma companies name each other constantly; that is not CNS evidence."""
    res = cns_screen.prefilter("Unlike Pfizer, we did not pursue that deal.")
    assert res.screen is False


def test_bd_term_needs_a_domain_term_within_the_window():
    near = ("We see real opportunity in neuroscience. We have significant "
            "capacity for business development this year.")
    res_near = cns_screen.prefilter(near)
    assert res_near.bd_hits, "a BD term next to a domain term is the money signal"

    far = ("We see real opportunity in neuroscience. " + ("filler text. " * 200)
           + "We have significant capacity for business development.")
    res_far = cns_screen.prefilter(far)
    assert res_far.screen is True, "the domain term still opens the gate"
    assert not res_far.bd_hits, "a BD term 2000+ chars away is not co-occurrence"


def test_pd_requires_parkinsons_context_and_is_blocked_by_pharmacodynamics():
    ok = "Our Parkinson's program advanced; PD patients showed less OFF time."
    assert any(term == "PD" for term, _ in cns_screen.prefilter(ok).domain_hits)

    # PD-1 is masked by the exclusion list, so it cannot yield a PD hit at all
    onc = "Our PD-1 and PD-L1 assets in oncology performed well."
    assert not any(term == "PD" for term, _ in cns_screen.prefilter(onc).domain_hits)

    # pharmacodynamics is NOT in the exclusion list, so the ambiguity gate
    # itself has to reject this one
    pk = "The PK/PD profile and PD data supported dose selection."
    assert not any(term == "PD" for term, _ in cns_screen.prefilter(pk).domain_hits)


def test_msa_mds_aes_ambiguity_gates():
    # MSA as a master services agreement, no neuro nearby -> rejected
    assert not any(t == "MSA" for t, _ in
                   cns_screen.prefilter("We signed an MSA with the supplier.").domain_hits)
    # MSA with neuro context -> accepted
    assert any(t == "MSA" for t, _ in cns_screen.prefilter(
        "In multiple system atrophy, our MSA cohort enrolled fully.").domain_hits)
    # MDS as myelodysplastic syndromes -> rejected without neuro context
    assert not any(t == "MDS" for t, _ in
                   cns_screen.prefilter("Our MDS franchise in heme-onc grew.").domain_hits)
    # AES needs epilepsy/seizure context
    assert not any(t == "AES" for t, _ in cns_screen.prefilter(
        "There were no treatment-related AEs or AES in the study.").standalone_hits)
    assert any(t == "AES" for t, _ in cns_screen.prefilter(
        "We will present seizure-freedom data at AES this December.").standalone_hits)


def test_prefilter_collects_high_signal_terms_with_offsets():
    text = "We are studying apathy in Parkinson's disease with a 5-HT2C agonist."
    res = cns_screen.prefilter(text)
    terms = {t.lower() for t, _ in res.high_signal_hits}
    assert "apathy" in terms
    assert "5-ht2c" in terms
    for _, offset in res.high_signal_hits:
        assert 0 <= offset < len(text)
```

- [ ] **Step 7: Run to verify failure**

Run: `python -m pytest tests/test_cns_screen.py -k "prefilter or bd_term or pd_requires or ambiguity" -v`
Expected: FAIL with `AttributeError: module 'cns_screen' has no attribute 'prefilter'`

- [ ] **Step 8: Implement the prefilter**

Append to `cns_screen.py`:

```python
# ======================================================================
#  PREFILTER
# ======================================================================
# The keyword YAML asks for co-occurrence "within the same paragraph". FMP
# transcripts have NO paragraphs -- format A is one unbroken 56,663-character
# line with zero newlines, and format B's speaker turns reach 25,525 characters
# (Roche has a single 25K turn). Treating a turn as a paragraph would let a BD
# term co-occur with a domain term 25,000 characters away, which is not a gate.
#
# So co-occurrence is CHARACTER PROXIMITY: within CNS_GATE_WINDOW characters.
# This is a deliberate, documented deviation from the YAML's wording.
CNS_GATE_WINDOW = int(os.environ.get("CNS_GATE_WINDOW", "600"))

# The YAML's `ambiguity_rules` block is human-readable PROSE, so each rule is
# implemented explicitly below and asserted by a test. Adding a rule to the
# YAML does NOT auto-apply it -- that needs a code change here. CLAUDE.md
# records this.
_PD_BLOCKERS = re.compile(r"pharmacodynamic|PK\s*/\s*PD|\bPD-?L?1\b", re.IGNORECASE)
_NEURO_CUE = re.compile(
    r"\b(neuro\w*|brain|CNS|central nervous|Parkinson\w*|Alzheimer\w*|epilep\w*|"
    r"seizure|dementia|psychiatr\w*|cognitive|multiple system atrophy|ataxia)\b",
    re.IGNORECASE)
_EPILEPSY_CUE = re.compile(r"\b(epilep\w*|seizure|Dravet|Lennox|convulsi\w*)\b",
                           re.IGNORECASE)

# Terms whose bare match is not trusted. Each maps to a gate function below.
_GATED_TERMS = ("PD", "MSA", "MDS", "AES", "CNS")


class PrefilterResult:
    """Hits from the keyword layer. Every offset indexes the NORMALIZED text,
    which is the same length as the raw text, so offsets are interchangeable
    with the Q&A boundary and with verification offsets."""

    __slots__ = ("domain_hits", "bd_hits", "standalone_hits",
                 "high_signal_hits", "screen", "masked")

    def __init__(self, domain_hits, bd_hits, standalone_hits,
                 high_signal_hits, screen, masked):
        self.domain_hits = domain_hits
        self.bd_hits = bd_hits
        self.standalone_hits = standalone_hits
        self.high_signal_hits = high_signal_hits
        self.screen = screen
        self.masked = masked

    def as_summary(self) -> dict:
        """-> a small JSON-safe dict for the status file and the digest."""
        return {
            "screen": self.screen,
            "domain_terms": sorted({t for t, _ in self.domain_hits}),
            "bd_terms": sorted({t for t, _ in self.bd_hits}),
            "standalone_terms": sorted({t for t, _ in self.standalone_hits}),
            "high_signal_terms": sorted({t for t, _ in self.high_signal_hits}),
        }


def _near(offset: int, others, window: int) -> bool:
    """-> True when any offset in `others` is within `window` characters."""
    return any(abs(offset - other) <= window for other in others)


def _gate_ok(term: str, masked: str, offset: int, parkinsons_offsets, window: int) -> bool:
    """-> True when a gated term's match should count, per the YAML's
    ambiguity_rules. Runs on the MASKED text, so any term also present in the
    exclusion list (PD-1, PD-L1, PK/PD) has already been neutralized -- the
    blocker regex below is defense in depth for the cases the mask misses,
    notably a bare 'pharmacodynamic', which is NOT an exclusion entry."""
    lo = max(0, offset - window)
    hi = min(len(masked), offset + window)
    context = masked[lo:hi]
    if term == "PD":
        if _PD_BLOCKERS.search(context):
            return False
        return _near(offset, parkinsons_offsets, window)
    if term in ("MSA", "MDS"):
        # master services agreement / myelodysplastic syndromes
        return bool(_NEURO_CUE.search(context))
    if term == "AES":
        # "adverse events, serious" unless an epilepsy context is nearby
        return bool(_EPILEPSY_CUE.search(context))
    if term == "CNS":
        # consumer-nutrition segment at a few issuers
        return not re.search(r"consumer (nutrition|health)", context, re.IGNORECASE)
    return True


def _collect(groups, masked, parkinsons_offsets, window):
    """-> [(term, offset)] for every match in `groups` that survives its gate."""
    hits = []
    for term, pattern in groups:
        gated = term in _GATED_TERMS
        for match in pattern.finditer(masked):
            offset = match.start()
            if gated and not _gate_ok(term, masked, offset, parkinsons_offsets, window):
                continue
            hits.append((term, offset))
    return hits


def prefilter(text: str, kw: dict = None, window: int = None) -> PrefilterResult:
    """-> PrefilterResult.

    Order matters:
      1. normalize (length-preserving)
      2. mask the safe harbor, then the exclusion list -- both equal-length, so
         offsets stay valid and an excluded phrase cannot yield a term hit
      3. match the domain groups, applying the ambiguity gates
      4. match BD terms, keeping only those with a domain term within `window`
      5. match the standalone groups
      6. match the high-signal rare terms for the independent recall check

    `screen` is True when the DOMAIN gate opened at all. That is the skip gate:
    a transcript with no domain term never reaches the model.
    """
    kw = kw or load_keywords()
    window = CNS_GATE_WINDOW if window is None else int(window)

    normalized = normalize_text(text)
    masked = mask_safe_harbor(normalized)
    masked = mask_spans(masked, [p for _, p in compile_group(kw, "exclusions")])

    parkinsons_offsets = [
        m.start()
        for _, pattern in compile_group(kw, PARKINSONS_GROUP)
        for m in pattern.finditer(masked)
    ]

    domain_groups = []
    for group in DOMAIN_GROUPS:
        domain_groups.extend(compile_group(kw, group))
    # A named CNS asset is inherently in-domain -- see the group-role comment.
    domain_groups.extend(compile_group(kw, ASSET_GROUP))
    domain_hits = _collect(domain_groups, masked, parkinsons_offsets, window)
    domain_offsets = [offset for _, offset in domain_hits]

    # Generic BD language appears on nearly every earnings call and is NOISE by
    # itself. It counts only alongside domain vocabulary. This is the single
    # most valuable thing the screen can find, and the single easiest thing to
    # flood the digest with if the gate is loose.
    bd_hits = [
        (term, offset)
        for term, offset in _collect(compile_group(kw, BD_GROUP), masked,
                                     parkinsons_offsets, window)
        if _near(offset, domain_offsets, window)
    ]

    standalone_groups = []
    for group in STANDALONE_GROUPS:
        standalone_groups.extend(compile_group(kw, group))
    standalone_hits = _collect(standalone_groups, masked, parkinsons_offsets, window)

    high_signal_hits = _collect(compile_group(kw, "high_signal_rare_terms"),
                                masked, parkinsons_offsets, window)

    return PrefilterResult(
        domain_hits=domain_hits,
        bd_hits=bd_hits,
        standalone_hits=standalone_hits,
        high_signal_hits=high_signal_hits,
        screen=bool(domain_hits),
        masked=masked,
    )
```

- [ ] **Step 9: Run to verify pass**

Run: `python -m pytest tests/test_cns_screen.py -v`
Expected: PASS (10 tests)

If `test_msa_mds_aes_ambiguity_gates` fails on the AES case, check whether `AES` is in `conference_mentions` (a STANDALONE group) rather than a domain group -- the assertion reads `standalone_hits` for that reason.

- [ ] **Step 10: Verify syntax and commit**

```bash
python -c "import ast; ast.parse(open('cns_screen.py').read()); print('OK')"
python -m ruff check cns_screen.py cns_fmp.py
git add cns_screen.py tests/test_cns_screen.py
git commit -m "feat(cns): keyword prefilter with proximity gating and ambiguity rules"
```

---

The remaining tasks (4 through 10) continue in the same shape. I will write them into this file next, in this order:

| Task | Deliverable |
|---|---|
| 4 | Q&A zone detection (position-banded, multi-signal) + `prepare_transcript` |
| 5 | Claude screening: JSON schema, fenced untrusted input, beta-fallback retry, `stop_reason` handling |
| 6 | Quote verification in canonical space + zone recomputation + unconfirmed high-signal recall check |
| 7 | State: transcript store, ledger with retryable gaps, atomic saves, run lock, status |
| 8 | Digest render + Graph send |
| 9 | `run_daily` orchestration and `run_season` wrap-up |
| 10 | `app.py` wiring (3 routes, 2 cron jobs), requirements, version bump, CLAUDE.md, deploy, Q2 2026 backfill |
