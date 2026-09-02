# Tests for the Teams window upper bound and the not-JSON retry (2.33.1).
#
# WINDOW: Teams collection only ever had a LOWER bound. On the weekly cron the
# window ends at "now" so nothing could leak, which is why this survived. A
# 2026-09-01 replay of the Aug 23-30 week swept in two extra days of traffic.
# Emails (receivedDateTime ge/le) and Fireflies (fromDate/toDate) were bounded
# on both sides all along.
#
# RETRY: on 2026-09-01 the briefing pass answered a JSON request with a 17K-char
# markdown report and proposed zero updates. The parser caught it cleanly, but a
# caught failure is still a whole pass contributing nothing -- and a silent zero
# is indistinguishable from "nothing happened".
import logging
import threading

import pytest

import app as app_module

START = "2026-08-23T19:00:00Z"
END = "2026-08-30T19:00:00Z"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self):
        return self._payload


def route(monkeypatch, handler):
    monkeypatch.setattr(app_module.requests, "get", lambda url, **kw: handler(url))


def msg(stamp, body="a message long enough to survive the filter"):
    return {"messageType": "message", "createdDateTime": stamp,
            "body": {"content": "<p>%s</p>" % body}}


# ----------------------------------------------------------------------
# window
# ----------------------------------------------------------------------

class TestMessageWindow:
    def test_inside_window_kept(self):
        assert app_module._pulse_msg_in_window("2026-08-25T10:00:00Z", START, END)

    def test_before_window_dropped(self):
        assert not app_module._pulse_msg_in_window("2026-08-01T10:00:00Z", START, END)

    def test_after_window_dropped(self):
        """The actual bug -- a replay pulled in everything since."""
        assert not app_module._pulse_msg_in_window("2026-09-01T10:00:00Z", START, END)

    def test_no_end_means_no_upper_bound(self):
        assert app_module._pulse_msg_in_window("2026-09-01T10:00:00Z", START, None)

    def test_undated_message_is_kept(self):
        assert app_module._pulse_msg_in_window("", START, END)

    def test_boundaries_are_inclusive(self):
        assert app_module._pulse_msg_in_window(START, START, END)
        assert app_module._pulse_msg_in_window(END, START, END)


class TestChatWindowApplied:
    def _chats(self):
        return {"value": [{"id": "c1", "chatType": "oneOnOne",
                           "lastMessagePreview": {
                               "createdDateTime": "2026-08-29T12:00:00Z"}}]}

    def _run(self, monkeypatch, messages, end_iso):
        def handler(url):
            if "/messages" in url:
                return FakeResponse(200, {"value": messages})
            return FakeResponse(200, self._chats())

        route(monkeypatch, handler)
        return app_module._pulse_fetch_user_chats(
            {"id": "u1", "mail": "ken@palomar-labs.com"}, START, {}, set(),
            threading.Lock(), end_iso=end_iso)

    def test_messages_after_the_window_are_dropped(self, monkeypatch):
        out, _ = self._run(monkeypatch,
                           [msg("2026-08-25T10:00:00Z", "in window keep this one"),
                            msg("2026-09-01T10:00:00Z", "after window drop this one")],
                           END)
        assert len(out) == 1
        assert "in window" in out[0]["content_preview"]

    def test_without_end_iso_nothing_is_dropped(self, monkeypatch):
        """Back-compat: the weekly cron passes a window ending at now."""
        out, _ = self._run(monkeypatch,
                           [msg("2026-08-25T10:00:00Z", "in window keep this one"),
                            msg("2026-09-01T10:00:00Z", "later but no upper bound")],
                           None)
        assert len(out) == 2


class TestChannelWindowApplied:
    def test_channel_messages_after_window_dropped(self, monkeypatch):
        route(monkeypatch, lambda url: FakeResponse(200, {"value": [
            msg("2026-08-25T10:00:00Z", "in window keep this one"),
            msg("2026-09-01T10:00:00Z", "after window drop this one")]}))
        out, _ = app_module._pulse_fetch_channel_messages(
            "t1", "Negev", {"id": "ch1", "displayName": "General"}, START, {},
            end_iso=END)
        assert len(out) == 1 and "in window" in out[0]["content_preview"]

    def test_channel_without_end_keeps_everything(self, monkeypatch):
        route(monkeypatch, lambda url: FakeResponse(200, {"value": [
            msg("2026-08-25T10:00:00Z"), msg("2026-09-01T10:00:00Z")]}))
        out, _ = app_module._pulse_fetch_channel_messages(
            "t1", "Negev", {"id": "ch1", "displayName": "General"}, START, {})
        assert len(out) == 2


# ----------------------------------------------------------------------
# not-JSON retry
# ----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


class TestJsonRetry:
    def _replies(self, monkeypatch, replies):
        seen = []

        def fake(prompt, model=None, use_briefing=True):
            seen.append(prompt)
            return replies[min(len(seen), len(replies)) - 1]

        monkeypatch.setattr(app_module, "_pulse_call_claude", fake)
        return seen

    def test_valid_json_first_time_does_not_retry(self, monkeypatch):
        seen = self._replies(monkeypatch, ['{"green": ["a"], "yellow": [], "red": []}'])
        out = app_module._pulse_call_claude_json("PROMPT", label="Pass 1")
        assert out["green"] == ["a"]
        assert len(seen) == 1, "must not spend a second call when the first parsed"

    def test_prose_reply_triggers_a_retry_with_the_contract(self, monkeypatch, caplog):
        """The real 2026-09-01 Pass 5 failure: a markdown report."""
        seen = self._replies(monkeypatch, [
            "# Weekly Pulse Report\n\n## EXECUTIVE SUMMARY\n\nA high-volume week...",
            '{"proposed_updates": [{"section": "S"}], "no_changes_needed": false}'])
        with caplog.at_level(logging.WARNING):
            out = app_module._pulse_call_claude_json("PROMPT", label="Pass 5")
        assert len(seen) == 2
        assert app_module.PULSE_JSON_CONTRACT in seen[1]
        assert "PROMPT" in seen[1], "retry must keep the original prompt"
        assert out["proposed_updates"] == [{"section": "S"}]
        assert "was not JSON" in caplog.text

    def test_both_attempts_failing_is_logged_as_an_error(self, monkeypatch, caplog):
        seen = self._replies(monkeypatch, ["not json at all", "still not json"])
        with caplog.at_level(logging.ERROR):
            out = app_module._pulse_call_claude_json("PROMPT", label="Pass 5")
        assert len(seen) == 2
        assert out["green"] == [] and "_raw" in out
        assert "contributed nothing" in caplog.text

    def test_prose_wrapped_json_needs_no_retry(self, monkeypatch):
        """The 2.30.1 parser already recovers this shape -- do not pay twice."""
        seen = self._replies(monkeypatch, [
            'Here is my analysis.\n\n```json\n{"green": ["kept"]}\n```\n\nDone.'])
        out = app_module._pulse_call_claude_json("PROMPT", label="Pass 1")
        assert out["green"] == ["kept"] and len(seen) == 1

    def test_chunked_pass_routes_through_the_retry(self, monkeypatch):
        calls = []

        def fake_json(prompt, model=None, use_briefing=True, label="pass"):
            calls.append(label)
            return {"green": [], "yellow": [], "red": [], "key_entities": []}

        monkeypatch.setattr(app_module, "_pulse_call_claude_json", fake_json)
        monkeypatch.setattr(app_module, "load_briefing_book", lambda: "")
        app_module._pulse_run_chunked_pass(
            "Pass 1", "P {emails_text}", "{emails_text}", ["a", "b"], "(none)", 0)
        assert calls, "chunked pass must use the retrying caller"
