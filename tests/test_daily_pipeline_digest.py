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
#  Owner resolution + graceful degradation
# ----------------------------------------------------------------------

def test_owner_map_from_env_reverses_mapping(monkeypatch):
    monkeypatch.setenv("HUBSPOT_OWNER_MAP",
                       '{"bk@negevlabs.com":"241153249","shlomi@negevlabs.com":"31267643"}')
    out = dpd._owner_map_from_env()
    assert out == {"241153249": "bk@negevlabs.com", "31267643": "shlomi@negevlabs.com"}


def test_owner_map_from_env_handles_bad_json(monkeypatch):
    monkeypatch.setenv("HUBSPOT_OWNER_MAP", "not-json")
    assert dpd._owner_map_from_env() == {}


def test_get_owner_maps_degrades_to_env_on_403(monkeypatch):
    monkeypatch.setenv("HUBSPOT_OWNER_MAP", '{"bk@negevlabs.com":"241153249"}')

    def forbidden(method, endpoint, data=None, params=None):
        raise RuntimeError("403 Client Error: Forbidden")

    monkeypatch.setattr(eps, "hubspot_request", forbidden)
    skipped = []
    by_owner, by_user = dpd.get_owner_maps(skipped)
    assert by_owner == {"241153249": "bk@negevlabs.com"}
    assert by_user == {}
    assert any("owners.read" in s for s in skipped)


def test_owner_name_fallbacks():
    by_owner = {"241153249": "Ken Belotsky"}
    assert dpd.owner_name(by_owner, "241153249") == "Ken Belotsky"
    assert dpd.owner_name(by_owner, "999") == "Owner 999"   # present but unmapped
    assert dpd.owner_name(by_owner, "") == "Unassigned"
    assert dpd.owner_name(by_owner, None) == "Unassigned"


def test_collect_activity_skips_object_type_on_search_failure(monkeypatch):
    # notes search 403s; the type is recorded in skipped and the rest proceeds
    def search(object_type, filters, properties):
        if object_type == "notes":
            raise RuntimeError("403 Forbidden")
        return []

    monkeypatch.setattr(dpd, "search_objects", search)
    skipped = []
    items = dpd.collect_activity(NOW - timedelta(hours=24), NOW, {}, {}, skipped)
    assert items == []
    assert any("notes" in s for s in skipped)


def test_collect_open_tasks_degrades_to_empty(monkeypatch):
    monkeypatch.setattr(dpd, "search_objects",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403")))
    skipped = []
    assert dpd.collect_open_tasks({}, {}, NOW, skipped) == []
    assert any("open tasks" in s for s in skipped)


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
#  HTML email rendering (the actual email body)
# ----------------------------------------------------------------------

def test_fmt_change_value():
    assert dpd._fmt_change_value("close date", "2026-06-14T13:53:41.234Z") == "2026-06-14"
    assert dpd._fmt_change_value("amount", "250000") == "$250,000"
    assert dpd._fmt_change_value("amount", "250000.0") == "$250,000"
    assert dpd._fmt_change_value("close date", None) == "(none)"
    assert dpd._fmt_change_value("amount", "garbage") == "garbage"


def test_render_html_suppresses_empty_sections():
    html = dpd.render_html(_data())
    assert "Stage moves" not in html
    assert "New activity" not in html
    assert "Flags" not in html
    assert "Due today" not in html
    # Footer totals always present
    assert "Pipeline: 1 deals" in html
    assert "$100,000" in html
    # Well-formed wrapper
    assert html.startswith("<div") and html.endswith("</div>")


def test_render_html_includes_sections_with_structure():
    move = {"deal": "Misha - NL 2026", "from": "Discovery", "to": "Value Validation",
            "by": "Vadim Usov", "at": ""}
    activity = dpd.group_by_owner([
        {"owner": "Vadim Usov", "deal": "Misha - NL 2026", "type": "note logged",
         "summary": "Call scheduled for 15 June", "author": "Alex K", "at": ""}])
    flag = {"type": "stale", "deal": "Quiet Fund", "owner": "Shlomi",
            "detail": "no activity for 9 days"}
    data = _data(stage_moves=[move], activity=activity, flags=dpd.group_by_owner([flag]))
    html = dpd.render_html(data)
    assert "<h3" in html and "Stage moves" in html
    assert "<ul" in html and "<li" in html
    assert "Discovery &rarr; Value Validation" in html
    assert "Vadim Usov" in html
    assert "Call scheduled for 15 June" in html
    assert "New activity" in html
    assert "Flags" in html
    assert "no activity for 9 days" in html


def test_render_html_escapes_user_content():
    # A deal name with markup must not break the email structure
    move = {"deal": "<script>alert(1)</script> & Co", "from": "A", "to": "B",
            "by": "X", "at": ""}
    html = dpd.render_html(_data(stage_moves=[move]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; Co" in html


def test_render_html_formats_property_changes():
    change = {"deal": "Yan", "property": "close date", "from": None,
              "to": "2026-06-14T13:53:41.234Z", "by": "Alex K", "at": ""}
    html = dpd.render_html(_data(property_changes=[change]))
    assert "close date" in html
    assert "2026-06-14" in html
    assert "2026-06-14T13:53:41" not in html  # raw timestamp must be prettified


# ----------------------------------------------------------------------
#  HubSpot deep links
# ----------------------------------------------------------------------

def test_record_url_builds_link():
    url = dpd._record_url("12345", "deals", "999")
    assert url == "https://app.hubspot.com/contacts/12345/record/0-3/999"
    assert dpd._record_url("12345", "notes", "5") == \
        "https://app.hubspot.com/contacts/12345/record/0-46/5"


def test_record_url_empty_when_missing_pieces():
    assert dpd._record_url("", "deals", "999") == ""        # no portal
    assert dpd._record_url("12345", "deals", "") == ""       # no object id
    assert dpd._record_url("12345", "unknown", "1") == ""    # unknown type


def test_link_falls_back_to_escaped_text_without_url():
    assert dpd._link("", "A & B") == "A &amp; B"
    out = dpd._link("https://x/y?a=1&b=2", "Deal")
    assert out.startswith('<a href="https://x/y?a=1&amp;b=2"')
    assert ">Deal</a>" in out


def test_render_html_links_deals_and_activities_with_portal():
    move = {"deal": "Tetrad VC", "deal_id": "111", "from": "Discovery",
            "to": "Value Validation", "by": "Alex K", "at": ""}
    activity = dpd.group_by_owner([
        {"owner": "Vadim", "deal": "Tetrad VC", "deal_id": "111", "type": "note logged",
         "object_type": "notes", "object_id": "222",
         "summary": "Call scheduled", "author": "Alex K", "at": ""}])
    data = _data(stage_moves=[move], activity=activity)
    data["hubspot_portal_id"] = "98765"
    html = dpd.render_html(data)
    assert "/contacts/98765/record/0-3/111" in html    # deal link
    assert "/contacts/98765/record/0-46/222" in html    # note (activity) link


def test_render_html_no_links_without_portal_id():
    move = {"deal": "Tetrad VC", "deal_id": "111", "from": "A", "to": "B",
            "by": "X", "at": ""}
    data = _data(stage_moves=[move])  # _data has no portal id
    html = dpd.render_html(data)
    assert "<a href" not in html
    assert "Tetrad VC" in html  # name still shown, just not linked


def test_render_html_links_overdue_task_and_due_today():
    overdue = {"type": "overdue task", "deal": "TKN", "deal_id": "111",
               "task_id": "333", "owner": "Ken", "detail": "\"Follow up\" due 2026-05-11"}
    due = [{"owner": "Ken", "task": "Send UPA", "deal": "Tetrad", "deal_id": "111",
            "task_id": "444", "due": "2026-06-16T08:00:00+00:00"}]
    data = _data(flags=dpd.group_by_owner([overdue]),
                 due_today=dpd.group_by_owner(due))
    data["hubspot_portal_id"] = "98765"
    html = dpd.render_html(data)
    assert "/record/0-27/333" in html   # overdue task -> task record
    assert "open task" in html
    assert "/record/0-27/444" in html   # due-today task -> task record


def test_build_digest_data_threads_portal_and_task_ids():
    stages = [{"id": "s2", "label": "Value Validation", "probability": 0.4,
               "is_closed": False, "order": 0}]
    due = [{"id": "444", "subject": "Send UPA", "due": NOW,
            "owner": "Ken", "deals": ["Tetrad"], "deal_ids": ["111"]}]
    data = dpd.build_digest_data(NOW - timedelta(hours=24), NOW, [], [], [], [],
                                 due, [_deal()], stages, portal_id="98765")
    assert data["hubspot_portal_id"] == "98765"
    item = data["due_today"]["Ken"][0]
    assert item["task_id"] == "444"
    assert item["deal_id"] == "111"  # single deal -> linkable


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


def test_digest_trigger_sync_returns_result(flask_client, monkeypatch):
    def fake_run(dry_run=False, since_override=None):
        return {"status": "ok", "dry_run": dry_run, "quiet": True, "sent": True}

    monkeypatch.setattr(dpd, "run_digest", fake_run)
    resp = flask_client.get("/digest/trigger?sync=true&dry_run=true")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    # Lock released after a sync run
    assert app_module._digest_lock.acquire(timeout=5)
    app_module._digest_lock.release()


def test_digest_trigger_sync_returns_traceback_on_error(flask_client, monkeypatch):
    def boom(dry_run=False, since_override=None):
        raise RuntimeError("HubSpot 400: property does not exist")

    monkeypatch.setattr(dpd, "run_digest", boom)
    resp = flask_client.get("/digest/trigger?sync=true")
    assert resp.status_code == 500
    payload = resp.get_json()
    assert payload["status"] == "error"
    assert "property does not exist" in payload["error"]
    assert "RuntimeError" in payload["traceback"]
    assert app_module._digest_lock.acquire(timeout=5)
    app_module._digest_lock.release()


def test_digest_status_endpoint_reads_persisted_status(flask_client, monkeypatch, tmp_path):
    monkeypatch.setattr(dpd, "STATUS_PATH", str(tmp_path / "status.json"))
    resp = flask_client.get("/digest/status")
    assert resp.get_json()["status"] == "no_runs"
    dpd.write_status({"status": "ok", "sent": True, "quiet": False})
    resp = flask_client.get("/digest/status")
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["sent"] is True


# ----------------------------------------------------------------------
#  run_digest orchestration (status persistence + result contract)
# ----------------------------------------------------------------------

@pytest.fixture
def stub_collectors(monkeypatch):
    """Stub the HubSpot collectors so run_digest exercises orchestration,
    rules, quiet detection, status persistence -- with no network."""
    stage = {"id": "s2", "label": "Value Validation", "probability": 0.4,
             "is_closed": False, "order": 0}
    monkeypatch.setattr(dpd, "get_pipeline_stages", lambda: [stage])
    monkeypatch.setattr(dpd, "get_owner_maps", lambda skipped=None: ({}, {}))
    monkeypatch.setattr(dpd, "get_pipeline_deals", lambda closing_stage_id="": [])
    monkeypatch.setattr(dpd, "collect_activity", lambda *a, **k: [])
    monkeypatch.setattr(dpd, "collect_open_tasks", lambda *a, **k: [])


def test_run_digest_quiet_day_sends_one_liner(ledger, monkeypatch, tmp_path, stub_collectors):
    monkeypatch.setattr(dpd, "STATUS_PATH", str(tmp_path / "status.json"))
    sent = {}
    monkeypatch.setattr(dpd, "send_digest_email",
                        lambda subject, body: sent.update(subject=subject, body=body))

    result = dpd.run_digest(dry_run=False)
    assert result["status"] == "ok"
    assert result["quiet"] is True
    assert result["sent"] is True
    assert "No pipeline changes" in sent["body"]
    # Status file persisted for /digest/status
    assert dpd.read_status()["sent"] is True


def test_run_digest_dry_run_does_not_send(ledger, monkeypatch, tmp_path, stub_collectors):
    monkeypatch.setattr(dpd, "STATUS_PATH", str(tmp_path / "status.json"))
    calls = []
    monkeypatch.setattr(dpd, "send_digest_email", lambda s, b: calls.append((s, b)))
    result = dpd.run_digest(dry_run=True)
    assert result["sent"] is False
    assert calls == []


def test_run_digest_records_error_with_traceback(ledger, monkeypatch, tmp_path):
    monkeypatch.setattr(dpd, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(dpd, "get_pipeline_stages",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom-400")))
    with pytest.raises(RuntimeError):
        dpd.run_digest(dry_run=False)
    status = dpd.read_status()
    assert status["status"] == "error"
    assert "boom-400" in status["error"]
    assert "RuntimeError" in status["traceback"]
