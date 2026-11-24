"""Current native sidecars for tests that intentionally hand-build other tables.

Read the shipped schema, not private migration tooling or duplicated permissive
DDL. This only initializes synthetic test stores; runtime still refuses legacy
stores and does not create or migrate sidecars implicitly.
"""
from pathlib import Path
import sqlite3


def install_current_sidecars(conn: sqlite3.Connection, *, extra=()) -> None:
    schema = (Path(__file__).resolve().parents[1]
              / "native/memery/crates/memery-core/src/store/current-schema.sql")
    names = ("fm_v2_trust_assignments", "fm_v2_job_manifests", *extra)
    with sqlite3.connect(":memory:") as canonical:
        canonical.executescript(schema.read_text(encoding="utf-8"))
        for name in names:
            statement = canonical.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            assert statement and statement[0], f"Native schema lacks {name}"
            conn.execute(statement[0])
            for (index_sql,) in canonical.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                (name,),
            ):
                conn.execute(index_sql)
