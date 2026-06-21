#!/usr/bin/env python3
"""
learn-digest -- Sara module (Read/Learn Digest, Phase 1)

Weekly drains Ken's Outlook "read/learn" folder, resolves each saved link
(X posts, articles, YouTube, podcasts, X video), then CLUSTERS the items by
topic and CURATES each cluster down to the single most useful, most current
item for Ken's needs -- dropping redundant and superseded saves. Emits one
Friday digest grouped by topic cluster plus Asana tasks for the keepers.

The core of the module is the cluster-and-curate pass, not a flat per-item
digest. Phase 2 (X bookmarks) is out of scope here.

Reuses Sara's existing Microsoft Graph auth, /data volume, Claude pipeline,
atomic run-lock pattern (mirrors the Weekly Pulse lock), and the existing
send_email path. It is NOT a new service.

Usage:
    python learn_digest.py                 # process unread, send digest
    python learn_digest.py --backlog       # force full-backlog first run
    python learn_digest.py --dry-run       # resolve+cluster+curate, send nothing,
                                            # create no tasks, mark/move nothing

Spec: read-learn-digest-spec.md (v0.2).
ASCII-only comments and non-user-facing strings (PowerShell corrupts Unicode).
Author: Negev Labs
"""

import os
import re
import json
import html
import time
import uuid
import logging
import argparse
import requests
from datetime import datetime, timezone, timedelta

# Shared Graph helpers (app-only token, retrying GET/POST, html_to_text).
import email_pipeline_sync as eps
# Generic Asana request helper (project + section routing done here).
import asana_client

logger = logging.getLogger("learn-digest")

# ======================================================================
#  CONFIG
# ======================================================================

MAILBOX = "bk@negevlabs.com"

# The "/" in "read/learn" defeats Graph display-name lookup -- address by the
# cached folder ID (seeded with the known ID; never look up by name).
LEARN_FOLDER_ID = (
    "AAMkAGY0Nzc0N2Q0LWU2NWYtNDFlMi05MmM3LWI5ZWIwODY5ZDA4YwAuAAAAAAD2HZAEgE0d"
    "Q6DKSpP8o42sAQBxq2Btx8bBQbRoRXyUmqLCAAlms0PtAAA="
)
PROCESSED_SUBFOLDER_NAME = "Processed"

# Asana "Read/Learn Triage" project and its 6 bucket sections. The 6 buckets
# the curator tags map 1:1 to these section names.
ASANA_PROJECT_GID_LEARN = "1215897524719950"
ASANA_SECTIONS = {
    "Negev Labs": "1215897524642810",
    "Zirmania Family Office": "1215886226827868",
    "Sara Pipeline": "1215899505871771",
    "Travel Relay": "1215886226830719",
    "Ariadne Website": "1215898379087843",
    "General/Reference": "1215899505871835",
}
BUCKETS = list(ASANA_SECTIONS.keys())
DEFAULT_BUCKET = "General/Reference"

# Models: summaries on the extraction tier (Sonnet), cluster/curate/currency on
# the judgement tier (Opus) -- same tiers as the Weekly Pulse.
SUMMARY_MODEL = os.environ.get("LEARN_SUMMARY_MODEL", "claude-sonnet-4-6")
CURATE_MODEL = os.environ.get("LEARN_CURATE_MODEL", "claude-opus-4-8")

# Output-token budgets per stage. The CLUSTER pass emits one JSON object covering
# the WHOLE batch (every item's index at once), so it needs a large budget: too
# small a cap truncates the JSON, parsing fails, and the batch wrongly collapses
# to singletons (no consolidation -- the 2.18.2 dry-run bug). Kept under the ~16k
# non-streaming ceiling. CURATE is per-cluster, so a smaller budget suffices.
CLUSTER_MAX_TOKENS = int(os.environ.get("LEARN_CLUSTER_MAX_TOKENS", "12000"))
CURATE_MAX_TOKENS = int(os.environ.get("LEARN_CURATE_MAX_TOKENS", "4000"))

# fast-moving (default) | off | all -- which clusters get the live web check.
LEARN_CURRENCY_CHECK = os.environ.get("LEARN_CURRENCY_CHECK", "fast-moving")

# Optional keys (XAI_API_KEY, SPOKEN_API_KEY, JINA_API_KEY) are read at CALL TIME
# inside each resolver via os.environ.get -- never at module top level, never via
# os.environ[...] indexing. An absent key logs a warning and degrades that item to
# "content not retrieved"; it never raises and never fabricates. Articles (Jina)
# and YouTube (youtube-transcript-api) resolve with no key. This keeps local runs
# (no Railway env) and tests safe, and lets Railway-set keys take effect without a
# re-import.

LEARN_RECIPIENTS = [
    r.strip() for r in os.environ.get("LEARN_RECIPIENTS", "bk@negevlabs.com").split(",") if r.strip()
]

# Fixed UTC offset only for the subject-line date (codebase convention; the
# scheduled cron itself is tz-aware via Asia/Jerusalem). 3 = IDT (summer).
ISRAEL_UTC_OFFSET_HOURS = int(os.environ.get("ISRAEL_UTC_OFFSET_HOURS", "3"))

# Topics whose keepers get the live currency web-check. AI-tooling changes
# monthly; biotech/investing/health age slowly.
FAST_MOVING_KEYWORDS = [
    "claude code", "claude", "anthropic", "cowork", "mcp", "model context protocol",
    "skill", "agent", "agentic", "llm", "gpt", "openai", "sonnet", "opus", "haiku",
    "gemini", "cursor", "copilot", "model", "prompt", "rag", "fine-tun", "langchain",
    "managed agent", "subagent", "tool use", "ai tool", "ai-tool", "vector",
]

# Ken's-needs profile -- the curation judge scores "best for Ken", not "most
# popular". Kept in the prompt (not hardcoded magic).
KEN_PROFILE = """Ken Belotsky's working context (judge "best for KEN", not "most popular"):
- Active builds: the Sara meeting-pipeline, Travel Relay (TAS), and the Ariadne Bio website;
  plus Negev Labs (a biotech venture studio) and Zirmania Family Office.
- Stack in daily use: Claude Code (Sonnet for dev, Opus for hard problems), Railway/Flask,
  Anthropic Managed Agents, MCP connectors, Google Apps Script, HubSpot, Asana,
  Microsoft Graph/Teams, Telegram bots. Disciplines: SVL (self-verifying loop),
  one-chat-one-deployable-unit, spec-in-claude.ai then implement-in-Claude-Code.
- "Best" = most applicable to how Ken actually works AND most current -- not most popular.
- Currency matters most for AI-tooling (Claude Code, Cowork, MCP, skills, agent frameworks,
  models): these change monthly. Biotech, investing, and health age slowly and are judged on
  intra-cluster recency plus content only.
Anti-inflation rules: do NOT invent a project connection; do NOT promote "interesting" to
"action required"; an item whose content was not retrieved is judged from title/sender only
and never fabricated; if a cluster has no clearly useful item, say so rather than manufacture
a keeper. The email subject line is a PRIMARY relevance signal (Ken's own topic tag)."""

# ----------------------------------------------------------------------
#  State files on /data (mirrors the Pulse lock scale/pattern). Resolved
#  from DATA_DIR when set (tests), else /data on Railway, else the repo dir.
# ----------------------------------------------------------------------
_LEARN_DATA_DIR = (
    os.environ.get("DATA_DIR")
    or ("/data" if os.path.isdir("/data") else os.path.dirname(os.path.abspath(__file__)))
)
LEARN_PROCESSED_FILE = os.path.join(_LEARN_DATA_DIR, "learn_processed.json")
LEARN_LOCK_FILE = os.path.join(_LEARN_DATA_DIR, "learn_lock.json")
LEARN_STATUS_FILE = os.path.join(_LEARN_DATA_DIR, "learn_status.json")
# A backlog run resolves dozens of links and makes many API calls; allow a
# generous window before a held lock is treated as orphaned.
LEARN_LOCK_MAX_AGE = int(os.environ.get("LEARN_LOCK_MAX_AGE", str(2 * 3600)))

# In-process guard for manual trigger + scheduler in the same process.
import threading as _threading  # noqa: E402
_learn_lock = _threading.Lock()


# ======================================================================
#  ATOMIC RUN LOCK (mirrors the Weekly Pulse running lock)
# ======================================================================


def _acquire_run_lock() -> bool:
    """Atomically claim the cross-process run lock with O_CREAT|O_EXCL so only
    one run proceeds even if two workers fire at the same instant. Stale locks
    (older than LEARN_LOCK_MAX_AGE) are reclaimed automatically.

    Returns True if acquired, False if a run is already in progress.
    """
    try:
        existing_age = time.time() - os.path.getmtime(LEARN_LOCK_FILE)
        if existing_age > LEARN_LOCK_MAX_AGE:
            logger.warning(f"[learn] Removing stale run lock (age {existing_age/60:.0f}min)")
            try:
                os.remove(LEARN_LOCK_FILE)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        pass

    try:
        fd = os.open(LEARN_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        payload = json.dumps({
            "pid": os.getpid(),
            "started_at": time.time(),
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        })
        os.write(fd, payload.encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_run_lock():
    try:
        os.remove(LEARN_LOCK_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"[learn] Failed to release run lock: {e}")


# ======================================================================
#  PROCESSED-ID STORE (belt-and-suspenders dedup, independent of mark-read)
# ======================================================================


def _load_processed_ids() -> set:
    try:
        with open(LEARN_PROCESSED_FILE) as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else data.get("ids", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_processed_ids(ids):
    try:
        with open(LEARN_PROCESSED_FILE, "w") as f:
            json.dump(sorted(ids), f)
    except Exception as e:
        logger.error(f"[learn] Failed to write processed-id store: {e}")


# ======================================================================
#  URL EXTRACTION + CLASSIFICATION
# ======================================================================

# Hosts that are boilerplate, not saved content (Outlook signature, trackers).
_BOILERPLATE_HOSTS = (
    "aka.ms", "go.microsoft.com", "outlook.com", "outlook.office.com",
    "office.com", "microsoft.com", "w3.org", "schemas.microsoft.com",
)
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.I)
_SIGNATURE_RE = re.compile(r'<div[^>]*id\s*=\s*["\']?ms-outlook-mobile-signature', re.I)
_GET_OUTLOOK_RE = re.compile(r"get\s+outlook\s+for\s+(ios|android)", re.I)


def _strip_signature(body_html: str) -> str:
    """Drop the Outlook mobile signature block (and everything after it -- it is
    always trailing boilerplate) plus 'Get Outlook for iOS/Android' lines."""
    if not body_html:
        return ""
    m = _SIGNATURE_RE.search(body_html)
    if m:
        body_html = body_html[: m.start()]
    # Belt-and-suspenders: also drop a bare "Get Outlook for ..." tail.
    g = _GET_OUTLOOK_RE.search(body_html)
    if g:
        body_html = body_html[: g.start()]
    return body_html


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    # Strip trailing punctuation that commonly clings to bare URLs in text.
    url = url.rstrip('.,;:)>"\']')
    return url


def _is_boilerplate(url: str) -> bool:
    low = url.lower()
    return any(("//" + h in low) or ("." + h + "/" in low) or low.endswith("." + h) for h in _BOILERPLATE_HOSTS)


def extract_urls(body_html: str) -> list:
    """Strip the Outlook mobile signature + boilerplate, then return deduped
    links from both anchors (href=) and bare text URLs, in first-seen order.

    Null-safe: empty/None body returns []."""
    cleaned = _strip_signature(body_html or "")
    found = []
    for href in _HREF_RE.findall(cleaned):
        found.append(href)
    # Bare URLs in the text (self-sent saves are often just a bare link).
    text = eps.html_to_text(cleaned)
    for bare in _URL_RE.findall(text):
        found.append(bare)
    # Some bodies put the bare URL directly in HTML with no anchor.
    for bare in _URL_RE.findall(cleaned):
        found.append(bare)

    seen, out = set(), []
    for u in found:
        u = _normalize_url(html.unescape(u))
        if not u or len(u) < 8:
            continue
        if _is_boilerplate(u):
            continue
        key = u.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def classify_url(url: str) -> str:
    """Map a URL to one of: x | youtube | podcast | article. (The x-video
    subtype is detected later, during X resolution, when a video media is
    present.)"""
    low = (url or "").lower()
    host = low.split("//", 1)[-1].split("/", 1)[0]
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if any(h in host for h in (
        "open.spotify.com", "podcasts.apple.com", "pca.st", "overcast.fm",
        "pod.link", "anchor.fm", "megaphone.fm", "podcasts.google.com",
        "castbox.fm", "podbean.com",
    )):
        return "podcast"
    if any(h in host for h in ("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com")):
        return "x"
    return "article"


# ======================================================================
#  CONTENT RESOLVERS (all null-safe; partial=true + reason on failure)
# ======================================================================


def _http_get(url, headers=None, timeout=30):
    """Single GET via the shared retry helper. Returns the Response or None on
    any failure (never raises)."""
    try:
        return eps._request_with_retry("GET", url, headers or {}, params=None)
    except Exception as e:
        logger.warning(f"[learn] GET failed {url[:80]}: {e}")
        return None


def _partial(kind, reason):
    return {"text": "", "kind": kind, "partial": True, "reason": reason, "content_date": None}


def _fetch_jina(url: str):
    """Article reader: GET https://r.jina.ai/{url} -> clean markdown."""
    headers = {"Accept": "text/plain"}
    jina_key = os.environ.get("JINA_API_KEY", "")  # optional: only raises rate limits
    if jina_key:
        headers["Authorization"] = f"Bearer {jina_key}"
    resp = _http_get("https://r.jina.ai/" + url, headers=headers)
    if resp is None or resp.status_code >= 400 or not resp.text:
        logger.info(f"[learn] jina shape: status={getattr(resp, 'status_code', None)} url={url[:80]}")
        return None
    return resp.text


def _fetch_trafilatura(url: str):
    """Article fallback: trafilatura. Lazy import so a missing dep degrades to
    partial rather than breaking module import."""
    try:
        import trafilatura  # noqa: F401
    except Exception:
        logger.info("[learn] trafilatura not available; skipping fallback")
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return text or None
    except Exception as e:
        logger.warning(f"[learn] trafilatura failed {url[:80]}: {e}")
        return None


def resolve_article(url: str) -> dict:
    text = _fetch_jina(url)
    source = "jina"
    if not text:
        text = _fetch_trafilatura(url)
        source = "trafilatura"
    if not text:
        return _partial("article", "article fetch returned nothing (Jina + trafilatura)")
    logger.info(f"[learn] article resolved via {source}: {len(text)} chars {url[:80]}")
    return {"text": text[:20000], "kind": "article", "partial": False, "reason": "", "content_date": None}


def _youtube_video_id(url: str):
    m = re.search(r"(?:youtu\.be/|v=|/shorts/|/embed/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else None


def _fetch_youtube_transcript(url: str):
    """Primary YouTube resolver: youtube-transcript-api. Lazy import."""
    vid = _youtube_video_id(url)
    if not vid:
        return None
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except Exception:
        logger.info("[learn] youtube-transcript-api not available; will fall back")
        return None
    try:
        chunks = YouTubeTranscriptApi.get_transcript(vid)
        text = " ".join(c.get("text", "") for c in (chunks or []))
        return text or None
    except Exception as e:
        logger.warning(f"[learn] youtube transcript failed {vid}: {e}")
        return None


def _fetch_spoken(url: str):
    """Podcast / YouTube fallback transcript via spoken.md (pass the URL).

    NOTE: the exact spoken.md request shape is provisional pending the first
    live run (SPOKEN_API_KEY is not set yet). On any non-200 this returns None
    so the item degrades to partial -- it never fabricates content."""
    spoken_key = os.environ.get("SPOKEN_API_KEY", "")
    if not spoken_key:
        logger.warning("[learn] SPOKEN_API_KEY not set -- podcast/transcript degraded to "
                       "'content not retrieved' (never fabricated)")
        return None
    headers = {"Authorization": f"Bearer {spoken_key}", "Accept": "application/json"}
    try:
        resp = eps._request_with_retry(
            "GET", "https://api.spoken.md/v1/transcript",
            headers, params={"url": url},
        )
    except Exception as e:
        logger.warning(f"[learn] spoken.md failed {url[:80]}: {e}")
        return None
    if resp is None or resp.status_code >= 400:
        logger.info(f"[learn] spoken.md shape: status={getattr(resp, 'status_code', None)}")
        return None
    try:
        data = resp.json()
    except Exception:
        return resp.text or None
    return data.get("transcript") or data.get("text") or None


def resolve_youtube(url: str) -> dict:
    text = _fetch_youtube_transcript(url)
    source = "youtube-transcript-api"
    if not text:
        text = _fetch_spoken(url)
        source = "spoken.md"
    if not text:
        return _partial("youtube", "no transcript (youtube-transcript-api + spoken.md)")
    logger.info(f"[learn] youtube resolved via {source}: {len(text)} chars")
    return {"text": text[:20000], "kind": "youtube", "partial": False, "reason": "", "content_date": None}


def resolve_podcast(url: str) -> dict:
    text = _fetch_spoken(url)
    if not text:
        return _partial("podcast", "no transcript (spoken.md); key may be unset")
    logger.info(f"[learn] podcast resolved via spoken.md: {len(text)} chars")
    return {"text": text[:20000], "kind": "podcast", "partial": False, "reason": "", "content_date": None}


def _tweet_id(url: str):
    m = re.search(r"/status(?:es)?/(\d+)", url) or re.search(r"/i/status/(\d+)", url)
    return m.group(1) if m else None


def _grok_responses_call(prompt: str, model: str, timeout: int = 90) -> dict:
    """Call the xAI Agent Tools API (POST /v1/responses) with server-side
    x_search + web_search so Grok reads an X post directly from its URL -- no X
    API bearer token, just XAI_API_KEY. Returns the parsed JSON; raises on a
    network/HTTP error after one transient retry. Uses requests directly (NOT
    the shared 30s helper) -- x_search routinely takes longer than 30s."""
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY not set")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model, "tools": [{"type": "x_search"}, {"type": "web_search"}],
            "input": [{"role": "user", "content": prompt}]}
    last = None
    for attempt in range(2):
        try:
            resp = requests.post("https://api.x.ai/v1/responses", headers=headers,
                                 json=body, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 1:
                last = f"status {resp.status_code}"
                time.sleep(3)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < 1:
                time.sleep(3)
                continue
            raise RuntimeError(last)
    raise RuntimeError(last or "unknown")


def _parse_grok_responses(data: dict):
    """Extract the assistant text + url citations from an xAI /v1/responses
    payload. Walks output[] for the assistant message because the top-level
    output_text convenience is frequently null. Returns (text, citations)."""
    data = data or {}
    citations = []
    txt = (data.get("output_text") or "").strip()
    for item in (data.get("output") or []):
        if not isinstance(item, dict):
            continue
        if not txt and item.get("type") == "message" and item.get("role") == "assistant":
            parts = [c["text"] for c in (item.get("content") or [])
                     if c.get("type") == "output_text" and c.get("text")]
            if parts:
                txt = "\n".join(parts).strip()
        for c in (item.get("content") or []):
            for a in (c.get("annotations") or []):
                if a.get("type") == "url_citation" and a.get("url"):
                    citations.append(a["url"])
    return txt, citations


def _grok_stt(mp4_url: str):
    """DORMANT: transcribe an mp4's audio via Grok STT (api.x.ai/v1/stt). Kept
    for a future X-video path but currently UNWIRED -- the Grok x_search
    resolver does not surface mp4 URLs, so X-video AUDIO is not transcribed (the
    post is text/context-summarized instead). Wiring real audio transcription
    needs an mp4 source (X API media variants, or a video extractor)."""
    xai_key = os.environ.get("XAI_API_KEY", "")
    if not xai_key or not mp4_url:
        return None
    headers = {"Authorization": f"Bearer {xai_key}", "Content-Type": "application/json"}
    try:
        resp = eps._request_with_retry(
            "POST", "https://api.x.ai/v1/stt", headers,
            json_body={"model": "grok-stt", "url": mp4_url},
        )
    except Exception as e:
        logger.warning(f"[learn] Grok STT failed: {e}")
        return None
    if resp is None or resp.status_code >= 400:
        logger.info(f"[learn] Grok STT shape: status={getattr(resp, 'status_code', None)}")
        return None
    try:
        data = resp.json()
    except Exception:
        return None
    return data.get("text") or data.get("transcript") or None


def resolve_x(url: str) -> dict:
    """Resolve an X post's CONTENT via Grok's Agent Tools API (x_search), using
    only XAI_API_KEY -- no X API bearer token. Returns the post's faithful
    content for the Sonnet summarizer; null-safe graceful degrade to partial on
    any failure (never fabricated). NOTE: this path text/context-summarizes the
    post; it does NOT transcribe X-video audio (see _grok_stt, dormant)."""
    model = os.environ.get("LEARN_X_MODEL", "grok-4.20-non-reasoning")
    prompt = (
        "Fetch the X (Twitter) post at this exact URL and report its FULL actual content "
        "faithfully: the author handle, the complete post text, any thread context, a brief "
        "description of any image or video media present, and the post date if shown. Do NOT "
        "editorialize or add outside information. If you cannot access or find the specific "
        "post, reply with exactly CANNOT_ACCESS.\nURL: " + url
    )
    try:
        data = _grok_responses_call(prompt, model)
    except Exception as e:
        logger.warning(f"[learn] Grok x_search failed {url[:60]}: {e}")
        return _partial("x", f"Grok x_search error: {e}")
    text, citations = _parse_grok_responses(data)
    if not text or text.strip().upper().startswith("CANNOT_ACCESS"):
        return _partial("x", "Grok could not access the post (CANNOT_ACCESS)")
    logger.info(f"[learn] x resolved via Grok x_search ({model}): {len(text)} chars {url[:60]}")
    return {"text": text[:20000], "kind": "x", "partial": False, "reason": "",
            "content_date": None, "citations": citations}


def resolve_item(item: dict) -> dict:
    """Dispatch a classified item to its resolver. Always returns a dict;
    never raises."""
    kind = item.get("type")
    url = item.get("url", "")
    try:
        if kind == "youtube":
            return resolve_youtube(url)
        if kind == "podcast":
            return resolve_podcast(url)
        if kind == "x":
            return resolve_x(url)
        return resolve_article(url)
    except Exception as e:  # defensive: a resolver bug must not kill the run
        logger.error(f"[learn] resolver crashed for {url[:80]}: {e}", exc_info=True)
        return _partial(kind or "article", f"resolver error: {e}")


# ======================================================================
#  CLAUDE CALL HELPERS
# ======================================================================


def _call_claude_text(prompt: str, model: str, max_tokens: int = 2000, tools=None) -> str:
    import anthropic
    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)
    kwargs = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]}
    if tools:
        kwargs["tools"] = tools
    resp = client.messages.create(**kwargs)
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()


def _call_claude_web(prompt: str) -> str:
    """Currency check uses Claude's server-side web_search tool -- runs inside
    the API call, so no new Railway egress and no search key to host."""
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]
    return _call_claude_text(prompt, CURATE_MODEL, max_tokens=1500, tools=tools)


def _cluster_call(prompt: str, model: str) -> str:
    """Default cluster-pass caller: large output budget so the whole-batch JSON
    is never truncated (a truncated response silently defeats consolidation)."""
    return _call_claude_text(prompt, model, max_tokens=CLUSTER_MAX_TOKENS)


def _curate_call(prompt: str, model: str) -> str:
    """Default curate-pass caller (per-cluster, so a smaller budget is fine)."""
    return _call_claude_text(prompt, model, max_tokens=CURATE_MAX_TOKENS)


def _extract_json(text: str):
    """Tolerant JSON extraction from an LLM reply (handles ```json fences and
    surrounding prose). Returns the parsed object or None."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(candidate[start:end + 1])
    except Exception:
        return None


# ======================================================================
#  PER-ITEM SUMMARY (Sonnet)
# ======================================================================


def summarize_item(item: dict, resolved: dict, call_fn=None) -> dict:
    """Produce the per-item summary that feeds clustering + curation.

    Returns: title, type, url, subject, content_date, tools[], specifics[],
    summary, confidence, partial. A partial/unfetched item is summarized from
    its title/sender only and never fabricated."""
    call_fn = call_fn or _call_claude_text
    subject = (item.get("subject") or "").strip()
    url = item.get("url", "")
    kind = resolved.get("kind") or item.get("type") or "article"
    partial = resolved.get("partial", True)
    content = (resolved.get("text") or "").strip()

    base = {
        "title": subject or url, "type": kind, "url": url, "subject": subject,
        "content_date": resolved.get("content_date"), "tools": [], "specifics": [],
        "summary": "", "confidence": "low", "partial": partial,
    }

    if partial or not content:
        base["summary"] = "content not retrieved -- from title/sender only"
        if resolved.get("reason"):
            base["summary"] += f" ({resolved['reason']})"
        return base

    instructions = (
        "You summarize one saved link for a personal read-later digest. Return ONLY a JSON "
        "object with keys: title (string), content_date (string YYYY-MM-DD or null if not "
        "stated in the content), tools (array of named tools/approaches mentioned), specifics "
        "(array of concrete key points), summary (2-3 sentences), confidence (high|medium|low). "
        "Do not invent a date -- use null if the content does not state one. Be faithful to the "
        "content; never embellish.\n\n"
        "Email subject (Ken's own topic tag, a strong signal): " + json.dumps(subject) + "\n"
        "Link type: " + kind + "\nURL: " + url + "\n\nContent:\n" + content[:14000]
    )
    try:
        raw = call_fn(instructions, SUMMARY_MODEL)
        parsed = _extract_json(raw) or {}
    except Exception as e:
        logger.warning(f"[learn] summarize failed {url[:80]}: {e}")
        parsed = {}

    base["title"] = (parsed.get("title") or subject or url).strip()
    base["content_date"] = parsed.get("content_date") or resolved.get("content_date")
    base["tools"] = parsed.get("tools") or []
    base["specifics"] = parsed.get("specifics") or []
    base["summary"] = (parsed.get("summary") or "").strip() or "(no summary produced)"
    base["confidence"] = parsed.get("confidence") or "medium"
    return base


# ======================================================================
#  CLUSTER (Opus) -- group the whole batch by topic
# ======================================================================

# Diagnostics from the most recent cluster_items call (surfaced in run status so
# a real-run failure -- e.g. a swallowed API error -- is visible without logs).
_LAST_CLUSTER_DIAG = {}


def cluster_items(summaries: list, call_fn=None) -> list:
    """Group all item summaries into topic clusters. The whole batch is seen at
    once so near-duplicates collapse. Returns a list of
    {"topic": str, "items": [summary, ...]}. Any summary not placed by the model
    becomes its own singleton cluster."""
    call_fn = call_fn or _cluster_call
    summaries = summaries or []
    if not summaries:
        return []
    if len(summaries) == 1:
        return [{"topic": summaries[0].get("title") or "Item", "items": [summaries[0]]}]

    lines = []
    for i, s in enumerate(summaries):
        lines.append(json.dumps({
            "index": i, "title": s.get("title"), "type": s.get("type"),
            "subject": s.get("subject"), "summary": s.get("summary"),
        }))
    instructions = (
        "Group these saved items into topic clusters. Items about the same tool, approach, "
        "person, or theme belong in one cluster (the queue is heavily redundant -- collapse "
        "near-duplicates). Return ONLY a JSON object: "
        '{"clusters": [{"topic": "short topic label", "members": [list of integer indices]}]}. '
        "Every index 0.." + str(len(summaries) - 1) + " must appear in exactly one cluster.\n\n"
        "Items:\n" + "\n".join(lines)
    )
    # Retry the whole-batch call: a transient overload/rate-limit (429/529) or a
    # truncated/unparseable response must NOT nuke consolidation into singletons.
    raw, last_err, attempts, parsed = "", None, 0, {}
    for attempt in range(3):
        attempts = attempt + 1
        try:
            raw = call_fn(instructions, CURATE_MODEL) or ""
            parsed = _extract_json(raw) or {}
            if parsed.get("clusters"):
                last_err = None
                break
            last_err = f"unparseable or empty response (raw_len={len(raw)})"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        logger.warning(f"[learn] clustering attempt {attempts} unsuccessful: {last_err}")
        if attempt < 2:
            time.sleep(2 * (attempt + 1))

    _LAST_CLUSTER_DIAG.clear()
    _LAST_CLUSTER_DIAG.update({
        "items": len(summaries), "attempts": attempts, "raw_len": len(raw or ""),
        "error": last_err, "model_clusters": len(parsed.get("clusters") or []),
        "fell_back_to_singletons": not bool(parsed.get("clusters")),
    })
    if not parsed.get("clusters"):
        logger.warning(
            f"[learn] clustering produced no clusters after {attempts} attempts ({last_err}) -- "
            f"falling back to singletons for {len(summaries)} items")

    clusters = []
    placed = set()
    for c in (parsed.get("clusters") or []):
        members = []
        for idx in (c.get("members") or []):
            if isinstance(idx, int) and 0 <= idx < len(summaries) and idx not in placed:
                members.append(summaries[idx])
                placed.add(idx)
        if members:
            clusters.append({"topic": (c.get("topic") or "Topic").strip(), "items": members})
    # Anything the model missed -> singleton clusters (never silently dropped).
    for i, s in enumerate(summaries):
        if i not in placed:
            clusters.append({"topic": s.get("title") or "Item", "items": [s]})
    return clusters


# ======================================================================
#  CURATE (Opus) -- best 1-2 per cluster for Ken; mark the rest superseded
# ======================================================================


def _parse_date(value):
    if not value or not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:len(fmt) + 6], fmt)
        except Exception:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            return None
    return None


def _newest_index(items: list) -> int:
    """Index of the most recent item by content_date; ties / all-None fall back
    to the last item (latest-saved)."""
    best_i, best_dt = len(items) - 1, None
    for i, it in enumerate(items):
        dt = _parse_date(it.get("content_date"))
        if dt is not None and (best_dt is None or dt > best_dt):
            best_dt, best_i = dt, i
    return best_i


def _normalize_bucket(bucket) -> str:
    if bucket in ASANA_SECTIONS:
        return bucket
    # tolerate case / partial matches
    low = (bucket or "").strip().lower()
    for name in BUCKETS:
        if name.lower() == low:
            return name
    return DEFAULT_BUCKET


def curate_cluster(cluster: dict, profile: str = KEN_PROFILE, call_fn=None) -> dict:
    """Pick the best 1-2 keepers for Ken and mark the rest superseded/redundant
    with a one-line reason. Within a cluster the newest credible item usually
    wins; an older one is kept only if it adds unique complementary value.

    Returns {"topic", "keepers": [item + meta], "superseded": [item + reason]}.
    Deterministic fallback (newest wins) if the model output is unusable."""
    call_fn = call_fn or _curate_call
    items = cluster.get("items") or []
    topic = cluster.get("topic") or "Topic"
    if not items:
        return {"topic": topic, "keepers": [], "superseded": []}
    if len(items) == 1:
        it = dict(items[0])
        it["why"] = "only item in this cluster"
        it["bucket"] = DEFAULT_BUCKET
        it["has_action"] = False
        it["action"] = ""
        return {"topic": topic, "keepers": [it], "superseded": []}

    lines = []
    for i, s in enumerate(items):
        lines.append(json.dumps({
            "index": i, "title": s.get("title"), "type": s.get("type"),
            "subject": s.get("subject"), "content_date": s.get("content_date"),
            "summary": s.get("summary"), "specifics": s.get("specifics"),
            "partial": s.get("partial"),
        }))
    buckets_str = ", ".join('"' + b + '"' for b in BUCKETS)
    instructions = (
        profile + "\n\n"
        'Curate this topic cluster ("' + topic + '") down to the best 1-2 items FOR KEN. '
        "Prefer the newest credible item; keep an older one only if it adds unique "
        "complementary value. Mark every other item superseded/redundant with a one-line "
        "reason. Tag each keeper with one bucket from: [" + buckets_str + "]. Flag whether the "
        "keeper carries a concrete action for Ken (has_action) and, if so, a one-line action.\n"
        "Return ONLY a JSON object:\n"
        '{"keepers": [{"index": int, "why": "why this one is best for Ken", "bucket": "one of the buckets", '
        '"has_action": true/false, "action": "one line or empty"}], '
        '"superseded": [{"index": int, "reason": "why dropped"}]}\n\n'
        "Items:\n" + "\n".join(lines)
    )
    try:
        raw = call_fn(instructions, CURATE_MODEL)
        parsed = _extract_json(raw) or {}
    except Exception as e:
        logger.warning(f"[learn] curation failed for cluster '{topic}': {e}")
        parsed = {}

    keepers, superseded = [], []
    keeper_idxs = set()
    for k in (parsed.get("keepers") or []):
        idx = k.get("index")
        if isinstance(idx, int) and 0 <= idx < len(items) and idx not in keeper_idxs:
            it = dict(items[idx])
            it["why"] = (k.get("why") or "").strip() or "best fit for Ken's current work"
            it["bucket"] = _normalize_bucket(k.get("bucket"))
            it["has_action"] = bool(k.get("has_action"))
            it["action"] = (k.get("action") or "").strip()
            keepers.append(it)
            keeper_idxs.add(idx)

    if not keepers:
        # Deterministic fallback: newest item wins, rest superseded.
        win = _newest_index(items)
        it = dict(items[win])
        it["why"] = "most recent item in the cluster (auto-selected)"
        it["bucket"] = DEFAULT_BUCKET
        it["has_action"] = False
        it["action"] = ""
        keepers.append(it)
        keeper_idxs.add(win)
        for i, s in enumerate(items):
            if i not in keeper_idxs:
                d = dict(s)
                d["reason"] = "redundant -- superseded by the most recent item in this cluster"
                superseded.append(d)
        return {"topic": topic, "keepers": keepers, "superseded": superseded}

    reasons = {s.get("index"): (s.get("reason") or "").strip()
               for s in (parsed.get("superseded") or []) if isinstance(s.get("index"), int)}
    for i, s in enumerate(items):
        if i not in keeper_idxs:
            d = dict(s)
            d["reason"] = reasons.get(i) or "redundant or superseded within this cluster"
            superseded.append(d)
    return {"topic": topic, "keepers": keepers, "superseded": superseded}


# ======================================================================
#  CURRENCY CHECK (Claude web_search) -- fast-moving AI-tooling keepers only
# ======================================================================


def is_fast_moving(topic: str) -> bool:
    low = (topic or "").lower()
    return any(kw in low for kw in FAST_MOVING_KEYWORDS)


def currency_check(keeper: dict, topic: str, mode: str = None, call_fn=None) -> dict:
    """For a keeper in a fast-moving AI-tooling cluster, confirm it is still
    current; if superseded, annotate keeper['currency_note'] with the newer
    resource. Gated by LEARN_CURRENCY_CHECK (fast-moving|off|all). The web call
    is NOT made for skipped clusters."""
    mode = mode or LEARN_CURRENCY_CHECK
    if mode == "off":
        return keeper
    if mode == "fast-moving" and not is_fast_moving(topic):
        return keeper  # slow-moving topic: judged on recency + content only

    call_fn = call_fn or _call_claude_web
    prompt = (
        "Today, is the tool/approach below still the current best practice, or has it been "
        "superseded by something newer? Use web search to check. Be concise.\n"
        'Return ONLY a JSON object: {"current": true/false, "note": "if superseded, name the '
        'newer canonical tool/resource in one line; else empty"}.\n\n'
        "Topic: " + (topic or "") + "\n"
        "Item title: " + (keeper.get("title") or "") + "\n"
        "Item date: " + str(keeper.get("content_date")) + "\n"
        "Summary: " + (keeper.get("summary") or "")
    )
    try:
        raw = call_fn(prompt)
        parsed = _extract_json(raw) or {}
    except Exception as e:
        logger.warning(f"[learn] currency check failed: {e}")
        return keeper
    if parsed.get("current") is False:
        note = (parsed.get("note") or "").strip()
        keeper["currency_note"] = "likely superseded" + (": " + note if note else "")
    elif parsed.get("current") is True:
        keeper["currency_note"] = "confirmed current"
    return keeper


# ======================================================================
#  DIGEST RENDERING (HTML, grouped by cluster, ordered by importance)
# ======================================================================


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _cluster_sort_key(c):
    keepers = c.get("keepers") or []
    has_action = any(k.get("has_action") for k in keepers)
    saved = len(keepers) + len(c.get("superseded") or [])
    return (0 if has_action else 1, -saved)


def render_digest_html(curated: list) -> str:
    """Build the Friday digest: one block per topic cluster (ordered by
    importance), each 'N saved -> recommended best' with why-this-one, currency
    note and action; a collapsed 'Skipped' list at the end."""
    ordered = sorted([c for c in curated if c.get("keepers")], key=_cluster_sort_key)
    total_saved = sum(len(c.get("keepers") or []) + len(c.get("superseded") or []) for c in curated)
    total_keepers = sum(len(c.get("keepers") or []) for c in curated)
    total_skipped = sum(len(c.get("superseded") or []) for c in curated)

    css_body = "font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;line-height:1.5;"
    parts = [f'<div style="{css_body}">']
    parts.append(
        f'<p style="color:#555;">Drained your read/learn folder: <b>{total_saved}</b> saved, '
        f'consolidated to <b>{total_keepers}</b> worth your time across '
        f'<b>{len(ordered)}</b> topics. {total_skipped} set aside as redundant or outdated.</p>'
    )

    for c in ordered:
        keepers = c.get("keepers") or []
        saved = len(keepers) + len(c.get("superseded") or [])
        parts.append('<div style="margin:18px 0;padding:12px 14px;border-left:3px solid #2b6cb0;background:#f7fafc;">')
        parts.append(
            f'<div style="font-size:15px;font-weight:bold;color:#1a202c;">{_esc(c.get("topic"))} '
            f'<span style="font-weight:normal;color:#718096;">({saved} saved -&gt; '
            f'{len(keepers)} recommended)</span></div>'
        )
        for k in keepers:
            title, url = _esc(k.get("title")), _esc(k.get("url"))
            link = f'<a href="{url}" style="color:#2b6cb0;text-decoration:none;">{title}</a>' if url else title
            parts.append(f'<div style="margin-top:10px;font-weight:bold;">{link}</div>')
            if k.get("partial"):
                parts.append('<div style="color:#b7791f;font-size:12px;">content not retrieved -- from title/sender only</div>')
            parts.append(f'<div style="margin-top:3px;">{_esc(k.get("summary"))}</div>')
            if k.get("why"):
                parts.append(f'<div style="margin-top:3px;color:#2f855a;"><b>Why this one:</b> {_esc(k.get("why"))}</div>')
            if k.get("currency_note"):
                parts.append(f'<div style="margin-top:3px;color:#c05621;"><b>Currency:</b> {_esc(k.get("currency_note"))}</div>')
            if k.get("has_action") and k.get("action"):
                parts.append(f'<div style="margin-top:3px;color:#553c9a;"><b>Action:</b> {_esc(k.get("action"))}</div>')
        parts.append('</div>')

    # Collapsed skipped list.
    skipped = []
    for c in curated:
        for s in (c.get("superseded") or []):
            skipped.append(s)
    if skipped:
        parts.append(f'<details style="margin-top:20px;"><summary style="cursor:pointer;color:#718096;">'
                     f'Skipped as redundant or outdated ({len(skipped)} items)</summary>'
                     f'<ul style="color:#718096;font-size:13px;margin-top:8px;">')
        for s in skipped:
            title, url = _esc(s.get("title")), _esc(s.get("url"))
            link = f'<a href="{url}" style="color:#718096;">{title}</a>' if url else title
            parts.append(f'<li style="margin-bottom:4px;">{link} -- {_esc(s.get("reason"))}</li>')
        parts.append('</ul></details>')

    parts.append('</div>')
    return "".join(parts)


def send_digest_email(subject: str, body: str, content_type: str = "HTML"):
    sender = os.environ.get("BOT_SENDER_EMAIL", "")
    if not sender:
        raise RuntimeError("BOT_SENDER_EMAIL not set -- digest not emailed")
    message = {
        "subject": subject,
        "body": {"contentType": content_type, "content": body},
        "toRecipients": [{"emailAddress": {"address": r}} for r in LEARN_RECIPIENTS],
    }
    eps.graph_post(f"{eps.MS_GRAPH_BASE}/users/{sender}/sendMail",
                   {"message": message, "saveToSentItems": False})
    logger.info(f"[learn] Digest emailed to {', '.join(LEARN_RECIPIENTS)}")


# ======================================================================
#  ASANA ROUTING (keepers with an action -> matching bucket section)
# ======================================================================


def _section_for_bucket(bucket: str) -> str:
    return ASANA_SECTIONS.get(_normalize_bucket(bucket), ASANA_SECTIONS[DEFAULT_BUCKET])


def create_triage_task(keeper: dict) -> str:
    """Create a task in 'Read/Learn Triage', placed in the matching bucket
    section. Returns the task GID or '' on failure (never raises)."""
    try:
        notes_lines = [
            keeper.get("summary") or "",
            "",
            "Link: " + (keeper.get("url") or ""),
            "Why this one: " + (keeper.get("why") or ""),
        ]
        if keeper.get("currency_note"):
            notes_lines.append("Currency: " + keeper["currency_note"])
        if keeper.get("action"):
            notes_lines.append("Action: " + keeper["action"])
        data = {
            "name": (keeper.get("title") or "Read/Learn item")[:250],
            "notes": "\n".join(notes_lines),
            "projects": [ASANA_PROJECT_GID_LEARN],
        }
        ws = os.environ.get("ASANA_WORKSPACE_GID", "")
        if ws:
            data["workspace"] = ws
        task = asana_client.asana_request("POST", "/tasks", data)
        task_gid = (task or {}).get("gid")
        if not task_gid:
            return ""
        section_gid = _section_for_bucket(keeper.get("bucket"))
        asana_client.asana_request("POST", f"/sections/{section_gid}/addTask", {"task": task_gid})
        logger.info(f"[learn] Asana task {task_gid} -> section {keeper.get('bucket')}")
        return task_gid
    except Exception as e:
        logger.error(f"[learn] Asana task creation failed: {e}")
        return ""


# ======================================================================
#  MAIL FETCH + POST-PROCESS (mark read, move to Processed)
# ======================================================================


def fetch_unread(folder_id: str = LEARN_FOLDER_ID, processed_ids: set = None, backlog: bool = False,
                 limit: int = None) -> list:
    """Fetch unread messages from the read/learn folder by ID, skipping any IDs
    already in learn_processed.json. First run fetches the full backlog; we do
    NOT chunk -- the clustering pass must see the whole set at once. limit (if
    set) caps the number returned -- a diagnostic knob for fast partial runs."""
    processed_ids = processed_ids or set()
    base = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/mailFolders/{folder_id}/messages"
    params = {
        "$filter": "isRead eq false",
        "$select": "id,subject,from,receivedDateTime,body,webLink",
        "$top": "50",
    }
    url = base
    messages, pages = [], 0
    while url and pages < 25:
        data = eps.graph_get(url, params=params if url == base else None) or {}
        for m in (data.get("value") or []):
            if m.get("id") in processed_ids:
                continue
            messages.append(m)
        url = data.get("@odata.nextLink")
        pages += 1
        if limit and len(messages) >= limit:
            break
    if limit:
        messages = messages[:limit]
    logger.info(f"[learn] Fetched {len(messages)} unread (backlog={backlog}, pages={pages}, limit={limit})")
    return messages


def build_item(msg: dict) -> dict:
    """Turn a Graph message into the first URL-bearing item (extract + classify).
    A message with no usable URL is summarized from its subject only."""
    subject = (msg.get("subject") or "").strip()
    body_html = ((msg.get("body") or {}).get("content")) or ""
    urls = extract_urls(body_html)
    url = urls[0] if urls else ""
    return {
        "message_id": msg.get("id"),
        "subject": subject,
        "url": url,
        "type": classify_url(url) if url else "article",
        "all_urls": urls,
        "received": msg.get("receivedDateTime"),
    }


_processed_folder_cache = {"id": None}


def ensure_processed_subfolder(folder_id: str = LEARN_FOLDER_ID) -> str:
    """Find (or create) the 'Processed' childfolder under read/learn. Items are
    moved there, never deleted. Returns the folder ID or '' on failure."""
    if _processed_folder_cache["id"]:
        return _processed_folder_cache["id"]
    url = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/mailFolders/{folder_id}/childFolders"
    try:
        data = eps.graph_get(url) or {}
        for f in (data.get("value") or []):
            if (f.get("displayName") or "").lower() == PROCESSED_SUBFOLDER_NAME.lower():
                _processed_folder_cache["id"] = f.get("id")
                return _processed_folder_cache["id"]
        created = eps.graph_post(url, {"displayName": PROCESSED_SUBFOLDER_NAME}) or {}
        _processed_folder_cache["id"] = created.get("id")
        return _processed_folder_cache["id"] or ""
    except Exception as e:
        logger.error(f"[learn] ensure_processed_subfolder failed: {e}")
        return ""


def _graph_patch(url: str, body: dict):
    headers = {"Authorization": f"Bearer {eps.get_graph_token()}", "Content-Type": "application/json"}
    return eps._request_with_retry("PATCH", url, headers, json_body=body)


def mark_read_and_move(message_id: str, processed_folder_id: str):
    """Mark a message read and move it to the Processed subfolder. Best effort
    -- a failure here is logged but does not fail the run."""
    if not message_id:
        return
    base = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/messages/{message_id}"
    try:
        _graph_patch(base, {"isRead": True})
    except Exception as e:
        logger.warning(f"[learn] mark-read failed {message_id[:16]}: {e}")
    if processed_folder_id:
        try:
            eps.graph_post(base + "/move", {"destinationId": processed_folder_id})
        except Exception as e:
            logger.warning(f"[learn] move failed {message_id[:16]}: {e}")


# ======================================================================
#  STATUS
# ======================================================================


def write_status(status: dict):
    try:
        with open(LEARN_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, default=str)
    except Exception as e:
        logger.warning(f"[learn] Could not write status: {e}")


def read_status() -> dict:
    try:
        with open(LEARN_STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"status": "no_runs", "message": "No learn-digest run has completed yet."}
    except Exception as e:
        return {"status": "error", "error": f"could not read status: {e}"}


# ======================================================================
#  RUN ORCHESTRATION
# ======================================================================


def _learn_run_inner(dry_run: bool, backlog: bool, limit: int = None) -> dict:
    """The pipeline, run with the lock held. Mirrors the spec's 11-step flow."""
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc)
    processed_ids = _load_processed_ids()

    messages = fetch_unread(LEARN_FOLDER_ID, processed_ids, backlog, limit=limit)
    if not messages:
        result = {"status": "ok", "run_id": run_id, "sent": False, "reason": "no unread items",
                  "clusters": 0, "keepers": 0, "skipped": 0, "dry_run": dry_run}
        write_status(result)
        return result

    # Resolve + summarize each message's primary link.
    summaries = []
    for msg in messages:
        item = build_item(msg)
        resolved = resolve_item(item) if item.get("url") else _partial(item.get("type"), "no link found in message")
        summ = summarize_item(item, resolved)
        summ["message_id"] = item.get("message_id")
        summaries.append(summ)

    # Cluster the whole batch, then curate each cluster.
    clusters = cluster_items(summaries)
    curated = [curate_cluster(c) for c in clusters]

    # Currency check on fast-moving keepers; tag buckets already done in curate.
    for c in curated:
        c["keepers"] = [currency_check(k, c.get("topic")) for k in c.get("keepers", [])]

    total_keepers = sum(len(c.get("keepers") or []) for c in curated)
    total_skipped = sum(len(c.get("superseded") or []) for c in curated)

    local_date = (started + timedelta(hours=ISRAEL_UTC_OFFSET_HOURS)).strftime("%a %d %b %Y")
    subject = f"[Sara] Read/Learn digest -- {local_date}"
    body = render_digest_html(curated)

    sent, tasks_created = False, 0
    if dry_run:
        logger.info(f"[learn] [dry-run] {len(messages)} items -> {len(clusters)} clusters, "
                    f"{total_keepers} keepers, {total_skipped} skipped (no email/tasks/move)")
    else:
        send_digest_email(subject, body)
        sent = True
        # Asana tasks for keepers that carry an action.
        for c in curated:
            for k in c.get("keepers", []):
                if k.get("has_action"):
                    if create_triage_task(k):
                        tasks_created += 1
        # Post-process: mark ALL processed (keepers + skipped) read + move.
        processed_folder = ensure_processed_subfolder()
        new_ids = set()
        for s in summaries:
            mid = s.get("message_id")
            if mid:
                mark_read_and_move(mid, processed_folder)
                new_ids.add(mid)
        _save_processed_ids(processed_ids | new_ids)

    result = {
        "status": "ok", "run_id": run_id,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run, "backlog": backlog, "sent": sent,
        "items": len(messages), "clusters": len(clusters),
        "keepers": total_keepers, "skipped": total_skipped,
        "tasks_created": tasks_created, "subject": subject,
        "cluster_diag": dict(_LAST_CLUSTER_DIAG),
        # Body is persisted so a dry-run can be sanity-checked via /learn/status
        # without sending the email or mutating the mailbox.
        "body": body,
    }
    write_status(result)
    logger.info(f"[learn] Run {run_id} done: {result}")
    return result


def run_learn(dry_run: bool = False, backlog: bool = False, force: bool = False, limit: int = None) -> dict:
    """Public entry. Acquires the cross-process file lock + the in-process lock,
    runs the pipeline, releases both. Guarantees exactly one run (and exactly
    one digest send) even under a two-worker race.

    force=True unconditionally clears a pre-existing run lock first -- an
    operator override for an orphaned lock (e.g. a container killed mid-run).
    Do NOT use force on the real send run; it defeats the single-send guard."""
    if force:
        logger.warning("[learn] force=1 -- clearing any existing run lock before starting")
        _release_run_lock()
    if not _acquire_run_lock():
        logger.warning("[learn] Skipping -- another run already in progress (cross-process)")
        return {"status": "skipped", "reason": "run already in progress"}
    if not _learn_lock.acquire(blocking=False):
        logger.warning("[learn] Skipping -- another run already in progress (in-process)")
        _release_run_lock()
        return {"status": "skipped", "reason": "run already in progress"}
    try:
        return _learn_run_inner(dry_run, backlog, limit=limit)
    except Exception as e:
        import traceback as _tb
        tb = _tb.format_exc()
        logger.error(f"[learn] Run failed: {tb}")
        write_status({"status": "error", "error": str(e), "traceback": tb,
                      "finished_at": datetime.now(timezone.utc).isoformat()})
        raise
    finally:
        _learn_lock.release()
        _release_run_lock()


def main():
    parser = argparse.ArgumentParser(description="Read/Learn Digest (Sara module)")
    parser.add_argument("--backlog", action="store_true", help="Force the full-backlog first run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve+cluster+curate; send no email, create no tasks, move nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_learn(dry_run=args.dry_run, backlog=args.backlog)


if __name__ == "__main__":
    main()
