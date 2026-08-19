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


def test_new_watch_interval_days_zero_vs_none():
    # Explicit 0 ("chase same-day") is a valid business-day count and must
    # survive as 0, not be silently upgraded to the default -- only an
    # actual None ("caller did not specify") should fall back.
    kwargs = dict(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z",
        subject="Negev_28-Day dog tox", ask="status of the investigation",
        recipients=["salim.tamboli@vimta.com"],
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake",
    )
    w_zero = fue.new_watch(interval_days=0, **kwargs)
    assert w_zero["interval_days"] == 0
    w_none = fue.new_watch(interval_days=None, **kwargs)
    assert w_none["interval_days"] == fue.FOLLOWUP_DEFAULT_BUSINESS_DAYS


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


def test_trigger_and_media_regexes():
    assert fue._TRIGGER_RE.search("please follow up if no reply")
    assert fue._TRIGGER_RE.search("send a REMINDER on Thursday")
    assert fue._TRIGGER_RE.search("chase Vimta on both points")
    assert not fue._TRIGGER_RE.search("here are the meeting notes")
    assert fue._MEDIA_RE.search("watch https://youtu.be/abc123")
    assert not fue._MEDIA_RE.search("see https://vimta.com/about")


def test_parse_command_words():
    assert fue._parse_command("stop") == "cancel"
    assert fue._parse_command("Please CANCEL fw_1234abcd") == "cancel"
    assert fue._parse_command("done, they replied") == "cancel"
    assert fue._parse_command("resume chasing") == "resume"
    assert fue._parse_command("keep going") == "resume"
    assert fue._parse_command("thanks!") is None


def test_extract_json_fenced_and_bare():
    assert fue._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert fue._extract_json('noise {"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    assert fue._extract_json("no json here") is None


def test_parse_instruction_happy(monkeypatch):
    canned = json.dumps({
        "is_request": True,
        "thread_subject": "Negev_28-Day repeated dose toxicity study in dogs",
        "counterparties": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"],
        "asks": [
            {"ask": "status of the investigation",
             "recipients": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"],
             "days": 2, "date": None},
            {"ask": "summary report for the 28-day dog study",
             "recipients": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"],
             "days": 2, "date": None},
        ],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    out = fue.parse_instruction("FW: Negev_28-Day dog tox", "if Vimta does not reply within 2 days ...")
    assert out["is_request"] is True and len(out["asks"]) == 2


def test_parse_instruction_degrades_on_garbage(monkeypatch):
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: "NOT JSON")
    assert fue.parse_instruction("s", "b") == {"is_request": False}
    def _boom(p, m, max_tokens=2000, **kw):
        raise RuntimeError("api down")
    monkeypatch.setattr(ld, "_call_claude_text", _boom)
    assert fue.parse_instruction("s", "b") == {"is_request": False}


# Self-review additions below: real-behavior coverage the brief's tests did
# not exercise -- fenced+nested JSON (parse_instruction's actual shape), the
# asks-filter branch, and the days=0 contract called out in the Task 2 prompt.


def test_extract_json_fenced_nested():
    fenced = '```json\n{"is_request": true, "asks": [{"ask": "x"}, {"ask": "y"}]}\n```'
    assert fue._extract_json(fenced) == {
        "is_request": True,
        "asks": [{"ask": "x"}, {"ask": "y"}],
    }


def test_parse_command_none_body():
    assert fue._parse_command(None) is None


def test_parse_instruction_preserves_days_zero(monkeypatch):
    # Task 1 fixed new_watch to honor an explicit interval_days=0 (same-day
    # chase) instead of upgrading it to the default; parse_instruction must
    # not undo that with a truthiness check on the parsed "days".
    canned = json.dumps({
        "is_request": True,
        "thread_subject": "s",
        "counterparties": ["a@vimta.com"],
        "asks": [{"ask": "status", "recipients": [], "days": 0, "date": None}],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    out = fue.parse_instruction("s", "b")
    assert out["asks"][0]["days"] == 0


def test_parse_instruction_filters_blank_asks(monkeypatch):
    canned = json.dumps({
        "is_request": True,
        "thread_subject": "s",
        "counterparties": [],
        "asks": [{"ask": "  "}, {"ask": "status"}, {"ask": ""}],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    out = fue.parse_instruction("s", "b")
    assert [a["ask"] for a in out["asks"]] == ["status"]


def test_parse_instruction_all_blank_asks_degrades(monkeypatch):
    canned = json.dumps({"is_request": True, "asks": [{"ask": ""}, {"ask": "   "}]})
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    assert fue.parse_instruction("s", "b") == {"is_request": False}


# Review-round fixes below: regressions for two Important findings against
# the brief's verbatim regexes (gerund false-negative on _TRIGGER_RE, domain
# substring false-positive on _MEDIA_RE).


def test_trigger_re_covers_inflected_forms():
    # The original follow[\s-]?up alternative required "follow" immediately
    # adjacent to "up", so "following up" -- one of the most common real
    # phrasings of this exact request -- never gated in, and was not
    # rescued by the remind(er)?/chase alternatives either.
    for s in ["follow up", "follow-up", "followup",
              "following up", "followed up", "follows up"]:
        assert fue._TRIGGER_RE.search(s), s
    assert not fue._TRIGGER_RE.search("here are the meeting notes")


def test_media_re_requires_left_boundary():
    # The original pattern had no left boundary before the domain, so
    # "x.com/" matched INSIDE unrelated domains like "vertex.com/".
    for s in ["https://x.com/foo/status/123", "https://twitter.com/foo",
              "x.com/foo", "https://www.x.com/a",
              "watch https://youtu.be/abc123"]:
        assert fue._MEDIA_RE.search(s), s
    for s in ["https://vertex.com/tools", "https://convertex.com/pricing",
              "https://mytwitter.com/fake"]:
        assert not fue._MEDIA_RE.search(s), s
