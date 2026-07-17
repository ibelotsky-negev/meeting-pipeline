"""Offline tests for x_search_cli -- the date-scoped X timeline sweep helper.

All xAI HTTP is mocked; the no_network fixture (conftest) blocks any real call.
"""
import json
from datetime import datetime, timezone

import pytest

import x_search_cli as xs


# ----------------------------------------------------------------------
#  default_window -- date math
# ----------------------------------------------------------------------

def test_default_window_computes_trailing_range():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    frm, to = xs.default_window(90, now=now)
    assert to == "2026-07-17"
    assert frm == "2026-04-18"


def test_default_window_30_days():
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    frm, to = xs.default_window(30, now=now)
    assert frm == "2026-06-17"
    assert to == "2026-07-17"


def test_default_window_rejects_nonpositive():
    with pytest.raises(ValueError):
        xs.default_window(0)


# ----------------------------------------------------------------------
#  build_prompt -- window enforced in prompt, empty topic rejected
# ----------------------------------------------------------------------

def test_build_prompt_includes_topic_and_window():
    p = xs.build_prompt("Saronic Technologies", "2026-04-18", "2026-07-17", 25)
    assert "Saronic Technologies" in p
    assert "2026-04-18" in p and "2026-07-17" in p
    assert "25" in p
    # freshness + no-fabrication guardrails must be present
    assert "STRICT DATE WINDOW" in p
    assert "never fabricate" in p.lower()


def test_build_prompt_rejects_empty_topic():
    with pytest.raises(ValueError):
        xs.build_prompt("   ", "2026-04-18", "2026-07-17", 25)


# ----------------------------------------------------------------------
#  parse_response -- walks output[] for the assistant message
# ----------------------------------------------------------------------

def test_parse_response_extracts_text_and_citations():
    data = {
        "output_text": None,
        "output": [
            {"type": "reasoning", "content": "ignore me"},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Post A on 2026-05-01.",
                 "annotations": [
                     {"type": "url_citation", "url": "https://x.com/a/status/1"},
                     {"type": "other", "url": "https://ignore.me"},
                 ]},
            ]},
        ],
    }
    text, cits = xs.parse_response(data)
    assert "Post A" in text
    assert cits == ["https://x.com/a/status/1"]


def test_parse_response_null_safe_on_string_content():
    # A non-dict content must not raise (the learn_digest gotcha).
    data = {"output": [
        {"type": "message", "role": "assistant", "content": "raw string"},
    ]}
    text, cits = xs.parse_response(data)
    assert text == ""
    assert cits == []


def test_parse_response_prefers_top_level_output_text():
    data = {"output_text": "top level", "output": []}
    text, _ = xs.parse_response(data)
    assert text == "top level"


# ----------------------------------------------------------------------
#  x_search -- happy path, missing key, retry on 5xx
# ----------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"status {self.status_code}")
            err.response = self
            raise err


def test_x_search_missing_key_raises(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        xs.x_search("Saronic", days=30)


def test_x_search_happy_path(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    payload = {"output": [
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "found 3 posts",
             "annotations": [{"type": "url_citation", "url": "https://x.com/s/status/9"}]},
        ]},
    ]}
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return _Resp(200, payload)

    monkeypatch.setattr(xs.requests, "post", fake_post)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    result = xs.x_search("Saronic", days=90, now=now)
    assert result["from_date"] == "2026-04-18"
    assert result["to_date"] == "2026-07-17"
    assert result["text"] == "found 3 posts"
    assert result["citations"] == ["https://x.com/s/status/9"]
    # x_search tool must be requested
    tool_types = {t["type"] for t in captured["body"]["tools"]}
    assert "x_search" in tool_types


def test_x_search_retries_on_5xx(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setattr(xs.time, "sleep", lambda *_: None)
    payload = {"output_text": "ok", "output": []}
    calls = {"n": 0}

    def flaky_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(503, {})
        return _Resp(200, payload)

    monkeypatch.setattr(xs.requests, "post", flaky_post)
    result = xs.x_search("Saronic", days=30)
    assert calls["n"] == 2
    assert result["text"] == "ok"


# ----------------------------------------------------------------------
#  main -- CLI wiring
# ----------------------------------------------------------------------

def test_main_json_output(monkeypatch, capsys):
    monkeypatch.setattr(
        xs, "x_search",
        lambda topic, **kw: {"topic": topic, "from_date": "2026-06-17",
                             "to_date": "2026-07-17", "model": "m",
                             "text": "t", "citations": []})
    rc = xs.main(["Saronic", "Technologies", "--days", "30", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["topic"] == "Saronic Technologies"


def test_main_reports_failure(monkeypatch, capsys):
    def boom(topic, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(xs, "x_search", boom)
    rc = xs.main(["Saronic"])
    assert rc == 1
    assert "boom" in capsys.readouterr().err
