import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from src.constants import GENERATED_IMAGES_DIR


GENERATED_IMAGE_DIR = Path(GENERATED_IMAGES_DIR)
GENERATED_IMAGE_RE = re.compile(
    r"^[a-f0-9]{8,64}\.(png|jpg|jpeg|webp|gif|mp4|mov|webm|mkv|m4v)$"
)
GENERATED_IMAGE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
_GALLERY_STAGE_PREFIX = ".openclank-gallery-stage-"
_GALLERY_BACKUP_PREFIX = ".openclank-gallery-backup-"


def gallery_owner_key(user: object) -> Optional[str]:
    """Return a named Gallery principal; missing identity stays missing."""
    owner = str(user or "").strip()
    return owner or None


def gallery_image_root(root: str | Path | None = None) -> Path:
    """Resolve the one configured Gallery byte root."""

    try:
        return Path(root if root is not None else GENERATED_IMAGE_DIR).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise HTTPException(status_code=500, detail="Gallery storage is unavailable") from None


def has_generated_image_provenance(
    session_factory,
    gallery_model,
    *,
    filename: str,
    owner: Optional[str],
) -> bool:
    """Fail-closed proof that one active Gallery row owns ``filename``."""

    if not owner or not str(owner).strip():
        return False
    db = None
    allowed = False
    try:
        db = session_factory()
        allowed = db.query(gallery_model.id).filter(
            gallery_model.filename == filename,
            gallery_model.owner == owner,
            gallery_model.is_active == True,
        ).first() is not None
    except Exception:
        allowed = False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                allowed = False
    return allowed


def resolve_gallery_image_path(
    filename: str,
    *,
    root: str | Path | None = None,
    require_exists: bool = False,
) -> Path:
    """Resolve an exact safe Gallery basename under generated-images storage."""
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", filename):
        raise HTTPException(status_code=400, detail="Unsafe gallery filename")
    if filename in {".", ".."} or Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Unsafe gallery filename")
    if filename.startswith((_GALLERY_STAGE_PREFIX, _GALLERY_BACKUP_PREFIX)):
        raise HTTPException(status_code=400, detail="Unsafe gallery filename")
    resolved_root = gallery_image_root(root)
    candidate = resolved_root / filename
    # A stored symlink is never an image authority, even when it currently
    # points back inside the root.  This also keeps replace/delete from
    # following a link an attacker swaps into the configured directory.
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail="Unsafe gallery filename")
    path = candidate.resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unsafe gallery filename") from None
    if require_exists:
        try:
            mode = path.lstat().st_mode
        except OSError:
            raise HTTPException(status_code=404, detail="Image not found") from None
        if not stat.S_ISREG(mode):
            raise HTTPException(status_code=404, detail="Image not found")
    return path


def resolve_generated_image_path(
    filename: str,
    *,
    root: str | Path | None = None,
) -> Path:
    if not isinstance(filename, str) or not GENERATED_IMAGE_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return resolve_gallery_image_path(filename, root=root, require_exists=True)


def stage_gallery_image_bytes(
    content: bytes,
    *,
    root: str | Path | None = None,
) -> Path:
    """Durably stage bytes under the Gallery root without making them routable."""

    if not isinstance(content, bytes):
        raise TypeError("Gallery image content must be bytes")
    resolved_root = gallery_image_root(root)
    resolved_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix=_GALLERY_STAGE_PREFIX, dir=resolved_root)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return Path(raw_path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(raw_path)
        except FileNotFoundError:
            pass
        raise


def _checked_stage_path(staged: str | Path, root: str | Path | None) -> tuple[Path, Path]:
    resolved_root = gallery_image_root(root)
    stage = Path(staged)
    try:
        if stage.parent.resolve() != resolved_root or not stage.name.startswith(_GALLERY_STAGE_PREFIX):
            raise ValueError
        mode = stage.lstat().st_mode
        if not stat.S_ISREG(mode) or stage.is_symlink():
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise RuntimeError("Invalid Gallery staging file") from None
    return resolved_root, stage


def _fsync_gallery_root(root: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(root, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_staged_gallery_image(
    staged: str | Path,
    filename: str,
    *,
    root: str | Path | None = None,
    replace: bool = False,
) -> Path:
    """Atomically publish a staged file at an owner-provenanced filename.

    The function has a strict failure boundary: when it raises, a new
    destination is absent and a replaced destination has its prior bytes.
    Callers can therefore compensate database metadata without guessing
    whether a post-rename directory sync already exposed the new file.
    """

    resolved_root, stage = _checked_stage_path(staged, root)
    destination = resolve_gallery_image_path(filename, root=resolved_root)

    if not replace:
        linked = False
        try:
            os.link(stage, destination, follow_symlinks=False)
            linked = True
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            _fsync_gallery_root(resolved_root)
        except FileExistsError as exc:
            raise RuntimeError("Gallery filename already exists") from exc
        except BaseException:
            if linked:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                try:
                    _fsync_gallery_root(resolved_root)
                except OSError:
                    pass
            raise

        # The destination is durable now.  Stage cleanup is not allowed to
        # turn a successful publication into an ambiguous failure; the caller's
        # finally block can retry cleanup if this unlink is transiently denied.
        try:
            stage.unlink()
        except FileNotFoundError:
            pass
        try:
            _fsync_gallery_root(resolved_root)
        except OSError:
            pass
        return destination

    backup: Path | None = None
    replaced = False
    try:
        try:
            existing_mode = destination.lstat().st_mode
        except FileNotFoundError:
            existing_mode = None
        if existing_mode is not None:
            if not stat.S_ISREG(existing_mode) or destination.is_symlink():
                raise RuntimeError("Gallery replacement target is not a regular file")
            descriptor, backup_name = tempfile.mkstemp(
                prefix=_GALLERY_BACKUP_PREFIX,
                dir=resolved_root,
            )
            os.close(descriptor)
            os.unlink(backup_name)
            backup = Path(backup_name)
            os.link(destination, backup, follow_symlinks=False)

        os.replace(stage, destination)
        replaced = True
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass
        _fsync_gallery_root(resolved_root)
    except BaseException:
        if replaced:
            try:
                if backup is not None and backup.exists():
                    os.replace(backup, destination)
                else:
                    destination.unlink()
            finally:
                try:
                    _fsync_gallery_root(resolved_root)
                except OSError:
                    pass
        if backup is not None:
            try:
                backup.unlink()
            except FileNotFoundError:
                pass
        raise

    if backup is not None:
        try:
            backup.unlink()
        except OSError:
            # The replacement itself is already durable.  A private hidden
            # backup cleanup failure must not report an ambiguous publication
            # failure to callers; later maintenance can remove the remnant.
            pass
    try:
        _fsync_gallery_root(resolved_root)
    except OSError:
        pass
    return destination


def discard_staged_gallery_image(
    staged: str | Path | None,
    *,
    root: str | Path | None = None,
) -> None:
    """Remove only a helper-created, non-routable Gallery staging file."""

    if staged is None:
        return
    try:
        _resolved_root, stage = _checked_stage_path(staged, root)
    except RuntimeError:
        return
    try:
        stage.unlink()
    except FileNotFoundError:
        pass
