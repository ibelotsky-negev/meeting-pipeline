"""Offline tests for the CNS screen. Fixtures under tests/fixtures/cns/ are
trimmed excerpts of real FMP transcripts captured 2026-09-09."""
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


def test_mask_safe_harbor_preserves_length_and_removes_the_cue():
    prefix = "Good morning everyone. This call contains "
    # a single cue phrase only -- "private securities litigation" is also a
    # cue and would trigger a second, independent masked span if included
    cue = "forward-looking statements within the meaning of applicable law."
    suffix = " Now let's discuss our results."
    text = prefix + cue + suffix
    # explicit span sized to the cue only, so the mask does not run past it
    # into the trailing sentence, keeping the assertions below meaningful
    masked = cns_screen.mask_safe_harbor(text, span=len(cue))
    assert len(masked) == len(text)
    assert "forward-looking statements" not in masked
    assert "Good morning everyone." in masked
    assert "Now let's discuss our results." in masked


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
    # MDS is myelodysplastic syndromes in heme-onc; it lives in
    # conference_mentions (a standalone group), so assert there -- a
    # domain_hits assertion would pass vacuously and test nothing.
    assert not any(t == "MDS" for t, _ in
                   cns_screen.prefilter("Our MDS franchise in heme-onc grew.").standalone_hits)
    assert any(t == "MDS" for t, _ in cns_screen.prefilter(
        "We will present Parkinson's data at MDS this year.").standalone_hits)
    # AES needs epilepsy/seizure context
    assert not any(t == "AES" for t, _ in cns_screen.prefilter(
        "There were no treatment-related AEs or AES in the study.").standalone_hits)
    assert any(t == "AES" for t, _ in cns_screen.prefilter(
        "We will present seizure-freedom data at AES this December.").standalone_hits)


def test_cns_gate_blocks_the_consumer_health_exclusion_phrasing():
    """Regression guard for the CNS gate reading the wrong string. 'Consumer
    Health' is an exclusion-list entry, so masking erases it before the gate
    ever runs -- if the gate reads the MASKED text (the bug), it finds no cue
    and wrongly accepts CNS. The gate must read the UNMASKED normalized text
    instead, where 'Consumer Health' is still present to block it."""
    text = "Our Consumer Health segment reported CNS sales growth this quarter."
    res = cns_screen.prefilter(text)
    assert not any(t == "CNS" for t, _ in res.domain_hits)


def test_cns_gate_blocks_the_consumer_nutrition_phrasing():
    text = "Our Consumer Nutrition segment reported CNS sales growth this quarter."
    res = cns_screen.prefilter(text)
    assert not any(t == "CNS" for t, _ in res.domain_hits)


def test_cns_gate_accepts_ordinary_pharma_context():
    text = "Our CNS pipeline advanced with a new Phase 2 readout in epilepsy."
    res = cns_screen.prefilter(text)
    assert any(t == "CNS" for t, _ in res.domain_hits)


def test_prefilter_collects_high_signal_terms_with_offsets():
    text = "We are studying apathy in Parkinson's disease with a 5-HT2C agonist."
    res = cns_screen.prefilter(text)
    terms = {t.lower() for t, _ in res.high_signal_hits}
    assert "apathy" in terms
    assert "5-ht2c" in terms
    for _, offset in res.high_signal_hits:
        assert 0 <= offset < len(text)
