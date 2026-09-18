import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.prefs_routes as prefs_routes


def test_load_ignores_non_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps(["not", "a", "prefs", "object"]), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes._load() == {}
    assert prefs_routes._load_for_user("alice") == {}


def test_load_keeps_object_prefs_file(tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes._load_for_user("alice") == {"theme": "dark"}


def test_permission_mode_route_is_strict_normalized_and_owner_scoped(tmp_path, monkeypatch):
    current = {"user": "alice"}
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(prefs_routes, "get_current_user", lambda request=None: current["user"])
    app = FastAPI()
    app.include_router(prefs_routes.setup_prefs_routes())
    http = TestClient(app)

    assert http.get("/api/prefs/permission_mode").json()["value"] == "manual"
    assert http.put("/api/prefs/permission_mode", json={"value": "YOLO"}).json() == {
        "key": "permission_mode",
        "value": "yolo",
    }
    assert http.put("/api/prefs/permission_mode", json={"value": "not-a-mode"}).status_code == 422

    current["user"] = "bob"
    assert http.get("/api/prefs/permission_mode").json()["value"] == "manual"


def test_memory_trust_preferences_require_real_booleans(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "user_prefs.json"))
    monkeypatch.setattr(prefs_routes, "get_current_user", lambda request=None: "alice")
    app = FastAPI()
    app.include_router(prefs_routes.setup_prefs_routes())
    http = TestClient(app)

    assert http.put("/api/prefs/memory_trust_auto", json={"value": "false"}).status_code == 422
    assert http.put(
        "/api/prefs/memory_trust_auto_kinds",
        json={"value": {"fact": "true"}},
    ).status_code == 422
    assert http.put(
        "/api/prefs/memory_trust_auto_kinds",
        json={"value": {"fact": True}},
    ).json()["value"] == {"fact": True}
