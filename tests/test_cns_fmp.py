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
