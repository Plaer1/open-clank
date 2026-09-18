"""Manifest-bound migration of local Clanker corpora.

The migration is intentionally file-granular.  A plan records the source
content, mode, and relative names before any mutation.  Apply preflights every
entry, moves files with same-filesystem ``os.replace`` where possible, and
persists progress after each entry.  Rollback verifies the recorded destination
bytes before restoring them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable

from core.atomic_io import atomic_write_json


SCHEMA = "open-clank-layout-migration/v1"
DEFAULT_MANIFEST = ".clanker/layout-migration.json"
_HASH_FIELDS = ("relative_path", "destination_relative_path", "size", "mode", "sha256")


class LayoutMigrationError(RuntimeError):
    """A migration precondition, collision, or recovery check failed."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        raise LayoutMigrationError(f"source corpus is not a directory: {root}")
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in names:
            candidate = directory_path / name
            if candidate.is_symlink():
                raise LayoutMigrationError(f"symlinks are not supported in migration corpora: {candidate}")
        for name in files:
            candidate = directory_path / name
            if candidate.is_symlink() or not candidate.is_file():
                raise LayoutMigrationError(f"source entry is not a regular file: {candidate}")
            yield candidate


def _entry(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    info = path.stat()
    return {
        "relative_path": relative,
        "size": int(info.st_size),
        "mode": int(stat.S_IMODE(info.st_mode)),
        "sha256": _sha256_file(path),
    }


def corpus_manifest(root: str | os.PathLike[str], *, exclude_root_ds_store: bool = False) -> dict[str, Any]:
    """Return a deterministic manifest for a regular-file corpus."""
    base = Path(root).expanduser().resolve()
    all_entries = sorted((_entry(base, path) for path in _regular_files(base)), key=lambda item: item["relative_path"])
    excluded = [item for item in all_entries if exclude_root_ds_store and item["relative_path"] == ".DS_Store"]
    entries = [item for item in all_entries if item not in excluded]
    tree_sha256 = hashlib.sha256(_canonical_json(entries)).hexdigest()
    return {
        "root": str(base),
        "entries": entries,
        "tree_sha256": tree_sha256,
        "file_count": len(entries),
        "excluded_entries": excluded,
        "total_bytes": sum(int(item["size"]) for item in entries),
    }


def _plan_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema": manifest["schema"],
        "source_root": manifest["source_root"],
        "destination_root": manifest["destination_root"],
        "source_manifest": manifest["source_manifest"],
        "entries": [{key: item[key] for key in _HASH_FIELDS} for item in manifest["entries"]],
    }
    if "destination_before" in manifest:
        payload["destination_before"] = manifest.get("destination_before")
    return payload


def _plan_hash(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(_plan_payload(manifest))).hexdigest()


def _resolve_manifest_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()


def _assert_not_nested(source: Path, destination: Path) -> None:
    if source == destination:
        raise LayoutMigrationError("source and destination must differ")
    for child, parent, label in ((source, destination, "source inside destination"), (destination, source, "destination inside source")):
        try:
            child.relative_to(parent)
        except ValueError:
            continue
        raise LayoutMigrationError(label)


def _assert_manifest_paths(manifest_path: Path, source: Path, destination: Path) -> None:
    for root, label in ((source, "source"), (destination, "destination")):
        try:
            manifest_path.relative_to(root)
        except ValueError:
            continue
        raise LayoutMigrationError(f"manifest must not live inside the {label} corpus")


def _assert_safe_root(root: Path, label: str) -> None:
    if root in {Path("/").resolve(), Path.home().resolve()}:
        raise LayoutMigrationError(f"refusing broad {label} root: {root}")


def create_manifest(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create and atomically persist a migration plan without moving files."""
    source_root = Path(source).expanduser().resolve()
    destination_root = Path(destination).expanduser().resolve()
    manifest_file = _resolve_manifest_path(manifest_path)
    _assert_not_nested(source_root, destination_root)
    _assert_safe_root(source_root, "source")
    _assert_safe_root(destination_root, "destination")
    _assert_manifest_paths(manifest_file, source_root, destination_root)
    source_snapshot = corpus_manifest(source_root, exclude_root_ds_store=source_root.name == ".robonotes")
    destination_snapshot = corpus_manifest(destination_root) if destination_root.is_dir() else None
    entries = [
        {
            **item,
            "destination_relative_path": item["relative_path"],
            "state": "pending",
            "action": None,
            "collision": "unique",
        }
        for item in source_snapshot["entries"]
    ]
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "source_root": str(source_root),
        "destination_root": str(destination_root),
        "source_manifest": {
            "tree_sha256": source_snapshot["tree_sha256"],
            "file_count": source_snapshot["file_count"],
            "entries": source_snapshot["entries"],
            "excluded_entries": source_snapshot["excluded_entries"],
            "total_bytes": source_snapshot["total_bytes"],
        },
        "destination_before": (
            {
                "tree_sha256": destination_snapshot["tree_sha256"],
                "file_count": destination_snapshot["file_count"],
                "entries": destination_snapshot["entries"],
            }
            if destination_snapshot is not None
            else None
        ),
        "entries": entries,
        "total_bytes": source_snapshot["total_bytes"],
        "plan_hash": "",
        "status": "planned",
        "events": [],
    }
    manifest["plan_hash"] = _plan_hash(manifest)
    _write_manifest(manifest_file, manifest)
    return manifest


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(str(path), manifest, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_manifest(path: str | os.PathLike[str]) -> tuple[Path, dict[str, Any]]:
    manifest_path = _resolve_manifest_path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LayoutMigrationError(f"could not read migration manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise LayoutMigrationError("unsupported or malformed migration manifest")
    if not isinstance(manifest.get("entries"), list) or not isinstance(manifest.get("source_manifest"), dict):
        raise LayoutMigrationError("migration manifest is missing its source snapshot")
    if str(manifest.get("plan_hash") or "") != _plan_hash(manifest):
        raise LayoutMigrationError("migration manifest plan hash does not match its immutable inputs")
    return manifest_path, manifest


def _entry_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise LayoutMigrationError(f"manifest entry escapes its root: {relative}") from exc
    return path


def _matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and int(info.st_size) == int(expected["size"])
        and int(stat.S_IMODE(info.st_mode)) == int(expected["mode"])
        and _sha256_file(path) == str(expected["sha256"])
    )


def _verify_source_snapshot(manifest: dict[str, Any], *, pending_only: bool) -> None:
    source = Path(manifest["source_root"])
    destination = Path(manifest["destination_root"])
    for item in manifest["entries"]:
        if pending_only and item.get("state") != "pending":
            continue
        if item.get("state") == "rolled_back":
            continue
        source_path = _entry_path(source, item["relative_path"])
        destination_path = _entry_path(destination, item["destination_relative_path"])
        if item.get("state") == "pending" and not _matches(source_path, item):
            # A crash can occur after os.replace and before the journal update.
            # Treat an exact destination as an interrupted move and recover it.
            if _matches(destination_path, item):
                item["state"] = "moved"
                item["action"] = "moved"
                item["collision"] = "unique"
                continue
            raise LayoutMigrationError(f"source changed or disappeared: {source_path}")


def _preflight_collisions(manifest: dict[str, Any]) -> None:
    source = Path(manifest["source_root"])
    destination = Path(manifest["destination_root"])
    for item in manifest["entries"]:
        if item.get("state") != "pending":
            continue
        source_path = _entry_path(source, item["relative_path"])
        destination_path = _entry_path(destination, item["destination_relative_path"])
        if not _matches(source_path, item):
            if _matches(destination_path, item):
                item["state"] = "moved"
                item["action"] = "moved"
                item["collision"] = "unique"
                continue
            raise LayoutMigrationError(f"source changed before apply: {source_path}")
        if destination_path.is_symlink():
            raise LayoutMigrationError(f"destination symlink collision: {destination_path}")
        if destination_path.exists():
            if not _matches(destination_path, item):
                raise LayoutMigrationError(f"divergent destination collision: {destination_path}")
            item["collision"] = "identical"
            item["action"] = "deduplicated"
        else:
            item["collision"] = "unique"
            item["action"] = "moved"


def _remove_empty_source_dirs(source: Path, entries: list[dict[str, Any]]) -> None:
    directories: set[Path] = set()
    for item in entries:
        directory = _entry_path(source, item["relative_path"]).parent
        while directory != source and source in directory.parents:
            directories.add(directory)
            directory = directory.parent
    directories = sorted(directories, key=lambda path: len(path.parts), reverse=True)
    for directory in directories:
        if directory == source:
            continue
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_manifest(path: str | os.PathLike[str], *, dry_run: bool = False) -> dict[str, Any]:
    """Apply a planned migration, or return its preflight without mutation."""
    manifest_path, manifest = load_manifest(path)
    if manifest.get("status") == "rolled_back":
        raise LayoutMigrationError("a rolled-back migration manifest cannot be applied")
    if manifest.get("status") in {"applied", "finalized"}:
        verify_manifest(path)
        return {"status": "already_migrated", "plan_hash": manifest["plan_hash"], "file_count": len(manifest["entries"])}
    if dry_run:
        _preflight_collisions(manifest)
        return {
            "status": "dry_run",
            "plan_hash": manifest["plan_hash"],
            "file_count": len(manifest["entries"]),
            "actions": {str(item["action"]): sum(1 for row in manifest["entries"] if row.get("action") == item["action"]) for item in manifest["entries"]},
        }
    manifest["status"] = "applying"
    manifest.setdefault("events", []).append({"kind": "apply_started"})
    _write_manifest(manifest_path, manifest)
    try:
        _preflight_collisions(manifest)
        _write_manifest(manifest_path, manifest)
        source = Path(manifest["source_root"])
        destination = Path(manifest["destination_root"])
        destination.mkdir(parents=True, exist_ok=True)
        for item in manifest["entries"]:
            if item.get("state") != "pending":
                continue
            source_path = _entry_path(source, item["relative_path"])
            destination_path = _entry_path(destination, item["destination_relative_path"])
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if item.get("action") == "deduplicated":
                if not _matches(source_path, item) or not _matches(destination_path, item):
                    raise LayoutMigrationError(f"file changed during deduplication: {source_path}")
                item["state"] = "deduplicated"
            else:
                os.replace(source_path, destination_path)
                if not _matches(destination_path, item):
                    raise LayoutMigrationError(f"destination verification failed: {destination_path}")
                item["state"] = "moved"
            _write_manifest(manifest_path, manifest)
        verify_manifest(manifest_path)
        manifest["status"] = "applied"
        manifest.setdefault("events", []).append({"kind": "apply_completed"})
        _write_manifest(manifest_path, manifest)
        return {"status": "applied", "plan_hash": manifest["plan_hash"], "file_count": len(manifest["entries"])}
    except Exception as exc:
        manifest["status"] = "partial"
        manifest.setdefault("events", []).append({"kind": "apply_failed", "error": str(exc)[:500]})
        _write_manifest(manifest_path, manifest)
        if isinstance(exc, LayoutMigrationError):
            raise
        raise LayoutMigrationError(str(exc)) from exc


def _copy_back_atomic(source: Path, destination: Path, mode: int) -> None:
    source.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{source.name}.rollback.", dir=str(source.parent))
    try:
        with os.fdopen(fd, "wb") as handle, destination.open("rb") as original:
            shutil.copyfileobj(original, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, source)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def rollback_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Restore every applied entry after verifying destination integrity."""
    manifest_path, manifest = load_manifest(path)
    if manifest.get("status") not in {"applied", "finalized", "partial", "applying"}:
        raise LayoutMigrationError("migration is not applied or recoverably partial")
    source = Path(manifest["source_root"])
    destination = Path(manifest["destination_root"])
    for item in reversed(manifest["entries"]):
        state = item.get("state")
        if state in {"pending", "rolled_back"}:
            continue
        destination_path = _entry_path(destination, item["destination_relative_path"])
        source_path = _entry_path(source, item["relative_path"])
        if not _matches(destination_path, item):
            raise LayoutMigrationError(f"destination changed; refusing rollback: {destination_path}")
        if item.get("action") == "moved":
            if source_path.exists():
                raise LayoutMigrationError(f"rollback source already exists: {source_path}")
            source_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination_path, source_path)
        else:
            if source_path.exists() and not _matches(source_path, item):
                raise LayoutMigrationError(f"rollback source collision: {source_path}")
            if not source_path.exists():
                _copy_back_atomic(source_path, destination_path, int(item["mode"]))
        item["state"] = "rolled_back"
        _write_manifest(manifest_path, manifest)
    _remove_empty_source_dirs(source, manifest["entries"])
    manifest["status"] = "rolled_back"
    manifest.setdefault("events", []).append({"kind": "rollback_completed"})
    _write_manifest(manifest_path, manifest)
    return {"status": "rolled_back", "plan_hash": manifest["plan_hash"], "file_count": len(manifest["entries"])}


def verify_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Verify every planned destination entry without changing anything."""
    _manifest_path, manifest = load_manifest(path)
    destination = Path(manifest["destination_root"])
    expected = {
        str(item["relative_path"]): item
        for item in ((manifest.get("destination_before") or {}).get("entries") or [])
    }
    expected.update({str(item["destination_relative_path"]): item for item in manifest["entries"]})
    for relative, item in expected.items():
        destination_path = _entry_path(destination, relative)
        if not _matches(destination_path, item):
            raise LayoutMigrationError(f"destination verification failed: {destination_path}")
    actual = {path.relative_to(destination).as_posix() for path in _regular_files(destination)} if destination.is_dir() else set()
    extras = sorted(actual - set(expected))
    if extras:
        raise LayoutMigrationError(f"destination contains unplanned files: {extras[0]}")
    return {"status": "verified", "plan_hash": manifest["plan_hash"], "file_count": len(expected)}


def finalize_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Remove only verified identical legacy duplicates after apply/verify."""
    manifest_path, manifest = load_manifest(path)
    if manifest.get("status") == "finalized":
        return {"status": "already_finalized", "plan_hash": manifest["plan_hash"], "file_count": len(manifest["entries"])}
    if manifest.get("status") != "applied":
        raise LayoutMigrationError("finalize requires an applied migration")
    verify_manifest(path)
    source = Path(manifest["source_root"])
    for item in manifest["entries"]:
        if item.get("action") != "deduplicated" or item.get("state") == "finalized":
            continue
        source_path = _entry_path(source, item["relative_path"])
        if not _matches(source_path, item):
            raise LayoutMigrationError(f"duplicate source changed; refusing finalize: {source_path}")
        source_path.unlink()
        item["state"] = "finalized"
        _write_manifest(manifest_path, manifest)
    _remove_empty_source_dirs(source, manifest["entries"])
    manifest["status"] = "finalized"
    manifest.setdefault("events", []).append({"kind": "finalize_completed"})
    _write_manifest(manifest_path, manifest)
    return {"status": "finalized", "plan_hash": manifest["plan_hash"], "file_count": len(manifest["entries"])}


def inspect_roots(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> dict[str, Any]:
    """Inspect named roots without creating or changing any files."""
    source_root = Path(source).expanduser().resolve()
    destination_root = Path(destination).expanduser().resolve()
    _assert_not_nested(source_root, destination_root)
    _assert_safe_root(source_root, "source")
    _assert_safe_root(destination_root, "destination")
    source_info = corpus_manifest(source_root, exclude_root_ds_store=source_root.name == ".robonotes")
    destination_info = corpus_manifest(destination_root) if destination_root.is_dir() else {"file_count": 0, "entries": [], "tree_sha256": None, "total_bytes": 0}
    destination_by_path = {item["relative_path"]: item for item in destination_info["entries"]}
    collisions = {"unique": 0, "identical": 0, "divergent": 0}
    for item in source_info["entries"]:
        other = destination_by_path.get(item["relative_path"])
        state = "unique" if other is None else "identical" if other == item else "divergent"
        collisions[state] += 1
    return {"source": source_info, "destination": destination_info, "collisions": collisions}


__all__ = [
    "DEFAULT_MANIFEST",
    "LayoutMigrationError",
    "SCHEMA",
    "apply_manifest",
    "corpus_manifest",
    "create_manifest",
    "finalize_manifest",
    "inspect_roots",
    "load_manifest",
    "rollback_manifest",
    "verify_manifest",
]
