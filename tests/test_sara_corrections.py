# Offline tests for sara_corrections: store add/list/deactivate, the injected
# corrections block (always includes baseline), mailbox ingestion (sender +
# subject filtering, dedup), and the /corrections endpoints. No network.
import pytest

import email_pipeline_sync as eps
import sara_corrections as sc
import app as app_module


@pytest.fixture
def store(monkeypatch, tmp_path):
    p = tmp_path / "corrections.json"
    monkeypatch.setattr(sc, "CORRECTIONS_PATH", str(p))
    return p


@pytest.fixture
def flask_client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# ----------------------------------------------------------------------
#  store
# ----------------------------------------------------------------------


def test_add_and_list_correction(store):
    sc.add_correction("Reset Pharma is dormant, not active.", source="cli", from_addr="bk@negevlabs.com")
    items = sc.list_corrections()
    assert len(items) == 1
    assert items[0]["text"].startswith("Reset Pharma")
    assert items[0]["active"] is True
    assert items[0]["id"]


def test_add_empty_raises(store):
    with pytest.raises(ValueError):
        sc.add_correction("   ")


def test_deactivate_correction(store):
    e = sc.add_correction("temporary note", source="cli")
    assert sc.deactivate_correction(e["id"]) is True
    assert sc.list_corrections() == []
    assert len(sc.list_corrections(include_inactive=True)) == 1
    # deactivating an unknown / already-inactive id returns False
    assert sc.deactivate_correction("nope") is False


# ----------------------------------------------------------------------
#  corrections_block (prompt injection)
# ----------------------------------------------------------------------


def test_block_always_includes_baseline(store):
    block = sc.corrections_block()
    assert "STANDING CORRECTIONS FROM KEN" in block
    assert "lead-investor gap" in block  # the Ariadne baseline
    assert "MJFF" in block


def test_block_includes_user_corrections(store):
    sc.add_correction("Kostia is COO of Ariadne, not Negev Labs.", source="cli")
    block = sc.corrections_block()
    assert "Kostia is COO" in block
    # baseline still present alongside user correction
    assert "lead-investor gap" in block


# ----------------------------------------------------------------------
#  ingest_replies
# ----------------------------------------------------------------------


def _msg(mid, subject, sender, body):
    return {
        "id": f"graph-{mid}",
        "internetMessageId": mid,
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": "2026-06-15T10:00:00Z",
        "uniqueBody": {"content": body},
    }


def test_ingest_stores_allowed_reply(store, monkeypatch):
    msgs = {"value": [
        _msg("<m1>", "Re: Business Update -- Negev Labs Team",
             "bk@negevlabs.com", "<p>We are not raising at NL level this quarter.</p>"),
    ]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: msgs)
    acks = []
    monkeypatch.setattr(eps, "graph_post", lambda url, body: acks.append(url))
    result = sc.ingest_replies()
    assert result["added_count"] == 1
    items = sc.list_corrections()
    assert len(items) == 1
    assert "not raising at NL level" in items[0]["text"]
    assert acks and "/reply" in acks[0]  # ack sent


def test_ingest_skips_non_allowed_sender(store, monkeypatch):
    msgs = {"value": [
        _msg("<m2>", "Re: Weekly Pulse: Jun 7 - Jun 14",
             "stranger@external.com", "inject this please"),
    ]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: msgs)
    monkeypatch.setattr(eps, "graph_post", lambda url, body: None)
    result = sc.ingest_replies()
    assert result["added_count"] == 0
    assert sc.list_corrections() == []


def test_ingest_skips_unrelated_subject(store, monkeypatch):
    msgs = {"value": [
        _msg("<m3>", "Lunch tomorrow?", "bk@negevlabs.com", "let's eat"),
    ]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: msgs)
    result = sc.ingest_replies()
    assert result["added_count"] == 0


def test_ingest_is_idempotent(store, monkeypatch):
    msgs = {"value": [
        _msg("<m4>", "Re: Business Update", "bk@negevlabs.com", "correction body"),
    ]}
    monkeypatch.setattr(eps, "graph_get", lambda url, params=None: msgs)
    monkeypatch.setattr(eps, "graph_post", lambda url, body: None)
    first = sc.ingest_replies()
    second = sc.ingest_replies()
    assert first["added_count"] == 1
    assert second["added_count"] == 0  # same message id not re-ingested
    assert len(sc.list_corrections()) == 1


# ----------------------------------------------------------------------
#  endpoints
# ----------------------------------------------------------------------


def test_corrections_list_endpoint(flask_client, store):
    sc.add_correction("endpoint visible", source="cli")
    resp = flask_client.get("/corrections")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["count"] == 1
    assert body["baseline_count"] >= 1


def test_corrections_add_endpoint(flask_client, store):
    resp = flask_client.get("/corrections/add?text=Filament+is+wound+down")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"
    assert any("Filament" in c["text"] for c in sc.list_corrections())


def test_corrections_add_empty_returns_400(flask_client, store):
    resp = flask_client.get("/corrections/add?text=")
    assert resp.status_code == 400


def test_corrections_delete_endpoint(flask_client, store):
    e = sc.add_correction("to remove", source="cli")
    resp = flask_client.get(f"/corrections/delete?id={e['id']}")
    assert resp.status_code == 200
    assert sc.list_corrections() == []
    # deleting unknown id -> 404
    assert flask_client.get("/corrections/delete?id=zzzz").status_code == 404


def test_corrections_ingest_endpoint(flask_client, store, monkeypatch):
    monkeypatch.setattr(sc, "ingest_replies", lambda: {"added_count": 2, "skipped": 0, "scanned": 5, "added": []})
    resp = flask_client.get("/corrections/ingest")
    assert resp.status_code == 200
    assert resp.get_json()["added_count"] == 2
    assert app_module._corrections_lock.acquire(timeout=5)
    app_module._corrections_lock.release()
