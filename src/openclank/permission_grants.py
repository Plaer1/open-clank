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
    resource TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    revoked_at TEXT,
    UNIQUE (owner, session_id, permission_type, pattern, workspace, resource)
)
"""


def derive_pattern(raw_input: Optional[dict]) -> str:
    """Grant pattern for a request: file dir for file requests, else '*'."""
    if isinstance(raw_input, dict):
        filepath = raw_input.get("filepath")
        if isinstance(filepath, str) and filepath:
            return os.path.dirname(filepath) or filepath
    return "*"


class GrantStore:
    """Durable (permission_type, pattern) grants in a SQLite db file."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        with self._connect() as conn:
            self._ensure_schema(conn)

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(_SCHEMA)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(permission_grants)")
        }
        required = {
            "owner", "session_id", "workspace", "resource",
            "expires_at", "revoked_at",
        }
        if required.issubset(columns):
            return
        conn.execute("ALTER TABLE permission_grants RENAME TO permission_grants_legacy")
        conn.execute(_SCHEMA)
        conn.execute(
            """
            INSERT INTO permission_grants
                (permission_type, pattern, created_at, revoked_at)
            SELECT permission_type, pattern, created_at, datetime('now')
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
        resource: str = "",
        expires_at: Optional[str] = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO permission_grants
                    (owner, session_id, permission_type, pattern, workspace,
                     resource, created_at, expires_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, NULL)
                """,
                (owner, session_id, permission_type, pattern, workspace, resource, expires_at),
            )

    def match(
        self,
        permission_type: str,
        filepath: Optional[str] = None,
        *,
        owner: str = "",
        session_id: str = "",
        workspace: str = "",
        resource: str = "",
    ) -> bool:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT pattern, session_id, workspace, resource, expires_at
                FROM permission_grants
                WHERE owner = ? AND permission_type = ? AND revoked_at IS NULL
                """,
                (owner, permission_type),
            ).fetchall()
        now = datetime.now(timezone.utc)
        for pattern, grant_session, grant_workspace, grant_resource, expires_at in rows:
            if grant_session and grant_session != session_id:
                continue
            if grant_workspace and grant_workspace != workspace:
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
                       workspace, resource, created_at, expires_at
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

    def rename_owner(self, old_owner: str, new_owner: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE permission_grants SET owner = ? WHERE owner = ?",
                (new_owner, old_owner),
            )

    def purge_owner(self, owner: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM permission_grants WHERE owner = ?", (owner,))
