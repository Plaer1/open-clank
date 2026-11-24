"""Resolve an already-authorized chat against its existing native mapping."""
from __future__ import annotations
from src.openclank.orchestration_projection import resolve_binding


def existing_chat_handoff(snapshot, *, owner: str, host_chat_id: str, expected_map_revision: int | None = None):
    revision = snapshot.get('mapRevision') if expected_map_revision is None and isinstance(snapshot, dict) else expected_map_revision
    binding = resolve_binding(snapshot, owner, host_chat_id, revision)
    return {'owner': owner, 'host_chat_id': binding.host_chat_id, 'engine_session_id': binding.engine_session_id,
            'map_revision': binding.map_revision, 'binding_revision': binding.binding_revision, 'actor_id': 'main'}
