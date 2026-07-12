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
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS permission_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    permission_type TEXT NOT NULL,
    pattern TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (permission_type, pattern)
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
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=5.0)

    def add(self, permission_type: str, pattern: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO permission_grants (permission_type, pattern) VALUES (?, ?)",
                (permission_type, pattern),
            )

    def match(self, permission_type: str, filepath: Optional[str] = None) -> bool:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT pattern FROM permission_grants WHERE permission_type = ?",
                (permission_type,),
            ).fetchall()
        for (pattern,) in rows:
            if pattern == "*":
                return True
            if filepath and (filepath == pattern or filepath.startswith(pattern + "/")):
                return True
        return False

    def list(self) -> list:
        with self._lock, self._connect() as conn:
            return conn.execute(
                "SELECT permission_type, pattern, created_at FROM permission_grants ORDER BY id"
            ).fetchall()

    def remove(self, permission_type: str, pattern: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM permission_grants WHERE permission_type = ? AND pattern = ?",
                (permission_type, pattern),
            )
            return cur.rowcount > 0
