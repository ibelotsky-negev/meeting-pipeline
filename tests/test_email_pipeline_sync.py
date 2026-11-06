# Offline tests for email_pipeline_sync: alias indexing, dedupe matching,
# ledger idempotency, and partial-write handling. No network (see conftest).
import pytest

import email_pipeline_sync as eps


@pytest.fixture(autouse=True)
def clean_engagement_cache():
    eps._contact_engagements_cache.clear()
    yield
    eps._contact_engagements_cache.clear()


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(eps, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    conn = eps.ledger_connect()
    yield conn
    conn.close()


def _msg(**overrides):
    base = {
        "internet_message_id": "<msg1@example.com>",
        "graph_id": "g1",
        "mailbox": "bk@negevlabs.com",
        "subject": "Tetrad VC / Negev Labs",
        "from": "anna@tetrad.vc",
        "participants": {"anna@tetrad.vc", "bk@negevlabs.com"},
        "received": "2026-05-08T15:00:21Z",
        "body_text": "KYC documents attached",
        "matched_contacts": {
            "anna@tetrad.vc": {
                "contact_id": "c1",
                "name": "Anna",
                "deals": [{"id": "d1", "name": "Tetrad VC - NL 2026", "stage_label": "Closing"}],
            }
        },
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
#  contact_email_addresses (alias indexing)
# ----------------------------------------------------------------------

def test_contact_email_addresses_primary_only():
    assert eps.contact_email_addresses({"email": "A@Fund.com"}) == ["a@fund.com"]


def test_contact_email_addresses_includes_additional():
    props = {"email": "a@fund.com", "hs_additional_emails": "B@Gmail.com; a@fund.com ;c@alias.io"}
    assert eps.contact_email_addresses(props) == ["a@fund.com", "b@gmail.com", "c@alias.io"]


def test_contact_email_addresses_no_primary():
    props = {"email": "", "hs_additional_emails": "only@alias.io"}
    assert eps.contact_email_addresses(props) == ["only@alias.io"]


def test_contact_email_addresses_empty():
    assert eps.contact_email_addresses({}) == []
    assert eps.contact_email_addresses({"email": None, "hs_additional_emails": None}) == []


# ----------------------------------------------------------------------
#  find_logged_engagement (dedupe)
# ----------------------------------------------------------------------

def test_find_logged_engagement_by_message_id():
    eps._contact_engagements_cache["c1"] = [
        {"id": "111", "message_id": "<msg1@example.com>", "subject": "x", "ts_ms": 0, "from": ""},
    ]
    assert eps.find_logged_engagement(_msg(), "c1") == "111"


def _msg_ts_ms():
    from datetime import datetime
    return int(datetime.fromisoformat("2026-05-08T15:00:21+00:00").timestamp() * 1000)


def test_find_logged_engagement_fallback_subject_time_sender():
    # Engagement 60s after the message, no message id -> fallback match
    msg_ts = _msg_ts_ms()
    eps._contact_engagements_cache["c1"] = [
        {"id": "222", "message_id": "", "subject": "tetrad vc / negev labs",
         "ts_ms": msg_ts + 60_000, "from": "anna@tetrad.vc"},
    ]
    assert eps.find_logged_engagement(_msg(), "c1") == "222"


def test_find_logged_engagement_fallback_rejects_wrong_sender():
    msg_ts = _msg_ts_ms()
    eps._contact_engagements_cache["c1"] = [
        {"id": "222", "message_id": "", "subject": "tetrad vc / negev labs",
         "ts_ms": msg_ts + 60_000, "from": "someone-else@other.com"},
    ]
    assert eps.find_logged_engagement(_msg(), "c1") == ""


def test_find_logged_engagement_fallback_rejects_outside_window():
    msg_ts = _msg_ts_ms()
    eps._contact_engagements_cache["c1"] = [
        {"id": "222", "message_id": "", "subject": "tetrad vc / negev labs",
         "ts_ms": msg_ts + 300_000, "from": "anna@tetrad.vc"},
    ]
    assert eps.find_logged_engagement(_msg(), "c1") == ""


def test_find_logged_engagement_no_match():
    eps._contact_engagements_cache["c1"] = []
    assert eps.find_logged_engagement(_msg(), "c1") == ""


# ----------------------------------------------------------------------
#  Run ledger (idempotency + partial-write state)
# ----------------------------------------------------------------------

def test_ledger_roundtrip_and_dry_run_noop(ledger):
    msg = _msg()
    assert eps.ledger_get(ledger, msg["internet_message_id"]) is None

    eps.ledger_record(ledger, msg, "logged", "ok", "run1", dry_run=True)
    assert eps.ledger_get(ledger, msg["internet_message_id"]) is None

    eps.ledger_record(ledger, msg, "logged", "engagement=42; ok", "run1", dry_run=False)
    outcome, detail = eps.ledger_get(ledger, msg["internet_message_id"])
    assert outcome == "logged"
    assert "engagement=42" in detail


def test_ledger_update_outcome_partial_to_logged(ledger):
    msg = _msg()
    eps.ledger_record(ledger, msg, "logged-partial", "engagement=42; failed=deals:d1", "run1")
    eps.ledger_update_outcome(ledger, msg["internet_message_id"], "logged",
                              "engagement=42; associations repaired")
    outcome, detail = eps.ledger_get(ledger, msg["internet_message_id"])
    assert outcome == "logged"
    assert "repaired" in detail


# ----------------------------------------------------------------------
#  Partial-write handling in the logger
# ----------------------------------------------------------------------

def test_log_email_returns_failed_associations(monkeypatch):
    monkeypatch.setattr(eps, "hubspot_request",
                        lambda method, endpoint, data=None, params=None: {"id": "999"})
    monkeypatch.setattr(eps, "get_associated_ids", lambda *a: ["comp1"])

    def fail_deals(email_id, to_type, to_id):
        if to_type == "deals":
            raise RuntimeError("HubSpot 500")

    monkeypatch.setattr(eps, "create_default_association", fail_deals)
    email_id, failed = eps.log_email_to_hubspot(_msg())
    assert email_id == "999"
    assert failed == ["deals:d1"]


def test_run_daily_window_and_flags(monkeypatch):
    captured = {}

    def fake_run_sync(since, until, mailboxes, dry_run, send_report):
        captured.update(since=since, until=until, mailboxes=mailboxes,
                        dry_run=dry_run, send_report=send_report)
        return {"logged": 1}

    monkeypatch.setattr(eps, "run_sync", fake_run_sync)
    result = eps.run_daily(lookback_days=3)

    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).date()
    # Scheduler contract: real write, report on, rolling lookback window
    assert captured["dry_run"] is False
    assert captured["send_report"] is True
    assert captured["mailboxes"] == eps.DEFAULT_MAILBOXES
    assert captured["since"] == (today - timedelta(days=3)).isoformat()
    assert captured["until"] == (today + timedelta(days=1)).isoformat()
    assert result == {"logged": 1}


def test_log_email_all_associations_ok(monkeypatch):
    monkeypatch.setattr(eps, "hubspot_request",
                        lambda method, endpoint, data=None, params=None: {"id": "999"})
    monkeypatch.setattr(eps, "get_associated_ids", lambda *a: ["comp1"])
    calls = []
    monkeypatch.setattr(eps, "create_default_association",
                        lambda email_id, to_type, to_id: calls.append((to_type, to_id)))
    email_id, failed = eps.log_email_to_hubspot(_msg())
    assert email_id == "999"
    assert failed == []
    assert ("contacts", "c1") in calls
    assert ("deals", "d1") in calls
    assert ("companies", "comp1") in calls


# ----------------------------------------------------------------------
#  Two-pipeline routing (Palomar vs Ariadne fundraise)
# ----------------------------------------------------------------------

PALOMAR_DEAL = {"id": "dp", "name": "Tetrad - PL 2027", "stage_label": "Discovery", "pipeline": "palomar"}
ARIADNE_DEAL = {"id": "da", "name": "Tetrad - Ariadne 2027", "stage_label": "Closing", "pipeline": "ariadne"}


def _two_pipeline_msg(deals=None):
    msg = _msg()
    msg["matched_contacts"]["anna@tetrad.vc"]["deals"] = (
        [dict(PALOMAR_DEAL), dict(ARIADNE_DEAL)] if deals is None else deals)
    return msg


def test_pipelines_configured_with_ids():
    by_key = {p["key"]: p["id"] for p in eps.PIPELINES}
    assert by_key == {"palomar": "4252440818", "ariadne": "4283794618"}


def test_parse_classification_routes_to_named_fundraise():
    msg = _two_pipeline_msg()
    text = '{"classification": "DEAL_RELEVANT", "fundraise": "ariadne", "reason": "Ariadne SAFE"}'
    assert eps.parse_classification(text, msg) == ("DEAL_RELEVANT", "Ariadne SAFE", "ariadne")
    text = 'Sure: {"classification": "DEAL_RELEVANT", "fundraise": "Palomar", "reason": "UPA"}'
    assert eps.parse_classification(text, msg) == ("DEAL_RELEVANT", "UPA", "palomar")


def test_parse_classification_missing_fundraise_single_pipeline_infers_it():
    msg = _two_pipeline_msg(deals=[dict(ARIADNE_DEAL)])
    text = '{"classification": "DEAL_RELEVANT", "fundraise": null, "reason": "wire"}'
    assert eps.parse_classification(text, msg) == ("DEAL_RELEVANT", "wire", "ariadne")


def test_parse_classification_missing_fundraise_two_pipelines_is_uncertain():
    msg = _two_pipeline_msg()
    text = '{"classification": "DEAL_RELEVANT", "reason": "wire"}'
    label, reason, pipeline = eps.parse_classification(text, msg)
    assert (label, pipeline) == ("UNCERTAIN", None)
    assert "fundraise not identified" in reason


def test_parse_classification_fundraise_without_deal_is_uncertain():
    """An Ariadne email to a contact who only has a Palomar deal is never
    logged onto the Palomar deal -- it goes to human review."""
    msg = _two_pipeline_msg(deals=[dict(PALOMAR_DEAL)])
    text = '{"classification": "DEAL_RELEVANT", "fundraise": "ariadne", "reason": "Ariadne terms"}'
    label, reason, pipeline = eps.parse_classification(text, msg)
    assert (label, pipeline) == ("UNCERTAIN", None)
    assert "Ariadne 2027 Fundraise" in reason


def test_parse_classification_legacy_deal_without_pipeline_is_palomar():
    msg = _msg()  # deal dict carries no "pipeline" key
    text = '{"classification": "DEAL_RELEVANT", "reason": "KYC"}'
    assert eps.parse_classification(text, msg) == ("DEAL_RELEVANT", "KYC", "palomar")


@pytest.mark.parametrize("text,expected_label,expected_reason", [
    ('{"classification": "NOT_RELEVANT", "fundraise": "ariadne", "reason": "personal"}',
     "NOT_RELEVANT", "personal"),
    ("no json here", "UNCERTAIN", None),
    ('{"classification": "MAYBE", "reason": "x"}', "UNCERTAIN", None),
    ('["DEAL_RELEVANT"]', "UNCERTAIN", None),
])
def test_parse_classification_non_routed_outcomes(text, expected_label, expected_reason):
    label, reason, pipeline = eps.parse_classification(text, _two_pipeline_msg())
    assert label == expected_label
    assert pipeline is None
    if expected_reason is not None:
        assert reason == expected_reason


def test_associations_restricted_to_routed_pipeline(monkeypatch):
    monkeypatch.setattr(eps, "get_associated_ids", lambda *a: ["comp1"])
    calls = []
    monkeypatch.setattr(eps, "create_default_association",
                        lambda email_id, to_type, to_id: calls.append((to_type, to_id)))
    failed = eps.ensure_engagement_associations("999", _two_pipeline_msg(), "ariadne")
    assert failed == []
    assert ("deals", "da") in calls
    assert ("deals", "dp") not in calls
    assert ("contacts", "c1") in calls and ("companies", "comp1") in calls


def test_associations_unrouted_links_every_deal(monkeypatch):
    monkeypatch.setattr(eps, "get_associated_ids", lambda *a: [])
    calls = []
    monkeypatch.setattr(eps, "create_default_association",
                        lambda email_id, to_type, to_id: calls.append((to_type, to_id)))
    eps.ensure_engagement_associations("999", _two_pipeline_msg(), None)
    assert {("deals", "da"), ("deals", "dp")} <= set(calls)


def test_detail_pipeline_roundtrip():
    assert eps.detail_pipeline("engagement=42; pipeline=ariadne; failed=deals:da") == "ariadne"
    assert eps.detail_pipeline("engagement=42; failed=deals:d1") is None
    assert eps.detail_pipeline("engagement=42; pipeline=bogus; x") is None


def test_build_roster_tags_deals_with_pipeline(monkeypatch):
    deals_by_pipeline = {
        "4252440818": [{"id": "dp", "properties": {"dealname": "P deal", "dealstage": "s1"}}],
        "4283794618": [{"id": "da", "properties": {"dealname": "A deal", "dealstage": "s9"}}],
    }
    monkeypatch.setattr(eps, "get_stage_labels", lambda pid=None: {"s1": "Discovery", "s9": "Closing"})
    monkeypatch.setattr(eps, "get_pipeline_deals", lambda pid=None: deals_by_pipeline[pid])
    monkeypatch.setattr(eps, "get_associated_ids", lambda f, i, t: ["c1"])
    monkeypatch.setattr(eps, "batch_read_contacts", lambda ids: [
        {"id": "c1", "properties": {"email": "anna@tetrad.vc", "firstname": "Anna"}}])
    roster = eps.build_roster()
    deals = {d["id"]: d for d in roster["anna@tetrad.vc"]["deals"]}
    assert deals["dp"]["pipeline"] == "palomar"
    assert deals["da"]["pipeline"] == "ariadne"
    assert deals["da"]["stage_label"] == "Closing"


def _stub_run(monkeypatch, msg, classification):
    entry = msg["matched_contacts"]["anna@tetrad.vc"]
    monkeypatch.setattr(eps, "build_roster", lambda: {"anna@tetrad.vc": entry})
    monkeypatch.setattr(eps, "scan_mailbox", lambda *a: {msg["internet_message_id"]: msg})
    monkeypatch.setattr(eps, "find_logged_engagement", lambda m, cid: "")
    monkeypatch.setattr(eps, "classify_email", lambda m: classification)
    logged = []
    monkeypatch.setattr(eps, "log_email_to_hubspot",
                        lambda m, pipeline_key=None: logged.append(pipeline_key) or ("777", []))
    return logged


def test_run_sync_logs_to_routed_pipeline(ledger, monkeypatch):
    msg = _two_pipeline_msg()
    logged = _stub_run(monkeypatch, msg, ("DEAL_RELEVANT", "Ariadne SAFE", "ariadne"))
    stats = eps.run_sync("2026-11-01", "2026-11-07", ["bk@negevlabs.com"],
                         dry_run=False, send_report=False)
    assert logged == ["ariadne"]
    assert stats["logged"] == 1
    assert stats["logged_by_pipeline"] == {"ariadne": 1}
    outcome, detail = eps.ledger_get(ledger, msg["internet_message_id"])
    assert outcome == "logged" and "pipeline=ariadne" in detail


def test_run_sync_partial_retry_keeps_routed_pipeline(ledger, monkeypatch):
    msg = _two_pipeline_msg()
    _stub_run(monkeypatch, msg, ("DEAL_RELEVANT", "x", "ariadne"))
    eps.ledger_record(ledger, msg, "logged-partial",
                      "engagement=777; pipeline=ariadne; failed=deals:da", "run0")
    seen = []
    monkeypatch.setattr(eps, "ensure_engagement_associations",
                        lambda eid, m, pipeline_key=None: seen.append((eid, pipeline_key)) or [])
    stats = eps.run_sync("2026-11-01", "2026-11-07", ["bk@negevlabs.com"],
                         dry_run=False, send_report=False)
    assert seen == [("777", "ariadne")]
    assert stats["repaired"] == 1


def test_build_report_splits_logged_by_pipeline():
    stats = {"scanned": 3, "logged": 3, "duplicates": 0, "irrelevant": 0, "uncertain": 0,
             "errors": 0, "logged_by_pipeline": {"palomar": 2, "ariadne": 1}}
    report = eps.build_report(stats, [], [], [], "w", False)
    assert "Logged:     3 (Palomar 2, Ariadne 1)" in report
    assert "4283794618" in report and "4252440818" in report
