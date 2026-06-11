# Offline tests for Teams subscription ensure/renew logic and /teams/subscribe endpoint.
# All Graph HTTP calls are mocked -- no real network.
import json
import pytest
import unittest.mock as mock

import app as app_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resp(status_code, body):
    """Build a minimal fake requests.Response."""
    r = mock.MagicMock()
    r.status_code = status_code
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def _sub_response(sub_id="sub-abc", expiry="2099-01-01T00:00:00.0000000Z"):
    return _make_resp(201, {"id": sub_id, "expirationDateTime": expiry})


def _patch_get_token(monkeypatch):
    monkeypatch.setattr(app_module, "get_graph_app_only_token", lambda: "fake-token")


def _reset_subscription_state(monkeypatch):
    """Clear in-memory subscription globals before each test."""
    monkeypatch.setattr(app_module, "_teams_subscription_id", None)
    monkeypatch.setattr(app_module, "_teams_subscription_expiry", None)


# ---------------------------------------------------------------------------
# ensure_teams_subscription -- no subscription exists -> creates
# ---------------------------------------------------------------------------

class TestEnsureTeamsSubscriptionCreate:
    def test_no_sub_calls_create(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEAMS_TRANSCRIPT_ENABLED", "true")
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", True)
        monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
        monkeypatch.setattr(app_module, "MS_GRAPH_TENANT_ID", "tid")
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        _reset_subscription_state(monkeypatch)
        _patch_get_token(monkeypatch)

        post_resp = _sub_response("new-sub-id", "2099-06-01T00:00:00.0000000Z")
        with mock.patch("requests.post", return_value=post_resp) as mock_post:
            app_module.ensure_teams_subscription()

        mock_post.assert_called_once()
        assert app_module._teams_subscription_id == "new-sub-id"
        assert app_module._teams_subscription_expiry == "2099-06-01T00:00:00.0000000Z"
        # State persisted to disk
        saved = json.loads((tmp_path / "sub.json").read_text())
        assert saved["subscription_id"] == "new-sub-id"

    def test_disabled_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", False)
        _reset_subscription_state(monkeypatch)
        with mock.patch("requests.post") as mock_post:
            app_module.ensure_teams_subscription()
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_teams_subscription -- near-expiry -> PATCH renews
# ---------------------------------------------------------------------------

class TestEnsureTeamsSubscriptionRenew:
    def test_near_expiry_patches(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", True)
        monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
        monkeypatch.setattr(app_module, "MS_GRAPH_TENANT_ID", "tid")
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        # Expiry is 10min from now -- within 30min window
        import datetime
        near_expiry = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.0000000Z"
        )
        monkeypatch.setattr(app_module, "_teams_subscription_id", "existing-sub")
        monkeypatch.setattr(app_module, "_teams_subscription_expiry", near_expiry)
        _patch_get_token(monkeypatch)

        new_expiry = "2099-01-01T00:00:00.0000000Z"
        patch_resp = _make_resp(200, {"id": "existing-sub", "expirationDateTime": new_expiry})
        with mock.patch("requests.patch", return_value=patch_resp) as mock_patch, \
             mock.patch("requests.post") as mock_post:
            app_module.ensure_teams_subscription()

        mock_patch.assert_called_once()
        mock_post.assert_not_called()
        assert app_module._teams_subscription_expiry == new_expiry

    def test_fresh_subscription_is_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", True)
        monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
        monkeypatch.setattr(app_module, "MS_GRAPH_TENANT_ID", "tid")
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        # Expiry is 45min from now -- well beyond 30min threshold
        import datetime
        fresh_expiry = (datetime.datetime.utcnow() + datetime.timedelta(minutes=45)).strftime(
            "%Y-%m-%dT%H:%M:%S.0000000Z"
        )
        monkeypatch.setattr(app_module, "_teams_subscription_id", "fresh-sub")
        monkeypatch.setattr(app_module, "_teams_subscription_expiry", fresh_expiry)

        with mock.patch("requests.patch") as mock_patch, \
             mock.patch("requests.post") as mock_post:
            app_module.ensure_teams_subscription()

        mock_patch.assert_not_called()
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_teams_subscription -- Graph 404 on renew -> falls back to create
# ---------------------------------------------------------------------------

class TestEnsureTeamsSubscriptionFallback:
    def test_404_on_renew_recreates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", True)
        monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
        monkeypatch.setattr(app_module, "MS_GRAPH_TENANT_ID", "tid")
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        import datetime
        near_expiry = (datetime.datetime.utcnow() + datetime.timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.0000000Z"
        )
        monkeypatch.setattr(app_module, "_teams_subscription_id", "gone-sub")
        monkeypatch.setattr(app_module, "_teams_subscription_expiry", near_expiry)
        _patch_get_token(monkeypatch)

        patch_resp = _make_resp(404, {"error": {"code": "ExtensionError"}})
        create_resp = _sub_response("brand-new-sub", "2099-01-01T00:00:00.0000000Z")
        with mock.patch("requests.patch", return_value=patch_resp), \
             mock.patch("requests.post", return_value=create_resp) as mock_post:
            app_module.ensure_teams_subscription()

        mock_post.assert_called_once()
        assert app_module._teams_subscription_id == "brand-new-sub"


# ---------------------------------------------------------------------------
# /teams/subscribe endpoint
# ---------------------------------------------------------------------------

class TestTeamsSubscribeEndpoint:
    @pytest.fixture
    def client(self):
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client()

    def test_success_returns_ok_with_id(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", True)
        monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
        monkeypatch.setattr(app_module, "MS_GRAPH_TENANT_ID", "tid")
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        _patch_get_token(monkeypatch)

        post_resp = _sub_response("endpoint-sub", "2099-01-01T00:00:00.0000000Z")
        with mock.patch("requests.post", return_value=post_resp):
            rv = client.post("/teams/subscribe")

        assert rv.status_code == 200
        body = rv.get_json()
        assert body["status"] == "ok"
        assert body["subscription_id"] == "endpoint-sub"
        assert body["expires"] == "2099-01-01T00:00:00.0000000Z"

    def test_graph_error_returns_502_and_error_status(self, monkeypatch, client, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", True)
        monkeypatch.setattr(app_module, "MS_GRAPH_CLIENT_ID", "cid")
        monkeypatch.setattr(app_module, "MS_GRAPH_TENANT_ID", "tid")
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        _patch_get_token(monkeypatch)

        error_resp = _make_resp(400, {"error": {"code": "ValidationError", "message": "timed out"}})
        with mock.patch("requests.post", return_value=error_resp):
            rv = client.post("/teams/subscribe")

        assert rv.status_code == 502
        body = rv.get_json()
        assert body["status"] == "error"
        assert body["http_status"] == 400

    def test_not_configured_returns_400(self, monkeypatch, client):
        monkeypatch.setattr(app_module, "TEAMS_TRANSCRIPT_ENABLED", False)
        rv = client.post("/teams/subscribe")
        assert rv.status_code == 400
        assert rv.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# State file round-trip
# ---------------------------------------------------------------------------

class TestTeamsSubscriptionStateFile:
    def test_save_and_load_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "sub.json"))
        app_module._save_teams_subscription_state("rt-sub", "2099-06-01T00:00:00.0000000Z")
        sub_id, expiry = app_module._load_teams_subscription_state()
        assert sub_id == "rt-sub"
        assert expiry == "2099-06-01T00:00:00.0000000Z"

    def test_load_missing_file_returns_nones(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "TEAMS_SUBSCRIPTION_FILE", str(tmp_path / "nonexistent.json"))
        sub_id, expiry = app_module._load_teams_subscription_state()
        assert sub_id is None
        assert expiry is None
