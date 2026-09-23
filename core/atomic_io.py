"""Atomic and compare-and-swap file writes.

Use this everywhere a JSON config file is persisted. A plain `open("w") +
json.dump` truncates the file on first write and only fills it with new
content afterwards — a kill -9 / power loss / OOM in between produces a
truncated or empty file. For password DBs (`auth.json`) and live state
(`sessions.json`, `settings.json`, `integrations.json`, `cookbook_state.json`),
that's a data-loss event.

`atomic_write_json` writes to a sibling tmp file, fsyncs, then `os.replace`s
into place. On POSIX `os.replace` is atomic on the same filesystem.
"""

from __future__ import annotations

import json
import os
import hashlib
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from typing import Any, NamedTuple, Optional, Sequence

from src.openclank.history_capture import (
    HistoryContext,
    begin_file_capture,
    complete_file_capture,
)


class AtomicWriteConflict(RuntimeError):
    """The target changed after the caller observed it."""


class AtomicRollbackError(RuntimeError):
    """A failed batch left recoverable sibling backups on disk."""

    def __init__(self, failures: Sequence[str], backups: Sequence[str]):
        self.failures = tuple(failures)
        self.backups = tuple(backups)
        super().__init__(
            "atomic batch rollback failed; recoverable backups retained: "
            + ", ".join(self.backups)
        )


def _require_history_complete(result: dict[str, Any]) -> None:
    if result.get("history_status") in {"paused", "unavailable", "unconfigured"}:
        return
    if result.get("history_status") != "complete":
        phase = result.get("capture_phase") or "unknown"
        detail = result.get("error") or "history reconciliation did not complete"
        raise AtomicWriteConflict(
            f"history reconciliation pending ({phase}): {detail}"
        )


def _canonical_target(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _assert_canonical_target(path: str, expected: str) -> None:
    if os.path.normcase(_canonical_target(path)) != os.path.normcase(expected):
        raise AtomicWriteConflict(f"{path}: canonical target changed during write")


@contextmanager
def _path_locks(paths: Sequence[str]):
    """Serialize overlapping path sets while disjoint files remain parallel."""
    lock_root = os.path.join(tempfile.gettempdir(), "open-clank-file-locks")
    os.makedirs(lock_root, mode=0o700, exist_ok=True)
    keys = sorted({
        os.path.normcase(_canonical_target(path))
        for path in paths
    })
    with ExitStack() as stack:
        for key in keys:
            digest = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
            handle = stack.enter_context(open(os.path.join(lock_root, digest + ".lock"), "a+b"))
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                stack.callback(
                    lambda locked=handle: (
                        locked.seek(0),
                        msvcrt.locking(locked.fileno(), msvcrt.LK_UNLCK, 1),
                    )
                )
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                stack.callback(fcntl.flock, handle.fileno(), fcntl.LOCK_UN)
        yield


def fingerprint_bytes(data: bytes) -> str:
    """Return the stable content fingerprint used by both reads and writes."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}"


def file_fingerprint(path: str) -> Optional[str]:
    """Fingerprint a regular file, or return ``None`` when it does not exist."""
    try:
        with open(path, "rb") as handle:
            return fingerprint_bytes(handle.read())
    except FileNotFoundError:
        return None


def _fsync_directory(directory: str) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _stage_bytes(path: str, data: bytes, mode: Optional[int]) -> str:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.tmp.", dir=directory)
    try:
        if mode is not None and os.name != "nt":
            os.fchmod(fd, stat.S_IMODE(mode))
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return tmp
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_bytes(
    path: str,
    data: bytes,
    *,
    expected_fingerprint: Optional[str] = None,
    require_missing: bool = False,
    mode: Optional[int] = None,
    action_id: Optional[str] = None,
    history_context: Optional[HistoryContext] = None,
) -> str:
    """Atomically replace ``path`` and reject a stale caller snapshot.

    ``expected_fingerprint`` is a compare-and-swap precondition.  New-file
    callers use ``require_missing`` so a concurrent creator is never silently
    overwritten.  Existing executable/permission bits are preserved unless an
    explicit ``mode`` is supplied.
    """
    if not isinstance(data, bytes):
        raise TypeError("atomic_write_bytes expects bytes")
    requested = path
    canonical = _canonical_target(requested)
    path = canonical
    with _path_locks([canonical]):
        _assert_canonical_target(requested, canonical)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        _assert_canonical_target(requested, canonical)
        before = file_fingerprint(path)
        if require_missing and before is not None:
            raise AtomicWriteConflict(f"{requested}: expected a missing file")
        if expected_fingerprint is not None and before != expected_fingerprint:
            raise AtomicWriteConflict(f"{requested}: changed since it was read")
        if mode is None and before is not None:
            mode = os.stat(path, follow_symlinks=False).st_mode

        history = begin_file_capture(
            path,
            operation="replace",
            context=history_context,
            action_id=action_id,
        )
        tmp: Optional[str] = None
        history_completion_failed = False
        try:
            tmp = _stage_bytes(path, data, mode)
            _assert_canonical_target(requested, canonical)
            current = file_fingerprint(path)
            if require_missing and current is not None:
                raise AtomicWriteConflict(f"{requested}: created by another writer")
            if expected_fingerprint is not None and current != expected_fingerprint:
                raise AtomicWriteConflict(f"{requested}: changed during the write")
            if require_missing:
                try:
                    os.link(tmp, path)
                except FileExistsError as exc:
                    raise AtomicWriteConflict(
                        f"{requested}: created by another writer"
                    ) from exc
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            else:
                os.replace(tmp, path)
            _fsync_directory(directory)
            try:
                _require_history_complete(complete_file_capture(history, path))
            except BaseException:
                history_completion_failed = True
                raise
        except BaseException:
            if not history_completion_failed:
                history.abort()
            raise
        finally:
            try:
                if tmp is not None:
                    os.unlink(tmp)
            except FileNotFoundError:
                pass
    return fingerprint_bytes(data)


class AtomicFileChange(NamedTuple):
    path: str
    data: Optional[bytes]
    expected_fingerprint: Optional[str] = None
    require_missing: bool = False
    mode: Optional[int] = None
    action_id: Optional[str] = None
    history_context: Optional[HistoryContext] = None


def atomic_write_batch(changes: Sequence[AtomicFileChange]) -> None:
    """Stage a file set, then commit it with rollback on any failure.

    Filesystems cannot expose a truly atomic rename across unrelated paths.
    This helper provides the useful tool contract: no validation or ordinary
    I/O failure leaves a partially-applied patch.  Every replacement is staged
    beside its destination and every prior file is retained until the whole
    batch succeeds.
    """
    action_ids = {change.action_id for change in changes if change.action_id}
    if len(action_ids) > 1:
        raise ValueError("atomic batch contains multiple action ids")
    contexts = {id(change.history_context) for change in changes if change.history_context is not None}
    if len(contexts) > 1:
        raise ValueError("atomic batch contains multiple history contexts")
    canonical_targets = [_canonical_target(change.path) for change in changes]
    if len({os.path.normcase(path) for path in canonical_targets}) != len(changes):
        raise ValueError("atomic batch contains duplicate target paths")

    with _path_locks(canonical_targets):
        primary = canonical_targets[0] if canonical_targets else ""
        action_id = next((change.action_id for change in changes if change.action_id), None)
        history = (
            begin_file_capture(
                primary,
                operation="batch",
                context=next((change.history_context for change in changes if change.history_context), None),
                action_id=action_id,
                paths=canonical_targets[1:],
            )
            if primary
            else None
        )
        recoverability_requested = any(
            change.history_context is not None for change in changes
        ) and len(changes) > 1
        if recoverability_requested and (history is None or not history.available):
            raise AtomicWriteConflict(
                "history_prepare_required: recoverable multi-resource mutation was not durably prepared"
            )
        staged: dict[str, str] = {}
        backups: dict[str, str] = {}
        installed: set[str] = set()
        retained_backups: set[str] = set()
        history_completion_failed = False
        try:
            for change, canonical in zip(changes, canonical_targets):
                requested = change.path
                target = canonical
                _assert_canonical_target(requested, target)
                directory = os.path.dirname(target) or "."
                os.makedirs(directory, exist_ok=True)
                _assert_canonical_target(requested, target)
                before = file_fingerprint(target)
                if change.require_missing and before is not None:
                    raise AtomicWriteConflict(f"{requested}: expected a missing file")
                if change.expected_fingerprint is not None and before != change.expected_fingerprint:
                    raise AtomicWriteConflict(f"{requested}: changed since it was read")
                mode = change.mode
                if mode is None and before is not None:
                    mode = os.stat(target, follow_symlinks=False).st_mode
                if change.data is not None:
                    staged[target] = _stage_bytes(target, change.data, mode)

            for change, canonical in zip(changes, canonical_targets):
                requested = change.path
                target = canonical
                _assert_canonical_target(requested, target)
                current = file_fingerprint(target)
                if change.require_missing and current is not None:
                    raise AtomicWriteConflict(f"{requested}: created by another writer")
                if change.expected_fingerprint is not None and current != change.expected_fingerprint:
                    raise AtomicWriteConflict(f"{requested}: changed during the batch")

                if current is not None:
                    directory = os.path.dirname(target) or "."
                    fd, backup = tempfile.mkstemp(
                        prefix=f".{os.path.basename(target)}.bak.",
                        dir=directory,
                    )
                    os.close(fd)
                    os.unlink(backup)
                    os.replace(target, backup)
                    backups[target] = backup
                if change.data is not None:
                    tmp = staged[target]
                    if change.require_missing:
                        try:
                            os.link(tmp, target)
                        except FileExistsError as exc:
                            raise AtomicWriteConflict(
                                f"{requested}: created by another writer"
                            ) from exc
                        installed.add(target)
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        else:
                            staged.pop(target)
                    else:
                        os.replace(tmp, target)
                        staged.pop(target)
                        installed.add(target)
                _fsync_directory(os.path.dirname(target) or ".")
            if history is not None:
                try:
                    _require_history_complete(complete_file_capture(history, primary))
                except BaseException:
                    history_completion_failed = True
                    raise
        except BaseException as original:
            if history is not None and not history_completion_failed:
                history.abort()
            if history_completion_failed:
                # The physical batch is already installed. Keep it in place
                # and surface the durable history retry requirement; restoring
                # backups here would falsely report rollback after a committed
                # live action.
                raise
            rollback_failures: list[str] = []
            for change, target in reversed(list(zip(changes, canonical_targets))):
                backup = backups.get(target)
                try:
                    if target in installed:
                        os.unlink(target)
                    if backup and os.path.exists(backup):
                        os.replace(backup, target)
                    _fsync_directory(os.path.dirname(target) or ".")
                except OSError as rollback_error:
                    rollback_failures.append(f"{change.path}: {rollback_error}")
                    if backup and os.path.exists(backup):
                        retained_backups.add(backup)
            if rollback_failures:
                raise AtomicRollbackError(
                    rollback_failures,
                    sorted(retained_backups),
                ) from original
            raise
        finally:
            for tmp in (*staged.values(), *backups.values()):
                if tmp in retained_backups:
                    continue
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass


def atomic_write_json(path: str, data: Any, *, indent: Optional[int] = None) -> None:
    """Atomically persist `data` as JSON at `path`.

    The temp file uses the live PID as a suffix so two processes saving the
    same file (e.g. unit tests) don't collide on the rename target.
    """
    text = json.dumps(data, indent=indent)
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_text(path: str, text: str) -> None:
    if not isinstance(text, str):
        raise TypeError("atomic_write_text expects a string")
    atomic_write_bytes(path, text.encode("utf-8"))
