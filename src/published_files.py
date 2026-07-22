"""One lifecycle for agent-published bytes and their revocable links."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import secrets
import stat
import tempfile
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from core.database import PublishedFile, PublishedFileGrant, SessionLocal
from core.platform_compat import safe_chmod
from src.constants import UPLOAD_DIR
from src.upload_handler import get_chat_upload_max_bytes

logger = logging.getLogger(__name__)


class PublishedFileError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean_filename(value: str) -> str:
    name = unicodedata.normalize("NFC", Path(value or "file").name)
    name = "".join(ch for ch in name if ord(ch) >= 32 and ch not in {"\x7f", "/", "\\"})
    return (name.strip() or "file")[:240]


def _clean_origin(value: Optional[str]) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PublishedFileError("The public app URL must be an http(s) origin.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PublishedFileError("The public app URL must not contain credentials, a query, or a fragment.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class PublishedFileService:
    def __init__(
        self,
        storage_root: Optional[str] = None,
        session_factory: Callable = SessionLocal,
    ) -> None:
        self.storage_root = os.path.realpath(
            storage_root or os.path.join(UPLOAD_DIR, ".published")
        )
        self.session_factory = session_factory

    def _path(self, file_id: str) -> str:
        if len(file_id) != 32 or any(ch not in "0123456789abcdef" for ch in file_id):
            raise PublishedFileError("Invalid file ID.")
        path = os.path.realpath(os.path.join(self.storage_root, file_id[:2], file_id))
        if os.path.commonpath([path, self.storage_root]) != self.storage_root:
            raise PublishedFileError("Invalid file ID.")
        return path

    @staticmethod
    def _active(grant: PublishedFileGrant, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        return grant.revoked_at is None and (
            grant.expires_at is None or grant.expires_at > now
        )

    def _grant(
        self,
        db,
        row: PublishedFile,
        *,
        audience: str,
        expires_in_hours: Optional[int],
        public_origin: Optional[str],
    ) -> tuple[PublishedFileGrant, str, str]:
        audience = str(audience or "owner").strip().lower()
        if audience not in {"owner", "public"}:
            raise PublishedFileError("audience must be owner or public.")

        origin = _clean_origin(public_origin)
        if audience == "public":
            if not origin:
                raise PublishedFileError("Set the public app URL before creating a public link.")
            hours = 24 if expires_in_hours is None else int(expires_in_hours)
            if hours < 1 or hours > 24 * 30:
                raise PublishedFileError("Public links must expire in 1 to 720 hours.")
            expires_at = _utcnow() + timedelta(hours=hours)
        elif expires_in_hours is not None:
            hours = int(expires_in_hours)
            if hours < 1 or hours > 24 * 365:
                raise PublishedFileError("Owner links may expire in 1 to 8760 hours.")
            expires_at = _utcnow() + timedelta(hours=hours)
        else:
            expires_at = None

        token = secrets.token_urlsafe(32)
        grant = PublishedFileGrant(
            id=uuid.uuid4().hex,
            file_id=row.id,
            token_hash=_token_hash(token),
            audience=audience,
            expires_at=expires_at,
        )
        db.add(grant)
        relative = f"/api/files/download/{token}"
        return grant, token, f"{origin}{relative}" if origin else relative

    def publish(
        self,
        source_path: str,
        *,
        owner: str,
        audience: str = "owner",
        expires_in_hours: Optional[int] = None,
        public_origin: Optional[str] = None,
    ) -> dict:
        owner = str(owner or "").strip().lower()
        source_path = os.path.abspath(source_path)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(source_path, flags)
        except (FileNotFoundError, NotADirectoryError):
            raise PublishedFileError("The source file does not exist.")
        except OSError as exc:
            raise PublishedFileError("The source must be a readable regular file.") from exc

        file_id = uuid.uuid4().hex
        destination = self._path(file_id)
        os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
        safe_chmod(self.storage_root, 0o700)
        safe_chmod(os.path.dirname(destination), 0o700)
        temp_path = ""
        digest = hashlib.sha256()
        copied = 0
        try:
            before = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            raise PublishedFileError("The source must be a readable regular file.") from exc
        if not stat.S_ISREG(before.st_mode):
            os.close(fd)
            raise PublishedFileError("The source must be a regular file.")
        try:
            with os.fdopen(fd, "rb", closefd=True) as source:
                limit = get_chat_upload_max_bytes()
                if before.st_size <= 0:
                    raise PublishedFileError("The source file is empty.")
                if before.st_size > limit:
                    raise PublishedFileError(f"The source file exceeds the {limit}-byte limit.")

                out_fd, temp_path = tempfile.mkstemp(prefix=".publishing-", dir=os.path.dirname(destination))
                try:
                    os.fchmod(out_fd, 0o600)
                    with os.fdopen(out_fd, "wb", closefd=True) as target:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            copied += len(chunk)
                            if copied > limit:
                                raise PublishedFileError(f"The source file exceeds the {limit}-byte limit.")
                            digest.update(chunk)
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                except Exception:
                    try:
                        os.close(out_fd)
                    except OSError:
                        pass
                    raise

                after = os.fstat(source.fileno())
                fingerprint_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                fingerprint_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if copied != before.st_size or fingerprint_before != fingerprint_after:
                    raise PublishedFileError("The source changed while it was being published; try again.")

            os.replace(temp_path, destination)
            temp_path = ""
            safe_chmod(destination, 0o600)

            filename = _clean_filename(source_path)
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            db = self.session_factory()
            try:
                row = PublishedFile(
                    id=file_id,
                    owner=owner,
                    filename=filename,
                    mime_type=mime,
                    size=copied,
                    sha256=digest.hexdigest(),
                    source="agent",
                )
                db.add(row)
                db.flush()
                grant, _token, url = self._grant(
                    db,
                    row,
                    audience=audience,
                    expires_in_hours=expires_in_hours,
                    public_origin=public_origin,
                )
                db.commit()
                db.refresh(row)
                return {
                    **self.serialize(row, grants=[grant]),
                    "grant_id": grant.id,
                    "audience": grant.audience,
                    "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
                    "download_url": url,
                }
            except Exception:
                db.rollback()
                try:
                    os.unlink(destination)
                except FileNotFoundError:
                    pass
                raise
            finally:
                db.close()
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def serialize(self, row: PublishedFile, grants=None) -> dict:
        grants = list(row.grants if grants is None else grants)
        now = _utcnow()
        active = [grant for grant in grants if self._active(grant, now)]
        return {
            "id": row.id,
            "kind": "published",
            "filename": row.filename,
            "mime_type": row.mime_type,
            "size": row.size,
            "sha256": row.sha256,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "active_grant_count": len(active),
            "audiences": sorted({grant.audience for grant in active}),
            "next_expiry": min(
                (grant.expires_at for grant in active if grant.expires_at),
                default=None,
            ).isoformat() if any(grant.expires_at for grant in active) else None,
        }

    def list(self, *, owner: str, search: str = "", limit: int = 200) -> list[dict]:
        owner = str(owner or "").strip().lower()
        db = self.session_factory()
        try:
            query = db.query(PublishedFile).filter(PublishedFile.owner == owner)
            if search.strip():
                query = query.filter(PublishedFile.filename.ilike(f"%{search.strip()}%"))
            rows = query.order_by(PublishedFile.created_at.desc()).limit(max(1, min(limit, 500))).all()
            return [self.serialize(row) for row in rows]
        finally:
            db.close()

    def get_owned(self, file_id: str, *, owner: str) -> Optional[dict]:
        owner = str(owner or "").strip().lower()
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(
                PublishedFile.id == file_id,
                PublishedFile.owner == owner,
            ).first()
            if not row:
                return None
            path = self._path(row.id)
            if not os.path.isfile(path):
                return None
            return {**self.serialize(row), "path": path, "owner": row.owner}
        finally:
            db.close()

    def resolve_grant(self, token: str) -> Optional[dict]:
        if not token or len(token) > 128:
            return None
        db = self.session_factory()
        try:
            grant = db.query(PublishedFileGrant).filter(
                PublishedFileGrant.token_hash == _token_hash(token)
            ).first()
            if not grant or not self._active(grant) or not grant.file:
                return None
            path = self._path(grant.file.id)
            if not os.path.isfile(path):
                return None
            return {
                **self.serialize(grant.file),
                "path": path,
                "owner": grant.file.owner,
                "audience": grant.audience,
                "grant_id": grant.id,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
        finally:
            db.close()

    def create_grant(
        self,
        file_id: str,
        *,
        owner: str,
        audience: str = "owner",
        expires_in_hours: Optional[int] = None,
        public_origin: Optional[str] = None,
    ) -> dict:
        owner = str(owner or "").strip().lower()
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(
                PublishedFile.id == file_id,
                PublishedFile.owner == owner,
            ).first()
            if not row or not os.path.isfile(self._path(row.id)):
                raise PublishedFileError("File not found.")
            grant, _token, url = self._grant(
                db,
                row,
                audience=audience,
                expires_in_hours=expires_in_hours,
                public_origin=public_origin,
            )
            db.commit()
            return {
                "grant_id": grant.id,
                "audience": grant.audience,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
                "download_url": url,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def revoke_all(self, file_id: str, *, owner: str) -> int:
        owner = str(owner or "").strip().lower()
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(
                PublishedFile.id == file_id,
                PublishedFile.owner == owner,
            ).first()
            if not row:
                raise PublishedFileError("File not found.")
            now = _utcnow()
            count = db.query(PublishedFileGrant).filter(
                PublishedFileGrant.file_id == row.id,
                PublishedFileGrant.revoked_at.is_(None),
            ).update({"revoked_at": now}, synchronize_session=False)
            db.commit()
            return int(count)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete(self, file_id: str, *, owner: str) -> bool:
        owner = str(owner or "").strip().lower()
        db = self.session_factory()
        parked = ""
        original = ""
        try:
            row = db.query(PublishedFile).filter(
                PublishedFile.id == file_id,
                PublishedFile.owner == owner,
            ).first()
            if not row:
                return False
            original = self._path(row.id)
            if os.path.lexists(original):
                parked = original + ".deleting-" + uuid.uuid4().hex
                os.replace(original, parked)
            db.delete(row)
            db.commit()
        except Exception:
            db.rollback()
            if parked and os.path.exists(parked):
                try:
                    os.replace(parked, original)
                except OSError:
                    logger.exception("Could not restore published file after failed metadata deletion")
            raise
        finally:
            db.close()
        if parked:
            try:
                os.unlink(parked)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Published-file tombstone could not be removed: %s", os.path.basename(parked))
        return True
