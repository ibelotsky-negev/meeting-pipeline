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


def test_discover_from_feed_walks_past_page_zero(fake_fmp):
    """Three scripted pages: page 0 and page 1 each carry one in-universe row
    for a different symbol, page 2 is empty (the natural end of the feed).
    Both symbols must be found, proving the walk continues past page 0."""
    script, _ = fake_fmp
    script["earning-call-transcript-latest"] = [
        _Reply([{"symbol": "ABBV", "period": "Q2", "fiscalYear": 2026,
                 "date": "2026-07-31"}]),
        _Reply([{"symbol": "BIIB", "period": "Q2", "fiscalYear": 2026,
                 "date": "2026-07-29"}]),
        _Reply([]),
    ]
    found = cns_fmp.discover_from_feed({"ABBV": {}, "BIIB": {}},
                                       "2026-07-01", "2026-09-15")
    assert {f["symbol"] for f in found} == {"ABBV", "BIIB"}


def test_discover_from_feed_stops_at_max_pages(fake_fmp):
    """Same two-page script as above, but max_pages=1 must bound the walk to
    page 0 only -- the page-1 symbol is never reached."""
    script, _ = fake_fmp
    script["earning-call-transcript-latest"] = [
        _Reply([{"symbol": "ABBV", "period": "Q2", "fiscalYear": 2026,
                 "date": "2026-07-31"}]),
        _Reply([{"symbol": "BIIB", "period": "Q2", "fiscalYear": 2026,
                 "date": "2026-07-29"}]),
    ]
    found = cns_fmp.discover_from_feed({"ABBV": {}, "BIIB": {}},
                                       "2026-07-01", "2026-09-15", max_pages=1)
    assert [f["symbol"] for f in found] == ["ABBV"]


def test_discover_from_feed_dedupes_repeated_period(fake_fmp):
    script, _ = fake_fmp
    script["earning-call-transcript-latest"] = [
        {"symbol": "ABBV", "period": "Q2", "fiscalYear": 2026, "date": "2026-07-31"},
        {"symbol": "abbv", "quarter": 2, "fiscalYear": 2026, "date": "2026-07-31"},
    ]
    found = cns_fmp.discover_from_feed({"ABBV": {}}, "2026-07-01", "2026-09-15", max_pages=1)
    assert len(found) == 1


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
