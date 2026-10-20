"""Bounded, resumable Stats backfill from canonical assistant messages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from core.database import ChatMessage, Session, SessionLocal
from core.stats_models import StatsEvent
from .ledger import _message_replay_key, capture_message_event


@dataclass(frozen=True)
class BackfillResult:
    revision: str
    scanned: int
    admitted: int
    inserted: int
    duplicates: int
    skipped: int
    cursor: str | None
    complete: bool
    cancelled: bool
    dry_run: bool
    metrics: dict[str, int]


def backfill_messages(*, db=None, owner: str, batch_size: int = 250, cursor: str | None = None,
                      revision: str = "s01-backfill-v1", dry_run: bool = False,
                      cancel: Callable[[], bool] | None = None) -> BackfillResult:
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("owner is required for Stats backfill")
    try:
        requested_size = int(batch_size)
    except (TypeError, ValueError):
        raise ValueError("batch_size must be an integer") from None
    if requested_size < 1:
        raise ValueError("batch_size must be positive")
    if cursor is not None and not str(cursor).strip():
        raise ValueError("cursor must be non-empty when supplied")
    if not str(revision or "").strip():
        raise ValueError("revision is required")
    own_db = db is None
    db = db or SessionLocal()
    size = min(requested_size, 1000)
    scanned = admitted = inserted = duplicates = skipped = 0
    last_id = cursor
    original_cursor = cursor
    savepoint = None if dry_run else db.begin_nested()
    try:
        query = (db.query(ChatMessage, Session).join(Session, Session.id == ChatMessage.session_id)
                 .filter(ChatMessage.role == "assistant", Session.owner == owner).order_by(ChatMessage.id))
        if cursor:
            query = query.filter(ChatMessage.id > cursor)
        for message, session in query.limit(size).all():
            if cancel and cancel():
                if savepoint is not None:
                    savepoint.rollback()
                if own_db:
                    db.rollback()
                # A cancelled batch is atomic.  Do not hand the caller a
                # cursor or counts that describe rows rolled back by the
                # savepoint.
                return BackfillResult(revision, scanned, admitted, 0, 0, skipped,
                                      original_cursor, False, True, dry_run,
                                      {"scanned": 0, "admitted": 0, "inserted": 0,
                                       "duplicates": 0, "skipped": 0})
            scanned += 1
            last_id = message.id
            try:
                metadata = json.loads(message.meta_data) if message.meta_data else {}
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not isinstance(metadata, dict):
                skipped += 1
                continue
            eligible = int(
                message.role == "assistant"
                and not metadata.get("incognito")
                and not metadata.get("stats_disabled")
            )
            replay_key = _message_replay_key(str(message.id))
            if dry_run:
                admitted += eligible
                exists = db.query(StatsEvent).filter(StatsEvent.replay_key == replay_key).first() is not None
                duplicates += int(eligible and exists)
                inserted += int(eligible and not exists)
            else:
                existed = db.query(StatsEvent).filter(StatsEvent.replay_key == replay_key).first() is not None
                event = capture_message_event(
                    db, session, message,
                    type("Message", (), {"role": message.role, "metadata": metadata})(),
                    producer_revision=revision,
                )
                admitted += int(event is not None)
                if event is not None:
                    inserted += int(not existed)
                    duplicates += int(existed)
        if savepoint is not None:
            savepoint.commit()
        if own_db and not dry_run:
            db.commit()
        return BackfillResult(revision, scanned, admitted, inserted, duplicates, skipped,
                              last_id, scanned < size, False, dry_run,
                              {"scanned": scanned, "admitted": admitted,
                               "inserted": inserted, "duplicates": duplicates,
                               "skipped": skipped})
    except Exception:
        if savepoint is not None and savepoint.is_active:
            savepoint.rollback()
        if own_db:
            db.rollback()
        raise
    finally:
        if own_db:
            db.close()
