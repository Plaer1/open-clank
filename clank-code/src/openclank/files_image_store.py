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
from src.generated_images import (
    discard_staged_gallery_image,
    gallery_image_root,
    publish_staged_gallery_image,
    resolve_gallery_image_path,
    stage_gallery_image_bytes,
)


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
                     source_provider: str | None = None, source_resource_id: str | None = None, collision: str = "rename") -> FilesImageResource:
        owner, name = _owner(owner), _name(name)
        if collision not in {"rename", "fail"}:
            raise FilesImageError("invalid image collision policy", code="unsupported_operation")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise FilesImageError("image bytes are required", code="invalid_content")
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        db = self.session_factory()
        path: Path | None = None
        final: Path | None = None
        try:
            # Hold the SQLite write transaction across name selection and
            # allocation, including imports from another app process.
            if db.bind.dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
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
                if collision == "fail":
                    raise FilesImageError("image name already exists", code="resource_changed")
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

    def owns_locator(self, owner: str, locator: str) -> bool:
        """Return whether an active Files image locator belongs to ``owner``.

        Delivery uses this narrow lookup before resolving a filename on disk.
        A locator remains private implementation data: callers receive no row
        and cannot use this check to enumerate another owner's resources.
        """
        owner = _owner(owner)
        value = str(locator or "").strip()
        if not value or Path(value).name != value:
            return False
        db = self.session_factory()
        try:
            return db.query(FilesImageResource.id).filter(
                FilesImageResource.owner == owner,
                FilesImageResource.kind == "image",
                FilesImageResource.locator == value,
                FilesImageResource.is_active.is_(True),
            ).first() is not None
        finally:
            db.close()

    def image(self, owner: str, resource_id: str) -> FilesImageResource:
        """Return one active, owner-scoped image resource."""
        owner = _owner(owner)
        db = self.session_factory()
        try:
            row = self._get(db, owner, str(resource_id).removeprefix("image:"))
            if row.kind != "image":
                raise FilesImageError("Files image resource is unavailable")
            db.expunge(row)
            return row
        finally:
            db.close()

    def find_by_digest(self, owner: str, digest: str) -> FilesImageResource | None:
        """Find an active image by content digest without crossing owners."""
        owner = _owner(owner)
        value = str(digest or "").strip().lower()
        if not value:
            return None
        db = self.session_factory()
        try:
            row = db.query(FilesImageResource).filter(
                FilesImageResource.owner == owner,
                FilesImageResource.kind == "image",
                FilesImageResource.digest == value,
                FilesImageResource.is_active.is_(True),
            ).first()
            if row is not None:
                db.expunge(row)
            return row
        finally:
            db.close()

    def update_provenance(self, owner: str, resource_id: str, **values: Any) -> FilesImageResource:
        """Merge bounded caller metadata onto an owner-scoped image resource."""
        owner = _owner(owner)
        db = self.session_factory()
        try:
            row = self._get(db, owner, str(resource_id).removeprefix("image:"))
            if row.kind != "image":
                raise FilesImageError("Files image resource is unavailable")
            row.provenance = _bounded_provenance({**dict(row.provenance or {}), **values})
            row.revision += 1
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_images(self, owner: str, *, parent_id: str | None = None,
                    session_id: str | None = None) -> tuple[FilesImageResource, ...]:
        """List active images for one owner, optionally by folder or session."""
        owner = _owner(owner)
        db = self.session_factory()
        try:
            query = db.query(FilesImageResource).filter(
                FilesImageResource.owner == owner,
                FilesImageResource.kind == "image",
                FilesImageResource.is_active.is_(True),
            )
            if parent_id is not None:
                query = query.filter(FilesImageResource.parent_id == parent_id)
            rows = tuple(query.order_by(FilesImageResource.id).all())
            if session_id is not None:
                expected_session = str(session_id)
                rows = tuple(
                    row for row in rows
                    if expected_session in {
                        str((row.provenance or {}).get("session_id") or ""),
                        *(
                            str(value)
                            for value in (row.provenance or {}).get("session_ids", [])
                            if isinstance((row.provenance or {}).get("session_ids", []), list)
                        ),
                    }
                )
            for row in rows:
                db.expunge(row)
            return rows
        finally:
            db.close()

    def claim_reference(self, owner: str, resource_id: str, scope: str) -> None:
        """Serialize durable reference admission with image retirement."""
        from sqlalchemy import text
        db = self.session_factory()
        try:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = self._get(db, _owner(owner), str(resource_id).removeprefix("image:"))
            provenance = dict(row.provenance or {})
            claims = set(provenance.get("reference_claims") or [])
            claims.add(str(scope))
            provenance["reference_claims"] = sorted(claims)
            row.provenance = _bounded_provenance(provenance)
            row.revision += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def settle_reference_claim(self, resource_id: str, scope: str, *, committed: bool, settled_scope: str = "domain:core") -> None:
        from sqlalchemy import text
        db = self.session_factory()
        try:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = db.query(FilesImageResource).filter(FilesImageResource.id == resource_id).one_or_none()
            if row is None:
                return
            provenance = dict(row.provenance or {})
            claims = set(provenance.get("reference_claims") or [])
            if scope not in claims:
                return
            claims.discard(scope)
            if committed:
                claims.add(settled_scope)
            provenance["reference_claims"] = sorted(claims)
            row.provenance = _bounded_provenance(provenance)
            row.revision += 1
            db.commit()
        finally:
            db.close()

    def bind_session(self, owner: str, resource_id: str, session_id: str | None) -> FilesImageResource:
        """Record one verified chat provenance without replacing other chats."""
        owner = _owner(owner)
        value = str(session_id or "").strip()
        if not value:
            return self.image(owner, resource_id)
        db = self.session_factory()
        try:
            from sqlalchemy import text
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = self._get(db, owner, str(resource_id).removeprefix("image:"))
            if row.kind != "image":
                raise FilesImageError("Files image resource is unavailable")
            provenance = dict(row.provenance or {})
            session_ids = {
                str(item).strip()
                for item in provenance.get("session_ids", [])
                if str(item).strip()
            }
            legacy_session = str(provenance.get("session_id") or "").strip()
            if legacy_session:
                session_ids.add(legacy_session)
            if value in session_ids and provenance.get("session_ids") == sorted(session_ids):
                db.expunge(row)
                return row
            session_ids.add(value)
            provenance["session_ids"] = sorted(session_ids)
            provenance.pop("session_id", None)
            row.provenance = _bounded_provenance(provenance)
            row.revision += 1
            db.commit(); db.refresh(row); db.expunge(row)
            return row
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def detach_session(self, owner: str, resource_id: str, session_id: str) -> bool:
        """Remove one chat provenance and retire only an unshared image."""
        owner, session_id = _owner(owner), str(session_id or "").strip()
        if not session_id:
            return False
        db = self.session_factory()
        try:
            from sqlalchemy import text
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = self._get(db, owner, str(resource_id).removeprefix("image:"))
            if row.kind != "image":
                raise FilesImageError("Files image resource is unavailable")
            provenance = dict(row.provenance or {})
            session_ids = {
                str(item).strip()
                for item in provenance.get("session_ids", [])
                if str(item).strip()
            }
            legacy_session = str(provenance.get("session_id") or "").strip()
            if legacy_session:
                session_ids.add(legacy_session)
            if session_id not in session_ids:
                return False
            session_ids.remove(session_id)
            if session_ids or provenance.get("reference_claims"):
                provenance["session_ids"] = sorted(session_ids)
                provenance.pop("session_id", None)
                row.provenance = _bounded_provenance(provenance)
                row.revision += 1
                db.commit()
                return False
            locator = str(row.locator or "")
            quarantine = None
            shared = bool(locator) and db.query(FilesImageResource.id).filter(
                FilesImageResource.locator == locator,
                FilesImageResource.id != row.id,
                FilesImageResource.is_active.is_(True),
            ).first()
            if locator and not shared:
                source = resolve_gallery_image_path(locator, root=self.blob_root, require_exists=True)
                quarantine = self.blob_root / f".retiring-files-image-{uuid.uuid4().hex}"
                os.replace(source, quarantine)
            row.is_active = False
            row.revision += 1
            try:
                db.commit()
            except Exception:
                db.rollback()
                if quarantine is not None and quarantine.exists():
                    os.replace(quarantine, self.blob_root / locator)
                raise
            if quarantine is not None:
                try:
                    quarantine.unlink()
                except OSError:
                    # The durable state now correctly retires the resource.
                    # Keep the inaccessible quarantine byte for later GC.
                    pass
            return True
        finally:
            db.close()

    def replace_bytes(self, owner: str, resource_id: str, content: bytes, *,
                      expected_revision: int | None = None) -> FilesImageResource:
        """Atomically replace one Files image's bytes and metadata.

        The row and locator remain stable, which is required by an Imps project
        binding. A database failure restores the prior bytes before returning.
        """
        owner = _owner(owner)
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise FilesImageError("image bytes are required", code="invalid_content")
        db = self.session_factory()
        staged = None
        previous: bytes | None = None
        locator = ""
        published = False
        try:
            row = self._get(db, owner, str(resource_id).removeprefix("image:"))
            if row.kind != "image" or not row.locator:
                raise FilesImageError("Files image resource is unavailable")
            if expected_revision is not None and row.revision != int(expected_revision):
                raise FilesImageError("resource revision is stale", code="resource_ref_stale")
            locator = str(row.locator)
            path = resolve_gallery_image_path(locator, root=self.blob_root, require_exists=True)
            previous = path.read_bytes()
            staged = stage_gallery_image_bytes(bytes(content), root=self.blob_root)
            publish_staged_gallery_image(staged, locator, root=self.blob_root, replace=True)
            published = True
            row.digest = hashlib.sha256(content).hexdigest()
            row.size = len(content)
            row.revision += 1
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row
        except Exception as exc:
            db.rollback()
            if published and previous is not None:
                restore = None
                try:
                    restore = stage_gallery_image_bytes(previous, root=self.blob_root)
                    publish_staged_gallery_image(restore, locator, root=self.blob_root, replace=True)
                except Exception as restore_exc:
                    raise FilesImageError(
                        "Files image update partially applied and byte restoration failed",
                        code="partial_mutation",
                    ) from restore_exc
                finally:
                    discard_staged_gallery_image(restore)
            raise
        finally:
            discard_staged_gallery_image(staged)
            db.close()

    def retire(self, owner: str, resource_id: str, *, only_unreferenced: bool = False, session_id: str | None = None, expected_revision: int | None = None) -> bool:
        """Deactivate an owner-scoped image with compensating byte staging."""
        owner = _owner(owner)
        db = self.session_factory()
        locator = ""
        quarantine: Path | None = None
        try:
            from sqlalchemy import text
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            if only_unreferenced:
                existing = db.query(FilesImageResource).filter(FilesImageResource.id == str(resource_id).removeprefix("image:"), FilesImageResource.owner == owner).one_or_none()
                if existing is None:
                    return False
                staged = self.blob_root / f".retiring-files-image-{existing.id}"
                if not existing.is_active:
                    shared = db.query(FilesImageResource.id).filter(FilesImageResource.locator == existing.locator, FilesImageResource.is_active.is_(True)).first()
                    if not shared:
                        source = resolve_gallery_image_path(existing.locator, root=self.blob_root, require_exists=False)
                        source.unlink(missing_ok=True)
                        staged.unlink(missing_ok=True)
                    return True
                source = resolve_gallery_image_path(existing.locator, root=self.blob_root, require_exists=False)
                if staged.exists() and not source.exists():
                    os.replace(staged, source)
            row = self._get(db, owner, str(resource_id).removeprefix("image:"))
            if row.kind != "image":
                raise FilesImageError("Files image resource is unavailable")
            if only_unreferenced:
                if expected_revision is not None and row.revision != expected_revision:
                    return False
                provenance = dict(row.provenance or {})
                sessions = set(provenance.get("session_ids") or [])
                if provenance.get("session_id"):
                    sessions.add(provenance["session_id"])
                sessions.discard(session_id)
                excluded = {f"chat:{session_id}"} if session_id else set()
                if expected_revision is not None:
                    excluded.add("domain:core")
                claims = set(provenance.get("reference_claims") or []) - excluded
                if sessions or claims:
                    return False
            locator = str(row.locator or "")
            shared = bool(locator) and db.query(FilesImageResource.id).filter(
                FilesImageResource.locator == locator,
                FilesImageResource.id != row.id,
                FilesImageResource.is_active.is_(True),
            ).first()
            if locator and not shared:
                source = resolve_gallery_image_path(locator, root=self.blob_root, require_exists=False)
                quarantine = self.blob_root / f".retiring-files-image-{row.id}"
                if source.exists():
                    os.replace(source, quarantine)
                else:
                    quarantine = None
            row.is_active = False
            row.revision += 1
            try:
                db.commit()
            except Exception:
                db.rollback()
                if quarantine is not None and quarantine.exists():
                    os.replace(quarantine, self.blob_root / locator)
                raise
            if quarantine is not None:
                try:
                    quarantine.unlink()
                except OSError:
                    # The durable retirement succeeded. Leave the isolated
                    # orphan for later cleanup rather than claiming rollback.
                    pass
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def ref(row: FilesImageResource) -> str:
        return f"image:{row.id}"
