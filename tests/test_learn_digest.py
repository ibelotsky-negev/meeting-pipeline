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
                            lambda dry_run=False, backlog=False, force=False, limit=None: {
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


# ----------------------------------------------------------------------
#  12. force=1 clears an orphaned run lock
# ----------------------------------------------------------------------

class TestForceLock:
    def test_force_clears_orphaned_lock(self, learn_files, monkeypatch):
        # Simulate an orphaned lock (a run killed mid-flight left the file behind).
        with open(ld.LEARN_LOCK_FILE, "w") as f:
            f.write("{}")
        # Without force, a fresh (non-stale) lock blocks the run.
        assert ld.run_learn(dry_run=True, force=False)["status"] == "skipped"
        # With force, the lock is cleared and the run proceeds.
        monkeypatch.setattr(ld, "fetch_unread", lambda *a, **k: [])
        result = ld.run_learn(dry_run=True, force=True)
        assert result["status"] == "ok"
        # Lock released afterwards -- a later run can acquire again.
        assert ld._acquire_run_lock() is True
        ld._release_run_lock()


# ----------------------------------------------------------------------
#  13. Cluster/curate output-token budget (regression guard for the 2.18.2
#      truncation bug: a 2000-token cap truncated the whole-batch cluster JSON
#      and collapsed every item into its own singleton).
# ----------------------------------------------------------------------

class TestTokenBudgets:
    def test_cluster_budget_is_large(self):
        assert ld.CLUSTER_MAX_TOKENS >= 8000

    def test_cluster_default_caller_uses_large_budget(self, monkeypatch):
        captured = {}

        def fake(prompt, model, max_tokens=2000, tools=None, timeout=None):
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            return json.dumps({"clusters": [{"topic": "Cowork", "members": [0, 1]}]})

        monkeypatch.setattr(ld, "_call_claude_text", fake)
        ld.cluster_items(_cowork_summaries()[:2])  # no call_fn -> production default path
        assert captured["max_tokens"] == ld.CLUSTER_MAX_TOKENS
        assert captured["timeout"] == ld.LEARN_CLUSTER_TIMEOUT  # bounded, not the 10min default

    def test_curate_default_caller_uses_curate_budget(self, monkeypatch):
        captured = {}

        def fake(prompt, model, max_tokens=2000, tools=None, timeout=None):
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            return json.dumps({"keepers": [{"index": 0, "why": "w", "bucket": "General/Reference",
                                            "has_action": False, "action": ""}],
                               "superseded": [{"index": 1, "reason": "dup"}]})

        monkeypatch.setattr(ld, "_call_claude_text", fake)
        ld.curate_cluster({"topic": "Cowork", "items": _cowork_summaries()[:2]})
        assert captured["max_tokens"] == ld.CURATE_MAX_TOKENS


# ----------------------------------------------------------------------
#  14. Cluster-call resilience: retry transient failures instead of nuking
#      the whole batch to singletons; record a diagnostic either way.
# ----------------------------------------------------------------------

class TestClusterResilience:
    def test_retries_transient_failure_then_succeeds(self, monkeypatch):
        monkeypatch.setattr(ld.time, "sleep", lambda *a, **k: None)
        calls = {"n": 0}

        def flaky(prompt, model):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient 529 overloaded")
            return json.dumps({"clusters": [{"topic": "Cowork", "members": [0, 1, 2, 3, 4]}]})

        clusters = ld.cluster_items(_cowork_summaries(), call_fn=flaky)
        assert calls["n"] >= 2  # it retried rather than falling back on the first failure
        assert len(clusters) == 1 and len(clusters[0]["items"]) == 5
        assert ld._LAST_CLUSTER_DIAG.get("fell_back_to_singletons") is False

    def test_persistent_failure_falls_back_and_records_error(self, monkeypatch):
        monkeypatch.setattr(ld.time, "sleep", lambda *a, **k: None)

        def always_fail(prompt, model):
            raise RuntimeError("529 overloaded")

        clusters = ld.cluster_items(_cowork_summaries(), call_fn=always_fail)
        assert len(clusters) == 5  # singleton fallback only after retries exhausted
        assert ld._LAST_CLUSTER_DIAG.get("fell_back_to_singletons") is True
        assert "529" in (ld._LAST_CLUSTER_DIAG.get("error") or "")


class TestFetchLimit:
    def test_fetch_unread_respects_limit(self, monkeypatch):
        page = {"value": [{"id": f"m{i}", "subject": str(i)} for i in range(5)]}
        monkeypatch.setattr(ld.eps, "graph_get", lambda url, params=None: page)
        assert len(ld.fetch_unread(limit=2)) == 2
        assert len(ld.fetch_unread()) == 5

    def test_backlog_true_reprocesses_seen_ids(self, monkeypatch):
        page = {"value": [{"id": "seen-1", "subject": "old"}, {"id": "fresh-2", "subject": "new"}]}
        monkeypatch.setattr(ld.eps, "graph_get", lambda url, params=None: page)
        # Normal run (backlog=False): an already-processed id is skipped.
        assert [m["id"] for m in ld.fetch_unread(processed_ids={"seen-1"})] == ["fresh-2"]
        # Backlog run (backlog=True): the processed-ID store is ignored -> re-process all.
        assert [m["id"] for m in ld.fetch_unread(processed_ids={"seen-1"}, backlog=True)] \
            == ["seen-1", "fresh-2"]


# ----------------------------------------------------------------------
#  15. X resolver via Grok Agent Tools API (x_search) -- parser + degrade
# ----------------------------------------------------------------------

# Mirrors the real xAI /v1/responses payload shape: reasoning + custom_tool_call
# items, then the assistant message whose content carries output_text +
# url_citation annotations. Top-level output_text is null (must walk output[]).
GROK_RESPONSES_REAL = {
    "output_text": None,
    "output": [
        {"type": "reasoning", "summary": [{"text": "thinking...", "type": "summary_text"}], "status": "completed"},
        {"type": "custom_tool_call", "name": "x_thread_fetch", "input": '{"post_id":"123"}', "status": "completed"},
        {"type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text",
             "text": "Author @rubenhassid. A one-time Cowork setup. \"A folder beats a clever prompt\".",
             "annotations": [{"type": "url_citation", "url": "https://x.com/i/status/123"}]},
        ]},
    ],
}


class TestGrokXResolver:
    def test_parse_walks_output_array_when_output_text_null(self):
        text, citations = ld._parse_grok_responses(GROK_RESPONSES_REAL)
        assert "@rubenhassid" in text
        assert "A folder beats a clever prompt" in text
        assert citations == ["https://x.com/i/status/123"]

    def test_parse_prefers_top_level_output_text(self):
        text, _ = ld._parse_grok_responses({"output_text": "direct answer", "output": []})
        assert text == "direct answer"

    def test_parse_no_assistant_message_returns_empty(self):
        text, citations = ld._parse_grok_responses(
            {"output_text": None, "output": [{"type": "reasoning", "summary": []}]})
        assert text == "" and citations == []

    def test_parse_survives_tool_result_with_string_content(self):
        # xAI can return a tool_result item whose "content" is a STRING (not a
        # list). The citation walk must not iterate it as a list of dicts and
        # crash (AttributeError), which would mislabel the X post as partial.
        data = {"output_text": None, "output": [
            {"type": "tool_result", "content": "raw string result from x_thread_fetch"},
            {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Author @real. The actual post text.",
                 "annotations": [{"type": "url_citation", "url": "https://x.com/i/status/9"}]}]},
        ]}
        text, citations = ld._parse_grok_responses(data)
        assert "actual post text" in text
        assert citations == ["https://x.com/i/status/9"]

    def test_resolve_x_returns_content_on_success(self, monkeypatch):
        monkeypatch.setattr(ld, "_grok_responses_call", lambda prompt, model: GROK_RESPONSES_REAL)
        r = ld.resolve_x("https://x.com/i/status/123")
        assert r["partial"] is False and r["kind"] == "x"
        assert "@rubenhassid" in r["text"]
        assert r["citations"] == ["https://x.com/i/status/123"]

    def test_resolve_x_partial_on_cannot_access(self, monkeypatch):
        cannot = {"output_text": None, "output": [
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "CANNOT_ACCESS"}]}]}
        monkeypatch.setattr(ld, "_grok_responses_call", lambda prompt, model: cannot)
        r = ld.resolve_x("https://x.com/i/status/123")
        assert r["partial"] is True and r["text"] == ""

    def test_resolve_x_partial_on_call_error_never_raises(self, monkeypatch):
        def boom(prompt, model):
            raise RuntimeError("xAI 500")
        monkeypatch.setattr(ld, "_grok_responses_call", boom)
        r = ld.resolve_x("https://x.com/i/status/123")
        assert r["partial"] is True
        assert "xAI 500" in r["reason"]

    def test_resolve_x_partial_with_distinct_reason_on_parse_error(self, monkeypatch):
        # A parse crash must degrade to partial with a PARSE-specific reason,
        # not be mislabeled as "could not access".
        monkeypatch.setattr(ld, "_grok_responses_call", lambda prompt, model: {"ok": True})

        def bad_parse(data):
            raise ValueError("unexpected shape")
        monkeypatch.setattr(ld, "_parse_grok_responses", bad_parse)
        r = ld.resolve_x("https://x.com/i/status/123")
        assert r["partial"] is True
        assert "parse error" in r["reason"]

    def test_grok_call_fails_fast_on_non_retryable_4xx(self, monkeypatch):
        import requests as _rq
        calls = {"n": 0}

        class _Resp:
            status_code = 400

            def raise_for_status(self):
                raise _rq.HTTPError(response=self)

            def json(self):
                return {}

        def fake_post(*a, **k):
            calls["n"] += 1
            return _Resp()
        monkeypatch.setattr(ld.requests, "post", fake_post)
        monkeypatch.setattr(ld.time, "sleep", lambda *a, **k: None)
        with pytest.raises(RuntimeError):
            ld._grok_responses_call("p", "m")
        assert calls["n"] == 1  # 400 is not retried (only network/429/5xx are)


# ----------------------------------------------------------------------
#  16. Bounded execution: concurrency (order + failure isolation), heartbeat,
#      and degrade-on-timeout so a run can never hang invisibly.
# ----------------------------------------------------------------------

class TestConcurrency:
    def test_preserves_order(self):
        out = ld._run_concurrent(list(range(10)), lambda i, x: x * 2, workers=4)
        assert out == [x * 2 for x in range(10)]

    def test_isolates_a_crashing_item(self):
        def fn(i, x):
            if x == 3:
                raise RuntimeError("boom")
            return x
        out = ld._run_concurrent(list(range(6)), fn, workers=4)
        assert out[3] is None  # the crasher
        assert [out[i] for i in (0, 1, 2, 4, 5)] == [0, 1, 2, 4, 5]  # others intact, order kept

    def test_sequential_path_when_workers_one(self):
        out = ld._run_concurrent([1, 2, 3], lambda i, x: x + 100, workers=1)
        assert out == [101, 102, 103]


class TestHeartbeat:
    def test_bump_and_set_progress(self, monkeypatch):
        ld._set_progress(phase="resolve+summarize", done=0, total=3, last="", run_id="r1")
        assert ld._LEARN_PROGRESS["phase"] == "resolve+summarize"
        assert ld._LEARN_PROGRESS["total"] == 3
        for _ in range(3):
            ld._bump_progress("item")
        assert ld._LEARN_PROGRESS["done"] == 3
        assert ld._LEARN_PROGRESS["updated_at"] is not None

    def test_read_status_includes_live_progress(self):
        ld._set_progress(phase="cluster", done=1, total=1, last="x")
        status = ld.read_status()
        assert "live_progress" in status
        assert status["live_progress"]["phase"] == "cluster"


class TestDegradeOnFailure:
    def test_summarize_degrades_when_call_raises(self):
        # Simulate a timeout / API error during summarize -> base summary, no crash.
        def boom(prompt, model):
            raise RuntimeError("APITimeoutError")
        item = {"subject": "Cowork", "url": "https://e/1", "type": "article"}
        resolved = {"text": "real content", "kind": "article", "partial": False, "reason": "", "content_date": None}
        summ = ld.summarize_item(item, resolved, call_fn=boom)
        assert summ["title"]  # produced a summary record, did not raise
        assert summ["type"] == "article"

    def test_currency_check_degrades_when_call_raises(self):
        def boom(prompt):
            raise RuntimeError("APITimeoutError")
        keeper = {"title": "MCP setup", "content_date": "2026-01-01", "summary": "..."}
        out = ld.currency_check(keeper, "Claude Code MCP", mode="fast-moving", call_fn=boom)
        assert out is keeper  # returned unchanged, no crash
        assert "currency_note" not in out


# ----------------------------------------------------------------------
#  17. Source links: every keeper carries a clickable source URL in the
#      digest + Asana notes (X->citation, else extracted; omit when none).
# ----------------------------------------------------------------------

class TestSourceLinks:
    def test_summarize_prefers_x_citation_as_source(self):
        item = {"subject": "Cowork", "url": "https://x.com/user/status/9?s=20", "type": "x"}
        resolved = {"text": "real post content", "kind": "x", "partial": False, "reason": "",
                    "content_date": None, "citations": ["https://x.com/i/status/9"]}
        summ = ld.summarize_item(item, resolved, call_fn=lambda p, m: "{}")
        assert summ["url"] == "https://x.com/i/status/9"  # canonical citation, not the ?s=20 link

    def test_summarize_email_only_fallback_uses_extracted_link(self):
        item = {"subject": "Burry", "url": "https://x.com/i/status/5", "type": "x"}
        resolved = ld._partial("x", "no content retrieved")  # no citations
        summ = ld.summarize_item(item, resolved, call_fn=lambda p, m: "{}")
        assert summ["url"] == "https://x.com/i/status/5"

    def test_summarize_no_url_when_none_extracted(self):
        item = {"subject": "note to self", "url": "", "type": "article"}
        summ = ld.summarize_item(item, ld._partial("article", "no link"), call_fn=lambda p, m: "{}")
        assert summ["url"] == ""

    def _keeper(self, url, partial=False):
        return {"title": "Best", "url": url, "summary": "s", "why": "w",
                "bucket": "General/Reference", "has_action": False, "action": "", "partial": partial}

    def test_render_links_best_item_to_source(self):
        html = ld.render_digest_html([{"topic": "T", "superseded": [],
                                       "keepers": [self._keeper("https://example.com/post")]}])
        assert '<a href="https://example.com/post"' in html and ">Best</a>" in html

    def test_render_no_anchor_when_no_url(self):
        html = ld.render_digest_html([{"topic": "T", "superseded": [],
                                       "keepers": [self._keeper("", partial=True)]}])
        assert "Best" in html and '<a href="">' not in html

    def test_render_also_saved_links_each_member(self):
        curated = [{"topic": "T", "keepers": [self._keeper("https://example.com/1")],
                    "superseded": [{"title": "Older", "url": "https://example.com/2", "reason": "older"}]}]
        html = ld.render_digest_html(curated)
        assert "Also saved" in html and '<a href="https://example.com/2"' in html

    def test_asana_notes_include_source_url(self, monkeypatch):
        captured = {}

        def fake_req(method, endpoint, data=None):
            if endpoint == "/tasks":
                captured["notes"] = data.get("notes", "")
                return {"gid": "T1"}
            return {}
        monkeypatch.setattr(ld.asana_client, "asana_request", fake_req)
        gid = ld.create_triage_task(self._keeper("https://example.com/post"))
        assert gid == "T1"
        assert "https://example.com/post" in captured["notes"]

    def test_asana_notes_omit_source_when_no_url(self, monkeypatch):
        captured = {}

        def fake_req(method, endpoint, data=None):
            if endpoint == "/tasks":
                captured["notes"] = data.get("notes", "")
                return {"gid": "T2"}
            return {}
        monkeypatch.setattr(ld.asana_client, "asana_request", fake_req)
        ld.create_triage_task(self._keeper(""))
        assert "Source:" not in captured["notes"]


# ----------------------------------------------------------------------
#  11. Deterministic section routing (Part A) + Priority at creation (Part B)
# ----------------------------------------------------------------------

class TestRoutingAndPriority:
    def _k(self, **kw):
        base = {"topic": "", "subject": "", "title": "", "summary": "", "specifics": []}
        base.update(kw)
        return base

    def test_route_health(self):
        gid = ld.route_section(self._k(topic="Huberman sleep and longevity protocol"))
        assert gid == ld.LEARN_SECTION_GID["Health"]

    def test_route_biotech_investing_to_negev(self):
        gid = ld.route_section(self._k(topic="Biotech rNPV valuation, Phase II asset"))
        assert gid == ld.LEARN_SECTION_GID["Negev Labs"]

    def test_route_general_markets_to_zirmania(self):
        gid = ld.route_section(self._k(topic="Copper supercycle and public equities thesis"))
        assert gid == "1215886226827868"
        gid2 = ld.route_section(self._k(topic="Michael Burry semiconductor short"))
        assert gid2 == ld.LEARN_SECTION_GID["Zirmania Family Office"]

    def test_route_travel(self):
        gid = ld.route_section(self._k(topic="Cheap flight booking via Kiwi"))
        assert gid == ld.LEARN_SECTION_GID["Travel Relay"]

    def test_route_design_to_ariadne(self):
        gid = ld.route_section(self._k(topic="Design system and Tailwind UI components"))
        assert gid == ld.LEARN_SECTION_GID["Ariadne Website"]

    def test_route_sara_parallel(self):
        gid = ld.route_section(self._k(topic="OpenClaw chief-of-staff meeting pipeline build"))
        assert gid == ld.LEARN_SECTION_GID["Sara Pipeline"]

    def test_route_default_general(self):
        # generic Claude tooling (Cowork/Obsidian) falls through to General (convention b)
        gid = ld.route_section(self._k(topic="Claude Cowork setup with Obsidian notes"))
        assert gid == ld.LEARN_SECTION_GID["General / Reference"]

    def test_biotech_signal_wins_tiebreaker(self):
        # an item carrying BOTH a biotech and a general-investing word -> Negev
        gid = ld.route_section(self._k(topic="biotech valuation and public equities allocation"))
        assert gid == ld.LEARN_SECTION_GID["Negev Labs"]

    def test_priority_field_gid_guard(self):
        assert ld.LEARN_PRIORITY_FIELD_GID == "1199941453034656"
        assert ld.LEARN_PRIORITY_FIELD_GID != "1206810235510187"

    def test_priority_option_gids(self):
        assert ld.LEARN_PRIORITY_OPTION_GID["High"] == "1199941453034657"
        assert ld.LEARN_PRIORITY_OPTION_GID["Medium"] == "1199941453034658"
        assert ld.LEARN_PRIORITY_OPTION_GID["Low"] == "1199941453034659"

    def test_normalize_priority(self):
        assert ld._normalize_priority("High") == "High"
        assert ld._normalize_priority("low") == "Low"
        assert ld._normalize_priority("Medium") == "Medium"
        assert ld._normalize_priority(None) == "Medium"
        assert ld._normalize_priority("garbage") == "Medium"

    def test_default_priority_partial_low(self):
        assert ld._default_priority({"partial": True}) == "Low"
        assert ld._default_priority({"partial": False}) == "Medium"

    def _capture(self, monkeypatch, keeper):
        cap = {}

        def fake_req(method, endpoint, data=None):
            if endpoint == "/tasks":
                cap["task"] = data
                return {"gid": "T9"}
            if "addTask" in endpoint:
                cap["section_endpoint"] = endpoint
            return {}
        monkeypatch.setattr(ld.asana_client, "asana_request", fake_req)
        ld.create_triage_task(keeper)
        return cap

    def test_create_task_priority_high(self, monkeypatch):
        k = {"title": "Ariadne raise targets", "summary": "s", "has_action": True,
             "priority": "High", "url": ""}
        cap = self._capture(monkeypatch, k)
        assert cap["task"]["custom_fields"] == {"1199941453034656": "1199941453034657"}

    def test_create_task_priority_medium_default(self, monkeypatch):
        # no priority key -> Medium
        k = {"title": "x", "summary": "s", "has_action": True, "url": ""}
        cap = self._capture(monkeypatch, k)
        assert cap["task"]["custom_fields"]["1199941453034656"] == "1199941453034658"

    def test_create_task_priority_low(self, monkeypatch):
        k = {"title": "promo thread", "summary": "s", "has_action": True,
             "priority": "Low", "url": ""}
        cap = self._capture(monkeypatch, k)
        assert cap["task"]["custom_fields"]["1199941453034656"] == "1199941453034659"

    def test_create_task_routes_to_health_section(self, monkeypatch):
        k = {"title": "Huberman sleep protocol", "summary": "longevity", "topic": "health",
             "has_action": True, "priority": "Medium", "url": ""}
        cap = self._capture(monkeypatch, k)
        assert cap["section_endpoint"] == "/sections/1215899542179143/addTask"

