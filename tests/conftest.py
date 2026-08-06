# Test bootstrap for the Sara meeting pipeline.
# Sets dummy env vars and a temp data dir BEFORE importing app, so the
# module import is side-effect free (no scheduler, no /data writes) and
# every test runs fully offline.
import os
import sys
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="sara-test-data-")

os.environ.setdefault("FIREFLIES_API_KEY", "test-fireflies-key")
os.environ.setdefault("CLAUDE_API_KEY", "test-claude-key")
os.environ.setdefault("HUBSPOT_API_KEY", "test-hubspot-key")
os.environ.setdefault("ASANA_API_KEY", "test-asana-key")
# Read/Learn optional resolver keys -- dummy values so tests never read a real
# key; all x.ai / spoken.md HTTP is mocked and the no_network fixture blocks live calls.
os.environ.setdefault("XAI_API_KEY", "xai-test")
os.environ.setdefault("SPOKEN_API_KEY", "pt_test")
os.environ["DATA_DIR"] = _TEST_DATA_DIR
os.environ["RUN_SCHEDULER"] = "0"
# Production-like domain config (Railway INTERNAL_DOMAINS includes these)
os.environ["INTERNAL_DOMAINS"] = (
    "negevlabs.com,negevcap.com,ariadnebio.com,adres.bio,zirmania.onmicrosoft.com,palomar-labs.com"
)
os.environ["HUBSPOT_OWNER_MAP"] = (
    '{"bk@negevlabs.com":"241153249",'
    '"shlomi@negevlabs.com":"31267643",'
    '"dan@negevlabs.com":"31299775"}'
)
os.environ["HUBSPOT_OWNER_ID"] = "241153249"
# Keep Graph in app-only mode with no creds so nothing tries delegated auth
os.environ.setdefault("MS_GRAPH_CLIENT_ID", "")
os.environ.setdefault("MS_GRAPH_CLIENT_SECRET", "")
os.environ.setdefault("MS_GRAPH_TENANT_ID", "test-tenant")
os.environ.setdefault("MS_GRAPH_REFRESH_TOKEN", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import requests as _requests  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any code path reaches for the real network."""
    def _blocked(*args, **kwargs):
        raise AssertionError("real network call attempted during offline test")
    for method in ("get", "post", "put", "patch", "delete", "head", "request"):
        monkeypatch.setattr(_requests, method, _blocked)
    monkeypatch.setattr(_requests.Session, "request", _blocked)
    yield


@pytest.fixture(autouse=True)
def clean_caches():
    """Per-test isolation for module-level caches."""
    app_module._hubspot_owner_cache.clear()
    yield
    app_module._hubspot_owner_cache.clear()


@pytest.fixture
def pulse_files(monkeypatch, tmp_path):
    """Redirect all pulse state files into a per-test temp dir."""
    monkeypatch.setattr(app_module, "PULSE_STATUS_FILE", str(tmp_path / "pulse_status.json"))
    monkeypatch.setattr(app_module, "PULSE_LOCK_FILE", str(tmp_path / "pulse_lock.json"))
    monkeypatch.setattr(app_module, "PULSE_RUNNING_LOCK_FILE", str(tmp_path / "pulse_running.lock"))
    return tmp_path


@pytest.fixture
def flask_client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()
