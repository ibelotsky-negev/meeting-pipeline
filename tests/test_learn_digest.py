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
    monkeypatch.setattr(ld, "LEARN_PENDING_STT_FILE", str(tmp_path / "learn_pending_stt.json"))
    monkeypatch.setattr(ld, "LEARN_STT_STATUS_FILE", str(tmp_path / "learn_stt_status.json"))
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
#  5b. FIX 1: financial deal-analysis / investment-workflow automation tooling
#      is the HIGH-priority class -- it OUTRANKS the generic AI-tooling = Medium
#      default; generic dev/coding tooling stays Medium.
# ----------------------------------------------------------------------

class TestFinancialPriorityClass:
    def _fin_item(self, **kw):
        base = {
            "title": "Anthropic open-sources Wall Street financial-workflow agents",
            "type": "article", "subject": "Claude for Financial Services",
            "content_date": "2026-06-20",
            "summary": ("Open-source agents that automate financial deal analysis and "
                        "investment-workflow tasks (valuation, due-diligence data pulls)."),
            "specifics": [], "partial": False,
        }
        base.update(kw)
        return base

    def _generic_code_item(self, **kw):
        base = {
            "title": "New GitHub Copilot autocomplete for developers",
            "type": "article", "subject": "Copilot",
            "content_date": "2026-06-20",
            "summary": "A coding assistant that suggests code completions in your IDE.",
            "specifics": [], "partial": False,
        }
        base.update(kw)
        return base

    def test_detector_flags_financial_workflow_tool(self):
        assert ld._is_financial_workflow_tool(self._fin_item()) is True

    def test_detector_ignores_generic_coding_tool(self):
        assert ld._is_financial_workflow_tool(self._generic_code_item()) is False

    def test_detector_ignores_model_evaluation_false_positive(self):
        # "evaluation" must NOT trip the "valuation" finance signal.
        item = {"title": "A harness for LLM model evaluation", "subject": "evals",
                "summary": "Run model evaluation suites with this agent framework.",
                "topic": "", "specifics": []}
        assert ld._is_financial_workflow_tool(item) is False

    def test_finance_tool_floored_to_high_single_item(self):
        # single-item cluster default is Medium; the finance floor lifts to High.
        out = ld.curate_cluster({"topic": "Claude for Financial Services",
                                 "items": [self._fin_item()]})
        assert out["keepers"][0]["priority"] == "High"

    def test_generic_coding_stays_medium_single_item(self):
        out = ld.curate_cluster({"topic": "Coding assistants",
                                 "items": [self._generic_code_item()]})
        assert out["keepers"][0]["priority"] == "Medium"

    def test_finance_floor_overrides_model_medium(self):
        # the model judges Medium; the deterministic floor must override to High.
        items = [self._fin_item(),
                 self._fin_item(title="FactSet ships a deal-data agent")]

        def fake_call(prompt, model):
            return json.dumps({
                "keepers": [{"index": 0, "why": "best", "bucket": "Zirmania Family Office",
                             "priority": "Medium", "has_action": False, "action": ""}],
                "superseded": [{"index": 1, "reason": "dup"}],
            })

        out = ld.curate_cluster({"topic": "Claude for Financial Services", "items": items},
                                call_fn=fake_call)
        assert out["keepers"][0]["priority"] == "High"

    def test_generic_coding_not_floored_model_medium_stays(self):
        items = [self._generic_code_item(),
                 self._generic_code_item(title="Cursor tab improvements")]

        def fake_call(prompt, model):
            return json.dumps({
                "keepers": [{"index": 0, "why": "best", "bucket": "General/Reference",
                             "priority": "Medium", "has_action": False, "action": ""}],
                "superseded": [{"index": 1, "reason": "dup"}],
            })

        out = ld.curate_cluster({"topic": "Coding assistants", "items": items},
                                call_fn=fake_call)
        assert out["keepers"][0]["priority"] == "Medium"

    def test_finance_tool_routes_to_zirmania(self):
        # routing decision: this class -> Zirmania Family Office by default.
        gid = ld.route_section(self._fin_item(
            topic="Claude for Financial Services deal-analysis agents"))
        assert gid == ld.LEARN_SECTION_GID["Zirmania Family Office"]


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

    def test_unverifiable_claim_keeps_relevance_and_does_not_downgrade(self):
        # FIX 2 exemplar: the specific repo claim cannot be web-confirmed, but the
        # capability is real and high-relevance. The caveat is informational only;
        # it must NOT frame the item as superseded/fabricated and must NOT touch
        # priority (relevance is curation's call, not the currency check's).
        keeper = {"title": "Anthropic open-sources financial-workflow agents",
                  "content_date": "2026-06-20",
                  "summary": "Open-source repo for Claude financial deal-analysis agents.",
                  "priority": "High"}

        def fake_web(prompt):
            return json.dumps({"current": True, "verifiable": False,
                               "note": "could not confirm the named open-source repo exists"})

        out = ld.currency_check(keeper, "Claude for Financial Services agents",
                                mode="fast-moving", call_fn=fake_web)
        assert out["priority"] == "High"                       # not auto-downgraded
        note = out["currency_note"].lower()
        assert "superseded" not in note                        # not framed as outdated
        assert "fabricat" not in note                          # not framed as fabricated
        assert "unverified" in note                            # caveat IS recorded
        assert "relevance" in note

    def test_superseded_still_flagged_when_verifiable(self):
        # The genuinely-superseded path is unchanged: name the newer resource.
        keeper = {"title": "Old approach", "content_date": "2025-01-01", "summary": "..."}

        def fake_web(prompt):
            return json.dumps({"current": False, "verifiable": True,
                               "note": "superseded by the v2 connector"})

        out = ld.currency_check(keeper, "Claude Code MCP", mode="fast-moving", call_fn=fake_web)
        assert out["currency_note"].startswith("likely superseded")
        assert "v2 connector" in out["currency_note"]


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
        text, err = ld._grok_stt_from_file("/tmp/fake.m4a")
        assert text is None and "XAI_API_KEY" in (err or "")

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


class TestFetchWindow:
    """A normal run filters by a trailing receivedDateTime window (read/unread
    agnostic); a backlog run omits any $filter so the whole folder is fetched."""

    def _capture(self, monkeypatch):
        seen = {}
        page = {"value": [{"id": "m1", "subject": "x"}]}

        def _get(url, params=None):
            seen["params"] = params or {}
            return page
        monkeypatch.setattr(ld.eps, "graph_get", _get)
        return seen

    def test_normal_run_uses_recency_window_not_read_flag(self, monkeypatch):
        seen = self._capture(monkeypatch)
        ld.fetch_unread()
        flt = seen["params"].get("$filter", "")
        assert flt.startswith("receivedDateTime ge ")  # window, not read state
        assert "isRead" not in flt  # read items are no longer skipped

    def test_backlog_run_has_no_filter(self, monkeypatch):
        seen = self._capture(monkeypatch)
        ld.fetch_unread(backlog=True)
        assert "$filter" not in seen["params"]

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


# ----------------------------------------------------------------------
#  14. X-video STT capture + replay
# ----------------------------------------------------------------------

GROK_VIDEO_RESPONSE = {
    "output_text": None,
    "output": [{
        "type": "message", "role": "assistant",
        "content": [{
            "type": "output_text",
            "text": "Author @hamptonism. Peter Thiel on AI. Attached video plays inline.\nVIDEO_WITH_AUDIO: yes",
            "annotations": [{"type": "url_citation", "url": "https://x.com/hamptonism/status/1"}],
        }],
    }],
}


class TestXVideoSttCapture:
    def test_x_post_has_video_marker(self):
        assert ld._x_post_has_video("Some post.\nVIDEO_WITH_AUDIO: yes")
        assert ld._x_post_has_video("Post with attached video media")
        assert not ld._x_post_has_video("Plain text only")

    def test_resolve_x_flags_video_and_needs_stt(self, monkeypatch):
        monkeypatch.setattr(ld, "_grok_responses_call", lambda prompt, model: GROK_VIDEO_RESPONSE)
        monkeypatch.setattr(ld, "_probe_x_native_video", lambda url, timeout=None: (True, 42, ""))
        r = ld.resolve_x("https://x.com/hamptonism/status/1")
        assert r["needs_stt"] is True
        assert r["partial"] is True
        assert "pending STT replay" in r["reason"]

    def test_resolve_x_video_no_native_surfaces_summary_no_stt(self, monkeypatch):
        # Grok says video-with-audio, but yt-dlp finds no native clip -> do NOT
        # queue STT; surface Grok's summary instead (partial, needs_stt False).
        monkeypatch.setattr(ld, "_grok_responses_call", lambda prompt, model: GROK_VIDEO_RESPONSE)
        monkeypatch.setattr(ld, "_probe_x_native_video",
                            lambda url, timeout=None: (False, 0, "No video could be found in this tweet"))
        r = ld.resolve_x("https://x.com/hamptonism/status/1")
        assert r["needs_stt"] is False
        assert r["partial"] is True
        assert "not natively downloadable" in r["reason"]
        assert r["text"]  # Grok visual/text content is preserved, not discarded

    def test_capture_writes_pending_entry(self, learn_files):
        ld._capture_pending_stt({
            "source_url": "https://x.com/hamptonism/status/1",
            "title": "Thiel video",
            "date": "2026-06-20T10:00:00Z",
            "processed_folder_location": "Processed",
        })
        data = ld._load_pending_stt()
        assert len(data["entries"]) == 1
        e = data["entries"][0]
        assert e["source_url"] == "https://x.com/hamptonism/status/1"
        assert e["title"] == "Thiel video"
        assert e["status"] == "pending"
        assert e["attempts"] == 0

    def test_capture_dedupes_by_source_url(self, learn_files):
        base = {
            "source_url": "https://x.com/hamptonism/status/1",
            "title": "A",
            "date": "2026-06-20",
            "processed_folder_location": "Processed",
        }
        ld._capture_pending_stt(base)
        ld._capture_pending_stt({**base, "title": "B"})
        assert len(ld._load_pending_stt()["entries"]) == 1
        assert ld._load_pending_stt()["entries"][0]["title"] == "B"


class TestXVideoSttReplay:
    def _seed_pending(self, learn_files, **overrides):
        entry = {
            "id": "abc12345",
            "source_url": "https://x.com/hamptonism/status/1",
            "title": "Thiel video",
            "date": "2026-06-20",
            "processed_folder_location": "Processed",
            "status": "pending",
            "attempts": 0,
            "last_error": "",
            "transcript": "",
            "captured_at": "2026-06-20T10:00:00Z",
        }
        entry.update(overrides)
        ld._save_pending_stt({"entries": [entry]})
        return entry

    def test_replay_success_marks_done(self, learn_files, monkeypatch, tmp_path):
        self._seed_pending(learn_files)
        scratch = tmp_path / "ytdlp_scratch"
        scratch.mkdir()
        audio = scratch / "clip.m4a"
        audio.write_bytes(b"fake-audio")

        monkeypatch.setattr(
            ld, "extract_x_post_audio",
            lambda url, timeout=None: (str(audio), 30.0, None, str(scratch)),
        )
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda path, timeout=None: ("hello transcript", None))
        monkeypatch.setattr(ld, "send_digest_email", lambda s, b: None)

        result = ld.run_stt_replay(dry_run=False, send_email=False)
        assert result["succeeded"] == 1
        e = ld._load_pending_stt()["entries"][0]
        assert e["status"] == "done"
        assert e["transcript"] == "hello transcript"

    def test_stt_failure_increments_attempts(self, learn_files, monkeypatch, tmp_path):
        self._seed_pending(learn_files)
        scratch = tmp_path / "ytdlp_scratch"
        scratch.mkdir()
        audio = scratch / "clip.m4a"
        audio.write_bytes(b"fake-audio")
        monkeypatch.setattr(
            ld, "extract_x_post_audio",
            lambda url, timeout=None: (str(audio), 30.0, None, str(scratch)),
        )
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda path, timeout=None: (None, "STT empty transcript"))

        ld.run_stt_replay(dry_run=False, send_email=False)
        e = ld._load_pending_stt()["entries"][0]
        assert e["status"] == "pending"
        assert e["attempts"] == 1
        assert "STT" in e["last_error"]

    def test_attempts_cap_marks_permanently_failed(self, learn_files, monkeypatch, tmp_path):
        self._seed_pending(learn_files, attempts=2)
        scratch = tmp_path / "ytdlp_scratch"
        scratch.mkdir()
        audio = scratch / "clip.m4a"
        audio.write_bytes(b"fake-audio")
        monkeypatch.setattr(
            ld, "extract_x_post_audio",
            lambda url, timeout=None: (str(audio), 30.0, None, str(scratch)),
        )
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda path, timeout=None: (None, "STT timeout"))

        ld.run_stt_replay(dry_run=False, send_email=False)
        e = ld._load_pending_stt()["entries"][0]
        assert e["status"] == "failed"
        assert e["attempts"] == 3

    def test_oversized_video_skipped_with_reason(self, learn_files, monkeypatch, tmp_path):
        self._seed_pending(learn_files)
        scratch = str(tmp_path / "ytdlp_scratch")
        monkeypatch.setattr(
            ld, "extract_x_post_audio",
            lambda url, timeout=None: (None, 0, "duration 7200s exceeds cap (3600s)", scratch),
        )

        ld.run_stt_replay(dry_run=False, send_email=False)
        e = ld._load_pending_stt()["entries"][0]
        assert e["status"] == "pending"
        assert "duration" in e["last_error"]
        assert e["attempts"] == 1

    def test_dry_run_does_not_write_store(self, learn_files):
        self._seed_pending(learn_files)
        result = ld.run_stt_replay(dry_run=True, send_email=False)
        assert result["queued"] == 1
        e = ld._load_pending_stt()["entries"][0]
        assert e["status"] == "pending"
        assert e["attempts"] == 0

    def test_replay_success_writes_status_when_email_fails(
        self, learn_files, monkeypatch, tmp_path,
    ):
        self._seed_pending(learn_files)
        scratch = tmp_path / "ytdlp_scratch"
        scratch.mkdir()
        audio = scratch / "clip.m4a"
        audio.write_bytes(b"fake-audio")
        monkeypatch.setattr(
            ld, "extract_x_post_audio",
            lambda url, timeout=None: (str(audio), 30.0, None, str(scratch)),
        )
        monkeypatch.setattr(ld, "_grok_stt_from_file", lambda path, timeout=None: ("hello transcript", None))

        def boom(subject, body):
            raise RuntimeError("smtp down")

        monkeypatch.setattr(ld, "send_digest_email", boom)
        result = ld.run_stt_replay(dry_run=False, send_email=True)
        assert result["status"] == "ok"
        assert result["succeeded"] == 1
        assert result["sent"] is False
        assert "smtp down" in (result.get("email_error") or "")
        status = ld.read_stt_status()
        assert status["succeeded"] == 1
        assert ld._load_pending_stt()["entries"][0]["status"] == "done"


class TestPendingSttConcurrency:
    def test_concurrent_capture_preserves_all_entries(self, learn_files):
        def capture(i):
            ld._capture_pending_stt({
                "source_url": f"https://x.com/user/status/{i}",
                "title": f"video {i}",
                "date": "2026-06-20",
                "processed_folder_location": "Processed",
            })

        threads = [threading.Thread(target=capture, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = ld._load_pending_stt()["entries"]
        assert len(entries) == 12
        urls = {ld._normalize_x_url(e["source_url"]) for e in entries}
        assert len(urls) == 12


class TestPickExtractedAudio:
    def test_prefers_m4a_over_thumbnail(self, tmp_path):
        (tmp_path / "thumb.jpg").write_bytes(b"thumb")
        m4a = tmp_path / "clip.m4a"
        m4a.write_bytes(b"audio" * 200)
        (tmp_path / "other.mp3").write_bytes(b"x")
        picked = ld._pick_extracted_audio_path(str(tmp_path), {}, None, "")
        assert picked == str(m4a)

    def test_uses_requested_downloads_filepath(self, tmp_path):
        audio = tmp_path / "from_ytdlp.m4a"
        audio.write_bytes(b"audio")
        info = {"requested_downloads": [{"filepath": str(audio)}]}
        assert ld._pick_extracted_audio_path(str(tmp_path), info, None, "") == str(audio)



# ----------------------------------------------------------------------
#  X-video content is surfaced (not discarded) while its spoken transcript
#  is pending STT replay -- partial-but-retrieved items are summarized.
# ----------------------------------------------------------------------

class TestPartialWithContentSurfaced:
    def test_summarize_partial_xvideo_surfaces_content_and_flags_pending(self):
        # resolve_x returns partial=True (needs_stt) but WITH the Grok visual/text
        # description. That content must be summarized, not thrown away.
        item = {"subject": "Fable loops", "url": "https://x.com/u/status/7", "type": "x"}
        resolved = {"text": "Author explains a Fable agent loop over a task queue.",
                    "kind": "x", "partial": True, "reason": "x-video audio pending STT replay",
                    "content_date": None, "citations": ["https://x.com/i/status/7"]}
        summ = ld.summarize_item(
            item, resolved,
            call_fn=lambda p, m: '{"summary": "A loop over a task queue.", "confidence": "medium"}')
        assert summ["content_retrieved"] is True
        assert "content not retrieved" not in summ["summary"]
        assert summ["summary"].startswith("[x-video audio pending STT replay] ")
        assert "task queue" in summ["summary"]

    def test_summarize_no_content_still_reports_not_retrieved(self):
        item = {"subject": "Cost optimization", "url": "https://x.com/i/status/8", "type": "x"}
        resolved = ld._partial("x", "Grok could not access the post (CANNOT_ACCESS)")
        summ = ld.summarize_item(item, resolved, call_fn=lambda p, m: "{}")
        assert summ["content_retrieved"] is False
        assert summ["summary"].startswith("content not retrieved -- from title/sender only")
        assert "CANNOT_ACCESS" in summ["summary"]

    def test_render_omits_not_retrieved_banner_when_content_retrieved(self):
        keeper = {"title": "Best", "url": "https://x.com/i/status/7", "summary": "s",
                  "why": "w", "bucket": "General/Reference", "has_action": False,
                  "action": "", "partial": True, "content_retrieved": True}
        html = ld.render_digest_html([{"topic": "T", "superseded": [], "keepers": [keeper]}])
        assert "content not retrieved" not in html

    def test_render_shows_not_retrieved_banner_when_no_content(self):
        keeper = {"title": "Best", "url": "https://x.com/i/status/8", "summary": "s",
                  "why": "w", "bucket": "General/Reference", "has_action": False,
                  "action": "", "partial": True, "content_retrieved": False}
        html = ld.render_digest_html([{"topic": "T", "superseded": [], "keepers": [keeper]}])
        assert "content not retrieved" in html


# ----------------------------------------------------------------------
#  Important video keepers become Asana "watch" tasks (Video to watch
#  section) even without an explicit action; detection is tightened so
#  no-native-video X posts are not stranded in the STT queue.
# ----------------------------------------------------------------------

class TestVideoWatchTasks:
    def test_is_watchable_video_true_for_high_med_video(self):
        assert ld._is_watchable_video({"type": "x", "priority": "High"}) is True
        assert ld._is_watchable_video({"type": "youtube", "priority": "Medium"}) is True

    def test_is_watchable_video_false_for_low_or_nonvideo(self):
        assert ld._is_watchable_video({"type": "x", "priority": "Low"}) is False
        assert ld._is_watchable_video({"type": "article", "priority": "High"}) is False
        assert ld._is_watchable_video({"type": "podcast", "priority": "High"}) is False

    def test_watch_task_routes_to_video_section_and_prefixes_title(self, monkeypatch):
        calls = []
        def fake(method, endpoint, data=None):
            calls.append((method, endpoint, data))
            if endpoint == "/tasks":
                return {"gid": "T1"}
            return {}
        monkeypatch.setattr(ld.asana_client, "asana_request", fake)
        gid = ld.create_triage_task(
            {"title": "Peter Thiel clip", "type": "x", "priority": "High",
             "url": "https://x.com/i/status/1", "why": "w", "summary": "s"},
            watch=True)
        assert gid == "T1"
        # task created with a "Watch: " prefixed name
        create = [c for c in calls if c[1] == "/tasks"][0]
        assert create[2]["name"].startswith("Watch: ")
        # added to the manual "Video to watch" section, not a topic bucket
        add = [c for c in calls if "/addTask" in c[1]][0]
        assert add[1] == f"/sections/{ld.VIDEO_TO_WATCH_SECTION_GID}/addTask"

    def test_non_watch_task_still_topic_routed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ld.asana_client, "asana_request",
                            lambda m, e, data=None: calls.append((m, e, data)) or (
                                {"gid": "T2"} if e == "/tasks" else {}))
        ld.create_triage_task(
            {"title": "Biotech deal tool", "type": "article", "priority": "High",
             "topic": "Negev Labs biotech", "url": "https://e/1", "why": "w", "summary": "s"})
        add = [c for c in calls if "/addTask" in c[1]][0]
        assert f"/sections/{ld.VIDEO_TO_WATCH_SECTION_GID}/" not in add[1]


# ----------------------------------------------------------------------
#  YouTube caption resolver -- both library generations
#
#  The API changed incompatibly at youtube-transcript-api 1.0 (static
#  get_transcript -> instance fetch, dict chunks -> snippet objects), and the
#  old 0.6.2 pin broke against YouTube's current response format, returning
#  "no element found: line 1, column 0" for every video. These pin the shim
#  that spans both -- nothing covered it before, because every other test in
#  the repo mocks _fetch_youtube_transcript itself rather than its internals.
# ----------------------------------------------------------------------

class _Snippet:
    """Stand-in for a 1.x FetchedTranscriptSnippet (carries .text)."""

    def __init__(self, text):
        self.text = text


class _Track:
    """Stand-in for a 1.x Transcript: one caption track in one language."""

    def __init__(self, language_code, is_generated, texts):
        self.language_code = language_code
        self.is_generated = is_generated
        self._texts = texts

    def fetch(self):
        return [_Snippet(t) for t in self._texts]


def _api_with_tracks(*tracks):
    """A 1.x-shaped fake API exposing list() -> tracks. `fetch` is defined only
    so the resolver takes the 1.x branch; calling it is a failure."""

    class Api1x:
        def list(self, vid):
            return list(tracks)

        def fetch(self, vid):
            raise AssertionError("resolver must go through list(), not fetch()")

    return Api1x


def _install_fake_yta(monkeypatch, api_cls):
    """Inject a fake youtube_transcript_api so the lazy import inside the
    resolver picks it up. Offline: the real library is never imported."""
    import sys
    import types
    mod = types.ModuleType("youtube_transcript_api")
    mod.YouTubeTranscriptApi = api_cls
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", mod)


class TestFetchYoutubeTranscript:
    _URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        """Transient failures retry with a real sleep in production. Tests must
        exercise the retry path without paying for it."""
        monkeypatch.setattr(ld, "LEARN_YT_RETRY_WAIT", 0)

    def test_modern_1x_instance_api_with_snippet_objects(self, monkeypatch):
        _install_fake_yta(monkeypatch,
                          _api_with_tracks(_Track("en", True, ["hello", "world"])))
        assert ld._fetch_youtube_transcript(self._URL) == "hello world"

    def test_non_english_only_video_is_transcribed(self, monkeypatch):
        """The regression that mattered: api.fetch(vid) defaults to English and
        raises NoTranscriptFound when the only captions are in another language,
        so a Russian-captioned video looked exactly like one with no captions."""
        _install_fake_yta(monkeypatch,
                          _api_with_tracks(_Track("ru", True, ["privet", "mir"])))
        assert ld._fetch_youtube_transcript(self._URL) == "privet mir"

    def test_manual_track_preferred_over_auto_generated(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks(
            _Track("en", True, ["asr"]),
            _Track("de", False, ["human"]),
        ))
        assert ld._fetch_youtube_transcript(self._URL) == "human"

    def test_first_track_used_when_all_auto_generated(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks(
            _Track("ru", True, ["first"]),
            _Track("en", True, ["second"]),
        ))
        assert ld._fetch_youtube_transcript(self._URL) == "first"

    def test_no_tracks_at_all_is_none(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks())
        assert ld._fetch_youtube_transcript(self._URL) is None

    def test_legacy_06x_static_api_with_dict_chunks(self, monkeypatch):
        seen = []

        class Api06:
            @staticmethod
            def get_transcript(vid):
                seen.append(vid)
                return [{"text": "hello"}, {"text": "world"}]

        _install_fake_yta(monkeypatch, Api06)
        assert ld._fetch_youtube_transcript(self._URL) == "hello world"
        assert seen == ["dQw4w9WgXcQ"]

    def test_fetch_is_preferred_when_both_exist(self, monkeypatch):
        """1.x is the generation that actually works -- never fall back to the
        legacy call while fetch is present."""

        class ApiBoth:
            def list(self, vid):
                return [_Track("en", True, ["modern"])]

            def fetch(self, vid):
                return [_Snippet("modern")]

            @staticmethod
            def get_transcript(vid):
                raise AssertionError("legacy path must not run when fetch exists")

        _install_fake_yta(monkeypatch, ApiBoth)
        assert ld._fetch_youtube_transcript(self._URL) == "modern"

    def test_resolver_error_degrades_to_none(self, monkeypatch):
        """The exact 0.6.2 breakage shape -- must degrade so the caller can
        still try yt-dlp rather than raising out."""

        class ApiBroken:
            def list(self, vid):
                raise Exception("no element found: line 1, column 0")

            def fetch(self, vid):
                raise Exception("no element found: line 1, column 0")

        _install_fake_yta(monkeypatch, ApiBroken)
        assert ld._fetch_youtube_transcript(self._URL) is None

    def test_empty_transcript_is_none_not_empty_string(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks(_Track("en", True, [])))
        assert ld._fetch_youtube_transcript(self._URL) is None

    def test_blank_snippets_are_skipped(self, monkeypatch):
        _install_fake_yta(monkeypatch,
                          _api_with_tracks(_Track("en", True, ["", "kept", None])))
        assert ld._fetch_youtube_transcript(self._URL) == "kept"

    def test_no_video_id_never_reaches_the_library(self, monkeypatch):
        class ApiNever:
            def list(self, vid):
                raise AssertionError("must not be reached without a video id")

            def fetch(self, vid):
                raise AssertionError("must not be reached without a video id")

        _install_fake_yta(monkeypatch, ApiNever)
        assert ld._fetch_youtube_transcript("https://www.youtube.com/@somechannel") is None


# ----------------------------------------------------------------------
#  Transient-vs-permanent caption failures
#
#  A rate-limited fetch used to be indistinguishable from "this video has no
#  captions": both returned None, the caller fell through to yt-dlp, and the
#  reply carried whatever THAT failed with -- usually "duration exceeds cap".
#  A real user read that and concluded there was an hour limit on videos.
# ----------------------------------------------------------------------

class _LibError(Exception):
    """Stand-in for a youtube_transcript_api exception: what matters to the
    classifier is the class name and its defining module."""
    __module__ = "youtube_transcript_api._errors"


def _named_lib_error(name):
    return type(name, (_LibError,), {"__module__": "youtube_transcript_api._errors"})


class TestTransientClassification:
    def test_rate_limit_and_request_errors_are_transient(self):
        for name in ("RequestBlocked", "IpBlocked", "YouTubeRequestFailed",
                     "YouTubeDataUnparsable"):
            assert ld._is_transient_yt_error(_named_lib_error(name)("x")), name

    def test_settled_facts_about_the_video_are_permanent(self):
        for name in ("TranscriptsDisabled", "VideoUnavailable", "InvalidVideoId",
                     "AgeRestricted", "NoTranscriptFound"):
            assert not ld._is_transient_yt_error(_named_lib_error(name)("x")), name

    def test_network_errors_outside_the_library_are_transient(self):
        import requests
        assert ld._is_transient_yt_error(TimeoutError("read timed out"))
        assert ld._is_transient_yt_error(ConnectionError("dns"))
        assert ld._is_transient_yt_error(OSError("socket"))
        assert ld._is_transient_yt_error(requests.RequestException("boom"))

    def test_unrecognized_exceptions_are_permanent_not_retried(self):
        """A code-level break is deterministic -- retrying it just multiplies a
        guaranteed failure. The 0.6.2 incident raised a bare Exception for EVERY
        video; that must fail fast, not three times per video."""
        assert not ld._is_transient_yt_error(Exception("no element found: line 1, column 0"))
        assert not ld._is_transient_yt_error(AttributeError("fetch"))
        assert not ld._is_transient_yt_error(TypeError("bad arg"))


class TestCaptionRetry:
    _URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr(ld, "LEARN_YT_RETRY_WAIT", 0)

    def _api_failing_then(self, fails, exc, texts=("ok",)):
        calls = {"n": 0}

        class Api:
            def list(self, vid):
                calls["n"] += 1
                if calls["n"] <= fails:
                    raise exc
                return [_Track("en", True, list(texts))]

            def fetch(self, vid):
                raise AssertionError("must go through list()")

        return Api, calls

    def test_transient_failure_is_retried_and_succeeds(self, monkeypatch):
        monkeypatch.setattr(ld, "LEARN_YT_ATTEMPTS", 3)
        Api, calls = self._api_failing_then(2, _named_lib_error("RequestBlocked")("429"))
        _install_fake_yta(monkeypatch, Api)
        text, err = ld.fetch_youtube_transcript(self._URL)
        assert text == "ok" and err == ""
        assert calls["n"] == 3  # two failures then success

    def test_unrecognized_exception_fails_fast(self, monkeypatch):
        """The 0.6.2 breakage shape. Deterministic, so one attempt only."""
        Api, calls = self._api_failing_then(99, Exception("no element found: line 1, column 0"))
        _install_fake_yta(monkeypatch, Api)
        text, err = ld.fetch_youtube_transcript(self._URL)
        assert text is None and err == ""
        assert calls["n"] == 1

    def test_permanent_failure_is_not_retried(self, monkeypatch):
        Api, calls = self._api_failing_then(99, _named_lib_error("TranscriptsDisabled")("off"))
        _install_fake_yta(monkeypatch, Api)
        text, err = ld.fetch_youtube_transcript(self._URL)
        assert text is None
        assert err == ""          # not transient -> caller uses its audio fallback
        assert calls["n"] == 1    # fail fast, do not waste the caller's time

    def test_exhausted_retries_report_a_transient_reason(self, monkeypatch):
        Api, calls = self._api_failing_then(99, _named_lib_error("IpBlocked")("blocked"))
        _install_fake_yta(monkeypatch, Api)
        text, err = ld.fetch_youtube_transcript(self._URL)
        assert text is None
        assert "captions fetch failed (temporary)" in err
        assert "IpBlocked" in err
        assert calls["n"] == ld.LEARN_YT_ATTEMPTS

    def test_success_first_try_reports_no_error(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks(_Track("ru", True, ["privet"])))
        assert ld.fetch_youtube_transcript(self._URL) == ("privet", "")

    def test_video_without_captions_is_not_an_error(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks())
        assert ld.fetch_youtube_transcript(self._URL) == (None, "")

    def test_text_only_wrapper_still_returns_a_string(self, monkeypatch):
        _install_fake_yta(monkeypatch, _api_with_tracks(_Track("en", True, ["hi"])))
        assert ld._fetch_youtube_transcript(self._URL) == "hi"
