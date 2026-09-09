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
