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
import secrets
import socket
import time
import uuid
from datetime import datetime, timezone
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


class MemoryNukeCoordinator:
    """Bind an owner reset commit to one short-lived, durable preview."""

    def __init__(
        self,
        memory_provider,
        skills_manager=None,
        *,
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
    ) -> dict[str, Any]:
        components = self.normalize_components(components)
        db_components = [item for item in components if item in _DB_COMPONENTS]
        expected: dict[str, dict[str, Any]] = {}
        provider_implications: list[str] = []

        if db_components:
            reset_owner = getattr(self.memory_provider, "reset_owner", None)
            if not callable(reset_owner):
                raise MemoryNukeUnavailable(
                    "active memory provider does not expose owner reset"
                )
            provider_preview = None
            try:
                provider_preview = await reset_owner(
                    "reset_preview",
                    owner=provider_owner,
                    components=db_components,
                    expected_counts=None,
                )
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
        if "ingest" in expected and owner:
            from services.memory.import_batch import MemoryImportBatchStore
            from src.constants import FM_DB_PATH

            batch_store = MemoryImportBatchStore(
                db_path=str(
                    getattr(self.memory_provider, "_fm_db_path", None) or FM_DB_PATH
                ),
                data_dir=str(self.data_dir),
            )
            staging_expected = batch_store.preview_owner_staging(owner)

        operation_id = "nuke_" + uuid.uuid4().hex
        preview_token = secrets.token_urlsafe(32)
        expires_epoch = self.clock() + self.preview_ttl_seconds
        binding = {
            "version": 1,
            "operation_id": operation_id,
            "owner": owner,
            "provider_owner": provider_owner,
            "components": components,
            "expected": expected,
            "agent_memory_expected": agent_memory_expected,
            "legacy_memory_expected": legacy_memory_expected,
            "media_expected": media_expected,
            "staging_expected": staging_expected,
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
        if staging_expected is not None:
            public_counts["ingest"]["count"] += int(staging_expected["count"])
        return {
            "status": "preview",
            "complete": False,
            "operation_id": operation_id,
            "preview_token": preview_token,
            "confirmation": confirmation,
            "components": public_counts,
            "expanded_components": sorted(expected),
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

    async def commit(
        self,
        *,
        owner: str,
        provider_owner: str,
        operation_id: str,
        preview_token: str,
        confirmation: str,
        agent_supervisor=None,
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
                if held.locked()
            }
        lock = self._commit_locks.setdefault(operation_id, asyncio.Lock())
        async with lock:
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
                if isinstance(record.get("result"), dict):
                    return dict(record["result"])
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
                record["commit_claim"] = {
                    "coordinator_id": self._commit_claim_id,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                }
                record["updated_at"] = self.clock()
                operations[operation_id] = record
                self._save_journal(operations)

            selected = list(record["components"])
            expected = dict(record["expected"])
            categories: dict[str, dict[str, Any]] = {}
            db_components = sorted(item for item in expected if item in _DB_COMPONENTS)
            provider_memories_cleared = False
            if db_components:
                try:
                    provider_result = await self.memory_provider.reset_owner(
                        "reset_commit",
                        owner=provider_owner,
                        components=db_components,
                        expected_counts={key: expected[key] for key in db_components},
                    )
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
                    error = _clean_error(exc)
                    for component in db_components:
                        categories[component] = {
                            "state": "failed",
                            "count": 0,
                            "error": error,
                        }

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
                        agent_result = await agent_supervisor.reset_owner_memory(
                            owner,
                            expected=agent_memory_expected,
                        )
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
                    legacy_result = self._purge_legacy_residue(
                        owner,
                        expected=legacy_memory_expected,
                    )
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
                        media_result = self.media_store.purge_owner(
                            owner,
                            expected=media_expected,
                        )
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
                        self.memory_provider, "_fm_db_path", None
                    )
                    if principal_db_path:
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

                    staging_result = MemoryImportBatchStore(
                        db_path=str(
                            getattr(self.memory_provider, "_fm_db_path", None)
                            or FM_DB_PATH
                        ),
                        data_dir=str(self.data_dir),
                    ).purge_owner_staging(owner, expected=staging_expected)
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
                    skill_result = self.skills_manager.purge_owner(
                        owner,
                        expected=expected["skills"],
                    )
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
            prior_counts = record.get("accumulated_counts")
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
            return result


__all__ = [
    "MemoryNukeConflict",
    "MemoryNukeCoordinator",
    "MemoryNukeError",
    "MemoryNukeUnavailable",
    "NUKE_COMPONENTS",
]
