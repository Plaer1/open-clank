"""Existing S08 contributor guards shared by automatic work and human review."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

from src.memery_consolidation import bounded_codelivery_pairs, bounded_importance_target_pairs
from src.openclank.chat_source_bridge import CompletedTurnLifecycleOwner
from src.openclank.memory_job_admission import _completed_turn_thread


@asynccontextmanager
async def held_s08_sources(*, lifecycle, selection, origins, current, human_review=False, journal=None, importance_target=False):
    """Hold exact ordered chat guards; inputs are original owner selections.

    Human review uses current readable source checks, never automatic-capture
    eligibility. The caller retains its account barrier and current authority.
    """
    if type(lifecycle) is not CompletedTurnLifecycleOwner or not callable(current):
        raise ValueError('S08 source authority unavailable')
    pairs=journal.pairs() if journal is not None else (bounded_importance_target_pairs(selection,origins) if importance_target is True else bounded_codelivery_pairs(selection,origins))   # BQ-8: journal included rows
    sources={item.source for item,_ in pairs}
    if not sources or any(source.account_id!=lifecycle.account_id for source in sources):
        raise ValueError('S08 source authority unavailable')
    chats=sorted({source.chat_id for source in sources})
    ordered=sorted(sources,key=lambda ref:(ref.chat_id,ref.message_id,ref.revision))
    async with AsyncExitStack() as stack:
        guards={chat:await stack.enter_async_context(lifecycle.mapping.async_chat_admission_lock(chat)) for chat in chats}
        async def validate():
            if current() is not True: return False
            for source in ordered:
                def check():
                    if human_review:
                        lifecycle._assert_owner_source(source=source,guard=guards[source.chat_id])
                        result=lifecycle.source_authority.verify_feedback_origin(source)
                        return result.get('state') == 'available' and result.get('source') == source
                    lifecycle.for_source(source=source,guard=guards[source.chat_id])
                    return True
                result,cancelled=await _completed_turn_thread(check)
                if cancelled: raise asyncio.CancelledError
                if result is not True: return False
            return current() is True
        if not await validate(): raise ValueError('S08 source authority unavailable')
        yield guards,validate


@asynccontextmanager
async def held_s08_automatic_sources(*, lifecycle, sources, current):
    """Hold and revalidate the exact automatic-work sources under their owning lifecycle."""
    from src.memery_external_sources import SourceRef, _source_ref_error
    if (type(lifecycle) is not CompletedTurnLifecycleOwner or not callable(current)
            or type(sources) is not tuple or not 2 <= len(sources) <= 8
            or len(set(sources)) != len(sources)
            or any(type(source) is not SourceRef or _source_ref_error(source)
                   or source.account_id != lifecycle.account_id for source in sources)):
        raise ValueError('automatic source authority unavailable')
    ordered=sorted(sources,key=lambda ref:(ref.chat_id,ref.message_id,ref.revision))
    chats=sorted({source.chat_id for source in ordered})
    if len(chats)!=len(ordered): raise ValueError('distinct source chats required')
    async with AsyncExitStack() as stack:
        guards={chat:await stack.enter_async_context(lifecycle.mapping.async_chat_admission_lock(chat)) for chat in chats}
        async def validate():
            if current() is not True: return False
            for source in ordered:
                def check():
                    lifecycle.for_source(source=source,guard=guards[source.chat_id])
                    return True
                result,cancelled=await _completed_turn_thread(check)
                if cancelled: raise asyncio.CancelledError
                if result is not True or current() is not True: return False
            return current() is True
        if not await validate(): raise ValueError('automatic source authority unavailable')
        yield guards,validate


@asynccontextmanager
async def held_s08_review_sources(*, lifecycle, sources, current):
    """Hold every source from a verified private original review manifest."""
    from src.memery_external_sources import SourceRef, _source_ref_error
    if (type(lifecycle) is not CompletedTurnLifecycleOwner or not callable(current)
            or type(sources) is not tuple or not 1 <= len(sources) <= 64
            or any(type(source) is not SourceRef or _source_ref_error(source)
                   or source.account_id != lifecycle.account_id for source in sources)):
        raise ValueError('S08 source authority unavailable')
    ordered=sorted(set(sources),key=lambda ref:(ref.chat_id,ref.message_id,ref.revision))
    chats=sorted({source.chat_id for source in ordered})
    async with AsyncExitStack() as stack:
        guards={chat:await stack.enter_async_context(lifecycle.mapping.async_chat_admission_lock(chat)) for chat in chats}
        async def validate():
            if current() is not True: return False
            for source in ordered:
                def check():
                    lifecycle._assert_owner_source(source=source,guard=guards[source.chat_id])
                    result=lifecycle.source_authority.verify_feedback_origin(source)
                    return result.get('state') == 'available' and result.get('source') == source
                result,cancelled=await _completed_turn_thread(check)
                if cancelled: raise asyncio.CancelledError
                if result is not True: return False
            return current() is True
        if not await validate(): raise ValueError('S08 source authority unavailable')
        yield guards,validate


@asynccontextmanager
async def held_s08_human_review_sources(*, request, user, account, provider, sources, current):
    """Use the existing human chat guards for exact private-manifest sources."""
    from core.database import SessionLocal
    from src.memery_external_sources import CurrentHostUserSource, SourceRef, _source_ref_error
    from src.openclank.chat_source_bridge import guarded_source_mutation
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    if (not callable(current) or type(sources) is not tuple or not 1 <= len(sources) <= 64
            or any(type(source) is not SourceRef or _source_ref_error(source) or source.account_id != account for source in sources)):
        raise ValueError('S08 source authority unavailable')
    ordered=sorted(set(sources),key=lambda ref:(ref.chat_id,ref.message_id,ref.revision))
    authority=CurrentHostUserSource(owner=user,auth_manager=request.app.state.auth_manager,session_factory=SessionLocal,
        archive=get_conversation_archive(),workspace_repository=request.app.state.files_policy_repository,policy_store=STORE,guarded_preparation=provider._guarded_preparation)
    def check():
        if current() is not True: raise ValueError('S08 review authority changed')
    async with AsyncExitStack() as stack:
        guards={}
        for chat in sorted({source.chat_id for source in ordered}):
            guard=await stack.enter_async_context(guarded_source_mutation(request,owner=user,chat_id=chat,pre_fence=check))
            if guard is None or guard.provider is not provider: raise ValueError('S08 source guard unavailable')
            guards[chat]=guard
        def current_guards():
            try:
                check()
                for chat,guard in guards.items(): guard.validate(provider=provider,owner=user,chat_id=chat)
                return True
            except Exception: return False
        async def validate():
            if current_guards() is not True: return False
            for source in ordered:
                result,cancelled=await _completed_turn_thread(authority.verify_feedback_origin,source)
                if cancelled: raise asyncio.CancelledError
                if result.get('state')!='available' or result.get('source')!=source or current_guards() is not True: return False
            return current_guards()
        if await validate() is not True: raise ValueError('S08 source authority unavailable')
        yield current_guards,validate
