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
    # Alector (ALEC) is the case that proved the criterion: still actively
    # trading (isActivelyTrading=True, ~$255M) but sparse FMP coverage
    # (last transcript 2025-08-07), so it belongs in force_include to remain
    # visible, not in entity_only. Regression test: do not silently move it
    # back to entity_only without re-verifying its trading status.
    assert "ALEC" in cfg["force_include"]
    assert not any(t.upper() == "ALEC" for t in cfg["entity_only"])
    # an acquired company or inactive trader must never end up in the fetch list
    assert not any(t.upper() in ("CERE", "KRTX", "ITCI", "SAGE")
                   for t in cfg["force_include"])


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
