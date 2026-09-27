"""Content-free lifecycle adapter for the application's common SQL owner rows.

Authentication identities and the durable account-operation barrier deliberately
live outside :mod:`core.database.Base`.  This adapter only moves or removes the
mutable, username-keyed application rows behind that barrier.  Every mutation
uses one injected SQLAlchemy session and one transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from sqlalchemy import func, tuple_

from core.database import (
    AgentTurn,
    Base,
    CalendarCal,
    CalendarEvent,
    ChatMessage,
    Document,
    DocumentVersion,
    MimoProjectionState,
    MimoProjection,
    ModelCapability,
    ModelEndpoint,
    ModelShare,
    ModelShareSubscription,
    PublishedFile,
    PublishedFileGrant,
    ScheduledTask,
    Session,
    TaskRun,
    TuiTurnSubmission,
    TurnActor,
    UserTool,
    UserToolData,
)
# Register additive Stats owner rows in the same dynamically discovered
# lifecycle registry as the canonical application tables.
from core import stats_models  # noqa: F401


_SPECIAL_OWNER_COLUMNS = {
    MimoProjectionState.__tablename__: "owner_id",
    ModelShareSubscription.__tablename__: "subscriber",
}

# These tables are intentionally outside ``core.database.Base`` today.  Keep
# the explicit deny-list as a second fail-safe if a future refactor registers
# immutable authentication identities or the lifecycle barrier on that Base.
_IMMUTABLE_BARRIER_TABLES = frozenset(
    {
        "account_lifecycle_operations",
        "auth_accounts",
        "auth_identities",
        "auth_users",
    }
)


class SqlOwnerLifecycleError(RuntimeError):
    """Base error for a common-SQL owner lifecycle operation."""


class SqlOwnerLifecycleConflict(SqlOwnerLifecycleError):
    """Raised before mutation when owner state is ambiguous or stale."""

    def __init__(
        self,
        message: str,
        *,
        source: Mapping[str, Any] | None = None,
        target: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.source = dict(source or {})
        self.target = dict(target or {})


@dataclass(frozen=True)
class SqlOwnerBinding:
    """One mutable owner key discovered on ``core.database.Base``."""

    model: type
    column_name: str

    @property
    def key(self) -> str:
        return f"{self.model.__table__.name}.{self.column_name}"


@dataclass(frozen=True)
class SqlOwnerDependent:
    """A row whose owner is derived through one or more parent foreign keys."""

    model: type
    joins: tuple[tuple[type, str, type, str], ...]
    root_model: type
    root_owner_column: str

    @property
    def key(self) -> str:
        return (
            f"{self.model.__table__.name}.via:"
            f"{self.root_model.__table__.name}.{self.root_owner_column}"
        )

    @property
    def depth(self) -> int:
        return len(self.joins)


_DEPENDENT_BINDINGS = (
    SqlOwnerDependent(
        TurnActor,
        (
            (TurnActor, "root_turn_id", AgentTurn, "root_turn_id"),
            (AgentTurn, "root_turn_id", ChatMessage, "id"),
            (ChatMessage, "session_id", Session, "id"),
        ),
        Session,
        "owner",
    ),
    SqlOwnerDependent(
        AgentTurn,
        (
            (AgentTurn, "root_turn_id", ChatMessage, "id"),
            (ChatMessage, "session_id", Session, "id"),
        ),
        Session,
        "owner",
    ),
    SqlOwnerDependent(
        CalendarEvent,
        ((CalendarEvent, "calendar_id", CalendarCal, "id"),),
        CalendarCal,
        "owner",
    ),
    SqlOwnerDependent(
        ChatMessage,
        ((ChatMessage, "session_id", Session, "id"),),
        Session,
        "owner",
    ),
    SqlOwnerDependent(
        DocumentVersion,
        ((DocumentVersion, "document_id", Document, "id"),),
        Document,
        "owner",
    ),
    SqlOwnerDependent(
        MimoProjection,
        ((MimoProjection, "odysseus_session_id", Session, "id"),),
        Session,
        "owner",
    ),
    SqlOwnerDependent(
        ModelCapability,
        ((ModelCapability, "endpoint_id", ModelEndpoint, "id"),),
        ModelEndpoint,
        "owner",
    ),
    SqlOwnerDependent(
        ModelShareSubscription,
        ((ModelShareSubscription, "share_id", ModelShare, "id"),),
        ModelShare,
        "owner",
    ),
    SqlOwnerDependent(
        PublishedFileGrant,
        ((PublishedFileGrant, "file_id", PublishedFile, "id"),),
        PublishedFile,
        "owner",
    ),
    SqlOwnerDependent(
        TaskRun,
        ((TaskRun, "task_id", ScheduledTask, "id"),),
        ScheduledTask,
        "owner",
    ),
    SqlOwnerDependent(
        TuiTurnSubmission,
        ((TuiTurnSubmission, "session_id", Session, "id"),),
        Session,
        "owner",
    ),
    SqlOwnerDependent(
        UserToolData,
        ((UserToolData, "tool_id", UserTool, "id"),),
        UserTool,
        "owner",
    ),
)


@dataclass(frozen=True)
class SqlOwnerLifecycleReceipt:
    """Content-free evidence for one transactional lifecycle action."""

    action: str
    state: str
    source_owner: str
    target_owner: str | None
    changed: Mapping[str, int]
    source_before: Mapping[str, Any]
    target_before: Mapping[str, Any] | None
    source_after: Mapping[str, Any]
    target_after: Mapping[str, Any] | None

    @property
    def count(self) -> int:
        return sum(int(value) for value in self.changed.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "state": self.state,
            "source_owner": self.source_owner,
            "target_owner": self.target_owner,
            "count": self.count,
            "changed": dict(self.changed),
            "source_before": dict(self.source_before),
            "target_before": (
                dict(self.target_before) if self.target_before is not None else None
            ),
            "source_after": dict(self.source_after),
            "target_after": (
                dict(self.target_after) if self.target_after is not None else None
            ),
        }


def _owner(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized or "\x00" in normalized:
        raise SqlOwnerLifecycleError("SQL lifecycle owner is required")
    return normalized


def _same_inventory(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        int(left.get("count") or 0) == int(right.get("count") or 0)
        and str(left.get("fingerprint") or "")
        == str(right.get("fingerprint") or "")
    )


def _owner_predicate(column: Any, owner: str) -> Any:
    """Match canonical and historical mixed-case username owner keys."""
    return func.lower(func.trim(column)) == owner


class CommonSqlOwnerLifecycle:
    """Inventory and converge every mutable username key on the common Base.

    ``session_factory`` must return a SQLAlchemy ORM Session.  The adapter owns
    and closes each injected session.  Authentication identity and operation
    barrier rows are never discovered or mutated here.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        base: Any = Base,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        self._session_factory = session_factory
        self._base = base
        self.bindings = self._discover_bindings(base)
        self.dependents = tuple(
            dependent
            for dependent in _DEPENDENT_BINDINGS
            if dependent.model.__table__.metadata is base.metadata
        )
        if not self.bindings:
            raise SqlOwnerLifecycleError("common SQL owner registry is empty")

    @staticmethod
    def _discover_bindings(base: Any) -> tuple[SqlOwnerBinding, ...]:
        bindings: list[SqlOwnerBinding] = []
        for mapper in base.registry.mappers:
            model = mapper.class_
            table = mapper.local_table
            if table.metadata is not base.metadata:
                continue
            if table.name in _IMMUTABLE_BARRIER_TABLES:
                continue
            column_name = _SPECIAL_OWNER_COLUMNS.get(table.name)
            if column_name is None and "owner" in table.c:
                column_name = "owner"
            if column_name is None:
                continue
            if column_name not in table.c:
                raise SqlOwnerLifecycleError(
                    f"owner registry column is missing for {table.name}"
                )
            bindings.append(SqlOwnerBinding(model, column_name))
        return tuple(sorted(bindings, key=lambda item: item.key))

    @property
    def scope_keys(self) -> tuple[str, ...]:
        """Return the deterministic, content-free registry covered by the adapter."""
        return tuple(binding.key for binding in self.bindings)

    @property
    def dependent_scope_keys(self) -> tuple[str, ...]:
        """Return owner-derived rows included in inventory and purge closure."""
        return tuple(dependent.key for dependent in self.dependents)

    @staticmethod
    def _identity_rows(query: Any) -> list[tuple[Any, ...]]:
        rows = [tuple(row) for row in query.all()]
        return sorted(
            rows,
            key=lambda row: json.dumps(
                [str(value) if value is not None else None for value in row],
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _identity_fingerprint(identities: list[tuple[Any, ...]] | list[list[Any]]) -> str:
        normalized = [
            [str(value) if value is not None else None for value in row]
            for row in identities
        ]
        material = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _dependent_query(db: Any, dependent: SqlOwnerDependent, owner: str) -> Any:
        primary_keys = [
            getattr(dependent.model, column.name)
            for column in dependent.model.__table__.primary_key.columns
        ]
        query = db.query(*primary_keys)
        for child, child_column, parent, parent_column in dependent.joins:
            query = query.join(
                parent,
                getattr(child, child_column) == getattr(parent, parent_column),
            )
        return query.filter(
            _owner_predicate(
                getattr(dependent.root_model, dependent.root_owner_column),
                owner,
            )
        ).distinct()

    def _inventory(self, db: Any, owner: str) -> dict[str, Any]:
        counts: dict[str, int] = {}
        fingerprints: dict[str, str] = {}
        for binding in self.bindings:
            column = getattr(binding.model, binding.column_name)
            identity_columns = [
                table_column
                for table_column in binding.model.__table__.primary_key.columns
                if table_column.name != binding.column_name
            ]
            if identity_columns:
                rows = db.query(*identity_columns).filter(
                    _owner_predicate(column, owner)
                ).all()
                identities = sorted(
                    [str(value) if value is not None else None for value in row]
                    for row in rows
                )
                count = len(rows)
            else:
                count = int(
                    db.query(func.count())
                    .select_from(binding.model)
                    .filter(_owner_predicate(column, owner))
                    .scalar()
                    or 0
                )
                # A few singleton tables use the owner itself as their only
                # primary key.  Cardinality is the only content-free identity
                # those schemas expose.
                identities = [[] for _ in range(count)]
            counts[binding.key] = count
            fingerprints[binding.key] = self._identity_fingerprint(identities)
        for dependent in self.dependents:
            identities = self._identity_rows(
                self._dependent_query(db, dependent, owner)
            )
            counts[dependent.key] = len(identities)
            fingerprints[dependent.key] = self._identity_fingerprint(identities)
        material = json.dumps(
            {"counts": counts, "fingerprints": fingerprints},
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "schema_version": 1,
            "owner": owner,
            "count": sum(counts.values()),
            "tables": counts,
            "table_fingerprints": fingerprints,
            # Deliberately derived from table names, counts, and stable primary
            # row identities with the mutable owner key removed.  No row
            # content, credentials, user text, or hashes of content enter
            # lifecycle receipts.
            "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        owner = _owner(owner)
        db = self._session_factory()
        try:
            return self._inventory(db, owner)
        finally:
            db.close()

    @staticmethod
    def _require_expected(
        observed: Mapping[str, Any],
        expected: Mapping[str, Any] | None,
        *,
        label: str,
        other: Mapping[str, Any] | None = None,
    ) -> None:
        if expected is not None and not _same_inventory(observed, expected):
            raise SqlOwnerLifecycleConflict(
                f"{label} SQL owner inventory changed",
                source=observed,
                target=other,
            )

    def _move(
        self,
        source_owner: str,
        target_owner: str,
        *,
        action: str,
        reconcile: bool,
        expected_source: Mapping[str, Any] | None,
    ) -> SqlOwnerLifecycleReceipt:
        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        db = self._session_factory()
        try:
            source_before = self._inventory(db, source_owner)
            target_before = self._inventory(db, target_owner)
            if source_owner == target_owner:
                return SqlOwnerLifecycleReceipt(
                    action,
                    "unchanged",
                    source_owner,
                    target_owner,
                    {},
                    source_before,
                    target_before,
                    source_before,
                    target_before,
                )

            source_count = int(source_before["count"])
            target_count = int(target_before["count"])
            if source_count and target_count:
                raise SqlOwnerLifecycleConflict(
                    "source and target SQL owners both contain state",
                    source=source_before,
                    target=target_before,
                )

            if not source_count:
                if not reconcile and target_count:
                    raise SqlOwnerLifecycleConflict(
                        "target SQL owner already contains state",
                        source=source_before,
                        target=target_before,
                    )
                if expected_source is not None:
                    if target_count:
                        self._require_expected(
                            target_before,
                            expected_source,
                            label="reconciled target",
                            other=source_before,
                        )
                    else:
                        self._require_expected(
                            source_before,
                            expected_source,
                            label="missing source",
                            other=target_before,
                        )
                state = "already_applied" if target_count else "empty"
                return SqlOwnerLifecycleReceipt(
                    action,
                    state,
                    source_owner,
                    target_owner,
                    {},
                    source_before,
                    target_before,
                    source_before,
                    target_before,
                )

            self._require_expected(
                source_before,
                expected_source,
                label="source",
                other=target_before,
            )
            changed: dict[str, int] = {}
            for binding in self.bindings:
                column = getattr(binding.model, binding.column_name)
                count = int(
                    db.query(binding.model)
                    .filter(_owner_predicate(column, source_owner))
                    .update({column: target_owner}, synchronize_session=False)
                    or 0
                )
                if count:
                    changed[binding.key] = count
            db.flush()
            source_after = self._inventory(db, source_owner)
            target_after = self._inventory(db, target_owner)
            if int(source_after["count"]):
                raise SqlOwnerLifecycleError("source SQL owner remains after staging")
            if not _same_inventory(target_after, source_before):
                raise SqlOwnerLifecycleError("staged SQL owner inventory does not reconcile")
            db.commit()
            return SqlOwnerLifecycleReceipt(
                action,
                "applied",
                source_owner,
                target_owner,
                changed,
                source_before,
                target_before,
                source_after,
                target_after,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def stage_owner(
        self,
        source_owner: str,
        target_owner: str,
        *,
        expected_source: Mapping[str, Any] | None = None,
    ) -> SqlOwnerLifecycleReceipt:
        """Atomically move a source owner to a rename key or tombstone."""
        return self._move(
            source_owner,
            target_owner,
            action="stage",
            reconcile=False,
            expected_source=expected_source,
        )

    rename_owner = stage_owner

    def reconcile_owner(
        self,
        source_owner: str,
        target_owner: str,
        *,
        expected_source: Mapping[str, Any] | None = None,
    ) -> SqlOwnerLifecycleReceipt:
        """Resume a stage, accepting only an exact already-applied inventory."""
        return self._move(
            source_owner,
            target_owner,
            action="reconcile",
            reconcile=True,
            expected_source=expected_source,
        )

    reconcile_rename = reconcile_owner

    def compensate_owner(
        self,
        source_owner: str,
        staged_owner: str,
        *,
        expected_source: Mapping[str, Any] | None = None,
    ) -> SqlOwnerLifecycleReceipt:
        """Idempotently restore an exact staged/tombstoned inventory."""
        return self._move(
            staged_owner,
            source_owner,
            action="compensate",
            reconcile=True,
            expected_source=expected_source,
        )

    def purge_owner(
        self,
        owner: str,
        *,
        expected_inventory: Mapping[str, Any] | None = None,
    ) -> SqlOwnerLifecycleReceipt:
        """Atomically purge one exact owner inventory; retries are empty no-ops."""
        owner = _owner(owner)
        db = self._session_factory()
        try:
            before = self._inventory(db, owner)
            if expected_inventory is not None and int(before["count"]):
                self._require_expected(
                    before,
                    expected_inventory,
                    label="purge source",
                )
            changed: dict[str, int] = {}
            # Delete every owner-derived row explicitly, deepest first.  This
            # is deliberate even where production FKs also cascade: imported
            # SQLite databases may have enforcement disabled, and cross-owner
            # share subscriptions must not survive their source share.
            for dependent in sorted(
                self.dependents,
                key=lambda item: (-item.depth, item.key),
            ):
                identities = self._identity_rows(
                    self._dependent_query(db, dependent, owner)
                )
                if not identities:
                    continue
                primary_keys = [
                    getattr(dependent.model, column.name)
                    for column in dependent.model.__table__.primary_key.columns
                ]
                if len(primary_keys) == 1:
                    predicate = primary_keys[0].in_([row[0] for row in identities])
                else:
                    predicate = tuple_(*primary_keys).in_(
                        [tuple(row) for row in identities]
                    )
                count = int(
                    db.query(dependent.model)
                    .filter(predicate)
                    .delete(synchronize_session=False)
                    or 0
                )
                if count:
                    changed[dependent.key] = count
            # Delete dependent subscription rows before their share parents.
            ordered = sorted(
                self.bindings,
                key=lambda item: (
                    item.model.__table__.name != "model_share_subscriptions",
                    item.key,
                ),
            )
            for binding in ordered:
                column = getattr(binding.model, binding.column_name)
                count = int(
                    db.query(binding.model)
                    .filter(_owner_predicate(column, owner))
                    .delete(synchronize_session=False)
                    or 0
                )
                if count:
                    changed[binding.key] = count
            db.flush()
            after = self._inventory(db, owner)
            if int(after["count"]):
                raise SqlOwnerLifecycleError("SQL owner remains after purge")
            db.commit()
            return SqlOwnerLifecycleReceipt(
                "purge",
                "applied" if changed else "already_applied",
                owner,
                None,
                changed,
                before,
                None,
                after,
                None,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def verify_owner_absent(self, owner: str) -> dict[str, Any]:
        """Return an empty inventory or fail closed with content-free evidence."""
        inventory = self.owner_inventory(owner)
        if int(inventory["count"]):
            raise SqlOwnerLifecycleConflict(
                "SQL owner still contains state",
                source=inventory,
            )
        return inventory

    def verify_staged(
        self,
        source_owner: str,
        target_owner: str,
        *,
        expected_source: Mapping[str, Any],
    ) -> dict[str, Mapping[str, Any]]:
        """Verify source absence and an exact count-derived target fingerprint."""
        source = self.verify_owner_absent(source_owner)
        target = self.owner_inventory(target_owner)
        self._require_expected(
            target,
            expected_source,
            label="staged target",
            other=source,
        )
        return {"source": source, "target": target}


__all__ = [
    "CommonSqlOwnerLifecycle",
    "SqlOwnerBinding",
    "SqlOwnerLifecycleConflict",
    "SqlOwnerLifecycleError",
    "SqlOwnerLifecycleReceipt",
]
