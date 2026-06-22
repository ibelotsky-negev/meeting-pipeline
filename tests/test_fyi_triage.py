# Tests for the FYI Triage module (fyi_triage.py).
#
# All offline + mocked. Graph is reached only through eps.graph_get/graph_post,
# which tests monkeypatch; the Anthropic classifier takes an injectable call_fn
# so the LLM is never reached. The classification QUALITY (does the real model
# agree with Ken?) is deliberately NOT asserted here -- that is what Ken reviews
# in STATE B of the gated rollout. These tests pin the deterministic behavior:
# parsing, body-read wiring, the dual gate, the lookback window, dedup, the
# single-run lock, and the move safety guard. When Ken finds a misclassification
# in STATE B, the loop adds it here as a permanent case (the rubric only tightens).
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

import fyi_triage as ft


# ----------------------------------------------------------------------
#  Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def fyi_files(monkeypatch, tmp_path):
    """Redirect all FYI state files into a per-test temp dir and clear caches."""
    monkeypatch.setattr(ft, "FYI_LOCK_FILE", str(tmp_path / "fyi_lock.json"))
    monkeypatch.setattr(ft, "FYI_PROCESSED_FILE", str(tmp_path / "fyi_processed.json"))
    monkeypatch.setattr(ft, "FYI_STATUS_FILE", str(tmp_path / "fyi_status.json"))
    ft._folder_id_cache.clear()
    yield tmp_path
    ft._folder_id_cache.clear()


# The three folder ids used across the run tests.
FYI_ID = "DEST-FYI"
SRC_NOTIF = "SRC-NOTIFICATION"
SRC_MKTG = "SRC-MARKETING"


@pytest.fixture
def stub_folders(monkeypatch):
    """Stub resolve_folder_map AND populate the id cache (so the real
    _assert_safe_move / move guard sees the resolved dest)."""
    mapping = {
        ft.DEST_FOLDER_NAME: FYI_ID,
        "4: notification": SRC_NOTIF,
        "8: marketing": SRC_MKTG,
    }

    def _resolve(force=False):
        ft._folder_id_cache.update(mapping)
        return dict(mapping)

    monkeypatch.setattr(ft, "resolve_folder_map", _resolve)
    return mapping


def _msg(mid, subject, sender, body="", folder_received="2026-06-20T10:00:00Z"):
    return {
        "id": mid, "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "body": {"contentType": "html", "content": body},
        "bodyPreview": body[:200], "receivedDateTime": folder_received,
        "webLink": f"https://outlook/{mid}", "isRead": True,
    }


class _AlwaysAcquireLock:
    """Stands in for the in-process lock to simulate a second worker process,
    where each process has its own (uncontended) threading.Lock."""

    def acquire(self, blocking=True):
        return True

    def release(self):
        pass

    def locked(self):
        return False


# ======================================================================
#  1. Classifier: parses verdict, reads the BODY, safe default on garbage,
#     and the 6 confirmed exemplars + representative noise route correctly.
# ======================================================================

# Realistic message fixtures from Ken's calibration. NOTE several "IMPORTANT"
# ones arrive from bulk/automation senders -- the signal is in the BODY, which is
# exactly why the classifier must read it.
EXEMPLARS_IMPORTANT = [
    _msg("imp1", "Signature requested by YC Safes",
         "noreply@mail.hellosign.com",
         "Safe for Kinro (Pierre-Alexandre Kamienny) - investment by Ken Belotsky. Please sign."),
    _msg("imp2", "New form submission on Webflow for Negev-Labs",
         "no-reply-forms@webflow.com",
         "Noah Petermann, CEO of Aniva Health, submitted the contact form: 'I'd like to discuss a raise.'"),
    _msg("imp3", "Solvonis Announces Result of Annual General Meeting",
         "info@solvonis.com",
         "The Company announces the results of its Annual General Meeting held today. All resolutions passed."),
    _msg("imp4", "Sponsorship opportunity -- psychedelics conference",
         "events@psychsummit.org",
         "Hi Ken, this is Yanis Dida. I'd love to discuss Negev sponsoring our psychedelics conference."),
    _msg("imp5", "Concentric AI Series C invite",
         "investors@concentric.ai",
         "We are opening our Series C and would like to invite Negev Labs to participate with an allocation."),
    _msg("imp6", "Relay banking invitation",
         "invites@relayfi.com",
         "Alexander Kubasov invited you to set up Negev's Relay business banking account."),
]

EXEMPLARS_NOISE = [
    _msg("n1", "Fireflies.ai Notetaker Ken has joined your meeting", "no-reply@zoom.us",
         "Fireflies.ai Notetaker Ken has joined your meeting. Topic: Maria and Ken."),
    _msg("n2", "158 people visited your profile", "messages-noreply@linkedin.com",
         "See who has been viewing your LinkedIn profile this week."),
    _msg("n3", "Ship your first AI agent in a day", "theaicorner1@substack.com",
         "This week's newsletter: build an AI agent fast. Read more and subscribe."),
    _msg("n4", "Your Trademark Login OTP is 1199", "customer.service@trademarkia.com",
         "Your one-time verification code is 1199. Do not share it."),
    _msg("n5", "Catch up on yesterday in 2 minutes", "fred@fireflies.ai",
         "Your meeting recap is ready. Here is what happened yesterday."),
    _msg("n6", "New Event: Josh Ismin - 08:00am Mon - Online Zoom call (30 min)",
         "notifications@calendly.com",
         "A new event has been scheduled via Calendly. Join the Zoom call."),
]

# "Named individual inside an otherwise-marketing email" -- must be IMPORTANT.
NAMED_INSIDE_MARKETING = _msg(
    "named1", "Weekly portfolio digest", "newsletter@somefund.com",
    "Markets roundup ... PS from James Lanthier: Ken, separately -- can we talk about co-investing "
    "in your next round? Reply when you have a minute.")


def _oracle(prompt, model):
    """Stand-in for the Sonnet classifier: an oracle keyed on body content cues,
    used to prove the exemplar/noise FIXTURES carry their distinguishing signal in
    the prompt the model receives. NOT a test of the real model (that is STATE B).

    Inspects ONLY the email portion of the prompt -- the rubric text above it also
    contains these cue phrases (as worked examples), so matching the whole prompt
    would always fire."""
    low = prompt.split("Email:\n", 1)[-1].lower()
    noise_cues = ("notetaker has joined", "linkedin profile", "newsletter", "verification code",
                  "meeting recap", "scheduled via calendly")
    important_cues = ("please sign", "submitted the contact form", "annual general meeting",
                      "i'd love to discuss negev sponsoring", "series c", "invited you to set up",
                      "co-investing")
    if any(c in low for c in important_cues):
        return json.dumps({"decision": "IMPORTANT", "reason": "real action/person/deal in body"})
    if any(c in low for c in noise_cues):
        return json.dumps({"decision": "NOISE", "reason": "automation/social/newsletter/otp"})
    return json.dumps({"decision": "NOISE", "reason": "no clear signal"})


class TestClassifier:
    def test_parses_important_and_noise(self):
        d, r = ft.classify_message(_msg("x", "s", "a@b.com", "body"),
                                   call_fn=lambda p, m: '{"decision":"IMPORTANT","reason":"deal"}')
        assert d == "IMPORTANT" and r == "deal"
        d, r = ft.classify_message(_msg("x", "s", "a@b.com", "body"),
                                   call_fn=lambda p, m: '{"decision":"NOISE","reason":"newsletter"}')
        assert d == "NOISE"

    def test_unparseable_reply_defaults_noise(self):
        d, r = ft.classify_message(_msg("x", "s", "a@b.com", "body"),
                                   call_fn=lambda p, m: "the model rambled with no json")
        assert d == "NOISE" and "NOISE" in r.upper()

    def test_unknown_decision_value_defaults_noise(self):
        d, _ = ft.classify_message(_msg("x", "s", "a@b.com", "b"),
                                   call_fn=lambda p, m: '{"decision":"MAYBE","reason":"?"}')
        assert d == "NOISE"

    def test_body_and_sender_are_in_the_prompt(self):
        captured = {}

        def cap(prompt, model):
            captured["prompt"] = prompt
            return '{"decision":"IMPORTANT","reason":"x"}'

        ft.classify_message(NAMED_INSIDE_MARKETING, call_fn=cap)
        # The from-address AND the body's personal note must both reach the model
        # (we do not judge by from-address alone).
        assert "newsletter@somefund.com" in captured["prompt"]
        assert "co-investing" in captured["prompt"]

    @pytest.mark.parametrize("msg", EXEMPLARS_IMPORTANT, ids=lambda m: m["id"])
    def test_six_exemplars_route_important(self, msg):
        d, _ = ft.classify_message(msg, call_fn=_oracle)
        assert d == "IMPORTANT"

    @pytest.mark.parametrize("msg", EXEMPLARS_NOISE, ids=lambda m: m["id"])
    def test_representative_noise_routes_noise(self, msg):
        d, _ = ft.classify_message(msg, call_fn=_oracle)
        assert d == "NOISE"

    def test_named_individual_inside_marketing_is_important(self):
        d, _ = ft.classify_message(NAMED_INSIDE_MARKETING, call_fn=_oracle)
        assert d == "IMPORTANT"


# ======================================================================
#  2. Dry-run vs live: dry moves nothing & writes no ids; live moves exactly
#     one per IMPORTANT and records the moved id.
# ======================================================================

def _classify_by_subject(monkeypatch):
    """Important iff the subject starts with 'IMP'."""
    monkeypatch.setattr(ft, "classify_message",
                        lambda m, call_fn=None: ("IMPORTANT", "imp") if m["subject"].startswith("IMP")
                        else ("NOISE", "noise"))


class TestDryVsLive:
    def _two_messages(self, monkeypatch):
        # one IMPORTANT, one NOISE in the notification folder; none in marketing
        def fetch(folder_id, since_iso, processed_ids=None, backlog=False, limit=None):
            if folder_id == SRC_NOTIF:
                return ([_msg("a", "IMP signature", "x@y.com"),
                         _msg("b", "newsletter", "n@l.com")], False)
            return ([], False)
        monkeypatch.setattr(ft, "fetch_messages", fetch)

    def test_dry_run_moves_nothing_writes_no_ids(self, fyi_files, stub_folders, monkeypatch):
        self._two_messages(monkeypatch)
        _classify_by_subject(monkeypatch)
        move = MagicMock(return_value=True)
        monkeypatch.setattr(ft, "move_to_fyi", move)
        res = ft.run_fyi(dry_run=True)
        assert res["dry_run"] is True
        assert res["important"] == 1 and res["moved"] == 0
        assert move.call_count == 0
        assert ft._load_processed_ids() == set()  # nothing recorded

    def test_live_moves_one_per_important_and_records_id(self, fyi_files, stub_folders, monkeypatch):
        self._two_messages(monkeypatch)
        _classify_by_subject(monkeypatch)
        monkeypatch.setenv("FYI_LIVE", "1")
        move = MagicMock(return_value=True)
        monkeypatch.setattr(ft, "move_to_fyi", move)
        res = ft.run_fyi(live=True)
        assert res["dry_run"] is False
        assert res["important"] == 1 and res["moved"] == 1
        assert move.call_count == 1
        # Moved to the resolved FYI folder, never a source.
        assert move.call_args.args[1] == FYI_ID
        ids = ft._load_processed_ids()
        assert "a" in ids        # the moved IMPORTANT message
        assert "b" in ids        # the confidently-classified NOISE is also recorded


# ======================================================================
#  3. Dual gate: a move requires BOTH ?live=1 AND env FYI_LIVE=1.
# ======================================================================

class TestDualGate:
    def _one_important(self, monkeypatch):
        monkeypatch.setattr(ft, "fetch_messages",
                            lambda fid, s, processed_ids=None, backlog=False, limit=None:
                            ([_msg("a", "IMP x", "x@y.com")], False) if fid == SRC_NOTIF else ([], False))
        _classify_by_subject(monkeypatch)
        move = MagicMock(return_value=True)
        monkeypatch.setattr(ft, "move_to_fyi", move)
        return move

    def test_live_flag_without_env_stays_dry(self, fyi_files, stub_folders, monkeypatch):
        monkeypatch.delenv("FYI_LIVE", raising=False)
        move = self._one_important(monkeypatch)
        res = ft.run_fyi(live=True)
        assert res["dry_run"] is True and move.call_count == 0

    def test_env_without_live_flag_stays_dry(self, fyi_files, stub_folders, monkeypatch):
        monkeypatch.setenv("FYI_LIVE", "1")
        move = self._one_important(monkeypatch)
        res = ft.run_fyi(live=False)   # e.g. a manual run that did not pass ?live=1
        assert res["dry_run"] is True and move.call_count == 0

    def test_both_gates_open_runs_live(self, fyi_files, stub_folders, monkeypatch):
        monkeypatch.setenv("FYI_LIVE", "1")
        move = self._one_important(monkeypatch)
        res = ft.run_fyi(live=True)
        assert res["dry_run"] is False and move.call_count == 1

    def test_explicit_dry_run_overrides_open_gate(self, fyi_files, stub_folders, monkeypatch):
        monkeypatch.setenv("FYI_LIVE", "1")
        move = self._one_important(monkeypatch)
        res = ft.run_fyi(dry_run=True, live=True)
        assert res["dry_run"] is True and move.call_count == 0


# ======================================================================
#  4. Window selection: days override; cron default uses FYI_LOOKBACK_HOURS.
# ======================================================================

class TestWindow:
    def _approx_cutoff(self, iso, expected_delta_seconds, tol=120):
        from datetime import datetime, timezone
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        actual = (datetime.now(timezone.utc) - dt).total_seconds()
        assert abs(actual - expected_delta_seconds) <= tol, (actual, expected_delta_seconds)

    def test_days_7(self):
        self._approx_cutoff(ft._cutoff_iso(days=7), 7 * 86400)

    def test_days_30(self):
        self._approx_cutoff(ft._cutoff_iso(days=30), 30 * 86400)

    def test_default_uses_lookback_hours(self, monkeypatch):
        monkeypatch.setattr(ft, "FYI_LOOKBACK_HOURS", 24)
        self._approx_cutoff(ft._cutoff_iso(), 24 * 3600)

    def test_fetch_filter_carries_cutoff(self, monkeypatch):
        captured = {}

        def fake_get(url, params=None):
            captured["params"] = params
            return {"value": []}

        monkeypatch.setattr(ft.eps, "graph_get", fake_get)
        ft.fetch_messages(SRC_NOTIF, "2026-06-15T00:00:00Z")
        assert "receivedDateTime ge 2026-06-15T00:00:00Z" in captured["params"]["$filter"]


# ======================================================================
#  5. Dedup: processed ids are skipped on fetch; backlog re-processes; a dry
#     run writes no ids so a later backfill still sees everything.
# ======================================================================

class TestDedup:
    def test_processed_id_skipped_and_backlog_reprocesses(self, monkeypatch):
        page = {"value": [_msg("seen-1", "old", "a@b.com"), _msg("fresh-2", "new", "c@d.com")]}
        monkeypatch.setattr(ft.eps, "graph_get", lambda url, params=None: page)
        msgs, _ = ft.fetch_messages(SRC_NOTIF, "2026-06-01T00:00:00Z", processed_ids={"seen-1"})
        assert [m["id"] for m in msgs] == ["fresh-2"]
        # backlog ignores the processed store -> both come back
        msgs2, _ = ft.fetch_messages(SRC_NOTIF, "2026-06-01T00:00:00Z",
                                     processed_ids={"seen-1"}, backlog=True)
        assert [m["id"] for m in msgs2] == ["seen-1", "fresh-2"]

    def test_dry_run_writes_no_ids_so_backfill_sees_all(self, fyi_files, stub_folders, monkeypatch):
        monkeypatch.setattr(ft, "fetch_messages",
                            lambda fid, s, processed_ids=None, backlog=False, limit=None:
                            ([_msg("a", "IMP x", "x@y.com")], False) if fid == SRC_NOTIF else ([], False))
        _classify_by_subject(monkeypatch)
        monkeypatch.setattr(ft, "move_to_fyi", MagicMock(return_value=True))
        ft.run_fyi(dry_run=True)
        assert ft._load_processed_ids() == set()


# ======================================================================
#  6. Single-run lock: two concurrent workers -> exactly one run.
# ======================================================================

class TestSingleRunLock:
    def test_two_concurrent_workers_one_run(self, fyi_files, stub_folders, monkeypatch):
        monkeypatch.setattr(ft, "_fyi_lock", _AlwaysAcquireLock())  # simulate two processes
        monkeypatch.setenv("FYI_LIVE", "1")
        monkeypatch.setattr(ft, "fetch_messages",
                            lambda fid, s, processed_ids=None, backlog=False, limit=None:
                            ([_msg("a", "IMP x", "x@y.com")], False) if fid == SRC_NOTIF else ([], False))

        def slow_classify(m, call_fn=None):
            time.sleep(0.4)  # hold the run open so the second fire overlaps
            return ("IMPORTANT", "imp")
        monkeypatch.setattr(ft, "classify_message", slow_classify)
        move = MagicMock(return_value=True)
        monkeypatch.setattr(ft, "move_to_fyi", move)

        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait(timeout=5)
            results.append(ft.run_fyi(live=True))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        oks = [r for r in results if r.get("status") == "ok"]
        skipped = [r for r in results if r.get("status") == "skipped"]
        assert len(oks) == 1 and len(skipped) == 1, results
        assert move.call_count == 1, "the atomic file lock failed to reject the second run"

    def test_lock_acquire_release_and_stale_reclaim(self, fyi_files, monkeypatch):
        import os
        assert ft._acquire_run_lock() is True
        assert ft._acquire_run_lock() is False           # held
        ft._release_run_lock()
        assert ft._acquire_run_lock() is True             # released -> reacquire
        # Backdate the lock past staleness -> reclaimed.
        stale = time.time() - ft.FYI_LOCK_MAX_AGE - 60
        os.utime(ft.FYI_LOCK_FILE, (stale, stale))
        assert ft._acquire_run_lock() is True
        ft._release_run_lock()

    def test_touch_refreshes_lock_so_long_run_is_not_reclaimed(self, fyi_files):
        # Regression guard for the stale-reclaim race: a long live backfill must
        # keep its lock fresh so a concurrent worker (e.g. the daily cron) does
        # NOT reclaim it and start a second run against the same folders.
        import os
        assert ft._acquire_run_lock() is True
        stale = time.time() - ft.FYI_LOCK_MAX_AGE - 60
        os.utime(ft.FYI_LOCK_FILE, (stale, stale))   # would normally be reclaimed
        ft._touch_run_lock()                          # the run heartbeats its lock
        assert ft._acquire_run_lock() is False        # fresh again -> still held
        ft._release_run_lock()

    def test_force_clears_orphaned_lock(self, fyi_files, stub_folders, monkeypatch):
        with open(ft.FYI_LOCK_FILE, "w") as f:
            f.write("{}")
        monkeypatch.setattr(ft, "fetch_messages",
                            lambda *a, **k: ([], False))
        # A fresh (non-stale) lock blocks without force.
        assert ft.run_fyi(dry_run=True, force=False)["status"] == "skipped"
        # force clears it and the run proceeds.
        assert ft.run_fyi(dry_run=True, force=True)["status"] == "ok"
        assert ft._acquire_run_lock() is True
        ft._release_run_lock()


# ======================================================================
#  7. Safety: destination is always "2: FYI"; never a source folder.
# ======================================================================

class TestMoveSafety:
    def test_assert_safe_move_accepts_fyi_dest(self, fyi_files):
        ft._folder_id_cache.update({ft.DEST_FOLDER_NAME: FYI_ID})
        ft._assert_safe_move(FYI_ID, {SRC_NOTIF, SRC_MKTG})  # must not raise

    def test_assert_safe_move_rejects_source_dest(self, fyi_files):
        ft._folder_id_cache.update({ft.DEST_FOLDER_NAME: FYI_ID})
        with pytest.raises(RuntimeError):
            ft._assert_safe_move(SRC_NOTIF, {SRC_NOTIF, SRC_MKTG})

    def test_assert_safe_move_rejects_non_fyi_dest(self, fyi_files):
        ft._folder_id_cache.update({ft.DEST_FOLDER_NAME: FYI_ID})
        with pytest.raises(RuntimeError):
            ft._assert_safe_move("SOME-OTHER-FOLDER", {SRC_NOTIF, SRC_MKTG})

    def test_move_targets_fyi_and_posts_to_move_endpoint(self, fyi_files, monkeypatch):
        ft._folder_id_cache.update({ft.DEST_FOLDER_NAME: FYI_ID})
        captured = {}

        def fake_post(url, body):
            captured["url"] = url
            captured["body"] = body
            return {}

        monkeypatch.setattr(ft.eps, "graph_post", fake_post)
        ok = ft.move_to_fyi("msg-1", FYI_ID, {SRC_NOTIF, SRC_MKTG})
        assert ok is True
        assert captured["url"].endswith("/messages/msg-1/move")
        assert captured["body"] == {"destinationId": FYI_ID}

    def test_move_refuses_when_dest_is_a_source(self, fyi_files, monkeypatch):
        ft._folder_id_cache.update({ft.DEST_FOLDER_NAME: FYI_ID})
        post = MagicMock()
        monkeypatch.setattr(ft.eps, "graph_post", post)
        # move_to_fyi calls _assert_safe_move first, which raises -> no POST.
        with pytest.raises(RuntimeError):
            ft.move_to_fyi("msg-1", SRC_NOTIF, {SRC_NOTIF, SRC_MKTG})
        assert post.call_count == 0

    def test_resolve_map_rejects_dest_equal_to_source(self, fyi_files, monkeypatch):
        # If the FYI folder somehow resolves to the same id as a source, abort.
        clash = "SAME"
        monkeypatch.setattr(ft, "_walk_mail_folders",
                            lambda *a, **k: {ft.DEST_FOLDER_NAME: clash,
                                             "4: notification": clash, "8: marketing": SRC_MKTG})
        with pytest.raises(RuntimeError):
            ft.resolve_folder_map(force=True)


# ======================================================================
#  8. Folder resolution: live by display name; cross-check warns but trusts.
# ======================================================================

class TestFolderResolution:
    def test_resolves_all_three_by_display_name(self, fyi_files, monkeypatch):
        monkeypatch.setattr(ft, "_walk_mail_folders",
                            lambda *a, **k: {"2: FYI": "F", "4: notification": "N",
                                             "8: marketing": "M", "Inbox": "I"})
        m = ft.resolve_folder_map(force=True)
        assert m == {"2: FYI": "F", "4: notification": "N", "8: marketing": "M"}

    def test_missing_folder_raises(self, fyi_files, monkeypatch):
        monkeypatch.setattr(ft, "_walk_mail_folders",
                            lambda *a, **k: {"2: FYI": "F", "4: notification": "N"})  # marketing missing
        with pytest.raises(RuntimeError):
            ft.resolve_folder_map(force=True)

    def test_expected_ids_match_transcribed_crosscheck(self):
        # Guard the cross-check constants against silent edits.
        assert ft.EXPECTED_FOLDER_IDS["2: FYI"].endswith("baALi-AAA=")
        assert ft.EXPECTED_FOLDER_IDS["4: notification"].endswith("baALi9AAA=")
        assert ft.EXPECTED_FOLDER_IDS["8: marketing"].endswith("baALi5AAA=")


# ======================================================================
#  9. fyi_live_enabled reads env at call time.
# ======================================================================

class TestLiveEnv:
    def test_reads_env_at_call_time(self, monkeypatch):
        monkeypatch.delenv("FYI_LIVE", raising=False)
        assert ft.fyi_live_enabled() is False
        monkeypatch.setenv("FYI_LIVE", "1")
        assert ft.fyi_live_enabled() is True
        monkeypatch.setenv("FYI_LIVE", "0")
        assert ft.fyi_live_enabled() is False


# ======================================================================
#  10. /fyi/run + /fyi/status endpoints (thin wrappers).
# ======================================================================

class TestEndpoints:
    def test_status_returns_module_status(self, flask_client, monkeypatch):
        monkeypatch.setattr(ft, "read_status",
                            lambda: {"status": "ok", "moved": 2, "live_progress": {}})
        resp = flask_client.get("/fyi/status")
        assert resp.status_code == 200
        assert resp.get_json()["moved"] == 2

    def test_sync_run_returns_result(self, flask_client, monkeypatch):
        monkeypatch.setattr(ft, "run_fyi",
                            lambda dry_run=None, days=None, live=False, backlog=False,
                            force=False, limit=None, send_summary=False:
                            {"status": "ok", "dry_run": not (live), "window": f"{days}d" if days else "24h"})
        resp = flask_client.get("/fyi/run?sync=true&days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok" and data["window"] == "7d"

    def test_returns_409_when_already_running(self, flask_client):
        import app as app_module
        assert app_module._fyi_trigger_lock.acquire(blocking=False) is True
        try:
            resp = flask_client.get("/fyi/run")
            assert resp.status_code == 409
            assert resp.get_json()["status"] == "already_running"
        finally:
            app_module._fyi_trigger_lock.release()
