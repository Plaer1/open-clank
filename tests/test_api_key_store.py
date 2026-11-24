"""Encrypted provider key persistence, permissions and failure recovery."""
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from src.api_key_manager import APIKeyManager


_WINDOWS = sys.platform.startswith("win")


@pytest.fixture
def mgr(tmp_path):
    return APIKeyManager(str(tmp_path))


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(_WINDOWS, reason="POSIX permission bits only")
def test_new_key_file_is_owner_only(mgr):
    mgr.get_or_create_key()
    assert _mode(mgr.key_file) == 0o600, f"expected 0o600, got {oct(_mode(mgr.key_file))}"


@pytest.mark.skipif(_WINDOWS, reason="POSIX permission bits only")
def test_existing_world_readable_key_is_relocked(mgr):
    # Simulate a key written by an older version with a permissive umask.
    with open(mgr.key_file, "wb") as f:
        f.write(b"x" * 44)
    os.chmod(mgr.key_file, 0o644)
    mgr.get_or_create_key()  # existing-file branch should re-lock it
    assert _mode(mgr.key_file) == 0o600, f"expected re-lock to 0o600, got {oct(_mode(mgr.key_file))}"


def test_encrypt_decrypt_roundtrip_still_works(mgr):
    # The permission hardening must not change functional behaviour.
    enc = mgr.encrypt_api_key("sk-secret")
    assert enc and enc != "sk-secret"
    assert mgr.decrypt_api_key(enc) == "sk-secret"


def test_save_creates_atomic_tmp_file(mgr):
    """Verify save() writes to a temp file and replaces atomically."""
    mgr.save("openai", "sk-test")

    # The final file should exist with the correct content
    assert os.path.exists(mgr.api_keys_file)
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    assert "openai" in keys

    # The temp file should NOT remain after successful save
    tmp_file = mgr.api_keys_file + ".tmp"
    assert not os.path.exists(tmp_file)


def test_save_preserves_existing_keys_atomically(mgr):
    """Verify atomic save doesn't corrupt other providers' keys."""
    mgr.save("openai", "sk-openai")
    mgr.save("anthropic", "sk-anthropic")

    loaded = mgr.load()
    assert loaded["openai"] == "sk-openai"
    assert loaded["anthropic"] == "sk-anthropic"


def test_save_preserves_original_on_write_failure(mgr):
    """An I/O failure must leave the original keys file intact."""
    mgr.save("openai", "sk-original")

    # Simulate a storage I/O failure during save.
    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            mgr.save("anthropic", "sk-new")

    # Original file must still be intact with the original key
    loaded = mgr.load()
    assert loaded == {"openai": "sk-original"}
    assert "anthropic" not in loaded


def test_save_cleans_up_tmp_on_failure(mgr):
    """Temp file should be removed if the write fails."""
    mgr.save("openai", "sk-original")

    tmp_file = mgr.api_keys_file + ".tmp"

    # Force a failure after the temp file is opened
    original_open = open

    def failing_open(*args, **kwargs):
        f = original_open(*args, **kwargs)
        if args and isinstance(args[0], str) and args[0].endswith(".tmp"):
            # Close the file then raise
            f.close()
            raise OSError("simulated write failure")
        return f

    with patch("builtins.open", side_effect=failing_open):
        with pytest.raises(OSError):
            mgr.save("anthropic", "sk-new")

    # Temp file should be cleaned up
    assert not os.path.exists(tmp_file)

    # Original should be intact
    loaded = mgr.load()
    assert loaded == {"openai": "sk-original"}


def test_corrupt_json_returns_empty(mgr):
    Path(mgr.api_keys_file).write_text("{not valid json", encoding="utf-8")
    assert mgr.load() == {}


def test_list_shape_returns_empty(mgr):
    Path(mgr.api_keys_file).write_text('["openai", "anthropic"]', encoding="utf-8")
    assert mgr.load() == {}


def test_missing_file_returns_empty(mgr):
    assert mgr.load() == {}


def test_valid_roundtrip(mgr):
    mgr.save("openai", "sk-secret")
    assert mgr.load() == {"openai": "sk-secret"}


def test_api_key_manager_load_resilience(mgr):

    # Save a valid key
    mgr.save("good_provider", "good_value")

    # A token encrypted under a different key must be skipped.
    other_key = Fernet.generate_key()
    other_f = Fernet(other_key)
    undecryptable_token = other_f.encrypt(b"bad_value").decode()

    # Manually edit api_keys.json to include the undecryptable token
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)

    keys["bad_provider"] = undecryptable_token
    # Also add a malformed/garbage token (causes ValueError/binascii.Error)
    keys["garbage_provider"] = "not-a-valid-base64-fernet-token"

    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)

    # Load keys
    loaded = mgr.load()

    # Assert load() returns the still-decryptable key and skips the bad ones without raising
    assert "good_provider" in loaded
    assert loaded["good_provider"] == "good_value"
    assert "bad_provider" not in loaded
    assert "garbage_provider" not in loaded


def test_load_ignores_non_string_raw_values(mgr):

    mgr.save("openai", "sk-openai")
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)

    keys["missing_provider"] = None
    keys["numeric_provider"] = 42
    keys["object_provider"] = {"encrypted": keys["openai"]}
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)

    assert mgr.load() == {"openai": "sk-openai"}
