"""Owner-scoped, preview-bound destructive memory reset coordination.

This is deliberately separate from the administrator danger-zone wipe.  A
browser user can only reset their own live memory domains, after previewing
the exact selection and counts and echoing the generated confirmation.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from core.atomic_io import atomic_write_json
from services.memory.skill_lifecycle import locked
from src.constants import DATA_DIR


NUKE_COMPONENTS = frozenset({"memories", "graph", "ingest", "skills"})
_DB_COMPONENTS = frozenset({"memories", "graph", "ingest"})
_JOURNAL_VERSION = 1
_RETAINED_DOMAINS = (
    {
        "id": "rag_documents",
        "description": "RAG documents, chunks, indexes, and source revisions are retained.",
    },
    {
        "id": "exports_backups_sources",
        "description": "Exports, backups, and original source files are retained.",
    },
    {
        "id": "memory_policy",
        "description": "Retention policy and project/policy/hex state are retained.",
    },
)
logger = logging.getLogger(__name__)


class MemoryNukeError(ValueError):
    """The reset request is invalid or no longer matches its preview."""


class MemoryNukeConflict(MemoryNukeError):
    """The reset preview is stale, expired, or belongs to another owner."""


class MemoryNukeUnavailable(RuntimeError):
    """A selected live-memory component cannot currently be reset."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MemoryNukeError("memory reset preview is not canonical JSON") from exc


def _clean_error(exc: BaseException) -> str:
    """Return one bounded, plain-text error (never a provider HTML response)."""
    detail = " ".join(str(exc).split())
    if not detail:
        detail = exc.__class__.__name__
    elif exc.__class__.__name__ not in detail:
        detail = f"{exc.__class__.__name__}: {detail}"
    return detail[:300]


def _expires_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _process_is_running(pid: object) -> bool:
    """Best-effort local-process liveness check for a durable commit claim."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Failing closed is safer than allowing a second destructive commit.
        return True
    return True


_RESET_CLAIM_SEAL = object()


@dataclass(frozen=True)
class MemoryResetClaim:
    """Host-issued proof of one active, durable reset claim."""

    _coordinator: Any
    owner: str
    account_id: str
    operation_id: str
    _seal: object
    _terminal_s08_witness: Any = field(default=None,repr=False,compare=False)

    def __post_init__(self):
        if self._seal is not _RESET_CLAIM_SEAL:
            raise ValueError("reset claim cannot be constructed by callers")

    def assert_current(self, *, owner, account_id, provider, operation_id):
        coordinator = self._coordinator
        if (self._seal is not _RESET_CLAIM_SEAL or owner != self.owner
                or account_id != self.account_id or operation_id != self.operation_id
                or provider is None or (provider is not coordinator.memory_provider and provider is not coordinator.canonical_memory_provider)):
            raise MemoryNukeUnavailable("reset claim identity changed")
        auth = getattr(coordinator, "_reset_auth", None)
        if (auth is None or auth.account_id(owner) != account_id
                or auth.username_for_account_id(account_id) != owner):
            raise MemoryNukeUnavailable("reset account changed")
        from src.openclank.chat_source_bridge import canonical_source_provider
        if canonical_source_provider(getattr(coordinator, '_reset_state', None)) is not (coordinator.canonical_memory_provider or coordinator.memory_provider):
            raise MemoryNukeUnavailable('reset canonical source changed')
        with locked(os.fspath(coordinator.lock_target)):
            row = coordinator._load_journal().get(operation_id)
            claim = row.get("commit_claim") if isinstance(row, dict) else None
            participants = row.get('participants') if isinstance(row, dict) else None
            if provider is coordinator.canonical_memory_provider and provider is not coordinator.memory_provider:
                if (type(participants) is not dict or type(participants.get('fm')) is not dict
                        or provider.persisted_database_identity()['database_id'] != participants['fm'].get('database_id')):
                    raise MemoryNukeUnavailable('reset FM participant changed')
            scope = getattr(provider, '_scope', None)
            if callable(scope):
                current_owner = scope(owner).owner
            else:
                from src.memory_scope import memory_owner
                current_owner = memory_owner(owner)
                if type(participants) is not dict or type(participants.get('native')) is not dict:
                    raise MemoryNukeUnavailable('reset Native participant unavailable')
                frozen = participants['native'].get('snapshot')
                current = provider.lifecycle_identity(current_owner)
                if type(frozen) is not dict or any(current.get(key) != frozen.get(key) for key in ('version','kind','root','device','inode','owner')):
                    raise MemoryNukeUnavailable('reset Native participant changed')
            if (not isinstance(row, dict) or row.get("owner") != owner
                    or row.get("account_id") != account_id or row.get("state") != "committing"
                    or row.get("guarded_reset") is not True or row.get("result") is not None
                    or row.get("provider_owner") != current_owner
                    or not isinstance(claim, dict)
                    or claim.get("coordinator_id") != coordinator._commit_claim_id
                    or claim.get("host") != socket.gethostname() or claim.get("pid") != os.getpid()):
                raise MemoryNukeUnavailable("reset claim is no longer current")


    def assert_current_post_dispatch(self, *, provider, frozen):
        """Check only the original started claim before an exact receipt read.

        This is not a mutation grant or a committed-receipt assertion.
        """
        coordinator = self._coordinator
        if (self._seal is not _RESET_CLAIM_SEAL or provider is None
            or provider is not (coordinator.canonical_memory_provider or coordinator.memory_provider)
            or type(frozen) is not dict or set(frozen) != {'version','database','request'}
            or frozen.get('version') != 1 or type(frozen.get('request')) is not dict):
            raise MemoryNukeUnavailable('post-dispatch reset claim unavailable')
        auth = getattr(coordinator, '_reset_auth', None)
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.fm_reset_receipt import _identity
        if (auth is None or auth.account_id(self.owner) != self.account_id
            or auth.username_for_account_id(self.account_id) != self.owner
            or canonical_source_provider(getattr(coordinator,'_reset_state',None)) is not provider
            or _identity(getattr(provider,'_guarded_preparation',None)) != frozen['database']):
            raise MemoryNukeUnavailable('post-dispatch reset authority changed')
        scope = getattr(provider,'_scope',None)
        if not callable(scope): raise MemoryNukeUnavailable('post-dispatch reset provider unavailable')
        provider_owner = scope(self.owner).owner
        with locked(os.fspath(coordinator.lock_target)):
            row = coordinator._load_journal().get(self.operation_id)
            claim = row.get('commit_claim') if type(row) is dict else None
            request = frozen['request']
            if (type(row) is not dict or row.get('owner') != self.owner
                or row.get('account_id') != self.account_id or row.get('provider_owner') != provider_owner
                or row.get('state') != 'committing' or row.get('guarded_reset') is not True
                or row.get('result') is not None or type(claim) is not dict
                or claim.get('coordinator_id') != coordinator._commit_claim_id
                or claim.get('host') != socket.gethostname() or claim.get('pid') != os.getpid()
                or row.get('reset_native_started') is not True or row.get('reset_native_frozen') != frozen
                or row.get('reset_native_request') != request or request.get('owner') != provider_owner
                or request.get('account_id') != self.account_id or request.get('operation_id') != self.operation_id):
                raise MemoryNukeUnavailable('post-dispatch reset claim changed')
            participants = row.get('participants')
            if provider is not coordinator.memory_provider:
                fm = participants.get('fm') if type(participants) is dict else None
                if (type(fm) is not dict or fm.get('database_id') != frozen['database'].get('database_id')
                    or fm.get('components') != request.get('components') or fm.get('expected') != request.get('expected_counts')):
                    raise MemoryNukeUnavailable('post-dispatch reset participant changed')
        if (auth.account_id(self.owner) != self.account_id or auth.username_for_account_id(self.account_id) != self.owner
            or canonical_source_provider(getattr(coordinator,'_reset_state',None)) is not provider
            or _identity(getattr(provider,'_guarded_preparation',None)) != frozen['database']):
            raise MemoryNukeUnavailable('post-dispatch reset authority changed')
        return True


class MemoryNukeCoordinator:
    """Bind an owner reset commit to one short-lived, durable preview."""

    def __init__(
        self,
        memory_provider,
        skills_manager=None,
        *,
        canonical_memory_provider=None,
        memory_manager=None,
        media_store=None,
        session_factory=None,
        skill_job_canceller=None,
        skill_job_runtime_sync=None,
        data_dir: str | os.PathLike[str] | None = None,
        preview_ttl_seconds: int = 300,
        clock: Callable[[], float] = time.time,
    ):
        self.memory_provider = memory_provider
        self.canonical_memory_provider = canonical_memory_provider
        self.skills_manager = skills_manager
        self.memory_manager = memory_manager
        self.media_store = media_store
        if session_factory is None and memory_manager is not None:
            from core.database import SessionLocal

            session_factory = SessionLocal
        self.session_factory = session_factory
        self.skill_job_canceller = skill_job_canceller
        # Optional process-local map repair hook.  The durable SkillsManager
        # purge removes the owner rows from disk, but the route module also
        # keeps in-memory job maps; invoke this only after the purge has
        # completed so a stale callback cannot resurrect the rows.
        self.skill_job_runtime_sync = skill_job_runtime_sync
        root = Path(
            data_dir
            or getattr(skills_manager, "data_dir", None)
            or DATA_DIR
        ).resolve()
        self.data_dir = root
        self.journal_root = root / ".memory-nuke"
        if self.journal_root.is_symlink():
            raise MemoryNukeUnavailable("memory reset journal root cannot be a symlink")
        self.journal_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.journal_root, 0o700)
        self.journal_file = self.journal_root / "operations.json"
        self.lock_target = self.journal_root / "JOURNAL"
        self.preview_ttl_seconds = max(30, int(preview_ttl_seconds))
        self.clock = clock
        self._commit_locks: dict[str, asyncio.Lock] = {}
        self._commit_claim_id = uuid.uuid4().hex

    @staticmethod
    def _legacy_row_owner(value: object, owner: str) -> bool:
        return str(value or "") == owner

    def _legacy_residue_snapshot(
        self,
        owner: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Snapshot compatibility stores that could replay provider memory."""
        memory_entries: list[dict[str, Any]] = []
        retained_entries: list[dict[str, Any]] = []
        original_entries: list[dict[str, Any]] = []
        memory_file = None
        provider_owns_json = (
            self.memory_manager is not None
            and getattr(self.memory_provider, "memory_manager", None)
            is self.memory_manager
        )
        # In auth-disabled mode the provider's durable owner is ``local`` but
        # pre-convention compatibility rows may still be ownerless. Let the
        # provider clear ``local`` and clear only those ownerless rows here.
        if self.memory_manager is not None and (
            not provider_owns_json or owner == ""
        ):
            memory_file = Path(self.memory_manager.memory_file)
            if memory_file.is_symlink():
                raise MemoryNukeUnavailable("legacy memory file cannot be a symlink")
            try:
                raw_entries = json.loads(memory_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw_entries = []
            except (OSError, ValueError) as exc:
                raise MemoryNukeUnavailable("legacy memory file is unreadable") from exc
            if not isinstance(raw_entries, list) or any(
                not isinstance(row, dict) for row in raw_entries
            ):
                raise MemoryNukeUnavailable("legacy memory file has an invalid shape")
            original_entries = list(raw_entries)
            comparison_entries = (
                self.memory_manager.load_all()
                if provider_owns_json and owner == ""
                else raw_entries
            )
            memory_entries = [
                row for row in comparison_entries
                if self._legacy_row_owner(row.get("owner"), owner)
            ]
            retained_entries = [
                row for row in raw_entries
                if not self._legacy_row_owner(row.get("owner"), owner)
            ]

        tidy_file = (
            Path(self.memory_manager.memory_file).with_name("memory_tidy_state.json")
            if self.memory_manager is not None
            else None
        )
        tidy_state: dict[str, Any] = {}
        tidy_present = False
        if tidy_file is not None:
            if tidy_file.is_symlink():
                raise MemoryNukeUnavailable("memory tidy state cannot be a symlink")
            try:
                value = json.loads(tidy_file.read_text(encoding="utf-8"))
            except FileNotFoundError:
                value = {}
            except (OSError, ValueError) as exc:
                raise MemoryNukeUnavailable("memory tidy state is unreadable") from exc
            if not isinstance(value, dict):
                raise MemoryNukeUnavailable("memory tidy state has an invalid shape")
            tidy_state = value
            tidy_present = owner in tidy_state

        database_rows: list[dict[str, Any]] = []
        if self.session_factory is not None:
            from core.database import Memory

            session = self.session_factory()
            try:
                query = session.query(Memory)
                if owner:
                    query = query.filter(Memory.owner == owner)
                else:
                    from sqlalchemy import or_

                    query = query.filter(or_(Memory.owner.is_(None), Memory.owner == ""))
                for row in query.order_by(Memory.id).all():
                    database_rows.append({
                        "id": str(row.id),
                        "text_sha256": hashlib.sha256(
                            str(row.text or "").encode("utf-8")
                        ).hexdigest(),
                        "category": str(row.category or ""),
                        "source": str(row.source or ""),
                        "owner": str(row.owner or ""),
                        "session_id": str(row.session_id or ""),
                        "timestamp": int(row.timestamp or 0),
                    })
            finally:
                session.close()

        material = {
            "memory_json": memory_entries,
            "tidy": tidy_state.get(owner) if tidy_present else None,
            "database": database_rows,
        }
        preview = {
            "count": len(memory_entries) + len(database_rows) + int(tidy_present),
            "fingerprint": "sha256:" + hashlib.sha256(
                _canonical_json(material)
            ).hexdigest(),
        }
        return preview, {
            "memory_file": memory_file,
            "original_entries": original_entries,
            "retained_entries": retained_entries,
            "tidy_file": tidy_file,
            "tidy_state": tidy_state,
            "tidy_present": tidy_present,
            "database_ids": [row["id"] for row in database_rows],
        }

    def _purge_legacy_residue(
        self,
        owner: str,
        *,
        expected: dict[str, Any],
    ) -> dict[str, Any]:
        from contextlib import nullcontext
        guard = locked(self.memory_manager.memory_file) if self.memory_manager is not None else nullcontext()
        with guard:
            current, plan = self._legacy_residue_snapshot(owner)
            if current != expected and int(current.get("count") or 0) != 0:
                raise MemoryNukeConflict("legacy memory reset preview is stale")
            if int(current.get("count") or 0) == 0:
                return {"complete": True, "count": 0}

            session = self.session_factory() if self.session_factory is not None else None
            try:
                if plan["memory_file"] is not None:
                    self.memory_manager.save(plan["retained_entries"])
                if plan["tidy_file"] is not None and plan["tidy_present"]:
                    retained_tidy = dict(plan["tidy_state"])
                    retained_tidy.pop(owner, None)
                    atomic_write_json(os.fspath(plan["tidy_file"]), retained_tidy, indent=2)
                if session is not None and plan["database_ids"]:
                    from core.database import Memory

                    session.query(Memory).filter(
                        Memory.id.in_(plan["database_ids"])
                    ).delete(synchronize_session=False)
                    session.commit()
            except Exception:
                if session is not None:
                    session.rollback()
                # Compatibility files are small and atomically replaced. Restore
                # them if the SQL compatibility delete did not commit.
                if plan["memory_file"] is not None:
                    self.memory_manager.save(plan["original_entries"])
                if plan["tidy_file"] is not None and plan["tidy_present"]:
                    atomic_write_json(
                        os.fspath(plan["tidy_file"]), plan["tidy_state"], indent=2
                    )
                raise
            finally:
                if session is not None:
                    session.close()
            after, _unused = self._legacy_residue_snapshot(owner)
            if after.get("count") != 0:
                raise MemoryNukeUnavailable("legacy memory rows remain after reset")
            return {"complete": True, "count": int(current["count"])}

    @staticmethod
    def normalize_components(values: Iterable[object]) -> list[str]:
        if not isinstance(values, list) or not values:
            raise MemoryNukeError("components must be a non-empty array")
        if any(not isinstance(value, str) or value not in NUKE_COMPONENTS for value in values):
            raise MemoryNukeError("components contain an unknown memory reset category")
        if len(set(values)) != len(values):
            raise MemoryNukeError("components must be unique")
        return sorted(values)

    def _load_journal(self) -> dict[str, dict[str, Any]]:
        if not self.journal_file.exists():
            return {}
        if self.journal_file.is_symlink():
            raise MemoryNukeUnavailable("memory reset journal cannot be a symlink")
        try:
            value = json.loads(self.journal_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MemoryNukeUnavailable("memory reset journal is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("version") != _JOURNAL_VERSION
            or not isinstance(value.get("operations"), dict)
        ):
            raise MemoryNukeUnavailable("memory reset journal is invalid")
        return dict(value["operations"])

    def _save_journal(self, operations: dict[str, dict[str, Any]]) -> None:
        # Bound terminal history without discarding a commit that already
        # entered (and may need retry after restart). Only untouched expired
        # previews are safe to prune by their confirmation TTL.
        terminal = sorted(
            (
                (float(row.get("updated_at") or 0), key)
                for key, row in operations.items()
                if isinstance(row, dict) and row.get("result") is not None
            ),
            reverse=True,
        )
        keep_terminal = {key for _updated, key in terminal[:256]}
        now = self.clock()
        kept = {
            key: row
            for key, row in operations.items()
            if (
                not isinstance(row, dict)
                or (
                    row.get("result") is None
                    and (
                        row.get("state") != "preview"
                        or float(row.get("expires_at_epoch") or 0) >= now
                    )
                )
                or (
                    row.get("result") is not None
                    and key in keep_terminal
                )
            )
        }
        atomic_write_json(
            os.fspath(self.journal_file),
            {"version": _JOURNAL_VERSION, "operations": kept},
            indent=2,
        )
        os.chmod(self.journal_file, 0o600)

    def owner_is_fenced(self, owner: str) -> bool:
        with locked(os.fspath(self.lock_target)):
            operations = self._load_journal()
            for row in operations.values():
                if not isinstance(row, dict):
                    raise MemoryNukeUnavailable("memory reset journal is invalid")
                if row.get("guarded_reset") is True and row.get("owner") == owner and row.get("state") in {"committing", "partial"} and row.get("result") is None:
                    return True
        return False

    async def recovery_preview(self, *, owner: str, provider_owner: str) -> dict[str, Any]:
        with locked(os.fspath(self.lock_target)):
            operations = self._load_journal()
            matches = [(key, row) for key, row in operations.items()
                       if isinstance(row, dict) and row.get("owner") == owner
                       and row.get("provider_owner") == provider_owner
                       and row.get("guarded_reset") is True
                       and row.get("state") in {"committing", "partial"} and row.get("result") is None]
        if not matches:
            return {"status":"none"}
        if len(matches) != 1:
            raise MemoryNukeUnavailable("one recoverable reset is required")
        operation_id, _ = matches[0]
        lock = self._commit_locks.setdefault(operation_id, asyncio.Lock())
        if lock.locked():
            raise MemoryNukeConflict("memory reset commit is already in progress")
        async with lock:
            with locked(os.fspath(self.lock_target)):
                operations = self._load_journal()
                row = operations.get(operation_id)
                if (not isinstance(row, dict) or row.get("owner") != owner
                        or row.get("provider_owner") != provider_owner
                        or row.get("guarded_reset") is not True
                        or row.get("state") not in {"committing", "partial"} or row.get("result") is not None):
                    raise MemoryNukeConflict("memory reset recovery changed")
                claim = row.get("commit_claim")
                if isinstance(claim, dict) and (claim.get("host") != socket.gethostname()
                        or (claim.get("coordinator_id") != self._commit_claim_id and _process_is_running(claim.get("pid")))):
                    raise MemoryNukeConflict("memory reset commit is already in progress")
                recovery_components = {key: {'count': int(value['count'])} for key, value in row.get('expected', {}).items()}
                participants = row.get('participants')
                if participants is not None:
                    try:
                        for key, value in participants['fm']['expected'].items():
                            recovery_components.setdefault(key, {'count': 0})['count'] += int(value['count'])
                    except (TypeError, KeyError, ValueError) as exc:
                        raise MemoryNukeUnavailable('retained reset recovery unavailable') from exc
                token = secrets.token_urlsafe(32)
                confirmation = "RESUME " + secrets.token_hex(4).upper()
                row["preview_token_sha256"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
                row["confirmation"] = confirmation
                row["updated_at"] = self.clock()
                self._save_journal(operations)
                return {"status":"preview", "complete":False, "operation_id":operation_id,
                        "preview_token":token, "confirmation":confirmation,
                        "components":{key:{'count_unavailable':True} for key in recovery_components},
                        "expanded_components":sorted(recovery_components), "requested_components":row.get("components", []),
                        "recovery":True, "progress":row.get("last_attempt")}

    @staticmethod
    def _validate_component_preview(value: object, component: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise MemoryNukeUnavailable(
                f"{component} reset preview did not return a component object"
            )
        count = value.get("count")
        fingerprint = value.get("fingerprint")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise MemoryNukeUnavailable(
                f"{component} reset preview returned an invalid count"
            )
        if not isinstance(fingerprint, str) or not fingerprint:
            raise MemoryNukeUnavailable(
                f"{component} reset preview returned no fingerprint"
            )
        # Preserve the provider's full canonical component object unchanged;
        # commit passes this exact object back as the CAS expectation.
        return json.loads(_canonical_json(value))

    async def preview(
        self,
        *,
        owner: str,
        provider_owner: str,
        components: list[str],
        agent_supervisor=None,
        reset_auth=None,
        reset_state=None,
        selective_account_id=None,
        selective_target_authority=None,
        selective_scope_current=None,
        full_account_id=None,
        full_target_authority=None,
        full_scope_current=None,
    ) -> dict[str, Any]:
        components = self.normalize_components(components)
        db_components = [item for item in components if item in _DB_COMPONENTS]
        expected: dict[str, dict[str, Any]] = {}
        provider_implications: list[str] = []
        participants = None
        selective_provider = self.canonical_memory_provider or self.memory_provider
        selective = components == ['ingest'] and getattr(selective_provider,'_guarded_v17',False) is True
        full = 'memories' in components and getattr(selective_provider,'_guarded_v17',False) is True
        scoped_account_id=full_account_id if full else selective_account_id
        scoped_target_authority=full_target_authority if full else selective_target_authority
        scoped_current=full_scope_current if full else selective_scope_current
        preview_target = None
        if selective or full:
            from src.openclank.memory_job_admission import TargetAuthority
            from src.openclank.mimo_supervisor import _queued_owned_thread
            if (type(scoped_account_id) is not str or not scoped_account_id
                or type(scoped_target_authority) is not TargetAuthority or not callable(scoped_current)
                or reset_auth is None or scoped_target_authority._auth is not reset_auth):
                raise MemoryNukeUnavailable('selective preview authority unavailable')
            def current():
                if (scoped_current() is not True or reset_auth.account_id(owner) != scoped_account_id
                    or reset_auth.username_for_account_id(scoped_account_id) != owner):
                    raise MemoryNukeUnavailable('selective preview authority changed')
                target = scoped_target_authority.resolve(scoped_account_id)
                if target.source_account_id != scoped_account_id or target.memory_owner != provider_owner:
                    raise MemoryNukeUnavailable('selective preview target changed')
                return target
            preview_target = await _queued_owned_thread(current)
        async def provider_preview_for(provider):
            if selective and provider is selective_provider:
                preview = getattr(provider,'_host_selective_ingest_preview',None)
                if not callable(preview): raise MemoryNukeUnavailable('selective preview unavailable')
                before = await _queued_owned_thread(current)
                if before != preview_target: raise MemoryNukeUnavailable('selective preview target changed')
                value = await preview(owner=before.memory_owner,workspace_id=before.memory_workspace,
                    account_id=scoped_account_id,deadline=float(asyncio.get_running_loop().time()+5.0))
                if await _queued_owned_thread(current) != before:
                    raise MemoryNukeUnavailable('selective preview authority changed')
                return value
            if full and provider is selective_provider:
                preview = getattr(provider,'_host_guarded_full_memory_preview',None)
                if not callable(preview): raise MemoryNukeUnavailable('full reset preview unavailable')
                before = await _queued_owned_thread(current)
                if before != preview_target: raise MemoryNukeUnavailable('full reset preview target changed')
                value = await preview(owner=before.memory_owner,workspace_id=before.memory_workspace,
                    account_id=scoped_account_id,components=db_components,deadline=float(asyncio.get_running_loop().time()+5.0))
                if await _queued_owned_thread(current) != before:
                    raise MemoryNukeUnavailable('full reset preview authority changed')
                return value
            return await provider.reset_owner('reset_preview',owner=provider_owner,
                components=db_components,expected_counts=None)

        if db_components:
            reset_owner = getattr(self.memory_provider, "reset_owner", None)
            if not callable(reset_owner):
                raise MemoryNukeUnavailable(
                    "active memory provider does not expose owner reset"
                )
            provider_preview = None
            try:
                provider_preview = await provider_preview_for(self.memory_provider)
            except Exception as exc:
                # A provider refusal (e.g. immutable ingest job-event history)
                # carries an actionable reason; surface it bounded instead of
                # letting the route fall through to a generic 503.
                raise MemoryNukeUnavailable(
                    f"memory provider refused the reset preview: {_clean_error(exc)}"
                ) from exc
            returned = (
                provider_preview.get("components")
                if isinstance(provider_preview, dict)
                else None
            )
            if not isinstance(returned, dict):
                raise MemoryNukeUnavailable(
                    "active memory provider returned an invalid reset preview"
                )
            raw_implications = provider_preview.get("implications")
            if isinstance(raw_implications, list):
                provider_implications = [
                    str(value)[:500]
                    for value in raw_implications
                    if isinstance(value, str) and value.strip()
                ]
            expanded = provider_preview.get("expanded_components")
            if expanded is None:
                expanded = db_components
            if (
                not isinstance(expanded, list)
                or any(
                    not isinstance(value, str) or value not in _DB_COMPONENTS
                    for value in expanded
                )
                or len(expanded) != len(set(expanded))
            ):
                raise MemoryNukeUnavailable(
                    "active memory provider returned invalid reset dependencies"
                )
            effective_db_components = sorted(set(db_components) | set(expanded))
            for component in effective_db_components:
                expected[component] = self._validate_component_preview(
                    returned.get(component), component
                )

            canonical = self.canonical_memory_provider
            if canonical is not None and canonical is not self.memory_provider:
                from src.memory_provider import NativeMemoryProvider
                from src.memery_provider import MemeryProvider
                if type(self.memory_provider) is not NativeMemoryProvider or not isinstance(canonical, MemeryProvider) or not canonical._guarded_v17:
                    raise MemoryNukeUnavailable('retained reset participants unavailable')
                native_snapshot = self.memory_provider.lifecycle_snapshot(provider_owner)
                if ('memories' in expected and (expected['memories']['count'] != native_snapshot['count']
                        or expected['memories']['fingerprint'] != native_snapshot['reset_fingerprint'])):
                    raise MemoryNukeConflict('native reset preview changed')
                identity = canonical.persisted_database_identity()
                fm_preview = await provider_preview_for(canonical)
                fm_returned = fm_preview.get('components') if isinstance(fm_preview, dict) else None
                fm_expanded = fm_preview.get('expanded_components') if isinstance(fm_preview, dict) else None
                if (type(fm_returned) is not dict or type(fm_expanded) is not list or len(set(fm_expanded)) != len(fm_expanded)
                        or any(type(value) is not str or value not in _DB_COMPONENTS for value in fm_expanded)):
                    raise MemoryNukeUnavailable('retained reset preview unavailable')
                fm_components = sorted(set(db_components) | set(fm_expanded))
                fm_expected = {key:self._validate_component_preview(fm_returned.get(key), key) for key in fm_components}
                participants = {'version':1, 'native':{'snapshot':native_snapshot},
                    'fm':{'database_id':identity['database_id'], 'components':fm_components, 'expected':fm_expected}}

        if "skills" in components:
            preview_owner_purge = getattr(
                self.skills_manager, "preview_owner_purge", None
            )
            if not callable(preview_owner_purge):
                raise MemoryNukeUnavailable(
                    "active skills store does not expose owner reset"
                )
            expected["skills"] = self._validate_component_preview(
                preview_owner_purge(owner), "skills"
            )

        agent_memory_expected = None
        if "memories" in expected:
            # Fail closed, symmetrically with commit: without an agent
            # runtime the authored-memory side would be silently skipped
            # while the operation reports complete.
            preview_agent_memory = (
                getattr(agent_supervisor, "preview_owner_memory", None)
                if agent_supervisor is not None
                else None
            )
            if not callable(preview_agent_memory):
                raise MemoryNukeUnavailable(
                    "active agent runtime does not expose authored-memory reset"
                )
            agent_memory_expected = self._validate_component_preview(
                await preview_agent_memory(owner), "agent authored memory"
            )
        legacy_memory_expected = None
        if "memories" in expected:
            legacy_memory_expected, _legacy_plan = self._legacy_residue_snapshot(owner)
        media_expected = None
        if "memories" in expected and self.media_store is not None:
            media_expected = self._validate_component_preview(
                self.media_store.preview_owner_purge(owner),
                "memory media",
            )
        staging_expected = None
        snapshot_import_claims = None
        if "ingest" in expected and owner:
            from services.memory.import_batch import MemoryImportBatchStore
            from src.constants import FM_DB_PATH

            batch_store = MemoryImportBatchStore(
                db_path=str(
                    getattr(self.canonical_memory_provider or self.memory_provider, "_fm_db_path", None) or FM_DB_PATH
                ),
                data_dir=os.path.dirname(str(getattr(self.canonical_memory_provider or self.memory_provider, "_fm_db_path", None) or FM_DB_PATH)) or DATA_DIR,
            )
            staging_expected = batch_store.preview_owner_staging(owner)
            snapshot_account = reset_auth.account_id(owner) if reset_auth is not None else None
            if snapshot_account and reset_auth.username_for_account_id(snapshot_account) == owner:
                snapshot_import_claims = batch_store.preview_snapshot_import_claims(owner=owner, account=snapshot_account)

        operation_id = "nuke_" + uuid.uuid4().hex
        queued_expected = None
        source_provider = self.canonical_memory_provider or self.memory_provider
        fm_components = ((participants or {}).get('fm') or {}).get('components', [])
        if (getattr(source_provider,'_guarded_v17',False)
            and {'memories','ingest'}.intersection(set(expected) | set(fm_components))):
            account_id = reset_auth.account_id(owner) if reset_auth is not None else None
            if not account_id or reset_auth.username_for_account_id(account_id)!=owner:
                raise MemoryNukeUnavailable('queued reset account unavailable')
            from src.openclank.conversation_archive import get_conversation_archive
            try:
                queued_expected=await asyncio.to_thread(get_conversation_archive().queued_reset_inventory,account_id,operation_id)
            except Exception as exc:
                raise MemoryNukeUnavailable('queued source reset inventory unavailable') from exc
            if queued_expected['count']:
                preview = getattr(reset_state, 'queued_reset_preview', None)
                if not callable(preview):
                    raise MemoryNukeUnavailable('queued native runtime retirement is unavailable')
                try:
                    queued_profile = await preview(owner=owner, account_id=account_id,
                        operation_id=operation_id, expected=queued_expected, provider=source_provider)
                except Exception as exc:
                    raise MemoryNukeUnavailable('queued native runtime retirement is unavailable') from exc
                if (type(queued_profile) is not dict or set(queued_profile) != {'version','fingerprint'}
                    or type(queued_profile['version']) is not int or queued_profile['version'] != 1
                    or type(queued_profile['fingerprint']) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', queued_profile['fingerprint']) is None):
                    raise MemoryNukeUnavailable('queued native runtime retirement is unavailable')
            else:
                refresh=getattr(reset_state,'refresh_reset_capability_for_owner',None)
                if callable(refresh):
                    await refresh(state=reset_state,owner=owner,account_id=account_id,provider=source_provider,
                        terminal_s08='memories' in components)
        if (selective or full) and await _queued_owned_thread(current) != preview_target:
            raise MemoryNukeUnavailable('reset preview authority changed')
        preview_token = secrets.token_urlsafe(32)
        expires_epoch = self.clock() + self.preview_ttl_seconds
        binding = {
            "version": 1,
            "operation_id": operation_id,
            "owner": owner,
            "provider_owner": provider_owner,
            "components": components,
            "expected": expected,
            **({'selective_account_id': selective_account_id} if selective else {}),
            **({'full_account_id': full_account_id} if full else {}),
            **({'queued_expected':queued_expected,'queued_account_id':account_id} if queued_expected is not None else {}),
            **({'queued_profile':queued_profile} if queued_expected is not None and queued_expected['count'] else {}),
            **({'participants': participants} if participants is not None else {}),
            "agent_memory_expected": agent_memory_expected,
            "legacy_memory_expected": legacy_memory_expected,
            "media_expected": media_expected,
            "staging_expected": staging_expected,
            **({"snapshot_import_claims": snapshot_import_claims} if snapshot_import_claims is not None else {}),
            "retained": [dict(item) for item in _RETAINED_DOMAINS],
            "expires_at_epoch": expires_epoch,
        }
        binding_digest = hashlib.sha256(_canonical_json(binding)).hexdigest()
        confirmation = "NUKE " + binding_digest[:8].upper()
        record = {
            **binding,
            "binding_digest": binding_digest,
            "preview_token_sha256": hashlib.sha256(
                preview_token.encode("utf-8")
            ).hexdigest(),
            "confirmation": confirmation,
            "state": "preview",
            "created_at": self.clock(),
            "updated_at": self.clock(),
            "result": None,
        }
        with locked(os.fspath(self.lock_target)):
            operations = self._load_journal()
            operations[operation_id] = record
            self._save_journal(operations)

        implications = list(provider_implications)
        if db_components:
            implications.insert(0,
                "Selected live memory data is permanently cleared for this account."
            )
        if "skills" in components:
            implications.append(
                "Selected user-authored skills and their live lifecycle records are cleared."
            )
        for retained in _RETAINED_DOMAINS:
            description = retained["description"]
            if description not in implications:
                implications.append(description)
        public_counts = {
            key: {"count": int(expected[key]["count"])}
            for key in sorted(expected)
        }
        if participants is not None:
            for key, value in participants['fm']['expected'].items():
                public_counts.setdefault(key, {'count': 0})['count'] += int(value['count'])
            implications.append('The selected Native store and retained Memery store are both included.')
        selected_ingest = ((participants or {}).get('fm') or {}).get('expected', {}).get('ingest') if participants else expected.get('ingest')
        if type(selected_ingest) is dict and 'selective' in selected_ingest:
            public_counts.setdefault('ingest', {'count':0})['selective'] = json.loads(_canonical_json(selected_ingest['selective']))
        if agent_memory_expected is not None:
            public_counts["memories"]["count"] += int(
                agent_memory_expected["count"]
            )
        if legacy_memory_expected is not None:
            public_counts["memories"]["count"] += int(
                legacy_memory_expected["count"]
            )
        if media_expected is not None:
            public_counts["memories"]["count"] += int(media_expected["count"])
        if staging_expected is not None and not selective:
            public_counts["ingest"]["count"] += int(staging_expected["count"])
        return {
            "status": "preview",
            "complete": False,
            "operation_id": operation_id,
            "preview_token": preview_token,
            "confirmation": confirmation,
            "components": public_counts,
            "expanded_components": sorted(public_counts),
            "implications": implications,
            "retained": [dict(item) for item in _RETAINED_DOMAINS],
            "expires_at": _expires_at(expires_epoch),
        }

    @staticmethod
    def _category_result(value: object, *, fallback_count: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {
                "state": "failed",
                "count": 0,
                "error": "provider returned no category result",
            }
        state = str(value.get("state") or "").lower()
        complete = state in {"complete", "completed", "deleted", "purged", "success"}
        count = value.get("count", fallback_count if complete else 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            count = 0
            complete = False
        out: dict[str, Any] = {
            "state": "complete" if complete else "failed",
            "count": count,
        }
        if 'selective' in value:
            out['selective'] = json.loads(_canonical_json(value['selective']))
        if not complete:
            error = value.get("error") or "component reset did not complete"
            out["error"] = " ".join(str(error).split())[:300]
        return out

    def _validated_record(
        self,
        *,
        operations: dict[str, dict[str, Any]],
        owner: str,
        provider_owner: str,
        operation_id: str,
        preview_token: str,
        confirmation: str,
        check_expiry: bool = True,
    ) -> dict[str, Any]:
        record = operations.get(operation_id)
        if not isinstance(record, dict):
            raise MemoryNukeConflict("memory reset preview was not found")
        supplied_hash = hashlib.sha256(preview_token.encode("utf-8")).hexdigest()
        valid = (
            record.get("owner") == owner
            and record.get("provider_owner") == provider_owner
            and hmac.compare_digest(
                str(record.get("preview_token_sha256") or ""), supplied_hash
            )
            and hmac.compare_digest(
                str(record.get("confirmation") or ""), confirmation
            )
        )
        if not valid:
            raise MemoryNukeConflict("memory reset confirmation does not match preview")
        if check_expiry and record.get("state") == "preview" and float(
            record.get("expires_at_epoch") or 0
        ) < self.clock():
            raise MemoryNukeConflict("memory reset preview expired")
        return record

    async def _selective_ingest_partition(self, *, claim, provider, target_authority, barrier, witness, deadline):
        """Read a selective partition under the original, zero-queue reset scope.

        This is unexposed preparation only; it does not authorize reset issue.
        """
        from src.openclank.account_request_barrier import AccountRequestBarrier, _ResetDrainWitness
        from src.openclank.conversation_archive import get_conversation_archive, QueuedResetCompletion
        from src.openclank.memory_job_admission import TargetAuthority
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(claim) is not MemoryResetClaim or claim._coordinator is not self
            or type(target_authority) is not TargetAuthority or type(barrier) is not AccountRequestBarrier
            or type(witness) is not _ResetDrainWitness or witness.owner != claim.owner
            or barrier is not getattr(self._reset_state, 'account_request_barrier', None)
            or not barrier.reset_witness_current(witness)):
            raise MemoryNukeUnavailable('selective ingest authority unavailable')
        def current():
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=provider,operation_id=claim.operation_id)
            if not barrier.reset_witness_current(witness):
                raise MemoryNukeUnavailable('selective ingest exclusion changed')
            with locked(os.fspath(self.lock_target)):
                row = self._load_journal().get(claim.operation_id)
                expected = row.get('queued_expected') if type(row) is dict else None
                if (type(expected) is not dict or type(expected.get('count')) is not int or expected['count'] != 0
                    or row.get('queued_account_id') != claim.account_id):
                    raise MemoryNukeUnavailable('selective queued inventory unavailable')
                expected = json.loads(_canonical_json(expected))
            archive = get_conversation_archive()
            proof = archive.queued_reset_completion(claim=claim,provider=provider,owner=claim.owner,
                account_id=claim.account_id,operation_id=claim.operation_id,expected=expected)
            if type(proof) is not QueuedResetCompletion:
                raise MemoryNukeUnavailable('selective queued inventory unavailable')
            target = target_authority.resolve(claim.account_id)
            claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=provider,operation_id=claim.operation_id)
            if not barrier.reset_witness_current(witness):
                raise MemoryNukeUnavailable('selective ingest exclusion changed')
            return expected, target
        before = await _queued_owned_thread(current)
        partition = await provider._host_selective_ingest_partition(claim=claim,target_authority=target_authority,
            barrier=barrier,witness=witness,deadline=deadline)
        after = await _queued_owned_thread(current)
        if before != after or type(partition) is not dict:
            raise MemoryNukeUnavailable('selective ingest partition changed')
        return {'queued_inventory':before[0], 'partition':partition}

    async def _settle_selective_ingest_handoff(self, *, token, provider, barrier, witness):
        """Settle only the original token, checkpoint facts, then release it."""
        from services.memory.import_batch import MemoryImportBatchStore
        from src.openclank.mimo_supervisor import _queued_owned_thread
        from src.constants import FM_DB_PATH
        claim=token.claim; frozen=token.frozen
        actual_db=getattr(self.memory_provider,'_fm_db_path',None) or FM_DB_PATH
        if os.path.realpath(str(actual_db)) != frozen.get('database',{}).get('path'):
            raise MemoryNukeUnavailable('selective batch owner database differs')
        staging_store=MemoryImportBatchStore(db_path=str(actual_db),data_dir=os.path.dirname(str(actual_db)) or DATA_DIR)
        prepared=await provider._run_original_reset_handoff(token,claim.assert_current_post_dispatch)
        receipt=prepared.get('receipt') if type(prepared) is dict else None
        if (type(receipt) is not dict or receipt.get('state') != 'committed'
            or type(receipt.get('sql_result')) is not dict):
            raise MemoryNukeUnavailable('selective original receipt unavailable')
        provider._rebind_original_reset_consumers(token,claim.assert_current_post_dispatch)
        with locked(os.fspath(self.lock_target)):
            row=self._load_journal().get(claim.operation_id)
            steps=row.get('reset_local_steps') if type(row) is dict else None
            prior=steps.get('staging') if type(steps) is dict else None
            if (type(row) is not dict or row.get('reset_native_frozen') != frozen
                or (prior is not None and (type(prior) is not dict or set(prior)!={'state','result'}
                    or prior.get('state')!='complete' or type(prior.get('result')) is not dict
                    or prior['result'].get('complete') is not True
                    or row.get('reset_native_result') != receipt['sql_result']))):
                raise MemoryNukeUnavailable('selective staging checkpoint changed')
        if prior is None:
            staging_result=await _queued_owned_thread(staging_store._cleanup_selective_staging,
                claim=claim,provider=provider,barrier=barrier,witness=witness)
            if type(staging_result) is not dict or staging_result.get('complete') is not True:
                raise MemoryNukeUnavailable('selective staging outcome unavailable')
            with locked(os.fspath(self.lock_target)):
                operations=self._load_journal();row=operations.get(claim.operation_id)
                if type(row) is not dict or row.get('reset_native_frozen') != frozen:
                    raise MemoryNukeUnavailable('selective original receipt changed')
                steps=row.setdefault('reset_local_steps',{})
                if type(steps) is not dict or steps.get('staging') is not None:
                    raise MemoryNukeUnavailable('selective staging checkpoint changed')
                steps['staging']={'state':'complete','result':staging_result}
                row['reset_native_result']=receipt['sql_result'];row['reset_selective_native_complete']=True
                self._save_journal(operations)
        provider._finish_original_reset_handoff(token,claim.assert_current_post_dispatch)
        return receipt['sql_result']

    async def _selective_ingest_original(self, *, claim, provider, target_authority, barrier, witness,
                                         write_deadline, untouched_preview, published_first_claim,
                                         withdraw_untouched_claim):
        """Unexposed original-only selective dispatch and owned cleanup."""
        from src.openclank.fm_reset_receipt import freeze_reset_receipt
        from src.openclank.mimo_supervisor import _queued_owned_thread
        if (type(untouched_preview) is not dict or type(published_first_claim) is not dict
            or not callable(withdraw_untouched_claim)):
            raise MemoryNukeUnavailable('selective original preview unavailable')
        def read_deadline():
            return float(min(write_deadline, asyncio.get_running_loop().time()+5.0))
        async def run():
            first = await self._selective_ingest_partition(claim=claim,provider=provider,
                target_authority=target_authority,barrier=barrier,witness=witness,deadline=read_deadline())
            manifest = {'version':1,'account_id':claim.account_id,'operation_id':claim.operation_id,
                        'partition':first['partition'],'queued_inventory':first['queued_inventory']}
            digest = hashlib.sha256(_canonical_json(manifest)).hexdigest()
            from services.memory.import_batch import MemoryImportBatchStore, _STAGING_LOCK
            imports = first['partition'].get('imports')
            if type(imports) is not dict:
                raise MemoryNukeUnavailable('selective import partition unavailable')
            from src.constants import FM_DB_PATH
            staging_db = getattr(self.memory_provider,'_fm_db_path',None) or FM_DB_PATH
            provider_db = getattr(provider,'_fm_db_path',None) or FM_DB_PATH
            if os.path.realpath(str(staging_db)) != os.path.realpath(str(provider_db)):
                raise MemoryNukeUnavailable('selective batch owner database differs')
            staging_store = MemoryImportBatchStore(db_path=str(staging_db),
                data_dir=os.path.dirname(str(staging_db)) or DATA_DIR)
            def observe_staging():
                def staging_current():
                    try:
                        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=provider,
                            operation_id=claim.operation_id)
                        return barrier.reset_witness_current(witness) is True
                    except Exception:
                        return False
                if staging_current() is not True: raise MemoryNukeUnavailable('selective exclusion changed')
                from src.openclank.import_staging_custody import verified_staging_relocations
                with staging_store._connect() as db:
                    db.execute('BEGIN')
                    row=db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
                    if row is None: raise MemoryNukeUnavailable('selective staging database unavailable')
                    verified_staging_relocations(db,database_id=row[0],account_id=claim.account_id,
                        current_owner=claim.owner,lifecycle=getattr(self._reset_state,'account_owner_lifecycle',None),
                        current=staging_current)
                    db.commit()
                with _STAGING_LOCK:
                    snapshot=staging_store._selective_staging_snapshot_unlocked(claim.owner,
                        retained=imports.get('retained_staging'),erasable=imports.get('erasable_staging'))
                claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=provider,operation_id=claim.operation_id)
                if not barrier.reset_witness_current(witness): raise MemoryNukeUnavailable('selective exclusion changed')
                return snapshot
            staging = await _queued_owned_thread(observe_staging)
            staging_hash = hashlib.sha256(_canonical_json(staging)).hexdigest()
            with locked(os.fspath(self.lock_target)):
                row = self._load_journal().get(claim.operation_id)
                participants = row.get('participants') if type(row) is dict else None
                fm = participants.get('fm') if type(participants) is dict else None
                expected = fm.get('expected') if type(fm) is dict else (row.get('expected') if type(row) is dict else None)
                components = fm.get('components') if type(fm) is dict else list(expected or {})
                if (row != published_first_claim or type(expected) is not dict or components != ['ingest'] or set(expected) != {'ingest'}
                    or row.get('reset_native_started') is True):
                    raise MemoryNukeUnavailable('selective original request unavailable')
                expected = json.loads(_canonical_json(expected))
            target = await _queued_owned_thread(target_authority.resolve,claim.account_id)
            frozen = await _queued_owned_thread(freeze_reset_receipt,provider._guarded_preparation,
                owner=target.memory_owner,account_id=claim.account_id,operation_id=claim.operation_id,
                workspace_id=target.memory_workspace,components=['ingest'],expected_counts=expected,
                queued_retired=False,selective_manifest_sha256=digest)
            second = await self._selective_ingest_partition(claim=claim,provider=provider,
                target_authority=target_authority,barrier=barrier,witness=witness,deadline=read_deadline())
            if second != first or await _queued_owned_thread(observe_staging) != staging:
                raise MemoryNukeUnavailable('selective partition or staging changed')
            with locked(os.fspath(self.lock_target)):
                operations = self._load_journal(); row = operations.get(claim.operation_id)
                if (row != published_first_claim or row.get('reset_native_started') is True
                    or row.get('reset_selective_manifest') not in (None,manifest)
                    or row.get('reset_selective_manifest_sha256') not in (None,digest)
                    or row.get('reset_native_frozen') not in (None,frozen)
                    or row.get('reset_native_request') not in (None,frozen['request'])):
                    raise MemoryNukeUnavailable('selective original request changed')
                row['reset_selective_manifest']=manifest; row['reset_selective_manifest_sha256']=digest
                row['reset_selective_staging']=staging; row['reset_selective_staging_sha256']=staging_hash
                row['reset_native_frozen']=frozen; row['reset_native_request']=frozen['request']
                self._save_journal(operations)
            begin_attempted = False
            if (type(write_deadline) is not float or not __import__('math').isfinite(write_deadline)
                or write_deadline - asyncio.get_running_loop().time() < 1.0):
                def rewind_pre_begin():
                    if begin_attempted:
                        raise MemoryNukeUnavailable('selective handoff outcome unavailable')
                    claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=provider,operation_id=claim.operation_id)
                    with locked(os.fspath(self.lock_target)):
                        operations=self._load_journal();row=operations.get(claim.operation_id)
                        checkpoint=dict(published_first_claim)
                        checkpoint.update({'reset_selective_manifest':manifest,
                            'reset_selective_manifest_sha256':digest,'reset_selective_staging':staging,
                            'reset_selective_staging_sha256':staging_hash,'reset_native_frozen':frozen,
                            'reset_native_request':frozen['request']})
                        if row != checkpoint or row.get('reset_native_started') is True:
                            raise MemoryNukeUnavailable('selective pre-begin checkpoint changed')
                        operations[claim.operation_id]=dict(published_first_claim)
                        self._save_journal(operations)
                        return True
                await barrier.publish(claim.owner,rewind_pre_begin)
                await withdraw_untouched_claim()
                raise MemoryNukeUnavailable('selective original write deadline unavailable')
            begin_attempted = True
            token = provider._begin_original_reset_handoff(claim,frozen,barrier=barrier,witness=witness)
            try:
                with locked(os.fspath(self.lock_target)):
                    operations=self._load_journal();row=operations.get(claim.operation_id)
                    if (type(row) is not dict or row.get('reset_native_started') is True
                        or row.get('reset_native_frozen') != frozen or row.get('reset_selective_manifest') != manifest
                        or row.get('reset_selective_manifest_sha256') != digest):
                        raise MemoryNukeUnavailable('selective original request changed')
                    row['reset_native_started']=True;self._save_journal(operations)
                await provider._host_candidate_request(target.memory_owner,target.memory_workspace,
                    {'phase':'guarded_reset_commit',**frozen['request']},require_live=True,
                    write_deadline=write_deadline,_reset_handoff_token=token)
            except Exception as issue_error:
                try:
                    fact = await provider._abort_original_reset_handoff(token)
                except Exception:
                    # Issued or unresolved work is never withdrawn or replayed.
                    pass
                else:
                    def withdraw_unsent():
                        provider._assert_original_reset_unsent(fact,claim)
                        with locked(os.fspath(self.lock_target)):
                            operations=self._load_journal();row=operations.get(claim.operation_id)
                            expected_row=dict(published_first_claim)
                            expected_row.update({'reset_selective_manifest':manifest,
                                'reset_selective_manifest_sha256':digest,'reset_selective_staging':staging,
                            'reset_selective_staging_sha256':staging_hash,'reset_native_frozen':frozen,
                                'reset_native_request':frozen['request']})
                            if type(row) is dict and row.get('reset_native_started') is True:
                                expected_row['reset_native_started']=True
                            if row != expected_row:
                                raise MemoryNukeUnavailable('selective unsent checkpoint changed')
                            operations[claim.operation_id]=dict(untouched_preview)
                            self._save_journal(operations)
                            return True
                    await barrier.publish(claim.owner,withdraw_unsent)
                    raise MemoryNukeUnavailable('selective reset was not issued; preview may be retried') from issue_error
                # A matching original receipt is the only settlement for an unknown issue.
            return await self._settle_selective_ingest_handoff(token=token,provider=provider,
                barrier=barrier,witness=witness)
        task = asyncio.create_task(run())
        cancelled = False
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: cancelled = True
        if cancelled:
            try: task.result()
            except BaseException: pass
            raise asyncio.CancelledError
        return task.result()

    async def commit(
        self,
        *,
        owner: str,
        provider_owner: str,
        operation_id: str,
        preview_token: str,
        confirmation: str,
        agent_supervisor=None,
        reset_barrier=None,
        reset_state=None,
        reset_auth=None,
    ) -> dict[str, Any]:
        """Keep the original admission alive through selective completion."""
        arguments = dict(owner=owner,provider_owner=provider_owner,operation_id=operation_id,
            preview_token=preview_token,confirmation=confirmation,agent_supervisor=agent_supervisor,
            reset_barrier=reset_barrier,reset_state=reset_state,reset_auth=reset_auth)
        selective = False
        if all(isinstance(value,str) and value for value in (operation_id,preview_token,confirmation)):
            with locked(os.fspath(self.lock_target)):
                original = self._validated_record(operations=self._load_journal(),owner=owner,
                    provider_owner=provider_owner,operation_id=operation_id,preview_token=preview_token,
                    confirmation=confirmation)
                participants = original.get('participants') or {}
                fm = participants.get('fm') or {}
                expected = fm.get('expected') if type(fm.get('expected')) is dict else original.get('expected',{})
                ingest = expected.get('ingest') if type(expected) is dict else None
                selective = (original.get('components') == ['ingest'] and type(ingest) is dict
                    and 'selective' in ingest)
        if not selective:
            return await self._commit_owned(**arguments)
        task = asyncio.create_task(self._commit_owned(**arguments))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            try: task.result()
            except BaseException: pass
            raise asyncio.CancelledError
        return task.result()

    async def _commit_owned(
        self,
        *,
        owner: str,
        provider_owner: str,
        operation_id: str,
        preview_token: str,
        confirmation: str,
        agent_supervisor=None,
        reset_barrier=None,
        reset_state=None,
        reset_auth=None,
    ) -> dict[str, Any]:
        if not all(
            isinstance(value, str) and value
            for value in (operation_id, preview_token, confirmation)
        ):
            raise MemoryNukeError("operation_id, preview_token, and confirmation are required")
        if len(self._commit_locks) > 64:
            # Bound growth: entries whose commit already finished are safe to
            # drop; an in-flight commit holds its lock, and between this check
            # and setdefault there is no await, so no acquirer can interleave.
            self._commit_locks = {
                key: held
                for key, held in self._commit_locks.items()
                if held.locked() or getattr(held,'_terminal_s08_witness',None) is not None
            }
        lock = self._commit_locks.setdefault(operation_id, asyncio.Lock())
        async with lock:
            source_provider = self.canonical_memory_provider or self.memory_provider
            guarded = bool(getattr(source_provider, "_guarded_v17", False))
            if guarded and (reset_barrier is None or reset_state is None or reset_auth is None):
                raise MemoryNukeUnavailable("guarded reset authority is unavailable")
            account_id = reset_auth.account_id(owner) if guarded else None
            if guarded and (not account_id or reset_auth.username_for_account_id(account_id) != owner):
                raise MemoryNukeUnavailable("reset account is unavailable")
            self._reset_auth = reset_auth if guarded else None
            self._reset_state = reset_state if guarded else None
            source_identity = (await asyncio.to_thread(source_provider.persisted_database_identity)
                if source_provider is not self.memory_provider else None)

            def retained_reset_finalizer():
                try:
                    pool=agent_supervisor.owner_lifecycle_pool()
                    matches=[lease for lease in tuple(pool._queued_retirement_leases.values())
                        if getattr(lease,'owner',None)==owner and getattr(lease,'account_id',None)==account_id
                        and getattr(getattr(lease,'claim',None),'_coordinator',None) is self
                        and getattr(getattr(lease,'claim',None),'operation_id',None)==operation_id]
                    return (len(matches)==1 and callable(getattr(matches[0],'reset_finalizer',None))
                        and getattr(matches[0],'reset_transport_witness',None) is source_provider._reset_retirement_witness)
                except Exception:return False

            untouched_preview = None
            published_first_claim = None
            def publish_claim():
              nonlocal untouched_preview, published_first_claim
              with locked(os.fspath(self.lock_target)):
                operations = self._load_journal()
                record = self._validated_record(
                    operations=operations,
                    owner=owner,
                    provider_owner=provider_owner,
                    operation_id=operation_id,
                    preview_token=preview_token,
                    confirmation=confirmation,
                )
                if (record.get('selective_account_id') is not None
                    and (not guarded or record['selective_account_id'] != account_id)):
                    raise MemoryNukeConflict('selective preview account changed')
                if (record.get('full_account_id') is not None
                    and (not guarded or record['full_account_id'] != account_id)):
                    raise MemoryNukeConflict('full reset preview account changed')
                if source_provider is not self.memory_provider and any(key in _DB_COMPONENTS for key in record.get('expected', {})):
                    participants = record.get('participants')
                    if (type(participants) is not dict or participants.get('version') != 1
                            or type(participants.get('native')) is not dict or type(participants.get('fm')) is not dict
                            or source_identity['database_id'] != participants['fm'].get('database_id')):
                        raise MemoryNukeUnavailable('retained reset participant manifest unavailable')
                    frozen = participants['native'].get('snapshot')
                    if type(frozen) is not dict:
                        raise MemoryNukeUnavailable('native reset participant manifest unavailable')
                if guarded and any(key != operation_id and isinstance(row, dict)
                        and row.get("owner") == owner and row.get("state") in {"committing", "partial"}
                        and row.get("result") is None for key, row in operations.items()):
                    raise MemoryNukeConflict("another memory reset is active")
                if isinstance(record.get("result"), dict):
                    return record
                # A fresh claim must not fence a review whose original outcome
                # still needs the human's current-read reconciliation.
                if (guarded and record.get('state') == 'preview'
                    and (not callable(getattr(source_provider,'_host_import_reset_available',None))
                         or source_provider._host_import_reset_available() is not True)):
                    raise MemoryNukeUnavailable('import review original custody unavailable')
                # Selective reset is supported only by the existing primary
                # guarded owner and its original zero-queue custody path.
                expected = record.get('expected', {})
                participants = record.get('participants') or {}
                fm = participants.get('fm') or {}
                fm_expected = fm.get('expected') or {}
                effective = set(expected) | set(fm.get('components') or [])
                if ('memories' not in effective and any(
                    isinstance(value, dict) and 'selective' in value
                    for value in (expected.get('ingest'), fm_expected.get('ingest'))
                )):
                    from src.memery_provider import MemeryProvider
                    queued = record.get('queued_expected')
                    original = (record.get('state') == 'preview' or
                        (record.get('state') in {'committing', 'partial'}
                         and record.get('reset_native_started') is True
                         and type(record.get('reset_native_frozen')) is dict))
                    if (not guarded or source_provider is not self.memory_provider
                        or not isinstance(source_provider, MemeryProvider)
                        or record.get('components') != ['ingest']
                        or record.get('participants') is not None
                        or type(expected) is not dict or set(expected) != {'ingest'}
                        or type(expected.get('ingest')) is not dict
                        or 'selective' not in expected['ingest']
                        or record.get('selective_account_id') != account_id
                        or record.get('queued_account_id') != account_id
                        or type(queued) is not dict or type(queued.get('count')) is not int
                        or queued['count'] != 0
                        or type(record.get('staging_expected')) is not dict
                        or type(record.get('snapshot_import_claims')) is not list
                        or not original):
                        raise MemoryNukeUnavailable('selective ingest reset is unavailable')
                # Only the exact original preview shape establishes that no
                # previous invocation has started work. Unknown fields fail closed.
                preview_keys = {'version','operation_id','owner','provider_owner','components','expected',
                    'queued_expected','queued_account_id','queued_profile','participants','selective_account_id','full_account_id',
                    'agent_memory_expected','legacy_memory_expected','media_expected','staging_expected','snapshot_import_claims',
                    'retained','expires_at_epoch','binding_digest','preview_token_sha256',
                    'confirmation','state','created_at','updated_at','result'}
                if (guarded and record.get('state') == 'preview' and record.get('result') is None
                    and set(record).issubset(preview_keys)):
                    untouched_preview = json.loads(json.dumps(record))
                queued=record.get('queued_expected')
                if (guarded and 'memories' in record.get('components',[]) and type(queued) is dict
                    and type(queued.get('count')) is int and queued['count']==0):
                    mode=record.get('reset_s08_mode')
                    if mode is None:
                        if record.get('state')!='preview': raise MemoryNukeUnavailable('reset S08 authority unavailable')
                        ready=getattr(source_provider,'_terminal_s08_ready_local',None)
                        mode='terminal-s08-v1' if callable(ready) and ready() else 'no-s08-v1'
                        record['reset_s08_mode']=mode
                        if mode=='terminal-s08-v1': lock._terminal_s08_witness=source_provider._terminal_s08_witness
                        else: lock._terminal_s08_witness=None
                    elif mode=='terminal-s08-v1':
                        if (getattr(lock,'_terminal_s08_witness',None) is None
                            or getattr(lock,'_terminal_s08_witness',None) is not source_provider._terminal_s08_witness
                            or not source_provider._terminal_s08_ready_local()):
                            raise MemoryNukeUnavailable('reset S08 live capability changed')
                    elif mode!='no-s08-v1': raise MemoryNukeUnavailable('reset S08 authority unavailable')
                if (guarded and type(queued) is dict and type(queued.get('count')) is int and queued['count']>0
                    and not record.get('reset_native_started')
                    and ((not callable(getattr(reset_state,'queued_reset_finalize',None)) and not retained_reset_finalizer())
                         or not callable(getattr(source_provider,'_reset_retirement_ready_local',None))
                         or not source_provider._reset_retirement_ready_local())):
                    raise MemoryNukeUnavailable('queued reset live capability unavailable')
                prior_claim = record.get("commit_claim")
                if record.get("state") == "committing" and isinstance(
                    prior_claim, dict
                ):
                    claim_id = str(prior_claim.get("coordinator_id") or "")
                    claim_host = str(prior_claim.get("host") or "")
                    claim_pid = prior_claim.get("pid")
                    same_coordinator = claim_id == self._commit_claim_id
                    foreign_host = bool(claim_host and claim_host != socket.gethostname())
                    if not same_coordinator and (
                        foreign_host or _process_is_running(claim_pid)
                    ):
                        raise MemoryNukeConflict(
                            "memory reset commit is already in progress"
                        )
                record["state"] = "committing"
                if guarded:
                    if record.get("account_id") not in (None, account_id):
                        raise MemoryNukeConflict("reset account changed")
                    record["account_id"] = account_id
                    record["guarded_reset"] = True
                record["commit_claim"] = {
                    "coordinator_id": self._commit_claim_id,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                }
                record["updated_at"] = self.clock()
                operations[operation_id] = record
                self._save_journal(operations)
                if untouched_preview is not None:
                    published_first_claim = json.loads(json.dumps(record))
                return record

            if guarded:
                # The terminal-S08 witness is a per-process liveness token (re-obtainable); store identity is
                # re-proven further down the resume path. Refresh it here because publish_claim is synchronous.
                with locked(os.fspath(self.lock_target)):
                    resumed = self._load_journal().get(operation_id)
                if (type(resumed) is dict and resumed.get('reset_s08_mode') == 'terminal-s08-v1'
                        and resumed.get('state') in {'committing', 'partial'} and resumed.get('result') is None
                        and (getattr(lock, '_terminal_s08_witness', None) is None
                             or lock._terminal_s08_witness is not source_provider._terminal_s08_witness
                             or not source_provider._terminal_s08_ready_local())):
                    refresh = getattr(reset_state, 'refresh_reset_capability_for_owner', None)
                    if callable(refresh):
                        await refresh(state=reset_state, owner=owner, account_id=account_id,
                            provider=source_provider, terminal_s08=True)
                    if source_provider._terminal_s08_ready_local():
                        lock._terminal_s08_witness = source_provider._terminal_s08_witness
            record = await reset_barrier.publish(owner, publish_claim) if guarded else publish_claim()
            if isinstance(record.get("result"), dict):
                if record.get('queued_finalization_intent') is not None:
                    try:
                        return await agent_supervisor.owner_lifecycle_pool().cleanup_completed_queued_reset(
                            coordinator=self,owner=owner,account_id=account_id,operation_id=operation_id,
                            record=record,barrier=reset_barrier)
                    except Exception as exc:
                        raise MemoryNukeUnavailable('queued reset cleanup requires reconciliation') from exc
                if (guarded and record.get('reset_selective_native_complete') is True
                    and type(record.get('reset_native_frozen')) is dict):
                    finished_claim=MemoryResetClaim(self,owner,account_id,operation_id,_RESET_CLAIM_SEAL)
                    source_provider._ack_finished_original_reset_handoff(finished_claim,
                        record['reset_native_frozen'],record['result'])
                if getattr(lock,'_terminal_s08_witness',None) is not None: lock._terminal_s08_witness=None
                return dict(record["result"])
            selective_fm = ((record.get('participants') or {}).get('fm') or {}).get('expected') or {}
            selective_root = record.get('expected') or {}
            selective_original = any(type(value) is dict and 'selective' in value for value in
                (selective_root.get('ingest'),selective_fm.get('ingest')))
            selective_result = None
            original_reset_unsent = None
            reset_claim = None
            queued_proof = None
            stopped_closure = None
            source_closure = None
            resume_native = False
            def invocation_deferred(lease):
                if (lease is None or getattr(lease,'reset_invocation_remaining',None)!=0
                    or getattr(lease,'pending',None) is not None): return False
                with locked(os.fspath(self.lock_target)):
                    current=self._load_journal().get(operation_id)
                    if type(current) is not dict: return False
                    maps=(current.get('reset_source_receipts'),current.get('reset_queued_receipts'))
                    return all(type(items) is dict and not any(type(v) is dict and v.get('state')=='started'
                        for v in items.values()) for items in maps)
            if guarded:
                reset_claim = MemoryResetClaim(self, owner, account_id, operation_id, _RESET_CLAIM_SEAL,
                    getattr(lock,'_terminal_s08_witness',None))
                async def withdraw_untouched_claim():
                    if untouched_preview is None or published_first_claim is None:
                        return
                    def withdraw():
                        with locked(os.fspath(self.lock_target)):
                            operations = self._load_journal()
                            if operations.get(operation_id) != published_first_claim:
                                return False
                            operations[operation_id] = untouched_preview
                            self._save_journal(operations)
                            return True
                    if await reset_barrier.publish(owner, withdraw):
                        lock._terminal_s08_witness = None
                try:
                    await asyncio.wait_for(reset_barrier.drain(owner), timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    # The barrier callback is synchronous under its admission
                    # lock. Keep its task alive through repeated cancellation.
                    withdrawal = asyncio.create_task(withdraw_untouched_claim())
                    while not withdrawal.done():
                        try:
                            await asyncio.shield(withdrawal)
                        except asyncio.CancelledError:
                            continue
                    try:
                        withdrawal.result()
                    except Exception:
                        logger.exception('untouched reset claim withdrawal unavailable')
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise MemoryNukeUnavailable('owner work has not settled; retry this reset') from exc
                witness = await reset_barrier.reset_drain_witness(owner)
                async def drain_original_reset(work):
                    task=asyncio.create_task(work)
                    cancelled=False
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: cancelled=True
                    if cancelled:
                        try: task.result()
                        except BaseException: pass
                        raise asyncio.CancelledError
                    return task.result()
                async def withdraw_nonselective_unsent(provider, frozen):
                    if untouched_preview is None or published_first_claim is None:
                        return False
                    try:
                        fact=provider._seal_nonselective_reset_unsent(claim=reset_claim,frozen=frozen)
                    except Exception:
                        return False
                    expected_row=dict(published_first_claim)
                    expected_row.update({'reset_native_started':True,'reset_native_frozen':frozen,
                        'reset_native_request':frozen['request']})
                    with locked(os.fspath(self.lock_target)):
                        if self._load_journal().get(operation_id)!=expected_row:
                            return False
                    def withdraw():
                        provider._assert_nonselective_reset_unsent(fact,reset_claim,frozen)
                        with locked(os.fspath(self.lock_target)):
                            operations=self._load_journal();row=operations.get(operation_id)
                            if row!=expected_row:
                                return False
                            operations[operation_id]=dict(untouched_preview)
                            self._save_journal(operations)
                        provider._retire_nonselective_reset_unsent(fact,reset_claim,untouched_preview)
                        return True
                    return await reset_barrier.publish(owner,withdraw)
                async def adopt_original_reset_stamp(provider, frozen):
                    from src.openclank.fm_reset_receipt import read_committed_reset_receipt
                    from src.memery_external_sources import _queued_owned_thread
                    def current():
                        try:
                            return (reset_barrier.reset_witness_current(witness) is True
                                and reset_claim.assert_current_post_dispatch(provider=provider,frozen=frozen) is True)
                        except Exception:
                            return False
                    async def read_and_adopt():
                        receipt=await _queued_owned_thread(read_committed_reset_receipt,provider._guarded_preparation,frozen,current=current)
                        if receipt.get('state')!='committed': raise MemoryNukeUnavailable('reset original receipt unavailable')
                        await provider._host_guarded_reset_stamp(claim=reset_claim,frozen=frozen,receipt=receipt,current=current)
                        return receipt
                    task=asyncio.create_task(read_and_adopt())
                    cancelled=False
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: cancelled=True
                    if cancelled:
                        try: task.result()
                        except BaseException: pass
                        raise asyncio.CancelledError
                    return task.result()
                original_reset_receipt=None
                original_reset_unsent=None
                if record.get('reset_native_started') is True and not selective_original:
                    original=record.get('reset_native_frozen')
                    if type(original) is not dict: raise MemoryNukeUnavailable('reset original request unavailable')
                    saved=record.get('reset_native_result')
                    if saved is None:
                        try:
                            original_reset_unsent=source_provider._seal_nonselective_reset_unsent(claim=reset_claim,frozen=original)
                        except Exception:
                            # Absence of a positive fact grants nothing: read original status only.
                            original_reset_receipt=await adopt_original_reset_stamp(source_provider,original)
                    else:
                        from src.openclank.fm_reset_receipt import read_committed_reset_receipt
                        from src.memery_external_sources import _queued_owned_thread
                        def original_current():
                            try:
                                return (reset_barrier.reset_witness_current(witness) is True
                                    and reset_claim.assert_current_post_dispatch(provider=source_provider,frozen=original) is True)
                            except Exception: return False
                        original_reset_receipt=await _queued_owned_thread(read_committed_reset_receipt,source_provider._guarded_preparation,original,current=original_current)
                        await _queued_owned_thread(source_provider._guarded_preparation.assert_valid)
                        if not original_current() or original_reset_receipt.get('sql_result')!=saved:
                            raise MemoryNukeUnavailable('reset saved original changed')
                if (not callable(getattr(source_provider,'_host_import_reset_available',None))
                    or source_provider._host_import_reset_available() is not True):
                    # Only the exact untouched original preview may be restored;
                    # prior started/partial operations remain fenced.
                    await withdraw_untouched_claim()
                    raise MemoryNukeUnavailable('import review original custody unavailable')
                if selective_original:
                    # First profile is the actual primary guarded FM owner only.
                    # Dual participants need an independent selective closure.
                    from src.memery_provider import MemeryProvider
                    from src.openclank.memory_job_admission import TargetAuthority
                    if (record.get('components') != ['ingest'] or record.get('participants') is not None
                        or source_provider is not self.memory_provider or not isinstance(source_provider,MemeryProvider)
                        or record.get('selective_account_id') != account_id
                        or type(record.get('queued_expected')) is not dict
                        or record['queued_expected'].get('count') != 0
                        or type(record.get('staging_expected')) is not dict
                        or type(record.get('snapshot_import_claims')) is not list):
                        raise MemoryNukeUnavailable('selective reset participant unavailable')
                    witness = await reset_barrier.reset_drain_witness(owner)
                    if record.get('reset_native_started') is True:
                        frozen=record.get('reset_native_frozen')
                        if type(frozen) is not dict:
                            raise MemoryNukeUnavailable('selective original request unavailable')
                        try:
                            finished=source_provider._finished_original_reset_handoff(reset_claim,frozen,
                                barrier=reset_barrier,witness=witness)
                        except Exception:
                            token=source_provider._resume_original_reset_handoff(reset_claim,frozen,
                                barrier=reset_barrier,witness=witness)
                            selective_result=await self._settle_selective_ingest_handoff(token=token,
                                provider=source_provider,barrier=reset_barrier,witness=witness)
                        else:
                            receipt=finished.get('receipt') if type(finished) is dict and finished.get('state')=='finished' else None
                            step=(record.get('reset_local_steps') or {}).get('staging')
                            if (type(receipt) is not dict or receipt.get('state')!='committed'
                                or type(receipt.get('sql_result')) is not dict
                                or record.get('reset_native_result') != receipt['sql_result']
                                or record.get('reset_selective_native_complete') is not True
                                or type(step) is not dict or set(step)!={'state','result'}
                                or step.get('state')!='complete' or type(step.get('result')) is not dict
                                or step['result'].get('complete') is not True):
                                raise MemoryNukeUnavailable('finished selective reset checkpoint unavailable')
                            selective_result=receipt['sql_result']
                    else:
                        authority = TargetAuthority(auth_manager=reset_auth,supervisor=agent_supervisor.owner_lifecycle_pool())
                        selective_result = await self._selective_ingest_original(claim=reset_claim,provider=source_provider,
                            target_authority=authority,barrier=reset_barrier,witness=witness,
                            write_deadline=float(asyncio.get_running_loop().time()+30.0),untouched_preview=untouched_preview,
                            published_first_claim=published_first_claim,withdraw_untouched_claim=withdraw_untouched_claim)
                    with locked(os.fspath(self.lock_target)):
                        record=dict(self._load_journal()[operation_id])
                else:
                    if record.get('queued_finalization_intent') is not None:
                        try:
                            from src.openclank.mimo_supervisor import _queued_owned_thread
                            await _queued_owned_thread(source_provider._assert_reset_retirement_ready)
                            inventory=None
                            if type(record.get('reset_native_frozen')) is not dict:
                                from src.openclank.conversation_archive import get_conversation_archive
                                inventory=await asyncio.to_thread(get_conversation_archive().queued_reset_producer_inventory,
                                    claim=reset_claim,provider=source_provider,owner=owner,account_id=account_id,
                                    operation_id=operation_id,expected=record.get('queued_expected'))
                            return await agent_supervisor.owner_lifecycle_pool().resume_queued_reset_finalization(
                                claim=reset_claim,inventory=inventory,record=record)
                        except Exception as exc:
                            raise MemoryNukeUnavailable('queued reset finalization requires reconciliation') from exc
                    if record.get('reset_native_started') is True:
                        frozen=record.get('reset_native_frozen')
                        if type(frozen) is not dict:
                            raise MemoryNukeUnavailable('reset native request proof unavailable')
                        queued_saved=record.get('queued_expected')
                        if queued_saved is None and not {'memories','ingest'}.intersection(frozen['request'].get('components', [])):
                            queued_saved={'count':0}  # This original does not select queued-source cleanup.
                        if (type(queued_saved) is not dict or type(queued_saved.get('count')) is not int
                            or queued_saved['count']<0):
                            raise MemoryNukeUnavailable('queued reset preview unavailable')
                        if queued_saved['count']:
                            try:
                                pool=agent_supervisor.owner_lifecycle_pool()
                                stopped_closure=await pool.refresh_post_dispatch_queued_reset(claim=reset_claim,frozen=frozen)
                            except Exception as exc:
                                raise MemoryNukeUnavailable('reset native outcome requires same-process reconciliation') from exc
                        resume_native=True
                    queued_expected = record.get('queued_expected')
                    guarded_components = ((record.get('participants') or {}).get('fm') or {}).get('components', [])
                    if not resume_native and {'memories', 'ingest'}.intersection(set(record.get('expected', {})) | set(guarded_components)):
                        if (type(queued_expected) is not dict
                            or type(queued_expected.get('count')) is not int
                            or queued_expected['count'] < 0):
                            raise MemoryNukeUnavailable('queued native runtime retirement is unavailable')
                        if queued_expected['count']:
                            from src.openclank.mimo_supervisor import _queued_owned_thread
                            await _queued_owned_thread(source_provider._assert_reset_retirement_ready)
                            # The stopped-holder lease must also be closed under
                            # the still-published reset fence before final success.
                            # Do not start source mutation without that owner seam.
                            from src.openclank.conversation_archive import get_conversation_archive
                            from src.openclank.memory_job_admission import TargetAuthority
                            archive=get_conversation_archive(); pool=agent_supervisor.owner_lifecycle_pool()
                            profile=getattr(reset_state,'s05_profile',None)
                            lease=None
                            try:
                                inventory=await asyncio.to_thread(archive.queued_reset_producer_inventory,claim=reset_claim,
                                    provider=source_provider,owner=owner,account_id=account_id,operation_id=operation_id,expected=queued_expected)
                                matches=[item for item in tuple(pool._queued_retirement_leases.values())
                                    if getattr(item,'owner',None)==owner and getattr(getattr(item,'claim',None),'operation_id',None)==operation_id]
                                if len(matches)>1: raise ValueError('queued retirement ambiguous')
                                if matches and not callable(getattr(matches[0],'reset_finalizer',None)):
                                    raise ValueError('queued reset finalization unavailable')
                                if not matches and not callable(getattr(reset_state,'queued_reset_finalize',None)):
                                    raise ValueError('queued reset finalization unavailable')
                                if matches and getattr(matches[0],'closure',None) is not None:
                                    stopped_closure=await pool.resume_stopped_queued_reset(claim=reset_claim,inventory=inventory)
                                    if getattr(stopped_closure.lease,'reset_transport_witness',None) is not source_provider._reset_retirement_witness:
                                        raise ValueError('queued reset live child changed')
                                    if type(record.get('reset_source_manifest')) is not dict:
                                        raise MemoryNukeUnavailable('reset source manifest unavailable')
                                    stopped_closure.lease.reset_invocation_remaining=256
                                else:
                                    if matches:
                                        lease=await pool.resume_queued_reset_lease(claim=reset_claim,inventory=inventory)
                                        if getattr(lease,'reset_transport_witness',None) is not source_provider._reset_retirement_witness:
                                            raise ValueError('queued reset live child changed')
                                    else:
                                        authority=TargetAuthority(auth_manager=reset_auth,supervisor=pool)
                                        lease=await pool.issue_queued_reset_lease(claim=reset_claim,inventory=inventory,
                                            target_authority=authority,profile=profile,barrier=reset_barrier)
                                        if not source_provider._reset_retirement_ready_local():
                                            raise ValueError('queued reset live child unavailable')
                                        lease.reset_transport_witness=source_provider._reset_retirement_witness
                                        lease.reset_finalizer=reset_state.queued_reset_finalize
                                    from src.openclank.chat_source_bridge import queued_reset_source_manifest
                                    manifest=await queued_reset_source_manifest(state=reset_state,claim=lease.claim,
                                        lease=lease,inventory=lease.inventory)
                                    with locked(os.fspath(self.lock_target)):
                                        operations=self._load_journal();current=operations[operation_id]
                                        old=current.get('reset_source_manifest')
                                        if old is None:
                                            if (lease.touched or current.get('queued_holder_progress') or current.get('queued_holder_retirement')
                                                or current.get('reset_origin_cursor') is not None or current.get('reset_origin_count',0)
                                                or current.get('reset_native_started') or current.get('reset_selected_started')):
                                                raise MemoryNukeUnavailable('prior reset source work requires reconciliation')
                                            current['reset_source_manifest']=manifest
                                            current['reset_source_receipts']={}
                                            current['reset_queued_receipts']={}
                                            current['reset_fence_attempts']=0
                                            self._save_journal(operations)
                                        elif (old!=manifest or type(current.get('reset_source_receipts')) is not dict
                                              or type(current.get('reset_queued_receipts')) is not dict):
                                            raise MemoryNukeUnavailable('reset source manifest changed')
                                    lease.reset_invocation_remaining=256
                                    retire=getattr(reset_state,'queued_reset_retirement',None)
                                    if not callable(retire): raise ValueError('queued retirement unavailable')
                                    stopped_closure=await retire(lease)
                                await pool.validate_queued_reset_closure(stopped_closure,claim=reset_claim)
                            except BaseException as exc:
                                if lease is not None and not lease.touched:
                                    release=asyncio.create_task(pool.release_queued_reset_lease(lease))
                                    while not release.done():
                                        try: await asyncio.shield(release)
                                        except asyncio.CancelledError: continue
                                    release.result()
                                if isinstance(exc,asyncio.CancelledError): raise
                                if invocation_deferred(lease):
                                    raise MemoryNukeUnavailable('reset work deferred; retry this same operation') from exc
                                raise MemoryNukeUnavailable('queued holder retirement requires reconciliation') from exc
                            with locked(os.fspath(self.lock_target)):
                                record=dict(self._load_journal()[operation_id])
                    if (not resume_native and type(queued_expected) is dict and queued_expected.get('count')==0
                            and {'memories','ingest'}.intersection(set(record.get('expected', {})) | set(guarded_components))):
                        # A zero-queue source closure uses the same durable manifest and
                        # contributor progress, but never represents a retired worker.
                        from src.openclank.mimo_supervisor import _queued_owned_thread
                        from src.openclank.conversation_archive import get_conversation_archive
                        from src.openclank.memory_job_admission import TargetAuthority, _conn
                        archive=get_conversation_archive();pool=agent_supervisor.owner_lifecycle_pool()
                        profile=getattr(reset_state,'s05_profile',None)
                        saved_manifest=record.get('reset_source_manifest')
                        if saved_manifest is not None and (type(saved_manifest) is not dict or saved_manifest.get('version')!=2 or saved_manifest.get('kind')!='zero_queue'):
                            raise MemoryNukeUnavailable('zero queue source manifest unavailable')
                        def has_pair():
                            reset_claim.assert_current(owner=owner,account_id=account_id,provider=source_provider,operation_id=operation_id)
                            prepared=source_provider._guarded_preparation
                            if prepared is None: raise ValueError('guarded source profile unavailable')
                            prepared.assert_valid()
                            with _conn(prepared.database_path) as db:
                                deadline=time.monotonic()+5.0
                                db.set_progress_handler(lambda:int(time.monotonic()>=deadline),1000)
                                db.execute('BEGIN')
                                # Probe the current owner's jobs independently: a missing or malformed ref is not absence.
                                orphan=db.execute("SELECT 1 FROM fm_v2_jobs WHERE owner_id=? AND kind='assistant_interpretation_review' LIMIT 1",(provider_owner,)).fetchone()
                                refs=db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='fm_v2_s05_job_refs'").fetchone()
                                if refs is None:
                                    first=second=None
                                else:
                                    query="SELECT 1 FROM fm_v2_s05_job_refs r INDEXED BY s04_s05_source_account LEFT JOIN fm_v2_jobs j USING(owner_id,job_id) WHERE r.source_account_id=? AND (j.kind='assistant_interpretation_review' OR j.kind IS NULL) LIMIT 1"
                                    first=db.execute(query,(account_id,)).fetchone()
                                    query="SELECT 1 FROM fm_v2_s05_job_refs r INDEXED BY s04_s05_ref_account LEFT JOIN fm_v2_jobs j USING(owner_id,job_id) WHERE json_valid(r.source_ref_json) AND json_extract(r.source_ref_json,'$.account_id')=? AND (j.kind='assistant_interpretation_review' OR j.kind IS NULL) LIMIT 1"
                                    second=db.execute(query,(account_id,)).fetchone()
                            prepared.assert_valid()
                            reset_claim.assert_current(owner=owner,account_id=account_id,provider=source_provider,operation_id=operation_id)
                            return bool(orphan or first or second)
                        if saved_manifest is not None or await _queued_owned_thread(has_pair):
                            try:
                                await _queued_owned_thread(source_provider._assert_reset_retirement_ready)
                                inventory=await _queued_owned_thread(archive.queued_reset_producer_inventory,claim=reset_claim,
                                    provider=source_provider,owner=owner,account_id=account_id,operation_id=operation_id,expected=queued_expected)
                                authority=TargetAuthority(auth_manager=reset_auth,supervisor=pool)
                                lease=await pool.issue_zero_queue_source_lease(claim=reset_claim,inventory=inventory,
                                    target_authority=authority,profile=profile,barrier=reset_barrier,witness=witness)
                                if saved_manifest is None:
                                    from src.openclank.chat_source_bridge import queued_reset_source_manifest
                                    manifest=await queued_reset_source_manifest(state=reset_state,claim=reset_claim,lease=lease,inventory=inventory)
                                    with locked(os.fspath(self.lock_target)):
                                        operations=self._load_journal();current=operations[operation_id]
                                        if (current.get('reset_source_manifest') is not None or current.get('reset_origin_cursor') is not None
                                                or current.get('reset_origin_count',0) or current.get('reset_native_started') or current.get('reset_selected_started')):
                                            raise MemoryNukeUnavailable('prior source work requires reconciliation')
                                        current['reset_source_manifest']=manifest;current['reset_source_receipts']={}
                                        current['reset_queued_receipts']={};current['reset_fence_attempts']=0
                                        self._save_journal(operations)
                                source_closure=await pool.close_zero_queue_source_lease(lease,claim=reset_claim)
                                await pool.validate_queued_reset_closure(source_closure,claim=reset_claim)
                            except Exception as exc:
                                raise MemoryNukeUnavailable('zero queue source closure unavailable') from exc
                            with locked(os.fspath(self.lock_target)):
                                record=dict(self._load_journal()[operation_id])
                    if not resume_native and source_provider is not self.memory_provider and record.get('participants') is not None:
                        frozen = record['participants']['native']['snapshot']
                        current_native = await asyncio.to_thread(self.memory_provider.lifecycle_snapshot, provider_owner)
                        if (any(current_native.get(key) != frozen.get(key) for key in ('version','kind','root','device','inode','owner'))
                                or ('memories' in record.get('expected', {}) and not record.get('reset_selected_started') and current_native != frozen)):
                            raise MemoryNukeUnavailable('native reset participant changed')
                    fm_components = ((record.get('participants') or {}).get('fm') or {}).get('components', [])
                    if not resume_native and {"memories", "ingest"}.intersection(set(record.get("expected", {})) | set(fm_components)):
                        from src.openclank.chat_source_bridge import canonical_source_provider
                        if canonical_source_provider(reset_state) is not source_provider:
                            raise MemoryNukeUnavailable("reset source provider changed")
                        from src.openclank.chat_source_bridge import retire_reset_origin_page
                        cursor = record.get("reset_origin_cursor")
                        if (cursor is not None and (not isinstance(cursor, list) or len(cursor) != 2
                                or any(not isinstance(value, str) or not value or value != value.strip() or len(value) > 512 for value in cursor))):
                            raise MemoryNukeUnavailable("reset origin checkpoint unavailable")
                        after = tuple(cursor) if cursor is not None else None
                        count = record.get("reset_origin_count", 0)
                        if type(count) is not int or count < 0 or type(record.get("reset_origins_complete", False)) is not bool:
                            raise MemoryNukeUnavailable("reset origin checkpoint unavailable")
                        if not record.get("reset_origins_complete"):
                            while True:
                                try:
                                    page = await retire_reset_origin_page(state=reset_state, claim=reset_claim,
                                        owner=owner, account_id=account_id, operation_id=operation_id, after=after, reset_closure=(source_closure or stopped_closure))
                                except Exception as exc:
                                    if (source_closure or stopped_closure) is not None and invocation_deferred((source_closure or stopped_closure).lease):
                                        raise MemoryNukeUnavailable('reset work deferred; retry this same operation') from exc
                                    raise
                                if (page.owner != owner or page.account_id != account_id or page.operation_id != operation_id
                                        or page.after != after or len(page.items) > 128 or (page.next_after is not None
                                        and (not page.items or page.next_after <= (after or ("", ""))
                                             or page.next_after != page.items[-1][:2]))):
                                    raise MemoryNukeUnavailable("reset origin checkpoint changed")
                                await asyncio.to_thread(reset_claim.assert_current, owner=owner, account_id=account_id,
                                    provider=source_provider, operation_id=operation_id)
                                with locked(os.fspath(self.lock_target)):
                                    operations = self._load_journal()
                                    current = operations[operation_id]
                                    if current.get("reset_origin_cursor") != (list(after) if after else None):
                                        raise MemoryNukeUnavailable("reset origin checkpoint changed")
                                    current["reset_origin_cursor"] = list(page.next_after) if page.next_after else None
                                    current["reset_origin_count"] = count + len(page.items)
                                    current["reset_origins_complete"] = page.next_after is None
                                    self._save_journal(operations)
                                count += len(page.items)
                                if page.next_after is None:
                                    break
                                after = page.next_after
                        # A cursor is progress only. An independent indexed check must
                        # prove the entire fenced population retired before reset.
                        from src.openclank.chat_source_bridge import ResetOriginCompletion, verify_reset_origins_retired
                        proof = await verify_reset_origins_retired(state=reset_state, claim=reset_claim,
                            owner=owner, account_id=account_id, operation_id=operation_id, reset_closure=(source_closure or stopped_closure))
                        if (type(proof) is not ResetOriginCompletion or proof.owner != owner
                                or proof.account_id != account_id or proof.operation_id != operation_id):
                            raise MemoryNukeUnavailable("reset origin completion proof changed")
                        from src.openclank.conversation_archive import get_conversation_archive, QueuedResetCompletion
                        archive=get_conversation_archive(); queued_expected=record.get('queued_expected')
                        if (record.get('queued_account_id')!=account_id or type(queued_expected) is not dict
                            or set(queued_expected)!={'version','count','fingerprint','device','inode'}
                            or type(queued_expected.get('version')) is not int or queued_expected['version'] not in (1,2)
                            or type(queued_expected.get('count')) is not int or queued_expected['count']<0
                            or (queued_expected['version']==2 and queued_expected['count']!=0)
                            or type(queued_expected.get('device')) is not int or queued_expected['device']<0
                            or type(queued_expected.get('inode')) is not int or queued_expected['inode']<0
                            or type(queued_expected.get('fingerprint')) is not str or len(queued_expected['fingerprint'])!=64
                            or any(char not in '0123456789abcdef' for char in queued_expected['fingerprint'])
                            or type(record.get('reset_queued_complete',False)) is not bool):
                            raise MemoryNukeUnavailable('queued reset preview unavailable')
                        inventory=await asyncio.to_thread(archive.queued_reset_inventory,account_id,operation_id)
                        if inventory!=queued_expected: raise MemoryNukeUnavailable('queued source inventory changed')
                        # A live native marker must be terminalized or its exact
                        # holder retired under this claim before archive authority
                        # is retired. Preserve it until that proof is available.
                        if queued_expected['count'] and stopped_closure is None:
                            raise MemoryNukeUnavailable('queued native runtime retirement is unavailable')
                        cursor=record.get('reset_queued_cursor',0)
                        if type(cursor) is not int or cursor<0: raise MemoryNukeUnavailable('queued reset checkpoint unavailable')
                        if record.get('reset_queued_complete') is not True and queued_expected['version']==2:
                            with locked(os.fspath(self.lock_target)):
                                operations=self._load_journal();current=operations[operation_id]
                                if current.get('reset_queued_cursor',0)!=cursor: raise MemoryNukeUnavailable('queued reset checkpoint changed')
                                current['reset_queued_complete']=True;current['reset_queued_count']=0
                                self._save_journal(operations)
                        elif record.get('reset_queued_complete') is not True:
                            while True:
                                if stopped_closure is not None: await pool.validate_queued_reset_closure(stopped_closure,claim=reset_claim)
                                page=await asyncio.to_thread(archive.retire_queued_reset_page,claim=reset_claim,provider=source_provider,
                                    owner=owner,account_id=account_id,operation_id=operation_id,expected=queued_expected,after=cursor)
                                if stopped_closure is not None: await pool.validate_queued_reset_closure(stopped_closure,claim=reset_claim)
                                next_after=page['next_after']
                                with locked(os.fspath(self.lock_target)):
                                    operations=self._load_journal();current=operations[operation_id]
                                    if current.get('reset_queued_cursor',0)!=cursor: raise MemoryNukeUnavailable('queued reset checkpoint changed')
                                    current['reset_queued_cursor']=next_after if next_after is not None else cursor
                                    current['reset_queued_complete']=next_after is None
                                    if next_after is None: current['reset_queued_count']=queued_expected['count']
                                    self._save_journal(operations)
                                if next_after is None: break
                                cursor=next_after
                        queued_proof=await asyncio.to_thread(archive.queued_reset_completion,claim=reset_claim,provider=source_provider,
                            owner=owner,account_id=account_id,operation_id=operation_id,expected=queued_expected)
                        if stopped_closure is not None: await pool.validate_queued_reset_closure(stopped_closure,claim=reset_claim)
                        if type(queued_proof) is not QueuedResetCompletion: raise MemoryNukeUnavailable('queued reset proof unavailable')
            if guarded and original_reset_unsent is not None:
                # Previous source effects stay checkpointed. Re-prove their
                # existing completion under this claim before the one original issue.
                original=record.get('reset_native_frozen')
                source_provider._assert_nonselective_reset_unsent(original_reset_unsent,reset_claim,original)
                original_components=original['request']['components']
                if {'memories','ingest'}.intersection(original_components):
                    if record.get('reset_origins_complete') is not True or record.get('reset_queued_complete') is not True:
                        raise MemoryNukeUnavailable('reset source completion unavailable')
                    from src.openclank.chat_source_bridge import ResetOriginCompletion, verify_reset_origins_retired
                    proof=await verify_reset_origins_retired(state=reset_state,claim=reset_claim,owner=owner,
                        account_id=account_id,operation_id=operation_id,reset_closure=(source_closure or stopped_closure))
                    if (type(proof) is not ResetOriginCompletion or proof.owner!=owner
                        or proof.account_id!=account_id or proof.operation_id!=operation_id):
                        raise MemoryNukeUnavailable('reset source completion changed')
                    from src.openclank.conversation_archive import get_conversation_archive, QueuedResetCompletion
                    from src.memery_external_sources import _queued_owned_thread
                    queued_proof=await _queued_owned_thread(get_conversation_archive().queued_reset_completion,
                        claim=reset_claim,provider=source_provider,owner=owner,account_id=account_id,
                        operation_id=operation_id,expected=record.get('queued_expected'))
                    if type(queued_proof) is not QueuedResetCompletion:
                        raise MemoryNukeUnavailable('queued reset proof unavailable')
                source_provider._assert_nonselective_reset_unsent(original_reset_unsent,reset_claim,original)
            selected = list(record["components"])
            expected = dict(record["expected"])
            categories: dict[str, dict[str, Any]] = {}
            db_components = sorted(item for item in expected if item in _DB_COMPONENTS)
            provider_memories_cleared = False
            participants = record.get('participants')
            if selective_original:
                if (type(selective_result) is not dict or selective_result.get('complete') is not True
                    or type(selective_result.get('categories')) is not dict
                    or set(selective_result['categories']) != {'ingest'}):
                    raise MemoryNukeUnavailable('selective reset receipt unavailable')
                categories['ingest'] = self._category_result(selective_result['categories']['ingest'],fallback_count=0)
                if categories['ingest']['state'] != 'complete':
                    raise MemoryNukeUnavailable('selective reset receipt unavailable')
            elif db_components and participants is not None:
                try:
                    fm = participants['fm']
                    native = participants['native']
                    fm_components = fm['components']
                    fm_expected = fm['expected']
                    if (type(fm_components) is not list or type(fm_expected) is not dict
                            or set(fm_components) != set(fm_expected) or type(native.get('snapshot')) is not dict):
                        raise MemoryNukeUnavailable('reset participant manifest unavailable')
                    request = {'owner':provider_owner, 'account_id':account_id, 'operation_id':operation_id,
                        'components':fm_components, 'expected_counts':fm_expected}
                    frozen=None
                    if guarded:
                        from src.openclank.fm_reset_receipt import freeze_reset_receipt
                        from src.memery_external_sources import _queued_owned_thread
                        if record.get('reset_native_started') is True:
                            frozen=record.get('reset_native_frozen')
                            if type(frozen) is not dict or type(frozen.get('request')) is not dict:
                                raise MemoryNukeUnavailable('reset original request unavailable')
                        else:
                            frozen=await _queued_owned_thread(freeze_reset_receipt,source_provider._guarded_preparation,
                                owner=provider_owner,account_id=account_id,operation_id=operation_id,
                                workspace_id=source_provider._scope(provider_owner).workspace_id,
                                components=fm_components,expected_counts=fm_expected,
                                queued_retired=bool(record.get('queued_expected',{}).get('count',0)))
                        request=frozen['request']
                        if (request.get('owner')!=provider_owner or request.get('account_id')!=account_id
                            or request.get('operation_id')!=operation_id or request.get('components')!=fm_components
                            or request.get('expected_counts')!=fm_expected):
                            raise MemoryNukeUnavailable('reset original request changed')
                        if stopped_closure is not None: lease=stopped_closure.lease
                    await asyncio.to_thread(reset_claim.assert_current, owner=owner, account_id=account_id, provider=source_provider, operation_id=operation_id)
                    with locked(os.fspath(self.lock_target)):
                        operations = self._load_journal(); current = operations[operation_id]
                        previous = current.get('reset_native_request')
                        if previous is not None and previous != request:
                            raise MemoryNukeUnavailable('reset FM request changed')
                        fm_result = current.get('reset_native_result')
                        if fm_result is None:
                            if not current.get('reset_native_started'):
                                current['reset_native_request'] = request
                                if frozen is not None: current['reset_native_frozen']=frozen
                                if stopped_closure is not None:
                                    lease.reset_native_frozen=frozen
                                    current['reset_source_manifest_sha256']=hashlib.sha256(json.dumps(current['reset_source_manifest'],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                                    lease.reset_manifest_sha256=current['reset_source_manifest_sha256']
                                current['reset_native_started'] = True
                                self._save_journal(operations)
                    if fm_result is None:
                        if stopped_closure is None and record.get('reset_native_started') is True and original_reset_unsent is None:
                            if type(original_reset_receipt) is not dict: raise MemoryNukeUnavailable('reset original receipt unavailable')
                            async def recover_and_checkpoint_fm():
                                value=await source_provider._host_guarded_reset_cleanup_no_closure(claim=reset_claim,frozen=frozen,
                                    receipt=original_reset_receipt,barrier=reset_barrier,witness=witness)
                                with locked(os.fspath(self.lock_target)):
                                    operations=self._load_journal();operations[operation_id]['reset_native_result']=value;self._save_journal(operations)
                                return value
                            fm_result=await drain_original_reset(recover_and_checkpoint_fm())
                        elif stopped_closure is None:
                            async def issue_and_checkpoint_fm():
                                try:
                                    value=await source_provider.reset_owner('reset_commit', owner=provider_owner,
                                        components=fm_components,expected_counts=fm_expected,_reset_capability=reset_claim,_queued_retirement=queued_proof,
                                        _reset_frozen=frozen,_reset_barrier=reset_barrier,_reset_witness=(stopped_closure.lease.witness if stopped_closure is not None else witness),
                                        _reset_unsent_fact=original_reset_unsent)
                                except Exception:
                                    await withdraw_nonselective_unsent(source_provider,frozen)
                                    raise
                                if guarded: await adopt_original_reset_stamp(source_provider,frozen)
                                await asyncio.to_thread(reset_claim.assert_current,owner=owner,account_id=account_id,provider=source_provider,operation_id=operation_id)
                                with locked(os.fspath(self.lock_target)):
                                    operations=self._load_journal();operations[operation_id]['reset_native_result']=value;self._save_journal(operations)
                                return value
                            fm_result=await drain_original_reset(issue_and_checkpoint_fm())
                        else:
                            if getattr(lease,'reset_native_frozen',None)!=frozen:
                                raise MemoryNukeUnavailable('reset native request changed')
                            task=getattr(lease,'reset_native_task',None)
                            if original_reset_unsent is not None:
                                if task is not None and not task.done(): raise MemoryNukeUnavailable('original reset I/O unsettled')
                                task=None
                            if task is None and (not resume_native or original_reset_unsent is not None):
                                async def run_native():
                                    try:
                                        value=await source_provider.reset_owner('reset_commit',owner=provider_owner,
                                            components=fm_components,expected_counts=fm_expected,_reset_capability=reset_claim,
                                            _queued_retirement=queued_proof,_reset_closure=stopped_closure,
                                            _reset_frozen=frozen,_reset_barrier=reset_barrier,_reset_witness=(stopped_closure.lease.witness if stopped_closure is not None else witness),
                                            _reset_unsent_fact=original_reset_unsent)
                                        lease.reset_native_result=value
                                        with locked(os.fspath(self.lock_target)):
                                            operations=self._load_journal();current=operations[operation_id]
                                            if current.get('reset_native_frozen')!=frozen: raise MemoryNukeUnavailable('reset native request changed')
                                            current['reset_native_result']=value;self._save_journal(operations)
                                        return value
                                    except Exception:
                                        # A content-free unresolved result stays on the exact lease.
                                        return None
                                task=asyncio.create_task(run_native());lease.reset_native_task=task
                                def consume_native_failure(done):
                                    if not done.cancelled():
                                        try: done.exception()
                                        except Exception: pass
                                task.add_done_callback(consume_native_failure)
                            if task is not None and not task.done():
                                try: await asyncio.shield(task)
                                except asyncio.CancelledError: raise
                                except Exception: pass
                            if task is not None and task.done() and not task.cancelled():
                                try: task.exception()
                                except Exception: pass
                            value=getattr(lease,'reset_native_result',None)
                            if value is None:
                                from src.openclank.fm_reset_receipt import read_committed_reset_receipt
                                from src.openclank.mimo_supervisor import _queued_owned_thread
                                def current():
                                    try: pool.assert_post_dispatch_queued_reset(stopped_closure,claim=reset_claim,frozen=frozen);return True
                                    except Exception: return False
                                try:
                                    receipt=await _queued_owned_thread(read_committed_reset_receipt,source_provider._guarded_preparation,frozen,current=current)
                                except Exception as exc:
                                    raise MemoryNukeUnavailable('reset FM outcome requires reconciliation') from exc
                                if receipt.get('state')=='committed':
                                    with locked(os.fspath(self.lock_target)):
                                        operations=self._load_journal();current=operations[operation_id]
                                        if current.get('reset_native_frozen')!=frozen:
                                            raise MemoryNukeUnavailable('reset native request changed')
                                        if current.get('reset_sql_receipt') not in (None,receipt):
                                            raise MemoryNukeUnavailable('reset SQL receipt changed')
                                        current['reset_sql_receipt']=receipt;self._save_journal(operations)
                                    cleanup=getattr(lease,'reset_cleanup_task',None)
                                    if cleanup is None or (resume_native and cleanup.done() and getattr(lease,'reset_native_result',None) is None):
                                        async def run_cleanup():
                                            try:
                                                value=await source_provider._host_guarded_reset_cleanup(claim=reset_claim,
                                                    closure=stopped_closure,frozen=frozen,receipt=receipt)
                                                lease.reset_native_result=value
                                                return value
                                            except Exception:return None
                                        cleanup=asyncio.create_task(run_cleanup());lease.reset_cleanup_task=cleanup
                                    if not cleanup.done():
                                        try: await asyncio.shield(cleanup)
                                        except asyncio.CancelledError: raise
                                        except Exception: pass
                                    value=getattr(lease,'reset_native_result',None)
                                    if value is None:
                                        raise MemoryNukeUnavailable('reset committed_pending_cleanup; owned cleanup unavailable or unproved')
                                else:
                                    raise MemoryNukeUnavailable('reset FM outcome requires reconciliation')
                            await pool.validate_post_dispatch_queued_reset(stopped_closure,claim=reset_claim,frozen=frozen)
                            fm_result=value
                            with locked(os.fspath(self.lock_target)):
                                operations=self._load_journal();current=operations[operation_id]
                                if current.get('reset_native_frozen')!=frozen: raise MemoryNukeUnavailable('reset native request changed')
                                current['reset_native_result']=fm_result;self._save_journal(operations)
                    if guarded:
                        source_provider._ack_original_reset_io(claim=reset_claim,frozen=frozen,result=fm_result)
                    fm_categories = fm_result.get('categories') if isinstance(fm_result, dict) else None
                    if type(fm_result) is not dict or fm_result.get('complete') is not True or type(fm_categories) is not dict:
                        raise MemoryNukeUnavailable('reset FM receipt unavailable')
                    if stopped_closure is not None:
                        from src.openclank.mimo_supervisor import _queued_owned_thread
                        await _queued_owned_thread(source_provider._assert_reset_retirement_ready,stopped_closure.lease.reset_transport_witness)
                    await asyncio.to_thread(reset_claim.assert_current, owner=owner, account_id=account_id, provider=self.memory_provider, operation_id=operation_id)
                    with locked(os.fspath(self.lock_target)):
                        operations = self._load_journal(); current = operations[operation_id]
                        native_result = current.get('reset_selected_result')
                        if native_result is None:
                            current['reset_selected_started'] = True
                            self._save_journal(operations)
                    if native_result is None:
                        if 'memories' in db_components:
                            worker = asyncio.create_task(asyncio.to_thread(self.memory_provider.lifecycle_clear, provider_owner, original=native['snapshot']))
                            try:
                                native_result = await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                while not worker.done():
                                    try:
                                        await asyncio.shield(worker)
                                    except asyncio.CancelledError:
                                        continue
                                if worker.cancelled():
                                    raise MemoryNukeUnavailable('native reset outcome unavailable')
                                worker.result()
                                raise
                        else:
                            native_result = {'state':'applied', 'count':0}
                        await asyncio.to_thread(reset_claim.assert_current, owner=owner, account_id=account_id, provider=self.memory_provider, operation_id=operation_id)
                        with locked(os.fspath(self.lock_target)):
                            operations = self._load_journal(); operations[operation_id]['reset_selected_result'] = native_result
                            self._save_journal(operations)
                    if type(native_result) is not dict or native_result.get('state') not in ('applied', 'reconciled'):
                        raise MemoryNukeUnavailable('reset Native receipt unavailable')
                    db_components = sorted(set(db_components) | set(fm_components))
                    for component in db_components:
                        raw_fm = fm_categories.get(component) if component in fm_expected else None
                        if component in fm_expected and (type(raw_fm) is not dict or type(raw_fm.get('count')) is not int or raw_fm['count'] < 0):
                            raise MemoryNukeUnavailable('reset FM count unavailable')
                        fm_value = self._category_result(raw_fm, fallback_count=0) if component in fm_expected else {'state':'complete','count':0}
                        native_count = native_result.get('count') if component == 'memories' else 0
                        unknown = native_count is None
                        if not unknown and (type(native_count) is not int or native_count < 0):
                            raise MemoryNukeUnavailable('reset Native count unavailable')
                        categories[component] = {'state':fm_value['state'], 'count':fm_value['count'] + (0 if unknown else native_count),
                            **({'count_unavailable':True} if unknown else {}), **({'error':fm_value['error']} if 'error' in fm_value else {})}
                    provider_memories_cleared = categories.get('memories', {}).get('state') == 'complete'
                except MemoryNukeUnavailable:
                    raise
                except Exception as exc:
                    raise MemoryNukeUnavailable('guarded participant reset requires reconciliation') from exc
            elif db_components:
                try:
                    provider_result = record.get("reset_native_result") if guarded else None
                    native_request = {"owner":provider_owner, "account_id":account_id, "operation_id":operation_id,
                        "components":db_components, "expected_counts":{key:expected[key] for key in db_components}}
                    frozen = None
                    if guarded and not record.get('reset_native_started'):
                        from src.openclank.fm_reset_receipt import freeze_reset_receipt
                        from src.memery_external_sources import _queued_owned_thread
                        frozen=await _queued_owned_thread(freeze_reset_receipt,self.memory_provider._guarded_preparation,
                            owner=provider_owner,account_id=account_id,operation_id=operation_id,
                            workspace_id=self.memory_provider._scope(provider_owner).workspace_id,
                            components=db_components,expected_counts={key:expected[key] for key in db_components},
                            queued_retired=bool(record.get('queued_expected',{}).get('count',0)))
                        native_request=frozen['request']
                    elif guarded and record.get('reset_native_started'):
                        frozen=record.get('reset_native_frozen')
                        if type(frozen) is not dict or type(frozen.get('request')) is not dict:
                            raise MemoryNukeUnavailable('reset original request unavailable')
                        native_request=frozen['request']
                        if (native_request.get('owner')!=provider_owner or native_request.get('account_id')!=account_id
                            or native_request.get('operation_id')!=operation_id or native_request.get('components')!=db_components
                            or native_request.get('expected_counts')!={key:expected[key] for key in db_components}):
                            raise MemoryNukeUnavailable('reset original request changed')
                    if guarded and record.get("reset_native_started") and record.get("reset_native_request") != native_request:
                        raise MemoryNukeUnavailable("reset provider request changed")
                    if guarded and record.get("reset_native_started") and provider_result is None and original_reset_unsent is None:
                        if type(original_reset_receipt) is not dict: raise MemoryNukeUnavailable('reset original receipt unavailable')
                        async def recover_and_checkpoint_primary():
                            if stopped_closure is not None:
                                value=await self.memory_provider._host_guarded_reset_cleanup(claim=reset_claim,closure=stopped_closure,
                                    frozen=frozen,receipt=original_reset_receipt)
                            else:
                                value=await self.memory_provider._host_guarded_reset_cleanup_no_closure(claim=reset_claim,frozen=frozen,
                                    receipt=original_reset_receipt,barrier=reset_barrier,witness=witness)
                            with locked(os.fspath(self.lock_target)):
                                operations=self._load_journal();operations[operation_id]['reset_native_result']=value;self._save_journal(operations)
                            return value
                        provider_result=await drain_original_reset(recover_and_checkpoint_primary())
                    if guarded and provider_result is None:
                        with locked(os.fspath(self.lock_target)):
                            operations = self._load_journal()
                            previous = operations[operation_id].get("reset_native_request")
                            if previous is not None and previous != native_request:
                                raise MemoryNukeUnavailable("reset provider request changed")
                            operations[operation_id]["reset_native_request"] = native_request
                            if frozen is not None: operations[operation_id]['reset_native_frozen'] = frozen
                            if stopped_closure is not None:
                                lease=stopped_closure.lease
                                lease.reset_native_frozen=frozen
                                manifest=operations[operation_id].get('reset_source_manifest')
                                if type(manifest) is not dict: raise MemoryNukeUnavailable('reset source manifest unavailable')
                                digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
                                operations[operation_id]['reset_source_manifest_sha256']=digest
                                lease.reset_manifest_sha256=digest
                            operations[operation_id]["reset_native_started"] = True
                            self._save_journal(operations)
                    if provider_result is None:
                      async def issue_and_checkpoint_primary():
                        try:
                            value=await self.memory_provider.reset_owner(
                                "reset_commit", owner=provider_owner, components=db_components,
                                expected_counts={key: expected[key] for key in db_components},
                                **({"_reset_capability": reset_claim, "_queued_retirement":queued_proof,
                                    "_reset_frozen":frozen, "_reset_barrier":reset_barrier, "_reset_witness":(stopped_closure.lease.witness if stopped_closure is not None else witness),
                                    "_reset_unsent_fact":original_reset_unsent,
                                    **({'_reset_closure':stopped_closure} if stopped_closure is not None else {})} if guarded else {}))
                        except Exception:
                            if guarded: await withdraw_nonselective_unsent(self.memory_provider,frozen)
                            raise
                        if guarded:
                            await adopt_original_reset_stamp(self.memory_provider,frozen)
                            await asyncio.to_thread(reset_claim.assert_current,owner=owner,account_id=account_id,
                                provider=self.memory_provider,operation_id=operation_id)
                            with locked(os.fspath(self.lock_target)):
                                operations=self._load_journal();operations[operation_id]['reset_native_result']=value;self._save_journal(operations)
                        return value
                      provider_result=await drain_original_reset(issue_and_checkpoint_primary()) if guarded else await issue_and_checkpoint_primary()
                    if guarded:
                        self.memory_provider._ack_original_reset_io(claim=reset_claim,frozen=frozen,result=provider_result)
                    returned = (
                        provider_result.get("categories")
                        if isinstance(provider_result, dict)
                        else None
                    )
                    for component in db_components:
                        value = returned.get(component) if isinstance(returned, dict) else None
                        categories[component] = self._category_result(
                            value,
                            fallback_count=int(expected[component]["count"]),
                        )
                    provider_memories_cleared = (
                        categories.get("memories", {}).get("state") == "complete"
                    )
                except Exception as exc:
                    if guarded:
                        raise MemoryNukeUnavailable("guarded reset outcome requires reconciliation") from exc
                    error = _clean_error(exc)
                    for component in db_components:
                        categories[component] = {
                            "state": "failed",
                            "count": 0,
                            "error": error,
                        }

            async def local_step(name, invoke, snapshot, expected_value, *, empty_proves=True, reconcile=None):
                if not guarded:
                    value = invoke()
                    return await value if inspect.isawaitable(value) else value
                with locked(os.fspath(self.lock_target)):
                    operations = self._load_journal()
                    steps = operations[operation_id].setdefault("reset_local_steps", {})
                    if not isinstance(steps, dict):
                        raise MemoryNukeUnavailable("local reset checkpoint unavailable")
                    step = steps.get(name)
                    if isinstance(step, dict) and step.get("state") == "complete":
                        if (set(step) != {"state", "result"} or not isinstance(step["result"], dict)
                                or step["result"].get("complete") is not True):
                            raise MemoryNukeUnavailable("local reset checkpoint unavailable")
                        return dict(step["result"])
                    if step is not None and step != {"state":"started"}:
                        raise MemoryNukeUnavailable("local reset checkpoint unavailable")
                if step is not None:
                    recovered = None
                    if reconcile is not None:
                        try:
                            recovered = reconcile()
                            if inspect.isawaitable(recovered):
                                recovered = await recovered
                        except (ValueError, OSError):
                            recovered = None
                    current = snapshot()
                    if inspect.isawaitable(current):
                        current = await current
                    if not isinstance(current, dict) or type(current.get("count")) is not int:
                        raise MemoryNukeUnavailable("local reset postcondition unavailable")
                    if current["count"] == 0:
                        if recovered is not None:
                            value = recovered
                        elif current == expected_value:
                            # The complete original snapshot is still present;
                            # retrying its existing CAS is safe even if empty.
                            value = None
                        elif not empty_proves:
                            raise MemoryNukeUnavailable("local reset quarantine requires reconciliation")
                        else:
                            value = {"complete":True, "count":0, "count_unavailable":True, "reconciled":True}
                    elif current != expected_value:
                        raise MemoryNukeUnavailable("local reset scope changed")
                    else:
                        value = None
                else:
                    value = None
                if value is None:
                    with locked(os.fspath(self.lock_target)):
                        operations = self._load_journal()
                        operations[operation_id].setdefault("reset_local_steps", {})[name] = {"state":"started"}
                        self._save_journal(operations)
                    value = invoke()
                    if inspect.isawaitable(value):
                        value = await value
                    if not isinstance(value, dict) or value.get("complete") is not True:
                        raise MemoryNukeUnavailable("local reset step requires reconciliation")
                with locked(os.fspath(self.lock_target)):
                    operations = self._load_journal()
                    operations[operation_id].setdefault("reset_local_steps", {})[name] = {"state":"complete", "result":value}
                    self._save_journal(operations)
                return value

            agent_memory_expected = record.get("agent_memory_expected")
            if isinstance(agent_memory_expected, dict):
                current_memory = categories.get("memories")
                if agent_supervisor is None or not callable(
                    getattr(agent_supervisor, "reset_owner_memory", None)
                ):
                    agent_result = None
                    agent_error = "active agent runtime cannot clear authored memory"
                else:
                    try:
                        agent_result = await local_step("agent_memory",
                            lambda: agent_supervisor.reset_owner_memory(owner, expected=agent_memory_expected),
                            lambda: agent_supervisor.preview_owner_memory(owner), agent_memory_expected)
                        agent_error = ""
                    except Exception as exc:
                        agent_result = None
                        agent_error = _clean_error(exc)
                agent_complete = bool(
                    isinstance(agent_result, dict)
                    and agent_result.get("complete") is True
                )
                if current_memory is None:
                    current_memory = {"state": "failed", "count": 0}
                    categories["memories"] = current_memory
                if agent_complete:
                    if agent_result.get("count_unavailable"):
                        current_memory["count_unavailable"] = True
                    current_memory["count"] = int(current_memory.get("count") or 0) + int(
                        agent_result.get("count") or 0
                    )
                elif not agent_complete:
                    current_memory["state"] = "failed"
                    current_memory["error"] = agent_error or "agent authored-memory reset failed"

            legacy_memory_expected = record.get("legacy_memory_expected")
            if isinstance(legacy_memory_expected, dict):
                current_memory = categories.get("memories")
                try:
                    legacy_result = await local_step("legacy_memory",
                        lambda: self._purge_legacy_residue(owner, expected=legacy_memory_expected),
                        lambda: self._legacy_residue_snapshot(owner)[0], legacy_memory_expected)
                    legacy_error = ""
                except Exception as exc:
                    legacy_result = None
                    legacy_error = _clean_error(exc)
                legacy_complete = bool(
                    isinstance(legacy_result, dict)
                    and legacy_result.get("complete") is True
                )
                if current_memory is None:
                    current_memory = {"state": "failed", "count": 0}
                    categories["memories"] = current_memory
                if legacy_complete:
                    if legacy_result.get("count_unavailable"):
                        current_memory["count_unavailable"] = True
                    current_memory["count"] = int(current_memory.get("count") or 0) + int(
                        legacy_result.get("count") or 0
                    )
                elif not legacy_complete:
                    prior_error = str(current_memory.get("error") or "").strip()
                    current_memory["state"] = "failed"
                    current_memory["error"] = "; ".join(
                        item for item in [prior_error, legacy_error or "legacy memory reset failed"]
                        if item
                    )[:300]

            media_expected = record.get("media_expected")
            if isinstance(media_expected, dict):
                current_memory = categories.get("memories")
                if current_memory is None:
                    current_memory = {"state": "failed", "count": 0}
                    categories["memories"] = current_memory
                if current_memory.get("state") == "complete":
                    try:
                        media_result = await local_step("media",
                            lambda: self.media_store.purge_owner(owner, expected=media_expected),
                            lambda: self.media_store.preview_owner_purge(owner), media_expected)
                        media_complete = bool(
                            isinstance(media_result, dict)
                            and media_result.get("complete") is True
                        )
                        media_error = ""
                    except Exception as exc:
                        media_result = None
                        media_complete = False
                        media_error = _clean_error(exc)
                    if media_complete:
                        if media_result.get("count_unavailable"):
                            current_memory["count_unavailable"] = True
                        current_memory["count"] = int(
                            current_memory.get("count") or 0
                        ) + int(media_result.get("count") or 0)
                    else:
                        current_memory["state"] = "failed"
                        current_memory["error"] = (
                            media_error or "memory media reset failed"
                        )[:300]

            principal_baseline: dict[str, Any] = {"state": "not_selected"}
            if provider_memories_cleared:
                # The provider reset removes principal bindings. A memoized
                # binding would otherwise be returned until process restart,
                # leaving /api/memory/principals unavailable after a nuke.
                try:
                    from services.memory.principal_context import (
                        ensure_principal_context_cached,
                        invalidate_principal_cache,
                        principal_context_applies,
                    )
                    from src.memory_scope import chat_workspace

                    principal_owner = str(provider_owner or "").strip()
                    if not principal_owner:
                        raise MemoryNukeUnavailable(
                            "principal reset convergence requires an exact owner"
                        )
                    invalidate_principal_cache(principal_owner)
                    principal_baseline = {"state": "cache_invalidated"}
                    principal_db_path = getattr(
                        source_provider if participants is not None else self.memory_provider, "_fm_db_path", None
                    )
                    if principal_db_path and not principal_context_applies(str(principal_db_path)):
                        # Guarded (non-v16) stores have no principal group; nothing to reseed.
                        principal_baseline = {"state": "not_applicable"}
                    elif principal_db_path:
                        context = ensure_principal_context_cached(
                            owner=principal_owner,
                            workspace_id=chat_workspace(),
                            db_path=str(principal_db_path),
                        )
                        if not isinstance(context, dict):
                            raise MemoryNukeUnavailable(
                                "reserved Memory principals could not be reseeded"
                            )
                        principal_baseline = {
                            "state": "reseeded",
                            "assistant": "self",
                            "handler": "Handler",
                        }
                except Exception as exc:
                    error = _clean_error(exc)
                    principal_baseline = {"state": "failed", "error": error}
                    current_memory = categories.setdefault(
                        "memories", {"state": "failed", "count": 0}
                    )
                    prior_error = str(current_memory.get("error") or "").strip()
                    current_memory["state"] = "failed"
                    current_memory["error"] = "; ".join(
                        item
                        for item in [
                            prior_error,
                            f"principal baseline convergence failed: {error}",
                        ]
                        if item
                    )[:300]

            staging_expected = record.get("staging_expected")
            if (
                isinstance(staging_expected, dict)
                and categories.get("ingest", {}).get("state") == "complete"
            ):
                try:
                    from services.memory.import_batch import MemoryImportBatchStore
                    from src.constants import FM_DB_PATH

                    staging_store = MemoryImportBatchStore(
                        db_path=str(
                            getattr(self.canonical_memory_provider or self.memory_provider, "_fm_db_path", None)
                            or FM_DB_PATH
                        ),
                        data_dir=os.path.dirname(str(getattr(self.canonical_memory_provider or self.memory_provider, "_fm_db_path", None) or FM_DB_PATH)) or DATA_DIR,
                    )
                    staging_result = await local_step("staging",
                        lambda: staging_store.purge_owner_staging(owner, expected=staging_expected),
                        lambda: staging_store.preview_owner_staging(owner), staging_expected)
                    snapshot_claims = record.get("snapshot_import_claims")
                    selective_manifest = record.get('reset_selective_manifest')
                    if selective_manifest is not None:
                        if (type(selective_manifest) is not dict or hashlib.sha256(_canonical_json(selective_manifest)).hexdigest()
                            != record.get('reset_selective_manifest_sha256')
                            or selective_manifest.get('account_id') != account_id
                            or selective_manifest.get('operation_id') != operation_id):
                            raise MemoryNukeUnavailable('selective snapshot scope unavailable')
                        imports=selective_manifest.get('partition',{}).get('imports')
                        if (type(imports) is not dict or type(imports.get('snapshot_claims')) is not list
                            or type(imports.get('erasable_job_selectors')) is not list
                            or type(snapshot_claims) is not list):
                            raise MemoryNukeUnavailable('selective snapshot scope unavailable')
                        selected=[]
                        for item in imports['snapshot_claims']:
                            if type(item) is not dict or set(item)!={'workspace_id','job_selector','item_id','account','upload_id','scope'}:
                                raise MemoryNukeUnavailable('selective snapshot scope unavailable')
                            if item['job_selector'] not in imports['erasable_job_selectors']:
                                continue
                            entry={key:item[key] for key in ('item_id','account','upload_id','scope')}
                            if entry['account'] != account_id or entry not in snapshot_claims or entry in selected:
                                raise MemoryNukeUnavailable('selective snapshot scope changed')
                            selected.append(entry)
                        snapshot_claims=selected
                    if snapshot_claims:
                        if not guarded or reset_claim is None or staging_result.get("complete") is not True:
                            raise MemoryNukeUnavailable("snapshot staging retirement unavailable")
                        def retire_snapshot_claims():
                            from src.tool_utils import get_upload_handler
                            from src.openclank.analysis_snapshot_upload import retire_snapshot_import_claims
                            retire_snapshot_import_claims(reset_claim=reset_claim, staging_store=staging_store,
                                handler=get_upload_handler(), claims=snapshot_claims, owner=owner,
                                account_id=account_id, provider=self.canonical_memory_provider or self.memory_provider,
                                operation_id=operation_id)
                        retire_task = asyncio.create_task(asyncio.to_thread(retire_snapshot_claims))
                        try:
                            await asyncio.shield(retire_task)
                        except asyncio.CancelledError:
                            while not retire_task.done():
                                try: await asyncio.shield(retire_task)
                                except asyncio.CancelledError: continue
                            try: retire_task.result()
                            except Exception as exc: raise asyncio.CancelledError() from exc
                            raise
                    if not selective_original:
                        if staging_result.get("count_unavailable"):
                            categories["ingest"]["count_unavailable"] = True
                        categories["ingest"]["count"] = int(
                            categories["ingest"].get("count") or 0
                        ) + int(staging_result.get("count") or 0)
                except Exception as exc:
                    categories["ingest"]["state"] = "failed"
                    categories["ingest"]["error"] = _clean_error(exc)

            if "skills" in selected:
                try:
                    if callable(self.skill_job_canceller):
                        quiesced = self.skill_job_canceller(owner)
                        if inspect.isawaitable(quiesced):
                            await quiesced
                    skill_result = await local_step("skills",
                        lambda: self.skills_manager.purge_owner(owner, expected=expected["skills"], **({"operation_id":operation_id} if guarded else {})),
                        lambda: self.skills_manager.preview_owner_purge(owner), expected["skills"], empty_proves=False,
                        reconcile=lambda: self.skills_manager.reconcile_owner_purge(owner, operation_id=operation_id, expected=expected["skills"]))
                    skill_complete = bool(
                        isinstance(skill_result, dict)
                        and skill_result.get("complete") is True
                    )
                    categories["skills"] = {
                        "state": "complete" if skill_complete else "failed",
                        "count": int(
                            skill_result.get("count", 0)
                            if isinstance(skill_result, dict)
                            else 0
                        ),
                    }
                    if skill_result.get("count_unavailable"):
                        categories["skills"]["count_unavailable"] = True
                    if not skill_complete:
                        failures = (
                            skill_result.get("failures")
                            if isinstance(skill_result, dict)
                            else None
                        )
                        categories["skills"]["error"] = (
                            "; ".join(str(item) for item in failures)[:300]
                            if failures
                            else "skill reset did not complete"
                        )
                except Exception as exc:
                    categories["skills"] = {
                        "state": "failed",
                        "count": 0,
                        "error": _clean_error(exc),
                    }
                finally:
                    # Release any process-local persistence suspension even
                    # when the CAS purge failed. A retry must be able to
                    # persist the owner's unchanged durable rows normally.
                    if callable(self.skill_job_runtime_sync):
                        try:
                            synchronized = self.skill_job_runtime_sync(owner)
                            if inspect.isawaitable(synchronized):
                                await synchronized
                        except Exception as exc:
                            if categories.get("skills", {}).get("state") == "complete":
                                prior_count = int(
                                    categories["skills"].get("count") or 0
                                )
                                categories["skills"] = {
                                    "state": "failed",
                                    "count": prior_count,
                                    "error": _clean_error(exc),
                                }

            result_components = list(db_components)
            if "skills" in selected:
                result_components.append("skills")

            # Preserve truthful deletion totals across a partial attempt and
            # its later retry. Completed substeps are idempotent and return
            # zero on retry; without this durable accumulation the terminal
            # receipt incorrectly claimed that nothing was deleted.
            prior_counts = {} if guarded else record.get("accumulated_counts")
            if not isinstance(prior_counts, dict):
                prior_counts = {}
            accumulated_counts: dict[str, int] = {}
            for component in result_components:
                prior = prior_counts.get(component, 0)
                if isinstance(prior, bool) or not isinstance(prior, int) or prior < 0:
                    prior = 0
                current_count = categories.get(component, {}).get("count", 0)
                if (
                    isinstance(current_count, bool)
                    or not isinstance(current_count, int)
                    or current_count < 0
                ):
                    current_count = 0
                accumulated_counts[component] = prior + current_count
                categories.setdefault(component, {})["count"] = accumulated_counts[
                    component
                ]

            complete = all(
                categories.get(component, {}).get("state") == "complete"
                for component in result_components
            )
            result: dict[str, Any] = {
                "status": "complete" if complete else "partial",
                "complete": complete,
                "operation_id": operation_id,
                "categories": {key: categories[key] for key in result_components},
                "receipt": {
                    "attempt": int(record.get("attempt_count") or 0) + 1,
                    "requested_components": list(selected),
                    "expanded_components": list(result_components),
                    "retained": [
                        dict(item)
                        for item in record.get("retained", _RETAINED_DOMAINS)
                        if isinstance(item, dict)
                    ],
                    "principal_baseline": principal_baseline,
                },
            }
            if not complete:
                result["message"] = (
                    "Some selected live-memory categories could not be cleared; "
                    "review the per-category errors before retrying."
                )
            if complete and stopped_closure is not None:
                # The exact pool owner must finish its stopped-holder
                # transition while the durable reset claim is still current.
                # No implementation is installed until its retry-safe journal
                # handoff has been qualified.
                return await stopped_closure.lease.reset_finalizer(stopped_closure,claim=reset_claim,
                    result=result,accumulated_counts=accumulated_counts)
            with locked(os.fspath(self.lock_target)):
                operations = self._load_journal()
                current = self._validated_record(
                    operations=operations,
                    owner=owner,
                    provider_owner=provider_owner,
                    operation_id=operation_id,
                    preview_token=preview_token,
                    confirmation=confirmation,
                    # The TTL gates entry into the destructive section (checked
                    # at commit start above), not its accounting: a slow
                    # commit must still finalize its journal row honestly.
                    check_expiry=False,
                )
                if isinstance(current.get("result"), dict):
                    if getattr(lock,'_terminal_s08_witness',None) is not None: lock._terminal_s08_witness=None
                    return dict(current["result"])
                current["state"] = result["status"]
                current.pop("commit_claim", None)
                current["attempt_count"] = result["receipt"]["attempt"]
                current["accumulated_counts"] = accumulated_counts
                if complete:
                    current["result"] = result
                else:
                    # A partial result is not terminal: the operation stays
                    # retryable under the same preview binding, and the
                    # attempt outcome is observability only, never replayed.
                    current["last_attempt"] = result
                current["updated_at"] = self.clock()
                operations[operation_id] = current
                self._save_journal(operations)
            if complete and selective_original:
                source_provider._ack_finished_original_reset_handoff(reset_claim,
                    record['reset_native_frozen'],result)
            if complete and getattr(lock,'_terminal_s08_witness',None) is not None: lock._terminal_s08_witness=None
            return result


__all__ = [
    "MemoryNukeConflict",
    "MemoryNukeCoordinator",
    "MemoryNukeError",
    "MemoryNukeUnavailable",
    "NUKE_COMPONENTS",
]
