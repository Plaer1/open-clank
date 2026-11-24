"""Original chat-scoped authority for the private document vector read."""
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass

_SEAL=object()

@dataclass(frozen=True)
class _NativeVectorRanking:
    _admission:object
    _pointer:dict
    _watermark:str
    _hits:tuple
    _truncated:bool
    _backend:str
    _approximate:bool
    _policy_current:object
    _audit:object
    _seal:object

    def project(self,owner,workspace,project,pointer,watermark,eligible):
        if (self._seal is not _SEAL or type(self._admission) is not _ChatDocumentSearch
                or self._admission.current() is not True or self._policy_current() is not True or owner!=self._admission.owner
                or workspace!='' or project!='' or pointer.get('health')!='current'
                or (pointer.get('generation_id'),pointer.get('publication_id'),pointer.get('source_watermark'))
                    !=(self._pointer['generation_id'],self._pointer['publication_id'],self._pointer['publication_watermark'])
                or watermark!=self._watermark or any(item['chunk_id'] not in eligible for item in self._hits)):
            raise ValueError('document vector ranking changed')
        return [(item['chunk_id'],item['score']) for item in self._hits],self._truncated,self._backend,self._approximate,dict(self._audit) if self._audit is not None else None

class _ChatDocumentSearch:
    def __init__(self,request,origin,owner,chat,root,credential,seal):
        if seal is not _SEAL:raise ValueError('document search admission unavailable')
        self.request=request;self.origin=origin;self.owner=owner;self.chat=chat;self.root=root
        self.account=origin.owner;self.credential=credential;self.operation_id='dvquery_'+hashlib.sha256(json.dumps([self.account,chat,root],separators=(',',':')).encode()).hexdigest()
        self._active=False;self._mapping=None;self._guard=None;self._barrier=None;self._admission=None;self._auth=None;self._verifier=None;self._source_ref=None;self._provider=None;self._rag=None;self._factory=None;self._capture=None;self._selected=None
    def current(self):
        from src.auth_helpers import effective_user,require_privilege
        from src.rag_singleton import get_rag_manager
        try:
            state=self.request.app.state
            return (self._active and effective_user(self.request)==self.owner and require_privilege(self.request,'can_use_documents')==self.owner
                and self.credential() is True and state.auth_manager is self._auth and state.account_request_barrier is self._barrier
                and getattr(state,'capture_original_credential',None) is self._capture
                and (getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)) is self._provider
                and get_rag_manager() is self._rag and getattr(self._rag,'_embedding_client',None) is self._factory
                and (self._selected is None or self._factory.for_owner(self.owner) is self._selected)
                and self._auth.account_id(self.owner)==self.account and self._auth.username_for_account_id(self.account)==self.owner
                and self._barrier.assert_live_admission(self.owner,self._admission) is None
                and self._mapping.validate_chat_guard(self.chat,self._guard) is None
                and (self._source_ref is None or self._verifier.verify_feedback_origin(self._source_ref).get('state')=='available'))
        except Exception:return False
    def _seal_ranking(self,pointer,watermark,hits,truncated,backend,approximate,policy_current,audit=None):
        if self.current() is not True:raise ValueError('document search admission changed')
        return _NativeVectorRanking(self,dict(pointer),watermark,tuple(dict(x) for x in hits),truncated,backend,approximate,policy_current,dict(audit) if audit is not None else None,_SEAL)
    async def baseline(self,rag,query,k):
        from src.auth_helpers import effective_user,require_privilege
        from src.openclank.account_request_barrier import AccountRequestBarrier,_current_admission
        state=self.request.app.state;barrier=getattr(state,'account_request_barrier',None);auth=getattr(state,'auth_manager',None)
        if type(barrier) is not AccountRequestBarrier or auth is None:raise ValueError('document search unavailable')
        def current(admission):
            try:
                barrier.assert_live_admission(self.owner,admission)
                return (effective_user(self.request)==self.owner and require_privilege(self.request,'can_use_documents')==self.owner
                    and self.credential() is True and getattr(state,'auth_manager',None) is auth
                    and getattr(state,'account_request_barrier',None) is barrier and auth.account_id(self.owner)==self.account
                    and auth.username_for_account_id(self.account)==self.owner)
            except Exception:return False
        async with barrier.admitted(self.owner):
            admission=_current_admission.get()
            if not current(admission):raise ValueError('document search unavailable')
            results=rag.search(query,k,owner=self.owner)
            if not current(admission):raise ValueError('document search changed')
            return results
    async def search(self,rag,provider,query,k):
        from sqlalchemy import text as sql_text
        from core.database import SessionLocal
        from src.embeddings import ManagedEmbeddingClient
        from src.memery_external_sources import CurrentHostUserSource,SourceRef
        from src.openclank.account_request_barrier import AccountRequestBarrier,_current_admission
        from src.openclank.conversation_archive import get_conversation_archive
        from src.openclank.memory_policy import STORE
        from src.openclank.session_map import OwnerSessionMap
        from src.rag_singleton import get_rag_manager
        state=self.request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
        provider=getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)
        from src.memery_provider import MemeryProvider
        if type(provider) is not MemeryProvider:raise ValueError('document search unavailable')
        supervisor=getattr(state,'mimo_supervisor',None)
        if type(barrier) is not AccountRequestBarrier or auth is None or supervisor is None:raise ValueError('document search unavailable')
        pool=supervisor.owner_lifecycle_pool();mapping=OwnerSessionMap(pool._runtime_home(self.owner)/'session-map.json',self.owner)
        archive=get_conversation_archive();factory=getattr(rag,'_embedding_client',None)
        if get_rag_manager() is not rag or not callable(getattr(factory,'for_owner',None)):raise ValueError('document search unavailable')
        verifier=CurrentHostUserSource(owner=self.owner,auth_manager=auth,session_factory=SessionLocal,archive=archive,
            workspace_repository=getattr(state,'files_policy_repository',None),policy_store=STORE,guarded_preparation=getattr(provider,'_guarded_preparation',None))
        async with barrier.admitted(self.owner):
            admission=_current_admission.get()
            async with mapping.async_chat_admission_lock(self.chat,blocking=False) as guard:
                if guard is None:raise ValueError('document search unavailable')
                self._mapping=mapping;self._guard=guard;self._barrier=barrier;self._admission=admission;self._auth=auth
                self._provider=provider;self._rag=rag;self._factory=factory;self._capture=getattr(state,'capture_original_credential',None);self._active=True
                try:
                    if self.current() is not True:raise ValueError('document search unavailable')
                    def source():
                        with SessionLocal() as db:
                            row=db.execute(sql_text("SELECT m.source_body_revision,m.source_body_hash,s.workspace_id,i.origin_json,i.origin_state FROM chat_messages m JOIN sessions s ON s.id=m.session_id JOIN host_source_delivery_intent i ON i.owner=:account AND i.chat_id=s.id AND i.message_id=m.id AND i.part_id='body' AND i.revision=1 WHERE m.id=:message AND m.session_id=:chat AND m.role='user'"),{'account':self.account,'message':self.root,'chat':self.chat}).first()
                        if not row or row[0]!=1 or not isinstance(row[1],str) or not re.fullmatch(r'[0-9a-f]{64}',row[1]) or row[4] not in ('pending','delivered'):
                            raise ValueError('document source unavailable')
                        origin=json.loads(row[3]);choices=json.loads(origin['choices_json'])
                        if any(choices.get(x) is not False for x in ('incognito','noMemory','compareMode')):
                            raise ValueError('document source unavailable')
                        return SourceRef(2 if row[2] is None else 1,'host_user_body',self.account,self.chat,'main',self.root,'body',1,row[1],row[2],None)
                    ref=await asyncio.to_thread(source)
                    self._verifier=verifier;self._source_ref=ref
                    async def verify():
                        return self.current() is True and (await asyncio.to_thread(verifier.verify_feedback_origin,ref)).get('state')=='available' and self.current() is True
                    if not await verify():raise ValueError('document source unavailable')
                    selected=factory.for_owner(self.owner)
                    if type(selected) is not ManagedEmbeddingClient or selected.owner!=self.owner:raise ValueError('document model unavailable')
                    self._selected=selected
                    client=selected.for_root_operation(self.operation_id)
                    ranking=await provider._host_document_vector_search(self,client,query,deadline=asyncio.get_running_loop().time()+10.0)
                    if not await verify():raise ValueError('document source changed')
                    results=rag.search(query,k,owner=self.owner,_native_vector=ranking)
                    if not await verify():raise ValueError('document source changed')
                    return results
                finally:self._active=False;self._verifier=None;self._source_ref=None;self._selected=None

def _issue_chat_document_search(request,origin,*,owner,chat,root,incognito,compare_mode,no_memory):
    from fastapi import Request
    from src.auth_helpers import effective_user,require_privilege
    from src.openclank.chat_source_bridge import HostOriginAdmission
    if (not isinstance(request,Request) or type(origin) is not HostOriginAdmission or incognito or compare_mode or no_memory
            or not owner or effective_user(request)!=owner or require_privilege(request,'can_use_documents')!=owner
            or origin.chat_id!=chat or origin.root_operation_id!=root):
        return None
    auth=getattr(request.app.state,'auth_manager',None);capture=getattr(request.app.state,'capture_original_credential',None)
    if auth is None or not callable(capture) or auth.account_id(owner)!=origin.owner or auth.username_for_account_id(origin.owner)!=owner:return None
    credential=capture(request,owner,origin.owner)
    if not callable(credential) or credential() is not True:return None
    return _ChatDocumentSearch(request,origin,owner,chat,root,credential,_SEAL)
