"""Offline tests for followup_engine (silent-thread reminder drafts).

Reuse points are mocked in their HOME modules:
- Graph read/write  -> email_pipeline_sync.graph_get/graph_post/graph_patch
- Claude            -> learn_digest._call_claude_text
- Sara inbox reply  -> x_transcribe_email.send_threaded_reply
The autouse no_network fixture (conftest) fails any real HTTP.
"""
import json          # noqa: F401 -- consumed by later tasks' tests
from datetime import date

import pytest

import followup_engine as fue
import email_pipeline_sync as eps      # noqa: F401 -- consumed by later tasks' tests
import learn_digest as ld              # noqa: F401 -- consumed by later tasks' tests
import x_transcribe_email as xte       # noqa: F401 -- consumed by later tasks' tests
import config                          # noqa: F401 -- consumed by later tasks' tests


@pytest.fixture
def fue_files(monkeypatch, tmp_path):
    monkeypatch.setattr(fue, "REGISTRY_PATH", str(tmp_path / "followups.json"))
    monkeypatch.setattr(fue, "PROCESSED_PATH", str(tmp_path / "processed.json"))
    monkeypatch.setattr(fue, "STATUS_PATH", str(tmp_path / "status.json"))
    return tmp_path


def test_add_business_days_skips_weekend():
    # Wed Aug 5 2026 + 2 business days = Fri Aug 7 (the doc's example)
    assert fue.add_business_days(date(2026, 8, 5), 2) == date(2026, 8, 7)
    # Fri + 2 business days = Tue
    assert fue.add_business_days(date(2026, 8, 7), 2) == date(2026, 8, 11)
    assert fue.add_business_days(date(2026, 8, 5), 0) == date(2026, 8, 5)


def test_registry_roundtrip(fue_files):
    reg = fue._load_registry()
    assert reg == {"watches": []}
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z",
        subject="Negev_28-Day dog tox", ask="status of the investigation",
        recipients=["salim.tamboli@vimta.com"], interval_days=2,
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake",
    )
    assert w["id"].startswith("fw_") and len(w["id"]) == 11
    assert w["status"] == "active" and w["nudges_sent"] == 0
    assert w["deadline"] == "2026-08-07"
    reg["watches"].append(w)
    fue._save_registry(reg)
    again = fue._load_registry()
    assert again["watches"][0]["ask"] == "status of the investigation"


def test_registry_corrupt_file_degrades_empty(fue_files, tmp_path):
    (tmp_path / "followups.json").write_text("{not json", encoding="utf-8")
    assert fue._load_registry() == {"watches": []}


def test_processed_persist_and_dry_run(fue_files):
    fue._persist_processed({"a", "b"})
    assert fue._load_processed() == {"a", "b"}
    fue._persist_processed({"a", "b", "c"}, dry_run=True)
    assert fue._load_processed() == {"a", "b"}


def test_live_gate_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    assert fue._live() is False
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    assert fue._live() is True
