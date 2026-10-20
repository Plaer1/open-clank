"""Background job execution for the agent's `bash` tool.

Long commands (installs, ffmpeg, model downloads) should NOT block the chat
stream — a multi-minute held SSE connection is fragile (model-stops-early,
timeouts, tab suspend). Instead we launch them **detached** and let an
always-on monitor re-invoke the agent when they finish ("auto-continue").

Design goals:
  * Restart-safe: status is derived from an on-disk exit-code file, not a live
    PID, so a uvicorn restart never loses a job or its result.
  * Idempotent follow-up: a job stays {done, followed_up: False} until the
    agent has actually been re-invoked, so completion can never silently
    "do nothing" — the monitor retries on the next tick.
  * Bounded: a hard max-runtime marks a runaway job failed and STILL triggers
    a follow-up ("timed out"), so you always hear back.

This module only owns launch + state. The monitor / agent re-invocation lives
in the caller (so this stays import-light and unit-testable).
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import (
    detached_popen_kwargs,
    find_bash,
    kill_process_tree,
    pid_alive,
)

from src.constants import BG_JOBS_DIR, BG_JOBS_FILE
from src.shell_policy import (
    ShellApprovalError,
    append_shell_audit,
    contained_argv,
    destructive_actions,
    minimal_shell_env,
    redact_text,
    shell_approval_binding,
    shell_command_argv,
)


def history_context_mapping(value: Any) -> Optional[Dict[str, Any]]:
    """Serialize trusted capture context for the detached overlay worker."""
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    actor_id = str(getattr(value, "actor_id", "") or "")
    account_id = str(getattr(value, "account_id", "") or "")
    workspace_id = str(getattr(value, "workspace_id", "") or "")
    roots = list(getattr(value, "roots", ()) or ())
    socket_path = str(getattr(value, "socket_path", "") or "")
    token = str(getattr(value, "token", "") or "")
    session_id = str(getattr(value, "session_id", "") or "")
    run_id = str(getattr(value, "run_id", "") or "")
    task_id = str(getattr(value, "task_id", "") or "")
    tool_id = str(getattr(value, "tool_id", "") or "")
    if not actor_id or not account_id or not workspace_id or not roots:
        return None
    result = {
        "history_capture": True,
        "actor_id": actor_id,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "history_socket": socket_path,
        "history_token": token,
        "history_roots": roots,
    }
    for key, item in (("session_id", session_id), ("run_id", run_id), ("task_id", task_id), ("tool_id", tool_id)):
        if item:
            result[key] = item
    return result

_JOBS_DIR = Path(BG_JOBS_DIR)
_STORE = Path(BG_JOBS_FILE)

# A job that runs longer than this is presumed stuck and reaped (the agent
# still gets a "timed out" follow-up so nothing hangs forever).
DEFAULT_MAX_RUNTIME_S = 3600  # 1 hour
_ACTIVE_STATUSES = frozenset({"running", "reconciling"})
_TERMINAL_STATUSES = frozenset({"done", "failed"})


def is_active(rec: Dict[str, Any]) -> bool:
    """Whether a job can still own a child or capture reconciliation."""
    return str(rec.get("status") or "") in _ACTIVE_STATUSES
# Cap how much captured output we keep / feed back to the model.
_MAX_OUTPUT_CHARS = 16000
# How long a finished-and-followed-up job (record + its .sh/.cmd.sh/.log/.exit
# files) is kept before pruning, so neither the store nor data/bg_jobs/ grows
# without bound. The agent has already consumed the result by then.
_RETENTION_S = 3600  # 1 hour after follow-up
_TERMINAL_RETENTION_S = 24 * 3600
_FOLLOWUP_CLAIM_LEASE_S = 20 * 60
_FOLLOWUP_MAX_ATTEMPTS = 3
_MAX_COMMAND_BYTES = 1024 * 1024


@contextmanager
def _store_lock():
    """Cross-process lock for read/modify/write follow-up transitions."""
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _STORE.with_suffix(_STORE.suffix + ".lock")
    with open(lock_path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        if _STORE.exists():
            data = json.loads(_STORE.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return {}
            return {str(job_id): rec for job_id, rec in data.items() if isinstance(rec, dict)}
    except Exception:
        pass
    return {}


def _save(jobs: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(str(_STORE), jobs, indent=2)


def _pid_alive(pid: Optional[int]) -> bool:
    # Delegates to the platform-safe probe. NB: a bare os.kill(pid, 0) is unsafe
    # on Windows — CPython routes it to TerminateProcess, which would KILL the
    # job we're only trying to check. core.platform_compat.pid_alive handles
    # both OSes correctly.
    return pid_alive(pid)


def launch(
    command: str,
    session_id: str,
    cwd: Optional[str] = None,
    max_runtime_s: int = DEFAULT_MAX_RUNTIME_S,
    *,
    owner: Optional[str] = None,
    workspace: Optional[str] = None,
    network: Optional[str] = None,
    approval_binding: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    history_context: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    action_id: Optional[str] = None,
    writable_roots: Optional[List[str]] = None,
    tool_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Launch `command` detached. Returns the job record (status='running').

    Output + the final exit code are written to files so status survives a
    server restart. The process is put in its own session (setsid) so it
    outlives the request/stream that started it.
    """
    scope_session = str(session_id or "")
    scope_owner = str(owner or "")
    scope_workspace = str(workspace or "")
    if not scope_session or not scope_owner or not scope_workspace:
        raise ValueError(
            "background jobs require an authenticated owner, chat session, and workspace"
        )

    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    durable_action_id = str(action_id or f"shell:{scope_session}:{job_id}").strip()
    if not durable_action_id:
        raise ValueError("background shell commands require a durable action identity")
    durable_run_id = str(run_id or (history_context or {}).get("run_id") or "").strip() or None
    durable_task_id = str(task_id or (history_context or {}).get("task_id") or "").strip() or None
    durable_tool_id = str(tool_id or (history_context or {}).get("tool_id") or "shell").strip()
    log_path = _JOBS_DIR / f"{job_id}.log"
    exit_path = _JOBS_DIR / f"{job_id}.exit"
    child_pid_path = _JOBS_DIR / f"{job_id}.child.pid"
    stdin_path = (
        rf"\\.\pipe\open-clank-shell-{job_id}"
        if os.name == "nt"
        else str(_JOBS_DIR / f"{job_id}.stdin")
    )
    stdin_ready_path = _JOBS_DIR / f"{job_id}.stdin.ready"
    spec_path = _JOBS_DIR / f"{job_id}.spec.json"
    capture_state_path = _JOBS_DIR / f"{job_id}.capture.json"
    native_cancel_path = _JOBS_DIR / f"{job_id}.native-cancel.json"
    root_journal_path = _JOBS_DIR / f"{job_id}.root-journal.json"
    active_workspace = os.path.realpath(scope_workspace)
    active_cwd = os.path.realpath(cwd or active_workspace)
    shell = (
        os.getenv("OPEN_CLANK_SHELL")
        or find_bash()
        or os.environ.get("ComSpec", "cmd.exe")
    )
    from src.constants import FM_DB_PATH
    from src.project_hex import HexResolutionError, require_project_mutation_admission

    project_id = None
    hex_check = False
    try:
        project = require_project_mutation_admission(
            owner=scope_owner,
            workspace=active_workspace,
            db_path=FM_DB_PATH,
        )
        if project:
            import sqlite3

            with sqlite3.connect(FM_DB_PATH, timeout=30) as conn:
                row = conn.execute(
                    "SELECT state FROM fm_v2_policy_projections "
                    "WHERE owner_id=? AND project_id=?",
                    (scope_owner, project["project_id"]),
                ).fetchone()
            if row and row[0] == "active":
                project_id = str(project["project_id"])
                hex_check = True
    except HexResolutionError as exc:
        raise ShellApprovalError(f"active project policy is unavailable: {exc}") from exc
    actions = destructive_actions(command)
    if hex_check and os.name == "nt":
        from src.project_hex import require_hex_activation
        from src.openclank.windows_shell_worker import system_shell, CONTAINMENT
        require_hex_activation(
            active_workspace, owner=scope_owner, project_id=project_id,
            db_path=FM_DB_PATH, workspace_root=active_workspace,
        )
        try:
            if os.path.commonpath([active_workspace, active_cwd]) != active_workspace:
                raise ShellApprovalError("shell cwd must stay inside the active workspace")
        except ValueError as exc:
            raise ShellApprovalError("shell cwd must stay inside the active workspace") from exc
        native_shell = str(system_shell()[0])
        override = os.getenv("OPEN_CLANK_SHELL")
        if override and os.path.normcase(override) not in {
            os.path.normcase(native_shell), "powershell", "powershell.exe"
        }:
            raise ShellApprovalError("active native Hex shell supports OS PowerShell only")
        shell = native_shell
        containment = CONTAINMENT
    else:
        _, containment = contained_argv(
            shell_command_argv(shell, "exit 0" if os.name == "nt" else "true"),
            workspace=active_workspace,
            cwd=active_cwd,
            network=network,
            owner=scope_owner,
            project_id=project_id,
            hex_target=active_workspace,
            hex_db_path=FM_DB_PATH,
        )
        if hex_check:
            containment = "bwrap-overlay"
    network_mode = str(
        network or os.getenv("OPEN_CLANK_SHELL_NETWORK", "enabled")
    ).lower()
    expected_binding = shell_approval_binding(
        command,
        cwd=active_cwd,
        containment=containment,
        network=network_mode,
        workspace=active_workspace,
    )
    if actions and approval_binding != expected_binding:
        raise ShellApprovalError(
            "destructive background command needs approval for its exact execution tuple"
        )
    command_bytes = command.encode("utf-8")
    if len(command_bytes) > _MAX_COMMAND_BYTES:
        raise ValueError("background shell command exceeds 1 MiB")
    spec = {
        "job_id": job_id,
        "command_size": len(command_bytes),
        "workspace": active_workspace,
        "cwd": active_cwd,
        "log_path": str(log_path),
        "exit_path": str(exit_path),
        "child_pid_path": str(child_pid_path),
        "stdin_path": stdin_path,
        "stdin_ready_path": str(stdin_ready_path),
        "shell": shell,
        "network": network_mode,
        "owner": scope_owner,
        "session_id": scope_session,
        "run_id": durable_run_id,
        "task_id": durable_task_id,
        "action_id": durable_action_id,
        "tool_id": durable_tool_id,
        "project_id": project_id,
        "hex_target": active_workspace,
        "hex_db_path": FM_DB_PATH,
        "hex_check": hex_check,
        "native_shell_owner": bool(os.name == "nt" and hex_check),
        "destructive_actions": actions,
        "approval_binding": approval_binding if actions else None,
        # Non-secret transport flag: the worker applies the sudo -A askpass
        # rewrite itself, AFTER the binding check, so approval always binds to
        # the exact command the owner approved.
        "sudo_askpass": bool(extra_env and extra_env.get("SUDO_ASKPASS")),
        "history_context": dict(history_context) if history_context else None,
        # The caller supplies the same roots already covered by its external
        # directory grant.  The worker validates them against that context;
        # this list never expands authority on its own.
        "history_roots": list(
            (history_context or {}).get("history_roots", [])
            if isinstance(history_context, dict)
            else []
        ) + [str(root) for root in (writable_roots or [])],
        "capture_state_path": str(capture_state_path),
        "native_cancel_path": str(native_cancel_path),
        "root_journal_path": str(root_journal_path),
        # The worker holds the authenticated batch handle through descendant
        # reconciliation.  This bounds that real continuation at the same
        # durable job deadline used by refresh().
        "reconciliation_timeout_s": max(1, int(max_runtime_s)),
    }
    atomic_write_json(str(spec_path), spec)
    if os.name != "nt":
        os.chmod(spec_path, 0o600)
    project_root = str(Path(__file__).resolve().parent.parent)
    argv = [sys.executable, "-m", "src.shell_worker", str(spec_path)]

    worker_env = minimal_shell_env(cwd=active_workspace)
    for name in ("OPEN_CLANK_DATA_DIR", "OPEN_CLANK_SHELL_SANDBOX"):
        if os.environ.get(name):
            worker_env[name] = os.environ[name]
    if extra_env:
        # Merged after minimal_shell_env so allowlisted filtering cannot strip
        # caller-provided values (e.g. the sudo askpass secret). Nothing here is
        # written to the spec or the audit log.
        worker_env.update({str(key): str(value) for key, value in extra_env.items()})
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE,
            cwd=project_root,
            env=worker_env,
            **detached_popen_kwargs(),  # detach from the request lifecycle (setsid / DETACHED_PROCESS)
        )
        assert proc.stdin is not None
        proc.stdin.write(command_bytes)
        proc.stdin.close()
    except BaseException:
        try:
            spec_path.unlink()
        except FileNotFoundError:
            pass
        if "proc" in locals():
            kill_process_tree(proc.pid)
        raise
    append_shell_audit(
        command=command,
        owner=scope_owner,
        session_id=scope_session,
        workspace=active_workspace,
        containment=containment,
        network=network_mode,
        actions=actions,
    )

    rec = {
        "id": job_id,
        "session_id": scope_session,
        "owner": scope_owner,
        "workspace": active_workspace,
        "command": redact_text(command),
        "destructive_actions": actions,
        "containment": containment,
        "network": network_mode,
        "status": "running",       # running | reconciling | done | failed
        "pid": proc.pid,
        "started_at": time.time(),
        "ended_at": None,
        "exit_code": None,
        "max_runtime_s": max_runtime_s,
        "followed_up": False,       # has the agent been re-invoked with the result?
        "log_path": str(log_path),
        "exit_path": str(exit_path),
        "child_pid_path": str(child_pid_path),
        "stdin_path": stdin_path,
        "stdin_ready_path": str(stdin_ready_path),
        "capture_state_path": str(capture_state_path),
        "native_cancel_path": str(native_cancel_path),
        "root_journal_path": str(root_journal_path),
        "action_id": durable_action_id,
        "run_id": durable_run_id,
        "task_id": durable_task_id,
        "tool_id": durable_tool_id,
        "capture_phase": "not_started",
        "native_shell_owner": bool(os.name == "nt" and hex_check),
    }
    with _store_lock():
        jobs = _load()
        jobs[job_id] = rec
        _save(jobs)
    return rec


def _read_output(rec: Dict[str, Any]) -> str:
    try:
        txt = Path(rec["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(txt) > _MAX_OUTPUT_CHARS:
        # Keep head + tail — the interesting bits are usually at both ends.
        head = txt[: _MAX_OUTPUT_CHARS // 2]
        tail = txt[-_MAX_OUTPUT_CHARS // 2:]
        txt = head + "\n…[truncated]…\n" + tail
    return redact_text(txt)


def _owned_paths(job_id: str) -> List[Path]:
    """Resolve one job's files without treating its durable ID as a glob."""
    prefix = f"{job_id}."
    try:
        return [
            path
            for path in _JOBS_DIR.iterdir()
            if path.name == job_id or path.name.startswith(prefix)
        ]
    except FileNotFoundError:
        return []


def _prune(jobs: Dict[str, Dict[str, Any]], now: float) -> bool:
    """Drop records (and their on-disk files) for jobs that finished, were
    followed up, and are older than the retention window. Mutates `jobs`."""
    stale = [
        jid
        for jid, rec in jobs.items()
        if rec.get("ended_at")
        and (
            (rec.get("followed_up") and (now - rec["ended_at"]) > _RETENTION_S)
            or (now - rec["ended_at"]) > _TERMINAL_RETENTION_S
        )
    ]
    for jid in stale:
        jobs.pop(jid, None)
        for p in _owned_paths(jid):
            try:
                p.unlink()
            except Exception:
                pass
    return bool(stale)


def _cleanup_orphans(jobs: Dict[str, Dict[str, Any]], now: float) -> None:
    """Remove old spill/session files that no longer have a durable record."""
    known = set(jobs)
    try:
        paths = list(_JOBS_DIR.iterdir())
    except FileNotFoundError:
        return
    for path in paths:
        job_id = path.name.split(".", 1)[0]
        if job_id in known:
            continue
        try:
            if now - path.stat().st_mtime > _TERMINAL_RETENTION_S:
                path.unlink()
        except FileNotFoundError:
            pass


def _capture_state(rec: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(rec.get("capture_state_path", ""))
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def _capture_phase(rec: Dict[str, Any]) -> str:
    state = _capture_state(rec)
    return str(state.get("phase") or rec.get("capture_phase") or "not_started")


def _record_capture_phase(rec: Dict[str, Any]) -> str:
    phase = _capture_phase(rec)
    rec["capture_phase"] = phase
    return phase


def _abandon_root_journal(rec: Dict[str, Any], *, reason: str) -> None:
    """Terminalize an open root journal honestly after worker loss."""
    path = str(rec.get("root_journal_path") or "")
    if not path:
        return
    from src.openclank.history_capture import RootJournal, RootJournalError, WriterOwner

    journal = RootJournal(path)
    state = journal.read()
    if state is None:
        # root_journal_path is recorded unconditionally; only an actually
        # opened journal may be abandoned (never mint a phantom terminal).
        return
    result = rec.get("capture_result") if isinstance(rec.get("capture_result"), dict) else None
    try:
        journal.claim_recovery(WriterOwner(
            owner_id=str(rec.get("owner") or ""),
            session_id=str(rec.get("session_id") or ""),
            action_id=str(rec.get("action_id") or state.get("action_id") or ""),
            run_id=rec.get("run_id"),
            task_id=rec.get("task_id"),
            tool_id=str(rec.get("tool_id") or "shell"),
        ))
        journal.abandon(reason, result)
    except (OSError, ValueError, RootJournalError):
        pass


def refresh() -> Dict[str, Dict[str, Any]]:
    """Reconcile every running job against disk. Marks done/failed (incl.
    timeout). Idempotent — safe to call from a poll loop. Returns the store."""
    with _store_lock():
        jobs = _load()
        changed = False
        now = time.time()
        for rec in jobs.values():
            if not is_active(rec):
                continue
            exit_path = Path(rec.get("exit_path", ""))
            if exit_path.exists():
                phase = _record_capture_phase(rec)
                # A worker that still has descendants or an unstable root
                # leaves an explicit reconciling state.  Never turn that into
                # a terminal success merely because its wrapper exited.
                if phase in {"pending", "reconciling", "running", "prepared"}:
                    # The worker has already closed its capture handle. There
                    # is no owner left to resume this action, so preserve the
                    # durable failure and make the job terminal instead of
                    # creating an immortal reconciling record.
                    rec["status"] = "failed"
                    rec["capture_phase"] = "after_failed"
                    rec["capture_error"] = (
                        "capture worker exited before after-state reconciliation completed"
                    )
                    state = _capture_state(rec)
                    if isinstance(state.get("result"), dict):
                        rec["capture_result"] = dict(state["result"])
                    _abandon_root_journal(
                        rec,
                        reason="capture worker exited before after-state reconciliation completed",
                    )
                    rec["ended_at"] = now
                    try:
                        code = int(exit_path.read_text(encoding="utf-8", errors="replace").strip() or "1")
                    except Exception:
                        code = 1
                    rec["exit_code"] = code
                    rec["signal"] = -code if code < 0 else None
                    changed = True
                    continue
                try:
                    code = int(exit_path.read_text(encoding="utf-8", errors="replace").strip() or "1")
                except Exception:
                    code = 1
                rec["exit_code"] = code
                rec["status"] = "done" if code == 0 else "failed"
                rec["signal"] = -code if code < 0 else None
                rec["ended_at"] = now
                changed = True
            elif (now - rec.get("started_at", now)) > rec.get("max_runtime_s", DEFAULT_MAX_RUNTIME_S):
                _kill_record(rec)
                rec["timed_out"] = True
                rec["exit_code"] = -1
                rec["signal"] = signal.SIGTERM if os.name != "nt" else None
                if not _pid_alive(rec.get("pid")) and not exit_path.exists():
                    # Timed-out dead workers take the same honest abandon path
                    # as vanished workers; never invent success and never leave
                    # an open journal behind in immortal reconciling.
                    rec["status"] = "failed"
                    rec["ended_at"] = now
                    rec["died"] = True
                    _abandon_root_journal(
                        rec,
                        reason="capture worker timed out before after-state reconciliation completed",
                    )
                else:
                    rec["status"] = "reconciling"
                    rec["ended_at"] = None
                changed = True
            elif not _pid_alive(rec.get("pid")) and not exit_path.exists():
                rec["status"] = "failed"
                rec["exit_code"] = -1
                rec["ended_at"] = now
                rec["died"] = True
                _abandon_root_journal(
                    rec,
                    reason="capture worker died before after-state reconciliation completed",
                )
                changed = True
        if _prune(jobs, now):
            changed = True
        _cleanup_orphans(jobs, now)
        if changed:
            _save(jobs)
        return jobs


def _kill(pid: Optional[int]) -> None:
    kill_process_tree(pid)


def _kill_record(rec: Dict[str, Any]) -> None:
    """Ask the worker to stop so it can record partial history first."""
    if os.name == "nt" and rec.get("native_shell_owner"):
        endpoint = Path(rec.get("native_cancel_path") or "")
        if not endpoint.is_file():
            raise ShellApprovalError("native shell cancellation endpoint unavailable; owner identity/cleanup unverified")
        if endpoint.exists():
            from src.openclank.windows_shell_cancel import cancel_owner
            rec["native_cancel_result"] = cancel_owner(
                endpoint, job_id=str(rec["id"]),
                action_id=str(rec.get("action_id") or ""),
                owner=str(rec.get("owner") or ""), worker_pid=int(rec["pid"]),
            )
            # Only the durable owner can stop its private Job and reconcile.
            # A forced owner exit is last-handle-close error recovery, never a
            # claim that capture/promotion or whole-tree accounting completed.
            return
    child_pid = None
    try:
        child_pid = int(
            Path(rec.get("child_pid_path", "")).read_text(encoding="utf-8").strip()
        )
    except (OSError, TypeError, ValueError):
        pass
    worker_pid = rec.get("pid")
    if worker_pid and os.name != "nt":
        try:
            os.kill(int(worker_pid), signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _pid_alive(worker_pid):
            time.sleep(0.05)
    elif worker_pid:
        _kill(worker_pid)
    # The worker's signal handler normally kills this group.  This fallback
    # handles a dead worker and ensures a timeout cannot leave a writer alive.
    if child_pid is not None and _pid_alive(child_pid):
        _kill(child_pid)
    if worker_pid and _pid_alive(worker_pid):
        _kill(worker_pid)


def pending_followups() -> List[Dict[str, Any]]:
    """Finished jobs the agent hasn't been re-invoked for yet. The monitor
    drains these; mark_followed_up() flips the flag only on success."""
    jobs = refresh()
    return [r for r in jobs.values()
            if r.get("status") in _TERMINAL_STATUSES and not r.get("followed_up")]


def claim_pending_followups(claimant: str) -> List[Dict[str, Any]]:
    """Atomically lease ready follow-ups to one monitor process."""
    refresh()
    now = time.time()
    claimed = []
    with _store_lock():
        jobs = _load()
        for rec in jobs.values():
            if rec.get("status") not in _TERMINAL_STATUSES or rec.get("followed_up"):
                continue
            if rec.get("followup_state") == "failed":
                continue
            if float(rec.get("next_followup_at") or 0) > now:
                continue
            claimed_at = float(rec.get("followup_claimed_at") or 0)
            if rec.get("followup_state") == "claimed" and now - claimed_at < _FOLLOWUP_CLAIM_LEASE_S:
                continue
            attempts = int(rec.get("followup_attempts") or 0)
            if attempts >= _FOLLOWUP_MAX_ATTEMPTS:
                rec.update({
                    "followup_state": "failed",
                    "followed_up": True,
                    "followup_error": rec.get("followup_error") or "Follow-up retry budget exhausted",
                })
                continue
            token = f"{claimant}:{uuid.uuid4().hex}"
            rec.update({
                "followup_state": "claimed",
                "followup_claim": token,
                "followup_claimed_at": now,
                "followup_attempts": attempts + 1,
            })
            item = dict(rec)
            item["_claim_token"] = token
            claimed.append(item)
        _save(jobs)
    return claimed


def finish_followup(job_id: str, claim_token: str) -> bool:
    with _store_lock():
        jobs = _load()
        rec = jobs.get(job_id)
        if not rec or rec.get("followup_claim") != claim_token:
            return False
        rec.update({
            "followed_up": True,
            "followup_state": "completed",
            "followup_claim": None,
            "followup_claimed_at": None,
            "followup_error": None,
        })
        _save(jobs)
        return True


def fail_followup(job_id: str, claim_token: str, error: str, *, count_attempt: bool = True) -> str:
    """Release a claim with bounded durable backoff; return pending or failed."""
    with _store_lock():
        jobs = _load()
        rec = jobs.get(job_id)
        if not rec or rec.get("followup_claim") != claim_token:
            return "stale"
        attempts = int(rec.get("followup_attempts") or 0)
        if not count_attempt:
            attempts = max(0, attempts - 1)
            rec["followup_attempts"] = attempts
        terminal = attempts >= _FOLLOWUP_MAX_ATTEMPTS
        rec.update({
            "followup_state": "failed" if terminal else "pending",
            "followup_claim": None,
            "followup_claimed_at": None,
            "followup_error": redact_text(error)[:2000],
            "next_followup_at": None if terminal else time.time() + min(300, 15 * (2 ** max(0, attempts - 1))),
            "followed_up": terminal,
        })
        _save(jobs)
        return rec["followup_state"]


def mark_followed_up(job_id: str) -> None:
    with _store_lock():
        jobs = _load()
        if job_id in jobs:
            jobs[job_id]["followed_up"] = True
            jobs[job_id]["followup_state"] = "completed"
            _save(jobs)


def get(job_id: str) -> Optional[Dict[str, Any]]:
    refresh()  # reconcile against disk so status/exit_code are current
    rec = _load().get(job_id)
    if rec:
        rec = dict(rec)
        rec["output"] = _read_output(rec)
    return rec


def _scope_matches(
    rec: Dict[str, Any],
    *,
    session_id: str,
    owner: Optional[str],
    workspace: Optional[str],
) -> bool:
    caller_session = str(session_id or "")
    caller_owner = str(owner or "")
    caller_workspace = str(workspace or "")
    if not caller_session or not caller_owner or not caller_workspace:
        return False
    if str(rec.get("session_id") or "") != caller_session:
        return False
    stored_owner = str(rec.get("owner") or "")
    if not stored_owner or stored_owner != caller_owner:
        return False
    stored_workspace = str(rec.get("workspace") or "")
    if not stored_workspace:
        return False
    return os.path.realpath(stored_workspace) == os.path.realpath(caller_workspace)


def _operation_record(
    jobs: Dict[str, Dict[str, Any]],
    job_id: str,
    *,
    session_id: Optional[str],
    owner: Optional[str],
    workspace: Optional[str],
) -> Optional[Dict[str, Any]]:
    rec = jobs.get(job_id)
    if rec is None:
        return None
    if session_id is None and owner is None and workspace is None:
        return rec
    if not _scope_matches(
        rec,
        session_id=str(session_id or ""),
        owner=owner,
        workspace=workspace,
    ):
        return None
    return rec


def get_scoped(
    job_id: str,
    *,
    session_id: str,
    owner: Optional[str],
    workspace: Optional[str],
) -> Optional[Dict[str, Any]]:
    refresh()
    with _store_lock():
        rec = _load().get(job_id)
        rec = dict(rec) if rec is not None else None
    if rec is None or not _scope_matches(
        rec,
        session_id=session_id,
        owner=owner,
        workspace=workspace,
    ):
        return None
    rec["output"] = _read_output(rec)
    return rec


def list_for_scope(
    *,
    session_id: str,
    owner: Optional[str],
    workspace: Optional[str],
) -> List[Dict[str, Any]]:
    return [
        dict(rec)
        for rec in refresh().values()
        if _scope_matches(
            rec,
            session_id=session_id,
            owner=owner,
            workspace=workspace,
        )
    ]


def list_for_session(session_id: str) -> List[Dict[str, Any]]:
    return [r for r in refresh().values() if r.get("session_id") == session_id]


def tail(
    job_id: str,
    *,
    cursor: int = 0,
    limit: int = 16_384,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Dict[str, Any]:
    refresh()
    with _store_lock():
        rec = _operation_record(
            _load(),
            job_id,
            session_id=session_id,
            owner=owner,
            workspace=workspace,
        )
        if rec is None:
            raise KeyError(job_id)
        path = Path(rec["log_path"])
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        start = max(0, min(int(cursor), size))
        budget = max(1, min(int(limit), 64 * 1024))
        raw = b""
        if size:
            with open(path, "rb") as handle:
                handle.seek(start)
                raw = handle.read(budget)
    next_cursor = start + len(raw)
    return {
        "job_id": job_id,
        "status": rec.get("status"),
        "cursor": start,
        "next_cursor": next_cursor,
        "eof": next_cursor >= size,
        "truncated": next_cursor < size,
        "total_bytes": size,
        "returned_bytes": len(raw),
        "returned_lines": raw.count(b"\n"),
        "output": redact_text(raw.decode("utf-8", errors="replace")),
        "exit_code": rec.get("exit_code"),
    }


def write(
    job_id: str,
    data: str,
    *,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    workspace: Optional[str] = None,
) -> bool:
    refresh()
    deadline = time.monotonic() + 5.0
    while True:
        fd = None
        pipe_name = ""
        with _store_lock():
            rec = _operation_record(
                _load(),
                job_id,
                session_id=session_id,
                owner=owner,
                workspace=workspace,
            )
            if rec is None or not is_active(rec):
                return False
            fifo = str(rec.get("stdin_path") or "")
            ready = str(rec.get("stdin_ready_path") or "")
            if not fifo:
                return False
            if not ready or Path(ready).exists():
                if os.name == "nt":
                    pipe_name = fifo
                else:
                    try:
                        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError:
                        pass
        if pipe_name:
            try:
                from multiprocessing.connection import Client

                connection = Client(pipe_name, family="AF_PIPE", authkey=None)
                try:
                    connection.send_bytes(str(data).encode("utf-8"))
                finally:
                    connection.close()
                return True
            except (OSError, EOFError):
                pass
        if fd is not None:
            break
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    try:
        pending = memoryview(str(data).encode("utf-8"))
        while pending:
            written = os.write(fd, pending)
            pending = pending[written:]
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def wait(job_id: str, *, timeout_s: float = 30.0) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + max(0.0, min(float(timeout_s), 30.0))
    while True:
        rec = get(job_id)
        if rec is None or not is_active(rec) or time.monotonic() >= deadline:
            return rec
        time.sleep(0.1)


def kill(
    job_id: str,
    *,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    workspace: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Terminate a running job's process tree and mark it killed. Returns the
    updated record, or None if the id is unknown. Idempotent: a job that already
    finished is returned unchanged. Sets followed_up so the monitor does not also
    fire an auto-continue for a job the agent deliberately stopped."""
    with _store_lock():
        jobs = _load()
        rec = _operation_record(
            jobs,
            job_id,
            session_id=session_id,
            owner=owner,
            workspace=workspace,
        )
        if rec is None:
            return None
        if is_active(rec):
            _kill_record(rec)
            # Legacy records created before capture-state persistence can only
            # report the historical terminal shape. New records stay
            # reconciling until the worker writes its terminal receipt.
            rec["status"] = "reconciling" if rec.get("capture_state_path") else "failed"
            rec["exit_code"] = -1
            rec["signal"] = signal.SIGTERM if os.name != "nt" else None
            rec["ended_at"] = None
            rec["killed"] = True
            rec["followed_up"] = not bool(rec.get("capture_state_path"))
            rec["capture_phase"] = _record_capture_phase(rec)
            _save(jobs)
        return dict(rec)


def delete(
    job_id: str,
    *,
    session_id: Optional[str] = None,
    owner: Optional[str] = None,
    workspace: Optional[str] = None,
) -> bool:
    """Delete one durable shell session and every file owned by its ID."""
    with _store_lock():
        jobs = _load()
        rec = _operation_record(
            jobs,
            job_id,
            session_id=session_id,
            owner=owner,
            workspace=workspace,
        )
        if rec is None:
            return False
        jobs.pop(job_id, None)
        if is_active(rec):
            _kill_record(rec)
        _save(jobs)
    for path in _owned_paths(job_id):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return True


def delete_for_session_owner(*, session_id: str, owner: str) -> int:
    """Delete every durable shell job owned by one authoritative chat scope."""
    caller_session = str(session_id or "")
    caller_owner = str(owner or "")
    if not caller_session:
        return 0
    removed: list[str] = []
    with _store_lock():
        jobs = _load()
        for job_id, rec in list(jobs.items()):
            if (
                str(rec.get("session_id") or "") != caller_session
                or str(rec.get("owner") or "") != caller_owner
            ):
                continue
            if is_active(rec):
                _kill_record(rec)
            jobs.pop(job_id, None)
            removed.append(job_id)
        if removed:
            _save(jobs)
    for job_id in removed:
        for path in _owned_paths(job_id):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return len(removed)


def result_text(rec: Dict[str, Any]) -> str:
    """Human/agent-readable summary of a finished job, for the follow-up."""
    out = _read_output(rec)
    if rec.get("killed"):
        head = "Background job was killed."
    elif rec.get("timed_out"):
        head = f"Background job timed out after {rec.get('max_runtime_s')}s."
    elif rec.get("died"):
        head = "Background job process died unexpectedly (no exit code)."
    else:
        head = f"Background job finished with exit code {rec.get('exit_code')}."
    return f"{head}\nCommand: {rec.get('command')}\n\nOutput:\n{out or '(no output)'}"
