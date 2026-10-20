import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base, ChatMessage as DbChatMessage, Session as DbSession
from core.models import ChatMessage, Session
from core.session_manager import SessionManager
from core.stats_models import StatsEvent
from services.stats.backfill import backfill_messages
from services.stats.ledger import (
    _insert_once,
    capture_message_event,
    project_admitted_events,
    select_admitted_events,
)
from src.openclank.operation_router import (
    ManagedOperationRequest,
    ManagedOperationRouter,
    OperationRoute,
)
from src.openclank.sql_owner_lifecycle import CommonSqlOwnerLifecycle


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stats.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def test_persist_message_captures_numeric_facts_in_same_transaction(tmp_path, monkeypatch):
    factory = _db(tmp_path)
    db = factory()
    db.add(DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice"))
    db.commit()
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = {"chat-1": Session("chat-1", "Chat", "local", "model", history=[], owner="alice")}
    manager.upload_handler = None
    monkeypatch.setattr("core.session_manager.SessionLocal", factory)
    message = ChatMessage(role="assistant", content="answer", metadata={"root_operation_id": "root-1", "metrics": {"input_tokens": 12, "output_tokens": 7}})
    manager.add_message("chat-1", message)
    event = db.query(StatsEvent).one()
    assert (event.owner, event.input_tokens, event.output_tokens) == ("alice", 12, 7)
    assert event.message_id == message.metadata["_db_id"]
    db.close()


def test_incognito_and_replay_are_not_durable_twice(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    session = DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice")
    db.add(session)
    db.flush()
    first = SimpleNamespace(role="assistant", metadata={"metrics": {"output_tokens": 3}, "stats_replay_key": "same"})
    row = DbChatMessage(id="msg-1", session_id="chat-1", role="assistant", content="x", timestamp=datetime.utcnow())
    db.add(row)
    capture_message_event(db, session, row, first)
    capture_message_event(db, session, row, first)
    incognito = SimpleNamespace(role="assistant", metadata={"incognito": True, "metrics": {"output_tokens": 4}})
    row2 = DbChatMessage(id="msg-2", session_id="chat-1", role="assistant", content="x", timestamp=datetime.utcnow())
    db.add(row2)
    capture_message_event(db, session, row2, incognito)
    db.commit()
    assert db.query(StatsEvent).count() == 1
    db.close()


def test_persisted_coverage_model_and_immutable_message_replay_identity(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    session = DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice")
    row = DbChatMessage(id="msg-stable", session_id="chat-1", role="assistant", content="x",
                        timestamp=datetime.utcnow())
    db.add_all([session, row])
    db.flush()
    message = SimpleNamespace(role="assistant", metadata={
        "model": "observed-model", "model_fingerprint": "fp-1", "covers_attempts": True,
        "metrics": {"output_tokens": 3},
    })
    first = capture_message_event(db, session, row, message)
    assert (first.actual_model, first.model_fingerprint, first.attempt_coverage) == (
        "observed-model", "fp-1", "covered")
    session.owner = "alice-renamed"
    second = capture_message_event(db, session, row, message)
    assert second.id == first.id
    assert db.query(StatsEvent).count() == 1
    db.rollback()
    db.close()


def test_projection_prefers_terminal_snapshot_and_keeps_disjoint_deltas():
    now = datetime.utcnow()
    common = dict(owner="alice", event_kind="response", source="test", producer_revision="t", terminal=True, ingested_at=now)
    rows = [SimpleNamespace(**common, replay_key="d1", attempt_id="a", observation_kind="delta", sequence=1, event_time=now, input_tokens=2, output_tokens=None), SimpleNamespace(**common, replay_key="d2", attempt_id="a", observation_kind="delta", sequence=2, event_time=now + timedelta(seconds=1), input_tokens=3, output_tokens=None), SimpleNamespace(**common, replay_key="s1", attempt_id="a", observation_kind="final_snapshot", sequence=1, event_time=now, input_tokens=9, output_tokens=None), SimpleNamespace(**common, replay_key="s2", attempt_id="a", observation_kind="final_snapshot", sequence=2, event_time=now + timedelta(seconds=2), input_tokens=11, output_tokens=None)]
    assert [row.replay_key for row in select_admitted_events(rows)] == ["s2"]
    unknown_coverage = [SimpleNamespace(**common, replay_key="delta", attempt_id="b",
                                        observation_kind="delta", sequence=2, event_time=now,
                                        input_tokens=2, output_tokens=None),
                        SimpleNamespace(**common, replay_key="snapshot", attempt_id="b",
                                        observation_kind="final_snapshot", sequence=None,
                                        event_time=now, input_tokens=9, output_tokens=None)]
    assert [row.replay_key for row in select_admitted_events(unknown_coverage)] == ["snapshot"]


def test_backfill_is_bounded_resumable_and_idempotent(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    db.add(DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice"))
    for index in range(3):
        db.add(DbChatMessage(id=f"msg-{index}", session_id="chat-1", role="assistant", content="x", meta_data=json.dumps({"metrics": {"output_tokens": index + 1}}), timestamp=datetime.utcnow()))
    db.commit()
    dry = backfill_messages(db=db, owner="alice", batch_size=2, dry_run=True)
    assert (dry.scanned, dry.admitted, dry.complete) == (2, 2, False)
    first = backfill_messages(db=db, owner="alice", batch_size=2)
    second = backfill_messages(db=db, owner="alice", batch_size=2, cursor=first.cursor)
    assert (first.scanned, second.scanned, second.complete) == (2, 1, True)
    again = backfill_messages(db=db, owner="alice", batch_size=10)
    assert (again.admitted, again.inserted, again.duplicates) == (3, 0, 3)
    assert db.query(StatsEvent).count() == 3
    assert {row.producer_revision for row in db.query(StatsEvent).all()} == {"s01-backfill-v1"}
    db.close()


def test_live_capture_and_any_revision_backfill_share_message_fact(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    session = DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice")
    message = DbChatMessage(
        id="canonical-message", session_id="chat-1", role="assistant", content="x",
        meta_data=json.dumps({"metrics": {"output_tokens": 4}}), timestamp=datetime.utcnow(),
    )
    db.add_all([session, message])
    db.flush()
    capture_message_event(db, session, message, SimpleNamespace(role="assistant", metadata={"metrics": {"output_tokens": 4}}))
    db.commit()
    dry = backfill_messages(db=db, owner="alice", revision="later-normalization", dry_run=True)
    assert (dry.admitted, dry.duplicates, dry.inserted) == (1, 1, 0)
    run = backfill_messages(db=db, owner="alice", revision="later-normalization")
    assert (run.admitted, run.duplicates, run.inserted) == (1, 1, 0)
    assert db.query(StatsEvent).count() == 1
    db.close()


def test_backfill_cancel_rolls_back_batch_and_preserves_callers_pending_work(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    db.add(DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice"))
    for index in range(2):
        db.add(DbChatMessage(id=f"msg-{index}", session_id="chat-1", role="assistant", content="x",
                             meta_data=json.dumps({"metrics": {"output_tokens": index + 1}}),
                             timestamp=datetime.utcnow()))
    db.commit()
    pending = DbSession(id="pending", name="Pending", endpoint_url="local", model="model", owner="alice")
    db.add(pending)
    calls = iter((False, True))
    result = backfill_messages(db=db, owner="alice", batch_size=2, cancel=lambda: next(calls))
    assert result.cancelled is True
    assert result.cursor is None
    assert result.inserted == 0
    assert db.query(StatsEvent).count() == 0
    assert db.query(DbSession).filter_by(id="pending").first() is not None
    db.rollback()
    assert db.query(DbSession).filter_by(id="pending").first() is None
    db.close()


def test_projection_keeps_distinct_attempts_and_explicit_aggregate_coverage():
    now = datetime.utcnow()
    common = dict(owner="alice", event_kind="response", source="test", producer_revision="t",
                  terminal=True, ingested_at=now, event_time=now, input_tokens=1, output_tokens=None)
    attempts = [SimpleNamespace(**common, replay_key="a1", root_operation_id="root", operation_id="op",
                                attempt_id="attempt-1", observation_kind="final_snapshot", sequence=1,
                                event_metadata={}),
                SimpleNamespace(**common, replay_key="a2", root_operation_id="root", operation_id="op",
                                attempt_id="attempt-2", observation_kind="final_snapshot", sequence=1,
                                event_metadata={})]
    assert {row.replay_key for row in select_admitted_events(attempts)} == {"a1", "a2"}
    aggregate = SimpleNamespace(**common, replay_key="agg", root_operation_id="root", operation_id="op",
                                attempt_id=None, observation_kind="final_snapshot", sequence=1,
                                event_metadata={"covers_attempts": True})
    assert [row.replay_key for row in select_admitted_events([*attempts, aggregate])] == ["agg"]


def test_projection_excludes_unknown_overlapping_root_and_marks_partial_coverage():
    now = datetime.utcnow()
    common = dict(owner="alice", event_kind="response", source="test", producer_revision="t",
                  terminal=True, ingested_at=now, event_time=now, input_tokens=1, output_tokens=None)
    attempt = SimpleNamespace(**common, replay_key="attempt", root_operation_id="root-unknown",
                              operation_id="op", attempt_id="attempt-1", observation_kind="final_snapshot",
                              sequence=1, event_metadata={})
    aggregate = SimpleNamespace(**common, replay_key="aggregate", root_operation_id="root-unknown",
                                operation_id=None, attempt_id=None, observation_kind="final_snapshot",
                                sequence=1, event_metadata={})
    projection = project_admitted_events([aggregate, attempt])
    assert [row.replay_key for row in projection.events] == ["attempt"]
    assert [row.replay_key for row in projection.excluded] == ["aggregate"]
    assert projection.coverage == "partial_ambiguous"


def test_projection_keeps_sibling_operation_aggregates_distinct_under_one_root():
    now = datetime.utcnow()
    common = dict(owner="alice", event_kind="response", source="test", producer_revision="t",
                  terminal=True, ingested_at=now, event_time=now, input_tokens=1, output_tokens=None)
    rows = [
        SimpleNamespace(**common, replay_key="op-a", root_operation_id="shared-root", operation_id="op-a",
                        attempt_id=None, observation_scope="operation", observation_kind="final_snapshot",
                        sequence=1, attempt_coverage="covered", event_metadata={"covers_attempts": True}),
        SimpleNamespace(**common, replay_key="op-b", root_operation_id="shared-root", operation_id="op-b",
                        attempt_id=None, observation_scope="operation", observation_kind="final_snapshot",
                        sequence=1, attempt_coverage="covered", event_metadata={"covers_attempts": True}),
    ]
    projection = project_admitted_events(rows)
    assert {row.replay_key for row in projection.events} == {"op-a", "op-b"}


def test_projection_hierarchical_root_operation_attempt_overlap():
    now = datetime.utcnow()
    common = dict(owner="alice", event_kind="response", source="test", producer_revision="t",
                  terminal=True, ingested_at=now, event_time=now, input_tokens=1, output_tokens=None,
                  sequence=1, observation_kind="final_snapshot")
    root = SimpleNamespace(**common, replay_key="root", root_operation_id="r", operation_id=None,
                           attempt_id=None, observation_scope="root", attempt_coverage="unavailable",
                           event_metadata={})
    op_a = SimpleNamespace(**common, replay_key="op-a", root_operation_id="r", operation_id="a",
                           attempt_id=None, observation_scope="operation", attempt_coverage="covered",
                           event_metadata={"covers_attempts": True})
    op_b = SimpleNamespace(**common, replay_key="op-b", root_operation_id="r", operation_id="b",
                           attempt_id=None, observation_scope="operation", attempt_coverage="covered",
                           event_metadata={"covers_attempts": True})
    attempt_a = SimpleNamespace(**common, replay_key="attempt-a", root_operation_id="r", operation_id="a",
                                attempt_id="aa", observation_scope="attempt", attempt_coverage="unavailable",
                                event_metadata={})
    attempt_b = SimpleNamespace(**common, replay_key="attempt-b", root_operation_id="r", operation_id="b",
                                attempt_id="bb", observation_scope="attempt", attempt_coverage="unavailable",
                                event_metadata={})
    projection = project_admitted_events([root, op_a, op_b, attempt_a, attempt_b])
    assert {row.replay_key for row in projection.events} == {"op-a", "op-b"}
    assert "root" in {row.replay_key for row in projection.excluded}
    assert {row.replay_key for row in projection.excluded} >= {"root", "attempt-a", "attempt-b"}
    assert projection.coverage == "partial_ambiguous"


def test_projection_known_root_supersedes_operations_and_attempts():
    now = datetime.utcnow()
    def row(key, scope, operation=None, attempt=None, coverage="unavailable", tokens=1):
        return SimpleNamespace(owner="alice", event_kind="response", source="test", producer_revision="t",
            terminal=True, ingested_at=now, event_time=now, input_tokens=tokens, output_tokens=None,
            sequence=1, observation_kind="final_snapshot", replay_key=key, root_operation_id="r",
            operation_id=operation, attempt_id=attempt, observation_scope=scope,
            attempt_coverage=coverage, event_metadata={"covers_attempts": coverage == "covered"})
    root = row("root", "root", coverage="covered", tokens=30)
    op_a = row("op-a", "operation", operation="a", coverage="covered", tokens=10)
    op_b = row("op-b", "operation", operation="b", coverage="covered", tokens=20)
    attempt_a = row("attempt-a", "attempt", operation="a", attempt="aa", tokens=10)
    attempt_b = row("attempt-b", "attempt", operation="b", attempt="bb", tokens=20)
    projection = project_admitted_events([root, op_a, op_b, attempt_a, attempt_b])
    assert [event.replay_key for event in projection.events] == ["root"]


def test_projection_uses_current_snapshot_for_coverage_authority():
    now = datetime.utcnow()
    def event(key, sequence, coverage):
        return SimpleNamespace(owner="alice", event_kind="response", source="test", producer_revision="t",
            terminal=True, ingested_at=now, event_time=now, input_tokens=1, output_tokens=None,
            sequence=sequence, observation_kind="final_snapshot", replay_key=key, root_operation_id="r",
            operation_id="a", attempt_id=None, observation_scope="operation",
            attempt_coverage=coverage, event_metadata={"covers_attempts": coverage == "covered"})
    old = event("old-covered", 1, "covered")
    current = event("current-unknown", 2, "unavailable")
    attempt_values = event("attempt", 1, "unavailable").__dict__.copy()
    attempt_values.update(attempt_id="aa", observation_scope="attempt")
    attempt = SimpleNamespace(**attempt_values)
    projection = project_admitted_events([old, current, attempt])
    assert {item.replay_key for item in projection.events} == {"attempt"}
    assert projection.coverage == "partial_ambiguous"


def test_operation_router_persists_validated_terminal_usage(tmp_path, monkeypatch):
    factory = _db(tmp_path)
    route = OperationRoute("connection", "provider", "standard", "route", "model")

    async def execute(_owner, _payload):
        return {
            "operationID": "operation-1",
            "rootOperationID": "root-1",
            "operation": "text.generate",
            "state": "complete",
            "committed": True,
            "replayed": False,
            "modelRouteID": "route",
            "connectionID": "connection",
            "billingLane": "standard",
            "output": {"text": "ok"},
            "usage": {"inputTokens": 4, "outputTokens": 2},
        }

    router = ManagedOperationRouter(session_factory=factory, executor=execute)
    monkeypatch.setattr(router, "_resolve_routes", lambda _request: (route,))
    request = ManagedOperationRequest(owner="alice", operation="text.generate", input={},
                                      root_operation_id="root-1", idempotency_key="idempotency-key-1")
    result = __import__("asyncio").run(router.execute(request))
    assert result.state == "complete"
    db = factory()
    event = db.query(StatsEvent).one()
    assert (event.event_kind, event.provider_id, event.billable, event.input_tokens, event.output_tokens) == (
        "response", "provider", None, 4, 2)
    assert (event.actual_model, event.event_metadata.get("model_identity_source"),
            event.event_metadata.get("billing_lane")) == ("model", "selected_route", "standard")
    assert event.event_metadata.get("normalization_profile") == "managed-sdk-inclusive-v1"
    assert event.observation_scope == "operation"
    db.close()


def test_common_owner_lifecycle_preserves_orphans_until_owner_purge(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    db.add(DbSession(id="chat-1", name="Chat", endpoint_url="local", model="model", owner="alice"))
    db.flush()
    db.add(StatsEvent(
        id="linked", replay_key="replay-linked", owner="alice", session_id="chat-1",
        event_kind="response", event_time=datetime.utcnow(), source="test",
        producer_revision="test", event_metadata={},
    ))
    db.add(StatsEvent(
        id="orphan", replay_key="replay-orphan", owner="alice", event_kind="response",
        event_time=datetime.utcnow(), source="test", producer_revision="test",
        event_metadata={},
    ))
    db.commit()
    db.delete(db.get(DbSession, "chat-1"))
    db.commit()
    assert db.query(StatsEvent).filter_by(id="linked").count() == 0
    assert db.query(StatsEvent).filter_by(id="orphan").count() == 1
    db.close()

    lifecycle = CommonSqlOwnerLifecycle(factory)
    renamed = lifecycle.rename_owner("alice", "alice-renamed")
    assert renamed.changed["stats_events.owner"] == 1
    db = factory()
    assert db.query(StatsEvent).one().owner == "alice-renamed"
    db.close()
    purged = lifecycle.purge_owner("alice-renamed")
    assert purged.changed["stats_events.owner"] == 1
    db = factory()
    assert db.query(StatsEvent).count() == 0
    db.close()


def test_backfill_validates_before_opening_db_and_non_replay_integrity_errors_escape(tmp_path):
    opened = []
    with pytest.raises(ValueError):
        backfill_messages(db=lambda: opened.append(True), owner="alice", batch_size=0)
    assert opened == []

    factory = _db(tmp_path)
    db = factory()
    base = dict(
        id="same-id", replay_key="replay-a", owner="alice", event_kind="response",
        event_time=datetime.utcnow(), source="test", producer_revision="test",
        event_metadata={}, attempt_coverage="unavailable", incognito=False,
    )
    _insert_once(db, base)
    with pytest.raises(Exception):
        _insert_once(db, {**base, "replay_key": "replay-b"})
    db.rollback()
    db.close()
    lifecycle = CommonSqlOwnerLifecycle(factory)
    renamed = lifecycle.rename_owner("alice", "alice-renamed")
    assert renamed.changed["stats_events.owner"] == 1
    db = factory()
    assert db.query(StatsEvent).one().owner == "alice-renamed"
    db.close()
    purged = lifecycle.purge_owner("alice-renamed")
    assert purged.changed["stats_events.owner"] == 1
    db = factory()
    assert db.query(StatsEvent).count() == 0
    db.close()
