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
import shutil
import logging
import argparse
import tempfile
import requests
import threading
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

# ----------------------------------------------------------------------
#  DETERMINISTIC SECTION ROUTING (Part A) + PRIORITY AT CREATION (Part B).
#  Routing is keyed off the cluster topic + each keeper's own subject/title/
#  summary tags already produced -- NO extra LLM call. Rules below are applied
#  FIRST MATCH WINS, in order; the order encodes Ken's 2026-06-22 manual
#  re-categorization. The biotech-vs-general-investing tie-breaker IS the
#  ordering: a biotech signal (Negev Labs) is tested before the general
#  family-office bucket (Zirmania), so "is the thesis biotech?" -> yes lands
#  Negev, and a genuine 50/50 also defaults to Negev (per Ken). The router
#  never returns the manual-only sections (Untitled / Video to watch / Drop --
#  Not important); those are for Ken's hand-triage.
#  GIDs verified live against project 1215897524719950 on 2026-06-22.
LEARN_SECTION_GID = {
    "Health": "1215899542179143",
    "Negev Labs": "1215897524642810",
    "Zirmania Family Office": "1215886226827868",
    "Travel Relay": "1215886226830719",
    "Ariadne Website": "1215898379087843",
    "Sara Pipeline": "1215899505871771",
    "General / Reference": "1215899505871835",
}
LEARN_DEFAULT_SECTION = "General / Reference"

# Sara Pipeline encodes convention (b) (Ken's choice 2026-06-22): ONLY items
# that parallel the Sara meeting-pipeline (post-meeting / transcript / meeting-
# intelligence / chief-of-staff system builds like OpenClaw, plus self-
# referential Sara infra). General Claude tooling (Cowork, Obsidian, PKM,
# second-brain) falls through to General / Reference.
_ROUTING_RULES = [
    ("Health", (
        "health", "wellness", "biohack", "longevity", "peptide", "nootropic",
        "sleep", "supplement", "diet", "digestion", "gut health", "microbiome",
        "huberman", "hormone", "hormonal", "testosterone", "metabolic",
        "nutrition", "fasting", "fitness", "vitamin", "mens health",
    )),
    ("Negev Labs", (
        "drug develop", "clinical", "biotech", "pharma", "therapeutic", "rnpv",
        "unmet need", "psychedelic", "psilocybin", "mdma", "ketamine", "cns",
        "parkinson", "pd-apathy", "apathy", "ai-in-bio", "ai in bio", "techbio",
        "ariadne bio", "ariadne fundrais", "ariadne raise", "ariadne investor",
        "skinny label", "skinny-label", "hikma", "ra capital", "lonza", "ardd",
        "coefficient bio", "2048 ventures", "life science", "life-science",
        "fda", "indication", "preclinical", "phase ii", "phase iii",
    )),
    ("Zirmania Family Office", (
        "macro", "market crash", "stock market", "recession", "public equit",
        "equities", "commodit", "copper", "semiconductor", "michael burry",
        "burry", "generalist vc", "co-invest", "vntr", "startup nation",
        "deal sourcing", "deal-sourcing", "gp/lp", "storyteller",
        "asset allocation", "saas vs biotech", "family office", "hedge fund",
        "valuation",
        # Financial deal-analysis / investment-workflow automation tooling routes
        # here by default (Zirmania = general non-biotech investing/automation).
        # Biotech is tested first, so a biotech deal-analysis tool still lands
        # Negev; only a build-it-yourself meeting-pipeline parallel goes to Sara.
        "financial services", "financial workflow", "financial model",
        "deal analysis", "deal-analysis", "deal data", "deal-data",
        "due diligence", "due-diligence", "investment workflow",
        "investment-workflow", "underwriting", "bloomberg", "factset",
        "pitchbook", "capital iq", "dcf", "lbo",
    )),
    ("Travel Relay", (
        "travel", "flight", "booking", "itinerary", "hotel", "airline", "kiwi",
        "trip", "boarding", "check-in", "travel assistant",
    )),
    ("Ariadne Website", (
        "design system", "ui design", "ux design", "web design", "css",
        "tailwind", "figma", "landing page", "site build", "site-build",
        "frontend", "front-end", "typography", "web component",
    )),
    ("Sara Pipeline", (
        "meeting pipeline", "meeting-pipeline", "post-meeting", "openclaw",
        "meeting intelligence", "transcript pipeline", "chief of staff",
        "chief-of-staff", "sara pipeline",
    )),
]

# Priority custom field bound to the Read/Learn Triage project (Part B). GUARD:
# the workspace also has a DUPLICATE Priority field 1206810235510187 -- never
# use it; only this project-bound field GID is correct.
LEARN_PRIORITY_FIELD_GID = "1199941453034656"
LEARN_PRIORITY_OPTION_GID = {
    "High": "1199941453034657",
    "Medium": "1199941453034658",
    "Low": "1199941453034659",
}

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

# Bounded execution so a run can never hang invisibly. Concurrency turns the
# ~80 sequential Grok/Anthropic per-item calls from ~hours into ~minutes;
# per-call timeouts cap any single stuck call (the SDK default is 10min x3).
LEARN_CONCURRENCY = int(os.environ.get("LEARN_CONCURRENCY", "5"))
LEARN_ANTHROPIC_TIMEOUT = int(os.environ.get("LEARN_ANTHROPIC_TIMEOUT", "90"))
LEARN_CLUSTER_TIMEOUT = int(os.environ.get("LEARN_CLUSTER_TIMEOUT", "180"))

# fast-moving (default) | off | all -- which clusters get the live web check.
LEARN_CURRENCY_CHECK = os.environ.get("LEARN_CURRENCY_CHECK", "fast-moving")

# Trailing window (days) for a normal (non-backlog) run. Saved items received
# within this many days are processed REGARDLESS of read/unread state. Ken
# forwards most saves to himself, so they arrive already READ -- the old
# unread-only filter silently skipped the whole queue (the 2026-07 empty-digest
# bug). Dedup is the processed-ID store + the move-to-Processed step, NOT the
# read flag. The window bounds the scan to recent saves so a pre-existing
# historical backlog is not swept in one shot; backlog=1 is the escape hatch
# that ignores both this window and the processed-ID store.
LEARN_LOOKBACK_DAYS = int(os.environ.get("LEARN_LOOKBACK_DAYS", "14"))

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
- INCLUDE-HIGH signal: financial deal-analysis / investment-workflow automation tooling
  (Claude for Financial Services, valuation/DCF/LBO modeling, due-diligence or deal-data
  automation agents, Bloomberg/FactSet/PitchBook-class pulls). This is Zirmania family-office
  core AND feeds Ken's own automation builds -- always KEEP it and rate it High; it OUTRANKS
  the generic "AI tooling = Medium" default. Generic dev/coding tooling with no financial /
  deal-analysis purpose stays Medium.
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
LEARN_PENDING_STT_FILE = os.path.join(_LEARN_DATA_DIR, "learn_pending_stt.json")
LEARN_STT_STATUS_FILE = os.path.join(_LEARN_DATA_DIR, "learn_stt_status.json")
LEARN_STT_MAX_ATTEMPTS = 3
LEARN_STT_MAX_DURATION_SEC = 3600  # 60 min
LEARN_STT_MAX_BYTES = 100 * 1024 * 1024  # 100MB
LEARN_YTDLP_TIMEOUT = int(os.environ.get("LEARN_YTDLP_TIMEOUT", "120"))
LEARN_STT_PROBE_TIMEOUT = int(os.environ.get("LEARN_STT_PROBE_TIMEOUT", "45"))
LEARN_STT_API_TIMEOUT = int(os.environ.get("LEARN_STT_API_TIMEOUT", "180"))
# A backlog run resolves dozens of links and makes many API calls; allow a
# generous window before a held lock is treated as orphaned.
LEARN_LOCK_MAX_AGE = int(os.environ.get("LEARN_LOCK_MAX_AGE", str(2 * 3600)))

# In-process guard for manual trigger + scheduler in the same process.
import threading as _threading  # noqa: E402
_learn_lock = _threading.Lock()
_pending_stt_lock = _threading.Lock()


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
#  PENDING X-VIDEO STT STORE (capture during digest, replay separately)
# ======================================================================


def _normalize_x_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"^https?://(?:www\.)?(?:twitter|x)\.com", "https://x.com", u, flags=re.I)
    return u.lower()


def _load_pending_stt() -> dict:
    try:
        with open(LEARN_PENDING_STT_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {"entries": data}
        return data if isinstance(data, dict) else {"entries": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": []}


def _save_pending_stt(data: dict):
    try:
        parent = os.path.dirname(LEARN_PENDING_STT_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LEARN_PENDING_STT_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"[learn] Failed to write pending STT store: {e}")


def _mutate_pending_stt(mutator):
    """Atomic load-modify-save for learn_pending_stt.json (thread-safe)."""
    with _pending_stt_lock:
        data = _load_pending_stt()
        mutator(data)
        _save_pending_stt(data)
        return data


def _capture_pending_stt(entry: dict):
    """Record an X post whose video audio could not be transcribed inline."""
    try:
        raw_url = (entry.get("source_url") or "").strip()
        norm = _normalize_x_url(raw_url)
        if not norm:
            return

        def mutate(data):
            entries = data.get("entries") or []
            for e in entries:
                if _normalize_x_url(e.get("source_url") or "") == norm:
                    if (e.get("status") or "") == "done":
                        return
                    e["title"] = entry.get("title") or e.get("title") or ""
                    e["date"] = entry.get("date") or e.get("date") or ""
                    e["processed_folder_location"] = (
                        entry.get("processed_folder_location")
                        or e.get("processed_folder_location")
                        or PROCESSED_SUBFOLDER_NAME
                    )
                    data["entries"] = entries
                    return
            entries.append({
                "id": uuid.uuid4().hex[:8],
                "source_url": raw_url or norm,
                "title": entry.get("title") or "",
                "date": entry.get("date") or "",
                "processed_folder_location": (
                    entry.get("processed_folder_location") or PROCESSED_SUBFOLDER_NAME
                ),
                "status": "pending",
                "attempts": 0,
                "last_error": "",
                "transcript": "",
                "captured_at": datetime.now(timezone.utc).isoformat(),
            })
            data["entries"] = entries

        _mutate_pending_stt(mutate)
        logger.info(f"[learn] Captured x-video for STT replay: {raw_url[:80]}")
    except Exception as e:
        logger.warning(f"[learn] pending STT capture failed: {e}")


def _find_pending_entry(data: dict, entry_id: str, source_url: str):
    norm = _normalize_x_url(source_url or "")
    for ent in data.get("entries") or []:
        if entry_id and ent.get("id") == entry_id:
            return ent
        if norm and _normalize_x_url(ent.get("source_url") or "") == norm:
            return ent
    return None


def _record_stt_failure(entry_id: str, source_url: str, error: str):
    def mutate(data):
        ent = _find_pending_entry(data, entry_id, source_url)
        if not ent:
            return
        ent["attempts"] = int(ent.get("attempts") or 0) + 1
        ent["last_error"] = (error or "unknown error")[:500]
        if ent["attempts"] >= LEARN_STT_MAX_ATTEMPTS:
            ent["status"] = "failed"
        else:
            ent["status"] = "pending"

    _mutate_pending_stt(mutate)


def _mark_stt_success(entry_id: str, source_url: str, text: str):
    def mutate(data):
        ent = _find_pending_entry(data, entry_id, source_url)
        if not ent:
            return
        ent["status"] = "done"
        ent["transcript"] = (text or "")[:50000]
        ent["last_error"] = ""
        ent["transcribed_at"] = datetime.now(timezone.utc).isoformat()

    _mutate_pending_stt(mutate)


def write_stt_status(result: dict):
    try:
        parent = os.path.dirname(LEARN_STT_STATUS_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(LEARN_STT_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"[learn] Failed to write STT replay status: {e}")


def read_stt_status() -> dict:
    try:
        with open(LEARN_STT_STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


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
            # Retry only transient failures (network errors, or 429/5xx). A
            # non-retryable 4xx (400/401/404) fails fast -- no wasted retry/fee.
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status in (429, 500, 502, 503, 504)
            if attempt < 1 and retryable:
                time.sleep(3)
                continue
            raise RuntimeError(last)
    raise RuntimeError(last or "unknown")


def _parse_grok_responses(data: dict):
    """Extract the assistant text + url citations from an xAI /v1/responses
    payload. Walks output[] for the ASSISTANT MESSAGE item only -- the top-level
    output_text convenience is frequently null, and other item types (reasoning,
    tool_result) can carry a STRING content that must NOT be iterated as dicts
    (would raise AttributeError and blank the item). Returns (text, citations)."""
    data = data or {}
    citations = []
    txt = (data.get("output_text") or "").strip()
    for item in (data.get("output") or []):
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "assistant":
            continue
        parts = []
        for c in (item.get("content") or []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "output_text" and c.get("text"):
                parts.append(c["text"])
            for a in (c.get("annotations") or []):
                if isinstance(a, dict) and a.get("type") == "url_citation" and a.get("url"):
                    citations.append(a["url"])
        if not txt and parts:
            txt = "\n".join(parts).strip()
    return txt, citations


_VIDEO_WITH_AUDIO_MARKER = re.compile(r"VIDEO_WITH_AUDIO:\s*yes\b", re.I)
_VIDEO_SIGNAL = re.compile(
    r"\b(video|attached video|plays inline|mp4|video clip|video media)\b", re.I
)


def _x_post_has_video(text: str) -> bool:
    if not text:
        return False
    if _VIDEO_WITH_AUDIO_MARKER.search(text):
        return True
    return bool(_VIDEO_SIGNAL.search(text))


def _strip_video_marker(text: str) -> str:
    if not text:
        return text
    return _VIDEO_WITH_AUDIO_MARKER.sub("", text).strip()


def _is_transient_ytdlp_error(err: str) -> bool:
    low = (err or "").lower()
    return any(x in low for x in ("timeout", "429", "503", "502", "connection", "network"))


_AUDIO_EXTS = frozenset({".m4a", ".mp3", ".opus", ".ogg", ".wav", ".aac", ".flac", ".mp4", ".webm", ".mkv"})
_SKIP_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".vtt", ".json", ".description", ".part"})


def _pick_extracted_audio_path(tmpdir: str, info: dict, ydl, outtmpl: str):
    """Pick the audio file yt-dlp produced; ignore thumbnails and side artifacts."""
    for req in (info or {}).get("requested_downloads") or []:
        fp = (req or {}).get("filepath")
        if fp and os.path.isfile(fp):
            return fp
    if info and ydl:
        try:
            base = ydl.prepare_filename(info, outtmpl=outtmpl)
            for ext in (".m4a", ".mp3", ".opus", ".ogg", ".aac"):
                candidate = os.path.splitext(base)[0] + ext
                if os.path.isfile(candidate):
                    return candidate
        except Exception:
            pass
    audio_files = []
    for f in os.listdir(tmpdir):
        path = os.path.join(tmpdir, f)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext in _SKIP_EXTS:
            continue
        if ext in _AUDIO_EXTS:
            audio_files.append(path)
    if not audio_files:
        return None
    audio_files.sort(key=lambda p: (0 if p.lower().endswith(".m4a") else 1, -os.path.getsize(p)))
    return audio_files[0]


def extract_x_post_audio(source_url: str, timeout: int = None):
    """Extract audio from an X post URL via yt-dlp + ffmpeg.

    Returns (audio_path, duration_sec, error_reason, tmpdir). audio_path is None
    on failure; tmpdir is a temp directory the caller must remove."""
    timeout = timeout or LEARN_YTDLP_TIMEOUT
    tmpdir = tempfile.mkdtemp(prefix="learn_stt_")
    last_err = "unknown yt-dlp error"

    def _work(holder):
        try:
            import yt_dlp
            outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": outtmpl,
                "quiet": True,
                "noplaylist": True,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                }],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(source_url, download=False)
                if not info:
                    holder["error"] = "yt-dlp: no media info"
                    return
                dur = info.get("duration") or 0
                if dur and dur > LEARN_STT_MAX_DURATION_SEC:
                    holder["error"] = (
                        f"duration {dur}s exceeds cap ({LEARN_STT_MAX_DURATION_SEC}s)"
                    )
                    return
                info = ydl.extract_info(source_url, download=True)
                path = _pick_extracted_audio_path(tmpdir, info, ydl, outtmpl)
                if not path:
                    holder["error"] = "yt-dlp: no audio file produced"
                    return
                size = os.path.getsize(path)
                if size > LEARN_STT_MAX_BYTES:
                    holder["error"] = f"audio size {size} exceeds cap ({LEARN_STT_MAX_BYTES})"
                    return
                holder["path"] = path
                holder["duration"] = (info or {}).get("duration") or dur
        except Exception as e:
            holder["error"] = f"yt-dlp: {e}"

    for attempt in range(2):
        holder = {}
        t = threading.Thread(target=_work, args=(holder,), daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            last_err = f"yt-dlp timeout after {timeout}s"
            if attempt < 1:
                time.sleep(3)
                continue
            return None, 0, last_err, tmpdir
        if holder.get("path"):
            return holder["path"], holder.get("duration") or 0, None, tmpdir
        last_err = holder.get("error") or last_err
        if attempt < 1 and _is_transient_ytdlp_error(last_err):
            time.sleep(3)
            continue
        return None, 0, last_err, tmpdir
    return None, 0, last_err, tmpdir


def _probe_x_native_video(url: str, timeout: int = None):
    """Cheap yt-dlp metadata probe (no download): does this X post have a NATIVE
    downloadable video within the duration cap? Returns (ok, duration, reason).

    Lets resolve_x avoid stranding STT captures for posts Grok flags as video-
    with-audio that have no fetchable native clip (linked/quoted video, GIF) or
    exceed LEARN_STT_MAX_DURATION_SEC. Any failure -> ok=False with a reason
    (never raises); an unfetchable post then surfaces its Grok summary instead
    of queueing for an STT pass that could never succeed."""
    timeout = timeout or LEARN_STT_PROBE_TIMEOUT
    holder = {}

    def _work(h):
        try:
            import yt_dlp
            opts = {"quiet": True, "noplaylist": True, "skip_download": True,
                    "format": "bestaudio/best"}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                h["reason"] = "no media info"
                return
            dur = info.get("duration") or 0
            if dur and dur > LEARN_STT_MAX_DURATION_SEC:
                h["reason"] = f"duration {int(dur)}s over {LEARN_STT_MAX_DURATION_SEC}s cap"
                return
            h["ok"] = True
            h["duration"] = dur
        except Exception as e:
            h["reason"] = str(e)[:200]

    t = threading.Thread(target=_work, args=(holder,), daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return False, 0, f"probe timeout after {timeout}s"
    return bool(holder.get("ok")), holder.get("duration") or 0, holder.get("reason") or ""


def _grok_stt_from_file(audio_path: str, timeout: int = None):
    """Transcribe local audio via Grok STT multipart upload. Returns (text, error)."""
    xai_key = os.environ.get("XAI_API_KEY", "")
    if not xai_key:
        return None, "XAI_API_KEY not set"
    if not audio_path or not os.path.isfile(audio_path):
        return None, "audio file missing"
    timeout = timeout or LEARN_STT_API_TIMEOUT
    headers = {"Authorization": f"Bearer {xai_key}"}
    basename = os.path.basename(audio_path)
    mime = "audio/mp4" if basename.endswith(".m4a") else "audio/mpeg"
    last_err = None
    for attempt in range(2):
        try:
            with open(audio_path, "rb") as f:
                resp = requests.post(
                    "https://api.x.ai/v1/stt",
                    headers=headers,
                    data=[("language", "en")],
                    files={"file": (basename, f, mime)},
                    timeout=timeout,
                )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 1:
                last_err = f"STT status {resp.status_code}"
                time.sleep(3)
                continue
            if resp.status_code >= 400:
                return None, f"STT status {resp.status_code}: {(resp.text or '')[:200]}"
            try:
                data = resp.json()
            except Exception:
                data = {}
            text = ((data or {}).get("text") or (data or {}).get("transcript") or "").strip()
            if not text:
                return None, "STT empty transcript"
            return text, None
        except Exception as e:
            last_err = f"STT {type(e).__name__}: {e}"
            if attempt < 1:
                time.sleep(3)
                continue
    return None, last_err or "STT failed"


def _grok_stt_from_url(audio_url: str, timeout: int = None):
    """Transcribe audio at a public URL via Grok STT URL mode. Returns (text, error)."""
    xai_key = os.environ.get("XAI_API_KEY", "")
    if not xai_key:
        return None, "XAI_API_KEY not set"
    if not audio_url:
        return None, "audio url missing"
    timeout = timeout or LEARN_STT_API_TIMEOUT
    headers = {"Authorization": f"Bearer {xai_key}"}
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.post(
                "https://api.x.ai/v1/stt",
                headers=headers,
                data=[("url", audio_url), ("language", "en")],
                timeout=timeout,
            )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 1:
                last_err = f"STT status {resp.status_code}"
                time.sleep(3)
                continue
            if resp.status_code >= 400:
                return None, f"STT status {resp.status_code}: {(resp.text or '')[:200]}"
            try:
                data = resp.json()
            except Exception:
                data = {}
            text = ((data or {}).get("text") or (data or {}).get("transcript") or "").strip()
            if not text:
                return None, "STT empty transcript"
            return text, None
        except Exception as e:
            last_err = f"STT {type(e).__name__}: {e}"
            if attempt < 1:
                time.sleep(3)
                continue
    return None, last_err or "STT failed"


def _grok_stt(mp4_url: str):
    """Transcribe audio via Grok STT URL mode. Returns transcript text or None."""
    text, _err = _grok_stt_from_url(mp4_url)
    return text


def resolve_x(url: str) -> dict:
    """Resolve an X post's CONTENT via Grok's Agent Tools API (x_search), using
    only XAI_API_KEY -- no X API bearer token. Returns the post's faithful
    content for the Sonnet summarizer; null-safe graceful degrade to partial on
    any failure (never fabricated). X-video posts get a visual/text summary here;
    spoken audio is captured for a separate STT replay pass."""
    model = os.environ.get("LEARN_X_MODEL", "grok-4.20-non-reasoning")
    prompt = (
        "Fetch the X (Twitter) post at this exact URL and report its FULL actual content "
        "faithfully: the author handle, the complete post text, any thread context, a brief "
        "description of any image or video media present, and the post date if shown. Do NOT "
        "editorialize or add outside information. If the post contains a video with spoken "
        "audio, end your reply with exactly: VIDEO_WITH_AUDIO: yes. If you cannot access "
        "or find the specific post, reply with exactly CANNOT_ACCESS.\nURL: " + url
    )
    try:
        data = _grok_responses_call(prompt, model)
    except Exception as e:
        logger.warning(f"[learn] Grok x_search failed {url[:60]}: {e}")
        return _partial("x", f"Grok x_search error: {e}")
    try:
        text, citations = _parse_grok_responses(data)
    except Exception as e:
        logger.warning(f"[learn] Grok response parse failed {url[:60]}: {e}")
        return _partial("x", f"Grok response parse error: {e}")
    if not text or text.strip().upper().startswith("CANNOT_ACCESS"):
        return _partial("x", "Grok could not access the post (CANNOT_ACCESS)")
    display_text = _strip_video_marker(text)
    has_video = _x_post_has_video(text)
    if has_video:
        # Grok VIDEO_WITH_AUDIO over-fires: it flags posts as video-with-audio
        # that have no NATIVE downloadable clip (linked/quoted video, GIF) or
        # exceed the duration cap. Probe with yt-dlp before committing the item
        # to the STT queue so those posts are not stranded there forever.
        native_ok, _dur, probe_reason = _probe_x_native_video(url)
        if native_ok:
            logger.info(f"[learn] x-video native audio present (queueing STT): {url[:60]}")
            return {
                "text": display_text[:20000], "kind": "x", "partial": True,
                "reason": "x-video audio pending STT replay",
                "content_date": None, "citations": citations, "needs_stt": True,
            }
        logger.info(f"[learn] x-video not natively fetchable ({probe_reason}); "
                    f"surfacing Grok summary instead: {url[:60]}")
        return {
            "text": display_text[:20000], "kind": "x", "partial": True,
            "reason": f"x-video not natively downloadable ({probe_reason or 'no native video'})",
            "content_date": None, "citations": citations, "needs_stt": False,
        }
    logger.info(f"[learn] x resolved via Grok x_search ({model}): {len(display_text)} chars {url[:60]}")
    return {"text": display_text[:20000], "kind": "x", "partial": False, "reason": "",
            "content_date": None, "citations": citations, "needs_stt": False}


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


def _call_claude_text(prompt: str, model: str, max_tokens: int = 2000, tools=None, timeout: int = None) -> str:
    import anthropic
    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")
    # Hard per-call timeout + single retry so a stuck call degrades fast instead
    # of blocking the whole run on the SDK's 10-minute default (x3 retries).
    client = anthropic.Anthropic(api_key=api_key).with_options(
        timeout=float(timeout or LEARN_ANTHROPIC_TIMEOUT), max_retries=1)
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
    return _call_claude_text(prompt, model, max_tokens=CLUSTER_MAX_TOKENS, timeout=LEARN_CLUSTER_TIMEOUT)


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
    summary, confidence, partial. An item with NO retrieved content is summarized
    from its title/sender only; an item with partial content (e.g. an X-video
    whose spoken transcript is pending STT) is summarized from what was retrieved,
    flagged with its pending reason. Never fabricated."""
    call_fn = call_fn or _call_claude_text
    subject = (item.get("subject") or "").strip()
    # Source URL for the digest/Asana link. Prefer the resolver's citation (X
    # posts -> Grok's canonical x.com url_citation); else the URL extracted from
    # the email (articles/YouTube/podcasts were fetched from it, and it is the
    # fallback for email-only / unresolved items). "" -> no link is emitted.
    url = (resolved.get("citations") or [None])[0] or item.get("url", "")
    kind = resolved.get("kind") or item.get("type") or "article"
    partial = resolved.get("partial", True)
    content = (resolved.get("text") or "").strip()

    base = {
        "title": subject or url, "type": kind, "url": url, "subject": subject,
        "content_date": resolved.get("content_date"), "tools": [], "specifics": [],
        "summary": "", "confidence": "low", "partial": partial,
        "content_retrieved": bool(content),
    }

    if not content:
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
    # Partial-but-retrieved (e.g. an X-video where Grok returned the visual/text
    # description but the spoken transcript is still pending STT replay): keep the
    # summary of what we DID get, and prefix the pending reason so the reader knows
    # a fuller transcript is coming from the replay pass.
    if partial and resolved.get("reason"):
        base["summary"] = f"[{resolved['reason']}] " + base["summary"]
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
        it["topic"] = topic
        it["priority"] = _apply_priority_floor(it, _default_priority(it))
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
        "Also assign each keeper a priority of High, Medium, or Low for Ken:\n"
        "- High: live and actionable for Ken's active builds, the Ariadne raise, or portfolio "
        "diligence (fundraising targets, psychedelic/biotech news, CLAUDE.md/loop discipline he "
        "is actively using, valuation/DD frameworks, the flight MCP for Travel).\n"
        "- High (financial/investment tooling): tools, platforms, or agents that DO financial "
        "deal analysis or its automation -- valuation/DCF/LBO modeling, due-diligence automation, "
        "investment-workflow automation, or deal-data pulls (Bloomberg/FactSet/PitchBook-class). "
        "This serves Zirmania family-office deal analysis AND Ken's own automation builds, and "
        "OUTRANKS the generic AI-tooling = Medium default. The discriminator is 'does the tool DO "
        "financial/investment/deal analysis or its automation?'; generic dev/coding tooling with "
        "no financial/deal-analysis purpose stays Medium.\n"
        "- Medium: relevant background, worth evaluating but not time-sensitive (most general AI "
        "tooling, conference notes, macro/IR references, secondary tools).\n"
        "- Low: tangential or evergreen (personal-product pages, market-doom commentary, "
        "unverifiable/promotional threads, content-not-retrieved items, fabricated-model items). "
        "If you cannot confidently assign, use Medium.\n"
        "Return ONLY a JSON object:\n"
        '{"keepers": [{"index": int, "why": "why this one is best for Ken", "bucket": "one of the buckets", '
        '"priority": "High|Medium|Low", "has_action": true/false, "action": "one line or empty"}], '
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
            it["topic"] = topic
            it["priority"] = _apply_priority_floor(it, _normalize_priority(k.get("priority")))
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
        it["topic"] = topic
        it["priority"] = _apply_priority_floor(it, _default_priority(it))
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
    current; annotate keeper['currency_note']. Gated by LEARN_CURRENCY_CHECK
    (fast-moving|off|all). The web call is NOT made for skipped clusters.

    The check assesses TWO INDEPENDENT axes -- (1) is the approach superseded,
    (2) can the item's SPECIFIC claim (a named repo/feature) be verified -- and
    keeps them separate. An unverifiable sub-claim is recorded as an
    informational caveat ONLY: it never frames the item as superseded/fabricated
    and never downgrades priority. Relevance is judged by curation, not by
    whether one sub-claim could be web-confirmed (the 2026-06-23 over-hedge
    fix). currency_check itself never touches keeper['priority']."""
    mode = mode or LEARN_CURRENCY_CHECK
    if mode == "off":
        return keeper
    if mode == "fast-moving" and not is_fast_moving(topic):
        return keeper  # slow-moving topic: judged on recency + content only

    call_fn = call_fn or _call_claude_web
    prompt = (
        "Assess the item below on TWO INDEPENDENT axes. Use web search. Be concise.\n"
        "1) current: is the general tool/approach still current best practice, or has it "
        "been superseded by something newer?\n"
        "2) verifiable: can you confirm the item's SPECIFIC factual claim (e.g. a named "
        "open-source repo, product, or feature actually exists)?\n"
        "Keep them SEPARATE: if you cannot verify a specific sub-claim but the underlying "
        "capability is real and plausible, set verifiable=false and current=true -- do NOT "
        "mark it superseded merely because a sub-claim is unverifiable.\n"
        'Return ONLY a JSON object: {"current": true/false, "verifiable": true/false, '
        '"note": "one line -- if superseded, name the newer canonical resource; if a '
        'sub-claim is unverifiable, say which; else empty"}.\n\n'
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
    verifiable = parsed.get("verifiable")
    current = parsed.get("current")
    note = (parsed.get("note") or "").strip()
    # Unverifiable sub-claim but NOT superseded -> caveat only; relevance intact.
    # (verifiable absent -> None -> falls through to the current-based branches,
    # preserving the original binary behavior.)
    if verifiable is False and current is not False:
        keeper["currency_note"] = (
            "specific claim unverified" + (": " + note if note else "")
            + " -- kept on relevance; underlying capability stands"
        )
    elif current is False:
        keeper["currency_note"] = "likely superseded" + (": " + note if note else "")
    elif current is True:
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
            if k.get("partial") and not k.get("content_retrieved"):
                parts.append('<div style="color:#b7791f;font-size:12px;">content not retrieved -- from title/sender only</div>')
            parts.append(f'<div style="margin-top:3px;">{_esc(k.get("summary"))}</div>')
            if k.get("why"):
                parts.append(f'<div style="margin-top:3px;color:#2f855a;"><b>Why this one:</b> {_esc(k.get("why"))}</div>')
            if k.get("currency_note"):
                parts.append(f'<div style="margin-top:3px;color:#c05621;"><b>Currency:</b> {_esc(k.get("currency_note"))}</div>')
            if k.get("has_action") and k.get("action"):
                parts.append(f'<div style="margin-top:3px;color:#553c9a;"><b>Action:</b> {_esc(k.get("action"))}</div>')
        # "Also saved" -- the cluster's other items, each linking to its own
        # source so superseded saves aren't lost (reasons are in the Skipped list).
        also = c.get("superseded") or []
        if also:
            links = []
            for s in also:
                t, u = _esc(s.get("title")), _esc(s.get("url"))
                links.append(f'<a href="{u}" style="color:#718096;">{t}</a>' if u else t)
            parts.append('<div style="margin-top:8px;font-size:12px;color:#718096;">'
                         f'Also saved ({len(also)}): ' + ", ".join(links) + '</div>')
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


def render_stt_replay_email(entries: list) -> str:
    """HTML body for supplementary X-video transcript email."""
    parts = ['<div style="font-family:sans-serif;max-width:720px;">']
    parts.append("<h2>X video transcripts now available</h2>")
    parts.append("<p>Transcripts for saved X video posts (replay pass):</p>")
    for e in entries:
        title = html.escape((e.get("title") or "Untitled").strip())
        parts.append(f"<h3>{title}</h3>")
        url = (e.get("source_url") or "").strip()
        if url:
            parts.append(f'<p><a href="{html.escape(url)}">{html.escape(url)}</a></p>')
        tx = (e.get("transcript") or "")[:2000]
        parts.append(f"<pre style='white-space:pre-wrap'>{html.escape(tx)}</pre>")
        if len(e.get("transcript") or "") > 2000:
            parts.append("<p><em>Transcript truncated in email; full text in replay status.</em></p>")
    parts.append("</div>")
    return "".join(parts)


def run_stt_replay(dry_run: bool = False, send_email: bool = True) -> dict:
    """Replay pending X-video STT entries: yt-dlp audio extract -> Grok STT.

    Sequential, idempotent. Failed entries stay pending with attempt counter;
    permanently failed after LEARN_STT_MAX_ATTEMPTS. dry_run lists work only."""
    started = datetime.now(timezone.utc)
    with _pending_stt_lock:
        data = _load_pending_stt()
        entries = [
            dict(e) for e in (data.get("entries") or [])
            if (e.get("status") or "") == "pending"
            and int(e.get("attempts") or 0) < LEARN_STT_MAX_ATTEMPTS
        ]
    outcomes = []
    successes = []
    email_error = None

    try:
        for entry in entries:
            url = entry.get("source_url") or ""
            entry_id = entry.get("id") or ""
            outcome = {
                "id": entry_id,
                "source_url": url,
                "title": entry.get("title"),
                "status": entry.get("status"),
            }
            if dry_run:
                outcome["dry_run"] = True
                outcomes.append(outcome)
                continue

            tmpdir = None
            try:
                audio_path, _duration, err, tmpdir = extract_x_post_audio(url)
                if err or not audio_path:
                    _record_stt_failure(entry_id, url, err or "no audio extracted")
                    with _pending_stt_lock:
                        fresh = _find_pending_entry(_load_pending_stt(), entry_id, url) or {}
                    outcome["status"] = fresh.get("status", entry.get("status"))
                    outcome["last_error"] = fresh.get("last_error", err)
                    outcomes.append(outcome)
                    continue

                text, stt_err = _grok_stt_from_file(audio_path)
                if stt_err or not text:
                    _record_stt_failure(entry_id, url, stt_err or "empty transcript")
                    with _pending_stt_lock:
                        fresh = _find_pending_entry(_load_pending_stt(), entry_id, url) or {}
                    outcome["status"] = fresh.get("status", entry.get("status"))
                    outcome["last_error"] = fresh.get("last_error", stt_err)
                    outcomes.append(outcome)
                    continue

                _mark_stt_success(entry_id, url, text)
                with _pending_stt_lock:
                    fresh = _find_pending_entry(_load_pending_stt(), entry_id, url) or {}
                outcome["status"] = "done"
                outcome["transcript_len"] = len(text)
                successes.append(dict(fresh))
                outcomes.append(outcome)
            finally:
                if tmpdir and os.path.isdir(tmpdir):
                    shutil.rmtree(tmpdir, ignore_errors=True)

        sent = False
        if successes and send_email and not dry_run:
            local_date = (started + timedelta(hours=ISRAEL_UTC_OFFSET_HOURS)).strftime("%a %d %b %Y")
            subject = f"[Sara] Read/Learn -- X video transcripts ({len(successes)}) -- {local_date}"
            body = render_stt_replay_email(successes)
            try:
                send_digest_email(subject, body)
                sent = True
            except Exception as e:
                email_error = str(e)
                logger.error(f"[learn] STT replay email failed (transcripts saved): {e}", exc_info=True)

        result = {
            "status": "ok",
            "dry_run": dry_run,
            "queued": len(entries),
            "succeeded": len(successes),
            "outcomes": outcomes,
            "sent": sent,
            "email_error": email_error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"[learn] STT replay failed: {e}", exc_info=True)
        result = {
            "status": "error",
            "error": str(e),
            "dry_run": dry_run,
            "queued": len(entries),
            "succeeded": len(successes),
            "outcomes": outcomes,
            "sent": False,
            "email_error": email_error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    write_stt_status(result)
    logger.info(
        f"[learn] STT replay done: queued={len(entries)} succeeded={len(successes)} "
        f"sent={result.get('sent')} email_error={email_error}"
    )
    return result


# ======================================================================
#  ASANA ROUTING (keepers with an action -> matching bucket section)
# ======================================================================


def _normalize_priority(value) -> str:
    """Map a model-provided priority to one of High/Medium/Low. Anything
    unknown, empty, or unparseable defaults to Medium (per the rubric)."""
    v = (value or "").strip().lower()
    if v.startswith("high"):
        return "High"
    if v.startswith("low"):
        return "Low"
    return "Medium"


def _default_priority(item: dict) -> str:
    """Priority for keepers NOT judged by the curation model (single-item
    clusters and the deterministic fallback): content-not-retrieved items
    are Low per the rubric; everything else defaults to Medium."""
    return "Low" if item.get("partial") else "Medium"


# Financial deal-analysis / investment-workflow automation tooling is Zirmania
# family-office core AND feeds Ken's own automation builds, so this class is
# HIGH priority -- it OUTRANKS the generic "AI tooling = Medium" default. The
# discriminator is "does the tool DO financial/investment/deal analysis or its
# automation?": a finance-domain signal AND a tooling/agent/automation signal
# must BOTH be present, so generic dev/coding tooling (no finance signal) stays
# Medium and finance NEWS with no tool (no tooling signal) is not elevated.
# Finance terms use word boundaries so "valuation" does NOT match "evaluation"
# (model-eval is everywhere in AI-tooling content).
_FINANCE_SIGNAL_RE = re.compile(
    r"\b("
    r"financial[ -]?(?:workflow|service|services|model|modeling|modelling|analy\w+)"
    r"|deal[ -]?(?:analysis|analytics|sourcing|data|flow|memo)"
    r"|due[ -]?diligence|diligence automation"
    r"|valuation|dcf|discounted cash flow|lbo|leveraged buyout"
    r"|investment[ -]?(?:workflow|analysis|analytics|research|committee|memo)"
    r"|underwriting|wall street|bloomberg|factset|pitchbook|capital iq|capiq|cap table"
    r")\b",
    re.I,
)
# Substring tooling signals (safe: no bare "ai"/"api"/"app" which collide with
# common words like email/capital/happen). "model" intentionally covers
# modeling; "automat" covers automation/automate/automated.
_TOOLING_SIGNALS = (
    "agent", "automat", "workflow", "tool", "platform", "software", "copilot",
    "assistant", "llm", "claude", "gpt", "mcp", "pipeline", "open-source",
    "open source", "framework", "model",
)


def _is_financial_workflow_tool(item: dict) -> bool:
    """True when an item is tooling/platform/agent for financial deal analysis
    or its automation (the HIGH-priority Zirmania/own-builds class). Requires a
    finance-domain signal AND a tooling signal across the same text route_section
    reads (topic + subject + title + summary + specifics)."""
    text = " ".join(str(item.get(k) or "") for k in
                    ("topic", "subject", "title", "summary", "specifics")).lower()
    if not _FINANCE_SIGNAL_RE.search(text):
        return False
    return any(sig in text for sig in _TOOLING_SIGNALS)


def _apply_priority_floor(item: dict, priority: str) -> str:
    """Floor financial deal-analysis / investment-workflow automation tooling to
    High, overriding a lower model/default verdict. The relevance is Ken's, not
    the model's, to downrank (the 2026-06-23 under-rating fix)."""
    if priority != "High" and _is_financial_workflow_tool(item):
        return "High"
    return priority


def route_section(keeper: dict) -> str:
    """Deterministic Asana section GID for a keeper: FIRST MATCH WINS over
    _ROUTING_RULES, keyed off the cluster topic + the keeper own subject/
    title/summary tags already produced. No LLM call. Falls back to
    General / Reference; never returns a manual-only section."""
    text = " ".join(str(keeper.get(k) or "") for k in
                    ("topic", "subject", "title", "summary", "specifics")).lower()
    for name, keywords in _ROUTING_RULES:
        if any(kw in text for kw in keywords):
            return LEARN_SECTION_GID[name]
    return LEARN_SECTION_GID[LEARN_DEFAULT_SECTION]


def _section_for_bucket(bucket: str) -> str:
    return ASANA_SECTIONS.get(_normalize_bucket(bucket), ASANA_SECTIONS[DEFAULT_BUCKET])


# "Video to watch" is a manual section the auto-router never targets. An
# important video keeper with no explicit action is filed here as a "watch"
# task so a clip worth Ken's time is never left in the digest email only. GID
# verified live against project 1215897524719950 on 2026-07-19.
VIDEO_TO_WATCH_SECTION_GID = "1215909647377755"
_VIDEO_KEEPER_TYPES = ("x", "youtube")


def _is_watchable_video(keeper: dict) -> bool:
    """A curated video keeper worth an explicit Asana 'watch' task even without
    a concrete action: an X or YouTube item at High/Medium priority. Low-priority
    (tangential/promotional) video keepers stay in the digest email only."""
    if (keeper.get("type") or "").lower() not in _VIDEO_KEEPER_TYPES:
        return False
    return _normalize_priority(keeper.get("priority")) in ("High", "Medium")


def create_triage_task(keeper: dict, watch: bool = False) -> str:
    """Create a task in 'Read/Learn Triage'. Action keepers route to their topic
    section deterministically (route_section); a watch=True video keeper routes
    to the manual 'Video to watch' section (title prefixed 'Watch: '). Priority
    is set at creation. Returns the task GID or '' on failure (never raises)."""
    try:
        notes_lines = [keeper.get("summary") or "", ""]
        if keeper.get("url"):  # source link; omitted when no URL could be extracted (never fabricated)
            notes_lines.append("Source: " + keeper["url"])
        notes_lines.append("Why this one: " + (keeper.get("why") or ""))
        if keeper.get("currency_note"):
            notes_lines.append("Currency: " + keeper["currency_note"])
        if keeper.get("action"):
            notes_lines.append("Action: " + keeper["action"])
        priority = _normalize_priority(keeper.get("priority"))
        name = keeper.get("title") or "Read/Learn item"
        if watch and not name.lower().startswith("watch:"):
            name = "Watch: " + name
        data = {
            "name": name[:250],
            "notes": "\n".join(notes_lines),
            "projects": [ASANA_PROJECT_GID_LEARN],
            # Priority set at creation (Part B). Guard: never the duplicate
            # workspace field 1206810235510187 -- only the project-bound field.
            "custom_fields": {LEARN_PRIORITY_FIELD_GID: LEARN_PRIORITY_OPTION_GID[priority]},
        }
        ws = os.environ.get("ASANA_WORKSPACE_GID", "")
        if ws:
            data["workspace"] = ws
        task = asana_client.asana_request("POST", "/tasks", data)
        task_gid = (task or {}).get("gid")
        if not task_gid:
            return ""
        section_gid = VIDEO_TO_WATCH_SECTION_GID if watch else route_section(keeper)
        asana_client.asana_request("POST", f"/sections/{section_gid}/addTask", {"task": task_gid})
        logger.info(f"[learn] Asana task {task_gid} -> section {section_gid} (priority={priority}, watch={watch})")
        return task_gid
    except Exception as e:
        logger.error(f"[learn] Asana task creation failed: {e}")
        return ""


# ======================================================================
#  MAIL FETCH + POST-PROCESS (mark read, move to Processed)
# ======================================================================


def fetch_unread(folder_id: str = LEARN_FOLDER_ID, processed_ids: set = None, backlog: bool = False,
                 limit: int = None) -> list:
    """Fetch messages from the read/learn folder by ID. Normal runs fetch items
    received within the trailing LEARN_LOOKBACK_DAYS window REGARDLESS of
    read/unread state, and skip IDs already in learn_processed.json. (Saved items
    are typically forwarded to self and arrive READ, so an unread-only filter
    silently skipped the whole queue -- dedup is the processed-ID store + the
    move-to-Processed step, not the read flag.) A backlog run (backlog=True) is
    the manual 'reprocess everything' switch: it fetches the ENTIRE folder (no
    window) AND ignores the processed-ID store, so already-seen and older items
    are re-processed from scratch. We do NOT chunk -- the clustering pass must see
    the whole set at once. limit (if set) caps the number returned (diagnostic knob)."""
    processed_ids = processed_ids or set()
    base = f"{eps.MS_GRAPH_BASE}/users/{MAILBOX}/mailFolders/{folder_id}/messages"
    params = {"$select": "id,subject,from,receivedDateTime,body,webLink", "$top": "50"}
    if not backlog:
        # Trailing-window filter (read/unread agnostic); backlog=True omits it.
        cutoff = datetime.now(timezone.utc) - timedelta(days=LEARN_LOOKBACK_DAYS)
        params["$filter"] = f"receivedDateTime ge {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    url = base
    messages, pages = [], 0
    while url and pages < 25:
        data = eps.graph_get(url, params=params if url == base else None) or {}
        for m in (data.get("value") or []):
            if not backlog and m.get("id") in processed_ids:
                continue  # backlog=True re-processes already-seen items
            messages.append(m)
        url = data.get("@odata.nextLink")
        pages += 1
        if limit and len(messages) >= limit:
            break
    if limit:
        messages = messages[:limit]
    logger.info(f"[learn] Fetched {len(messages)} messages (backlog={backlog}, pages={pages}, limit={limit})")
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
            data = json.load(f)
    except FileNotFoundError:
        data = {"status": "no_runs", "message": "No learn-digest run has completed yet."}
    except Exception as e:
        data = {"status": "error", "error": f"could not read status: {e}"}
    # Live heartbeat of any in-flight run (shared module state, single worker).
    data["live_progress"] = dict(_LEARN_PROGRESS)
    return data


# ======================================================================
#  RUN ORCHESTRATION
# ======================================================================

# Live progress (heartbeat) + bounded concurrency for the per-item phases.
# Single gunicorn worker -> these module globals are shared with the
# /learn/status request thread, so progress is visible mid-run.
_PROGRESS_LOCK = _threading.Lock()
_LEARN_PROGRESS = {"phase": "idle", "done": 0, "total": 0, "last": "", "run_id": None, "updated_at": None}


def _set_progress(**kw):
    with _PROGRESS_LOCK:
        _LEARN_PROGRESS.update(kw)
        _LEARN_PROGRESS["updated_at"] = datetime.now(timezone.utc).isoformat()


def _bump_progress(last: str):
    with _PROGRESS_LOCK:
        _LEARN_PROGRESS["done"] = _LEARN_PROGRESS.get("done", 0) + 1
        _LEARN_PROGRESS["last"] = (last or "")[:120]
        _LEARN_PROGRESS["updated_at"] = datetime.now(timezone.utc).isoformat()


def _run_concurrent(items: list, fn, workers: int = None) -> list:
    """Run fn(index, item) over items with a bounded thread pool, preserving
    input order. I/O-bound calls (Grok/Anthropic/HTTP) release the GIL, so
    threads give real speedup. A crash in one item yields None for that slot
    (caller filters) and never fails the run. workers<=1 runs sequentially."""
    workers = workers or LEARN_CONCURRENCY
    n = len(items)
    results = [None] * n
    if workers <= 1 or n <= 1:
        for i, it in enumerate(items):
            try:
                results[i] = fn(i, it)
            except Exception as e:
                logger.error(f"[learn] concurrent item {i} crashed: {e}")
        return results
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, i, it): i for i, it in enumerate(items)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                logger.error(f"[learn] concurrent item {i} crashed: {e}")
    return results


def _learn_run_inner(dry_run: bool, backlog: bool, limit: int = None) -> dict:
    """The pipeline, run with the lock held. Mirrors the spec's 11-step flow."""
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc)
    processed_ids = _load_processed_ids()
    _set_progress(phase="fetch", done=0, total=0, last="", run_id=run_id)

    messages = fetch_unread(LEARN_FOLDER_ID, processed_ids, backlog, limit=limit)
    if not messages:
        _set_progress(phase="done", done=0, total=0, last="no unread items")
        result = {"status": "ok", "run_id": run_id, "sent": False, "reason": "no unread items",
                  "clusters": 0, "keepers": 0, "skipped": 0, "dry_run": dry_run}
        write_status(result)
        return result

    # Phase 1: resolve + summarize each message's link (concurrent, bounded).
    # I/O-bound (Grok/Jina/Anthropic) -> threads turn ~80 sequential calls into
    # minutes; each item degrades to partial on failure, never fails the run.
    _set_progress(phase="resolve+summarize", done=0, total=len(messages))

    def _resolve_one(i, msg):
        item = build_item(msg)
        resolved = resolve_item(item) if item.get("url") else _partial(item.get("type"), "no link found in message")
        if resolved.get("needs_stt"):
            _capture_pending_stt({
                "source_url": item.get("url") or (resolved.get("citations") or [""])[0],
                "title": item.get("subject") or "",
                "date": item.get("received") or "",
                "processed_folder_location": PROCESSED_SUBFOLDER_NAME,
            })
        summ = summarize_item(item, resolved)
        summ["message_id"] = item.get("message_id")
        _bump_progress(f"[{item.get('type')}] {(item.get('url') or item.get('subject') or '')[:50]}")
        return summ

    summaries = [s for s in _run_concurrent(messages, _resolve_one) if s]

    # Phase 2: cluster the WHOLE batch at once (single call -- consolidation
    # needs the full set; deliberately NOT parallelized).
    _set_progress(phase="cluster", done=0, total=1, last="clustering whole batch")
    clusters = cluster_items(summaries)

    # Phase 3: curate each cluster + currency-check its keepers (concurrent per
    # cluster -- this was the silent ~20-min window before).
    _set_progress(phase="curate+currency", done=0, total=len(clusters))

    def _curate_one(i, c):
        cur = curate_cluster(c)
        cur["keepers"] = [currency_check(k, cur.get("topic")) for k in cur.get("keepers", [])]
        _bump_progress(f"curated: {(cur.get('topic') or '')[:50]}")
        return cur

    curated = [c for c in _run_concurrent(clusters, _curate_one) if c]

    total_keepers = sum(len(c.get("keepers") or []) for c in curated)
    total_skipped = sum(len(c.get("superseded") or []) for c in curated)

    local_date = (started + timedelta(hours=ISRAEL_UTC_OFFSET_HOURS)).strftime("%a %d %b %Y")
    subject = f"[Sara] Read/Learn digest -- {local_date}"
    body = render_digest_html(curated)
    _set_progress(phase=("send+postprocess" if not dry_run else "render"),
                  done=len(curated), total=len(curated), last=subject)

    sent, tasks_created = False, 0
    if dry_run:
        logger.info(f"[learn] [dry-run] {len(messages)} items -> {len(clusters)} clusters, "
                    f"{total_keepers} keepers, {total_skipped} skipped (no email/tasks/move)")
    else:
        send_digest_email(subject, body)
        sent = True
        # Asana tasks for keepers: action items keep topic routing; important
        # video keepers with no explicit action become explicit "watch" tasks in
        # the manual 'Video to watch' section so a clip worth Ken's time is never
        # left in the digest email only.
        for c in curated:
            for k in c.get("keepers", []):
                if k.get("has_action"):
                    if create_triage_task(k):
                        tasks_created += 1
                elif _is_watchable_video(k):
                    if create_triage_task(k, watch=True):
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
    _set_progress(phase="done", last=f"{total_keepers} keepers / {total_skipped} skipped")
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
