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


def _record_period(found: list, seen: set, symbol: str, row: dict, call_date: str) -> bool:
    """Normalize one discovery row and append it if the period is new.

    -> True when appended. Both discovery paths share this so the emitted
    dict shape and the dedup key are defined in exactly one place."""
    parsed = normalize_period(row)
    if not parsed:
        return False
    fiscal_year, quarter = parsed
    key = period_key(symbol, fiscal_year, quarter)
    if key in seen:
        return False
    seen.add(key)
    found.append({"symbol": symbol, "fiscal_year": fiscal_year,
                  "quarter": quarter, "date": call_date})
    return True


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
            _record_period(found, seen, symbol, row, call_date)
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
            _record_period(found, seen, symbol, row, call_date)
    return found


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
