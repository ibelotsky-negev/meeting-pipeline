"""Offline tests for x_transcribe_email (email Sara an x.com link -> transcript reply).

All network + reuse points are mocked in their HOME modules:
- inbox read / reply send   -> email_pipeline_sync.graph_get / graph_post
- audio extract / STT       -> learn_digest.extract_x_post_audio / _grok_stt_from_file
- summary                   -> patched via xte.summarize_transcript
The autouse no_network fixture (conftest) fails any real HTTP.
"""
import os
import json
import base64

import pytest

import x_transcribe_email as xte
import learn_digest as ld
import email_pipeline_sync as eps


@pytest.fixture
def xte_files(monkeypatch, tmp_path):
    monkeypatch.setattr(xte, "STORE_PATH", str(tmp_path / "store.json"))
    monkeypatch.setattr(xte, "STATUS_PATH", str(tmp_path / "status.json"))
    return tmp_path


def _msg(mid, sender, subject, body_html):
    return {
        "id": mid, "internetMessageId": mid, "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": "2026-07-04T10:00:00Z",
        "uniqueBody": {"content": body_html},
    }


def _mock_inbox(monkeypatch, messages):
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: {"value": messages})


def _capture_sends(monkeypatch):
    sent = []
    monkeypatch.setattr(eps, "graph_post", lambda url, json_body: sent.append({"url": url, "body": json_body}) or {})
    return sent


# ----------------------------------------------------------------------
#  Link detection + rendering (pure)
# ----------------------------------------------------------------------

class TestLinkDetection:
    def test_finds_status_links_excludes_profile_and_youtube(self):
        html = ('see https://x.com/i/status/111 and '
                '<a href="https://twitter.com/a/status/222">x</a> '
                'profile https://x.com/someone yt https://youtube.com/watch?v=z')
        links = xte.find_x_links(html)
        assert any("111" in u for u in links)
        assert any("222" in u for u in links)
        assert not any("youtube" in u for u in links)
        assert not any(u.rstrip("/").endswith("someone") for u in links)

    def test_dedups_same_link_case_and_slash(self):
        html = "a https://x.com/i/status/111 b https://X.com/i/status/111/ c"
        assert len(xte.find_x_links(html)) == 1

    def test_empty_body_no_links(self):
        assert xte.find_x_links("") == []
        assert xte.find_x_links("just text, no links") == []


class TestSummaryRendering:
    def test_labels_bolded_bullets_listed_title_dropped(self):
        s = "TITLE: T\nTL;DR: the gist\nKEY POINTS:\n- alpha\n- beta"
        h = xte._summary_to_html(s)
        assert "<b>TL;DR:" in h
        assert "<li>alpha</li>" in h and "<li>beta</li>" in h
        assert "TITLE" not in h

    def test_parse_title(self):
        assert xte._parse_title("TITLE: Hello world\nTL;DR: x", "fb") == "Hello world"
        assert xte._parse_title("no title line", "fb") == "fb"

    def test_status_id(self):
        assert xte._status_id("https://x.com/i/status/2069002271216787464") == "2069002271216787464"
        assert xte._status_id("https://x.com/no/id/here") == "post"

    def test_render_reply_mixed_ok_and_failure(self):
        results = [
            {"url": "https://x.com/i/status/1", "ok": True, "title": "Good one",
             "summary": "TITLE: Good one\nTL;DR: it works\nKEY POINTS:\n- a"},
            {"url": "https://x.com/i/status/2", "ok": False, "title": "https://x.com/i/status/2",
             "error": "no audio could be extracted"},
        ]
        body = xte.render_reply(results, truncated=1)
        assert "Good one" in body
        assert "Could not transcribe" in body and "no audio" in body
        assert "max" in body  # truncation notice
        assert "1/2 transcribed" in body


# ----------------------------------------------------------------------
#  transcribe_link (audio + STT reuse mocked)
# ----------------------------------------------------------------------

class TestTranscribeLink:
    def test_success(self, monkeypatch, tmp_path):
        d = tmp_path / "aud"; d.mkdir()
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (str(d / "a.m4a"), 30.0, None, str(d)))
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda p, timeout=None: ("hello world", None))
        r = xte.transcribe_link("https://x.com/i/status/111")
        assert r["ok"] and r["transcript"] == "hello world" and r["chars"] == 11
        assert not os.path.isdir(str(d))  # tmpdir cleaned up

    def test_no_audio_extracted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (None, 0, "no video could be found", str(tmp_path / "x")))
        r = xte.transcribe_link("https://x.com/i/status/111")
        assert not r["ok"] and "no video" in r["error"]

    def test_stt_failure(self, monkeypatch, tmp_path):
        d = tmp_path / "aud"; d.mkdir()
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (str(d / "a.m4a"), 30.0, None, str(d)))
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda p, timeout=None: (None, "STT status 400"))
        r = xte.transcribe_link("https://x.com/i/status/111")
        assert not r["ok"] and "400" in r["error"]


# ----------------------------------------------------------------------
#  run() -- inbox scan, gating, reply, dedup
# ----------------------------------------------------------------------

class TestRun:
    def _patch_ok(self, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u: {"url": u, "ok": True, "transcript": "T" * 50, "chars": 50, "error": ""})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t: "TITLE: Vid\nTL;DR: ok\nKEY POINTS:\n- p1")

    def test_dry_run_lists_but_sends_nothing_and_writes_no_store(self, xte_files, monkeypatch):
        _mock_inbox(monkeypatch, [_msg("m1", "bk@negevlabs.com", "hey", "https://x.com/i/status/111")])
        sent = _capture_sends(monkeypatch)
        res = xte.run(dry_run=True)
        assert res["scanned"] == 1 and res["replied"] == 0
        assert res["outcomes"][0]["would_transcribe"]
        assert sent == []
        assert not os.path.exists(xte.STORE_PATH)

    def test_live_replies_with_transcript_attachment(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m1", "bk@negevlabs.com", "please transcribe", "https://x.com/i/status/111")])
        sent = _capture_sends(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1 and len(sent) == 1
        msg = sent[0]["body"]["message"]
        assert msg["toRecipients"][0]["emailAddress"]["address"] == "bk@negevlabs.com"
        assert msg["subject"].startswith("Re:")
        atts = msg["attachments"]
        assert atts and atts[0]["@odata.type"] == "#microsoft.graph.fileAttachment"
        assert atts[0]["name"] == "transcript_111.md"
        assert "TTTTT" in base64.b64decode(atts[0]["contentBytes"]).decode("utf-8")
        store = json.load(open(xte.STORE_PATH))
        assert "m1" in store["processed_ids"]

    def test_external_sender_gated_out(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m2", "rando@gmail.com", "hi", "https://x.com/i/status/111")])
        sent = _capture_sends(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and sent == []
        assert "m2" in json.load(open(xte.STORE_PATH))["processed_ids"]  # not reconsidered

    def test_skips_saras_own_mail_and_link_free_mail(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        msgs = [
            _msg("m3", xte.SARA_MAILBOX, "loop", "https://x.com/i/status/111"),   # loop guard
            _msg("m4", "dan@negevlabs.com", "fyi", "no links in here at all"),     # nothing to do
        ]
        _mock_inbox(monkeypatch, msgs)
        sent = _capture_sends(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and sent == []

    def test_dedup_skips_already_processed_id(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        os.makedirs(os.path.dirname(xte.STORE_PATH), exist_ok=True)
        json.dump({"processed_ids": ["m1"]}, open(xte.STORE_PATH, "w"))
        _mock_inbox(monkeypatch, [_msg("m1", "bk@negevlabs.com", "again", "https://x.com/i/status/111")])
        sent = _capture_sends(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and sent == []

    def test_reply_failure_not_marked_processed(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m5", "bk@negevlabs.com", "x", "https://x.com/i/status/111")])

        def boom(url, json_body):
            raise RuntimeError("graph 500")
        monkeypatch.setattr(eps, "graph_post", boom)
        res = xte.run()
        assert res["replied"] == 0
        assert "m5" not in json.load(open(xte.STORE_PATH))["processed_ids"]  # will retry next run
