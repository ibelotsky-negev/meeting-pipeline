# Tests for fireflies_client.get_recent_transcripts's poll window.
#
# Before the fix, the window filtered on the meeting's START time only. A
# meeting longer than the poll window (POLL_INTERVAL_MINUTES + 10, default
# 15 min) would have its start timestamp scroll out of the cutoff before
# Fireflies even finished processing the transcript -- silently dropping it
# from both the poll AND (if the webhook also missed it) the pipeline
# entirely. Real-world case: an 86-min meeting on 2026-08-05 that no
# notification email ever went out for. Fix gates on meeting END time
# (start + duration) instead.
from datetime import datetime, timedelta, timezone

import fireflies_client as fc


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_long_meeting_within_window_is_not_dropped(monkeypatch):
    """A meeting that started before the window but ENDED inside it must
    still be picked up -- this is the 86-min-meeting regression."""
    now = datetime.now(timezone.utc)
    long_meeting = {
        "id": "long1",
        "title": "NL Partners meetings (weekly)",
        "dateString": _iso(now - timedelta(minutes=40)),  # start: outside a 15-min window
        "duration": 30,  # end: now - 10 min -- inside a 15-min window
    }
    monkeypatch.setattr(fc, "fireflies_query", lambda q, v=None: {"transcripts": [long_meeting]})

    recent = fc.get_recent_transcripts(since_minutes=15)

    assert [t["id"] for t in recent] == ["long1"]


def test_genuinely_old_meeting_is_dropped(monkeypatch):
    """A meeting whose end time also falls outside the window is correctly excluded."""
    now = datetime.now(timezone.utc)
    old_meeting = {
        "id": "old1",
        "title": "Ancient meeting",
        "dateString": _iso(now - timedelta(hours=3)),
        "duration": 20,  # ended ~2h40m ago -- well outside any short window
    }
    monkeypatch.setattr(fc, "fireflies_query", lambda q, v=None: {"transcripts": [old_meeting]})

    recent = fc.get_recent_transcripts(since_minutes=15)

    assert recent == []


def test_short_recent_meeting_is_kept(monkeypatch):
    """Baseline: a short meeting entirely inside the window still passes (no regression)."""
    now = datetime.now(timezone.utc)
    short_meeting = {
        "id": "short1",
        "title": "Quick sync",
        "dateString": _iso(now - timedelta(minutes=5)),
        "duration": 3,
    }
    monkeypatch.setattr(fc, "fireflies_query", lambda q, v=None: {"transcripts": [short_meeting]})

    recent = fc.get_recent_transcripts(since_minutes=15)

    assert [t["id"] for t in recent] == ["short1"]


def test_missing_duration_falls_back_to_start_time(monkeypatch):
    """No duration field (e.g. API omission) should not crash -- treat as a
    zero-length meeting and gate on start time, same as before the fix."""
    now = datetime.now(timezone.utc)
    no_duration_meeting = {
        "id": "nodur1",
        "title": "No duration field",
        "dateString": _iso(now - timedelta(minutes=5)),
    }
    monkeypatch.setattr(fc, "fireflies_query", lambda q, v=None: {"transcripts": [no_duration_meeting]})

    recent = fc.get_recent_transcripts(since_minutes=15)

    assert [t["id"] for t in recent] == ["nodur1"]
