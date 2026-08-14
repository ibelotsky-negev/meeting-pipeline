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
- Only INTERNAL senders are served; Sara's own mail is skipped (loop guard).
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
from datetime import datetime, timezone

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

# Inbox scan window (most recent N messages) and per-email link cap (cost guard).
XTE_MAX_MESSAGES = int(os.environ.get("XTE_MAX_MESSAGES", "25"))
XTE_MAX_LINKS = int(os.environ.get("XTE_MAX_LINKS", "5"))
XTE_SUMMARY_MODEL = os.environ.get("XTE_SUMMARY_MODEL", ld.SUMMARY_MODEL)

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


# ======================================================================
#  LINK DETECTION
# ======================================================================


# Kinds this module acts on. An article link is not a transcription request and
# is omitted entirely, so a message carrying only articles falls through to the
# existing skip and is left for other handlers.
_SUPPORTED_KINDS = ("x", "youtube", "podcast")


def find_media_links(body_html: str) -> list:
    """Return de-duped (url, kind) pairs for x / youtube / podcast links found
    in the HTML, in first-seen order. Extracts URLs locally (extract_urls dedupes
    by lowercasing, which loses case-sensitive URL info). Reuses classify_url,
    _youtube_video_id, and _normalize_x_url for media type logic.

    ANY X link is returned (not just /status/), so a link with no video still
    earns an honest "no video found" reply; only the bare domain and navigation
    pages (home/search/settings/...) are ignored.

    YouTube: deduplicate by video ID (case-sensitive), so youtu.be/AbC and
    youtube.com/watch?v=AbC are the same video. If video ID extraction fails,
    fall back to lexical form. Podcast: deduplicate by normalized URL (scheme
    and host only, case-sensitive path/query) so IDs differing only by case
    are kept as distinct entries."""
    # Extract URLs locally to preserve case (extract_urls dedupes by lowercasing)
    import re
    import html as html_module
    _URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
    _HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)

    cleaned = body_html or ""
    found = []
    for href in _HREF_RE.findall(cleaned):
        found.append(href)
    for bare in _URL_RE.findall(cleaned):
        found.append(bare)

    out, seen = [], set()
    for u_raw in found:
        u = html_module.unescape(u_raw).strip()
        if not u or len(u) < 8:
            continue
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
            # Only lowercase scheme and host, preserve case in path/query
            stripped = u.rstrip("/")
            parsed = stripped.split("://", 1)
            if len(parsed) == 2:
                scheme, rest = parsed
                host_path = rest.split("/", 1)
                host = host_path[0].lower()
                path = "/" + host_path[1] if len(host_path) > 1 else ""
                norm = f"{scheme.lower()}://{host}{path}"
            else:
                norm = stripped.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append((u, kind))
    return out


# ======================================================================
#  TRANSCRIBE + SUMMARIZE (reuse learn_digest)
# ======================================================================


def transcribe_link(url: str) -> dict:
    """Transcribe one X post's video audio. Returns a result dict; never raises.
    ok=False carries a specific, honest error reason (no fabrication)."""
    result = {"url": url, "ok": False, "error": "", "transcript": "", "chars": 0}
    tmpdir = None
    try:
        audio_path, _duration, err, tmpdir = ld.extract_x_post_audio(url)
        if err or not audio_path:
            result["error"] = err or "no audio could be extracted from this post"
            return result
        text, stt_err = ld._grok_stt_from_file(audio_path)
        if stt_err or not text:
            result["error"] = stt_err or "speech-to-text returned an empty transcript"
            return result
        result["ok"] = True
        result["transcript"] = text
        result["chars"] = len(text)
        return result
    except Exception as e:
        logger.warning(f"[xte] transcribe failed {url[:70]}: {e}")
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if tmpdir and os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)


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


def summarize_transcript(url: str, transcript: str) -> str:
    """Claude summary of the transcript in the fixed TITLE/TL;DR/KEY POINTS shape.
    Returns '' on failure (caller still sends the transcript)."""
    prompt = _SUMMARY_INSTRUCTIONS + f"Source: {url}\n\nTranscript:\n{transcript[:14000]}"
    try:
        return (ld._call_claude_text(prompt, XTE_SUMMARY_MODEL, max_tokens=1200) or "").strip()
    except Exception as e:
        logger.warning(f"[xte] summarize failed {url[:70]}: {e}")
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
    labels = ("TL;DR:", "KEY POINTS:", "NOTABLE QUOTES:")
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
    m = _STATUS_LINK_RE.search(url or "")
    return m.group(1) if m else "post"


def _transcript_md(title: str, url: str, transcript: str) -> str:
    return (
        f"# Transcript -- {title}\n\n"
        f"- Source: {url}\n"
        f"- Transcribed by: Sara (xAI Grok STT)\n\n"
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
    if any(k in low for k in ("no video", "no audio", "no downloadable", "no media", "no transcribable")):
        return "No video was found at this link, so there was nothing to transcribe."
    if any(k in low for k in ("timeout", "guest token", "429", "502", "503", "504", "temporar")):
        return "Could not fetch the video just now (temporary issue) -- try re-sending in a moment."
    return "Could not transcribe this link: " + (error or "unknown reason")


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

    results = []
    for url, _kind in pairs:
        r = transcribe_link(url)
        if r["ok"]:
            r["summary"] = summarize_transcript(url, r["transcript"])
            r["title"] = _parse_title(r["summary"], url)
        else:
            r["title"] = url
        results.append(r)

    attachments = [
        _attachment(f"transcript_{_status_id(r['url'])}.md",
                    _transcript_md(r["title"], r["url"], r["transcript"]))
        for r in results if r["ok"]
    ]
    send_threaded_reply(m.get("id"), render_reply(results, truncated), attachments)

    return {"from": sender, "subject": subject, "replied": True,
            "links": [{"url": r["url"], "ok": r["ok"], "chars": r.get("chars", 0),
                       "error": r.get("error", "")} for r in results]}


def run(dry_run: bool = False, limit: int = None) -> dict:
    """Scan Sara's inbox for internal mail carrying X links and reply with the
    transcript(s) + summary. Idempotent (processed message ids). dry_run lists
    what would be transcribed without extracting, calling STT, or replying."""
    started = datetime.now(timezone.utc)
    store = _load()
    processed = set(store.get("processed_ids") or [])
    limit = limit or XTE_MAX_MESSAGES

    url = f"{eps.MS_GRAPH_BASE}/users/{SARA_MAILBOX}/mailFolders/inbox/messages"
    params = {
        "$select": "id,subject,from,receivedDateTime,uniqueBody,internetMessageId",
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
        if not links:
            continue  # not a transcription request -- leave for other handlers
        if not config.is_internal_email(sender):
            logger.info(f"[xte] ignoring X-link mail from external sender {sender}")
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
