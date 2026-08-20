# Tests for Fireflies daily-quota resilience (2.29.0).
#
# Background: Fireflies enforces a hard ~50 requests/day quota that resets at
# 00:00 UTC and is shared workspace-wide. With POLL_INTERVAL_MINUTES=5 the
# poller alone wanted 288 calls/day, so the quota died every morning by
# ~04:13 UTC. After that BOTH ingestion paths failed -- the poll raised on
# every run, and the webhook's 3 retries (15/30/45s) could not outlast a quota
# that resets at midnight, so the transcript was DROPPED. The miss was
# permanent: the poll window is only POLL_INTERVAL + 10 minutes, so by the
# next reset the meeting had scrolled out of range. Verified in production on
# 2026-08-19: 195/245 polls 429'd, 0 Phase-1 runs, 0 notification emails, and
# one real webhook (01M00NDKDYP0ZN1FRZYV2KA0TX) lost outright.
#
# All offline -- the conftest no_network fixture blocks real HTTP.
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

import app as app_module
import fireflies_client as fc


def _quota_error_payload(retry_after_ms=None):
    """Shape of a real Fireflies 429, as captured from production logs."""
    metadata = {}
    if retry_after_ms is not None:
        metadata["retryAfter"] = retry_after_ms
    return {
        "errors": [
            {
                "friendly": True,
                "message": "Too many requests. Please retry after Thu, 20 Aug 2026 00:00:00 GMT (UTC)",
                "code": "too_many_requests",
                "extensions": {
                    "code": "too_many_requests",
                    "status": 429,
                    "metadata": metadata,
                },
            }
        ]
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def quota_state(monkeypatch, tmp_path):
    """Per-test quota + deferred state files; never touch the real volume."""
    monkeypatch.setattr(fc, "QUOTA_STATE_FILE", str(tmp_path / "fireflies_quota.json"))
    deferred = tmp_path / "fireflies_deferred.json"
    import stores
    monkeypatch.setattr(stores, "FIREFLIES_DEFERRED_FILE", str(deferred))
    monkeypatch.setattr(app_module, "PROCESSED_FILE", str(tmp_path / "processed.json"))
    import config
    monkeypatch.setattr(config, "PROCESSED_FILE", str(tmp_path / "processed.json"))
    monkeypatch.setattr(stores, "PROCESSED_FILE", str(tmp_path / "processed.json"))
    return tmp_path


# --------------------------------------------------------------- 429 parsing


def test_quota_429_raises_typed_error_with_reset_time(monkeypatch):
    """A quota 429 must surface as FirefliesQuotaExceeded carrying the reset
    time, not as a generic Exception -- callers branch on the type."""
    reset = datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc)
    payload = _quota_error_payload(retry_after_ms=int(reset.timestamp() * 1000))
    monkeypatch.setattr(fc.requests, "post", lambda *a, **k: _Resp(payload))

    with pytest.raises(fc.FirefliesQuotaExceeded) as excinfo:
        fc.fireflies_query("query { transcripts { id } }")

    assert excinfo.value.retry_after == reset


def test_quota_429_without_retry_after_defaults_to_next_midnight(monkeypatch):
    """Fireflies always sent retryAfter in practice, but a missing one must
    not degrade to 'retry immediately'."""
    monkeypatch.setattr(fc.requests, "post", lambda *a, **k: _Resp(_quota_error_payload()))

    with pytest.raises(fc.FirefliesQuotaExceeded) as excinfo:
        fc.fireflies_query("query { transcripts { id } }")

    retry_after = excinfo.value.retry_after
    assert retry_after > datetime.now(timezone.utc)
    assert (retry_after.hour, retry_after.minute) == (0, 0)


def test_non_quota_api_error_still_raises_generic_exception(monkeypatch):
    """Only quota errors get the special treatment; a schema error must not be
    mistaken for one (that would wrongly suspend polling)."""
    payload = {"errors": [{"message": "Cannot query field 'nope'", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}]}
    monkeypatch.setattr(fc.requests, "post", lambda *a, **k: _Resp(payload))

    with pytest.raises(Exception) as excinfo:
        fc.fireflies_query("query { nope }")

    assert not isinstance(excinfo.value, fc.FirefliesQuotaExceeded)
    assert fc.quota_blocked_until() is None


# ------------------------------------------------------------- quota gating


def test_quota_block_short_circuits_without_sending_a_request(monkeypatch):
    """The whole point: once the quota is known-spent, stop calling. Burning
    288 doomed requests/day is what starved the webhook path."""
    reset = datetime.now(timezone.utc) + timedelta(hours=6)
    payload = _quota_error_payload(retry_after_ms=int(reset.timestamp() * 1000))

    calls = {"n": 0}

    def _post(*args, **kwargs):
        calls["n"] += 1
        return _Resp(payload)

    monkeypatch.setattr(fc.requests, "post", _post)

    with pytest.raises(fc.FirefliesQuotaExceeded):
        fc.fireflies_query("query { transcripts { id } }")
    assert calls["n"] == 1

    # Second and third calls must not reach the network at all.
    for _ in range(2):
        with pytest.raises(fc.FirefliesQuotaExceeded):
            fc.fireflies_query("query { transcripts { id } }")
    assert calls["n"] == 1


def test_quota_block_expires_after_reset_time(monkeypatch):
    """A block in the past must not keep the pipeline down forever."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    fc._save_quota_block(past)
    assert fc.quota_blocked_until() is None

    monkeypatch.setattr(fc.requests, "post", lambda *a, **k: _Resp({"data": {"transcripts": []}}))
    assert fc.fireflies_query("query { transcripts { id } }") == {"transcripts": []}


def test_quota_block_survives_restart_via_persisted_file(monkeypatch):
    """State lives on disk, so a container restart cannot re-burn the quota."""
    reset = datetime.now(timezone.utc) + timedelta(hours=3)
    fc._save_quota_block(reset)

    with open(fc.QUOTA_STATE_FILE, "r") as f:
        assert json.load(f)["blocked_until"].startswith(reset.isoformat()[:19])

    assert fc.quota_blocked_until() is not None


def test_clear_quota_block(monkeypatch):
    fc._save_quota_block(datetime.now(timezone.utc) + timedelta(hours=3))
    assert fc.quota_blocked_until() is not None
    fc.clear_quota_block()
    assert fc.quota_blocked_until() is None


def test_corrupt_quota_file_is_treated_as_unblocked(quota_state):
    """A malformed state file must fail open, not wedge ingestion shut."""
    with open(fc.QUOTA_STATE_FILE, "w") as f:
        f.write("{not json")
    assert fc.quota_blocked_until() is None


# ------------------------------------------------------ duration normalizer


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Real values observed from the live API on 2026-08-20.
        (48, 48.0),
        (11.319999694824219, 11.319999694824219),
        (53.25, 53.25),
        (72.08000183105469, 72.08000183105469),
        (0, 0.0),
        (None, 0.0),
        ("", 0.0),
        ("bogus", 0.0),
        (-5, 0.0),             # never extend the window backwards
        (10 ** 9, 600.0),      # garbage is clamped, not rescaled
        (2700, 600.0),         # 45 HOURS as minutes is garbage -> clamped
    ],
)
def test_duration_to_minutes(raw, expected):
    """The field is MINUTES, confirmed against live data on 2026-08-20.

    app.py's Weekly Pulse collector used to divide `duration` by 60 as SECONDS
    while fireflies_client added it as minutes; the Pulse side was wrong, and
    reported a 48-minute meeting to Claude as "1min". Both call sites now share
    this normalizer.
    """
    assert fc.duration_to_minutes(raw) == expected


def test_duration_is_never_rescaled_down():
    """Guard against reintroducing a seconds-detection heuristic: a genuinely
    long recording must not be shrunk 60x and dropped from the poll window."""
    assert fc.duration_to_minutes(500) == 500.0


def test_duration_clamp_bounds_the_poll_window():
    """Whatever the units turn out to be, the window stays bounded."""
    assert fc.duration_to_minutes(10 ** 12) <= fc.MAX_MEETING_MINUTES


# ------------------------------------------------------------- poll gating


def test_poll_skips_entirely_while_quota_is_blocked(monkeypatch):
    """No Fireflies call, and no ERROR-level stack trace spam either."""
    fc._save_quota_block(datetime.now(timezone.utc) + timedelta(hours=5))

    def _boom(*args, **kwargs):
        raise AssertionError("poll must not call Fireflies while quota is blocked")

    monkeypatch.setattr(app_module, "get_recent_transcripts", _boom)
    monkeypatch.setattr(app_module, "drain_deferred_transcripts", _boom)

    app_module.poll_and_process()  # must return quietly


def test_poll_quota_error_does_not_raise(monkeypatch):
    """A spent quota is expected operationally -- the scheduled job must not
    blow up (APScheduler would log a full traceback every 5 minutes)."""
    def _quota(*args, **kwargs):
        raise fc.FirefliesQuotaExceeded("spent", retry_after=datetime.now(timezone.utc) + timedelta(hours=2))

    monkeypatch.setattr(app_module, "get_recent_transcripts", _quota)
    monkeypatch.setattr(app_module, "drain_deferred_transcripts", lambda *a, **k: 0)

    app_module.poll_and_process()


def test_poll_parks_transcript_when_quota_dies_mid_fetch(monkeypatch):
    """The list call succeeded but the body fetch hit the wall: the id must be
    parked, otherwise that meeting is silently lost."""
    monkeypatch.setattr(app_module, "get_recent_transcripts", lambda **k: [{"id": "t-mid"}])
    monkeypatch.setattr(app_module, "drain_deferred_transcripts", lambda *a, **k: 0)

    def _quota(_tid):
        raise fc.FirefliesQuotaExceeded("spent", retry_after=datetime.now(timezone.utc) + timedelta(hours=2))

    monkeypatch.setattr(app_module, "get_transcript_by_id", _quota)

    app_module.poll_and_process()

    assert "t-mid" in app_module.load_fireflies_deferred()


# ---------------------------------------------------------- park and drain


def test_defer_transcript_records_and_is_idempotent():
    app_module.defer_transcript("t1", "quota exhausted")
    first = app_module.load_fireflies_deferred()["t1"]["queued_at"]
    app_module.defer_transcript("t1", "quota exhausted again")
    entry = app_module.load_fireflies_deferred()["t1"]
    assert entry["queued_at"] == first  # original wait time preserved
    assert len(app_module.load_fireflies_deferred()) == 1


def test_drain_recovers_parked_meeting_and_clears_it(monkeypatch):
    """The recovery this whole change exists for: a meeting lost to a dead
    window gets its Phase-1 email once the quota is back."""
    app_module.defer_transcript("t-recover", "quota exhausted")

    monkeypatch.setattr(app_module, "get_transcript_by_id", lambda tid: {"id": tid, "title": "Recovered"})
    ran = {}

    def _phase1(transcript):
        ran["id"] = transcript["id"]
        return "appr1"

    monkeypatch.setattr(app_module, "process_transcript_phase1", _phase1)

    assert app_module.drain_deferred_transcripts() == 1
    assert ran["id"] == "t-recover"
    assert app_module.load_fireflies_deferred() == {}
    assert "t-recover" in app_module.load_processed()


def test_drain_stops_and_keeps_queue_when_quota_dies_again(monkeypatch):
    """Draining must never burn through a fresh quota; the rest stays parked."""
    app_module.defer_transcript("t-a", "quota")
    app_module.defer_transcript("t-b", "quota")

    def _quota(_tid):
        raise fc.FirefliesQuotaExceeded("spent", retry_after=datetime.now(timezone.utc) + timedelta(hours=1))

    monkeypatch.setattr(app_module, "get_transcript_by_id", _quota)
    monkeypatch.setattr(app_module, "process_transcript_phase1", lambda t: "x")

    assert app_module.drain_deferred_transcripts() == 0
    assert set(app_module.load_fireflies_deferred()) == {"t-a", "t-b"}


def test_drain_gives_up_after_max_attempts(monkeypatch):
    """A transcript that will never resolve must not be retried forever."""
    app_module.defer_transcript("t-dead", "never ready")
    monkeypatch.setattr(app_module, "get_transcript_by_id", lambda tid: None)
    monkeypatch.setattr(app_module, "process_transcript_phase1", lambda t: "x")

    for _ in range(app_module.FIREFLIES_DEFER_MAX_ATTEMPTS):
        app_module.drain_deferred_transcripts()

    assert app_module.load_fireflies_deferred() == {}


def test_drain_drops_already_processed_id_without_an_api_call(monkeypatch):
    """Never spend quota re-fetching something already notified."""
    app_module.defer_transcript("t-done", "quota")
    processed = app_module.load_processed()
    processed.add("t-done")
    app_module.save_processed(processed)

    def _boom(_tid):
        raise AssertionError("must not fetch an already-processed transcript")

    monkeypatch.setattr(app_module, "get_transcript_by_id", _boom)

    assert app_module.drain_deferred_transcripts() == 0
    assert app_module.load_fireflies_deferred() == {}


def test_drain_respects_limit(monkeypatch):
    """Bounded per run so a big backlog is spread across reset windows."""
    for i in range(5):
        app_module.defer_transcript(f"t{i}", "quota")

    monkeypatch.setattr(app_module, "get_transcript_by_id", lambda tid: {"id": tid})
    monkeypatch.setattr(app_module, "process_transcript_phase1", lambda t: "a")

    assert app_module.drain_deferred_transcripts(limit=2) == 2
    assert len(app_module.load_fireflies_deferred()) == 3


def test_drain_noop_on_empty_queue(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("no API call for an empty queue")

    monkeypatch.setattr(app_module, "get_transcript_by_id", _boom)
    assert app_module.drain_deferred_transcripts() == 0


# ------------------------------------------------------------ webhook path


def test_webhook_parks_instead_of_retrying_on_quota(monkeypatch, flask_client):
    """The production failure: 3 retries in 90s against a midnight reset, then
    the meeting was dropped. Now it is parked, and no time is wasted sleeping.
    """
    def _quota(_tid):
        raise fc.FirefliesQuotaExceeded("spent", retry_after=datetime.now(timezone.utc) + timedelta(hours=4))

    monkeypatch.setattr(app_module, "get_transcript_by_id", _quota)

    # The route does a function-local `import time`, so patch the real module.
    # Record rather than raise: the assertion belongs in the test body, and the
    # polling loop below still needs a working sleep.
    real_sleep = time.sleep
    slept = []
    monkeypatch.setattr("time.sleep", lambda seconds: slept.append(seconds))

    resp = flask_client.post(
        "/webhook/fireflies",
        json={"meetingId": "01M00NDKDYP0ZN1FRZYV2KA0TX", "eventType": "Transcription completed"},
    )
    assert resp.status_code == 200

    # Phase 1 runs on a background thread -- wait for the parked id to appear.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "01M00NDKDYP0ZN1FRZYV2KA0TX" in app_module.load_fireflies_deferred():
            break
        real_sleep(0.02)

    assert "01M00NDKDYP0ZN1FRZYV2KA0TX" in app_module.load_fireflies_deferred()
    # Never burn 15/30/45s of retries against a quota that resets at midnight.
    assert slept == []


# ------------------------------------------------------- /fireflies/status


def test_status_endpoint_reports_clear_state(flask_client):
    body = flask_client.get("/fireflies/status").get_json()
    assert body["quota_blocked"] is False
    assert body["quota_frees_up"] is None
    assert body["deferred_count"] == 0
    assert body["poll_calls_per_day"] == round(1440 / app_module.POLL_INTERVAL_MINUTES)


def test_status_endpoint_reports_block_and_parked_queue(flask_client):
    """The endpoint exists so this state is visible without trawling Railway
    logs -- it must make no Fireflies call, so it is safe while blocked."""
    reset = datetime.now(timezone.utc) + timedelta(hours=7)
    fc._save_quota_block(reset)
    app_module.defer_transcript("t-parked", "quota exhausted when webhook arrived")

    body = flask_client.get("/fireflies/status").get_json()

    assert body["quota_blocked"] is True
    assert body["quota_frees_up"].startswith(reset.isoformat()[:19])
    assert body["deferred_count"] == 1
    assert body["deferred"]["t-parked"]["reason"] == "quota exhausted when webhook arrived"
    assert body["deferred"]["t-parked"]["attempts"] == 0


# ------------------------------------------------- /process/<id> bookkeeping


def test_manual_process_records_transcript_as_processed(monkeypatch, flask_client):
    """Replaying via /process must record the id.

    Without it, replaying a meeting still inside the poll window
    (POLL_INTERVAL + 10 min) let the next poll process it again and send the
    organizer a SECOND notification -- and it broke the invariant that a
    transcript in the processed store has already been notified.
    """
    monkeypatch.setattr(app_module, "get_transcript_by_id", lambda tid: {"id": tid, "title": "Replayed"})
    monkeypatch.setattr(app_module, "process_transcript_phase1", lambda t: "appr-x")

    resp = flask_client.post("/process/t-replay")
    assert resp.status_code == 200

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if "t-replay" in app_module.load_processed():
            break
        time.sleep(0.02)

    assert "t-replay" in app_module.load_processed()


def test_manual_process_does_not_record_when_transcript_missing(monkeypatch, flask_client):
    """A 404 from Fireflies must not poison the processed store -- otherwise a
    transcript that later becomes available would be skipped forever."""
    monkeypatch.setattr(app_module, "get_transcript_by_id", lambda tid: None)

    def _boom(_t):
        raise AssertionError("Phase 1 must not run without a transcript")

    monkeypatch.setattr(app_module, "process_transcript_phase1", _boom)

    resp = flask_client.post("/process/t-missing")
    assert resp.status_code == 200

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        time.sleep(0.05)

    assert "t-missing" not in app_module.load_processed()


def test_manual_process_still_replays_an_already_processed_id(monkeypatch, flask_client):
    """Manual replay must NOT skip a known id -- that is what the endpoint is
    for (it is how the 08-06..08-19 backlog was recovered)."""
    processed = app_module.load_processed()
    processed.add("t-known")
    app_module.save_processed(processed)

    ran = {}
    monkeypatch.setattr(app_module, "get_transcript_by_id", lambda tid: {"id": tid})
    monkeypatch.setattr(app_module, "process_transcript_phase1", lambda t: ran.setdefault("id", t["id"]))

    resp = flask_client.post("/process/t-known")
    assert resp.status_code == 200

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ran.get("id"):
            break
        time.sleep(0.02)

    assert ran.get("id") == "t-known"
