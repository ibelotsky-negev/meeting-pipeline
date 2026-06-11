# Tests that the Asana webhook handler fetches task state from the API
# instead of trusting new_value in the payload (Asana sends new_value=None
# for all fields).
import threading

import app as app_module


def _event(action="changed", field="completed", new_value=None, gid="111"):
    return {
        "action": action,
        "resource": {"resource_type": "task", "gid": gid},
        "change": {"field": field, "new_value": new_value},
    }


def test_handshake_echoes_hook_secret(flask_client):
    resp = flask_client.post("/webhook/asana", headers={"X-Hook-Secret": "abc123"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Hook-Secret") == "abc123"


def test_completed_event_fetches_state_from_api(flask_client, monkeypatch):
    done = threading.Event()
    api_calls = []
    completed_calls = []

    def fake_asana_request(method, endpoint, data=None):
        api_calls.append((method, endpoint))
        return {"completed": True}

    def fake_complete(todo_task_id, asana_gid=None):
        completed_calls.append((todo_task_id, asana_gid))
        done.set()

    monkeypatch.setattr(app_module, "asana_request", fake_asana_request)
    monkeypatch.setattr(app_module, "complete_todo_task", fake_complete)
    monkeypatch.setattr(
        app_module, "load_sync_map",
        lambda: {"mappings": {"111": {"todo_task_id": "td-1"}}})

    # Asana sends new_value=None even when the task was just completed
    resp = flask_client.post("/webhook/asana", json={"events": [_event(new_value=None)]})
    assert resp.status_code == 200
    assert done.wait(timeout=5), "webhook background thread never completed the To-Do"

    # The handler fetched the real state from the Asana API
    assert any("completed" in endpoint for method, endpoint in api_calls
               if method == "GET")
    assert completed_calls == [("td-1", "111")]


def test_payload_new_value_is_not_trusted(flask_client, monkeypatch):
    # Payload claims completed, but the API says the task is NOT completed.
    # The handler must follow the API (reopen), not the payload (complete).
    done = threading.Event()
    reopened = []
    completed = []

    monkeypatch.setattr(
        app_module, "asana_request",
        lambda method, endpoint, data=None: {"completed": False})
    monkeypatch.setattr(
        app_module, "complete_todo_task",
        lambda todo_task_id, asana_gid=None: completed.append(todo_task_id))

    def fake_reopen(todo_task_id, asana_gid=None):
        reopened.append(todo_task_id)
        done.set()

    monkeypatch.setattr(app_module, "reopen_todo_task", fake_reopen)
    monkeypatch.setattr(
        app_module, "load_sync_map",
        lambda: {"mappings": {"111": {"todo_task_id": "td-1"}}})

    resp = flask_client.post(
        "/webhook/asana", json={"events": [_event(new_value=True)]})
    assert resp.status_code == 200
    assert done.wait(timeout=5)
    assert reopened == ["td-1"]
    assert completed == []


def test_field_change_fetches_current_values(flask_client, monkeypatch):
    done = threading.Event()
    updates = []

    monkeypatch.setattr(
        app_module, "asana_request",
        lambda method, endpoint, data=None: {
            "name": "Renamed task", "notes": "fresh notes", "due_on": "2026-07-01"})

    def fake_update(todo_task_id, asana_gid=None, title=None, notes=None, due_date=None):
        updates.append({"title": title, "due_date": due_date})
        done.set()

    monkeypatch.setattr(app_module, "update_todo_task", fake_update)
    monkeypatch.setattr(
        app_module, "load_sync_map",
        lambda: {"mappings": {"111": {"todo_task_id": "td-1"}}})

    # new_value=None in the payload -- values must come from the API fetch
    resp = flask_client.post(
        "/webhook/asana", json={"events": [_event(field="name", new_value=None)]})
    assert resp.status_code == 200
    assert done.wait(timeout=5)
    assert updates[0]["title"] == "Renamed task"
    assert updates[0]["due_date"] == "2026-07-01"


def test_non_task_events_ignored(flask_client, monkeypatch):
    api_calls = []
    monkeypatch.setattr(
        app_module, "asana_request",
        lambda method, endpoint, data=None: api_calls.append(endpoint) or {})
    monkeypatch.setattr(app_module, "load_sync_map", lambda: {"mappings": {}})

    payload = {"events": [{
        "action": "changed",
        "resource": {"resource_type": "story", "gid": "999"},
        "change": {"field": "completed", "new_value": None},
    }]}
    resp = flask_client.post("/webhook/asana", json=payload)
    assert resp.status_code == 200
    # Give the background thread a moment, then confirm no API fetch happened
    import time
    time.sleep(0.3)
    assert api_calls == []
