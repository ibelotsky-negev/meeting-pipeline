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
