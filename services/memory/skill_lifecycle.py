"""Small on-disk lifecycle primitives shared by the Python skill manager."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterator

from core.atomic_io import atomic_write_json, atomic_write_text


STATE_FILE = "_lifecycle.json"
REVISIONS_DIR = "_revisions"
USAGE_EVENTS_FILE = "_usage_events.jsonl"
PROMOTIONS_FILE = "_promotions.json"
BUNDLE_VERSION = 2
BUNDLE_MANIFEST = "_manifest.json"
_BUNDLE_INTERNAL = {
    STATE_FILE,
    REVISIONS_DIR,
    ".lifecycle.lock",
}
_LOCK_STATE = threading.local()


def legacy_skill_id(skill, path: str) -> str:
    """Give legacy files a stable ID without relying on their mutable slug."""
    seed = "|".join(
        (
            str(skill.owner or ""),
            str(skill.created or ""),
            os.path.realpath(path),
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"open-clank-skill:{seed}"))


def hydrate_identity(skill, path: str) -> None:
    if not skill.skill_id:
        skill.skill_id = legacy_skill_id(skill, path)
    skill.revision = max(1, int(skill.revision or 1))
    skill.content_hash = skill.compute_content_hash()


def state_path(skill_path: str) -> str:
    return os.path.join(os.path.dirname(skill_path), STATE_FILE)


def revision_path(skill_path: str, revision: int, content_hash: str) -> str:
    return os.path.join(
        os.path.dirname(skill_path),
        REVISIONS_DIR,
        f"{int(revision):08d}-{content_hash}.md",
    )


def load_state(skill_path: str) -> dict:
    try:
        with open(state_path(skill_path), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(skill_path: str, state: dict) -> None:
    atomic_write_json(state_path(skill_path), state, indent=2)


def ensure_revision(skill, skill_path: str, state: dict | None = None) -> dict:
    """Persist the exact head revision without granting activation authority."""
    hydrate_identity(skill, skill_path)
    state = dict(load_state(skill_path) if state is None else state)
    owner = str(skill.owner or "")
    if (
        ("skill_id" in state and state.get("skill_id") != skill.skill_id)
        or ("owner" in state and str(state.get("owner") or "") != owner)
    ):
        # Identity changes are staging operations. Never let a stale pointer
        # survive a head ownership/identity rewrite.
        state["published"] = None
    state.setdefault("skill_id", skill.skill_id)
    state.setdefault("owner", owner)
    state.setdefault("head_revision", skill.revision)
    state.setdefault("head_hash", skill.content_hash)
    # A lifecycle without an explicit pointer is staged. In particular, never
    # turn legacy frontmatter (`status: published`) into local authority.
    state.setdefault("published", None)
    history = state.setdefault("history", [])
    snapshot = revision_path(skill_path, skill.revision, skill.content_hash)
    row = next(
        (
            item
            for item in history
            if isinstance(item, dict)
            and item.get("revision") == skill.revision
            and item.get("content_hash") == skill.content_hash
        ),
        None,
    )
    if row is None:
        Path(snapshot).parent.mkdir(parents=True, exist_ok=True)
        snapshot_text = skill.to_markdown()
        atomic_write_text(snapshot, snapshot_text)
        row = {
            "skill_id": skill.skill_id,
            "owner": owner,
            "revision": skill.revision,
            "parent_revision": skill.parent_revision,
            "content_hash": skill.content_hash,
            "snapshot": os.path.relpath(snapshot, os.path.dirname(skill_path)),
            "snapshot_sha256": hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest(),
            "created_at": time.time(),
        }
        history.append(row)
    elif not row.get("snapshot_sha256"):
        try:
            snapshot_text = Path(snapshot).read_text(encoding="utf-8")
        except OSError:
            snapshot_text = ""
        if snapshot_text:
            row["snapshot_sha256"] = hashlib.sha256(
                snapshot_text.encode("utf-8")
            ).hexdigest()
    state["skill_id"] = skill.skill_id
    state["owner"] = owner
    state["head_revision"] = skill.revision
    state["head_hash"] = skill.content_hash
    save_state(skill_path, state)
    return state


def read_snapshot(skill_path: str, pointer: dict) -> str | None:
    relative = str(pointer.get("snapshot") or "")
    parts = PurePosixPath(relative).parts
    if (
        not relative
        or "\x00" in relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or not parts
        or parts[0] != REVISIONS_DIR
        or any(part in ("", ".", "..") for part in parts)
    ):
        return None
    base = Path(skill_path).resolve().parent
    if _path_components_have_symlink(base, parts):
        return None
    raw_target = base.joinpath(*parts)
    target = raw_target.resolve()
    try:
        if (
            os.path.commonpath((str(base), str(target))) != str(base)
            or raw_target.is_symlink()
            or not target.is_file()
        ):
            return None
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


def _safe_bundle_path(value: str) -> str | None:
    if not value or "\x00" in value or "\\" in value:
        return None
    path = PurePosixPath(value)
    parts = path.parts
    if (
        path.is_absolute()
        or not parts
        or parts[0] in _BUNDLE_INTERNAL
        or any(part in ("", ".", "..") for part in parts)
    ):
        return None
    return "/".join(parts)


def _path_components_have_symlink(base: Path, parts: tuple[str, ...]) -> bool:
    """Reject a bundle path if any component below its resolved base is a link."""
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _revision_snapshot_bytes(
    skill_path: str,
    revision: int,
    content_hash: str,
) -> bytes:
    target = Path(revision_path(skill_path, revision, content_hash))
    try:
        return target.read_bytes()
    except OSError as exc:
        raise ValueError("skill revision snapshot is unavailable") from exc


def _live_bundle_files(
    skill_path: str,
    revision: int,
    content_hash: str,
) -> dict[str, tuple[bytes, int]]:
    raw_base = Path(skill_path).parent
    if raw_base.is_symlink():
        raise ValueError("invalid skill bundle root")
    base = raw_base.resolve()
    if not base.is_dir():
        raise ValueError("invalid skill bundle root")
    files: dict[str, tuple[bytes, int]] = {
        "SKILL.md": (
            _revision_snapshot_bytes(skill_path, revision, content_hash),
            stat.S_IMODE(Path(skill_path).stat().st_mode),
        )
    }
    for current, dirs, names in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            item = current_path / name
            if item.is_symlink():
                raise ValueError("skill bundle cannot contain symlinks")
            relative = item.relative_to(base).as_posix()
            if relative in _BUNDLE_INTERNAL:
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            item = current_path / name
            relative = item.relative_to(base).as_posix()
            if relative == "SKILL.md" or relative in _BUNDLE_INTERNAL:
                continue
            if item.is_symlink() or not item.is_file():
                raise ValueError("skill bundle must contain regular files")
            safe = _safe_bundle_path(relative)
            if safe != relative:
                raise ValueError("invalid skill bundle path")
            files[relative] = (
                item.read_bytes(),
                stat.S_IMODE(item.stat().st_mode),
            )
    return files


def _bundle_manifest(
    skill_path: str,
    revision: int,
    content_hash: str,
) -> tuple[dict, dict[str, tuple[bytes, int]]]:
    source = _live_bundle_files(skill_path, revision, content_hash)
    files = {
        relative: {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "mode": mode,
        }
        for relative, (content, mode) in sorted(source.items())
    }
    return {"version": BUNDLE_VERSION, "files": files}, source


def _manifest_text(manifest: dict) -> str:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def current_bundle_sha256(
    skill_path: str,
    revision: int | None = None,
    content_hash: str | None = None,
) -> str:
    state = load_state(skill_path)
    resolved_revision = int(
        revision if revision is not None else state.get("head_revision") or 0
    )
    resolved_hash = str(
        content_hash if content_hash is not None else state.get("head_hash") or ""
    )
    if resolved_revision < 1 or not resolved_hash:
        raise ValueError("skill lifecycle has no head revision")
    manifest, _source = _bundle_manifest(
        skill_path,
        resolved_revision,
        resolved_hash,
    )
    return hashlib.sha256(_manifest_text(manifest).encode("utf-8")).hexdigest()


def snapshot_bundle(
    skill_path: str,
    revision: int,
    content_hash: str,
) -> dict[str, str | int]:
    """Pin one complete authored skill tree as an immutable revision bundle."""
    manifest, source = _bundle_manifest(skill_path, revision, content_hash)
    text = _manifest_text(manifest)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    relative_root = PurePosixPath(
        REVISIONS_DIR,
        f"{int(revision):08d}-{content_hash}-{digest}.bundle",
    ).as_posix()
    target = Path(skill_path).parent.joinpath(
        *PurePosixPath(relative_root).parts
    )
    manifest_relative = PurePosixPath(
        relative_root,
        BUNDLE_MANIFEST,
    ).as_posix()
    descriptor = {
        "bundle_root": relative_root,
        "bundle_manifest": manifest_relative,
        "bundle_sha256": digest,
        "bundle_version": BUNDLE_VERSION,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if read_bundle_snapshot(skill_path, descriptor) is None:
            raise ValueError("immutable skill bundle snapshot was modified")
        return descriptor

    staging = Path(tempfile.mkdtemp(prefix=".skill-bundle-", dir=target.parent))
    try:
        for relative, (content, mode) in source.items():
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with open(destination, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(destination, mode)
        atomic_write_text(str(staging / BUNDLE_MANIFEST), text)
        os.replace(staging, target)
        parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        **descriptor,
    }


def read_bundle_snapshot(skill_path: str, pointer: dict) -> dict | None:
    relative_root = str(pointer.get("bundle_root") or "")
    relative_manifest = str(pointer.get("bundle_manifest") or "")
    digest = str(pointer.get("bundle_sha256") or "")
    if (
        pointer.get("bundle_version") != BUNDLE_VERSION
        or not relative_root
        or not relative_manifest
        or "\x00" in relative_root
        or "\x00" in relative_manifest
        or "\\" in relative_root
        or "\\" in relative_manifest
        or len(digest) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in digest)
    ):
        return None
    root_parts = PurePosixPath(relative_root).parts
    manifest_parts = PurePosixPath(relative_manifest).parts
    if (
        PurePosixPath(relative_root).is_absolute()
        or PurePosixPath(relative_manifest).is_absolute()
        or not root_parts
        or root_parts[0] != REVISIONS_DIR
        or any(part in ("", ".", "..") for part in root_parts)
        or any(part in ("", ".", "..") for part in manifest_parts)
    ):
        return None
    base = Path(skill_path).resolve().parent
    raw_root = base.joinpath(*root_parts)
    raw_manifest = base.joinpath(*manifest_parts)
    if (
        _path_components_have_symlink(base, root_parts)
        or _path_components_have_symlink(base, manifest_parts)
    ):
        return None
    root = raw_root.resolve()
    manifest_path = raw_manifest.resolve()
    try:
        if (
            os.path.commonpath((str(base), str(root))) != str(base)
            or manifest_path != root / BUNDLE_MANIFEST
            or raw_root.is_symlink()
            or not root.is_dir()
            or raw_manifest.is_symlink()
        ):
            return None
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != BUNDLE_VERSION
        or not isinstance(payload.get("files"), dict)
    ):
        return None
    expected_paths: set[str] = set()
    for relative_path, metadata in payload["files"].items():
        if (
            not isinstance(relative_path, str)
            or _safe_bundle_path(relative_path) != relative_path
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("sha256"), str)
            or len(metadata["sha256"]) != 64
            or type(metadata.get("size")) is not int
            or metadata["size"] < 0
            or type(metadata.get("mode")) is not int
            or not 0 <= metadata["mode"] <= 0o7777
        ):
            return None
        target = root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            if (
                os.path.commonpath((str(root), str(target.resolve()))) != str(root)
                or target.is_symlink()
                or not target.is_file()
            ):
                return None
            content = target.read_bytes()
            if (
                len(content) != metadata["size"]
                or hashlib.sha256(content).hexdigest() != metadata["sha256"]
                or stat.S_IMODE(target.stat().st_mode) != metadata["mode"]
            ):
                return None
        except (OSError, ValueError):
            return None
        expected_paths.add(relative_path)
    actual_paths: set[str] = set()
    try:
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in dirs:
                if (current_path / name).is_symlink():
                    return None
            for name in names:
                item = current_path / name
                if item.is_symlink() or not item.is_file():
                    return None
                relative = item.relative_to(root).as_posix()
                if relative != BUNDLE_MANIFEST:
                    actual_paths.add(relative)
    except (OSError, ValueError):
        return None
    if actual_paths != expected_paths or "SKILL.md" not in expected_paths:
        return None
    payload["root"] = str(root)
    return payload


def read_bundle_file(
    skill_path: str,
    pointer: dict,
    relative_path: str,
) -> str | None:
    safe_path = _safe_bundle_path(relative_path)
    if safe_path is None:
        return None
    payload = read_bundle_snapshot(skill_path, pointer)
    if payload is None:
        return None
    if safe_path not in payload["files"]:
        return None
    try:
        return (
            Path(payload["root"])
            .joinpath(*PurePosixPath(safe_path).parts)
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError):
        return None


@contextmanager
def locked(skill_path: str) -> Iterator[None]:
    """Serialize lifecycle compare-and-swap operations across local processes."""
    lock_path = os.path.join(os.path.dirname(skill_path), ".lifecycle.lock")
    lock_path = os.path.realpath(lock_path)
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    if held.get(lock_path, 0):
        held[lock_path] += 1
        try:
            yield
        finally:
            held[lock_path] -= 1
        return
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt locks a byte range, so the lock file must hold at least
            # one byte before locking (mirrors core/atomic_io.py).
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                held[lock_path] = 1
                yield
            finally:
                held.pop(lock_path, None)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                held[lock_path] = 1
                yield
            finally:
                held.pop(lock_path, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_usage_event(skills_root: str, event: dict) -> None:
    """Append one cross-runtime event in a single O_APPEND write."""
    path = os.path.join(skills_root, USAGE_EVENTS_FILE)
    payload = json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def load_usage_events(skills_root: str) -> list[dict]:
    path = os.path.join(skills_root, USAGE_EVENTS_FILE)
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    out.append(event)
    except OSError:
        pass
    return out
