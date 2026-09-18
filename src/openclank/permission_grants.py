"""C1 — durable permission grants.

Stores 'Always allow' choices in the odysseus app.db so approvals survive
restarts (mimo's own permission memory resets per launch). One tiny table,
stdlib sqlite3 — the store is consulted from PermissionHandler.handle()
between the safe-dirs check and the human prompt.

Grant semantics (e's rulings 2026-07-09):
- requests carrying a filepath: pattern is the file's directory; a grant
  covers the whole subtree (match on directory boundary, not raw prefix).
- everything else: pattern '*' covers the whole permission type.
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS permission_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    permission_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    workspace TEXT NOT NULL DEFAULT '',
    workspace_id TEXT NOT NULL DEFAULT '',
    resource TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    revoked_at TEXT,
    UNIQUE (
        owner, session_id, permission_type, pattern, workspace, workspace_id,
        resource
    )
)
"""

_SCOPED_UNIQUE_COLUMNS = (
    "owner",
    "session_id",
    "permission_type",
    "pattern",
    "workspace",
    "workspace_id",
    "resource",
)


def derive_pattern(raw_input: Optional[dict]) -> str:
    """Grant pattern for a request: file dir for file requests, else '*'."""
    if isinstance(raw_input, dict):
        filepath = raw_input.get("filepath")
        if isinstance(filepath, str) and filepath:
            return os.path.dirname(filepath) or filepath
    return "*"


def grant_scope_for_lifetime(
    lifetime: str,
    *,
    session_id: str = "",
    workspace: str = "",
    workspace_id: str = "",
) -> tuple[str, str, str]:
    """Map the human-facing lifetime to durable matcher dimensions.

    ``once`` is intentionally not persisted. Chat grants bind to one stable
    session, Workspace grants bind across chats to the selected workspace, and
    Always grants have neither dimension. This keeps the labels honest instead
    of storing the old session+workspace combination under “Always”.
    """
    lifetime = str(lifetime or "").strip().lower()
    if lifetime == "chat":
        # A chat-scoped approval without a stable chat identity cannot be
        # safely persisted; callers treat the empty pair as Once.
        if not session_id:
            return "", "", ""
        return str(session_id), "", str(workspace_id or "")
    if lifetime == "workspace":
        # A filesystem path is mutable and can be rebound to a different
        # Location. New Workspace approvals therefore require the stable,
        # owner-scoped policy identity and degrade to Once without it.
        if not workspace_id:
            return "", "", ""
        return "", "", str(workspace_id)
    if lifetime == "always":
        return "", "", ""
    return "", "", ""


class GrantStore:
    """Durable (permission_type, pattern) grants in a SQLite db file."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._connect() as conn:
            try:
                # The lock above is instance-local; multiple app workers can
                # initialize this store concurrently. Serialize schema
                # inspection and any rename/create/copy/drop migration at the
                # SQLite boundary, then re-read the live schema while holding
                # the write reservation.
                conn.execute("BEGIN IMMEDIATE")
                self._ensure_schema(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(_SCHEMA)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(permission_grants)")
        }
        required = {
            "owner", "session_id", "workspace", "workspace_id", "resource",
            "expires_at", "revoked_at",
        }
        unique_scopes = set()
        for index in conn.execute("PRAGMA index_list(permission_grants)"):
            if not bool(index[2]):
                continue
            unique_scopes.add(tuple(
                row[2]
                for row in conn.execute(
                    f"PRAGMA index_info({index[1]!r})"
                )
            ))
        if (
            required.issubset(columns)
            and _SCOPED_UNIQUE_COLUMNS in unique_scopes
        ):
            return
        conn.execute("ALTER TABLE permission_grants RENAME TO permission_grants_legacy")
        conn.execute(_SCHEMA)
        legacy_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(permission_grants_legacy)")
        }
        modern_legacy = {
            "owner",
            "session_id",
            "workspace",
            "resource",
            "expires_at",
            "revoked_at",
        }.issubset(legacy_columns)

        def source(column: str, fallback: str) -> str:
            return column if column in legacy_columns else fallback

        # Preserve modern rows byte-for-byte while adding an empty stable
        # identity. Ancient grants lacked enough ownership/scope dimensions to
        # prove their authority, so those rows remain visible for audit but are
        # imported soft-revoked. No migration guesses a Workspace from a path.
        revoked_source = "datetime('now')"
        if modern_legacy:
            revoked_source = f"""
                CASE
                    WHEN revoked_at IS NULL
                     AND {source('workspace', "''")} <> ''
                     AND {source('session_id', "''")} = ''
                     AND {source('workspace_id', "''")} = ''
                    THEN datetime('now')
                    ELSE revoked_at
                END
            """
        conn.execute(
            f"""
            INSERT INTO permission_grants
                (id, owner, session_id, permission_type, pattern, workspace,
                 workspace_id, resource, created_at, expires_at, revoked_at)
            SELECT
                {source('id', 'NULL')},
                {source('owner', "''")},
                {source('session_id', "''")},
                permission_type,
                pattern,
                {source('workspace', "''")},
                {source('workspace_id', "''")},
                {source('resource', "''")},
                {source('created_at', "datetime('now')")},
                {source('expires_at', 'NULL')},
                {revoked_source}
            FROM permission_grants_legacy
            """
        )
        conn.execute("DROP TABLE permission_grants_legacy")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=5.0)

    def add(
        self,
        permission_type: str,
        pattern: str,
        *,
        owner: str = "",
        session_id: str = "",
        workspace: str = "",
        workspace_id: str = "",
        resource: str = "",
        expires_at: Optional[str] = None,
    ) -> None:
        if workspace and workspace_id:
            raise ValueError(
                "a grant cannot bind both a legacy path and a stable Workspace ID"
            )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO permission_grants
                    (owner, session_id, permission_type, pattern, workspace,
                     workspace_id, resource, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, NULL)
                ON CONFLICT(
                    owner, session_id, permission_type, pattern, workspace,
                    workspace_id, resource
                ) DO UPDATE SET
                    created_at = datetime('now'),
                    expires_at = excluded.expires_at,
                    revoked_at = NULL
                """,
                (
                    owner,
                    session_id,
                    permission_type,
                    pattern,
                    workspace,
                    workspace_id,
                    resource,
                    expires_at,
                ),
            )

    def match(
        self,
        permission_type: str,
        filepath: Optional[str] = None,
        *,
        owner: str = "",
        session_id: str = "",
        workspace: str = "",
        workspace_id: str = "",
        resource: str = "",
    ) -> bool:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT pattern, session_id, workspace, workspace_id, resource,
                       expires_at
                FROM permission_grants
                WHERE owner = ? AND permission_type = ? AND revoked_at IS NULL
                """,
                (owner, permission_type),
            ).fetchall()
        now = datetime.now(timezone.utc)
        for (
            pattern,
            grant_session,
            grant_workspace,
            grant_workspace_id,
            grant_resource,
            expires_at,
        ) in rows:
            if grant_session and grant_session != session_id:
                continue
            if grant_workspace and grant_workspace != workspace:
                continue
            # A stable caller never inherits a legacy path grant merely
            # because a different Workspace later resolves to the same path.
            # Legacy rows remain usable only on the measured unbound lane.
            if workspace_id and grant_workspace and not grant_workspace_id:
                continue
            if grant_workspace_id and grant_workspace_id != workspace_id:
                continue
            if grant_resource and grant_resource != resource:
                continue
            if expires_at:
                try:
                    expiry = datetime.fromisoformat(expires_at)
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    if expiry <= now:
                        continue
                except ValueError:
                    continue
            if pattern == "*":
                return True
            if filepath and (filepath == pattern or filepath.startswith(pattern + "/")):
                return True
        return False

    def list(self, *, owner: Optional[str] = None) -> list:
        with self._lock, self._connect() as conn:
            if owner is None:
                return conn.execute(
                    "SELECT permission_type, pattern, created_at FROM permission_grants WHERE revoked_at IS NULL ORDER BY id"
                ).fetchall()
            return conn.execute(
                "SELECT permission_type, pattern, created_at FROM permission_grants WHERE owner = ? AND revoked_at IS NULL ORDER BY id",
                (owner,),
            ).fetchall()

    def remove(self, permission_type: str, pattern: str, *, owner: str = "") -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE permission_grants SET revoked_at = datetime('now') WHERE owner = ? AND permission_type = ? AND pattern = ? AND revoked_at IS NULL",
                (owner, permission_type, pattern),
            )
            return cur.rowcount > 0

    def list_records(self, *, owner: str) -> list[dict]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, owner, session_id, permission_type, pattern,
                       workspace, workspace_id, resource, created_at, expires_at
                FROM permission_grants
                WHERE owner = ? AND revoked_at IS NULL
                ORDER BY id
                """,
                (owner,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, grant_id: int, *, owner: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE permission_grants SET revoked_at = datetime('now') WHERE id = ? AND owner = ? AND revoked_at IS NULL",
                (grant_id, owner),
            )
            return cur.rowcount > 0

    def revoke_scope(
        self,
        *,
        owner: str,
        session_id: str = "",
        workspace: str = "",
        workspace_id: str = "",
    ) -> int:
        """Soft-revoke grants bound to one chat/workspace scope.

        A workspace reset also removes descendant chat grants by matching the
        stored workspace dimension. Passing neither scope is intentionally
        rejected; the account-wide reset uses the existing per-record revoke
        route so the UI can report exactly what it changed.
        """
        if not session_id and not workspace and not workspace_id:
            raise ValueError("a session or workspace scope is required")
        clauses = ["owner = ?", "revoked_at IS NULL"]
        params: list[str] = [str(owner)]
        if session_id:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if workspace and workspace_id:
            clauses.append("(workspace_id = ? OR (workspace_id = '' AND workspace = ?))")
            params.extend((str(workspace_id), str(workspace)))
        elif workspace:
            clauses.append("workspace = ?")
            params.append(str(workspace))
        elif workspace_id:
            clauses.append("workspace_id = ?")
            params.append(str(workspace_id))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE permission_grants SET revoked_at = datetime('now') WHERE "
                + " AND ".join(clauses),
                params,
            )
            return int(cur.rowcount or 0)

    @staticmethod
    def _path_within(root: str, target: str) -> bool:
        try:
            normalized_root = os.path.normcase(os.path.abspath(str(root)))
            normalized_target = os.path.normcase(os.path.abspath(str(target)))
            return os.path.commonpath(
                [normalized_root, normalized_target]
            ) == normalized_root
        except (OSError, ValueError):
            return False

    @classmethod
    def _matches_agent_reset(
        cls,
        row: sqlite3.Row,
        *,
        scope: str,
        chat_id: str,
        workspace_id: str,
        legacy_workspace: str,
        location_workspace_ids: set[str],
        legacy_location_path: str,
    ) -> bool:
        if scope == "all_agent":
            return True
        if scope == "chat":
            return bool(chat_id and row["session_id"] == chat_id)
        if scope == "workspace":
            return bool(
                (workspace_id and row["workspace_id"] == workspace_id)
                or (
                    legacy_workspace
                    and row["workspace"] == legacy_workspace
                )
            )
        if scope == "location":
            if row["workspace_id"] in location_workspace_ids:
                return True
            return bool(
                legacy_location_path
                and row["workspace"]
                and cls._path_within(
                    legacy_location_path,
                    row["workspace"],
                )
            )
        raise ValueError("unsupported compatibility reset scope")

    @classmethod
    def _agent_reset_ids_from_connection(
        cls,
        connection: sqlite3.Connection,
        *,
        owner: str,
        scope: str,
        chat_id: str = "",
        workspace_id: str = "",
        legacy_workspace: str = "",
        location_workspace_ids: tuple[str, ...] = (),
        legacy_location_path: str = "",
    ) -> list[int]:
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"chat", "workspace", "location", "all_agent"}:
            raise ValueError("unsupported compatibility reset scope")
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id,session_id,workspace,workspace_id
            FROM permission_grants
            WHERE owner=? AND revoked_at IS NULL
            ORDER BY id
            """,
            (str(owner),),
        ).fetchall()
        workspace_ids = {
            str(value)
            for value in location_workspace_ids
            if str(value)
        }
        return [
            int(row["id"])
            for row in rows
            if cls._matches_agent_reset(
                row,
                scope=normalized_scope,
                chat_id=str(chat_id or ""),
                workspace_id=str(workspace_id or ""),
                legacy_workspace=str(legacy_workspace or ""),
                location_workspace_ids=workspace_ids,
                legacy_location_path=str(legacy_location_path or ""),
            )
        ]

    def preview_agent_reset(self, **scope) -> int:
        """Count compatibility approvals selected by a canonical reset."""
        with self._lock, self._connect() as connection:
            return len(self._agent_reset_ids_from_connection(connection, **scope))

    def reset_agent_permissions(self, **scope) -> int:
        """Soft-revoke compatibility approvals selected by one reset domain."""
        with self._lock, self._connect() as connection:
            try:
                # Keep selection and revocation under one write reservation so
                # concurrent workers cannot insert a matching approval between
                # the two steps of a reset.
                connection.execute("BEGIN IMMEDIATE")
                ids = self._agent_reset_ids_from_connection(connection, **scope)
                if not ids:
                    connection.rollback()
                    return 0
                placeholders = ",".join("?" for _ in ids)
                cursor = connection.execute(
                    "UPDATE permission_grants SET revoked_at=datetime('now') "
                    f"WHERE owner=? AND revoked_at IS NULL AND id IN ({placeholders})",
                    (str(scope.get("owner") or ""), *ids),
                )
                connection.commit()
                return int(cursor.rowcount or 0)
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _empty_owner_inventory(owner: str) -> dict:
        material = json.dumps([], separators=(",", ":"))
        return {
            "schema_version": 1,
            "owner": str(owner),
            "count": 0,
            "active": 0,
            "revoked": 0,
            "fingerprint": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
            "content_included": False,
        }

    @staticmethod
    def _inventory_matches(actual: dict, expected: dict) -> bool:
        return all(
            actual.get(key) == expected.get(key)
            for key in ("count", "active", "revoked", "fingerprint")
        )

    @classmethod
    def _validate_owner_inventory(cls, inventory: dict, owner: str, *, label: str) -> None:
        counts = (
            inventory.get("count"),
            inventory.get("active"),
            inventory.get("revoked"),
        ) if isinstance(inventory, dict) else ()
        fingerprint = inventory.get("fingerprint") if isinstance(inventory, dict) else None
        if (
            not isinstance(inventory, dict)
            or inventory.get("schema_version") != 1
            or inventory.get("owner") != owner
            or inventory.get("content_included") is not False
            or len(counts) != 3
            or any(type(value) is not int or value < 0 for value in counts)
            or counts[1] + counts[2] != counts[0]
            or not isinstance(fingerprint, str)
            or not fingerprint.startswith("sha256:")
            or len(fingerprint.removeprefix("sha256:")) != 64
            or any(
                character not in "0123456789abcdef"
                for character in fingerprint.removeprefix("sha256:")
            )
            or (
                counts[0] == 0
                and not cls._inventory_matches(
                    inventory,
                    cls._empty_owner_inventory(owner),
                )
            )
        ):
            raise RuntimeError(f"invalid {label} permission-grant inventory")

    @classmethod
    def _validate_owner_rename_manifest(
        cls,
        manifest: dict,
        old_owner: str,
        new_owner: str,
    ) -> tuple[dict, dict]:
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("content_included") is not False
        ):
            raise RuntimeError("invalid permission-grant lifecycle manifest")
        source = dict(manifest.get("source") or {})
        target = dict(manifest.get("target") or {})
        cls._validate_owner_inventory(source, old_owner, label="source")
        cls._validate_owner_inventory(target, new_owner, label="target")
        if not cls._inventory_matches(
            target,
            cls._empty_owner_inventory(new_owner),
        ):
            raise RuntimeError("invalid permission-grant lifecycle manifest")
        return source, target

    @classmethod
    def _owner_inventory_from_connection(
        cls,
        connection: sqlite3.Connection,
        owner: str,
    ) -> dict:
        normalized = str(owner or "").strip().lower()
        rows = connection.execute(
            """
            SELECT id,session_id,permission_type,pattern,workspace,workspace_id,
                   resource,created_at,expires_at,revoked_at
            FROM permission_grants
            WHERE lower(trim(owner))=?
            ORDER BY id
            """,
            (normalized,),
        ).fetchall()
        if not rows:
            return cls._empty_owner_inventory(normalized)
        # Receipt material is never returned. Hashing the complete durable row
        # shape makes the lifecycle CAS sensitive to concurrent grant changes
        # without exposing paths, permission patterns, or timestamps.
        encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str)
        revoked = sum(1 for row in rows if row[-1] is not None)
        return {
            "schema_version": 1,
            "owner": normalized,
            "count": len(rows),
            "active": len(rows) - revoked,
            "revoked": revoked,
            "fingerprint": "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "content_included": False,
        }

    def owner_inventory(self, owner: str) -> dict:
        """Return a content-free inventory of active and revoked owner rows."""
        with self._lock, self._connect() as connection:
            return self._owner_inventory_from_connection(connection, owner)

    def preview_owner_rename(self, old_owner: str, new_owner: str) -> dict:
        old_key = str(old_owner or "").strip().lower()
        new_key = str(new_owner or "").strip().lower()
        if not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct permission-grant owners are required")
        with self._lock, self._connect() as connection:
            source = self._owner_inventory_from_connection(connection, old_key)
            target = self._owner_inventory_from_connection(connection, new_key)
        if source["count"] and target["count"]:
            raise RuntimeError("target permission-grant owner already contains state")
        return {
            "schema_version": 1,
            "source": source,
            "target": target,
            "content_included": False,
        }

    def reconcile_owner_rename(
        self,
        old_owner: str,
        new_owner: str,
        manifest: dict,
    ) -> dict:
        old_key = str(old_owner or "").strip().lower()
        new_key = str(new_owner or "").strip().lower()
        if not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct permission-grant owners are required")
        expected, _expected_target = self._validate_owner_rename_manifest(
            manifest,
            old_key,
            new_key,
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                source = self._owner_inventory_from_connection(connection, old_key)
                target = self._owner_inventory_from_connection(connection, new_key)
                empty_source = self._empty_owner_inventory(old_key)
                if int(expected.get("count") or 0) == 0:
                    if source["count"] or target["count"]:
                        raise RuntimeError("permission-grant owner state changed after preflight")
                    state = "empty"
                elif self._inventory_matches(source, expected) and target["count"] == 0:
                    connection.execute(
                        "UPDATE permission_grants SET owner=? WHERE lower(trim(owner))=?",
                        (new_key, old_key),
                    )
                    state = "applied"
                elif source["count"] == 0 and self._inventory_matches(target, expected):
                    state = "already_applied"
                else:
                    raise RuntimeError("permission-grant owner state changed after preflight")
                source_after = self._owner_inventory_from_connection(connection, old_key)
                target_after = self._owner_inventory_from_connection(connection, new_key)
                if source_after["count"] or (
                    int(expected.get("count") or 0)
                    and not self._inventory_matches(target_after, expected)
                ):
                    raise RuntimeError("permission-grant owner rename did not converge")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "schema_version": 1,
            "state": state,
            "source": source_after,
            "target": target_after,
            "content_included": False,
        }

    def compensate_owner_rename(
        self,
        old_owner: str,
        new_owner: str,
        manifest: dict,
    ) -> dict:
        """Restore either a complete or interrupted owner move to its source."""
        old_key = str(old_owner or "").strip().lower()
        new_key = str(new_owner or "").strip().lower()
        if not old_key or not new_key or old_key == new_key:
            raise RuntimeError("distinct permission-grant owners are required")
        expected, _expected_target = self._validate_owner_rename_manifest(
            manifest,
            old_key,
            new_key,
        )
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                source = self._owner_inventory_from_connection(connection, old_key)
                target = self._owner_inventory_from_connection(connection, new_key)
                if int(expected.get("count") or 0) == 0:
                    if source["count"] or target["count"]:
                        raise RuntimeError("permission-grant compensation found unrelated state")
                    state = "empty"
                elif self._inventory_matches(source, expected) and target["count"] == 0:
                    state = "already_compensated"
                elif source["count"] == 0 and self._inventory_matches(target, expected):
                    connection.execute(
                        "UPDATE permission_grants SET owner=? WHERE lower(trim(owner))=?",
                        (old_key, new_key),
                    )
                    state = "compensated"
                else:
                    raise RuntimeError("permission-grant compensation found split state")
                source_after = self._owner_inventory_from_connection(connection, old_key)
                target_after = self._owner_inventory_from_connection(connection, new_key)
                if target_after["count"] or (
                    int(expected.get("count") or 0)
                    and not self._inventory_matches(source_after, expected)
                ):
                    raise RuntimeError("permission-grant compensation did not converge")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "schema_version": 1,
            "state": state,
            "source": source_after,
            "target": target_after,
            "content_included": False,
        }

    def purge_owner_lifecycle(self, owner: str, *, expected: dict | None = None) -> dict:
        key = str(owner or "").strip().lower()
        if expected is not None:
            self._validate_owner_inventory(expected, key, label="purge")
        with self._lock, self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                before = self._owner_inventory_from_connection(connection, key)
                if expected is not None and before["count"]:
                    if not self._inventory_matches(before, expected):
                        raise RuntimeError("permission-grant owner state changed before purge")
                elif expected is not None and not before["count"]:
                    state = "already_applied"
                else:
                    state = "applied" if before["count"] else "empty"
                if before["count"]:
                    connection.execute(
                        "DELETE FROM permission_grants WHERE lower(trim(owner))=?",
                        (key,),
                    )
                    state = "applied"
                after = self._owner_inventory_from_connection(connection, key)
                if after["count"]:
                    raise RuntimeError("permission-grant purge did not converge")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "schema_version": 1,
            "state": state,
            "before": before,
            "after": after,
            "content_included": False,
        }

    def rename_owner(self, old_owner: str, new_owner: str) -> None:
        manifest = self.preview_owner_rename(old_owner, new_owner)
        self.reconcile_owner_rename(old_owner, new_owner, manifest)

    def purge_owner(self, owner: str) -> None:
        self.purge_owner_lifecycle(owner, expected=self.owner_inventory(owner))
