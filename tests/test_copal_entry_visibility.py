"""Slice 04A — Copal per-entry visibility pref is per-user and isolated."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import prefs_routes


def test_entry_visibility_pref_is_per_user_and_isolated(tmp_path, monkeypatch):
    """Two users hold independent visibility maps in one prefs store."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(tmp_path / "prefs.json"))
    current = {"u": "e"}
    monkeypatch.setattr(prefs_routes, "get_current_user", lambda request=None: current["u"])
    app = FastAPI()
    app.include_router(prefs_routes.setup_prefs_routes())
    http = TestClient(app)

    # e hides Wiki, keeps Notes visible.
    current["u"] = "e"
    r = http.put("/api/prefs/copal_entry_visibility", json={"value": {"wiki": False, "notes": True}})
    assert r.status_code == 200
    assert http.get("/api/prefs/copal_entry_visibility").json()["value"]["wiki"] is False

    # sam starts with no pref (isolated from e's choice).
    current["u"] = "sam"
    assert http.get("/api/prefs/copal_entry_visibility").json()["value"] is None
    # sam makes a different choice.
    http.put("/api/prefs/copal_entry_visibility", json={"value": {"notes": False}})

    # e's pref is unchanged by sam's write — no cross-user leakage.
    current["u"] = "e"
    e_pref = http.get("/api/prefs/copal_entry_visibility").json()["value"]
    assert e_pref["wiki"] is False
    assert e_pref.get("notes") is True

    # sam's pref is independently persisted.
    current["u"] = "sam"
    assert http.get("/api/prefs/copal_entry_visibility").json()["value"]["notes"] is False
