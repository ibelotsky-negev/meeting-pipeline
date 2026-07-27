#!/usr/bin/env python3
"""
biweekly-business-update -- Sara module

Distills the last ~2 weeks of Weekly Pulse archives into a team-facing BUSINESS
status update -- significant business movements only (fundraising, partnerships,
regulatory/clinical milestones in plain language, key risks/needs), with the
technical and scientific detail stripped out. Emails Ken a polished HTML brief
he reviews and forwards to the Negev Labs team.

Usage:
    python biweekly_business_update.py                          # trailing 14 days
    python biweekly_business_update.py --start 2026-06-01 --end 2026-06-14
    python biweekly_business_update.py --dry-run               # compose, no email
    python biweekly_business_update.py --force                 # ignore cadence gate

Reads the pulse archives written by app.py (weekly pulse) at $DATA_DIR/pulse.
Shares Graph auth + HTTP helpers with email_pipeline_sync.py. No new deps.

Author: Negev Labs
"""

import os
import re
import json
import html
import logging
import argparse
import uuid
from datetime import datetime, timedelta, timezone

# Shared Graph auth + HTTP helpers (retry, token cache)
import email_pipeline_sync as eps

logger = logging.getLogger("biweekly-business-update")

# ======================================================================
#  CONFIG
# ======================================================================

# Match app.py: the weekly pulse archives live at $DATA_DIR/pulse (Railway
# volume = /data). Resolved the same way so we read exactly what the pulse wrote.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
PULSE_ARCHIVE_DIR = os.path.join(DATA_DIR, "pulse")

# Distilled update goes to Ken only; he forwards to the team. Config-only to
# switch to a team distribution list later.
BIWEEKLY_RECIPIENTS = [
    r.strip() for r in os.environ.get("BIWEEKLY_RECIPIENTS", "bk@negevlabs.com").split(",") if r.strip()
]

# Distillation is a rewrite/translation pass -- quality matters more than cost,
# so use the pulse extract tier (sonnet) rather than haiku.
DISTILL_MODEL = os.environ.get("BIWEEKLY_MODEL", "claude-sonnet-4-6")

# Every-other-week cadence gate. The scheduled job runs every Monday; it fires
# only on Mondays whose ISO week number has the configured parity (default odd =
# 1), which is deterministic and -- unlike a "days since last send" gate -- is
# NOT poisoned by a manual/backfill send landing mid-cycle (that bug skipped the
# 2026-06-29 run because a Tue backfill left only ~12.5 days before the Monday).
# A small floor suppresses a duplicate when a send already went out within the
# last few days (manual catch-up, or the rare year-boundary W53->W1 where two
# odd weeks fall back-to-back).
BIWEEKLY_ISO_PARITY = int(os.environ.get("BIWEEKLY_ISO_PARITY", "1"))  # 1=odd weeks, 0=even
BIWEEKLY_MIN_GAP_DAYS = int(os.environ.get("BIWEEKLY_MIN_GAP_DAYS", "10"))

DEFAULT_WINDOW_DAYS = 14

# Last-run status. Lives on the Railway volume when available so scheduled-run
# outcomes are inspectable via /biweekly/status without log access.
STATUS_PATH = (
    "/data/biweekly_business_update_status.json"
    if os.path.isdir("/data")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "biweekly_business_update_status.json")
)

# Rolling archive of past updates so each new update can be made INCREMENTAL --
# report only what changed since the previous update, rather than re-narrating
# still-true items (a rename, a closed financing) that prior updates already
# covered. Lives on the volume next to the status file.
HISTORY_PATH = (
    "/data/biweekly_history.json"
    if os.path.isdir("/data")
    else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "biweekly_history.json")
)
HISTORY_KEEP = int(os.environ.get("BIWEEKLY_HISTORY_KEEP", "12"))


# ======================================================================
#  TIME HELPERS
# ======================================================================


def _parse_iso(value):
    """ISO8601 string (the pulse archive format) -> aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ======================================================================
#  PULSE ARCHIVE SELECTION
# ======================================================================


def select_pulses(start_dt: datetime, end_dt: datetime, archive_dir: str = None) -> list:
    """Pulse archives whose [period_start, period_end] overlaps the window
    [start_dt, end_dt], sorted oldest -> newest. Each entry:
    {filename, period_start, period_end, generated_at, report_markdown,
    signals, stats}. Unreadable archives are skipped, not fatal."""
    archive_dir = archive_dir or PULSE_ARCHIVE_DIR
    out = []
    if not os.path.isdir(archive_dir):
        return out
    for fname in os.listdir(archive_dir):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(archive_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"Skipping unreadable pulse archive {fname}: {e}")
            continue
        ps = _parse_iso(data.get("period_start"))
        pe = _parse_iso(data.get("period_end"))
        if ps is None or pe is None:
            continue
        # Overlap: periods touch the window iff it starts on/before the window
        # ends AND ends on/after the window starts.
        if pe < start_dt or ps > end_dt:
            continue
        out.append({
            "filename": fname,
            "period_start": ps,
            "period_end": pe,
            "generated_at": data.get("generated_at"),
            "report_markdown": data.get("report_markdown") or "",
            "signals": data.get("signals") or {},
            "stats": data.get("stats") or {},
        })
    out.sort(key=lambda p: p["period_start"])
    return out


# ======================================================================
#  DISTILLATION (the core logic -- one Claude rewrite pass)
# ======================================================================

# ASCII-only per repo rules. This prompt is the heart of the feature: it turns
# the dense, technical weekly pulse into a plain-language business update written
# in Ken's own voice (first person, narrative, numbered moves -- see the email
# style Ken approved).
DISTILL_SYSTEM_PROMPT = """You are writing as Ken Belotsky, lead of Palomar Labs (formerly Negev Labs), sending a periodic business status update to the internal Palomar Labs team. Write the EMAIL BODY in Ken's voice -- first person plural ("we"), confident, direct, specific. This is a finished email the team reads as-is.

Distill the weekly pulse report(s) provided into ONLY the significant business moves of the period, told as a short narrative. Strip all technical and scientific detail.

Honor the STANDING CORRECTIONS FROM KEN appended at the end of these instructions: they are authoritative and override anything in the pulse that conflicts with them.

KEEP (business-significant): fundraising and capital moves; partnerships and BD; regulatory and clinical MILESTONES stated as business outcomes (e.g. "FDA gave positive feedback endorsing our proposed trial design" -- not endpoint names or study mechanics); major operational decisions (program kills, hires, partner appointments); the most important risks or asks for the team.

STRIP (never include -- not even in passing, not even inside a narrative sentence). If tempted to include a scientific term, replace it with its plain business meaning or drop it:
- Manufacturing / CMC entirely: GMP, batch production, capsule/tablet manufacturing, formulation, assay, purity, yield, stability. If drug supply matters, say only "clinical drug supply is on track" -- never mention batches or assay results.
- Clinical/scientific endpoint or scale names and acronyms (e.g. LARS, CGIC) and primary/secondary endpoint mechanics. For FDA or regulatory feedback, state ONLY the business outcome ("FDA gave positive feedback endorsing our proposed trial design") -- never name the endpoints.
- Toxicology/histopathology, dosing, pharmacology.
- Document/filing minutiae (IMPD, QP release, protocol version numbers) and specific vendor/supplier names unless the vendor IS the business story.

RULES:
- Merge items that appear in more than one pulse; report the NET state over the whole period.
- INCREMENTAL: this is a recurring update. If an "ALREADY REPORTED IN THE PREVIOUS UPDATE" section is provided, treat everything in it as already communicated to the team -- do NOT re-report it. Surface a topic only if there is genuinely NEW progress or a material change this period, framed as the new development. A one-time announcement from an earlier update (a company rename, a closed financing round) is NOT news again -- omit it unless there is a concrete new development on it this period.
- Never upgrade status: "committed" is not "wired", "in diligence" is not "closed".
- Drop internal pulse tags ([CONFIRMED], [ADVANCING], [AT RISK]) and any [Recording] links.
- No filler, no hedging. Banned openers: "Just checking in", "I just wanted to", "I hope this finds you well".

OUTPUT FORMAT -- match this structure exactly, in markdown:
- Start with the greeting line on its own: Guys,
- One short intro paragraph (2-4 sentences, plain text, NOT bold) that says how many significant moves there were this period and summarizes them in flowing prose.
- Then one numbered section per significant move (typically 3-5). Each section is a full-sentence bold headline on its own line, formatted EXACTLY as: **1. <headline sentence>** -- then a newline, then a short narrative paragraph.
- Where a section needs to enumerate items (e.g. several programs), use indented sub-bullets formatted as: - **<name>** -- <one line>.
- End with these two lines:
Best Regards,
Ken

Write ONLY the email body, starting with "Guys," and ending with "Ken". No subject line, no preamble, no closing commentary."""


def distill_business_update(pulses: list, start_dt: datetime, end_dt: datetime) -> str:
    """One Claude pass: weekly pulse report(s) -> team-facing business update
    (markdown). Raises if the API key is missing or the model returns empty."""
    import anthropic

    api_key = os.environ.get("CLAUDE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_API_KEY not set")

    sections = []
    for p in pulses:
        label = f"{p['period_start'].strftime('%b %d')} - {p['period_end'].strftime('%b %d, %Y')}"
        body = p.get("report_markdown") or "(no report markdown archived for this week)"
        sections.append(f"=== Weekly Pulse: {label} ===\n{body}")
    source = "\n\n".join(sections)

    period_label = f"{start_dt.strftime('%B %d')} - {end_dt.strftime('%B %d, %Y')}"

    # Make the update INCREMENTAL: give the model the previous update so it does
    # not re-report items the team has already seen (only genuinely new progress).
    prior_block = ""
    try:
        prev = previous_update_markdown(start_dt)
    except Exception as e:
        prev = None
        logger.warning(f"Could not load previous update (non-fatal): {e}")
    if prev:
        prior_block = (
            "\n\nALREADY REPORTED IN THE PREVIOUS UPDATE (the team has already seen "
            "all of this -- do NOT repeat any of it as a news item; include a topic "
            "only if there is genuinely NEW progress or a material change since, and "
            "frame it as the new development, not a restatement):\n" + prev)

    user_prompt = (
        f"Period covered: {period_label}\n"
        f"Source: {len(pulses)} weekly pulse report(s) below.\n\n"
        f"{source}"
        f"{prior_block}\n\n"
        f"Produce the biweekly business update for {period_label} now. Markdown only."
    )

    # Append Ken's standing corrections (baseline + email-submitted) as
    # authoritative overrides. Never fatal -- distill still runs without them.
    system_prompt = DISTILL_SYSTEM_PROMPT
    try:
        import sara_corrections
        system_prompt += "\n\n" + sara_corrections.corrections_block()
    except Exception as e:
        logger.warning(f"Could not load standing corrections (non-fatal): {e}")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=DISTILL_MODEL,
        max_tokens=2500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    if not text:
        raise RuntimeError("distiller returned empty body")
    return text


# ======================================================================
#  HTML RENDERING
# ======================================================================


def _md_inline(text: str) -> str:
    """Escape HTML, then apply **bold**. Order matters -- escape first so user
    content cannot inject markup."""
    text = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)


def render_html(markdown_text: str) -> str:
    """Render the distilled markdown (H2/H3/**bold**/- bullets/paragraphs) into a
    styled HTML email body. Self-contained -- no dependency on app.py."""
    parts = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    for raw in markdown_text.split("\n"):
        s = raw.strip()
        if not s:
            close_list()
            continue
        if s.startswith("## "):
            close_list()
            parts.append(
                f'<h2 style="color:#1a1a1a;font-size:21px;margin:20px 0 8px;">{_md_inline(s[3:])}</h2>')
            continue
        if s.startswith("### "):
            close_list()
            parts.append(
                '<h3 style="color:#1a3a5f;font-size:16px;margin:18px 0 6px;'
                f'border-bottom:1px solid #e2e2e2;padding-bottom:3px;">{_md_inline(s[4:])}</h3>')
            continue
        # A line that is entirely bold (e.g. "**1. Headline.**") is a numbered
        # section header -- give it heading spacing while keeping the <strong>.
        if re.match(r"^\*\*.+\*\*$", s):
            close_list()
            parts.append(f'<p style="margin:18px 0 4px;font-size:15px;">{_md_inline(s)}</p>')
            continue
        if s.startswith("- "):
            if not in_list:
                parts.append('<ul style="margin:4px 0 10px;padding-left:22px;">')
                in_list = True
            parts.append(f'<li style="margin:4px 0;">{_md_inline(s[2:])}</li>')
            continue
        close_list()
        parts.append(f'<p style="margin:6px 0;">{_md_inline(s)}</p>')
    close_list()

    body = "\n".join(parts)
    footer = (
        '<p style="margin-top:20px;padding-top:8px;border-top:1px solid #e2e2e2;'
        'color:#888;font-size:12px;">Drafted by Sara from the weekly pulse. '
        'Review before forwarding to the team.</p>')
    return (
        '<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;'
        'line-height:1.6;color:#1a1a1a;max-width:680px;margin:0 auto;padding:16px;">'
        f'{body}{footer}</div>')


# ======================================================================
#  MAILER (same mechanism as the daily digest / pulse reports)
# ======================================================================


def send_update_email(subject: str, html_body: str, recipients: list = None):
    sender = os.environ.get("BOT_SENDER_EMAIL", "")
    if not sender:
        raise RuntimeError("BOT_SENDER_EMAIL not set -- update not emailed")
    recipients = recipients or BIWEEKLY_RECIPIENTS
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
    }
    eps.graph_post(f"{eps.MS_GRAPH_BASE}/users/{sender}/sendMail",
                   {"message": message, "saveToSentItems": True})
    logger.info(f"Biweekly update emailed to {', '.join(recipients)}")


# ======================================================================
#  STATUS PERSISTENCE
# ======================================================================


def write_status(status: dict):
    """Persist the last run outcome. Best effort -- never breaks the run."""
    try:
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status, f, default=str)
    except Exception as e:
        logger.warning(f"Could not write biweekly status: {e}")


def read_status() -> dict:
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"status": "no_runs", "message": "No biweekly update has run yet."}
    except Exception as e:
        return {"status": "error", "error": f"could not read status: {e}"}


def _load_history() -> list:
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning(f"Could not read biweekly history: {e}")
        return []


def append_history(window: dict, markdown: str, completed_at: str):
    """Record a sent update so later runs can diff against it. Best effort --
    a history-write failure never breaks the run."""
    hist = _load_history()
    hist.append({"window": window, "markdown": markdown, "completed_at": completed_at})
    hist = hist[-HISTORY_KEEP:]
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(hist, f, default=str, indent=2)
    except Exception as e:
        logger.warning(f"Could not write biweekly history: {e}")


def previous_update_markdown(start_dt: datetime) -> str:
    """Markdown of the most recent archived update whose window ended on or before
    this window's start -- the true 'previous update' to make this one
    incremental against. Returns None when there is no prior update."""
    prior = []
    for h in _load_history():
        end = _parse_iso((h.get("window") or {}).get("end"))
        if end is not None and end <= start_dt:
            prior.append((end, h.get("markdown") or ""))
    if not prior:
        return None
    prior.sort(key=lambda t: t[0])
    return prior[-1][1] or None


# ======================================================================
#  CADENCE GATE
# ======================================================================


def should_run_biweekly(now: datetime = None, force: bool = False) -> bool:
    """Gate for the weekly Monday cron so it fires every OTHER week. Fires when
    the current ISO week has the configured parity (default odd) AND no send has
    gone out within the last BIWEEKLY_MIN_GAP_DAYS. The parity check is what
    makes the cadence deterministic and immune to a manual/backfill send shifting
    the anchor; the gap floor only suppresses a near-duplicate (recent manual
    catch-up, or back-to-back odd weeks at the year boundary). Dry runs record
    sent=False and never suppress (they are not real sends)."""
    if force:
        return True
    now = now or datetime.now(timezone.utc)
    if now.isocalendar()[1] % 2 != BIWEEKLY_ISO_PARITY:
        return False  # off-parity week -> this is the in-between week
    status = read_status()
    if status.get("status") == "ok" and status.get("sent"):
        last = _parse_iso(status.get("completed_at"))
        if last is not None and (now - last) < timedelta(days=BIWEEKLY_MIN_GAP_DAYS):
            return False  # a send already went out very recently -> don't duplicate
    return True


# ======================================================================
#  RUN ORCHESTRATION
# ======================================================================


def run_biweekly(dry_run: bool = False, start_override: datetime = None,
                 end_override: datetime = None, force: bool = False) -> dict:
    """Read pulse archives in the window, distill a business update, email it to
    Ken (unless dry_run), persist status. `force` is accepted for API symmetry
    with the trigger endpoint; the cadence gate lives in the scheduler wrapper,
    so an explicit run always proceeds."""
    import traceback as _tb

    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    end_dt = end_override or now
    start_dt = start_override or (end_dt - timedelta(days=DEFAULT_WINDOW_DAYS))
    window = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}
    logger.info(f"Biweekly update {run_id}: window {window['start']} .. {window['end']}, dry_run={dry_run}")

    subject = ""
    try:
        pulses = select_pulses(start_dt, end_dt)
        if not pulses:
            msg = "No weekly pulse archives in window -- nothing to summarize."
            logger.warning(msg)
            status = {
                "status": "empty", "run_id": run_id,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "window": window, "dry_run": dry_run, "sent": False,
                "pulses_used": [], "message": msg,
            }
            write_status(status)
            return status

        markdown = distill_business_update(pulses, start_dt, end_dt)
        html_body = render_html(markdown)
        subject = (f"Business Update -- Palomar Labs Team. Period: "
                   f"{start_dt.strftime('%b %d')} - {end_dt.strftime('%b %d, %Y')}")

        sent = False
        if dry_run:
            logger.info(f"[dry-run] biweekly update:\n{markdown}")
            print(f"\nSubject: {subject}\n\n{markdown}\n")
        else:
            send_update_email(subject, html_body)
            sent = True

        status = {
            "status": "ok", "run_id": run_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "window": window, "dry_run": dry_run, "sent": sent,
            "pulses_used": [p["filename"] for p in pulses],
            "subject": subject, "markdown": markdown, "body": html_body,
        }
        write_status(status)
        if sent:
            append_history(window, markdown, status["completed_at"])
        return status
    except Exception as e:
        tb = _tb.format_exc()
        logger.error(f"Biweekly update {run_id} failed: {tb}")
        write_status({
            "status": "error", "run_id": run_id,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "window": window, "dry_run": dry_run, "sent": False,
            "subject": subject, "error": str(e), "traceback": tb,
        })
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Biweekly business update distilled from weekly pulse archives")
    parser.add_argument("--start", help="Window start YYYY-MM-DD")
    parser.add_argument("--end", help="Window end YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="Compose but send no email")
    parser.add_argument("--force", action="store_true", help="Ignore the biweekly cadence gate")
    args = parser.parse_args()

    start_override = (datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
                      if args.start else None)
    end_override = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
                    if args.end else None)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_biweekly(dry_run=args.dry_run, start_override=start_override,
                 end_override=end_override, force=args.force)


if __name__ == "__main__":
    main()
