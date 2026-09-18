from __future__ import annotations

import json

import pytest

from src.openclank.client_profiles import (
    ClientCredentialVault,
    ClientProfile,
    ProfileError,
    ProfileStore,
    normalize_server_url,
)


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("http://127.0.0.1:7777/", "http://127.0.0.1:7777"),
        ("http://[::1]:7777", "http://[::1]:7777"),
        ("https://clank.example.test/base/", "https://clank.example.test/base"),
    ],
)
def test_profile_url_normalization(url, normalized):
    assert normalize_server_url(url)[0] == normalized


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.2:7777",
        "http://clank.example.test",
        "https://user:secret@clank.example.test",
        "https://clank.example.test/?token=secret",
    ],
)
def test_remote_profiles_are_tls_only_and_credential_free(url):
    with pytest.raises(ProfileError):
        normalize_server_url(url)


def test_profile_store_never_contains_tokens(tmp_path):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    store.put(ClientProfile.create("local", "http://localhost:7777", auto_start=True))
    store.put(ClientProfile.create("remote", "https://clank.example.test"), make_active=True)

    assert store.get().name == "remote"
    payload = json.loads(path.read_text())
    assert payload["active"] == "remote"
    assert "token" not in path.read_text().lower()

    assert store.remove("remote") is True
    assert store.get().name == "local"


def test_remote_profile_cannot_auto_start():
    with pytest.raises(ProfileError, match="loopback"):
        ClientProfile.create("remote", "https://clank.example.test", auto_start=True)


def test_vault_fallback_is_memory_only(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    vault = ClientCredentialVault()
    assert vault.persistent is False
    vault.set("local", "oct_example")
    assert vault.get("local") == "oct_example"
    vault.delete("local")
    assert vault.get("local") is None


def test_vault_rejects_non_tui_tokens(monkeypatch):
    monkeypatch.setitem(__import__("sys").modules, "keyring", None)
    vault = ClientCredentialVault()
    with pytest.raises(ProfileError, match="non-TUI"):
        vault.set("local", "ody_not_for_tui")
