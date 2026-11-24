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

import hashlib
import json
import os
import signal
import stat
import shutil
import subprocess
import sys
import time
import threading
import uuid
import secrets
import functools
from contextlib import contextmanager
from dataclasses import dataclass, field
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
# Original supervised POSIX workers only; no reconnect or PID adoption.
_BOUND_INPUTS = {}
_BOUND_INPUTS_LOCK = threading.RLock()

# One owner-wide Linux exclusion gate. It contains no job or authority data.
_EXECUTION_GATE = _JOBS_DIR / '.execution-gate.lock'
_GATE_STORE_LIMIT = 4 * 1024 * 1024
_GATE_ROW_LIMIT = 4096

def _open_execution_gate(*, exclusive: bool) -> tuple[int, tuple[int, int]]:
    if sys.platform != 'linux':
        raise JobStoreError('background exclusion unavailable')
    import fcntl
    _JOBS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = os.open(_JOBS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        root = os.fstat(directory)
        if root.st_uid != os.geteuid() or root.st_mode & 0o022:
            raise JobStoreError('background exclusion unavailable')
        fd = os.open(_EXECUTION_GATE.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        try:
            held = os.fstat(fd); named = os.stat(_EXECUTION_GATE.name, dir_fd=directory, follow_symlinks=False)
            if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.geteuid() or held.st_mode & 0o077 or held.st_nlink != 1
                    or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)):
                raise JobStoreError('background exclusion unavailable')
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            named = os.stat(_EXECUTION_GATE.name, dir_fd=directory, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino):
                raise JobStoreError('background exclusion changed')
            return fd, (held.st_dev, held.st_ino)
        except BaseException:
            os.close(fd); raise
    finally:
        os.close(directory)

@dataclass(eq=False)
class BackgroundClose:
    _fd: int
    _identity: tuple[int, int]
    _released: bool = False

    def _identity_current(self) -> bool:
        if self._released: return False
        try:
            held = os.fstat(self._fd); named = os.lstat(_EXECUTION_GATE)
            return stat.S_ISREG(named.st_mode) and (held.st_dev, held.st_ino) == self._identity == (named.st_dev, named.st_ino)
        except OSError: return False

    def current(self) -> bool:
        try:
            with _store_lock():
                if not self._identity_current(): return False
                try: size = _STORE.stat().st_size
                except FileNotFoundError: size = 0
                if size > _GATE_STORE_LIMIT: return False
                jobs = _load()
                return len(jobs) <= _GATE_ROW_LIMIT and all(_cleanup_settled(row) for row in jobs.values()) and self._identity_current()
        except (OSError, JobStoreError): return False

    def release(self) -> bool:
        if self._released: return False
        self._released = True
        os.close(self._fd)
        return True

def begin_background_close() -> BackgroundClose | None:
    """Nonblocking global close; None means busy or proof unavailable."""
    try:
        fd, identity = _open_execution_gate(exclusive=True)
    except (OSError, JobStoreError): return None
    close = BackgroundClose(fd, identity)
    if close.current(): return close
    close.release(); return None

def _cleanup_settled(row: Dict[str, Any]) -> bool:
    if not (_is_shell(row) or _is_bound_shell(row)) or row.get('status') not in _TERMINAL_STATUSES:
        return False
    claim = row.get('worker_claim_sha256') if _is_bound_shell(row) else row.get('cleanup_claim_sha256')
    if type(claim) is not str or len(claim) != 64 or any(ch not in '0123456789abcdef' for ch in claim): return False
    if row.get('cleanup_not_issued') is True: return True
    proof = row.get('child_settlement')
    return bool(type(proof) is dict and set(proof) == {'state','waited','child_exit_code','worker_exit_code'}
        and proof.get('state') == 'settled' and proof.get('waited') is True
        and type(proof.get('child_exit_code')) is int and type(proof.get('worker_exit_code')) is int
        and proof['worker_exit_code'] == row.get('exit_code'))

def _settle_bound_not_issued(reservation: Any) -> bool:
    if type(reservation) is not dict or not _is_bound_shell(reservation): return False
    job_id, nonce = reservation.get('id'), reservation.get('spawn_nonce')
    if type(job_id) is not str or type(nonce) is not str or not nonce: return False
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        if row != reservation or row.get('status') != 'reserved' or row.get('child_settlement') is not None:
            return False
        row.update({'status':'failed','cleanup_not_issued':True,'worker_claim_sha256':hashlib.sha256(nonce.encode()).hexdigest(),
                    'spawn_nonce':None,'ended_at':time.time()})
        _save(jobs)
        return True

def validate_inherited_execution_gate(value: Any) -> int:
    """Verify the exact inherited shared-lock descriptor before worker effects."""
    if sys.platform != 'linux' or type(value) is not dict or set(value) != {'fd','device','inode'}:
        raise JobStoreError('background exclusion unavailable')
    fd, device, inode = value['fd'], value['device'], value['inode']
    if type(fd) is not int or fd < 3 or type(device) is not int or device < 0 or type(inode) is not int or inode <= 0:
        raise JobStoreError('background exclusion unavailable')
    held=os.fstat(fd); named=os.lstat(_EXECUTION_GATE)
    if (not stat.S_ISREG(held.st_mode) or not stat.S_ISREG(named.st_mode) or held.st_uid != os.geteuid()
            or held.st_nlink != 1 or held.st_mode & 0o077 or (held.st_dev,held.st_ino)!=(device,inode)
            or (named.st_dev,named.st_ino)!=(device,inode)):
        raise JobStoreError('background exclusion unavailable')
    return fd

def settle_legacy_cleanup(job_id: str, nonce: str, *, child_exit_code: int, worker_exit_code: int) -> None:
    """Record the new legacy worker's own positively waited child once."""
    if type(child_exit_code) is not int or type(worker_exit_code) is not int or type(nonce) is not str:
        raise JobStoreError('background settlement unavailable')
    with _store_lock():
        jobs=_load(); row=jobs.get(job_id)
        if (not _is_shell(row or {}) or _is_bound_shell(row or {}) or row.get('status') not in ('spawning','running','reconciling')
                or row.get('cleanup_claim_sha256') != hashlib.sha256(nonce.encode()).hexdigest() or row.get('child_settlement') is not None):
            raise JobStoreError('background settlement unavailable')
        row['child_settlement']={'state':'settled','waited':True,'child_exit_code':child_exit_code,'worker_exit_code':worker_exit_code}
        row['status']='done' if worker_exit_code == 0 else 'failed'; row['exit_code']=worker_exit_code; row['ended_at']=time.time()
        _save(jobs)

# A job that runs longer than this is presumed stuck and reaped (the agent
# still gets a "timed out" follow-up so nothing hangs forever).
DEFAULT_MAX_RUNTIME_S = 3600  # 1 hour
_ACTIVE_STATUSES = frozenset({"running", "reconciling"})
_TERMINAL_STATUSES = frozenset({"done", "failed"})


class JobStoreError(RuntimeError):
    """The shared job authority is unreadable or malformed."""


def _is_shell(rec: Dict[str, Any]) -> bool:
    return rec.get("kind") is None or (rec.get("kind") == "shell" and rec.get("version") in (None, 1))


def _is_bound_shell(rec: Dict[str, Any]) -> bool:
    return rec.get("kind") == "shell" and rec.get("version") == 2


_BOUND_PUBLICATION_SEAL = object()


@dataclass(frozen=True)
class BoundShellPublication:
    """Host-issued exact same-row publication authority, never a wire DTO."""
    owner: str
    account_id: str
    session_id: str
    workspace_id: str
    workspace: str
    privacy: str
    job_id: str
    attempt_id: str
    message_id: str
    body: str
    body_sha256: str
    metadata_json: str
    metadata_sha256: str
    claim_id: str
    allow_insert: bool
    _seal: Any = field(repr=False, compare=False)
    _insert_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    _insert_used: Any = field(default_factory=lambda: [False], repr=False, compare=False)

    def __post_init__(self):
        if self._seal is not _BOUND_PUBLICATION_SEAL:
            raise ValueError('bound shell publication cannot be constructed by callers')

    def assert_current(self) -> None:
        with _store_lock():
            row = _load().get(self.job_id)
            identity = row.get('identity') if _is_bound_shell(row or {}) else None
            follow = row.get('bound_followup') if type(row) is dict else None
            if (self._seal is not _BOUND_PUBLICATION_SEAL or type(identity) is not dict or type(follow) is not dict
                    or row.get('owner') != self.owner or row.get('account_id') != self.account_id
                    or any(identity.get(k) != getattr(self, k) for k in ('session_id','workspace_id','workspace','privacy'))
                    or follow.get('attempt_id') != self.attempt_id or follow.get('claim_id') != self.claim_id
                    or follow.get('message_id') != self.message_id or follow.get('state') != 'dispatching'
                    or follow.get('body') != self.body or follow.get('body_sha256') != self.body_sha256
                    or follow.get('metadata_json') != self.metadata_json or follow.get('metadata_sha256') != self.metadata_sha256
                    or hashlib.sha256(self.body.encode('utf-8')).hexdigest() != self.body_sha256
                    or hashlib.sha256(self.metadata_json.encode('utf-8')).hexdigest() != self.metadata_sha256):
                raise JobStoreError('bound shell publication authority changed')

    def claim_insert(self) -> bool:
        """Consume the single in-process insert right before any append attempt."""
        with self._insert_lock:
            if not self.allow_insert or self._insert_used[0]:
                return False
            self.assert_current()
            self._insert_used[0] = True
            return True


def begin_bound_publication(job_id: str, attempt_id: str, claim_id: str) -> BoundShellPublication:
    """Mark a fresh append issued, or return read-only reconciliation authority."""
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        follow = row.get('bound_followup') if _is_bound_shell(row or {}) else None
        identity = row.get('identity') if type(row) is dict else None
        if (type(follow) is not dict or type(identity) is not dict or follow.get('attempt_id') != attempt_id
                or follow.get('claim_id') != claim_id or follow.get('state') not in ('publication_pending','dispatching','publication_unknown')):
            raise JobStoreError('bound shell publication unavailable')
        fresh = follow['state'] == 'publication_pending'
        if fresh or follow['state']=='publication_unknown':
            follow['state'] = 'dispatching'
            _save(jobs)
        try:
            proof = BoundShellPublication(owner=row['owner'],account_id=row['account_id'],
                session_id=identity['session_id'],workspace_id=identity['workspace_id'],workspace=identity['workspace'],
                privacy=identity['privacy'],job_id=job_id,attempt_id=attempt_id,message_id=follow['message_id'],
                body=follow['body'],body_sha256=follow['body_sha256'],metadata_json=follow['metadata_json'],
                metadata_sha256=follow['metadata_sha256'],claim_id=claim_id,allow_insert=fresh,_seal=_BOUND_PUBLICATION_SEAL)
        except (KeyError, TypeError) as exc:
            raise JobStoreError('bound shell publication malformed') from exc
    proof.assert_current()
    return proof


def finish_bound_publication(proof: BoundShellPublication, outcome: str) -> None:
    if type(proof) is not BoundShellPublication or outcome not in ('inserted','already_present','unknown'):
        raise JobStoreError('bound shell publication result unavailable')
    proof.assert_current()
    with _store_lock():
        jobs = _load(); row = jobs.get(proof.job_id); follow = row.get('bound_followup') if type(row) is dict else None
        if (not _is_bound_shell(row or {}) or type(follow) is not dict or follow.get('state') != 'dispatching'
                or follow.get('attempt_id') != proof.attempt_id or follow.get('claim_id') != proof.claim_id
                or row.get('owner') != proof.owner or row.get('account_id') != proof.account_id
                or type(row.get('identity')) is not dict
                or any(row['identity'].get(k) != getattr(proof,k) for k in ('session_id','workspace_id','workspace','privacy'))
                or follow.get('message_id') != proof.message_id or follow.get('body') != proof.body
                or follow.get('metadata_json') != proof.metadata_json
                or follow.get('body_sha256') != proof.body_sha256 or follow.get('metadata_sha256') != proof.metadata_sha256):
            raise JobStoreError('bound shell publication authority changed')
        follow['state'] = 'published' if outcome in ('inserted','already_present') else 'publication_unknown'
        _save(jobs)


def _bound_shell_state(row: dict) -> str:
    state=row.get('status'); settlement=row.get('child_settlement')
    if state == 'failed' and row.get('cleanup_not_issued') is True and _cleanup_settled(row): return 'failed'
    if state in ('done','failed'):
        if (type(settlement) is not dict or set(settlement)!={'state','waited','child_exit_code','worker_exit_code'}
                or settlement.get('state')!='settled' or settlement.get('waited') is not True
                or type(settlement.get('child_exit_code')) is not int or type(settlement.get('worker_exit_code')) is not int
                or settlement['worker_exit_code']!=row.get('exit_code')): return 'unknown'
    elif state not in ('reserved','spawning','running','reconciling'):
        return 'unknown'
    return state


def bound_shell_page(*, account_id: str, owner: str, session_id: str, workspace_id: str, workspace: str, limit: int = 32) -> list[dict]:
    """Closed owner-scoped view of v2 rows; no legacy or private fields."""
    if (not all(type(v) is str and v for v in (account_id,owner,session_id,workspace_id,workspace))
            or type(limit) is not int or not 1 <= limit <= 100):
        raise JobStoreError('bound shell scope unavailable')
    with _store_lock():
        rows = []
        for row in _load().values():
            identity = row.get('identity') if _is_bound_shell(row) else None
            if row.get('retired_from_view') == 1: continue
            if (type(identity) is not dict or row.get('account_id') != account_id or row.get('owner') != owner
                    or identity.get('session_id') != session_id or identity.get('workspace_id') != workspace_id
                    or identity.get('workspace') != workspace or identity.get('privacy') != 'normal'):
                continue
            follow = row.get('bound_followup')
            follow_state=follow.get('state') if type(follow) is dict else 'not_started'
            if follow_state in ('issued','publication_unknown'): follow_state='unknown'
            if follow_state not in ('not_started','claimed','publication_pending','dispatching','published','unknown'): follow_state='unknown'
            status=_bound_shell_state(row)
            cleanup='settled' if type(follow) is dict and follow.get('cleanup_state')=='settled' else 'unknown'
            rows.append({'jobID': row['id'], 'status': status, 'followup':follow_state, 'cleanup':cleanup})
        return rows[:limit]


def workload_job_page(*, source: str, account_id: str, owner: str, session_id: str,
                      workspace_id: str, workspace: str, after: str = '', limit: int = 50) -> dict:
    """Read a bounded, scoped observation without refreshing or reconciling jobs."""
    import re
    if (source not in ('shell_job','media_job') or type(limit) is not int or not 1 <= limit <= 100
            or type(after) is not str or (after and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',after))
            or not all(type(v) is str and v for v in (account_id,owner,session_id,workspace_id,workspace))):
        raise JobStoreError('job observation scope unavailable')
    def unique(pairs):
        value = {}
        for key,item in pairs:
            if key in value: raise JobStoreError('job observation malformed')
            value[key] = item
        return value
    with _store_lock():
        try:
            if _STORE.is_symlink(): raise JobStoreError('job observation unavailable')
            with _STORE.open('rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise JobStoreError('job observation unavailable')
                raw = stream.read(16 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return {'items':[], 'next_after':None}
        except OSError as exc:
            raise JobStoreError('job observation unavailable') from exc
        if len(raw) > 16 * 1024 * 1024: raise JobStoreError('job observation exceeds bounds')
        try: jobs = json.loads(raw,object_pairs_hook=unique)
        except (ValueError,UnicodeError) as exc: raise JobStoreError('job observation malformed') from exc
        if type(jobs) is not dict or len(jobs) > 10000: raise JobStoreError('job observation exceeds bounds')
        selected = []
        for key,row in jobs.items():
            if type(key) is not str or type(row) is not dict: raise JobStoreError('job observation malformed')
            if row.get('owner') != owner or row.get('account_id') != account_id: continue
            if source == 'shell_job' and not _is_bound_shell(row): continue
            if source == 'shell_job' and row.get('retired_from_view') == 1: continue
            if source == 'media_job' and not (row.get('kind') == 'media' and row.get('version') == 1): continue
            identity = row.get('identity')
            if (type(identity) is not dict or identity.get('account_id') != account_id
                    or type(identity.get('session_id')) is not str or type(identity.get('workspace_id')) is not str):
                if source == 'media_job' and row.get('chat_detached') is True: continue
                raise JobStoreError('job observation scope unknown')
            if row.get('chat_detached') is True: continue
            if identity['session_id'] != session_id or identity['workspace_id'] != workspace_id: continue
            if source == 'shell_job' and (identity.get('privacy') != 'normal' or identity.get('workspace') != workspace): continue
            if (row.get('session_id') != session_id
                    or (source == 'shell_job' and row.get('workspace') != workspace)
                    or (source == 'media_job' and row.get('workspace_id') != workspace_id)):
                raise JobStoreError('job observation scope changed')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',key) or row.get('id') != key:
                raise JobStoreError('job observation identity invalid')
            if key <= after: continue
            if source == 'shell_job':
                selected.append((key,_bound_shell_state(row),None))
            else:
                if type(row.get('revision')) is not int or row['revision'] < 1 or row.get('status') not in _MEDIA_STATES:
                    raise JobStoreError('job observation invalid')
                selected.append((key,row['status'],row['revision']))
        selected.sort(key=lambda item:item[0]); page = selected[:limit]
        return {'items':[{'id':key,'status':status,'revision':revision} for key,status,revision in page],
                'next_after':page[-1][0] if len(selected) > limit else None}


def bound_followup_candidates(limit: int = 32) -> list[dict]:
    """Enumerate only candidate identities; every consumer must re-admit scope."""
    if type(limit) is not int or not 1 <= limit <= 32:
        raise JobStoreError('bound shell candidate limit unavailable')
    with _store_lock():
        return [{'job_id':row['id'],'owner':row['owner'],'account_id':row['account_id'],
                 'session_id':row['identity']['session_id']}
                for row in _load().values() if _is_bound_shell(row) and row.get('status') in _TERMINAL_STATUSES
                and row.get('retired_from_view') != 1
                and type(row.get('identity')) is dict and row['identity'].get('privacy') == 'normal'
                and (row.get('bound_followup') is None or (type(row.get('bound_followup')) is dict
                    and row['bound_followup'].get('state') in ('publication_pending','dispatching')))][:limit]


def bound_shell_tail(job_id: str, *, account_id: str, owner: str, session_id: str,
                     workspace_id: str, workspace: str, cursor: int = 0, limit: int = 16384) -> dict:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 65536:
        raise JobStoreError('bound shell cursor unavailable')
    with _store_lock():
        row=_load().get(job_id); identity=row.get('identity') if _is_bound_shell(row or {}) else None
        if (type(identity) is not dict or row.get('account_id')!=account_id or row.get('owner')!=owner
                or identity.get('session_id')!=session_id or identity.get('workspace_id')!=workspace_id
                or identity.get('workspace')!=workspace or identity.get('privacy')!='normal'):
            raise JobStoreError('bound shell unavailable')
        if row.get('retired_from_view') == 1:
            return {'jobID':job_id,'status':'retired','cursor':cursor,'nextCursor':cursor,'eof':True,'output':''}
        path=Path(str(row.get('log_path') or ''))
        if path.parent != _JOBS_DIR or path.name != f'{job_id}.log':
            raise JobStoreError('bound shell output unavailable')
        try:
            if os.name == 'nt':
                from src.openclank.windows_history_io import read_live_log_range
                start,size,raw=read_live_log_range(path,cursor,limit)
            else:
                if not hasattr(os,'O_NOFOLLOW'): raise JobStoreError('bound shell output unavailable')
                with os.fdopen(os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise JobStoreError('bound shell output unavailable')
                    stream.seek(0,os.SEEK_END); size=stream.tell(); start=min(cursor,size)
                    stream.seek(start); raw=stream.read(limit)
        except (OSError, ValueError) as exc:
            raise JobStoreError('bound shell output unavailable') from exc
        next_cursor=start+len(raw)
        output=redact_text(raw.decode('utf-8',errors='replace'))
        if len(output.encode('utf-8'))>65536:
            raise JobStoreError('bound shell output exceeds limit')
        return {'jobID':job_id,'status':_bound_shell_state(row),'cursor':start,
                'nextCursor':next_cursor,'eof':next_cursor>=size,'output':output}


def bound_shell_origin(job_id: str, *, account_id: str, owner: str, session_id: str,
                       workspace_id: str, workspace: str) -> dict:
    """Project the exact v2 producer identity and positively settled child exit."""
    if not all(type(v) is str and v for v in (job_id, account_id, owner, session_id, workspace_id, workspace)):
        raise JobStoreError('bound shell scope unavailable')
    with _store_lock():
        row = _load().get(job_id)
        identity = row.get('identity') if _is_bound_shell(row or {}) else None
        if (type(identity) is not dict or row.get('id') != job_id or row.get('account_id') != account_id
                or row.get('owner') != owner or identity.get('account_id') != account_id
                or identity.get('owner') != owner or identity.get('session_id') != session_id
                or identity.get('workspace_id') != workspace_id or identity.get('workspace') != workspace
                or identity.get('privacy') != 'normal' or row.get('retired_from_view') == 1):
            raise JobStoreError('bound shell unavailable')
        fields = ('engine_session_id', 'root_operation_id', 'actor_id', 'execution_revision',
                  'turn_id', 'message_id', 'tool_call_id', 'holder', 'generation')
        origin = {field: identity.get(field) for field in fields}
        if (any(type(origin[k]) is not str or not origin[k] or len(origin[k]) > 1024
                for k in fields if k not in ('execution_revision', 'generation'))
                or any(type(origin[k]) is not int or origin[k] < 0 for k in ('execution_revision', 'generation'))):
            raise JobStoreError('bound shell origin unavailable')
        status = _bound_shell_state(row)
        settlement = row.get('child_settlement')
        exit_code = None
        if (status in ('done', 'failed') and type(settlement) is dict
                and set(settlement) == {'state', 'waited', 'child_exit_code', 'worker_exit_code'}
                and settlement.get('state') == 'settled' and settlement.get('waited') is True
                and type(settlement.get('child_exit_code')) is int and type(settlement.get('worker_exit_code')) is int
                and settlement['worker_exit_code'] == row.get('exit_code')):
            exit_code = settlement['child_exit_code']
        return {'jobID': job_id, 'origin': origin, 'status': status,
                'settledExit': {'state': 'settled' if exit_code is not None else 'unknown', 'exitCode': exit_code}}


def bound_shell_status(job_id: str, *, account_id: str, owner: str, session_id: str,
                       workspace_id: str, workspace: str) -> dict:
    """Read one exact v2 job without refreshing or observing process state."""
    if not all(type(v) is str and v for v in
               (job_id, account_id, owner, session_id, workspace_id, workspace)):
        raise JobStoreError('bound shell scope unavailable')
    with _store_lock():
        row = _load().get(job_id)
        identity = row.get('identity') if _is_bound_shell(row or {}) else None
        if (type(identity) is not dict or row.get('account_id') != account_id or row.get('owner') != owner
                or identity.get('session_id') != session_id or identity.get('workspace_id') != workspace_id
                or identity.get('workspace') != workspace or identity.get('privacy') != 'normal'):
            raise JobStoreError('bound shell unavailable')
        return {'jobID': job_id, 'status': 'retired' if row.get('retired_from_view') == 1 else _bound_shell_state(row)}


def retire_bound_shell(job_id: str, *, account_id: str, owner: str, session_id: str, workspace_id: str, workspace: str) -> str:
    """Hide one positively settled row, retaining every authority and sidecar."""
    import re
    with _store_lock():
        jobs=_load(); row=jobs.get(job_id); identity=row.get('identity') if _is_bound_shell(row or {}) else None
        if (type(identity) is not dict or row.get('id')!=job_id or row.get('account_id')!=account_id or row.get('owner')!=owner
                or identity.get('account_id')!=account_id or identity.get('owner')!=owner or identity.get('session_id')!=session_id
                or identity.get('workspace_id')!=workspace_id or identity.get('workspace')!=workspace or identity.get('privacy')!='normal'):
            raise JobStoreError('bound shell unavailable')
        if row.get('retired_from_view') == 1: return 'already_retired'
        if ('retired_from_view' in row or _bound_shell_state(row) not in ('done','failed')
                or row.get('terminate_ready') is not False or row.get('terminate_closed') is not True
                or type(row.get('worker_claim_sha256')) is not str or not re.fullmatch(r'[0-9a-f]{64}',row['worker_claim_sha256'])):
            raise JobStoreError('bound shell retirement unavailable')
        control=row.get('terminate_control')
        if control is not None and (type(control) is not dict or set(control)!={'version','id','identity','args_sha256','worker_claim_sha256','state','closed'}
                or control.get('version')!=1 or control.get('closed') is not True or row.get('terminate_closed') is not True
                or control.get('state') not in ('requested','not_needed') or control.get('worker_claim_sha256')!=row.get('worker_claim_sha256')):
            raise JobStoreError('bound shell retirement unavailable')
        follow=row.get('bound_followup')
        if type(follow) is not dict or follow.get('state')!='published' or follow.get('cleanup_state')!='settled' or not _bound_shell_model_idle(job_id,row):
            raise JobStoreError('bound shell retirement unavailable')
        row['retired_from_view']=1; _save(jobs); return 'retired'


def bound_followup_snapshot(job_id: str, *, owner: str, account_id: str) -> dict:
    with _store_lock():
        row=_load().get(job_id)
        if (not _is_bound_shell(row or {}) or row.get('owner')!=owner or row.get('account_id')!=account_id
                or type(row.get('identity')) is not dict):
            raise JobStoreError('bound shell unavailable')
        return {'identity':dict(row['identity']), 'status':row.get('status'),
                'followup':dict(row['bound_followup']) if type(row.get('bound_followup')) is dict else None}


def claim_bound_followup(job_id: str, *, owner: str, account_id: str) -> dict:
    """Reserve exactly one pre-issue attempt with a bounded frozen shell result."""
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        settlement=row.get('child_settlement') if type(row) is dict else None
        if (not _is_bound_shell(row or {}) or row.get('owner') != owner or row.get('account_id') != account_id
                or row.get('status') not in _TERMINAL_STATUSES or type(row.get('exit_code')) is not int
                or type(settlement) is not dict or set(settlement)!={'state','waited','child_exit_code','worker_exit_code'}
                or settlement.get('state')!='settled' or settlement.get('waited') is not True
                or type(settlement.get('child_exit_code')) is not int or settlement.get('worker_exit_code')!=row.get('exit_code')
                or type(row.get('identity')) is not dict or row['identity'].get('privacy') != 'normal'
                or row.get('bound_followup') is not None):
            raise JobStoreError('bound shell follow-up unavailable')
        path=Path(str(row.get('log_path') or ''))
        if path.parent!=_JOBS_DIR or path.name!=f'{job_id}.log' or not hasattr(os,'O_NOFOLLOW'):
            raise JobStoreError('bound shell result unavailable')
        try:
            with os.fdopen(os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK),'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise JobStoreError('bound shell result unavailable')
                stream.seek(0,os.SEEK_END); size=stream.tell(); stream.seek(0)
                first=stream.read(65536)
                if size>131072:
                    stream.seek(-65536,os.SEEK_END); raw=first+b'\n...[truncated]...\n'+stream.read(65536)
                else:
                    raw=first+stream.read(65536)
        except OSError as exc:
            raise JobStoreError('bound shell result unavailable') from exc
        output = redact_text(raw.decode('utf-8',errors='replace'))
        if type(output) is not str or len(output.encode('utf-8')) > 262144:
            raise JobStoreError('bound shell result unavailable')
        follow = {'state':'claimed','attempt_id':uuid.uuid4().hex,'claim_id':uuid.uuid4().hex,
                  'message_id':str(uuid.uuid4()),'shell_output':output,
                  'shell_sha256':hashlib.sha256(output.encode('utf-8')).hexdigest(),
                  'exit_code':row['exit_code']}
        row['bound_followup'] = follow; _save(jobs)
        return {'identity':dict(row['identity']), 'followup':dict(follow)}


def defer_bound_followup(job_id: str, attempt_id: str, claim_id: str) -> bool:
    with _store_lock():
        jobs=_load(); row=jobs.get(job_id); follow=row.get('bound_followup') if _is_bound_shell(row or {}) else None
        if (type(follow) is not dict or follow.get('state') != 'claimed' or follow.get('attempt_id') != attempt_id
                or follow.get('claim_id') != claim_id): return False
        row.pop('bound_followup'); _save(jobs); return True


def issue_bound_followup(job_id: str, attempt_id: str, claim_id: str, route: dict) -> None:
    """Durably freeze route before the sole possible model dispatch."""
    if type(route) is not dict:
        raise JobStoreError('bound shell route unavailable')
    encoded=json.dumps(route,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if len(encoded.encode('utf-8')) > 16384:
        raise JobStoreError('bound shell route unavailable')
    with _store_lock():
        jobs=_load(); row=jobs.get(job_id); follow=row.get('bound_followup') if _is_bound_shell(row or {}) else None
        if (type(follow) is not dict or follow.get('state')!='claimed' or follow.get('attempt_id')!=attempt_id
                or follow.get('claim_id')!=claim_id):
            raise JobStoreError('bound shell issue unavailable')
        follow.update(state='issued',route_json=encoded,route_sha256=hashlib.sha256(encoded.encode('utf-8')).hexdigest())
        _save(jobs)


def record_bound_terminal(job_id: str, attempt_id: str, claim_id: str, body: str, metadata: dict) -> None:
    """Persist the observed terminal result before stream cleanup."""
    if type(body) is not str or len(body.encode('utf-8')) > 262144 or type(metadata) is not dict:
        raise JobStoreError('bound shell terminal result unavailable')
    encoded=json.dumps(metadata,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if len(encoded.encode('utf-8')) > 65536:
        raise JobStoreError('bound shell terminal metadata unavailable')
    with _store_lock():
        jobs=_load(); row=jobs.get(job_id); follow=row.get('bound_followup') if _is_bound_shell(row or {}) else None
        if (type(follow) is not dict or follow.get('state')!='issued' or follow.get('attempt_id')!=attempt_id
                or follow.get('claim_id')!=claim_id):
            raise JobStoreError('bound shell terminal authority changed')
        follow.update(state='publication_pending',cleanup_state='unproved',body=body,body_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest(),
                      metadata_json=encoded,metadata_sha256=hashlib.sha256(encoded.encode('utf-8')).hexdigest())
        _save(jobs)


def record_bound_cleanup(job_id: str, attempt_id: str, claim_id: str, evidence: dict) -> None:
    """Record only actual direct attachment and local teardown evidence."""
    keys={'direct_settled','model_terminal','session_closed','worker_released'}
    if type(evidence) is not dict or set(evidence)!=keys or any(type(evidence[k]) is not bool for k in keys):
        raise JobStoreError('bound shell cleanup evidence unavailable')
    with _store_lock():
        jobs=_load(); row=jobs.get(job_id); follow=row.get('bound_followup') if _is_bound_shell(row or {}) else None
        if (type(follow) is not dict or follow.get('attempt_id')!=attempt_id or follow.get('claim_id')!=claim_id
                or follow.get('state') not in ('issued','publication_pending','dispatching','publication_unknown','published')):
            raise JobStoreError('bound shell cleanup authority changed')
        follow['cleanup_state']='settled' if all(evidence.values()) and follow['state']!='issued' else 'unproved'
        _save(jobs)


def reserve_bound_shell(identity: Dict[str, Any], *, command_sha256: str, runtime_s: int) -> Dict[str, Any]:
    """Reserve one host-verified logical tool invocation in the existing store."""
    import re
    import secrets
    fields = {"account_id", "owner", "session_id", "engine_session_id", "workspace", "workspace_id", "root_operation_id", "actor_id", "execution_revision", "turn_id", "message_id", "tool_call_id", "holder", "generation", "privacy"}
    if type(identity) is not dict or set(identity) != fields:
        raise ValueError("shell invocation identity unavailable")
    if any(type(identity[k]) is not str or not identity[k] or len(identity[k]) > 1024 for k in fields - {"execution_revision", "generation", "privacy"}):
        raise ValueError("shell invocation identity unavailable")
    if type(identity["execution_revision"]) is not int or identity["execution_revision"] < 0 or type(identity["generation"]) is not int or identity["generation"] < 0 or identity["privacy"] not in ("normal", "private"):
        raise ValueError("shell invocation identity unavailable")
    if type(runtime_s) is not int or not 1 <= runtime_s <= 86400 or not re.fullmatch(r"[0-9a-f]{64}", command_sha256 or ""):
        raise ValueError("shell invocation request unavailable")
    logical = [identity[k] for k in ("session_id", "engine_session_id", "root_operation_id", "actor_id", "execution_revision", "turn_id", "message_id", "tool_call_id")]
    key = hashlib.sha256(json.dumps(logical, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    with _store_lock():
        jobs = _load()
        for row in jobs.values():
            if _is_bound_shell(row) and row.get("invocation_key") == key:
                if row.get("identity") != identity or row.get("command_sha256") != command_sha256 or row.get("max_runtime_s") != runtime_s:
                    raise ValueError("shell invocation changed")
                return dict(row)
        job_id = uuid.uuid4().hex[:12]
        if job_id in jobs:
            raise ValueError("shell job identity unavailable")
        row = {"id": job_id, "kind": "shell", "version": 2, "owner": identity["owner"],
               "account_id": identity["account_id"], "session_id": identity["session_id"],
               "workspace": identity["workspace"], "identity": dict(identity), "invocation_key": key,
               "command_sha256": command_sha256, "max_runtime_s": runtime_s,
               "spawn_nonce": secrets.token_urlsafe(32), "status": "reserved", "followup_state": "unsupported",
               "bound_followup_protocol": 1,
               "followed_up": False, "created_at": time.time()}
        jobs[job_id] = row
        _save(jobs)
        return dict(row)


def claim_bound_shell(job_id: str, nonce: str, command: bytes, runtime_s: int) -> None:
    """Consume the fixed worker's exact launch once, before command effects."""
    with _store_lock():
        jobs = _load()
        row = jobs.get(job_id)
        if (not _is_bound_shell(row or {}) or row.get("status") != "spawning"
                or row.get("spawn_nonce") != nonce or row.get("max_runtime_s") != runtime_s
                or row.get("command_sha256") != hashlib.sha256(command).hexdigest()):
            raise ValueError("shell worker claim unavailable")
        row["status"] = "running"
        row["spawn_nonce"] = None
        row["worker_claim_sha256"] = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
        row["pid"] = os.getpid()
        row["worker_claimed_at"] = time.time()
        _save(jobs)


def bound_shell_input_target(job_id: str, *, account_id: str, owner: str, session_id: str, workspace_id: str, workspace: str) -> str:
    """Resolve the original live worker without exposing an IPC endpoint."""
    with _BOUND_INPUTS_LOCK:
        entry=_BOUND_INPUTS.get((account_id,job_id))
        if not entry or entry['process'].poll() is not None: raise JobStoreError('bound input unavailable')
        with _store_lock():
            row=_load().get(job_id);identity=row.get('identity') if _is_bound_shell(row or {}) else None
            if (type(identity) is not dict or row.get('owner')!=owner or row.get('account_id')!=account_id
                or identity.get('session_id')!=session_id or identity.get('workspace_id')!=workspace_id
                or identity.get('workspace')!=workspace or identity.get('privacy')!='normal' or row.get('status')!='running' or row.get('input_ready') is not True
                or row.get('worker_claim_sha256')!=entry['nonce_hash']): raise JobStoreError('bound input unavailable')
            return entry['nonce_hash']


def bound_shell_input_status(job_id: str, *, account_id: str, owner: str, session_id: str, workspace_id: str,
                             workspace: str, control_identity: dict, args_sha256: str, payload_sha256: str, length: int) -> Optional[str]:
    control_id=hashlib.sha256(json.dumps(['openclank.bg-input.v1',account_id,job_id,control_identity],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    with _store_lock():
        row=_load().get(job_id);identity=row.get('identity') if _is_bound_shell(row or {}) else None
        if (type(identity) is not dict or row.get('account_id')!=account_id or row.get('owner')!=owner
            or identity.get('session_id')!=session_id or identity.get('workspace_id')!=workspace_id
            or identity.get('workspace')!=workspace or identity.get('privacy')!='normal'): raise JobStoreError('bound input unavailable')
        control=(row.get('input_controls') or {}).get(control_id)
        if control is None:return None
        if (control.get('identity')!=control_identity or control.get('args_sha256')!=args_sha256
            or control.get('sha256')!=payload_sha256 or control.get('length')!=length): raise JobStoreError('bound input conflict')
        return 'accepted' if control.get('state')=='accepted' else 'unknown'


def request_bound_shell_input(job_id: str, *, account_id: str, owner: str, session_id: str, workspace_id: str,
                              workspace: str, control_identity: dict, args_sha256: str, payload: bytes, expected_nonce_hash: str, issue_fence=None) -> str:
    import re,struct
    if not isinstance(payload,bytes) or not 1<=len(payload)<=16384 or type(control_identity) is not dict:
        raise JobStoreError('bound input unavailable')
    digest=hashlib.sha256(payload).hexdigest()
    control_id=hashlib.sha256(json.dumps(['openclank.bg-input.v1',account_id,job_id,control_identity],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if not re.fullmatch(r'[0-9a-f]{64}',args_sha256): raise JobStoreError('bound input unavailable')
    with _BOUND_INPUTS_LOCK:
        entry=_BOUND_INPUTS.get((account_id,job_id))
        if not entry or entry['process'].poll() is not None or entry['nonce_hash']!=expected_nonce_hash: raise JobStoreError('bound input unavailable')
        lock=entry.setdefault('lock',threading.Lock())
    with lock:
        with _BOUND_INPUTS_LOCK:
            if _BOUND_INPUTS.get((account_id,job_id)) is not entry or entry['process'].poll() is not None: raise JobStoreError('bound input unavailable')
        from contextlib import nullcontext
        with (issue_fence() if issue_fence is not None else nullcontext()):
            with _store_lock():
                jobs=_load();row=jobs.get(job_id);identity=row.get('identity') if _is_bound_shell(row or {}) else None
                if (type(identity) is not dict or row.get('account_id')!=account_id or row.get('owner')!=owner
                    or identity.get('session_id')!=session_id or identity.get('workspace_id')!=workspace_id or identity.get('workspace')!=workspace or identity.get('privacy')!='normal'
                    or row.get('worker_claim_sha256')!=entry['nonce_hash']): raise JobStoreError('bound input unavailable')
                controls=row.setdefault('input_controls',{});old=controls.get(control_id)
                if old is not None:
                    if old.get('identity')!=control_identity or old.get('args_sha256')!=args_sha256 or old.get('sha256')!=digest or old.get('length')!=len(payload): raise JobStoreError('bound input conflict')
                    return old['state'] if old['state'] in ('accepted','unknown') else 'unknown'
                if len(controls)>=128 or row.get('input_ready') is not True or row.get('status')!='running' or row.get('input_active') is not None:
                    raise JobStoreError('bound input unavailable')
                controls[control_id]={'identity':dict(control_identity),'args_sha256':args_sha256,'sha256':digest,'length':len(payload),'state':'issued','worker_claim_sha256':entry['nonce_hash']}
                row['input_active']=control_id
                try:_save(jobs)
                except Exception:return 'unknown'
        try:
            entry['socket'].settimeout(5.0);entry['socket'].sendall(control_id.encode('ascii')+struct.pack('!I',len(payload))+payload)
        except (OSError,TimeoutError):return 'unknown'
    deadline=time.monotonic()+5.0
    while time.monotonic()<deadline:
        try:
            with _store_lock():
                row=_load().get(job_id) or {};control=(row.get('input_controls') or {}).get(control_id) or {}
                if control.get('state') in ('accepted','unknown'):return control['state']
        except Exception:return 'unknown'
        time.sleep(0.025)
    return 'unknown'


def open_bound_shell_input(job_id: str, nonce: str) -> None:
    with _store_lock():
        jobs=_load();row=jobs.get(job_id);claim=hashlib.sha256(nonce.encode()).hexdigest()
        if not _is_bound_shell(row or {}) or row.get('worker_claim_sha256')!=claim or row.get('status')!='running':raise JobStoreError('bound input unavailable')
        row['input_ready']=True;_save(jobs)


def claim_bound_shell_input(job_id: str, nonce: str, control_id: str, payload: bytes) -> None:
    with _store_lock():
        jobs=_load();row=jobs.get(job_id);claim=hashlib.sha256(nonce.encode()).hexdigest();control=(row.get('input_controls') or {}).get(control_id) if row else None
        if (not _is_bound_shell(row or {}) or row.get('worker_claim_sha256')!=claim or row.get('input_active')!=control_id or not isinstance(control,dict)
            or control.get('worker_claim_sha256')!=claim or control.get('state')!='issued' or control.get('length')!=len(payload)
            or control.get('sha256')!=hashlib.sha256(payload).hexdigest()):raise JobStoreError('bound input unavailable')


def finish_bound_shell_input(job_id: str, nonce: str, control_id: str, state: str) -> None:
    if state not in ('accepted','unknown'):raise JobStoreError('bound input unavailable')
    with _store_lock():
        jobs=_load();row=jobs.get(job_id);claim=hashlib.sha256(nonce.encode()).hexdigest();control=(row.get('input_controls') or {}).get(control_id) if row else None
        if (not _is_bound_shell(row or {}) or row.get('worker_claim_sha256')!=claim or row.get('input_active')!=control_id or not isinstance(control,dict) or control.get('state')!='issued'):
            raise JobStoreError('bound input unavailable')
        control['state']=state;row['input_active']=None
        if state=='unknown':row['input_ready']=False
        _save(jobs)


def close_bound_shell_input(job_id: str, nonce: str) -> None:
    with _store_lock():
        jobs=_load();row=jobs.get(job_id);claim=hashlib.sha256(nonce.encode()).hexdigest()
        if not _is_bound_shell(row or {}) or row.get('worker_claim_sha256')!=claim:raise JobStoreError('bound input unavailable')
        active=row.get('input_active');control=(row.get('input_controls') or {}).get(active)
        if isinstance(control,dict) and control.get('state')=='issued':control['state']='unknown'
        row['input_active']=None;row['input_ready']=False;_save(jobs)


def request_bound_shell_terminate(job_id: str, *, account_id: str, owner: str, session_id: str,
                                  workspace_id: str, workspace: str, control_identity: dict,
                                  args_sha256: str) -> str:
    """Issue one exact, content-free terminate request on its existing v2 row."""
    import re
    fields = {'sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','toolCallID','toolName'}
    if (type(control_identity) is not dict or set(control_identity) != fields
            or any(type(control_identity[k]) is not str or not control_identity[k] or len(control_identity[k]) > 512
                   for k in fields - {'executionRevision'})
            or type(control_identity['executionRevision']) is not int or control_identity['executionRevision'] < 0
            or type(args_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', args_sha256)):
        raise JobStoreError('bound shell control unavailable')
    control_id = hashlib.sha256(json.dumps(['openclank.bg-control.v1',account_id,job_id,control_identity],
        ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        identity = row.get('identity') if _is_bound_shell(row or {}) else None
        if (type(identity) is not dict or row.get('account_id') != account_id or row.get('owner') != owner
                or identity.get('session_id') != session_id or identity.get('workspace_id') != workspace_id
                or identity.get('workspace') != workspace or identity.get('privacy') != 'normal'):
            raise JobStoreError('bound shell unavailable')
        old = row.get('terminate_control')
        if old is not None:
            if (type(old) is not dict or old.get('id') != control_id or old.get('identity') != control_identity
                    or old.get('args_sha256') != args_sha256):
                raise JobStoreError('bound shell control conflict')
            if old.get('state') not in ('pending','claimed','requested','not_needed','unknown'):
                raise JobStoreError('bound shell control unavailable')
            return 'unknown' if old.get('state') in ('claimed','unknown') else 'already_requested'
        if (row.get('status') != 'running' or row.get('terminate_ready') is not True
                or row.get('terminate_closed') is True
                or not isinstance(row.get('worker_claim_sha256'),str)
                or row.get('child_settlement') is not None):
            raise JobStoreError('bound shell control unavailable')
        row['terminate_control'] = {'version':1,'id':control_id,'identity':dict(control_identity),
            'args_sha256':args_sha256,'worker_claim_sha256':row['worker_claim_sha256'],
            'state':'pending','closed':False}
        _save(jobs)
        return 'requested'


def open_bound_shell_terminate(job_id: str, nonce: str) -> None:
    """Advertise only the exact owning worker's started control pump."""
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        claim = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
        if (not _is_bound_shell(row or {}) or row.get('worker_claim_sha256') != claim
                or row.get('status') != 'running' or row.get('terminate_closed') is True
                or row.get('terminate_ready') is not None or row.get('terminate_control') is not None):
            raise JobStoreError('bound shell control unavailable')
        row['terminate_ready'] = True; _save(jobs)


def claim_bound_shell_terminate(job_id: str, nonce: str) -> Optional[dict]:
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        control = row.get('terminate_control') if _is_bound_shell(row or {}) else None
        claim = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
        if (not _is_bound_shell(row or {}) or row.get('worker_claim_sha256') != claim):
            raise JobStoreError('bound shell worker unavailable')
        if control is None: return None
        if (type(control) is not dict or control.get('worker_claim_sha256') != claim
                or control.get('closed') is not False):
            raise JobStoreError('bound shell control unavailable')
        if control.get('state') != 'pending': return None
        control['state'] = 'claimed'; _save(jobs)
        return {'id':control['id']}


def ack_bound_shell_terminate(job_id: str, nonce: str, control_id: str, state: str) -> None:
    if state not in ('requested','not_needed','unknown'):
        raise JobStoreError('bound shell control unavailable')
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        control = row.get('terminate_control') if _is_bound_shell(row or {}) else None
        claim = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
        if (type(control) is not dict or row.get('worker_claim_sha256') != claim
                or control.get('worker_claim_sha256') != claim or control.get('id') != control_id
                or control.get('state') != 'claimed' or control.get('closed') is not False):
            raise JobStoreError('bound shell control unavailable')
        control['state'] = state; _save(jobs)


def close_bound_shell_terminate(job_id: str, nonce: str) -> None:
    """Close only after the owning worker's control pump has joined."""
    with _store_lock():
        jobs = _load(); row = jobs.get(job_id)
        claim = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
        if not _is_bound_shell(row or {}) or row.get('worker_claim_sha256') != claim:
            raise JobStoreError('bound shell worker unavailable')
        control = row.get('terminate_control')
        if control is None:
            row['terminate_ready'] = False; row['terminate_closed'] = True; _save(jobs); return
        if type(control) is not dict or control.get('worker_claim_sha256') != claim or control.get('closed') is not False:
            raise JobStoreError('bound shell control unavailable')
        if control.get('state') in ('pending','claimed'): control['state'] = 'unknown'
        control['closed'] = True; row['terminate_ready'] = False; row['terminate_closed'] = True; _save(jobs)


def settle_bound_shell(job_id: str, nonce: str, command: bytes, *, child_waited: bool,
                       child_exit_code: Optional[int], worker_exit_code: int) -> None:
    """Record only the fixed worker's own exact child-wait fact, once."""
    if type(child_waited) is not bool or type(worker_exit_code) is not int:
        raise ValueError("shell settlement unavailable")
    if child_waited != (type(child_exit_code) is int):
        raise ValueError("shell settlement unavailable")
    with _store_lock():
        jobs = _load()
        row = jobs.get(job_id)
        if (not _is_bound_shell(row or {}) or row.get("status") not in ("running", "reconciling", "unknown")
                or row.get("worker_claim_sha256") != hashlib.sha256(nonce.encode('utf-8')).hexdigest()
                or row.get("command_sha256") != hashlib.sha256(command).hexdigest()
                or row.get('terminate_ready') is True
                or row.get('input_ready') is True or row.get('input_active') is not None
                or (row.get('terminate_control') is not None and row.get('terminate_closed') is not True)
                or row.get("child_settlement") is not None):
            raise ValueError("shell settlement unavailable")
        row["child_settlement"] = {"state": "settled" if child_waited else "unknown", "waited": child_waited,
            "child_exit_code": child_exit_code, "worker_exit_code": worker_exit_code}
        row["status"] = ("done" if worker_exit_code == 0 else "failed") if child_waited else "unknown"
        row["exit_code"] = worker_exit_code if child_waited else None
        row["ended_at"] = time.time() if child_waited else None
        _save(jobs)


def is_active(rec: Dict[str, Any]) -> bool:
    """Whether a job can still own a child or capture reconciliation."""
    return (_is_shell(rec) or _is_bound_shell(rec)) and str(rec.get("status") or "") in _ACTIVE_STATUSES
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
        if _STORE.is_symlink():
            raise JobStoreError("job authority cannot be a symlink")
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise JobStoreError("job authority contains duplicate keys")
                result[key] = value
            return result
        data = json.loads(_STORE.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise JobStoreError("job authority is unreadable or malformed") from exc
    if not isinstance(data, dict) or any(not isinstance(key, str) or not isinstance(rec, dict) for key, rec in data.items()):
        raise JobStoreError("job authority is malformed")
    return data


def _save(jobs: Dict[str, Dict[str, Any]]) -> None:
    _load()  # Never overwrite a corrupt authority, including from shell callers.
    atomic_write_json(str(_STORE), jobs, indent=2)


def _pid_alive(pid: Optional[int]) -> bool:
    # Delegates to the platform-safe probe. NB: a bare os.kill(pid, 0) is unsafe
    # on Windows — CPython routes it to TerminateProcess, which would KILL the
    # job we're only trying to check. core.platform_compat.pid_alive handles
    # both OSes correctly.
    return pid_alive(pid)


def _launch_impl(
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
    bound_reservation: Optional[Dict[str, Any]] = None,
    _execution_gate: Optional[tuple[int, tuple[int, int]]] = None,
    _execution_state: Optional[dict] = None,
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

    # Refuse before any process or staging side effect if the authority is bad.
    with _store_lock():
        _load()

    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = str(bound_reservation.get("id")) if bound_reservation is not None else uuid.uuid4().hex[:12]
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
    legacy_nonce = None
    if _execution_gate is not None and bound_reservation is None:
        legacy_nonce = secrets.token_urlsafe(32)
        with _store_lock():
            jobs = _load()
            if job_id in jobs: raise JobStoreError('background job identity unavailable')
            jobs[job_id] = {'id':job_id,'kind':'shell','owner':scope_owner,'session_id':scope_session,
                'workspace':active_workspace,'cwd':active_cwd,'status':'reserved',
                'cleanup_claim_sha256':hashlib.sha256(legacy_nonce.encode()).hexdigest(),'created_at':time.time()}
            _save(jobs)
            if _execution_state is not None: _execution_state.update({'job_id':job_id,'nonce':legacy_nonce})
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
    if bound_reservation is not None:
        if (not _is_bound_shell(bound_reservation) or bound_reservation.get("status") != "reserved"
                or bound_reservation.get("owner") != scope_owner or bound_reservation.get("session_id") != scope_session
                or os.path.realpath(str(bound_reservation.get("workspace") or "")) != active_workspace
                or bound_reservation.get("max_runtime_s") != max_runtime_s
                or bound_reservation.get("command_sha256") != hashlib.sha256(command_bytes).hexdigest()):
            raise ValueError("shell reservation changed")
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
    input_parent=input_child=None
    if bound_reservation is not None and os.name != "nt":
        import socket
        with _BOUND_INPUTS_LOCK:
            for key,entry in list(_BOUND_INPUTS.items()):
                if entry['process'].poll() is not None:
                    entry['socket'].close();_BOUND_INPUTS.pop(key,None)
            if len(_BOUND_INPUTS)>=128: raise JobStoreError("bound input capacity unavailable")
        input_parent,input_child=socket.socketpair()
        spec["bound_input_fd"]=input_child.fileno()
    if bound_reservation is not None:
        spec["bound_shell_nonce"] = bound_reservation["spawn_nonce"]
        spec["bound_max_runtime_s"] = max_runtime_s
    if _execution_gate is not None:
        gate_fd, gate_identity = _execution_gate
        spec['execution_gate'] = {'fd':gate_fd,'device':gate_identity[0],'inode':gate_identity[1]}
        if legacy_nonce is not None: spec['legacy_cleanup_nonce'] = legacy_nonce
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
    if bound_reservation is not None:
        with _store_lock():
            jobs = _load()
            row = jobs.get(job_id)
            if row != bound_reservation:
                raise ValueError("shell reservation changed")
            row.update({"status":"spawning", "spawn_intent_at":time.time(), "command":redact_text(command),
                "destructive_actions":actions, "containment":containment, "network":network_mode,
                "started_at":time.time(), "ended_at":None, "exit_code":None, "pid":None,
                "log_path":str(log_path), "exit_path":str(exit_path), "child_pid_path":str(child_pid_path),
                "stdin_path":stdin_path, "stdin_ready_path":str(stdin_ready_path),
                "capture_state_path":str(capture_state_path), "native_cancel_path":str(native_cancel_path),
                "root_journal_path":str(root_journal_path), "action_id":durable_action_id,
                "run_id":durable_run_id, "task_id":durable_task_id, "tool_id":durable_tool_id,
                "capture_phase":"not_started", "native_shell_owner":bool(os.name == "nt" and hex_check)})
            _save(jobs)
    elif legacy_nonce is not None:
        with _store_lock():
            jobs = _load(); row = jobs.get(job_id)
            if not row or row.get('status') != 'reserved' or row.get('cleanup_claim_sha256') != hashlib.sha256(legacy_nonce.encode()).hexdigest():
                raise JobStoreError('background reservation changed')
            row['status'] = 'spawning'; _save(jobs)
    try:
        if _execution_state is not None: _execution_state['issued'] = True
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.PIPE,
            cwd=project_root,
            env=worker_env,
            **({'pass_fds':tuple(([ _execution_gate[0] ] if _execution_gate is not None else []) + ([input_child.fileno()] if input_child is not None else []))} if os.name != 'nt' else {}),
            **detached_popen_kwargs(),  # detach from the request lifecycle (setsid / DETACHED_PROCESS)
        )
        if input_child is not None:
            input_child.close();input_child=None
            with _BOUND_INPUTS_LOCK:
                if len(_BOUND_INPUTS)>=128:
                    input_parent.close()
                else:
                    _BOUND_INPUTS[(bound_reservation["account_id"],job_id)]={"process":proc,"socket":input_parent,"nonce_hash":hashlib.sha256(bound_reservation["spawn_nonce"].encode()).hexdigest()}
        assert proc.stdin is not None
        proc.stdin.write(command_bytes)
        proc.stdin.close()
    except BaseException:
        if input_child is not None: input_child.close()
        if input_parent is not None and "proc" not in locals(): input_parent.close()
        if bound_reservation is not None:
            # The spawn boundary may have been crossed. Preserve the row and
            # its worker claim for reconciliation; never issue another Popen.
            raise
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
        "kind": "shell",
        "session_id": scope_session,
        "owner": scope_owner,
        "workspace": active_workspace,
        "cwd": active_cwd,
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
        if bound_reservation is not None:
            current = jobs.get(job_id)
            if (not _is_bound_shell(current or {}) or current.get("status") not in ("spawning", "running", "done", "failed", "unknown")
                    or any(current.get(key)!=bound_reservation.get(key) for key in ("identity","invocation_key","command_sha256","max_runtime_s"))
                    or current.get("pid") not in (None,proc.pid)
                    or (current.get("status") == "spawning" and current.get("spawn_nonce") != bound_reservation.get("spawn_nonce"))
                    or (current.get("status") != "spawning" and (current.get("pid") != proc.pid or current.get("worker_claim_sha256") != hashlib.sha256(bound_reservation["spawn_nonce"].encode()).hexdigest()))):
                raise ValueError("shell spawn outcome unavailable")
            current["pid"] = proc.pid
            rec = dict(current)
        elif legacy_nonce is not None:
            current = jobs.get(job_id)
            if not current or current.get('cleanup_claim_sha256') != hashlib.sha256(legacy_nonce.encode()).hexdigest():
                raise JobStoreError('background reservation changed')
            rec.update({key:current[key] for key in ('cleanup_claim_sha256','child_settlement','retired_from_view') if key in current})
            if current.get('child_settlement') is not None:
                rec.update({key:current.get(key) for key in ('status','exit_code','ended_at')})
            jobs[job_id] = rec
        else:
            jobs[job_id] = rec
        _save(jobs)
    return rec


@functools.wraps(_launch_impl)
def launch(*args, **kwargs) -> Dict[str, Any]:
    if sys.platform != 'linux': return _launch_impl(*args, **kwargs)
    reservation=kwargs.get('bound_reservation')
    try:
        gate = _open_execution_gate(exclusive=False)
    except BaseException:
        _settle_bound_not_issued(reservation)
        raise
    state = {'issued':False}
    try:
        return _launch_impl(*args, **kwargs, _execution_gate=gate, _execution_state=state)
    except BaseException:
        if not state['issued'] and reservation is not None: _settle_bound_not_issued(reservation)
        if not state['issued'] and state.get('job_id') and state.get('nonce'):
            with _store_lock():
                jobs=_load(); row=jobs.get(state['job_id'])
                if (row and row.get('status') in ('reserved','spawning') and row.get('cleanup_claim_sha256') == hashlib.sha256(state['nonce'].encode()).hexdigest()):
                    row.update({'status':'failed','cleanup_not_issued':True,'ended_at':time.time()}); _save(jobs)
        raise
    finally:
        # Close only this duplicate. LOCK_UN would also unlock the child's fd.
        os.close(gate[0])


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
        if _is_shell(rec) and not _is_bound_shell(rec) and rec.get("ended_at")
        and (sys.platform != 'linux' or _cleanup_settled(rec))
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
        if path.name == _EXECUTION_GATE.name:
            continue
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
            if _is_bound_shell(rec):
                if rec.get('pid') and not _pid_alive(rec['pid']):
                    rec['status']='unknown'
                    changed=True
                elif rec.get('pid') and (now-rec.get('started_at',now))>rec.get('max_runtime_s',DEFAULT_MAX_RUNTIME_S):
                    rec['timed_out']=True
                    rec['status']='unknown'
                    changed=True
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
            if _is_shell(r) and not _is_bound_shell(r) and r.get('retired_from_view') != 1 and r.get("status") in _TERMINAL_STATUSES and not r.get("followed_up")]


def claim_pending_followups(claimant: str) -> List[Dict[str, Any]]:
    """Atomically lease ready follow-ups to one monitor process."""
    refresh()
    now = time.time()
    claimed = []
    with _store_lock():
        jobs = _load()
        for rec in jobs.values():
            if not _is_shell(rec) or _is_bound_shell(rec) or rec.get('retired_from_view') == 1 or rec.get("status") not in _TERMINAL_STATUSES or rec.get("followed_up"):
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
        if not rec or not _is_shell(rec) or rec.get("followup_claim") != claim_token:
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
        if not rec or not _is_shell(rec) or rec.get("followup_claim") != claim_token:
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
        if job_id in jobs and _is_shell(jobs[job_id]):
            jobs[job_id]["followed_up"] = True
            jobs[job_id]["followup_state"] = "completed"
            _save(jobs)


def get(job_id: str) -> Optional[Dict[str, Any]]:
    refresh()  # reconcile against disk so status/exit_code are current
    rec = _load().get(job_id)
    if rec and _is_shell(rec) and rec.get('retired_from_view') != 1:
        rec = dict(rec)
        rec["output"] = _read_output(rec)
        return rec
    return None


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
    if rec is None or not _is_shell(rec) or rec.get('retired_from_view') == 1:
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
    if rec is None or not _is_shell(rec) or rec.get('retired_from_view') == 1 or not _scope_matches(
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
        if _is_shell(rec) and rec.get('retired_from_view') != 1 and _scope_matches(
            rec,
            session_id=session_id,
            owner=owner,
            workspace=workspace,
        )
    ]


def list_for_session(session_id: str) -> List[Dict[str, Any]]:
    return [r for r in refresh().values() if _is_shell(r) and r.get('retired_from_view') != 1 and r.get("session_id") == session_id]


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
        if sys.platform == 'linux' and not _cleanup_settled(rec):
            rec['retired_from_view'] = 1
            _save(jobs)
            return True
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


def _session_owner_purge_ready(jobs: Dict[str, Dict[str, Any]], session_id: str, owner: str) -> bool:
    return not any((_is_shell(rec) or _is_bound_shell(rec)) and str(rec.get('session_id') or '') == session_id
        and str(rec.get('owner') or '') == owner and not _cleanup_settled(rec) for rec in jobs.values())

def assert_session_owner_purge_ready(*, session_id: str, owner: str, account_id: Optional[str] = None) -> None:
    """Read-only preflight; the later purge independently repeats this check."""
    if sys.platform != 'linux': return
    if not session_id or not owner: raise JobStoreError('background job scope unavailable')
    with _store_lock():
        if not _session_owner_purge_ready(_load(),str(session_id),str(owner)):
            raise JobStoreError('background job settlement is incomplete')

def delete_for_session_owner(*, session_id: str, owner: str, account_id: Optional[str] = None) -> int:
    """Delete every durable shell job owned by one authoritative chat scope."""
    caller_session = str(session_id or "")
    caller_owner = str(owner or "")
    if not caller_session:
        return 0
    removed: list[str] = []
    detached = False
    with _store_lock():
        jobs = _load()
        if sys.platform == 'linux' and not _session_owner_purge_ready(jobs,caller_session,caller_owner):
            raise JobStoreError('background job settlement is incomplete')
        for job_id, rec in list(jobs.items()):
            if (
                str(rec.get("session_id") or "") != caller_session
                or str(rec.get("owner") or "") != caller_owner
            ):
                continue
            if rec.get("kind") == "media" and rec.get("version") == 1:
                if not account_id or rec.get('account_id') != account_id or not isinstance(rec.get('identity'),dict) or rec['identity'].get('account_id') != account_id:
                    continue
                rec.update(session_id=None, actor_id=None, turn_id=None, publication_withdrawn=True, chat_detached=True)
                if isinstance(rec.get('identity'),dict):
                    rec['identity']={**rec['identity'], 'session_id':None, 'actor_id':None, 'turn_id':None}
                    rec['identity_redacted']=True
                rec['revision']=int(rec.get('revision') or 0)+1
                detached = True
                continue
            if not _is_shell(rec):
                continue
            if is_active(rec):
                _kill_record(rec)
            jobs.pop(job_id, None)
            removed.append(job_id)
        if removed or detached:
            _save(jobs)
    for job_id in removed:
        for path in _owned_paths(job_id):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return len(removed)


def detach_media_for_chat(*, session_id: str, owner: str, account_id: str) -> int:
    """Withdraw this current account's chat publication without cancelling jobs."""
    if not session_id or not owner or not account_id: return 0
    changed=0
    with _store_lock():
        jobs=_load()
        for rec in jobs.values():
            identity=rec.get('identity')
            if (rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('session_id')!=session_id
                or rec.get('owner')!=owner or rec.get('account_id')!=account_id or not isinstance(identity,dict)
                or identity.get('account_id')!=account_id): continue
            rec.update(session_id=None,actor_id=None,turn_id=None,publication_withdrawn=True,chat_detached=True,identity_redacted=True)
            rec['identity']={**identity,'session_id':None,'actor_id':None,'turn_id':None}
            rec['revision']=int(rec.get('revision') or 0)+1; changed+=1
        if changed: _save(jobs)
    return changed


_MEDIA_STATES = frozenset({"prepared", "submitting", "queued", "running", "collecting", "stopping", "complete", "failed", "cancel_requested", "cancelled", "unknown_outcome", "reconciling"})
_MEDIA_ACTION_STATES = {
    "submit": {"prepared"},
    "status": {"queued", "running", "stopping", "unknown_outcome", "reconciling", "cancel_requested"},
    "cancel": {"prepared", "queued", "running", "stopping", "cancel_requested"},
    "collect": {"collecting"},
    "publish": {"collecting"},
}
_MEDIA_OUTCOMES = {
    "submit": {"queued", "running", "collecting", "failed", "unknown_outcome"},
    "status": {"queued", "running", "collecting", "failed", "cancelled", "unknown_outcome", "reconciling"},
    "cancel": {"complete", "cancel_requested", "cancelled", "unknown_outcome", "failed"},
    "collect": {"collecting", "failed", "unknown_outcome"},
    "publish": {"complete", "unknown_outcome"},
}

from src.openclank.media_adapters.base import media_foreground_adapters

_S05_MEDIA_MODELS = media_foreground_adapters('model')
_S05_MEDIA_CPU = media_foreground_adapters('cpu')


def _media_facts(identity: Any):
    """Only compiled facts matching the immutable recorded operation qualify."""
    if type(identity) is not dict: return None
    from src.openclank.media_adapters.base import media_operation_facts
    facts = media_operation_facts(identity.get('adapter_id'), identity.get('adapter_version'))
    return facts if facts is not None and identity.get('operation') == facts.operation else None


def _media_foreground_kind(identity: Any):
    facts = _media_facts(identity)
    if facts is not None: return facts.foreground_kind
    adapter = identity.get('adapter_id') if type(identity) is dict else None
    if adapter in _S05_MEDIA_MODELS: return 'model'
    if adapter in _S05_MEDIA_CPU: return 'cpu'
    return None


def _media_owned_fetch(identity: Any, legacy: tuple[str, ...]) -> bool:
    # A known immutable declaration (exact id, version and operation) decides; any other record keeps the snapshot's caller-supplied legacy list.
    facts = _media_facts(identity)
    if facts is not None: return facts.owned_fetch
    return type(identity) is dict and identity.get('adapter_id') in legacy


# Neutral name for the owned-fetch claim marker; the donor-named label written by earlier versions stays readable. Stored records are never rewritten.
OWNED_LIFETIME = 'owned-fetch-v1'
OWNED_LIFETIMES = (OWNED_LIFETIME, 'meshy-fetch-v1')
_SPRIXEN_ANIMATION_TASK_ID = r'anim_[A-Za-z0-9_-]{1,124}'


def _media_task_id_ok(identity: Any, task_id: str) -> bool:
    # A known declaration states its pattern; every other record keeps the snapshot rule (the Sprixen animation id shape only).
    import re
    facts = _media_facts(identity)
    pattern = facts.task_id_pattern if facts is not None else _SPRIXEN_ANIMATION_TASK_ID if (
        type(identity) is dict and identity.get('adapter_id') == 'sprixen-animation-v1') else ''
    return not (pattern and not re.fullmatch(pattern, task_id))


def _media_foreground_identity(record: Dict[str, Any], token: str) -> str:
    import hashlib
    identity=record.get('identity') if type(record) is dict else None
    if (type(identity) is not dict or _media_foreground_kind(identity) != 'model'
        or type(token) is not str or not token or record.get('submission_token')!=token
        or record.get('account_id')!=identity.get('account_id') or type(record.get('identity_digest')) is not str
        or type(record.get('id')) is not str or type(identity.get('route_id')) is not str):raise ValueError('media foreground identity unavailable')
    fields=['s05.media.foreground.v1',record['account_id'],record['id'],token,record['identity_digest'],identity['route_id']]
    return 'fg_media_'+hashlib.sha256(json.dumps(fields,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def media_foreground_operation(record: Dict[str, Any], token: str) -> str:
    """Derive a stable, content-free operation from the exact J0 submit claim."""
    if ((record.get('claim') or {}).get('token')!=token or (record.get('claim') or {}).get('action')!='submit'
        or record.get('status')!='submitting' or record.get('identity_redacted')):raise ValueError('media foreground identity unavailable')
    return _media_foreground_identity(record,token)

def _valid_media_foreground(record: Dict[str, Any], binding: Any) -> bool:
    fields={'version','operation_id','account_id','memory_owner','connection_id','coverage_generation','clock_generation','observer_identity'}
    try:
        return (type(binding) is dict and set(binding)==fields and type(binding['version']) is int and binding['version']==1
            and all(type(binding[k]) is str and bool(binding[k]) for k in fields-{'version'})
            and binding['account_id']==record['account_id'] and binding['connection_id']==record['identity']['route_id']
            and binding['operation_id']==_media_foreground_identity(record,record.get('submission_token')))
    except (KeyError,TypeError,ValueError):return False

def bind_media_foreground(job_id: str, *, account_id: str, token: str, binding: Dict[str, Any]) -> None:
    fields={'version','operation_id','account_id','memory_owner','connection_id','coverage_generation','clock_generation','observer_identity'}
    if type(binding) is not dict or set(binding)!=fields or type(binding['version']) is not int or binding['version']!=1 or any(type(binding[k]) is not str or not binding[k] for k in fields-{'version'}):raise ValueError('media foreground binding unavailable')
    with _store_lock():
        jobs=_load();row=jobs.get(job_id)
        if (not row or row.get('account_id')!=account_id or binding['account_id']!=account_id
            or media_foreground_operation(row,token)!=binding['operation_id'] or row['identity']['route_id']!=binding['connection_id']
            or row.get('model_foreground') not in (None,binding)):raise ValueError('media foreground changed')
        row['model_foreground']=dict(binding);row['revision']+=1;row['updated_at']=time.time();_save(jobs)

def record_media_presend_unsent(job_id: str, *, account_id: str, token: str, operation_id: str, absent_coverage: tuple[str,str] | None = None) -> None:
    """Retain the host's known pre-task outcome, even if FM accounting is unknown."""
    with _store_lock():
        jobs=_load();row=jobs.get(job_id)
        if not row or row.get('account_id')!=account_id or media_foreground_operation(row,token)!=operation_id:raise ValueError('media pre-send authority changed')
        old=row.get('model_presend');value={'version':1,'operation_id':operation_id,'fact':'not_dispatched','accounting':'unknown'}
        if absent_coverage is not None:
            if type(absent_coverage) is not tuple or len(absent_coverage)!=2 or any(type(v) is not str or not v for v in absent_coverage):raise ValueError('media pre-send accounting unavailable')
            value.update(accounting='fm_absent',coverage_generation=absent_coverage[0],clock_generation=absent_coverage[1])
        if old is not None and (type(old) is not dict or any(old.get(k)!=value[k] for k in ('version','operation_id','fact')) or old.get('accounting') not in ('unknown',value['accounting'])):raise ValueError('media pre-send conflict')
        row['model_presend']=value;row['revision']+=1;row['updated_at']=time.time();_save(jobs)

def record_media_model_terminal(job_id: str, *, account_id: str, token: str, fact: str) -> Dict[str, Any]:
    """Retain a trusted terminal model fact, independent of Files publication."""
    if fact not in {'not_dispatched','completed','failed','cancelled'}:raise ValueError('invalid model terminal fact')
    with _store_lock():
        jobs=_load();row=jobs.get(job_id);claim=row.get('claim') if row else None;binding=row.get('model_foreground') if row else None
        active=isinstance(claim,dict) and claim.get('token')==token
        late_cancel=bool(row and fact=='cancelled' and row.get('cancellation_token')==token and row.get('status') in {'cancel_requested','unknown_outcome','reconciling'} and row.get('unknown_action') in (None,'cancel'))
        if (not row or row.get('account_id')!=account_id or not _valid_media_foreground(row,binding)
            or (fact=='not_dispatched' and not (active and claim.get('action')=='submit'))
            or (fact!='not_dispatched' and (not row.get('remote_task_id') or not ((active and claim.get('action')=='status') or (fact=='cancelled' and ((active and claim.get('action')=='cancel') or late_cancel)))))):
            raise ValueError('media terminal authority unavailable')
        value={'version':1,'fact':fact,'operation_id':binding['operation_id']}
        if row.get('model_terminal') not in (None,value):raise ValueError('media terminal conflict')
        row['model_terminal']=value;row['revision']+=1;row['updated_at']=time.time();_save(jobs)
        return dict(binding)

def acknowledge_media_model_settlement(job_id: str, *, account_id: str, binding: Dict[str, Any], mode: str) -> None:
    """Persist only the exact already-settled FM or explicitly unobserved result."""
    modes={'fm_settled','shadow_settled','no_store','shadow_unobserved'}
    if mode not in modes:raise ValueError('media settlement unavailable')
    with _store_lock():
        jobs=_load();row=jobs.get(job_id);terminal=row.get('model_terminal') if row else None
        if (not row or row.get('account_id')!=account_id or row.get('model_foreground')!=binding or not _valid_media_foreground(row,binding)
            or type(terminal) is not dict or terminal.get('operation_id')!=binding['operation_id']
            or terminal.get('fact') not in {'not_dispatched','completed','failed','cancelled'}):raise ValueError('media settlement changed')
        expected_mode={'media_shadow_v1':'shadow_settled','media_no_store_v1':'no_store','media_unobserved_v1':'shadow_unobserved'}.get(binding['coverage_generation'])
        if binding['coverage_generation']=='media_unknown_v1' or (expected_mode is not None and (mode!=expected_mode or binding['clock_generation']!=binding['coverage_generation'])) or (expected_mode is None and (mode!='fm_settled' or binding['coverage_generation'].startswith('media_'))):raise ValueError('media settlement mode unavailable')
        expected={'version':1,'operation_id':binding['operation_id'],'coverage_generation':binding['coverage_generation'],
                  'clock_generation':binding['clock_generation'],'fact':terminal['fact'],'mode':mode}
        if row.get('model_settlement') not in (None,expected):raise ValueError('media settlement conflict')
        row['model_settlement']=expected;row['revision']+=1;row['updated_at']=time.time();_save(jobs)

def media_model_lifecycle_safe(row: Dict[str, Any]) -> bool:
    """Unknown/legacy dispatch never becomes a terminal fact by status alone."""
    if (row.get('status') in {'prepared','cancelled'} and row.get('claim') is None and row.get('submission_token') is None and row.get('model_foreground') is None):return True
    identity=row.get('identity');adapter=identity.get('adapter_id') if type(identity) is dict else None
    kind=_media_foreground_kind(identity)
    if kind == 'cpu':return True
    if kind != 'model':return False
    presend=row.get('model_presend')
    if (type(presend) is dict and set(presend)=={'version','operation_id','fact','accounting','coverage_generation','clock_generation'}
        and type(presend['version']) is int and presend['version']==1 and presend['fact']=='not_dispatched' and presend['accounting']=='fm_absent'
        and type(presend['coverage_generation']) is str and bool(presend['coverage_generation']) and type(presend['clock_generation']) is str and bool(presend['clock_generation'])):
        try:
            if presend['operation_id']==_media_foreground_identity(row,row.get('submission_token')) and row.get('model_foreground') is None:return True
        except (TypeError,ValueError,KeyError):pass
    binding=row.get('model_foreground');terminal=row.get('model_terminal');ack=row.get('model_settlement')
    if not _valid_media_foreground(row,binding) or type(terminal) is not dict or type(ack) is not dict:return False
    if set(terminal)!={'version','fact','operation_id'} or type(terminal.get('version')) is not int or terminal['version']!=1 or terminal.get('fact') not in {'not_dispatched','completed','failed','cancelled'}:return False
    if set(ack)!={'version','operation_id','coverage_generation','clock_generation','fact','mode'} or type(ack.get('version')) is not int or ack['version']!=1:return False
    expected_mode={'media_shadow_v1':'shadow_settled','media_no_store_v1':'no_store','media_unobserved_v1':'shadow_unobserved'}.get(binding['coverage_generation'])
    if expected_mode is None:
        if binding['coverage_generation'].startswith('media_'):return False
        expected_mode='fm_settled'
    elif binding['clock_generation']!=binding['coverage_generation']:return False
    return (ack['operation_id']==binding['operation_id']==terminal['operation_id'] and ack['coverage_generation']==binding['coverage_generation']
        and ack['clock_generation']==binding['clock_generation'] and ack['fact']==terminal['fact'] and ack['mode']==expected_mode)

def _bound_shell_model_idle(job_id: str, row: dict) -> bool:
    """Recognize only writer-versioned absence or exact settled publication."""
    identity = row.get('identity')
    fields = {'account_id','owner','session_id','engine_session_id','workspace','workspace_id',
              'root_operation_id','actor_id','execution_revision','turn_id','message_id','tool_call_id',
              'holder','generation','privacy'}
    if (row.get('id') != job_id or type(identity) is not dict or set(identity) != fields
            or any(type(identity[k]) is not str or not identity[k] or len(identity[k]) > 1024
                   for k in fields - {'execution_revision','generation','privacy'})
            or type(identity['execution_revision']) is not int or identity['execution_revision'] < 0
            or type(identity['generation']) is not int or identity['generation'] < 0
            or identity['privacy'] not in ('normal','private')
            or any(row.get(k) != identity[k] for k in ('owner','account_id','session_id','workspace'))):
        return False
    follow = row.get('bound_followup')
    if follow is None and 'bound_followup' not in row:
        return type(row.get('bound_followup_protocol')) is int and row['bound_followup_protocol'] == 1
    if (type(follow) is not dict or follow.get('state') != 'published'
            or follow.get('cleanup_state') != 'settled'
            or any(type(follow.get(k)) is not str or not follow[k] or len(follow[k]) > 512
                   for k in ('attempt_id','claim_id','message_id'))):
        return False
    for value, digest, limit in (('shell_output','shell_sha256',262144), ('route_json','route_sha256',16384),
                                 ('body','body_sha256',262144), ('metadata_json','metadata_sha256',65536)):
        data = follow.get(value)
        if (type(data) is not str or len(data.encode('utf-8')) > limit
                or follow.get(digest) != hashlib.sha256(data.encode('utf-8')).hexdigest()):
            return False
    try:
        if type(json.loads(follow['route_json'])) is not dict or type(json.loads(follow['metadata_json'])) is not dict:
            return False
    except (ValueError, UnicodeError):
        return False
    return True


def assert_media_foreground_startup_clear() -> None:
    """Cold qualification never guesses an unresolved prior model job idle."""
    with _store_lock():
        try:
            if _STORE.is_symlink():raise JobStoreError('job authority cannot be a symlink')
            with _STORE.open('rb') as stream: raw=stream.read(16*1024*1024+1)
        except FileNotFoundError:return
        except OSError as exc:raise JobStoreError('media inventory unavailable') from exc
    if len(raw)>16*1024*1024:raise JobStoreError('media inventory exceeds bounds')
    def unique(items):
        result={}
        for key,value in items:
            if key in result:raise JobStoreError('duplicate media inventory')
            result[key]=value
        return result
    try:jobs=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError) as exc:raise JobStoreError('media inventory unavailable') from exc
    if type(jobs) is not dict or len(jobs)>10000:raise JobStoreError('media inventory exceeds bounds')
    for job_id,row in jobs.items():
        if type(row) is not dict:raise JobStoreError('unknown media inventory')
        if _is_shell(row):raise JobStoreError('unverified legacy shell model work')
        if _is_bound_shell(row):
            if not _bound_shell_model_idle(job_id,row):raise JobStoreError('unresolved shell model work')
            continue
        if row.get('kind')!='media' or row.get('version')!=1 or row.get('id')!=job_id or type(row.get('identity')) is not dict:raise JobStoreError('unknown media inventory')
        adapter=row['identity'].get('adapter_id')
        kind=_media_foreground_kind(row['identity'])
        if kind == 'cpu':continue
        if kind != 'model':raise JobStoreError('unknown media adapter')
        if row.get('status')=='prepared' and row.get('claim') is None and row.get('submission_token') is None:continue
        if not media_model_lifecycle_safe(row):raise JobStoreError('unresolved media model work')


def _media_text(value: Any, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > limit or any(ord(c) < 32 for c in value) or '://' in value:
        raise ValueError(f"invalid media {field}")
    return value


def _media_hash(value: Any) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError('invalid media digest')
    return value


def _media_artifact_for(rec: dict, value: Any) -> dict:
    import re
    identity=rec.get('identity') or {}; facts=_media_facts(identity)
    adapter=identity.get('adapter_id')
    kind=facts.metadata_kind if facts is not None else {
        'sprixen-animation-v1':'animation', 'blender-glb-four-heading-v1':'four_heading',
        'blender-glb-rig-clip-v1':'rig_clip', 'sprite-sheet-v1':'sprite_sheet',
        'sprite-sheet-crops-v1':'sprite_crops', 'comfyui-flac-v1':'audio'}.get(adapter, 'none')
    animation=kind=='animation'; sheet=kind=='four_heading'; rigclip=kind=='rig_clip'
    sprites=kind=='sprite_sheet'; crops=kind=='sprite_crops'; audio=kind=='audio'
    fields={'artifact_id','sha256','mime_type','size'} | ({'animation'} if animation else set()) | ({'layout'} if sheet or rigclip or sprites or crops else set()) | ({'audio'} if audio else set())
    if type(value) is not dict or set(value)!=fields or type(value['size']) is not int or not 0<value['size']<=268435456:
        raise ValueError('invalid media artifact')
    clean={'artifact_id':_media_text(value['artifact_id'],'artifact id',256),'sha256':_media_hash(value['sha256']),
        'mime_type':_media_text(value['mime_type'],'MIME',128),'size':value['size']}
    adapter=(rec.get('identity') or {}).get('adapter_id')
    expected='video/webm' if adapter in ('screen-record-v1','trim-webm-v1') else 'application/pdf' if adapter=='latex-pdflatex-v1' else 'model/gltf-binary' if adapter in ('meshy-preview-v1','meshy-refine-v1','tripo-text3d-v1','fal-hunyuan-image3d-v1') else 'audio/flac' if adapter=='comfyui-flac-v1' else 'image/png'
    if facts is not None:
        expected=facts.mime_type
        max_size=facts.artifact_max_bytes
    else:
        max_size=32*1024*1024 if adapter in ('latex-pdflatex-v1','fal-hunyuan-image3d-v1') else 64*1024*1024 if adapter in ('screen-record-v1','trim-webm-v1') else 268435456
    if clean['mime_type']!=expected or clean['size']>max_size: raise ValueError('invalid media artifact MIME')
    if audio:
        meta=value['audio']
        if (type(meta) is not dict or set(meta)!={'sampleRate','channels','bitsPerSample','declaredSamples','payloadVerified'}
            or any(type(meta.get(k)) is not int for k in ('sampleRate','channels','bitsPerSample','declaredSamples'))
            or not 8000<=meta['sampleRate']<=192000 or meta['channels'] not in (1,2)
            or meta['bitsPerSample'] not in (16,24) or not 0<meta['declaredSamples']<=meta['sampleRate']*600
            or meta.get('payloadVerified') is not False):
            raise ValueError('invalid FLAC header witness')
        clean['audio']=dict(meta)
    if animation:
        meta=value['animation']; binding=(rec.get('identity') or {}).get('resource_binding') or {}
        params=binding.get('animationParameters') or {}
        if (type(meta) is not dict or set(meta)!={'animationID','type','frameCount','fps','width','height','sha256'}
                or type(meta.get('animationID')) is not str or not re.fullmatch(r'anim_[A-Za-z0-9_-]{1,124}',meta['animationID'])
                or meta['animationID']!=rec.get('remote_task_id') or meta.get('sha256')!=clean['sha256']
                or any(meta.get(k)!=params.get(k) for k in ('type','frameCount','fps'))
                or meta.get('type') not in ('idle','walk','run','attack','jump','death','hurt')
                or type(meta.get('frameCount')) is not int or not 2<=meta['frameCount']<=12
                or type(meta.get('fps')) is not int or not 4<=meta['fps']<=60
                or type(meta.get('width')) is not int or not 1<=meta['width']<=4096
                or type(meta.get('height')) is not int or not 1<=meta['height']<=4096
                or meta['width']*meta['height']>16777216):
            raise ValueError('invalid animation artifact')
        clean['animation']=dict(meta)
    if sheet:
        layout=value['layout']
        expected={'version':1,'coordinateFrame':'imported-world-z','cellWidth':512,'cellHeight':512,
                  'sheetWidth':1024,'sheetHeight':1024,
                  'views':[{'degrees':d,'x':x,'y':y,'width':512,'height':512}
                           for d,x,y in ((0,0,0),(90,512,0),(180,0,512),(270,512,512))],
                  'sha256':clean['sha256']}
        if type(layout) is not dict or layout != expected or any(type(layout.get(k)) is not int for k in ('version','cellWidth','cellHeight','sheetWidth','sheetHeight')) or any(type(v.get(k)) is not int for v in layout.get('views',[]) for k in ('degrees','x','y','width','height')):
            raise ValueError('invalid sheet layout')
        clean['layout']=expected
    if rigclip:
        from src.openclank.blender_rig_metadata import layout_for
        layout=value['layout']; binding=rec['identity']['resource_binding']; importer=layout.get('importer') if isinstance(layout,dict) else None
        if (not isinstance(importer,dict) or set(importer)!={'blenderVersion','operator','module','moduleVersion','sourceFileSHA256','settings'}
            or importer.get('operator')!='IMPORT_SCENE_OT_gltf' or importer.get('settings')!='factory-default-gltf-v1'
            or any(not isinstance(importer.get(k),str) or not importer[k] or len(importer[k])>128 for k in ('blenderVersion','module','moduleVersion'))
            or (importer.get('sourceFileSHA256') is not None and _media_hash(importer['sourceFileSHA256'])!=importer['sourceFileSHA256'])
            or any(type(layout.get(k)) is not int for k in ('version','cellWidth','cellHeight','sheetWidth','sheetHeight','fps'))
            or not isinstance(layout.get('frames'),list) or any(not isinstance(v,dict) or any(type(v.get(k)) is not int for k in ('frameIndex','degrees','x','y','width','height')) for v in layout['frames'])
            or layout!=layout_for(clean['sha256'],binding['rig'],importer)):
            raise ValueError('invalid rig clip metadata')
        clean['layout']=json.loads(json.dumps(layout))
    if sprites:
        from src.openclank.sprite_sheet import layout_for
        layout=value['layout']; binding=rec['identity']['resource_binding']; side=layout.get('cellWidth') if isinstance(layout,dict) else None
        inputs=[{k:source[k] for k in ('resourceID','revision','sha256')} for source in binding['inputs']]
        if (type(side) is not int or side!=binding['cellSize'] or not 1<=side<=512 or layout!=layout_for(clean['sha256'],inputs,side,binding['annotations'])
                or any(type(layout.get(k)) is not int for k in ('version','cellWidth','cellHeight','sheetWidth','sheetHeight','fps'))
                or any(type(frame.get(k)) is not int for frame in layout.get('frames',[]) for k in ('index','x','y','width','height'))
                or any(not isinstance(frame.get('pivot'),dict) or any(type(frame['pivot'].get(k)) is not int for k in ('x','y')) for frame in layout.get('frames',[]))):
            raise ValueError('invalid sprite sheet metadata')
        clean['layout']=json.loads(json.dumps(layout))
    if crops:
        from src.openclank.sprite_sheet import selected_layout_for
        binding=rec['identity']['resource_binding'];source={k:binding['inputs'][0][k] for k in ('resourceID','revision','sha256')}
        expected=selected_layout_for(clean['sha256'],source,binding['sourceWidth'],binding['sourceHeight'],binding['rectangles'],binding['annotations'])
        try:matches=json.dumps(value['layout'],sort_keys=True,separators=(',',':'))==json.dumps(expected,sort_keys=True,separators=(',',':'))
        except (TypeError,ValueError):raise ValueError('invalid selected sheet layout')
        if not matches:raise ValueError('invalid selected sheet layout')
        clean['layout']=json.loads(json.dumps(expected))
    return clean


def _media_result_for(rec: dict, value: Any, operation_id: str) -> dict:
    artifact=rec.get('artifact') or {}; identity=rec.get('identity') or {}; facts=_media_facts(identity)
    adapter=identity.get('adapter_id')
    kind=facts.metadata_kind if facts is not None else {
        'sprixen-animation-v1':'animation', 'comfyui-flac-v1':'audio',
        'blender-glb-four-heading-v1':'four_heading', 'blender-glb-rig-clip-v1':'rig_clip',
        'sprite-sheet-v1':'sprite_sheet', 'sprite-sheet-crops-v1':'sprite_crops'}.get(adapter, 'none')
    animation=kind=='animation'; audio=kind=='audio'
    sheet=kind in ('four_heading','rig_clip','sprite_sheet','sprite_crops')
    fields={'operation_id','item_id','resource_ref','sha256','mime_type'} | ({'animation'} if animation else set()) | ({'layout'} if sheet else set()) | ({'audio'} if audio else set())
    if (type(value) is not dict or set(value)!=fields or value.get('operation_id')!=operation_id
            or value.get('item_id')!='output' or value.get('sha256')!=artifact.get('sha256')
            or value.get('mime_type')!=artifact.get('mime_type')
            or (animation and value.get('animation')!=artifact.get('animation'))
            or (audio and value.get('audio')!=artifact.get('audio'))
            or (sheet and value.get('layout')!=artifact.get('layout'))):
        raise ValueError('invalid media result')
    clean={'operation_id':operation_id,'item_id':'output','resource_ref':_media_text(value['resource_ref'],'result ref',16384),
        'sha256':_media_hash(value['sha256']),'mime_type':_media_text(value['mime_type'],'MIME',128)}
    if animation: clean['animation']=dict(value['animation'])
    if audio: clean['audio']=_media_artifact_for(rec,artifact)['audio']
    if sheet: clean['layout']=_media_artifact_for(rec,artifact)['layout']
    return clean


_MEDIA_BINDING_REQUIRED_TEXT = {'trellis-image3d-v1':'TRELLIS source binding required','hunyuan3d-v2-shape-v1':'Hunyuan source binding required',
    'blender-material-bake-v1':'bake source binding required','material-pack-v1':'material source binding required',
    'voidless-image-v1':'Voidless profile binding required','meshy-rig-v1':'rig source binding required','fal-hunyuan-image3d-v1':'image source binding required'}


def reserve_media(identity: Dict[str, Any], *, with_created: bool = False):
    """Reserve one closed media request before any adapter side effect."""
    fields = {'account_id','owner','session_id','workspace_id','actor_id','turn_id','root_operation_id','attempt_id','request_digest','adapter_id','adapter_version','operation','route_id','route_kind','model_revision','policy_generation','input_hashes'}
    if not isinstance(identity, dict) or set(identity) not in (fields, fields | {'resource_binding'}):
        raise ValueError('invalid media identity')
    clean = {key:_media_text(identity[key],key) for key in fields - {'request_digest','policy_generation','input_hashes'}}
    from src.openclank.media_adapters.base import NO_DECLARATION, media_declaration_for_id
    # An id without a declaration is reserved as at the snapshot (older records and fixtures use such ids); it gets no declared
    # interface, so any branch that needs one refuses it exactly as the snapshot's literal checks did.
    facts = media_declaration_for_id(clean['adapter_id']) or NO_DECLARATION
    clean['request_digest'] = _media_hash(identity['request_digest'])
    if clean['route_kind'] not in {'local','remote'}: raise ValueError('invalid media route kind')
    if type(identity['policy_generation']) is not int or identity['policy_generation'] < 0:
        raise ValueError('invalid media policy generation')
    clean['policy_generation'] = identity['policy_generation']
    hashes = identity['input_hashes']
    if not isinstance(hashes,list) or len(hashes)>32:
        raise ValueError('invalid media input hashes')
    clean['input_hashes'] = [_media_hash(value) for value in hashes]
    if facts.binding_required and (type(identity.get('resource_binding')) is not dict or identity['resource_binding'].get('interface')!=facts.binding_interface): raise ValueError(_MEDIA_BINDING_REQUIRED_TEXT[clean['adapter_id']])  # declared by the adapter's facts; text stays per adapter
    if 'resource_binding' in identity:
        binding = identity['resource_binding']
        if isinstance(binding, dict) and binding.get('interface') in ('sprixen-sprite-v1','meshy-preview-v1','tripo-text3d-v1','fal-flux-schnell-v1','voidless-image-v1'):
            required = {'connectionID','connectionRevision','interface','ownerAccountID','providerAccountID',
                        'providerAccountRevision','credentialVersion','destination_ref','destination_revision',
                        'destination_name','project_ref','project_revision'}
            animation=clean['adapter_id']=='sprixen-animation-v1'
            refine=clean['adapter_id']=='meshy-refine-v1'
            rig=clean['adapter_id']=='meshy-rig-v1'
            image_mesh=clean['adapter_id']=='fal-hunyuan-image3d-v1'
            required |= set(facts.binding_extra_fields)
            if clean['adapter_id']=='voidless-image-v1' and (clean['operation']!='image.generate' or clean['adapter_version']!='1' or clean['model_revision']!='flux2-klein' or clean['input_hashes']!=[]): raise ValueError('invalid Voidless identity')
            if (set(binding) != required or clean['route_kind'] != 'remote' or binding.get('interface') != facts.binding_interface
                    or (animation and (clean['operation']!=facts.operation or clean['model_revision']!=facts.fixed_model_revision or clean['adapter_version']!='1'))
                    or (refine and (clean['operation']!=facts.operation or clean['adapter_version']!='1'))
                    or (rig and (clean['operation']!=facts.operation or clean['adapter_version']!='1' or clean['model_revision']!=facts.fixed_model_revision))
                    or (image_mesh and (clean['operation']!=facts.operation or clean['adapter_version']!='1' or clean['model_revision']!=facts.fixed_model_revision))):
                raise ValueError('invalid remote media resource binding')
            clean_binding = {key:_media_text(binding[key],key,4096 if key.endswith('_ref') else 256)
                             for key in ('connectionID','interface','ownerAccountID','providerAccountID','destination_ref','destination_name')}
            for key in ('connectionRevision','providerAccountRevision','credentialVersion'):
                if type(binding[key]) is not int or binding[key] < 1: raise ValueError('invalid Sprixen profile revision')
                clean_binding[key] = binding[key]
            if clean_binding['connectionID'] != clean['route_id'] or clean_binding['ownerAccountID'] != clean['account_id']:
                raise ValueError('invalid Sprixen account binding')
            revision = binding['destination_revision']
            if not isinstance(revision,dict) or set(revision) != {'kind','value'}:
                raise ValueError('invalid Sprixen destination revision')
            clean_binding['destination_revision'] = {part:_media_text(revision[part],part,512) for part in ('kind','value')}
            if binding['project_ref'] is not None or binding['project_revision'] is not None:
                raise ValueError('project-aware Sprixen outputs unavailable')
            clean_binding['project_ref'] = clean_binding['project_revision'] = None
            if rig:
                revision=binding['source_revision']
                if type(revision) is not dict or set(revision)!={'kind','value'}: raise ValueError('invalid rig source revision')
                clean_binding['source_ref']=_media_text(binding['source_ref'],'source ref',16384)
                clean_binding['source_revision']={key:_media_text(revision[key],key,512) for key in ('kind','value')}
                clean_binding['source_sha256']=_media_hash(binding['source_sha256'])
                if clean['input_hashes']!=[clean_binding['source_sha256']]: raise ValueError('rig source changed')
            if image_mesh:
                source=binding['image_source']
                if (type(source) is not dict or set(source)!={'ref','resourceID','revision','sha256','mime_type','width','height','name'}
                    or type(source['revision']) is not dict or set(source['revision'])!={'kind','value'}
                    or source['mime_type'] not in ('image/png','image/jpeg')
                    or type(source['width']) is not int or type(source['height']) is not int
                    or not 1<=source['width']<=4096 or not 1<=source['height']<=4096
                    or source['width']*source['height']>16777216):
                    raise ValueError('invalid image source witness')
                name=_media_text(source['name'],'name',240)
                clean_binding['image_source']={'ref':_media_text(source['ref'],'ref',16384),
                    'resourceID':_media_text(source['resourceID'],'resource',512),
                    'revision':{k:_media_text(source['revision'][k],k,512) for k in ('kind','value')},
                    'sha256':_media_hash(source['sha256']),'mime_type':source['mime_type'],
                    'width':source['width'],'height':source['height'],'name':name}
                if clean['input_hashes'] != [clean_binding['image_source']['sha256']]:
                    raise ValueError('image source hash changed')
            if refine:
                for key in ('sourceJobID','sourceTaskID','sourceArtifactID'):
                    clean_binding[key]=_media_text(binding[key],key,512)
                if type(binding['sourceRevision']) is not int or binding['sourceRevision']<1: raise ValueError('invalid source revision')
                clean_binding['sourceRevision']=binding['sourceRevision']
                clean_binding['sourceSHA256']=_media_hash(binding['sourceSHA256'])
                if clean_binding['sourceSHA256'] not in clean['input_hashes']: raise ValueError('refine source unavailable')
            if animation:
                import re
                for key in ('projectID','spriteID'):
                    value=binding[key]
                    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value):
                        raise ValueError('invalid animation source identity')
                    clean_binding[key]=value
                clean_binding['sourceSHA256']=_media_hash(binding['sourceSHA256'])
                if clean_binding['sourceSHA256'] not in clean['input_hashes']:
                    raise ValueError('animation source witness unavailable')
                parameters=binding['animationParameters']
                if (type(parameters) is not dict or set(parameters)!={'type','frameCount','fps'}
                        or parameters.get('type') not in ('idle','walk','run','attack','jump','death','hurt')
                        or type(parameters.get('frameCount')) is not int or not 2<=parameters['frameCount']<=12
                        or type(parameters.get('fps')) is not int or not 4<=parameters['fps']<=60):
                    raise ValueError('invalid animation parameters')
                clean_binding['animationParameters']=dict(parameters)
        elif isinstance(binding, dict) and binding.get('interface') == 'trim-webm-v1':
            from src.openclank.trim_contract import validate_binding
            if (facts.binding_interface != binding['interface'] or clean['route_id'] != clean['adapter_id']
                or clean['operation'] != facts.operation or clean['adapter_version'] != '1'
                or clean['model_revision'] != 'fixed-v1' or clean['route_kind'] != facts.route_kind):
                raise ValueError('invalid trim job identity')
            clean_binding = validate_binding(binding,account=clean['account_id'],hashes=clean['input_hashes'])
        elif isinstance(binding, dict) and binding.get('interface') == 'screen-record-v1':
            from src.openclank.recording_contract import validate_binding
            if (facts.binding_interface != binding['interface'] or clean['route_id'] != clean['adapter_id']
                or clean['operation'] != facts.operation or clean['adapter_version'] != '1'
                or clean['model_revision'] != 'fixed-v1' or clean['route_kind'] != facts.route_kind):
                raise ValueError('invalid recording job identity')
            clean_binding = validate_binding(binding, account=clean['account_id'], hashes=clean['input_hashes'])
        elif isinstance(binding, dict) and binding.get('interface') == 'latex-pdflatex-v1':
            from src.openclank.latex_contract import validate_binding
            if (facts.binding_interface != binding['interface'] or clean['route_id'] != clean['adapter_id']
                or clean['operation'] != facts.operation or clean['adapter_version'] != '1'
                or clean['model_revision'] != 'fixed-v1' or clean['route_kind'] != facts.route_kind):
                raise ValueError('invalid LaTeX job identity')
            clean_binding = validate_binding(binding,account=clean['account_id'],hashes=clean['input_hashes'])
        elif isinstance(binding, dict) and binding.get('interface') == 'material-pack-v1':
            if (set(binding)!={'interface','ownerAccountID','inputs','manifest_sha256','destination_ref','destination_revision','destination_name'}
                    or facts.binding_interface!=binding['interface'] or clean['adapter_version']!='1' or clean['route_id']!=clean['adapter_id']
                    or clean['operation']!=facts.operation or clean['model_revision']!='fixed-v1' or clean['route_kind']!=facts.route_kind
                    or binding['ownerAccountID']!=clean['account_id'] or type(binding['inputs']) is not list
                    or [v.get('role') if type(v) is dict else None for v in binding['inputs']] not in
                        (['baseColor','roughness','metallic'], ['baseColor','roughness','metallic','normal'])):
                raise ValueError('invalid material binding')
            inputs=[]
            for item in binding['inputs']:
                if (type(item) is not dict or set(item)!={'role','ref','resourceID','revision','sha256','mime_type','width','height','name'}
                        or type(item['revision']) is not dict or set(item['revision'])!={'kind','value'}
                        or item['mime_type']!='image/png' or type(item['width']) is not int or type(item['height']) is not int
                        or not 1<=item['width']<=2048 or not 1<=item['height']<=2048): raise ValueError('invalid material source')
                inputs.append({'role':item['role'],'ref':_media_text(item['ref'],'ref',16384),
                    'resourceID':_media_text(item['resourceID'],'resource',256),
                    'revision':{k:_media_text(item['revision'][k],k,512) for k in ('kind','value')},
                    'sha256':_media_hash(item['sha256']),'mime_type':'image/png','width':item['width'],'height':item['height'],
                    'name':_media_text(item['name'],'name',256)})
            if (len({v['resourceID'] for v in inputs})!=len(inputs) or len({v['ref'] for v in inputs})!=len(inputs)
                    or len({(v['width'],v['height']) for v in inputs})!=1
                    or [v['sha256'] for v in inputs]!=clean['input_hashes']): raise ValueError('material sources changed')
            rev=binding['destination_revision']
            if type(rev) is not dict or set(rev)!={'kind','value'}: raise ValueError('invalid destination')
            clean_binding={'interface':'material-pack-v1','ownerAccountID':clean['account_id'],'inputs':inputs,
                'manifest_sha256':_media_hash(binding['manifest_sha256']),
                'destination_ref':_media_text(binding['destination_ref'],'destination',16384),
                'destination_name':_media_text(binding['destination_name'],'name',256),
                'destination_revision':{k:_media_text(rev[k],k,512) for k in ('kind','value')}}
        elif isinstance(binding, dict) and binding.get('interface') == 'sprite-sheet-v1':
            from src.openclank.sprite_sheet import annotations
            if (set(binding)!={'interface','ownerAccountID','inputs','annotations','cellSize','destination_ref','destination_revision','destination_name'}
                    or facts.binding_interface!=binding['interface'] or clean['adapter_version']!='1' or clean['route_id']!=clean['adapter_id']
                    or clean['operation']!=facts.operation or clean['model_revision']!='fixed-v1' or clean['route_kind']!=facts.route_kind or binding['ownerAccountID']!=clean['account_id']
                    or not isinstance(binding['inputs'],list) or len(binding['inputs'])!=4): raise ValueError('invalid sprite binding')
            inputs=[]
            for item in binding['inputs']:
                if not isinstance(item,dict) or set(item)!={'ref','resourceID','revision','sha256'} or not isinstance(item['revision'],dict) or set(item['revision'])!={'kind','value'}: raise ValueError('invalid sprite source')
                inputs.append({'ref':_media_text(item['ref'],'ref',16384),'resourceID':_media_text(item['resourceID'],'resource',256),
                    'revision':{k:_media_text(item['revision'][k],k,512) for k in ('kind','value')},'sha256':_media_hash(item['sha256'])})
            if [v['sha256'] for v in inputs]!=clean['input_hashes']: raise ValueError('sprite source hashes changed')
            rev=binding['destination_revision']
            if not isinstance(rev,dict) or set(rev)!={'kind','value'}: raise ValueError('invalid destination')
            if type(binding['cellSize']) is not int or not 1<=binding['cellSize']<=512: raise ValueError('invalid sprite frame size')
            if any(p['x']>binding['cellSize'] or p['y']>binding['cellSize'] for p in annotations(binding['annotations'])['pivots']): raise ValueError('invalid sprite pivot')
            clean_binding={'interface':'sprite-sheet-v1','ownerAccountID':clean['account_id'],'inputs':inputs,'annotations':annotations(binding['annotations']),'cellSize':binding['cellSize'],
                'destination_ref':_media_text(binding['destination_ref'],'destination',16384),'destination_name':_media_text(binding['destination_name'],'name',256),
                'destination_revision':{k:_media_text(rev[k],k,512) for k in ('kind','value')}}
        elif isinstance(binding, dict) and binding.get('interface') == 'sprite-sheet-crops-v1':
            from src.openclank.sprite_sheet import annotations,sheet_rectangles
            if (set(binding)!={'interface','ownerAccountID','inputs','annotations','cellSize','sourceWidth','sourceHeight','rectangles','destination_ref','destination_revision','destination_name'}
                or facts.binding_interface!=binding['interface'] or clean['adapter_version']!='1' or clean['route_id']!=clean['adapter_id']
                or clean['operation']!=facts.operation or clean['model_revision']!='fixed-v1' or clean['route_kind']!=facts.route_kind
                or binding['ownerAccountID']!=clean['account_id'] or not isinstance(binding['inputs'],list) or len(binding['inputs'])!=1):raise ValueError('invalid selected sheet binding')
            item=binding['inputs'][0];rev=binding['destination_revision']
            if not isinstance(item,dict) or set(item)!={'ref','resourceID','revision','sha256'} or not isinstance(item['revision'],dict) or set(item['revision'])!={'kind','value'} or not isinstance(rev,dict) or set(rev)!={'kind','value'}:raise ValueError('invalid selected sheet source')
            source={'ref':_media_text(item['ref'],'ref',16384),'resourceID':_media_text(item['resourceID'],'resource',256),
                'revision':{k:_media_text(item['revision'][k],k,512) for k in ('kind','value')},'sha256':_media_hash(item['sha256'])}
            rects,side=sheet_rectangles(binding['rectangles'],binding['sourceWidth'],binding['sourceHeight']);params=annotations(binding['annotations'])
            if [source['sha256']]!=clean['input_hashes'] or type(binding['cellSize']) is not int or binding['cellSize']!=side or any(p['x']>side or p['y']>side for p in params['pivots']):raise ValueError('selected sheet source changed')
            clean_binding={'interface':'sprite-sheet-crops-v1','ownerAccountID':clean['account_id'],'inputs':[source],'annotations':params,'cellSize':side,
                'sourceWidth':binding['sourceWidth'],'sourceHeight':binding['sourceHeight'],'rectangles':rects,
                'destination_ref':_media_text(binding['destination_ref'],'destination',16384),'destination_name':_media_text(binding['destination_name'],'name',256),
                'destination_revision':{k:_media_text(rev[k],k,512) for k in ('kind','value')}}
        elif isinstance(binding, dict) and binding.get('interface') == 'trellis-image3d-v1':
            required={'connectionID','connectionRevision','interface','ownerAccountID','manifestSHA256','image_source','seed','destination_ref','destination_revision','destination_name','project_ref','project_revision'}
            source=binding.get('image_source'); rev=binding.get('destination_revision')
            if (set(binding)!=required or facts.binding_interface!=binding['interface'] or clean['adapter_version']!='1' or clean['operation']!=facts.operation or clean['route_kind']!=facts.route_kind or clean['model_revision']!=facts.fixed_model_revision or binding['connectionID']!=clean['route_id'] or binding['ownerAccountID']!=clean['account_id'] or type(binding['connectionRevision']) is not int or binding['connectionRevision']<1 or type(binding['seed']) is not int or not 0<=binding['seed']<=2147483647 or binding['project_ref'] is not None or binding['project_revision'] is not None): raise ValueError('invalid TRELLIS binding')
            if (type(source) is not dict or set(source)!={'ref','resourceID','revision','sha256','mime_type','width','height','name'} or type(source['revision']) is not dict or set(source['revision'])!={'kind','value'} or source['mime_type']!='image/png' or type(source['width']) is not int or type(source['height']) is not int or not 1<=source['width']<=4096 or not 1<=source['height']<=4096 or type(rev) is not dict or set(rev)!={'kind','value'}): raise ValueError('invalid TRELLIS source')
            image={'ref':_media_text(source['ref'],'ref',16384),'resourceID':_media_text(source['resourceID'],'resource',512),'revision':{k:_media_text(source['revision'][k],k,512) for k in ('kind','value')},'sha256':_media_hash(source['sha256']),'mime_type':'image/png','width':source['width'],'height':source['height'],'name':_media_text(source['name'],'name',240)}
            if clean['input_hashes']!=[image['sha256']]: raise ValueError('TRELLIS source changed')
            clean_binding={'connectionID':_media_text(binding['connectionID'],'connection',256),'connectionRevision':binding['connectionRevision'],'interface':'trellis-image3d-v1','ownerAccountID':clean['account_id'],'manifestSHA256':_media_hash(binding['manifestSHA256']),'image_source':image,'seed':binding['seed'],'destination_ref':_media_text(binding['destination_ref'],'destination',16384),'destination_revision':{k:_media_text(rev[k],k,512) for k in ('kind','value')},'destination_name':_media_text(binding['destination_name'],'name',256),'project_ref':None,'project_revision':None}
        elif isinstance(binding, dict) and binding.get('interface') == 'hunyuan3d-v2-shape-v1':
            required={'connectionID','connectionRevision','interface','ownerAccountID','manifestSHA256','image_source','seed','destination_ref','destination_revision','destination_name','project_ref','project_revision','device'}
            source=binding.get('image_source'); rev=binding.get('destination_revision')
            if (binding.get('device') not in ('cpu-fp32','cuda-fp16') or set(binding)!=required or facts.binding_interface!=binding['interface'] or clean['adapter_version']!='1' or clean['operation']!=facts.operation or clean['route_kind']!=facts.route_kind or clean['model_revision']!=facts.fixed_model_revision or binding['connectionID']!=clean['route_id'] or binding['ownerAccountID']!=clean['account_id'] or type(binding['connectionRevision']) is not int or binding['connectionRevision']<1 or type(binding['seed']) is not int or not 0<=binding['seed']<=2147483647 or binding['project_ref'] is not None or binding['project_revision'] is not None): raise ValueError('invalid Hunyuan binding')
            if (type(source) is not dict or set(source)!={'ref','resourceID','revision','sha256','mime_type','width','height','name'} or type(source['revision']) is not dict or set(source['revision'])!={'kind','value'} or source['mime_type']!='image/png' or type(source['width']) is not int or type(source['height']) is not int or not 1<=source['width']<=4096 or not 1<=source['height']<=4096 or type(rev) is not dict or set(rev)!={'kind','value'}): raise ValueError('invalid Hunyuan source')
            image={'ref':_media_text(source['ref'],'ref',16384),'resourceID':_media_text(source['resourceID'],'resource',512),'revision':{k:_media_text(source['revision'][k],k,512) for k in ('kind','value')},'sha256':_media_hash(source['sha256']),'mime_type':'image/png','width':source['width'],'height':source['height'],'name':_media_text(source['name'],'name',240)}
            if clean['input_hashes']!=[image['sha256']]: raise ValueError('Hunyuan source changed')
            clean_binding={'connectionID':_media_text(binding['connectionID'],'connection',256),'connectionRevision':binding['connectionRevision'],'interface':'hunyuan3d-v2-shape-v1','ownerAccountID':clean['account_id'],'manifestSHA256':_media_hash(binding['manifestSHA256']),'image_source':image,'seed':binding['seed'],'destination_ref':_media_text(binding['destination_ref'],'destination',16384),'destination_revision':{k:_media_text(rev[k],k,512) for k in ('kind','value')},'destination_name':_media_text(binding['destination_name'],'name',256),'project_ref':None,'project_revision':None,'device':binding['device']}
        elif isinstance(binding, dict) and binding.get('interface') == 'blender-render-v1':
            required = {'connectionID','connectionRevision','interface','ownerAccountID','source_ref','source_revision','source_sha256',
                        'destination_ref','destination_revision','destination_name','project_ref','project_revision'}
            rigclip='rig' in facts.binding_extra_fields
            bake='resolution' in facts.binding_extra_fields
            if set(binding) != (required | ({'rig'} if rigclip else set()) | ({'resolution'} if bake else set())) or facts.binding_interface != binding['interface'] or clean['route_kind'] != facts.route_kind:
                raise ValueError('invalid Blender resource binding')
            clean_binding = {key:_media_text(binding[key], key, 4096 if key.endswith('_ref') else 256)
                             for key in ('connectionID','interface','ownerAccountID','source_ref','destination_ref','destination_name')}
            clean_binding['source_sha256'] = _media_hash(binding['source_sha256'])
            if (clean_binding['source_sha256'] not in clean['input_hashes'] or clean_binding['connectionID'] != clean['route_id']
                    or clean_binding['ownerAccountID'] != clean['account_id'] or type(binding['connectionRevision']) is not int
                    or binding['connectionRevision'] < 1):
                raise ValueError('invalid Blender profile binding')
            clean_binding['connectionRevision'] = binding['connectionRevision']
            for key in ('source_revision','destination_revision'):
                revision = binding[key]
                if not isinstance(revision, dict) or set(revision) != {'kind','value'}:
                    raise ValueError('invalid Blender resource revision')
                clean_binding[key] = {part:_media_text(revision[part],part,512) for part in ('kind','value')}
            if binding['project_ref'] is not None or binding['project_revision'] is not None:
                raise ValueError('project-aware Blender outputs unavailable')
            clean_binding['project_ref'] = clean_binding['project_revision'] = None
            if bake:
                if (clean['adapter_version'] != '1' or clean['operation'] != facts.operation or clean['model_revision'] != 'fixed-material-bake-v1' or type(binding['resolution']) is not int or binding['resolution'] not in (256,512,1024,2048) or clean['input_hashes'] != [clean_binding['source_sha256']]): raise ValueError('invalid bake identity')
                clean_binding['resolution']=binding['resolution']
            if rigclip:
                from src.openclank.blender_rig_inspect import validate_rig_witness
                clean_binding['rig']=validate_rig_witness(binding['rig'],clean_binding['source_sha256'])
        else:
            if not isinstance(binding, dict): raise ValueError('invalid media resource binding')  # B4-FIX 7: before binding.get
            required = {'connectionID', 'connectionRevision', 'interface', 'ownerAccountID', 'recipe_ref', 'recipe_revision', 'recipe_sha256',
                        'destination_ref', 'destination_revision', 'destination_name', 'project_ref', 'project_revision'}
            generated=clean['adapter_id']=='comfyui-material-v1'
            if generated:
                required |= {'recipe_resource_id','material_resolution','material_outputs'}
                if (clean['adapter_version']!='1' or clean['operation']!=facts.operation or clean['route_kind']!=facts.route_kind
                        or type(binding.get('material_resolution')) is not int or binding['material_resolution'] not in (256,512,1024,2048)):
                    raise ValueError('invalid generated material identity')
            if set(binding) != required:
                raise ValueError('invalid media resource binding')
            clean_binding = {}
            for key in required - {'recipe_sha256', 'recipe_revision', 'destination_revision', 'project_ref', 'project_revision', 'connectionRevision', 'material_resolution', 'material_outputs'}:
                clean_binding[key] = _media_text(binding[key], key, 4096 if key.endswith('_ref') else 256)
            clean_binding['recipe_sha256'] = _media_hash(binding['recipe_sha256'])
            if type(binding['connectionRevision']) is not int or binding['connectionRevision'] < 0:
                raise ValueError('invalid media profile revision')
            clean_binding['connectionRevision'] = binding['connectionRevision']
            if (clean_binding['connectionID'] != clean['route_id'] or clean_binding['ownerAccountID'] != clean['account_id']
                or clean_binding['interface'] != 'comfyui-workflow-v1'
                or (facts is not NO_DECLARATION and clean['adapter_id'] not in ('comfyui','comfyui-flac-v1','comfyui-material-v1'))):  # B4-FIX 3: a declared non-ComfyUI adapter takes no ComfyUI binding; undeclared ids keep the snapshot rule
                raise ValueError('invalid media profile binding')
            for key in ('recipe_revision', 'destination_revision'):
                revision = binding[key]
                if not isinstance(revision, dict) or set(revision) != {'kind', 'value'}:
                    raise ValueError('invalid media resource revision')
                clean_binding[key] = {part: _media_text(revision[part], part, 512) for part in ('kind', 'value')}
            if generated:
                outputs=binding['material_outputs']
                if type(outputs) is not dict or set(outputs)!={'baseColor','roughness','metallic'}: raise ValueError('invalid material outputs')
                selected={}; used=set()
                for role in ('baseColor','roughness','metallic'):
                    value=outputs[role]
                    if type(value) is not dict or set(value)!={'node_id','index'} or type(value['index']) is not int or not 0<=value['index']<=63: raise ValueError('invalid material output')
                    node=_media_text(value['node_id'],'node',128); pair=(node,value['index'])
                    if pair in used: raise ValueError('duplicate material output')
                    used.add(pair); selected[role]={'node_id':node,'index':value['index']}
                clean_binding['material_resolution']=binding['material_resolution']
                clean_binding['material_outputs']=selected
            project_ref, project_revision = binding['project_ref'], binding['project_revision']
            if project_ref is None and project_revision is None:
                clean_binding['project_ref'] = clean_binding['project_revision'] = None
            elif isinstance(project_revision, dict) and set(project_revision) == {'kind', 'value'}:
                clean_binding['project_ref'] = _media_text(project_ref, 'project_ref', 4096)
                clean_binding['project_revision'] = {part: _media_text(project_revision[part], part, 512) for part in ('kind', 'value')}
            else:
                raise ValueError('invalid media project binding')
        name = clean_binding['destination_name']
        suffix = {'video/webm':'.webm','application/pdf':'.pdf','model/gltf-binary':'.glb','audio/flac':'.flac','application/zip':'.zip'}.get(facts.mime_type,'.png')
        if name in {'.', '..'} or '/' in name or '\\' in name or not name.lower().endswith(suffix):
            raise ValueError('invalid media output name')
        clean['resource_binding'] = clean_binding
    identity_digest=hashlib.sha256(json.dumps(clean,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()
    with _store_lock():
        jobs=_load()
        for record in jobs.values():
            if _is_bound_shell(record):
                shell_identity=record.get('identity')
                if (not record.get('account_id') or type(shell_identity) is not dict
                        or shell_identity.get('account_id')!=record['account_id']):
                    raise JobStoreError('bound shell account authority unavailable')
                continue
            if not _is_shell(record) and (record.get('kind') != 'media' or record.get('version') != 1):
                if record.get('account_id') in (None,clean['account_id']):
                    raise JobStoreError('unknown job kind prevents safe media reservation')
            if record.get('kind') == 'media' and record.get('version') == 1 and record.get('account_id') == clean['account_id'] and record.get('root_operation_id') == clean['root_operation_id'] and record.get('attempt_id') == clean['attempt_id']:
                if record.get('identity_digest') != identity_digest or record.get('identity_redacted'):
                    raise ValueError('media operation identity conflict')
                return (dict(record), False) if with_created else dict(record)
        job_id=uuid.uuid4().hex
        record={'id':job_id,'kind':'media','version':1,'account_id':clean['account_id'],'owner':clean['owner'],
                'session_id':clean['session_id'],'workspace_id':clean['workspace_id'],'actor_id':clean['actor_id'],'turn_id':clean['turn_id'],
                'root_operation_id':clean['root_operation_id'],'attempt_id':clean['attempt_id'],'idempotency_key':f'media:{job_id}','identity':clean,'identity_digest':identity_digest,'identity_redacted':False,
                'status':'prepared','revision':1,'claim':None,'unknown_action':None,'submission_token':None,'collection_token':None,'publication_token':None,'cancellation_token':None,'remote_task_id':None,'local_process_job_id':None,
                'publication_withdrawn':False,'chat_detached':False,'created_at':time.time(),'updated_at':time.time()}
        jobs[job_id]=record; _save(jobs); return (dict(record), True) if with_created else dict(record)


def _media_trim_timing(rec: Dict[str, Any]) -> Dict[str, Any] | None:
    if (rec.get('identity') or {}).get('adapter_id') != 'trim-webm-v1': return None
    try:
        from src.openclank.trim_contract import source_evidence
        evidence=source_evidence(rec['identity'],rec)
        return {'requested':evidence['requested'],'observed':evidence['observed']}
    except (KeyError,TypeError,ValueError): return None


def _media_recording_observations(rec: Dict[str, Any]) -> Dict[str, Any] | None:
    if (rec.get('identity') or {}).get('adapter_id') != 'screen-record-v1': return None
    try:
        from src.openclank.recording_contract import validate_receipt
        child = rec['local_child']
        protocol=child['profile'].get('recording_protocol')
        if child.get('state') != 'terminal' or protocol not in ('record-v5','record-v6','record-v7','record-v8'): return None
        receipt = validate_receipt(child['receipt'], protocol=protocol)
        if receipt['metadata'] is None: return None
        return {'measurements':receipt['metadata']['observations'], 'upstream_loss':'unknown'}
    except (KeyError, TypeError, ValueError): return None


def _media_recording_audio_tracks(rec: Dict[str, Any]) -> list | None:
    try:
        from src.openclank.recording_contract import validate_receipt
        child=rec['local_child']; control=rec['audio_selection']
        if child.get('state')!='terminal' or child['profile'].get('recording_protocol') not in ('record-v6','record-v7') or control.get('state')!='accepted': return None
        receipt=validate_receipt(child['receipt'], protocol=child['profile']['recording_protocol']); metadata=receipt['metadata']
        if (metadata is None or metadata.get('selection_id')!=control.get('selection_id') or metadata.get('selection_digest')!=control.get('selection_digest')
            or [v['kind'] for v in metadata['tracks']]!=control.get('selected_kinds')): return None
        return metadata['tracks']
    except (KeyError,TypeError,ValueError): return None


def list_media(*, account_id: str, owner: str, after: str = '', limit: int = 50) -> Dict[str, Any]:
    """Owner-scoped bounded projection; never expose stored job identities."""
    import heapq, re
    if type(limit) is not int or not 1 <= limit <= 100 or (after and not re.fullmatch(r'[a-f0-9]{32}', after)):
        raise ValueError('invalid media page')
    with _store_lock():
        jobs = _load()
        ids = heapq.nsmallest(limit + 1, (key for key, row in jobs.items() if key > after and re.fullmatch(r'[a-f0-9]{32}', key)
                     and row.get('kind') == 'media' and row.get('version') == 1
                     and row.get('account_id') == account_id and row.get('owner') == owner
                     and isinstance(row.get('identity'), dict) and row['identity'].get('account_id') == account_id))
        selected = ids[:limit]
        rows = [{'id':key, 'revision':jobs[key]['revision'], 'status':jobs[key]['status'],
                 '_adapter_id':jobs[key]['identity'].get('adapter_id'), '_adapter_version':jobs[key]['identity'].get('adapter_version'),
                 'chat_detached':bool(jobs[key].get('chat_detached')), 'result':jobs[key].get('result'),
                 'reason':jobs[key].get('reason'),
                 'stop_control':({'state':jobs[key]['stop_control'].get('state')} if isinstance(jobs[key].get('stop_control'),dict) else None),
                 'partial_available':isinstance(jobs[key].get('recording_partial'),dict),
                 'recording_phase':jobs[key].get('recording_phase'),
                 '_recording_protocol':(jobs[key].get('identity',{}).get('resource_binding') or {}).get('recording_protocol'),
                 'audio_selection':({k:jobs[key]['audio_selection'].get(k) for k in ('state','selection_id','selection_digest','generation','selected_kinds','crop')} if isinstance(jobs[key].get('audio_selection'),dict) else None),
                 'trim_timing':_media_trim_timing(jobs[key]),
                 'recording_observations':_media_recording_observations(jobs[key]),
                 'audio_tracks':_media_recording_audio_tracks(jobs[key]),
                 'partial_recovery':({'recoveryID':jobs[key]['partial_recovery']['id'],'state':jobs[key]['partial_recovery']['state']} if isinstance(jobs[key].get('partial_recovery'),dict) else None),
                 'recovery':({ 'recoveryID':jobs[key]['recovery']['id'], 'state':jobs[key]['recovery']['state'] }
                    if isinstance(jobs[key].get('recovery'), dict) and jobs[key]['recovery'].get('state') in {'prepared','dispatched','unknown','complete'} else None),
                 'reconciliation_required':'files_receipt' if jobs[key].get('unknown_action') == 'publish' else None}
                for key in selected]
        return {'items':rows, 'next_cursor':selected[-1] if len(ids) > limit else None, 'omitted':len(ids) > limit}


def get_media(job_id: str, *, account_id: str) -> Optional[Dict[str, Any]]:
    with _store_lock():
        rec=_load().get(job_id)
        if not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id:
            return None
        if not isinstance(rec.get('identity'),dict) or rec.get('status') not in _MEDIA_STATES or type(rec.get('revision')) is not int:
            raise JobStoreError('media job record is malformed')
        return json.loads(json.dumps(rec))


def prepare_media_recovery(job_id: str, *, account_id: str, revision: int, binding: Dict[str, Any]) -> Dict[str, Any]:
    """Freeze one explicit, not-yet-dispatched Files recovery in the job row."""
    if set(binding) != {'destination_ref','destination_revision','destination_name','generation','artifact_id','sha256','size'}:
        raise ValueError('invalid recovery binding')
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id)
        if (not rec or rec.get('account_id') != account_id or rec.get('revision') != revision
            or rec.get('kind') != 'media' or rec.get('version') != 1 or rec.get('claim')
            or not isinstance(rec.get('identity'), dict) or rec['identity'].get('account_id') != account_id
            or rec.get('chat_detached') or rec.get('publication_withdrawn') or rec.get('unknown_action')
            or rec.get('status') != 'collecting' or rec.get('result') or not isinstance(rec.get('artifact'), dict)):
            raise ValueError('media recovery unavailable')
        artifact = rec['artifact']
        if any(binding.get(k) != artifact.get(k) for k in ('artifact_id','sha256','size')):
            raise ValueError('media artifact changed')
        previous = rec.get('recovery')
        if previous and previous.get('state') != 'prepared': raise ValueError('media recovery requires reconciliation')
        recovery = {'id':uuid.uuid4().hex, 'state':'prepared', 'binding':json.loads(json.dumps(binding))}
        rec['recovery'] = recovery; rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return json.loads(json.dumps(rec))


def claim_media_recovery(job_id: str, *, account_id: str, revision: int, recovery_id: str) -> Dict[str, Any]:
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); recovery = rec.get('recovery') if isinstance(rec, dict) else None
        if (not rec or rec.get('kind') != 'media' or rec.get('version') != 1 or rec.get('account_id') != account_id
            or not isinstance(rec.get('identity'), dict) or rec['identity'].get('account_id') != account_id
            or rec.get('revision') != revision or rec.get('claim')
            or rec.get('chat_detached') or rec.get('publication_withdrawn') or rec.get('unknown_action')
            or rec.get('status') != 'collecting' or not isinstance(recovery, dict)
            or recovery.get('id') != recovery_id or recovery.get('state') != 'prepared'):
            raise ValueError('media recovery changed')
        token = uuid.uuid4().hex
        recovery.update(state='dispatched', token=token)
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return {'token':token, 'record':json.loads(json.dumps(rec))}


def finish_media_recovery(job_id: str, *, account_id: str, recovery_id: str, token: str, result: Dict[str, Any] | None) -> bool:
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); recovery = rec.get('recovery') if isinstance(rec, dict) else None
        if (not rec or rec.get('kind') != 'media' or rec.get('version') != 1 or rec.get('account_id') != account_id
            or not isinstance(rec.get('identity'), dict) or rec['identity'].get('account_id') != account_id or not isinstance(recovery, dict)
            or recovery.get('id') != recovery_id or recovery.get('token') != token
            or recovery.get('state') not in {'dispatched','unknown'}): return False
        if result is None:
            recovery['state'] = 'unknown'
        else:
            try: clean=_media_result_for(rec,result,recovery_id)
            except (ValueError,TypeError,KeyError): return False
            recovery['state'] = 'complete'; rec['result'] = clean; rec['status'] = 'complete'
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs); return True


def claim_media(job_id: str, *, account_id: str, revision: int, action: str) -> Dict[str, Any]:
    if action not in _MEDIA_ACTION_STATES: raise ValueError('unsupported media action')
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('revision')!=revision or rec.get('claim') is not None or rec.get('status') not in _MEDIA_ACTION_STATES[action]):
            raise ValueError('media claim is stale or unavailable')
        token=uuid.uuid4().hex
        rec['claim']={'action':action,'token':token}
        if action=='collect' and _media_owned_fetch(rec.get('identity'), ('meshy-preview-v1','meshy-refine-v1','tripo-text3d-v1','fal-flux-schnell-v1','fal-hunyuan-image3d-v1','comfyui-flac-v1')) and not rec.get('artifact'):
            # Durable before the owned fetch can start. Absence of its task in
            # another process is not evidence that the fetch has settled.
            rec['claim']['owned_lifetime']=OWNED_LIFETIME
        if action=='submit': rec['submission_token']=token; rec['status']='submitting'
        elif action=='collect': rec['collection_token']=token
        elif action=='publish':
            if rec.get('publication_withdrawn') or not rec.get('artifact'): raise ValueError('media publication is unavailable')
            rec['publication_token']=token
        elif action=='cancel': rec['cancellation_token']=token; rec['status']='cancel_requested'
        elif action=='status' and rec['status']=='unknown_outcome': rec['status']='reconciling'
        rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs)
        return {'token':token,'revision':rec['revision'],'record':json.loads(json.dumps(rec))}


def finish_media(job_id: str, *, account_id: str, token: str, status: str, remote_task_id: Optional[str] = None, local_process_job_id: Optional[str] = None, artifact: Optional[Dict[str, Any]] = None, result: Optional[Dict[str, Any]] = None, reason: Optional[str] = None) -> bool:
    """Complete only the exact currently claimed action; never create a row."""
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id:
            return False
        claim=rec.get('claim')
        if not isinstance(claim,dict) or claim.get('token')!=token or status not in _MEDIA_OUTCOMES.get(claim.get('action'),set()):
            return False
        if reason is not None and not (reason == 'not_dispatched' and status == 'failed' and claim['action'] == 'submit'):
            return False
        if remote_task_id is not None:
            if rec['identity'].get('route_kind') != 'remote': return False
            remote_task_id=_media_text(remote_task_id,'remote task id',512)
            import re
            if not re.fullmatch(r'[A-Za-z0-9._:@-]+',remote_task_id): return False
            if not _media_task_id_ok(rec['identity'],remote_task_id): return False
            if rec.get('remote_task_id') not in (None,remote_task_id): return False
            rec['remote_task_id']=remote_task_id
        if local_process_job_id is not None:
            if rec['identity'].get('route_kind') != 'local': return False
            local_process_job_id=_media_text(local_process_job_id,'local process job id',128)
            import re
            if not re.fullmatch(r'[A-Za-z0-9._:@-]+',local_process_job_id): return False
            if rec.get('local_process_job_id') not in (None,local_process_job_id): return False
            rec['local_process_job_id']=local_process_job_id
        if status in {'queued','running','collecting'} and not (rec.get('remote_task_id') or rec.get('local_process_job_id')):
            return False
        if artifact is not None:
            try: artifact=_media_artifact_for(rec,artifact)
            except (ValueError,TypeError,KeyError): return False
            if rec.get('artifact') not in (None,artifact): return False
            rec['artifact']=artifact
        if result is not None:
            if claim.get('action')!='publish': return False
            try: result=_media_result_for(rec,result,job_id)
            except (ValueError,TypeError,KeyError): return False
        if status=='complete' and result is None and not rec.get('result'):
            return False
        if result is not None: rec['result']=result
        rec['unknown_action']=claim['action'] if status=='unknown_outcome' else None
        if (claim.get('action') == 'status' and status == 'running'
            and (rec.get('identity') or {}).get('adapter_id') == 'screen-record-v1' and rec.get('stop_control')):
            status = 'stopping'
        rec['status']=status; rec['claim']=None; rec['revision']+=1; rec['updated_at']=time.time()
        if reason is not None: rec['reason']=reason
        _save(jobs); return True


def record_media_submission(job_id: str, *, account_id: str, token: str, remote_task_id: str) -> bool:
    """Record a late known task for the original submit; never recreate a row."""
    remote_task_id=_media_text(remote_task_id,'remote task id',512)
    import re
    if not re.fullmatch(r'[A-Za-z0-9._:@-]+',remote_task_id): return False
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('submission_token')!=token or rec.get('status') not in {'submitting','unknown_outcome','reconciling'}
            or not isinstance(rec.get('identity'),dict) or rec['identity'].get('route_kind') != 'remote'
            or rec.get('unknown_action') not in (None,'submit','status') or rec.get('publication_token') is not None or rec.get('artifact') is not None
            or rec.get('remote_task_id') not in (None,remote_task_id)):
            return False
        if not _media_task_id_ok(rec.get('identity') or {},remote_task_id): return False
        rec['remote_task_id']=remote_task_id; rec['status']='queued'; rec['unknown_action']=None
        rec['claim']=None  # A late receipt supersedes any in-flight reconciliation.
        rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs); return True


def record_media_local_start(job_id: str, *, account_id: str, token: str, local_process_job_id: str) -> bool:
    local_process_job_id=_media_text(local_process_job_id,'local process job id',128)
    import re
    if not re.fullmatch(r'[A-Za-z0-9._:@-]+',local_process_job_id): return False
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('submission_token')!=token or rec.get('status') not in {'submitting','unknown_outcome','reconciling'}
            or not isinstance(rec.get('identity'),dict) or rec['identity'].get('route_kind')!='local' or rec.get('local_process_job_id') not in (None,local_process_job_id)
            or rec.get('unknown_action') not in (None,'submit','status') or rec.get('publication_token') is not None or rec.get('artifact') is not None): return False
        rec['local_process_job_id']=local_process_job_id; rec['status']='running'; rec['unknown_action']=None; rec['claim']=None
        rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs); return True


def record_media_artifact(job_id: str, *, account_id: str, token: str, artifact: Dict[str, Any]) -> bool:
    """Retain a late artifact receipt, with no automatic publication."""
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('collection_token')!=token or rec.get('status') not in {'collecting','unknown_outcome','reconciling'}
            or rec.get('unknown_action') not in (None,'collect') or rec.get('publication_token') is not None):
            return False
        try: clean=_media_artifact_for(rec,artifact)
        except (ValueError,TypeError,KeyError): return False
        if rec.get('artifact') not in (None,clean): return False
        rec['artifact']=clean; rec['status']='collecting'; rec['unknown_action']=None; rec['claim']=None
        rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs); return True


def record_media_cancel(job_id: str, *, account_id: str, token: str) -> bool:
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('cancellation_token')!=token or rec.get('status') not in {'cancel_requested','unknown_outcome','reconciling'}
            or rec.get('unknown_action') not in (None,'cancel') or rec.get('publication_token') is not None): return False
        rec['status']='cancelled'; rec['unknown_action']=None; rec['claim']=None; rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs); return True


def record_media_publication(job_id: str, *, account_id: str, token: str, result: Dict[str, Any]) -> bool:
    """Retain an exact known Files receipt even if its chat was detached."""
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id); artifact=rec.get('artifact') if isinstance(rec,dict) else None
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('publication_token')!=token or rec.get('status') not in {'collecting','unknown_outcome','reconciling'}
            or not isinstance(artifact,dict)): return False
        try: clean=_media_result_for(rec,result,job_id)
        except (ValueError,TypeError,KeyError): return False
        rec['result']=clean; rec['status']='complete'; rec['unknown_action']=None; rec['claim']=None; rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs); return True


def mark_media_uncertain(job_id: str, *, account_id: str, revision: int) -> bool:
    """Explicitly retire a claim as unknown; it never dispatches or retries."""
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id)
        if (not rec or rec.get('kind')!='media' or rec.get('version')!=1 or rec.get('account_id')!=account_id
            or rec.get('revision')!=revision or not isinstance(rec.get('claim'),dict)): return False
        if (rec['claim'].get('owned_lifetime') in OWNED_LIFETIMES
                or (_media_owned_fetch(rec.get('identity'), ('meshy-preview-v1','meshy-refine-v1','tripo-text3d-v1','fal-flux-schnell-v1','fal-hunyuan-image3d-v1'))
                    and rec['claim'].get('action')=='collect')): return False
        rec['unknown_action']=rec['claim'].get('action'); rec['status']='unknown_outcome'; rec['claim']=None
        rec['revision']+=1; rec['updated_at']=time.time(); _save(jobs); return True


def result_text(rec: Dict[str, Any]) -> str:
    """Human/agent-readable summary of a finished job, for the follow-up."""
    if not _is_shell(rec):
        raise ValueError('not a shell job')
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


def reserve_media_local_child(job_id: str, *, account_id: str, token: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    """Reserve one fixed local adapter child on the existing submit claim."""
    fields = {'adapter','executable_sha256','executable_version','runner_sha256','input_sha256','artifact_authority'}
    sprite=isinstance(profile,dict) and profile.get('adapter') in ('sprite-sheet-v1','sprite-sheet-crops-v1')
    material=isinstance(profile,dict) and profile.get('adapter')=='material-pack-v1'
    rigclip=isinstance(profile,dict) and profile.get('adapter')=='blender-glb-rig-clip-v1'
    bake=isinstance(profile,dict) and profile.get('adapter')=='blender-material-bake-v1'
    latex=isinstance(profile,dict) and profile.get('adapter')=='latex-pdflatex-v1'
    recording=isinstance(profile,dict) and profile.get('adapter')=='screen-record-v1'
    trim=isinstance(profile,dict) and profile.get('adapter')=='trim-webm-v1'
    if trim:
        fields={'adapter','helper_sha256','runner_sha256','artifact_authority','input_sha256','start_ms','end_ms'}
        if (set(profile)!=fields or type(profile['start_ms']) is not int or type(profile['end_ms']) is not int
            or not 0<=profile['start_ms']<profile['end_ms']<=86400000 or profile['end_ms']-profile['start_ms']>60000):
            raise ValueError('invalid trim child profile')
        clean={'adapter':profile['adapter'],'helper_sha256':_media_hash(profile['helper_sha256']),
            'runner_sha256':_media_hash(profile['runner_sha256']),'artifact_authority':_media_hash(profile['artifact_authority']),
            'input_sha256':_media_hash(profile['input_sha256']),'start_ms':profile['start_ms'],'end_ms':profile['end_ms']}
    elif recording:
        from src.openclank.recording_contract import LIMITS
        if (set(profile) not in ({'adapter','helper_sha256','runner_sha256','artifact_authority','limits'}, {'adapter','helper_sha256','runner_sha256','artifact_authority','limits','recording_protocol'}, {'adapter','helper_sha256','runner_sha256','artifact_authority','limits','recording_protocol','audio_intent'}) or profile.get('recording_protocol','record-v3') not in ('record-v3','record-v5','record-v6','record-v7','record-v8') or (profile.get('recording_protocol') in ('record-v6','record-v7')) != ('audio_intent' in profile) or profile['limits']!=LIMITS):
            raise ValueError('invalid recording child profile')
        clean={'adapter':profile['adapter'],'helper_sha256':_media_hash(profile['helper_sha256']),
               'runner_sha256':_media_hash(profile['runner_sha256']),'artifact_authority':_media_hash(profile['artifact_authority']),
               'limits':dict(LIMITS), **({'recording_protocol':profile['recording_protocol']} if 'recording_protocol' in profile else {}), **({'audio_intent':profile['audio_intent']} if 'audio_intent' in profile else {})}
    elif latex:
        if set(profile)!={'adapter','executable_sha256','executable_version','runner_sha256','input_hashes','artifact_authority'} or not isinstance(profile['input_hashes'],list) or not 1<=len(profile['input_hashes'])<=32: raise ValueError('invalid LaTeX child profile')
        clean={'adapter':profile['adapter'],'executable_sha256':_media_hash(profile['executable_sha256']),
               'executable_version':_media_text(profile['executable_version'],'executable version',128),
               'runner_sha256':_media_hash(profile['runner_sha256']),'input_hashes':[_media_hash(v) for v in profile['input_hashes']],
               'artifact_authority':_media_hash(profile['artifact_authority'])}
    elif isinstance(profile,dict) and profile.get('adapter')=='trellis-image3d-v1':
        fields={'adapter','recipe','connectionID','connectionRevision','ownerAccountID','manifestSHA256','interpreterSHA256','interpreterVersion','runnerSHA256','recipeSHA256','artifact_authority','input_sha256','seed'}
        if set(profile)!=fields or profile['recipe']!='trellis-image-glb-v1' or type(profile['connectionRevision']) is not int or profile['connectionRevision']<1 or type(profile['seed']) is not int or not 0<=profile['seed']<=2147483647: raise ValueError('invalid TRELLIS child')
        clean={k:_media_hash(profile[k]) for k in ('manifestSHA256','interpreterSHA256','runnerSHA256','recipeSHA256','artifact_authority','input_sha256')}
        clean.update({k:_media_text(profile[k],k,256) for k in ('adapter','recipe','connectionID','ownerAccountID','interpreterVersion')})
        clean.update(connectionRevision=profile['connectionRevision'],seed=profile['seed'])
    elif isinstance(profile,dict) and profile.get('adapter')=='hunyuan3d-v2-shape-v1':
        fields={'adapter','recipe','connectionID','connectionRevision','ownerAccountID','manifestSHA256','interpreterSHA256','interpreterVersion','runnerSHA256','recipeSHA256','helperSHA256','artifact_authority','input_sha256','seed','device'}
        if profile.get('device') not in ('cpu-fp32','cuda-fp16') or set(profile)!=fields or profile['recipe']!='hunyuan3d-v2-image-glb-v1' or type(profile['connectionRevision']) is not int or profile['connectionRevision']<1 or type(profile['seed']) is not int or not 0<=profile['seed']<=2147483647: raise ValueError('invalid Hunyuan child')
        clean={k:_media_hash(profile[k]) for k in ('manifestSHA256','interpreterSHA256','runnerSHA256','recipeSHA256','helperSHA256','artifact_authority','input_sha256')}
        clean.update({k:_media_text(profile[k],k,256) for k in ('adapter','recipe','connectionID','ownerAccountID','interpreterVersion')})
        clean.update(connectionRevision=profile['connectionRevision'],seed=profile['seed'],device=profile['device'])
    elif material:
        if set(profile)!={'adapter','input_hashes','artifact_authority','manifest_sha256'} or type(profile['input_hashes']) is not list or len(profile['input_hashes']) not in (3,4): raise ValueError('invalid material child profile')
        clean={'adapter':profile['adapter'],'input_hashes':[_media_hash(v) for v in profile['input_hashes']],
            'artifact_authority':_media_hash(profile['artifact_authority']),'manifest_sha256':_media_hash(profile['manifest_sha256'])}
    elif sprite:
        if set(profile)!={'adapter','input_hashes','artifact_authority'} or not isinstance(profile['input_hashes'],list) or len(profile['input_hashes'])!=(1 if profile['adapter']=='sprite-sheet-crops-v1' else 4): raise ValueError('invalid local child profile')
        clean={'adapter':profile['adapter'],'input_hashes':[_media_hash(v) for v in profile['input_hashes']],'artifact_authority':_media_hash(profile['artifact_authority'])}
    elif bake:
        if set(profile)!=fields | {'resolution','recipe'} or type(profile['resolution']) is not int or profile['resolution'] not in (256,512,1024,2048) or profile['recipe']!='blender-material-channels-v1' or not __import__('re').fullmatch(r'4\.5\.[0-9]+',profile['executable_version']): raise ValueError('invalid bake child profile')
        clean={'adapter':profile['adapter'],'executable_sha256':_media_hash(profile['executable_sha256']),'executable_version':_media_text(profile['executable_version'],'executable version',128),'runner_sha256':_media_hash(profile['runner_sha256']),'input_sha256':_media_hash(profile['input_sha256']),'artifact_authority':_media_hash(profile['artifact_authority']),'resolution':profile['resolution'],'recipe':profile['recipe']}
    elif not isinstance(profile, dict) or set(profile) != (fields | ({'rig'} if rigclip else set())) or profile['adapter'] not in ('blender-glb-png-v1','blender-glb-four-heading-v1','blender-glb-rig-clip-v1'):
        raise ValueError('invalid local child profile')
    else:
        clean = {'adapter':profile['adapter'], 'executable_sha256':_media_hash(profile['executable_sha256']),
                 'executable_version':_media_text(profile['executable_version'],'executable version',128),
                 'runner_sha256':_media_hash(profile['runner_sha256']), 'input_sha256':_media_hash(profile['input_sha256']),
                 'artifact_authority':_media_hash(profile['artifact_authority'])}
    if rigclip:
        from src.openclank.blender_rig_inspect import validate_rig_witness
        clean['rig']=validate_rig_witness(profile['rig'],clean['input_sha256'])
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id)
        if (not rec or rec.get('kind') != 'media' or rec.get('version') != 1 or rec.get('account_id') != account_id
                or rec.get('submission_token') != token or rec.get('status') != 'submitting'
                or (rec.get('claim') or {}).get('token') != token or rec.get('identity',{}).get('route_kind') != 'local'
                or rec.get('identity',{}).get('adapter_id') != clean['adapter']
                or (rec.get('identity',{}).get('input_hashes') != [] if recording else clean['input_hashes'] != rec.get('identity',{}).get('input_hashes', []) if sprite or latex or material else clean['input_sha256'] not in rec.get('identity',{}).get('input_hashes', []))):
            raise ValueError('local child admission unavailable')
        if clean['adapter']=='trellis-image3d-v1':
            bound=rec['identity']['resource_binding']
            if any(clean[k]!=bound[k] for k in ('connectionID','connectionRevision','ownerAccountID','manifestSHA256','seed')) or clean['input_sha256']!=bound['image_source']['sha256']: raise ValueError('TRELLIS child identity changed')
        if clean['adapter']=='hunyuan3d-v2-shape-v1':
            bound=rec['identity']['resource_binding']
            if any(clean[k]!=bound[k] for k in ('connectionID','connectionRevision','ownerAccountID','manifestSHA256','seed','device')) or clean['input_sha256']!=bound['image_source']['sha256']: raise ValueError('Hunyuan child identity changed')
        if bake and (clean['resolution'] != rec['identity']['resource_binding']['resolution'] or clean['input_sha256'] != rec['identity']['resource_binding']['source_sha256']): raise ValueError('bake identity changed')
        if material and clean['manifest_sha256'] != rec['identity']['resource_binding']['manifest_sha256']: raise ValueError('material manifest changed')
        if trim:
            bound=rec['identity']['resource_binding']
            if (clean['helper_sha256']!=bound['helper_sha256'] or clean['input_sha256']!=bound['source_sha256']
                or clean['start_ms']!=bound['start_ms'] or clean['end_ms']!=bound['end_ms']): raise ValueError('trim child identity changed')
        if recording and (clean['helper_sha256'] != rec['identity']['resource_binding']['helper_sha256'] or clean.get('recording_protocol','record-v3') != rec['identity']['resource_binding'].get('recording_protocol','record-v3') or clean.get('audio_intent') != rec['identity']['resource_binding'].get('audio_intent')): raise ValueError('recording helper identity changed')
        if latex and clean['executable_sha256'] != rec['identity']['resource_binding']['compiler_sha256']: raise ValueError('LaTeX compiler identity changed')
        if rigclip and clean['rig']!=rec['identity']['resource_binding']['rig']: raise ValueError('rig identity changed')
        current = rec.get('local_child')
        if current is not None:
            if current.get('profile') != clean or current.get('claim_token') != token:
                raise ValueError('local child identity conflict')
            return json.loads(json.dumps(current))
        child = {'version':1, 'child_id':uuid.uuid4().hex, 'nonce':uuid.uuid4().hex,
                 'account_id':account_id, 'job_id':job_id, 'claim_token':token,
                 'identity_digest':rec['identity_digest'], 'profile':clean, 'state':'launching'}
        rec['local_child'] = child; rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return json.loads(json.dumps(child))


def prepare_recording_partial_recovery(job_id: str, *, account_id: str, revision: int, binding: Dict[str, Any]) -> Dict[str, Any]:
    fields = {'destination_ref','destination_revision','destination_name','generation','artifact_id','sha256','size'}
    if not isinstance(binding,dict) or set(binding) != fields: raise ValueError('partial recovery unavailable')
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); partial = rec.get('recording_partial') if rec else None
        if (not rec or rec.get('account_id') != account_id or rec.get('revision') != revision
            or (rec.get('identity') or {}).get('adapter_id') not in ('screen-record-v1','trim-webm-v1')
            or rec.get('chat_detached') or rec.get('publication_withdrawn') or rec.get('claim')
            or not isinstance(partial,dict) or partial.get('outcome') != 'partial' or partial.get('finalized') is not False
            or any(binding.get(k) != partial.get(k) for k in ('artifact_id','sha256','size'))):
            raise ValueError('partial recovery unavailable')
        prior = rec.get('partial_recovery')
        if prior and prior.get('state') != 'prepared': raise ValueError('partial recovery requires status')
        rec['partial_recovery'] = {'id':uuid.uuid4().hex,'state':'prepared','binding':json.loads(json.dumps(binding))}
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return json.loads(json.dumps(rec))


def claim_recording_partial_recovery(job_id: str, *, account_id: str, revision: int, recovery_id: str) -> Dict[str, Any]:
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); recovery = rec.get('partial_recovery') if rec else None
        if (not rec or rec.get('account_id') != account_id or rec.get('revision') != revision or rec.get('claim')
            or rec.get('chat_detached') or rec.get('publication_withdrawn') or not isinstance(recovery,dict)
            or recovery.get('id') != recovery_id or recovery.get('state') != 'prepared'):
            raise ValueError('partial recovery unavailable')
        token = uuid.uuid4().hex; recovery.update(state='dispatched',token=token)
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return {'token':token,'record':json.loads(json.dumps(rec))}


def finish_recording_partial_recovery(job_id: str, *, account_id: str, recovery_id: str, token: str, resource_ref: str | None) -> bool:
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); recovery = rec.get('partial_recovery') if rec else None
        if (not rec or rec.get('account_id') != account_id or not isinstance(recovery,dict)
            or recovery.get('id') != recovery_id or recovery.get('token') != token
            or recovery.get('state') not in {'dispatched','unknown'}): return False
        if resource_ref is None: recovery['state'] = 'unknown'
        else:
            recovery['result'] = {'resource_ref':_media_text(resource_ref,'partial ref',16384),
                'sha256':recovery['binding']['sha256'],'mime_type':'application/octet-stream','finalized':False}
            recovery['state'] = 'complete'
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs); return True


def issue_recording_audio_selection(job_id: str, *, account_id: str, revision: int, child_id: str, nonce: str,
                                    generation: str, selection_id: str, digest: str, selected_kinds: list[str], crop: dict | None = None, source_basis: dict | None = None, choice_handles: dict | None = None) -> Dict[str, Any]:
    import re
    if (not re.fullmatch(r'[a-f0-9]{32}', generation) or not re.fullmatch(r'[a-f0-9]{32}', selection_id)
        or not re.fullmatch(r'[a-f0-9]{64}', digest) or selected_kinds not in ([],['microphone'],['system_output'],['microphone','system_output'])):
        raise ValueError('audio selection unavailable')
    with _store_lock():
        jobs=_load(); rec=jobs.get(job_id); child=rec.get('local_child') if rec else None
        if (not rec or rec.get('account_id')!=account_id or not isinstance(child,dict) or child.get('child_id')!=child_id
            or child.get('nonce')!=nonce or child.get('state')!='running' or child.get('profile',{}).get('recording_protocol') not in ('record-v6','record-v7')):
            raise ValueError('audio selection unavailable')
        protocol=child['profile'].get('recording_protocol')
        if (protocol == 'record-v7') != (crop is not None and source_basis is not None and choice_handles is not None) or (protocol != 'record-v7' and (source_basis is not None or choice_handles is not None)): raise ValueError('recording crop unavailable')
        if choice_handles is not None:
            if (type(choice_handles) is not dict or set(choice_handles) != {'microphone','system_output'} or any(v is not None and (type(v) is not str or not re.fullmatch(r'[a-f0-9]{32}',v)) for v in choice_handles.values()) or [k for k in ('microphone','system_output') if choice_handles[k] is not None] != selected_kinds): raise ValueError('recording selection unavailable')
            choice_handles=dict(choice_handles)
        if crop is not None:
            if type(crop) is not dict or set(crop) != {'mode','basis_id','rect'}: raise ValueError('recording crop unavailable')
            crop=json.loads(json.dumps(crop))
            if (type(source_basis) is not dict or set(source_basis) != {'source_width','source_height'} or any(type(v) is not int or not 1<=v<=16384 for v in source_basis.values()) or source_basis['source_width']*source_basis['source_height']>16000000): raise ValueError('recording basis unavailable')
            source_basis=dict(source_basis)
        previous=rec.get('audio_selection')
        expected={'selection_id':selection_id,'selection_digest':digest,'generation':generation,'selected_kinds':selected_kinds, **({'crop':crop,'source_basis':source_basis,'choice_handles':choice_handles} if crop is not None else {})}
        if previous is not None:
            if any(previous.get(k)!=v for k,v in expected.items()): raise ValueError('audio selection already issued')
            return {'issue':False,'record':json.loads(json.dumps(rec))}
        if rec.get('revision')!=revision or rec.get('status')!='running' or rec.get('recording_phase')!='audio_choices' or rec.get('stop_control'):
            raise ValueError('audio selection unavailable')
        token=uuid.uuid4().hex;rec['audio_selection']={**expected,'token':token,'state':'issued'}
        rec['revision']+=1;rec['updated_at']=time.time();_save(jobs)
        return {'issue':True,'token':token,'record':json.loads(json.dumps(rec))}


def finish_recording_audio_selection(job_id: str, *, account_id: str, child_id: str, nonce: str,
                                     selection_id: str, digest: str, generation: str, selected_kinds: list[str], accepted: bool, fact_current=None, crop: dict | None = None, source_basis: dict | None = None, choice_handles: dict | None = None) -> bool:
    with _store_lock():
        jobs=_load();rec=jobs.get(job_id);child=rec.get('local_child') if rec else None; control=rec.get('audio_selection') if rec else None
        if (not rec or rec.get('account_id')!=account_id or not isinstance(child,dict) or child.get('child_id')!=child_id
            or child.get('nonce')!=nonce or child.get('state')!='running' or child.get('identity_digest')!=rec.get('identity_digest')
            or child.get('claim_token')!=rec.get('submission_token') or child.get('profile',{}).get('recording_protocol') not in ('record-v6','record-v7')
            or not callable(fact_current) or fact_current() is not True or not isinstance(control,dict) or control.get('selection_id')!=selection_id
            or control.get('selection_digest')!=digest or control.get('generation')!=generation or control.get('selected_kinds')!=selected_kinds
            or control.get('crop')!=crop or control.get('source_basis')!=source_basis or control.get('choice_handles')!=choice_handles or control.get('state') not in ('issued','unknown')): return False
        control['state']='accepted' if accepted else 'unknown';rec['revision']+=1;rec['updated_at']=time.time();_save(jobs);return True


def issue_recording_stop(job_id: str, *, account_id: str, revision: int, stop_id: str) -> Dict[str, Any]:
    """Reserve one original Stop; any repeat observes it without resending EOS."""
    stop_id = _media_text(stop_id, 'stop id', 128)
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); child = rec.get('local_child') if rec else None
        if (not rec or rec.get('account_id') != account_id or (rec.get('identity') or {}).get('adapter_id') != 'screen-record-v1'):
            raise ValueError('recording stop unavailable')
        previous = rec.get('stop_control')
        if previous is not None:
            if previous.get('id') != stop_id: raise ValueError('recording stop already requested')
            return {'issue':False, 'record':json.loads(json.dumps(rec))}
        if (rec.get('revision') != revision or rec.get('status') != 'running'
            or not isinstance(child, dict) or child.get('state') != 'running'):
            raise ValueError('recording stop unavailable')
        token = uuid.uuid4().hex
        rec['stop_control'] = {'id':stop_id, 'token':token, 'state':'issued'}
        rec['status'] = 'stopping'; rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return {'issue':True, 'token':token, 'record':json.loads(json.dumps(rec))}


def finish_recording_stop(job_id: str, *, account_id: str, stop_id: str, token: str, accepted: bool) -> bool:
    if type(accepted) is not bool: raise ValueError('invalid recording stop state')
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); control = rec.get('stop_control') if rec else None
        if (not rec or rec.get('account_id') != account_id or not isinstance(control, dict)
            or control != {'id':stop_id, 'token':token, 'state':'issued'}): return False
        control['state'] = 'accepted' if accepted else 'unknown'
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs); return True


def claim_media_local_child(job_id: str, *, account_id: str, child_id: str, nonce: str) -> Dict[str, Any]:
    """The fixed wrapper claims precisely the original attachment once."""
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); child = rec.get('local_child') if rec else None
        if (not isinstance(child,dict) or rec.get('account_id') != account_id or child.get('account_id') != account_id
                or child.get('job_id') != job_id or child.get('child_id') != child_id or child.get('nonce') != nonce
                or child.get('identity_digest') != rec.get('identity_digest') or child.get('claim_token') != rec.get('submission_token')
                or child.get('state') != 'launching'):
            raise ValueError('local child claim unavailable')
        child['state'] = 'running'
        if child.get('profile',{}).get('adapter') == 'screen-record-v1': rec['recording_phase'] = 'consent_pending'
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return json.loads(json.dumps(child))


def observe_recording_phase(job_id: str, *, account_id: str, child_id: str, nonce: str, phase: str) -> bool:
    if phase not in ('audio_choices','starting','recording'): raise ValueError('invalid recording phase')
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); child = rec.get('local_child') if rec else None
        if (not isinstance(child,dict) or rec.get('account_id') != account_id or child.get('child_id') != child_id
            or child.get('nonce') != nonce or child.get('state') != 'running'
            or child.get('identity_digest') != rec.get('identity_digest')
            or (rec.get('identity') or {}).get('adapter_id') != 'screen-record-v1'): return False
        previous = rec.get('recording_phase')
        if (previous,phase) not in (('consent_pending','audio_choices'),('audio_choices','starting'),('consent_pending','starting'),('starting','recording'),('starting','starting'),('recording','recording')): return False
        if previous != phase:
            rec['recording_phase'] = phase; rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return True


def finish_media_local_child(job_id: str, *, account_id: str, child_id: str, nonce: str, receipt: Dict[str, Any]) -> bool:
    """Publish a positively waited fixed-child receipt; never recreate a row."""
    if isinstance(receipt, dict) and 'outcome' in receipt:
        with _store_lock():
            jobs = _load(); rec = jobs.get(job_id); child = rec.get('local_child') if rec else None
            adapter=(rec.get('identity') or {}).get('adapter_id') if rec else None
            if (not isinstance(child, dict) or rec.get('account_id') != account_id
                or adapter not in ('screen-record-v1','trim-webm-v1')
                or child.get('account_id') != account_id or child.get('job_id') != job_id
                or child.get('child_id') != child_id or child.get('nonce') != nonce
                or child.get('identity_digest') != rec.get('identity_digest')
                or child.get('claim_token') != rec.get('submission_token') or child.get('state') != 'running'):
                return False
            if adapter == 'trim-webm-v1':
                from src.openclank.trim_contract import validate_receipt
                binding=rec['identity']['resource_binding']
                clean=validate_receipt(receipt,start_ms=binding['start_ms'],end_ms=binding['end_ms'])
            else:
                from src.openclank.recording_contract import validate_receipt
                clean=validate_receipt(receipt, protocol=child['profile'].get('recording_protocol','record-v3'))
                if child['profile'].get('recording_protocol') in ('record-v6','record-v7') and clean['metadata'] is not None:
                    choice=rec.get('audio_selection') or {}; metadata=clean['metadata']
                    if (choice.get('state') != 'accepted' or metadata.get('selection_id') != choice.get('selection_id') or metadata.get('selection_digest') != choice.get('selection_digest') or [v['kind'] for v in metadata.get('tracks',[])] != choice.get('selected_kinds')): raise ValueError('audio selection changed')
                    if child['profile'].get('recording_protocol') == 'record-v7':
                        selected=choice.get('crop') or {}; basis=choice.get('source_basis') or {}; actual=metadata.get('crop') or {}
                        rect=selected.get('rect') if selected.get('mode') == 'crop' else {'x':0,'y':0,'width':basis.get('source_width'),'height':basis.get('source_height')}
                        if selected.get('mode') not in ('full','crop') or set(basis) != {'source_width','source_height'} or any(actual.get(k)!=basis[k] for k in basis) or actual.get('basis_id') != selected.get('basis_id') or actual.get('rect') != rect: raise ValueError('recording crop changed')
            child['state'] = 'terminal'; child['receipt'] = clean
            if clean['outcome'] == 'partial':
                rec['recording_partial'] = {**clean, 'child_id':child_id, 'nonce':nonce}
            rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs); return True
    if (not isinstance(receipt,dict) or set(receipt) not in ({'exit_code','output_sha256','output_size'}, {'exit_code','output_sha256','output_size','artifact_id'}, {'exit_code','output_sha256','output_size','artifact_id','layout'})
            or type(receipt['exit_code']) is not int or not -255 <= receipt['exit_code'] <= 255
            or type(receipt['output_size']) is not int or not 0 <= receipt['output_size'] <= 268435456):
        raise ValueError('invalid local child receipt')
    if receipt['exit_code'] == 0:
        _media_hash(receipt['output_sha256'])
        if not receipt['output_size'] or 'artifact_id' not in receipt: raise ValueError('empty render')
        _media_text(receipt['artifact_id'], 'artifact id', 256)
    elif receipt['output_sha256'] is not None or receipt['output_size'] != 0 or 'artifact_id' in receipt:
        raise ValueError('invalid failed render receipt')
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); child = rec.get('local_child') if rec else None
        if (not isinstance(child,dict) or rec.get('account_id') != account_id or child.get('account_id') != account_id
                or child.get('job_id') != job_id or child.get('child_id') != child_id or child.get('nonce') != nonce
                or child.get('identity_digest') != rec.get('identity_digest') or child.get('claim_token') != rec.get('submission_token')
                or child.get('state') != 'running'):
            return False
        sheet=rec.get('identity',{}).get('adapter_id') in ('blender-glb-four-heading-v1','blender-glb-rig-clip-v1','sprite-sheet-v1','sprite-sheet-crops-v1')
        if receipt['exit_code']==0:
            try:
                artifact={'artifact_id':receipt['artifact_id'],'sha256':receipt['output_sha256'],'mime_type':'application/pdf' if rec.get('identity',{}).get('adapter_id')=='latex-pdflatex-v1' else 'application/zip' if rec.get('identity',{}).get('adapter_id') in ('material-pack-v1','blender-material-bake-v1') else 'model/gltf-binary' if rec.get('identity',{}).get('adapter_id') in ('trellis-image3d-v1','hunyuan3d-v2-shape-v1') else 'image/png','size':receipt['output_size']}
                if sheet: artifact['layout']=receipt.get('layout')
                if set(receipt) != ({'exit_code','output_sha256','output_size','artifact_id','layout'} if sheet else {'exit_code','output_sha256','output_size','artifact_id'}):
                    raise ValueError('invalid local child receipt')
                _media_artifact_for(rec,artifact)
            except (KeyError, ValueError):
                raise ValueError('invalid local child receipt')
        child['state'] = 'terminal'; child['receipt'] = dict(receipt)
        rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs); return True


def adopt_media_local_artifact(job_id: str, *, account_id: str, child_id: str) -> bool:
    """Project a completed exact child custody receipt into its original row."""
    with _store_lock():
        jobs = _load(); rec = jobs.get(job_id); child = rec.get('local_child') if rec else None
        if (not isinstance(child, dict) or rec.get('kind') != 'media' or rec.get('account_id') != account_id
                or child.get('account_id') != account_id or child.get('job_id') != job_id
                or child.get('child_id') != child_id or child.get('state') != 'terminal'
                or child.get('identity_digest') != rec.get('identity_digest')
                or child.get('claim_token') != rec.get('submission_token')):
            return False
        receipt = child.get('receipt')
        if (rec.get('identity') or {}).get('adapter_id') in ('screen-record-v1','trim-webm-v1'):
            try:
                if rec['identity']['adapter_id'] == 'trim-webm-v1':
                    from src.openclank.trim_contract import validate_receipt
                    binding=rec['identity']['resource_binding']
                    clean=validate_receipt(receipt,start_ms=binding['start_ms'],end_ms=binding['end_ms'])
                else:
                    from src.openclank.recording_contract import validate_receipt
                    clean=validate_receipt(receipt, protocol=child['profile'].get('recording_protocol','record-v3'))
                if clean['outcome'] != 'finalized': return False
                artifact = _media_artifact_for(rec, {'artifact_id':clean['artifact_id'], 'sha256':clean['sha256'],
                    'mime_type':clean['mime_type'], 'size':clean['size']})
            except (ValueError, TypeError, KeyError): return False
            if rec.get('artifact') not in (None, artifact): return False
            if rec.get('artifact') is None:
                rec['artifact'] = artifact; rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
            return True
        sheet=rec.get('identity',{}).get('adapter_id') in ('blender-glb-four-heading-v1','blender-glb-rig-clip-v1','sprite-sheet-v1','sprite-sheet-crops-v1')
        fields={'exit_code','output_sha256','output_size','artifact_id'} | ({'layout'} if sheet else set())
        if not isinstance(receipt, dict) or set(receipt) != fields or receipt['exit_code'] != 0:
            return False
        try:
            artifact = {'artifact_id':_media_text(receipt['artifact_id'], 'artifact id', 256),
                        'sha256':_media_hash(receipt['output_sha256']), 'mime_type':'application/pdf' if rec.get('identity',{}).get('adapter_id')=='latex-pdflatex-v1' else 'application/zip' if rec.get('identity',{}).get('adapter_id') in ('material-pack-v1','blender-material-bake-v1') else 'model/gltf-binary' if rec.get('identity',{}).get('adapter_id') in ('trellis-image3d-v1','hunyuan3d-v2-shape-v1') else 'image/png',
                        'size':receipt['output_size']}
            if sheet: artifact['layout']=receipt['layout']
            artifact=_media_artifact_for(rec,artifact)
        except ValueError:
            return False
        if rec.get('artifact') not in (None, artifact): return False
        if rec.get('artifact') is None:
            rec['artifact'] = artifact; rec['revision'] += 1; rec['updated_at'] = time.time(); _save(jobs)
        return True
