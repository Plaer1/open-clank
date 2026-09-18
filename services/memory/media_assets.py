"""Pre-alpha owner-scoped Memory photo admission and asset storage."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any, Mapping, Optional
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError


PHOTO_CONTRACT = "openclank.memory-media-asset/v1"
ASSOCIATED_TEXT_CONTRACT = "openclank.memory-associated-text/v1"
PHOTO_MAX_BYTES = 10 * 1024 * 1024
PHOTO_MAX_DIMENSION = 16_384
PHOTO_MAX_PIXELS = 40_000_000
PHOTO_FORMATS = {
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".png": ("PNG", "image/png"),
    ".webp": ("WEBP", "image/webp"),
}


class MediaAssetError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


@dataclass(frozen=True)
class AdmittedPhoto:
    asset_id: str
    original_sha256: str
    canonical_sha256: str
    media_type: str
    format: str
    width: int
    height: int
    bytes: bytes


def admit_photo(content: bytes, filename: str, declared_media_type: Optional[str] = None) -> AdmittedPhoto:
    if not isinstance(content, bytes) or not content:
        raise MediaAssetError("photo_empty", "The photo is empty or unreadable.")
    if len(content) > PHOTO_MAX_BYTES:
        raise MediaAssetError("photo_too_large", "Photos must be 10 MB or smaller.")
    suffix = Path(filename or "").suffix.lower()
    expected = PHOTO_FORMATS.get(suffix)
    if expected is None:
        raise MediaAssetError("photo_format_unsupported", "Use a JPEG, PNG, or WebP photo.")
    if declared_media_type and declared_media_type.lower().split(";", 1)[0] != expected[1]:
        raise MediaAssetError("photo_mime_mismatch", "The photo extension and media type do not agree.")
    try:
        with Image.open(BytesIO(content)) as source:
            source.verify()
        with Image.open(BytesIO(content)) as image:
            if image.format != expected[0]:
                raise MediaAssetError("photo_magic_mismatch", "The photo bytes do not match their extension.")
            if getattr(image, "n_frames", 1) != 1:
                raise MediaAssetError("photo_animation_unsupported", "Animated photos are not supported yet.")
            width, height = image.size
            if width < 1 or height < 1 or max(width, height) > PHOTO_MAX_DIMENSION or width * height > PHOTO_MAX_PIXELS:
                raise MediaAssetError("photo_dimensions_unsupported", "The photo dimensions exceed the pre-alpha limit.")
            image = ImageOps.exif_transpose(image)
            if expected[0] == "JPEG":
                if image.mode not in {"RGB", "L"}:
                    image = image.convert("RGB")
                save_kwargs = {"format": "JPEG", "quality": 95, "optimize": True}
            elif expected[0] == "PNG":
                if image.mode not in {"1", "L", "LA", "RGB", "RGBA", "I", "P"}:
                    image = image.convert("RGBA")
                save_kwargs = {"format": "PNG", "optimize": True}
            else:
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.mode else "RGB")
                save_kwargs = {"format": "WEBP", "quality": 95, "method": 6}
            output = BytesIO()
            # Deliberately omit EXIF/ICC/info when re-encoding. Orientation is
            # applied above; private metadata never enters canonical storage.
            image.save(output, **save_kwargs)
            canonical = output.getvalue()
    except UnidentifiedImageError as exc:
        raise MediaAssetError("photo_decode_failed", "The photo could not be decoded.") from exc
    except (SyntaxError, TypeError, ValueError) as exc:
        raise MediaAssetError("photo_metadata_invalid", "The photo metadata could not be safely normalized.") from exc
    except OSError as exc:
        raise MediaAssetError("photo_decode_failed", "The photo could not be decoded.") from exc
    return AdmittedPhoto(
        asset_id="asset_" + sha256(canonical).hexdigest()[:32],
        original_sha256=sha256(content).hexdigest(),
        canonical_sha256=sha256(canonical).hexdigest(),
        media_type=expected[1],
        format=expected[0],
        width=width,
        height=height,
        bytes=canonical,
    )


class MemoryMediaStore:
    """SQLite metadata plus owner-local content-addressed canonical bytes."""

    def __init__(self, db_path: str, data_dir: str) -> None:
        self.db_path = str(Path(db_path).expanduser().resolve())
        self.data_dir = Path(data_dir).expanduser().resolve()

    @classmethod
    def for_provider(
        cls,
        provider,
        *,
        default_db_path: str,
        default_data_dir: str,
    ) -> "MemoryMediaStore":
        """Resolve provider-specific and default roots without dirname drift."""

        provider_db = getattr(provider, "_fm_db_path", None) if provider is not None else None
        if provider_db:
            db_path = Path(str(provider_db)).expanduser().resolve()
            data_dir = db_path.parent
        else:
            db_path = Path(default_db_path).expanduser().resolve()
            data_dir = Path(default_data_dir).expanduser().resolve()
        return cls(str(db_path), str(data_dir))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS fm_v2_media_assets (
                owner_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                byte_size INTEGER NOT NULL,
                original_sha256 TEXT NOT NULL,
                canonical_sha256 TEXT NOT NULL,
                blob_key TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, asset_id),
                UNIQUE(owner_id, source_id, asset_id)
            );
            CREATE TABLE IF NOT EXISTS fm_v2_media_representations (
                owner_id TEXT NOT NULL,
                representation_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, representation_id),
                FOREIGN KEY(owner_id, asset_id) REFERENCES fm_v2_media_assets(owner_id, asset_id)
            );
            """
        )
        return conn

    def _blob_path(self, owner: str, canonical_sha256: str) -> Path:
        if not isinstance(owner, str) or not owner.strip():
            raise MediaAssetError("asset_owner_invalid", "The photo owner is invalid.")
        if not re.fullmatch(r"[a-f0-9]{64}", str(canonical_sha256 or "")):
            raise MediaAssetError("asset_hash_invalid", "The photo hash is invalid.")
        owner_key = sha256(owner.encode("utf-8")).hexdigest()[:32]
        root = self._media_root()
        directory = root / owner_key
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            )
        return directory / f"{canonical_sha256}.bin"

    def _media_root(self) -> Path:
        """Return the lexical media root only when it is not redirected."""

        root = self.data_dir / "memory_media"
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            )
        return root

    def _blob_key(self, blob: Path) -> str:
        try:
            return blob.relative_to(self.data_dir).as_posix()
        except ValueError:
            raise MediaAssetError("asset_path_invalid", "The photo storage reference is invalid.") from None

    def _path_from_stored_key(self, key: str) -> Path:
        raw = Path(str(key or ""))
        media_root = self._media_root()
        try:
            path = raw.resolve() if raw.is_absolute() else (self.data_dir / raw).resolve()
            path.relative_to(media_root.resolve())
        except (OSError, RuntimeError, ValueError):
            raise MediaAssetError("asset_path_invalid", "The photo storage reference is invalid.") from None
        return path

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stage_blob(self, blob: Path, data: bytes) -> Path:
        media_root = self._media_root()
        try:
            relative_parent = blob.parent.relative_to(media_root)
        except ValueError:
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            ) from None
        if len(relative_parent.parts) != 1:
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            )
        blob.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Recheck after mkdir so a pre-existing owner-directory symlink cannot
        # turn mkstemp into an out-of-root write authority.
        if (
            self._media_root() != media_root
            or blob.parent.is_symlink()
            or not blob.parent.is_dir()
        ):
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            )
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".openclank-memory-media-stage-",
            dir=blob.parent,
        )
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return Path(raw_path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(raw_path)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _discard_stage(stage: Optional[Path]) -> None:
        if stage is None or not stage.name.startswith(".openclank-memory-media-stage-"):
            return
        try:
            stage.unlink()
        except FileNotFoundError:
            pass

    def _publish_stage(self, stage: Path, blob: Path) -> None:
        created = False
        try:
            if stage.parent.resolve() != blob.parent.resolve():
                raise OSError("staging root changed")
            mode = stage.lstat().st_mode
            if not stat.S_ISREG(mode) or stage.is_symlink():
                raise OSError("staging file is not regular")
            os.link(stage, blob, follow_symlinks=False)
            created = True
            stage.unlink()
            try:
                os.chmod(blob, 0o600)
            except OSError:
                pass
            self._fsync_directory(blob.parent)
        except FileExistsError:
            # A concurrent idempotent writer may have published the same
            # owner-local CAS object.  It is accepted only after exact verify.
            data = self._read_regular_file(blob)
            expected = blob.stem
            if sha256(data).hexdigest() != expected:
                raise MediaAssetError(
                    "asset_integrity",
                    "The stored photo failed its integrity check.",
                ) from None
        except Exception:
            if created:
                try:
                    blob.unlink()
                    self._fsync_directory(blob.parent)
                except OSError:
                    pass
            raise

    @staticmethod
    def _read_regular_file(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):
                raise OSError("photo blob is not a regular file")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _owner_blob_directory(self, owner: str) -> Path:
        return self._blob_path(owner, "0" * 64).parent

    def _owner_blob_inventory(self, owner: str) -> list[dict[str, Any]]:
        directory = self._owner_blob_directory(owner)
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            )
        files = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            is_blob = bool(re.fullmatch(r"[a-f0-9]{64}\.bin", path.name))
            is_stage = path.name.startswith(".openclank-memory-media-stage-")
            if not is_blob and not is_stage:
                raise MediaAssetError(
                    "asset_path_invalid",
                    "The photo storage reference is invalid.",
                )
            try:
                data = self._read_regular_file(path)
            except OSError as exc:
                raise MediaAssetError(
                    "asset_path_invalid",
                    "The photo storage reference is invalid.",
                ) from exc
            files.append({
                "name": path.name,
                "size": len(data),
                "sha256": sha256(data).hexdigest(),
            })
        return files

    def preview_owner_purge(self, owner: str) -> dict[str, Any]:
        """Fingerprint every owner media row and byte before destructive reset."""

        with self._connect() as conn:
            assets = [
                dict(row)
                for row in conn.execute(
                    "SELECT asset_id,source_id,canonical_sha256,blob_key,state FROM fm_v2_media_assets WHERE owner_id=? ORDER BY asset_id",
                    (owner,),
                ).fetchall()
            ]
            representations = [
                dict(row)
                for row in conn.execute(
                    "SELECT representation_id,asset_id,text_hash FROM fm_v2_media_representations WHERE owner_id=? ORDER BY representation_id",
                    (owner,),
                ).fetchall()
            ]
        files = self._owner_blob_inventory(owner)
        referenced_hashes = {str(row["canonical_sha256"]) for row in assets}
        orphan_files = [
            row for row in files
            if not row["name"].endswith(".bin")
            or row["name"][:-4] not in referenced_hashes
        ]
        material = {
            "assets": assets,
            "representations": representations,
            "files": files,
        }
        fingerprint = sha256(
            json.dumps(
                material,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return {
            "count": len(assets) + len(representations) + len(orphan_files),
            "fingerprint": "sha256:" + fingerprint,
        }

    def rename_owner(self, old_owner: str, new_owner: str) -> dict[str, Any]:
        """Crash-reconcile one owner's media rows and owner-hashed byte tree.

        The byte directory moves before the SQLite transaction.  A process
        death in that narrow window is recognizable on retry as old-owner rows
        plus a new-owner byte directory; the same call then finishes the row
        copy.  A target that already contains rows or bytes while the source
        still contains the corresponding domain fails closed instead of
        merging two principals.
        """

        old_owner = str(old_owner or "").strip().lower()
        new_owner = str(new_owner or "").strip().lower()
        if not old_owner or not new_owner or "\x00" in old_owner or "\x00" in new_owner:
            raise MediaAssetError("asset_owner_invalid", "The photo owner is invalid.")
        if old_owner == new_owner:
            current = self.preview_owner_purge(old_owner)
            return {
                "complete": True,
                "already_applied": True,
                "count": int(current["count"]),
                "fingerprint": current["fingerprint"],
            }

        old_directory = self._owner_blob_directory(old_owner)
        new_directory = self._owner_blob_directory(new_owner)
        old_files = self._owner_blob_inventory(old_owner)
        new_files = self._owner_blob_inventory(new_owner)
        with self._connect() as conn:
            old_assets = conn.execute(
                "SELECT * FROM fm_v2_media_assets WHERE owner_id=? ORDER BY asset_id",
                (old_owner,),
            ).fetchall()
            new_asset_count = int(conn.execute(
                "SELECT count(*) FROM fm_v2_media_assets WHERE owner_id=?",
                (new_owner,),
            ).fetchone()[0])
            old_representations = conn.execute(
                "SELECT * FROM fm_v2_media_representations WHERE owner_id=? ORDER BY representation_id",
                (old_owner,),
            ).fetchall()
            new_representation_count = int(conn.execute(
                "SELECT count(*) FROM fm_v2_media_representations WHERE owner_id=?",
                (new_owner,),
            ).fetchone()[0])

        old_row_count = len(old_assets) + len(old_representations)
        new_row_count = new_asset_count + new_representation_count
        if old_row_count and new_row_count:
            raise MediaAssetError(
                "asset_owner_conflict",
                "Both photo owners contain durable media state.",
                retryable=True,
            )
        if old_files and new_files:
            raise MediaAssetError(
                "asset_owner_conflict",
                "Both photo owners contain durable media bytes.",
                retryable=True,
            )
        if old_row_count and not old_files and not new_files:
            raise MediaAssetError(
                "asset_integrity",
                "The stored photo bytes are missing during owner migration.",
                retryable=True,
            )
        if new_row_count and not old_files and not new_files:
            raise MediaAssetError(
                "asset_integrity",
                "The target photo bytes are missing during owner migration.",
                retryable=True,
            )

        moved_directory = False
        if old_files:
            try:
                new_directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if new_directory.exists():
                    if new_directory.is_symlink() or not new_directory.is_dir():
                        raise OSError("target photo directory is invalid")
                    if any(new_directory.iterdir()):
                        raise OSError("target photo directory is not empty")
                    new_directory.rmdir()
                os.replace(old_directory, new_directory)
                self._fsync_directory(new_directory.parent)
                moved_directory = True
                new_files = old_files
                old_files = []
            except OSError as exc:
                raise MediaAssetError(
                    "asset_owner_move_failed",
                    "The photo bytes could not be moved to the new owner.",
                    retryable=True,
                ) from exc

        try:
            if old_row_count:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    target_count = int(conn.execute(
                        "SELECT (SELECT count(*) FROM fm_v2_media_assets WHERE owner_id=?) + "
                        "(SELECT count(*) FROM fm_v2_media_representations WHERE owner_id=?)",
                        (new_owner, new_owner),
                    ).fetchone()[0])
                    if target_count:
                        raise MediaAssetError(
                            "asset_owner_conflict",
                            "The target photo owner changed during migration.",
                            retryable=True,
                        )
                    for row in old_assets:
                        blob = self._blob_path(new_owner, row["canonical_sha256"])
                        conn.execute(
                            "INSERT INTO fm_v2_media_assets(owner_id,asset_id,source_id,filename,media_type,width,height,byte_size,original_sha256,canonical_sha256,blob_key,state,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                new_owner,
                                row["asset_id"],
                                row["source_id"],
                                row["filename"],
                                row["media_type"],
                                row["width"],
                                row["height"],
                                row["byte_size"],
                                row["original_sha256"],
                                row["canonical_sha256"],
                                self._blob_key(blob),
                                row["state"],
                                row["created_at"],
                            ),
                        )
                    for row in old_representations:
                        conn.execute(
                            "INSERT INTO fm_v2_media_representations(owner_id,representation_id,asset_id,kind,text,text_hash,provenance_json,created_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                new_owner,
                                row["representation_id"],
                                row["asset_id"],
                                row["kind"],
                                row["text"],
                                row["text_hash"],
                                row["provenance_json"],
                                row["created_at"],
                            ),
                        )
                    conn.execute(
                        "DELETE FROM fm_v2_media_representations WHERE owner_id=?",
                        (old_owner,),
                    )
                    conn.execute(
                        "DELETE FROM fm_v2_media_assets WHERE owner_id=?",
                        (old_owner,),
                    )
                    conn.commit()
        except Exception as exc:
            if moved_directory and new_directory.exists() and not old_directory.exists():
                try:
                    os.replace(new_directory, old_directory)
                    self._fsync_directory(old_directory.parent)
                except OSError:
                    pass
            if isinstance(exc, MediaAssetError):
                raise
            raise MediaAssetError(
                "asset_owner_store_failed",
                "The photo metadata could not be moved to the new owner.",
                retryable=True,
            ) from exc

        source_after = self.preview_owner_purge(old_owner)
        target_after = self.preview_owner_purge(new_owner)
        if int(source_after["count"]) != 0:
            raise MediaAssetError(
                "asset_owner_move_incomplete",
                "The photo owner migration did not converge.",
                retryable=True,
            )
        return {
            "complete": True,
            "already_applied": old_row_count == 0 and not moved_directory,
            "count": int(target_after["count"]),
            "fingerprint": target_after["fingerprint"],
            "assets": len(old_assets) if old_row_count else new_asset_count,
            "representations": (
                len(old_representations) if old_row_count else new_representation_count
            ),
            "files": len(new_files),
        }

    def purge_owner(
        self,
        owner: str,
        *,
        expected: Mapping[str, Any],
    ) -> dict[str, Any]:
        """CAS-delete owner metadata, then GC only unreferenced owner blobs."""

        current = self.preview_owner_purge(owner)
        if dict(expected) != current and current["count"] != 0:
            raise MediaAssetError(
                "asset_purge_conflict",
                "The photo reset preview is stale.",
                retryable=True,
            )
        if current["count"] == 0:
            return {"complete": True, "count": 0, "blobs_removed": 0}

        inventory = self._owner_blob_inventory(owner)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM fm_v2_media_representations WHERE owner_id=?",
                (owner,),
            )
            conn.execute(
                "DELETE FROM fm_v2_media_assets WHERE owner_id=?",
                (owner,),
            )
            conn.commit()

        removed = 0
        directory = self._owner_blob_directory(owner)
        for item in inventory:
            path = directory / item["name"]
            if item["name"].endswith(".bin"):
                canonical_sha256 = item["name"][:-4]
                with self._connect() as conn:
                    referenced = conn.execute(
                        "SELECT 1 FROM fm_v2_media_assets WHERE owner_id=? AND canonical_sha256=? LIMIT 1",
                        (owner, canonical_sha256),
                    ).fetchone()
                if referenced is not None:
                    continue
            try:
                mode = path.lstat().st_mode
                if not stat.S_ISREG(mode) or path.is_symlink():
                    raise OSError("photo blob is not regular")
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MediaAssetError(
                    "asset_purge_incomplete",
                    "The photo reset could not remove every owner blob.",
                    retryable=True,
                ) from exc
        try:
            directory.rmdir()
        except OSError:
            pass
        after = self.preview_owner_purge(owner)
        if after["count"] != 0:
            raise MediaAssetError(
                "asset_purge_incomplete",
                "The photo reset did not converge.",
                retryable=True,
            )
        return {
            "complete": True,
            "count": int(current["count"]),
            "blobs_removed": removed,
        }

    def put(
        self,
        *,
        owner: str,
        source_id: str,
        filename: str,
        photo: AdmittedPhoto,
        associated_text: str,
        provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(source_id, str) or not source_id.strip():
            raise MediaAssetError("asset_source_invalid", "The photo source is invalid.")
        blob = self._blob_path(owner, photo.canonical_sha256)
        stage = None
        try:
            existing = self._read_regular_file(blob)
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise MediaAssetError(
                "asset_path_invalid",
                "The photo storage reference is invalid.",
            ) from exc
        if existing is not None and sha256(existing).hexdigest() != photo.canonical_sha256:
            raise MediaAssetError("asset_integrity", "The stored photo failed its integrity check.")
        if existing is None:
            stage = self._stage_blob(blob, photo.bytes)
        now = datetime.now(timezone.utc).isoformat()
        representation_id = "repr_" + sha256(
            json.dumps([photo.asset_id, associated_text, provenance], sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:32]
        asset_inserted = False
        representation_inserted = False
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO fm_v2_media_assets(owner_id,asset_id,source_id,filename,media_type,width,height,byte_size,original_sha256,canonical_sha256,blob_key,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (owner, photo.asset_id, source_id, filename[:255], photo.media_type, photo.width, photo.height, len(photo.bytes), photo.original_sha256, photo.canonical_sha256, self._blob_key(blob), "active", now),
                )
                asset_inserted = cursor.rowcount == 1
                if not asset_inserted:
                    current = conn.execute(
                        "SELECT canonical_sha256,state FROM fm_v2_media_assets WHERE owner_id=? AND asset_id=?",
                        (owner, photo.asset_id),
                    ).fetchone()
                    if (
                        current is None
                        or current[0] != photo.canonical_sha256
                        or current[1] != "active"
                    ):
                        raise MediaAssetError(
                            "asset_conflict",
                            "The photo asset conflicts with existing provenance.",
                        )
                if associated_text.strip():
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO fm_v2_media_representations(owner_id,representation_id,asset_id,kind,text,text_hash,provenance_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (owner, representation_id, photo.asset_id, "associated_text", associated_text.strip(), sha256(associated_text.strip().encode("utf-8")).hexdigest(), json.dumps(dict(provenance), sort_keys=True), now),
                    )
                    representation_inserted = cursor.rowcount == 1
                conn.commit()
        except MediaAssetError:
            self._discard_stage(stage)
            raise
        except Exception as exc:
            self._discard_stage(stage)
            raise MediaAssetError(
                "asset_store_unavailable",
                "The photo could not be stored.",
                retryable=True,
            ) from exc

        if stage is not None:
            try:
                self._publish_stage(stage, blob)
            except Exception as exc:
                self._discard_stage(stage)
                try:
                    with self._connect() as conn:
                        if representation_inserted:
                            conn.execute(
                                "DELETE FROM fm_v2_media_representations WHERE owner_id=? AND representation_id=?",
                                (owner, representation_id),
                            )
                        if asset_inserted:
                            conn.execute(
                                "DELETE FROM fm_v2_media_assets WHERE owner_id=? AND asset_id=?",
                                (owner, photo.asset_id),
                            )
                        conn.commit()
                except Exception:
                    pass
                if isinstance(exc, MediaAssetError):
                    raise
                raise MediaAssetError(
                    "asset_publish_failed",
                    "The photo could not be published.",
                    retryable=True,
                ) from exc
            finally:
                self._discard_stage(stage)
        return {
            "contract": PHOTO_CONTRACT,
            "associated_text_contract": ASSOCIATED_TEXT_CONTRACT,
            "asset_id": photo.asset_id,
            "representation_id": representation_id,
            "media_type": photo.media_type,
            "width": photo.width,
            "height": photo.height,
            "byte_size": len(photo.bytes),
            "canonical_sha256": photo.canonical_sha256,
            "filename": filename[:255],
        }

    def list_assets(self, owner: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset_id,source_id,filename,media_type,width,height,byte_size,canonical_sha256,blob_key,state,created_at FROM fm_v2_media_assets WHERE owner_id=? AND state='active' ORDER BY asset_id",
                (owner,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                representations = conn.execute(
                    "SELECT representation_id,kind,text,text_hash,provenance_json,created_at FROM fm_v2_media_representations WHERE owner_id=? AND asset_id=? ORDER BY representation_id",
                    (owner, row[0]),
                ).fetchall()
                item["representations"] = [
                    {**dict(rep), "provenance": json.loads(rep[4] or "{}")}
                    for rep in representations
                ]
                result.append(item)
            return result

    def read_blob(self, owner: str, asset_id: str) -> tuple[dict[str, Any], bytes]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset_id,media_type,canonical_sha256,blob_key,byte_size FROM fm_v2_media_assets WHERE owner_id=? AND asset_id=? AND state='active'",
                (owner, asset_id),
            ).fetchone()
            if not row:
                raise MediaAssetError("asset_not_found", "The photo is not available.")
        expected = self._blob_path(owner, row[2])
        path = self._path_from_stored_key(row[3])
        if path != expected:
            raise MediaAssetError("asset_path_invalid", "The photo storage reference is invalid.")
        try:
            data = self._read_regular_file(path)
        except OSError as exc:
            raise MediaAssetError("asset_not_found", "The photo is not available.") from exc
        if len(data) != row[4] or sha256(data).hexdigest() != row[2]:
            raise MediaAssetError("asset_integrity", "The stored photo failed its integrity check.")
        return dict(row), data
