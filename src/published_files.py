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


def _chat_snapshot_id(value) -> bool:
    try:
        return type(value) is str and bool(value) and value == value.strip() and len(value.encode('utf-8', 'strict')) <= 512
    except UnicodeError:
        return False


def _chat_snapshot_hash(value) -> bool:
    import re
    return type(value) is str and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def render_chat_snapshot(observation: dict) -> tuple[bytes, str, str]:
    """Render a bounded observed transcript; this is not source authorization."""
    import json
    if (type(observation) is not dict or set(observation) != {'version','chat_id','workspace_id','source_sha256','messages'}
            or type(observation['version']) is not int or observation['version'] != 1
            or not _chat_snapshot_id(observation['chat_id'])
            or (observation['workspace_id'] is not None and not _chat_snapshot_id(observation['workspace_id']))
            or not _chat_snapshot_hash(observation['source_sha256'])
            or type(observation['messages']) is not list or not 1 <= len(observation['messages']) <= 256):
        raise PublishedFileError('Chat snapshot unavailable.')
    rows = []
    total = 0
    nonempty = False
    try:
        for message in observation['messages']:
            if (type(message) is not dict or set(message) != {'role','text'} or message['role'] not in ('user','assistant')
                    or type(message['text']) is not str):
                raise PublishedFileError('Chat snapshot unavailable.')
            total += len(message['text'].encode('utf-8', 'strict'))
            if total > 1024 * 1024: raise PublishedFileError('Chat snapshot unavailable.')
            nonempty = nonempty or bool(message['text'])
            rows.append(('You' if message['role'] == 'user' else 'Clank') + ':\n' + message['text'])
        if not nonempty: raise PublishedFileError('Chat snapshot unavailable.')
        data = ('\n\n'.join(rows) + '\n').encode('utf-8', 'strict')
        if len(data) > 2 * 1024 * 1024: raise PublishedFileError('Chat snapshot unavailable.')
        text_sha256 = hashlib.sha256(data).hexdigest()
        review = {'version':1, 'chat_id':observation['chat_id'], 'workspace_id':observation['workspace_id'],
                  'source_sha256':observation['source_sha256'], 'text_sha256':text_sha256}
        review_sha256 = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')).hexdigest()
        return data, text_sha256, review_sha256
    except UnicodeError as exc:
        raise PublishedFileError('Chat snapshot unavailable.') from exc


def _typed_target(value, *, owner_subject_id: str) -> dict:
    """Validate server-derived identity metadata; it grants no source access."""
    import json
    import re
    from pathlib import PurePosixPath
    from src.openclank.resource_refs import stable_resource_id
    if type(value) is dict and type(value.get('version')) is int and value['version'] == 2:
        chat = value.get('chat')
        if (set(value) != {'version','mode','chat','dependencies'} or value['mode'] != 'snapshot'
                or type(value['dependencies']) is not list or value['dependencies'] != []
                or type(chat) is not dict or set(chat) != {'id','workspace_id','source_sha256','text_sha256'}
                or not _chat_snapshot_id(chat['id'])
                or (chat['workspace_id'] is not None and not _chat_snapshot_id(chat['workspace_id']))
                or not _chat_snapshot_hash(chat['source_sha256']) or not _chat_snapshot_hash(chat['text_sha256'])):
            raise PublishedFileError('Publication target unavailable.')
        return {'version':2, 'mode':'snapshot', 'chat':dict(chat), 'dependencies':[]}
    if (type(value) is not dict or set(value) != {'version','mode','resource','dependencies'}
            or type(value['version']) is not int or value['version'] != 1 or value['mode'] not in {'snapshot','live'}
            or type(value['dependencies']) is not list or len(value['dependencies']) > 32):
        raise PublishedFileError('Publication target unavailable.')
    def resource(item):
        if (type(item) is not dict or set(item) != {'provider','origin_id','resource_id','location_id','workspace_id','revision'}
                or item['provider'] != 'host' or type(item['origin_id']) is not str or not item['origin_id'] or len(item['origin_id']) > 8192
                or type(item['location_id']) is not str or not item['location_id'] or len(item['location_id']) > 256
                or (item['workspace_id'] is not None and (type(item['workspace_id']) is not str or not item['workspace_id'] or len(item['workspace_id']) > 256))
                or type(item['revision']) is not dict or set(item['revision']) != {'kind','value'}
                or any(type(item['revision'][k]) is not str or not item['revision'][k] or len(item['revision'][k]) > 8192 for k in ('kind','value'))
                or item['resource_id'] != stable_resource_id(owner_subject_id=owner_subject_id,provider='host',origin_id=item['origin_id'])):
            raise PublishedFileError('Publication resource unavailable.')
        return dict(item)
    main = resource(value['resource']); dependencies = []; names = set()
    for item in value['dependencies']:
        if type(item) is not dict or set(item) != {'name','resource','snapshot_file_id'}: raise PublishedFileError('Publication dependency unavailable.')
        name = item['name']; path = PurePosixPath(name) if type(name) is str else None
        if (not path or not name or len(name.encode('utf-8','strict')) > 1024 or path.is_absolute()
                or any(part in {'','.','..'} for part in name.split('/')) or '\\' in name or name in names
                or any(ord(c)<32 or ord(c)==127 for c in name)):
            raise PublishedFileError('Publication dependency unavailable.')
        child = item['snapshot_file_id']
        if value['mode'] == 'snapshot':
            if type(child) is not str or not re.fullmatch(r'[0-9a-f]{32}',child): raise PublishedFileError('Publication dependency unavailable.')
        elif child is not None: raise PublishedFileError('Publication dependency unavailable.')
        names.add(name); dependencies.append({'name':name,'resource':resource(item['resource']),'snapshot_file_id':child})
    result = {'version':1,'mode':value['mode'],'resource':main,'dependencies':dependencies}
    if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8')) > 65536:
        raise PublishedFileError('Publication target is too large.')
    return result


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
        owner_subject_id: Optional[str] = None,
        typed_target: Optional[dict] = None,
        filename_override: Optional[str] = None,
    ) -> dict:
        owner = str(owner or "").strip().lower()
        sealed_target = None
        if typed_target is not None:
            from src.secret_storage import encrypt
            import json
            if not isinstance(owner_subject_id,str) or not owner_subject_id: raise PublishedFileError("Publication account unavailable.")
            validated = _typed_target(typed_target, owner_subject_id=owner_subject_id)
            sealed_target = encrypt(json.dumps(validated,ensure_ascii=False,separators=(',',':')))
            if not sealed_target.startswith('enc:'): raise PublishedFileError("Publication target unavailable.")
        elif owner_subject_id is not None:
            raise PublishedFileError("Publication target unavailable.")
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

            if typed_target is not None and validated['version'] == 2:
                if (copied > 2 * 1024 * 1024 or digest.hexdigest() != validated['chat']['text_sha256']
                        or filename_override != 'conversation-snapshot.txt'):
                    raise PublishedFileError('Chat snapshot changed.')
            os.replace(temp_path, destination)
            temp_path = ""
            safe_chmod(destination, 0o600)

            filename = _clean_filename(filename_override if filename_override is not None else source_path)
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
                    owner_subject_id=owner_subject_id,
                    target_ciphertext=sealed_target,
                )
                if typed_target is not None and validated['mode'] == 'snapshot':
                    from src.secret_storage import decrypt, is_encrypted
                    import json
                    for dependency in validated['dependencies']:
                        child = db.query(PublishedFile).filter(PublishedFile.id == dependency['snapshot_file_id'],
                            PublishedFile.owner == owner, PublishedFile.owner_subject_id == owner_subject_id).first()
                        if (child is None or not child.target_ciphertext or not is_encrypted(child.target_ciphertext)
                                or not os.path.isfile(self._path(child.id))):
                            raise PublishedFileError('Publication dependency unavailable.')
                        try:
                            child_target = _typed_target(json.loads(decrypt(child.target_ciphertext)), owner_subject_id=owner_subject_id)
                        except Exception as exc:
                            raise PublishedFileError('Publication dependency unavailable.') from exc
                        if child_target['version'] != 1 or child_target['mode'] != 'snapshot' or child_target['dependencies'] or child_target['resource'] != dependency['resource']:
                            raise PublishedFileError('Publication dependency changed.')
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

    def owned_live_feedback_targets(self, *, owner: str, owner_subject_id: str, resource_id: str) -> tuple[list[dict], bool]:
        """Bounded navigation candidates; callers independently authorize each use."""
        import json
        from src.secret_storage import decrypt, is_encrypted
        db = self.session_factory()
        try:
            rows = db.query(PublishedFile).filter(PublishedFile.owner == owner, PublishedFile.owner_subject_id == owner_subject_id,
                PublishedFile.target_ciphertext.isnot(None)).order_by(PublishedFile.id).limit(201).all()
            partial=len(rows)>200; result=[]
            for row in rows[:200]:
                try:
                    if not is_encrypted(row.target_ciphertext) or not any(self._active(grant) for grant in row.grants): continue
                    target=_typed_target(json.loads(decrypt(row.target_ciphertext)),owner_subject_id=owner_subject_id)
                    if target.get('mode')=='live' and target['resource']['resource_id']==resource_id:
                        result.append({'id':row.id,'name':row.filename})
                except Exception: partial=True
            return result,partial
        finally: db.close()

    def shared_candidates(self, *, owners: list[str], after_id: str, limit: int = 64) -> list[dict]:
        """Private bounded keyset scan restricted to known role-owner usernames."""
        import re
        if (not owners or len(owners) > 32 or len(set(owners)) != len(owners)
                or any(type(owner) is not str or not owner or len(owner) > 256 for owner in owners)
                or (after_id and not re.fullmatch(r'[0-9a-f]{32}', after_id)) or limit != 64):
            return []
        db = self.session_factory()
        try:
            rows = db.query(PublishedFile.id, PublishedFile.filename).filter(
                PublishedFile.owner.in_(owners), PublishedFile.target_ciphertext.isnot(None),
                PublishedFile.owner_subject_id.isnot(None), PublishedFile.id > after_id
            ).order_by(PublishedFile.id).limit(65).all()
            return [{'id':row.id,'filename':row.filename} for row in rows]
        finally:
            db.close()

    def shared_candidate(self, *, file_id: str, owners: list[str]) -> Optional[dict]:
        db = self.session_factory()
        try:
            row = db.query(PublishedFile.id, PublishedFile.filename).filter(
                PublishedFile.id == file_id, PublishedFile.owner.in_(owners),
                PublishedFile.target_ciphertext.isnot(None), PublishedFile.owner_subject_id.isnot(None)).first()
            return {'id':row.id,'filename':row.filename} if row else None
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
            result = {
                **self.serialize(grant.file),
                "path": path,
                "owner": grant.file.owner,
                "audience": grant.audience,
                "grant_id": grant.id,
                "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            }
            if grant.file.target_ciphertext is not None:
                from src.secret_storage import decrypt, is_encrypted
                import json
                if not is_encrypted(grant.file.target_ciphertext) or not grant.file.owner_subject_id:
                    return None
                try:
                    result['_target'] = _typed_target(json.loads(decrypt(grant.file.target_ciphertext)), owner_subject_id=grant.file.owner_subject_id)
                except Exception:
                    return None
                result['_owner_subject_id'] = grant.file.owner_subject_id
                if result['_target']['mode'] == 'snapshot' and not os.path.isfile(path): return None
            elif not os.path.isfile(path):
                return None
            return result
        finally:
            db.close()

    def typed_live_target(self, file_id: str) -> Optional[dict]:
        """Return an internal live target candidate; this is not authorization."""
        import json, re
        from src.secret_storage import decrypt, is_encrypted
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id):
            return None
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(PublishedFile.id == file_id).first()
            if not row or not row.owner_subject_id or not row.target_ciphertext or not is_encrypted(row.target_ciphertext):
                return None
            try:
                target = _typed_target(json.loads(decrypt(row.target_ciphertext)), owner_subject_id=row.owner_subject_id)
            except Exception:
                return None
            if target['mode'] != 'live':
                return None
            return {'id': row.id, 'owner': row.owner, 'owner_subject_id': row.owner_subject_id, 'target': target}
        finally:
            db.close()

    def typed_owned_target(self, file_id: str, *, owner: str) -> Optional[dict]:
        """Read a private typed candidate; caller independently proves authority."""
        import json, re
        from src.secret_storage import decrypt, is_encrypted
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id): return None
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(PublishedFile.id == file_id, PublishedFile.owner == owner).first()
            if not row or not row.owner_subject_id or not row.target_ciphertext or not is_encrypted(row.target_ciphertext): return None
            try: target = _typed_target(json.loads(decrypt(row.target_ciphertext)), owner_subject_id=row.owner_subject_id)
            except Exception: return None
            return {'id':row.id, 'owner':row.owner, 'owner_subject_id':row.owner_subject_id, 'target':target}
        finally:
            db.close()

    def create_typed_grant(self, candidate: dict, *, audience: str, expires_in_hours: Optional[int],
                           public_origin: Optional[str], current) -> dict:
        """Create a grant only while the original row and owner authority remain current."""
        from src.secret_storage import decrypt, is_encrypted
        import json
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(PublishedFile.id == candidate['id'],
                PublishedFile.owner == candidate['owner'], PublishedFile.owner_subject_id == candidate['owner_subject_id']).first()
            if not row or not row.target_ciphertext or not is_encrypted(row.target_ciphertext) or not current():
                raise PublishedFileError('Publication unavailable or changed.')
            target = _typed_target(json.loads(decrypt(row.target_ciphertext)), owner_subject_id=row.owner_subject_id)
            if target != candidate['target'] or (target['mode'] == 'snapshot' and not os.path.isfile(self._path(row.id))):
                raise PublishedFileError('Publication unavailable or changed.')
            grant, _token, url = self._grant(db, row, audience=audience,
                expires_in_hours=expires_in_hours, public_origin=public_origin)
            if not current(): raise PublishedFileError('Publication unavailable or changed.')
            db.commit()
            return {'grant_id':grant.id,'audience':grant.audience,
                    'expires_at':grant.expires_at.isoformat() if grant.expires_at else None,'download_url':url}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_live(self, *, owner: str, owner_subject_id: str, filename: str, mime_type: str,
                    size: int, sha256: str, typed_target: dict, audience: str, expires_in_hours: Optional[int],
                    public_origin: Optional[str]) -> dict:
        """Publish server-qualified live identity without retaining its bytes."""
        import json, re
        from src.secret_storage import encrypt
        if (not isinstance(owner_subject_id,str) or not owner_subject_id or type(size) is not int or size < 0
                or not isinstance(sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',sha256)
                or not isinstance(mime_type,str) or not mime_type or len(mime_type)>256):
            raise PublishedFileError('Publication metadata unavailable.')
        target = _typed_target(typed_target,owner_subject_id=owner_subject_id)
        if target['mode'] != 'live': raise PublishedFileError('Publication target unavailable.')
        sealed = encrypt(json.dumps(target,ensure_ascii=False,separators=(',',':')))
        if not sealed.startswith('enc:'): raise PublishedFileError('Publication target unavailable.')
        db = self.session_factory()
        try:
            row = PublishedFile(id=uuid.uuid4().hex,owner=str(owner or '').strip().lower(),filename=_clean_filename(filename),
                mime_type=mime_type,size=size,sha256=sha256,source='agent',owner_subject_id=owner_subject_id,target_ciphertext=sealed)
            db.add(row);db.flush()
            grant, _token, url = self._grant(db,row,audience=audience,expires_in_hours=expires_in_hours,public_origin=public_origin)
            db.commit();db.refresh(row)
            return {**self.serialize(row,grants=[grant]),'grant_id':grant.id,'audience':grant.audience,
                    'expires_at':grant.expires_at.isoformat() if grant.expires_at else None,'download_url':url}
        except Exception:
            db.rollback();raise
        finally:
            db.close()

    def resolve_snapshot_dependency(self, *, file_id: str, owner: str, owner_subject_id: str, resource: dict) -> Optional[dict]:
        """Resolve one already-owned retained child under its parent grant."""
        from src.secret_storage import decrypt, is_encrypted
        import json
        db = self.session_factory()
        try:
            row = db.query(PublishedFile).filter(PublishedFile.id == file_id,PublishedFile.owner == owner,
                PublishedFile.owner_subject_id == owner_subject_id).first()
            if row is None or not row.target_ciphertext or not is_encrypted(row.target_ciphertext): return None
            try: target = _typed_target(json.loads(decrypt(row.target_ciphertext)),owner_subject_id=owner_subject_id)
            except Exception: return None
            path = self._path(row.id)
            if target['version'] != 1 or target['mode'] != 'snapshot' or target['dependencies'] or target['resource'] != resource or not os.path.isfile(path): return None
            return {'path':path,'filename':row.filename,'mime_type':row.mime_type,'sha256':row.sha256,'size':row.size}
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
            # A typed target needs a fresh owner-source check before a new link.
            # The legacy generic grant endpoint cannot perform that check.
            if row.target_ciphertext is not None:
                raise PublishedFileError("Typed publication requires current source authority.")
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
