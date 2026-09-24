"""Canonical transcript revision and active Open Clank agent projection records."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import OperationalError

from core.database import ChatMessage, MimoProjection, Session, SessionLocal


_EXPECTED_UNSET = object()


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
        raise ValueError("Open Clank agent projections require an authenticated owner")
    db = SessionLocal()
    try:
        row = (
            db.query(MimoProjection)
            .filter(
                MimoProjection.odysseus_session_id == snapshot.session_id,
                MimoProjection.owner == snapshot.owner,
            )
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
    if owner is None:
        raise ValueError("projection reads require an owner")
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


def mark_projection_stale(session_id: str, owner: Optional[str] = None) -> None:
    if owner is None:
        raise ValueError("projection updates require an owner")
    db = SessionLocal()
    try:
        query = db.query(MimoProjection).filter(MimoProjection.odysseus_session_id == session_id)
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        row = query.first()
        if row is not None:
            row.lifecycle_state = "stale"
            db.commit()
    finally:
        db.close()


def delete_projection(session_id: str, owner: Optional[str] = None) -> None:
    if owner is None:
        raise ValueError("projection deletes require an owner")
    db = SessionLocal()
    try:
        query = db.query(MimoProjection).filter(MimoProjection.odysseus_session_id == session_id)
        if owner is not None:
            query = query.filter(MimoProjection.owner == owner)
        query.delete(synchronize_session=False)
        db.commit()
    except OperationalError:
        db.rollback()
        raise RuntimeError("managed projection deletion was not confirmed")
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
    expected_workspace_revision: Optional[int] = None,
) -> dict:
    """Persist a secret-free negotiated control-plane snapshot."""
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        previous = dict(session.mimo_state or {})
        if expected_workspace_revision is not None:
            binding = previous.get("managed_binding")
            actual_workspace_revision = int(binding.get("workspaceRevision") or 0) if isinstance(binding, dict) else 0
            if actual_workspace_revision != int(expected_workspace_revision):
                raise ValueError("managed binding workspace revision conflict")
        before = {key: value for key, value in previous.items() if key != "revision"}
        after = {key: value for key, value in state.items() if key != "revision"}
        previous_binding = previous.get("managed_binding")
        # Whole-state callers never own the host binding. Preserve an existing
        # record unconditionally, and discard an attempted first write; host
        # binding creation/replacement goes through save_managed_binding so it
        # has one owner-qualified CAS boundary.
        if isinstance(previous_binding, dict):
            after["managed_binding"] = previous_binding
        else:
            after.pop("managed_binding", None)
        revision = int(previous.get("revision") or 0)
        if before != after:
            revision += 1
        # Whole-state writers are merge-safe: managed binding is owned by the
        # host cwd CAS and cannot be erased by a stale plan/model snapshot.
        payload = dict(after)
        payload["revision"] = revision
        session.mimo_state = payload
        db.commit()
        return payload
    finally:
        db.close()


def get_managed_binding(session_id: str, owner: Optional[str] = None) -> dict:
    """Return the detached owner-qualified host binding, if present."""
    if not owner:
        raise ValueError("managed binding reads require an owner")
    state = get_mimo_state(session_id, owner=owner)
    binding = state.get("managed_binding")
    return dict(binding) if isinstance(binding, dict) else {}


def save_managed_binding(
    session_id: str,
    binding: dict,
    *,
    owner: Optional[str] = None,
    expected_workspace_revision: Optional[int] = None,
    expected_engine_session_id: object = _EXPECTED_UNSET,
    expected_map_revision: object = _EXPECTED_UNSET,
    expected_mapping_revision: object = _EXPECTED_UNSET,
) -> dict:
    """Merge a host binding without clobbering unrelated session state."""
    if not owner:
        raise ValueError("managed binding writes require an owner")
    required_strings = (
        "owner", "stableChatID", "engineSessionID", "memoryWorkspaceID",
        "authorityWorkspaceID", "copalWorkspace", "physicalCwd",
    )
    if any(
        not isinstance(binding.get(key), str)
        or not binding[key]
        or binding[key] != binding[key].strip()
        for key in required_strings
    ):
        raise ValueError("managed binding is incomplete")
    if binding.get("owner") != owner or binding.get("stableChatID") != session_id or not os.path.isabs(binding["physicalCwd"]):
        raise ValueError("managed binding owner or cwd is invalid")
    if str(os.path.realpath(binding["physicalCwd"])) != binding["physicalCwd"]:
        raise ValueError("managed binding cwd is not canonical")
    aliases = binding.get("engineAliases")
    if (
        not isinstance(aliases, list)
        or len(aliases) > 16
        or any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            for value in aliases
        )
    ):
        raise ValueError("managed binding aliases are invalid")
    if len(set(aliases)) != len(aliases) or binding["engineSessionID"] in aliases:
        raise ValueError("managed binding aliases are not unique")
    if not isinstance(binding.get("memoryEnabled"), bool):
        raise ValueError("managed binding memory flag is invalid")
    for key in ("workspaceRevision", "mapRevision", "mappingRevision"):
        value = binding.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("managed binding revisions are invalid")
    transition = binding.get("transition")
    if transition is not None:
        if not isinstance(transition, dict) or transition.get("phase") not in {"in_flight", "reconciling"}:
            raise ValueError("managed binding transition is invalid")
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = db.query(Session).filter(Session.id == session_id)
        if owner is not None:
            query = query.filter(Session.owner == owner)
        session = query.first()
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        state = dict(session.mimo_state or {})
        previous = state.get("managed_binding")
        previous = dict(previous) if isinstance(previous, dict) else {}
        actual = int(previous.get("workspaceRevision") or 0)
        if expected_workspace_revision is not None and actual != int(expected_workspace_revision):
            raise ValueError("managed binding workspace revision conflict")
        actual_engine = str(previous.get("engineSessionID") or "") or None
        actual_map = int(previous.get("mapRevision") or 0)
        actual_mapping = int(previous.get("mappingRevision") or 0)
        if expected_engine_session_id is not _EXPECTED_UNSET and actual_engine != expected_engine_session_id:
            raise ValueError("managed binding engine-session conflict")
        if expected_map_revision is not _EXPECTED_UNSET and actual_map != int(expected_map_revision):
            raise ValueError("managed binding map revision conflict")
        if expected_mapping_revision is not _EXPECTED_UNSET and actual_mapping != int(expected_mapping_revision):
            raise ValueError("managed binding mapping revision conflict")
        candidate = dict(binding)
        candidate["workspaceRevision"] = int(candidate.get("workspaceRevision", actual))
        if previous and candidate.get("engineSessionID") != previous.get("engineSessionID"):
            same_axes = all(
                candidate.get(key) == previous.get(key)
                for key in ("workspaceRevision", "mapRevision", "mappingRevision")
            )
            if same_axes:
                raise ValueError("managed binding engine replacement lacks a mapping revision")
        state["managed_binding"] = candidate
        before = {key: value for key, value in state.items() if key != "revision"}
        old_state = dict(session.mimo_state or {})
        old_state.pop("revision", None)
        revision = int(session.mimo_state.get("revision", 0) if session.mimo_state else 0)
        if before != old_state:
            revision += 1
        state["revision"] = revision
        session.mimo_state = state
        db.commit()
        return dict(candidate)
    finally:
        db.close()


def delete_managed_binding(
    session_id: str,
    *,
    owner: Optional[str] = None,
    expected_workspace_revision: Optional[int] = None,
    expected_engine_session_id: object = _EXPECTED_UNSET,
    expected_map_revision: object = _EXPECTED_UNSET,
    expected_mapping_revision: object = _EXPECTED_UNSET,
) -> None:
    """Remove a host binding as compensation for an unexposed engine.

    This is deliberately owner-qualified and CAS-protected.  The engine
    admission transaction uses it only while the durable map still points at
    the candidate, so a competing winner cannot have its binding removed by a
    stale cleanup path.
    """
    if not owner:
        raise ValueError("managed binding deletes require an owner")
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session = (
            db.query(Session)
            .filter(Session.id == session_id, Session.owner == owner)
            .first()
        )
        if session is None:
            raise KeyError(f"Canonical session {session_id!r} not found")
        state = dict(session.mimo_state or {})
        previous = state.get("managed_binding")
        actual = int(previous.get("workspaceRevision") or 0) if isinstance(previous, dict) else 0
        if expected_workspace_revision is not None and actual != int(expected_workspace_revision):
            raise ValueError("managed binding workspace revision conflict")
        actual_engine = str(previous.get("engineSessionID") or "") or None if isinstance(previous, dict) else None
        actual_map = int(previous.get("mapRevision") or 0) if isinstance(previous, dict) else 0
        actual_mapping = int(previous.get("mappingRevision") or 0) if isinstance(previous, dict) else 0
        if expected_engine_session_id is not _EXPECTED_UNSET and actual_engine != expected_engine_session_id:
            raise ValueError("managed binding engine-session conflict")
        if expected_map_revision is not _EXPECTED_UNSET and actual_map != int(expected_map_revision):
            raise ValueError("managed binding map revision conflict")
        if expected_mapping_revision is not _EXPECTED_UNSET and actual_mapping != int(expected_mapping_revision):
            raise ValueError("managed binding mapping revision conflict")
        if not isinstance(previous, dict):
            db.commit()
            return
        state.pop("managed_binding", None)
        state["revision"] = int(state.get("revision") or 0) + 1
        session.mimo_state = state
        db.commit()
    finally:
        db.close()


async def purge_execution_projection(
    supervisor,
    session_id: str,
    *,
    owner: Optional[str] = None,
) -> bool:
    """Securely remove any Open Clank agent execution state for a canonical session."""
    if owner is None:
        raise ValueError("projection purge requires an owner")
    row = get_projection(session_id, owner=owner)
    if row is not None and owner is not None and row["owner"] != owner:
        raise PermissionError("Open Clank agent projection belongs to another owner")
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
        raise RuntimeError("Open Clank agent is unavailable; execution state was not deleted")

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
