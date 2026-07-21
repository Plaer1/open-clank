from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import AgentTurn, Base, ChatMessage, Session, TurnActor
import src.agent_actor_accounting as accounting


def _database(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(accounting, "SessionLocal", factory)
    db = factory()
    db.add(Session(id="s1", name="test", endpoint_url="mimo://acp", model="mimo"))
    db.add(ChatMessage(id="user-1", session_id="s1", role="user", content="go"))
    db.add(ChatMessage(id="assistant-1", session_id="s1", role="assistant", content="done"))
    db.commit()
    db.close()
    return factory


def _feed(revision, actor=None):
    payload = {
        "rootTurnId": "user-1",
        "mimoSessionId": "mimo-1",
        "sourceRevision": revision,
        "acknowledged": True,
    }
    if actor is not None:
        payload["actor"] = actor
    return payload


def _actor(actor_id, *, status="running", outcome=None, parent=None, system=False):
    actor = {
        "actorID": actor_id,
        "parentActorID": parent,
        "mode": "subagent",
        "agent": "explore",
        "description": actor_id,
        "background": True,
        "lifecycle": "ephemeral",
        "status": status,
        "systemSpawned": system,
        "countsTowardTotal": not system,
        "time": {"created": 1_700_000_000_000, "updated": 1_700_000_000_001},
    }
    if outcome:
        actor["lastOutcome"] = outcome
    return actor


def test_zero_missing_monotonic_nested_and_restart(monkeypatch):
    factory = _database(monkeypatch)

    assert accounting.begin_agent_turn("user-1", "mimo-1")
    assert accounting.actor_accounting_for_root("user-1")["total"] is None

    zero = accounting.apply_actor_feed(_feed(1))
    assert zero["state"] == "recording"
    assert zero["total"] == 0

    one = accounting.apply_actor_feed(_feed(2, _actor("explore-1")))
    assert one["total"] == 1
    assert one["running"] == 1

    nested = accounting.apply_actor_feed(_feed(3, _actor("explore-2", parent="explore-1")))
    assert nested["total"] == 2
    assert nested["actors"][1]["parent_id"] == "explore-1"

    excluded = accounting.apply_actor_feed(_feed(4, _actor("checkpoint-1", system=True)))
    assert excluded["total"] == 2

    done = accounting.apply_actor_feed(_feed(5, _actor("explore-1", status="idle", outcome="success")))
    assert done["completed"] == 1
    stale = accounting.apply_actor_feed(_feed(4, _actor("explore-1", status="running")))
    assert stale["completed"] == 1

    closed = accounting.close_agent_turn("user-1", "assistant-1")
    assert closed["state"] == "closed"
    assert accounting.actor_accounting_for_assistant("assistant-1")["total"] == 2

    accounting.recover_actor_accounting_after_restart()
    restarted = accounting.actor_accounting_for_root("user-1")
    assert restarted["completed"] == 1
    assert restarted["interrupted"] == 1

    db = factory()
    db.query(ChatMessage).filter(ChatMessage.id == "user-1").delete()
    db.commit()
    assert db.query(AgentTurn).count() == 0
    assert db.query(TurnActor).count() == 0
    db.close()


def test_pending_turn_closes_as_unavailable_not_zero(monkeypatch):
    _database(monkeypatch)
    accounting.begin_agent_turn("user-1", "mimo-1")

    result = accounting.close_agent_turn("user-1", "assistant-1")

    assert result["state"] == "unavailable"
    assert result["total"] is None
