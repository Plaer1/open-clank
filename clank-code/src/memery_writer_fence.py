"""Inert, versioned writer capability registration for compatible connections.

No schema guards or migration are installed here. The capability is a local
compatibility marker, not a credential or security boundary.
"""
from __future__ import annotations

import sqlite3
from enum import Enum

WRITER_CAPABILITY = "openclank.frankenmemory.writer.v1"
WRITER_FUNCTION = "fm_writer_capability"


class RegistrationStatus(Enum):
    REGISTERED = "registered"
    TRUSTED_SCHEMA_DISABLED = "trusted_schema_disabled"


def register_writer_capability(connection: sqlite3.Connection) -> RegistrationStatus:
    """Register this exact implementation epoch without changing SQLite policy.

    Python cannot mark a SQL function innocuous. On trusted_schema=OFF the
    guard cannot safely use it: leave the pragma unchanged and report that
    writes requiring this capability are unsupported. This is inert on v16;
    a future activation must explicitly surface this status before writes.
    """
    row = connection.execute("PRAGMA trusted_schema").fetchone()
    if row is None or row[0] != 1:
        return RegistrationStatus.TRUSTED_SCHEMA_DISABLED
    connection.create_function(WRITER_FUNCTION, 0, lambda: WRITER_CAPABILITY, deterministic=True)
    return RegistrationStatus.REGISTERED


def connect_writer(database: str, **kwargs) -> sqlite3.Connection:
    """Open a writable candidate with the same sqlite3 options and factory."""
    connection = sqlite3.connect(database, **kwargs)
    try:
        register_writer_capability(connection)
    except BaseException:
        connection.close()
        raise
    return connection
