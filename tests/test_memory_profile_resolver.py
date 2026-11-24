"""resolve_memory_store_profile (G-DEF decision 1): pure/temp-dir behaviour, no binaries.

Covers every row of the behaviour table: env value x (no file / v16 / v17 / v18 / 0-byte / non-sqlite /
other version / stray companion) x (Linux / non-Linux, via monkeypatched sys.platform). The resolver must
never write to an EXISTING file (compared byte-for-byte and by mtime before/after), and an unknown env value
is always refused.
"""
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from src import memery_database as fd


def _db(version=None, *, empty=False, garbage=False):
    path = os.path.join(tempfile.mkdtemp(prefix="fd-resolver-"), "fm.db")
    if garbage:
        Path(path).write_bytes(b"not a sqlite file, just bytes" * 4)
        return path
    if empty:
        Path(path).touch()
        return path
    if version is not None:
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE fm_meta(key TEXT, value TEXT)")
            db.execute("INSERT INTO fm_meta VALUES ('database_id','did-1')")
            db.execute(f"PRAGMA user_version={version}")
            db.commit()
    return path


def _snapshot(path):
    try:
        return Path(path).read_bytes(), os.stat(path).st_mtime_ns
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _clean_env():
    old = os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    yield
    os.environ.pop("OPENCLANK_MEMORY_STORE_PROFILE", None)
    if old is not None:
        os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = old


def _no_prepare():
    """Patch out the fresh-store builder so these pure tests never touch binaries."""
    return patch.object(fd, "_prepare_fresh_guarded_v18", side_effect=AssertionError("must not prepare"))


def _linux(value=True):
    return patch.object(fd.sys, "platform", "linux" if value else "darwin")


# ---- env unset, Linux ------------------------------------------------------------------------------------

def test_unset_linux_no_file_prepares_fresh_guarded(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(prefix="fd-resolver-"), "fm.db")
    called = {}
    def fake_prepare(p, **kw):
        called["path"] = p
        Path(p).write_bytes(b"prepared")
    with _linux(), patch.object(fd, "_prepare_fresh_guarded_v18", side_effect=fake_prepare):
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v18" and called["path"] == path
    assert os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] == "guarded-v18"


def test_unset_linux_v16_file_stays_legacy_untouched():
    path = _db(16)
    before = _snapshot(path)
    with _linux(), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "legacy-v16"
    assert _snapshot(path) == before


def test_unset_linux_v17_file_detected_untouched():
    path = _db(17)
    before = _snapshot(path)
    with _linux(), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v17"
    assert _snapshot(path) == before


def test_unset_linux_v18_file_detected_untouched():
    path = _db(18)
    before = _snapshot(path)
    with _linux(), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v18"
    assert _snapshot(path) == before


def test_unset_linux_zero_byte_file_refused_untouched():
    path = _db(empty=True)
    before = _snapshot(path)
    with _linux(), _no_prepare(), pytest.raises(RuntimeError):
        fd.resolve_memory_store_profile(path)
    assert _snapshot(path) == before


def test_unset_linux_non_sqlite_file_refused_untouched():
    path = _db(garbage=True)
    before = _snapshot(path)
    with _linux(), _no_prepare(), pytest.raises(RuntimeError):
        fd.resolve_memory_store_profile(path)
    assert _snapshot(path) == before


def test_unset_linux_other_version_refused_untouched():
    path = _db(19)
    before = _snapshot(path)
    with _linux(), _no_prepare(), pytest.raises(RuntimeError):
        fd.resolve_memory_store_profile(path)
    assert _snapshot(path) == before


def test_unset_linux_stray_companion_without_main_file_refused():
    path = os.path.join(tempfile.mkdtemp(prefix="fd-resolver-"), "fm.db")
    Path(path + "-wal").touch()
    with _linux(), _no_prepare(), pytest.raises(RuntimeError):
        fd.resolve_memory_store_profile(path)
    assert not os.path.exists(path)  # never prepared over a stray companion


# ---- env unset, non-Linux ---------------------------------------------------------------------------------

def test_unset_non_linux_no_file_stays_legacy_no_prepare():
    path = os.path.join(tempfile.mkdtemp(prefix="fd-resolver-"), "fm.db")
    with _linux(False), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "legacy-v16" and not os.path.exists(path)


def test_unset_non_linux_v16_file_stays_legacy_untouched():
    path = _db(16)
    before = _snapshot(path)
    with _linux(False), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "legacy-v16"
    assert _snapshot(path) == before


# ---- env set -----------------------------------------------------------------------------------------------

def test_env_legacy_wins_even_with_a_guarded_file_present():
    path = _db(18)
    before = _snapshot(path)
    os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = "legacy-v16"
    with _linux(), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "legacy-v16"
    assert _snapshot(path) == before


def test_env_guarded_with_existing_file_returned_unchanged_no_prepare():
    path = _db(18)
    before = _snapshot(path)
    os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = "guarded-v18"
    with _linux(), _no_prepare():
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v18"
    assert _snapshot(path) == before


def test_env_guarded_with_absent_file_prepares_fresh():
    path = os.path.join(tempfile.mkdtemp(prefix="fd-resolver-"), "fm.db")
    called = {}
    def fake_prepare(p, **kw):
        called["path"] = p
        Path(p).write_bytes(b"prepared")
    os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = "guarded-v17"
    with _linux(), patch.object(fd, "_prepare_fresh_guarded_v18", side_effect=fake_prepare):
        profile = fd.resolve_memory_store_profile(path)
    assert profile == "guarded-v18" and called["path"] == path


def test_env_unknown_value_refused():
    os.environ["OPENCLANK_MEMORY_STORE_PROFILE"] = "nonsense-profile"
    with pytest.raises(RuntimeError, match="unknown memory store profile"):
        fd.resolve_memory_store_profile(os.path.join(tempfile.mkdtemp(prefix="fd-resolver-"), "fm.db"))
