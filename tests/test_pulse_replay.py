# Tests for pulse REPLAY runs and the chat-message early exit (2.33.0).
#
# Replay = re-analyze a PAST window and mail it to someone specific, to compare
# against what the original run produced. Two things it must NOT do:
#   * apply briefing-book updates (the original run already applied them --
#     re-applying double-counts)
#   * archive (that would overwrite the very report being compared against)
#
# Early exit on chat messages is only sound because the walk asks for
# $orderby=createdDateTime desc explicitly. Probed live 2026-09-01: the DEFAULT
# order sorts by lastModifiedDateTime, so an edited old message floats to the
# top and 3 of 6 sampled chats came back unsorted by createdDateTime. With the
# explicit $orderby, 8/8 were strictly descending. Channel messages REJECT
# $orderby with a 400, which is why they have no early exit.
import datetime
import threading
from unittest.mock import MagicMock

import pytest

import app as app_module

UTC = datetime.timezone.utc


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("raise_for_status on %d" % self.status_code)


def route(monkeypatch, handler):
    monkeypatch.setattr(app_module.requests, "get", lambda url, **kw: handler(url))


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------

class TestParseEnd:
    def test_date_only(self):
        dt = app_module._pulse_parse_end("2026-08-30")
        assert (dt.year, dt.month, dt.day) == (2026, 8, 30)
        assert dt.tzinfo is not None, "must be tz-aware or the subtraction blows up"

    def test_full_iso_z(self):
        dt = app_module._pulse_parse_end("2026-08-30T19:00:00Z")
        assert dt.hour == 19 and dt.utcoffset().total_seconds() == 0

    def test_iso_with_offset_is_normalized_to_utc(self):
        dt = app_module._pulse_parse_end("2026-08-30T21:00:00+02:00")
        assert dt.hour == 19 and dt.utcoffset().total_seconds() == 0

    @pytest.mark.parametrize("raw", [None, "", "   ", "not-a-date", "30/08/2026"])
    def test_unparseable_is_none(self, raw):
        assert app_module._pulse_parse_end(raw) is None


class TestParseRecipients:
    def test_single(self):
        assert app_module._pulse_parse_recipients("bk@negevlabs.com") == \
            ["bk@negevlabs.com"]

    def test_comma_separated_and_trimmed(self):
        assert app_module._pulse_parse_recipients(" a@b.com , c@d.com ") == \
            ["a@b.com", "c@d.com"]

    @pytest.mark.parametrize("raw", [None, "", "  ", "notanemail", ","])
    def test_absent_or_invalid_is_none(self, raw):
        assert app_module._pulse_parse_recipients(raw) is None


# ----------------------------------------------------------------------
# replay semantics
# ----------------------------------------------------------------------

@pytest.fixture
def run_harness(monkeypatch, pulse_files):
    """Stub the whole pipeline; expose the mocks the assertions care about."""
    seen = {}

    def fake_collect(kind):
        def _inner(start, end):
            seen[kind] = (start, end)
            return []
        return _inner

    monkeypatch.setattr(app_module, "pulse_collect_emails", fake_collect("emails"))
    monkeypatch.setattr(app_module, "pulse_collect_teams", fake_collect("teams"))
    monkeypatch.setattr(app_module, "pulse_collect_meetings", fake_collect("meetings"))
    monkeypatch.setattr(app_module, "pulse_analyze",
                        lambda *a, **k: ("the report", {},
                                         [{"section": "S", "confidence": "high"}]))
    send = MagicMock()
    archive = MagicMock()
    briefing = MagicMock(return_value=[])
    monkeypatch.setattr(app_module, "pulse_send_email", send)
    monkeypatch.setattr(app_module, "pulse_archive", archive)
    monkeypatch.setattr(app_module, "pulse_update_briefing_book", briefing)
    return {"seen": seen, "send": send, "archive": archive, "briefing": briefing}


class TestReplayRun:
    END = datetime.datetime(2026, 8, 30, 19, 0, 0, tzinfo=UTC)

    def test_replay_uses_the_requested_window(self, run_harness):
        app_module._pulse_run_inner(7, False, end_dt=self.END,
                                    recipients=["bk@negevlabs.com"])
        start, end = run_harness["seen"]["emails"]
        assert end == self.END
        assert start == self.END - datetime.timedelta(days=7)
        # The original 2026-W35 report covered exactly this window.
        assert start.strftime("%Y-%m-%d") == "2026-08-23"

    def test_replay_sends_only_to_the_override(self, run_harness):
        app_module._pulse_run_inner(7, False, end_dt=self.END,
                                    recipients=["bk@negevlabs.com"])
        assert run_harness["send"].call_count == 1
        assert run_harness["send"].call_args.kwargs["recipients"] == \
            ["bk@negevlabs.com"]

    def test_replay_does_not_archive(self, run_harness):
        """Archiving would overwrite the original week's stored report."""
        app_module._pulse_run_inner(7, False, end_dt=self.END,
                                    recipients=["bk@negevlabs.com"])
        assert run_harness["archive"].call_count == 0

    def test_replay_does_not_reapply_briefing_updates(self, run_harness):
        """The original run already applied them; re-applying double-counts."""
        app_module._pulse_run_inner(7, False, end_dt=self.END,
                                    recipients=["bk@negevlabs.com"])
        assert run_harness["briefing"].call_count == 0

    def test_recipients_alone_marks_a_replay(self, run_harness):
        app_module._pulse_run_inner(7, False, recipients=["bk@negevlabs.com"])
        assert run_harness["archive"].call_count == 0
        assert run_harness["briefing"].call_count == 0

    def test_normal_run_still_archives_and_applies(self, run_harness):
        """A replay must not change what an ordinary weekly run does."""
        app_module._pulse_run_inner(7, False)
        assert run_harness["archive"].call_count == 1
        assert run_harness["briefing"].call_count == 1
        assert run_harness["send"].call_args.kwargs["recipients"] is None

    def test_dry_run_replay_sends_nothing(self, run_harness):
        app_module._pulse_run_inner(7, True, end_dt=self.END,
                                    recipients=["bk@negevlabs.com"])
        assert run_harness["send"].call_count == 0
        assert run_harness["archive"].call_count == 0


class TestSendEmailRecipients:
    def _send(self, monkeypatch, recipients):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["to"] = [r["emailAddress"]["address"]
                              for r in json["message"]["toRecipients"]]
            return FakeResponse(202)

        monkeypatch.setattr(app_module, "get_ms_graph_token", lambda: "tok")
        monkeypatch.setattr(app_module.requests, "post", fake_post)
        app_module.pulse_send_email("# report", datetime.datetime(2026, 8, 23, tzinfo=UTC),
                                    datetime.datetime(2026, 8, 30, tzinfo=UTC),
                                    recipients=recipients)
        return captured["to"]

    def test_override_replaces_the_distribution_list(self, monkeypatch):
        to = self._send(monkeypatch, ["bk@negevlabs.com"])
        assert to == ["bk@negevlabs.com"]
        assert "vu@negevcap.com" not in to

    def test_default_is_the_configured_list(self, monkeypatch):
        assert self._send(monkeypatch, None) == list(app_module.PULSE_RECIPIENTS)


# ----------------------------------------------------------------------
# chat message early exit
# ----------------------------------------------------------------------

class TestChatMessageEarlyExit:
    WINDOW = "2026-08-30T00:00:00Z"

    def _chat_page(self):
        return {"value": [{"id": "c1", "chatType": "oneOnOne",
                           "lastMessagePreview": {
                               "createdDateTime": "2026-08-31T12:00:00Z"}}]}

    def test_message_walk_requests_explicit_ordering(self, monkeypatch):
        """Without $orderby the default sorts by lastModifiedDateTime, so an
        edited old message floats up and the early exit would cut in-window
        messages. The explicit ordering is what makes it safe."""
        urls = []

        def handler(url):
            urls.append(url)
            if "/messages" in url:
                return FakeResponse(200, {"value": []})
            return FakeResponse(200, self._chat_page())

        route(monkeypatch, handler)
        app_module._pulse_fetch_user_chats(
            {"id": "u1", "mail": "ken@palomar-labs.com"}, self.WINDOW, {},
            set(), threading.Lock())
        msg_urls = [u for u in urls if "/messages" in u]
        assert msg_urls, "messages were never requested"
        assert "$orderby=createdDateTime desc" in msg_urls[0]

    def test_walk_stops_at_first_out_of_window_message(self, monkeypatch):
        urls = []
        fresh = {"messageType": "message", "createdDateTime": "2026-08-31T09:00:00Z",
                 "body": {"content": "<p>in-window and long enough to keep</p>"}}
        stale = {"messageType": "message", "createdDateTime": "2026-07-04T09:00:00Z",
                 "body": {"content": "<p>older than the window entirely</p>"}}

        def handler(url):
            urls.append(url)
            if url == "msg-page-2":
                return FakeResponse(200, {"value": [fresh]})
            if "/messages" in url:
                return FakeResponse(200, {"value": [fresh, stale],
                                          "@odata.nextLink": "msg-page-2"})
            return FakeResponse(200, self._chat_page())

        route(monkeypatch, handler)
        out, stats = app_module._pulse_fetch_user_chats(
            {"id": "u1", "mail": "ken@palomar-labs.com"}, self.WINDOW, {},
            set(), threading.Lock())
        assert "msg-page-2" not in urls, "paged past the window unnecessarily"
        assert len(out) == 1
        assert stats["truncated"] == 0, "an early exit is not a truncation"

    def test_undated_message_does_not_end_the_walk(self, monkeypatch):
        undated = {"messageType": "message",
                   "body": {"content": "<p>no timestamp but still real text</p>"}}
        fresh = {"messageType": "message", "createdDateTime": "2026-08-31T09:00:00Z",
                 "body": {"content": "<p>in-window and long enough to keep</p>"}}

        def handler(url):
            if "/messages" in url:
                return FakeResponse(200, {"value": [undated, fresh]})
            return FakeResponse(200, self._chat_page())

        route(monkeypatch, handler)
        out, _ = app_module._pulse_fetch_user_chats(
            {"id": "u1", "mail": "ken@palomar-labs.com"}, self.WINDOW, {},
            set(), threading.Lock())
        assert len(out) == 2

    def test_channel_walk_has_no_orderby(self, monkeypatch):
        """Graph rejects $orderby on channel messages with a 400 -- asserting
        this so nobody 'improves' the channel path to match the chat one."""
        urls = []

        def handler(url):
            urls.append(url)
            return FakeResponse(200, {"value": []})

        route(monkeypatch, handler)
        app_module._pulse_fetch_channel_messages(
            "t1", "Negev", {"id": "ch1", "displayName": "General"}, self.WINDOW, {})
        assert urls and "$orderby" not in urls[0]
