"""Canonical transcript revision and active MiMo projection records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import OperationalError

from core.database import ChatMessage, MimoProjection, Session, SessionLocal


@dataclass(frozen=True)
class CanonicalSnapshot:
    session_id: str
    owner: str
    revision: int
    message_ids: tuple[str, ...]
    digest: str


def canonical_snapshot(session_id: str, owner: Optional[str] = None) -> CanonicalSnapshot:
    db = SessionLocal()
    try:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session is None or (owner is not None and (session.owner or "") != owner):
            raise KeyError(f"Canonical session {session_id!r} not found")
        rows = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.timestamp, ChatMessage.id)
            .all()
        )
        material = [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "metadata": row.meta_data,
            }
            for row in rows
        ]
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return CanonicalSnapshot(
            session_id=session_id,
            owner=session.owner or "",
            revision=int(session.transcript_revision or 0),
            message_ids=tuple(row.id for row in rows),
            digest=digest,
        )
    finally:
        db.close()


def record_projection(
    snapshot: CanonicalSnapshot,
    *,
    mimo_session_id: str,
    workspace: str,
    endpoint_url: str,
    model: str,
    turn_id: str,
    mode_config_revision: int = 0,
) -> None:
    if not snapshot.owner:
        raise ValueError("MiMo projections require an authenticated owner")
    db = SessionLocal()
    try:
        row = (
            db.query(MimoProjection)
            .filter(MimoProjection.odysseus_session_id == snapshot.session_id)
            .first()
        )
        values = {
            "mimo_session_id": mimo_session_id,
            "owner": snapshot.owner,
            "workspace": workspace,
            "endpoint_url": endpoint_url,
            "model": model,
            "transcript_revision": snapshot.revision,
            "covered_message_ids": json.dumps(snapshot.message_ids),
            "canonical_digest": snapshot.digest,
            "mode_config_revision": mode_config_revision,
            "lifecycle_state": "active",
            "active_turn_id": turn_id,
        }
        if row is None:
            row = MimoProjection(odysseus_session_id=snapshot.session_id, **values)
            db.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def get_projection(session_id: str, owner: Optional[str] = None) -> Optional[dict]:
    db = SessionLocal()
    try:
        query = db.query(MimoProjection).filter(
            MimoProjection.odysseus_session_id == session_id
        )
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        try:
            row = query.first()
        except OperationalError:
            return None
        if row is None:
            return None
        return {
            "odysseus_session_id": row.odysseus_session_id,
            "mimo_session_id": row.mimo_session_id,
            "owner": row.owner,
            "workspace": row.workspace,
            "endpoint_url": row.endpoint_url,
            "model": row.model,
            "transcript_revision": row.transcript_revision,
            "covered_message_ids": json.loads(row.covered_message_ids or "[]"),
            "canonical_digest": row.canonical_digest,
            "mode_config_revision": row.mode_config_revision,
            "lifecycle_state": row.lifecycle_state,
            "active_turn_id": row.active_turn_id,
        }
    finally:
        db.close()


def list_projections(owner: Optional[str] = None) -> list[dict]:
    db = SessionLocal()
    try:
        query = db.query(MimoProjection)
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        try:
            rows = query.all()
        except OperationalError:
            return []
        return [
            {
                "odysseus_session_id": row.odysseus_session_id,
                "mimo_session_id": row.mimo_session_id,
                "owner": row.owner,
                "lifecycle_state": row.lifecycle_state,
            }
            for row in rows
        ]
    finally:
        db.close()


def mark_projection_stale(session_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.query(MimoProjection).filter(
            MimoProjection.odysseus_session_id == session_id
        ).first()
        if row is not None:
            row.lifecycle_state = "stale"
            db.commit()
    finally:
        db.close()


def delete_projection(session_id: str) -> None:
    db = SessionLocal()
    try:
        try:
            db.query(MimoProjection).filter(
                MimoProjection.odysseus_session_id == session_id
            ).delete(synchronize_session=False)
            db.commit()
        except OperationalError:
            db.rollback()
    finally:
        db.close()


def get_mimo_state(session_id: str, owner: Optional[str] = None) -> dict:
    db = SessionLocal()
    try:
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        return dict(session.mimo_state or {})
    finally:
        db.close()


def save_mimo_state(
    session_id: str,
    state: dict,
    *,
    owner: Optional[str] = None,
) -> dict:
    """Persist a secret-free negotiated control-plane snapshot."""
    db = SessionLocal()
    try:
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        previous = dict(session.mimo_state or {})
        before = {key: value for key, value in previous.items() if key != "revision"}
        after = {key: value for key, value in state.items() if key != "revision"}
        revision = int(previous.get("revision") or 0)
        if before != after:
            revision += 1
        payload = dict(after)
        payload["revision"] = revision
        session.mimo_state = payload
        db.commit()
        return payload
    finally:
        db.close()


async def purge_execution_projection(
    supervisor,
    session_id: str,
    *,
    owner: Optional[str] = None,
) -> bool:
    """Securely remove any MiMo execution state for a canonical session."""
    row = get_projection(session_id)
    if row is not None and owner is not None and row["owner"] != owner:
        raise PermissionError("MiMo projection belongs to another owner")
    effective_owner = owner or (row["owner"] if row is not None else None)

    if supervisor is not None and hasattr(supervisor, "mapped_sessions"):
        mapped = supervisor.mapped_sessions(owner=effective_owner)
    else:
        bridge = getattr(supervisor, "bridge", None) if supervisor else None
        mapped = bridge.mapped_sessions() if bridge is not None else {}
    mimo_session_id = (
        row["mimo_session_id"] if row is not None else mapped.get(session_id)
    )
    if mimo_session_id is None:
        return False
    try:
        alive = bool(supervisor and supervisor.is_alive(owner=effective_owner))
    except TypeError:
        alive = bool(supervisor and supervisor.is_alive())
    if not alive:
        raise RuntimeError("MiMo is unavailable; execution state was not deleted")

    try:
        await supervisor.delete_session(
            session_id,
            owner=effective_owner,
            mimo_session_id=mimo_session_id,
        )
    except TypeError:
        await supervisor.delete_session(
            session_id,
            mimo_session_id=mimo_session_id,
        )
    return True
