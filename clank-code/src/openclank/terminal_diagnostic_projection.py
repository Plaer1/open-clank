"""Deletion-coupled, content-free recorded Native diagnostic snapshots."""
import asyncio

from sqlalchemy import text

_NAMES = frozenset({'ProviderAuthError', 'APIError', 'MessageOutputLengthError',
    'StructuredOutputError', 'InvalidOutputError', 'StreamIncompleteError',
    'TextToolCallError', 'ContentFilterError', 'ModelError', 'UnknownError'})


async def current_diagnostics(*, state, owner, account, chat_id, admission, barrier, credential_current, budget):
    """Read positive Host-owned snapshots, independent of any Native worker or mapping."""
    from core.database import SessionLocal
    from src.openclank.account_request_barrier import _Admission, AccountRequestBarrier
    from src.openclank.chat_source_bridge import read_terminal_error_association
    from src.openclank.conversation_archive import get_conversation_archive

    auth = getattr(state, 'auth_manager', None)
    if (type(admission) is not _Admission or not isinstance(barrier, AccountRequestBarrier)
            or getattr(state, 'account_request_barrier', None) is not barrier or auth is None
            or not callable(credential_current) or type(budget) is not dict
            or type(budget.get('deadline')) not in (int, float)
            or not await barrier.is_live(owner, admission)):
        return None
    loop = asyncio.get_running_loop()
    archive = get_conversation_archive()
    def current():
        try:
            return bool(not admission.released and not barrier._fenced(admission.owner)
                and admission.owner == owner.strip().lower()
                and getattr(state, 'account_request_barrier', None) is barrier
                and getattr(state, 'auth_manager', None) is auth and credential_current() is True
                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner)
        except Exception: return False
    def load():
        if loop.time() >= budget['deadline'] or not current() or archive.chat_erasure_started(owner=owner, chat_id=chat_id): return None
        with SessionLocal() as db:
            db.connection().exec_driver_sql('PRAGMA busy_timeout=200')
            session = db.execute(text('SELECT owner, archived FROM sessions WHERE id=:chat'), {'chat':chat_id}).first()
            if not session or session[0] != owner or bool(session[1]): return None
            rows = db.execute(text("SELECT i.message_id FROM host_source_delivery_intent i JOIN chat_messages m ON m.id=i.message_id AND m.session_id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.part_id='body' AND i.revision=1 AND i.origin_completed=0 AND i.origin_assistant_id IS NULL AND i.origin_state='pending' AND CASE WHEN json_valid(i.execution_snapshot_json) THEN json_extract(i.execution_snapshot_json,'$.kind') END='terminal-diagnostic-snapshot-v2' ORDER BY m.timestamp DESC, i.message_id DESC LIMIT 3"), {'owner':account, 'chat':chat_id}).all()
            values = [read_terminal_error_association(db, archive, owner=account, chat_id=chat_id,
                root_operation_id=row[0], current_authority=current) for row in rows]
            if not all(values) or not current() or archive.chat_erasure_started(owner=owner, chat_id=chat_id): return None
            return values
    async def guarded_load():
        task = asyncio.create_task(asyncio.to_thread(load))
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            try: task.result()
            except Exception: pass
            raise
    try:
        first = await guarded_load()
        if first is None or not await barrier.is_live(owner, admission): return None
        second = await guarded_load()
        if second != first or not current() or not await barrier.is_live(owner, admission): return None
        return {(v['diagnostic']['errorName'], v['diagnostic']['stage']) for v in first}
    except Exception:
        return None


async def current_source_wire_observation(*, state, owner, account, chat_id, admission, barrier, credential_current, budget):
    """Read only current, retained positive paired-source closures."""
    from core.database import SessionLocal
    from src.openclank.account_request_barrier import _Admission, AccountRequestBarrier
    from src.openclank.chat_source_bridge import read_current_forward_closure
    from src.openclank.conversation_archive import get_conversation_archive

    auth = getattr(state, 'auth_manager', None)
    if (type(admission) is not _Admission or not isinstance(barrier, AccountRequestBarrier)
            or getattr(state, 'account_request_barrier', None) is not barrier or auth is None
            or not callable(credential_current) or type(budget) is not dict
            or type(budget.get('deadline')) not in (int, float)
            or not await barrier.is_live(owner, admission)):
        return None
    loop = asyncio.get_running_loop()
    archive = get_conversation_archive()
    def current():
        try:
            return bool(not admission.released and not barrier._fenced(admission.owner)
                and admission.owner == owner.strip().lower()
                and getattr(state, 'account_request_barrier', None) is barrier
                and getattr(state, 'auth_manager', None) is auth and credential_current() is True
                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner)
        except Exception: return False
    def load():
        if loop.time() >= budget['deadline'] or not current() or archive.chat_erasure_started(owner=owner, chat_id=chat_id): return None
        with SessionLocal() as db:
            db.connection().exec_driver_sql('PRAGMA busy_timeout=200')
            session = db.execute(text('SELECT owner, archived FROM sessions WHERE id=:chat'), {'chat':chat_id}).first()
            if not session or session[0] != owner or bool(session[1]): return None
            rows = db.execute(text("SELECT i.message_id FROM host_source_delivery_intent i JOIN chat_messages m ON m.id=i.message_id AND m.session_id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.part_id='body' AND i.revision=1 AND i.origin_completed=1 AND i.origin_assistant_id IS NOT NULL AND i.origin_state IN ('pending','delivered','projection_user_dropped','projection_assistant_dropped','projection_both_dropped') AND CASE WHEN json_valid(i.execution_snapshot_json) THEN json_extract(i.execution_snapshot_json,'$.version') END IN (3,4) ORDER BY m.timestamp DESC, i.message_id DESC LIMIT 3"), {'owner':account, 'chat':chat_id}).all()
            values = [read_current_forward_closure(db, archive, owner=account, provider_owner=owner,
                chat_id=chat_id, message_id=row[0]) for row in rows]
            if not rows or not all(values) or not current() or archive.chat_erasure_started(owner=owner, chat_id=chat_id): return None
            return values
    async def guarded_load():
        task = asyncio.create_task(asyncio.to_thread(load))
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            try: task.result()
            except Exception: pass
            raise
    try:
        first = await guarded_load()
        if first is None or not await barrier.is_live(owner, admission): return None
        second = await guarded_load()
        if second != first or not current() or not await barrier.is_live(owner, admission): return None
        return {'state':'observed', 'source_wire_observed':True, 'paired_output_recorded':True,
                'coverage':'partial', 'max_sources':3}
    except Exception:
        return None
