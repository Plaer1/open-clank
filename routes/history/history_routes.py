"""History routes — session history, truncation, fork, conversation topics."""

import json
import os
import uuid
import logging
import re
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional

from fastapi import APIRouter, Request, HTTPException, Query

from core.models import ChatMessage
from core.database import SessionLocal, ChatMessage as DbChatMessage, Session as DbSession
from src.auth_helpers import effective_user
from src.topic_analyzer import analyze_topics
from src.upload_handler import reserve_message_upload_references
from src.transcript_layout import strip_invalid_transcript_layout
from src.agent_actor_accounting import actor_accounting_for_assistant
from routes.session_routes import (
    _message_role,
    _message_text,
    _prepare_context_mutation,
    _verify_session_owner,
)

logger = logging.getLogger(__name__)

_HISTORY_IO_WORKERS = 4
_HISTORY_RESOURCE_MATCH_LIMIT = 16
_HISTORY_IO_EXECUTOR = ThreadPoolExecutor(
    max_workers=_HISTORY_IO_WORKERS,
    thread_name_prefix="openclank-history-http",
)
_HISTORY_IO_SLOTS = asyncio.Semaphore(_HISTORY_IO_WORKERS)


async def _bounded_history_io(call, *, timeout: float = 13.0):
    """Run blocking History IPC away from the event loop with a hard bound.

    A timed-out request keeps its slot until the socket worker really exits,
    so repeated client deadlines cannot build an unbounded queue of orphaned
    blocking calls.
    """
    try:
        await asyncio.wait_for(_HISTORY_IO_SLOTS.acquire(), timeout=0.25)
    except asyncio.TimeoutError as exc:
        raise HTTPException(503, "History is busy; retry shortly") from exc
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_HISTORY_IO_EXECUTOR, call)

    def release_when_done(done):
        try:
            done.exception()
        except BaseException:
            pass
        _HISTORY_IO_SLOTS.release()

    future.add_done_callback(release_when_done)
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "History service request exceeded its deadline") from exc

_HISTORY_INLINE_MEDIA_THRESHOLD = 200_000
_DATA_IMAGE_RE = re.compile(r"data:image/[^;,\"]+;base64,[A-Za-z0-9+/=\s]+")


def _leading_system_prefix(messages):
    """Return genuine authority before any prior compaction projection."""
    prefix = []
    for message in messages:
        if _message_role(message) != "system":
            break
        metadata = getattr(message, "metadata", None)
        if not isinstance(metadata, dict):
            metadata = message.get("metadata", {}) if isinstance(message, dict) else {}
        content = _message_text(message)
        if metadata.get("compacted") or content.startswith("[Conversation summary"):
            break
        prefix.append(message)
    return prefix


def _history_display_content(content: Any) -> Any:
    """Return a lightweight browser-display copy of stored message content.

    Older multimodal user messages may be persisted as a JSON *string*
    containing image_url blocks with inline base64 image bytes. Those bytes are
    needed for model calls when the turn is first sent, but they should not be
    sent back through /api/history every time the user opens the chat. The
    attachment metadata already carries file ids/names for the UI cards.
    """
    if isinstance(content, list):
        text_parts = []
        omitted_media = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif block.get("type") in {"image_url", "input_image", "audio", "input_audio"}:
                omitted_media += 1
        text = "\n".join(text_parts).strip()
        if omitted_media and not text:
            return f"[{omitted_media} media attachment{'s' if omitted_media != 1 else ''} omitted from history view]"
        return text

    if not isinstance(content, str):
        return content
    if len(content) < _HISTORY_INLINE_MEDIA_THRESHOLD and "data:image/" not in content:
        return content

    stripped = content.lstrip()
    if stripped.startswith("["):
        try:
            blocks = json.loads(content)
        except (json.JSONDecodeError, TypeError, ValueError):
            blocks = None
        if isinstance(blocks, list):
            text_parts = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
            if text_parts:
                return "\n".join(text_parts).strip()

    if "data:image/" in content:
        return _DATA_IMAGE_RE.sub("[inline image omitted from history view]", content)
    return content


def _merge_continue_rows_to_delete(db_messages, db1, db2):
    """DB rows to delete when merging the last two assistant messages.

    Always the second assistant message (db2), plus ONLY the single
    intervening "continue" user message (the one carrying "previous response
    was interrupted") — matching the in-memory merge. The previous code
    deleted the whole index range between the two assistant rows, destroying
    any tool/system/user messages in between and desyncing the DB from the
    in-memory history.
    """
    to_delete = [db2]
    i1 = next((i for i, m in enumerate(db_messages) if m is db1), None)
    i2 = next((i for i, m in enumerate(db_messages) if m is db2), None)
    if i1 is not None and i2 is not None and i2 - 1 > i1:
        between = db_messages[i2 - 1]
        if getattr(between, "role", "") == "user" and            "previous response was interrupted" in (getattr(between, "content", "") or ""):
            to_delete.append(between)
    return to_delete


def setup_history_routes(session_manager, upload_handler=None) -> APIRouter:
    router = APIRouter(tags=["history"])

    def _reserve_message_uploads(
        request: Request,
        content: Any,
        metadata: Any = None,
    ) -> None:
        try:
            missing_id = reserve_message_upload_references(
                upload_handler,
                effective_user(request),
                content,
                metadata,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Invalid message attachment metadata") from exc
        if missing_id:
            raise HTTPException(
                409,
                f"Referenced upload is no longer available: {missing_id}",
            )

    def _db_history_entry(m: DbChatMessage) -> Dict[str, Any]:
        entry = {"role": m.role, "content": _history_display_content(m.content)}
        meta = {}
        if m.meta_data:
            try:
                meta = json.loads(m.meta_data) or {}
            except (json.JSONDecodeError, ValueError):
                meta = {}
        if m.timestamp and "timestamp" not in meta:
            meta["timestamp"] = m.timestamp.isoformat() + "Z"
        meta = strip_invalid_transcript_layout(m.content, meta)
        accounting = actor_accounting_for_assistant(m.id)
        if accounting is not None:
            meta["actor_accounting"] = accounting
        elif meta.get("tool_events"):
            meta["actor_accounting"] = {"state": "not_recorded", "total": None, "actors": []}
        if meta:
            entry["metadata"] = meta
        return entry

    def _db_message_metadata(m: DbChatMessage) -> Dict[str, Any]:
        meta = {}
        if m.meta_data:
            try:
                meta = json.loads(m.meta_data) or {}
            except (json.JSONDecodeError, ValueError):
                meta = {}
        if m.timestamp and "timestamp" not in meta:
            meta["timestamp"] = m.timestamp.isoformat() + "Z"
        return meta

    def _hydrate_session_history_from_db(session_id: str, rows: list[DbChatMessage]) -> None:
        """Rebuild in-memory context from raw DB rows after a history load.

        The browser history endpoint can return paged/display-trimmed messages,
        but the next model call reads ``session.history``. After a restart or a
        stale in-memory session, selecting an old chat through the paged endpoint
        used to show the transcript while the model only saw fresh context.
        """
        if not rows:
            return
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            return
        session.history = [
            ChatMessage(role=m.role, content=m.content, metadata=_db_message_metadata(m) or None)
            for m in rows
        ]
        session.message_count = len(session.history)

    def _session_needs_db_history_hydration(session_id: str, total: int) -> bool:
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            return False
        return len(session.history or []) < int(total or 0)

    @router.get("/api/history/{session_id}")
    async def get_session_history(
        request: Request,
        session_id: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> Dict[str, Any]:
        owner = _verify_session_owner(request, session_id)
        cached = session_manager.sessions.get(session_id)
        if not getattr(cached, "incognito", False):
            from src.openclank.conversation_archive import get_conversation_archive, ArchiveUnavailableError
            try:
                if get_conversation_archive().chat_erasure_started(owner=owner, chat_id=session_id):
                    raise HTTPException(409, "Conversation deletion is pending; retry erasure to finish")
            except ArchiveUnavailableError as exc:
                raise HTTPException(503, str(exc)) from exc
        if limit is not None:
            page_limit = max(1, min(int(limit), 100))
            db = SessionLocal()
            try:
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session is None:
                    raise HTTPException(404, f"Session '{session_id}' not found")

                total = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id)
                    .count()
                )
                page_offset = int(offset) if offset is not None else max(total - page_limit, 0)
                page_offset = max(0, min(page_offset, total))
                rows = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id)
                    .order_by(DbChatMessage.timestamp)
                    .offset(page_offset)
                    .limit(page_limit)
                    .all()
                )
                if _session_needs_db_history_hydration(session_id, total):
                    full_rows = (
                        db.query(DbChatMessage)
                        .filter(DbChatMessage.session_id == session_id)
                        .order_by(DbChatMessage.timestamp)
                        .all()
                    )
                    _hydrate_session_history_from_db(session_id, full_rows)
                history_dict = [
                    entry for entry in (_db_history_entry(m) for m in rows)
                    if not (entry.get("metadata") or {}).get("hidden")
                ]
                return {
                    "history": history_dict,
                    "model": db_session.model,
                    "endpoint_url": db_session.endpoint_url,
                    "name": db_session.name,
                    "offset": page_offset,
                    "limit": page_limit,
                    "total": total,
                    "has_more_before": page_offset > 0,
                    "has_more_after": page_offset + len(rows) < total,
                }
            finally:
                db.close()

        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, f"Session '{session_id}' not found")

        history_dict = []
        for msg in session.history:
            if isinstance(msg, ChatMessage):
                # Skip hidden messages (e.g. compaction summaries for AI context)
                if msg.metadata and msg.metadata.get("hidden"):
                    continue
                entry = {"role": msg.role, "content": _history_display_content(msg.content)}
                if msg.metadata:
                    meta = strip_invalid_transcript_layout(msg.content, msg.metadata)
                    accounting = actor_accounting_for_assistant(meta.get("_db_id"))
                    if accounting is not None:
                        meta = {**meta, "actor_accounting": accounting}
                    elif meta.get("tool_events"):
                        meta = {**meta, "actor_accounting": {"state": "not_recorded", "total": None, "actors": []}}
                    entry["metadata"] = meta
                history_dict.append(entry)
            elif isinstance(msg, dict):
                if msg.get("metadata", {}).get("hidden"):
                    continue
                entry = {
                    "role": msg.get("role", ""),
                    "content": _history_display_content(msg.get("content", "")),
                }
                if msg.get("metadata"):
                    meta = strip_invalid_transcript_layout(
                        msg.get("content", ""), msg["metadata"]
                    )
                    accounting = actor_accounting_for_assistant(meta.get("_db_id"))
                    if accounting is not None:
                        meta = {**meta, "actor_accounting": accounting}
                    elif meta.get("tool_events"):
                        meta = {**meta, "actor_accounting": {"state": "not_recorded", "total": None, "actors": []}}
                    entry["metadata"] = meta
                history_dict.append(entry)

        # Fallback: load from DB if in-memory is empty
        if not history_dict:
            db = SessionLocal()
            try:
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id)
                    .order_by(DbChatMessage.timestamp)
                    .all()
                )
                db_history = []
                for m in db_messages:
                    db_history.append(_db_history_entry(m))
                if db_history:
                    # Rebuild in-memory history from the full set so hidden
                    # messages (e.g. compaction summaries) are kept for AI context.
                    _hydrate_session_history_from_db(session_id, db_messages)
                # Response excludes hidden messages, matching the in-memory path.
                history_dict = [
                    m for m in db_history
                    if not (m.get("metadata") or {}).get("hidden")
                ]
            except Exception as e:
                logger.error(f"DB fallback failed for {session_id}: {e}")
            finally:
                db.close()

        return {
            "history": history_dict,
            "model": session.model,
            "endpoint_url": session.endpoint_url,
            "name": session.name,
        }

    @router.post("/api/session/{session_id}/truncate")
    async def truncate_session(request: Request, session_id: str):
        verified_owner = _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")
        mutation_owner = await _prepare_context_mutation(
            request, session_id, session_owner=getattr(session, "owner", None),
            verified_owner=verified_owner,
        )
        try:
            body = await request.json()
            keep_count = body.get("keep_count", 0)
            result = session_manager.truncate_messages(session_id, keep_count)
            return {"status": "ok", "kept": keep_count, "truncated": result}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Truncate error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/message")
    async def add_message(request: Request, session_id: str):
        """Add a message to a session (for slash command persistence)."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            role = body.get("role", "assistant")
            content = body.get("content", "")
            if not content:
                raise HTTPException(400, "content is required")
            metadata = body.get("metadata")
            _reserve_message_uploads(request, content, metadata)
            msg = ChatMessage(role=role, content=content, metadata=metadata)
            session_manager.add_message(session_id, msg)
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")

    @router.post("/api/session/{session_id}/delete-messages")
    async def delete_messages(request: Request, session_id: str):
        """Delete specific messages by DB ID (or legacy index)."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            msg_ids = body.get("msg_ids", [])
            indices = body.get("indices")  # legacy fallback

            session = session_manager.get_session(session_id)
            db = SessionLocal()
            try:
                if msg_ids:
                    # New ID-based delete
                    deleted = 0
                    for mid in msg_ids:
                        db_msg = db.query(DbChatMessage).filter(
                            DbChatMessage.id == mid,
                            DbChatMessage.session_id == session_id,
                        ).first()
                        if db_msg:
                            db.delete(db_msg)
                            deleted += 1

                    # Remove from in-memory history by matching _db_id
                    def _get_db_id(m):
                        meta = m.metadata if isinstance(m, ChatMessage) else (m.get('metadata') if isinstance(m, dict) else None)
                        return meta.get('_db_id') if isinstance(meta, dict) else None
                    session.history = [m for m in session.history if _get_db_id(m) not in msg_ids]
                elif indices:
                    # Legacy index-based delete
                    indices = sorted(indices, reverse=True)
                    db_messages = db.query(DbChatMessage).filter(
                        DbChatMessage.session_id == session_id
                    ).order_by(DbChatMessage.timestamp).all()

                    deleted = 0
                    for idx in indices:
                        if 0 <= idx < len(db_messages):
                            db.delete(db_messages[idx])
                            deleted += 1
                        if 0 <= idx < len(session.history):
                            session.history.pop(idx)
                else:
                    return {"status": "ok", "deleted": 0}

                session.message_count = len(session.history)
                db_session = db.query(DbSession).filter(DbSession.id == session_id).first()
                if db_session:
                    db_session.message_count = len(session.history)
                    from datetime import datetime, timezone
                    db_session.updated_at = datetime.now(timezone.utc)

                db.commit()
                return {"status": "ok", "deleted": deleted}
            finally:
                db.close()
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Delete messages error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/edit-message")
    async def edit_message(request: Request, session_id: str):
        """Edit the content of a message by its database ID."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            msg_id = body.get("msg_id")
            content = body.get("content")
            if not msg_id or content is None:
                raise HTTPException(400, "msg_id and content are required")

            _reserve_message_uploads(request, content)

            session = session_manager.get_session(session_id)
            db = SessionLocal()
            try:
                db_msg = db.query(DbChatMessage).filter(
                    DbChatMessage.id == msg_id,
                    DbChatMessage.session_id == session_id,
                ).first()
                if not db_msg:
                    raise HTTPException(404, "Message not found")

                db_msg.content = content
                meta = {}
                if db_msg.meta_data:
                    try: meta = json.loads(db_msg.meta_data)
                    except (json.JSONDecodeError, ValueError): pass
                meta['edited'] = True
                db_msg.meta_data = json.dumps(meta)

                # Update in-memory history by matching _db_id
                for hmsg in session.history:
                    hmeta = hmsg.metadata if isinstance(hmsg, ChatMessage) else hmsg.get('metadata')
                    if isinstance(hmeta, dict) and hmeta.get('_db_id') == msg_id:
                        if isinstance(hmsg, ChatMessage):
                            hmsg.content = content
                            hmsg.metadata['edited'] = True
                        elif isinstance(hmsg, dict):
                            hmsg['content'] = content
                            hmsg['metadata']['edited'] = True
                        break

                db.commit()
                return {"status": "ok"}
            finally:
                db.close()
        except KeyError:
            raise HTTPException(404, "Session not found")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Edit message error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/mark-stopped")
    async def mark_stopped(request: Request, session_id: str):
        """Mark the last assistant message as stopped by user."""
        _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
            # Find last assistant message and add stopped metadata
            for msg in reversed(session.history):
                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                    if isinstance(msg, ChatMessage):
                        if not msg.metadata:
                            msg.metadata = {}
                        msg.metadata['stopped'] = True
                        if not msg.metadata.get('model'):
                            msg.metadata['model'] = session.model
                    else:
                        if 'metadata' not in msg:
                            msg['metadata'] = {}
                        msg['metadata']['stopped'] = True
                        if not msg['metadata'].get('model'):
                            msg['metadata']['model'] = session.model
                    break
            # Also update in DB
            db = SessionLocal()
            try:
                import json as _json
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, DbChatMessage.role == 'assistant')
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                if db_messages:
                    meta = {}
                    if db_messages.meta_data:
                        try:
                            meta = _json.loads(db_messages.meta_data)
                        except (json.JSONDecodeError, ValueError):
                            pass
                    meta['stopped'] = True
                    if not meta.get('model'):
                        meta['model'] = session.model
                    db_messages.meta_data = _json.dumps(meta)
                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Mark stopped error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/update-last-meta")
    async def update_last_meta(request: Request, session_id: str):
        """Merge metadata into the last assistant message (e.g. save variants)."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            meta_update = body.get("metadata", {})
            session = session_manager.get_session(session_id)

            # Update in-memory
            for msg in reversed(session.history):
                if (isinstance(msg, ChatMessage) and msg.role == 'assistant') or \
                   (isinstance(msg, dict) and msg.get('role') == 'assistant'):
                    if isinstance(msg, ChatMessage):
                        if not msg.metadata:
                            msg.metadata = {}
                        msg.metadata.update(meta_update)
                    else:
                        if 'metadata' not in msg:
                            msg['metadata'] = {}
                        msg['metadata'].update(meta_update)
                    break

            # Update in DB
            db = SessionLocal()
            try:
                import json as _json
                db_msg = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id, DbChatMessage.role == 'assistant')
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                if db_msg:
                    meta = {}
                    if db_msg.meta_data:
                        try: meta = _json.loads(db_msg.meta_data)
                        except (json.JSONDecodeError, ValueError): pass
                    meta.update(meta_update)
                    db_msg.meta_data = _json.dumps(meta)
                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok"}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Update last meta error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/merge-last-assistant")
    async def merge_last_assistant(request: Request, session_id: str):
        """Merge the last two assistant messages into one (for continue)."""
        _verify_session_owner(request, session_id)
        await _prepare_context_mutation(request, session_id)
        try:
            body = await request.json()
            separator = body.get("separator", "\n\n")
            session = session_manager.get_session(session_id)

            # Find last two assistant messages in-memory
            ai_indices = []
            for i, msg in enumerate(session.history):
                role = msg.role if isinstance(msg, ChatMessage) else msg.get('role', '')
                if role == 'assistant':
                    ai_indices.append(i)

            if len(ai_indices) < 2:
                return {"status": "ok", "merged": False}

            idx1, idx2 = ai_indices[-2], ai_indices[-1]
            msg1, msg2 = session.history[idx1], session.history[idx2]

            content1 = msg1.content if isinstance(msg1, ChatMessage) else msg1.get('content', '')
            content2 = msg2.content if isinstance(msg2, ChatMessage) else msg2.get('content', '')
            merged_content = content1 + separator + content2

            # Merge metadata
            meta1 = (msg1.metadata if isinstance(msg1, ChatMessage) else msg1.get('metadata')) or {}
            meta2 = (msg2.metadata if isinstance(msg2, ChatMessage) else msg2.get('metadata')) or {}
            merged_meta = {**meta1, **meta2}
            merged_meta.pop('stopped', None)  # no longer stopped after continue

            # Update first message, remove second
            if isinstance(msg1, ChatMessage):
                msg1.content = merged_content
                msg1.metadata = merged_meta
            else:
                msg1['content'] = merged_content
                msg1['metadata'] = merged_meta

            # Also remove the hidden "continue" user message between them if present
            # It's the message at idx2-1 if it's a user message with continue text
            remove_indices = [idx2]
            if idx2 - 1 > idx1:
                between = session.history[idx2 - 1]
                between_role = between.role if isinstance(between, ChatMessage) else between.get('role', '')
                between_content = between.content if isinstance(between, ChatMessage) else between.get('content', '')
                if between_role == 'user' and 'previous response was interrupted' in between_content:
                    remove_indices.insert(0, idx2 - 1)

            for ri in sorted(remove_indices, reverse=True):
                session.history.pop(ri)

            # Update DB
            db = SessionLocal()
            try:
                import json as _json
                db_messages = (
                    db.query(DbChatMessage)
                    .filter(DbChatMessage.session_id == session_id)
                    .order_by(DbChatMessage.timestamp)
                    .all()
                )
                # Find last two assistant messages in DB
                ai_db = [(i, m) for i, m in enumerate(db_messages) if m.role == 'assistant']
                if len(ai_db) >= 2:
                    (_, db1), (_, db2) = ai_db[-2], ai_db[-1]
                    db1.content = merged_content
                    db1.meta_data = _json.dumps(merged_meta)

                    # Mirror the in-memory deletion: remove the second assistant
                    # message and ONLY the "continue" user message between them
                    # (not arbitrary tool/system/user rows). The old
                    # range-delete destroyed every row between the two assistant
                    # messages, desyncing the DB from the in-memory history.
                    for _row in _merge_continue_rows_to_delete(db_messages, db1, db2):
                        db.delete(_row)

                    db.commit()
            finally:
                db.close()
            session_manager.save_sessions()
            return {"status": "ok", "merged": True}
        except KeyError:
            raise HTTPException(404, "Session not found")
        except Exception as e:
            logger.error(f"Merge assistant error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/fork")
    async def fork_session(request: Request, session_id: str):
        """Create a new session with messages copied up to keep_count."""
        _verify_session_owner(request, session_id)
        try:
            body = await request.json()
            keep_count = body.get("keep_count", 0)

            # Get the source session
            source = session_manager.sessions.get(session_id)
            if not source:
                raise HTTPException(404, "Session not found")

            # Create new session
            new_id = str(uuid.uuid4())
            fork_name = f"\u2ADD {source.name}"
            new_session = session_manager.create_session(
                session_id=new_id,
                name=fork_name,
                endpoint_url=source.endpoint_url,
                model=source.model,
                endpoint_id=getattr(source, "endpoint_id", None),
                rag=False,
                owner=getattr(source, 'owner', None),
            )

            # Copy messages up to keep_count
            msgs_to_copy = source.history[:keep_count]
            for msg in msgs_to_copy:
                # Copy the metadata dict. Sharing it would let the fork's
                # persistence (add_message -> _persist_message stamps
                # _db_id/timestamp onto the dict) mutate the SOURCE session's
                # in-memory messages, corrupting their _db_id and breaking
                # edit/delete-by-id on the original conversation.
                meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else None
                new_session.add_message(ChatMessage(msg.role, msg.content, meta))
            try:
                from src.event_bus import fire_event
                fire_event("session_created", getattr(source, 'owner', None))
            except Exception:
                logger.debug("session_created event dispatch failed", exc_info=True)

            return {
                "status": "ok",
                "id": new_id,
                "name": fork_name,
                "kept": len(msgs_to_copy),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Fork error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.get("/api/conversations/topics")
    async def get_conversation_topics(request: Request) -> Dict[str, Any]:
        from src.auth_helpers import require_user
        user = require_user(request)
        try:
            return analyze_topics(session_manager, owner=user or None)
        except Exception as e:
            raise HTTPException(500, f"Topic analysis failed: {e}")

    @router.get("/api/session/{session_id}/context")
    async def get_session_context_usage(request: Request, session_id: str) -> Dict[str, Any]:
        """Return an estimated whole-chat context usage for the session's model.

        Streaming footers report the prompt size for the last request. This
        endpoint estimates the persisted session context so the header can show
        when the whole chat is approaching compaction.
        """
        _verify_session_owner(request, session_id)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")

        try:
            from src.model_context import estimate_tokens, get_context_length

            messages = session.get_context_messages()
            used = int(estimate_tokens(messages))
            ctx_len = int(get_context_length(session.endpoint_url, session.model) or 0)
            pct = round((used / ctx_len) * 100, 1) if ctx_len else 0.0
            pct = max(0.0, min(100.0, pct))
            visible_messages = sum(
                1 for m in session.history
                if not (getattr(m, "metadata", None) or {}).get("hidden")
            )
            compacted_messages = sum(
                1 for m in session.history
                if (getattr(m, "metadata", None) or {}).get("compacted")
            )
            can_compact = used > 0
            return {
                "session_id": session_id,
                "model": session.model,
                "endpoint_url": session.endpoint_url,
                "used_tokens": used,
                "context_length": ctx_len,
                "context_percent": pct,
                "messages": visible_messages,
                "context_messages": len(messages),
                "compacted_messages": compacted_messages,
                "can_compact": can_compact,
                "should_compact": pct >= 70,
                "auto_compact_threshold": 85,
            }
        except Exception as e:
            logger.error(f"Context usage error {session_id}: {e}")
            raise HTTPException(500, str(e))

    @router.post("/api/session/{session_id}/compact")
    async def compact_session(request: Request, session_id: str):
        """Manually trigger context compaction for a session."""
        verified_owner = _verify_session_owner(request, session_id)
        from src.auth_helpers import effective_user
        owner = effective_user(request)
        try:
            session = session_manager.get_session(session_id)
        except KeyError:
            raise HTTPException(404, "Session not found")
        # Persisted owner remains authoritative after authenticated scope check.
        owner = getattr(session, "owner", None) or owner

        # One active compactor per execution context: refuse host compaction
        # when a persistent ACP engine compactor owns the session. Bind on the
        # real protocol (model_target.transport), not missing Session attrs.
        # Checked before any context-mutation side effects.
        from src.context_compactor import session_has_persistent_engine

        if session_has_persistent_engine(session):
            raise HTTPException(
                409,
                "Persistent ACP sessions compact through the engine "
                "(session.summarize); host compaction is reserved for "
                "finite contexts.",
            )
        mutation_owner = await _prepare_context_mutation(
            request, session_id, session_owner=getattr(session, "owner", None),
            verified_owner=verified_owner,
        )
        owner = mutation_owner or getattr(session, "owner", None) or owner
        try:
            from src.model_context import estimate_tokens, get_context_length
            from src.openclank.modality_facade import complete_text

            system_prefix = _leading_system_prefix(session.history)
            conversation = session.history[len(system_prefix):]
            keep_count = min(8, max(4, len(conversation) // 4))
            older = conversation[:-keep_count]
            if not older:
                return {"status": "ok", "message": "Not enough messages to compact"}

            ctx_len = get_context_length(session.endpoint_url, session.model)
            messages_before = session.get_context_messages()
            used_before = estimate_tokens(messages_before)
            pct_before = round((used_before / ctx_len) * 100, 1) if ctx_len else 0
            msg_count_before = len(session.history)

            # Keep only last 4 conversation messages, summarize the rest.
            # System/persona authority is split out before this boundary.
            recent = conversation[-keep_count:]

            # Build text to summarize
            convo_text = "\n".join(
                f"{_message_role(m).upper()}: "
                f"{_message_text(m)[:2000]}"
                for m in older
            )

            from src.context_compactor import SELF_SUMMARY_SYSTEM_PROMPT, normalize_compaction_summary
            compaction_count = sum(1 for m in session.history if isinstance(m, ChatMessage) and "[Conversation summary" in (m.content or ""))
            sys_prompt = SELF_SUMMARY_SYSTEM_PROMPT.replace("{count}", str(len(older))).replace("{n}", str(compaction_count + 1))
            import hashlib

            summary = await complete_text(
                owner=owner,
                purpose="utility",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": convo_text},
                ],
                temperature=0.2,
                max_output_tokens=1024,
                idempotency_key=(
                    "history-compact-"
                    + hashlib.sha256(
                        f"{session_id}\0{convo_text}".encode("utf-8")
                    ).hexdigest()[:32]
                ),
            )
            summary = normalize_compaction_summary(summary)
            if not (summary or "").strip():
                raise HTTPException(502, "Compaction summary was empty; history unchanged")

            # Replace session history: preserve the leading system/persona
            # prefix, then add the summary and recent conversation.  The
            # prefix is active authority, not source text to summarize away.
            system_summary = ChatMessage(
                role="system",
                content=f"[Conversation summary — {len(older)} earlier messages were compacted]\n\n{summary}",
                metadata={
                    "compacted": True,
                    "hidden": True,
                    "compaction_trigger": "manual",
                    "compaction_actor": "host_finite",
                    "source_boundary_split": len(older),
                },
            )
            # Visible assistant message just shows stats
            summary_msg = ChatMessage(
                role="assistant",
                content=f"**Conversation compacted** — {len(older)} messages summarized, {len(recent)} kept.",
                metadata={"compacted": True, "messages_removed": len(older)},
            )
            new_history = system_prefix + [system_summary, summary_msg] + list(recent)
            # Projection writer: archives source parts first, preserves
            # retained message IDs, never rekeys original history identity.
            # Archive failure raises recoverable-unavailable and leaves source
            # intact — the broad except below must not swallow that as 500.
            try:
                replaced = session_manager.replace_messages(session_id, new_history)
            except Exception as exc:
                from src.openclank.conversation_archive import ArchiveUnavailableError

                if isinstance(exc, ArchiveUnavailableError):
                    raise HTTPException(
                        503,
                        "conversation archive unavailable; history unchanged",
                    ) from exc
                raise
            if not replaced:
                raise HTTPException(500, "Failed to save compacted history")
            session.history = new_history
            session.message_count = len(session.history)
            logger.info(f"Compact: session {session_id} history now has {len(session.history)} messages (was {msg_count_before})")

            session_manager.save_sessions()

            used_after = estimate_tokens(session.get_context_messages())
            pct_after = round((used_after / ctx_len) * 100, 1) if ctx_len else 0

            return {
                "status": "ok",
                "message": f"Compacted: {msg_count_before} msgs → {len(session.history)} msgs ({pct_before}% → {pct_after}%)",
                "before": pct_before,
                "after": pct_after,
                "ok": True,
                "summarized": len(older),
                "kept": len(recent),
                "message_count": len(session.history),
                "projection": "host_finite",
                "source_parts_retained": True,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Manual compact error {session_id}: {e}")
            raise HTTPException(500, str(e))

    return router


def setup_history_settings_routes() -> APIRouter:
    """Authenticated policy/usage API for the Lore history foundation."""
    import os
    from src.auth_helpers import effective_user, get_current_user, require_user
    from src.openclank import file_policy as file_policy_module
    from src.openclank.filesystem_registry import FilesystemRootRegistry
    from src.openclank.history_client import (
        HistoryAvailabilityError,
        HistoryClient,
        HistoryClientError,
    )
    from src.openclank.history_paths import scope_id

    router = APIRouter(prefix="/api/history", tags=["history-settings"])

    def is_admin(request: Request, user: str) -> bool:
        manager = getattr(request.app.state, "auth_manager", None)
        return bool(manager and user and manager.is_admin(user))

    def account_id_for(request: Request, user: str) -> str:
        manager = getattr(request.app.state, "auth_manager", None)
        resolver = getattr(manager, "account_id", None)
        if callable(resolver) and user:
            try:
                resolved = resolver(user)
                if resolved:
                    return str(resolved)
            except Exception:
                pass
        return str(user or "local-installation")

    def authorized_roots(owner: str, admin: bool) -> list[dict[str, Any]]:
        registry = FilesystemRootRegistry()
        roots: list[dict[str, Any]] = []
        if admin:
            roots.extend(registry.list(owner))
        roots.extend(
            item.get("root") or {}
            for item in registry.visibility_for_subject(owner)
        )
        return [
            root for root in roots
            if root.get("kind") == "recursive_directory"
            and root.get("enabled")
            and root.get("availability") == "available"
            and "read" in set(root.get("capabilities") or [])
        ]

    def is_authorized_directory(value: str, roots: list[dict[str, Any]]) -> bool:
        import os
        candidate = os.path.realpath(os.path.expanduser(value))
        for root in roots:
            base = os.path.realpath(str(root.get("canonical_path") or ""))
            try:
                if base and os.path.commonpath((base, candidate)) == base:
                    return True
            except ValueError:
                continue
        return False

    def service_client(account_id: str, request: Request) -> HistoryClient:
        socket_path = os.environ.get("OPENCLANK_HISTORY_SOCKET")
        if not socket_path:
            raise HTTPException(503, "history service is unavailable")
        supervisor = getattr(request.app.state, "history_supervisor", ...)
        if supervisor is None:
            raise HTTPException(503, "history service is unavailable")
        if supervisor is not ...:
            process = getattr(supervisor, "process", None)
            poll = getattr(process, "poll", None)
            if process is None or (callable(poll) and poll() is not None):
                raise HTTPException(503, "history service is unavailable")
        actor_id = str(get_current_user(request) or account_id)
        return HistoryClient(socket_path, actor_id=actor_id, account_id=account_id, timeout=4.0)

    def availability_http_error(exc: HistoryAvailabilityError) -> HTTPException:
        status_code = {
            "resource_not_found": 404,
            "version_not_found": 404,
            "version_expired": 410,
            "version_expiring": 409,
            "version_not_restorable": 409,
            "resource_unavailable": 409,
            "invalid_cursor": 422,
            "invalid_limit": 422,
            "invalid_offset": 416,
            "invalid_version_ref": 422,
            "ambiguous_version_ref": 409,
        }.get(exc.code, 503)
        return HTTPException(
            status_code,
            detail={"code": exc.code, "message": str(exc)},
        )

    async def resolve_history_resource_ids(
        client: HistoryClient,
        resource_id: str,
        request: Request,
        owner: str,
        account_id: str,
        *,
        resolved_paths: dict[str, str] | None = None,
    ) -> list[str]:
        """Resolve Files identity through current authorized paths and History registrations.

        Registry paths and private IDs stay inside this server process. Inactive
        registrations qualify only while the exact path remains the current,
        permitted Host resource for this authenticated account.
        """
        requested = str(resource_id or "").strip()
        if requested.startswith("file:"):
            response = await _bounded_history_io(lambda: client.resolve_resource(requested))
            handle = (response.get("Resource") or {}).get("handle") if isinstance(response, dict) else None
            if not isinstance(handle, dict) or str(handle.get("account_id") or "") != account_id:
                raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")
            return [requested]
        if not re.fullmatch(r"resource-[0-9a-f]{64}", requested):
            raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")

        from pathlib import Path

        from src.openclank.files_facade import FilesFacadeError, ProviderContext
        from src.openclank.files_host_provider import HostFilesProvider, host_origin_for_path
        from src.openclank.resource_refs import stable_resource_id

        files_user = str(get_current_user(request) or owner or "").strip().lower()
        if not files_user or account_id_for(request, files_user) != account_id:
            raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")
        policy_repository = file_policy_module.FilePolicyRepository()
        host_provider = HostFilesProvider(registry=FilesystemRootRegistry())
        files_context = ProviderContext(
            owner_subject_id=account_id,
            owner_username=files_user,
            policy_generation=policy_repository.generation(),
            is_admin=is_admin(request, files_user),
            workspace_id="default",
        )

        matches: list[str] = []
        seen_private_ids: set[str] = set()
        cursor = None
        for _page_number in range(16):
            page = await _bounded_history_io(
                lambda cursor=cursor: client.list_registered_resources(cursor=cursor, limit=100),
                timeout=1.5,
            )
            for registration in page["items"]:
                if (
                    registration.get("account_id") != account_id
                    or not registration.get("workspace_id")
                ):
                    continue
                root_path = str(registration.get("root_path") or "")
                relative_path = str(registration.get("relative_path") or "")
                relative = Path(relative_path)
                if (
                    not root_path
                    or not Path(root_path).is_absolute()
                    or not relative_path
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    continue
                candidate_path = str(Path(root_path) if relative_path == "." else Path(root_path) / relative)
                try:
                    candidate_origin = host_origin_for_path(candidate_path)
                    candidate_public_id = stable_resource_id(
                        owner_subject_id=account_id,
                        provider="host",
                        origin_id=candidate_origin,
                    )
                except (OSError, TypeError, ValueError):
                    continue
                if candidate_public_id != requested:
                    continue
                try:
                    current = await host_provider.resource_for_path(files_context, path=candidate_path)
                except FilesFacadeError as exc:
                    if exc.code in {"resource_unavailable", "invalid_resource_ref"}:
                        continue
                    raise HTTPException(503, "Files authorization could not be checked") from exc
                current_public_id = stable_resource_id(
                    owner_subject_id=account_id,
                    provider="host",
                    origin_id=current.origin_id,
                )
                if current_public_id == requested:
                    private_id = str(registration.get("resource_id") or "").strip()
                    if not private_id.startswith("file:") or private_id in seen_private_ids:
                        continue
                    seen_private_ids.add(private_id)
                    matches.append(private_id)
                    if resolved_paths is not None:
                        resolved_paths[private_id] = candidate_path
                    if len(matches) > _HISTORY_RESOURCE_MATCH_LIMIT:
                        raise HistoryAvailabilityError(
                            "resource_unavailable",
                            "Files identity has too many matching History registrations",
                        )
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        else:
            raise HistoryAvailabilityError(
                "resource_unavailable",
                "Files identity could not be resolved within the bounded History registry scan",
            )
        if not matches:
            raise HistoryAvailabilityError("resource_not_found", "registered Files resource is unavailable")
        return sorted(matches)

    async def run_history_candidate_calls(candidate_ids: list[str], call_factory, *, timeout: float = 1.5):
        """Run at most one worker-sized batch of trusted IPC calls at a time."""
        results = []
        for start in range(0, len(candidate_ids), _HISTORY_IO_WORKERS):
            batch = candidate_ids[start : start + _HISTORY_IO_WORKERS]
            results.extend(
                await asyncio.gather(
                    *(
                        _bounded_history_io(call_factory(private_id), timeout=timeout)
                        for private_id in batch
                    )
                )
            )
        return results

    def history_page_cursor(version: Dict[str, Any]) -> str:
        timestamp = version.get("timestamp_millis")
        action_id = version.get("action_id")
        version_id = version.get("version_id")
        source_provider = version.get("source_provider", "filesystem")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or not isinstance(action_id, str)
            or not action_id
            or not isinstance(version_id, str)
            or not version_id
            or not isinstance(source_provider, str)
            or not source_provider
            or len(source_provider) > 128
        ):
            raise HistoryAvailabilityError("resource_unavailable", "History returned an invalid version descriptor")
        payload = json.dumps(
            {
                "timestamp_millis": timestamp,
                "action_id": action_id,
                "version_id": version_id,
                "source_provider": source_provider,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    async def list_history_versions_for_candidates(
        client: HistoryClient,
        candidate_ids: list[str],
        *,
        cursor: str | None,
        limit: int,
    ) -> Dict[str, Any]:
        pages = await run_history_candidate_calls(
            candidate_ids,
            lambda private_id: lambda: client.list_resource_versions(
                private_id,
                cursor=cursor,
                limit=limit,
            ),
        )
        combined = []
        seen_version_refs: dict[str, str] = {}
        has_more = False
        for private_id, page in zip(candidate_ids, pages):
            has_more = has_more or page.get("next_cursor") is not None
            for item in page["items"]:
                version_ref = str(item.get("id") or "")
                if not version_ref:
                    raise HistoryAvailabilityError("resource_unavailable", "History returned a version without an identity")
                previous_owner = seen_version_refs.get(version_ref)
                if previous_owner is not None and previous_owner != private_id:
                    raise HistoryAvailabilityError(
                        "resource_unavailable",
                        "a History version matches multiple private registrations",
                    )
                seen_version_refs[version_ref] = private_id
                combined.append(item)
        combined.sort(
            key=lambda item: (
                int(item.get("timestamp_millis", -1)),
                str(item.get("action_id") or ""),
                str(item.get("version_id") or ""),
                str(item.get("source_provider") or "filesystem"),
            ),
            reverse=True,
        )
        has_more = has_more or len(combined) > limit
        selected = combined[:limit]
        next_cursor = history_page_cursor(selected[-1]) if has_more and selected else None
        return {"items": selected, "next_cursor": next_cursor}

    async def run_version_lookup(candidate_ids: list[str], call_factory):
        outcomes = []
        for start in range(0, len(candidate_ids), _HISTORY_IO_WORKERS):
            batch = candidate_ids[start : start + _HISTORY_IO_WORKERS]
            outcomes.extend(
                await asyncio.gather(
                    *(
                        _bounded_history_io(call_factory(private_id), timeout=1.5)
                        for private_id in batch
                    ),
                    return_exceptions=True,
                )
            )
        matches = []
        failures = []
        for private_id, outcome in zip(candidate_ids, outcomes):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, HistoryAvailabilityError) or outcome.code != "version_not_found":
                    failures.append(outcome)
            else:
                matches.append((private_id, outcome))
        if failures:
            failure = failures[0]
            if isinstance(failure, BaseException):
                raise failure
        if len(matches) > 1:
            raise HistoryAvailabilityError(
                "resource_unavailable",
                "selected History version matches multiple private registrations",
            )
        if not matches:
            raise HistoryAvailabilityError("version_not_found", "selected History version is unavailable")
        return matches[0]

    async def find_history_version_resource_id(
        client: HistoryClient,
        candidate_ids: list[str],
        version_ref: str,
    ) -> str:
        private_id, _selection = await run_version_lookup(
            candidate_ids,
            lambda private_id: lambda: client.resolve_resource_version(private_id, version_ref),
        )
        return private_id

    async def read_history_version_chunk_for_candidates(
        client: HistoryClient,
        candidate_ids: list[str],
        version_ref: str,
        *,
        offset: int,
        length: int,
    ) -> Dict[str, Any]:
        _private_id, chunk = await run_version_lookup(
            candidate_ids,
            lambda private_id: lambda: client.read_resource_version_chunk(
                private_id,
                version_ref,
                offset=offset,
                length=length,
            ),
        )
        return chunk

    def public_history_preview(preview: Dict[str, Any], resource_id: str) -> Dict[str, Any]:
        result = dict(preview)
        resource = result.get("resource")
        if isinstance(resource, dict):
            safe_resource = {"resource_id": resource_id}
            if isinstance(resource.get("generation"), int):
                safe_resource["generation"] = resource["generation"]
            result["resource"] = safe_resource
        return result

    def public_history_restore_result(response: Dict[str, Any], resource_id: str, request: Request) -> Dict[str, Any]:
        result = dict(response)
        receipt = result.get("Restore")
        if isinstance(receipt, dict):
            safe_receipt = dict(receipt)
            resources = receipt.get("resources")
            if isinstance(resources, list):
                safe_receipt["resources"] = [
                    {**dict(item), "resource_id": resource_id}
                    for item in resources
                    if isinstance(item, dict)
                ]
            result["Restore"] = safe_receipt
            proof = receipt.get("verification") or {}
            if receipt.get("outcome") == "Complete" and proof.get("status") == "Verified" and proof.get("content_hash") and proof.get("content_hash") == proof.get("restored_content_hash"):
                from src.openclank.achievement_producers import record_activity
                record_activity(request, "lore.restore.committed", str(proof.get("restore_id") or ""), {
                    "resourceId": resource_id, "versionId": proof.get("version_id"),
                    "contentHash": proof.get("content_hash"), "restoredContentHash": proof.get("restored_content_hash"),
                    "receiptKind": "VerifiedRestore", "digestOnly": False,
                })
            if isinstance(safe_receipt.get("verification"), dict):
                safe_receipt["verification"] = {**safe_receipt["verification"], "resource_id": resource_id}
        return result

    def visible_snapshot(owner: str, admin: bool, request: Request) -> dict[str, Any]:
        account_id = account_id_for(request, owner)
        client = service_client(account_id, request)
        try:
            policy_response = client.get_policy()
            usage_response = client.get_usage()
            status_response = client.get_status()
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        policy = dict(policy_response.get("Policy") or {})
        status = dict(status_response.get("Status") or {})
        status["available"] = True
        if not admin:
            inherited_global = dict(policy.get("global") or {})
            inherited_global["inherited"] = True
            policy["global"] = inherited_global
        roots = authorized_roots(owner, admin)
        visible_scopes = []
        for scope in policy.get("scopes") or []:
            if not isinstance(scope, dict):
                continue
            if not admin and scope.get("owner_account_id") != account_id:
                continue
            if scope.get("kind") in {"Global", "Workspace", "Directory"}:
                scope["kind"] = str(scope["kind"]).lower()
            if scope.get("kind") == "directory" and not is_authorized_directory(str(scope.get("root") or scope.get("value") or ""), roots):
                continue
            if scope.get("kind") == "directory":
                canonical = os.path.realpath(str(scope.get("root") or ""))
                match = next((root for root in roots if os.path.realpath(str(root.get("canonical_path"))) == canonical), None)
                if not match:
                    continue
                scope["root_id"] = match.get("id")
                scope["display_path"] = match.get("display_path")
            visible_scopes.append(scope)
        policy["scopes"] = visible_scopes
        result = {
            "policy": policy,
            "usage": usage_response.get("Usage") or {},
            "status": status,
            "directory_options": [
                {"id": root.get("id"), "label": root.get("display_path") or root.get("canonical_path")}
                for root in roots
            ],
        }
        return result

    def workspace_options(owner: str, request: Request, policy: Dict[str, Any], *, admin: bool = False) -> list[str]:
        account_id = account_id_for(request, owner)
        # Workspace choices come from the canonical Files policy repository,
        # so a newly-created workspace is available to History before a
        # history scope has been configured for it. Existing scope IDs remain
        # visible as edit targets during migration or after a workspace was
        # archived, but they never broaden account visibility.
        options = {
            str(scope.get("workspace_id"))
            for scope in (policy.get("scopes") or [])
            if isinstance(scope, dict)
            and (admin or scope.get("owner_account_id") == account_id)
            and str(scope.get("kind") or "").lower() == "workspace"
            and scope.get("workspace_id")
        }
        try:
            repository = file_policy_module.FilePolicyRepository()
            workspaces = repository.list_workspaces(
                owner_subject_id=None if admin else account_id,
                include_archived=False,
            )
            options.update(str(workspace.id) for workspace in workspaces if workspace.id)
        except (file_policy_module.FilePolicyError, OSError, ValueError) as exc:
            # History settings remain readable when the canonical policy store
            # is temporarily unavailable; configured scope IDs still support
            # safe editing and the next request can retry discovery.
            logger.warning("canonical workspace discovery unavailable: %s", exc)
        options.add("default")
        return sorted(item for item in options if item)

    @router.post("/restore")
    async def restore_history_resource(request: Request) -> Dict[str, Any]:
        """Forward an authenticated restore through the Rust provider boundary.

        ``destination_path`` is only an untrusted routing hint. The service
        resolves it against its private ResourceKey registry and rejects it
        unless it names the registered resource in the typed request.
        """
        user = require_user(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "history restore must be a JSON object") from exc
        if not isinstance(body, dict):
            raise HTTPException(422, "history restore must be a JSON object")
        restore_request = body.get("request")
        source = body.get("source")
        resource_id = body.get("resource_id")
        version_ref = body.get("version_ref")
        destination_path = body.get("destination_path")
        has_selection = isinstance(resource_id, str) and bool(resource_id.strip()) and isinstance(version_ref, str) and bool(version_ref.strip())
        selection_was_requested = resource_id is not None or version_ref is not None
        if selection_was_requested and not has_selection:
            raise HTTPException(422, "resource_id and version_ref must be provided together")
        if source is not None and has_selection:
            raise HTTPException(422, "provide either a version selection or a source receipt")
        has_destination_hint = isinstance(destination_path, str) and bool(destination_path.strip())
        pathless_selection = has_selection and source is None and not has_destination_hint
        if not pathless_selection and (
            not isinstance(restore_request, dict)
            or (not isinstance(source, dict) and not has_selection)
            or not has_destination_hint
        ):
            raise HTTPException(422, "restore requires a selected version or source receipt and a registered destination")
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        if pathless_selection:
            nested_request = restore_request if isinstance(restore_request, dict) else {}
            restore_id = str(body.get("restore_id") or nested_request.get("restore_id") or "").strip()
            expected_fingerprint = body.get("expected_destination_fingerprint")
            if expected_fingerprint is None:
                expected_fingerprint = nested_request.get("expected_destination_fingerprint")
            if not restore_id or len(restore_id) > 256:
                raise HTTPException(422, "restore_id is required")
            if not isinstance(expected_fingerprint, str) or not expected_fingerprint or len(expected_fingerprint) > 256:
                raise HTTPException(422, "the reviewed destination fingerprint from restore preview is required")
        try:
            client = service_client(account_id, request)
            if pathless_selection:
                public_resource_id = resource_id
                resolved_paths: dict[str, str] = {}
                private_resource_ids = await resolve_history_resource_ids(
                    client, public_resource_id, request, owner, account_id,
                    resolved_paths=resolved_paths,
                )
                private_resource_id = await find_history_version_resource_id(
                    client, private_resource_ids, version_ref
                )
                response = await _bounded_history_io(
                    lambda: client.restore_resource_version(
                        restore_id,
                        private_resource_id,
                        version_ref,
                        expected_destination_fingerprint=expected_fingerprint,
                    )
                )
                result = public_history_restore_result(response, public_resource_id, request)
                receipt = result.get("Restore")
                if isinstance(receipt, dict) and receipt.get("outcome") == "Complete":
                    # The mutation has committed. Refresh failure must never turn
                    # it into a failed restore or encourage a mutation retry.
                    try:
                        from src.openclank.files_facade import FilesFacade, ProviderContext
                        from src.openclank.files_host_provider import HostFilesProvider
                        files_user = str(get_current_user(request) or owner or "").strip().lower()
                        if not files_user or account_id_for(request, files_user) != account_id:
                            raise ValueError("restore account changed")
                        path = resolved_paths.get(private_resource_id)
                        if not path:
                            raise ValueError("registered refresh locator unavailable")
                        context = ProviderContext(
                            owner_subject_id=account_id, owner_username=files_user,
                            policy_generation=file_policy_module.FilePolicyRepository().generation(),
                            is_admin=is_admin(request, files_user), workspace_id="default",
                        )
                        facade = FilesFacade([HostFilesProvider(registry=FilesystemRootRegistry())])
                        refreshed = await facade.host_resource_for_path(context, path=path)
                        if refreshed.get("id") != public_resource_id or refreshed.get("provider") != "host":
                            raise ValueError("restored resource identity changed")
                        receipt["refreshed_resource"] = refreshed
                    except Exception:
                        receipt["refresh_error"] = "The restored file could not be reauthorized for its open view."
                return result

            if str(restore_request.get("account_id") or "") != account_id:
                raise HTTPException(403, "restore request belongs to another account")
            destination = restore_request.get("destination")
            if not isinstance(destination, dict) or str(destination.get("account_id") or "") != account_id:
                raise HTTPException(403, "restore destination belongs to another account")
            if not str(restore_request.get("restore_id") or "").strip():
                raise HTTPException(422, "restore_id is required")
            if not has_selection and not all(str(restore_request.get(field) or "").strip() for field in ("source_action_id", "source_version_id")):
                raise HTTPException(422, "restore source identifiers are required")
            selected_resource_id = None
            if has_selection:
                selected_resource_ids = await resolve_history_resource_ids(
                    client, resource_id, request, owner, account_id
                )
                selected_resource_id = await find_history_version_resource_id(
                    client, selected_resource_ids, version_ref
                )

            def apply_selected_restore():
                effective_request = dict(restore_request)
                selected_source = source
                if has_selection:
                    selection = client.resolve_resource_version(selected_resource_id, version_ref)
                    resource = selection["resource"]
                    version = selection["version"]
                    if (
                        str(destination.get("resource_id") or "") not in {
                            str(resource.get("resource_id") or ""),
                            str(resource_id),
                        }
                        or str(destination.get("workspace_id") or "") != str(resource.get("workspace_id") or "")
                        or str(destination.get("provider") or "") != "filesystem"
                        or str(destination.get("account_id") or "") != str(resource.get("account_id") or "")
                    ):
                        raise HTTPException(403, "restore destination must be the selected Files resource")
                    selected_action = str(version.get("action_id") or "")
                    selected_version = str(version.get("version_id") or "")
                    if (
                        restore_request.get("source_action_id") not in (None, selected_action)
                        or restore_request.get("source_version_id") not in (None, selected_version)
                    ):
                        raise HTTPException(409, "restore source changed from the selected History version")
                    effective_request["destination"] = {
                        **dict(destination),
                        "resource_id": str(resource.get("resource_id") or ""),
                    }
                    effective_request["source_action_id"] = selected_action
                    effective_request["source_version_id"] = selected_version
                    selected_source = selection["receipt"]
                response = client.restore_host(
                    effective_request,
                    selected_source,
                    destination_path=destination_path,
                    source_host_metadata=body.get("source_host_metadata") if isinstance(body.get("source_host_metadata"), dict) else None,
                )
                return public_history_restore_result(response, str(resource_id or ""), request)

            return await _bounded_history_io(apply_selected_restore)
        except HTTPException:
            raise
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HistoryClientError as exc:
            message = str(exc)
            status = 409 if "conflict" in message.lower() else 422
            raise HTTPException(status, message) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc

    @router.post("/capture-repair/{action_id}")
    async def repair_history_capture(action_id: str, request: Request) -> Dict[str, Any]:
        """Repair a selected committed recovery gap under the original actor."""
        from src.auth_helpers import copal_owner_for_user
        from src.openclank.copal_errors import CopalBridgeError
        from src.openclank.history_repair import CaptureRepairUnavailable, repair_capture, snapshot_native_resource
        from src.openclank.files_facade import FilesFacadeError, ProviderContext
        from src.openclank.files_host_provider import HostFilesProvider, host_origin_for_path
        user = require_user(request)
        account_id = account_id_for(request, user)
        if not action_id.strip() or len(action_id) > 256:
            raise HTTPException(422, "Recovery action identity is invalid")
        client = service_client(account_id, request)

        async def copal_snapshot(key):
            bridge = getattr(request.app.state, "copal_bridge", None)
            if bridge is None:
                raise CaptureRepairUnavailable("Copal storage is unavailable; recovery gap remains.")
            return await bridge.call("get", {"owner": copal_owner_for_user(user), "workspace_id": key["workspace_id"], "id": key["resource_id"]}, timeout=4)

        async def file_snapshot(path):
            if not os.path.isabs(path):
                raise CaptureRepairUnavailable("The recovery locator is invalid.")
            context = ProviderContext(owner_subject_id=account_id, owner_username=user,
                policy_generation=file_policy_module.FilePolicyRepository().generation(), is_admin=is_admin(request, user), workspace_id="default")
            provider = HostFilesProvider(registry=FilesystemRootRegistry())
            content = await provider.content(context, origin_id=host_origin_for_path(path))
            if content.size is None or content.size > 10 * 1024 * 1024 or content.stream is None:
                raise CaptureRepairUnavailable("This recovery copy requires a bounded provider reconciliation.")
            captured = bytearray()
            async for chunk in content.stream(0, content.size):
                captured.extend(chunk)
                if len(captured) > 10 * 1024 * 1024:
                    raise CaptureRepairUnavailable("Recovery copy exceeds the repair bound.")
            if len(captured) != content.size:
                raise CaptureRepairUnavailable("The original file snapshot is unavailable.")
            return bytes(captured)

        async def resource_snapshot(proof):
            context = ProviderContext(owner_subject_id=account_id, owner_username=user,
                policy_generation=file_policy_module.FilePolicyRepository().generation(), is_admin=is_admin(request, user), workspace_id=proof['resource_key']['workspace_id'])
            provider = HostFilesProvider(registry=FilesystemRootRegistry())
            async def authorize(path):
                await provider._authorized_path(context, host_origin_for_path(path))
            return await snapshot_native_resource(proof, authorize=authorize, read_file=file_snapshot)

        try:
            return await repair_capture(client, action_id, account_id=account_id, copal_snapshot=copal_snapshot, file_snapshot=file_snapshot, resource_snapshot=resource_snapshot)
        except CaptureRepairUnavailable as exc:
            raise HTTPException(409, {"code": "capture_repair_unavailable", "message": str(exc), "action_id": action_id}) from exc
        except HistoryClientError as exc:
            # Exact actor/account failures remain unavailable; a human cannot
            # choose an agent actor identity to acquire its completion rights.
            raise HTTPException(409, {"code": "capture_repair_unavailable", "message": "Recovery action is unavailable or requires its original actor.", "action_id": action_id}) from exc
        except (FilesFacadeError, CopalBridgeError, OSError, asyncio.TimeoutError) as exc:
            raise HTTPException(503, {"code": "capture_repair_unavailable", "message": "Recovery snapshot is unavailable; the gap remains.", "action_id": action_id}) from exc

    @router.get("/resources/{resource_id}/versions")
    async def list_history_resource_versions(
        resource_id: str,
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=2048),
    ) -> Dict[str, Any]:
        """List bounded Lore version metadata for an authenticated Files resource."""
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            client = service_client(account_id, request)
            private_resource_ids = await resolve_history_resource_ids(
                client, resource_id, request, owner, account_id
            )
            page = await list_history_versions_for_candidates(
                client,
                private_resource_ids,
                cursor=cursor,
                limit=limit,
            )
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        return {"resource_id": resource_id, **page}

    @router.get("/resources/{resource_id}/versions/{version_ref}")
    async def read_history_resource_version(
        resource_id: str,
        version_ref: str,
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=64 * 1024, ge=1, le=256 * 1024),
    ) -> Dict[str, Any]:
        """Read one bounded base64 chunk of one exact listed version."""
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            client = service_client(account_id, request)
            private_resource_ids = await resolve_history_resource_ids(
                client, resource_id, request, owner, account_id
            )
            chunk = await read_history_version_chunk_for_candidates(
                client,
                private_resource_ids,
                version_ref,
                offset=offset,
                length=limit,
            )
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        content = chunk.get("content")
        return {
            "resource_id": resource_id,
            "version": chunk["version"],
            "offset": chunk["offset"],
            "content_encoding": "base64",
            "content": base64.b64encode(content).decode("ascii") if content is not None else None,
            "eof": chunk["eof"],
        }

    @router.get("/resources/{resource_id}/versions/{version_ref}/restore-preview")
    async def preview_history_resource_restore(
        resource_id: str,
        version_ref: str,
        request: Request,
    ) -> Dict[str, Any]:
        """Review the selected source against the current private Files target."""
        user = require_user(request)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        try:
            client = service_client(account_id, request)
            private_resource_ids = await resolve_history_resource_ids(
                client, resource_id, request, owner, account_id
            )
            private_resource_id = await find_history_version_resource_id(
                client, private_resource_ids, version_ref
            )
            preview = await _bounded_history_io(
                lambda: client.preview_resource_version_restore(private_resource_id, version_ref)
            )
        except HistoryAvailabilityError as exc:
            raise availability_http_error(exc) from exc
        except HTTPException:
            raise
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        return public_history_preview(preview, resource_id)

    @router.get("/settings")
    async def get_history_settings(request: Request) -> Dict[str, Any]:
        user = require_user(request)
        owner = effective_user(request) or user or "local-installation"

        def load_settings():
            admin = is_admin(request, user)
            result = visible_snapshot(owner, admin, request)
            result["owner"] = owner
            result["workspace_options"] = workspace_options(
                owner,
                request,
                result.get("policy") or {},
                admin=admin,
            )
            return result

        return await _bounded_history_io(load_settings)

    @router.put("/settings")
    async def put_history_settings(request: Request) -> Dict[str, Any]:
        user = require_user(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "history settings must be a JSON object") from exc
        if not isinstance(body, dict):
            raise HTTPException(422, "history settings must be a JSON object")
        try:
            expected = int(body.get("expected_revision"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(428, "expected_revision is required") from exc
        requested = body.get("policy", body)
        if not isinstance(requested, dict):
            raise HTTPException(422, "policy must be an object")
        admin = is_admin(request, user)
        owner = effective_user(request) or user
        account_id = account_id_for(request, owner)
        client = service_client(account_id, request)
        try:
            current_response = await _bounded_history_io(client.get_policy)
        except (HistoryClientError, OSError, ValueError) as exc:
            raise HTTPException(503, f"history service unavailable: {exc}") from exc
        current = dict(current_response.get("Policy") or {})
        if not admin and "global" in requested:
            raise HTTPException(403, "installation history target requires administration")
        roots = authorized_roots(owner, admin)
        patch = dict(requested)
        if "scopes" in requested:
            requested_scopes = requested.get("scopes") or []
            if not isinstance(requested_scopes, list):
                raise HTTPException(422, "scopes must be an array")
            options = set(
                await _bounded_history_io(
                    lambda: workspace_options(owner, request, current, admin=admin)
                )
            )
            for scope in requested_scopes:
                if not isinstance(scope, dict):
                    raise HTTPException(422, "scope must be an object")
                scope_owner = str(scope.get("owner_account_id") or account_id).strip()
                if not admin and scope_owner != account_id:
                    raise HTTPException(403, "workspace history settings belong to their owner")
                kind = str(scope.get("kind") or "").strip().lower()
                if kind == "workspace" and str(scope.get("workspace_id") or "").strip() not in options:
                    raise HTTPException(403, "workspace is not assigned to this account")
                if kind == "workspace" and not str(scope.get("workspace_id") or "").strip():
                    raise HTTPException(422, "workspace history settings require a workspace id")
                if kind == "directory":
                    root_id = str(scope.get("root_id") or "")
                    selected = next((root for root in roots if root.get("id") == root_id and root.get("kind") == "recursive_directory"), None)
                    if selected is None:
                        raise HTTPException(403, "directory history settings require an assigned Files root")
                    candidate = os.path.realpath(str(selected.get("canonical_path")))
                scope["scope_id"] = scope_id(
                    kind,
                    candidate if kind == "directory" else str(scope.get("workspace_id") or ""),
                    str(scope.get("workspace_id") or "") or None,
                    scope_owner,
                )
                scope["revision"] = int(current.get("revision") or expected) + 1
                scope["enabled"] = bool(scope.get("enabled", True))
                scope["kind"] = {"global": "Global", "workspace": "Workspace", "directory": "Directory"}.get(kind, scope.get("kind"))
                scope["owner_account_id"] = scope_owner
                if scope["kind"] == "Directory":
                    scope["root"] = candidate
                    scope.pop("root_id", None)
                    scope.pop("value", None)
            if not admin:
                preserved = [
                    scope for scope in current.get("scopes") or []
                    if isinstance(scope, dict) and scope.get("owner_account_id") != account_id
                ]
                patch["scopes"] = preserved + requested_scopes
        merged = dict(current)
        if isinstance(requested.get("global"), dict):
            merged["global"] = {**(current.get("global") or {}), **requested["global"]}
        if "scopes" in patch:
            merged["scopes"] = patch["scopes"]
        for key in ("revision",):
            merged[key] = current.get(key)
        # The worker owns merging scopes belonging to other accounts. Sending
        # their rows or global policy as a normal actor broadens the request
        # and causes the worker's authorization checks to reject it.
        submission = merged if admin else {"revision": current.get("revision")}
        if not admin and "scopes" in requested:
            submission["scopes"] = requested_scopes
        try:
            await _bounded_history_io(
                lambda: client.set_policy(submission, expected_revision=expected)
            )
        except HistoryClientError as exc:
            message = str(exc)
            status = 409 if "RevisionMismatch" in message or "revision" in message.lower() else 422
            raise HTTPException(status, message) from exc
        def load_updated_settings():
            result = visible_snapshot(owner, admin, request)
            result["owner"] = owner or "local-installation"
            result["workspace_options"] = workspace_options(
                owner,
                request,
                result.get("policy") or {},
                admin=admin,
            )
            return result

        return await _bounded_history_io(load_updated_settings)

    return router
