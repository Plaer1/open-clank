"""Durable projection of MiMo actor lifecycle into one Agent turn."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.database import AgentTurn, ChatMessage, SessionLocal, TurnActor, utcnow_naive


_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "unknown"}


def _source_time(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def _project_status(actor: dict[str, Any]) -> tuple[str, str | None]:
    status = str(actor.get("status") or "unknown")
    outcome = str(actor.get("lastOutcome") or actor.get("outcome") or "") or None
    if status in {"pending", "running"}:
        return status, outcome
    if outcome == "success":
        return "completed", outcome
    if outcome == "failure":
        return "failed", outcome
    if outcome == "cancelled":
        return "cancelled", outcome
    return (status if status in _TERMINAL else "unknown"), outcome


def begin_agent_turn(root_turn_id: str, mimo_session_id: str) -> bool:
    """Create the pending zero-vs-unobserved discriminator for a real DB turn."""
    if not root_turn_id or not mimo_session_id:
        return False
    db = SessionLocal()
    try:
        if db.query(ChatMessage.id).filter(ChatMessage.id == root_turn_id).first() is None:
            return False
        row = db.get(AgentTurn, root_turn_id)
        if row is None:
            db.add(
                AgentTurn(
                    root_turn_id=root_turn_id,
                    mimo_session_id=mimo_session_id,
                    actor_accounting_state="pending",
                )
            )
        else:
            row.mimo_session_id = mimo_session_id
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def apply_actor_feed(payload: dict[str, Any]) -> dict[str, Any]:
    """Acknowledge a root and monotonically upsert one complete actor snapshot."""
    root_turn_id = str(payload.get("rootTurnId") or payload.get("root_turn_id") or "")
    mimo_session_id = str(payload.get("mimoSessionId") or payload.get("mimo_session_id") or "")
    revision = max(0, int(payload.get("sourceRevision") or payload.get("source_revision") or 0))
    actor = payload.get("actor")
    db = SessionLocal()
    try:
        turn = db.get(AgentTurn, root_turn_id)
        if turn is None:
            return {"state": "not_recorded", "total": None, "actors": []}
        if turn.actor_accounting_state == "pending":
            turn.actor_accounting_state = "recording"
        turn.last_source_revision = max(turn.last_source_revision or 0, revision)
        turn.updated_at = utcnow_naive()

        if isinstance(actor, dict) and actor.get("actorID"):
            actor_id = str(actor["actorID"])
            existing_any = (
                db.query(TurnActor)
                .filter(
                    TurnActor.mimo_session_id == mimo_session_id,
                    TurnActor.actor_id == actor_id,
                )
                .first()
            )
            parent_id = str(actor.get("parentActorID") or "") or None
            parent = None
            if parent_id:
                parent = (
                    db.query(TurnActor)
                    .filter(
                        TurnActor.mimo_session_id == mimo_session_id,
                        TurnActor.actor_id == parent_id,
                    )
                    .first()
                )
            target_root = existing_any.root_turn_id if existing_any else (parent.root_turn_id if parent else root_turn_id)
            target_turn = db.get(AgentTurn, target_root)
            if target_turn is not None:
                row = db.get(TurnActor, (target_root, actor_id))
                if row is None:
                    status, outcome = _project_status(actor)
                    counts = bool(
                        actor.get("countsTowardTotal")
                        if "countsTowardTotal" in actor
                        else actor.get("mode") == "subagent" and not actor.get("systemSpawned", False)
                    )
                    row = TurnActor(
                        root_turn_id=target_root,
                        actor_id=actor_id,
                        mimo_session_id=mimo_session_id,
                        parent_actor_id=parent_id,
                        mode=str(actor.get("mode") or "subagent"),
                        agent=str(actor.get("agent") or "unknown"),
                        description=str(actor.get("description") or ""),
                        background=bool(actor.get("background")),
                        lifecycle=str(actor.get("lifecycle") or "ephemeral"),
                        counts_toward_total=counts,
                        status=status,
                        outcome=outcome,
                        last_error=str(actor.get("lastError") or actor.get("error") or "") or None,
                        source_revision=revision,
                        created_at=_source_time((actor.get("time") or {}).get("created")) or utcnow_naive(),
                        updated_at=_source_time((actor.get("time") or {}).get("updated")) or utcnow_naive(),
                        completed_at=_source_time((actor.get("time") or {}).get("completed")),
                    )
                    db.add(row)
                elif revision > (row.source_revision or 0):
                    status, outcome = _project_status(actor)
                    if row.status not in _TERMINAL or status in _TERMINAL:
                        row.status = status
                        row.outcome = outcome
                    row.parent_actor_id = parent_id
                    row.mode = str(actor.get("mode") or row.mode)
                    row.agent = str(actor.get("agent") or row.agent)
                    row.description = str(actor.get("description") or row.description)
                    row.background = bool(actor.get("background"))
                    row.lifecycle = str(actor.get("lifecycle") or row.lifecycle)
                    row.last_error = str(actor.get("lastError") or actor.get("error") or "") or None
                    row.source_revision = revision
                    row.updated_at = _source_time((actor.get("time") or {}).get("updated")) or utcnow_naive()
                    row.completed_at = _source_time((actor.get("time") or {}).get("completed")) or row.completed_at
        db.commit()
        return actor_accounting_for_root(root_turn_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def close_agent_turn(root_turn_id: str, assistant_message_id: str | None) -> dict[str, Any]:
    db = SessionLocal()
    try:
        turn = db.get(AgentTurn, root_turn_id)
        if turn is None:
            return {"state": "not_recorded", "total": None, "actors": []}
        turn.assistant_message_id = assistant_message_id or turn.assistant_message_id
        turn.closed_at = utcnow_naive()
        turn.updated_at = turn.closed_at
        turn.actor_accounting_state = (
            "closed" if turn.actor_accounting_state in {"recording", "closed"} else "unavailable"
        )
        db.commit()
        return actor_accounting_for_root(root_turn_id)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def actor_accounting_for_root(root_turn_id: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        turn = db.get(AgentTurn, root_turn_id)
        return _aggregate(db, turn)
    finally:
        db.close()


def actor_accounting_for_assistant(message_id: str | None) -> dict[str, Any] | None:
    if not message_id:
        return None
    db = SessionLocal()
    try:
        turn = db.query(AgentTurn).filter(AgentTurn.assistant_message_id == message_id).first()
        return _aggregate(db, turn) if turn is not None else None
    finally:
        db.close()


def _aggregate(db, turn: AgentTurn | None) -> dict[str, Any]:
    if turn is None:
        return {"state": "not_recorded", "total": None, "actors": []}
    rows = (
        db.query(TurnActor)
        .filter(TurnActor.root_turn_id == turn.root_turn_id, TurnActor.counts_toward_total.is_(True))
        .order_by(TurnActor.created_at, TurnActor.actor_id)
        .all()
    )
    state = turn.actor_accounting_state
    recorded = state in {"recording", "closed"}
    actors = [
        {
            "id": row.actor_id,
            "parent_id": row.parent_actor_id,
            "agent": row.agent,
            "description": row.description,
            "status": row.status,
            "outcome": row.outcome,
            "background": row.background,
        }
        for row in rows
    ]
    counts = {key: 0 for key in ("running", "completed", "failed", "cancelled", "interrupted", "unknown")}
    for actor in actors:
        key = actor["status"]
        if key in {"pending", "running"}:
            counts["running"] += 1
        else:
            counts[key if key in counts else "unknown"] += 1
    return {
        "state": state,
        "total": len(actors) if recorded else None,
        **counts,
        "actors": actors,
    }


def recover_actor_accounting_after_restart() -> None:
    """Close process-local feeds honestly after a full server restart."""
    db = SessionLocal()
    try:
        now = utcnow_naive()
        for turn in db.query(AgentTurn).filter(AgentTurn.actor_accounting_state.in_(["pending", "recording"])).all():
            if turn.actor_accounting_state == "pending":
                turn.actor_accounting_state = "unavailable"
            else:
                turn.actor_accounting_state = "closed"
            turn.closed_at = turn.closed_at or now
            turn.updated_at = now
        for actor in db.query(TurnActor).filter(TurnActor.status.in_(["pending", "running"])).all():
            actor.status = "interrupted"
            actor.last_error = actor.last_error or "actor state unavailable after server restart"
            actor.completed_at = actor.completed_at or now
            actor.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
