# Tests for the mail DELIVERY map (config.delivery_address / delivery_addresses).
#
# After the Negev -> Palomar rename, identity stays canonical (@negevlabs.com)
# but the team reads @palomar-labs.com. Every Sara email used to go to the
# canonical address, i.e. the old mailbox. These pin the mapping and that the
# send sites apply it. Offline only: Graph is monkeypatched.
import config
import daily_pipeline_digest as dpd
import email_pipeline_sync as eps
import followup_engine as fue
import fyi_triage as ft
import learn_digest as ld


def test_canonical_team_address_delivers_to_palomar():
    assert config.delivery_address("bk@negevlabs.com") == "ken@palomar-labs.com"
    assert config.delivery_address("shlomi@negevlabs.com") == "shlomi@palomar-labs.com"
    assert config.delivery_address("dan@negevlabs.com") == "dan@palomar-labs.com"
    assert config.delivery_address("ka@negevlabs.com") == "kostia@palomar-labs.com"


def test_alias_resolves_through_identity_then_delivery():
    # An older alias reaches the person's CURRENT mailbox, not the canonical one.
    assert config.delivery_address("bk@ariadnebio.com") == "ken@palomar-labs.com"
    assert config.delivery_address("kostia@negevcap.com") == "kostia@palomar-labs.com"
    assert config.delivery_address(" BK@NegevLabs.com ") == "ken@palomar-labs.com"


def test_palomar_and_external_addresses_pass_through():
    assert config.delivery_address("ken@palomar-labs.com") == "ken@palomar-labs.com"
    assert config.delivery_address("vu@negevcap.com") == "vu@negevcap.com"
    assert config.delivery_address("investor@fund.com") == "investor@fund.com"
    assert config.delivery_address("") == ""


def test_identity_normalization_is_unchanged():
    # The delivery map must never leak into identity lookups.
    assert config.normalize_team_email("ken@palomar-labs.com") == "bk@negevlabs.com"


def test_delivery_addresses_dedupes_and_accepts_a_string():
    assert config.delivery_addresses(
        ["bk@negevlabs.com", "ken@palomar-labs.com", "", "vu@negevcap.com"]
    ) == ["ken@palomar-labs.com", "vu@negevcap.com"]
    assert config.delivery_addresses("bk@negevlabs.com") == ["ken@palomar-labs.com"]
    assert config.delivery_addresses(None) == []


def _capture_post(monkeypatch):
    calls = []
    monkeypatch.setattr(eps, "graph_post", lambda url, body, *a, **k: calls.append(body) or {})
    monkeypatch.setenv("BOT_SENDER_EMAIL", "sara@palomar-labs.com")
    return calls


def _to(body):
    return [r["emailAddress"]["address"] for r in body["message"]["toRecipients"]]


def test_fyi_summary_goes_to_palomar(monkeypatch):
    calls = _capture_post(monkeypatch)
    monkeypatch.setattr(ft, "FYI_RECIPIENTS", ["bk@negevlabs.com"])
    ft.send_summary_email("s", "b")
    assert _to(calls[0]) == ["ken@palomar-labs.com"]


def test_learn_digest_goes_to_palomar(monkeypatch):
    calls = _capture_post(monkeypatch)
    monkeypatch.setattr(ld, "LEARN_RECIPIENTS", ["bk@negevlabs.com"])
    ld.send_digest_email("s", "b")
    assert _to(calls[0]) == ["ken@palomar-labs.com"]


def test_pipeline_digest_maps_to_and_cc(monkeypatch):
    calls = _capture_post(monkeypatch)
    dpd.send_digest_email("s", "b", recipients=["bk@negevlabs.com", "shlomi@negevlabs.com"],
                          cc=["dan@negevlabs.com"])
    body = calls[0]
    assert _to(body) == ["ken@palomar-labs.com", "shlomi@palomar-labs.com"]
    assert [r["emailAddress"]["address"] for r in body["message"]["ccRecipients"]] == [
        "dan@palomar-labs.com"]


def test_followup_report_goes_to_palomar(monkeypatch):
    calls = _capture_post(monkeypatch)
    fue._send_email(["bk@negevlabs.com"], "s", "b")
    assert _to(calls[0]) == ["ken@palomar-labs.com"]


def test_mailboxes_read_from_palomar_by_default():
    assert ft.MAILBOX == "ken@palomar-labs.com"
    assert ld.MAILBOX == "ken@palomar-labs.com"
