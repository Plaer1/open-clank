"""Owner-scoped, content-addressed spool for managed model operations."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from core.database import SessionLocal
from core.operation_models import ArtifactRecord, operation_utcnow


DEFAULT_ARTIFACT_TTL = timedelta(hours=24)
DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


class ArtifactError(RuntimeError):
    pass


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    pass


def _owner(value: str) -> str:
    clean = str(value or "").strip().lower()
    if not clean or "\x00" in clean:
        raise ArtifactError("artifact owner is required")
    return clean


def _utc(value: datetime | None = None) -> datetime:
    result = value or operation_utcnow()
    if result.tzinfo is not None:
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        session_factory: Callable[..., Any] = SessionLocal,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        clock: Callable[[], datetime] = operation_utcnow,
    ):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._session_factory = session_factory
        self.max_bytes = max(1, int(max_bytes))
        self._clock = clock

    @contextmanager
    def _transaction(self):
        db = self._session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _owner_root(self, owner: str) -> Path:
        target = self._owner_path(owner)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        return target

    def _owner_path(self, owner: str) -> Path:
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        target = (self.root / digest).resolve()
        if not target.is_relative_to(self.root):
            raise ArtifactError("artifact owner path escaped the spool")
        return target

    def _path(self, relative: str) -> Path:
        if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ArtifactIntegrityError("artifact record contains an unsafe path")
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise ArtifactIntegrityError("artifact path escaped the spool")
        return target

    def put(
        self,
        *,
        owner: str,
        chunks: Iterable[bytes],
        media_type: str,
        ttl: timedelta = DEFAULT_ARTIFACT_TTL,
    ) -> ArtifactRecord:
        owner = _owner(owner)
        media_type = str(media_type or "application/octet-stream").strip()
        if not media_type or len(media_type) > 255 or "\x00" in media_type:
            raise ArtifactError("invalid artifact media type")
        owner_root = self._owner_root(owner)
        digest = hashlib.sha256()
        size = 0
        fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=owner_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ArtifactError("artifact chunks must be bytes")
                    value = bytes(chunk)
                    size += len(value)
                    if size > self.max_bytes:
                        raise ArtifactError("artifact exceeds the configured size limit")
                    digest.update(value)
                    handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            sha256 = digest.hexdigest()
            destination = owner_root / sha256[:2] / sha256
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            relative = destination.relative_to(self.root).as_posix()
            now = _utc(self._clock())
            expires_at = now + ttl
            with self._transaction() as db:
                existing = (
                    db.query(ArtifactRecord)
                    .filter(
                        ArtifactRecord.owner == owner,
                        ArtifactRecord.content_sha256 == sha256,
                    )
                    .first()
                )
                if existing is not None:
                    if destination.is_file() and destination.stat().st_size == size:
                        existing.state = "staged"
                        existing.expires_at = expires_at
                        existing.media_type = media_type
                        db.flush()
                        db.refresh(existing)
                        db.expunge(existing)
                        return existing
                    existing.relative_path = relative
                    existing.size_bytes = size
                    existing.media_type = media_type
                    existing.state = "staged"
                    existing.expires_at = expires_at
                    existing.acknowledged_at = None
                    row = existing
                else:
                    row = ArtifactRecord(
                        id="art_" + secrets.token_urlsafe(24),
                        owner=owner,
                        content_sha256=sha256,
                        relative_path=relative,
                        size_bytes=size,
                        media_type=media_type,
                        state="staged",
                        created_at=now,
                        expires_at=expires_at,
                    )
                    db.add(row)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(temporary, destination)
                os.chmod(destination, 0o600)
                db.flush()
                db.refresh(row)
                db.expunge(row)
                return row
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def get(self, *, owner: str, artifact_id: str) -> ArtifactRecord:
        owner = _owner(owner)
        with self._transaction() as db:
            row = (
                db.query(ArtifactRecord)
                .filter(
                    ArtifactRecord.id == str(artifact_id),
                    ArtifactRecord.owner == owner,
                    ArtifactRecord.state != "deleted",
                )
                .first()
            )
            if row is None:
                raise ArtifactNotFound("artifact was not found")
            db.expunge(row)
            return row

    def read_chunks(
        self,
        *,
        owner: str,
        artifact_id: str,
        chunk_size: int = 1024 * 1024,
    ) -> Iterator[bytes]:
        row = self.get(owner=owner, artifact_id=artifact_id)
        path = self._path(row.relative_path)
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(max(1, min(int(chunk_size), 8 * 1024 * 1024)))
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    yield chunk
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("artifact payload is missing") from exc
        if total != row.size_bytes or digest.hexdigest() != row.content_sha256:
            raise ArtifactIntegrityError("artifact payload failed its content hash")

    def acknowledge(self, *, owner: str, artifact_id: str) -> ArtifactRecord:
        owner = _owner(owner)
        with self._transaction() as db:
            row = (
                db.query(ArtifactRecord)
                .filter(
                    ArtifactRecord.id == str(artifact_id),
                    ArtifactRecord.owner == owner,
                    ArtifactRecord.state != "deleted",
                )
                .first()
            )
            if row is None:
                raise ArtifactNotFound("artifact was not found")
            row.state = "acknowledged"
            row.acknowledged_at = _utc(self._clock())
            row.expires_at = None
            db.flush()
            db.refresh(row)
            db.expunge(row)
            return row

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        timestamp = _utc(now or self._clock())
        removed = 0
        with self._transaction() as db:
            rows = (
                db.query(ArtifactRecord)
                .filter(
                    ArtifactRecord.state != "acknowledged",
                    ArtifactRecord.state != "deleted",
                    ArtifactRecord.expires_at.is_not(None),
                    ArtifactRecord.expires_at <= timestamp,
                )
                .all()
            )
            for row in rows:
                path = self._path(row.relative_path)
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                row.state = "deleted"
                removed += 1
        return removed

    def rename_owner(self, old_owner: str, new_owner: str) -> int:
        """Move one owner's records and hashed spool partition together.

        The filesystem rename is compensated if the database transaction does
        not commit.  Existing destination rows or bytes fail closed rather
        than merging two account identities.
        """

        old_owner = _owner(old_owner)
        new_owner = _owner(new_owner)
        if old_owner == new_owner:
            return 0
        old_root = self._owner_path(old_owner)
        new_root = self._owner_path(new_owner)
        moved = False
        try:
            with self._transaction() as db:
                if (
                    db.query(ArtifactRecord.id)
                    .filter(ArtifactRecord.owner == new_owner)
                    .first()
                    is not None
                    or new_root.exists()
                ):
                    raise ArtifactError(
                        "target artifact owner already has durable state"
                    )
                rows = db.query(ArtifactRecord).filter(
                    ArtifactRecord.owner == old_owner,
                ).all()
                old_prefix = old_root.name + "/"
                new_prefix = new_root.name + "/"
                for row in rows:
                    if not str(row.relative_path or "").startswith(old_prefix):
                        raise ArtifactIntegrityError(
                            "artifact record is outside its owner partition"
                        )
                    if row.state != "deleted" and not self._path(row.relative_path).is_file():
                        raise ArtifactIntegrityError("artifact payload is missing")
                if old_root.exists():
                    old_root.replace(new_root)
                    moved = True
                for row in rows:
                    row.owner = new_owner
                    row.relative_path = new_prefix + row.relative_path[len(old_prefix):]
                return len(rows)
        except Exception:
            if moved and new_root.exists() and not old_root.exists():
                new_root.replace(old_root)
            raise

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        owner = _owner(owner)
        owner_root = self._owner_path(owner)
        with self._transaction() as db:
            rows = db.query(ArtifactRecord).filter(
                ArtifactRecord.owner == owner
            ).order_by(ArtifactRecord.id).all()
            material = [
                [
                    row.id,
                    row.content_sha256,
                    row.relative_path,
                    int(row.size_bytes),
                    row.state,
                ]
                for row in rows
            ]
        return {
            "count": len(material),
            "bytes": sum(item[3] for item in material),
            "root_present": owner_root.exists(),
            "fingerprint": hashlib.sha256(
                json.dumps(material, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    def purge_owner(self, owner: str) -> int:
        """Remove one exact owner's records and payload partition recoverably."""

        owner = _owner(owner)
        owner_root = self._owner_path(owner)
        tombstone = (self.root / f".{owner_root.name}.purging").resolve()
        if not tombstone.is_relative_to(self.root):  # pragma: no cover - hash invariant
            raise ArtifactError("artifact purge tombstone escaped the spool")
        staged = False
        deleted = 0
        try:
            with self._transaction() as db:
                rows = db.query(ArtifactRecord).filter(
                    ArtifactRecord.owner == owner,
                ).all()
                # Recover the only crash window: bytes were staged but the DB
                # transaction did not commit.  When no rows remain, the prior
                # commit succeeded and only physical cleanup is pending.
                if tombstone.exists() and owner_root.exists():
                    raise ArtifactError("artifact purge partitions conflict")
                if tombstone.exists() and rows:
                    tombstone.replace(owner_root)
                if owner_root.exists():
                    owner_root.replace(tombstone)
                    staged = True
                deleted = int(
                    db.query(ArtifactRecord)
                    .filter(ArtifactRecord.owner == owner)
                    .delete(synchronize_session=False)
                )
        except Exception:
            if staged and tombstone.exists() and not owner_root.exists():
                tombstone.replace(owner_root)
            raise
        if tombstone.exists():
            try:
                shutil.rmtree(tombstone)
            except OSError as exc:
                raise ArtifactError(
                    "artifact records were purged but payload cleanup is pending"
                ) from exc
        return deleted


__all__ = [
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactNotFound",
    "ArtifactStore",
    "DEFAULT_ARTIFACT_TTL",
]
