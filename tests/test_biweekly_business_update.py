# Offline tests for biweekly_business_update: pulse-archive window selection,
# the every-other-week cadence gate, HTML rendering, run orchestration
# (dry-run / empty / error paths), and the /biweekly endpoints. No network.
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

import biweekly_business_update as bwu
import app as app_module

NOW = datetime(2026, 6, 15, 4, 15, 0, tzinfo=timezone.utc)  # a Monday, 07:15 IDT


# ----------------------------------------------------------------------
#  fixtures / helpers
# ----------------------------------------------------------------------


@pytest.fixture
def archive_dir(monkeypatch, tmp_path):
    d = tmp_path / "pulse"
    d.mkdir()
    monkeypatch.setattr(bwu, "PULSE_ARCHIVE_DIR", str(d))
    return d


@pytest.fixture
def status_path(monkeypatch, tmp_path):
    p = tmp_path / "biweekly_status.json"
    monkeypatch.setattr(bwu, "STATUS_PATH", str(p))
    return p


def _write_pulse(d, week, start, end, markdown="## Weekly Pulse\n\n### Green\n- a win"):
    archive = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated_at": end.isoformat(),
        "version": "1.0",
        "stats": {"emails_scanned": 10},
        "signals": {"email_signals": {"green": ["a win"]}},
        "report_markdown": markdown,
    }
    (d / f"{week}.json").write_text(json.dumps(archive), encoding="utf-8")


# ----------------------------------------------------------------------
#  select_pulses
# ----------------------------------------------------------------------


def test_select_pulses_returns_overlapping_sorted(archive_dir):
    # W23 May 31 - Jun 7, W24 Jun 7 - Jun 14 (the June 1-14 window)
    _write_pulse(archive_dir, "2026-W23",
                 datetime(2026, 5, 31, 19, tzinfo=timezone.utc),
                 datetime(2026, 6, 7, 19, tzinfo=timezone.utc))
    _write_pulse(archive_dir, "2026-W24",
                 datetime(2026, 6, 7, 19, tzinfo=timezone.utc),
                 datetime(2026, 6, 14, 19, tzinfo=timezone.utc))
    out = bwu.select_pulses(datetime(2026, 6, 1, tzinfo=timezone.utc),
                            datetime(2026, 6, 14, tzinfo=timezone.utc))
    assert [p["filename"] for p in out] == ["2026-W23.json", "2026-W24.json"]


def test_select_pulses_excludes_before_and_after_window(archive_dir):
    # W22 ends May 31 (before Jun 1); W25 starts Jun 14 19:00 (after Jun 14 00:00)
    _write_pulse(archive_dir, "2026-W22",
                 datetime(2026, 5, 24, 19, tzinfo=timezone.utc),
                 datetime(2026, 5, 31, 19, tzinfo=timezone.utc))
    _write_pulse(archive_dir, "2026-W23",
                 datetime(2026, 5, 31, 19, tzinfo=timezone.utc),
                 datetime(2026, 6, 7, 19, tzinfo=timezone.utc))
    _write_pulse(archive_dir, "2026-W25",
                 datetime(2026, 6, 14, 19, tzinfo=timezone.utc),
                 datetime(2026, 6, 21, 19, tzinfo=timezone.utc))
    out = bwu.select_pulses(datetime(2026, 6, 1, tzinfo=timezone.utc),
                            datetime(2026, 6, 14, tzinfo=timezone.utc))
    # W22 (pe=May31 < start Jun1) and W25 (ps=Jun14 19:00 > end Jun14 00:00) excluded
    assert [p["filename"] for p in out] == ["2026-W23.json"]


def test_select_pulses_empty_dir_returns_empty(archive_dir):
    assert bwu.select_pulses(NOW - timedelta(days=14), NOW) == []


def test_select_pulses_missing_dir_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(bwu, "PULSE_ARCHIVE_DIR", str(tmp_path / "nope"))
    assert bwu.select_pulses(NOW - timedelta(days=14), NOW) == []


def test_select_pulses_skips_unreadable_archive(archive_dir):
    _write_pulse(archive_dir, "2026-W24",
                 datetime(2026, 6, 7, 19, tzinfo=timezone.utc),
                 datetime(2026, 6, 14, 19, tzinfo=timezone.utc))
    (archive_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
    out = bwu.select_pulses(datetime(2026, 6, 1, tzinfo=timezone.utc),
                            datetime(2026, 6, 14, tzinfo=timezone.utc))
    assert [p["filename"] for p in out] == ["2026-W24.json"]


# ----------------------------------------------------------------------
#  should_run_biweekly (cadence gate)
# ----------------------------------------------------------------------


def test_should_run_when_no_prior_run(status_path):
    assert bwu.should_run_biweekly(now=NOW) is True


def test_should_not_run_when_last_send_recent(status_path):
    bwu.write_status({"status": "ok", "sent": True,
                      "completed_at": (NOW - timedelta(days=2)).isoformat()})
    assert bwu.should_run_biweekly(now=NOW) is False


def test_should_run_when_last_send_old_enough(status_path):
    bwu.write_status({"status": "ok", "sent": True,
                      "completed_at": (NOW - timedelta(days=14)).isoformat()})
    assert bwu.should_run_biweekly(now=NOW) is True


def test_dry_run_does_not_anchor_cadence(status_path):
    # A recent dry run records sent=False -> must not block the next real run
    bwu.write_status({"status": "ok", "sent": False,
                      "completed_at": (NOW - timedelta(days=1)).isoformat()})
    assert bwu.should_run_biweekly(now=NOW) is True


def test_force_bypasses_recent_send(status_path):
    bwu.write_status({"status": "ok", "sent": True,
                      "completed_at": (NOW - timedelta(days=1)).isoformat()})
    assert bwu.should_run_biweekly(now=NOW, force=True) is True


# Parity gate (default fires on ODD ISO weeks). These pin the behavior that a
# manual mid-cycle send can no longer poison -- the regression that silently
# skipped the 2026-06-29 scheduled run.
_ODD_WEEK_MON = datetime(2026, 6, 29, 4, 15, tzinfo=timezone.utc)   # ISO week 27 (odd)
_EVEN_WEEK_MON = datetime(2026, 6, 22, 4, 15, tzinfo=timezone.utc)  # ISO week 26 (even)


def test_fires_on_parity_week_even_after_recent_backfill(status_path):
    # The exact June 29 regression: a manual backfill sent 13 days earlier must
    # NOT skip the odd-week scheduled run.
    bwu.write_status({"status": "ok", "sent": True,
                      "completed_at": datetime(2026, 6, 16, 16, 0, tzinfo=timezone.utc).isoformat()})
    assert bwu.should_run_biweekly(now=_ODD_WEEK_MON) is True


def test_skips_on_off_parity_week(status_path):
    # Even ISO week is the in-between week -> never fires, regardless of history.
    assert bwu.should_run_biweekly(now=_EVEN_WEEK_MON) is False


def test_recent_send_suppresses_even_on_parity_week(status_path):
    # A send within the gap floor (e.g. a manual catch-up 3 days ago) suppresses
    # a duplicate on the very next parity Monday.
    bwu.write_status({"status": "ok", "sent": True,
                      "completed_at": (_ODD_WEEK_MON - timedelta(days=3)).isoformat()})
    assert bwu.should_run_biweekly(now=_ODD_WEEK_MON) is False


# ----------------------------------------------------------------------
#  render_html
# ----------------------------------------------------------------------


def test_render_html_structure_and_escaping():
    md = ("## Negev Labs Business Update: Jun 1 - Jun 14\n\n"
          "**Strong fundraising progress.**\n\n"
          "### Fundraising & Capital\n"
          "- Tetrad committed $2M <not closed>\n")
    out = bwu.render_html(md)
    assert "<h2" in out and "<h3" in out and "<li" in out
    assert "<strong>Strong fundraising progress.</strong>" in out
    assert "&lt;not closed&gt;" in out  # user content escaped
    assert "<not closed>" not in out
    assert "Review before forwarding" in out  # footer present


def test_render_html_narrative_style():
    # Ken's approved style: greeting, intro, numbered bold section headers,
    # sub-bullets, sign-off.
    md = ("Guys,\n\n"
          "Three significant moves this period.\n\n"
          "**1. Dan is now a partner.**\n"
          "Recognition of his work on the grant.\n\n"
          "**2. We shut down three programs.**\n"
          "- **Amanita** -- discontinued, off-strategy\n\n"
          "Best Regards,\nKen")
    out = bwu.render_html(md)
    # Full-line bold headers keep <strong> and render as spaced paragraphs
    assert "<strong>1. Dan is now a partner.</strong>" in out
    assert "<strong>2. We shut down three programs.</strong>" in out
    # Greeting, body, sub-bullet, and sign-off all present
    assert "Guys," in out
    assert "<li" in out and "<strong>Amanita</strong>" in out
    assert "Best Regards," in out and ">Ken<" in out


# ----------------------------------------------------------------------
#  run_biweekly
# ----------------------------------------------------------------------


def test_run_biweekly_empty_window_no_send(monkeypatch, status_path):
    monkeypatch.setattr(bwu, "select_pulses", lambda s, e: [])
    sent = []
    monkeypatch.setattr(bwu, "send_update_email", lambda *a, **k: sent.append(1))
    result = bwu.run_biweekly()
    assert result["status"] == "empty"
    assert result["sent"] is False
    assert sent == []
    assert json.loads(status_path.read_text())["status"] == "empty"


def test_run_biweekly_dry_run_composes_without_send(monkeypatch, status_path):
    monkeypatch.setattr(bwu, "select_pulses", lambda s, e: [{"filename": "2026-W24.json"}])
    monkeypatch.setattr(bwu, "distill_business_update", lambda p, s, e: "## Update\n\n- item")
    sent = []
    monkeypatch.setattr(bwu, "send_update_email", lambda *a, **k: sent.append(1))
    result = bwu.run_biweekly(dry_run=True)
    assert result["status"] == "ok"
    assert result["sent"] is False
    assert sent == []
    assert "Update" in result["markdown"]


def test_run_biweekly_success_sends_and_persists(monkeypatch, status_path):
    monkeypatch.setattr(bwu, "select_pulses",
                        lambda s, e: [{"filename": "2026-W23.json"}, {"filename": "2026-W24.json"}])
    monkeypatch.setattr(bwu, "distill_business_update", lambda p, s, e: "## Update\n\n- item")
    captured = {}

    def fake_send(subject, html_body, recipients=None):
        captured["subject"] = subject
        captured["html"] = html_body

    monkeypatch.setattr(bwu, "send_update_email", fake_send)
    result = bwu.run_biweekly()
    assert result["status"] == "ok"
    assert result["sent"] is True
    assert result["pulses_used"] == ["2026-W23.json", "2026-W24.json"]
    assert "Business Update" in captured["subject"]
    persisted = json.loads(status_path.read_text())
    assert persisted["sent"] is True


def test_run_biweekly_distill_error_writes_status_and_raises(monkeypatch, status_path):
    monkeypatch.setattr(bwu, "select_pulses", lambda s, e: [{"filename": "x.json"}])

    def boom(p, s, e):
        raise RuntimeError("claude 529 overloaded")

    monkeypatch.setattr(bwu, "distill_business_update", boom)
    with pytest.raises(RuntimeError):
        bwu.run_biweekly()
    persisted = json.loads(status_path.read_text())
    assert persisted["status"] == "error"
    assert "overloaded" in persisted["error"]


def test_run_biweekly_window_override(monkeypatch, status_path):
    seen = {}
    monkeypatch.setattr(bwu, "select_pulses",
                        lambda s, e: seen.update({"s": s, "e": e}) or [])
    bwu.run_biweekly(start_override=datetime(2026, 6, 1, tzinfo=timezone.utc),
                     end_override=datetime(2026, 6, 14, tzinfo=timezone.utc))
    assert seen["s"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert seen["e"] == datetime(2026, 6, 14, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
#  /biweekly endpoints
# ----------------------------------------------------------------------


@pytest.fixture
def flask_client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_biweekly_status_endpoint_reads_persisted(flask_client, monkeypatch, tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"status": "ok", "sent": True}), encoding="utf-8")
    monkeypatch.setattr(bwu, "STATUS_PATH", str(p))
    resp = flask_client.get("/biweekly/status")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_biweekly_trigger_sync_returns_result(flask_client, monkeypatch):
    def fake_run(dry_run=False, start_override=None, end_override=None, force=False):
        return {"status": "ok", "dry_run": dry_run, "sent": not dry_run}

    monkeypatch.setattr(bwu, "run_biweekly", fake_run)
    resp = flask_client.get("/biweekly/trigger?sync=true&dry_run=true")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    assert app_module._biweekly_lock.acquire(timeout=5)
    app_module._biweekly_lock.release()


def test_biweekly_trigger_passes_window_overrides(flask_client, monkeypatch):
    seen = {}

    def fake_run(dry_run=False, start_override=None, end_override=None, force=False):
        seen["start"] = start_override
        seen["end"] = end_override
        return {"status": "ok"}

    monkeypatch.setattr(bwu, "run_biweekly", fake_run)
    resp = flask_client.get("/biweekly/trigger?sync=true&start=2026-06-01&end=2026-06-14")
    assert resp.status_code == 200
    assert seen["start"] == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert seen["end"] == datetime(2026, 6, 14, tzinfo=timezone.utc)
    # lock released by the endpoint's finally after a sync run
    assert app_module._biweekly_lock.acquire(timeout=5)
    app_module._biweekly_lock.release()


def test_biweekly_trigger_bad_date_returns_400(flask_client):
    resp = flask_client.get("/biweekly/trigger?start=June1&sync=true")
    assert resp.status_code == 400
    assert "YYYY-MM-DD" in resp.get_json()["error"]


def test_biweekly_trigger_refuses_concurrent_run(flask_client):
    assert app_module._biweekly_lock.acquire(blocking=False)
    try:
        resp = flask_client.get("/biweekly/trigger?sync=true")
        assert resp.status_code == 409
        assert resp.get_json() == {"status": "already_running"}
    finally:
        app_module._biweekly_lock.release()


def test_biweekly_trigger_sync_returns_traceback_on_error(flask_client, monkeypatch):
    def boom(dry_run=False, start_override=None, end_override=None, force=False):
        raise RuntimeError("graph 403 forbidden")

    monkeypatch.setattr(bwu, "run_biweekly", boom)
    resp = flask_client.get("/biweekly/trigger?sync=true")
    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload["status"] == "error"
    assert "forbidden" in payload["error"]
    assert "RuntimeError" in payload["traceback"]
    assert app_module._biweekly_lock.acquire(timeout=5)
    app_module._biweekly_lock.release()


def test_biweekly_trigger_background_start(flask_client, monkeypatch):
    ran = threading.Event()
    seen = {}

    def fake_run(dry_run=False, start_override=None, end_override=None, force=False):
        seen["dry_run"] = dry_run
        ran.set()

    monkeypatch.setattr(bwu, "run_biweekly", fake_run)
    resp = flask_client.get("/biweekly/trigger?dry_run=true")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "started", "dry_run": True}
    assert ran.wait(timeout=5)
    assert seen["dry_run"] is True
    assert app_module._biweekly_lock.acquire(timeout=5)
    app_module._biweekly_lock.release()
