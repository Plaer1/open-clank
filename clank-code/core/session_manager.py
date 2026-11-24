# core/session_manager.py
"""
Session management — all session business logic and DB operations.

This is the single place that handles:
- Loading/saving sessions to database
- Adding messages to sessions
- Session lifecycle (create, archive, delete)
"""

import asyncio
from copy import deepcopy
from functools import wraps
import hashlib
import json
import time
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from .database import Session as DbSession, ChatMessage as DbChatMessage, Document as DbDocument, SessionLocal, utcnow_naive
from .models import Session, ChatMessage
from src.attachment_refs import attachment_refs_from_metadata, persistable_message_content
from src.upload_handler import reserve_message_upload_references

# Bound at module scope so archive-import failure paths can raise a real
# recoverable type instead of NameError (N2).
try:
    from src.openclank.conversation_archive import ArchiveUnavailableError
except Exception:  # pragma: no cover - module present in normal deployments
    class ArchiveUnavailableError(Exception):
        """Fallback recoverable type when conversation_archive is unimportable."""

        message: str = "conversation archive unavailable"
        retry_after_ms: int = 250

        def __str__(self) -> str:
            return getattr(self, "message", "conversation archive unavailable")

# Re-export singleton accessors from models for convenience
from .models import set_session_manager_instance, get_session_manager_instance

logger = logging.getLogger(__name__)


def _cached_history_mutation(fn):
    """Serialize one synchronous cache/DB mutation on its existing Session."""
    @wraps(fn)
    def wrapped(self, session_id, *args, **kwargs):
        session = kwargs.get('_session') or self.sessions.get(session_id)
        if session is None:
            return fn(self, session_id, *args, **kwargs)
        with session._history_lock:
            if self.sessions.get(session_id) is not session:
                raise RuntimeError('Session cache changed')
            return fn(self, session_id, *args, **kwargs)
    return wrapped


def _now_ms() -> int:
    return int(time.time() * 1000)


def _message_timestamp_iso(value: Optional[datetime]) -> Optional[str]:
    """Return a stable ISO timestamp for chat message metadata."""
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _parse_msg_content(raw):
    """Parse message content from DB — deserialises JSON arrays back to lists
    (multimodal content with image/audio attachments)."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.startswith('[{') and '"type"' in raw:
        try:
            parsed = json.loads(raw)
            # Only treat as serialized multimodal content when EVERY element is
            # a dict whose "type" is a recognized content-block kind. Otherwise a
            # plain text message that merely *looks* like a JSON array of objects
            # (e.g. a user pasting an API schema/sample with a "type" field) was
            # silently parsed back into a list, destroying the original string.
            _BLOCK_TYPES = {
                "text", "image", "image_url", "audio", "input_audio",
                "input_image", "document", "file",
            }
            if (isinstance(parsed, list) and parsed
                    and all(isinstance(p, dict) and p.get("type") in _BLOCK_TYPES
                            for p in parsed)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return raw


async def _await_guarded_writer(function, *args, **kwargs):
    """Keep the caller's live guard until the synchronous writer settles."""
    import asyncio
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        try:
            task.result()
        except Exception:
            pass
        raise


def _history_fingerprint(session):
    def values(item):
        if isinstance(item, dict):
            return id(item), str(item.get('role')), str(item.get('content')), str(item.get('metadata'))
        return id(item), str(getattr(item,'role',None)), str(getattr(item,'content',None)), str(getattr(item,'metadata',None))
    return tuple(values(item) for item in session.history)


class SessionManager:
    """
    Manages chat sessions with database persistence.

    Usage:
        manager = SessionManager()
        session = manager.create_session(id, name, url, model)
        manager.add_message(session.id, ChatMessage("user", "hello"))
        session = manager.get_session(session_id)
    """

    def __init__(self, sessions_file: str = None):
        # sessions_file kept for backward compat, not used
        self.sessions: Dict[str, Session] = {}
        self.upload_handler = None
        self.load_sessions()

    async def publish_bound_shell(self, proof, *, mapping, chat_guard, target_authority):
        """Publish or reconcile one host-issued shell result under its held chat guard.

        A dispatching proof can insert only once. An absent row on a later
        reconciliation is unknown: it may have been inserted and then erased.
        The caller retains the account admission and chat guard until return.
        """
        from src.bg_jobs import BoundShellPublication
        from src.openclank.session_map import OwnerSessionMap
        from src.openclank.memory_job_admission import TargetAuthority
        if (type(proof) is not BoundShellPublication or type(mapping) is not OwnerSessionMap
                or type(target_authority) is not TargetAuthority or proof.privacy != 'normal'):
            return 'unavailable'
        state = getattr(self, 'lifecycle_context', None)
        auth = getattr(state, 'auth_manager', None)
        supervisor = getattr(state, 'mimo_supervisor', None)
        try:
            pool = supervisor.owner_lifecycle_pool()
            if (target_authority._auth is not auth or target_authority._supervisor is not pool
                    or mapping.owner != proof.owner
                    or mapping.path.resolve() != (pool._runtime_home(proof.owner) / 'session-map.json').resolve()):
                return 'unavailable'
            target = target_authority.resolve(proof.account_id)
            def current():
                proof.assert_current()
                mapping.validate_chat_guard(proof.session_id, chat_guard)
                if (auth.account_id(proof.owner) != proof.account_id
                        or auth.username_for_account_id(proof.account_id) != proof.owner
                        or target_authority.resolve(proof.account_id) != target):
                    raise ValueError('publication authority changed')
            current()
            result = await _await_guarded_writer(self._publish_bound_shell_locked, proof, current)
            current()
            return result
        except Exception:
            logger.warning('Bound shell publication unavailable')
            return 'unknown' if getattr(proof, '_insert_used', [False])[0] else 'unavailable'

    def _publish_bound_shell_locked(self, proof, current):
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.chat_source_bridge import available, enqueue_new, deliver
        archive = get_conversation_archive()
        current()
        session = self.sessions.get(proof.session_id)
        if session is None:
            return 'unavailable'
        with session._history_lock:
            if (self.sessions.get(proof.session_id) is not session or session.owner != proof.owner
                    or session.workspace_id != proof.workspace_id or session.incognito
                    or archive.chat_erasure_started(owner=proof.owner, chat_id=proof.session_id)):
                return 'unavailable'
            db = SessionLocal()
            claimed = False
            try:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                parent = db.query(DbSession).filter(DbSession.id == proof.session_id).first()
                if (parent is None or parent.owner != proof.owner or parent.workspace_id != proof.workspace_id
                        or parent.archived):
                    return 'unavailable'
                existing = db.query(DbChatMessage).filter(DbChatMessage.id == proof.message_id).first()
                if existing is not None:
                    if (existing.session_id != proof.session_id or existing.role != 'assistant'
                            or hashlib.sha256(existing.content.encode('utf-8')).hexdigest() != proof.body_sha256
                            or hashlib.sha256((existing.meta_data or '').encode('utf-8')).hexdigest() != proof.metadata_sha256):
                        return 'conflict'
                    from src.openclank.session_operations import visibility
                    excluded = visibility(parent.mimo_state or {}, require_settled=True)
                    rows = db.query(DbChatMessage).filter(
                        DbChatMessage.session_id == proof.session_id
                    ).order_by(DbChatMessage.timestamp, DbChatMessage.id).all()
                    visible = [row for row in rows if row.id not in excluded]
                    cached = list(session.history)
                    def cache_id(item):
                        metadata = item.metadata if isinstance(item, ChatMessage) else item.get('metadata')
                        return metadata.get('_db_id') if isinstance(metadata, dict) else None
                    ids = [cache_id(item) for item in cached]
                    if proof.message_id in excluded:
                        if proof.message_id in ids:
                            return 'unavailable'
                        current()
                        return 'already_present'
                    if ids.count(proof.message_id) == 1:
                        item = cached[ids.index(proof.message_id)]
                        role = item.role if isinstance(item, ChatMessage) else item.get('role')
                        content = item.content if isinstance(item, ChatMessage) else item.get('content')
                        if role != 'assistant' or content != proof.body:
                            return 'conflict'
                        current()
                        return 'already_present'
                    if proof.message_id in ids or any(not item for item in ids):
                        return 'unavailable'
                    others = [row for row in visible if row.id != proof.message_id]
                    if ids != [row.id for row in others]:
                        return 'unavailable'
                    for item, row in zip(cached, others):
                        role = item.role if isinstance(item, ChatMessage) else item.get('role')
                        content = item.content if isinstance(item, ChatMessage) else item.get('content')
                        if role != row.role or content != _parse_msg_content(row.content):
                            return 'unavailable'
                    metadata = json.loads(existing.meta_data or '{}')
                    if type(metadata) is not dict:
                        return 'unavailable'
                    metadata['_db_id'] = existing.id
                    metadata.setdefault('timestamp', _message_timestamp_iso(existing.timestamp))
                    recovered = ChatMessage(role='assistant', content=existing.content, metadata=metadata)
                    position = next((i for i, row in enumerate(visible) if row.id == proof.message_id), None)
                    if position is None:
                        return 'unavailable'
                    current()
                    session.history = cached[:position] + [recovered] + cached[position:]
                    session._history = session.history
                    session.message_count = len(session.history)
                    return 'already_present'
                if not session.history and session.message_count:
                    return 'unavailable'
                metadata = json.loads(proof.metadata_json)
                if type(metadata) is not dict:
                    raise ValueError('invalid publication metadata')
                current()
                if not proof.claim_insert():
                    return 'unknown'
                claimed = True
                now = datetime.utcnow()
                record = DbChatMessage(id=proof.message_id, session_id=proof.session_id,
                    role='assistant', content=proof.body, meta_data=proof.metadata_json, timestamp=now)
                db.add(record)
                source_enabled = available(db)
                source_owner = None
                if source_enabled:
                    source_owner = archive.resolve_owner(proof.owner)
                    if source_owner != proof.account_id:
                        raise ValueError('publication source owner changed')
                    db.flush()
                    enqueue_new(db, archive=archive, owner=source_owner, chat_id=proof.session_id,
                        message_id=proof.message_id, body=proof.body, origin=None)
                message = ChatMessage(role='assistant', content=proof.body, metadata=dict(metadata))
                from services.stats.ledger import capture_message_event
                capture_message_event(db, parent, record, message)
                parent.message_count = int(parent.message_count or 0) + 1
                parent.last_accessed = parent.last_message_at = datetime.now(timezone.utc)
                current()
                db.commit()
                message.metadata['_db_id'] = proof.message_id
                session.history.append(message)
                session._history = session.history
                session.message_count = len(session.history)
                self._archive_message_parts(proof.session_id, message, msg_id=proof.message_id,
                    revision=1, event_sequence=len(session.history) - 1, skip_body=source_enabled)
                if source_enabled:
                    try:
                        deliver(db, archive, owner=source_owner, chat_id=proof.session_id,
                            message_id=proof.message_id, revision=1)
                    except Exception:
                        db.rollback()
                        logger.warning('host source delivery pending')
                return 'inserted'
            except Exception:
                db.rollback()
                if claimed:
                    return 'unknown'
                raise
            finally:
                db.close()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_sessions(self):
        """Load recent session METADATA from the database — messages are
        hydrated on demand by `get_session`. Previously this walked every
        message of every session into RAM at boot, which on a long-running
        personal-server box could be tens of thousands of rows held forever
        in `self.sessions`.
        """
        db = SessionLocal()
        try:
            db_sessions = db.query(DbSession).filter(
                DbSession.archived == False,
                DbSession.message_count > 0,
            ).order_by(DbSession.last_accessed.desc()).limit(100).all()

            loaded_count = 0
            for db_session in db_sessions:
                try:
                    session = self._db_to_session_meta(db_session)
                    if session is not None:
                        self.sessions[db_session.id] = session
                        loaded_count += 1
                except Exception as e:
                    logger.error(f"Error loading session {db_session.id}: {e}")
                    continue

            logger.info(f"Loaded {loaded_count} session(s) (metadata only)")

        except Exception as e:
            logger.error(f"Error loading sessions: {e}")
            self.sessions = {}
        finally:
            db.close()

    def _db_to_session_meta(self, db_session: DbSession) -> Optional[Session]:
        """Build a Session with empty history. `get_session` will hydrate
        messages from the DB on first read."""
        headers = db_session.headers
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError:
                headers = {}
        session = Session(
            id=db_session.id,
            name=db_session.name,
            endpoint_url=db_session.endpoint_url,
            model=db_session.model,
            endpoint_id=getattr(db_session, "endpoint_id", None),
            provider_model_route_id=getattr(
                db_session, "provider_model_route_id", None
            ),
            rag=db_session.rag,
            archived=db_session.archived,
            headers=headers,
            history=[],
            owner=getattr(db_session, "owner", None),
            workspace_id=getattr(db_session, "workspace_id", None),
            response_style=db_session.response_style,
            response_style_revision=db_session.response_style_revision,
            is_important=getattr(db_session, "is_important", False) or False,
        )
        session.message_count = getattr(db_session, "message_count", 0) or 0
        return session

    def _db_to_session(self, db_session: DbSession, db) -> Optional[Session]:
        """Convert a database session to a Session object."""
        history = []

        # Try relationship first, then direct query
        if db_session.messages:
            for db_msg in db_session.messages:
                meta = json.loads(db_msg.meta_data) if db_msg.meta_data else {}
                if meta is None: meta = {}
                meta['_db_id'] = db_msg.id
                meta.setdefault('timestamp', _message_timestamp_iso(db_msg.timestamp))
                history.append(ChatMessage(
                    role=db_msg.role,
                    content=_parse_msg_content(db_msg.content),
                    metadata=meta,
                ))
        else:
            db_messages = db.query(DbChatMessage).filter(
                DbChatMessage.session_id == db_session.id
            ).order_by(DbChatMessage.timestamp).all()

            for db_msg in db_messages:
                meta = json.loads(db_msg.meta_data) if db_msg.meta_data else {}
                if meta is None: meta = {}
                meta['_db_id'] = db_msg.id
                meta.setdefault('timestamp', _message_timestamp_iso(db_msg.timestamp))
                history.append(ChatMessage(
                    role=db_msg.role,
                    content=_parse_msg_content(db_msg.content),
                    metadata=meta,
                ))

        if not history:
            return None

        # Parse headers
        headers = db_session.headers
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError:
                headers = {}

        session = Session(
            id=db_session.id,
            name=db_session.name,
            endpoint_url=db_session.endpoint_url,
            model=db_session.model,
            endpoint_id=getattr(db_session, "endpoint_id", None),
            provider_model_route_id=getattr(
                db_session, "provider_model_route_id", None
            ),
            rag=db_session.rag,
            archived=db_session.archived,
            headers=headers,
            history=history,
            owner=getattr(db_session, 'owner', None),
            workspace_id=getattr(db_session, "workspace_id", None),
            response_style=db_session.response_style,
            response_style_revision=db_session.response_style_revision,
            is_important=getattr(db_session, 'is_important', False) or False,
        )

        session.message_count = getattr(db_session, 'message_count', len(history))
        return session

    # ------------------------------------------------------------------
    # Message operations
    # ------------------------------------------------------------------

    def _archive_message_parts(
        self,
        session_id: str,
        message: ChatMessage,
        *,
        msg_id: str,
        revision: int = 1,
        event_sequence: int = 0,
        role_override: Optional[str] = None,
        skip_body: bool = False,
    ) -> None:
        """Archive one message as ordered source parts (not a text pair).

        History capture is not memory admission. This runs regardless of
        ``memory_mode`` so the canonical archive stays lossless; memory
        extraction remains separately gated at ``src.memory_gate``.
        Incognito callers never reach this path (they use a request-local store).
        """
        try:
            from src.openclank.conversation_archive import SourcePart, get_conversation_archive
        except Exception:
            logger.debug("conversation archive unavailable", exc_info=True)
            return

        try:
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                owner = getattr(db_session, "owner", None) or ""
                event_workspace = getattr(db_session, "workspace_id", None)
            finally:
                db.close()
        except Exception:
            owner = ""
            event_workspace = None

        metadata = dict(getattr(message, "metadata", None) or {})
        content = getattr(message, "content", None)
        alias_id = str(metadata.get("_db_id") or msg_id)
        parts: list[SourcePart] = []

        def _part(part_id: str, part_type: str, body, *, part_seq: int, assets=()) -> SourcePart:
            return SourcePart(
                owner=str(owner or ""),
                chat_id=str(session_id),
                actor_id=str(metadata.get("actor_id") or "main"),
                message_id=str(msg_id),
                part_id=str(part_id),
                revision=int(revision),
                runtime_generation=str(metadata.get("runtime_generation") or "0"),
                event_sequence=int(event_sequence or part_seq),
                event_workspace=event_workspace,
                event_project=metadata.get("event_project"),
                role=str(role_override or getattr(message, "role", "user") or "user"),
                part_type=part_type,
                content=body,
                aliases=(("host_message", alias_id),),
                assets=tuple(assets),
                time_created=_now_ms(),
                time_updated=_now_ms(),
            )

        seq = 0
        # Always archive a text (or block) body part so text-pair capture is
        # never the only durable record of a turn.
        if content is not None and not skip_body:
            parts.append(_part("body", "text", content, part_seq=seq))
            seq += 1

        # Tool calls / tool results / structured blocks ride on content lists
        # or metadata. Capture each as its own ordered part.
        if isinstance(content, list):
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type") or "block")
                if block_type in {"text", "image", "file"}:
                    continue
                parts.append(_part(f"block:{index}", block_type, block, part_seq=seq))
                seq += 1

        for index, tool_call in enumerate(metadata.get("tool_calls") or []):
            if isinstance(tool_call, dict):
                parts.append(_part(f"tool_call:{index}", "tool_call", tool_call, part_seq=seq))
                seq += 1

        for index, tool_result in enumerate(metadata.get("tool_results") or []):
            if isinstance(tool_result, dict):
                parts.append(_part(f"tool_result:{index}", "tool_result", tool_result, part_seq=seq))
                seq += 1

        asset_specs = []
        for ref in attachment_refs_from_metadata(metadata):
            asset_specs.append({
                "asset_id": ref.get("attachment_id") or ref.get("id") or "",
                "content_hash": ref.get("checksum_sha256"),
                "mime_type": ref.get("mime"),
                "byte_size": ref.get("size"),
                "provenance": {"source": "host_upload", "name": ref.get("name")},
            })
        if asset_specs and parts and not skip_body:
            # Attach assets to the body part revision (same dedupe key).
            from dataclasses import replace as _dc_replace
            parts[0] = _dc_replace(parts[0], assets=tuple(asset_specs))
        elif asset_specs:
            parts.append(_part("assets", "asset_refs", {"assets": asset_specs}, part_seq=seq, assets=asset_specs))

        if not parts:
            return
        try:
            get_conversation_archive().append_parts(parts)
        except Exception:
            # Archive delivery failure must not fail live chat persistence.
            # Outbox/backfill can retry; source is still in chat_messages.
            logger.warning("conversation archive append failed", exc_info=True)

    def add_message(self, session_id: str, message: ChatMessage):
        """
        Add a message to a session and persist to database.

        Updates the authoritative history list and persists through this
        manager directly so tests and temporary managers do not depend on the
        process-wide session-manager singleton.

        Args:
            session_id: Session ID
            message: ChatMessage to add
        """
        session = self.get_session(session_id)
        with session._history_lock:
            if self.sessions.get(session_id) is not session:
                raise RuntimeError("Session cache changed")
            persisted = self._persist_message(session_id, message)
            if not persisted:
                raise RuntimeError(f"Session {session_id} no longer exists")
            session.history.append(message)
            session._history = session.history
            session.message_count = len(session.history)

    def _persist_message(self, session_id: str, message: ChatMessage, *, origin_admission=None, connector_guard=None, channel_guard=None, tui_guard=None):
        """Persist a single message to the database."""
        db = SessionLocal()
        try:
            if connector_guard is not None and tui_guard is not None:
                raise ValueError('Multiple turn authorities unavailable')
            if connector_guard is not None or tui_guard is not None:
                from src.openclank.connector_turns import ConnectorTurnGuard
                from src.openclank.tui_turns import TuiTurnGuard
                guard = connector_guard if connector_guard is not None else tui_guard
                expected = ConnectorTurnGuard if connector_guard is not None else TuiTurnGuard
                if type(guard) is not expected or db.bind.dialect.name != 'sqlite':
                    raise ValueError('Turn transaction unavailable')
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if connector_guard is not None or tui_guard is not None:
                guard.prepare(db, db_session, message)
            if channel_guard is not None:
                from src.openclank.channels.turn_guard import ChannelTurnGuard
                if type(channel_guard) is not ChannelTurnGuard: raise ValueError('Channel transaction unavailable')
                channel_guard.prepare(db_session, message)
            if db_session is None:
                # A stream/tool callback can outlive a session delete. Do not
                # create a chat_messages row with no parent session; also drop
                # any stale cached session so later writes fail closed too.
                self.sessions.pop(session_id, None)
                logger.warning("Dropping message for deleted session %s", session_id)
                return False

            missing_upload_id = reserve_message_upload_references(
                getattr(self, "upload_handler", None),
                getattr(db_session, "owner", None),
                message.content,
                message.metadata,
                session_id=session_id,
            )
            if missing_upload_id:
                raise ValueError(
                    f"Referenced upload is no longer available: {missing_upload_id}"
                )

            if not getattr(self.sessions.get(session_id), "incognito", False):
                from src.openclank.conversation_archive import get_conversation_archive
                if get_conversation_archive().chat_erasure_started(owner=db_session.owner, chat_id=session_id):
                    raise RuntimeError("Conversation deletion is pending; history writes are closed")

            reserved_id = str(
                getattr(message, "persistence_id", None) or ""
            ).strip()
            message_root = str(
                (message.metadata or {}).get("root_operation_id") or ""
            ).strip()
            if reserved_id and (
                message.role != "user"
                or message_root != reserved_id
                or len(reserved_id) > 192
                or not reserved_id[0].isalnum()
                or not all(
                    char.isalnum() or char in "._:-"
                    for char in reserved_id
                )
            ):
                raise ValueError("Invalid reserved chat-message identity")
            msg_id = reserved_id or str(uuid.uuid4())
            msg_time = datetime.utcnow()
            if message.metadata is None:
                message.metadata = {}
            message.metadata.setdefault('timestamp', _message_timestamp_iso(msg_time))
            # Multimodal content may contain provider data URLs for the live
            # model call. Persist only readable text plus attachment references
            # so chat_messages/FTS do not duplicate upload bytes.
            _content = persistable_message_content(message.content, message.metadata)
            db_message = DbChatMessage(
                id=msg_id,
                session_id=session_id,
                role=message.role,
                content=_content,
                meta_data=json.dumps(message.metadata) if message.metadata else None,
                timestamp=msg_time,
            )
            db.add(db_message)
            from src.openclank.chat_source_bridge import available, enqueue_new, deliver
            source_enabled = available(db)
            source_owner = None
            if source_enabled:
                from src.openclank.conversation_archive import get_conversation_archive
                source_archive = get_conversation_archive()
                source_owner = source_archive.resolve_owner(db_session.owner)
                db.flush()
                if origin_admission is not None and (origin_admission.owner != source_owner or origin_admission.chat_id != session_id or origin_admission.root_operation_id != msg_id):
                    logger.warning("host origin admission no longer matches canonical scope")
                    origin_admission = None
                enqueue_new(db, archive=source_archive, owner=source_owner, chat_id=session_id, message_id=msg_id, body=_content, origin=origin_admission)
            # Numeric Stats facts share the canonical message transaction.
            from services.stats.ledger import capture_message_event
            capture_message_event(db, db_session, db_message, message)

            if session_id in self.sessions:
                history = self.sessions[session_id].history
                db_session.message_count = len(history) + (0 if any(item is message for item in history) else 1)
            else:
                db_session.message_count = 0
            _now = datetime.now(timezone.utc)
            db_session.last_accessed = _now
            # Clean "last conversation" timestamp — only bumped here on a
            # real message persist, so it powers an accurate "Last active"
            # sort that ignores renames / model swaps / mere opens.
            db_session.last_message_at = _now

            if connector_guard is not None or tui_guard is not None: guard.before_commit()
            if channel_guard is not None: channel_guard.assert_current()
            db.commit()

            # Store DB ID on the in-memory message for edit/delete by ID
            message.metadata['_db_id'] = msg_id

            # Full-part source archive (lossless ordered parts + outbox). This
            # is history capture, not memory admission, and is not gated by
            # memory_mode. Failure here never blocks live chat persistence.
            self._archive_message_parts(
                session_id,
                message,
                msg_id=msg_id,
                revision=1,
                event_sequence=len(self.sessions.get(session_id).history) if session_id in self.sessions else 0,
                skip_body=source_enabled,
            )
            if source_enabled:
                try:
                    deliver(db, source_archive, owner=source_owner, chat_id=session_id, message_id=msg_id, revision=1)
                except Exception:
                    db.rollback()
                    logger.warning("host source delivery pending", exc_info=True)

            logger.debug(f"Persisted message to session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Error persisting message: {e}")
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _lock_source_intents(db, session_id):
        """Keep pending origin completions from racing a guarded rewrite."""
        from src.openclank.chat_source_bridge import origin_available
        from sqlalchemy import text
        if db.get_bind().dialect.name == 'postgresql' and origin_available(db):
            # Exhaust the cursor so every existing intent is row-locked until
            # the host transaction commits; no native await occurs here.
            for _ in db.execute(text('SELECT message_id FROM host_source_delivery_intent WHERE chat_id=:chat FOR UPDATE').execution_options(stream_results=True, yield_per=128), {'chat':session_id}):
                pass

    def _truncate_snapshot(self, db, session_id, keep_count):
        row = db.query(DbSession).filter(DbSession.id == session_id).first()
        if row is None or type(keep_count) is not int or keep_count < 0:
            raise ValueError('truncate selection unavailable')
        rows = db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id).order_by(DbChatMessage.timestamp, DbChatMessage.id).all()
        ids = tuple(item.id for item in rows[keep_count:])
        roots = set()
        from src.openclank.chat_source_bridge import origin_available
        from src.openclank.conversation_archive import get_conversation_archive
        from sqlalchemy import text, bindparam
        if ids and origin_available(db):
            account = get_conversation_archive().resolve_owner(row.owner)
            stmt = text("SELECT DISTINCT message_id FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND part_id='body' AND revision=1 AND origin_assistant_id IN :ids").bindparams(bindparam('ids',expanding=True))
            for offset in range(0,len(ids),128):
                roots.update(item[0] for item in db.execute(stmt,{'owner':account,'chat':session_id,'ids':ids[offset:offset+128]}))
        from src.openclank.chat_source_bridge import available
        versions = {}
        if available(db):
            versions = {item[0]:(item[1],item[2]) for item in db.execute(text('SELECT id,source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat'), {'chat':session_id})}
        material = [row.owner, row.workspace_id, int(row.transcript_revision or 0), keep_count, sorted(roots), [(item.id,item.role,
            hashlib.sha256(str(item.content).encode()).hexdigest(),versions.get(item.id)) for item in rows]]
        return hashlib.sha256(json.dumps(material,separators=(',',':'),ensure_ascii=False).encode()).hexdigest(), ids, row.owner, tuple(sorted(roots))

    async def truncate_messages_guarded(self, session_id: str, keep_count: int) -> bool:
        from src.openclank.chat_source_bridge import guarded_source_mutation, canonical_source_provider
        state = getattr(self, 'lifecycle_context', None)
        provider = canonical_source_provider(state) if state is not None else None
        if provider is None:
            return self.truncate_messages(session_id, keep_count)
        try:
            session = self.get_session(session_id)
            with session._history_lock:
                before_history = _history_fingerprint(session)
            def snapshot():
                with SessionLocal() as db:
                    return self._truncate_snapshot(db,session_id,keep_count)
            expected, ids, owner, roots = await _await_guarded_writer(snapshot)
            def check():
                with SessionLocal() as db:
                    current, now_ids, now_owner, now_roots = self._truncate_snapshot(db,session_id,keep_count)
                    if (current,now_ids,now_owner,now_roots) != (expected,ids,owner,roots):
                        raise ValueError('truncate selection changed')
            async with guarded_source_mutation(state=state,owner=owner,chat_id=session_id,
                    message_ids=tuple(sorted(set(ids)|set(roots))),pre_fence=check,_bulk=True,_pre_fence_offloop=True) as guard:
                if guard is None or guard.provider is not provider or canonical_source_provider(state) is not provider:
                    raise ValueError('source authority changed')
                try:
                    result = await _await_guarded_writer(self.truncate_messages,session_id,keep_count,_source_capability=(guard,expected),_defer_cache=True,_session=session)
                except asyncio.CancelledError:
                    if self.sessions.get(session_id) is session:
                        self.sessions.pop(session_id, None)
                    raise
                if result:
                    with session._history_lock:
                        if _history_fingerprint(session) == before_history:
                            session.history = session.history[:keep_count]
                            session._history = session.history
                        elif self.sessions.get(session_id) is session:
                            self.sessions.pop(session_id, None)
                return result
        except Exception as exc:
            raise ArchiveUnavailableError(message='truncate source guard unavailable') from exc

    @_cached_history_mutation
    def truncate_messages(self, session_id: str, keep_count: int, *, _source_capability=None, _defer_cache=False, _session=None) -> bool:
        """Truncate session history, keeping only the first `keep_count` messages."""
        session = _session if _session is not None else self.get_session(session_id)
        if getattr(session, "id", None) != session_id:
            raise ValueError("session identity changed")
        if keep_count < 0:
            return False

        db = SessionLocal()
        try:
            from src.openclank.chat_source_bridge import canonical_source_provider, _HostChatFenceCapability, withdraw_deleted_messages
            from src.openclank.conversation_archive import get_conversation_archive
            state = getattr(self, 'lifecycle_context', None)
            provider = canonical_source_provider(state) if state is not None else None
            if provider is not None:
                if type(_source_capability) is not tuple or len(_source_capability) != 2 or type(_source_capability[0]) is not _HostChatFenceCapability:
                    raise ArchiveUnavailableError(message='truncate source guard unavailable')
                if db.get_bind().dialect.name == 'sqlite':
                    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                db.query(DbSession).filter(DbSession.id == session_id).with_for_update().first()
                self._lock_source_intents(db, session_id)
                expected, ids, owner, _roots = self._truncate_snapshot(db,session_id,keep_count)
                _source_capability[0].validate(provider=provider,owner=owner,chat_id=session_id)
                if not _source_capability[0].whole_chat and not (set(ids) | set(_roots)).issubset(_source_capability[0].fenced_ids):
                    raise ArchiveUnavailableError(message='truncate source fence incomplete')
                if expected != _source_capability[1]:
                    raise ArchiveUnavailableError(message='truncate selection changed')
                archive = get_conversation_archive()
                account = archive.resolve_owner(owner)
                withdraw_deleted_messages(db,archive,owner=account,chat_id=session_id,message_ids=ids)
            db_messages = db.query(DbChatMessage).filter(
                DbChatMessage.session_id == session_id
            ).order_by(DbChatMessage.timestamp, DbChatMessage.id).all()

            deleted = 0
            for msg in db_messages[keep_count:]:
                db.delete(msg)
                deleted += 1

            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                # keep_count can exceed the real message total (e.g. the AI tool
                # defaults to keep_count=10 on a short session); message_count must
                # track the rows that actually remain, not the requested cap.
                db_session.message_count = min(keep_count, len(db_messages))
                db_session.updated_at = datetime.now(timezone.utc)

            db.commit()

            # Update in-memory
            if not _defer_cache:
                session.history = session.history[:keep_count]
                session._history = session.history

            logger.info(f"Truncated session {session_id} to {keep_count} messages")
            return True

        except Exception as e:
            logger.error(f"Error truncating session: {e}")
            db.rollback()
            if isinstance(e, ArchiveUnavailableError):
                raise
            return False
        finally:
            db.close()

    @staticmethod
    def _projection_fingerprint(db, session_id, messages, expected_sources=()):
        """Hash current row identities and the proposed retained values."""
        row = db.query(DbSession).filter(DbSession.id == session_id).first()
        if row is None:
            raise ValueError('session unavailable')
        current = db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id).order_by(DbChatMessage.timestamp, DbChatMessage.id).all()
        existing = {item.id: item for item in current}
        for mid, role, content_hash in expected_sources:
            original = existing.get(mid)
            if original is None or original.role != role or hashlib.sha256(str(original.content).encode()).hexdigest() != content_hash:
                raise ValueError('projection source changed')
        proposed = []
        for message in messages:
            meta = message.metadata or {}
            mid = str(getattr(message, 'persistence_id', None) or meta.get('_db_id') or '')
            content = persistable_message_content(message.content, message.metadata)
            if mid in existing and existing[mid].role in ('user','assistant'):
                # Compaction retains source components verbatim. Changed source
                # bodies require the separate source-edit admission path.
                if existing[mid].role != message.role or existing[mid].content != content:
                    raise ValueError('projection changes retained source')
            proposed.append((mid, message.role, hashlib.sha256(str(content).encode()).hexdigest()))
        from src.openclank.chat_source_bridge import available
        from sqlalchemy import text
        versions = {}
        if available(db):
            versions = {item[0]:(item[1],item[2]) for item in db.execute(text('SELECT id,source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat'), {'chat':session_id})}
        material = [row.owner, row.workspace_id, int(row.transcript_revision or 0), list(expected_sources), [(item.id, item.role,
            hashlib.sha256(str(item.content).encode()).hexdigest(), versions.get(item.id)) for item in current], proposed]
        return hashlib.sha256(json.dumps(material, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()

    async def replace_messages_guarded(self, session_id: str, messages: list, *, _expected_sources=(), _required_provider=None, _expected_history=None) -> bool:
        """Serialize presentation replacement with host-source publication."""
        from src.openclank.chat_source_bridge import guarded_source_mutation, canonical_source_provider
        state = getattr(self, 'lifecycle_context', None)
        session = self.get_session(session_id)
        provider = canonical_source_provider(state) if state is not None else None
        if _required_provider is not None and provider is not _required_provider:
            raise ArchiveUnavailableError(message='projection source authority changed')
        if provider is None and not _expected_sources:
            return self.replace_messages(session_id, messages, _expected_history=_expected_history)
        owner = session.owner
        try:
            with session._history_lock:
                planned_messages = deepcopy(messages)
                before_history = _history_fingerprint(session)
                if _expected_history is not None and before_history != _expected_history:
                    return False
            expected_sources = tuple(_expected_sources)
            def snapshot():
                with SessionLocal() as db:
                    return self._projection_fingerprint(db, session_id, planned_messages, expected_sources)
            expected = await _await_guarded_writer(snapshot)
            def check():
                with SessionLocal() as db:
                    if self._projection_fingerprint(db, session_id, planned_messages, expected_sources) != expected:
                        raise ValueError('projection changed')
            from contextlib import asynccontextmanager
            @asynccontextmanager
            async def no_source_guard():
                yield None
            context = (guarded_source_mutation(state=state,owner=owner,chat_id=session_id,pre_fence=check,_pre_fence_offloop=True)
                       if state is not None else no_source_guard())
            async with context as guard:
                current_provider = canonical_source_provider(state) if state is not None else None
                if current_provider is not provider or (provider is not None and (guard is None or guard.provider is not provider)) or (provider is None and guard is not None):
                    raise ValueError('source authority changed')
                try:
                    result = await _await_guarded_writer(self.replace_messages,session_id,planned_messages,_source_capability=(guard,expected,expected_sources),_defer_cache=True,_session=session,_expected_history=before_history)
                except asyncio.CancelledError:
                    if self.sessions.get(session_id) is session:
                        self.sessions.pop(session_id, None)
                    raise
                if not result:
                    raise ValueError('projection write failed')
                with session._history_lock:
                    if _history_fingerprint(session) == before_history:
                        session.history = planned_messages
                        session._history = session.history
                        session.message_count = len(planned_messages)
                    elif self.sessions.get(session_id) is session:
                        self.sessions.pop(session_id, None)
                return result
        except Exception as exc:
            raise ArchiveUnavailableError(message='projection source guard unavailable') from exc

    @_cached_history_mutation
    def replace_messages(self, session_id: str, messages: list, *, _source_capability=None, _defer_cache=False, _session=None, _expected_history=None) -> bool:
        """Replace the presentation projection without deleting original history.

        Canonical source parts are archived first (idempotent). Retained
        messages keep their original durable identity (``_db_id`` /
        ``persistence_id``) — they are never rekeyed. Dropped projection rows
        remain retrievable through the conversation archive. This is the
        host finite-context projection writer; it is not a source-history
        destroy/rekey operation.
        """
        session = _session if _session is not None else self.get_session(session_id)
        if getattr(session, "id", None) != session_id:
            raise ValueError("session identity changed")
        if _expected_history is not None and _history_fingerprint(session) != _expected_history:
            return False
        db = SessionLocal()
        try:
            if _source_capability is not None and db.get_bind().dialect.name == 'sqlite':
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            query = db.query(DbSession).filter(DbSession.id == session_id)
            if _source_capability is not None:
                query = query.with_for_update()
            db_session = query.first()
            if _source_capability is not None:
                self._lock_source_intents(db, session_id)
            if db_session is None:
                logger.warning("Cannot replace history for missing session %s", session_id)
                return False

            from src.openclank.conversation_archive import get_conversation_archive
            from src.openclank.chat_source_bridge import canonical_source_provider, _HostChatFenceCapability
            lifecycle_context = getattr(self, 'lifecycle_context', None)
            provider = canonical_source_provider(lifecycle_context) if lifecycle_context is not None else None
            if provider is not None or _source_capability is not None:
                if type(_source_capability) is not tuple or len(_source_capability) != 3:
                    raise ArchiveUnavailableError(message='projection source guard unavailable')
                guard, expected, expected_sources = _source_capability
                if provider is not None:
                    if type(guard) is not _HostChatFenceCapability:
                        raise ArchiveUnavailableError(message='projection source guard unavailable')
                    guard.validate(provider=provider,owner=db_session.owner,chat_id=session_id)
                elif guard is not None:
                    raise ArchiveUnavailableError(message='projection source authority changed')
                if self._projection_fingerprint(db, session_id, messages, expected_sources) != expected:
                    raise ArchiveUnavailableError(message='projection changed')
            if get_conversation_archive().chat_erasure_started(owner=db_session.owner, chat_id=session_id):
                raise RuntimeError("Conversation deletion is pending; history writes are closed")

            # Reserve every incoming attachment before removing any durable
            # message row. reserve_upload() shares the upload lifecycle lock
            # with cleanup, so an upload cannot be deleted between this
            # ownership check/access touch and the replacement transaction.
            # A failed reservation must leave the existing transcript intact.
            for message in messages:
                missing_upload_id = reserve_message_upload_references(
                    getattr(self, "upload_handler", None),
                    getattr(db_session, "owner", None),
                    message.content,
                    message.metadata,
                    session_id=session_id,
                )
                if missing_upload_id:
                    raise ValueError(
                        f"Referenced upload is no longer available: {missing_upload_id}"
                    )

            # 1) Archive every currently persisted message BEFORE any
            #    projection mutation. Duplicates are idempotent no-ops.
            #    Archive commit is a hard gate: if append/tombstone fails the
            #    mutation aborts and source rows stay intact. Never swallow
            #    append errors then delete.
            existing_rows = (
                db.query(DbChatMessage)
                .filter(DbChatMessage.session_id == session_id)
                .order_by(DbChatMessage.timestamp, DbChatMessage.id)
                .all()
            )
            self._archive_existing_rows(session_id, existing_rows, owner=getattr(db_session, "owner", None))

            # 2) Classify the incoming projection: retained (stable identity)
            #    vs new summary/projection rows.
            retained_ids: list[str] = []
            for message in messages:
                if message.metadata is None:
                    message.metadata = {}
                existing_id = str(
                    getattr(message, "persistence_id", None)
                    or message.metadata.get("_db_id")
                    or ""
                ).strip()
                if existing_id and any(row.id == existing_id for row in existing_rows):
                    retained_ids.append(existing_id)

            # 3) Projection rewrite. Retained rows keep their original primary
            #    keys. Only rows not present in the incoming projection are
            #    removed from the presentation table; their source parts remain
            #    in the archive and an outbox tombstone records the projection
            #    drop without pruning source.
            now = datetime.now(timezone.utc)
            incoming_existing_ids = set(retained_ids)
            dropped_rows = [
                row for row in existing_rows if row.id not in incoming_existing_ids
            ]
            if provider is not None:
                from src.openclank.chat_source_bridge import mark_projection_drops
                archive = get_conversation_archive()
                account = archive.resolve_owner(db_session.owner)
                if not account:
                    raise ArchiveUnavailableError(message='projection owner unavailable')
                mark_projection_drops(db, archive, owner=account, chat_id=session_id, rows=dropped_rows)
            for row in dropped_rows:
                db.delete(row)
            # Flush the projection drops before any insert so the SQL order is
            # DELETE then INSERT (and a crash mid-rewrite cannot leave both the
            # dropped row and its replacement visible).
            db.flush()

            for i, message in enumerate(messages):
                reserved_id = str(
                    getattr(message, "persistence_id", None)
                    or (message.metadata or {}).get("_db_id")
                    or ""
                ).strip()
                # Preserve original identity for retained messages. New
                # projection rows (summaries) receive a fresh id once.
                msg_id = reserved_id if reserved_id in incoming_existing_ids or reserved_id else str(uuid.uuid4())
                if reserved_id and reserved_id not in incoming_existing_ids and any(
                    row.id == reserved_id for row in existing_rows
                ):
                    # Claiming an existing id for a rewritten body: keep the id
                    # so the original history identity is not rekeyed.
                    msg_id = reserved_id
                # Mirrors _persist_message: keep raw media bytes out of the
                # persisted transcript and search index.
                _content = persistable_message_content(message.content, message.metadata)
                _meta = json.dumps(message.metadata) if message.metadata else None
                _ts = now + timedelta(microseconds=i)
                if any(row.id == msg_id for row in existing_rows):
                    # Retained row: update in place. Never delete-then-insert
                    # (that is a rekey risk) and never INSERT (UNIQUE id).
                    db.query(DbChatMessage).filter(DbChatMessage.id == msg_id).update(
                        {
                            DbChatMessage.role: message.role,
                            DbChatMessage.content: _content,
                            DbChatMessage.meta_data: _meta,
                            DbChatMessage.timestamp: _ts,
                        },
                        synchronize_session=False,
                    )
                else:
                    db_message = DbChatMessage(
                        id=msg_id,
                        session_id=session_id,
                        role=message.role,
                        content=_content,
                        meta_data=_meta,
                        timestamp=_ts,
                    )
                    db.add(db_message)
                if message.metadata is None:
                    message.metadata = {}
                message.metadata["_db_id"] = msg_id
                message.persistence_id = msg_id

            db_session.message_count = len(messages)
            db_session.updated_at = now
            db_session.last_accessed = now
            db_session.last_message_at = now

            # 4) Tombstone every projection drop as a commit gate. If any
            #    tombstone fails, roll back the deletes/rekeys and leave source
            #    intact (recoverable archive-unavailable).
            for row in dropped_rows:
                self._tombstone_projection_drop(
                    session_id,
                    row,
                    owner=getattr(db_session, "owner", None),
                )

            db.commit()

            # 5) Record the active compaction projection against the archive.
            #    Source parts are already archived and remain retrievable.
            self._record_projection(session_id, messages, owner=getattr(db_session, "owner", None))

            if not _defer_cache:
                session.history = list(messages)
                session._history = session.history
                session.message_count = len(messages)
            logger.info(
                "Replaced session %s projection with %d messages (%d retained ids, source archived)",
                session_id,
                len(messages),
                len(retained_ids),
            )
            return True
        except Exception as e:
            # Archive/tombstone gate failure must not leave a half-applied
            # projection. Recoverable: source stays intact and the caller can
            # retry once the archive is back. Other failures keep the prior
            # False contract (reservation / missing session).
            logger.error("Error replacing session history: %s", e)
            db.rollback()
            if isinstance(e, ArchiveUnavailableError):
                raise
            return False
        finally:
            db.close()

    def _row_source_parts(
        self,
        session_id: str,
        row,
        *,
        owner=None,
        event_sequence: int = 0,
    ) -> list:
        """Decompose a persisted chat_messages row into ordered source parts.

        Parity with the live ``add_message`` capture path: body + tool_calls +
        tool_results + owned asset refs (not body+assets only). Tool parts that
        exist only in row ``meta_data`` are decomposed here so a later
        ``replace_messages`` cannot drop them outside the archive.
        """
        from src.openclank.conversation_archive import SourcePart

        metadata = {}
        if row.meta_data:
            try:
                metadata = json.loads(row.meta_data) or {}
            except (TypeError, ValueError):
                metadata = {}
        event_workspace = getattr(row, "workspace_id", None) or metadata.get("event_workspace")
        actor_id = str(metadata.get("actor_id") or "main")
        revision = int(metadata.get("source_revision") or 1)
        role = str(row.role or "user")
        now = _now_ms()
        parts = []

        def _part(part_id: str, part_type: str, content, *, part_seq: int, part_revision: int, assets=()):
            return SourcePart(
                owner=str(owner or ""),
                chat_id=str(session_id),
                actor_id=actor_id,
                message_id=str(row.id),
                part_id=str(part_id),
                revision=int(part_revision),
                runtime_generation=str(metadata.get("runtime_generation") or "0"),
                event_sequence=event_sequence * 100 + part_seq,
                event_workspace=event_workspace,
                event_project=metadata.get("event_project"),
                role=role,
                part_type=part_type,
                content=content,
                aliases=(("host_message", str(row.id)),),
                assets=tuple(assets),
                time_created=now,
                time_updated=now,
            )

        seq = 0
        parts.append(_part("body", "text", row.content, part_seq=seq, part_revision=revision))
        seq += 1
        for index, tool_call in enumerate(metadata.get("tool_calls") or []):
            if isinstance(tool_call, dict):
                parts.append(
                    _part(f"tool_call:{index}", "tool_call", tool_call, part_seq=seq, part_revision=revision)
                )
                seq += 1
        for index, tool_result in enumerate(metadata.get("tool_results") or []):
            if isinstance(tool_result, dict):
                parts.append(
                    _part(f"tool_result:{index}", "tool_result", tool_result, part_seq=seq, part_revision=revision)
                )
                seq += 1
        for ref_index, ref in enumerate(attachment_refs_from_metadata(metadata)):
            asset_spec = {
                "asset_id": ref.get("attachment_id") or ref.get("id") or "",
                "content_hash": ref.get("checksum_sha256"),
                "mime_type": ref.get("mime"),
                "byte_size": ref.get("size"),
                "provenance": {"source": "host_upload", "name": ref.get("name")},
            }
            parts.append(
                _part(
                    f"asset:{ref_index}",
                    "asset_refs",
                    ref,
                    part_seq=seq,
                    part_revision=1,
                    assets=(asset_spec,),
                )
            )
            seq += 1
        return parts

    def _archive_existing_rows(self, session_id: str, rows, *, owner=None) -> None:
        """Archive persisted chat_messages rows as full ordered source parts.

        Raises ``ArchiveUnavailableError`` on any append failure so
        ``replace_messages`` can refuse to prune projection rows.
        """
        try:
            from src.openclank.conversation_archive import get_conversation_archive
        except Exception as exc:
            logger.debug("conversation archive unavailable", exc_info=True)
            raise ArchiveUnavailableError(message=f"archive import failed: {exc}") from exc
        parts = []
        for index, row in enumerate(rows):
            parts.extend(
                self._row_source_parts(session_id, row, owner=owner, event_sequence=index)
            )
        if not parts:
            return
        try:
            get_conversation_archive().append_parts(parts)
        except Exception as exc:
            logger.warning("conversation archive backfill failed", exc_info=True)
            raise ArchiveUnavailableError(message=f"archive append failed: {exc}") from exc

    def _tombstone_projection_drop(self, session_id: str, row, *, owner=None) -> None:
        """Version a projection drop through the archive; never prune source.

        Uses the row's real ``actor_id`` (not hardcoded ``"main"``). Raises
        ``ArchiveUnavailableError`` on failure so the projection rewrite can
        roll back instead of committing an untombstoned drop.
        """
        try:
            from src.openclank.conversation_archive import get_conversation_archive
        except Exception as exc:
            raise ArchiveUnavailableError(message=f"archive import failed: {exc}") from exc
        try:
            metadata = {}
            if getattr(row, "meta_data", None):
                try:
                    metadata = json.loads(row.meta_data) or {}
                except (TypeError, ValueError):
                    metadata = {}
            actor_id = str(metadata.get("actor_id") or "main")
            archive = get_conversation_archive()
            # Tombstone every part identity this row would have archived so a
            # drop cannot leave tool parts without a version record.
            for part in self._row_source_parts(session_id, row, owner=owner):
                result = archive.tombstone_part(
                    owner=str(owner or ""),
                    chat_id=str(session_id),
                    actor_id=actor_id,
                    message_id=str(row.id),
                    part_id=part.part_id,
                    revision=part.revision,
                    reason="projection_drop",
                )
                if result.get("ok"):
                    continue
                error = str(result.get("error") or "")
                if error == "not_found":
                    # Fail-closed: not_found authorizes the drop only when
                    # archive state proves the part was never present or is
                    # already tombstoned (a prior version record exists).
                    probe = archive.get_part(
                        owner=str(owner or ""),
                        chat_id=str(session_id),
                        actor_id=actor_id,
                        message_id=str(row.id),
                        part_id=part.part_id,
                    )
                    if probe.get("ok") and probe.get("part", {}).get("tombstone"):
                        continue
                    if not probe.get("ok"):
                        listing = archive.get_message_parts(
                            owner=str(owner or ""),
                            chat_id=str(session_id),
                            actor_id=actor_id,
                            message_id=str(row.id),
                        )
                        if listing.get("ok"):
                            known = {
                                str(p.get("part_id"))
                                for p in listing.get("parts") or []
                            }
                            if part.part_id not in known:
                                # Never present in the archive: nothing to version.
                                continue
                        # Message-level miss is not proof of never-present
                        # after a successful append — fail closed.
                    raise ArchiveUnavailableError(
                        message=(
                            "projection drop tombstone not_found without archive "
                            f"proof for part {part.part_id}"
                        )
                    )
                raise ArchiveUnavailableError(
                    message=f"projection drop tombstone failed: {result.get('error')}"
                )
        except ArchiveUnavailableError:
            raise
        except Exception as exc:
            logger.debug("projection drop tombstone failed", exc_info=True)
            raise ArchiveUnavailableError(message=f"projection drop tombstone failed: {exc}") from exc

    def _record_projection(self, session_id: str, messages: list, *, owner=None) -> None:
        """Record an active-context compaction projection for the archive."""
        try:
            from src.openclank.conversation_archive import get_conversation_archive
            summary_ids = []
            source_summaries = []
            retained = []
            for message in messages:
                metadata = getattr(message, "metadata", None) or {}
                msg_id = str(metadata.get("_db_id") or getattr(message, "persistence_id", None) or "")
                if metadata.get("compacted"):
                    summary_ids.append(msg_id)
                    if metadata.get("hidden") or getattr(message, "role", None) == "system":
                        source_summaries.append((msg_id, str(getattr(message, "content", "") or "")))
                else:
                    retained.append(msg_id)
            if not summary_ids:
                return
            get_conversation_archive().record_compaction_projection(
                owner=str(owner or ""),
                chat_id=str(session_id),
                actor_id="main",
                summary_id=(source_summaries[-1][0] if source_summaries else summary_ids[-1]) or f"summary-{session_id}",
                projection_revision=len(summary_ids),
                summary_text=(source_summaries[-1][1] if source_summaries else str(getattr(messages[0], "content", "") or ""))[:4000],
                included_message_ids=[],
                retained_message_ids=retained,
                trigger_kind="host_finite_projection",
            )
        except Exception:
            logger.debug("compaction projection record failed", exc_info=True)

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def get_session(self, session_id: str, *, allow_pending_history: bool = False) -> Session:
        """Get a session by ID, loading from DB if needed.

        Sessions seeded by `load_sessions` start with empty history. The
        first read here hydrates them with the message rows.
        """
        if session_id not in self.sessions:
            self._load_session_from_db(session_id)
        else:
            cached = self.sessions[session_id]
            # Lazy hydrate: metadata-only entries get their messages on first read.
            if not cached.history and getattr(cached, "message_count", 0) > 0:
                self._load_session_from_db(session_id)

        current = self.sessions.get(session_id)
        if current is not None and not getattr(current, "incognito", False):
            from src.openclank.conversation_archive import get_conversation_archive
            if get_conversation_archive().chat_erasure_started(owner=current.owner, chat_id=session_id):
                raise RuntimeError("Conversation deletion is pending; retry erasure to finish")

        # Keep model/endpoint metadata fresh. Endpoint deletion can clear the
        # DB row while a session object is still cached in RAM.
        self.sync_session_metadata(session_id, allow_pending_history=allow_pending_history)

        # Update last_accessed
        self._touch_session(session_id)

        return self.sessions[session_id]

    @_cached_history_mutation
    def sync_session_metadata(self, session_id: str, *, allow_pending_history: bool = False) -> bool:
        """Refresh non-message session fields from the DB into the cached object."""
        from src.openclank.session_operations import HistoryOperationError
        session = self.sessions.get(session_id)
        if session is None:
            return False
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                return False
            headers = db_session.headers
            if isinstance(headers, str):
                try:
                    headers = json.loads(headers)
                except json.JSONDecodeError:
                    headers = {}
            session.name = db_session.name
            session.endpoint_url = db_session.endpoint_url or ""
            session.endpoint_id = getattr(db_session, "endpoint_id", None)
            session.provider_model_route_id = getattr(
                db_session, "provider_model_route_id", None
            )
            session.model = db_session.model or ""
            session.headers = headers or {}
            session.rag = db_session.rag
            session.archived = db_session.archived
            session.owner = getattr(db_session, "owner", None)
            session.workspace_id = getattr(db_session, "workspace_id", None)
            session.response_style = db_session.response_style
            session.response_style_revision = db_session.response_style_revision
            session.is_important = getattr(db_session, "is_important", False) or False
            session.message_count = getattr(db_session, "message_count", session.message_count) or 0
            from src.openclank.session_operations import visibility, HistoryOperationError
            state = db_session.mimo_state or {}
            excluded = visibility(state, require_settled=not allow_pending_history)
            operation = state.get("history_operation") or {}
            token = (operation.get("id"), operation.get("status")) if operation else None
            if token != getattr(session, "_native_history_view_revision", None):
                rows = db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id).order_by(DbChatMessage.timestamp, DbChatMessage.id).all()
                visible_history = []
                for item in rows:
                    if item.id in excluded: continue
                    meta = json.loads(item.meta_data) if item.meta_data else {}
                    meta = dict(meta or {})
                    meta["_db_id"] = item.id
                    meta.setdefault("timestamp", _message_timestamp_iso(item.timestamp))
                    visible_history.append(ChatMessage(item.role, _parse_msg_content(item.content), meta))
                session.history = visible_history
                session._native_history_view_revision = token
            return True
        except HistoryOperationError:
            raise
        except Exception as e:
            logger.error(f"Error syncing session metadata {session_id}: {e}")
            return False
        finally:
            db.close()

    @_cached_history_mutation
    def _load_session_from_db(self, session_id: str):
        """Hydrate a single session (with messages) from the database."""
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                raise KeyError(f"Session {session_id} not found")

            session = self._db_to_session(db_session, db)
            if session:
                self.sessions[session_id] = session
            else:
                # No messages — fall back to metadata-only entry so callers
                # don't crash on KeyError for empty sessions.
                meta = self._db_to_session_meta(db_session)
                if meta is None:
                    raise KeyError(f"Session {session_id} could not be loaded")
                self.sessions[session_id] = meta

        except KeyError:
            raise
        except Exception as e:
            logger.error(f"Error loading session {session_id}: {e}")
            raise
        finally:
            db.close()

    def _touch_session(self, session_id: str):
        """Update last_accessed timestamp."""
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                db_session.last_accessed = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            logger.error(f"Error updating last_accessed: {e}")
            db.rollback()
        finally:
            db.close()

    def create_session(
        self,
        session_id: str,
        name: str,
        endpoint_url: str,
        model: str,
        rag: bool = False,
        owner: str = None,
        endpoint_id: str | None = None,
        provider_model_route_id: str | None = None,
        workspace_id: str | None = None,
        response_style: str = 'normal',
        connector_create_digest: str | None = None,
        connector_current=None,
    ) -> Session:
        """Create a new session and save to database."""
        from src.openclank.response_style import normalize_mode
        response_style = normalize_mode(response_style)
        db = SessionLocal()
        try:
            db_session = DbSession(
                id=session_id,
                name=name,
                endpoint_url=endpoint_url,
                endpoint_id=endpoint_id,
                provider_model_route_id=provider_model_route_id,
                model=model,
                rag=rag,
                headers={},
                owner=owner,
                workspace_id=str(workspace_id).strip() or None if workspace_id else None,
                response_style=response_style, response_style_revision=0,
                connector_create_digest=connector_create_digest,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            db.add(db_session)
            if connector_current is not None and not connector_current():
                raise ValueError("Connector credential changed before session creation")
            db.commit()

            session = Session(
                id=session_id,
                name=name,
                endpoint_url=endpoint_url,
                model=model,
                endpoint_id=endpoint_id,
                provider_model_route_id=provider_model_route_id,
                rag=rag,
                headers={},
                owner=owner,
                workspace_id=str(workspace_id).strip() or None if workspace_id else None,
                response_style=response_style, response_style_revision=0,
            )

            self.sessions[session_id] = session
            return session

        except Exception as e:
            db.rollback()
            logger.error(f"Error creating session: {e}")
            raise
        finally:
            db.close()

    def update_session_workspace(self, session_id: str, workspace_id: str | None) -> bool:
        """Persist a stable Workspace ID before publishing it to the cache."""
        value = str(workspace_id or "").strip() or None
        cached = self.sessions.get(session_id)
        db = SessionLocal()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id).first()
            if row is None:
                if cached is not None and getattr(cached, "incognito", False):
                    cached.workspace_id = value
                    return True
                return False
            row.workspace_id = value
            row.updated_at = utcnow_naive()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        if cached is not None:
            cached.workspace_id = value
        return True

    @_cached_history_mutation
    def delete_session(self, session_id: str, *, retired_execution: bool = False, _source_capability=None) -> bool:
        """Permanently delete a session and all its messages."""
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            memory_session = self.sessions.get(session_id)
            session_owner = (
                getattr(db_session, "owner", None)
                if db_session is not None
                else getattr(memory_session, "owner", None)
            )
            if db_session is not None or memory_session is not None:
                from src.openclank.chat_source_bridge import require_chat_source_capability, canonical_source_provider
                lifecycle_context = getattr(self, 'lifecycle_context', None)
                source_provider = canonical_source_provider(lifecycle_context) if lifecycle_context is not None else None
                if db_session is not None or not getattr(memory_session, 'incognito', False):
                    require_chat_source_capability(provider=source_provider, owner=session_owner,
                        chat_id=session_id, capability=_source_capability)
                from src import agent_runs
                if agent_runs.is_active(session_id):
                    raise RuntimeError("Conversation has an active run")
                if not session_owner:
                    raise RuntimeError("Conversation owner is unavailable")
                from src import bg_jobs
                bg_jobs.assert_session_owner_purge_ready(
                    session_id=session_id, owner=str(session_owner)
                )
                if not getattr(memory_session, "incognito", False):
                    from src.openclank.conversation_archive import get_conversation_archive
                    get_conversation_archive().begin_chat_erasure(owner=session_owner, chat_id=session_id)
                bg_jobs.delete_for_session_owner(
                    session_id=session_id,
                    owner=str(session_owner or ""),
                )

            pending_image_ids: set[str] = set()
            try:
                from src.session_image_cleanup import (
                    mark_session_image_source_removed,
                    session_image_refs,
                )

                pending_image_ids = session_image_refs(
                    db,
                    session_id,
                    session_owner,
                )
                if self.upload_handler and session_owner:
                    self.upload_handler.conversation_attachment_intent(owner=session_owner, session_id=session_id, image_ids=pending_image_ids, persist=True)
            except Exception:
                raise

            if db_session is not None or memory_session is not None:
                # Independent native/Skills/Copal reference coverage is not
                # complete. Preserve Files bytes and mark removed provenance
                # in this same core transaction, rather than guess exclusivity.
                mark_session_image_source_removed(db, owner=session_owner, session_id=session_id, image_ids=pending_image_ids)
                if not getattr(memory_session, "incognito", False):
                    from src.openclank.session_deletion import erase_retained_content
                    erase_retained_content(db, owner=session_owner, session_id=session_id, upload_handler=self.upload_handler,
                        source_provider=source_provider, _source_capability=_source_capability)
                    # SessionLocal disables autoflush. Persist content-free
                    # Stats/Memory detachment before message/session cascades.
                    db.flush()

            # Detach documents so they survive as orphans in the library
            db.query(DbDocument).filter(DbDocument.session_id == session_id).update(
                {DbDocument.session_id: None}, synchronize_session=False
            )

            # Delete messages
            db.query(DbChatMessage).filter(DbChatMessage.session_id == session_id).delete()

            # Delete session
            if db_session:
                db.delete(db_session)

            # Drop the in-memory copy even when there is no DB row. A "ghost"
            # session lives only here (never persisted, or its row was removed
            # out-of-band); without this it can never be cleared and keeps
            # 404ing on every operation (issue #1044).
            removed_in_memory = session_id in self.sessions

            if db_session or removed_in_memory:
                # Commit the document-detach / message-delete above (a no-op when
                # the ghost had no rows) together with the session delete.
                db.commit()
                self.sessions.pop(session_id, None)
                from src.session_image_cleanup import retire_reconciled_session_images
                try:
                    retire_reconciled_session_images(owner=session_owner, session_id=session_id, upload_handler=self.upload_handler)
                except Exception:
                    # The host deletion already committed; durable image intent remains for reconciliation.
                    logger.warning("Could not reconcile committed chat image claims for %s", session_id)
                if retired_execution and self.upload_handler and session_owner:
                    try:
                        self.upload_handler.retire_chat_input_receipt_claims(owner=session_owner, session_id=session_id)
                    except Exception:
                        # The host deletion already committed. Leave receipt claims intact for reconciliation.
                        logger.warning("Could not retire committed chat input receipt claims for %s", session_id)
                logger.info(f"Deleted session {session_id}")
                return True
            return False

        except Exception as e:
            logger.error(f"Error deleting session: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Session updates
    # ------------------------------------------------------------------

    def update_session_name(self, session_id: str, name: str):
        """Update session name."""
        if session_id not in self.sessions:
            return

        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                db_session.name = name
                db_session.updated_at = datetime.now(timezone.utc)
                db.commit()
                self.sessions[session_id].name = name
        except Exception as e:
            db.rollback()
            logger.error(f"Error updating session name: {e}")
            raise
        finally:
            db.close()

    def archive_session(self, session_id: str):
        """Archive a session."""
        if session_id not in self.sessions:
            return

        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                db_session.archived = True
                db_session.updated_at = datetime.now(timezone.utc)
                db.commit()
                self.sessions[session_id].archived = True
        except Exception as e:
            db.rollback()
            logger.error(f"Error archiving session: {e}")
            raise
        finally:
            db.close()

    def mark_important(self, session_id: str, important: bool = True):
        """Mark session as important."""
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session:
                db_session.is_important = important
                db_session.updated_at = datetime.now(timezone.utc)
                db.commit()

                if session_id in self.sessions:
                    self.sessions[session_id].is_important = important
            else:
                raise KeyError(f"Session {session_id} not found")
        except Exception as e:
            db.rollback()
            logger.error(f"Error marking session important: {e}")
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_sessions_for_user(self, username: Optional[str] = None) -> Dict[str, Session]:
        """Return sessions for a specific user (or all if username is None)."""
        if username is None:
            return self.sessions
        normalized = str(username or "").strip().lower()
        return {
            sid: s for sid, s in self.sessions.items()
            if str(getattr(s, "owner", None) or "").strip().lower() == normalized
        }

    @staticmethod
    def _cache_owner(owner: object) -> str:
        normalized = str(owner or "").strip().lower()
        if not normalized or "\x00" in normalized:
            raise ValueError("session-cache lifecycle owner is required")
        return normalized

    def owner_cache_inventory(self, owner: str) -> dict:
        """Return content-free, stable evidence for one owner's cached sessions."""
        normalized = self._cache_owner(owner)
        session_ids = sorted(
            str(session_id)
            for session_id, session in self.sessions.items()
            if str(getattr(session, "owner", None) or "").strip().lower()
            == normalized
        )
        material = json.dumps(session_ids, separators=(",", ":"))
        return {
            "schema_version": 1,
            "count": len(session_ids),
            "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _same_cache_inventory(observed: dict, expected: dict | None) -> bool:
        if expected is None:
            return True
        return (
            int(observed.get("count") or 0) == int(expected.get("count") or 0)
            and str(observed.get("fingerprint") or "")
            == str(expected.get("fingerprint") or "")
        )

    def rename_owner_cache(
        self,
        source_owner: str,
        target_owner: str,
        *,
        expected_source: dict | None = None,
        expected_target: dict | None = None,
    ) -> dict:
        """Move exact-owner cached projections with idempotent replay checks.

        SQL remains authoritative.  This operation only keeps already-loaded
        projections coherent while the durable account saga moves their rows.
        """
        source = self._cache_owner(source_owner)
        target = self._cache_owner(target_owner)
        if source == target:
            raise ValueError("session-cache lifecycle owners must be distinct")
        source_before = self.owner_cache_inventory(source)
        target_before = self.owner_cache_inventory(target)

        if int(source_before["count"]):
            if int(target_before["count"]):
                raise RuntimeError("source and target session caches both contain state")
            if not self._same_cache_inventory(source_before, expected_source):
                raise RuntimeError("source session-cache inventory changed")
            if not self._same_cache_inventory(target_before, expected_target):
                raise RuntimeError("target session-cache inventory changed")
            for session in self.sessions.values():
                if (
                    str(getattr(session, "owner", None) or "").strip().lower()
                    == source
                ):
                    session.owner = target
            state = "applied"
        elif int(target_before["count"]):
            if expected_source is None or not self._same_cache_inventory(
                target_before,
                expected_source,
            ):
                raise RuntimeError("unexpected target session-cache state")
            state = "already_applied"
        else:
            if expected_source is not None and int(expected_source.get("count") or 0):
                raise RuntimeError("expected session-cache state is missing")
            state = "empty"

        source_after = self.owner_cache_inventory(source)
        target_after = self.owner_cache_inventory(target)
        if int(source_after["count"]):
            raise RuntimeError("source session cache remains after rename")
        if expected_source is not None and not self._same_cache_inventory(
            target_after,
            expected_source,
        ):
            raise RuntimeError("renamed session-cache inventory does not reconcile")
        return {
            "state": state,
            "moved": int(source_before["count"]),
            "source": source_after,
            "target": target_after,
        }

    def compensate_owner_cache(
        self,
        source_owner: str,
        target_owner: str,
        *,
        expected_source: dict | None = None,
    ) -> dict:
        """Idempotently move a staged target cache back to its source owner."""
        receipt = self.rename_owner_cache(
            target_owner,
            source_owner,
            expected_source=expected_source,
        )
        return {**receipt, "state": "restored"}

    def purge_owner_cache(
        self,
        owner: str,
        *,
        expected: dict | None = None,
    ) -> dict:
        """Evict one exact owner's cached projections without touching peers."""
        normalized = self._cache_owner(owner)
        before = self.owner_cache_inventory(normalized)
        if int(before["count"]) and not self._same_cache_inventory(before, expected):
            raise RuntimeError("session-cache purge inventory changed")
        removed = sorted(
            session_id
            for session_id, session in list(self.sessions.items())
            if str(getattr(session, "owner", None) or "").strip().lower()
            == normalized
        )
        for session_id in removed:
            self.sessions.pop(session_id, None)
        after = self.owner_cache_inventory(normalized)
        return {
            "state": "purged" if removed else "already_applied",
            "removed": len(removed),
            "before": before,
            "after": after,
        }

    def invalidate_owner_cache(self, *owners: str) -> dict:
        """Evict exact-owner projections before account rename or deletion.

        Durable session rows move in the account SQL transaction.  Mutating
        cached objects would create a second ownership authority that cannot be
        recovered reliably after a crash, so lifecycle convergence evicts the
        derived objects and lets later access rehydrate from SQL.
        """

        selected = sorted({
            self._cache_owner(owner)
            for owner in owners
            if str(owner or "").strip()
        })
        removed = sum(
            int(self.purge_owner_cache(owner)["removed"])
            for owner in selected
        )
        return {
            "state": "invalidated",
            "owners": selected,
            "count": removed,
        }

    def save_sessions(self):
        """No-op for DB compatibility."""

    def ensure_task_session(
        self,
        session_id: str,
        name: str,
        endpoint_url: str,
        model: str,
        owner: str = None,
        task: object = None,
        endpoint_id: str | None = None,
    ) -> Session:
        """Create a task session if it doesn't exist, or return the existing one.

        Unlike create_session, this checks the cache first and does NOT
        overwrite an existing in-memory session. The task scheduler must
        use this instead of direct dict assignment.
        """
        if session_id in self.sessions:
            return self.sessions[session_id]

        session = self.create_session(
            session_id, name, endpoint_url, model,
            owner=owner, endpoint_id=endpoint_id,
        )
        if task is not None:
            task.session_id = session_id
        return session

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_empty_sessions(self, auto_archive_days: int = 30, min_age_hours: int = 1) -> dict:
        """Clean up empty and old sessions.

        Args:
            auto_archive_days: Age in days before non-important sessions are archived.
            min_age_hours: Minimum age in hours before an empty session can be deleted.
                          Prevents deleting sessions that were just created.
        """
        db = SessionLocal()
        stats = {'deleted_empty': 0, 'archived_old': 0, 'total_checked': 0}

        try:
            all_sessions = db.query(DbSession).all()
            cutoff_date = utcnow_naive() - timedelta(days=auto_archive_days)
            min_age = utcnow_naive() - timedelta(hours=min_age_hours)

            for db_session in all_sessions:
                stats['total_checked'] += 1

                # Delete empty sessions only if older than min_age_hours
                if db_session.message_count == 0:
                    # Empty presentation history can still have qualified
                    # archived sources. Real deletion must use chat lifecycle.
                    from src.openclank.chat_source_bridge import canonical_source_provider
                    lifecycle_context = getattr(self, 'lifecycle_context', None)
                    if lifecycle_context is not None and canonical_source_provider(lifecycle_context) is not None:
                        continue
                    if db_session.created_at is not None:
                        created = db_session.created_at
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        if created > min_age:
                            continue  # Too young to delete
                    if db_session.id in self.sessions:
                        del self.sessions[db_session.id]
                    from src import bg_jobs

                    bg_jobs.delete_for_session_owner(
                        session_id=str(db_session.id),
                        owner=str(getattr(db_session, "owner", None) or ""),
                    )
                    db.delete(db_session)
                    stats['deleted_empty'] += 1

                # Archive old sessions
                elif (not db_session.archived and
                      db_session.last_accessed and
                      db_session.last_accessed < cutoff_date and
                      db_session.message_count > 0 and
                      not getattr(db_session, 'is_important', False)):
                    db_session.archived = True
                    stats['archived_old'] += 1

            db.commit()
            logger.info(f"Cleanup: {stats['deleted_empty']} deleted, {stats['archived_old']} archived")

        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            db.rollback()
            raise
        finally:
            db.close()

        return stats
