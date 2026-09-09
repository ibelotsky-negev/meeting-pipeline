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


# ======================================================================
#  TEXT NORMALIZATION
# ======================================================================
# EVERY replacement below is length-preserving on purpose. The prefilter's hit
# offsets, the Q&A boundary offset and quote verification all index the same
# normalized string, so a normalizer that changed length would silently
# misalign zone labels. The one multi-char case (an ellipsis) is deliberately
# NOT folded for that reason.
_CHAR_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2011": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u200b": " ",
}
_FOLD_TABLE = str.maketrans(_CHAR_FOLD)


def normalize_text(text: str) -> str:
    """-> text with smart quotes, dashes and exotic spaces folded to ASCII.

    LENGTH-PRESERVING. See _CHAR_FOLD."""
    return (text or "").translate(_FOLD_TABLE)


def term_pattern(term: str) -> re.Pattern:
    """-> a compiled, case-insensitive, word-boundaried pattern for one YAML term.

    Per the keyword file's own matching notes:
      - case-insensitive
      - \\b on every term, which is what stops ALS matching inside "also" and
        tau inside "taught"
      - a trailing '*' is a prefix stem (discontinu* -> discontinued/-ation)
    Interior whitespace becomes \\s+ so a multi-word term still matches when the
    phrase straddles a newline, which happens in the newline-delimited format.
    """
    term = normalize_text(term).strip()
    is_stem = term.endswith("*")
    if is_stem:
        term = term[:-1].strip()
    body = r"\s+".join(re.escape(part) for part in term.split())
    suffix = r"\w*" if is_stem else r"\b"
    return re.compile(r"\b" + body + suffix, re.IGNORECASE)


def compile_group(kw: dict, group: str):
    """-> [(term, pattern)] for one YAML group. Non-string entries are skipped
    so a malformed line degrades one term, not the whole group."""
    out = []
    for entry in (kw.get(group) or []):
        if not isinstance(entry, str) or not entry.strip():
            continue
        out.append((entry.strip(), term_pattern(entry)))
    return out


def mask_spans(text: str, patterns) -> str:
    """-> text with every match of every pattern replaced by '#' of EQUAL length.

    Equal length is the whole point: it neutralizes a phrase for term matching
    while keeping every downstream character offset valid.

    This is how the exclusion list does its work. Masking 'PD-1' means the 'PD'
    term regex can never match inside it, which is a stronger guarantee than
    checking for a blocker after the fact."""
    masked = text or ""
    for pattern in patterns:
        masked = pattern.sub(lambda m: "#" * len(m.group(0)), masked)
    return masked


_SAFE_HARBOR_CUE = re.compile(
    r"forward[-\s]looking statements?|safe harbor|private securities litigation",
    re.IGNORECASE)
CNS_SAFE_HARBOR_SPAN = int(os.environ.get("CNS_SAFE_HARBOR_SPAN", "4000"))


def mask_safe_harbor(text: str, span: int = None) -> str:
    """-> text with each safe-harbor disclaimer masked, EQUAL LENGTH.

    Ken's prompt says to ignore the safe-harbor section; masking enforces it
    mechanically for the KEYWORD layer rather than trusting an instruction.

    Bounded to `span` characters from each cue because the disclaimer has no
    reliable end marker. Frequently a no-op -- AbbVie's transcript contains zero
    occurrences of 'forward-looking' -- so this must never be load-bearing."""
    span = CNS_SAFE_HARBOR_SPAN if span is None else int(span)
    text = text or ""
    out = text
    for match in _SAFE_HARBOR_CUE.finditer(text):
        start = match.start()
        end = min(len(text), start + span)
        out = out[:start] + ("#" * (end - start)) + out[end:]
    return out


# ======================================================================
#  PREFILTER
# ======================================================================
# The keyword YAML asks for co-occurrence "within the same paragraph". FMP
# transcripts have NO paragraphs -- format A is one unbroken 56,663-character
# line with zero newlines, and format B's speaker turns reach 25,525 characters
# (Roche has a single 25K turn). Treating a turn as a paragraph would let a BD
# term co-occur with a domain term 25,000 characters away, which is not a gate.
#
# So co-occurrence is CHARACTER PROXIMITY: within CNS_GATE_WINDOW characters.
# This is a deliberate, documented deviation from the YAML's wording.
CNS_GATE_WINDOW = int(os.environ.get("CNS_GATE_WINDOW", "600"))

# The YAML's `ambiguity_rules` block is human-readable PROSE, so each rule is
# implemented explicitly below and asserted by a test. Adding a rule to the
# YAML does NOT auto-apply it -- that needs a code change here. CLAUDE.md
# records this.
_PD_BLOCKERS = re.compile(r"pharmacodynamic|PK\s*/\s*PD|\bPD-?L?1\b", re.IGNORECASE)
_NEURO_CUE = re.compile(
    r"\b(neuro\w*|brain|CNS|central nervous|Parkinson\w*|Alzheimer\w*|epilep\w*|"
    r"seizure|dementia|psychiatr\w*|cognitive|multiple system atrophy|ataxia)\b",
    re.IGNORECASE)
_EPILEPSY_CUE = re.compile(r"\b(epilep\w*|seizure|Dravet|Lennox|convulsi\w*)\b",
                           re.IGNORECASE)

# Terms whose bare match is not trusted. Each maps to a gate function below.
_GATED_TERMS = ("PD", "MSA", "MDS", "AES", "CNS")


class PrefilterResult:
    """Hits from the keyword layer. Every offset indexes the NORMALIZED text,
    which is the same length as the raw text, so offsets are interchangeable
    with the Q&A boundary and with verification offsets."""

    __slots__ = ("domain_hits", "bd_hits", "standalone_hits",
                 "high_signal_hits", "screen", "masked")

    def __init__(self, domain_hits, bd_hits, standalone_hits,
                 high_signal_hits, screen, masked):
        self.domain_hits = domain_hits
        self.bd_hits = bd_hits
        self.standalone_hits = standalone_hits
        self.high_signal_hits = high_signal_hits
        self.screen = screen
        self.masked = masked

    def as_summary(self) -> dict:
        """-> a small JSON-safe dict for the status file and the digest."""
        return {
            "screen": self.screen,
            "domain_terms": sorted({t for t, _ in self.domain_hits}),
            "bd_terms": sorted({t for t, _ in self.bd_hits}),
            "standalone_terms": sorted({t for t, _ in self.standalone_hits}),
            "high_signal_terms": sorted({t for t, _ in self.high_signal_hits}),
        }


def _near(offset: int, others, window: int) -> bool:
    """-> True when any offset in `others` is within `window` characters."""
    return any(abs(offset - other) <= window for other in others)


def _gate_ok(term: str, cue_text: str, offset: int, parkinsons_offsets, window: int) -> bool:
    """-> True when a gated term's match should count, per the keyword file's
    ambiguity_rules.

    `cue_text` is the normalized but UNMASKED text. Cue searching must not run
    on the masked string: masking erases exclusion phrases so that keyword
    TERMS cannot match inside them, but a gate needs to SEE those phrases to do
    its job. "Consumer Health" is an exclusion entry, so reading masked text
    made the CNS gate blind to the exact case it exists to block. Offsets are
    interchangeable between the two strings because every masking step is
    length-preserving."""
    lo = max(0, offset - window)
    hi = min(len(cue_text), offset + window)
    context = cue_text[lo:hi]
    if term == "PD":
        if _PD_BLOCKERS.search(context):
            return False
        return _near(offset, parkinsons_offsets, window)
    if term in ("MSA", "MDS"):
        # master services agreement / myelodysplastic syndromes
        return bool(_NEURO_CUE.search(context))
    if term == "AES":
        # "adverse events, serious" unless an epilepsy context is nearby
        return bool(_EPILEPSY_CUE.search(context))
    if term == "CNS":
        # consumer-nutrition segment at a few issuers
        return not re.search(r"consumer (nutrition|health)", context, re.IGNORECASE)
    return True


def _collect(groups, masked, cue_text, parkinsons_offsets, window):
    """-> [(term, offset)] for every match in `groups` that survives its gate.

    Term matching (`pattern.finditer`) runs on the MASKED text -- exclusion
    phrases must stay neutralized so a term cannot match inside one. Gate cue
    lookups run on `cue_text`, the unmasked normalized text, since a gate needs
    to see the very phrases masking erased. See `_gate_ok`."""
    hits = []
    for term, pattern in groups:
        gated = term in _GATED_TERMS
        for match in pattern.finditer(masked):
            offset = match.start()
            if gated and not _gate_ok(term, cue_text, offset, parkinsons_offsets, window):
                continue
            hits.append((term, offset))
    return hits


def prefilter(text: str, kw: dict = None, window: int = None) -> PrefilterResult:
    """-> PrefilterResult.

    Order matters:
      1. normalize (length-preserving)
      2. mask the safe harbor, then the exclusion list -- both equal-length, so
         offsets stay valid and an excluded phrase cannot yield a term hit
      3. match the domain groups, applying the ambiguity gates
      4. match BD terms, keeping only those with a domain term within `window`
      5. match the standalone groups
      6. match the high-signal rare terms for the independent recall check

    `screen` is True when the DOMAIN gate opened at all. That is the skip gate:
    a transcript with no domain term never reaches the model.
    """
    kw = load_keywords() if kw is None else kw
    window = CNS_GATE_WINDOW if window is None else int(window)

    normalized = normalize_text(text)
    masked = mask_safe_harbor(normalized)
    masked = mask_spans(masked, [p for _, p in compile_group(kw, "exclusions")])

    # Excludes "PD" itself: it is a member of PARKINSONS_GROUP but is the very
    # term being gated here. Without this exclusion, every "PD" match would
    # satisfy its own proximity check (distance 0 to itself), which defeats
    # the ambiguity rule entirely -- "a parkinsons/synuclein term" means a
    # DIFFERENT term than the ambiguous "PD" acronym. Computed from the MASKED
    # text on purpose: these come from term matching, and a Parkinson's term
    # sitting inside an excluded phrase should not count.
    parkinsons_offsets = [
        m.start()
        for term, pattern in compile_group(kw, PARKINSONS_GROUP)
        for m in pattern.finditer(masked)
        if term != "PD"
    ]

    domain_groups = []
    for group in DOMAIN_GROUPS:
        domain_groups.extend(compile_group(kw, group))
    # A named CNS asset is inherently in-domain -- see the group-role comment.
    domain_groups.extend(compile_group(kw, ASSET_GROUP))
    domain_hits = _collect(domain_groups, masked, normalized, parkinsons_offsets, window)
    domain_offsets = [offset for _, offset in domain_hits]

    # Generic BD language appears on nearly every earnings call and is NOISE by
    # itself. It counts only alongside domain vocabulary. This is the single
    # most valuable thing the screen can find, and the single easiest thing to
    # flood the digest with if the gate is loose.
    bd_hits = [
        (term, offset)
        for term, offset in _collect(compile_group(kw, BD_GROUP), masked, normalized,
                                     parkinsons_offsets, window)
        if _near(offset, domain_offsets, window)
    ]

    standalone_groups = []
    for group in STANDALONE_GROUPS:
        standalone_groups.extend(compile_group(kw, group))
    standalone_hits = _collect(standalone_groups, masked, normalized, parkinsons_offsets, window)

    high_signal_hits = _collect(compile_group(kw, "high_signal_rare_terms"),
                                masked, normalized, parkinsons_offsets, window)

    return PrefilterResult(
        domain_hits=domain_hits,
        bd_hits=bd_hits,
        standalone_hits=standalone_hits,
        high_signal_hits=high_signal_hits,
        screen=bool(domain_hits),
        masked=masked,
    )
