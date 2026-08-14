#!/usr/bin/env python3
"""
x-transcribe-email -- email Sara an x.com link, get the transcript + summary back.

Any INTERNAL team member emails sara@negevlabs.com an x.com / twitter.com post
link. Sara scans her inbox (scheduled, and via /transcribe-email/run), transcribes
each X video, summarizes it, and REPLIES to the sender with a structured summary
in the body and the full transcript attached as a .md file (one per link).

Reuses existing, deployed machinery -- single source of truth, no duplication:
- email_pipeline_sync (eps): Graph app-only token, graph_get / graph_post, html_to_text.
- learn_digest (ld): extract_urls, classify_url, extract_x_post_audio,
  _grok_stt_from_file, _call_claude_text, SUMMARY_MODEL (the 2.22.0 STT path).
- config.is_internal_email: the team allow-list.

Safety / robustness:
- Only INTERNAL senders (plus XTE_TEAM_EXTRA) are served; Sara's own mail is
  skipped (loop guard).
- Links are read from uniqueBody ONLY -- a link quoted in a reply's thread history
  does NOT re-trigger; only links the sender typed in THIS message count.
- Idempotent via processed message-ids at /data/x_transcribe_email.json.
- Per-email link cap (cost guard); the 60-min duration + size caps live inside
  learn_digest.extract_x_post_audio.
- Never fabricates: a link that cannot be transcribed is reported honestly in the
  reply with the specific reason.

Author: Negev Labs
"""

import os
import re
import json
import html
import base64
import shutil
import logging
import threading
from datetime import datetime, timedelta, timezone

import email_pipeline_sync as eps
import learn_digest as ld
import config

logger = logging.getLogger("x-transcribe-email")

# ======================================================================
#  CONFIG
# ======================================================================

SARA_MAILBOX = os.environ.get("BOT_SENDER_EMAIL", "sara@palomar-labs.com")

_DATA_DIR = "/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__))
STORE_PATH = os.path.join(_DATA_DIR, "x_transcribe_email.json")
STATUS_PATH = os.path.join(_DATA_DIR, "x_transcribe_email_status.json")

# Per-conversation transcript cache -- lets a follow-up question cost one Claude
# call instead of a re-transcription.
THREADS_PATH = os.path.join(_DATA_DIR, "x_transcribe_threads.json")
XTE_THREAD_TTL_DAYS = int(os.environ.get("XTE_THREAD_TTL_DAYS", "30"))
XTE_THREAD_MAX = int(os.environ.get("XTE_THREAD_MAX", "200"))
# Loop breaker, NOT a usage limit: Sara answers link-less mail in threads she
# owns, so an autoresponder on the other end could ping-pong indefinitely.
XTE_THREAD_MAX_QUESTIONS = int(os.environ.get("XTE_THREAD_MAX_QUESTIONS", "20"))

# Inbox scan window (most recent N messages) and per-email link cap (cost guard).
XTE_MAX_MESSAGES = int(os.environ.get("XTE_MAX_MESSAGES", "25"))
XTE_MAX_LINKS = int(os.environ.get("XTE_MAX_LINKS", "5"))
XTE_SUMMARY_MODEL = os.environ.get("XTE_SUMMARY_MODEL", ld.SUMMARY_MODEL)

# Extra addresses served beyond the internal domains -- for a team member's
# personal address. Empty by default; config.is_internal_email already covers
# every corporate address, so nothing needs to be set for the team or for
# vu@negevcap.com.
XTE_TEAM_EXTRA = [a.strip().lower() for a in os.environ.get("XTE_TEAM_EXTRA", "").split(",") if a.strip()]

# In-process guard so the scheduled scan and a manual trigger never overlap.
_run_lock = threading.Lock()

_STATUS_LINK_RE = re.compile(r"/status/(\d+)")
# Bare domain + navigation pages that are not a shareable post -> ignored.
_X_NONPOST = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/?"
    r"(?:home|explore|notifications|messages|settings|search|compose|i/grok)?/?$", re.I)


# ======================================================================
#  STORE
# ======================================================================


def _load() -> dict:
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"processed_ids": []}
    except Exception as e:
        logger.warning(f"[xte] could not read store ({e}); starting empty")
        return {"processed_ids": []}
    data.setdefault("processed_ids", [])
    return data


def _save(data: dict):
    try:
        os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
        with open(STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"[xte] could not write store: {e}")


def _write_status(result: dict):
    try:
        os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"[xte] could not write status: {e}")


def read_status() -> dict:
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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


# ======================================================================
#  LINK DETECTION
# ======================================================================


# Kinds this module acts on. An article link is not a transcription request and
# is omitted entirely, so a message carrying only articles falls through to the
# existing skip and is left for other handlers.
_SUPPORTED_KINDS = ("x", "youtube", "podcast")


def find_media_links(body_html: str) -> list:
    """Return de-duped (url, kind) pairs for x / youtube / podcast links found
    in the HTML, in first-seen order. Reuses learn_digest.extract_urls (handles
    both hrefs and plain text, strips trailing punctuation, removes boilerplate)
    and classify_url for link detection. Deduplication is per-kind: YouTube by
    video ID, Podcast by scheme+host only (preserves path/query case), X via
    _normalize_x_url. Note: extract_urls deduplicates case-insensitively
    (learn_digest.py:554), so case-variant URLs collapse upstream.

    ANY X link is returned (not just /status/), so a link with no video still
    earns an honest "no video found" reply; only the bare domain and navigation
    pages (home/search/settings/...) are ignored.

    YouTube: deduplicate by video ID, so youtu.be/ID and youtube.com/watch?v=ID
    are the same video. If video ID extraction fails, fall back to lexical form.
    Podcast: deduplicate by scheme+host only so path/query are not compared."""
    from urllib.parse import urlsplit

    out, seen = [], set()
    for u in ld.extract_urls(body_html or ""):
        kind = ld.classify_url(u)
        if kind not in _SUPPORTED_KINDS:
            continue
        if kind == "x":
            if _X_NONPOST.match(u):
                continue
            norm = ld._normalize_x_url(u)
        elif kind == "youtube":
            try:
                vid = ld._youtube_video_id(u)
                if vid:
                    norm = f"youtube:{vid}"
                else:
                    norm = (u or "").rstrip("/").lower()
            except Exception:
                norm = (u or "").rstrip("/").lower()
        else:  # podcast
            # Deduplicate by scheme + netloc only, preserving case in path/query/fragment
            parts = urlsplit(u)
            norm = f"{parts.scheme.lower()}://{parts.netloc.lower()}/{parts.path}{parts.query}{'#' + parts.fragment if parts.fragment else ''}".rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        out.append((u, kind))
    return out


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


# ======================================================================
#  TRANSCRIBE + SUMMARIZE (reuse learn_digest)
# ======================================================================


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


_SUMMARY_INSTRUCTIONS = (
    "You are Sara. Summarize the transcript of a video from an X (Twitter) post for the "
    "colleague who asked. Return PLAIN TEXT (no markdown symbols) in EXACTLY this shape:\n"
    "TITLE: <=10-word title\n"
    "TL;DR: one or two sentences with the real takeaway\n"
    "KEY POINTS:\n- point (3 to 7 bullets, concrete: names, numbers, claims)\n"
    "NOTABLE QUOTES:\n- \"verbatim line\" (include this section ONLY if a quote is genuinely worth keeping)\n\n"
    "Be faithful to what was actually said; pull real specifics; never invent. The transcript "
    "may be machine-generated and misrender proper nouns -- silently correct the obvious ones.\n\n"
)


_QUESTION_INSTRUCTIONS = (
    "The requester included this note with the link:\n\"\"\"\n{note}\n\"\"\"\n"
    "If it asks something about the video, BEGIN your reply with a single line:\n"
    "ANSWER: <direct answer, grounded ONLY in the transcript>\n"
    "If the transcript does not cover it, say exactly that on the ANSWER line -- never guess "
    "and never draw on outside knowledge.\n"
    "If the note is not a question (a greeting, a signature, forwarded boilerplate), omit the "
    "ANSWER line entirely and just summarize.\n\n"
)


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


NO_QUESTION_MARKER = "NO_QUESTION"


def _is_no_question(answer: str) -> bool:
    """True when the model judged the sender's note was not a question about the
    video. Matches only an exact first-line marker so a real answer that merely
    mentions the words is never suppressed."""
    first = (answer or "").strip().splitlines()[0] if (answer or "").strip() else ""
    return first.strip().rstrip(".!:;,").upper() == NO_QUESTION_MARKER


_ANSWER_INSTRUCTIONS = (
    "You are Sara. Answer the colleague's question about a video you already transcribed for "
    "them. Use ONLY the transcript(s) below -- no outside knowledge. Return PLAIN TEXT (no "
    "markdown symbols) starting with a single line:\n"
    "ANSWER: <direct answer in one short paragraph, or a few '- ' bullets if it is genuinely a list>\n"
    "If the transcript does not cover the question, say exactly that and do not guess.\n"
    "If the note is NOT a question about the video -- a thank-you, an acknowledgement, a "
    "signature, or forwarded boilerplate -- reply with exactly NO_QUESTION on the first "
    "line and nothing else.\n\n"
)


def answer_question(question: str, links: list) -> str:
    """Claude answer grounded strictly in the cached transcript(s).
    Returns '' on failure (caller says so rather than inventing an answer).
    Returns exactly NO_QUESTION_MARKER (see _is_no_question) when the model
    judges the sender's note was not actually a question about the video."""
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


def _parse_title(summary: str, fallback: str) -> str:
    for line in (summary or "").splitlines():
        if line.strip().upper().startswith("TITLE:"):
            t = line.split(":", 1)[1].strip()
            if t:
                return t
    return fallback


# ======================================================================
#  RENDER + SEND
# ======================================================================


def _summary_to_html(summary: str) -> str:
    """Escape + lightly format the labeled summary text into readable HTML
    (bold section labels, real bullet lists). The TITLE line is dropped -- it is
    surfaced as the section heading instead."""
    labels = ("ANSWER:", "TL;DR:", "KEY POINTS:", "NOTABLE QUOTES:")
    parts, bullets = [], []

    def flush():
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for raw in (summary or "").splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("TITLE:"):
            continue
        esc = html.escape(line)
        if line.startswith("- ") or line.startswith("* "):
            bullets.append(html.escape(line[2:].strip()))
            continue
        flush()
        upper = line.upper()
        if any(upper.startswith(lbl) for lbl in labels):
            parts.append(f"<p><b>{esc}</b></p>")
        else:
            parts.append(f"<p>{esc}</p>")
    flush()
    return "".join(parts)


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


def _transcript_md(title: str, url: str, transcript: str, source: str = "xAI Grok STT") -> str:
    return (
        f"# Transcript -- {title}\n\n"
        f"- Source: {url}\n"
        f"- Transcribed by: Sara ({source})\n\n"
        f"---\n\n{transcript}\n"
    )


def _attachment(name: str, text: str) -> dict:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": name,
        "contentType": "text/markdown",
        "contentBytes": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def _failure_message(error: str) -> str:
    """Turn a per-link failure reason into a clear sentence for the reply."""
    low = (error or "").lower()
    if "not supported yet" in low:
        return error or PODCAST_UNSUPPORTED
    if any(k in low for k in ("no video", "no audio", "no downloadable", "no media", "no transcribable")):
        return "No video was found at this link, so there was nothing to transcribe."
    if any(k in low for k in ("timeout", "guest token", "429", "502", "503", "504", "temporar")):
        return "Could not fetch the video just now (temporary issue) -- try re-sending in a moment."
    return "Could not transcribe this link: " + (error or "unknown reason")


_FOOTER_HTML = (
    "<p style='color:#888;font-size:12px;'>-- Sara<br>"
    "I transcribe X and YouTube links. Reply to this email to ask a question about the video. "
    "Spotify podcasts aren't supported yet.</p>"
)


def render_reply(results: list, truncated: int = 0) -> str:
    """HTML body for the reply: one section per link (summary or honest failure)."""
    parts = ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;line-height:1.5;max-width:720px;">']
    n_ok = sum(1 for r in results if r["ok"])
    if n_ok == 0:
        parts.append("<p>I couldn't get a transcript from your "
                     f"{'link' if len(results) == 1 else 'links'}:</p>")
    else:
        parts.append(f"<p>Here {'is' if len(results) == 1 else 'are'} the transcript"
                     f"{'' if len(results) == 1 else 's'} you asked for "
                     f"({n_ok}/{len(results)} transcribed). Full text is attached as .md.</p>")
    for r in results:
        title = html.escape(r.get("title") or r["url"])
        url = html.escape(r["url"])
        parts.append(f'<h3 style="margin-bottom:2px;">{title}</h3>')
        parts.append(f'<p style="margin-top:0;"><a href="{url}">{url}</a></p>')
        if r["ok"]:
            body = _summary_to_html(r.get("summary") or "")
            parts.append(body or "<p>(summary unavailable; see attached transcript)</p>")
        else:
            parts.append(f'<p style="color:#b45309;"><b>{html.escape(_failure_message(r.get("error")))}</b></p>')
        parts.append('<hr style="border:none;border-top:1px solid #eee;margin:14px 0;">')
    if truncated:
        parts.append(f"<p><em>{truncated} additional link(s) in your email were not processed "
                     f"(max {XTE_MAX_LINKS} per email).</em></p>")
    parts.append("<p style='color:#888;'>-- Sara</p></div>")
    return "".join(parts)


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


def send_threaded_reply(source_message_id: str, html_body: str, attachments: list = None):
    """Reply in-thread via createReply so the reply inherits conversationId,
    subject and recipient. sendMail would thread only by subject heuristics,
    which breaks the conversationId match that follow-up questions rely on.

    Sequence: createReply (draft) -> PATCH the body -> POST each attachment ->
    send. Raises on a missing draft id so the caller does NOT mark the message
    processed and the next run retries it.

    If PATCH/attach/send fails after the draft was created, the draft would
    otherwise be orphaned (never sent, never deleted) -- and since a retry
    calls createReply again, a persistent failure would leave a fresh
    abandoned draft in Sara's mailbox on every scan cycle. So on any failure
    past this point, best-effort DELETE the draft we just created, then
    re-raise the ORIGINAL error unchanged (the cleanup call's own failure is
    swallowed -- it must never mask the real error or change retry
    semantics)."""
    base = f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/messages"
    draft = eps.graph_post(f"{base}/{source_message_id}/createReply", {}) or {}
    draft_id = draft.get("id")
    if not draft_id:
        raise RuntimeError("createReply returned no draft id")
    try:
        eps.graph_patch(f"{base}/{draft_id}",
                        {"body": {"contentType": "HTML", "content": html_body}})
        for att in (attachments or []):
            eps.graph_post(f"{base}/{draft_id}/attachments", att)
        eps.graph_post(f"{base}/{draft_id}/send", {})
    except Exception:
        try:
            eps.graph_delete(f"{base}/{draft_id}")
        except Exception as cleanup_err:
            logger.warning(f"[xte] could not delete orphaned draft {draft_id}: {cleanup_err}")
        raise


# ======================================================================
#  MAIN SCAN
# ======================================================================


def _process_message(m: dict) -> dict:
    """Transcribe every X link in one message and reply to its sender.
    Returns an outcome dict; assumes the caller already gated sender + links."""
    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
    subject = m.get("subject") or ""
    body_html = (m.get("uniqueBody") or {}).get("content", "")
    pairs = find_media_links(body_html)
    truncated = max(0, len(pairs) - XTE_MAX_LINKS)
    pairs = pairs[:XTE_MAX_LINKS]
    note = extract_note(body_html, [u for u, _ in pairs])

    results = []
    for url, kind in pairs:
        r = transcribe_link(url, kind)
        if r["ok"]:
            r["summary"] = summarize_transcript(url, r["transcript"], note)
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
    send_threaded_reply(m.get("id"), render_reply(results, truncated), attachments)
    remember_thread(m.get("conversationId") or "", results)

    return {"from": sender, "subject": subject, "replied": True,
            "links": [{"url": r["url"], "kind": r.get("kind", ""), "ok": r["ok"],
                       "chars": r.get("chars", 0), "error": r.get("error", "")} for r in results]}


def _process_followup(m: dict, entry: dict) -> dict:
    """Answer a link-free reply in a conversation we already transcribed.

    Sends nothing when the model judges the note was not actually a question
    (see _is_no_question) -- e.g. "thanks" or a signature. A Claude FAILURE
    (answer_question returning '') is NOT a non-question: it still gets the
    existing honest-failure reply via render_answer."""
    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
    question = extract_note((m.get("uniqueBody") or {}).get("content", ""))
    answer = answer_question(question, entry.get("links") or [])
    if _is_no_question(answer):
        logger.info(f"[xte] follow-up from {sender} was not a question; not replying")
        return {"from": sender, "subject": m.get("subject") or "", "followup": True,
                "replied": False, "answered": False, "reason": "not a question",
                "question": question[:200]}
    send_threaded_reply(m.get("id"), render_answer(question, answer))
    return {"from": sender, "subject": m.get("subject") or "", "followup": True,
            "replied": True, "answered": True, "question": question[:200]}


def run(dry_run: bool = False, limit: int = None) -> dict:
    """Scan Sara's inbox for internal mail carrying X links and reply with the
    transcript(s) + summary. Idempotent (processed message ids). dry_run lists
    what would be transcribed without extracting, calling STT, or replying."""
    started = datetime.now(timezone.utc)
    store = _load()
    processed = set(store.get("processed_ids") or [])
    threads = _prune_threads(_load_threads())
    limit = limit or XTE_MAX_MESSAGES

    url = f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/mailFolders/inbox/messages"
    params = {
        "$select": "id,subject,from,receivedDateTime,uniqueBody,internetMessageId,conversationId,internetMessageHeaders",
        "$top": str(limit),
        "$orderby": "receivedDateTime desc",
    }
    resp = eps.graph_get(url, params=params)
    messages = resp.get("value") or []

    outcomes, replied = [], 0
    for m in messages:
        mid = m.get("internetMessageId") or m.get("id") or ""
        if not mid or mid in processed:
            continue
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        # Loop guard: never act on Sara's own outbound.
        if sender.strip().lower() == SARA_MAILBOX.strip().lower():
            continue
        # Links are read from uniqueBody so a quoted link in a reply never re-fires.
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
                outcome = _process_followup(m, entry)
                outcomes.append(outcome)
                # Re-read before writing: a link message processed earlier in
                # THIS scan may have cached a conversation via remember_thread,
                # which writes straight to disk -- saving the top-of-run snapshot
                # back wholesale would drop it.
                threads = _prune_threads(_load_threads())
                entry = threads.get(m["conversationId"]) or entry
                entry["questions"] = int(entry.get("questions") or 0) + 1
                entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                threads[m["conversationId"]] = entry
                _save_threads(threads)
                processed.add(mid)
                if outcome.get("replied"):
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

        if dry_run:
            outcomes.append({"from": sender, "subject": m.get("subject") or "",
                             "would_transcribe": [u for u, _ in links[:XTE_MAX_LINKS]], "dry_run": True})
            continue

        try:
            outcomes.append(_process_message(m))
            processed.add(mid)
            replied += 1
        except Exception as e:
            # Reply/transcribe failed at the message level -> do NOT mark processed
            # so the next run retries it.
            logger.error(f"[xte] processing failed for {sender}: {e}", exc_info=True)
            outcomes.append({"from": sender, "subject": m.get("subject") or "",
                             "replied": False, "error": f"{type(e).__name__}: {e}"})

    if not dry_run:
        store["processed_ids"] = sorted(processed)[-1000:]
        _save(store)

    result = {"status": "ok", "dry_run": dry_run, "scanned": len(messages),
              "replied": replied, "outcomes": outcomes,
              "finished_at": datetime.now(timezone.utc).isoformat(),
              "started_at": started.isoformat()}
    _write_status(result)
    logger.info(f"[xte] scan done: scanned={len(messages)} replied={replied} dry_run={dry_run}")
    return result


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Email Sara an x.com link -> transcript + summary reply")
    ap.add_argument("--dry-run", action="store_true", help="list would-transcribe links; no STT, no reply")
    ap.add_argument("--limit", type=int, default=None, help="inbox scan window (most recent N messages)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print(json.dumps(run(dry_run=args.dry_run, limit=args.limit), indent=2, default=str))


if __name__ == "__main__":
    main()
