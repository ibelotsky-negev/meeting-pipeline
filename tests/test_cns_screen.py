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
