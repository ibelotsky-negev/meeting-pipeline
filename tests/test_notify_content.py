"""Offline tests for the Phase-1 notification email: Fireflies summary section
and transcript attachment (notify_content.py + notify_organizer wiring)."""
import base64

import app as app_module
import notify_content as nc


TRANSCRIPT = {
    "id": "t1",
    "title": "peptides EU: Maria / Ken / Tadas",
    "dateString": "2026-11-03T14:00:00.000Z",
    "participants": ["bk@negevlabs.com", "tadas@example.com"],
    "summary": {
        "overview": "Discussed Phase 1 budget.\nAgreed on a Telegram group.",
        "shorthand_bullet": "\U0001F680 **Budget** (00:00 - 05:00)\n- Ad spend <$5k>\n- Domain for Pepsi.io",
    },
    "sentences": [
        {"speaker_name": "Ken", "text": "Hi all.", "start_time": 0.5},
        {"speaker_name": "Ken", "text": "Let's start.", "start_time": 2},
        {"speaker_name": "Tadas", "text": "Sure.", "start_time": 65},
        {"speaker_name": "Tadas", "text": "  ", "start_time": 66},
    ],
}


def test_summary_html_renders_overview_bullets_and_escapes():
    out = nc.build_fireflies_summary_html(TRANSCRIPT["summary"])
    assert "Meeting Summary (Fireflies)" in out
    assert "Discussed Phase 1 budget." in out and "Agreed on a Telegram group." in out
    assert "<strong>Budget</strong>" in out
    assert "<li" in out and "Ad spend &lt;$5k&gt;" in out
    assert "\U0001F680" not in out


def test_summary_html_empty_when_nothing():
    assert nc.build_fireflies_summary_html(None) == ""
    assert nc.build_fireflies_summary_html({"overview": "", "shorthand_bullet": None}) == ""
    assert nc.build_fireflies_summary_html("not a dict") == ""


def test_transcript_text_merges_turns_and_stamps():
    text = nc.build_transcript_text(TRANSCRIPT)
    assert "Meeting: peptides EU: Maria / Ken / Tadas" in text
    assert "Date: 2026-11-03" in text
    assert "Source: Fireflies transcript" in text
    assert "[00:00] Ken: Hi all. Let's start." in text
    assert "[01:05] Tadas: Sure." in text


def test_transcript_text_handles_missing_timestamps_and_empty():
    assert nc.build_transcript_text({"sentences": []}) == ""
    assert nc.build_transcript_text(None) == ""
    t = nc.build_transcript_text({"sentences": [{"speaker_name": None, "text": "x"}], "_source": "teams"})
    assert "Unknown: x" in t and "Microsoft Teams" in t and "[" not in t.split("\n\n", 1)[1]


def test_attachment_shape_and_roundtrip():
    att = nc.build_transcript_attachment(TRANSCRIPT)
    assert att["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert att["name"] == "transcript_peptides_EU_Maria__Ken__Tadas_2026-11-03.txt"
    assert base64.b64decode(att["contentBytes"]).decode("utf-8") == nc.build_transcript_text(TRANSCRIPT)
    assert nc.build_transcript_attachment({"sentences": []}) is None


def test_attachment_truncated_when_too_large(monkeypatch):
    monkeypatch.setattr(nc, "TRANSCRIPT_MAX_BYTES", 500)
    big = {"title": "x", "sentences": [{"speaker_name": f"S{i}", "text": "word " * 20} for i in range(50)]}
    data = base64.b64decode(nc.build_transcript_attachment(big)["contentBytes"])
    assert len(data) <= 500
    assert data.decode("utf-8").endswith("[Transcript truncated: too large to attach in full.]\n")


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = "err"


def _wire(monkeypatch, codes):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        import copy
        sent.append(copy.deepcopy(json))
        return _Resp(codes[len(sent) - 1])

    monkeypatch.setattr(app_module, "NOTIFY_VIA", "email")
    monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
    monkeypatch.setattr(app_module, "get_ms_graph_token", lambda: "tok")
    monkeypatch.setattr(app_module, "is_app_only_mode", lambda: True)
    monkeypatch.setattr(app_module.requests, "post", fake_post)
    return sent


INTEL = {"meeting_summary": "S", "action_items": [{"task": "Do thing", "owner": "Ken"}]}


def test_notify_attaches_transcript_and_summary_below_actions(monkeypatch):
    sent = _wire(monkeypatch, [202])
    app_module.notify_organizer(["bk@negevlabs.com"], "abc", "Mtg", INTEL, "", transcript=TRANSCRIPT)
    msg = sent[0]["message"]
    html_body = msg["body"]["content"]
    assert html_body.index("Action Items") < html_body.index("Meeting Summary (Fireflies)") < html_body.index("Review &amp; Approve")
    assert msg["attachments"][0]["name"].endswith(".txt")


def test_notify_retries_without_attachment_on_failure(monkeypatch):
    sent = _wire(monkeypatch, [413, 202])
    app_module.notify_organizer(["bk@negevlabs.com"], "abc", "Mtg", INTEL, "", transcript=TRANSCRIPT)
    assert len(sent) == 2
    assert "attachments" in sent[0]["message"] and "attachments" not in sent[1]["message"]


def test_notify_teams_source_skips_placeholder_summary(monkeypatch):
    sent = _wire(monkeypatch, [202])
    teams = dict(TRANSCRIPT, _source="teams")
    app_module.notify_organizer(["bk@negevlabs.com"], "abc", "Mtg", INTEL, "", transcript=teams)
    msg = sent[0]["message"]
    assert "Meeting Summary (Fireflies)" not in msg["body"]["content"]
    assert msg["attachments"]


def test_notify_without_transcript_unchanged(monkeypatch):
    sent = _wire(monkeypatch, [202])
    app_module.notify_organizer(["bk@negevlabs.com"], "abc", "Mtg", INTEL, "")
    assert "attachments" not in sent[0]["message"]
    assert "Meeting Summary (Fireflies)" not in sent[0]["message"]["body"]["content"]


def test_fireflies_meeting_url_prefers_api_then_id_and_skips_teams():
    assert nc.fireflies_meeting_url({"id": "t1", "transcript_url": "https://app.fireflies.ai/view/x::t1"}) == "https://app.fireflies.ai/view/x::t1"
    assert nc.fireflies_meeting_url({"id": "t1"}) == "https://app.fireflies.ai/view/t1"
    assert nc.fireflies_meeting_url({"id": "t1", "transcript_url": "javascript:alert(1)"}) == "https://app.fireflies.ai/view/t1"
    assert nc.fireflies_meeting_url({"id": "t1", "_source": "teams"}) == ""
    assert nc.fireflies_meeting_url({}) == ""
    assert nc.fireflies_meeting_url(None) == ""


def test_summary_html_link_only_when_no_summary():
    out = nc.build_fireflies_summary_html(None, "https://app.fireflies.ai/view/t1")
    assert "Meeting Summary (Fireflies)" in out
    assert 'href="https://app.fireflies.ai/view/t1"' in out


def test_notify_includes_fireflies_link(monkeypatch):
    sent = _wire(monkeypatch, [202])
    app_module.notify_organizer(["bk@negevlabs.com"], "abc", "Mtg", INTEL, "", transcript=TRANSCRIPT)
    body = sent[0]["message"]["body"]["content"]
    assert "https://app.fireflies.ai/view/t1" in body
    assert body.index("Meeting Summary (Fireflies)") < body.index("app.fireflies.ai/view/t1") < body.index("Review &amp; Approve")
