#!/usr/bin/env python3
"""
cns-screen -- Sara module (CNS / rare-neuro earnings-call screen).

Reads large-pharma earnings-call transcripts and flags passages relevant to
Ariadne Bio's agenda: appetite for external CNS assets (BD_INTENT), competitor
CNS pipeline movement (PIPELINE_MOVE), and therapeutic-area posture shifts
(TA_STRATEGY). It is a FILTER, not a summarizer -- most transcripts contain
nothing relevant and saying so is the correct answer.

What it does each run:
  1. Build/refresh the universe (cns_fmp): three pharma/biotech industry screens
     above $1B, US listings only, unioned with a force-include roster of ADRs
     the screener misses.
  2. Discover new transcripts from the global latest feed; reconcile weekly with
     a per-symbol sweep.
  3. Fetch and STORE each new transcript. Storage is required, not incidental:
     quote verification, the season wrap-up and replay all read it back.
  4. Prepare: mask the safe harbor, locate the Q&A boundary, run the keyword
     prefilter with proximity gating.
  5. Screen with Opus 5 under Ken's prompt and a JSON schema. A transcript with
     zero domain hits is recorded as no_cns_content and never sent to the model.
  6. VERIFY every quote appears verbatim in the stored transcript. A finding
     whose quote cannot be located is DROPPED -- never reported.
  7. Email a digest, only on days with new transcripts.

Two structural facts about FMP transcripts drive the design, both verified
across twelve real transcripts on 2026-09-09:
  - Neither transcript format has paragraphs, and format B's speaker turns run
    to 25,525 characters. So the keyword YAML's "same paragraph" co-occurrence
    rule is implemented as a CHARACTER PROXIMITY window instead.
  - Both formats contain "question-and-answer" and "Q&A" inside the operator's
    OPENING boilerplate (AbbVie at char 176 of 56663). So Q&A detection is
    position-banded, and when nothing lands in the band the zone is UNKNOWN
    rather than guessed.

Reuses Sara infra: cns_fmp for data, the Weekly-Pulse atomic O_CREAT|O_EXCL
lock pattern, email_pipeline_sync's Graph app-only send, the single-worker
scheduler topology.

Usage:
    python cns_screen.py                        # daily scan, dry-run
    python cns_screen.py --live --days 3        # real run
    python cns_screen.py --season Q2-2026       # season wrap-up

ASCII-only comments and non-user-facing strings.
Author: Negev Labs
"""

import os
import re
import json
import time
import logging
import argparse
import threading as _threading
from datetime import datetime, timezone, date

import yaml

import cns_fmp

logger = logging.getLogger("cns-screen")

_HERE = os.path.dirname(os.path.abspath(__file__))

KEYWORDS_PATH = os.environ.get("CNS_KEYWORDS_PATH", os.path.join(_HERE, "cns_screen_keywords.yaml"))
PROMPT_PATH = os.environ.get("CNS_PROMPT_PATH", os.path.join(_HERE, "cns_screen_prompt.md"))
UNIVERSE_CONFIG_PATH = os.environ.get("CNS_UNIVERSE_CONFIG_PATH",
                                      os.path.join(_HERE, "cns_screen_universe.yaml"))

# ======================================================================
#  KEYWORD GROUP ROLES
# ======================================================================
# The YAML is a flat set of named lists; these constants assign each list its
# gating role. Two assignments are judgment calls worth stating:
#
#   watchlist_assets  -> DOMAIN. An asset name like Cobenfy or trofinetide is
#                        inherently CNS, so it opens the domain gate by itself.
#   watchlist_companies -> STANDALONE signal, but NOT domain. Pharma companies
#                        name each other on every call ("unlike Pfizer, we..."),
#                        so a bare company name is not evidence of CNS content.
DOMAIN_GROUPS = (
    "domain_general",
    "domain_parkinsons_and_synuclein",
    "domain_neuropsych_symptoms",
    "domain_serotonergic_and_neuroplastogen",
    "domain_rare_neuro",
    "domain_cns_modalities",
)
ASSET_GROUP = "watchlist_assets"
BD_GROUP = "bd_intent"
STANDALONE_GROUPS = (
    "pipeline_events",
    "conference_mentions",
    "ta_strategy",
    "watchlist_companies",
)
PARKINSONS_GROUP = "domain_parkinsons_and_synuclein"

_keywords_cache = {}
_prompt_cache = {}
_universe_config_cache = {}


def load_keywords(path: str = None) -> dict:
    """-> the parsed keyword YAML, cached per path.

    Ken tunes this file directly; the code must never need editing to change a
    term. Raises on a malformed file: a silently-empty keyword set would make
    every transcript look CNS-free."""
    path = path or KEYWORDS_PATH
    if path not in _keywords_cache:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict) or not data:
            raise ValueError(f"keyword file {path} did not parse to a non-empty mapping")
        _keywords_cache[path] = data
    return _keywords_cache[path]


def load_prompt(path: str = None) -> str:
    """-> the system prompt only, extracted from Ken's markdown.

    The file wraps the prompt in horizontal rules: a paste instruction above the
    first `---`, the prompt between the rules, and usage notes (message format,
    verification advice, cost estimates) below the second. Sending the whole
    file would feed the model instructions addressed to the implementer."""
    path = path or PROMPT_PATH
    if path not in _prompt_cache:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()
        parts = re.split(r"(?m)^---[ \t]*$", raw)
        if len(parts) < 3:
            raise ValueError(f"prompt file {path} has no ----delimited prompt body")
        body = parts[1].strip()
        if "BD_INTENT" not in body:
            raise ValueError(f"prompt body extracted from {path} looks wrong "
                             "(no BD_INTENT); check the --- rules")
        _prompt_cache[path] = body
    return _prompt_cache[path]


def load_universe_config(path: str = None) -> dict:
    """-> {"force_include": [SYMBOL, ...], "entity_only": [name, ...]}

    force_include are screened. entity_only are names the model should
    recognize inside someone else's transcript but that hold no call of their
    own -- they must never reach cns_fmp.fetch_transcript."""
    path = path or UNIVERSE_CONFIG_PATH
    if path not in _universe_config_cache:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        _universe_config_cache[path] = {
            "force_include": [str(s).strip().upper()
                              for s in (data.get("force_include") or []) if str(s).strip()],
            "entity_only": [str(s).strip()
                            for s in (data.get("entity_only") or []) if str(s).strip()],
        }
    return _universe_config_cache[path]
