"""Resolve the active pinned Frankenmemory authority for Hex operations."""
from pathlib import Path
import sqlite3
from src.general_hex_library import GeneralHexError, ensure_general_hex_schema
from src.general_hex_composition import GeneralHexComposition, ensure_general_hex_snapshot_schema
from src.general_hex_exchange import ensure_general_hex_exchange_schema


def active_hex_db_path(provider):
    resolver = getattr(provider, '_local_db_path', None)
    path = resolver() if callable(resolver) else None
    return str(path) if path else None


def prepare_hex_authority(db_path):
    if not db_path or not Path(db_path).is_file():
        raise GeneralHexError('General Hex authority is unavailable; a pinned local Frankenmemory store is required')
    try:
        ensure_general_hex_schema(db_path)
        ensure_general_hex_snapshot_schema(db_path)
        ensure_general_hex_exchange_schema(db_path)
    except sqlite3.Error as exc:
        raise GeneralHexError('General Hex local authority cannot be opened') from exc
    return str(db_path)


def turn_hex_snapshot(provider, *, owner_id, boundary_id, workspace=None, workspace_id=None, task_id=None, budget_chars=24000, persist=True):
    try:
        path = prepare_hex_authority(active_hex_db_path(provider))
        composition = GeneralHexComposition(path)
        context = dict(workspace=workspace,workspace_id=workspace_id,task_id=task_id,budget_chars=budget_chars)
        if not persist:
            snapshot = composition.preview(owner_id, **context)
            snapshot['boundary_id'] = boundary_id
            snapshot['retention'] = 'ephemeral'
            return snapshot
        return composition.snapshot(owner_id,boundary_id,**context)
    except sqlite3.Error as exc:
        raise GeneralHexError('General Hex snapshot unavailable from the pinned store') from exc
