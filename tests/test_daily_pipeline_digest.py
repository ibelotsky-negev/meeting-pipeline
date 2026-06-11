# Offline tests for daily_pipeline_digest: timestamp parsing, resilient
# window, property-history delta extraction, rules engine, quiet-day and
# fallback rendering, and the /digest/trigger endpoint. No network.
import threading
from datetime import datetime, timedelta, timezone

import pytest

import email_pipeline_sync as eps
import daily_pipeline_digest as dpd
import app as app_module

NOW = datetime(2026, 6, 11, 4, 0, 0, tzinfo=timezone.utc)  # 07:00 Israel (IDT)


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(eps, "LEDGER_PATH", str(tmp_path / "ledger.db"))
    conn = dpd.ledger_connect()
    yield conn
    conn.close()


def _deal(**overrides):
    base = {
        "id": "d1",
        "name": "Tetrad VC - NL 2026",
        "stage_id": "s2",
        "stage_label": "Value Validation",
        "is_closed": False,
        "probability": 0.4,
        "in_closing": False,
        "owner": "Vadim",
        "amount": 250000.0,
        "last_activity": NOW - timedelta(days=1),
        "created": NOW - timedelta(days=30),
        "modified": NOW - timedelta(hours=2),
        "entered_closing": None,
    }
    base.update(overrides)
    return base


# ----------------------------------------------------------------------
#  _parse_ts
# ----------------------------------------------------------------------

def test_parse_ts_iso_z():
    dt = dpd._parse_ts("2026-06-10T12:00:00.123Z")
    assert dt == datetime(2026, 6, 10, 12, 0, 0, 123000, tzinfo=timezone.utc)


def test_parse_ts_epoch_ms_string():
    dt = dpd._parse_ts("1781179200000")
    assert dt == datetime.fromtimestamp(1781179200, tz=timezone.utc)


def test_parse_ts_invalid_and_empty():
    assert dpd._parse_ts(None) is None
    assert dpd._parse_ts("") is None
    assert dpd._parse_ts("not-a-date") is None


# ----------------------------------------------------------------------
#  Resilient window (ledger)
# ----------------------------------------------------------------------

def _insert_run(conn, window_until, status="ok", dry_run=0):
    conn.execute(
        "INSERT OR REPLACE INTO digest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"run-{window_until}-{status}-{dry_run}", "", "", "", window_until,
         0, 0, 0, 0, 1, dry_run, status),
    )
    conn.commit()


def test_window_default_24h_when_no_runs(ledger):
    assert dpd.get_window_start(ledger, NOW) == NOW - timedelta(hours=24)


def test_window_starts_at_last_successful_run(ledger):
    last = (NOW - timedelta(hours=30)).isoformat()
    _insert_run(ledger, last)
    assert dpd.get_window_start(ledger, NOW) == NOW - timedelta(hours=30)


def test_window_ignores_error_and_dry_runs(ledger):
    _insert_run(ledger, (NOW - timedelta(hours=36)).isoformat())
    _insert_run(ledger, (NOW - timedelta(hours=2)).isoformat(), status="error")
    _insert_run(ledger, (NOW - timedelta(hours=1)).isoformat(), dry_run=1)
    assert dpd.get_window_start(ledger, NOW) == NOW - timedelta(hours=36)


def test_window_capped_at_max_days(ledger):
    _insert_run(ledger, (NOW - timedelta(days=20)).isoformat())
    assert dpd.get_window_start(ledger, NOW) == NOW - timedelta(days=dpd.MAX_WINDOW_DAYS)


# ----------------------------------------------------------------------
#  extract_property_changes (delta collector core)
# ----------------------------------------------------------------------

def _ver(value, ts, user="42"):
    return {"value": value, "timestamp": ts, "updatedByUserId": user}


def test_extract_changes_pairs_before_and_after():
    versions = [  # newest first, as HubSpot returns them
        _ver("s3", "2026-06-10T15:00:00Z"),
        _ver("s2", "2026-06-10T10:00:00Z"),
        _ver("s1", "2026-06-01T10:00:00Z"),
    ]
    since, until = NOW - timedelta(hours=24), NOW
    changes = dpd.extract_property_changes(versions, since, until)
    assert [(c["from"], c["to"]) for c in changes] == [("s1", "s2"), ("s2", "s3")]
    assert changes[0]["by_user_id"] == "42"


def test_extract_changes_outside_window_excluded():
    versions = [_ver("s2", "2026-06-01T10:00:00Z"), _ver("s1", "2026-05-01T10:00:00Z")]
    changes = dpd.extract_property_changes(versions, NOW - timedelta(hours=24), NOW)
    assert changes == []


def test_extract_changes_first_version_in_window_reports_creation():
    versions = [_ver("s1", "2026-06-10T12:00:00Z")]
    changes = dpd.extract_property_changes(versions, NOW - timedelta(hours=24), NOW)
    assert changes == [{"from": None, "to": "s1", "at": "2026-06-10T12:00:00+00:00",
                        "by_user_id": "42"}]


def test_extract_changes_skips_no_op_rewrites():
    versions = [_ver("s1", "2026-06-10T12:00:00Z"), _ver("s1", "2026-06-01T10:00:00Z")]
    changes = dpd.extract_property_changes(versions, NOW - timedelta(hours=24), NOW)
    assert changes == []


# ----------------------------------------------------------------------
#  Rules engine
# ----------------------------------------------------------------------

def test_stale_flags_deal_with_old_activity():
    deals = [_deal(last_activity=NOW - timedelta(days=8))]
    flags = dpd.flag_stale_deals(deals, NOW, stale_days=7)
    assert len(flags) == 1
    assert flags[0]["type"] == "stale"
    assert "8 days" in flags[0]["detail"]


def test_stale_skips_fresh_and_closed_deals():
    deals = [
        _deal(last_activity=NOW - timedelta(days=6)),
        _deal(id="d2", is_closed=True, last_activity=NOW - timedelta(days=60)),
    ]
    assert dpd.flag_stale_deals(deals, NOW, stale_days=7) == []


def test_stale_falls_back_to_created_when_no_activity():
    fresh = _deal(last_activity=None, created=NOW - timedelta(days=2))
    old = _deal(id="d2", last_activity=None, created=NOW - timedelta(days=10))
    never = _deal(id="d3", last_activity=None, created=None)
    flags = dpd.flag_stale_deals([fresh, old, never], NOW, stale_days=7)
    assert len(flags) == 2
    details = [f["detail"] for f in flags]
    assert any("10 days" in d for d in details)
    assert "no activity ever logged" in details


def test_closing_watch_flags_after_threshold():
    watched = _deal(in_closing=True, entered_closing=NOW - timedelta(days=6))
    recent = _deal(id="d2", in_closing=True, entered_closing=NOW - timedelta(days=3))
    other = _deal(id="d3", in_closing=False, entered_closing=NOW - timedelta(days=30))
    flags = dpd.flag_closing_watch([watched, recent, other], NOW, watch_days=5)
    assert len(flags) == 1
    assert flags[0]["type"] == "wire-watch"
    assert "6 days" in flags[0]["detail"]


def test_split_overdue_and_due_today_israel_boundaries():
    # Israel day for NOW (offset +3): 2026-06-10T21:00Z .. 2026-06-11T21:00Z
    overdue = {"id": "t1", "subject": "chase KYC", "due": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
               "owner": "Vadim", "deals": ["Misha"]}
    today = {"id": "t2", "subject": "send UPA", "due": datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc),
             "owner": "Ken", "deals": ["Tetrad"]}
    future = {"id": "t3", "subject": "later", "due": datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
              "owner": "Ken", "deals": []}
    no_due = {"id": "t4", "subject": "no due", "due": None, "owner": "Ken", "deals": []}
    od, dt = dpd.split_overdue_and_due_today([overdue, today, future, no_due], NOW, offset_hours=3)
    assert [t["id"] for t in od] == ["t1"]
    assert [t["id"] for t in dt] == ["t2"]


def test_flag_overdue_tasks_format():
    task = {"id": "t1", "subject": "chase KYC", "due": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "owner": "Vadim", "deals": ["Misha - NL 2026"]}
    flags = dpd.flag_overdue_tasks([task])
    assert flags[0]["type"] == "overdue task"
    assert "chase KYC" in flags[0]["detail"]
    assert "2026-06-01" in flags[0]["detail"]


# ----------------------------------------------------------------------
#  Digest assembly: quiet detection + fallback rendering
# ----------------------------------------------------------------------

def _data(**overrides):
    stages = [{"id": "s2", "label": "Value Validation", "probability": 0.4,
               "is_closed": False, "order": 0}]
    base = dpd.build_digest_data(
        NOW - timedelta(hours=24), NOW,
        stage_moves=[], property_changes=[], activity=[], flags=[],
        due_today=[], deals=[_deal()], stages=stages,
    )
    base.update(overrides)
    return base


def test_quiet_day_detected_despite_totals():
    assert dpd.is_quiet(_data())


def test_not_quiet_with_stage_move():
    data = _data(stage_moves=[{"deal": "Misha", "from": "Discovery",
                               "to": "Value Validation", "by": "Vadim", "at": ""}])
    assert not dpd.is_quiet(data)


def test_fallback_render_suppresses_empty_sections():
    text = dpd.render_fallback(_data())
    assert "STAGE MOVES" not in text
    assert "NEW ACTIVITY" not in text
    assert "FLAGS" not in text
    assert "DUE TODAY" not in text
    assert "Pipeline: 1 deals" in text
    assert "Value Validation: 1" in text
    assert "$100,000" in text  # 250k * 0.4 weighted


def test_fallback_render_includes_populated_sections():
    move = {"deal": "Misha - NL 2026", "from": "Discovery", "to": "Value Validation",
            "by": "Vadim Usov", "at": ""}
    flag = {"type": "stale", "deal": "Quiet Fund", "owner": "Shlomi",
            "detail": "no activity for 9 days"}
    data = _data(stage_moves=[move], flags=dpd.group_by_owner([flag]))
    text = dpd.render_fallback(data)
    assert "STAGE MOVES" in text
    assert "Discovery -> Value Validation (Vadim Usov)" in text
    assert "FLAGS" in text
    assert "[stale] Quiet Fund: no activity for 9 days" in text


def test_group_by_owner_defaults_unassigned():
    grouped = dpd.group_by_owner([{"owner": "", "x": 1}, {"owner": "Ken", "x": 2}])
    assert set(grouped) == {"Unassigned", "Ken"}


# ----------------------------------------------------------------------
#  /digest/trigger endpoint
# ----------------------------------------------------------------------

def test_digest_trigger_starts_background_run(flask_client, monkeypatch):
    ran = threading.Event()
    seen = {}

    def fake_run(dry_run=False, since_override=None):
        seen["dry_run"] = dry_run
        ran.set()

    monkeypatch.setattr(dpd, "run_digest", fake_run)
    resp = flask_client.get("/digest/trigger?dry_run=true")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "started", "dry_run": True}
    assert ran.wait(timeout=5), "background digest thread never ran"
    assert seen["dry_run"] is True
    # Lock must be released after the run so the next trigger works
    assert app_module._digest_lock.acquire(timeout=5)
    app_module._digest_lock.release()


def test_digest_trigger_refuses_concurrent_run(flask_client):
    assert app_module._digest_lock.acquire(blocking=False)
    try:
        resp = flask_client.get("/digest/trigger")
        assert resp.status_code == 409
        assert resp.get_json() == {"status": "already_running"}
    finally:
        app_module._digest_lock.release()
