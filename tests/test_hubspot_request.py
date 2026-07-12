# Tests that hubspot_request surfaces the HTTP error body in the raised
# exception. Before the fix, resp.raise_for_status() produced a bare
# "400 Client Error: Bad Request for url: ..." with no reason, which made
# meeting-log 400s (e.g. alexander.alpern@gmail.com) undiagnosable.
import pytest
import requests

import hubspot_client as hc


class _FakeResp:
    def __init__(self, status_code, text, reason="Bad Request", content=b""):
        self.status_code = status_code
        self.text = text
        self.reason = reason
        self.content = content
        self.ok = 200 <= status_code < 300

    def json(self):
        return {}


def test_error_includes_response_body(monkeypatch):
    body = '{"status":"error","message":"Property values were not valid: hs_meeting_outcome"}'

    def fake_request(method, url, **kwargs):
        return _FakeResp(400, body)

    monkeypatch.setattr(hc.requests, "request", fake_request)

    with pytest.raises(requests.HTTPError) as exc:
        hc.hubspot_request("POST", "/crm/v3/objects/meetings", {"properties": {}})

    msg = str(exc.value)
    assert "400" in msg
    assert "hs_meeting_outcome" in msg  # the actual reason is now visible
    assert "/crm/v3/objects/meetings" in msg


def test_success_returns_json(monkeypatch):
    def fake_request(method, url, **kwargs):
        return _FakeResp(200, "", reason="OK", content=b"{}")

    monkeypatch.setattr(hc.requests, "request", fake_request)
    assert hc.hubspot_request("GET", "/crm/v3/objects/contacts") == {}
