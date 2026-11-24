"""Canonical optional memory DDL, executed inside the caller transaction."""
from pathlib import Path
import sqlite3


def apply_optional_schema(conn: sqlite3.Connection, group: str) -> None:
    if group not in ("principal", "media"):
        raise ValueError("unknown optional memory schema")
    sql = (Path(__file__).parent / "memory_schema" / (group + ".sql")).read_text(encoding="utf-8")
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            conn.execute(pending)
            pending = ""
    if pending.strip():
        raise ValueError("incomplete optional memory schema")
