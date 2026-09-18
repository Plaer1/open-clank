"""Canonical owner-scoped chat state transitions shared by UI and Files.

This service keeps archive/restore from becoming a second direct-database
implementation in the Files provider. It coordinates the same active-run and
agent-projection lifecycle before changing the canonical session row, then
updates the optional in-process SessionManager cache.
"""

from __future__ import annotations

import asyncio
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
    ) -> None:
        self.session_factory = session_factory
        self.session_manager = session_manager
        self.mimo_supervisor = mimo_supervisor

    async def set_archived(self, *, owner: str | None, session_id: str, archived: bool) -> None:
        normalized_owner = str(owner or "").strip()
        normalized_id = str(session_id or "").strip()
        if not normalized_id:
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
            # Auth-disabled single-user mode historically permits owner-stamped
            # rows after a deployment toggles authentication off. Authenticated
            # callers always supply an owner and remain exact-owner scoped.
            if owner:
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
