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
    assert prefs_routes.os.path.dirname(src) == str(prefs_file.parent)
    assert json.loads(prefs_file.read_text(encoding="utf-8")) == {"theme": "dark"}
    assert list(prefs_file.parent.iterdir()) == [prefs_file]


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


def test_existing_explicit_and_legacy_settings_survive_the_new_default(monkeypatch, tmp_path):
    from src.memory_gate import memory_mode

    prefs_file = tmp_path / "user_prefs.json"
    users = {
        "legacy-off": {"auto_memory": False},
        "legacy-on": {"auto_memory": True},
        "review": {"memory_mode": "MANUAL"},
        "off": {"memory_mode": "off", "auto_memory": True},
    }
    prefs_file.write_text(json.dumps({"_users": users}), encoding="utf-8")
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    for owner, mode in (("legacy-off", "off"), ("legacy-on", "automatic"),
                        ("review", "manual"), ("off", "off"), ("new", "automatic")):
        assert memory_mode(prefs_routes._load_for_user(owner)) == mode
    assert json.loads(prefs_file.read_text())["_users"] == users
