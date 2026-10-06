"""Detached shell worker: contain the child and redact its bounded log."""

from __future__ import annotations

import codecs
import json
import os
import re
import signal
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

from core.atomic_io import (
    AtomicFileChange,
    atomic_write_batch,
    atomic_write_text,
    atomic_write_json,
    file_fingerprint,
)
from core.platform_compat import kill_process_tree
from src.shell_policy import (
    ShellApprovalError,
    append_shell_audit,
    contained_argv,
    inject_sudo_askpass,
    minimal_shell_env,
    redact_text,
    shell_approval_binding,
    shell_command_argv,
    StreamingRedactor,
)
from src.clanker_paths import is_reference_path
from src.project_hex import (
    HexResolution,
    require_executable_trust,
    require_hex_activation,
    validate_project_file_candidates,
    verified_contract_snapshot,
)
from src.openclank import history_capture as _history_capture
from src.openclank.history_capture import (
    CaptureHandle,
    RootJournal,
    WriterOwner,
    complete_file_capture,
    context_from_mapping,
    begin_file_capture,
)

_MAX_LOG_BYTES = 4 * 1024 * 1024
_MAX_COMMAND_BYTES = 1024 * 1024
_TRUNCATED = b"\n...[output capped at 4 MiB]...\n"
_MAX_POLICY_CHANGE_BYTES = 256 * 1024 * 1024
_MAX_POLICY_FILES = 100_000
_POLICY_OVERLAY_SKIP = {
    ".git",
    ".references",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "data",
}


@dataclass
class _ShellCapture:
    """One pinned, recoverable capture for a shell command's writable roots.

    The command itself is still governed by the OS containment returned by
    ``contained_argv``.  This object only records the exact before/after state
    for roots which the caller already granted; it never expands that grant.
    """

    handle: CaptureHandle
    roots: tuple[str, ...]
    grace_ms: int = 250
    finished: bool = False
    result: dict | None = None
    _finish_started: bool = False
    capture_state_path: str = ""
    process_group_id: int | None = None
    root_journal: Any = None
    writer_owner: WriterOwner | None = None
    reconciliation_timeout_s: float = 0.0

    def _root_signatures(self) -> tuple[tuple[tuple[str, int, ...], ...] | None, ...]:
        """Read cheap identity metadata while writers are settling.

        Full directory manifests are exact but expensive.  Metadata polling is
        only a wakeup/correctness hint; the final CompleteBatch still reads an
        exact manifest once after the process group is gone and the signature
        is stable.
        """
        values: list[tuple[tuple[str, int, ...], ...] | None] = []
        for root in self.roots:
            try:
                records: list[tuple[str, int, ...]] = []
                root_info = os.lstat(root)
                records.append((".", int(root_info.st_dev), int(root_info.st_ino), int(root_info.st_mode), int(root_info.st_size), int(root_info.st_mtime_ns), int(root_info.st_ctime_ns)))
                for current, dirs, files in os.walk(root, followlinks=False):
                    dirs.sort()
                    files.sort()
                    base = Path(current)
                    for name in [*dirs, *files]:
                        path = base / name
                        info = os.lstat(path)
                        records.append((
                            str(path.relative_to(root)), int(info.st_dev), int(info.st_ino),
                            int(info.st_mode), int(info.st_size), int(info.st_mtime_ns),
                            int(info.st_ctime_ns),
                        ))
                values.append(tuple(records))
            except FileNotFoundError:
                values.append(None)
            except OSError as exc:
                raise ShellApprovalError(
                    f"shell mutation after-state metadata cannot be captured for authorized root: {exc}"
                ) from exc
        return tuple(values)

    def _process_group_alive(self) -> bool:
        if self.process_group_id is None or os.name == "nt":
            return False
        try:
            os.killpg(self.process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _state(self, phase: str, **extra: object) -> None:
        if not self.capture_state_path:
            return
        payload = {
            "action_id": self.handle.action_id,
            "phase": phase,
            "roots": list(self.roots),
            "process_group_id": self.process_group_id,
            "worker_pid": os.getpid(),
            "writer_owner": self.writer_owner.to_mapping() if self.writer_owner else None,
            "journal_generation": getattr(self.root_journal, "generation", None),
            **extra,
        }
        try:
            atomic_write_json(self.capture_state_path, payload, indent=2)
        except OSError:
            # The service receipt remains authoritative; state persistence is
            # an additional restart hint and must not change child semantics.
            pass

    def _refresh_after_metadata(self) -> None:
        """Bind top-level mode/size/mtime to the same after-state receipt."""
        entries = self.handle.batch_entries or []
        for index, root in enumerate(self.roots):
            if index >= len(entries):
                break
            try:
                info = os.lstat(root)
            except FileNotFoundError:
                metadata = {"mode": None, "size": None, "modified_millis": None, "opaque": None}
            except OSError as exc:
                raise ShellApprovalError(
                    f"shell mutation after metadata cannot be captured: {exc}"
                ) from exc
            else:
                metadata = {
                    "mode": stat.S_IMODE(info.st_mode),
                    "size": info.st_size,
                    "modified_millis": int(info.st_mtime_ns // 1_000_000),
                    "opaque": entries[index].get("metadata", {}).get("opaque"),
                }
            entries[index]["metadata"] = metadata

    def finish(self, *, committed: bool) -> dict:
        if self.finished:
            return dict(self.result or {"history_status": "failed", "capture_phase": "after_failed"})
        self.finished = True
        if self._finish_started:
            return dict(self.result or {"history_status": "failed", "capture_phase": "finish_in_progress"})
        self._finish_started = True
        if not committed:
            try:
                self.result = self.handle.finish(committed=False)
                self._state("not_committed", result=self.result)
                self._journal_abandon("not_committed")
                return self.result
            finally:
                self.handle.close()

        # A shell can leave a background writer behind after its parent exits
        # (for example ``(sleep 1; echo x > file) &``).  This worker remains
        # the authenticated continuation owner while that process group is
        # live; it does not exit with a misleading pending receipt.  The
        # durable job timeout remains the outer bound and terminates the
        # worker if the descendant never settles.
        try:
            self._state("reconciling")
            previous = self._root_signatures()
            grace_deadline = time.monotonic() + max(0, self.grace_ms) / 1000
            deadline = (
                time.monotonic() + self.reconciliation_timeout_s
                if self.reconciliation_timeout_s > 0
                else grace_deadline
            )
            stable = 0
            continuing = False
            # Unit/direct callers with no descendant grace retain the normal
            # single immediate completion path. Launched jobs always supply
            # the durable reconciliation deadline above.
            if self.grace_ms <= 0 and self.reconciliation_timeout_s <= 0:
                stable = 2
            while stable < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
                current = self._root_signatures()
                if current == previous and not self._process_group_alive():
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    previous = current
                    stable = 0
                if time.monotonic() >= grace_deadline and not continuing:
                    if self.root_journal is not None and self.writer_owner is not None:
                        self.root_journal.handoff(self.writer_owner)
                    continuing = True
                    self._state("reconciling", continuation="shell_worker")
            if stable < 2:
                self.result = self.handle.finish(
                    committed=True,
                    after_read_error=(
                        "late writer reconciliation did not reach a stable after-state "
                        "before the authenticated worker deadline"
                    ),
                )
                self._state("after_failed", result=self.result)
                self._journal_abandon("late_writer_reconciliation_timeout")
                return self.result
            self._refresh_after_metadata()
            self.result = complete_file_capture(self.handle, self.roots[0], committed=True)
            self._state(
                self.result.get("capture_phase") or self.result.get("history_status", "failed"),
                coverage_status=self.result.get("history_status"),
                result=self.result,
            )
            self._journal_settle_or_abandon()
            return self.result
        except Exception as exc:
            self.result = self.handle.finish(committed=True, after_read_error=str(exc))
            self._state("failed", result=self.result)
            self._journal_abandon("capture_failed")
            return self.result
        finally:
            # The action context is request-scoped.  It must be released on
            # success, pending reconciliation, cancellation, and every error;
            # a second caller cannot accidentally reuse this capture token.
            self.handle.close()

    def _journal_settle_or_abandon(self) -> None:
        if self.root_journal is None or self.result is None:
            return
        if self.result.get("history_status") == "complete":
            self.root_journal.settle(self.result)
        else:
            self._journal_abandon(str(self.result.get("capture_phase") or "after_failed"))

    def _journal_abandon(self, reason: str) -> None:
        if self.root_journal is None:
            return
        try:
            self.root_journal.abandon(reason, self.result)
        except OSError:
            pass


def _shell_history_roots(spec: dict, workspace: str, context) -> tuple[str, ...]:
    """Validate the already-granted local roots before reading any preimage."""
    raw_roots = spec.get("history_roots") or getattr(context, "roots", ()) or (workspace,)
    roots: list[str] = []
    pinned_identities: dict[str, tuple[int, int]] = {}
    for raw_root in raw_roots:
        if isinstance(raw_root, dict):
            raw_root = raw_root.get("canonical_path") or raw_root.get("path")
        root = str(raw_root or "").strip()
        if not root:
            continue
        lexical = os.path.abspath(root)
        canonical = os.path.realpath(root)
        if lexical != canonical:
            raise ShellApprovalError(
                f"shell mutation capture rejected symlinked writable root: {root}"
            )
        try:
            root_info = os.lstat(canonical)
        except OSError as exc:
            raise ShellApprovalError(
                f"shell mutation capture requires an existing writable directory: {root}: {exc}"
            ) from exc
        if not stat.S_ISDIR(root_info.st_mode):
            raise ShellApprovalError(
                f"shell mutation capture requires an existing writable directory: {root}"
            )
        if context.root_for(canonical) is None:
            raise ShellApprovalError(
                f"shell mutation capture root is outside the authenticated grant: {root}"
            )
        if canonical in roots:
            continue
        # Secret material and symlink boundaries cannot be captured honestly for
        # an arbitrary command.  Fail before spawn instead of claiming that a
        # recursive directory manifest protects a path the policy excludes.
        for current, dirs, files in os.walk(canonical, followlinks=False):
            current_path = Path(current)
            if _history_capture._excluded_secret(str(current_path)):
                raise ShellApprovalError(
                    f"shell mutation capture rejected protected root content: {current_path}"
                )
            for name in [*dirs, *files]:
                entry = current_path / name
                if _history_capture._excluded_secret(str(entry)):
                    raise ShellApprovalError(
                        f"shell mutation capture rejected protected path: {entry}"
                    )
                if entry.is_symlink():
                    raise ShellApprovalError(
                        f"shell mutation capture rejected symlink boundary: {entry}"
                    )
        try:
            final_root_info = os.lstat(canonical)
        except OSError as exc:
            raise ShellApprovalError(
                f"shell mutation capture root changed during validation: {root}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(final_root_info.st_mode)
            or (final_root_info.st_dev, final_root_info.st_ino)
            != (root_info.st_dev, root_info.st_ino)
        ):
            raise ShellApprovalError(
                f"shell mutation capture root changed during validation: {root}"
            )
        pinned_identities[canonical] = (int(root_info.st_dev), int(root_info.st_ino))
        roots.append(canonical)

    if not roots:
        raise ShellApprovalError("shell mutation capture has no authenticated writable roots")
    # Nested roots are already covered by the outer manifest.  Keeping only
    # the least-specific root avoids duplicate payloads while retaining every
    # independently granted root when the roots are foreign siblings.
    selected: list[str] = []
    for root in sorted(roots, key=lambda value: (len(Path(value).parts), value)):
        if any(os.path.commonpath((root, existing)) == existing for existing in selected):
            continue
        selected.append(root)
    context.root_identities = tuple(
        (root, *pinned_identities[root])
        for root in selected
        if root in pinned_identities
    )
    return tuple(selected)


def _prepare_shell_capture(spec: dict, workspace: str, command: str) -> _ShellCapture | None:
    """Prepare exact root preimages for a mutating shell before child spawn."""
    mutating = _shell_command_may_mutate(spec, command)
    raw_history = spec.get("history_context")
    if not mutating:
        return None
    if not isinstance(raw_history, dict):
        # Preserve the established live-shell behavior when Lore was not
        # admitted for this request.  This is explicitly not a recovery claim.
        atomic_write_json(str(spec.get("capture_state_path") or ""), {
            "phase": "unavailable", "history_status": "unavailable",
            "reason": "no authenticated history context", "roots": [],
        }, indent=2) if spec.get("capture_state_path") else None
        return None
    raw_history = dict(raw_history)
    # These fields are supplied by the trusted launcher/spec, never by the
    # model command.  They bind the service envelope to the pinned shell call.
    for key in ("session_id", "run_id", "task_id", "tool_id"):
        if key in spec:
            value = spec.get(key)
            raw_history[key] = "" if value is None else str(value)
    context = context_from_mapping(raw_history)
    if context is None:
        if spec.get("capture_state_path"):
            atomic_write_json(str(spec["capture_state_path"]), {
                "phase": "unavailable", "history_status": "unavailable",
                "reason": "incomplete authenticated history context", "roots": [],
            }, indent=2)
        return None
    roots = _shell_history_roots(spec, workspace, context)
    action_id = str(spec.get("action_id") or "").strip()
    if not action_id:
        session = str(spec.get("session_id") or "unknown").strip()
        job_path = str(spec.get("child_pid_path") or "").strip()
        job_id = Path(job_path).stem if job_path else ""
        if not job_id:
            raise ShellApprovalError(
                "shell mutation capture requires a durable action identity before spawn"
            )
        action_id = f"shell:{session}:{job_id}"
    try:
        expected_identities = {
            root: (dev, ino)
            for root, dev, ino in getattr(context, "root_identities", ())
        }
        handle = begin_file_capture(
            roots[0],
            operation="shell",
            context=context,
            action_id=action_id,
            paths=roots[1:],
            strict_tree=True,
            expected_identities=expected_identities,
        )
    except Exception as exc:
        raise ShellApprovalError(
            f"shell mutation capture unavailable before spawn: {exc}"
        ) from exc
    if not handle.available:
        if handle.capture_phase != "unavailable":
            raise ShellApprovalError(
                "shell mutation capture unavailable before spawn: "
                + str(handle.error or handle.capture_phase)
            )
        # A configured-but-unavailable worker is truthful best-effort state,
        # not authority to prevent the already-approved live mutation.
        return _ShellCapture(
            handle, roots, 0,
            capture_state_path=str(spec.get("capture_state_path") or ""),
        )
    root_journal = None
    journal_path = str(spec.get("root_journal_path") or "").strip()
    if journal_path:
        writer_owner = WriterOwner(
            owner_id=str(spec.get("owner") or ""),
            session_id=str(spec.get("session_id") or ""),
            action_id=action_id,
            run_id=spec.get("run_id"),
            task_id=spec.get("task_id"),
            tool_id=str(spec.get("tool_id") or "shell"),
        )
        try:
            root_journal = RootJournal(journal_path)
            root_journal.open(
                action_id=action_id,
                writer_owner=writer_owner,
                roots=roots,
            )
        except Exception as exc:
            handle.finish(committed=False)
            handle.close()
            raise ShellApprovalError(
                f"shell mutation capture root journal failed before spawn: {exc}"
            ) from exc
    return _ShellCapture(
        handle,
        roots,
        max(0, int(spec.get("late_writer_grace_ms") or 250)),
        capture_state_path=str(spec.get("capture_state_path") or ""),
        root_journal=root_journal,
        writer_owner=writer_owner if root_journal is not None else None,
        reconciliation_timeout_s=max(0.0, float(spec.get("reconciliation_timeout_s") or 0)),
    )


def _shell_command_may_mutate(spec: dict, command: str) -> bool:
    """Conservatively identify writes missing from the approval classifier.

    ``destructive_actions`` is the approval surface and deliberately does not
    label every ordinary file write (for example ``touch``).  Capture uses a
    broader local mutation gate, while read-only commands retain their normal
    permission lifetime and skip a full-root snapshot.
    """
    explicit = spec.get("history_mutation")
    if isinstance(explicit, bool):
        return explicit
    if spec.get("destructive_actions"):
        return True
    # A command is read-only only when it is a single, explicitly audited
    # inspection command.  Unknown binaries, interpreters, shell syntax,
    # redirects, and every mutating git subcommand remain capture-required;
    # this prevents an executable payload from silently bypassing recovery.
    text = command.strip()
    if not text:
        return False
    if re.search(r"(?:>>?|<<|`|\$\(|\n)", text):
        return True
    # A compound command is safe only when every simple command is on the
    # inspection list.  Pipes/conditionals are split after rejecting redirects
    # and command substitution; this preserves harmless ``printf; sleep`` and
    # ``git status | head`` without treating an arbitrary executable as read.
    parts = re.split(r"\s*(?:&&|\|\||[;|])\s*", text)
    readonly = {
        "cat", "cmp", "date", "diff", "file", "head", "ls", "printf", "pwd",
        "read", "readlink", "realpath", "rg", "grep", "find", "sed", "seq",
        "sleep", "sort", "stat", "tail", "test", "true", "tr", "type", "uniq",
        "wc", "which", "whoami",
    }
    for part in parts:
        try:
            tokens = shlex.split(part, posix=os.name != "nt")
        except ValueError:
            return True
        if not tokens:
            continue
        program = os.path.basename(tokens[0]).lower()
        if program == "git":
            if len(tokens) < 2 or tokens[1].lower() not in {
                "status", "diff", "log", "show", "ls-files", "ls-tree",
                "rev-parse", "rev-list", "describe", "remote",
            }:
                return True
            continue
        if program not in readonly:
            return True
        if program == "sed" and any(t == "-i" or t.startswith("-i") for t in tokens[1:]):
            return True
    return False


def _workspace_state(workspace: str) -> dict[str, tuple[int, int, int, int, int]]:
    """Pin source identities before an overlay command can race another writer."""
    root = Path(workspace).resolve(strict=True)
    state: dict[str, tuple[int, int, int, int, int]] = {}
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if listed.returncode == 0:
        relative_paths = sorted(
            {
                value.decode("utf-8", errors="surrogateescape")
                for value in listed.stdout.split(b"\0")
                if value
            }
        )
    else:
        relative_paths = []
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = [name for name in names if name not in _POLICY_OVERLAY_SKIP
                        and not is_reference_path((Path(directory) / name).relative_to(root))]
            base = Path(directory)
            relative_paths.extend(
                (base / name).relative_to(root).as_posix() for name in files
            )
    if len(relative_paths) > _MAX_POLICY_FILES:
        raise ShellApprovalError(
            "active project policy file set exceeds the 100,000-file limit"
        )
    for relative in relative_paths:
        if is_reference_path(relative) or any(part in _POLICY_OVERLAY_SKIP for part in Path(relative).parts):
            continue
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        state[Path(relative).as_posix()] = (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )
    return state


def _overlay_candidates(workspace: str, upper: str) -> tuple[dict[str, Optional[bytes]], dict[str, int]]:
    """Decode one bubblewrap overlay upperdir into exact file candidates."""
    root = Path(workspace).resolve(strict=True)
    upper_root = Path(upper).resolve(strict=True)
    candidates: dict[str, Optional[bytes]] = {}
    modes: dict[str, int] = {}
    total = 0
    for directory, names, files in os.walk(upper_root, followlinks=False):
        current = Path(directory)
        relative_dir = current.relative_to(upper_root)
        names[:] = [name for name in names if name not in _POLICY_OVERLAY_SKIP
                        and not is_reference_path(relative_dir / name)]
        if is_reference_path(relative_dir) or any(part in _POLICY_OVERLAY_SKIP for part in relative_dir.parts):
            continue
        try:
            opaque = os.getxattr(current, "user.overlay.opaque") == b"y"
        except (AttributeError, OSError):
            opaque = False
        if opaque:
            raise ShellApprovalError(
                "active project policy cannot publish a shell directory replacement atomically"
            )
        for name in tuple(names):
            entry = current / name
            info = entry.lstat()
            relative = entry.relative_to(upper_root)
            if is_reference_path(relative) or any(part in _POLICY_OVERLAY_SKIP for part in relative.parts):
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise ShellApprovalError(
                    "active project policy supports only regular-file shell changes"
                )
        for name in files:
            entry = current / name
            relative = entry.relative_to(upper_root)
            if is_reference_path(relative) or any(part in _POLICY_OVERLAY_SKIP for part in relative.parts):
                continue
            target = root / relative
            lexical = Path(os.path.abspath(target))
            try:
                lexical.relative_to(root)
            except ValueError as exc:
                raise ShellApprovalError("shell candidate escapes the active project") from exc
            info = entry.lstat()
            key = relative.as_posix()
            if stat.S_ISCHR(info.st_mode):
                if target.is_dir():
                    raise ShellApprovalError(
                        "active project policy cannot publish a shell directory deletion atomically"
                    )
                candidates[key] = None
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ShellApprovalError(
                    "active project policy supports only regular-file shell changes"
                )
            total += int(info.st_size)
            if total > _MAX_POLICY_CHANGE_BYTES:
                raise ShellApprovalError("shell candidate set exceeds the 256 MiB limit")
            candidates[key] = entry.read_bytes()
            modes[key] = stat.S_IMODE(info.st_mode)
    return candidates, modes


def _publish_overlay(
    workspace: str,
    upper: str,
    spec: dict,
    baseline: dict[str, tuple[int, int, int, int, int]],
) -> None:
    """Validate and atomically publish all regular-file overlay changes."""
    candidates, modes = _overlay_candidates(workspace, upper)
    _publish_candidate_set(workspace, candidates, modes, spec, baseline)


def _publish_candidate_set(workspace, candidates, modes, spec, baseline, *, before_commit=None):
    from src.project_hex import validate_registered_project_candidates

    if not candidates:
        if before_commit is not None:
            before_commit()
        return
    result = validate_registered_project_candidates(
        owner=str(spec.get("owner") or ""),
        workspace=workspace,
        db_path=str(spec.get("hex_db_path") or ""),
        candidates=candidates,
    )
    if not result.get("allowed"):
        raise ShellApprovalError(
            "active project policy rejected shell changes: "
            + json.dumps(result.get("findings") or [], ensure_ascii=False)[:4096]
        )
    root = Path(workspace).resolve(strict=True)
    history_context = None
    raw_history = spec.get("history_context")
    if isinstance(raw_history, dict):
        history_context = context_from_mapping(raw_history)
    changes: list[AtomicFileChange] = []
    for relative, payload in candidates.items():
        target = root / relative
        lexical = Path(os.path.abspath(target))
        resolved = target.resolve(strict=False)
        if resolved != lexical:
            raise ShellApprovalError("shell candidate traverses a symlink")
        expected_state = baseline.get(relative)
        try:
            current_info = target.lstat()
        except FileNotFoundError:
            current_state = None
        else:
            current_state = (
                int(current_info.st_dev),
                int(current_info.st_ino),
                int(current_info.st_size),
                int(current_info.st_mtime_ns),
                int(current_info.st_ctime_ns),
            )
        if current_state != expected_state:
            raise ShellApprovalError(
                "project source changed while the isolated shell command was running"
            )
        before = file_fingerprint(str(target))
        if payload is None and before is None:
            continue
        changes.append(
            AtomicFileChange(
                str(target),
                payload,
                expected_fingerprint=before,
                require_missing=payload is not None and before is None,
                mode=modes.get(relative),
                history_context=history_context,
                require_history=history_context is not None,
            )
        )
    if changes:
        if before_commit is not None:
            before_commit()
        atomic_write_batch(changes)


def _run_hex_check(
    resolution: HexResolution,
    *,
    snapshot: BinaryIO,
    spec: dict,
    workspace: str,
    cwd: str,
) -> None:
    result = validate_project_file_candidates(
        resolution,
        owner=str(spec.get("owner") or ""),
        project_id=str(spec.get("project_id") or ""),
        db_path=str(spec.get("hex_db_path") or ""),
        candidates={},
        stage="check",
    )
    if not result.get("allowed"):
        raise ShellApprovalError(
            "active Open Clank Hexes contract rejected the project worker: "
            + redact_text(json.dumps(result.get("findings") or [], ensure_ascii=False))[:4096]
        )


def _load_spec(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def run(spec_path: str) -> int:
    spec = _load_spec(Path(spec_path))
    command_size = int(spec["command_size"])
    if command_size < 0 or command_size > _MAX_COMMAND_BYTES:
        raise ValueError("invalid background shell command size")
    raw_command = sys.stdin.buffer.read(command_size + 1)
    if len(raw_command) != command_size:
        raise ValueError("background shell command transport was incomplete")
    command = raw_command.decode("utf-8")
    workspace = os.path.realpath(str(spec["workspace"]))
    cwd = os.path.realpath(str(spec.get("cwd") or workspace))
    log_path = Path(spec["log_path"])
    exit_path = Path(spec["exit_path"])
    child_pid_path = Path(spec["child_pid_path"])
    stdin_path = str(spec["stdin_path"])
    stdin_ready_path = Path(
        spec.get("stdin_ready_path") or f"{spec['stdin_path']}.ready"
    )
    shell = str(spec["shell"])
    network = str(spec.get("network") or "enabled")

    if os.name == "nt" and spec.get("hex_check"):
        from src.openclank.windows_shell_worker import run_native_shell
        return run_native_shell(
            spec, command, capture_factory=_prepare_shell_capture,
            publish=_publish_candidate_set,
            excluded=lambda value: is_reference_path(value)
                or any(part in _POLICY_OVERLAY_SKIP for part in Path(value).parts),
            audit=append_shell_audit,
        )

    pipe_listener = None
    if os.name != "nt":
        fifo_path = Path(stdin_path)
        fifo_path.parent.mkdir(parents=True, exist_ok=True)
        if not fifo_path.exists():
            os.mkfifo(fifo_path, 0o600)
        stdin_handle = open(fifo_path, "r+b", buffering=0)
    else:
        from multiprocessing.connection import Listener

        pipe_listener = Listener(
            stdin_path,
            family="AF_PIPE",
            authkey=None,
        )
        stdin_handle = subprocess.PIPE
    atomic_write_text(str(stdin_ready_path), "ready")
    # The approval binding below is computed on `command` exactly as the owner
    # approved it; the askpass rewrite only shapes the argv we execute.
    exec_command = inject_sudo_askpass(command) if spec.get("sudo_askpass") else command
    base_argv = shell_command_argv(shell, exec_command)
    snapshot_context = None
    snapshot = None
    overlay_context = None
    overlay_upper = None
    overlay_work = None
    workspace_baseline = None
    shell_capture: _ShellCapture | None = None
    child_started = False
    if spec.get("hex_check"):
        resolution = require_hex_activation(
            spec.get("hex_target") or workspace,
            owner=str(spec.get("owner") or ""),
            project_id=str(spec.get("project_id") or ""),
            db_path=str(spec.get("hex_db_path") or ""),
            workspace_root=workspace,
        )
        # A workspace without a declared project contract remains a valid
        # ordinary shell workspace. If an Open Clank `.hex` contract exists, the
        # active hash is mandatory and the exact contract is checked inside a
        # network-disabled worker before the user command starts. The same
        # verified bytes stay mounted for the user command's sandbox.
        if resolution.contract_path:
            require_executable_trust(
                resolution,
                owner=str(spec.get("owner") or ""),
                project_id=str(spec.get("project_id") or ""),
                db_path=str(spec.get("hex_db_path") or ""),
            )
            snapshot_context = verified_contract_snapshot(resolution)
            snapshot = snapshot_context.__enter__()
            overlay_context = tempfile.TemporaryDirectory(
                prefix="open-clank-shell-policy-"
            )
            overlay_root = Path(overlay_context.name)
            overlay_upper = overlay_root / "upper"
            overlay_work = overlay_root / "work"
            overlay_upper.mkdir()
            overlay_work.mkdir()
            try:
                argv, containment = contained_argv(
                    base_argv,
                    workspace=workspace,
                    cwd=cwd,
                    network=network,
                    owner=spec.get("owner"),
                    project_id=spec.get("project_id"),
                    hex_target=spec.get("hex_target") or workspace,
                    hex_db_path=spec.get("hex_db_path"),
                    workspace_overlay=(str(overlay_upper), str(overlay_work)),
                    readonly_data_mounts=((snapshot.fileno(), resolution.contract_path),),
                )
                _run_hex_check(
                    resolution,
                    snapshot=snapshot,
                    spec=spec,
                    workspace=workspace,
                    cwd=cwd,
                )
                snapshot.seek(0)
                workspace_baseline = _workspace_state(workspace)
            except BaseException:
                snapshot_context.__exit__(*sys.exc_info())
                snapshot_context = None
                snapshot = None
                raise
    if snapshot is None:
        argv, containment = contained_argv(
            base_argv,
            workspace=workspace,
            cwd=cwd,
            network=network,
            owner=spec.get("owner"),
            project_id=spec.get("project_id"),
            hex_target=spec.get("hex_target") or workspace,
            hex_db_path=spec.get("hex_db_path"),
        )
    actions = list(spec.get("destructive_actions") or ())
    approval_mismatch = bool(actions) and spec.get(
        "approval_binding"
    ) != shell_approval_binding(
        command,
        cwd=cwd,
        containment=containment,
        network=network,
        workspace=workspace,
    )
    child: Optional[subprocess.Popen] = None
    input_thread: Optional[threading.Thread] = None
    input_stop = threading.Event()

    def stop_child(_signum=None, _frame=None):
        if child is not None:
            kill_process_tree(child.pid)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop_child)

    try:
        if approval_mismatch:
            raise ShellApprovalError(
                "destructive background approval no longer matches the execution tuple"
            )
        # This is the durable prepare boundary.  It runs after exact approval
        # and containment have been recomputed, but before the child exists.
        # Reads leave the existing authorization untouched and do not create a
        # history action. A configured, available capture has exact writable-
        # root coverage; unavailable Lore remains an explicit best-effort
        # execution state rather than an invented recovery receipt.
        shell_capture = _prepare_shell_capture(spec, workspace, command)
        if shell_capture is not None:
            shell_capture.process_group_id = None
            shell_capture._state("prepared")
        try:
            child_env = minimal_shell_env(cwd=workspace)
            # Askpass delivery (sudo escalation): the launcher puts these in the
            # worker's own environment; forward them to the child shell. The
            # StreamingRedactor masks the secret's exact value in the job log.
            for name in ("SUDO_ASKPASS", "OPEN_CLANK_SUDO_SECRET"):
                if os.environ.get(name):
                    child_env[name] = os.environ[name]
            child = subprocess.Popen(
                argv,
                stdin=stdin_handle,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env=child_env,
                start_new_session=True if os.name != "nt" else False,
                pass_fds=(snapshot.fileno(),) if snapshot is not None else (),
            )
            child_started = True
            if shell_capture is not None:
                shell_capture.process_group_id = child.pid if os.name != "nt" else None
                shell_capture._state("running")
        finally:
            if snapshot_context is not None:
                snapshot_context.__exit__(*sys.exc_info())
                snapshot_context = None
                snapshot = None
        if pipe_listener is not None:
            def pump_windows_input():
                while not input_stop.is_set() and child is not None and child.poll() is None:
                    try:
                        connection = pipe_listener.accept()
                        try:
                            payload = connection.recv_bytes()
                        finally:
                            connection.close()
                        if payload and child.stdin is not None:
                            child.stdin.write(payload)
                            child.stdin.flush()
                    except (BrokenPipeError, EOFError, OSError):
                        if child is None or child.poll() is not None:
                            return

            input_thread = threading.Thread(
                target=pump_windows_input,
                name="open-clank-shell-stdin",
                daemon=True,
            )
            input_thread.start()
        atomic_write_text(str(child_pid_path), str(child.pid))
        written = 0
        capped = False
        redactor = StreamingRedactor()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "wb") as log:
            assert child.stdout is not None
            while True:
                chunk = child.stdout.read(8192)
                if not chunk:
                    break
                safe = redactor.feed(
                    decoder.decode(chunk)
                ).encode("utf-8")
                remaining = max(0, _MAX_LOG_BYTES - written)
                keep = safe[:remaining]
                if keep:
                    log.write(keep)
                    log.flush()
                    written += len(keep)
                if len(keep) < len(safe):
                    capped = True
            decoded_tail = decoder.decode(b"", final=True)
            final = (
                redactor.feed(decoded_tail) + redactor.finish()
            ).encode("utf-8")
            remaining = max(0, _MAX_LOG_BYTES - written)
            keep = final[:remaining]
            if keep:
                log.write(keep)
                written += len(keep)
            if len(keep) < len(final):
                capped = True
            if capped and written + len(_TRUNCATED) <= _MAX_LOG_BYTES + len(_TRUNCATED):
                log.write(_TRUNCATED)
            log.flush()
            os.fsync(log.fileno())
        code = child.wait()
        if overlay_upper is not None:
            _publish_overlay(
                workspace,
                str(overlay_upper),
                spec,
                workspace_baseline or {},
            )
        if shell_capture is not None:
            capture_result = shell_capture.finish(committed=True)
            if capture_result.get("history_status") not in {"complete", "paused", "unavailable", "unconfigured"}:
                raise ShellApprovalError(
                    "shell mutation completed with pending history reconciliation: "
                    + str(capture_result.get("error") or capture_result.get("capture_phase"))
                )
    except BaseException as exc:
        stop_child()
        if shell_capture is not None:
            # A cancelled or failed command may have committed a partial write;
            # reconcile it when a child existed, otherwise mark the prepared
            # action not-committed.  Both paths are idempotent and close the
            # capture token, including Popen failures.
            try:
                shell_capture.finish(committed=child_started)
            except Exception:
                pass
        code = 1
        error = (
            "\nworker error: " + redact_text(exc) + "\n"
        ).encode("utf-8")[:4096]
        with open(log_path, "a+b") as log:
            log.seek(0, os.SEEK_END)
            if log.tell() + len(error) > _MAX_LOG_BYTES:
                log.seek(max(0, _MAX_LOG_BYTES - len(error)))
                log.truncate()
            log.write(error)
    finally:
        if snapshot_context is not None:
            snapshot_context.__exit__(None, None, None)
        if overlay_context is not None:
            try:
                os.chmod(Path(overlay_work) / "work", 0o700)
            except OSError:
                pass
            overlay_context.cleanup()
        input_stop.set()
        if pipe_listener is not None:
            pipe_listener.close()
        if child is not None and child.stdin is not None:
            child.stdin.close()
        if (
            hasattr(stdin_handle, "close")
            and (child is None or stdin_handle is not child.stdin)
        ):
            stdin_handle.close()
        try:
            child_pid_path.unlink()
        except FileNotFoundError:
            pass
        try:
            stdin_ready_path.unlink()
        except FileNotFoundError:
            pass
    atomic_write_text(str(exit_path), str(code))
    append_shell_audit(
        command=command,
        owner=spec.get("owner"),
        session_id=spec.get("session_id"),
        workspace=workspace,
        containment=containment,
        network=network,
        actions=actions,
        event="exit",
    )
    return code


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
