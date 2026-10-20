"""Native current-schema admission before any Python memory projection."""
import json
import os
import sqlite3
import subprocess
from pathlib import Path


def prepare_frankenmemory_database(database_path: str | None = None) -> str:
    """Initialize a fresh store or admit current data using the configured native owner.

    The native executable rejects historical schemas. This entrypoint cannot
    perform conversion and does not start MCP or any embedding/model client.
    """
    if database_path is None:
        from src.constants import FM_DB_PATH
        database_path = FM_DB_PATH
    path = os.path.abspath(os.path.expanduser(database_path))
    command = os.environ.get("FM_MCP_COMMAND", "fm-mcp")
    env = dict(os.environ, FM_DB_PATH=path)
    result = subprocess.run(
        [command, "--prepare-database"], env=env, capture_output=True, text=True,
        timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Frankenmemory current-schema preparation failed: {result.stderr.strip()}")
    try:
        receipt = json.loads(result.stdout)
        database_id = receipt["database_id"]
        if not isinstance(database_id, str) or not database_id:
            raise ValueError("missing database identity")
        if receipt["database_path"] != path or not isinstance(receipt["schema_version"], int):
            raise ValueError("database path/schema receipt mismatch")
        with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as connection:
            stored = connection.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if stored is None or stored[0] != database_id or version != receipt["schema_version"]:
            raise ValueError("native receipt differs from durable database identity/schema")
    except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
        raise RuntimeError("Invalid Frankenmemory current-schema preparation receipt") from error
    expected = os.environ.get("FM_DB_ID", "").strip()
    if expected and expected != database_id:
        raise RuntimeError("Frankenmemory database identity mismatch")
    os.environ["FM_DB_ID"] = database_id
    return database_id
