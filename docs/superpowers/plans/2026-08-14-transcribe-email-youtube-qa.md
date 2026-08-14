# Transcribe-by-email: YouTube + Video Q&A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the deployed `x-transcribe-email` module so it transcribes YouTube links as well as X, and so anyone on the team can ask questions about a video -- both alongside the original link and by replying in-thread afterwards.

**Architecture:** All changes land in `x_transcribe_email.py`, plus one small helper added to `email_pipeline_sync.py`. No new module, no new mailbox, no new cron job, no new route. The existing 15-minute inbox scan and the existing `/transcribe-email/run` + `/transcribe-email/status` routes already carry this. Transcription reuses `learn_digest` resolvers; a new per-conversation transcript cache on `/data` makes follow-up questions cheap.

**Tech Stack:** Python 3.12, Flask (entrypoint only), Microsoft Graph (app-only auth), Anthropic Claude, xAI Grok STT, yt-dlp + ffmpeg, pytest.

## Global Constraints

- **ASCII-only** in comments and non-user-facing strings. Use `->` not an arrow character, `--` not an em-dash, `"` not smart quotes.
- **Never fabricate.** Every failure path reports its specific reason. No guessing, no filling gaps from outside knowledge.
- **Tests are offline-only.** Never write a test that calls a live API. The autouse `no_network` fixture in `tests/conftest.py` fails any real HTTP.
- **Never weaken, skip, or delete a test to make it pass.** Fix the code. Renaming a function and updating its tests to the new name is not weakening, provided the assertions stay equivalent.
- **Patch mocks in the function's home module** -- `monkeypatch.setattr(ld, "extract_x_post_audio", ...)`, not on `x_transcribe_email`.
- **`app.py` is stored CRLF** (`autocrlf=true`, no `.gitattributes`). Edit it preserving CRLF or `git diff` shows a full-file rewrite.
- **Version string** lives in exactly 2 places in `app.py` (`/version` and `/test`). Both get bumped to `2.28.0-transcribe-qa` in Task 8.
- **Existing behavior that must not regress:** links are read from `uniqueBody` only (a quoted link in thread history never re-fires); Sara's own outbound is skipped; a message whose processing raises is NOT marked processed so it retries; non-eligible senders are marked processed so they are not reconsidered every run.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `email_pipeline_sync.py` | Shared Graph HTTP helpers | Add `graph_patch` (~4 lines) |
| `x_transcribe_email.py` | The whole feature: scan, detect, transcribe, summarize, answer, reply | All other code changes |
| `tests/test_x_transcribe_email.py` | Offline tests for the above | Extended + updated for renames |
| `app.py` | Flask entrypoint | Version string only (2 places) |
| `CLAUDE.md` | Project map | Module section + failure modes |
| `CACHEBUST` | Docker layer invalidation | Fresh timestamp |

---

### Task 1: Threaded replies via createReply

Follow-up questions only work if Sara's reply stays in the same Exchange conversation. Today `send_reply` uses `sendMail`, which creates a fresh message that threads only by subject heuristics and does not reliably inherit `conversationId`. This task replaces it with Graph's `createReply` -> patch body -> attach -> send sequence, which inherits `conversationId`, subject and recipient.

**Files:**
- Modify: `email_pipeline_sync.py` (add `graph_patch` next to `graph_post` at line 172)
- Modify: `x_transcribe_email.py:300-309` (replace `send_reply`), `x_transcribe_email.py:337-344` (its call site)
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `eps.graph_post(url, json_body)`, `eps.MS_GRAPH_BASE`, `eps._request_with_retry(method, url, headers, json_body=None, params=None, ok_statuses=())`
- Produces: `eps.graph_patch(url: str, json_body: dict) -> dict`; `xte.send_threaded_reply(source_message_id: str, html_body: str, attachments: list = None) -> None` (raises `RuntimeError` if `createReply` returns no draft id). `send_reply` is removed.

- [ ] **Step 1: Add the Graph PATCH helper**

In `email_pipeline_sync.py`, immediately after `graph_post` (line 176):

```python
def graph_patch(url: str, json_body: dict) -> dict:
    headers = {"Authorization": f"Bearer {get_graph_token()}", "Content-Type": "application/json"}
    resp = _request_with_retry("PATCH", url, headers, json_body)
    return resp.json() if resp.content else {}
```

- [ ] **Step 2: Replace the test capture helper**

In `tests/test_x_transcribe_email.py`, replace `_capture_sends` (lines 40-43) with a helper that records the whole Graph call sequence and answers `createReply` with a draft id:

```python
def _capture_graph(monkeypatch):
    """Record every Graph write. createReply answers with a draft id so the
    threaded-reply sequence can run to completion offline."""
    calls = []

    def _post(url, json_body):
        calls.append({"method": "POST", "url": url, "body": json_body})
        if url.endswith("/createReply"):
            return {"id": "draft-1"}
        return {}

    def _patch(url, json_body):
        calls.append({"method": "PATCH", "url": url, "body": json_body})
        return {}

    monkeypatch.setattr(eps, "graph_post", _post)
    monkeypatch.setattr(eps, "graph_patch", _patch)
    return calls


def _sent_bodies(calls):
    """HTML bodies of every reply actually patched onto a draft."""
    return [c["body"]["body"]["content"] for c in calls
            if c["method"] == "PATCH" and "body" in (c["body"] or {})]


def _sent_attachments(calls):
    return [c["body"] for c in calls if c["url"].endswith("/attachments")]
```

- [ ] **Step 3: Write the failing test**

Add to `tests/test_x_transcribe_email.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestThreadedReply -v`
Expected: FAIL with `AttributeError: module 'x_transcribe_email' has no attribute 'send_threaded_reply'`

- [ ] **Step 5: Implement `send_threaded_reply`**

In `x_transcribe_email.py`, replace `send_reply` (lines 300-309) entirely with:

```python
def send_threaded_reply(source_message_id: str, html_body: str, attachments: list = None):
    """Reply in-thread via createReply so the reply inherits conversationId,
    subject and recipient. sendMail would thread only by subject heuristics,
    which breaks the conversationId match that follow-up questions rely on.

    Sequence: createReply (draft) -> PATCH the body -> POST each attachment ->
    send. Raises on a missing draft id so the caller does NOT mark the message
    processed and the next run retries it."""
    base = f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/messages"
    draft = eps.graph_post(f"{base}/{source_message_id}/createReply", {}) or {}
    draft_id = draft.get("id")
    if not draft_id:
        raise RuntimeError("createReply returned no draft id")
    eps.graph_patch(f"{base}/{draft_id}",
                    {"body": {"contentType": "HTML", "content": html_body}})
    for att in (attachments or []):
        eps.graph_post(f"{base}/{draft_id}/attachments", att)
    eps.graph_post(f"{base}/{draft_id}/send", {})
```

- [ ] **Step 6: Update the call site**

In `_process_message`, replace lines 342-344:

```python
    first_title = next((r["title"] for r in results if r["ok"]), "X video")
    subj = ("Re: " + subject) if subject.strip() else f"Transcript: {first_title}"
    send_reply(sender, subj, render_reply(results, truncated), attachments)
```

with (createReply sets the subject and recipient itself):

```python
    send_threaded_reply(m.get("id"), render_reply(results, truncated), attachments)
```

- [ ] **Step 7: Update the existing tests that asserted the sendMail shape**

In `TestRun`, replace every `_capture_sends(monkeypatch)` call with `_capture_graph(monkeypatch)`, rename the local from `sent` to `calls`, and update the two assertions that inspected the old payload.

`test_live_replies_with_transcript_attachment` becomes:

```python
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
```

`test_reply_failure_not_marked_processed` becomes:

```python
    def test_reply_failure_not_marked_processed(self, xte_files, monkeypatch):
        self._patch_ok(monkeypatch)
        _mock_inbox(monkeypatch, [_msg("m5", "bk@negevlabs.com", "x", "https://x.com/i/status/111")])

        def boom(url, json_body):
            raise RuntimeError("graph 500")
        monkeypatch.setattr(eps, "graph_post", boom)
        res = xte.run()
        assert res["replied"] == 0
        assert "m5" not in json.load(open(xte.STORE_PATH))["processed_ids"]  # will retry next run
```

For the three tests that only assert nothing was sent (`test_dry_run_...`, `test_external_sender_gated_out`, `test_skips_saras_own_mail_and_link_free_mail`, `test_dedup_skips_already_processed_id`), change `assert sent == []` to `assert calls == []`.

- [ ] **Step 8: Run the full file to verify everything passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 9: Commit**

```bash
git add email_pipeline_sync.py x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: thread xte replies via createReply so conversationId is preserved"
```

---

### Task 2: Multi-source link detection

`find_x_links` only ever returns X links. It becomes `find_media_links`, returning `(url, kind)` pairs for the three kinds this module acts on.

**Files:**
- Modify: `x_transcribe_email.py:119-136` (replace `find_x_links`), plus its two call sites at lines 323 and 379
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `ld.extract_urls(body_html) -> list[str]`, `ld.classify_url(url) -> str` (returns one of `x` / `youtube` / `podcast` / `article`), `ld._normalize_x_url(url) -> str`, module-level `_X_NONPOST` regex
- Produces: `xte.find_media_links(body_html: str) -> list[tuple[str, str]]` -- first-seen order, de-duplicated, each item `(url, kind)` where kind is `x`, `youtube` or `podcast`. `find_x_links` is removed.

- [ ] **Step 1: Write the failing test**

Replace the whole `TestLinkDetection` class in `tests/test_x_transcribe_email.py` with:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestLinkDetection -v`
Expected: FAIL with `AttributeError: module 'x_transcribe_email' has no attribute 'find_media_links'`

- [ ] **Step 3: Implement `find_media_links`**

In `x_transcribe_email.py`, replace `find_x_links` (lines 119-136) with:

```python
# Kinds this module acts on. An article link is not a transcription request and
# is omitted entirely, so a message carrying only articles falls through to the
# existing skip and is left for other handlers.
_SUPPORTED_KINDS = ("x", "youtube", "podcast")


def find_media_links(body_html: str) -> list:
    """Return de-duped (url, kind) pairs for x / youtube / podcast links found
    in the HTML, in first-seen order. Reuses learn_digest.extract_urls (handles
    both hrefs and plain text) and classify_url.

    ANY X link is returned (not just /status/), so a link with no video still
    earns an honest "no video found" reply; only the bare domain and navigation
    pages (home/search/settings/...) are ignored."""
    out, seen = [], set()
    for u in ld.extract_urls(body_html or ""):
        kind = ld.classify_url(u)
        if kind not in _SUPPORTED_KINDS:
            continue
        if kind == "x":
            if _X_NONPOST.match(u):
                continue
            norm = ld._normalize_x_url(u)
        else:
            norm = (u or "").strip().rstrip("/").lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append((u, kind))
    return out
```

- [ ] **Step 4: Update the two call sites so the module still imports and runs**

In `_process_message` (line 323), change:

```python
    links = find_x_links(body_html)
```

to:

```python
    pairs = find_media_links(body_html)
```

and change the two lines below it to operate on pairs:

```python
    truncated = max(0, len(pairs) - XTE_MAX_LINKS)
    pairs = pairs[:XTE_MAX_LINKS]
```

then change the transcription loop header (line 328) from `for url in links:` to `for url, _kind in pairs:` -- the kind is wired in during Task 3.

In `run()` (line 379), change:

```python
        links = find_x_links((m.get("uniqueBody") or {}).get("content", ""))
```

to:

```python
        links = find_media_links((m.get("uniqueBody") or {}).get("content", ""))
```

and in the dry-run branch (line 389) change `"would_transcribe": links[:XTE_MAX_LINKS]` to:

```python
                             "would_transcribe": [u for u, _ in links[:XTE_MAX_LINKS]],
```

- [ ] **Step 5: Run the full file to verify everything passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: detect youtube and podcast links alongside x in xte"
```

---

### Task 3: Source dispatch -- YouTube transcription, podcast reported unsupported

`extract_x_post_audio` at `learn_digest.py:840` is a generic yt-dlp wrapper despite its name: it takes any URL and enforces the `LEARN_STT_MAX_DURATION_SEC` (60 min) and `LEARN_STT_MAX_BYTES` caps. So YouTube needs no new extraction machinery -- only captions-first ordering and a dispatch.

**Files:**
- Modify: `x_transcribe_email.py:144-168` (`transcribe_link`), `:238-249` (`_status_id`, `_transcript_md`), `:261-268` (`_failure_message`)
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `ld.extract_x_post_audio(url, timeout=None) -> (audio_path, duration, err, tmpdir)`, `ld._grok_stt_from_file(path, timeout=None) -> (text, error)`, `ld._fetch_youtube_transcript(url) -> str | None`, `ld._youtube_video_id(url) -> str | None`
- Produces: `xte.transcribe_link(url: str, kind: str = "x") -> dict` with keys `url`, `ok`, `error`, `transcript`, `chars`, `source` (`"YouTube captions"` or `"xAI Grok STT"`, `""` on failure); `xte.PODCAST_UNSUPPORTED: str`; `xte._transcript_md(title, url, transcript, source="xAI Grok STT")`

- [ ] **Step 1: Write the failing test**

Replace the whole `TestTranscribeLink` class with:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestTranscribeLink -v`
Expected: FAIL -- `transcribe_link()` takes 1 positional argument but 2 were given

- [ ] **Step 3: Implement the dispatch**

In `x_transcribe_email.py`, replace `transcribe_link` (lines 144-168) with:

```python
PODCAST_UNSUPPORTED = (
    "Spotify and other podcast links are not supported yet -- podcast audio is "
    "DRM-protected and cannot be downloaded for transcription."
)


def _transcribe_audio_url(url: str) -> dict:
    """Shared yt-dlp + Grok STT path. ld.extract_x_post_audio is a generic
    yt-dlp wrapper despite its name -- it takes any URL and enforces the
    duration and size caps -- so this serves X and YouTube alike.
    Never raises; ok=False carries a specific, honest reason."""
    result = {"url": url, "ok": False, "error": "", "transcript": "", "chars": 0, "source": ""}
    tmpdir = None
    try:
        audio_path, _duration, err, tmpdir = ld.extract_x_post_audio(url)
        if err or not audio_path:
            result["error"] = err or "no audio could be extracted from this link"
            return result
        text, stt_err = ld._grok_stt_from_file(audio_path)
        if stt_err or not text:
            result["error"] = stt_err or "speech-to-text returned an empty transcript"
            return result
        result["ok"] = True
        result["transcript"] = text
        result["chars"] = len(text)
        result["source"] = "xAI Grok STT"
        return result
    except Exception as e:
        logger.warning(f"[xte] transcribe failed {url[:70]}: {e}")
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


def transcribe_link(url: str, kind: str = "x") -> dict:
    """Transcribe one link by kind. Never raises; ok=False carries a specific,
    honest error reason (no fabrication).

    youtube: captions first (fast, free, and most videos have them), falling
    back to the same yt-dlp + STT path as X only when there are none.
    podcast: reported unsupported without calling any resolver."""
    if kind == "podcast":
        return {"url": url, "ok": False, "error": PODCAST_UNSUPPORTED,
                "transcript": "", "chars": 0, "source": ""}
    if kind == "youtube":
        try:
            captions = ld._fetch_youtube_transcript(url)
        except Exception as e:
            logger.warning(f"[xte] youtube captions failed {url[:70]}: {e}")
            captions = None
        if captions:
            return {"url": url, "ok": True, "error": "", "transcript": captions,
                    "chars": len(captions), "source": "YouTube captions"}
    return _transcribe_audio_url(url)
```

- [ ] **Step 4: Make the attachment name and transcript header honest**

Replace `_status_id` (lines 238-240) with:

```python
def _status_id(url: str) -> str:
    """Stable short id for the attachment filename: X status id, else YouTube
    video id, else 'post'."""
    m = _STATUS_LINK_RE.search(url or "")
    if m:
        return m.group(1)
    try:
        vid = ld._youtube_video_id(url or "")
    except Exception:
        vid = None
    return vid or "post"
```

Replace `_transcript_md` (lines 243-249) with:

```python
def _transcript_md(title: str, url: str, transcript: str, source: str = "xAI Grok STT") -> str:
    return (
        f"# Transcript -- {title}\n\n"
        f"- Source: {url}\n"
        f"- Transcribed by: Sara ({source})\n\n"
        f"---\n\n{transcript}\n"
    )
```

Add a podcast branch as the FIRST check in `_failure_message` (line 263, before the `no video` check) so the full sentence passes through unmangled:

```python
    if "not supported yet" in low:
        return error or PODCAST_UNSUPPORTED
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: transcribe youtube via captions with STT fallback; report podcasts unsupported"
```

---

### Task 4: Wire the dispatch into the scan

The pieces exist; now `_process_message` passes the kind through, uses the real source in the attachment, and a podcast-only message earns a reply instead of silence.

**Files:**
- Modify: `x_transcribe_email.py:317-348` (`_process_message`)
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `find_media_links`, `transcribe_link(url, kind)`, `_transcript_md(..., source=...)` from Tasks 2-3
- Produces: no new public names; `_process_message(m: dict) -> dict` return shape gains `"kind"` per link entry

- [ ] **Step 1: Write the failing test**

Add to `tests/test_x_transcribe_email.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestSourceWiring -v`
Expected: FAIL -- `test_kind_is_passed_to_transcribe_link` gets `["x", "x"]`, and the attachment name is `transcript_post.md`

- [ ] **Step 3: Implement the wiring**

In `_process_message`, replace the transcription loop and attachment build (lines 327-341) with:

```python
    results = []
    for url, kind in pairs:
        r = transcribe_link(url, kind)
        if r["ok"]:
            r["summary"] = summarize_transcript(url, r["transcript"])
            r["title"] = _parse_title(r["summary"], url)
        else:
            r["title"] = url
        r["kind"] = kind
        results.append(r)

    attachments = [
        _attachment(f"transcript_{_status_id(r['url'])}.md",
                    _transcript_md(r["title"], r["url"], r["transcript"],
                                   source=r.get("source") or "xAI Grok STT"))
        for r in results if r["ok"]
    ]
```

and add `kind` to the per-link outcome (line 347):

```python
    return {"from": sender, "subject": subject, "replied": True,
            "links": [{"url": r["url"], "kind": r.get("kind", ""), "ok": r["ok"],
                       "chars": r.get("chars", 0), "error": r.get("error", "")} for r in results]}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 5: Commit**

```bash
git add x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: wire source dispatch into the xte scan; podcast-only mail gets an honest reply"
```

---

### Task 5: Answer a question asked alongside the link

Whatever prose the sender wrote in this message (not quoted history) becomes an optional note. No regex decides whether it is a question -- forwarded-mail boilerplate would fool one. The note goes to Claude and the model judges.

**Files:**
- Modify: `x_transcribe_email.py` (add `extract_note`; extend `summarize_transcript`; extend `_summary_to_html` labels; `_process_message`)
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `eps.html_to_text(content) -> str`, `ld._call_claude_text(prompt, model, max_tokens=2000, tools=None, timeout=None) -> str`
- Produces: `xte.extract_note(body_html: str, urls: list = None) -> str` (max 2000 chars, `""` when there is no prose); `xte.summarize_transcript(url: str, transcript: str, note: str = "") -> str`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_x_transcribe_email.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestExtractNote -v`
Expected: FAIL with `AttributeError: module 'x_transcribe_email' has no attribute 'extract_note'`

- [ ] **Step 3: Implement `extract_note`**

In `x_transcribe_email.py`, add directly after `find_media_links`:

```python
def extract_note(body_html: str, urls: list = None) -> str:
    """The sender's own prose from THIS message (uniqueBody), with links
    removed -- used as an optional question about the video.

    Deliberately does NOT decide whether the prose is a question: forwarded-mail
    boilerplate would fool any heuristic. The model judges, and a note that
    turns out to be a greeting simply produces the normal summary."""
    text = eps.html_to_text(body_html or "")
    for u in (urls or []):
        text = text.replace(u, " ")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:2000]
```

- [ ] **Step 4: Teach the summarizer to answer**

Add above `summarize_transcript` (line 183):

```python
_QUESTION_INSTRUCTIONS = (
    "The requester included this note with the link:\n\"\"\"\n{note}\n\"\"\"\n"
    "If it asks something about the video, BEGIN your reply with a single line:\n"
    "ANSWER: <direct answer, grounded ONLY in the transcript>\n"
    "If the transcript does not cover it, say exactly that on the ANSWER line -- never guess "
    "and never draw on outside knowledge.\n"
    "If the note is not a question (a greeting, a signature, forwarded boilerplate), omit the "
    "ANSWER line entirely and just summarize.\n\n"
)
```

Replace `summarize_transcript` (lines 183-191) with:

```python
def summarize_transcript(url: str, transcript: str, note: str = "") -> str:
    """Claude summary in the fixed TITLE/TL;DR/KEY POINTS shape, optionally
    preceded by an ANSWER line when the sender asked something.
    Returns '' on failure (caller still sends the transcript)."""
    prompt = _SUMMARY_INSTRUCTIONS
    if (note or "").strip():
        prompt += _QUESTION_INSTRUCTIONS.format(note=note.strip())
    prompt += f"Source: {url}\n\nTranscript:\n{transcript[:14000]}"
    try:
        return (ld._call_claude_text(prompt, XTE_SUMMARY_MODEL, max_tokens=1200) or "").strip()
    except Exception as e:
        logger.warning(f"[xte] summarize failed {url[:70]}: {e}")
        return ""
```

In `_summary_to_html`, add the new label so it renders bold (line 212):

```python
    labels = ("ANSWER:", "TL;DR:", "KEY POINTS:", "NOTABLE QUOTES:")
```

- [ ] **Step 5: Pass the note through in `_process_message`**

Directly after the `pairs = pairs[:XTE_MAX_LINKS]` line, add:

```python
    note = extract_note(body_html, [u for u, _ in pairs])
```

and change the summarize call in the loop to:

```python
            r["summary"] = summarize_transcript(url, r["transcript"], note)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 7: Commit**

```bash
git add x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: answer a question asked alongside the link in the same reply"
```

---

### Task 6: Per-conversation transcript cache

Follow-ups must cost one Claude call, not a re-transcription. A conversation where every link failed is deliberately NOT cached -- there would be nothing to answer from.

**Files:**
- Modify: `x_transcribe_email.py` (config block near line 57; new store section; `_process_message`)
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `datetime`, `timezone` (already imported), `timedelta` (new import)
- Produces: `xte.THREADS_PATH: str`; `xte.XTE_THREAD_TTL_DAYS: int`; `xte.XTE_THREAD_MAX: int`; `xte.XTE_THREAD_MAX_QUESTIONS: int`; `xte._load_threads() -> dict`; `xte._save_threads(threads: dict) -> None`; `xte._prune_threads(threads: dict, now: datetime = None) -> dict`; `xte.remember_thread(conversation_id: str, results: list) -> None`. Cache entry shape: `{"created_at": iso, "updated_at": iso, "questions": int, "links": [{"url", "title", "transcript"}]}`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_x_transcribe_email.py` (add `from datetime import datetime, timedelta, timezone` to the imports at the top of the file):

```python
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
```

- [ ] **Step 2: Add the fixture path so tests do not write to /data**

Extend the `xte_files` fixture (line 20) with the new store:

```python
@pytest.fixture
def xte_files(monkeypatch, tmp_path):
    monkeypatch.setattr(xte, "STORE_PATH", str(tmp_path / "store.json"))
    monkeypatch.setattr(xte, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(xte, "THREADS_PATH", str(tmp_path / "threads.json"))
    return tmp_path
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestThreadCache -v`
Expected: FAIL with `AttributeError: module 'x_transcribe_email' has no attribute 'THREADS_PATH'`

- [ ] **Step 4: Add config and the store**

In `x_transcribe_email.py`, change the datetime import (line 37) to:

```python
from datetime import datetime, timedelta, timezone
```

Add to the config block after `STATUS_PATH` (line 53):

```python
# Per-conversation transcript cache -- lets a follow-up question cost one Claude
# call instead of a re-transcription.
THREADS_PATH = os.path.join(_DATA_DIR, "x_transcribe_threads.json")
XTE_THREAD_TTL_DAYS = int(os.environ.get("XTE_THREAD_TTL_DAYS", "30"))
XTE_THREAD_MAX = int(os.environ.get("XTE_THREAD_MAX", "200"))
# Loop breaker, NOT a usage limit: Sara answers link-less mail in threads she
# owns, so an autoresponder on the other end could ping-pong indefinitely.
XTE_THREAD_MAX_QUESTIONS = int(os.environ.get("XTE_THREAD_MAX_QUESTIONS", "20"))
```

Add after `read_status()` (line 111):

```python
def _load_threads() -> dict:
    try:
        with open(THREADS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logger.warning(f"[xte] could not read thread cache ({e}); starting empty")
        return {}
    return data if isinstance(data, dict) else {}


def _save_threads(threads: dict):
    try:
        os.makedirs(os.path.dirname(THREADS_PATH), exist_ok=True)
        with open(THREADS_PATH, "w", encoding="utf-8") as f:
            json.dump(threads, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"[xte] could not write thread cache: {e}")


def _prune_threads(threads: dict, now: datetime = None) -> dict:
    """Drop conversations past the TTL, then keep only the newest
    XTE_THREAD_MAX by updated_at. An unparseable timestamp is dropped."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=XTE_THREAD_TTL_DAYS)
    kept = {}
    for cid, entry in (threads or {}).items():
        try:
            updated = datetime.fromisoformat(str((entry or {}).get("updated_at")).replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            continue
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if updated >= cutoff:
            kept[cid] = entry
    if len(kept) > XTE_THREAD_MAX:
        ordered = sorted(kept.items(), key=lambda kv: str(kv[1].get("updated_at")), reverse=True)
        kept = dict(ordered[:XTE_THREAD_MAX])
    return kept


def remember_thread(conversation_id: str, results: list):
    """Cache successful transcripts so follow-up questions in this conversation
    cost one Claude call. A conversation where every link failed is NOT cached --
    there would be nothing to answer from, and a reply to it correctly falls
    through to the existing skip."""
    ok = [r for r in results if r.get("ok")]
    if not conversation_id or not ok:
        return
    threads = _prune_threads(_load_threads())
    now = datetime.now(timezone.utc).isoformat()
    entry = threads.get(conversation_id) or {"created_at": now, "questions": 0, "links": []}
    entry["updated_at"] = now
    entry["links"] = (entry.get("links") or []) + [
        {"url": r["url"], "title": r.get("title") or r["url"],
         "transcript": r.get("transcript") or ""}
        for r in ok
    ]
    threads[conversation_id] = entry
    _save_threads(_prune_threads(threads))
```

- [ ] **Step 5: Cache after a successful reply**

In `_process_message`, immediately after the `send_threaded_reply(...)` call, add:

```python
    remember_thread(m.get("conversationId") or "", results)
```

Placed AFTER the send so a failed send raises first and nothing is cached for a reply the sender never received.

- [ ] **Step 6: Request conversationId from Graph**

In `run()`, extend the `$select` (line 362) to:

```python
        "$select": "id,subject,from,receivedDateTime,uniqueBody,internetMessageId,conversationId,internetMessageHeaders",
```

(`internetMessageHeaders` is added now so Task 7 does not have to touch this line again.)

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 8: Commit**

```bash
git add x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: cache transcripts per conversation for follow-up questions"
```

---

### Task 7: Follow-up questions in replies

The gate that drops link-less mail gains one branch: if the conversation is one we already transcribed, the sender is eligible, the message is not an autoresponder and the question cap is not hit, treat the prose as a question and answer it from the cache.

**Files:**
- Modify: `x_transcribe_email.py` (config block; new answer section; `run()` gate at lines 379-401)
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Consumes: `extract_note`, `_load_threads`, `_save_threads`, `_prune_threads`, `send_threaded_reply`, `_summary_to_html`, `ld._call_claude_text`, `config.is_internal_email`
- Produces: `xte.XTE_TEAM_EXTRA: list[str]`; `xte.is_auto_reply(m: dict) -> bool`; `xte.answer_question(question: str, links: list) -> str`; `xte.render_answer(question: str, answer: str) -> str`; `xte._process_followup(m: dict, entry: dict) -> dict`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_x_transcribe_email.py`:

```python
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
        self._seed(questions=xte.XTE_THREAD_MAX_QUESTIONS); self._patch_answer(monkeypatch)
        _mock_inbox(monkeypatch, [_followup_msg("f3", "bk@negevlabs.com", "CONV-A", "one more?")])
        calls = _capture_graph(monkeypatch)
        assert xte.run()["replied"] == 0
        assert calls == []   # silence, not a "limit reached" reply that would feed the loop

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestAutoReplyDetection -v`
Expected: FAIL with `AttributeError: module 'x_transcribe_email' has no attribute 'is_auto_reply'`

- [ ] **Step 3: Add the eligibility extension list**

In the config block, after `XTE_SUMMARY_MODEL` (line 58):

```python
# Extra addresses served beyond the internal domains -- for a team member's
# personal address. Empty by default; config.is_internal_email already covers
# every corporate address, so nothing needs to be set for the team or for
# vu@negevcap.com.
XTE_TEAM_EXTRA = [a.strip().lower() for a in os.environ.get("XTE_TEAM_EXTRA", "").split(",") if a.strip()]
```

- [ ] **Step 4: Implement the autoresponder guard and the answer path**

Add after `summarize_transcript`:

```python
_AUTO_REPLY_HEADERS = ("x-autoreply", "x-autorespond", "x-autoresponder")


def is_auto_reply(m: dict) -> bool:
    """True for out-of-office / ticketing autoresponders. Load-bearing: Sara
    now answers link-less mail in threads she owns, so without this an
    autoresponder could ping-pong with her indefinitely."""
    for h in (m.get("internetMessageHeaders") or []):
        name = (h.get("name") or "").strip().lower()
        value = (h.get("value") or "").strip().lower()
        if name == "auto-submitted":
            if value and value != "no":
                return True
        elif name in _AUTO_REPLY_HEADERS:
            return True
        elif name == "precedence" and value in ("auto_reply", "bulk", "junk"):
            return True
    return False


_ANSWER_INSTRUCTIONS = (
    "You are Sara. Answer the colleague's question about a video you already transcribed for "
    "them. Use ONLY the transcript(s) below -- no outside knowledge. Return PLAIN TEXT (no "
    "markdown symbols) starting with a single line:\n"
    "ANSWER: <direct answer in one short paragraph, or a few '- ' bullets if it is genuinely a list>\n"
    "If the transcript does not cover the question, say exactly that and do not guess.\n\n"
)


def answer_question(question: str, links: list) -> str:
    """Claude answer grounded strictly in the cached transcript(s).
    Returns '' on failure (caller says so rather than inventing an answer)."""
    blocks = []
    for l in (links or []):
        blocks.append(f"--- {l.get('title') or l.get('url')} ({l.get('url')}) ---\n"
                      f"{(l.get('transcript') or '')[:14000]}")
    prompt = _ANSWER_INSTRUCTIONS + f"Question: {question}\n\nTranscript(s):\n" + "\n\n".join(blocks)
    try:
        return (ld._call_claude_text(prompt, XTE_SUMMARY_MODEL, max_tokens=1200) or "").strip()
    except Exception as e:
        logger.warning(f"[xte] answer failed: {e}")
        return ""
```

- [ ] **Step 5: Implement the follow-up render and processor**

Add above `render_reply` (it must be defined before both renderers that use it):

```python
_FOOTER_HTML = (
    "<p style='color:#888;font-size:12px;'>-- Sara<br>"
    "I transcribe X and YouTube links. Reply to this email to ask a question about the video. "
    "Spotify podcasts aren't supported yet.</p>"
)
```

Add after `render_reply`:

```python
def render_answer(question: str, answer: str) -> str:
    """HTML body for a follow-up answer: no attachment, they already have the
    transcript."""
    parts = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;line-height:1.5;max-width:720px;">']
    parts.append(f"<p><b>You asked:</b> {html.escape(question)}</p>")
    parts.append(_summary_to_html(answer)
                 or "<p>I couldn't produce an answer just now -- try re-sending your question.</p>")
    parts.append(_FOOTER_HTML)
    parts.append("</div>")
    return "".join(parts)
```

Add after `_process_message`:

```python
def _process_followup(m: dict, entry: dict) -> dict:
    """Answer a link-free reply in a conversation we already transcribed."""
    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
    question = extract_note((m.get("uniqueBody") or {}).get("content", ""))
    answer = answer_question(question, entry.get("links") or [])
    send_threaded_reply(m.get("id"), render_answer(question, answer))
    return {"from": sender, "subject": m.get("subject") or "", "followup": True,
            "replied": True, "question": question[:200]}
```

- [ ] **Step 6: Rewire the `run()` gate**

Load the cache at the top of `run()`, after `processed = set(...)` (line 357):

```python
    threads = _prune_threads(_load_threads())
```

Replace the block from `links = find_media_links(...)` through the external-sender skip (lines 379-385) with:

```python
        links = find_media_links((m.get("uniqueBody") or {}).get("content", ""))
        eligible = (config.is_internal_email(sender)
                    or sender.strip().lower() in XTE_TEAM_EXTRA)

        if not links:
            # A link-free reply in a conversation we already transcribed is a
            # follow-up question. Anything else is not a transcription request
            # and is left for other handlers.
            entry = threads.get(m.get("conversationId") or "") if m.get("conversationId") else None
            if not entry or not eligible or is_auto_reply(m):
                continue
            if int(entry.get("questions") or 0) >= XTE_THREAD_MAX_QUESTIONS:
                logger.info(f"[xte] follow-up cap reached for {m.get('conversationId')}; staying silent")
                continue
            question = extract_note((m.get("uniqueBody") or {}).get("content", ""))
            if not question:
                continue
            if dry_run:
                outcomes.append({"from": sender, "followup": True,
                                 "would_answer": question[:200], "dry_run": True})
                continue
            try:
                outcomes.append(_process_followup(m, entry))
                entry["questions"] = int(entry.get("questions") or 0) + 1
                entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                threads[m["conversationId"]] = entry
                _save_threads(threads)
                processed.add(mid)
                replied += 1
            except Exception as e:
                logger.error(f"[xte] follow-up failed for {sender}: {e}", exc_info=True)
                outcomes.append({"from": sender, "followup": True, "replied": False,
                                 "error": f"{type(e).__name__}: {e}"})
            continue

        if not eligible:
            logger.info(f"[xte] ignoring media-link mail from external sender {sender}")
            processed.add(mid)  # do not reconsider every run
            continue
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_x_transcribe_email.py -v`
Expected: PASS, all tests

- [ ] **Step 8: Commit**

```bash
git add x_transcribe_email.py tests/test_x_transcribe_email.py
git commit -m "feat: answer follow-up questions in-thread from the cached transcript"
```

---

### Task 8: Footer, docs, version bump, deploy

**Files:**
- Modify: `x_transcribe_email.py` (`_FOOTER_HTML`, `render_reply`)
- Modify: `app.py` (version string, 2 places, CRLF-preserving)
- Modify: `CLAUDE.md`, `CACHEBUST`
- Test: `tests/test_x_transcribe_email.py`

**Interfaces:**
- Produces: `xte._FOOTER_HTML: str` -- referenced by `render_answer` from Task 7, so it must be defined ABOVE `render_reply` in the module

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_x_transcribe_email.py::TestFooter -v`
Expected: FAIL -- `assert "Reply to this email to ask" in body`

- [ ] **Step 3: Put the footer on the transcript reply**

`_FOOTER_HTML` was defined in Task 7 (it is referenced by `render_answer`). Only
`render_reply` still needs it.

In `render_reply`, replace the closing line:

```python
    parts.append("<p style='color:#888;'>-- Sara</p></div>")
```

with:

```python
    parts.append(_FOOTER_HTML + "</div>")
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, entire suite green

- [ ] **Step 5: Verify syntax and bump the version**

```bash
python -c "import ast; ast.parse(open('app.py').read()); print('OK')"
grep -n "2\.27\.2-palomar-sender-name" app.py
```

Expected: exactly 2 matches (the `/version` and `/test` handlers). Replace BOTH with `2.28.0-transcribe-qa`, preserving CRLF -- do not let the editor rewrite line endings. Confirm with:

```bash
git diff --stat app.py
```

Expected: a 2-line change, NOT a full-file rewrite. If it shows the whole file changed, the CRLF was lost -- revert and redo.

- [ ] **Step 6: Update CLAUDE.md**

In the **x-transcribe-email Module** section:
- Trigger line: it now acts on x.com/twitter.com **and** youtube.com/youtu.be links; podcast links are detected and reported unsupported.
- Flow line: add captions-first YouTube resolution with STT fallback; add that a note in the body is answered as a question, and that a link-free reply in a transcribed conversation is answered from the cached transcript.
- Safety line: add the autoresponder header skip and the per-conversation question cap (loop breaker, not a usage limit), and the `createReply` threading requirement.
- State line: add `/data/x_transcribe_threads.json`.

In **Environment Variables**, add to the x-transcribe-email line: `XTE_TEAM_EXTRA`, `XTE_THREAD_TTL_DAYS` (30), `XTE_THREAD_MAX` (200), `XTE_THREAD_MAX_QUESTIONS` (20).

In **Common Failure Modes**, add three rows:

| Symptom | Cause | Fix |
|---------|-------|-----|
| Follow-up question gets no reply | The reply landed in a different Exchange conversation, or the first run's transcripts were never cached (every link failed) | Replies must be sent via `createReply` so `conversationId` is inherited; check `/data/x_transcribe_threads.json` for the conversation |
| Spotify link replies "not supported yet" | By design -- podcast audio is DRM'd, yt-dlp cannot fetch it and `_fetch_spoken` has no key and an unvalidated request shape | Expected. Deferred work, not a bug |
| Sara keeps replying to an autoresponder | Autoresponder sends no `Auto-Submitted`/`Precedence` header | The per-conversation cap `XTE_THREAD_MAX_QUESTIONS` stops it after 20 and goes silent; lower it if needed |

- [ ] **Step 7: Commit and deploy**

```bash
ts=$(date +%Y%m%d%H%M%S)
echo -n "$ts" > CACHEBUST
git add x_transcribe_email.py tests/test_x_transcribe_email.py app.py CLAUDE.md CACHEBUST
git commit -m "deploy: 2.28.0-transcribe-qa -- youtube support + questions about the video [$ts]"
git push
```

- [ ] **Step 8: Poll until the exact version is live**

```bash
for i in $(seq 1 12); do sleep 20; curl -s https://meeting-pipeline-production.up.railway.app/version; echo; done
```

Expected: `2.28.0-transcribe-qa`. Stop as soon as it appears. Passing local tests do NOT mean deployed -- confirm via `/version` only, never `/test`.

- [ ] **Step 9: Smoke-test the live path**

```bash
curl -s "https://meeting-pipeline-production.up.railway.app/transcribe-email/run?dry_run=true&sync=1" | python -m json.tool
```

Expected: `status: ok`, a `scanned` count, and no traceback. Then send a real YouTube link from an internal address to `sara@palomar-labs.com` with a question in the body, wait one 15-minute cycle, and confirm the reply contains an ANSWER line, a summary, and a `.md` attachment. Reply to that reply with a second question and confirm it is answered in-thread.

---

## Prerequisites (outside this plan)

- The `vu@negevcap.com` mailbox must exist and be able to send before Vadim can use this. `negevcap.com` is already in `INTERNAL_DOMAINS`, so no code change is needed for him.
- Verify no Graph `ApplicationAccessPolicy` restricts Sara's app registration in a way that blocks `createReply` / `send` on her mailbox.
