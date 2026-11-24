"""Bounded, owner-scoped recorded storage accounting; never filesystem usage."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from sqlalchemy import String, cast, func
from core.operation_models import ArtifactRecord
from src.openclank.artifacts import media_artifact_owner
from src.openclank.logging_models import LoggingAttemptDetail, LoggingCaptureBody, LoggingCaptureEvent


def _read(factory, cancel, deadline, operation):
    db = factory()
    raw = None
    try:
        connection = db.connection()
        if connection.dialect.name != 'sqlite':
            raise RuntimeError('storage inventory requires SQLite')
        raw = connection.connection.driver_connection
        raw.set_progress_handler(lambda: int(cancel.is_set() or time.monotonic() >= deadline), 1000)
        if cancel.is_set() or time.monotonic() >= deadline:
            raise TimeoutError('storage inventory deadline')
        result = operation(db)
        if cancel.is_set() or time.monotonic() >= deadline:
            raise TimeoutError('storage inventory deadline')
        return result
    finally:
        if raw is not None:
            raw.set_progress_handler(None, 0)
        db.close()


def inventory(*, owner, account_id, logging_factory, artifact_factory, cancel, deadline):
    def capture(db):
        def amount(model):
            return int(db.query(func.coalesce(func.sum(model.size_bytes), 0)).filter_by(owner=owner).scalar())
        estimate = func.length(cast(LoggingAttemptDetail.attribution, String)) + func.length(cast(LoggingAttemptDetail.timing, String)) + 512
        metadata = db.query(func.count(LoggingAttemptDetail.id), func.coalesce(func.sum(estimate), 0)).filter_by(owner=owner).one()
        pending = db.query(func.count(LoggingAttemptDetail.id)).filter_by(owner=owner, capture_state='pending').scalar()
        return {'body_uncompressed_bytes':amount(LoggingCaptureBody), 'event_serialized_bytes':amount(LoggingCaptureEvent),
                'metadata_records':int(metadata[0]), 'metadata_estimated_bytes':int(metadata[1]), 'pending_captures':int(pending)}
    def artifacts(db):
        result = {}
        for label, namespace in (('owner', owner), ('account_media', media_artifact_owner(account_id))):
            result[label] = {}
            for state in ('staged', 'acknowledged'):
                count, size = db.query(func.count(ArtifactRecord.id), func.coalesce(func.sum(ArtifactRecord.size_bytes), 0)).filter_by(owner=namespace, state=state).one()
                result[label][state] = {'records':int(count), 'recorded_bytes':int(size)}
        return result
    return {'version':1, 'as_of':datetime.now(timezone.utc).isoformat(), 'capture':_read(logging_factory,cancel,deadline,capture),
            'artifacts':_read(artifact_factory,cancel,deadline,artifacts), 'physical_unique_bytes':None,
            'unavailable':['files_vault','lore','chat_archive','native_memory','code_index']}
