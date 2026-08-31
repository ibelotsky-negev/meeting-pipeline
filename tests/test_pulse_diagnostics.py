# Tests for the Weekly Pulse parse/diagnostics fixes (2.30.1).
#
# Context -- the 2026-08-30 pulse run (archive 2026-W35):
#   * Pass 3 emitted "Failed to parse Claude JSON" then "0G 0Y 0R", because
#     _pulse_parse_json only stripped a markdown fence when the reply STARTED
#     with one. Claude had opened with prose and fenced the payload partway
#     down, so a correctly-extracted meeting signal was thrown away.
#   * Teams collection reported 0 messages. /groups returned 403, and every
#     per-chat message read was a bare `continue` with no logging, so a total
#     failure was indistinguishable from a quiet week.
#   * /pulse/check reported ready:true throughout, because it probed
#     /users/{id}/joinedTeams instead of the calls the pulse actually makes.
import logging
import threading

import pytest

import app as app_module


# ----------------------------------------------------------------------
# B1: tolerant JSON parsing
# ----------------------------------------------------------------------

# Trimmed from the real Pass 3 reply archived at /data/pulse/2026-W35.json.
# Prose first, payload in a fenced block, trailing prose after it.
REAL_PASS3_REPLY = '''Looking at these meeting summaries, I need to assess what
is in scope for the analysis period.

**Meeting 1 (Kesha <> Ivan):** This appears to be about an external project.
**Meeting 2 (Ken-Vitaly, 16min):** No summary content provided.

Given the extremely thin in-scope signal, here is the extracted output:

```json
{
  "green": [],
  "yellow": [
    "FFG Austria grant submission targeting September remains a planned action item"
  ],
  "red": [],
  "key_entities": ["Ariadne Bio", "FFG Austria grant", "Herz und Heller"]
}
```

**Notes on exclusions:**
- The Brainsway pilot is entirely out of scope.
'''


class TestPulseParseJson:
    def test_bare_json_object(self):
        out = app_module._pulse_parse_json('{"green": ["a"], "yellow": [], "red": []}')
        assert out["green"] == ["a"]

    def test_leading_fence_still_parses(self):
        """The one shape the old parser handled -- must not regress."""
        raw = '```json\n{"green": ["kept"], "yellow": [], "red": []}\n```'
        assert app_module._pulse_parse_json(raw)["green"] == ["kept"]

    def test_bare_fence_without_language_tag(self):
        raw = '```\n{"green": ["kept"], "yellow": [], "red": []}\n```'
        assert app_module._pulse_parse_json(raw)["green"] == ["kept"]

    def test_prose_then_fenced_json_is_recovered(self):
        """The 2026-08-30 Pass 3 failure. Signal must survive."""
        out = app_module._pulse_parse_json(REAL_PASS3_REPLY)
        assert "_raw" not in out
        assert out["yellow"] == [
            "FFG Austria grant submission targeting September remains a planned action item"
        ]
        assert out["key_entities"] == ["Ariadne Bio", "FFG Austria grant", "Herz und Heller"]

    def test_prose_then_unfenced_object_is_recovered(self):
        raw = ('Here is my analysis of the week.\n\n'
               '{"green": ["shipped"], "yellow": [], "red": [], "key_entities": []}\n\n'
               'Let me know if you need more detail.')
        assert app_module._pulse_parse_json(raw)["green"] == ["shipped"]

    def test_unparseable_returns_empty_signals_and_keeps_raw(self, caplog):
        with caplog.at_level(logging.WARNING):
            out = app_module._pulse_parse_json("I could not complete that request.")
        assert out["green"] == [] and out["yellow"] == [] and out["red"] == []
        assert out["_raw"] == "I could not complete that request."
        # The raw head must reach the log -- a bare "failed to parse" line is
        # what made the 2026-08-30 loss undiagnosable from logs alone.
        assert "Failed to parse Claude JSON" in caplog.text
        assert "could not complete" in caplog.text

    @pytest.mark.parametrize("raw", ["", "   ", None])
    def test_empty_input_is_safe(self, raw):
        out = app_module._pulse_parse_json(raw)
        assert out["green"] == [] and out["yellow"] == [] and out["red"] == []

    def test_json_array_is_not_accepted_as_signals(self):
        """A top-level list has no green/yellow/red -- must not be returned."""
        out = app_module._pulse_parse_json('["a", "b"]')
        assert out["green"] == []
        assert "_raw" in out

    def test_downstream_keys_always_present(self):
        """pulse_analyze does .get('green', []) on the result; never return
        something that breaks the caller."""
        for raw in ["garbage", '{"unexpected": 1}', REAL_PASS3_REPLY]:
            out = app_module._pulse_parse_json(raw)
            assert isinstance(out, dict)
            assert isinstance(out.get("green", []), list)


# ----------------------------------------------------------------------
# B3: palomar-labs.com is part of the team
# ----------------------------------------------------------------------

class TestPalomarDomain:
    def test_meeting_organized_from_palomar_counts_as_team(self):
        assert app_module._pulse_is_team_meeting(["ken@palomar-labs.com"]) is True

    def test_legacy_domains_still_count(self):
        assert app_module._pulse_is_team_meeting(["bk@negevlabs.com"]) is True
        assert app_module._pulse_is_team_meeting(["x@ariadnebio.com"]) is True

    def test_external_only_meeting_is_excluded(self):
        assert app_module._pulse_is_team_meeting(["someone@example.com"]) is False

    def test_palomar_in_from_counts_as_team_email(self):
        msg = {"from": {"emailAddress": {"address": "ken@palomar-labs.com"}},
               "toRecipients": [{"emailAddress": {"address": "vc@example.com"}}]}
        assert app_module._pulse_has_team_in_from_or_to(msg) is True

    def test_palomar_in_to_counts_as_team_email(self):
        msg = {"from": {"emailAddress": {"address": "vc@example.com"}},
               "toRecipients": [{"emailAddress": {"address": "shlomi@palomar-labs.com"}}]}
        assert app_module._pulse_has_team_in_from_or_to(msg) is True

    def test_fully_external_email_still_excluded(self):
        msg = {"from": {"emailAddress": {"address": "a@example.com"}},
               "toRecipients": [{"emailAddress": {"address": "b@example.org"}}]}
        assert app_module._pulse_has_team_in_from_or_to(msg) is False


# ----------------------------------------------------------------------
# helpers for the HTTP-shaped tests
# ----------------------------------------------------------------------

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
    """Point app.py's requests.get at a URL-routing callable."""
    monkeypatch.setattr(app_module.requests, "get",
                        lambda url, **kw: handler(url))


# ----------------------------------------------------------------------
# B5: mailboxes that do not exist are summarized, not screamed about
# ----------------------------------------------------------------------

class TestUnprovisionedMailbox:
    def test_404_reports_the_address(self, monkeypatch):
        route(monkeypatch, lambda url: FakeResponse(404, text="not found"))
        emails, scanned, cc, missing = app_module._pulse_fetch_user_emails(
            {"id": "u1", "mail": "saar@negevlabs.com"}, "S", "E", {})
        assert emails == [] and scanned == 0 and cc == 0
        assert missing == "saar@negevlabs.com"

    def test_success_reports_no_missing_mailbox(self, monkeypatch):
        msg = {"subject": "Ichilov EC resubmission plan",
               "bodyPreview": "package is ready",
               "from": {"emailAddress": {"address": "bk@negevlabs.com", "name": "Ken"}},
               "toRecipients": [{"emailAddress": {"address": "x@example.com"}}],
               "receivedDateTime": "2026-08-28T10:00:00Z"}
        route(monkeypatch, lambda url: FakeResponse(200, {"value": [msg]}))
        emails, scanned, cc, missing = app_module._pulse_fetch_user_emails(
            {"id": "u1", "mail": "bk@negevlabs.com"}, "S", "E", {})
        assert missing is None
        assert scanned == 1 and len(emails) == 1

    def test_403_still_returns_four_values(self, monkeypatch):
        """Permission failure is distinct from a missing mailbox."""
        route(monkeypatch, lambda url: FakeResponse(403, text="denied"))
        emails, scanned, cc, missing = app_module._pulse_fetch_user_emails(
            {"id": "u1", "mail": "bk@negevlabs.com"}, "S", "E", {})
        assert missing is None and emails == []

    def test_collect_emails_summarizes_missing_mailboxes_once(self, monkeypatch, caplog):
        users = [{"id": "u1", "mail": "bk@negevlabs.com"},
                 {"id": "u2", "mail": "saar@negevlabs.com"},
                 {"id": "u3", "mail": "alp@negevlabs.com"}]
        monkeypatch.setattr(app_module, "pulse_get_team_users", lambda: users)
        monkeypatch.setattr(app_module, "get_ms_graph_token", lambda: "tok")

        def handler(url):
            if "u1" in url:
                return FakeResponse(200, {"value": []})
            return FakeResponse(404, text="not found")

        route(monkeypatch, handler)
        with caplog.at_level(logging.INFO):
            out = app_module.pulse_collect_emails(
                app_module.datetime(2026, 8, 23, tzinfo=app_module.timezone.utc),
                app_module.datetime(2026, 8, 30, tzinfo=app_module.timezone.utc))
        assert out == []
        text = caplog.text
        assert "2 directory user(s) have no mailbox" in text
        assert "alp@negevlabs.com, saar@negevlabs.com" in text
        # One summary line, not one WARNING per dead account.
        assert text.count("have no mailbox") == 1


# ----------------------------------------------------------------------
# B2: Teams failures are no longer silent
# ----------------------------------------------------------------------

class TestTeamsFailuresAreLogged:
    def _chat_user(self):
        return {"id": "u1", "mail": "bk@negevlabs.com"}

    def test_refused_chat_messages_are_logged_with_status(self, monkeypatch, caplog):
        def handler(url):
            if "/chats?" in url or url.endswith("/chats"):
                return FakeResponse(200, {"value": [
                    {"id": "c1", "chatType": "oneOnOne"},
                    {"id": "c2", "chatType": "group"}]})
            return FakeResponse(403, text="Forbidden: protected API")

        route(monkeypatch, handler)
        with caplog.at_level(logging.WARNING):
            out = app_module._pulse_fetch_user_chats(
                self._chat_user(), "2026-08-23T00:00:00Z", {}, set(), threading.Lock())
        assert out == []
        assert "Chat messages unreadable for bk@negevlabs.com" in caplog.text
        assert "403" in caplog.text
        assert "protected API" in caplog.text
        # Summarized once per user, not once per chat.
        assert caplog.text.count("Chat messages unreadable") == 1

    def test_successful_chats_log_nothing(self, monkeypatch, caplog):
        def handler(url):
            if "/chats?" in url or url.endswith("/chats"):
                return FakeResponse(200, {"value": [{"id": "c1", "chatType": "oneOnOne"}]})
            return FakeResponse(200, {"value": [{
                "messageType": "message",
                "createdDateTime": "2026-08-25T10:00:00Z",
                "body": {"content": "<p>The Ichilov package is ready to file.</p>"}}]})

        route(monkeypatch, handler)
        with caplog.at_level(logging.WARNING):
            out = app_module._pulse_fetch_user_chats(
                self._chat_user(), "2026-08-23T00:00:00Z", {}, set(), threading.Lock())
        assert len(out) == 1
        assert "Ichilov package" in out[0]["content_preview"]
        assert "unreadable" not in caplog.text

    def test_refused_channel_messages_are_logged(self, monkeypatch, caplog):
        route(monkeypatch, lambda url: FakeResponse(403, text="Forbidden"))
        with caplog.at_level(logging.WARNING):
            out = app_module._pulse_fetch_channel_messages(
                "t1", "Negev", {"id": "ch1", "displayName": "General"},
                "2026-08-23T00:00:00Z", {})
        assert out == []
        assert "Channel messages returned 403" in caplog.text
        assert "Negev/General" in caplog.text


# ----------------------------------------------------------------------
# B4: /pulse/check probes the calls the pulse really makes
# ----------------------------------------------------------------------

class TestPulseCheckHonesty:
    def _setup(self, monkeypatch, handler):
        monkeypatch.setattr(app_module, "get_ms_graph_token", lambda: "tok")
        monkeypatch.setattr(app_module, "pulse_get_team_users",
                            lambda: [{"id": "u1", "mail": "bk@negevlabs.com"}])
        route(monkeypatch, handler)

    def test_groups_403_reports_teams_not_ready(self, monkeypatch, flask_client):
        """The 2026-08-30 state: everything green while /groups was 403-ing."""
        def handler(url):
            if "/groups" in url:
                return FakeResponse(403, text="Authorization_RequestDenied")
            if "/messages" in url:
                return FakeResponse(200, {"value": [{"id": "m1"}]})
            if "/chats" in url:
                return FakeResponse(200, {"value": [{"id": "c1"}]})
            return FakeResponse(200, {"value": []})

        self._setup(monkeypatch, handler)
        body = flask_client.get("/pulse/check").get_json()
        assert body["permissions"]["Group.Read.All"] is False
        assert body["teams_ready"] is False
        assert body["ready"] is False
        # The mail path is healthy and must be reported as such.
        assert body["mail_ready"] is True
        assert "Authorization_RequestDenied" in body["diagnostics"]["Group.Read.All"]

    def test_all_green_reports_ready(self, monkeypatch, flask_client):
        def handler(url):
            if "/groups" in url:
                return FakeResponse(200, {"value": [{"id": "t1", "displayName": "Negev"}]})
            if "/channels" in url:
                return FakeResponse(200, {"value": [{"id": "ch1"}]})
            if "/chats" in url:
                return FakeResponse(200, {"value": [{"id": "c1"}]})
            return FakeResponse(200, {"value": [{"id": "m1"}]})

        self._setup(monkeypatch, handler)
        body = flask_client.get("/pulse/check").get_json()
        assert body["permissions"]["Group.Read.All"] is True
        assert body["permissions"]["ChannelMessage.Read.All"] is True
        assert body["permissions"]["Chat.Messages.Read"] is True
        assert body["teams_ready"] is True
        assert body["ready"] is True

    def test_chat_listing_ok_but_messages_refused(self, monkeypatch, flask_client):
        """Exactly the gap the old check could not see."""
        def handler(url):
            if "/groups" in url:
                return FakeResponse(200, {"value": []})
            if "/chats/" in url and "/messages" in url:
                return FakeResponse(403, text="protected API")
            if "/chats" in url:
                return FakeResponse(200, {"value": [{"id": "c1"}]})
            return FakeResponse(200, {"value": [{"id": "m1"}]})

        self._setup(monkeypatch, handler)
        body = flask_client.get("/pulse/check").get_json()
        assert body["permissions"]["Chat.Read.All"] is True
        assert body["permissions"]["Chat.Messages.Read"] is False
        assert "protected API" in body["diagnostics"]["Chat.Messages.Read"]
