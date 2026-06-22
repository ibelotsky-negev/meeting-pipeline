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
