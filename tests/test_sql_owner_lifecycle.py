"""Isolated common-SQL account lifecycle coverage."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, LargeBinary, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (
    Base,
    CalendarCal,
    CalendarEvent,
    ModelShare,
    ModelShareSubscription,
)
from src.openclank.sql_owner_lifecycle import (
    CommonSqlOwnerLifecycle,
    SqlOwnerLifecycleConflict,
)


def _raw_value(column, *, owner: str, ordinal: int):
    suffix = f"{owner}-{ordinal}"
    if isinstance(column.type, Boolean):
        return 1
    if isinstance(column.type, Integer):
        return ordinal + 1
    if isinstance(column.type, Float):
        return float(ordinal + 1)
    if isinstance(column.type, DateTime):
        return "2026-08-26 12:00:00"
    if isinstance(column.type, JSON):
        return "{}"
    if isinstance(column.type, LargeBinary):
        return suffix.encode("utf-8")
    return f"{column.table.name}-{column.name}-{suffix}"


def _seed_every_binding(engine, lifecycle, owner: str, *, ordinal: int = 0) -> None:
    """Seed every discovered owner table without touching a real database."""
    with engine.connect() as connection:
        # The ownership adapter has no business mutating relationship keys.
        # Disable FK checks only while this synthetic all-table matrix is
        # assembled, so every owner binding can be exercised independently.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for binding in lifecycle.bindings:
            table = binding.model.__table__
            columns = []
            values = []
            for column in table.columns:
                if column.name == binding.column_name:
                    value = owner
                elif column.nullable:
                    continue
                else:
                    value = _raw_value(column, owner=owner, ordinal=ordinal)
                columns.append(column.name)
                values.append(value)
            quote = connection.dialect.identifier_preparer.quote
            sql = (
                f"INSERT INTO {quote(table.name)} "
                f"({', '.join(quote(name) for name in columns)}) "
                f"VALUES ({', '.join('?' for _ in values)})"
            )
            connection.exec_driver_sql(sql, tuple(values))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.fixture()
def sql_lifecycle():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    lifecycle = CommonSqlOwnerLifecycle(factory)
    yield lifecycle, engine
    engine.dispose()


def test_registry_covers_every_common_base_owner_and_excludes_barriers(sql_lifecycle):
    lifecycle, _engine = sql_lifecycle
    expected = {
        f"{table.name}.owner"
        for table in Base.metadata.tables.values()
        if "owner" in table.c
    }
    expected.update(
        {
            "mimo_projection_states.owner_id",
            "model_share_subscriptions.subscriber",
        }
    )

    assert set(lifecycle.scope_keys) == expected
    assert set(lifecycle.dependent_scope_keys) == {
        "agent_turns.via:sessions.owner",
        "calendar_events.via:calendars.owner",
        "chat_messages.via:sessions.owner",
        "document_versions.via:documents.owner",
        "mimo_projections.via:sessions.owner",
        "model_capabilities.via:model_endpoints.owner",
        "model_share_subscriptions.via:model_shares.owner",
        "published_file_grants.via:published_files.owner",
        "task_runs.via:scheduled_tasks.owner",
        "tui_turn_submissions.via:sessions.owner",
        "turn_actors.via:sessions.owner",
        "user_tool_data.via:user_tools.owner",
    }
    assert "account_lifecycle_operations.owner" not in lifecycle.scope_keys
    assert "auth_identities.owner" not in lifecycle.scope_keys


def test_stage_reconcile_compensate_purge_and_verify_every_owner_binding(
    sql_lifecycle,
):
    lifecycle, engine = sql_lifecycle
    _seed_every_binding(engine, lifecycle, "alice")
    _seed_every_binding(engine, lifecycle, "bob")
    alice_before = lifecycle.owner_inventory("alice")
    bob_before = lifecycle.owner_inventory("bob")

    assert alice_before["count"] == len(lifecycle.scope_keys)
    assert all(
        alice_before["tables"][key] == 1 for key in lifecycle.scope_keys
    )
    assert all(
        alice_before["tables"][key] == 0
        for key in lifecycle.dependent_scope_keys
    )
    staged = lifecycle.stage_owner(
        "ALICE",
        "deleted:account-alice",
        expected_source=alice_before,
    )

    assert staged.state == "applied"
    assert staged.count == alice_before["count"]
    assert set(staged.changed) == set(lifecycle.scope_keys)
    lifecycle.verify_staged(
        "alice",
        "deleted:account-alice",
        expected_source=alice_before,
    )
    assert lifecycle.owner_inventory("bob") == bob_before

    retry = lifecycle.reconcile_owner(
        "alice",
        "deleted:account-alice",
        expected_source=alice_before,
    )
    assert retry.state == "already_applied"
    assert retry.count == 0

    restored = lifecycle.compensate_owner(
        "alice",
        "deleted:account-alice",
        expected_source=alice_before,
    )
    assert restored.state == "applied"
    assert lifecycle.owner_inventory("alice") == alice_before
    lifecycle.verify_owner_absent("deleted:account-alice")
    assert lifecycle.owner_inventory("bob") == bob_before

    purged = lifecycle.purge_owner("alice", expected_inventory=alice_before)
    assert purged.state == "applied"
    assert purged.count == alice_before["count"]
    lifecycle.verify_owner_absent("alice")
    retry_purge = lifecycle.purge_owner(
        "alice",
        expected_inventory=alice_before,
    )
    assert retry_purge.state == "already_applied"
    assert retry_purge.count == 0
    assert lifecycle.owner_inventory("bob") == bob_before


def test_split_source_and_target_fails_closed_before_any_mutation(sql_lifecycle):
    lifecycle, engine = sql_lifecycle
    _seed_every_binding(engine, lifecycle, "alice")
    _seed_every_binding(engine, lifecycle, "bob")
    _seed_every_binding(engine, lifecycle, "deleted:account-alice")
    alice_before = lifecycle.owner_inventory("alice")
    target_before = lifecycle.owner_inventory("deleted:account-alice")
    bob_before = lifecycle.owner_inventory("bob")

    with pytest.raises(
        SqlOwnerLifecycleConflict,
        match="source and target SQL owners both contain state",
    ) as caught:
        lifecycle.reconcile_owner(
            "alice",
            "deleted:account-alice",
            expected_source=alice_before,
        )

    assert caught.value.source == alice_before
    assert caught.value.target == target_before
    assert lifecycle.owner_inventory("alice") == alice_before
    assert lifecycle.owner_inventory("deleted:account-alice") == target_before
    assert lifecycle.owner_inventory("bob") == bob_before


def test_historical_mixed_case_owner_rows_are_moved_and_conflict_case_insensitively(
    sql_lifecycle,
):
    lifecycle, engine = sql_lifecycle
    _seed_every_binding(engine, lifecycle, "Alice")
    expected = lifecycle.owner_inventory("alice")
    assert expected["count"] == len(lifecycle.scope_keys)

    receipt = lifecycle.reconcile_owner(
        "ALICE",
        "alice2",
        expected_source=expected,
    )
    assert receipt.state == "applied"
    assert lifecycle.owner_inventory("alice")["count"] == 0
    target = lifecycle.owner_inventory("ALICE2")
    assert target["count"] == expected["count"]
    assert target["fingerprint"] == expected["fingerprint"]

    lifecycle.compensate_owner(
        "alice",
        "alice2",
        expected_source=expected,
    )
    _seed_every_binding(engine, lifecycle, "ALICE2", ordinal=9)
    with pytest.raises(SqlOwnerLifecycleConflict, match="both contain"):
        lifecycle.reconcile_owner(
            "alice",
            "alice2",
            expected_source=expected,
        )


def test_inventory_and_receipts_are_content_free_and_stale_safe(sql_lifecycle):
    lifecycle, engine = sql_lifecycle
    _seed_every_binding(engine, lifecycle, "alice")
    inventory = lifecycle.owner_inventory("alice")
    stale = dict(inventory)
    stale["fingerprint"] = "0" * 64

    with pytest.raises(SqlOwnerLifecycleConflict, match="source SQL owner inventory changed"):
        lifecycle.stage_owner(
            "alice",
            "alice-renamed",
            expected_source=stale,
        )

    assert lifecycle.owner_inventory("alice") == inventory
    receipt = lifecycle.stage_owner(
        "alice",
        "alice-renamed",
        expected_source=inventory,
    )
    encoded = json.dumps(receipt.as_dict(), sort_keys=True)
    assert len(inventory["fingerprint"]) == 64
    assert "memories-text-alice" not in encoded
    assert "api_tokens-token_hash-alice" not in encoded
    assert set(inventory) == {
        "schema_version",
        "owner",
        "count",
        "tables",
        "table_fingerprints",
        "fingerprint",
    }


def test_reconcile_rejects_same_shape_target_with_different_row_identities(
    sql_lifecycle,
):
    lifecycle, engine = sql_lifecycle
    _seed_every_binding(engine, lifecycle, "alice", ordinal=0)
    expected = lifecycle.owner_inventory("alice")
    lifecycle.purge_owner("alice", expected_inventory=expected)
    _seed_every_binding(engine, lifecycle, "deleted:account-alice", ordinal=9)

    with pytest.raises(
        SqlOwnerLifecycleConflict,
        match="reconciled target SQL owner inventory changed",
    ):
        lifecycle.reconcile_owner(
            "alice",
            "deleted:account-alice",
            expected_source=expected,
        )


def test_purge_removes_calendar_owned_dependents_without_touching_owner_b(
    sql_lifecycle,
):
    lifecycle, engine = sql_lifecycle
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as db:
        db.add_all(
            [
                CalendarCal(id="calendar-a", owner="alice", name="A"),
                CalendarCal(id="calendar-b", owner="bob", name="B"),
                CalendarEvent(
                    uid="event-a",
                    calendar_id="calendar-a",
                    summary="private-a",
                    dtstart=datetime(2026, 8, 26, 12, 0, 0),
                    dtend=datetime(2026, 8, 26, 13, 0, 0),
                ),
                CalendarEvent(
                    uid="event-b",
                    calendar_id="calendar-b",
                    summary="private-b",
                    dtstart=datetime(2026, 8, 26, 12, 0, 0),
                    dtend=datetime(2026, 8, 26, 13, 0, 0),
                ),
            ]
        )
        db.commit()

    receipt = lifecycle.purge_owner("alice")

    assert receipt.changed["calendars.owner"] == 1
    assert receipt.changed["calendar_events.via:calendars.owner"] == 1
    with factory() as db:
        assert db.get(CalendarCal, "calendar-a") is None
        assert db.get(CalendarEvent, "event-a") is None
        assert db.get(CalendarCal, "calendar-b").owner == "bob"
        assert db.get(CalendarEvent, "event-b").summary == "private-b"


def test_cross_owner_share_grants_follow_source_share_with_fk_cascades_disabled(
    sql_lifecycle,
):
    lifecycle, engine = sql_lifecycle
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with factory() as db:
        db.add_all(
            [
                ModelShare(
                    id="share-a",
                    owner="alice",
                    source_kind="native",
                    source_id="route-a",
                    model_id="model-a",
                ),
                ModelShare(
                    id="share-b",
                    owner="bob",
                    source_kind="native",
                    source_id="route-b",
                    model_id="model-b",
                ),
                ModelShareSubscription(
                    share_id="share-a",
                    subscriber="bob",
                    enabled=True,
                ),
                ModelShareSubscription(
                    share_id="share-a",
                    subscriber="carol",
                    enabled=True,
                ),
                ModelShareSubscription(
                    share_id="share-b",
                    subscriber="alice",
                    enabled=True,
                ),
                ModelShareSubscription(
                    share_id="share-b",
                    subscriber="bob",
                    enabled=True,
                ),
            ]
        )
        db.commit()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")

    alice_before = lifecycle.owner_inventory("alice")
    bob_before = lifecycle.owner_inventory("bob")
    assert alice_before["tables"]["model_shares.owner"] == 1
    assert alice_before["tables"]["model_share_subscriptions.subscriber"] == 1
    assert (
        alice_before["tables"][
            "model_share_subscriptions.via:model_shares.owner"
        ]
        == 2
    )

    staged = lifecycle.stage_owner(
        "alice",
        "deleted:account-alice",
        expected_source=alice_before,
    )

    assert staged.state == "applied"
    bob_after_stage = lifecycle.owner_inventory("bob")
    # Bob's own rows and direct subscription choices are stable.  The one
    # owner-derived edge that names Alice changes because Alice's subscription
    # to Bob's share must follow Alice's rename.
    for key in lifecycle.scope_keys:
        assert bob_after_stage["tables"][key] == bob_before["tables"][key]
        assert (
            bob_after_stage["table_fingerprints"][key]
            == bob_before["table_fingerprints"][key]
        )
    assert (
        bob_after_stage["tables"][
            "model_share_subscriptions.via:model_shares.owner"
        ]
        == 2
    )
    lifecycle.verify_staged(
        "alice",
        "deleted:account-alice",
        expected_source=alice_before,
    )
    with factory() as db:
        assert db.get(ModelShare, "share-a").owner == "deleted:account-alice"
        assert (
            db.get(ModelShareSubscription, ("share-a", "bob")).subscriber
            == "bob"
        )

    purged = lifecycle.purge_owner(
        "deleted:account-alice",
        expected_inventory=alice_before,
    )

    assert (
        purged.changed["model_share_subscriptions.via:model_shares.owner"]
        == 2
    )
    assert purged.changed["model_share_subscriptions.subscriber"] == 1
    assert purged.changed["model_shares.owner"] == 1
    with factory() as db:
        assert db.get(ModelShare, "share-a") is None
        assert db.get(ModelShareSubscription, ("share-a", "bob")) is None
        assert db.get(ModelShareSubscription, ("share-a", "carol")) is None
        assert db.get(ModelShare, "share-b").owner == "bob"
        assert db.get(ModelShareSubscription, ("share-b", "bob")).enabled is True
        assert db.get(ModelShareSubscription, ("share-b", "alice")) is None

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
