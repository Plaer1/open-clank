from __future__ import annotations

import sqlite3


def test_offline_nullable_acquisition_column_preserves_account_and_route_rows(tmp_path):
    database = tmp_path / "provider-before.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE provider_accounts (
                id TEXT PRIMARY KEY,
                credential_envelope TEXT,
                credential_version INTEGER NOT NULL,
                revision INTEGER NOT NULL
            );
            CREATE TABLE provider_model_routes (
                id TEXT PRIMARY KEY,
                provider_model_id TEXT NOT NULL,
                revision INTEGER NOT NULL
            );
            CREATE TABLE provider_account_entitlements (
                account_id TEXT NOT NULL,
                model_route_id TEXT NOT NULL,
                eligible INTEGER NOT NULL
            );
            """
        )
        sealed = bytes.fromhex("00ff1020aabb")
        connection.execute(
            "INSERT INTO provider_accounts VALUES (?, ?, ?, ?)",
            ("pac_keep", sealed.hex(), 7, 12),
        )
        connection.execute(
            "INSERT INTO provider_model_routes VALUES (?, ?, ?)",
            ("pmr_keep", "mimo-model", 5),
        )
        connection.execute(
            "INSERT INTO provider_account_entitlements VALUES (?, ?, ?)",
            ("pac_keep", "pmr_keep", 1),
        )
        before = (
            connection.execute("SELECT id, credential_envelope, credential_version, revision FROM provider_accounts").fetchall(),
            connection.execute("SELECT id, provider_model_id, revision FROM provider_model_routes").fetchall(),
            connection.execute("SELECT account_id, model_route_id, eligible FROM provider_account_entitlements").fetchall(),
        )

        # Offline conversion run against a copied database only:
        connection.execute("ALTER TABLE provider_accounts ADD COLUMN acquisition_method VARCHAR NULL")

        after = (
            connection.execute("SELECT id, credential_envelope, credential_version, revision FROM provider_accounts").fetchall(),
            connection.execute("SELECT id, provider_model_id, revision FROM provider_model_routes").fetchall(),
            connection.execute("SELECT account_id, model_route_id, eligible FROM provider_account_entitlements").fetchall(),
        )
        assert after == before
        assert connection.execute("SELECT acquisition_method FROM provider_accounts WHERE id = ?", ("pac_keep",)).fetchone() == (None,)
        assert bytes.fromhex(connection.execute("SELECT credential_envelope FROM provider_accounts WHERE id = ?", ("pac_keep",)).fetchone()[0]) == sealed
    finally:
        connection.close()
