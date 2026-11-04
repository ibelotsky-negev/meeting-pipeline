"""Content builders for the Phase-1 meeting notification email.

Pure functions, no network: the Fireflies summary HTML section and the
transcript file attachment (Graph fileAttachment) that notify_organizer
adds to the "N action items ready for review" email.
"""
import base64
import html
import re
from datetime import datetime, timezone

# Graph sendMail caps the whole request at ~4 MB and base64 inflates by 4/3,
# so keep the raw transcript comfortably under 3 MB.
TRANSCRIPT_MAX_BYTES = 2_500_000

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE0F\U0000200D]+"
)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _clean(text) -> str:
    return _EMOJI_RE.sub("", str(text or "")).strip()


def _inline(text: str) -> str:
    """Escape HTML, then turn markdown **bold** into <strong>."""
    return _BOLD_RE.sub(r"<strong>\1</strong>", html.escape(text))


def _lines_to_html(text: str) -> str:
    """Render Fireflies markdown-ish text: bullet lines -> <li>, others -> heading/para."""
    out = []
    in_list = False
    for raw in str(text or "").splitlines():
        line = _clean(raw)
        if not line:
            continue
        if _BULLET_RE.match(line):
            if not in_list:
                out.append('<ul style="margin:4px 0 10px 0;padding-left:20px;">')
                in_list = True
            out.append(f'<li style="margin-bottom:4px;">{_inline(_BULLET_RE.sub("", line))}</li>')
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f'<div style="margin:10px 0 4px 0;font-weight:600;color:#1e293b;">{_inline(line)}</div>')
    if in_list:
        out.append("</ul>")
    return "".join(out)


def fireflies_meeting_url(transcript) -> str:
    """Link to the meeting in Fireflies: the API's transcript_url, else /view/<id>.

    "" for Teams transcripts (not in Fireflies) or when there is no id.
    """
    if not isinstance(transcript, dict) or transcript.get("_source") == "teams":
        return ""
    url = str(transcript.get("transcript_url") or "").strip()
    if url.startswith("https://"):
        return url
    tid = str(transcript.get("id") or "").strip()
    return f"https://app.fireflies.ai/view/{tid}" if tid else ""


def build_fireflies_summary_html(summary, meeting_url: str = "") -> str:
    """HTML section with the Fireflies-generated summary (overview + topic bullets)
    and a link to the meeting in Fireflies.

    Returns "" when there is nothing to show, so the caller can concatenate
    unconditionally.
    """
    if not isinstance(summary, dict):
        summary = {}
    overview = _clean(summary.get("overview"))
    bullets = summary.get("shorthand_bullet") or ""
    if isinstance(bullets, list):
        bullets = "\n".join(str(b) for b in bullets)
    body = ""
    if overview:
        paras = [p for p in (_clean(x) for x in overview.splitlines()) if p]
        body += "".join(f'<p style="margin:0 0 10px 0;">{_inline(p)}</p>' for p in paras)
    bullets_html = _lines_to_html(bullets)
    if bullets_html:
        body += bullets_html
    if meeting_url:
        # Raw URL, not just an <a>: mobile Outlook strips anchors but auto-links https://
        safe = html.escape(meeting_url, quote=True)
        body += (
            '<p style="margin:12px 0 0 0;font-size:13px;">Open in Fireflies (recording + transcript): '
            f'<a href="{safe}" style="color:#2563eb;">{safe}</a></p>'
        )
    if not body:
        return ""
    return (
        '<div style="padding:20px 28px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;border-top:1px solid #e2e8f0;">'
        '<div style="font-size:12px;text-transform:uppercase;color:#64748b;font-weight:600;letter-spacing:0.5px;margin-bottom:8px;">Meeting Summary (Fireflies)</div>'
        f'<div style="font-size:14px;line-height:1.6;color:#334155;">{body}</div>'
        '</div>'
    )


def _fmt_ts(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _meeting_date(date_value) -> str:
    if isinstance(date_value, (int, float)):
        try:
            ts = date_value / 1000 if date_value > 1e12 else date_value
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    return str(date_value or "")[:10]


def _safe_filename(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "", _clean(text)).strip().replace(" ", "_")
    return (name or "meeting")[:60]


def build_transcript_text(transcript: dict) -> str:
    """Plain-text transcript: header, then consecutive same-speaker lines merged."""
    transcript = transcript or {}
    sentences = transcript.get("sentences") or []
    turns = []
    for s in sentences:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        speaker = str(s.get("speaker_name") or "Unknown").strip() or "Unknown"
        if turns and turns[-1][0] == speaker:
            turns[-1][2].append(text)
        else:
            turns.append([speaker, _fmt_ts(s.get("start_time")), [text]])
    if not turns:
        return ""
    source = "Microsoft Teams" if transcript.get("_source") == "teams" else "Fireflies"
    participants = transcript.get("participants") or []
    header = [
        f"Meeting: {transcript.get('title') or 'Untitled meeting'}",
        f"Date: {_meeting_date(transcript.get('dateString'))}",
        f"Source: {source} transcript",
    ]
    if participants:
        header.append(f"Participants: {', '.join(str(p) for p in participants)}")
    lines = ["\n".join(header), ""]
    for speaker, ts, texts in turns:
        stamp = f"[{ts}] " if ts else ""
        lines.append(f"{stamp}{speaker}: {' '.join(texts)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_transcript_attachment(transcript: dict):
    """Graph fileAttachment dict for the transcript, or None if there is no text."""
    text = build_transcript_text(transcript)
    if not text:
        return None
    data = text.encode("utf-8")
    if len(data) > TRANSCRIPT_MAX_BYTES:
        note = "\n\n[Transcript truncated: too large to attach in full.]\n".encode("utf-8")
        data = data[: TRANSCRIPT_MAX_BYTES - len(note)].decode("utf-8", "ignore").encode("utf-8") + note
    transcript = transcript or {}
    date = _meeting_date(transcript.get("dateString"))
    name = f"transcript_{_safe_filename(transcript.get('title') or '')}"
    if date:
        name += f"_{date}"
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": f"{name}.txt",
        "contentType": "text/plain; charset=utf-8",
        "contentBytes": base64.b64encode(data).decode("ascii"),
    }
