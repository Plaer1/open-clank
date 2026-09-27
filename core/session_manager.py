# core/session_manager.py
"""
Session management — all session business logic and DB operations.

This is the single place that handles:
- Loading/saving sessions to database
- Adding messages to sessions
- Session lifecycle (create, archive, delete)
"""

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
        if content is not None:
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
        if asset_specs and parts:
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
        session.history.append(message)
        session._history = session.history
        session.message_count = len(session.history)

        try:
            persisted = self._persist_message(session_id, message)
            if not persisted:
                raise RuntimeError(f"Session {session_id} no longer exists")
        except Exception:
            if session.history and session.history[-1] is message:
                session.history.pop()
            else:
                session.history = [item for item in session.history if item is not message]
            session._history = session.history
            session.message_count = len(session.history)
            raise

    def _persist_message(self, session_id: str, message: ChatMessage):
        """Persist a single message to the database."""
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
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
            )
            if missing_upload_id:
                raise ValueError(
                    f"Referenced upload is no longer available: {missing_upload_id}"
                )

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
            # Numeric Stats facts share the canonical message transaction.
            from services.stats.ledger import capture_message_event
            capture_message_event(db, db_session, db_message, message)

            if session_id in self.sessions:
                db_session.message_count = len(self.sessions[session_id].history)
            else:
                db_session.message_count = 0
            _now = datetime.now(timezone.utc)
            db_session.last_accessed = _now
            # Clean "last conversation" timestamp — only bumped here on a
            # real message persist, so it powers an accurate "Last active"
            # sort that ignores renames / model swaps / mere opens.
            db_session.last_message_at = _now

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
            )

            logger.debug(f"Persisted message to session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Error persisting message: {e}")
            db.rollback()
            raise
        finally:
            db.close()

    def truncate_messages(self, session_id: str, keep_count: int) -> bool:
        """Truncate session history, keeping only the first `keep_count` messages."""
        session = self.get_session(session_id)

        if keep_count < 0:
            return False

        db = SessionLocal()
        try:
            db_messages = db.query(DbChatMessage).filter(
                DbChatMessage.session_id == session_id
            ).order_by(DbChatMessage.timestamp).all()

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
            session.history = session.history[:keep_count]
            session._history = session.history

            logger.info(f"Truncated session {session_id} to {keep_count} messages")
            return True

        except Exception as e:
            logger.error(f"Error truncating session: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def replace_messages(self, session_id: str, messages: list) -> bool:
        """Replace the presentation projection without deleting original history.

        Canonical source parts are archived first (idempotent). Retained
        messages keep their original durable identity (``_db_id`` /
        ``persistence_id``) — they are never rekeyed. Dropped projection rows
        remain retrievable through the conversation archive. This is the
        host finite-context projection writer; it is not a source-history
        destroy/rekey operation.
        """
        session = self.get_session(session_id)
        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if db_session is None:
                logger.warning("Cannot replace history for missing session %s", session_id)
                return False

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
            retained = []
            for message in messages:
                metadata = getattr(message, "metadata", None) or {}
                msg_id = str(metadata.get("_db_id") or getattr(message, "persistence_id", None) or "")
                if metadata.get("compacted"):
                    summary_ids.append(msg_id)
                else:
                    retained.append(msg_id)
            if not summary_ids:
                return
            get_conversation_archive().record_compaction_projection(
                owner=str(owner or ""),
                chat_id=str(session_id),
                actor_id="main",
                summary_id=summary_ids[-1] or f"summary-{session_id}",
                projection_revision=len(summary_ids),
                summary_text=str(getattr(messages[0], "content", "") or "")[:4000],
                included_message_ids=[],
                retained_message_ids=retained,
                trigger_kind="host_finite_projection",
            )
        except Exception:
            logger.debug("compaction projection record failed", exc_info=True)

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Session:
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

        # Keep model/endpoint metadata fresh. Endpoint deletion can clear the
        # DB row while a session object is still cached in RAM.
        self.sync_session_metadata(session_id)

        # Update last_accessed
        self._touch_session(session_id)

        return self.sessions[session_id]

    def sync_session_metadata(self, session_id: str) -> bool:
        """Refresh non-message session fields from the DB into the cached object."""
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
            session.is_important = getattr(db_session, "is_important", False) or False
            session.message_count = getattr(db_session, "message_count", session.message_count) or 0
            return True
        except Exception as e:
            logger.error(f"Error syncing session metadata {session_id}: {e}")
            return False
        finally:
            db.close()

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
    ) -> Session:
        """Create a new session and save to database."""
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
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )
            db.add(db_session)
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

    def delete_session(self, session_id: str) -> bool:
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
                from src import bg_jobs

                bg_jobs.delete_for_session_owner(
                    session_id=session_id,
                    owner=str(session_owner or ""),
                )

            pending_image_filenames: set[str] = set()
            try:
                from src.session_image_cleanup import (
                    cleanup_session_image_files,
                    cleanup_session_images,
                    session_image_refs,
                )

                _image_ids, pending_image_filenames = session_image_refs(
                    db,
                    session_id,
                    session_owner,
                )
                cleanup_session_images(session_id, session_owner, db=db)
            except Exception as e:
                logger.warning(f"Image cleanup failed while deleting session {session_id}: {e}")

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
            removed_in_memory = self.sessions.pop(session_id, None) is not None

            if db_session or removed_in_memory:
                # Commit the document-detach / message-delete above (a no-op when
                # the ghost had no rows) together with the session delete.
                db.commit()
                try:
                    cleanup_session_image_files(pending_image_filenames)
                except Exception as exc:
                    logger.warning(
                        "Generated-image GC failed after deleting session %s: %s",
                        session_id,
                        exc,
                    )
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
