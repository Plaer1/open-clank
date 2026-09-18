import importlib.util
import json
import os
from pathlib import Path


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("odysseus_setup_under_test", Path("setup.py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_create_default_admin_normalizes_env_username(tmp_path, monkeypatch):
    setup_module = _load_setup_module()
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setenv("ODYSSEUS_ADMIN_USER", " AdminUser ")
    monkeypatch.setenv("ODYSSEUS_ADMIN_PASSWORD", "temporary-password")

    assert setup_module.create_default_admin() == "created"

    auth_path = tmp_path / "auth.json"
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "adminuser" in data["users"]
    assert "AdminUser" not in data["users"]


def test_main_loads_admin_password_from_env_file(tmp_path, monkeypatch):
    """Regression: setup.py must honor an admin password pre-seeded in .env on
    native installs, even when the var is not exported into the shell
    (docs/setup.md documents this). Previously setup.py never called
    load_dotenv(), so os.getenv() saw nothing and a random password was
    generated instead."""
    import bcrypt

    setup_module = _load_setup_module()

    # Credentials live ONLY in a .env beside setup.py (written with a UTF-8 BOM,
    # the Notepad-on-Windows case that utf-8-sig must tolerate) — not exported.
    monkeypatch.delenv("ODYSSEUS_ADMIN_USER", raising=False)
    monkeypatch.delenv("ODYSSEUS_ADMIN_PASSWORD", raising=False)
    (tmp_path / ".env").write_text(
        "ODYSSEUS_ADMIN_USER=presetuser\nODYSSEUS_ADMIN_PASSWORD=fromenvfile12345\n",
        encoding="utf-8-sig",
    )

    # Point setup at the temp dir and neutralize main()'s heavy steps.
    monkeypatch.setattr(setup_module, "BASE_DIR", str(tmp_path))
    auth_path = tmp_path / "auth.json"
    monkeypatch.setattr(setup_module, "AUTH_FILE", str(auth_path))
    monkeypatch.setattr(setup_module, "check_arch", lambda: None)
    monkeypatch.setattr(setup_module, "ensure_managed_engine", lambda: None)
    monkeypatch.setattr(setup_module, "ensure_provider_cutover", lambda: None)
    monkeypatch.setattr(setup_module, "create_dirs", lambda: None)
    monkeypatch.setattr(setup_module, "create_env", lambda: None)
    monkeypatch.setattr(setup_module, "check_deps", lambda: None)
    monkeypatch.setattr(setup_module, "init_database", lambda: None)
    monkeypatch.setattr(setup_module, "install_public_command", lambda: None)
    # Force the non-interactive branch so the test never blocks on a prompt.
    monkeypatch.setenv("ODYSSEUS_SKIP_ADMIN_PROMPT", "1")

    try:
        setup_module.main()
    finally:
        # load_dotenv writes real os.environ entries; undo so sibling tests
        # don't inherit them.
        os.environ.pop("ODYSSEUS_ADMIN_USER", None)
        os.environ.pop("ODYSSEUS_ADMIN_PASSWORD", None)

    data = json.loads(auth_path.read_text(encoding="utf-8"))
    assert "presetuser" in data["users"], data
    assert bcrypt.checkpw(
        b"fromenvfile12345", data["users"]["presetuser"]["password_hash"].encode()
    ), "admin password from .env was ignored; a random one was generated"


def test_main_completes_provider_cutover_before_database_import(monkeypatch):
    setup_module = _load_setup_module()
    calls = []

    monkeypatch.setattr(setup_module, "check_arch", lambda: None)
    monkeypatch.setattr(setup_module, "ensure_managed_engine", lambda: calls.append("engine"))
    monkeypatch.setattr(setup_module, "create_dirs", lambda: calls.append("dirs"))
    monkeypatch.setattr(setup_module, "create_env", lambda: None)
    monkeypatch.setattr(setup_module, "check_deps", lambda: None)
    monkeypatch.setattr(
        setup_module,
        "ensure_provider_cutover",
        lambda: calls.append("provider-cutover"),
    )
    monkeypatch.setattr(setup_module, "init_database", lambda: calls.append("database"))
    monkeypatch.setattr(setup_module, "create_default_admin", lambda: "exists")
    monkeypatch.setattr(
        setup_module,
        "install_public_command",
        lambda: calls.append("public-command"),
    )

    setup_module.main()

    assert calls == ["engine", "dirs", "provider-cutover", "database", "public-command"]


def test_main_does_not_initialize_database_after_cutover_failure(monkeypatch):
    setup_module = _load_setup_module()
    database_called = False

    monkeypatch.setattr(setup_module, "check_arch", lambda: None)
    monkeypatch.setattr(setup_module, "ensure_managed_engine", lambda: None)
    monkeypatch.setattr(setup_module, "create_dirs", lambda: None)
    monkeypatch.setattr(setup_module, "create_env", lambda: None)
    monkeypatch.setattr(setup_module, "check_deps", lambda: None)

    def fail_cutover():
        raise RuntimeError("cutover blocked")

    def database():
        nonlocal database_called
        database_called = True

    monkeypatch.setattr(setup_module, "ensure_provider_cutover", fail_cutover)
    monkeypatch.setattr(setup_module, "init_database", database)
    monkeypatch.setattr(setup_module, "install_public_command", lambda: None)

    import pytest

    with pytest.raises(RuntimeError, match="cutover blocked"):
        setup_module.main()
    assert database_called is False


def test_source_setup_installs_only_the_openclank_public_command(tmp_path):
    setup_module = _load_setup_module()
    target = setup_module.install_public_command(tmp_path)

    assert target == tmp_path / ("openclank.cmd" if os.name == "nt" else "openclank")
    text = target.read_text(encoding="utf-8")
    assert str((Path(setup_module.BASE_DIR) / "scripts" / "openclank").resolve()) in text
    assert not (tmp_path / "mimo").exists()
    assert not (tmp_path / "odysseus").exists()
    if os.name != "nt":
        assert target.stat().st_mode & 0o111
