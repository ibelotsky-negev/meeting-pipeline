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


def _capture_graph(monkeypatch):
    """Record every Graph write (POST/PATCH/DELETE). createReply answers with
    a draft id so the threaded-reply sequence can run to completion offline;
    DELETE (best-effort draft cleanup on failure) is recorded too, so tests
    can assert it happened without touching real Graph."""
    calls = []

    def _post(url, json_body):
        calls.append({"method": "POST", "url": url, "body": json_body})
        if url.endswith("/createReply"):
            return {"id": "draft-1"}
        return {}

    def _patch(url, json_body):
        calls.append({"method": "PATCH", "url": url, "body": json_body})
        return {}

    def _delete(url):
        calls.append({"method": "DELETE", "url": url})
        return {}

    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", _patch)
    monkeypatch.setattr(eps, "graph_delete", _delete)
    return calls


def _sent_bodies(calls):
    """HTML bodies of every reply actually patched onto a draft."""
    return [c["body"]["body"]["content"] for c in calls
            if c["method"] == "PATCH" and "body" in (c["body"] or {})]


def _sent_attachments(calls):
    return [c["body"] for c in calls if c["url"].endswith("/attachments")]


# ----------------------------------------------------------------------
#  Link detection + rendering (pure)
# ----------------------------------------------------------------------

class TestLinkDetection:
    def test_returns_x_youtube_and_podcast_with_kinds(self):
        html = ('x https://x.com/i/status/111 '
                'yt https://www.youtube.com/watch?v=abc123 '
                'pod https://open.spotify.com/episode/xyz '
                'article https://example.com/post')
        pairs = xte.find_media_links(html)
        kinds = dict((u, k) for u, k in pairs)
        assert kinds.get("https://x.com/i/status/111") == "x"
        assert kinds.get("https://www.youtube.com/watch?v=abc123") == "youtube"
        assert kinds.get("https://open.spotify.com/episode/xyz") == "podcast"
        assert not any("example.com" in u for u, _ in pairs)   # articles are not requests

    def test_keeps_x_profile_but_drops_nav_pages(self):
        html = 'profile https://x.com/someone nav https://x.com/home'
        pairs = xte.find_media_links(html)
        assert any(u.rstrip("/").endswith("someone") for u, _ in pairs)  # -> honest no-video reply
        assert not any(u.rstrip("/").endswith("x.com/home") for u, _ in pairs)

    def test_dedups_x_by_normalized_url(self):
        html = "a https://x.com/i/status/111 b https://X.com/i/status/111/ c"
        assert len(xte.find_media_links(html)) == 1

    def test_dedups_youtube_by_case_and_trailing_slash(self):
        html = "a https://youtu.be/AbC/ b https://youtu.be/AbC c"
        assert len(xte.find_media_links(html)) == 1

    def test_preserves_first_seen_order(self):
        html = "yt https://www.youtube.com/watch?v=one then x https://x.com/i/status/222"
        pairs = xte.find_media_links(html)
        assert [k for _, k in pairs] == ["youtube", "x"]

    def test_empty_body_no_links(self):
        assert xte.find_media_links("") == []
        assert xte.find_media_links("just text, no links") == []

    def test_youtube_same_video_different_forms_dedups(self):
        # youtu.be/AbCdefg and youtube.com/watch?v=AbCdefg are the same video
        html = 'a https://youtu.be/AbCdefg b https://www.youtube.com/watch?v=AbCdefg c'
        pairs = xte.find_media_links(html)
        assert len(pairs) == 1
        assert pairs[0][1] == "youtube"

    def test_youtube_case_variant_ids_collapse_upstream_in_extract_urls(self):
        # Case-variant URLs are deduplicated by ld.extract_urls (learn_digest.py:554,
        # key = u.lower()), which is upstream of find_media_links. This is pre-existing
        # behavior from the shared machinery and is accepted deliberately to avoid
        # blast radius on the Read/Learn digest and other consumers.
        html = 'a https://www.youtube.com/watch?v=dQw4w9WgXcQ b https://www.youtube.com/watch?v=dqw4w9wgxcq'
        pairs = xte.find_media_links(html)
        assert len(pairs) == 1  # collapsed by extract_urls case-insensitive dedup

    def test_youtube_unparseable_id_still_processed(self):
        # If video ID extraction fails, fall back to lexical form so no crash
        html = 'invalid https://youtube.com/foo/bar/baz'
        pairs = xte.find_media_links(html)
        assert len(pairs) == 1
        assert pairs[0][1] == "youtube"

    def test_podcast_case_variant_ids_collapse_upstream_in_extract_urls(self):
        # Case-variant URLs are deduplicated by ld.extract_urls (learn_digest.py:554,
        # key = u.lower()), which is upstream of find_media_links. This is pre-existing
        # behavior from the shared machinery and is accepted deliberately to avoid
        # blast radius on the Read/Learn digest and other consumers.
        html = 'a https://open.spotify.com/episode/ABC b https://open.spotify.com/episode/abc'
        pairs = xte.find_media_links(html)
        assert len(pairs) == 1  # collapsed by extract_urls case-insensitive dedup

    def test_youtube_url_with_trailing_period_stripped(self):
        # Trailing punctuation should be stripped by extract_urls normalization
        html = "check this out https://youtu.be/AbCdefgh. more text"
        pairs = xte.find_media_links(html)
        assert len(pairs) == 1
        assert pairs[0][0] == "https://youtu.be/AbCdefgh"  # period not in URL

    def test_x_nav_page_with_period_still_filtered(self):
        # X nav page followed by period should still match _X_NONPOST filter
        html = "go home https://x.com/home. stay safe"
        pairs = xte.find_media_links(html)
        assert not any(u.endswith("/home") or u.endswith("/home.") for u, _ in pairs)

    def test_x_link_in_anchor_href_extracted(self):
        # Anchor hrefs (not just bare URLs) should still be extracted via ld.extract_urls
        html = '<a href="https://x.com/i/status/123456789">click here</a>'
        pairs = xte.find_media_links(html)
        assert len(pairs) == 1
        assert "status/123456789" in pairs[0][0]
        assert pairs[0][1] == "x"


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
        assert "No video was found" in body   # clear no-video wording
        assert "max" in body  # truncation notice
        assert "1/2 transcribed" in body

    def test_render_reply_all_failed_says_no_video(self):
        results = [{"url": "https://x.com/someone", "ok": False,
                    "title": "https://x.com/someone", "error": "no video could be found in this post"}]
        body = xte.render_reply(results)
        assert "couldn't get a transcript" in body.lower()
        assert "No video was found" in body


# ----------------------------------------------------------------------
#  transcribe_link (audio + STT reuse mocked)
# ----------------------------------------------------------------------

class TestTranscribeLink:
    def _audio_ok(self, monkeypatch, tmp_path, text="hello world"):
        d = tmp_path / "aud"; d.mkdir()
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (str(d / "a.m4a"), 30.0, None, str(d)))
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda p, timeout=None: (text, None))
        return d

    def test_x_success_via_stt(self, monkeypatch, tmp_path):
        d = self._audio_ok(monkeypatch, tmp_path)
        r = xte.transcribe_link("https://x.com/i/status/111", "x")
        assert r["ok"] and r["transcript"] == "hello world" and r["chars"] == 11
        assert r["source"] == "xAI Grok STT"
        assert not os.path.isdir(str(d))  # tmpdir cleaned up

    def test_x_no_audio_extracted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (None, 0, "no video could be found", str(tmp_path / "x")))
        r = xte.transcribe_link("https://x.com/i/status/111", "x")
        assert not r["ok"] and "no video" in r["error"]

    def test_x_stt_failure(self, monkeypatch, tmp_path):
        d = tmp_path / "aud"; d.mkdir()
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (str(d / "a.m4a"), 30.0, None, str(d)))
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda p, timeout=None: (None, "STT status 400"))
        r = xte.transcribe_link("https://x.com/i/status/111", "x")
        assert not r["ok"] and "400" in r["error"]

    def test_youtube_prefers_captions_and_never_calls_stt(self, monkeypatch):
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", lambda u: "caption text")

        def _never(*a, **k):
            raise AssertionError("STT must not run when captions exist")
        monkeypatch.setattr(ld, "extract_x_post_audio", _never)
        r = xte.transcribe_link("https://www.youtube.com/watch?v=abc", "youtube")
        assert r["ok"] and r["transcript"] == "caption text"
        assert r["source"] == "YouTube captions"

    def test_youtube_falls_back_to_stt_when_no_captions(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", lambda u: None)
        self._audio_ok(monkeypatch, tmp_path, text="spoken words")
        r = xte.transcribe_link("https://www.youtube.com/watch?v=abc", "youtube")
        assert r["ok"] and r["transcript"] == "spoken words"
        assert r["source"] == "xAI Grok STT"

    def test_youtube_caption_error_falls_back_not_crashes(self, monkeypatch, tmp_path):
        def _boom(u):
            raise RuntimeError("captions api down")
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", _boom)
        self._audio_ok(monkeypatch, tmp_path, text="spoken words")
        r = xte.transcribe_link("https://www.youtube.com/watch?v=abc", "youtube")
        assert r["ok"] and r["transcript"] == "spoken words"

    def test_podcast_unsupported_without_calling_any_resolver(self, monkeypatch):
        def _never(*a, **k):
            raise AssertionError("no resolver may run for a podcast link")
        monkeypatch.setattr(ld, "extract_x_post_audio", _never)
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", _never)
        r = xte.transcribe_link("https://open.spotify.com/episode/xyz", "podcast")
        assert not r["ok"]
        assert "not supported yet" in r["error"]


class TestFailureMessage:
    def test_podcast_reason_passes_through_verbatim(self):
        assert xte._failure_message(xte.PODCAST_UNSUPPORTED) == xte.PODCAST_UNSUPPORTED

    def test_no_video_reason_is_humanized(self):
        assert "No video was found" in xte._failure_message("no video could be found")


class TestAttachmentNaming:
    def test_status_id_uses_x_status_then_youtube_id(self):
        assert xte._status_id("https://x.com/i/status/2069002271216787464") == "2069002271216787464"
        assert xte._status_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
        assert xte._status_id("https://x.com/no/id/here") == "post"

    def test_transcript_md_records_the_real_source(self):
        md = xte._transcript_md("T", "https://youtu.be/a", "words", source="YouTube captions")
        assert "YouTube captions" in md
        assert "Grok" not in md


class TestThreadedReply:
    def test_create_reply_patch_attach_send_in_order(self, monkeypatch):
        calls = _capture_graph(monkeypatch)
        xte.send_threaded_reply("msg-9", "<p>hi</p>",
                                [xte._attachment("t.md", "body text")])
        seq = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in calls]
        assert seq == [("POST", "createReply"), ("PATCH", "draft-1"),
                       ("POST", "attachments"), ("POST", "send")]
        assert calls[0]["url"].endswith("/messages/msg-9/createReply")
        assert calls[1]["body"]["body"]["content"] == "<p>hi</p>"
        assert calls[2]["body"]["name"] == "t.md"

    def test_no_attachments_skips_attachment_call(self, monkeypatch):
        calls = _capture_graph(monkeypatch)
        xte.send_threaded_reply("msg-9", "<p>hi</p>")
        assert not any(c["url"].endswith("/attachments") for c in calls)

    def test_missing_draft_id_raises(self, monkeypatch):
        monkeypatch.setattr(eps, "graph_post", lambda url, json_body: {})
        with pytest.raises(RuntimeError):
            xte.send_threaded_reply("msg-9", "<p>hi</p>")

    def test_patch_failure_deletes_orphaned_draft_and_reraises(self, monkeypatch):
        """PATCH fails after createReply already made a draft -- the draft must
        be deleted (not left orphaned) and the original error must still
        propagate so run() does not mark the source message processed."""
        calls = _capture_graph(monkeypatch)

        def _boom_patch(url, json_body):
            calls.append({"method": "PATCH", "url": url, "body": json_body})
            raise RuntimeError("graph 500 on patch")

        monkeypatch.setattr(eps, "graph_patch", _boom_patch)
        with pytest.raises(RuntimeError, match="graph 500 on patch"):
            xte.send_threaded_reply("msg-9", "<p>hi</p>")
        seq = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in calls]
        assert seq == [("POST", "createReply"), ("PATCH", "draft-1"), ("DELETE", "draft-1")]

    def test_send_failure_deletes_orphaned_draft_and_reraises(self, monkeypatch):
        """Same guarantee when the failure is the final send call instead of
        the body PATCH -- the whole post-draft sequence is covered, not just
        its first step."""
        calls = _capture_graph(monkeypatch)

        def _boom_post(url, json_body):
            calls.append({"method": "POST", "url": url, "body": json_body})
            if url.endswith("/createReply"):
                return {"id": "draft-1"}
            if url.endswith("/send"):
                raise RuntimeError("graph 500 on send")
            return {}

        monkeypatch.setattr(eps, "graph_post", _boom_post)
        with pytest.raises(RuntimeError, match="graph 500 on send"):
            xte.send_threaded_reply("msg-9", "<p>hi</p>")
        seq = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in calls]
        assert seq == [("POST", "createReply"), ("PATCH", "draft-1"),
                       ("POST", "send"), ("DELETE", "draft-1")]

    def test_cleanup_failure_does_not_mask_original_error(self, monkeypatch):
        """The cleanup DELETE is best-effort: if it also fails, the ORIGINAL
        error must still be what's raised, never the cleanup error."""
        _capture_graph(monkeypatch)

        def _boom_patch(url, json_body):
            raise RuntimeError("original graph 500")

        def _boom_delete(url):
            raise RuntimeError("delete also failed")

        monkeypatch.setattr(eps, "graph_patch", _boom_patch)
        monkeypatch.setattr(eps, "graph_delete", _boom_delete)
        with pytest.raises(RuntimeError, match="original graph 500"):
            xte.send_threaded_reply("msg-9", "<p>hi</p>")


# ----------------------------------------------------------------------
#  run() -- inbox scan, gating, reply, dedup
# ----------------------------------------------------------------------

class TestRun:
    def _patch_ok(self, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "T" * 50, "chars": 50, "error": ""})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t: "TITLE: Vid\nTL;DR: ok\nKEY POINTS:\n- p1")

    def test_dry_run_lists_but_sends_nothing_and_writes_no_store(self, xte_files, monkeypatch):
        _mock_inbox(monkeypatch, [_msg("m1", "bk@negevlabs.com", "hey", "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)
        res = xte.run(dry_run=True)
        assert res["scanned"] == 1 and res["replied"] == 0
        assert res["outcomes"][0]["would_transcribe"]
        assert calls == []
        assert not os.path.exists(xte.STORE_PATH)

    def test_live_replies_with_transcript_attachment(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m1", "bk@negevlabs.com", "please transcribe", "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert calls[0]["url"].endswith("/messages/m1/createReply")   # threaded onto the request
        atts = _sent_attachments(calls)
        assert atts and atts[0]["@odata.type"] == "#microsoft.graph.fileAttachment"
        assert atts[0]["name"] == "transcript_111.md"
        assert "TTTTT" in base64.b64decode(atts[0]["contentBytes"]).decode("utf-8")
        store = json.load(open(xte.STORE_PATH))
        assert "m1" in store["processed_ids"]

    def test_external_sender_gated_out(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m2", "rando@gmail.com", "hi", "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and calls == []
        assert "m2" in json.load(open(xte.STORE_PATH))["processed_ids"]  # not reconsidered

    def test_skips_saras_own_mail_and_link_free_mail(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        msgs = [
            _msg("m3", xte.SARA_MAILBOX, "loop", "https://x.com/i/status/111"),   # loop guard
            _msg("m4", "dan@negevlabs.com", "fyi", "no links in here at all"),     # nothing to do
        ]
        _mock_inbox(monkeypatch, msgs)
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and calls == []

    def test_dedup_skips_already_processed_id(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        os.makedirs(os.path.dirname(xte.STORE_PATH), exist_ok=True)
        json.dump({"processed_ids": ["m1"]}, open(xte.STORE_PATH, "w"))
        _mock_inbox(monkeypatch, [_msg("m1", "bk@negevlabs.com", "again", "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and calls == []

    def test_reply_failure_not_marked_processed(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m5", "bk@negevlabs.com", "x", "https://x.com/i/status/111")])

        def boom(url, json_body):
            raise RuntimeError("graph 500")
        monkeypatch.setattr(eps, "graph_post", boom)
        res = xte.run()
        assert res["replied"] == 0
        assert "m5" not in json.load(open(xte.STORE_PATH))["processed_ids"]  # will retry next run

    def test_post_draft_failure_leaves_message_unprocessed_and_cleans_up_draft(self, xte_files, monkeypatch):
        """Unlike test_reply_failure_not_marked_processed (which fails at the
        very first graph_post call, before any draft exists), this fails on
        the final send -- AFTER createReply already created a draft. Must
        still retry next run, AND must not leave that draft orphaned."""
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m6", "bk@negevlabs.com", "x", "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)

        def _boom_post(url, json_body):
            calls.append({"method": "POST", "url": url, "body": json_body})
            if url.endswith("/createReply"):
                return {"id": "draft-1"}
            if url.endswith("/send"):
                raise RuntimeError("graph 500 on send")
            return {}

        monkeypatch.setattr(eps, "graph_post", _boom_post)
        res = xte.run()
        assert res["replied"] == 0
        assert "m6" not in json.load(open(xte.STORE_PATH))["processed_ids"]  # will retry next run
        seq = [(c["method"], c["url"].rsplit("/", 1)[-1]) for c in calls]
        assert seq == [("POST", "createReply"), ("PATCH", "draft-1"),
                       ("POST", "attachments"), ("POST", "send"), ("DELETE", "draft-1")]


# ----------------------------------------------------------------------
#  Task 4 -- kind dispatch wired into the scan, real source in attachments
# ----------------------------------------------------------------------

class TestSourceWiring:
    def test_youtube_link_transcribes_and_attaches(self, xte_files, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "YT WORDS",
                                              "chars": 8, "error": "", "source": "YouTube captions"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: Clip\nTL;DR: ok\nKEY POINTS:\n- p1")
        _mock_inbox(monkeypatch, [_msg("y1", "bk@negevlabs.com", "watch",
                                       "https://www.youtube.com/watch?v=abc123")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        atts = _sent_attachments(calls)
        assert atts[0]["name"] == "transcript_abc123.md"
        md = base64.b64decode(atts[0]["contentBytes"]).decode("utf-8")
        assert "YT WORDS" in md and "YouTube captions" in md

    def test_kind_is_passed_to_transcribe_link(self, xte_files, monkeypatch):
        seen = []
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": seen.append(k) or {"url": u, "ok": False,
                                                                "error": "nope", "transcript": "",
                                                                "chars": 0, "source": ""})
        _mock_inbox(monkeypatch, [_msg("k1", "bk@negevlabs.com", "mixed",
                                       "https://x.com/i/status/111 https://youtu.be/abc")])
        _capture_graph(monkeypatch)
        xte.run()
        assert seen == ["x", "youtube"]

    def test_podcast_only_message_still_gets_a_reply(self, xte_files, monkeypatch):
        _mock_inbox(monkeypatch, [_msg("p1", "bk@negevlabs.com", "listen",
                                       "https://open.spotify.com/episode/xyz")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1          # silence would read as a broken service
        body = _sent_bodies(calls)[0]
        assert "not supported yet" in body
        assert not _sent_attachments(calls)  # nothing transcribed, nothing to attach
