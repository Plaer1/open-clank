"""Canonical owner-scoped chat state transitions shared by UI and Files.

This service keeps archive/restore from becoming a second direct-database
implementation in the Files provider. It coordinates the same active-run and
agent-projection lifecycle before changing the canonical session row, then
updates the optional in-process SessionManager cache.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from core.database import Session as DbSession, SessionLocal, utcnow_naive


class ChatLifecycleError(RuntimeError):
    def __init__(self, message: str, *, code: str = "chat_unavailable") -> None:
        super().__init__(message)
        self.code = code


class ChatLifecycleService:
    def __init__(
        self,
        *,
        session_factory: Callable = SessionLocal,
        session_manager: Any | None = None,
        mimo_supervisor: Any | None = None,
        memory_provider: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.session_manager = session_manager
        context = getattr(session_manager, "lifecycle_context", None)
        self.mimo_supervisor = mimo_supervisor or getattr(context, "mimo_supervisor", None)
        self.memory_provider = memory_provider or getattr(context, "memory_provider", None)
        self.auth_manager = getattr(context, "auth_manager", None)

    async def _release_code_subscriber(self, owner: str, session_id: str, workspace_id: str | None) -> None:
        """Release only this chat after the canonical lifecycle change succeeds."""
        if not workspace_id or not callable(getattr(self.memory_provider, "code_index_lifecycle", None)):
            return
        from src.openclank.code_index_lifecycle import workspace_index_event
        from src.openclank.file_policy import FilePolicyRepository
        try:
            await workspace_index_event(
                repository=FilePolicyRepository(), provider=self.memory_provider,
                auth_manager=self.auth_manager, owner=owner, workspace_id=workspace_id,
                subscriber_id="session:" + session_id, action="release",
            )
        except Exception:
            # Chat erasure/archive is authoritative even when the derived
            # index owner is unavailable. Native authority fences remain valid.
            logging.getLogger(__name__).warning("Could not release chat code index subscription")

    async def erase(self, *, owner: str, session_id: str, allow_protected: bool = False, current_account_id: str | None = None) -> dict[str, Any]:
        """One deletion journey for real routes and authenticated agent tools."""
        owner, session_id = str(owner or "").strip(), str(session_id or "").strip()
        if not owner or not session_id or self.session_manager is None:
            raise ChatLifecycleError("Conversation owner is unavailable")
        db = self.session_factory()
        try:
            row = db.query(DbSession).filter(DbSession.id == session_id, DbSession.owner == owner).first()
            cached = self.session_manager.sessions.get(session_id)
            workspace_id = getattr(row, "workspace_id", None) or getattr(cached, "workspace_id", None)
            if row is None and (cached is None or cached.owner != owner):
                raise ChatLifecycleError("Conversation is unavailable")
            if row is not None and row.is_important and not allow_protected:
                raise ChatLifecycleError("Unfavorite before deleting", code="protected")
        finally:
            db.close()
        from src import agent_runs
        if agent_runs.is_active(session_id):
            raise ChatLifecycleError("Conversation has an active run", code="active_run")
        from src.openclank.transcript_projection import purge_execution_projection
        try:
            from types import SimpleNamespace
            from src.openclank.chat_source_bridge import guarded_source_mutation
            context = getattr(self.session_manager, 'lifecycle_context', None)
            state = SimpleNamespace(memory_provider=self.memory_provider, mimo_supervisor=self.mimo_supervisor,
                                    auth_manager=self.auth_manager, s05_profile=getattr(context, 's05_profile', None))
            def pre_fence():
                nonlocal cached, workspace_id
                check = self.session_factory()
                try:
                    current = check.query(DbSession).filter(DbSession.id == session_id, DbSession.owner == owner).first()
                    current_cached = self.session_manager.sessions.get(session_id)
                    if current is None and (current_cached is None or current_cached.owner != owner):
                        raise RuntimeError("Conversation owner changed")
                    if current is not None and current.is_important and not allow_protected:
                        raise RuntimeError("Conversation protection changed")
                    cached = current_cached
                    workspace_id = getattr(current, "workspace_id", None) or getattr(current_cached, "workspace_id", None)
                    if agent_runs.is_active(session_id):
                        raise RuntimeError("Conversation has an active run")
                finally:
                    check.close()
            async with guarded_source_mutation(state=state, owner=owner, chat_id=session_id,
                    whole_chat=True, pre_fence=pre_fence) as source_capability:
                if current_account_id:
                    account_for=getattr(self.auth_manager,'account_id',None); owner_for=getattr(self.auth_manager,'username_for_account_id',None)
                    if (not callable(account_for) or not callable(owner_for) or account_for(owner) != current_account_id
                        or owner_for(current_account_id) != owner):
                        raise RuntimeError('Current account changed before chat deletion')
                    from src import bg_jobs
                    detach=asyncio.create_task(asyncio.to_thread(bg_jobs.detach_media_for_chat,session_id=session_id,owner=owner,account_id=current_account_id))
                    try:
                        await asyncio.shield(detach)
                    except asyncio.CancelledError:
                        while not detach.done():
                            try: await asyncio.shield(detach)
                            except asyncio.CancelledError: continue
                        detach.result()
                        raise
                # Retire all derived skill originals before the held chat is erased.
                if source_capability is not None:
                    from src.openclank.chat_source_bridge import retire_skill_draft_manifests
                    from src.openclank.conversation_archive import get_conversation_archive
                    await retire_skill_draft_manifests(capability=source_capability,
                        archive=get_conversation_archive())
                # The existing chat guard is held through the canonical commit.
                if source_capability is None:
                    await purge_execution_projection(self.mimo_supervisor, session_id, owner=owner)
                else:
                    await purge_execution_projection(self.mimo_supervisor, session_id, owner=owner, _chat_guard=source_capability.guard)
                if not getattr(cached, "incognito", False):
                    from src.openclank.conversation_archive import get_conversation_archive
                    get_conversation_archive().begin_chat_erasure(owner=owner, chat_id=session_id)
                    if self.memory_provider is not None:
                        detach = getattr(self.memory_provider, "mark_conversation_source_removed", None)
                        if not callable(detach):
                            raise RuntimeError("Memery source-link owner is unavailable")
                        receipt = await detach(owner=owner, session_id=session_id)
                        if not isinstance(receipt, dict) or receipt.get("complete") is not True:
                            raise RuntimeError("Memery source-link removal is incomplete")
                delete_kwargs = {'retired_execution': True}
                if source_capability is not None:
                    delete_kwargs['_source_capability'] = source_capability
                deletion = asyncio.create_task(asyncio.to_thread(self.session_manager.delete_session, session_id, **delete_kwargs))
                try:
                    deleted = await asyncio.shield(deletion)
                except asyncio.CancelledError:
                    # The synchronous commit cannot be cancelled. Keep the
                    # source guard until it actually settles, then propagate.
                    while not deletion.done():
                        try:
                            await asyncio.shield(deletion)
                        except asyncio.CancelledError:
                            continue
                    try:
                        deletion.result()
                    except Exception:
                        pass
                    raise
                if not deleted:
                    raise RuntimeError("Conversation erasure did not complete")
        except Exception as exc:
            raise ChatLifecycleError("Deletion incomplete; retry this conversation", code="erasure_pending") from exc
        await self._release_code_subscriber(owner, session_id, workspace_id)
        return {"status": "deleted", "retained_content_erased": True}

    async def set_archived(self, *, owner: str | None, session_id: str, archived: bool) -> None:
        normalized_owner = str(owner or "").strip()
        normalized_id = str(session_id or "").strip()
        if not normalized_id or not normalized_owner:
            raise ChatLifecycleError("Chat is unavailable")

        from src import agent_runs
        if agent_runs.is_active(normalized_id):
            raise ChatLifecycleError("Chat has an active run", code="active_run")

        from src.openclank.transcript_projection import purge_execution_projection

        try:
            await purge_execution_projection(
                self.mimo_supervisor,
                normalized_id,
                owner=normalized_owner or None,
            )
        except PermissionError as exc:
            raise ChatLifecycleError("Chat is unavailable") from exc
        except RuntimeError as exc:
            raise ChatLifecycleError("Chat projection is busy", code="projection_busy") from exc

        changed = await asyncio.to_thread(
            self._set_archived_row,
            normalized_owner,
            normalized_id,
            bool(archived),
        )
        if not changed:
            raise ChatLifecycleError("Chat is unavailable")

        if archived:
            db = self.session_factory()
            try:
                row = db.query(DbSession).filter(DbSession.id == normalized_id, DbSession.owner == normalized_owner).first()
                workspace_id = getattr(row, "workspace_id", None)
            finally:
                db.close()
            await self._release_code_subscriber(normalized_owner, normalized_id, workspace_id)

        manager = self.session_manager
        if manager is None:
            return
        cached = getattr(manager, "sessions", {}).get(normalized_id)
        if cached is not None:
            cached.archived = bool(archived)
        elif not archived:
            try:
                manager._load_session_from_db(normalized_id)
            except Exception:
                # The database is canonical; a future list/open lazily reloads
                # the row if this best-effort process cache update fails.
                pass

    def _set_archived_row(self, owner: str, session_id: str, archived: bool) -> bool:
        db = self.session_factory()
        try:
            query = db.query(DbSession).filter(DbSession.id == session_id)
            if not owner:
                return False
            query = query.filter(DbSession.owner == owner)
            row = query.first()
            if row is None:
                return False
            row.archived = bool(archived)
            row.updated_at = utcnow_naive()
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
