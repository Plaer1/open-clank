"""Small Files-owned image hierarchy used by Gallery/Imps compatibility paths."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError

from core.database import FilesImageResource, SessionLocal
from src.generated_images import gallery_image_root


class FilesImageError(RuntimeError):
    def __init__(self, message: str, *, code: str = "resource_unavailable"):
        super().__init__(message)
        self.code = code


def _owner(owner: str) -> str:
    value = str(owner or "").strip().lower()
    if not value:
        raise FilesImageError("owner is required", code="owner_required")
    return value


def _name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise FilesImageError("invalid Files image name", code="invalid_name")
    return value[:255]


def _collision_name(name: str, suffix: int) -> str:
    stem, extension = os.path.splitext(name)
    return f"{stem} ({suffix}){extension}"


def _bounded_provenance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise FilesImageError("image provenance is invalid", code="invalid_provenance") from exc
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise FilesImageError("image provenance is too large", code="provenance_too_large")
    return json.loads(encoded)


class FilesImageStore:
    """Owner-scoped hierarchy with immutable image IDs and atomic byte writes."""

    def __init__(self, session_factory=SessionLocal, *, blob_root: str | Path | None = None):
        self.session_factory = session_factory
        self.blob_root = Path(blob_root) if blob_root is not None else gallery_image_root()

    def _get(self, db, owner: str, resource_id: str) -> FilesImageResource:
        row = db.query(FilesImageResource).filter(
            FilesImageResource.id == str(resource_id),
            FilesImageResource.owner == owner,
            FilesImageResource.is_active.is_(True),
        ).one_or_none()
        if row is None:
            raise FilesImageError("Files image resource is unavailable")
        return row

    def ensure_gallery(self, owner: str) -> FilesImageResource:
        owner = _owner(owner)
        operation_key = f"builtin:{owner}:gallery"
        db = self.session_factory()
        try:
            row = db.query(FilesImageResource).filter(
                FilesImageResource.owner == owner,
                FilesImageResource.operation_key == operation_key,
                FilesImageResource.is_active.is_(True),
            ).one_or_none()
            if row is not None:
                return row
            row = db.query(FilesImageResource).filter(
                FilesImageResource.owner == owner,
                FilesImageResource.kind == "folder",
                FilesImageResource.parent_id.is_(None),
                FilesImageResource.display_name == "Gallery",
                FilesImageResource.is_active.is_(True),
            ).one_or_none()
            if row is None:
                row = FilesImageResource(
                    id=str(uuid.uuid4()), owner=owner, kind="folder",
                    display_name="Gallery", revision=1, operation_key=operation_key,
                    provenance={"builtin": True},
                )
                db.add(row)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    row = db.query(FilesImageResource).filter(
                        FilesImageResource.owner == owner,
                        FilesImageResource.operation_key == operation_key,
                        FilesImageResource.is_active.is_(True),
                    ).one()
            elif row.operation_key != operation_key:
                row.operation_key = operation_key
                db.commit()
            db.refresh(row)
            return row
        finally:
            db.close()

    def ensure_photos(self, owner: str) -> FilesImageResource:
        gallery = self.ensure_gallery(owner)
        return self.create_folder(
            owner, parent_id=gallery.id, name="Photos",
            operation_key=f"builtin:{_owner(owner)}:photos",
        )

    def create_folder(self, owner: str, *, parent_id: str | None, name: str, operation_key: str | None = None) -> FilesImageResource:
        owner, name = _owner(owner), _name(name)
        db = self.session_factory()
        try:
            if operation_key:
                prior = db.query(FilesImageResource).filter(
                    FilesImageResource.owner == owner,
                    FilesImageResource.operation_key == operation_key,
                    FilesImageResource.is_active.is_(True),
                ).one_or_none()
                if prior:
                    return prior
            if parent_id:
                parent = self._get(db, owner, parent_id)
                if parent.kind != "folder":
                    raise FilesImageError("parent is not a folder", code="invalid_parent")
            display = name
            suffix = 2
            while db.query(FilesImageResource.id).filter(
                FilesImageResource.owner == owner, FilesImageResource.parent_id == parent_id,
                FilesImageResource.display_name == display, FilesImageResource.is_active.is_(True),
            ).first():
                display, suffix = _collision_name(name, suffix), suffix + 1
            row = FilesImageResource(
                id=str(uuid.uuid4()), owner=owner, kind="folder", parent_id=parent_id,
                display_name=display, revision=1, operation_key=operation_key,
            )
            db.add(row)
            db.commit(); db.refresh(row)
            return row
        except IntegrityError as exc:
            db.rollback()
            if operation_key:
                winner = db.query(FilesImageResource).filter(
                    FilesImageResource.owner == owner,
                    FilesImageResource.operation_key == operation_key,
                    FilesImageResource.is_active.is_(True),
                ).one_or_none()
                if winner is not None:
                    return winner
            raise FilesImageError("folder operation already exists", code="idempotency_conflict") from exc
        finally:
            db.close()

    def import_image(self, owner: str, *, parent_id: str | None, name: str, data: bytes,
                     mime_type: str | None = None, operation_key: str | None = None,
                     provenance: Mapping[str, Any] | None = None,
                     source_provider: str | None = None, source_resource_id: str | None = None) -> FilesImageResource:
        owner, name = _owner(owner), _name(name)
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise FilesImageError("image bytes are required", code="invalid_content")
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        db = self.session_factory()
        path: Path | None = None
        final: Path | None = None
        try:
            if operation_key:
                prior = db.query(FilesImageResource).filter(
                    FilesImageResource.owner == owner,
                    FilesImageResource.operation_key == operation_key,
                    FilesImageResource.is_active.is_(True),
                ).one_or_none()
                if prior:
                    if prior.digest != digest:
                        raise FilesImageError("operation payload changed", code="idempotency_conflict")
                    return prior
            if parent_id:
                parent = self._get(db, owner, parent_id)
                if parent.kind != "folder":
                    raise FilesImageError("parent is not a folder", code="invalid_parent")
            display = name
            suffix = 2
            while db.query(FilesImageResource.id).filter(
                FilesImageResource.owner == owner, FilesImageResource.parent_id == parent_id,
                FilesImageResource.display_name == display, FilesImageResource.is_active.is_(True),
            ).first():
                display, suffix = _collision_name(name, suffix), suffix + 1
            rid = str(uuid.uuid4())
            self.blob_root.mkdir(parents=True, exist_ok=True)
            final = self.blob_root / f"files-image-{rid}{Path(name).suffix.lower()}"
            fd, temp = tempfile.mkstemp(prefix=".files-image-", dir=str(self.blob_root))
            os.close(fd)
            path = Path(temp)
            path.write_bytes(payload)
            os.replace(path, final)
            row = FilesImageResource(
                id=rid, owner=owner, kind="image", parent_id=parent_id,
                display_name=display, revision=1, digest=digest, size=len(payload),
                mime_type=mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream",
                locator=final.name, provenance=_bounded_provenance(provenance),
                operation_key=operation_key, source_provider=source_provider,
                source_resource_id=source_resource_id,
            )
            db.add(row); db.commit(); db.refresh(row)
            return row
        except IntegrityError as exc:
            db.rollback()
            if path and path.exists(): path.unlink(missing_ok=True)
            if final and final.exists(): final.unlink(missing_ok=True)
            if operation_key:
                prior = db.query(FilesImageResource).filter(
                    FilesImageResource.owner == owner,
                    FilesImageResource.operation_key == operation_key,
                    FilesImageResource.is_active.is_(True),
                ).one_or_none()
                if prior and prior.digest == digest:
                    return prior
            raise FilesImageError("image operation already exists", code="idempotency_conflict") from exc
        except Exception:
            db.rollback()
            if path and path.exists(): path.unlink(missing_ok=True)
            if final and final.exists(): final.unlink(missing_ok=True)
            raise
        finally:
            db.close()

    def move(self, owner: str, resource_id: str, *, parent_id: str | None, name: str, expected_revision: int) -> FilesImageResource:
        owner, name = _owner(owner), _name(name)
        db = self.session_factory()
        try:
            row = self._get(db, owner, resource_id)
            if row.revision != int(expected_revision):
                raise FilesImageError("resource revision is stale", code="resource_ref_stale")
            if parent_id == row.id:
                raise FilesImageError("resource cannot contain itself", code="cycle_detected")
            ancestor = parent_id
            while ancestor:
                if ancestor == row.id:
                    raise FilesImageError("folder move would create a cycle", code="cycle_detected")
                parent = self._get(db, owner, ancestor)
                if parent.kind != "folder":
                    raise FilesImageError("parent is not a folder", code="invalid_parent")
                ancestor = parent.parent_id
            sibling = db.query(FilesImageResource.id).filter(
                FilesImageResource.owner == owner, FilesImageResource.parent_id == parent_id,
                FilesImageResource.display_name == name, FilesImageResource.id != row.id,
                FilesImageResource.is_active.is_(True),
            ).first()
            if sibling:
                raise FilesImageError("destination name already exists", code="name_collision")
            row.parent_id, row.display_name, row.revision = parent_id, name, row.revision + 1
            db.commit(); db.refresh(row)
            return row
        finally:
            db.close()

    @staticmethod
    def ref(row: FilesImageResource) -> str:
        return f"image:{row.id}"
