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


def _graph_msg(mid, cid, subject, sender, received, to=None, cc=None):
    return {
        "id": mid, "conversationId": cid, "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": received,
        "toRecipients": [{"emailAddress": {"address": a}} for a in (to or [])],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
    }


def test_normalize_subject_strips_prefixes():
    assert fue._normalize_subject("FW: RE: Fwd: Dog tox study") == "Dog tox study"
    assert fue._normalize_subject("  Re:Re: x  ") == "x"


def test_resolve_thread_picks_counterparty_conversation(monkeypatch):
    wrong = _graph_msg("m9", "cid-wrong", "Dog tox study invoice",
                       "billing@other.com", "2026-08-04T10:00:00Z")
    older = _graph_msg("m1", "cid-right", "Dog tox study",
                       "upendra.kumar@adgyllifesciences.com", "2026-08-01T10:00:00Z",
                       to=["salim.tamboli@vimta.com"], cc=["dan@negevlabs.com"])
    newest = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                        "dan@negevlabs.com", "2026-08-05T07:23:00Z",
                        to=["salim.tamboli@vimta.com"], cc=["habibur.khan@vimta.in"])
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: {"value": [wrong, older, newest]})
    out = fue.resolve_thread("dan@negevlabs.com", "FW: Dog tox study",
                             ["salim.tamboli@vimta.com"])
    assert out["conversation_id"] == "cid-right"
    assert out["anchor_id"] == "m2"
    assert out["anchor_received"] == "2026-08-05T07:23:00Z"
    assert "habibur.khan@vimta.in" in out["participants"]


def test_resolve_thread_none_on_no_results(monkeypatch):
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    assert fue.resolve_thread("dan@negevlabs.com", "Nothing", []) is None
    assert fue.resolve_thread("dan@negevlabs.com", "", []) is None


# Self-review additions below: the brief's own tests would still pass even
# if the scoring ignored counterparty overlap entirely (its "wrong"
# candidate is also older than "right"), or if the anchor were picked
# positionally instead of by actual receivedDateTime. These pin the two
# cases where getting either one wrong means drafting a chase into the
# wrong conversation, plus the explicit refusal branch and the null-safety
# contract on Graph payloads.


def test_resolve_thread_counterparty_match_outranks_recency(monkeypatch):
    # A conversation with NO counterparty overlap but a MORE RECENT message
    # must still lose to an older conversation that actually involves the
    # named counterparty -- recency alone must never override identity.
    wrong_recent = _graph_msg("m9", "cid-wrong", "Dog tox study",
                              "billing@other.com", "2026-08-09T10:00:00Z")
    right_older = _graph_msg("m1", "cid-right", "Dog tox study",
                             "upendra.kumar@adgyllifesciences.com",
                             "2026-08-01T10:00:00Z",
                             to=["salim.tamboli@vimta.com"])
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: {"value": [wrong_recent, right_older]})
    out = fue.resolve_thread("dan@negevlabs.com", "Dog tox study",
                             ["salim.tamboli@vimta.com"])
    assert out["conversation_id"] == "cid-right"
    assert out["anchor_id"] == "m1"


def test_resolve_thread_anchor_is_true_newest_regardless_of_list_order(monkeypatch):
    # Graph's $search result order is not guaranteed chronological. The true
    # newest message sits in the MIDDLE of the returned list -- neither the
    # first nor the last element -- so a positional shortcut (first/last
    # appended) would pick the wrong anchor and still "look" plausible.
    newest = _graph_msg("m3", "cid-right", "RE: Dog tox study",
                        "dan@negevlabs.com", "2026-08-05T07:23:00Z",
                        to=["salim.tamboli@vimta.com"])
    middle = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                        "salim.tamboli@vimta.com", "2026-08-03T09:00:00Z")
    oldest = _graph_msg("m1", "cid-right", "Dog tox study",
                        "upendra.kumar@adgyllifesciences.com", "2026-08-01T10:00:00Z",
                        to=["salim.tamboli@vimta.com"])
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: {"value": [middle, newest, oldest]})
    out = fue.resolve_thread("dan@negevlabs.com", "Dog tox study",
                             ["salim.tamboli@vimta.com"])
    assert out["anchor_id"] == "m3"
    assert out["anchor_received"] == "2026-08-05T07:23:00Z"


def test_resolve_thread_refuses_when_counterparty_never_matches(monkeypatch):
    # Messages are found and grouped into real conversations, but NONE of
    # them include the named counterparty -- resolve_thread must refuse
    # (return None) rather than silently hand back its best-scoring wrong
    # guess, per the spec's "unresolvable thread -> honest failure, never
    # silence".
    a = _graph_msg("m1", "cid-a", "Dog tox study", "someone@else.com",
                   "2026-08-01T10:00:00Z")
    b = _graph_msg("m2", "cid-b", "RE: Dog tox study", "another@else.com",
                   "2026-08-05T07:23:00Z")
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: {"value": [a, b]})
    out = fue.resolve_thread("dan@negevlabs.com", "Dog tox study",
                             ["salim.tamboli@vimta.com"])
    assert out is None


def test_resolve_thread_searches_normalized_subject(monkeypatch):
    # The query sent to Graph must be the NORMALIZED subject (FW:/RE:
    # stripped), not the raw hint -- otherwise "resolution is by normalized
    # subject search" is decorative rather than real.
    captured = {}

    def _fake_graph_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _fake_graph_get)
    fue.resolve_thread("dan@negevlabs.com", "FW: RE: Dog tox study", ["x@vimta.com"])
    assert "dan@negevlabs.com" in captured["url"]
    assert captured["params"]["$search"] == '"subject:Dog tox study"'


def test_participants_handles_graph_nulls():
    # Graph payloads routinely carry explicit nulls (not missing keys) for
    # from/to/cc and their emailAddress sub-objects -- every hop must
    # degrade to empty rather than raising.
    msg = {
        "from": None,
        "toRecipients": None,
        "ccRecipients": [{"emailAddress": None}, None,
                          {"emailAddress": {"address": "Salim@Vimta.com"}}],
    }
    assert fue._participants(msg) == {"salim@vimta.com"}
    assert fue._participants({}) == set()


# Review-round fixes below: regressions for two Important findings inherited
# verbatim from the brief's Step 3 code (thread resolution).


def test_resolve_thread_no_usable_conversation_id_returns_none(monkeypatch):
    # Every message in a non-empty Graph response lacks a usable
    # conversationId (blank string, and the key missing entirely) -- convs
    # ends up empty, and max() on an empty sequence must not raise;
    # resolve_thread must refuse (None) instead of crashing the caller.
    no_cid_blank = _graph_msg("m1", "", "Dog tox study", "a@b.com",
                              "2026-08-01T10:00:00Z")
    no_cid_missing = _graph_msg("m2", "cid-x", "RE: Dog tox study", "c@d.com",
                                "2026-08-02T10:00:00Z")
    del no_cid_missing["conversationId"]
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: {"value": [no_cid_blank, no_cid_missing]})
    assert fue.resolve_thread("dan@negevlabs.com", "Dog tox study", []) is None


def test_normalize_subject_strips_leading_tags_interleaved_with_prefixes():
    # A leading tenant/gateway tag (e.g. Exchange's "[EXTERNAL]") must not
    # block the Re:/Fw:/Fwd: strip, in either relative order, and stacked
    # re/fw/fwd prefixes must still fully collapse.
    plain = fue._normalize_subject("Dog tox study")
    assert plain == "Dog tox study"
    for s in ["RE: FW: Dog tox study",
              "[EXTERNAL] FW: Dog tox study",
              "[EXTERNAL] RE: FW: Dog tox study",
              "RE: [EXTERNAL] FW: Dog tox study"]:
        assert fue._normalize_subject(s) == plain, s


def test_normalize_subject_keeps_mid_string_bracket():
    # Only a LEADING bracketed tag is noise; a bracket appearing after real
    # subject text must survive untouched.
    assert fue._normalize_subject("RE: mid [EXTERNAL] string") == "mid [EXTERNAL] string"


def _intake_msg(mid, sender, subject, body, cid="conv-intake-1"):
    return {
        "id": mid, "internetMessageId": mid, "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": "2026-08-18T08:00:00Z",
        "conversationId": cid,
        "uniqueBody": {"content": f"<html><body>{body}</body></html>"},
        "internetMessageHeaders": [],
    }


@pytest.fixture
def intake_world(monkeypatch, fue_files):
    """Sara inbox has one forward-with-instruction; owner mailbox search
    resolves one conversation. Claude parse returns two asks. Captures the
    confirmation reply."""
    instruction = _intake_msg(
        "im1", "dan@negevlabs.com", "FW: Dog tox study",
        "Sara: if Vimta does not reply within 2 days, follow up on the "
        "investigation status and the summary report.")
    thread_newest = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                               "upendra.kumar@adgyllifesciences.com",
                               "2026-08-05T07:23:00Z",
                               to=["salim.tamboli@vimta.com"],
                               cc=["dan@negevlabs.com", "habibur.khan@vimta.in"])

    def _graph_get(url, params=None):
        if f"/users/{fue.SARA_MAILBOX}/mailFolders/inbox/messages" in url:
            return {"value": [instruction]}
        if "/users/dan@negevlabs.com/messages" in url:
            return {"value": [thread_newest]}
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    canned = json.dumps({
        "is_request": True, "thread_subject": "Dog tox study",
        "counterparties": ["salim.tamboli@vimta.com"],
        "asks": [
            {"ask": "investigation status", "recipients": ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"], "days": 2, "date": None},
            {"ask": "summary report", "recipients": [], "days": 2, "date": None},
        ],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append((mid, html_body)))
    return {"replies": replies}


def test_run_intake_registers_watch_per_ask(intake_world):
    out = fue.run_intake()
    assert out["registered"] == 2
    reg = fue._load_registry()
    assert len(reg["watches"]) == 2
    w = reg["watches"][0]
    assert w["owner"] == "dan@negevlabs.com"
    assert w["mailbox"] == "dan@negevlabs.com"
    assert w["conversation_id"] == "cid-right"
    assert w["anchor_message_id"] == "m2"
    assert w["recipients"] == ["salim.tamboli@vimta.com", "habibur.khan@vimta.in"]
    # Ask 2 gave no explicit recipients -> external thread participants.
    w2 = reg["watches"][1]
    assert set(w2["recipients"]) == {"salim.tamboli@vimta.com",
                                     "habibur.khan@vimta.in",
                                     "upendra.kumar@adgyllifesciences.com"}
    # Confirmation reply went out on the instruction email and names both ids.
    replies = intake_world["replies"]
    assert len(replies) == 1 and replies[0][0] == "im1"
    assert w["id"] in replies[0][1] and w2["id"] in replies[0][1]
    # Idempotent: second scan does nothing.
    assert fue.run_intake()["registered"] == 0


def test_run_intake_dry_run_writes_nothing(intake_world):
    out = fue.run_intake(dry_run=True)
    assert out["registered"] == 2
    assert fue._load_registry()["watches"] == []
    assert intake_world["replies"] == []


def test_run_intake_ignores_external_and_media(monkeypatch, fue_files):
    ext = _intake_msg("x1", "spam@evil.com", "follow up", "follow up please")
    media = _intake_msg("x2", "dan@negevlabs.com", "fyi",
                        "follow up on https://youtu.be/abc123 later")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [ext, media]})
    called = []
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: called.append(1) or '{"is_request": false}')
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda *a, **kw: pytest.fail("must not reply"))
    out = fue.run_intake()
    assert out["registered"] == 0
    assert called == []  # media-link mail never reaches the parser


def test_run_intake_cancel_command_by_watch_id(monkeypatch, fue_files):
    reg = {"watches": [fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-right", anchor_message_id="m2",
        anchor_received="2026-08-05T07:23:00Z", subject="Dog tox",
        ask="investigation status", recipients=[], interval_days=2,
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake-1")]}
    wid = reg["watches"][0]["id"]
    fue._save_registry(reg)
    cmd = _intake_msg("c1", "dan@negevlabs.com", "RE: registered",
                      f"stop {wid} please", cid="conv-other")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [cmd]})
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append(mid))
    out = fue.run_intake()
    assert out["commands"] == 1
    assert fue._load_registry()["watches"][0]["status"] == "cancelled"
    assert replies == ["c1"]


def test_run_intake_unresolvable_thread_replies_honestly(monkeypatch, fue_files):
    instruction = _intake_msg("im2", "dan@negevlabs.com", "FW: Mystery",
                              "please follow up if they do not reply")
    def _graph_get(url, params=None):
        if "/mailFolders/inbox/" in url:
            return {"value": [instruction]}
        return {"value": []}  # owner-mailbox search finds nothing
    monkeypatch.setattr(eps, "graph_get", _graph_get)
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: json.dumps({
        "is_request": True, "thread_subject": "Mystery", "counterparties": [],
        "asks": [{"ask": "an answer", "recipients": [], "days": None, "date": None}]}))
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append((mid, html_body)))
    out = fue.run_intake()
    assert out["failures"] == 1 and fue._load_registry()["watches"] == []
    assert replies and "could not" in replies[0][1].lower()


# Self-review additions below: real-behavior coverage the brief's tests did
# not exercise -- the two gates never triggered by the brief's fixtures
# (Sara's own outbound, an auto-reply), the dry-run guard on the COMMAND
# path (the brief only dry-run-tests registration), the resume side of
# _apply_command (never exercised at all, directly or via run_intake), and
# the incremental-persistence contract (processed-ids written after EACH
# handled message, not batched at the end) that a crash mid-scan depends on.
# Also regression tests for a falsy-check bug in the brief's own Step 3 code
# (run_intake's "days" -> interval_days, and _apply_command's resume-deadline
# recompute both used `x or default`, which silently upgrades an explicit
# 0 -- a legitimate same-day-chase value per Task 1's new_watch contract --
# to FOLLOWUP_DEFAULT_BUSINESS_DAYS). Fixed to an `is None` check in both
# spots to match new_watch and parse_instruction's existing 0-preserving
# contract; these tests pin that fix.


def test_run_intake_preserves_days_zero(monkeypatch, fue_files):
    instruction = _intake_msg("d0", "dan@negevlabs.com", "FW: Same day",
                              "please follow up today if they do not reply")
    thread_newest = _graph_msg("m9", "cid-sameday", "RE: Same day",
                               "person@vimta.com", "2026-08-05T07:23:00Z")

    def _graph_get(url, params=None):
        if f"/users/{fue.SARA_MAILBOX}/mailFolders/inbox/messages" in url:
            return {"value": [instruction]}
        if "/users/dan@negevlabs.com/messages" in url:
            return {"value": [thread_newest]}
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    canned = json.dumps({
        "is_request": True, "thread_subject": "Same day", "counterparties": [],
        "asks": [{"ask": "a same-day chase", "recipients": [], "days": 0, "date": None}],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    monkeypatch.setattr(xte, "send_threaded_reply", lambda *a, **kw: None)
    out = fue.run_intake()
    assert out["registered"] == 1
    w = fue._load_registry()["watches"][0]
    assert w["interval_days"] == 0
    assert w["deadline"] == fue._today_il().isoformat()


def test_apply_command_resume_recomputes_deadline():
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z", subject="s", ask="a",
        recipients=[], interval_days=3, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-intake")
    w["status"] = "paused"
    changed = fue._apply_command({"watches": [w]}, [w], "resume")
    assert changed == [w["id"]]
    assert w["status"] == "active"
    assert w["deadline"] == fue.add_business_days(fue._today_il(), 3).isoformat()


def test_apply_command_resume_preserves_interval_days_zero():
    # Same falsy-check bug class Task 1 fixed in new_watch: an explicit
    # interval_days=0 (same-day chase) must not be silently upgraded to the
    # default when a paused watch is resumed and its deadline recomputed.
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z", subject="s", ask="a",
        recipients=[], interval_days=0, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-intake")
    w["status"] = "paused"
    fue._apply_command({"watches": [w]}, [w], "resume")
    assert w["deadline"] == fue._today_il().isoformat()


def test_apply_command_cancel_ignores_terminal_watch():
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z", subject="s", ask="a",
        recipients=[], interval_days=2, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-intake")
    w["status"] = "answered"
    changed = fue._apply_command({"watches": [w]}, [w], "cancel")
    assert changed == []
    assert w["status"] == "answered"


def test_run_intake_skips_own_outbound(monkeypatch, fue_files):
    # Loop guard: a message landing in Sara's inbox FROM Sara herself (a CC
    # loop, a bounce, etc.) must never be treated as a follow-up request.
    m = _intake_msg("s1", fue.SARA_MAILBOX, "FW: Dog tox study", "follow up please")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [m]})
    called = []
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: called.append(1) or '{"is_request": false}')
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda *a, **kw: pytest.fail("must not reply to self"))
    out = fue.run_intake()
    assert out["registered"] == 0 and out["commands"] == 0 and out["failures"] == 0
    assert called == []


def test_run_intake_ignores_auto_reply(monkeypatch, fue_files):
    # An autoresponder (OOO, ticketing bounce) must never be parsed as a
    # follow-up instruction even from an internal sender with trigger words.
    m = _intake_msg("a1", "dan@negevlabs.com", "Auto: Out of office",
                    "follow up: I am out of office this week")
    m["internetMessageHeaders"] = [{"name": "Auto-Submitted", "value": "auto-replied"}]
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [m]})
    called = []
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: called.append(1) or '{"is_request": false}')
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda *a, **kw: pytest.fail("must not reply to an auto-reply"))
    out = fue.run_intake()
    assert out["registered"] == 0 and out["commands"] == 0 and out["failures"] == 0
    assert called == []
    # Marked processed so it is not re-evaluated every 15-min scan forever.
    assert "a1" in fue._load_processed()


def test_run_intake_dry_run_command_leaves_registry_untouched(monkeypatch, fue_files):
    reg = {"watches": [fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-right", anchor_message_id="m2",
        anchor_received="2026-08-05T07:23:00Z", subject="Dog tox",
        ask="investigation status", recipients=[], interval_days=2,
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake-1")]}
    wid = reg["watches"][0]["id"]
    fue._save_registry(reg)
    cmd = _intake_msg("c1", "dan@negevlabs.com", "RE: registered",
                      f"stop {wid} please", cid="conv-other")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [cmd]})
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda *a, **kw: pytest.fail("dry run must not reply"))
    out = fue.run_intake(dry_run=True)
    assert out["commands"] == 1
    assert fue._load_registry()["watches"][0]["status"] == "active"
    assert fue._load_processed() == set()


def test_run_intake_persists_incrementally_survives_mid_scan_crash(monkeypatch, fue_files):
    """Processed-ids (and the registry mutation for a message already fully
    handled) must be on disk BEFORE a later message in the same batch can
    crash the scan -- otherwise a restart re-sends a reply that already went
    out. Message 1 completes normally; message 2's reply-send raises,
    simulating a crash/restart. A retry must not touch message 1 again."""
    watch_a = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-a", anchor_message_id="ma",
        anchor_received="2026-08-01T00:00:00Z", subject="A", ask="a",
        recipients=[], interval_days=2, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-a")
    watch_b = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-b", anchor_message_id="mb",
        anchor_received="2026-08-01T00:00:00Z", subject="B", ask="b",
        recipients=[], interval_days=2, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-b")
    fue._save_registry({"watches": [watch_a, watch_b]})
    cmd1 = _intake_msg("c1", "dan@negevlabs.com", "RE: a", f"stop {watch_a['id']}", cid="conv-a")
    cmd2 = _intake_msg("c2", "dan@negevlabs.com", "RE: b", f"stop {watch_b['id']}", cid="conv-b")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [cmd1, cmd2]})

    replies = []
    call_count = {"n": 0}

    def _flaky_send(mid, html_body, attachments=None):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash mid-scan")
        replies.append(mid)

    monkeypatch.setattr(xte, "send_threaded_reply", _flaky_send)
    with pytest.raises(RuntimeError):
        fue.run_intake()

    # Message 1 fully completed and persisted before message 2 crashed.
    assert fue._load_processed() == {"c1"}
    assert replies == ["c1"]
    reg_after_crash = fue._load_registry()["watches"]
    assert reg_after_crash[0]["status"] == "cancelled"
    # watch_b's mutation is ALSO already on disk (registry is saved before
    # the reply is sent in the command branch) -- not a bug: re-applying
    # cancel to an already-cancelled watch on retry is a safe no-op.
    assert reg_after_crash[1]["status"] == "cancelled"

    # Retry with a healthy send: c1 (already handled) must not be replayed.
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append(mid))
    out = fue.run_intake()
    assert out["commands"] == 1  # only c2 this scan; c1 was skipped outright
    assert replies == ["c1", "c2"]  # c1 not resent
    assert fue._load_processed() == {"c1", "c2"}


# Fix round 1 (review findings A, B, C) below.


def _boom(*a, **kw):
    raise RuntimeError("send failed")


def test_run_intake_registration_retry_after_reply_failure_does_not_duplicate(monkeypatch, fue_files):
    # FINDING A: _save_registry runs before send_threaded_reply, so a raise
    # from the reply-send leaves the new watch already persisted but mid
    # never marked processed. A retry must not call new_watch() again for
    # the same ask.
    instruction = _intake_msg(
        "im3", "dan@negevlabs.com", "FW: Dog tox study",
        "Sara: if Vimta does not reply within 2 days, follow up on the "
        "investigation status.")
    thread_newest = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                               "upendra.kumar@adgyllifesciences.com",
                               "2026-08-05T07:23:00Z",
                               to=["salim.tamboli@vimta.com"])

    def _graph_get(url, params=None):
        if f"/users/{fue.SARA_MAILBOX}/mailFolders/inbox/messages" in url:
            return {"value": [instruction]}
        if "/users/dan@negevlabs.com/messages" in url:
            return {"value": [thread_newest]}
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    canned = json.dumps({
        "is_request": True, "thread_subject": "Dog tox study",
        "counterparties": ["salim.tamboli@vimta.com"],
        "asks": [{"ask": "investigation status",
                  "recipients": ["salim.tamboli@vimta.com"], "days": 2, "date": None}],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)
    monkeypatch.setattr(xte, "send_threaded_reply", _boom)

    with pytest.raises(RuntimeError):
        fue.run_intake()

    reg_after_crash = fue._load_registry()["watches"]
    assert len(reg_after_crash) == 1  # registry save landed before the raise
    original_id = reg_after_crash[0]["id"]
    assert "im3" not in fue._load_processed()  # crash happened before this line

    # Retry with a healthy send: must not duplicate the already-registered ask.
    monkeypatch.setattr(xte, "send_threaded_reply", lambda *a, **kw: None)
    out = fue.run_intake()
    reg_after_retry = fue._load_registry()["watches"]
    assert len(reg_after_retry) == 1  # still one watch, not two
    assert reg_after_retry[0]["id"] == original_id  # the SAME watch, not a new id
    assert out["registered"] == 0  # nothing new -- the ask already existed


def test_find_existing_watch_ignores_cancelled_prior_watch():
    # A deliberate re-registration after a cancel must still be allowed --
    # only a non-cancelled watch with the same conversation+ask blocks it.
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid1", anchor_message_id="m1",
        anchor_received="2026-08-05T04:23:00Z", subject="s", ask="status",
        recipients=[], interval_days=2, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-x")
    w["status"] = "cancelled"
    reg = {"watches": [w]}
    assert fue._find_existing_watch(reg, [], "conv-x", "status") is None
    w["status"] = "active"
    assert fue._find_existing_watch(reg, [], "conv-x", "status") is w
    assert fue._find_existing_watch(reg, [], "conv-x", "a different ask") is None
    assert fue._find_existing_watch(reg, [], "conv-other", "status") is None


def test_run_intake_unknown_watch_id_replies_honestly_and_cancels_nothing(monkeypatch, fue_files):
    # FINDING B: a command word plus an fw_ id matching no real watch is
    # almost certainly a typo -- honest failure, never silence, and nothing
    # gets cancelled.
    reg = {"watches": [fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-right", anchor_message_id="m2",
        anchor_received="2026-08-05T07:23:00Z", subject="Dog tox",
        ask="investigation status", recipients=[], interval_days=2,
        deadline=date(2026, 8, 7), intake_conversation_id="conv-intake-1")]}
    fue._save_registry(reg)
    cmd = _intake_msg("c9", "dan@negevlabs.com", "RE: registered",
                      "please stop fw_deadbeef", cid="conv-other")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [cmd]})
    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append((mid, html_body)))
    out = fue.run_intake()
    assert out["failures"] == 1 and out["commands"] == 0
    assert fue._load_registry()["watches"][0]["status"] == "active"  # untouched
    assert len(replies) == 1 and replies[0][0] == "c9"
    assert "fw_deadbeef" in replies[0][1]


def test_parse_command_requires_leading_word_without_explicit_id():
    # FINDING C (human-approved deviation from spec line 53's literal
    # "reply CONTAINING stop|cancel|done" -- see followup-engine-spec.md;
    # to be reconciled in Task 9). All 5 required MUST-match cases.
    assert fue._parse_command("stop") == "cancel"
    assert fue._parse_command("Stop.") == "cancel"
    assert fue._parse_command("Hi Sara, stop") == "cancel"
    assert fue._parse_command("Thanks for the reminder.\ncancel") == "cancel"
    assert fue._parse_command("Please CANCEL fw_1234abcd") == "cancel"  # explicit id


def test_parse_command_ignores_command_words_not_leading():
    # FINDING C. All 5 required MUST-NOT-match cases -- ordinary English
    # sentences that happen to contain done/keep/continue nowhere near the
    # start must never be read as a command.
    assert fue._parse_command("once the report's done, loop in Legal") is None
    assert fue._parse_command("I'll keep you posted on the Vimta thread") is None
    assert fue._parse_command("we can continue this next week") is None
    assert fue._parse_command("Let me know when you're done with the draft") is None
    assert fue._parse_command("thanks!") is None


def test_parse_command_cancel_wins_across_lines():
    # "cancel wins over resume" (the function's own documented rule) must
    # hold body-wide, not just within whichever line happens to be checked
    # first -- a naive per-line short-circuit would return "resume" here
    # since line 1 leads with "keep", never reaching line 2's "cancel".
    assert fue._parse_command("keep going\ncancel it") == "cancel"


# Fix report (review round 2) -- Finding D below.


def _dog_tox_retry_world(monkeypatch):
    """Shared setup for the Finding D tests: one instruction, one ask, a
    thread that resolves cleanly. Caller still has to set send_threaded_reply
    for scan 1 (always _boom) and can override it again for scan 2."""
    instruction = _intake_msg(
        "im6", "dan@negevlabs.com", "FW: Dog tox study",
        "Sara: if Vimta does not reply within 2 days, follow up on the "
        "investigation status.")
    thread_newest = _graph_msg("m2", "cid-right", "RE: Dog tox study",
                               "upendra.kumar@adgyllifesciences.com",
                               "2026-08-05T07:23:00Z",
                               to=["salim.tamboli@vimta.com"])

    def _graph_get(url, params=None):
        if f"/users/{fue.SARA_MAILBOX}/mailFolders/inbox/messages" in url:
            return {"value": [instruction]}
        if "/users/dan@negevlabs.com/messages" in url:
            return {"value": [thread_newest]}
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    canned = json.dumps({
        "is_request": True, "thread_subject": "Dog tox study",
        "counterparties": ["salim.tamboli@vimta.com"],
        "asks": [{"ask": "investigation status",
                  "recipients": ["salim.tamboli@vimta.com"], "days": 2, "date": None}],
    })
    monkeypatch.setattr(ld, "_call_claude_text", lambda p, m, max_tokens=2000, **kw: canned)


def test_run_intake_retry_reconfirms_existing_watches_after_reply_failure(monkeypatch, fue_files):
    # FINDING D: Finding A's dedup fix must not turn a transient reply-send
    # failure into PERMANENT silence -- the owner still needs the watch ids
    # to ever cancel them. Scan 2 must re-send the confirmation naming the
    # SAME ids that already exist, not silently swallow it.
    _dog_tox_retry_world(monkeypatch)
    monkeypatch.setattr(xte, "send_threaded_reply", _boom)

    with pytest.raises(RuntimeError):
        fue.run_intake()

    original_id = fue._load_registry()["watches"][0]["id"]
    assert "im6" not in fue._load_processed()

    replies = []
    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda mid, html_body, attachments=None: replies.append((mid, html_body)))
    out = fue.run_intake()

    assert out["registered"] == 0  # nothing NEW -- the ask already existed
    watches = fue._load_registry()["watches"]
    assert len(watches) == 1 and watches[0]["id"] == original_id  # one per ask, unchanged
    assert len(replies) == 1 and replies[0][0] == "im6"
    assert original_id in replies[0][1]  # the SAME id -- not silence


def test_run_intake_retry_dry_run_reconfirmation_sends_and_writes_nothing(monkeypatch, fue_files):
    # A dry run on the reconfirmation retry must still send and write
    # nothing -- same dry-run contract as every other path in this function.
    _dog_tox_retry_world(monkeypatch)
    monkeypatch.setattr(xte, "send_threaded_reply", _boom)

    with pytest.raises(RuntimeError):
        fue.run_intake()

    watches_before = fue._load_registry()["watches"]
    assert len(watches_before) == 1

    monkeypatch.setattr(xte, "send_threaded_reply",
                        lambda *a, **kw: pytest.fail("dry run must not reply"))
    out = fue.run_intake(dry_run=True)
    assert out["registered"] == 0
    assert fue._load_registry()["watches"] == watches_before  # unchanged
    assert fue._load_processed() == set()  # dry run persists nothing


def _watch_in_registry(**over):
    w = fue.new_watch(
        owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
        conversation_id="cid-right", anchor_message_id="m2",
        anchor_received="2026-08-05T07:23:00Z", subject="Dog tox",
        ask="investigation status", recipients=["salim.tamboli@vimta.com"],
        interval_days=2, deadline=date(2026, 8, 7),
        intake_conversation_id="conv-intake-1")
    w.update(over)
    return w


def _thread_reply(mid, sender, body, received="2026-08-06T09:00:00Z", headers=None):
    return {"id": mid, "conversationId": "cid-right",
            "from": {"emailAddress": {"address": sender}},
            "receivedDateTime": received,
            "uniqueBody": {"content": f"<html><body>{body}</body></html>"},
            "internetMessageHeaders": headers or []}


def test_check_replies_answered_closes_watch(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [
        _thread_reply("r1", "salim.tamboli@vimta.com",
                      "Investigation complete, root cause was a pipetting error.")]})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "ANSWERED")
    events = fue.check_replies(reg)
    assert reg["watches"][0]["status"] == "answered"
    assert events[0]["type"] == "reply_answered"
    assert reg["watches"][0]["last_checked"] is not None
    assert reg["watches"][0]["latest_message_id"] == "r1"


def test_check_replies_human_nonanswer_pauses(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [
        _thread_reply("r2", "salim.tamboli@vimta.com", "We will get back to you next week.")]})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "NOT_ANSWERED")
    events = fue.check_replies(reg)
    assert reg["watches"][0]["status"] == "paused"
    assert events[0]["type"] == "reply_paused"


def test_check_replies_autoreply_and_internal_ignored(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    ooo = _thread_reply("r3", "salim.tamboli@vimta.com", "Out of office",
                        headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])
    own = _thread_reply("r4", "dan@negevlabs.com", "bumping this myself")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [ooo, own]})
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("no verdict for auto/internal"))
    events = fue.check_replies(reg)
    assert events == [] and reg["watches"][0]["status"] == "active"


def test_check_replies_no_new_messages(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry()]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    assert fue.check_replies(reg) == []
    assert reg["watches"][0]["status"] == "active"


# Task 5 self-review additions -- direct filter/format pinning, isolated gate
# tests (the brief's own combined test exercises both gates in one call),
# verdict-default mutation coverage, and multi-watch continuation. See
# task-5-report.md for the mutation-testing narrative these back up.


def test_fetch_new_messages_builds_filter_from_anchor_on_first_check(monkeypatch, fue_files):
    # No test above inspects the actual $filter/url/params reaching Graph --
    # they all stub graph_get with lambdas that ignore params. That is
    # precisely the gap the mandated correction warns about; pin the shape
    # directly so a broken filter string cannot ship unnoticed again.
    w = _watch_in_registry()
    captured = {}

    def _graph_get(url, params=None):
        captured["url"] = url
        captured["params"] = params
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    fue._fetch_new_messages(w)
    assert captured["url"] == f"{eps.MS_GRAPH_BASE}/users/dan@negevlabs.com/messages"
    assert captured["params"]["$filter"] == (
        "conversationId eq 'cid-right' and receivedDateTime gt 2026-08-05T07:23:00Z")
    assert captured["params"]["$top"] == "50"
    assert captured["params"]["$select"] == (
        "id,subject,from,receivedDateTime,uniqueBody,internetMessageHeaders,conversationId")


def test_fetch_new_messages_sorts_ascending_and_tracks_latest(monkeypatch, fue_files):
    w = _watch_in_registry()
    newer = _thread_reply("r20", "salim.tamboli@vimta.com", "second",
                          received="2026-08-06T10:00:00Z")
    older = _thread_reply("r21", "salim.tamboli@vimta.com", "first",
                          received="2026-08-06T09:00:00Z")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [newer, older]})
    msgs = fue._fetch_new_messages(w)
    assert [m["id"] for m in msgs] == ["r21", "r20"]
    assert w["latest_message_id"] == "r20"


def test_check_replies_second_check_uses_graph_format_timestamp(monkeypatch, fue_files):
    # MANDATED CORRECTION: last_checked must be written in Graph's own
    # Z-suffixed format (no microseconds, no +00:00) so a SECOND check's
    # $filter is well-formed. .isoformat() on a tz-aware datetime produces
    # "+00:00" plus microseconds, which breaks the Graph $filter on every
    # check after the first (the FIRST check's "since" comes from
    # anchor_received, Graph's own literal, so it always works regardless of
    # this bug -- only a SECOND check exercises the stored value).
    from datetime import datetime

    reg = {"watches": [_watch_in_registry()]}
    calls = []

    def _graph_get(url, params=None):
        calls.append(params)
        return {"value": []}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("no verdict expected"))

    assert fue.check_replies(reg) == []  # first check: since = anchor_received
    assert reg["watches"][0]["status"] == "active"
    assert reg["watches"][0]["last_checked"] is not None

    assert fue.check_replies(reg) == []  # second check: since = last_checked
    assert len(calls) == 2
    second_filter = calls[1]["$filter"]
    since_clause = second_filter.split("receivedDateTime gt ")[1]
    datetime.strptime(since_clause, "%Y-%m-%dT%H:%M:%SZ")  # raises if not this exact shape
    assert "+00:00" not in since_clause
    assert "." not in since_clause


def test_check_replies_autoreply_alone_ignored(monkeypatch, fue_files):
    # Isolates the auto-reply gate from the internal-sender gate (the brief's
    # own test exercises both at once via two different messages, so either
    # gate alone breaking still leaves the other filtering the list to empty).
    reg = {"watches": [_watch_in_registry()]}
    ooo = _thread_reply("r5", "salim.tamboli@vimta.com", "Out of office until Monday",
                        headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [ooo]})
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("auto-reply must not reach verdict"))
    events = fue.check_replies(reg)
    assert events == [] and reg["watches"][0]["status"] == "active"


def test_check_replies_internal_alone_ignored(monkeypatch, fue_files):
    # Isolates the internal-sender gate from the auto-reply gate.
    reg = {"watches": [_watch_in_registry()]}
    own = _thread_reply("r6", "dan@negevlabs.com", "bumping this myself, no reply yet")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [own]})
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("internal sender must not reach verdict"))
    events = fue.check_replies(reg)
    assert events == [] and reg["watches"][0]["status"] == "active"


def test_check_replies_verdict_prompt_excludes_noise_messages(monkeypatch, fue_files):
    # Confirms the FILTERED subset (not the raw fetch) is what reaches Claude
    # -- a regression here would leak auto-reply/internal noise into the
    # verdict prompt even though the brief's own all-noise test has nothing
    # left to filter down TO and so cannot catch this class of bug.
    reg = {"watches": [_watch_in_registry()]}
    ooo = _thread_reply("r7", "salim.tamboli@vimta.com", "Out of office until Monday",
                        headers=[{"name": "Auto-Submitted", "value": "auto-replied"}])
    own = _thread_reply("r8", "dan@negevlabs.com", "bumping this myself")
    real = _thread_reply("r9", "salim.tamboli@vimta.com",
                         "Investigation complete, root cause was a pipetting error.",
                         received="2026-08-06T11:00:00Z")
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": [ooo, own, real]})
    captured = {}

    def _capture(prompt, model, max_tokens=10, **kw):
        captured["prompt"] = prompt
        return "ANSWERED"

    monkeypatch.setattr(ld, "_call_claude_text", _capture)
    events = fue.check_replies(reg)
    assert "Out of office" not in captured["prompt"]
    assert "bumping this myself" not in captured["prompt"]
    assert "pipetting error" in captured["prompt"]
    assert events[0]["who"] == "salim.tamboli@vimta.com"
    assert events[0]["when"] == "2026-08-06T11:00:00Z"


def test_verdict_default_on_claude_exception_is_not_answered(monkeypatch):
    w = _watch_in_registry()
    msgs = [_thread_reply("r10", "salim.tamboli@vimta.com", "some update, unclear if final")]

    def _boom_claude(*a, **kw):
        raise RuntimeError("claude down")

    monkeypatch.setattr(ld, "_call_claude_text", _boom_claude)
    assert fue._verdict(w, msgs) == "NOT_ANSWERED"


def test_verdict_default_on_unparseable_response_is_not_answered(monkeypatch):
    w = _watch_in_registry()
    msgs = [_thread_reply("r11", "salim.tamboli@vimta.com", "some update, unclear if final")]
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "MAYBE, hard to say")
    assert fue._verdict(w, msgs) == "NOT_ANSWERED"


def test_check_replies_skips_paused_watch(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(status="paused")]}
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: pytest.fail(
                            "check_replies must not fetch for a non-active watch"))
    events = fue.check_replies(reg)
    assert events == []
    assert reg["watches"][0]["status"] == "paused"
    assert reg["watches"][0]["last_checked"] is None


def test_check_replies_skips_cancelled_watch(monkeypatch, fue_files):
    # A cancelled watch must never be revived by anything in this module.
    reg = {"watches": [_watch_in_registry(status="cancelled")]}
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: pytest.fail(
                            "cancelled watch must never be revisited"))
    events = fue.check_replies(reg)
    assert events == []
    assert reg["watches"][0]["status"] == "cancelled"


def test_check_replies_one_watch_fetch_failure_does_not_stop_others(monkeypatch, fue_files):
    broken = _watch_in_registry(mailbox="broken@negevlabs.com")
    ok = _watch_in_registry(mailbox="dan@negevlabs.com")
    reg = {"watches": [broken, ok]}

    def _graph_get(url, params=None):
        if "broken@negevlabs.com" in url:
            raise RuntimeError("graph down")
        return {"value": [_thread_reply("r12", "salim.tamboli@vimta.com",
                                        "all done, confirmed resolved")]}

    monkeypatch.setattr(eps, "graph_get", _graph_get)
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "ANSWERED")
    events = fue.check_replies(reg)
    assert reg["watches"][0]["status"] == "active"   # broken watch untouched
    assert reg["watches"][0]["last_checked"] is None
    assert reg["watches"][1]["status"] == "answered"  # second watch still processed
    assert len(events) == 1 and events[0]["watch_id"] == ok["id"]


def _capture_writes(monkeypatch, draft_id="d1", web_link="https://outlook.example/d1"):
    calls = []
    def _post(url, json_body):
        calls.append({"method": "POST", "url": url, "body": json_body})
        if url.endswith("/createReplyAll"):
            return {"id": draft_id, "webLink": web_link}
        return {}
    def _patch(url, json_body):
        calls.append({"method": "PATCH", "url": url, "body": json_body})
        return {}
    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", _patch)
    monkeypatch.setattr(eps, "graph_delete", lambda url: calls.append({"method": "DELETE", "url": url}))
    return calls


def test_process_deadlines_creates_draft_when_live(monkeypatch, fue_files):
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    calls = _capture_writes(monkeypatch)
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: "Dear Dr. Salim,\n\nA gentle reminder on the investigation status.\n\nBest regards")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    w = reg["watches"][0]
    assert events[0]["type"] == "draft" and events[0]["web_link"]
    assert w["nudges_sent"] == 1 and w["deadline"] == "2026-08-11"  # Fri + 2bd -> Tue
    assert w["drafts"][0] == {"message_id": "d1",
                              "web_link": "https://outlook.example/d1",
                              "created": w["drafts"][0]["created"], "sent": False}
    post = [c for c in calls if c["method"] == "POST"]
    assert post[0]["url"].endswith("/users/dan@negevlabs.com/messages/m2/createReplyAll")
    assert not any(c["url"].endswith("/send") for c in post)  # NEVER sends
    patch = [c for c in calls if c["method"] == "PATCH"][0]
    assert patch["body"]["toRecipients"] == [
        {"emailAddress": {"address": "salim.tamboli@vimta.com"}}]
    assert "reminder" in patch["body"]["body"]["content"].lower()


def test_process_deadlines_report_only_without_live(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    monkeypatch.setattr(eps, "graph_post", lambda *a, **kw: pytest.fail("no Graph writes in report-only"))
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    # AUTHORIZED FIX to the brief's literal test: _compose_draft's own given
    # implementation (and its documented "-> str (HTML body)" signature)
    # always wraps Claude's text in <p> tags, so the event body can never
    # equal the brief's literal raw-text expectation "Reminder body" -- it
    # is always "<p>Reminder body</p>". Confirmed this fails against the
    # brief's own unmodified code (see task-6-report.md). Fixing the
    # expected value to match the documented HTML-body contract, not
    # weakening what is asserted: still an exact match, still proves the
    # report-only body carries Claude's real content.
    assert events[0]["type"] == "would_draft" and events[0]["body"] == "<p>Reminder body</p>"
    # CONTROLLER RULING (dry-run budget fix): updated from the brief's
    # literal expectation (which asserted nudges_sent == 1). Report-only
    # must NOT consume the nudge budget -- nudges_sent stays 0 -- but the
    # deadline STILL advances by the watch's interval, so the watch
    # re-surfaces on the same cadence a live run would rather than being
    # reported once and then going stale forever. Asserting BOTH halves
    # positively so either regressing (the budget creeping again, or the
    # deadline going stale) fails this test.
    assert reg["watches"][0]["nudges_sent"] == 0
    assert reg["watches"][0]["deadline"] == "2026-08-11"  # Fri + 2bd (default interval) -> Tue


def test_process_deadlines_before_deadline_noop(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    assert fue.process_deadlines(reg, date(2026, 8, 6), dry_run=False) == []
    assert reg["watches"][0]["nudges_sent"] == 0


def test_process_deadlines_exhaustion(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", nudges_sent=3)]}
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events[0]["type"] == "exhausted"
    assert reg["watches"][0]["status"] == "exhausted"


class _FakeHTTPError(Exception):
    """Mimics requests.exceptions.HTTPError's .response.status_code shape
    (what eps.graph_get actually raises on a clean non-2xx response)
    without importing requests into this test file. A bare RuntimeError
    with "404" in its message -- the brief's original mock -- does NOT
    carry a status code the way a real Graph failure does, and the
    dry-run-inertness-class fix below needs to distinguish a genuine 404
    from a persistent 5xx or a network error by STATUS CODE, not by
    string content."""
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = type("_Resp", (), {"status_code": status_code})()


def test_sweep_unsent_marks_sent_on_404(monkeypatch, fue_files):
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "d1", "web_link": "L1", "created": "c", "sent": False},
                   {"message_id": "d2", "web_link": "L2", "created": "c", "sent": False}]
    reg = {"watches": [w]}
    def _get(url, params=None):
        if "/messages/d1" in url:
            raise _FakeHTTPError(404)   # sent or deleted -> gone
        return {"id": "d2", "isDraft": True}
    monkeypatch.setattr(eps, "graph_get", _get)
    unsent = fue.sweep_unsent(reg)
    assert w["drafts"][0]["sent"] is True
    assert [u["web_link"] for u in unsent] == ["L2"]


def test_sweep_unsent_410_gone_marks_sent(monkeypatch, fue_files):
    # 410 Gone gets the identical "definitively gone" treatment as 404.
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "d1", "web_link": "L1", "created": "c", "sent": False}]
    reg = {"watches": [w]}

    def _get(url, params=None):
        raise _FakeHTTPError(410)

    monkeypatch.setattr(eps, "graph_get", _get)
    unsent = fue.sweep_unsent(reg)
    assert w["drafts"][0]["sent"] is True
    assert unsent == []


def test_sweep_unsent_persistent_5xx_does_not_mark_sent(monkeypatch, fue_files):
    # FINDING (review, Important): a persistent 5xx -- eps.graph_get's own
    # 429/5xx retry loop already exhausted and raised -- is NOT a
    # definitive "gone" response. Only 404/410 may flip sent; anything
    # else must leave it False and still report the draft as unsent, or a
    # transient/persistent Graph outage would silently drop a draft still
    # sitting unsent in the owner's mailbox from every future report (the
    # same defect class the cancelled-watch ruling already settled,
    # reached here through an error path instead of a status field).
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "d1", "web_link": "L1", "created": "c", "sent": False}]
    reg = {"watches": [w]}

    def _get(url, params=None):
        raise _FakeHTTPError(503)  # retries already exhausted upstream, then raised

    monkeypatch.setattr(eps, "graph_get", _get)
    unsent = fue.sweep_unsent(reg)
    assert w["drafts"][0]["sent"] is False  # nothing written one-way
    assert [u["web_link"] for u in unsent] == ["L1"]  # still reported as unsent


def test_sweep_unsent_generic_connection_error_does_not_mark_sent(monkeypatch, fue_files):
    # Same guarantee for a raw network-level failure with no HTTP response
    # at all (no .response attribute, unlike _FakeHTTPError) -- must not be
    # confused with a definitive "gone".
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "d1", "web_link": "L1", "created": "c", "sent": False}]
    reg = {"watches": [w]}

    def _get(url, params=None):
        raise ConnectionError("network partition")

    monkeypatch.setattr(eps, "graph_get", _get)
    unsent = fue.sweep_unsent(reg)
    assert w["drafts"][0]["sent"] is False
    assert [u["web_link"] for u in unsent] == ["L1"]


def test_sweep_unsent_isdraft_true_still_reported(monkeypatch, fue_files):
    # Third required case, explicit: no exception at all, isDraft: True ->
    # unchanged behavior, still reported unsent. (Already exercised
    # incidentally by other tests in this file; pinned directly here for a
    # clean, explicitly-named three-case set alongside the two above.)
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "d1", "web_link": "L1", "created": "c", "sent": False}]
    reg = {"watches": [w]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"isDraft": True})
    unsent = fue.sweep_unsent(reg)
    assert w["drafts"][0]["sent"] is False
    assert [u["web_link"] for u in unsent] == ["L1"]


# Task 6 self-review additions -- mutation-testing driven. The brief's own
# given tests (above) never exercise: an explicit interval_days=0 through
# process_deadlines (the exact x-or-default anti-pattern this module's
# standing rule forbids, reintroduced in the brief's literal Step 3 code and
# fixed here the same way as Tasks 1 and 4), the dry_run=True parameter in
# combination with FOLLOWUP_LIVE=1, the OTHER side of the max_nudges
# boundary, non-active-watch skipping, the unparseable-deadline branch, a
# compose/draft-creation failure's effect on registry state, multi-run
# escalation, or any direct unit coverage of _create_draft/_compose_draft's
# own internal branches (target selection, recipients, webLink fallback,
# missing draft id, orphan-delete-on-failure, cleanup-failure-does-not-mask).
# See task-6-report.md for the full mutation-testing narrative.


def test_process_deadlines_interval_days_zero_survives_deadline_advance(monkeypatch, fue_files):
    # AUTHORIZED FIX (Global Constraints, not the one mandated correction):
    # the brief's literal code computes the next deadline via
    # `w.get("interval_days") or FOLLOWUP_DEFAULT_BUSINESS_DAYS` -- the exact
    # `x or default` anti-pattern already fixed in new_watch (Task 1) and
    # _apply_command's resume (Task 4). An explicit interval_days=0
    # ("chase same day") would be silently upgraded to the 2-day default.
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", interval_days=0)]}
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert reg["watches"][0]["deadline"] == "2026-08-07"  # same day, NOT +2 business days


def test_process_deadlines_unparseable_deadline_reset_respects_interval_days_zero(monkeypatch, fue_files):
    # Same anti-pattern, second occurrence -- the unparseable-deadline reset
    # branch has its own independent `w.get("interval_days") or DEFAULT`.
    reg = {"watches": [_watch_in_registry(deadline="not-a-date", interval_days=0)]}
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("unparseable deadline must reset before drafting"))
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events == []
    assert reg["watches"][0]["deadline"] == "2026-08-07"  # same day, NOT +2
    assert reg["watches"][0]["nudges_sent"] == 0  # this run only resets, never drafts


def test_process_deadlines_dry_run_true_forces_report_only_even_when_live(monkeypatch, fue_files):
    # Isolates dry_run's priority over the live gate -- an explicit
    # dry_run=True call must win even when FOLLOWUP_LIVE=1 (controller
    # ruling, mode 3: "whether or not _live()"). The brief's own
    # creates_draft_when_live test never passes dry_run=True, so a mutation
    # that let LIVE override an explicit dry_run=True would pass every
    # OTHER test in this file undetected.
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    monkeypatch.setattr(eps, "graph_post",
                        lambda *a, **kw: pytest.fail("dry_run=True must make zero Graph writes"))
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=True)
    assert events[0]["type"] == "would_draft"
    # CONTROLLER RULING (dry-run budget fix): dry_run=True must be safe to
    # invoke at any time and mutate NOTHING -- not nudges_sent, not
    # deadline, not a note. This function previously advanced nudges_sent
    # unconditionally even under an explicit dry_run=True, which meant a
    # mere PREVIEW call silently consumed the real nudge budget.
    assert reg["watches"][0]["nudges_sent"] == 0
    assert reg["watches"][0]["deadline"] == "2026-08-07"
    assert reg["watches"][0]["notes"] == []


def test_process_deadlines_exhaustion_leaves_nudges_and_deadline_unchanged(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", nudges_sent=3)]}
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("an exhausted watch must never be drafted for"))
    fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert reg["watches"][0]["nudges_sent"] == 3  # unchanged, not incremented to 4
    assert reg["watches"][0]["deadline"] == "2026-08-07"  # unchanged


def test_process_deadlines_one_below_max_nudges_still_drafts(monkeypatch, fue_files):
    # The exhaustion boundary's OTHER side: nudges_sent == max_nudges - 1
    # must still draft (escalation level == max_nudges), not exhaust early.
    # Report-only mode (FOLLOWUP_LIVE unset), so per the controller ruling
    # nudges_sent stays at its CURRENT value (2) rather than the brief-
    # literal 3 -- the reported escalation level is still correctly 3
    # (computed from the current count), it is just not persisted until a
    # real draft exists.
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", nudges_sent=2)]}
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events[0]["type"] == "would_draft" and events[0]["escalation"] == 3
    assert reg["watches"][0]["nudges_sent"] == 2  # unchanged -- report-only never consumes budget
    assert reg["watches"][0]["deadline"] == "2026-08-11"  # still advances -- Fri + 2bd -> Tue
    assert reg["watches"][0]["status"] == "active"  # not exhausted yet


def test_process_deadlines_skips_non_active_watches(monkeypatch, fue_files):
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("a non-active watch must never be drafted for"))
    for status in ("paused", "answered", "exhausted", "cancelled"):
        reg = {"watches": [_watch_in_registry(deadline="2026-08-07", status=status)]}
        events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
        assert events == [], f"status={status} must produce no events"
        assert reg["watches"][0]["nudges_sent"] == 0, f"status={status} must not advance nudges_sent"


def test_process_deadlines_unparseable_deadline_resets_and_skips(monkeypatch, fue_files):
    reg = {"watches": [_watch_in_registry(deadline="not-a-date")]}
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("must reset the bad deadline, not draft"))
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events == []
    assert reg["watches"][0]["deadline"] == "2026-08-11"  # today (Fri) + 2 business days -> Tue
    assert any("unparseable" in n["text"] for n in reg["watches"][0]["notes"])


def test_process_deadlines_compose_failure_skips_that_watch_only(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)  # report-only; explicit for clarity
    broken = _watch_in_registry(deadline="2026-08-07")
    ok = _watch_in_registry(deadline="2026-08-07")
    reg = {"watches": [broken, ok]}
    calls = {"n": 0}

    def _claude(prompt, model, max_tokens=1200, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("claude down")
        return "Reminder body"

    monkeypatch.setattr(ld, "_call_claude_text", _claude)
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert reg["watches"][0]["nudges_sent"] == 0  # broken watch untouched, retried next run
    assert reg["watches"][0]["deadline"] == "2026-08-07"  # unchanged
    # CONTROLLER RULING (dry-run budget fix): report-only, so the "ok" watch
    # still gets reported (would_draft) but its nudge budget is NOT
    # consumed -- only the deadline advances, same as any other report-only
    # watch, regardless of whether a SIBLING watch's compose call failed.
    assert reg["watches"][1]["nudges_sent"] == 0
    assert reg["watches"][1]["deadline"] == "2026-08-11"  # Fri + 2bd -> Tue
    assert len(events) == 1 and events[0]["watch_id"] == ok["id"]


def test_process_deadlines_draft_creation_failure_leaves_nudges_and_deadline_unchanged(monkeypatch, fue_files):
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07")]}
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")

    def _boom(*a, **kw):
        raise RuntimeError("graph down")

    monkeypatch.setattr(eps, "graph_post", _boom)
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events == []
    assert reg["watches"][0]["nudges_sent"] == 0
    assert reg["watches"][0]["deadline"] == "2026-08-07"
    assert reg["watches"][0]["drafts"] == []


def test_process_deadlines_second_reminder_escalates_to_2(monkeypatch, fue_files):
    # CONTROLLER RULING (dry-run budget fix): escalation only climbs across
    # runs when a REAL draft was created each time -- report-only no longer
    # advances nudges_sent at all (see test_process_deadlines_report_only_
    # without_live and the companion test right below this one), so this
    # scenario is now LIVE-only. Two distinct draft ids so each run's draft
    # is independently distinguishable.
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", interval_days=2)]}
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    _capture_writes(monkeypatch, draft_id="d1")
    first = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert first[0]["type"] == "draft" and first[0]["escalation"] == 1
    assert reg["watches"][0]["nudges_sent"] == 1
    assert reg["watches"][0]["deadline"] == "2026-08-11"  # Fri + 2bd -> Tue
    _capture_writes(monkeypatch, draft_id="d2")
    second = fue.process_deadlines(reg, date(2026, 8, 11), dry_run=False)
    assert second[0]["type"] == "draft" and second[0]["escalation"] == 2
    assert reg["watches"][0]["nudges_sent"] == 2


def test_process_deadlines_report_only_does_not_escalate_across_runs(monkeypatch, fue_files):
    # The direct counterpart to the LIVE test above: repeated report-only
    # runs stay at escalation level 1 forever (nudges_sent never leaves 0),
    # which is the ACCEPTED, INTENDED consequence of the controller ruling
    # ("the reported escalation stays 1 and the tone never climbs"), not a
    # separate bug.
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", interval_days=2)]}
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    first = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert first[0]["type"] == "would_draft" and first[0]["escalation"] == 1
    second = fue.process_deadlines(reg, date(2026, 8, 11), dry_run=False)
    assert second[0]["type"] == "would_draft" and second[0]["escalation"] == 1  # still 1, not 2
    assert reg["watches"][0]["nudges_sent"] == 0


def test_process_deadlines_live_mode_advances_nudges_and_deadline(monkeypatch, fue_files):
    # Required test 1/3 (controller ruling): LIVE mode is unchanged --
    # draft created, nudges_sent incremented, deadline advanced. Overlaps
    # deliberately with the brief's own test_process_deadlines_creates_
    # draft_when_live, giving a clean, explicitly-named three-mode set
    # alongside the report-only and dry-run tests above.
    monkeypatch.setenv("FOLLOWUP_LIVE", "1")
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", interval_days=2)]}
    _capture_writes(monkeypatch)
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=False)
    assert events[0]["type"] == "draft"
    assert reg["watches"][0]["nudges_sent"] == 1
    assert reg["watches"][0]["deadline"] == "2026-08-11"  # Fri + 2bd -> Tue


def test_process_deadlines_report_only_never_exhausts_the_nudge_budget(monkeypatch, fue_files):
    # THE motivating defect, directly reproduced and guarded against:
    # repeated report-only runs, well past max_nudges (default 3), must
    # NEVER flip the watch to "exhausted" or emit an "exhausted" event --
    # because zero real drafts were ever created in anyone's mailbox. Runs
    # the SAME watch through 10 consecutive report-only cycles (more than
    # 3x max_nudges), each one landing exactly on the deadline the PREVIOUS
    # cycle reported, simulating 10 real daily-cron days.
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", interval_days=1)]}
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    today = date(2026, 8, 7)
    for _ in range(10):
        events = fue.process_deadlines(reg, today, dry_run=False)
        assert all(e["type"] == "would_draft" for e in events)
        w = reg["watches"][0]
        assert w["status"] == "active"
        today = date.fromisoformat(w["deadline"])  # advance to the next reported deadline
    assert reg["watches"][0]["nudges_sent"] == 0
    assert reg["watches"][0]["status"] == "active"
    assert not any("max reminders" in n["text"] for n in reg["watches"][0]["notes"])


def test_create_draft_uses_latest_message_id_over_anchor(monkeypatch, fue_files):
    w = _watch_in_registry()
    w["anchor_message_id"] = "m2"
    w["latest_message_id"] = "m5"  # advanced by a prior check_replies run
    calls = _capture_writes(monkeypatch)
    fue._create_draft(w, "<p>body</p>")
    post = [c for c in calls if c["method"] == "POST"][0]
    assert post["url"].endswith("/users/dan@negevlabs.com/messages/m5/createReplyAll")


def test_create_draft_no_recipients_omits_to_field(monkeypatch, fue_files):
    w = _watch_in_registry(recipients=[])
    calls = _capture_writes(monkeypatch)
    fue._create_draft(w, "<p>body</p>")
    patch = [c for c in calls if c["method"] == "PATCH"][0]
    assert "toRecipients" not in patch["body"]


def test_create_draft_fetches_weblink_when_missing_from_create_response(monkeypatch, fue_files):
    w = _watch_in_registry()

    def _post(url, json_body):
        assert url.endswith("/createReplyAll")
        return {"id": "d7"}  # no webLink this time

    def _get(url, params=None):
        assert url.endswith("/messages/d7")
        return {"webLink": "https://outlook.example/d7"}

    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", lambda url, json_body: {})
    monkeypatch.setattr(eps, "graph_get", _get)
    result = fue._create_draft(w, "<p>body</p>")
    assert result["web_link"] == "https://outlook.example/d7"


def test_create_draft_no_draft_id_raises_without_patch_or_delete(monkeypatch, fue_files):
    w = _watch_in_registry()
    calls = _capture_writes(monkeypatch, draft_id=None)
    with pytest.raises(RuntimeError, match="no draft id"):
        fue._create_draft(w, "<p>body</p>")
    assert not any(c["method"] in ("PATCH", "DELETE") for c in calls)
    assert len(calls) == 1  # only the createReplyAll POST happened


def test_create_draft_patch_failure_deletes_orphan_and_reraises(monkeypatch, fue_files):
    w = _watch_in_registry()
    deleted = []

    def _patch_boom(url, json_body):
        raise RuntimeError("patch boom")

    monkeypatch.setattr(eps, "graph_post",
                        lambda url, json_body: {"id": "d9", "webLink": "https://outlook.example/d9"})
    monkeypatch.setattr(eps, "graph_patch", _patch_boom)
    monkeypatch.setattr(eps, "graph_delete", lambda url: deleted.append(url))
    with pytest.raises(RuntimeError, match="patch boom"):
        fue._create_draft(w, "<p>body</p>")
    assert deleted == [f"{eps.MS_GRAPH_BASE}/users/dan@negevlabs.com/messages/d9"]


def test_create_draft_cleanup_failure_does_not_mask_original_error(monkeypatch, fue_files):
    w = _watch_in_registry()

    def _patch_boom(url, json_body):
        raise RuntimeError("patch boom")

    def _delete_boom(url):
        raise RuntimeError("delete boom")

    monkeypatch.setattr(eps, "graph_post",
                        lambda url, json_body: {"id": "d9", "webLink": "https://outlook.example/d9"})
    monkeypatch.setattr(eps, "graph_patch", _patch_boom)
    monkeypatch.setattr(eps, "graph_delete", _delete_boom)
    with pytest.raises(RuntimeError, match="patch boom"):  # ORIGINAL error, not "delete boom"
        fue._create_draft(w, "<p>body</p>")


def test_create_draft_and_module_never_call_send(monkeypatch, fue_files):
    # THE ABSOLUTE RULE: this module has no code path that sends to a
    # counterparty. A runtime test can only prove a forbidden call is not
    # made for the specific scenarios it exercises; a static source scan
    # proves it for every scenario, including branches no test happens to
    # hit. HARDENING (review): beyond the literal "/send", also scan for
    # sendmail, /replyall, and /microsoft.graph.send -- three OTHER real
    # Graph send-equivalent endpoints. In particular, a createReplyAll ->
    # replyAll swap (create a DRAFT reply-all vs. immediately SEND one)
    # would slip past a /send-only scan entirely, since neither
    # "replyAll" (the dangerous endpoint) nor "createReplyAll" (the safe
    # one already in use) contains the substring "/send". The "/replyall"
    # pattern (WITH the leading slash) is chosen precisely so it does not
    # false-positive against the legitimate "createReplyAll" call: in that
    # literal the "/" sits before "create", never immediately before
    # "reply" -- confirmed by the sanity check below, which would fail
    # loudly if that reasoning were ever wrong.
    #
    # NARROWED (Task 7, controller ruling -- cross-task collision with the
    # brief's own _send_email): Task 7 adds the ONE legitimate sendMail
    # call in this module -- _send_email, which reports the run's outcome
    # to the OWNER (and FOLLOWUP_ALERT_CC on an escalation), both internal
    # addresses, from Sara's own mailbox. A blanket "no sendmail/send
    # anywhere in the module" scan cannot coexist with that any more, so
    # the guard is narrowed to two layers instead of weakened to nothing:
    # (a) the DRAFTING path -- _create_draft, process_deadlines,
    # sweep_unsent, named explicitly since those are exactly the functions
    # that touch a draft or a reply-all and must never gain a send call --
    # stays scanned exactly as before; (b) the WHOLE MODULE minus
    # _send_email's own source is ALSO still scanned, so every other
    # function -- present, or added by a future task -- stays completely
    # clean; only _send_email itself is exempted, and it is pinned down
    # separately and tightly in test_send_email_exempt_call_is_locked_down
    # below (recipients/subject/body only, fixed to SARA_MAILBOX, no
    # watch/mailbox/draft-id parameter for a later edit to abuse), so the
    # one carve-out cannot be repurposed to send a draft to a counterparty.
    import inspect
    forbidden = ["/send", "sendmail", "/replyall", "/microsoft.graph.send"]

    drafting_funcs = [fue._create_draft, fue.process_deadlines, fue.sweep_unsent]
    for fn in drafting_funcs:
        src = inspect.getsource(fn).lower()
        for pattern in forbidden:
            assert pattern not in src, f"found forbidden pattern {pattern!r} in {fn.__name__}"

    module_src = inspect.getsource(fue)
    send_email_src = inspect.getsource(fue._send_email)
    # Sanity: an exact substring, not a paraphrase -- if this ever fails,
    # the replace() below would silently no-op and the scan would be
    # vacuous (scanning the WHOLE module, exemption included).
    assert send_email_src in module_src
    remainder = module_src.replace(send_email_src, "", 1).lower()
    for pattern in forbidden:
        assert pattern not in remainder, (
            f"found forbidden pattern {pattern!r} outside the one _send_email exemption")

    # Not vacuous: the legitimate createReplyAll call is still present and
    # correctly did NOT trip the /replyall scan above.
    assert "createreplyall" in inspect.getsource(fue._create_draft).lower()


def test_compose_draft_wraps_paragraphs_and_escapes_html(monkeypatch):
    w = _watch_in_registry()
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: "Dear colleagues,\n\nAny update on <status> & timing?\n\nBest")
    body = fue._compose_draft(w)
    assert body == ("<p>Dear colleagues,</p>"
                    "<p>Any update on &lt;status&gt; &amp; timing?</p>"
                    "<p>Best</p>")


def test_compose_draft_empty_response_degrades_to_empty_paragraph(monkeypatch):
    w = _watch_in_registry()
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "")
    assert fue._compose_draft(w) == "<p></p>"


def test_sweep_unsent_skips_cancelled_watch(monkeypatch, fue_files):
    # MANDATED CORRECTION (pre-flight Ruling C): sweep_unsent must SKIP a
    # cancelled watch even though Graph still reports isDraft=True on its
    # draft -- the brief's literal code never reads w["status"], and
    # _apply_command's cancel path (Task 4) only flips status, never
    # touches w["drafts"], so an already-drafted watch that gets cancelled
    # would otherwise be reported "still unsent" forever (spec line 47:
    # re-report "daily until sent OR CANCELLED"). Paired with an otherwise-
    # identical ACTIVE watch in the SAME test so the skip cannot be
    # satisfied by sweep_unsent simply returning nothing.
    cancelled = _watch_in_registry(status="cancelled")
    cancelled["drafts"] = [{"message_id": "dc1", "web_link": "Lc1", "created": "c", "sent": False}]
    active = _watch_in_registry(status="active")
    active["drafts"] = [{"message_id": "da1", "web_link": "La1", "created": "c", "sent": False}]
    reg = {"watches": [cancelled, active]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"id": "x", "isDraft": True})
    unsent = fue.sweep_unsent(reg)
    watch_ids = {u["watch_id"] for u in unsent}
    assert cancelled["id"] not in watch_ids
    assert active["id"] in watch_ids
    assert [u["web_link"] for u in unsent] == ["La1"]
    # Skipped entirely -- the draft itself is left alone, not force-marked sent.
    assert cancelled["drafts"][0]["sent"] is False


def test_sweep_unsent_still_reports_paused_watch_draft(monkeypatch, fue_files):
    # Scope boundary of the mandated correction: ONLY "cancelled" is
    # skipped. A paused watch (owner replied but did not answer) still has
    # its stale draft reported -- nothing in the ruling or the spec says
    # paused should be silenced too.
    w = _watch_in_registry(status="paused")
    w["drafts"] = [{"message_id": "dp1", "web_link": "Lp1", "created": "c", "sent": False}]
    reg = {"watches": [w]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"isDraft": True})
    unsent = fue.sweep_unsent(reg)
    assert [u["web_link"] for u in unsent] == ["Lp1"]


def test_sweep_unsent_skips_already_sent_draft_without_graph_call(monkeypatch, fue_files):
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "ds1", "web_link": "Ls1", "created": "c", "sent": True}]
    reg = {"watches": [w]}
    monkeypatch.setattr(eps, "graph_get",
                        lambda url, params=None: pytest.fail("already-sent draft must not hit Graph"))
    assert fue.sweep_unsent(reg) == []


def test_sweep_unsent_isdraft_false_marks_sent_without_exception(monkeypatch, fue_files):
    # Distinct from the 404 path: the message still exists but is no longer
    # a draft (it was sent) -- no exception raised, just isDraft: False.
    w = _watch_in_registry()
    w["drafts"] = [{"message_id": "df1", "web_link": "Lf1", "created": "c", "sent": False}]
    reg = {"watches": [w]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"id": "df1", "isDraft": False})
    unsent = fue.sweep_unsent(reg)
    assert unsent == []
    assert w["drafts"][0]["sent"] is True
    assert any("left the Drafts folder" in n["text"] for n in w["notes"])


# Fix report (dry-run inertness) additions -- FIX 1 and FIX 2 below close the
# same defect class the report-only budget ruling settled: dry_run=True must
# mutate NOTHING, full stop. Each new test is paired with an existing
# non-dry counterpart (named in its own comment) so the guard cannot be
# satisfied by disabling the underlying feature outright.


def test_process_deadlines_dry_run_unparseable_deadline_leaves_watch_untouched(monkeypatch, fue_files):
    # FIX 1: a dry run must be safe to invoke at any time against live
    # state -- it must not repair a corrupted deadline either. Paired with
    # test_process_deadlines_unparseable_deadline_resets_and_skips
    # (dry_run=False), which proves the deadline DOES actually reset when
    # not in a dry run, so this guard cannot be satisfied by disabling the
    # reset feature outright.
    reg = {"watches": [_watch_in_registry(deadline="not-a-date")]}
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("unparseable deadline must never reach compose"))
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=True)  # must not raise
    assert events == []
    assert reg["watches"][0]["deadline"] == "not-a-date"  # byte-identical, NOT reset
    assert reg["watches"][0]["notes"] == []  # byte-identical, no note added


def test_process_deadlines_dry_run_exhausted_watch_emits_event_without_mutating(monkeypatch, fue_files):
    # FIX 2 (the more serious one): a dry_run=True preview against a watch
    # already at nudges_sent >= max_nudges must still show the "exhausted"
    # outcome (the event fires, so the preview is honest) but must NEVER
    # flip the watch to exhausted for real as a side effect of merely
    # asking "what would happen?". Paired with the brief's own
    # test_process_deadlines_exhaustion (dry_run=False), which proves
    # status DOES actually flip when not in a dry run, so this guard
    # cannot be satisfied by disabling the exhaustion feature outright.
    reg = {"watches": [_watch_in_registry(deadline="2026-08-07", nudges_sent=3)]}
    monkeypatch.setattr(ld, "_call_claude_text",
                        lambda *a, **kw: pytest.fail("an exhausted watch must never be drafted for"))
    events = fue.process_deadlines(reg, date(2026, 8, 7), dry_run=True)
    assert events[0]["type"] == "exhausted"  # still reported in the preview
    assert reg["watches"][0]["status"] == "active"  # NOT flipped for real
    assert reg["watches"][0]["notes"] == []  # unchanged


# ----------------------------------------------------------------------
#  Task 7: daily report email, orchestration (run_daily), status, CLI
# ----------------------------------------------------------------------


def _sendmail_capture(monkeypatch):
    sent = []
    def _post(url, json_body):
        if url.endswith("/sendMail"):
            sent.append(json_body)
            return {}
        if url.endswith("/createReplyAll"):
            return {"id": "d1", "webLink": "https://outlook.example/d1"}
        return {}
    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", lambda url, json_body: {})
    monkeypatch.setattr(eps, "graph_delete", lambda url: {})
    return sent


def test_run_daily_reports_would_draft_per_owner(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    reg = {"watches": [
        _watch_in_registry(deadline="2026-08-07"),
        _watch_in_registry(owner="ka@negevlabs.com", mailbox="ka@negevlabs.com",
                           deadline="2026-08-07"),
    ]}
    fue._save_registry(reg)
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily()
    assert out["reports"] == 2 and len(sent) == 2
    recipients = {s["message"]["toRecipients"][0]["emailAddress"]["address"] for s in sent}
    assert recipients == {"dan@negevlabs.com", "ka@negevlabs.com"}
    body = sent[0]["message"]["body"]["content"]
    assert "Reminder body" in body and "report-only" in body.lower()
    saved = fue._load_registry()
    # CORRECTED (brief/reality conflict, documented in task-7-report.md):
    # the brief's literal assertion here was `nudges_sent == 1`. That
    # contradicts the Task 6 controller ruling already shipped and
    # regression-tested -- FOLLOWUP_LIVE is unset in THIS exact test
    # (report-only mode), and process_deadlines' report-only branch
    # deliberately leaves nudges_sent UNTOUCHED so repeated report-only
    # runs can never silently exhaust a watch that was never really
    # drafted for (see test_process_deadlines_report_only_never_
    # exhausts_the_nudge_budget, which asserts nudges_sent == 0 across 10
    # report-only cycles and names the ruling explicitly). Reproduced and
    # confirmed here via mutation testing (see report). "persisted" is
    # still true -- just via the fields report-only mode DOES advance
    # (check_replies' last_checked stamp; process_deadlines' deadline
    # advance and note) -- not nudges_sent.
    assert saved["watches"][0]["nudges_sent"] == 0
    assert saved["watches"][0]["last_checked"] is not None
    assert saved["watches"][0]["deadline"] != "2026-08-07"


def test_run_daily_quiet_day_sends_nothing(monkeypatch, fue_files):
    fue._save_registry({"watches": [_watch_in_registry(deadline="2026-12-31")]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily()
    assert out["reports"] == 0 and sent == []
    assert fue.read_status()["reports"] == 0


def test_run_daily_escalation_ccs_alert(monkeypatch, fue_files):
    fue._save_registry({"watches": [
        _watch_in_registry(deadline="2026-08-07", nudges_sent=3)]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    sent = _sendmail_capture(monkeypatch)
    fue.run_daily()
    addrs = [r["emailAddress"]["address"]
             for r in sent[0]["message"]["toRecipients"]]
    assert addrs == ["dan@negevlabs.com", fue.ALERT_CC]


def test_run_daily_dry_run_persists_nothing(monkeypatch, fue_files):
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    fue._save_registry({"watches": [_watch_in_registry(deadline="2026-08-07")]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily(dry_run=True)
    assert out["reports"] == 1 and sent == []           # counted, not sent
    saved = fue._load_registry()["watches"][0]
    assert saved["nudges_sent"] == 0  # not persisted
    # MUTATION-TESTING FINDING (report-worthy): nudges_sent alone is a
    # WEAK witness for "the registry was not saved" -- process_deadlines
    # never touches nudges_sent under dry_run regardless of whether the
    # result gets persisted, so this assertion passes identically whether
    # or not `if not dry_run: _save_registry(reg)` is even there.
    # check_replies, by contrast, has NO dry_run parameter of its own and
    # unconditionally sets last_checked on the in-memory watch on every
    # call -- so it is last_checked, not nudges_sent, that actually proves
    # the save was skipped: reverting the registry-save guard to
    # unconditional reproduces a real timestamp here even under
    # dry_run=True (confirmed by deliberately reintroducing that mutation
    # and rerunning this test -- see task-7-report.md).
    assert saved["last_checked"] is None  # untouched on disk


def test_status_summary_shape(fue_files):
    fue._save_registry({"watches": [_watch_in_registry()]})
    fue._write_status({"reports": 0})
    s = fue.status_summary()
    assert s["last_run"] == {"reports": 0}
    assert s["watches"][0]["ask"] == "investigation status"
    assert "conversation_id" not in s["watches"][0]  # trimmed view


# Task 7 self-review additions -- carry-forward confirmations (dry-run must
# write NOTHING, including the status file) and explicit mutation-tested
# coverage for the per-owner grouping isolation, the exhausted rendering,
# and the "nothing happened -> no email" rule's more interesting flip side
# (nothing NEW happened, but something is still unsent). See
# task-7-report.md for the mutation-testing narrative these back up.


def test_run_daily_dry_run_does_not_touch_status_file(monkeypatch, fue_files):
    # CARRY-FORWARD (must-satisfy #1, status-file half): a dry run must be
    # safe to invoke at any time against live state and persist NOTHING --
    # the brief's own Step 3 code calls _write_status(result)
    # UNCONDITIONALLY, which would let a dry-run preview silently clobber
    # the last REAL run's recorded outcome. Seed a "real" status first so a
    # regression has something to clobber, then prove a dry run leaves it
    # byte-identical.
    fue._write_status({"reports": 999, "marker": "prior-real-run"})
    fue._save_registry({"watches": [_watch_in_registry(deadline="2026-08-07")]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    _sendmail_capture(monkeypatch)
    fue.run_daily(dry_run=True)
    assert fue.read_status() == {"reports": 999, "marker": "prior-real-run"}


def test_run_daily_per_owner_report_excludes_other_owners_content(monkeypatch, fue_files):
    # SELF-REVIEW: "would an owner ever see another owner's watches in
    # their report?" -- a real risk in a per-owner grouping loop, and
    # there are TWO independent filters that could leak (the events list
    # and the unsent list), so this exercises both, each with its own
    # DISTINCT, uniquely-markered ask text (unlike the brief's own
    # per-owner test, which uses the same default ask for both watches and
    # so cannot tell a leak from a coincidence). MUTATION-TESTED (see
    # task-7-report.md): a prior version of this test carried no unsent
    # items at all and passed unchanged even with the `un = [u for u in
    # unsent if u["owner"] == owner]` filter deleted outright -- a real
    # gap, closed by giving each owner their own stale unsent draft too.
    monkeypatch.delenv("FOLLOWUP_LIVE", raising=False)
    dan_event = _watch_in_registry(owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
                                   ask="Dan's would-draft ask", deadline="2026-08-07")
    ka_event = _watch_in_registry(owner="ka@negevlabs.com", mailbox="ka@negevlabs.com",
                                  ask="Ka's would-draft ask", deadline="2026-08-07")
    dan_unsent = _watch_in_registry(owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
                                    ask="Dan's stale unsent ask", deadline="2026-12-31")
    dan_unsent["drafts"] = [{"message_id": "d-dan-old", "web_link": "https://outlook.example/d-dan",
                             "created": "2026-08-01T00:00:00Z", "sent": False}]
    ka_unsent = _watch_in_registry(owner="ka@negevlabs.com", mailbox="ka@negevlabs.com",
                                   ask="Ka's stale unsent ask", deadline="2026-12-31")
    ka_unsent["drafts"] = [{"message_id": "d-ka-old", "web_link": "https://outlook.example/d-ka",
                            "created": "2026-08-01T00:00:00Z", "sent": False}]
    fue._save_registry({"watches": [dan_event, ka_event, dan_unsent, ka_unsent]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))

    def _graph_get_router(url, params=None):
        if url.endswith("/messages"):
            return {"value": []}  # check_replies: no new thread messages
        return {"id": url.rsplit("/", 1)[-1], "isDraft": True}  # sweep_unsent per-draft check
    monkeypatch.setattr(eps, "graph_get", _graph_get_router)
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    sent = _sendmail_capture(monkeypatch)
    fue.run_daily()
    assert len(sent) == 2
    by_owner = {s["message"]["toRecipients"][0]["emailAddress"]["address"]:
                s["message"]["body"]["content"] for s in sent}
    assert "Dan's would-draft ask" in by_owner["dan@negevlabs.com"]
    assert "Dan's stale unsent ask" in by_owner["dan@negevlabs.com"]
    assert "Ka's would-draft ask" not in by_owner["dan@negevlabs.com"]
    assert "Ka's stale unsent ask" not in by_owner["dan@negevlabs.com"]
    assert "Ka's would-draft ask" in by_owner["ka@negevlabs.com"]
    assert "Ka's stale unsent ask" in by_owner["ka@negevlabs.com"]
    assert "Dan's would-draft ask" not in by_owner["ka@negevlabs.com"]
    assert "Dan's stale unsent ask" not in by_owner["ka@negevlabs.com"]


def test_run_daily_sends_report_for_unsent_only_no_new_events(monkeypatch, fue_files):
    # Flip side of "nothing happened -> no email": nothing NEW happened
    # this run (deadline far in the future, no replies), but an earlier
    # draft is still sitting unsent -- spec step 4 requires it be reported
    # "daily until sent or cancelled", so a report must still go out with
    # zero new events.
    w = _watch_in_registry(deadline="2026-12-31")
    w["drafts"] = [{"message_id": "d-old", "web_link": "https://outlook.example/d-old",
                     "created": "2026-08-01T00:00:00Z", "sent": False}]
    fue._save_registry({"watches": [w]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))

    def _graph_get_router(url, params=None):
        if url.endswith("/messages"):
            return {"value": []}  # check_replies: no new thread messages
        return {"id": "d-old", "isDraft": True}  # sweep_unsent's per-draft check
    monkeypatch.setattr(eps, "graph_get", _graph_get_router)
    sent = _sendmail_capture(monkeypatch)
    out = fue.run_daily()
    assert out["reports"] == 1 and len(sent) == 1
    body = sent[0]["message"]["body"]["content"]
    assert "Still unsent" in body
    assert "https://outlook.example/d-old" in body
    assert "2026-08-01" in body


def test_run_daily_alert_cc_not_duplicated_when_owner_is_alert_cc(monkeypatch, fue_files):
    # Edge case in the CC logic the brief's own test does not reach: if
    # the owner IS FOLLOWUP_ALERT_CC, the escalation must not add a
    # duplicate recipient.
    fue._save_registry({"watches": [
        _watch_in_registry(owner=fue.ALERT_CC, mailbox=fue.ALERT_CC,
                           deadline="2026-08-07", nudges_sent=3)]})
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    sent = _sendmail_capture(monkeypatch)
    fue.run_daily()
    addrs = [r["emailAddress"]["address"] for r in sent[0]["message"]["toRecipients"]]
    assert addrs == [fue.ALERT_CC]  # not [ALERT_CC, ALERT_CC]


def test_run_daily_send_failure_for_one_owner_does_not_block_others(monkeypatch, fue_files):
    reg = {"watches": [
        _watch_in_registry(owner="dan@negevlabs.com", mailbox="dan@negevlabs.com",
                           deadline="2026-08-07"),
        _watch_in_registry(owner="ka@negevlabs.com", mailbox="ka@negevlabs.com",
                           deadline="2026-08-07"),
    ]}
    fue._save_registry(reg)
    monkeypatch.setattr(fue, "_today_il", lambda: date(2026, 8, 7))
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": []})
    monkeypatch.setattr(ld, "_call_claude_text", lambda *a, **kw: "Reminder body")
    sent = []
    def _post(url, json_body):
        if url.endswith("/sendMail"):
            to_addr = json_body["message"]["toRecipients"][0]["emailAddress"]["address"]
            if to_addr == "dan@negevlabs.com":
                raise RuntimeError("simulated Graph 503")
            sent.append(json_body)
            return {}
        if url.endswith("/createReplyAll"):
            return {"id": "d1", "webLink": "https://outlook.example/d1"}
        return {}
    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", lambda url, json_body: {})
    monkeypatch.setattr(eps, "graph_delete", lambda url: {})
    out = fue.run_daily()
    assert out["reports"] == 1  # only ka@ succeeded
    assert len(sent) == 1
    assert sent[0]["message"]["toRecipients"][0]["emailAddress"]["address"] == "ka@negevlabs.com"


def test_build_report_renders_all_section_headings():
    # CARRY-FORWARD (must-satisfy #3): build_report must actually RENDER
    # the exhausted event type (not just compute it) -- and, more broadly,
    # every section the spec's step 4 promises (new drafts, report-only
    # woulds, replies of both verdicts, escalations, still-unsent). None
    # of the brief's own given tests exercise the replies/exhausted/unsent
    # sections at the build_report level at all.
    events = [
        {"type": "draft", "owner": "dan@negevlabs.com", "watch_id": "fw_draft001",
         "ask": "draft ask", "recipients": ["cro@example.com"], "body": "<p>d</p>",
         "web_link": "https://outlook.example/draft1", "escalation": 1},
        {"type": "would_draft", "owner": "dan@negevlabs.com", "watch_id": "fw_would001",
         "ask": "would ask", "recipients": ["cro@example.com"], "body": "<p>w</p>",
         "escalation": 1},
        {"type": "reply_answered", "owner": "dan@negevlabs.com", "watch_id": "fw_ans0001",
         "ask": "answered ask", "who": "cro@example.com", "when": "2026-08-06T09:00:00Z"},
        {"type": "reply_paused", "owner": "dan@negevlabs.com", "watch_id": "fw_pau0001",
         "ask": "paused ask", "who": "cro@example.com", "when": "2026-08-06T09:00:00Z"},
        {"type": "exhausted", "owner": "dan@negevlabs.com", "watch_id": "fw_exh0001",
         "ask": "exhausted ask", "recipients": ["cro@example.com"], "escalation": 3},
    ]
    unsent = [{"owner": "dan@negevlabs.com", "watch_id": "fw_uns0001", "ask": "unsent ask",
               "web_link": "https://outlook.example/old", "created": "2026-08-01T00:00:00Z"}]
    out = fue.build_report("dan@negevlabs.com", events, unsent)
    assert "New reminder drafts" in out and "fw_draft001" in out
    assert "report-only" in out.lower() and "fw_would001" in out
    assert "Replies detected" in out
    assert "fw_ans0001" in out and "answered -- watch closed" in out
    assert "fw_pau0001" in out and "did not answer" in out
    assert "Escalations" in out and "fw_exh0001" in out and "3 reminders went unanswered" in out
    assert "Still unsent" in out and "fw_uns0001" in out and "2026-08-01" in out


def test_build_report_escapes_counterparty_controlled_text():
    # Global constraint: HTML built here is emailed to a human, so anything
    # that came from a counterparty (subjects, ask text, names) must be
    # escaped -- a malformed subject/ask must not break or inject into the
    # report.
    events = [{"type": "would_draft", "owner": "dan@negevlabs.com", "watch_id": "fw_xss0001",
               "ask": "<script>evil()</script>", "recipients": ["<img src=x onerror=alert(1)>"],
               "body": "<p>safe</p>", "escalation": 1}]
    out = fue.build_report("dan@negevlabs.com", events, [])
    assert "<script>evil()</script>" not in out
    assert "&lt;script&gt;" in out
    assert "<img src=x onerror=alert(1)>" not in out


def test_send_email_exempt_call_is_locked_down():
    # Companion to the narrowed static guard (test_create_draft_and_
    # module_never_call_send above): pins the ONE permitted sendMail call
    # down tight enough that it cannot be repurposed later to reach a
    # counterparty or send an existing draft. Fixed to SARA_MAILBOX
    # (Sara's own inbox, i.e. BOT_SENDER_EMAIL) and reachable only via a
    # recipient list, subject, and body -- no mailbox/watch/draft-id
    # parameter for a future edit to plug an owner-mailbox or draft id
    # into.
    import inspect
    params = list(inspect.signature(fue._send_email).parameters)
    assert params == ["to_list", "subject", "html_body"]

    src = inspect.getsource(fue._send_email).lower()
    assert "sendmail" in src
    assert "/users/{sara_mailbox}/sendmail" in src  # Sara's own mailbox, never a per-watch one
    for reachable_via_state in ("draft_id", "message_id", "['mailbox']", '["mailbox"]',
                                ".get(\"mailbox\")", ".get('mailbox')", "/messages/"):
        assert reachable_via_state not in src, (
            f"_send_email must not be reachable via {reachable_via_state!r}")


def test_main_default_runs_check_only(monkeypatch):
    calls = []
    monkeypatch.setattr(fue, "run_intake", lambda dry_run=False: calls.append(("intake", dry_run)) or {})
    monkeypatch.setattr(fue, "run_daily", lambda dry_run=False: calls.append(("check", dry_run)) or {})
    monkeypatch.setattr("sys.argv", ["followup_engine.py"])
    fue.main()
    assert calls == [("check", False)]


def test_main_intake_only_flag_skips_check(monkeypatch):
    calls = []
    monkeypatch.setattr(fue, "run_intake", lambda dry_run=False: calls.append(("intake", dry_run)) or {})
    monkeypatch.setattr(fue, "run_daily", lambda dry_run=False: calls.append(("check", dry_run)) or {})
    monkeypatch.setattr("sys.argv", ["followup_engine.py", "--intake", "--dry-run"])
    fue.main()
    assert calls == [("intake", True)]


def test_main_intake_and_check_flags_run_both(monkeypatch):
    calls = []
    monkeypatch.setattr(fue, "run_intake", lambda dry_run=False: calls.append(("intake", dry_run)) or {})
    monkeypatch.setattr(fue, "run_daily", lambda dry_run=False: calls.append(("check", dry_run)) or {})
    monkeypatch.setattr("sys.argv", ["followup_engine.py", "--intake", "--check"])
    fue.main()
    assert calls == [("intake", False), ("check", False)]
