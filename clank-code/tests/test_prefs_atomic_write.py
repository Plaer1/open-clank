import json

import routes.prefs_routes as prefs_routes


def test_save_replaces_prefs_file_atomically(monkeypatch, tmp_path):
    calls = []
    real_replace = prefs_routes.os.replace

    def fake_replace(src, dst):
        calls.append((src, dst))
        real_replace(src, dst)

    prefs_file = tmp_path / "data" / "user_prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))
    monkeypatch.setattr(prefs_routes.os, "replace", fake_replace)

    prefs_routes._save({"theme": "dark"})

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(prefs_file)
    assert src.startswith(str(prefs_file) + ".tmp.")
    assert json.loads(prefs_file.read_text(encoding="utf-8")) == {"theme": "dark"}
    assert not list(prefs_file.parent.glob("*.tmp.*"))


def test_save_for_user_preserves_scoped_user_prefs(monkeypatch, tmp_path):
    prefs_file = tmp_path / "data" / "user_prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    prefs_routes._save_for_user("alice", {"theme": "dark"})

    data = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert data == {"_users": {"alice": {"theme": "dark"}}}
    assert prefs_routes._load_for_user("alice") == {"theme": "dark"}


def test_save_for_user_preserves_flat_prefs_when_auth_disabled(monkeypatch, tmp_path):
    prefs_file = tmp_path / "data" / "user_prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    prefs_routes._save_for_user(None, {"theme": "dark"})

    data = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert data == {"theme": "dark"}
    assert prefs_routes._load_for_user(None) == {"theme": "dark"}


def test_memory_mode_backfill_freezes_existing_choices_and_is_idempotent(
    monkeypatch, tmp_path
):
    prefs_file = tmp_path / "data" / "user_prefs.json"
    prefs_file.parent.mkdir()
    prefs_file.write_text(
        json.dumps(
            {
                "_users": {
                    "alice": {"auto_memory": False},
                    "bob": {"theme": "dark"},
                    "carol": {"memory_mode": "MANUAL"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    assert prefs_routes.backfill_memory_modes(["alice", "bob", "carol"])
    assert not prefs_routes.backfill_memory_modes(["alice", "bob", "carol"])

    users = json.loads(prefs_file.read_text(encoding="utf-8"))["_users"]
    assert users["alice"]["memory_mode"] == "off"
    assert users["bob"]["memory_mode"] == "automatic"
    assert users["carol"]["memory_mode"] == "manual"


def test_new_account_without_backfill_uses_manual_default(monkeypatch, tmp_path):
    from src.memory_gate import memory_mode

    prefs_file = tmp_path / "data" / "user_prefs.json"
    prefs_file.parent.mkdir()
    prefs_file.write_text(
        json.dumps({"_users": {"existing": {"auto_memory": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    prefs_routes.backfill_memory_modes(["existing"])

    assert memory_mode(prefs_routes._load_for_user("existing")) == "automatic"
    assert memory_mode(prefs_routes._load_for_user("new-user")) == "manual"
