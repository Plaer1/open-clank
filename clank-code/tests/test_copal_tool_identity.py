import json

from src.tool_execution import _copal_account_id


def _write_auth(path, users):
    path.write_text(json.dumps({"users": users}), encoding="utf-8")


def test_tool_identity_tracks_rename_and_rejects_deleted_recreated_username(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    _write_auth(auth, {"alice": {"account_id": "acct-original"}, "bob": {"account_id": "acct-bob"}})
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_AUTH_PATH", str(auth))
    assert _copal_account_id("alice")[1] == "acct-original"

    _write_auth(auth, {"allie": {"account_id": "acct-original"}, "bob": {"account_id": "acct-bob"}})
    assert _copal_account_id("allie")[1] == "acct-original"
    assert _copal_account_id("alice")[1] is None

    _write_auth(auth, {"alice": {"account_id": "acct-recreated"}, "bob": {"account_id": "acct-bob"}})
    assert _copal_account_id("alice")[1] == "acct-recreated"


def test_tool_identity_fails_closed_when_auth_is_enabled_but_store_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_AUTH_PATH", str(tmp_path / "missing.json"))
    assert _copal_account_id("alice")[1] is None
    assert _copal_account_id("alice")[2]
