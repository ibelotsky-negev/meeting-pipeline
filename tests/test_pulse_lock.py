# Tests for the weekly pulse send path and duplicate-run protection.
#
# Production topology: the Dockerfile runs gunicorn with 2 workers. Each
# worker imports app.py and (before the RUN_SCHEDULER guard) started its
# own APScheduler, so the Sunday cron fired in BOTH workers. The in-memory
# _pulse_lock is per-process and the file lock was only written AFTER a
# run completed (minutes later), so both workers passed pulse_can_run()
# and sent independent emails. These tests simulate two workers by
# neutralizing the in-memory lock; the atomic O_CREAT|O_EXCL run lock is
# what must reject the second run.
import threading
import time
from unittest.mock import MagicMock

import pytest

import app as app_module


class _AlwaysAcquireLock:
    """Stands in for _pulse_lock to simulate a second worker process,
    where each process has its own (uncontended) threading.Lock."""

    def acquire(self, blocking=True):
        return True

    def release(self):
        pass

    def locked(self):
        return False


@pytest.fixture
def pulse_pipeline(monkeypatch, pulse_files):
    """Stub out every external call in the pulse pipeline. Returns the
    send_email mock so tests can assert call counts."""
    monkeypatch.setattr(app_module, "pulse_collect_emails", lambda s, e: [])
    monkeypatch.setattr(app_module, "pulse_collect_teams", lambda s, e: [])
    monkeypatch.setattr(app_module, "pulse_collect_meetings", lambda s, e: [])
    monkeypatch.setattr(app_module, "pulse_analyze",
                        lambda *a, **k: ("weekly report", {}, []))
    monkeypatch.setattr(app_module, "pulse_archive", lambda *a, **k: "archived")
    send_mock = MagicMock()
    monkeypatch.setattr(app_module, "pulse_send_email", send_mock)
    return send_mock


class TestSinglePulseRun:
    def test_one_run_sends_exactly_one_email(self, pulse_pipeline):
        ran = app_module._pulse_run_background(days=7, dry_run=False)
        assert ran is True
        assert pulse_pipeline.call_count == 1

    def test_dry_run_sends_no_email(self, pulse_pipeline):
        ran = app_module._pulse_run_background(days=7, dry_run=True)
        assert ran is True
        assert pulse_pipeline.call_count == 0

    def test_run_lock_released_after_run(self, pulse_pipeline):
        app_module._pulse_run_background(days=7, dry_run=False)
        # A follow-up run must be able to acquire the lock again
        assert app_module._acquire_running_lock() is True
        app_module._release_running_lock()


class TestConcurrentPulseRuns:
    def test_two_concurrent_workers_send_one_email(self, pulse_pipeline, monkeypatch):
        # Simulate two gunicorn workers: each has its own threading.Lock,
        # so the in-memory lock never rejects anything.
        monkeypatch.setattr(app_module, "_pulse_lock", _AlwaysAcquireLock())

        def slow_analyze(*a, **k):
            time.sleep(0.4)  # hold the run open so the second fire overlaps
            return ("weekly report", {}, [])
        monkeypatch.setattr(app_module, "pulse_analyze", slow_analyze)

        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait(timeout=5)
            results.append(app_module.pulse_weekly_run())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert pulse_pipeline.call_count == 1, (
            "two concurrent pulse runs both sent email -- duplicate-send race")

    def test_second_sequential_run_within_window_is_rejected(self, pulse_pipeline):
        app_module.pulse_weekly_run()
        assert pulse_pipeline.call_count == 1
        # Second fire within the 6h completion window (misfire grace,
        # restart, stale browser tab) must be a no-op.
        app_module.pulse_weekly_run()
        assert pulse_pipeline.call_count == 1


class TestAtomicRunLock:
    def test_second_acquire_rejected_while_held(self, pulse_files):
        assert app_module._acquire_running_lock() is True
        assert app_module._acquire_running_lock() is False
        app_module._release_running_lock()
        assert app_module._acquire_running_lock() is True
        app_module._release_running_lock()

    def test_stale_lock_is_reclaimed(self, pulse_files):
        import os
        assert app_module._acquire_running_lock() is True
        # Backdate the lock past the staleness threshold (crashed run)
        stale = time.time() - app_module.PULSE_RUNNING_LOCK_MAX_AGE - 60
        os.utime(app_module.PULSE_RUNNING_LOCK_FILE, (stale, stale))
        assert app_module._acquire_running_lock() is True
        app_module._release_running_lock()

    def test_fresh_lock_is_not_reclaimed(self, pulse_files):
        import os
        assert app_module._acquire_running_lock() is True
        recent = time.time() - 5 * 60  # 5 minutes old, run still in progress
        os.utime(app_module.PULSE_RUNNING_LOCK_FILE, (recent, recent))
        assert app_module._acquire_running_lock() is False
        app_module._release_running_lock()

    def test_release_when_not_held_is_safe(self, pulse_files):
        app_module._release_running_lock()  # must not raise


class TestCompletionWindow:
    def test_can_run_blocked_after_completion(self, pulse_files):
        app_module.pulse_set_lock()
        assert app_module.pulse_can_run() is False

    def test_can_run_after_window_expires(self, pulse_files, monkeypatch):
        import json
        app_module.pulse_set_lock()
        with open(app_module.PULSE_LOCK_FILE) as f:
            lock = json.load(f)
        lock["completed_at"] = time.time() - app_module.PULSE_LOCK_DURATION - 60
        with open(app_module.PULSE_LOCK_FILE, "w") as f:
            json.dump(lock, f)
        assert app_module.pulse_can_run() is True

    def test_can_run_with_no_lock_file(self, pulse_files):
        assert app_module.pulse_can_run() is True
