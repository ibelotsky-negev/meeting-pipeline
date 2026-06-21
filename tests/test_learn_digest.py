# Tests for the Read/Learn Digest module (learn_digest.py).
#
# All offline + mocked. The cluster/curate/currency steps take an injectable
# call_fn so the LLM is never reached; resolvers expose internal _fetch_* hooks
# so the network is never reached. The lock-race test mirrors test_pulse_lock:
# it neutralizes the in-process lock so the atomic O_CREAT|O_EXCL file lock is
# what must reject the second concurrent run.
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

import learn_digest as ld


# ----------------------------------------------------------------------
#  Fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def learn_files(monkeypatch, tmp_path):
    """Redirect all learn state files into a per-test temp dir."""
    monkeypatch.setattr(ld, "LEARN_LOCK_FILE", str(tmp_path / "learn_lock.json"))
    monkeypatch.setattr(ld, "LEARN_PROCESSED_FILE", str(tmp_path / "learn_processed.json"))
    monkeypatch.setattr(ld, "LEARN_STATUS_FILE", str(tmp_path / "learn_status.json"))
    return tmp_path


# The real "Burry" save shape: one x.com/i/status link (as an anchor AND repeated
# as a bare URL) plus the Outlook mobile-signature div with its aka.ms link.
BURRY_BODY = (
    "<html><body>"
    '<div>Worth a read on Burry: '
    '<a href="https://x.com/i/status/1790000000000000000">Michael Burry</a></div>'
    "<div>https://x.com/i/status/1790000000000000000</div>"
    '<div id="ms-outlook-mobile-signature"><div>Get '
    '<a href="https://aka.ms/o0ukef">Outlook for iOS</a></div></div>'
    "</body></html>"
)


def _cowork_summaries():
    """Five near-duplicate 'Cowork setup' saves with varying content dates
    (index 4 is the most recent)."""
    dates = ["2026-01-10", "2026-02-10", "2026-03-10", "2026-05-10", "2026-06-10"]
    out = []
    for i, d in enumerate(dates):
        out.append({
            "title": f"Cowork setup guide v{i + 1}", "type": "article",
            "url": f"https://example.com/cowork-{i + 1}", "subject": "Cowork",
            "content_date": d, "summary": f"How to set up Cowork (rev {i + 1}).",
            "specifics": [], "partial": False, "confidence": "medium",
        })
    return out


# ----------------------------------------------------------------------
#  1. URL extraction + signature stripping
# ----------------------------------------------------------------------

class TestExtractUrls:
    def test_strips_signature_and_dedups_to_one_x_link(self):
        urls = ld.extract_urls(BURRY_BODY)
        assert urls == ["https://x.com/i/status/1790000000000000000"]

    def test_drops_aka_ms_boilerplate(self):
        assert all("aka.ms" not in u for u in ld.extract_urls(BURRY_BODY))

    def test_empty_body_is_null_safe(self):
        assert ld.extract_urls("") == []
        assert ld.extract_urls(None) == []


# ----------------------------------------------------------------------
#  2. Host classification
# ----------------------------------------------------------------------

class TestClassifyUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://x.com/i/status/1790000000000000000", "x"),
        ("https://twitter.com/foo/status/123", "x"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://open.spotify.com/episode/abc", "podcast"),
        ("https://podcasts.apple.com/us/podcast/x/id123", "podcast"),
        ("https://stratechery.com/2026/some-post/", "article"),
    ])
    def test_classify(self, url, expected):
        assert ld.classify_url(url) == expected


# ----------------------------------------------------------------------
#  3. Resolver is null-safe on empty/None fetch
# ----------------------------------------------------------------------

class TestResolverPartial:
    def test_article_partial_when_both_fetchers_empty(self, monkeypatch):
        monkeypatch.setattr(ld, "_fetch_jina", lambda url: None)
        monkeypatch.setattr(ld, "_fetch_trafilatura", lambda url: None)
        result = ld.resolve_article("https://example.com/dead-link")
        assert result["partial"] is True
        assert result["reason"]
        assert result["text"] == ""

    def test_resolve_item_never_raises_on_resolver_error(self, monkeypatch):
        def boom(url):
            raise RuntimeError("network melted")
        monkeypatch.setattr(ld, "resolve_article", boom)
        out = ld.resolve_item({"type": "article", "url": "https://example.com/x"})
        assert out["partial"] is True


# ----------------------------------------------------------------------
#  4. Clustering groups near-duplicates into one cluster
# ----------------------------------------------------------------------

class TestClustering:
    def test_five_cowork_items_collapse_to_one_cluster(self):
        summaries = _cowork_summaries()

        def fake_call(prompt, model):
            return json.dumps({"clusters": [{"topic": "Cowork setup", "members": [0, 1, 2, 3, 4]}]})

        clusters = ld.cluster_items(summaries, call_fn=fake_call)
        assert len(clusters) == 1
        assert len(clusters[0]["items"]) == 5
        assert clusters[0]["topic"] == "Cowork setup"

    def test_unplaced_items_become_singletons(self):
        summaries = _cowork_summaries()

        def fake_call(prompt, model):
            # Model only places indices 0 and 1; 2,3,4 must not be dropped.
            return json.dumps({"clusters": [{"topic": "Cowork", "members": [0, 1]}]})

        clusters = ld.cluster_items(summaries, call_fn=fake_call)
        total = sum(len(c["items"]) for c in clusters)
        assert total == 5


# ----------------------------------------------------------------------
#  5. Curation selects the most-recent keeper, marks the rest superseded
# ----------------------------------------------------------------------

class TestCuration:
    def test_newest_kept_others_superseded_with_reason(self):
        items = _cowork_summaries()  # index 4 == 2026-06-10 (newest)
        cluster = {"topic": "Cowork setup", "items": items}

        def fake_call(prompt, model):
            return json.dumps({
                "keepers": [{"index": 4, "why": "newest and closest to Ken's TAS Cowork usage",
                             "bucket": "Travel Relay", "has_action": True,
                             "action": "Apply the daily-monitor pattern to TAS"}],
                "superseded": [{"index": i, "reason": "older revision of the same setup"} for i in range(4)],
            })

        result = ld.curate_cluster(cluster, call_fn=fake_call)
        assert len(result["keepers"]) == 1
        assert result["keepers"][0]["content_date"] == "2026-06-10"
        assert result["keepers"][0]["bucket"] == "Travel Relay"
        assert len(result["superseded"]) == 4
        assert all(s["reason"] for s in result["superseded"])

    def test_fallback_picks_newest_when_model_output_unusable(self):
        items = _cowork_summaries()
        cluster = {"topic": "Cowork setup", "items": items}
        result = ld.curate_cluster(cluster, call_fn=lambda p, m: "garbage not json")
        assert len(result["keepers"]) == 1
        assert result["keepers"][0]["content_date"] == "2026-06-10"
        assert len(result["superseded"]) == 4
        assert all(s["reason"] for s in result["superseded"])


# ----------------------------------------------------------------------
#  6. Currency check: annotate superseded keeper; skip slow-moving clusters
# ----------------------------------------------------------------------

class TestCurrencyCheck:
    def test_annotates_superseded_for_fast_moving_keeper(self):
        keeper = {"title": "Old MCP setup", "content_date": "2026-01-01", "summary": "..."}

        def fake_web(prompt):
            return json.dumps({"current": False, "note": "superseded by the new MCP connector spec"})

        out = ld.currency_check(keeper, "Claude Code MCP skills", mode="fast-moving", call_fn=fake_web)
        assert out["currency_note"].startswith("likely superseded")
        assert "MCP connector" in out["currency_note"]

    def test_skipped_for_slow_moving_cluster_no_web_call(self):
        keeper = {"title": "Gut health and the microbiome", "content_date": "2025-09-01"}
        fake_web = MagicMock()
        out = ld.currency_check(keeper, "Gut health Huberman digestion", mode="fast-moving", call_fn=fake_web)
        assert fake_web.call_count == 0
        assert "currency_note" not in out

    def test_off_mode_makes_no_call_even_for_fast_moving(self):
        fake_web = MagicMock()
        ld.currency_check({"title": "x"}, "Claude Code agents", mode="off", call_fn=fake_web)
        assert fake_web.call_count == 0


# ----------------------------------------------------------------------
#  7. Exactly one email under a two-worker lock race
# ----------------------------------------------------------------------

class _AlwaysAcquireLock:
    """Stands in for the in-process lock to simulate a second worker process,
    where each process has its own (uncontended) threading.Lock."""

    def acquire(self, blocking=True):
        return True

    def release(self):
        pass

    def locked(self):
        return False


class TestSingleSendRace:
    def test_two_concurrent_runs_send_one_email(self, learn_files, monkeypatch):
        monkeypatch.setattr(ld, "_learn_lock", _AlwaysAcquireLock())
        monkeypatch.setattr(ld, "fetch_unread",
                            lambda *a, **k: [{"id": "m1", "subject": "A", "body": {"content": ""}},
                                             {"id": "m2", "subject": "B", "body": {"content": ""}}])

        def slow_cluster(summaries, call_fn=None):
            time.sleep(0.4)  # hold the run open so the second fire overlaps
            return [{"topic": "T", "items": summaries}]
        monkeypatch.setattr(ld, "cluster_items", slow_cluster)
        monkeypatch.setattr(ld, "curate_cluster", lambda c, **k: {
            "topic": "T", "superseded": [],
            "keepers": [{"title": "A", "url": "", "summary": "s", "why": "w",
                         "bucket": "General/Reference", "has_action": False,
                         "action": "", "partial": True}]})
        monkeypatch.setattr(ld, "currency_check", lambda k, t, **kw: k)
        monkeypatch.setattr(ld, "ensure_processed_subfolder", lambda *a, **k: "FAKE")
        monkeypatch.setattr(ld, "mark_read_and_move", lambda *a, **k: None)
        monkeypatch.setattr(ld, "_save_processed_ids", lambda *a, **k: None)
        send_mock = MagicMock()
        monkeypatch.setattr(ld, "send_digest_email", send_mock)

        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait(timeout=5)
            results.append(ld.run_learn(dry_run=False))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert send_mock.call_count == 1, "two concurrent learn runs both sent email -- duplicate-send race"

    def test_dry_run_sends_no_email(self, learn_files, monkeypatch):
        monkeypatch.setattr(ld, "fetch_unread",
                            lambda *a, **k: [{"id": "m1", "subject": "A", "body": {"content": ""}}])
        monkeypatch.setattr(ld, "cluster_items", lambda s, **k: [{"topic": "T", "items": s}])
        monkeypatch.setattr(ld, "curate_cluster", lambda c, **k: {
            "topic": "T", "superseded": [],
            "keepers": [{"title": "A", "url": "", "summary": "s", "why": "w",
                         "bucket": "General/Reference", "has_action": False, "action": "", "partial": True}]})
        monkeypatch.setattr(ld, "currency_check", lambda k, t, **kw: k)
        send_mock = MagicMock()
        monkeypatch.setattr(ld, "send_digest_email", send_mock)
        result = ld.run_learn(dry_run=True)
        assert result["sent"] is False
        assert send_mock.call_count == 0


# ----------------------------------------------------------------------
#  8. Processed-ID dedup
# ----------------------------------------------------------------------

class TestProcessedDedup:
    def test_already_processed_id_is_skipped(self, monkeypatch):
        page = {"value": [{"id": "seen-1", "subject": "old"},
                          {"id": "fresh-2", "subject": "new"}]}
        monkeypatch.setattr(ld.eps, "graph_get", lambda url, params=None: page)
        out = ld.fetch_unread(processed_ids={"seen-1"})
        assert [m["id"] for m in out] == ["fresh-2"]


# ----------------------------------------------------------------------
#  9. Deterministic helpers
# ----------------------------------------------------------------------

class TestHelpers:
    def test_newest_index_by_date(self):
        items = _cowork_summaries()
        assert ld._newest_index(items) == 4

    def test_newest_index_all_none_falls_back_to_last(self):
        items = [{"content_date": None}, {"content_date": None}, {"content_date": None}]
        assert ld._newest_index(items) == 2

    def test_section_for_bucket_known_and_default(self):
        assert ld._section_for_bucket("Sara Pipeline") == ld.ASANA_SECTIONS["Sara Pipeline"]
        assert ld._section_for_bucket("nonsense") == ld.ASANA_SECTIONS["General/Reference"]

    def test_is_fast_moving(self):
        assert ld.is_fast_moving("Claude Code skills and MCP") is True
        assert ld.is_fast_moving("Gut health and digestion") is False


# ----------------------------------------------------------------------
#  10. /learn/run endpoint (thin wrapper around run_learn)
# ----------------------------------------------------------------------

class TestLearnRunEndpoint:
    def test_sync_run_returns_result(self, flask_client, monkeypatch):
        monkeypatch.setattr(ld, "run_learn",
                            lambda dry_run=False, backlog=False: {
                                "status": "ok", "keepers": 3, "dry_run": dry_run, "backlog": backlog})
        resp = flask_client.get("/learn/run?sync=true&dry_run=true&backlog=1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["dry_run"] is True and data["backlog"] is True

    def test_returns_409_when_a_run_is_already_in_progress(self, flask_client):
        import app as app_module
        assert app_module._learn_trigger_lock.acquire(blocking=False) is True
        try:
            resp = flask_client.get("/learn/run")
            assert resp.status_code == 409
            assert resp.get_json()["status"] == "already_running"
        finally:
            app_module._learn_trigger_lock.release()


# ----------------------------------------------------------------------
#  11. Optional-key handling: read at call time, degrade (never raise) if absent
# ----------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self.content = b"{}"
        self.text = ""
        self._payload = payload

    def json(self):
        return self._payload


class TestResolverKeys:
    def test_spoken_degrades_without_key(self, monkeypatch):
        monkeypatch.delenv("SPOKEN_API_KEY", raising=False)
        assert ld._fetch_spoken("https://open.spotify.com/episode/x") is None
        result = ld.resolve_podcast("https://open.spotify.com/episode/x")
        assert result["partial"] is True and result["text"] == ""

    def test_grok_stt_degrades_without_key(self, monkeypatch):
        monkeypatch.delenv("XAI_API_KEY", raising=False)
        assert ld._grok_stt("https://video.example/clip.mp4") is None

    def test_spoken_resolves_when_key_present_and_http_mocked(self, monkeypatch):
        monkeypatch.setenv("SPOKEN_API_KEY", "pt_test")
        monkeypatch.setattr(ld.eps, "_request_with_retry",
                            lambda *a, **k: _FakeResp({"transcript": "hello world transcript"}))
        result = ld.resolve_podcast("https://open.spotify.com/episode/x")
        assert result["partial"] is False
        assert "hello world" in result["text"]

    def test_keys_are_read_at_call_time_not_module_scope(self):
        # The resolvers must not capture keys at import time -- no module-level
        # XAI_API_KEY / SPOKEN_API_KEY / JINA_API_KEY constants.
        assert not hasattr(ld, "XAI_API_KEY")
        assert not hasattr(ld, "SPOKEN_API_KEY")
        assert not hasattr(ld, "JINA_API_KEY")
