# Tests that functions handling external API responses survive null fields
# (the 'or {}' / 'or []' pattern -- APIs return present-but-null keys).
import json

import app as app_module
import hubspot_client as hc


class _FakeMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class _FakeMessages:
    def __init__(self, response_text):
        self._response_text = response_text

    def create(self, **kwargs):
        return _FakeMessage(self._response_text)


class _FakeAnthropicClient:
    def __init__(self, response_text):
        self.messages = _FakeMessages(response_text)


def _patch_claude(monkeypatch, intelligence_dict):
    response_text = json.dumps(intelligence_dict)
    monkeypatch.setattr(
        app_module.anthropic, "Anthropic",
        lambda api_key=None: _FakeAnthropicClient(response_text))


MINIMAL_INTELLIGENCE = {
    "contacts": [],
    "signals": {},
    "action_items": [],
    "internal_lead_email": "",
    "hubspot_note": "",
    "follow_up_email": None,  # Claude sometimes returns null here
}


class TestExtractMeetingIntelligenceNullSafety:
    def test_survives_null_summary_sentences_participants(self, monkeypatch):
        _patch_claude(monkeypatch, MINIMAL_INTELLIGENCE)
        transcript = {
            "id": "t1",
            "title": "Test Meeting",
            "summary": None,
            "sentences": None,
            "participants": None,
            "organizer_email": "bk@negevlabs.com",
        }
        result = app_module.extract_meeting_intelligence(transcript)
        assert isinstance(result, dict)
        # Safety net must populate the null follow_up_email
        follow_up = result["follow_up_email"]
        assert follow_up["from_email"] == "bk@negevlabs.com"
        assert follow_up["subject"]
        assert follow_up["body_text"]

    def test_survives_completely_empty_transcript(self, monkeypatch):
        _patch_claude(monkeypatch, MINIMAL_INTELLIGENCE)
        result = app_module.extract_meeting_intelligence({})
        assert isinstance(result, dict)
        assert "follow_up_email" in result


class TestHubspotNullSafety:
    def test_find_contact_with_null_results(self, monkeypatch):
        monkeypatch.setattr(
            hc, "hubspot_request",
            lambda method, endpoint, data=None, params=None: {"results": None})
        assert app_module.find_hubspot_contact("x@y.com") is None

    def test_get_contact_associations_with_null_results(self, monkeypatch):
        monkeypatch.setattr(
            hc, "hubspot_request",
            lambda method, endpoint, data=None, params=None: {"results": None})
        assoc = app_module.get_contact_associations("123")
        assert assoc == {"companies": [], "deals": []}


class TestResolveInternalOrganizer:
    def test_null_participants_falls_back_to_organizer(self):
        result = app_module.resolve_internal_organizer("ext@gmail.com", None)
        assert result == "ext@gmail.com"

    def test_internal_organizer_kept(self):
        result = app_module.resolve_internal_organizer("BK@negevlabs.com", None)
        assert result == "bk@negevlabs.com"

    def test_external_organizer_routed_to_internal_lead(self):
        result = app_module.resolve_internal_organizer(
            "ext@gmail.com", ["other@gmail.com"], internal_lead_email="dan@negevlabs.com")
        assert result == "dan@negevlabs.com"
