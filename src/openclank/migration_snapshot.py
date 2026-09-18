"""Checksummed encrypted rollback archives for the provider hard cut."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.secret_storage import _get_fernet


ARCHIVE_MAGIC = b"OPENCLANK-PROVIDER-ROLLBACK-V1\n"
_TOP_LEVEL_FILES = (
    "app.db",
    "app.db-wal",
    "app.db-shm",
    "app.db-journal",
    "auth.json",
    "user_prefs.json",
    "settings.json",
    "embedding_endpoint.json",
)
_RUNTIME_NAMES = {"auth.json", "session-map.json", "config.json"}


class MigrationSnapshotError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotResult:
    archive: Path
    sha256: str
    file_count: int
    created_at: str


class MigrationLock(AbstractContextManager):
    """Cross-platform, process-exclusive provider migration lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handle = self.path.open("a+b")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                if self._handle.tell() == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._handle.close()
            self._handle = None
            raise MigrationSnapshotError("another provider migration owns the lock") from exc
        return self

    def __exit__(self, *_exc):
        if self._handle is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_provider_files(data_dir: Path) -> Iterable[Path]:
    root = data_dir / "runtime" / "agent-engine"
    if not root.is_dir():
        return ()
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _RUNTIME_NAMES or "projection" in path.name.lower():
            selected.append(path)
    return selected


def _copy_consistent_database(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source_db = sqlite3.connect(str(source), timeout=30)
    target_db = sqlite3.connect(str(target))
    try:
        source_db.execute("PRAGMA wal_checkpoint(FULL)")
        source_db.backup(target_db)
        target_db.commit()
    finally:
        target_db.close()
        source_db.close()


def create_rollback_archive(
    data_dir: Path,
    *,
    destination_dir: Path | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    """Create one encrypted mode-0600 archive without exposing credentials."""

    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        raise MigrationSnapshotError(f"data directory does not exist: {data_dir}")
    destination = Path(destination_dir or (data_dir / "backups")).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    final_path = destination / f"provider-cutover-{stamp}.ocbak"
    # A process can die after the archive is durably written but before the
    # phase journal records its name.  Preserve that valid recovery artifact
    # and choose a deterministic free sibling on retry.
    collision = 1
    while final_path.exists():
        final_path = destination / f"provider-cutover-{stamp}-{collision}.ocbak"
        collision += 1

    with tempfile.TemporaryDirectory(prefix="provider-snapshot-", dir=destination) as temporary_name:
        temporary = Path(temporary_name)
        staged = temporary / "payload"
        staged.mkdir()
        database = data_dir / "app.db"
        _copy_consistent_database(database, staged / "app.db")

        candidates: list[Path] = []
        for name in _TOP_LEVEL_FILES:
            source = data_dir / name
            if source.is_file() and name != "app.db":
                candidates.append(source)
        candidates.extend(_runtime_provider_files(data_dir))
        for source in candidates:
            relative = source.relative_to(data_dir)
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        files = sorted(path for path in staged.rglob("*") if path.is_file())
        manifest = {
            "schema_version": 1,
            "kind": "openclank-provider-cutover-rollback",
            "created_at": timestamp.isoformat(),
            "data_root": "data",
            "files": [
                {
                    "path": path.relative_to(staged).as_posix(),
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                    "mode": int(path.stat().st_mode & 0o777),
                }
                for path in files
            ],
        }
        manifest_path = staged / "rollback-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        tar_path = temporary / "rollback.tar"
        with tarfile.open(tar_path, "w") as archive:
            for path in sorted(staged.rglob("*")):
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(staged).as_posix(), recursive=False)
        encrypted = ARCHIVE_MAGIC + _get_fernet().encrypt(tar_path.read_bytes())
        temp_final = destination / f".{final_path.name}.{os.getpid()}.tmp"
        try:
            with temp_final.open("xb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_final, 0o600)
            os.replace(temp_final, final_path)
        finally:
            try:
                temp_final.unlink()
            except FileNotFoundError:
                pass
    return SnapshotResult(
        archive=final_path,
        sha256=_sha256(final_path),
        file_count=len(manifest["files"]),
        created_at=timestamp.isoformat(),
    )


def _read_archive(archive_path: Path) -> tuple[dict, dict[str, bytes]]:
    raw = Path(archive_path).read_bytes()
    if not raw.startswith(ARCHIVE_MAGIC):
        raise MigrationSnapshotError("rollback archive has an invalid header")
    try:
        plaintext = _get_fernet().decrypt(raw[len(ARCHIVE_MAGIC) :])
    except Exception as exc:
        raise MigrationSnapshotError("rollback archive authentication failed") from exc
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:") as archive:
            for member in archive.getmembers():
                path = Path(member.name)
                if not member.isfile() or path.is_absolute() or ".." in path.parts:
                    raise MigrationSnapshotError("rollback archive contains an unsafe member")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise MigrationSnapshotError("rollback archive member is unreadable")
                files[path.as_posix()] = extracted.read()
    except (tarfile.TarError, OSError) as exc:
        raise MigrationSnapshotError("rollback archive is corrupt") from exc
    try:
        manifest = json.loads(files.pop("rollback-manifest.json").decode("utf-8"))
    except (KeyError, UnicodeError, ValueError) as exc:
        raise MigrationSnapshotError("rollback archive manifest is missing or invalid") from exc
    expected = {item["path"]: item for item in manifest.get("files", [])}
    if set(expected) != set(files):
        raise MigrationSnapshotError("rollback archive file inventory does not match")
    for name, content in files.items():
        if hashlib.sha256(content).hexdigest() != expected[name].get("sha256"):
            raise MigrationSnapshotError("rollback archive checksum mismatch")
    return manifest, files


def verify_rollback_archive(archive_path: Path) -> dict:
    manifest, files = _read_archive(archive_path)
    return {
        "ok": True,
        "created_at": manifest.get("created_at"),
        "file_count": len(files),
        "sha256": _sha256(Path(archive_path)),
    }


def restore_rollback_archive(
    archive_path: Path,
    data_dir: Path,
    *,
    accept_post_cut_data_loss: bool,
) -> dict:
    """Restore the whole archive; there is intentionally no table rollback."""

    if not accept_post_cut_data_loss:
        raise MigrationSnapshotError(
            "full restore requires explicit acceptance of post-cut data loss"
        )
    data_dir = Path(data_dir).resolve()
    manifest, files = _read_archive(archive_path)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in files.items():
        relative = Path(name)
        target = (data_dir / relative).resolve()
        if not target.is_relative_to(data_dir):
            raise MigrationSnapshotError("rollback target escaped the data directory")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            mode = int(next(item["mode"] for item in manifest["files"] if item["path"] == name))
            os.chmod(temporary_name, mode & 0o700 or 0o600)
            os.replace(temporary_name, target)
        finally:
            try:
                Path(temporary_name).unlink()
            except FileNotFoundError:
                pass
    return {"restored": len(files), "created_at": manifest.get("created_at")}


__all__ = [
    "MigrationLock",
    "MigrationSnapshotError",
    "SnapshotResult",
    "create_rollback_archive",
    "restore_rollback_archive",
    "verify_rollback_archive",
]
