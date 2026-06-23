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


def _msg(mid, subject, sender, body="", folder_received=None):
    if folder_received is None:
        # Default to "recent" so messages survive the recency guard in any
        # reasonable window; tests that exercise recency pass an explicit date.
        from datetime import datetime, timezone, timedelta
        folder_received = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    # Automated urgency-bait (LEAK 2): NOISE despite "action required"/"important update".
    urgency_bait = ("model will be retired", "retirement notice", "automated product notice",
                    "important update regarding your paypal")
    noise_cues = ("notetaker has joined", "linkedin profile", "newsletter", "verification code",
                  "meeting recap", "scheduled via calendly", "market outlook", "webinar") + urgency_bait
    # 1:1 personally-executed action / named ask to Ken (LEAK 2 keepers + LEAK 3 1:1) plus the
    # STATE D deal-flow signal: a specific company + round/allocation + an ACTION ASK (confirm
    # interest / commit / participate). These are body-content cues -- language- and sender-
    # agnostic -- so a Russian "Replit Series D" allocation offer surfaces while a generic
    # "market outlook" digest (any language) does not.
    important_cues = ("please sign", "submitted the contact form", "annual general meeting",
                      "i'd love to discuss negev sponsoring", "series c", "invited you to set up",
                      "co-investing", "authorize a card payment", "take an allocation",
                      "confirm your interest", "confirm participation")
    # Urgency-bait is checked FIRST so a "please sign"-free automated notice cannot
    # be rescued by an incidental keyword.
    if any(c in low for c in urgency_bait):
        return json.dumps({"decision": "NOISE", "reason": "automated urgency-bait, no personal action"})
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

# ======================================================================
#  11. STATE B precision fixes -- over-inclusion leaks + dedup + recency.
#  Each case below is a permanent regression test for a confirmed false
#  positive from the first 7-day dry-run (79/394 flagged, target ~3-5%).
# ======================================================================

class TestLeak1OwnOutbound:
    """Mail FROM an internal domain is NOISE (own outbound / sent-copy / reply
    thread) even when addressed to investors and full of substance. Deterministic
    -- no LLM call."""

    def test_internal_sender_is_noise_without_llm(self):
        called = {"n": 0}

        def boom(p, m):
            called["n"] += 1
            return '{"decision":"IMPORTANT","reason":"lots of substance"}'

        # The "Negev Labs Q2 2026 Update" cluster: from bk@negevcap.com to external investors.
        msg = _msg("own1", "Negev Labs Q2 2026 Update", "bk@negevcap.com",
                   "Dear investors, our Q2 update: revenue, the raise, MJFF co-funding, wire details...")
        d, _ = ft.classify_message(msg, call_fn=boom)
        assert d == "NOISE"
        assert called["n"] == 0  # decided deterministically, never reached the model

    def test_test_send_from_internal_is_noise(self):
        msg = _msg("own2", "Test send - Negev Labs - Shareholder Letter", "bk@negevlabs.com",
                   "Testing delivery of the shareholder letter.")
        d, _ = ft.classify_message(msg, call_fn=lambda p, m: '{"decision":"IMPORTANT","reason":"x"}')
        assert d == "NOISE"

    def test_is_internal_sender_helper(self):
        for s in ("bk@negevcap.com", "bk@negevlabs.com", "x@ariadnebio.com",
                  "y@adres.bio", "z@zirmania.onmicrosoft.com"):
            assert ft._is_internal_sender(s) is True, s
        assert ft._is_internal_sender("ceo@external.com") is False
        assert ft._is_internal_sender("") is False


class TestLeak2UrgencyBait:
    """Automated product/service/account notices are NOISE even with 'Action
    required' / 'Important update', UNLESS they require a money movement or a
    signature Ken must personally execute."""

    def test_claude_retirement_notice_is_noise(self):
        msg = _msg("ub1", "[Action required] Retirement notice for Claude Sonnet 4",
                   "notice@email.anthropic.com",
                   "The Claude Sonnet 4 model will be retired. Migrate to a newer model id. "
                   "This is an automated product notice; no payment or signature is needed.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "NOISE"

    def test_paypal_important_update_is_noise(self):
        msg = _msg("ub2", "Reminder: Important update regarding your PayPal account",
                   "noreply@news.paypal.com",
                   "Important update regarding your PayPal account. Review our updated policy.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "NOISE"

    def test_signature_request_still_important(self):
        msg = _msg("ub3", "Signature requested by YC Safes", "noreply@mail.hellosign.com",
                   "Please sign the SAFE for Kinro - investment by Ken Belotsky.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "IMPORTANT"

    def test_payment_authorization_still_important(self):
        msg = _msg("ub4", "Authorize your card payment", "noreply@revolut.com",
                   "Please authorize a card payment of $4,200 to complete the transaction.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "IMPORTANT"


class TestLeak3Broadcast:
    """A named individual writing TO Ken = IMPORTANT. A one-to-many broadcast
    that merely CONTAINS deal specifics = NOISE, even with a real name in the
    signature. The clearest broker/ESP-infra cases are deterministic."""

    def test_vccross_offer_blast_is_noise_without_llm(self):
        called = {"n": 0}

        def boom(p, m):
            called["n"] += 1
            return '{"decision":"IMPORTANT","reason":"deal specifics"}'

        msg = _msg("bc1", "OFFERS// Crusoe, Figure AI, Anthropic secondary", "invest@vccross.com",
                   "We are offering secondary allocations in Crusoe, Figure AI, Anthropic...")
        d, _ = ft.classify_message(msg, call_fn=boom)
        assert d == "NOISE" and called["n"] == 0

    def test_lafferty_offer_is_noise(self):
        msg = _msg("bc2", "OFFER// Replit / Figure", "jgelet@rflafferty.com", "Broker offer blast.")
        assert ft.classify_message(msg, call_fn=lambda p, m: '{"decision":"IMPORTANT"}')[0] == "NOISE"

    def test_iangels_reminder_pitch_is_noise(self):
        msg = _msg("bc3", "REMINDER: Invitation to invest in SportsCenter", "myteam@iangels.com",
                   "Reminder to join the SportsCenter syndicate round.")
        assert ft.classify_message(msg, call_fn=lambda p, m: '{"decision":"IMPORTANT"}')[0] == "NOISE"

    def test_ccsend_capital_raise_is_noise(self):
        msg = _msg("bc4", "Reminder: LingoPure Capital Raise", "noreply@shared1.ccsend.com",
                   "LingoPure is raising; here is the deck.")
        assert ft.classify_message(msg, call_fn=lambda p, m: '{"decision":"IMPORTANT"}')[0] == "NOISE"

    def test_named_individual_to_ken_is_important(self):
        # Forge/Dakota style: not internal, not broadcast infra -> reaches the model,
        # which keeps a 1:1 named-person specific ask.
        msg = _msg("bc5", "Zirmania Team, Secondary Opportunity in Ramp", "matthew.bell@forgeglobal.com",
                   "Hi Ken, we have a secondary opportunity in Ramp at a specific price -- "
                   "can Zirmania take an allocation this week? Best, Matthew")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "IMPORTANT"

    def test_is_broadcast_helper(self):
        assert ft._is_broadcast(_msg("x", "OFFER// foo", "a@b.com", "")) is True
        assert ft._is_broadcast(_msg("x", "OFFERS// foo", "a@b.com", "")) is True
        assert ft._is_broadcast(_msg("x", "hi", "anyone@vccross.com", "")) is True
        assert ft._is_broadcast(_msg("x", "hi", "x@shared1.ccsend.com", "")) is True
        # Keepers must NOT be flagged as broadcast.
        assert ft._is_broadcast(_msg("x", "Re: Negev Capital // Dakota", "jk@dakota.com", "")) is False
        assert ft._is_broadcast(_msg("x", "Zirmania Team, Secondary in Ramp",
                                      "matthew.bell@forgeglobal.com", "")) is False


class TestRecencyGuard:
    def test_received_in_window_helper(self):
        cut = "2026-06-15T00:00:00Z"
        assert ft._received_in_window({"receivedDateTime": "2026-06-22T10:00:00Z"}, cut) is True
        assert ft._received_in_window({"receivedDateTime": "2026-01-10T10:00:00Z"}, cut) is False
        assert ft._received_in_window({}, cut) is True  # unknown received -> do not drop

    def test_stale_item_not_surfaced(self, fyi_files, stub_folders, monkeypatch):
        stale = _msg("xylo", "Xylo Bio Update, January 2026", "updates@xylobio.com",
                     "Our January 2026 update.", folder_received="2026-01-15T10:00:00Z")
        fresh = _msg("fresh", "IMP real ask", "ceo@startup.com", "A real ask.")  # default -> recent
        monkeypatch.setattr(ft, "fetch_messages",
                            lambda fid, s, processed_ids=None, backlog=False, limit=None:
                            ([stale, fresh], False) if fid == SRC_NOTIF else ([], False))
        monkeypatch.setattr(ft, "classify_message", lambda m, call_fn=None: ("IMPORTANT", "x"))
        res = ft.run_fyi(dry_run=True, days=7)
        ids = [d["id"] for d in res["decisions"]]
        assert "xylo" not in ids   # dropped by the recency guard
        assert "fresh" in ids


class TestRunDedup:
    def test_five_identical_invites_one_important(self, fyi_files, stub_folders, monkeypatch):
        subs = ["Kubasov invited you to Relay",
                "Reminder: Kubasov invited you to Relay",
                "RE: Kubasov invited you to Relay",
                "Reminder: Kubasov invited you to Relay",
                "Kubasov invited you to Relay"]
        msgs = [_msg(f"kub{i}", s, "invites@relayfi.com",
                     "Alexander Kubasov invited you to set up Relay banking.",
                     folder_received=f"2026-06-2{i}T10:00:00Z") for i, s in enumerate(subs)]
        monkeypatch.setattr(ft, "fetch_messages",
                            lambda fid, s, processed_ids=None, backlog=False, limit=None:
                            (msgs, False) if fid == SRC_NOTIF else ([], False))
        monkeypatch.setattr(ft, "classify_message", lambda m, call_fn=None: ("IMPORTANT", "invite"))
        monkeypatch.setattr(ft, "move_to_fyi", MagicMock(return_value=True))
        res = ft.run_fyi(dry_run=True, days=30)  # wide window so recency keeps all 5
        important = [d for d in res["decisions"] if d["decision"] == "IMPORTANT"]
        assert len(important) == 1, important

    def test_normalize_subject_strips_prefixes(self):
        assert ft._normalize_subject("Reminder: Foo") == ft._normalize_subject("Foo")
        assert ft._normalize_subject("RE: Foo") == ft._normalize_subject("FW: Foo") == ft._normalize_subject("Foo")


# ======================================================================
#  12. STATE B round 3 -- two confirmed false negatives, same root: the
#  classifier could not recognize (A) portfolio-holding IR or (B) an
#  allocation invite delivered via a syndicate platform.
# ======================================================================

class TestFixAPortfolioHoldings:
    """Material IR (AGM, clinical readout, financing, M&A) from a HELD/TRACKED
    company is IMPORTANT even from info@/no-reply with a generic salutation.
    Deterministic holdings lookup -- no LLM."""

    def test_solvonis_svn002_readout_is_important_without_llm(self):
        called = {"n": 0}

        def boom(p, m):
            called["n"] += 1
            return '{"decision":"NOISE","reason":"no evidence Solvonis is a holding"}'

        msg = _msg("pa1", "Solvonis Announces Positive SVN-002 Bridging Data", "info@solvonis.com",
                   "Solvonis announces positive topline data from the SVN-002 bridging study.")
        d, _ = ft.classify_message(msg, call_fn=boom)
        assert d == "IMPORTANT"
        assert called["n"] == 0  # decided deterministically by the holdings lookup

    def test_solvonis_agm_is_important(self):
        msg = _msg("pa2", "Solvonis Announces Result of Annual General Meeting", "info@solvonis.com",
                   "The Company announces the results of its Annual General Meeting; all resolutions passed.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "IMPORTANT"

    def test_unheld_company_press_release_is_noise(self):
        # Identical-shape press release from an UNHELD company -> NOISE.
        msg = _msg("pa3", "Acme Bio Announces Positive Phase 2 Data", "info@acmebio.com",
                   "Acme Bio announces positive topline data from its Phase 2 study.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "NOISE"

    def test_held_generic_marketing_still_noise(self):
        # A held company's NON-material marketing has no IR keyword -> falls
        # through to the model and stays NOISE.
        msg = _msg("pa4", "Solvonis quarterly newsletter", "info@solvonis.com",
                   "Read our latest blog post and follow us on social media.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "NOISE"

    def test_is_held_company_ir_helper(self):
        held_material = _msg("h1", "Solvonis Announces Positive SVN-002 Data", "info@solvonis.com",
                             "positive topline data")
        assert ft._is_held_company_ir(held_material) is True
        held_generic = _msg("h2", "Solvonis monthly digest", "info@solvonis.com",
                            "Follow our blog and socials.")
        assert ft._is_held_company_ir(held_generic) is False  # no material IR keyword
        unheld_material = _msg("h3", "Acme Announces Phase 2 Data", "info@acmebio.com",
                               "positive topline data")
        assert ft._is_held_company_ir(unheld_material) is False


class TestFixBSyndicateInvite:
    """A specific funding-round allocation/investment invite is deal flow even via
    a syndicate/ESP platform. Only generic platform marketing stays NOISE. The ESP
    domain must NOT auto-NOISE."""

    def test_concentric_series_c_invite_is_important(self):
        msg = _msg("sb1", "Concentric AI Series C Invite", "noreply@mail1.syndicategroup.com",
                   "You are invited to participate in the Concentric AI Series C with an allocation.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "IMPORTANT"

    def test_generic_syndicate_newsletter_is_noise(self):
        msg = _msg("sb2", "Weekly syndicate digest", "noreply@mail1.syndicategroup.com",
                   "Dear Investor, here are this week's deals and an event invite across the platform.")
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "NOISE"

    def test_syndicate_domain_does_not_auto_noise(self):
        # The ESP domain alone must not be a deterministic broadcast NOISE (unlike
        # the broker OFFER// domains), so a real invite can reach the model.
        assert ft._is_broadcast(_msg("x", "Concentric AI Series C Invite",
                                     "noreply@mail1.syndicategroup.com", "")) is False

    def test_broker_offer_blast_still_noise(self):
        # Must NOT re-leak VCCross/Lafferty OFFER// broker blasts.
        assert ft.classify_message(_msg("x", "OFFERS// Crusoe, Figure AI", "invest@vccross.com", "..."),
                                   call_fn=lambda p, m: '{"decision":"IMPORTANT"}')[0] == "NOISE"


# ======================================================================
#  13. STATE D round 5 -- a specific allocation/round/secondary offer is deal
#  flow ACROSS language, recipient count, and sender. The confirmed casualty:
#  a Russian-language "Replit. Series D" allocation offer ($9B), sent by an
#  external VC to a 3-person investor distribution with a "Dear investors"
#  salutation -- it was suppressed by three signals (non-English salutation,
#  multi-recipient broadcast, external fund) that must NOT override a specific
#  company + round + confirm-interest ask. Generic non-English VC marketing with
#  no allocation ask must STILL be NOISE (no language-based over-correction).
#
#  The discriminator stays with the classifier (no hardcoded sender): the oracle
#  keys on the BODY signal (named company + round/allocation + an action ask),
#  not the from-address. Salutations are transliterated to keep this file ASCII
#  (tools corrupt Unicode); the signal, not literal Cyrillic, is what matters.
# ======================================================================

REPLIT_SERIES_D = _msg(
    "repd1", "Replit. Series D", "ke@brv.vc",
    "Uvazhaemye investory! We are opening a Series D allocation in Replit at a $9B valuation. "
    "Please confirm your interest ASAP to reserve participation. IRR projections and an LP teaser attached.")

REPLIT_SERIES_D_REPLY = _msg(
    "repd2", "Re: Replit. Series D", "ke@brv.vc",
    "Uvazhaemye investory! Following up with the data room and SPV terms for the Replit Series D "
    "allocation. Confirm participation this week to be included.")

NON_ENGLISH_GENERIC_VC_NEWSLETTER = _msg(
    "repd3", "Nedelnyi obzor venchurnogo rynka", "newsletter@somevcfund.com",
    "Uvazhaemye investory! This week's venture market outlook and trends -- general commentary, no "
    "specific deal or allocation on offer. Read our blog and subscribe. In the news: Replit, OpenAI.")

NON_ENGLISH_SELF_HELP_BLAST = _msg(
    "repd4", "Sila mysli -- webinar Dzhona Kekho", "info@kehoe-mind.com",
    "Uvazhaemye druzya! Join John Kehoe's mind-power webinar. Transform your life. Save 20% today; subscribe now.")


class TestStateDAllocationAcrossLanguage:
    """A SPECIFIC investment opportunity (named company + round/secondary/
    allocation + an action ask) is IMPORTANT deal flow regardless of language,
    recipient count, or sender. Generic VC marketing -- in any language -- stays
    NOISE. The classifier owns this nuance (no deterministic sender rule)."""

    def test_replit_series_d_is_important(self):
        # The confirmed false negative -- non-English, distribution-list, external
        # fund, but a specific company + round + confirm-interest ask. IMPORTANT.
        assert ft.classify_message(REPLIT_SERIES_D, call_fn=_oracle)[0] == "IMPORTANT"

    def test_replit_series_d_reply_is_not_noise(self):
        # Same-thread follow-up materials carry the same allocation signal ->
        # IMPORTANT at classify time (a run collapses it as a dedup of the first;
        # either is acceptable, but it must never be NOISE-dropped).
        assert ft.classify_message(REPLIT_SERIES_D_REPLY, call_fn=_oracle)[0] == "IMPORTANT"

    def test_non_english_generic_vc_newsletter_is_noise(self):
        # Guards against language-based over-correction: a non-English digest that
        # merely mentions companies, with no specific allocation ask, stays NOISE.
        assert ft.classify_message(NON_ENGLISH_GENERIC_VC_NEWSLETTER, call_fn=_oracle)[0] == "NOISE"

    def test_non_english_self_help_blast_is_noise(self):
        # John-Kehoe-school marketing blast -> NOISE regardless of language.
        assert ft.classify_message(NON_ENGLISH_SELF_HELP_BLAST, call_fn=_oracle)[0] == "NOISE"

    def test_rule_generalizes_not_a_hardcoded_sender(self):
        # The verdict comes from the body signal, not the sender: the SAME external
        # fund address on a generic digest is NOISE, while a DIFFERENT fund carrying
        # a specific allocation ask is IMPORTANT. Proves there is no brv.vc hardcode.
        brv_generic = _msg("repd5", "Venture digest", "ke@brv.vc",
                           "Uvazhaemye investory! This week's market outlook -- no allocation on offer.")
        assert ft.classify_message(brv_generic, call_fn=_oracle)[0] == "NOISE"
        other_fund_alloc = _msg("repd6", "OpenAI secondary", "partner@anotherfund.io",
                                "We can offer Zirmania a specific allocation in the OpenAI secondary. "
                                "Please confirm your interest to reserve your participation.")
        assert ft.classify_message(other_fund_alloc, call_fn=_oracle)[0] == "IMPORTANT"


# Permanent keep-set: these 12 must stay IMPORTANT forever. Re-confirmed every run.
KEEP_SET = [
    _msg("ks1", "Signature requested by YC Safes", "noreply@mail.hellosign.com",
         "Please sign the SAFE for Kinro - investment by Ken Belotsky."),
    _msg("ks2", "New form submission on Webflow for Negev-Labs", "no-reply-forms@webflow.com",
         "Noah Petermann, CEO of Aniva Health, submitted the contact form: I'd like to discuss a raise."),
    _msg("ks3", "Solvonis Announces Result of Annual General Meeting", "info@solvonis.com",
         "The Company announces the results of its Annual General Meeting; all resolutions passed."),
    _msg("ks4", "Sponsorship opportunity -- psychedelics conference", "events@psychsummit.org",
         "Hi Ken, this is Yanis Dida. I'd love to discuss Negev sponsoring our psychedelics conference."),
    _msg("ks5", "Concentric AI Series C Invite", "noreply@mail1.syndicategroup.com",
         "You are invited to participate in the Concentric AI Series C with an allocation."),
    _msg("ks6", "Relay banking invitation", "invites@relayfi.com",
         "Alexander Kubasov invited you to set up Negev's Relay business banking account."),
    NAMED_INSIDE_MARKETING,
    _msg("ks8", "Zirmania Team, Secondary Opportunity in Ramp", "matthew.bell@forgeglobal.com",
         "Hi Ken, a secondary opportunity in Ramp -- can Zirmania take an allocation this week? Matthew"),
    _msg("ks9", "Authorize your card payment", "noreply@revolut.com",
         "Please authorize a card payment of $4,200 to complete the transaction."),
    _msg("ks10", "Solvonis Announces Positive SVN-002 Bridging Data", "info@solvonis.com",
         "Solvonis announces positive topline data from the SVN-002 bridging study."),
    _msg("ks11", "Re: Negev Capital // Dakota", "jkovaleski@dakota.com",
         "Hi Ken, following up on Negev Capital -- can Negev take an allocation? Best, Jordan"),
    # STATE D round 5: a non-English, distribution-list, external-fund allocation
    # offer is still deal flow (named company + round + confirm-interest ask).
    REPLIT_SERIES_D,
]

# Permanent noise-set: these must stay NOISE.
NOISE_SET = [
    _msg("nsx1", "Negev Labs Q2 2026 Update", "bk@negevcap.com",
         "Dear investors, our Q2 update with wire details and a lot of substance."),
    _msg("nsx2", "[Action required] Retirement notice for Claude Sonnet 4", "notice@email.anthropic.com",
         "The Claude Sonnet 4 model will be retired. Automated product notice; no action."),
    _msg("nsx3", "Reminder: Important update regarding your PayPal account", "noreply@news.paypal.com",
         "Important update regarding your PayPal account policy."),
    _msg("nsx4", "OFFERS// Crusoe, Figure AI", "invest@vccross.com", "Broker offer sheet."),
    _msg("nsx5", "OFFER// Replit / Figure", "jgelet@rflafferty.com", "Broker offer sheet."),
    _msg("nsx6", "REMINDER: Invitation to invest in SportsCenter", "myteam@iangels.com",
         "Syndicate reminder to join the round."),
    _msg("nsx7", "Ship your first AI agent in a day", "theaicorner1@substack.com",
         "This week's newsletter; subscribe."),
    _msg("nsx8", "Weekly syndicate digest", "noreply@mail1.syndicategroup.com",
         "Dear Investor, this week's deals across the platform."),
    _msg("nsx9", "Acme Bio Announces Positive Phase 2 Data", "info@acmebio.com",
         "Acme Bio announces positive topline data from its Phase 2 study."),
    # STATE D round 5: generic non-English VC marketing carries no specific
    # allocation ask -> NOISE (language never flips a verdict; do not re-leak).
    NON_ENGLISH_GENERIC_VC_NEWSLETTER,
    NON_ENGLISH_SELF_HELP_BLAST,
]


class TestKeepSetRegression:
    @pytest.mark.parametrize("msg", KEEP_SET, ids=lambda m: m["id"])
    def test_keep_set_stays_important(self, msg):
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "IMPORTANT"

    @pytest.mark.parametrize("msg", NOISE_SET, ids=lambda m: m["id"])
    def test_noise_set_stays_noise(self, msg):
        assert ft.classify_message(msg, call_fn=_oracle)[0] == "NOISE"


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
