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
from datetime import datetime, timedelta, timezone

import pytest

import x_transcribe_email as xte
import learn_digest as ld
import email_pipeline_sync as eps


@pytest.fixture
def xte_files(monkeypatch, tmp_path):
    monkeypatch.setattr(xte, "STORE_PATH", str(tmp_path / "store.json"))
    monkeypatch.setattr(xte, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(xte, "THREADS_PATH", str(tmp_path / "threads.json"))
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

    def test_drops_x_profile_and_nav_pages(self):
        """REVERSAL (was test_keeps_x_profile_but_drops_nav_pages): a profile
        URL used to be returned so it earned an honest "no video found" reply.
        It is now dropped like any other container -- see
        TestContainerUrlsAreNotRequests for why."""
        html = ('profile https://x.com/someone nav https://x.com/home '
                'post https://x.com/i/status/111')
        pairs = xte.find_media_links(html)
        assert not any(u.rstrip("/").endswith("someone") for u, _ in pairs)
        assert not any(u.rstrip("/").endswith("x.com/home") for u, _ in pairs)
        assert [u for u, _ in pairs] == ["https://x.com/i/status/111"]   # the post survives

    def test_dedups_x_by_normalized_url(self):
        html = "a https://x.com/i/status/111 b https://X.com/i/status/111/ c"
        assert len(xte.find_media_links(html)) == 1

    def test_dedups_youtube_by_case_and_trailing_slash(self):
        # The id has to be one _youtube_id actually parses (6+ chars): a URL
        # with no parseable video id is now a container and is dropped.
        html = "a https://youtu.be/AbCdefg/ b https://youtu.be/AbCdefg c"
        assert len(xte.find_media_links(html)) == 1

    def test_preserves_first_seen_order(self):
        html = ("yt https://www.youtube.com/watch?v=oneVideoId "
                "then x https://x.com/i/status/222")
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

    def test_youtube_unparseable_id_is_dropped(self):
        """REVERSAL (was test_youtube_unparseable_id_still_processed): a
        YouTube URL with no parseable video id used to be kept and handed on.
        It cannot name a single video, so it is now dropped like any other
        container -- see TestContainerUrlsAreNotRequests."""
        assert xte.find_media_links('invalid https://youtube.com/foo/bar/baz') == []

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
        # An X nav page followed by a period must still read as a container:
        # trailing punctuation is stripped upstream, and the URL names no item
        # (no /status/, /i/spaces/ or /i/broadcasts/ id). (_X_NONPOST, the old
        # bare-domain/nav list this used to name, was deleted -- the no-item
        # rule strictly subsumes it.)
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


class TestFooter:
    def test_footer_on_transcript_reply(self):
        body = xte.render_reply([{"url": "https://x.com/i/status/1", "ok": True, "title": "T",
                                  "summary": "TITLE: T\nTL;DR: ok"}])
        assert "Reply to this email to ask" in body
        assert "aren't supported yet" in body

    def test_footer_on_followup_reply(self):
        body = xte.render_answer("why?", "ANSWER: because")
        assert "Reply to this email to ask" in body
        assert "You asked:" in body


# ----------------------------------------------------------------------
#  transcribe_link (audio + STT reuse mocked)
# ----------------------------------------------------------------------

# A REAL-shaped YouTube video id (11 chars). transcribe_link now refuses a
# YouTube URL it cannot parse a video id out of (channel / playlist / handle),
# so the stand-in id in these fixtures has to be one ld._youtube_video_id
# actually matches -- its regex requires 6+ chars.
_YT_VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


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
        r = xte.transcribe_link(_YT_VIDEO, "youtube")
        assert r["ok"] and r["transcript"] == "caption text"
        assert r["source"] == "YouTube captions"

    def test_youtube_falls_back_to_stt_when_no_captions(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", lambda u: None)
        self._audio_ok(monkeypatch, tmp_path, text="spoken words")
        r = xte.transcribe_link(_YT_VIDEO, "youtube")
        assert r["ok"] and r["transcript"] == "spoken words"
        assert r["source"] == "xAI Grok STT"

    def test_youtube_caption_error_falls_back_not_crashes(self, monkeypatch, tmp_path):
        def _boom(u):
            raise RuntimeError("captions api down")
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", _boom)
        self._audio_ok(monkeypatch, tmp_path, text="spoken words")
        r = xte.transcribe_link(_YT_VIDEO, "youtube")
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
                            lambda u, t, note="": "TITLE: Vid\nTL;DR: ok\nKEY POINTS:\n- p1")

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
                                       "https://x.com/i/status/111 https://youtu.be/abcdefghijk")])
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


# ----------------------------------------------------------------------
#  Task 5 -- answer a question asked alongside the link
# ----------------------------------------------------------------------

class TestExtractNote:
    def test_strips_links_and_returns_prose(self):
        html = '<p>What does he say about valuations? https://x.com/i/status/111</p>'
        note = xte.extract_note(html, ["https://x.com/i/status/111"])
        assert note == "What does he say about valuations?"

    def test_link_only_body_yields_empty_note(self):
        assert xte.extract_note('<p>https://x.com/i/status/111</p>',
                                ["https://x.com/i/status/111"]) == ""

    def test_strips_untracked_urls_too(self):
        note = xte.extract_note("<p>see https://example.com/a and tell me why</p>", [])
        assert "example.com" not in note
        assert "tell me why" in note

    def test_truncates_to_2000_chars(self):
        note = xte.extract_note("<p>" + ("z" * 5000) + "</p>", [])
        assert len(note) == 2000


class TestQuestionInFirstEmail:
    def test_note_is_passed_to_the_summarizer(self, xte_files, monkeypatch):
        seen = {}
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "T" * 20,
                                              "chars": 20, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": seen.update(note=note) or "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("q1", "bk@negevlabs.com", "q",
                                       "What did he say about pricing? https://x.com/i/status/111")])
        _capture_graph(monkeypatch)
        xte.run()
        assert seen["note"] == "What did he say about pricing?"

    def test_no_prose_means_empty_note(self, xte_files, monkeypatch):
        seen = {}
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "T" * 20,
                                              "chars": 20, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": seen.update(note=note) or "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("q2", "bk@negevlabs.com", "", "https://x.com/i/status/111")])
        _capture_graph(monkeypatch)
        xte.run()
        assert seen["note"] == ""

    def test_prompt_includes_answer_instruction_only_when_note_present(self, monkeypatch):
        prompts = []
        monkeypatch.setattr(ld, "_call_claude_text",
                            lambda p, m, max_tokens=2000, tools=None, timeout=None:
                            prompts.append(p) or "TITLE: t")
        xte.summarize_transcript("https://x.com/i/status/1", "words", note="why?")
        xte.summarize_transcript("https://x.com/i/status/1", "words")
        assert "ANSWER:" in prompts[0] and "why?" in prompts[0]
        assert "ANSWER:" not in prompts[1]

    def test_answer_label_renders_bold(self):
        h = xte._summary_to_html("ANSWER: he said 40x\nTL;DR: gist")
        assert "<b>ANSWER: he said 40x</b>" in h


# ----------------------------------------------------------------------
#  Task 6 -- per-conversation transcript cache
# ----------------------------------------------------------------------

class TestThreadCache:
    def _ok(self, url="https://x.com/i/status/1", text="words"):
        return {"url": url, "ok": True, "title": "T", "transcript": text,
                "chars": len(text), "error": "", "source": "xAI Grok STT"}

    def test_remembers_successful_links(self, xte_files, monkeypatch):
        xte.remember_thread("conv-1", [self._ok()])
        entry = xte._load_threads()["conv-1"]
        assert entry["questions"] == 0
        assert entry["links"][0]["transcript"] == "words"
        assert entry["created_at"] and entry["updated_at"]

    def test_all_failed_is_not_cached(self, xte_files, monkeypatch):
        xte.remember_thread("conv-2", [{"url": "u", "ok": False, "error": "nope"}])
        assert "conv-2" not in xte._load_threads()

    def test_missing_conversation_id_is_ignored(self, xte_files, monkeypatch):
        xte.remember_thread("", [self._ok()])
        assert xte._load_threads() == {}

    def test_second_reply_appends_to_the_same_conversation(self, xte_files, monkeypatch):
        xte.remember_thread("conv-3", [self._ok(text="one")])
        xte.remember_thread("conv-3", [self._ok(url="https://youtu.be/b", text="two")])
        assert len(xte._load_threads()["conv-3"]["links"]) == 2

    def test_ttl_evicts_stale_conversations(self, xte_files, monkeypatch):
        old = (datetime.now(timezone.utc) - timedelta(days=xte.XTE_THREAD_TTL_DAYS + 1)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        pruned = xte._prune_threads({
            "old": {"created_at": old, "updated_at": old, "questions": 0, "links": []},
            "new": {"created_at": fresh, "updated_at": fresh, "questions": 0, "links": []},
        })
        assert "old" not in pruned and "new" in pruned

    def test_unparseable_timestamp_is_dropped_not_crashed(self, xte_files):
        pruned = xte._prune_threads({"bad": {"updated_at": "not-a-date", "links": []}})
        assert pruned == {}

    def test_max_entries_keeps_newest(self, xte_files, monkeypatch):
        monkeypatch.setattr(xte, "XTE_THREAD_MAX", 2)
        base = datetime.now(timezone.utc)
        threads = {}
        for i in range(4):
            ts = (base - timedelta(minutes=i)).isoformat()
            threads[f"c{i}"] = {"created_at": ts, "updated_at": ts, "questions": 0, "links": []}
        pruned = xte._prune_threads(threads)
        assert set(pruned) == {"c0", "c1"}   # newest two by updated_at

    def test_run_caches_the_conversation(self, xte_files, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": self._ok(u, "cached words"))
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        m = _msg("c1", "bk@negevlabs.com", "hi", "https://x.com/i/status/111")
        m["conversationId"] = "CONV-A"
        _mock_inbox(monkeypatch, [m])
        _capture_graph(monkeypatch)
        xte.run()
        assert xte._load_threads()["CONV-A"]["links"][0]["transcript"] == "cached words"


# ----------------------------------------------------------------------
#  Task 7 -- follow-up questions answered from the cached transcript
# ----------------------------------------------------------------------

def _followup_msg(mid, sender, conv_id, body):
    m = _msg(mid, sender, "Re: transcript", body)
    m["conversationId"] = conv_id
    return m


class TestAutoReplyDetection:
    def test_auto_submitted_flags_true(self):
        assert xte.is_auto_reply({"internetMessageHeaders": [
            {"name": "Auto-Submitted", "value": "auto-replied"}]})

    def test_auto_submitted_no_is_a_real_message(self):
        assert not xte.is_auto_reply({"internetMessageHeaders": [
            {"name": "Auto-Submitted", "value": "no"}]})

    def test_x_autoreply_and_precedence_bulk_flag_true(self):
        assert xte.is_auto_reply({"internetMessageHeaders": [{"name": "X-Autoreply", "value": "yes"}]})
        assert xte.is_auto_reply({"internetMessageHeaders": [{"name": "Precedence", "value": "bulk"}]})

    def test_no_headers_is_a_real_message(self):
        assert not xte.is_auto_reply({})


class TestRenderAnswer:
    def test_escapes_the_question_and_renders_the_answer(self):
        h = xte.render_answer("what about <b>margins</b>?", "ANSWER: 80 percent")
        assert "&lt;b&gt;margins&lt;/b&gt;" in h        # echoed back escaped, never injected
        assert "<b>ANSWER: 80 percent</b>" in h

    def test_empty_answer_says_so_rather_than_sending_an_empty_body(self):
        """answer_question returns '' when Claude fails -- the reply must say
        so plainly instead of arriving blank (or inventing an answer)."""
        h = xte.render_answer("what about margins?", "")
        assert "couldn't produce an answer" in h


class TestFollowUp:
    def _seed(self, conv_id="CONV-A", questions=0):
        now = datetime.now(timezone.utc).isoformat()
        xte._save_threads({conv_id: {
            "created_at": now, "updated_at": now, "questions": questions,
            "links": [{"url": "https://x.com/i/status/1", "title": "T",
                       "transcript": "he said margins are 80 percent"}]}})

    def _patch_answer(self, monkeypatch):
        monkeypatch.setattr(xte, "answer_question",
                            lambda q, links: "ANSWER: margins are 80 percent")

    def test_answers_link_free_reply_in_known_conversation(self, xte_files, monkeypatch):
        self._seed(); self._patch_answer(monkeypatch)
        _mock_inbox(monkeypatch, [_followup_msg("f1", "bk@negevlabs.com", "CONV-A", "what about margins?")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        body = _sent_bodies(calls)[0]
        assert "margins are 80 percent" in body
        assert not _sent_attachments(calls)          # transcript already delivered
        assert xte._load_threads()["CONV-A"]["questions"] == 1
        assert "f1" in json.load(open(xte.STORE_PATH))["processed_ids"]

    def test_unknown_conversation_is_ignored(self, xte_files, monkeypatch):
        self._seed(); self._patch_answer(monkeypatch)
        _mock_inbox(monkeypatch, [_followup_msg("f2", "bk@negevlabs.com", "CONV-UNKNOWN", "what about margins?")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0 and calls == []

    def test_question_cap_goes_silent(self, xte_files, monkeypatch):
        self._seed(questions=xte.XTE_THREAD_MAX_QUESTIONS)
        asked = []
        monkeypatch.setattr(xte, "answer_question",
                            lambda q, links: asked.append(q) or "ANSWER: margins are 80 percent")
        _mock_inbox(monkeypatch, [_followup_msg("f3", "bk@negevlabs.com", "CONV-A", "one more?")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0
        assert calls == []   # silence, not a "limit reached" reply that would feed the loop
        assert asked == []   # short-circuited before the Claude call, so no cost either

    def test_autoresponder_is_skipped(self, xte_files, monkeypatch):
        self._seed(); self._patch_answer(monkeypatch)
        m = _followup_msg("f4", "bk@negevlabs.com", "CONV-A", "I am out of office")
        m["internetMessageHeaders"] = [{"name": "Auto-Submitted", "value": "auto-replied"}]
        _mock_inbox(monkeypatch, [m])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0 and calls == []

    def test_ineligible_sender_is_ignored(self, xte_files, monkeypatch):
        self._seed(); self._patch_answer(monkeypatch)
        _mock_inbox(monkeypatch, [_followup_msg("f5", "rando@gmail.com", "CONV-A", "what about margins?")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0 and calls == []

    def test_team_extra_address_is_eligible(self, xte_files, monkeypatch):
        self._seed(); self._patch_answer(monkeypatch)
        monkeypatch.setattr(xte, "XTE_TEAM_EXTRA", ["ibelotsky@gmail.com"])
        _mock_inbox(monkeypatch, [_followup_msg("f6", "ibelotsky@gmail.com", "CONV-A", "margins?")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1 and calls

    def test_empty_question_is_skipped(self, xte_files, monkeypatch):
        self._seed(); self._patch_answer(monkeypatch)
        _mock_inbox(monkeypatch, [_followup_msg("f7", "bk@negevlabs.com", "CONV-A", "")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0 and calls == []

    def test_team_extra_address_gets_a_first_transcription_too(self, xte_files, monkeypatch):
        """Eligibility applies to the original link email, not just follow-ups."""
        monkeypatch.setattr(xte, "XTE_TEAM_EXTRA", ["ibelotsky@gmail.com"])
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "T" * 20,
                                              "chars": 20, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("e1", "ibelotsky@gmail.com", "hi",
                                       "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        assert _sent_attachments(calls)

    def test_followup_save_keeps_a_conversation_cached_in_the_same_scan(self, xte_files, monkeypatch):
        """A link message earlier in the SAME scan caches its conversation via
        remember_thread, which writes straight to disk. Writing run()'s
        top-of-run snapshot back wholesale would drop that entry and silently
        lose the transcript a later follow-up in it would need."""
        self._seed(); self._patch_answer(monkeypatch)
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "new words",
                                              "chars": 9, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        link_msg = _msg("n1", "bk@negevlabs.com", "new clip", "https://x.com/i/status/999")
        link_msg["conversationId"] = "CONV-B"
        _mock_inbox(monkeypatch, [link_msg,
                                  _followup_msg("f8", "bk@negevlabs.com", "CONV-A", "what about margins?")])
        _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 2
        threads = xte._load_threads()
        assert threads["CONV-A"]["questions"] == 1
        assert threads["CONV-B"]["links"][0]["transcript"] == "new words"   # not clobbered

    def test_answer_prompt_is_grounded_in_the_cached_transcript(self, monkeypatch):
        prompts = []
        monkeypatch.setattr(ld, "_call_claude_text",
                            lambda p, m, max_tokens=2000, tools=None, timeout=None:
                            prompts.append(p) or "ANSWER: yes")
        out = xte.answer_question("what margins?",
                                  [{"url": "u", "title": "T", "transcript": "margins are 80 percent"}])
        assert out == "ANSWER: yes"
        assert "margins are 80 percent" in prompts[0]
        assert "what margins?" in prompts[0]
        assert "ONLY" in prompts[0]


# ----------------------------------------------------------------------
#  Task 7b -- do not answer a follow-up that is not a question
# ----------------------------------------------------------------------

class TestNoQuestionFollowUp:
    def test_marker_detected_exactly(self):
        assert xte._is_no_question("NO_QUESTION")
        assert xte._is_no_question("  no_question  ")
        assert xte._is_no_question("NO_QUESTION.")

    def test_marker_not_matched_inside_a_real_answer(self):
        assert not xte._is_no_question("ANSWER: he said NO_QUESTION was asked")
        assert not xte._is_no_question("ANSWER: margins are 80 percent")

    def test_empty_answer_is_not_a_no_question(self):
        # A Claude failure must still produce the honest-failure reply.
        assert not xte._is_no_question("")
        assert not xte._is_no_question("   ")

    def test_thanks_reply_sends_nothing_but_is_still_counted(self, xte_files, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        xte._save_threads({"CONV-A": {
            "created_at": now, "updated_at": now, "questions": 0,
            "links": [{"url": "u", "title": "T", "transcript": "words"}]}})
        monkeypatch.setattr(xte, "answer_question", lambda q, links: "NO_QUESTION")
        _mock_inbox(monkeypatch, [_followup_msg("n1", "bk@negevlabs.com", "CONV-A", "thanks!")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert calls == []                                   # no reply sent
        assert res["replied"] == 0
        assert xte._load_threads()["CONV-A"]["questions"] == 1   # still budgeted
        assert "n1" in json.load(open(xte.STORE_PATH))["processed_ids"]

    def test_real_question_still_answered(self, xte_files, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        xte._save_threads({"CONV-A": {
            "created_at": now, "updated_at": now, "questions": 0,
            "links": [{"url": "u", "title": "T", "transcript": "margins are 80 percent"}]}})
        monkeypatch.setattr(xte, "answer_question", lambda q, links: "ANSWER: 80 percent")
        _mock_inbox(monkeypatch, [_followup_msg("n2", "bk@negevlabs.com", "CONV-A", "what margins?")])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert "80 percent" in _sent_bodies(calls)[0]

    def test_claude_failure_still_sends_honest_reply(self, xte_files, monkeypatch):
        now = datetime.now(timezone.utc).isoformat()
        xte._save_threads({"CONV-A": {
            "created_at": now, "updated_at": now, "questions": 0,
            "links": [{"url": "u", "title": "T", "transcript": "words"}]}})
        monkeypatch.setattr(xte, "answer_question", lambda q, links: "")
        _mock_inbox(monkeypatch, [_followup_msg("n3", "bk@negevlabs.com", "CONV-A", "what margins?")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        assert "couldn't produce an answer" in _sent_bodies(calls)[0]

    def test_prompt_tells_the_model_about_the_marker(self, monkeypatch):
        prompts = []
        monkeypatch.setattr(ld, "_call_claude_text",
                            lambda p, m, max_tokens=2000, tools=None, timeout=None:
                            prompts.append(p) or "NO_QUESTION")
        xte.answer_question("thanks", [{"url": "u", "title": "T", "transcript": "w"}])
        assert "NO_QUESTION" in prompts[0]


# ----------------------------------------------------------------------
#  Final review wave -- link-path guards (signature links, non-video
#  YouTube URLs, post-send failures)
# ----------------------------------------------------------------------

def _cached(conv_id, url, transcript="he said margins are 80 percent", questions=0, links=None):
    """Seed the per-conversation transcript cache with one already-transcribed
    link (or an explicit, possibly corrupted, links value)."""
    now = datetime.now(timezone.utc).isoformat()
    xte._save_threads({conv_id: {
        "created_at": now, "updated_at": now, "questions": questions,
        "links": links if links is not None else
        [{"url": url, "title": "T", "transcript": transcript}]}})


class TestLinkKey:
    def test_same_video_different_forms_share_a_key(self):
        assert (xte._link_key("https://youtu.be/dQw4w9WgXcQ")
                == xte._link_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_x_forms_share_a_key(self):
        assert (xte._link_key("https://twitter.com/i/status/111")
                == xte._link_key("https://X.com/i/status/111/"))

    def test_different_videos_do_not_share_a_key(self):
        assert (xte._link_key("https://youtu.be/dQw4w9WgXcQ")
                != xte._link_key("https://youtu.be/AbCdEfGhIjK"))

    def test_has_new_link_matches_a_cached_link_across_forms(self):
        entry = {"links": [{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}]}
        assert not xte._has_new_link([("https://youtu.be/dQw4w9WgXcQ", "youtube")], entry)
        assert xte._has_new_link([("https://youtu.be/AbCdEfGhIjK", "youtube")], entry)

    def test_has_new_link_survives_a_corrupted_cache_entry(self):
        assert xte._has_new_link([("https://youtu.be/dQw4w9WgXcQ", "youtube")],
                                 {"links": "corrupted-not-a-list"})
        assert xte._has_new_link([("https://youtu.be/dQw4w9WgXcQ", "youtube")], {"links": 7})


class TestSignatureLinkDoesNotHijackFollowUp:
    """FIX 1: uniqueBody keeps the sender's signature, and find_media_links
    accepts profile / channel / show URLs. Before this guard, a signature link
    pushed every follow-up down the transcription path -- the question was
    dropped and an unsolicited failure reply went out."""

    def test_followup_with_a_cached_signature_link_is_answered_not_transcribed(
            self, xte_files, monkeypatch):
        _cached("CONV-A", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        monkeypatch.setattr(xte, "answer_question",
                            lambda q, links: "ANSWER: margins are 80 percent")
        seen = []
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": seen.append(u) or {
                                "url": u, "ok": False, "error": "nope",
                                "transcript": "", "chars": 0, "source": ""})
        body = ('<p>what did he say about pricing?</p>'
                '<p>--<br>Ken Belotsky, Palomar Labs<br>'
                '<a href="https://youtu.be/dQw4w9WgXcQ">our latest clip</a></p>')
        _mock_inbox(monkeypatch, [_followup_msg("s1", "bk@negevlabs.com", "CONV-A", body)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert seen == []                                  # signature link never transcribed
        assert res["replied"] == 1
        assert res["outcomes"][0].get("followup") is True
        assert "margins are 80 percent" in _sent_bodies(calls)[0]
        assert not _sent_attachments(calls)
        assert xte._load_threads()["CONV-A"]["questions"] == 1

    def test_a_genuinely_new_link_in_a_cached_conversation_still_transcribes(
            self, xte_files, monkeypatch):
        _cached("CONV-A", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        asked = []
        monkeypatch.setattr(xte, "answer_question", lambda q, links: asked.append(q) or "ANSWER: no")
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "new words",
                                              "chars": 9, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        body = ('<p>and this one too?</p>'
                '<p>--<br>Ken<br><a href="https://youtu.be/dQw4w9WgXcQ">sig</a></p>'
                '<p>https://youtu.be/AbCdEfGhIjK</p>')
        _mock_inbox(monkeypatch, [_followup_msg("s2", "bk@negevlabs.com", "CONV-A", body)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert asked == []                                 # not routed to the follow-up path
        assert _sent_attachments(calls)                    # a real transcription reply

    def test_link_mail_in_an_uncached_conversation_still_transcribes(self, xte_files, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "words",
                                              "chars": 5, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("s3", "bk@negevlabs.com", "new", "https://x.com/i/status/111")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        assert _sent_attachments(calls)

    def test_autoresponder_carrying_a_media_link_is_skipped_and_marked_processed(
            self, xte_files, monkeypatch):
        """The link path had no is_auto_reply check at all: an autoresponder
        whose template carries a media link bypassed BOTH loop breakers."""
        seen = []
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": seen.append(u) or {"url": u, "ok": True,
                                                                "transcript": "w", "chars": 1,
                                                                "error": "", "source": ""})
        # Patched so a REGRESSION fails offline instead of reaching the live
        # Claude API (the no_network fixture blocks requests, not httpx).
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        m = _msg("a1", "bk@negevlabs.com", "Out of office", "https://x.com/i/status/111")
        m["internetMessageHeaders"] = [{"name": "Auto-Submitted", "value": "auto-replied"}]
        _mock_inbox(monkeypatch, [m])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 0 and calls == [] and seen == []
        assert "a1" in json.load(open(xte.STORE_PATH))["processed_ids"]   # not re-evaluated


class TestYouTubeNonVideoLinks:
    """FIX 2: a channel / playlist / handle URL has no video id, so captions
    return None and the old code handed it to yt-dlp -- which a channel has no
    duration to cap, on a daemon thread that outlives its join."""

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/playlist?list=PLabcdefgh",
        "https://www.youtube.com/c/SomeName",
    ])
    def test_non_video_url_fails_honestly_without_touching_ytdlp(self, monkeypatch, url):
        called = []
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: called.append(u) or (None, 0, "err", None))
        monkeypatch.setattr(ld, "_fetch_youtube_transcript",
                            lambda u: called.append(u) or None)
        r = xte.transcribe_link(url, "youtube")
        assert called == []                      # resolver never invoked
        assert not r["ok"]
        assert "channel or playlist" in r["error"]
        assert "channel or playlist" in xte._failure_message(r["error"])

    def test_a_real_video_url_still_reaches_the_resolvers(self, monkeypatch):
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", lambda u: "caption text")
        r = xte.transcribe_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube")
        assert r["ok"] and r["transcript"] == "caption text"

    def test_video_id_lookup_error_is_contained(self, monkeypatch):
        called = []

        def _boom(u):
            raise RuntimeError("regex blew up")

        monkeypatch.setattr(ld, "_youtube_video_id", _boom)
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: called.append(u) or (None, 0, "err", None))
        r = xte.transcribe_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube")
        assert called == [] and not r["ok"]


class TestPostSendFailures:
    """FIX 3: anything raising between the send and processed.add re-sends that
    email on every 15-minute scan, forever."""

    def _ok_transcribe(self, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "words",
                                              "chars": 5, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")

    def test_remember_thread_coerces_a_corrupted_links_value(self, xte_files):
        _cached("CONV-C", "", links="corrupted-not-a-list")
        xte.remember_thread("CONV-C", [{"url": "https://x.com/i/status/1", "ok": True,
                                        "title": "T", "transcript": "words"}])
        links = xte._load_threads()["CONV-C"]["links"]
        assert isinstance(links, list) and len(links) == 1

    def test_corrupted_cache_does_not_unsend_a_delivered_reply(self, xte_files, monkeypatch):
        self._ok_transcribe(monkeypatch)
        _cached("CONV-C", "", links="corrupted-not-a-list")
        m = _msg("c9", "bk@negevlabs.com", "clip", "https://x.com/i/status/111")
        m["conversationId"] = "CONV-C"
        _mock_inbox(monkeypatch, [m])
        _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert "c9" in json.load(open(xte.STORE_PATH))["processed_ids"]   # never re-sent

    def test_remember_thread_failure_does_not_unsend_the_reply(self, xte_files, monkeypatch):
        """Any caching failure at all -- not just a corrupted links value."""
        self._ok_transcribe(monkeypatch)

        def _boom(conversation_id, results):
            raise RuntimeError("disk full")

        monkeypatch.setattr(xte, "remember_thread", _boom)
        _mock_inbox(monkeypatch, [_msg("c10", "bk@negevlabs.com", "clip", "https://x.com/i/status/111")])
        _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert "c10" in json.load(open(xte.STORE_PATH))["processed_ids"]

    def test_corrupted_questions_counter_does_not_abort_the_scan(self, xte_files, monkeypatch):
        _cached("CONV-A", "https://x.com/i/status/1", questions="many")
        monkeypatch.setattr(xte, "answer_question", lambda q, links: "ANSWER: 80 percent")
        _mock_inbox(monkeypatch, [_followup_msg("q9", "bk@negevlabs.com", "CONV-A", "what margins?")])
        _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert xte._load_threads()["CONV-A"]["questions"] == 1

    def test_as_int_defaults_instead_of_raising(self):
        assert xte._as_int("many") == 0
        assert xte._as_int(None) == 0
        assert xte._as_int({"a": 1}) == 0
        assert xte._as_int("7") == 7
        assert xte._as_int(3) == 3

    def test_processed_persists_across_a_mid_scan_abort(self, xte_files, monkeypatch):
        """A container restart between two messages must not re-send the reply
        already delivered for the first one."""
        def _t(u, k="x"):
            if "999" in u:
                raise KeyboardInterrupt("container restart")
            return {"url": u, "ok": True, "transcript": "words", "chars": 5,
                    "error": "", "source": "xAI Grok STT"}

        monkeypatch.setattr(xte, "transcribe_link", _t)
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [
            _msg("ok1", "bk@negevlabs.com", "first", "https://x.com/i/status/111"),
            _msg("boom", "bk@negevlabs.com", "second", "https://x.com/i/status/999"),
        ])
        _capture_graph(monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            xte.run()
        store = json.load(open(xte.STORE_PATH))
        assert "ok1" in store["processed_ids"]      # already replied -> never re-sent
        assert "boom" not in store["processed_ids"]

    def test_followup_processed_persists_across_a_mid_scan_abort(self, xte_files, monkeypatch):
        _cached("CONV-A", "https://x.com/i/status/1")

        def _answer(q, links):
            if "boom" in q:
                raise KeyboardInterrupt("container restart")
            return "ANSWER: 80 percent"

        monkeypatch.setattr(xte, "answer_question", _answer)
        _mock_inbox(monkeypatch, [
            _followup_msg("fa", "bk@negevlabs.com", "CONV-A", "what margins?"),
            _followup_msg("fb", "bk@negevlabs.com", "CONV-A", "boom now"),
        ])
        _capture_graph(monkeypatch)
        with pytest.raises(KeyboardInterrupt):
            xte.run()
        assert "fa" in json.load(open(xte.STORE_PATH))["processed_ids"]


class TestPodcastDedupKey:
    def test_path_query_boundary_is_not_collapsed(self):
        """FIX 4: concatenating path+query with no separator made
        /episode/xy?zsi=abc and /episode/xyz?si=abc the same key, silently
        dropping one of the two links."""
        html = ("a https://open.spotify.com/episode/xy?zsi=abc "
                "b https://open.spotify.com/episode/xyz?si=abc c")
        pairs = xte.find_media_links(html)
        assert len(pairs) == 2

    def test_same_podcast_url_still_dedups(self):
        html = ("a https://open.spotify.com/episode/xyz?si=abc "
                "b https://open.spotify.com/episode/xyz?si=abc/ c")
        assert len(xte.find_media_links(html)) == 1

    def test_key_has_no_double_slash(self):
        assert "com//" not in xte._link_key("https://open.spotify.com/episode/xyz", "podcast")


class TestAutoReplyHyphenForm:
    def test_precedence_auto_reply_hyphen_is_detected(self):
        """FIX 5: some autoresponders emit the hyphen form. This is one of only
        two loop breakers."""
        assert xte.is_auto_reply({"internetMessageHeaders": [
            {"name": "Precedence", "value": "auto-reply"}]})

    def test_precedence_list_is_still_a_real_message(self):
        assert not xte.is_auto_reply({"internetMessageHeaders": [
            {"name": "Precedence", "value": "list"}]})


# ----------------------------------------------------------------------
#  Task 9 -- a container URL is not a transcription request
# ----------------------------------------------------------------------

class TestContainerUrlsAreNotRequests:
    """A CONTAINER url -- one naming a channel, profile, playlist or show
    rather than a single item -- produces no entry at all, exactly like an
    article URL.

    This REVERSES the original spec decision that ANY X link is returned "so a
    link with no video still earns an honest no-video reply". That rationale
    assumed the only way to reach Sara was to deliberately email her a link.
    Ordinary mail and follow-up questions now flow through the same gate (and
    Sara's inbox is shared with sara_corrections.py), so a container URL in a
    signature dropped the question and sent an unsolicited reply. The
    already-cached-link gate cannot help: a container URL can NEVER be cached
    -- a channel cannot transcribe, a profile has no video, a show is always
    unsupported -- so it could never self-heal."""

    @pytest.mark.parametrize("url", [
        "https://x.com/palomarlabs",                        # profile
        "https://twitter.com/someone",
        "https://x.com/home",                               # nav page
        "https://www.youtube.com/@palomarlabs",             # handle
        "https://www.youtube.com/c/SomeName",
        "https://www.youtube.com/user/SomeName",
        "https://www.youtube.com/channel/UCabcdefghij",
        "https://www.youtube.com/playlist?list=PLabcdefgh",
        "https://open.spotify.com/show/abc123",
        "https://open.spotify.com/playlist/abc123",
    ])
    def test_container_url_yields_no_entry(self, url):
        assert xte.find_media_links(f"see {url} thanks") == []

    @pytest.mark.parametrize("url", [
        "https://x.com/i/status/123",
        "https://x.com/someone/status/456",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://open.spotify.com/episode/abc123",           # still an item
    ])
    def test_single_item_url_is_still_detected(self, url):
        assert [u for u, _ in xte.find_media_links(f"see {url} thanks")] == [url]

    def test_spotify_show_gets_no_reply_at_all(self, xte_files, monkeypatch):
        """The /show/ page is a container: silence, not the unsupported reply."""
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("sh1", "bk@negevlabs.com", "listen",
                                       "https://open.spotify.com/show/abc123")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0
        assert calls == []

    def test_spotify_episode_still_gets_the_unsupported_reply(self, xte_files, monkeypatch):
        """The item URL keeps its honest "podcasts not supported yet" reply --
        the reversal drops containers, not detected items."""
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("ep1", "bk@negevlabs.com", "listen",
                                       "https://open.spotify.com/episode/abc123")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        assert "not supported yet" in _sent_bodies(calls)[0]

    def test_followup_with_an_uncached_profile_link_is_answered_not_transcribed(
            self, xte_files, monkeypatch):
        """END TO END. The gate added in the previous wave only recognized an
        ALREADY-CACHED link, and a profile URL can never be cached -- so a
        signature carrying one dropped the question and sent an off-topic
        failure reply instead of answering."""
        _cached("CONV-A", "https://x.com/i/status/1")
        monkeypatch.setattr(xte, "answer_question",
                            lambda q, links: "ANSWER: margins are 80 percent")
        seen = []
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": seen.append(u) or {
                                "url": u, "ok": False, "error": "nope",
                                "transcript": "", "chars": 0, "source": ""})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        body = ('<p>what did he say about pricing?</p>'
                '<p>--<br>Ken Belotsky, Palomar Labs<br>'
                '<a href="https://x.com/palomarlabs">follow us on X</a></p>')
        _mock_inbox(monkeypatch, [_followup_msg("cu1", "bk@negevlabs.com", "CONV-A", body)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert seen == []                                  # never transcribed
        assert res["replied"] == 1
        assert res["outcomes"][0].get("followup") is True  # answered, not transcribed
        assert "margins are 80 percent" in _sent_bodies(calls)[0]
        assert not _sent_attachments(calls)
        assert xte._load_threads()["CONV-A"]["questions"] == 1

    def test_ordinary_mail_carrying_only_container_links_gets_no_reply(
            self, xte_files, monkeypatch):
        """END TO END. Sara's inbox is SHARED -- sara_corrections.py scans the
        same folder, so team replies to Pulse/biweekly/digest reports are
        routine traffic in the same 25-message window. An ordinary email whose
        only links are a signature's channel/profile/show must earn NO reply:
        assert zero Graph calls, not merely replied == 0."""
        seen = []
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": seen.append(u) or {
                                "url": u, "ok": False, "error": "nope",
                                "transcript": "", "chars": 0, "source": ""})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        body = ('<p>Correction for the Pulse: Ariadne has no lead investor gap.</p>'
                '<p>--<br>Ken Belotsky, Palomar Labs<br>'
                '<a href="https://www.youtube.com/@palomarlabs">our channel</a> | '
                '<a href="https://x.com/palomarlabs">X</a> | '
                '<a href="https://open.spotify.com/show/abc123">our show</a></p>')
        _mock_inbox(monkeypatch, [_msg("od1", "bk@negevlabs.com", "Re: Weekly Pulse", body)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert calls == []                                 # no unsolicited reply, at all
        assert res["replied"] == 0
        assert seen == []


class TestYouTubeLiveAndLegacyForms:
    """REGRESSION FIX. ld._youtube_video_id covers only youtu.be/, v=,
    /shorts/ and /embed/, so youtube.com/live/<id> and youtube.com/v/<id>
    resolve to no id -- yet both are single videos that transcribed fine
    BEFORE this branch, and /live/ is the address-bar form for livestreams and
    premieres. Dropping every id-less YouTube URL would have swallowed them
    silently, which is worse than the wrong-reason refusal. learn_digest is
    shared and off-limits, so xte resolves them locally."""

    LIVE = "https://www.youtube.com/live/dQw4w9WgXcQ"
    LEGACY = "https://www.youtube.com/v/dQw4w9WgXcQ"

    @pytest.mark.parametrize("url", [LIVE, LEGACY])
    def test_detected_as_a_youtube_item(self, url):
        assert xte.find_media_links(f"watch {url} now") == [(url, "youtube")]

    @pytest.mark.parametrize("url", [LIVE, LEGACY])
    def test_reaches_the_transcription_path(self, monkeypatch, url):
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", lambda u: "caption text")
        r = xte.transcribe_link(url, "youtube")
        assert r["ok"] and r["transcript"] == "caption text"

    @pytest.mark.parametrize("url", [LIVE, LEGACY])
    def test_id_is_used_for_the_attachment_name(self, url):
        assert xte._status_id(url) == "dQw4w9WgXcQ"

    def test_live_form_is_the_same_video_as_the_watch_form(self):
        """One local resolver, so the dedup / already-cached key agrees with
        detection instead of falling back to the lexical form."""
        assert (xte._link_key(self.LIVE)
                == xte._link_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

    def test_live_url_transcribes_end_to_end(self, xte_files, monkeypatch):
        monkeypatch.setattr(ld, "_fetch_youtube_transcript", lambda u: "live caption text")
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: Stream\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("lv1", "bk@negevlabs.com", "stream", self.LIVE)])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        atts = _sent_attachments(calls)
        assert atts[0]["name"] == "transcript_dQw4w9WgXcQ.md"
        assert "live caption text" in base64.b64decode(atts[0]["contentBytes"]).decode("utf-8")


class TestModuleHygiene:
    def test_docstring_matches_what_the_module_now_does(self):
        """FIX 6: the docstring still described an X-only tool on the old
        domain, and omitted both loop breakers."""
        doc = xte.__doc__ or ""
        assert "sara@negevlabs.com" not in doc            # renamed to palomar-labs.com
        assert "youtube" in doc.lower()
        assert "is_auto_reply" in doc
        assert "XTE_THREAD_MAX_QUESTIONS" in doc

    def test_no_dead_module_lock(self):
        """FIX 7: _run_lock was never acquired -- the real guard is app.py's
        _xte_trigger_lock. A dead lock claiming that guarantee is misleading."""
        assert not hasattr(xte, "_run_lock")


# ----------------------------------------------------------------------
#  Task 10 / RESIDUAL A -- three ITEM shapes were swept up as containers
# ----------------------------------------------------------------------

class TestXSpacesAndBroadcastsAreItems:
    """RESIDUAL A. x.com/i/spaces/<id> and x.com/i/broadcasts/<id> name single
    ITEMS -- live audio that may genuinely transcribe -- but the blanket "an X
    path with no /status/ is a container" rule dropped them at detection, so
    find_media_links returned [], run() hit a bare continue, and the sender got
    TOTAL SILENCE. That contradicts this module's own principle (pinned in
    test_podcast_only_message_still_gets_a_reply: "silence would read as a
    broken service") -- a Spotify episode, which can NEVER transcribe, earned
    an honest reply while a Space, which might actually work, earned nothing."""

    SPACE = "https://x.com/i/spaces/1YpKkZWjWQvGj"
    BROADCAST = "https://x.com/i/broadcasts/1yNGaNqbrjbGj"

    @pytest.mark.parametrize("url", [SPACE, BROADCAST])
    def test_detected_as_an_x_item(self, url):
        assert xte.find_media_links(f"listen to {url} today") == [(url, "x")]

    @pytest.mark.parametrize("url", [SPACE, BROADCAST])
    def test_reaches_the_audio_resolver(self, monkeypatch, tmp_path, url):
        d = tmp_path / "aud"; d.mkdir()
        seen = []
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: seen.append(u) or (str(d / "a.m4a"), 30.0, None, str(d)))
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda p, timeout=None: ("space words", None))
        r = xte.transcribe_link(url, "x")
        assert seen == [url]                      # resolver actually invoked
        assert r["ok"] and r["transcript"] == "space words"

    @pytest.mark.parametrize("url,expected", [(SPACE, "1YpKkZWjWQvGj"),
                                              (BROADCAST, "1yNGaNqbrjbGj")])
    def test_attachment_id_is_the_item_id_not_post(self, url, expected):
        assert xte._status_id(url) == expected

    def test_space_and_broadcast_keys_do_not_collide(self):
        assert xte._link_key(self.SPACE) != xte._link_key(self.BROADCAST)
        assert xte._link_key(self.SPACE) != xte._link_key("https://x.com/i/status/111")

    def test_space_link_earns_an_honest_reply_instead_of_silence(self, xte_files, monkeypatch):
        """END TO END -- the residual itself: the reply may be a failure, but
        it must not be silence."""
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": False, "transcript": "", "chars": 0,
                                              "error": "no video could be found", "source": ""})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("sp1", "bk@negevlabs.com", "space", self.SPACE)])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        assert "No video was found" in _sent_bodies(calls)[0]

    @pytest.mark.parametrize("url", ["https://x.com/someone",
                                     "https://x.com/home",
                                     "https://twitter.com/palomarlabs"])
    def test_profiles_and_nav_pages_are_still_containers(self, url):
        """REGRESSION GUARD -- narrowing the rule must not reopen it."""
        assert xte.find_media_links(f"see {url} thanks") == []

    def test_spaces_start_nav_page_is_not_detected_but_a_real_id_still_is(self):
        """ITEM 3 regression guard: /i/spaces/start is X's own start-a-Space
        nav page, not a Space id -- narrowing _X_LIVE_ITEM_RE to exclude it
        must not reopen detection for a genuine Space id."""
        assert xte.find_media_links("see https://x.com/i/spaces/start thanks") == []
        assert xte.find_media_links(f"see {self.SPACE} thanks") == [(self.SPACE, "x")]


class TestYouTubeClipIsAnItem:
    """RESIDUAL A. A /clip/<id> URL names ONE item -- a user-trimmed excerpt of
    a single video -- but the underlying watch id is NOT in the URL, so
    _youtube_id can never resolve one: detection dropped it as a container
    (total silence) and transcribe_link, called directly, refused it as "a
    channel or playlist". yt-dlp can fetch a clip, so it now goes down the
    audio path and produces a real transcript or an honest failure. The clip id
    is NAMESPACED everywhere it is used as an identity, so it can never collide
    with a watch id made of the same characters."""

    CLIP = "https://www.youtube.com/clip/UgkxRV3S7Bnm0M2Rl0mKZDQ8ejlUgFR2m8Zx"
    SAME_CHARS = "dQw4w9WgXcQ"

    def test_detected_as_a_youtube_item(self):
        assert xte.find_media_links(f"watch {self.CLIP} now") == [(self.CLIP, "youtube")]

    def test_reaches_the_resolver_instead_of_the_channel_refusal(self, monkeypatch, tmp_path):
        # Captions are looked up by WATCH id, which a clip URL does not carry,
        # so ld._fetch_youtube_transcript short-circuits to None offline (no
        # import, no network) and the clip falls through to the audio path.
        d = tmp_path / "aud"; d.mkdir()
        seen = []
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: seen.append(u) or (str(d / "a.m4a"), 20.0, None, str(d)))
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda p, timeout=None: ("clip words", None))
        r = xte.transcribe_link(self.CLIP, "youtube")
        assert seen == [self.CLIP]                          # resolver actually invoked
        assert r["ok"] and r["transcript"] == "clip words"
        assert "channel or playlist" not in (r.get("error") or "")

    def test_clip_failure_is_honest_not_a_channel_refusal(self, monkeypatch):
        monkeypatch.setattr(ld, "extract_x_post_audio",
                            lambda u, timeout=None: (None, 0, "no video could be found in this clip", None))
        r = xte.transcribe_link(self.CLIP, "youtube")
        assert not r["ok"]
        assert "channel or playlist" not in r["error"]
        assert "No video was found" in xte._failure_message(r["error"])

    def test_clip_id_never_collides_with_a_watch_id(self):
        clip = f"https://www.youtube.com/clip/{self.SAME_CHARS}"
        watch = f"https://www.youtube.com/watch?v={self.SAME_CHARS}"
        assert xte._link_key(clip) != xte._link_key(watch)
        assert xte._status_id(clip) != xte._status_id(watch)

    def test_clip_key_is_stable_across_tracking_suffixes(self):
        assert xte._link_key(f"{self.CLIP}?si=abc123") == xte._link_key(self.CLIP)

    def test_clip_transcribes_end_to_end(self, xte_files, monkeypatch):
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "clip words",
                                              "chars": 10, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: Clip\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("cl1", "bk@negevlabs.com", "clip", self.CLIP)])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 1
        atts = _sent_attachments(calls)
        assert atts and atts[0]["name"].startswith("transcript_clip_")

    @pytest.mark.parametrize("url", ["https://www.youtube.com/@handle",
                                     "https://www.youtube.com/playlist?list=PLabcdefgh",
                                     "https://www.youtube.com/c/SomeName",
                                     "https://open.spotify.com/show/abc123"])
    def test_containers_are_still_dropped(self, url):
        """REGRESSION GUARD -- narrowing the rule must not reopen it."""
        assert xte.find_media_links(f"see {url} thanks") == []


# ----------------------------------------------------------------------
#  Task 10 / RESIDUAL B -- a podcast link never blocks a follow-up
# ----------------------------------------------------------------------

# A SHOW page on a non-Spotify host: the podcast container rule is Spotify
# path-shaped (/show/, /playlist/), so this is detected as an item.
_APPLE_SHOW = "https://podcasts.apple.com/us/podcast/acquired/id1050462261"
_SPOTIFY_EPISODE = "https://open.spotify.com/episode/abc123"


class TestPodcastLinkNeverBlocksAFollowUp:
    """RESIDUAL B. Show pages on Apple / anchor.fm / pod.link / overcast /
    castbox / podbean are still DETECTED as items (the container regex is
    Spotify-shaped), and a Spotify /episode/ deliberately is. A podcast result
    is NEVER ok, so remember_thread can never cache it -- if a podcast link
    counted as a new transcription request, _has_new_link would stay True
    forever and EVERY follow-up question in that conversation would be dropped
    in favour of a repeat "not supported yet" reply.

    Fixed at the GATE (only a TRANSCRIBABLE kind -- x, youtube -- can make a
    message a new request), NOT by chasing per-host URL shapes: a regex
    enumerating podcast hosts rots with every new host. Detection is unchanged,
    so a podcast-only email still earns its honest unsupported reply."""

    @pytest.mark.parametrize("podcast_url", [_APPLE_SHOW, _SPOTIFY_EPISODE])
    def test_followup_carrying_a_podcast_link_is_answered_not_transcribed(
            self, xte_files, monkeypatch, podcast_url):
        _cached("CONV-A", "https://x.com/i/status/1")
        monkeypatch.setattr(xte, "answer_question",
                            lambda q, links: "ANSWER: margins are 80 percent")
        seen = []
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": seen.append(u) or {
                                "url": u, "ok": False, "error": "nope",
                                "transcript": "", "chars": 0, "source": ""})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        body = ('<p>what did he say about pricing?</p>'
                '<p>--<br>Ken Belotsky, Palomar Labs<br>'
                f'<a href="{podcast_url}">our podcast</a></p>')
        _mock_inbox(monkeypatch, [_followup_msg("pb1", "bk@negevlabs.com", "CONV-A", body)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert seen == []                                  # podcast link never transcribed
        assert res["replied"] == 1
        assert res["outcomes"][0].get("followup") is True  # answered, not transcribed
        assert "margins are 80 percent" in _sent_bodies(calls)[0]
        assert not _sent_attachments(calls)
        assert xte._load_threads()["CONV-A"]["questions"] == 1

    def test_a_new_youtube_link_still_transcribes_alongside_a_podcast_link(
            self, xte_files, monkeypatch):
        """ORDERING: the gate must not over-block. A message in a cached
        conversation that carries a genuinely NEW transcribable link is still a
        transcription request, podcast link in the signature or not."""
        _cached("CONV-A", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        asked = []
        monkeypatch.setattr(xte, "answer_question",
                            lambda q, links: asked.append(q) or "ANSWER: no")
        monkeypatch.setattr(xte, "transcribe_link",
                            lambda u, k="x": {"url": u, "ok": True, "transcript": "new words",
                                              "chars": 9, "error": "", "source": "xAI Grok STT"})
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        body = ('<p>and this one?</p>'
                '<p>https://youtu.be/AbCdEfGhIjK</p>'
                f'<p>--<br>Ken<br><a href="{_APPLE_SHOW}">our podcast</a></p>')
        _mock_inbox(monkeypatch, [_followup_msg("pb3", "bk@negevlabs.com", "CONV-A", body)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert asked == []                                 # not routed to the follow-up path
        assert res["replied"] == 1
        assert _sent_attachments(calls)                    # a real transcription reply

    def test_podcast_only_mail_in_an_uncached_conversation_still_replies(
            self, xte_files, monkeypatch):
        """MUST NOT REGRESS. Detection is unchanged -- a podcast link is still
        an item, so a podcast-only email with nothing cached to answer from
        still earns its honest unsupported reply rather than silence."""
        monkeypatch.setattr(xte, "summarize_transcript",
                            lambda u, t, note="": "TITLE: V\nTL;DR: ok")
        _mock_inbox(monkeypatch, [_msg("pb4", "bk@negevlabs.com", "listen", _APPLE_SHOW)])
        calls = _capture_graph(monkeypatch)
        res = xte.run()
        assert res["replied"] == 1
        assert "not supported yet" in _sent_bodies(calls)[0]
        assert not _sent_attachments(calls)

    def test_has_new_link_ignores_podcast_kinds_in_a_known_conversation(self):
        entry = {"links": [{"url": "https://x.com/i/status/1"}]}
        assert not xte._has_new_link([(_APPLE_SHOW, "podcast")], entry)
        assert not xte._has_new_link([(_SPOTIFY_EPISODE, "podcast")], entry)
        assert xte._has_new_link([("https://youtu.be/AbCdEfGhIjK", "youtube")], entry)

    def test_podcast_link_still_counts_when_there_is_nothing_cached(self):
        """No entry means no transcript to answer from, so the podcast link
        must still read as a request -- that is what earns the honest reply."""
        assert xte._has_new_link([(_APPLE_SHOW, "podcast")], None)
        assert xte._has_new_link([(_APPLE_SHOW, "podcast")], {})
