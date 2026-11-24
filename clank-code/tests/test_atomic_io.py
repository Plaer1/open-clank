"""Tests for ``core.atomic_io`` durability and crash-safety behavior.

``core.atomic_io`` provides ``atomic_write_json`` and ``atomic_write_text``.
Both write to a sibling ``.tmp.<pid>`` file, ``fsync`` it, then ``os.replace``
into place so a crash mid-write leaves the previous good copy untouched rather
than a truncated/empty file.

These tests cover the happy path (round-trip, indent, parent-dir creation,
full overwrite, no leftover tmp) and the two failure paths the implementation
guarantees: the target file is preserved when serialization fails before the
replace, and when ``os.replace`` itself fails.
"""
import importlib.util
import json
from pathlib import Path

import pytest

# Load core/atomic_io.py directly by file path so this stays a pure unit test:
# importing the ``core`` package would pull in core/__init__.py and the
# database/session modules, making the test depend on data/app.db existing.
ROOT = Path(__file__).resolve().parents[1]
ATOMIC_IO_PATH = ROOT / "core" / "atomic_io.py"
_spec = importlib.util.spec_from_file_location("_atomic_io_under_test", ATOMIC_IO_PATH)
atomic_io = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(atomic_io)

atomic_write_json = atomic_io.atomic_write_json
atomic_write_text = atomic_io.atomic_write_text
atomic_write_bytes = atomic_io.atomic_write_bytes
atomic_write_batch = atomic_io.atomic_write_batch
AtomicFileChange = atomic_io.AtomicFileChange
AtomicRollbackError = atomic_io.AtomicRollbackError
AtomicWriteConflict = atomic_io.AtomicWriteConflict
file_fingerprint = atomic_io.file_fingerprint


def _tmp_siblings(directory: Path, name: str) -> list:
    """Return any ``<name>.tmp.*`` files the helpers may have left behind."""
    return [
        *directory.glob(f"{name}.tmp.*"),
        *directory.glob(f".{name}.tmp.*"),
        *directory.glob(f".{name}.bak.*"),
    ]


# ---------------------------------------------------------------------------
# atomic_write_json — happy path.
# ---------------------------------------------------------------------------
def test_atomic_write_json_round_trips_object(tmp_path):
    target = tmp_path / "data.json"
    original = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}, "s": "héllo"}

    atomic_write_json(str(target), original)

    assert json.loads(target.read_text(encoding="utf-8")) == original


def test_atomic_write_json_honors_indent(tmp_path):
    target = tmp_path / "indented.json"

    atomic_write_json(str(target), {"a": 1}, indent=2)

    text = target.read_text(encoding="utf-8")
    assert "\n" in text
    assert text == json.dumps({"a": 1}, indent=2)


def test_atomic_write_json_creates_missing_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "data.json"

    atomic_write_json(str(target), {"ok": True})

    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


def test_atomic_write_json_fully_overwrites_longer_content(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_json(str(target), {"k": "x" * 500})

    atomic_write_json(str(target), {"k": "short"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"k": "short"}
    # No trailing bytes from the previous, longer write.
    assert target.read_text(encoding="utf-8") == json.dumps({"k": "short"})


def test_atomic_write_json_leaves_no_tmp_file(tmp_path):
    target = tmp_path / "data.json"

    atomic_write_json(str(target), {"a": 1})

    assert _tmp_siblings(tmp_path, "data.json") == []


# ---------------------------------------------------------------------------
# atomic_write_json — failure path: target preserved on serialization error.
# ---------------------------------------------------------------------------
def test_atomic_write_json_preserves_target_when_serialization_fails(tmp_path):
    target = tmp_path / "data.json"
    atomic_write_json(str(target), {"existing": "value"})
    before = target.read_text(encoding="utf-8")

    # A set is not JSON-serializable, so json.dump raises after the tmp file
    # is opened but before os.replace runs.
    with pytest.raises(TypeError):
        atomic_write_json(str(target), {"bad": {1, 2, 3}})

    assert target.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# atomic_write_text — happy path.
# ---------------------------------------------------------------------------
def test_atomic_write_text_round_trips(tmp_path):
    target = tmp_path / "note.txt"
    text = "line one\nline two\nunicode: héllo\n"

    atomic_write_text(str(target), text)

    assert target.read_text(encoding="utf-8") == text


def test_atomic_write_text_creates_missing_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "note.txt"

    atomic_write_text(str(target), "content")

    assert target.exists()
    assert target.read_text(encoding="utf-8") == "content"


def test_atomic_write_text_fully_overwrites_longer_content(tmp_path):
    target = tmp_path / "note.txt"
    atomic_write_text(str(target), "x" * 500)

    atomic_write_text(str(target), "short")

    assert target.read_text(encoding="utf-8") == "short"


def test_atomic_write_text_leaves_no_tmp_file(tmp_path):
    target = tmp_path / "note.txt"

    atomic_write_text(str(target), "content")

    assert _tmp_siblings(tmp_path, "note.txt") == []


def test_atomic_write_text_rejects_non_string_before_tmp_file(tmp_path):
    target = tmp_path / "note.txt"

    with pytest.raises(TypeError):
        atomic_write_text(str(target), 123)

    assert not target.exists()
    assert _tmp_siblings(tmp_path, "note.txt") == []


# ---------------------------------------------------------------------------
# atomic_write_text — failure path: target preserved when replace fails.
# ---------------------------------------------------------------------------
def test_atomic_write_text_preserves_target_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    atomic_write_text(str(target), "original content")
    before = target.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(atomic_io.os, "replace", boom)

    with pytest.raises(OSError):
        atomic_write_text(str(target), "new content that never lands")

    assert target.read_text(encoding="utf-8") == before
    assert _tmp_siblings(tmp_path, "note.txt") == []


def test_atomic_write_bytes_rejects_stale_fingerprint_and_preserves_mode(tmp_path):
    target = tmp_path / "script.sh"
    target.write_bytes(b"old\n")
    target.chmod(0o751)
    observed = file_fingerprint(str(target))
    target.write_bytes(b"external\n")

    with pytest.raises(AtomicWriteConflict):
        atomic_write_bytes(
            str(target),
            b"ours\n",
            expected_fingerprint=observed,
        )

    assert target.read_bytes() == b"external\n"
    fresh = file_fingerprint(str(target))
    atomic_write_bytes(str(target), b"ours\n", expected_fingerprint=fresh)
    assert target.read_bytes() == b"ours\n"
    assert target.stat().st_mode & 0o777 == 0o751


def test_atomic_write_batch_rolls_back_when_later_replace_fails(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = atomic_io.os.replace
    installs = 0

    def fail_second_install(src, dst):
        nonlocal installs
        if ".tmp." in str(src):
            installs += 1
            if installs == 2:
                raise OSError("second install failed")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", fail_second_install)
    with pytest.raises(OSError, match="second install failed"):
        atomic_write_batch([
            AtomicFileChange(
                str(first),
                b"first-new",
                expected_fingerprint=file_fingerprint(str(first)),
            ),
            AtomicFileChange(
                str(second),
                b"second-new",
                expected_fingerprint=file_fingerprint(str(second)),
            ),
        ])

    assert first.read_text(encoding="utf-8") == "first-old"
    assert second.read_text(encoding="utf-8") == "second-old"
    assert _tmp_siblings(tmp_path, "first.txt") == []
    assert _tmp_siblings(tmp_path, "second.txt") == []


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_atomic_write_batch_rolls_back_every_commit_position(
    tmp_path,
    monkeypatch,
    fail_at,
):
    targets = [tmp_path / f"file-{index}.txt" for index in range(3)]
    for index, target in enumerate(targets):
        target.write_text(f"old-{index}", encoding="utf-8")
    real_replace = atomic_io.os.replace
    installs = 0

    def fail_selected_install(src, dst):
        nonlocal installs
        if ".tmp." in str(src):
            installs += 1
            if installs == fail_at:
                raise OSError(f"install {fail_at} failed")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", fail_selected_install)
    with pytest.raises(OSError, match=f"install {fail_at} failed"):
        atomic_write_batch([
            AtomicFileChange(
                str(target),
                f"new-{index}".encode(),
                expected_fingerprint=file_fingerprint(str(target)),
            )
            for index, target in enumerate(targets)
        ])

    for index, target in enumerate(targets):
        assert target.read_text(encoding="utf-8") == f"old-{index}"
        assert _tmp_siblings(tmp_path, target.name) == []


def test_atomic_batch_retains_backup_when_rollback_itself_fails(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = atomic_io.os.replace

    def fail_commit_then_rollback(src, dst):
        if ".tmp." in str(src) and dst == str(second):
            raise OSError("commit failed")
        if ".bak." in str(src) and dst == str(first):
            raise OSError("rollback failed")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", fail_commit_then_rollback)
    with pytest.raises(AtomicRollbackError) as raised:
        atomic_write_batch([
            AtomicFileChange(
                str(first),
                b"first-new",
                expected_fingerprint=file_fingerprint(str(first)),
            ),
            AtomicFileChange(
                str(second),
                b"second-new",
                expected_fingerprint=file_fingerprint(str(second)),
            ),
        ])

    assert second.read_text(encoding="utf-8") == "second-old"
    assert len(raised.value.backups) == 1
    retained = Path(raised.value.backups[0])
    assert retained.exists()
    assert retained.read_text(encoding="utf-8") == "first-old"


def test_atomic_batch_rejects_two_paths_to_same_canonical_target(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_text("old", encoding="utf-8")
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="duplicate target"):
        atomic_write_batch([
            AtomicFileChange(str(target), b"one"),
            AtomicFileChange(str(alias), b"two"),
        ])


def test_atomic_write_updates_symlink_target_without_replacing_link(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_text("old", encoding="utf-8")
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    atomic_write_bytes(
        str(alias),
        b"new",
        expected_fingerprint=file_fingerprint(str(alias)),
    )

    assert alias.is_symlink()
    assert target.read_bytes() == b"new"


def test_atomic_write_rejects_parent_symlink_swap(tmp_path, monkeypatch):
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(inside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    requested = alias / "new.txt"
    real_assert = atomic_io._assert_canonical_target
    checks = 0

    def swap_after_first_check(path, expected):
        nonlocal checks
        real_assert(path, expected)
        checks += 1
        if checks == 1:
            alias.unlink()
            alias.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(atomic_io, "_assert_canonical_target", swap_after_first_check)
    with pytest.raises(AtomicWriteConflict, match="canonical target changed"):
        atomic_write_bytes(str(requested), b"blocked", require_missing=True)

    assert not (inside / "new.txt").exists()
    assert not (outside / "new.txt").exists()


def test_atomic_create_never_overwrites_external_race(tmp_path, monkeypatch):
    target = tmp_path / "new.txt"
    real_link = atomic_io.os.link

    def external_creator_wins(src, dst):
        Path(dst).write_bytes(b"external")
        return real_link(src, dst)

    monkeypatch.setattr(atomic_io.os, "link", external_creator_wins)
    with pytest.raises(AtomicWriteConflict, match="created by another writer"):
        atomic_write_bytes(str(target), b"ours", require_missing=True)

    assert target.read_bytes() == b"external"
    assert _tmp_siblings(tmp_path, target.name) == []
