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
