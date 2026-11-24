"""Internal host-body revision and delivery seam; no privacy authority is minted."""
import hashlib
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from sqlalchemy import text, inspect


class HostSourceFenceUnavailable(RuntimeError):
    """The exact source transition could not be fenced."""


@dataclass(frozen=True)
class ResetOriginPage:
    owner: str
    account_id: str
    operation_id: str
    after: tuple[str,str] | None
    next_after: tuple[str,str] | None
    items: tuple[tuple[str,str,str,str], ...]


@dataclass(frozen=True)
class ResetOriginCompletion:
    owner: str
    account_id: str
    operation_id: str


def _require_reset_origin_indexes(db):
    import re
    if db.get_bind().dialect.name!='sqlite' or not origin_available(db):
        raise ValueError('reset origin schema unavailable')
    definitions = {
        'host_source_reset_roots': "CREATE INDEX host_source_reset_roots ON host_source_delivery_intent(owner,chat_id,message_id) WHERE part_id='body' AND revision=1 AND origin_json IS NOT NULL",
        'host_source_reset_orphans': "CREATE INDEX host_source_reset_orphans ON host_source_delivery_intent(owner,chat_id,message_id) WHERE part_id='body' AND revision=1 AND origin_json IS NULL AND origin_state!='none'",
    }
    def shape(sql):
        return ''.join(part if i%2 else ''.join(part.split()).lower() for i,part in enumerate(re.split(r"('(?:''|[^'])*')",sql or '')))
    for name,expected in definitions.items():
        actual=db.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name=:name"),{'name':name}).scalar()
        if shape(actual)!=shape(expected): raise ValueError('reset origin index unavailable')


def _reset_ref_digest(ref):
    import hashlib, json
    from dataclasses import asdict
    raw=json.dumps([type(ref).__name__,asdict(ref)],sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>16384: raise ValueError('reset source reference unavailable')
    return hashlib.sha256(raw).hexdigest()


async def queued_reset_source_manifest(*, state, claim, lease, inventory):
    """Freeze the complete bounded original host/FM set before retirement."""
    import asyncio, hashlib, json
    from core.database import SessionLocal
    from src.openclank.session_map import OwnerSessionMap
    provider=canonical_source_provider(state); pool=lease.authority._supervisor
    if provider is not lease.provider or lease.claim is not claim or lease.inventory is not inventory:
        raise HostSourceFenceUnavailable('reset manifest authority unavailable')
    async def current():
        from src.openclank.mimo_supervisor import _queued_owned_thread
        await _queued_owned_thread(provider._assert_reset_retirement_ready,lease.reset_transport_witness)
        await pool.validate_queued_reset_lease(lease,claim=claim)
        if not pool.queued_reset_lease_current(lease,claim=claim):
            raise HostSourceFenceUnavailable('reset manifest authority changed')
    def roots():
        db=SessionLocal()
        try:
            _require_reset_origin_indexes(db)
            orphan=db.execute(text("SELECT 1 FROM host_source_delivery_intent INDEXED BY host_source_reset_orphans WHERE owner=:owner AND part_id='body' AND revision=1 AND origin_json IS NULL AND origin_state!='none' LIMIT 1"),{'owner':lease.account_id}).first()
            rows=db.execute(text("SELECT chat_id,message_id,content_hash FROM host_source_delivery_intent INDEXED BY host_source_reset_roots WHERE owner=:owner AND part_id='body' AND revision=1 AND origin_json IS NOT NULL ORDER BY chat_id,message_id LIMIT 129"),{'owner':lease.account_id}).all()
            if orphan or len(rows)>128: raise ValueError('reset host source scope unavailable')
            return [tuple(row) for row in rows]
        finally: db.close()
    async def drain_thread(fn,*args,**kwargs):
        task=asyncio.create_task(asyncio.to_thread(fn,*args,**kwargs))
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result(); raise
    await current(); original=await drain_thread(roots); await current()
    if len({(row[0],row[1]) for row in original})!=len(original):
        raise HostSourceFenceUnavailable('reset host source duplicate')
    mapping=OwnerSessionMap(pool._runtime_home(lease.owner)/'session-map.json',lease.owner)
    host=[]; all_refs=[]; verified_erased={}
    for chat,message,body_hash in original:
        if (any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (chat,message))
            or type(body_hash) is not str or len(body_hash)!=64 or any(c not in '0123456789abcdef' for c in body_hash)):
            raise HostSourceFenceUnavailable('reset host source identity unavailable')
        async with mapping.async_chat_admission_lock(chat,blocking=False,existing_only=True) as guard:
            if guard is None: raise HostSourceFenceUnavailable('reset source guard unavailable')
            def erased_current():
                try:
                    mapping.validate_chat_guard(chat,guard)
                    return canonical_source_provider(state) is provider and pool.queued_reset_lease_current(lease,claim=claim)
                except Exception:return False
            async def erased_join(projection):
                verified=await _verified_erased_pair(projection=projection,mapping=mapping,guard=guard,provider=provider,
                    account_id=lease.account_id,current=erased_current)
                key=_reset_ref_digest(verified)
                if key in verified_erased and verified_erased[key]!=verified: raise HostSourceFenceUnavailable('paired erased source duplicate')
                verified_erased[key]=verified
                return verified
            async def collect():
                out=[]
                async for refs,_ in _source_inventory_pages(provider._host_source_refs,account_id=lease.account_id,chat_id=chat,
                    message_id=message,target_authority=lease.authority,reset_claim=claim,reset_lease=lease,
                    _erased_join=erased_join,_erased_current=erased_current):
                    await current(); mapping.validate_chat_guard(chat,guard)
                    out.extend(_reset_ref_digest(ref) for ref in refs)
                    if len(out)+len(all_refs)>512: raise HostSourceFenceUnavailable('reset source scope too large')
                return out
            task=asyncio.create_task(collect())
            try: refs=await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: continue
                task.result(); raise
            await current();mapping.validate_chat_guard(chat,guard)
        if len(refs)!=len(set(refs)): raise HostSourceFenceUnavailable('reset source duplicate')
        all_refs.extend(refs);host.append({'chat':chat,'message':message,'bodySHA256':body_hash,'refs':sorted(refs)})
    # Account-wide enumeration proves no native source is omitted by the host roots.
    async def account_refs():
        out=[]
        def page(**kwargs):
            return provider._host_source_inventory(account_id=lease.account_id,chat_id=None,message_id=None,
                target_authority=lease.authority,reset_claim=claim,reset_lease=lease,**kwargs)
        async def known_erased(projection):
            from dataclasses import replace
            candidate=replace(projection,transition_class='erased')
            verified=verified_erased.get(_reset_ref_digest(candidate))
            if verified!=candidate: raise HostSourceFenceUnavailable('paired erased source omitted')
            return verified
        def inventory_current():
            try: return canonical_source_provider(state) is provider and pool.queued_reset_lease_current(lease,claim=claim)
            except Exception:return False
        async for refs,_ in _source_inventory_pages(page,_erased_join=known_erased,_erased_current=inventory_current):
            await current();out.extend(_reset_ref_digest(ref) for ref in refs)
            if len(out)>512: raise HostSourceFenceUnavailable('reset source scope too large')
        return out
    task=asyncio.create_task(account_refs())
    try: complete=await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
        task.result(); raise
    if len(complete)!=len(set(complete)) or sorted(complete)!=sorted(all_refs):
        raise HostSourceFenceUnavailable('reset source inventory mismatch')
    await current()
    if await drain_thread(roots)!=original: raise HostSourceFenceUnavailable('reset host sources changed')
    await current()
    from src.openclank.mimo_supervisor import _ZeroQueueSourceLease
    zero=type(lease) is _ZeroQueueSourceLease
    payload={'version':2 if zero else 1,'accountID':lease.account_id,'operationID':claim.operation_id,
        'queuedSHA256':hashlib.sha256(inventory.payload_json.encode()).hexdigest(),
        'previewSHA256':lease.preview_fingerprint,'roots':host}
    if zero: payload['kind']='zero_queue'
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>1024*1024: raise HostSourceFenceUnavailable('reset manifest too large')
    return payload


async def retire_reset_origin_page(*, state, claim, owner, account_id, operation_id, after=None, limit=128, reset_closure=None):
    """Retire one bounded account page under the existing live reset claim."""
    import asyncio
    import re
    from core.database import SessionLocal
    from services.memory.nuke_coordinator import MemoryResetClaim
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.session_map import OwnerSessionMap
    provider = canonical_source_provider(state)
    supervisor = getattr(state,'mimo_supervisor',None)
    auth = getattr(state,'auth_manager',None)
    if (type(claim) is not MemoryResetClaim or provider is None or type(supervisor) is not AcpSupervisorAdapter
        or auth is None or type(limit) is not int or not 1 <= limit <= 128
        or (after is not None and (type(after) is not tuple or len(after)!=2 or any(type(v) is not str or not v for v in after)))):
        raise HostSourceFenceUnavailable('reset source authority unavailable')
    def check_account():
        if auth.account_id(owner)!=account_id or auth.username_for_account_id(account_id)!=owner:
            raise HostSourceFenceUnavailable('reset account changed')
    def check():
        claim.assert_current(owner=owner,account_id=account_id,provider=provider,operation_id=operation_id)
        if reset_closure is not None:
            if (not provider._reset_retirement_ready_local() or provider._reset_retirement_witness is not reset_closure.lease.reset_transport_witness): raise HostSourceFenceUnavailable('queued reset live capability changed')
            supervisor.owner_lifecycle_pool().assert_queued_reset_closure(reset_closure,claim=claim)
        check_account()
    async def check_async():
        await asyncio.to_thread(claim.assert_current,owner=owner,account_id=account_id,provider=provider,operation_id=operation_id)
        if reset_closure is not None:
            from src.openclank.mimo_supervisor import _queued_owned_thread
            await _queued_owned_thread(provider._assert_reset_retirement_ready,reset_closure.lease.reset_transport_witness)
            pool=supervisor.owner_lifecycle_pool()
            if not pool.queued_reset_closure_current(reset_closure,claim=claim): raise HostSourceFenceUnavailable('stopped reset authority changed')
            await pool.validate_queued_reset_closure(reset_closure,claim=claim)
            if not pool.queued_reset_closure_current(reset_closure,claim=claim): raise HostSourceFenceUnavailable('stopped reset authority changed')
        check_account()
    await check_async()
    pool=supervisor.owner_lifecycle_pool(); authority=reset_closure.lease.authority if reset_closure is not None else TargetAuthority(auth_manager=auth,supervisor=pool)
    def resolve():
        return authority.resolve(account_id) if reset_closure is None else authority._resolve_stopped_reset(account_id,closure=reset_closure,claim=claim)
    target=resolve()
    mapping=OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json',owner)
    def read_page():
        db=SessionLocal()
        try:
            _require_reset_origin_indexes(db)
            base="SELECT chat_id,message_id,content_hash,origin_state FROM host_source_delivery_intent INDEXED BY host_source_reset_roots WHERE owner=:owner AND part_id='body' AND revision=1 AND origin_json IS NOT NULL"
            suffix=" AND (chat_id,message_id) > (:chat,:message)" if after else ''
            rows=db.execute(text(base+suffix+" ORDER BY chat_id,message_id LIMIT :cap"),
                {'owner':account_id,'chat':after[0] if after else None,'message':after[1] if after else None,'cap':limit+1}).all()
            return [tuple(row) for row in rows]
        finally: db.close()
    rows=await asyncio.to_thread(read_page); await check_async()
    manifest_roots=None
    if reset_closure is not None:
        from services.memory.skill_lifecycle import locked
        coordinator=claim._coordinator
        with locked(str(coordinator.lock_target)):
            record=coordinator._load_journal().get(operation_id)
            manifest=record.get('reset_source_manifest') if type(record) is dict else None
        if (type(manifest) is not dict or manifest.get('accountID')!=account_id
            or manifest.get('operationID')!=operation_id or type(manifest.get('roots')) is not list):
            raise HostSourceFenceUnavailable('reset source manifest unavailable')
        manifest_roots={(item['chat'],item['message']):item for item in manifest['roots'] if type(item) is dict
                        and set(item)=={'chat','message','bodySHA256','refs'}}
        if len(manifest_roots)!=len(manifest['roots']): raise HostSourceFenceUnavailable('reset source manifest unavailable')
    selected=rows[:limit]; items=[]
    for chat_id,message_id,content_hash,_ in selected:
        if (any(type(v) is not str or not v or v!=v.strip() or len(v)>512 for v in (chat_id,message_id))
            or type(content_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',content_hash) is None):
            raise HostSourceFenceUnavailable('reset origin identity unavailable')
        planned=manifest_roots.get((chat_id,message_id)) if manifest_roots is not None else None
        if manifest_roots is not None and (planned is None or planned['bodySHA256']!=content_hash or type(planned['refs']) is not list):
            raise HostSourceFenceUnavailable('reset origin is outside the original manifest')
        async with mapping.async_chat_admission_lock(chat_id) as guard:
            await check_async()
            if resolve()!=target:
                raise HostSourceFenceUnavailable('reset source target changed')
            await fence_projected_source(mapping=mapping,guard=guard,provider=provider,target_authority=authority,
                account_id=account_id,chat_id=chat_id,message_id=message_id,reset_claim=claim,reset_closure=reset_closure,
                reset_manifest_refs=(planned['refs'] if planned is not None else None))
            mapping.validate_chat_guard(chat_id,guard); await check_async()
            if resolve()!=target:
                raise HostSourceFenceUnavailable('reset source target changed')
            def commit_one():
                db=SessionLocal()
                try:
                    db.execute(text('BEGIN IMMEDIATE'))
                    row=db.execute(text("SELECT content_hash,origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_json IS NOT NULL"),{'owner':account_id,'chat':chat_id,'message':message_id}).first()
                    if not row or row[0]!=content_hash:
                        raise ValueError('reset origin changed')
                    previous=row[1]
                    if previous in ('pending','delivered','projection_user_dropped','projection_assistant_dropped','projection_both_dropped'):
                        session_owner=db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat_id}).scalar()
                        if not session_owner or auth.account_id(session_owner)!=account_id:
                            raise ValueError('reset origin owner changed')
                        changed=db.execute(text("UPDATE host_source_delivery_intent SET origin_state='reset_retired' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_state=:previous AND content_hash=:hash"),{'owner':account_id,'chat':chat_id,'message':message_id,'previous':previous,'hash':content_hash})
                        if changed.rowcount!=1: raise ValueError('reset origin changed')
                        previous='reset_retired'
                    elif previous not in ('reset_retired','vetoed','deleted','completion_withdrawn','superseded'):
                        raise ValueError('reset origin state unavailable')
                    check(); db.commit(); return previous
                except BaseException:
                    db.rollback(); raise
                finally: db.close()
            task=asyncio.create_task(asyncio.to_thread(commit_one))
            try:
                final_state=await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try: await asyncio.shield(task)
                    except asyncio.CancelledError: continue
                task.result()
                raise
            mapping.validate_chat_guard(chat_id,guard); await check_async()
            if resolve()!=target:
                raise HostSourceFenceUnavailable('reset source target changed')
            items.append((chat_id,message_id,content_hash,final_state))
    next_after=(selected[-1][0],selected[-1][1]) if len(rows)>limit else None
    return ResetOriginPage(owner,account_id,operation_id,after,next_after,tuple(items))


async def verify_reset_origins_retired(*, state, claim, owner, account_id, operation_id, reset_closure=None):
    """Prove the frozen account has no eligible or malformed unretired root."""
    import asyncio
    from core.database import SessionLocal
    from services.memory.nuke_coordinator import MemoryResetClaim
    provider=canonical_source_provider(state); auth=getattr(state,'auth_manager',None)
    if type(claim) is not MemoryResetClaim or provider is None or auth is None:
        raise HostSourceFenceUnavailable('reset origin completion unavailable')
    def check():
        claim.assert_current(owner=owner,account_id=account_id,provider=provider,operation_id=operation_id)
        if reset_closure is not None: state.mimo_supervisor.owner_lifecycle_pool().assert_queued_reset_closure(reset_closure,claim=claim)
        if auth.account_id(owner)!=account_id or auth.username_for_account_id(account_id)!=owner:
            raise ValueError('reset account changed')
    def prove():
        check(); db=SessionLocal()
        try:
            _require_reset_origin_indexes(db)
            orphan=db.execute(text("SELECT EXISTS(SELECT 1 FROM host_source_delivery_intent INDEXED BY host_source_reset_orphans WHERE owner=:owner AND part_id='body' AND revision=1 AND origin_json IS NULL AND origin_state!='none')"),{'owner':account_id}).scalar()
            bad=db.execute(text("""SELECT EXISTS(SELECT 1 FROM host_source_delivery_intent INDEXED BY host_source_reset_roots
              WHERE owner=:owner AND part_id='body' AND revision=1 AND origin_json IS NOT NULL AND (
                origin_state NOT IN ('reset_retired','vetoed','deleted','completion_withdrawn','superseded') OR
                length(content_hash)!=64 OR content_hash GLOB '*[^0-9a-f]*' OR
                CASE WHEN length(origin_json)>16384 OR NOT json_valid(origin_json) THEN 1
                  WHEN json_type(origin_json,'$.choices_json') IS NOT 'text' OR NOT json_valid(json_extract(origin_json,'$.choices_json')) THEN 1
                  ELSE (json_type(origin_json) IS NOT 'object' OR
                    (SELECT count(*) FROM json_each(origin_json))!=6 OR (SELECT count(DISTINCT key) FROM json_each(origin_json))!=6 OR
                    json_extract(origin_json,'$.owner') IS NOT owner OR json_extract(origin_json,'$.chat_id') IS NOT chat_id OR
                    json_extract(origin_json,'$.root_operation_id') IS NOT message_id OR
                    json_type(origin_json,'$.policy_fingerprint') IS NOT 'text' OR length(json_extract(origin_json,'$.policy_fingerprint'))!=64 OR
                    json_extract(origin_json,'$.policy_fingerprint') GLOB '*[^0-9a-f]*' OR
                    COALESCE(json_type(origin_json,'$.capture_eligible'),'missing') NOT IN ('true','false') OR
                    json_type(json_extract(origin_json,'$.choices_json')) IS NOT 'object' OR
                    (SELECT count(*) FROM json_each(json_extract(origin_json,'$.choices_json')) WHERE key IN ('incognito','noMemory','compareMode'))!=3 OR
                    COALESCE(json_type(json_extract(origin_json,'$.choices_json'),'$.incognito'),'missing') NOT IN ('true','false') OR
                    COALESCE(json_type(json_extract(origin_json,'$.choices_json'),'$.noMemory'),'missing') NOT IN ('true','false') OR
                    COALESCE(json_type(json_extract(origin_json,'$.choices_json'),'$.compareMode'),'missing') NOT IN ('true','false')) END))"""),{'owner':account_id}).scalar()
            if orphan or bad: raise ValueError('reset origins remain unavailable')
            check()
        finally: db.close()
    try:
        await asyncio.to_thread(prove)
    except Exception as exc:
        raise HostSourceFenceUnavailable('reset origin completion unavailable') from exc
    return ResetOriginCompletion(owner,account_id,operation_id)


def canonical_source_provider(state):
    """Use the retained S05 store binding, independent of recall selection."""
    from src.memery_provider import MemeryProvider
    from src.openclank.memory_job_admission import SingleHostProfile
    profile = getattr(state, 's05_profile', None)
    if type(profile) is SingleHostProfile and isinstance(profile.provider, MemeryProvider) and profile.provider._guarded_v17:
        try:
            profile.assert_current()
        except Exception as exc:
            raise HostSourceFenceUnavailable('canonical memory source binding unavailable') from exc
        return profile.provider
    current = getattr(state, 'canonical_memory_provider', None) or getattr(state, 'memory_provider', None)
    if isinstance(current, MemeryProvider) and current._guarded_v17:
        return current
    return None


_PUBLICATION_CAPABILITY_SEAL = object()


@dataclass(frozen=True)
class CompletedTurnLifecycle:
    """Host-issued exact source/target binding; each use requires its live guard."""
    mapping: object
    provider: object
    profile: object
    target_authority: object
    account_lifecycle: object
    binding: object
    source: object
    _seal: object

    def assert_current(self, *, source, provider, profile, target_authority, guard):
        from src.memery_external_sources import SourceRef
        from src.openclank.memory_job_admission import TargetAuthority, SingleHostProfile
        from src.openclank.account_lifecycle import AccountOwnerLifecycle
        if (self._seal is not _PUBLICATION_CAPABILITY_SEAL or type(source) is not SourceRef
            or source != self.source or provider is not self.provider
            or type(profile) is not SingleHostProfile or profile is not self.profile or profile.provider is not provider
            or type(self.account_lifecycle) is not AccountOwnerLifecycle
            or type(target_authority) is not TargetAuthority or target_authority is not self.target_authority):
            raise HostSourceFenceUnavailable('host source publication capability unavailable')
        self.mapping.validate_chat_guard(source.chat_id, guard)
        profile.assert_current()
        if target_authority.resolve(source.account_id) != self.binding:
            raise HostSourceFenceUnavailable('host source lifecycle changed')
        owner = target_authority._auth.username_for_account_id(source.account_id)
        if not owner or self.account_lifecycle.owner_has_active_operation(owner):
            raise HostSourceFenceUnavailable('account lifecycle is active')


@dataclass(frozen=True)
class CompletedTurnLifecycleOwner:
    """Bound owner authority; queued SourceRefs remain the work selection."""
    mapping: object
    source_authority: object
    owner: str
    account_id: str
    provider: object
    profile: object
    target_authority: object
    account_lifecycle: object
    binding: object
    _seal: object

    def _assert_owner_source(self, *, source, guard):
        from src.memery_external_sources import SourceRef, CurrentHostUserSource
        from src.openclank.memory_job_admission import TargetAuthority, SingleHostProfile
        from src.openclank.account_lifecycle import AccountOwnerLifecycle
        if (self._seal is not _PUBLICATION_CAPABILITY_SEAL or type(source) is not SourceRef
            or source.account_id != self.account_id or type(self.source_authority) is not CurrentHostUserSource
            or self.source_authority._owner != self.owner or type(self.target_authority) is not TargetAuthority
            or type(self.profile) is not SingleHostProfile or self.profile.provider is not self.provider
            or type(self.account_lifecycle) is not AccountOwnerLifecycle or self.mapping.owner != self.owner):
            raise HostSourceFenceUnavailable('owner source lifecycle unavailable')
        self.mapping.validate_chat_guard(source.chat_id, guard)
        self.profile.assert_current()
        if (self.target_authority.resolve(self.account_id) != self.binding
            or self.account_lifecycle.owner_has_active_operation(self.owner)):
            raise HostSourceFenceUnavailable('owner source authority changed')

    def for_source(self, *, source, guard):
        self._assert_owner_source(source=source, guard=guard)
        if self.source_authority.hydrate(source).get('state') != 'available':
            raise HostSourceFenceUnavailable('owner source authority changed')
        return CompletedTurnLifecycle(mapping=self.mapping, provider=self.provider, profile=self.profile,
            target_authority=self.target_authority, account_lifecycle=self.account_lifecycle,
            binding=self.binding, source=source, _seal=_PUBLICATION_CAPABILITY_SEAL)

    def retirement(self, *, source, guard):
        self._assert_owner_source(source=source, guard=guard)
        return self.source_authority.retirement(source)


def issue_completed_turn_lifecycle_owner(*, state, owner, provider, profile, target_authority, source_authority):
    """Bind a qualified cooperating profile to app-owned source authorities.

    This grants no dispatch: each source use rechecks the live chat guard,
    account transition, source, target, policy and profile.
    """
    from core.database import SessionLocal
    from src.memery_external_sources import CurrentHostUserSource
    from src.memery_provider import MemeryProvider
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.account_lifecycle import AccountOwnerLifecycle
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_job_admission import TargetAuthority, SingleHostProfile, ForegroundRuntime, _conn
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap

    supervisor = getattr(state, 'mimo_supervisor', None)
    lifecycle = getattr(state, 'account_owner_lifecycle', None)
    auth = getattr(state, 'auth_manager', None)
    manager = getattr(state, 'session_manager', None)
    if (type(profile) is not SingleHostProfile or getattr(state, 's05_profile', None) is not profile
        or type(provider) is not MemeryProvider or profile.provider is not provider
        or not provider._guarded_v17 or canonical_source_provider(state) is not provider
        or type(supervisor) is not AcpSupervisorAdapter or supervisor._s05_profile is not profile
        or type(supervisor._s05_foreground_runtime) is not ForegroundRuntime
        or supervisor._s05_foreground_runtime.profile is not profile
        or type(lifecycle) is not AccountOwnerLifecycle or getattr(manager, 'lifecycle_context', None) is not state
        or type(target_authority) is not TargetAuthority or target_authority._auth is not auth
        or type(source_authority) is not CurrentHostUserSource or source_authority._owner != owner
        or source_authority._auth is not auth or source_authority._session_factory is not SessionLocal
        or source_authority._archive is not get_conversation_archive()
        or source_authority._workspaces is not getattr(state, 'files_policy_repository', None)
        or source_authority._workspaces is None or source_authority._policy is not STORE):
        raise HostSourceFenceUnavailable('host source publication lifecycle unavailable')
    try:
        profile.assert_current()
        pool = supervisor.owner_lifecycle_pool()
        if target_authority._supervisor is not pool:
            raise ValueError('supervisor changed')
        account = auth.account_id(owner)
        if not account or auth.username_for_account_id(account) != owner or lifecycle.owner_has_active_operation(owner):
            raise ValueError('owner authority unavailable')
        binding = target_authority.resolve(account)
        if source_authority._archive.resolve_owner(owner) != account:
            raise ValueError('archive owner changed')
        with _conn(profile.path) as db:
            occupancy = db.execute('SELECT verified,coverage_generation,clock_generation FROM fm_v2_s05_occupancy WHERE singleton=1').fetchone()
        if not occupancy or tuple(occupancy) != (1, profile.coverage_generation, profile.clock_generation):
            raise ValueError('foreground coverage unavailable')
        mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
        profile.assert_current()
        if target_authority.resolve(account) != binding or lifecycle.owner_has_active_operation(owner):
            raise ValueError('owner authority changed')
    except Exception as exc:
        raise HostSourceFenceUnavailable('host source publication lifecycle unavailable') from exc
    return CompletedTurnLifecycleOwner(mapping=mapping, source_authority=source_authority, owner=owner,
        account_id=account, provider=provider, profile=profile, target_authority=target_authority,
        account_lifecycle=lifecycle, binding=binding, _seal=_PUBLICATION_CAPABILITY_SEAL)


def _account_source_phase_hash(phase):
    keys={'database_id','account_id','operation_id','step','generation','action','owner','new_owner'}
    if (type(phase) is not dict or set(phase)!=keys or type(phase['generation']) is not int or phase['generation']<1
        or phase['action'] not in ('forward','reverse') or phase['owner']==phase['new_owner']
        or any(type(phase[k]) is not str or not phase[k] or phase[k]!=phase[k].strip() or len(phase[k])>512
               for k in keys-{'generation','action'})):
        raise HostSourceFenceUnavailable('account source phase unavailable')
    return hashlib.sha256(json.dumps(phase,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def account_source_summary_step(phase=None):
    return 'host_sources_fenced' if phase is None else 'host_sources_fenced_'+_account_source_phase_hash(phase)


def _account_source_page_step(after, phase=None):
    suffix=hashlib.sha256(('initial' if after is None else 'after:' + after).encode()).hexdigest()
    return 'host_source_page_'+suffix if phase is None else 'host_source_page_'+_account_source_phase_hash(phase)+'_'+suffix


def _account_source_saved_phase(operation):
    manifest=operation.get('manifest') or {};kind=operation.get('kind')
    step='memory_fm' if kind=='rename' else 'memory_fm_tombstoned'
    names=[key for key in manifest if type(key) is str and key.startswith('memory_fm_source_phase_')]
    if not names: return None
    parsed=[]
    for name in names:
        suffix=name.removeprefix('memory_fm_source_phase_')
        reverse=suffix.endswith('_reverse')
        number=suffix.removesuffix('_reverse') if reverse else suffix
        if not number.isdigit() or int(number)<1: raise HostSourceFenceUnavailable('account source phase unavailable')
        parsed.append((int(number),reverse,name))
    if len(parsed)>100000: raise HostSourceFenceUnavailable('account source phase unavailable')
    generation,reverse,name=max(parsed);phase=manifest.get(name)
    saved=(operation.get('steps') or {}).get(name) or {}
    old=operation.get('source_owner'); target=operation.get('target_owner') if kind=='rename' else operation.get('tombstone_owner')
    direction=(target,old) if reverse else (old,target)
    if (type(phase) is not dict or saved.get('state')!='applied' or saved.get('receipt')!=phase or phase.get('step')!=step
        or phase.get('generation')!=generation or phase.get('action')!=('reverse' if reverse else 'forward')
        or phase.get('operation_id')!=operation.get('operation_id') or phase.get('account_id')!=operation.get('subject_id')
        or (phase.get('owner'),phase.get('new_owner'))!=direction):
        raise HostSourceFenceUnavailable('account source phase changed')
    _account_source_phase_hash(phase)
    return phase


def _phase_pair_page_item(ref, phase, receipt):
    """Encode only a Native-proved materialized pair under its claimed phase."""
    from src.memery_external_sources import _assistant_ref_error
    from src.memery_provider import _HostPairedSourceProjection
    if type(ref) is not _HostPairedSourceProjection or _assistant_ref_error(ref.source):
        raise HostSourceFenceUnavailable('paired source identity unavailable')
    if (type(receipt) is not dict or set(receipt)!={'version','state','root_fence_id','assistant_fence_id'}
        or type(receipt['version']) is not int or receipt['version']!=1 or receipt['state']!='fenced'):
        raise HostSourceFenceUnavailable('paired source receipt unavailable')
    root=ref.source.root
    coverage={'version':1,'kind':'assistant_interpretation_review',
        'assistant':{'message_id':ref.source.assistant_message_id,'part_id':ref.source.assistant_part_id,
                     'revision':ref.source.assistant_revision,'content_hash':ref.source.assistant_content_hash},
        'pair_sha256':ref.source.pair_sha256}
    item={'kind':'paired','job_id':ref.job_id,'source':asdict(root),'coverage':coverage,
          'policy_fingerprint':ref.policy_fingerprint,'root_fence_id':receipt.get('root_fence_id'),
          'assistant_fence_id':receipt.get('assistant_fence_id')}
    _validate_phase_pair_item(item,phase=phase,account_id=root.account_id)
    return item


def _validate_phase_pair_item(item, *, phase, account_id):
    """Check the closed page codec and both domain-separated Native IDs."""
    from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _assistant_ref_error
    fields={'kind','job_id','source','coverage','policy_fingerprint','root_fence_id','assistant_fence_id'}
    if phase is None or type(item) is not dict or set(item)!=fields or item['kind']!='paired':
        raise HostSourceFenceUnavailable('paired source receipt unavailable')
    _account_source_phase_hash(phase)
    c=item['coverage']
    try:
        if type(c) is not dict or set(c)!={'version','kind','assistant','pair_sha256'} or type(c['version']) is not int or c['version']!=1 or c['kind']!='assistant_interpretation_review': raise ValueError()
        a=c['assistant']
        if type(a) is not dict or set(a)!={'message_id','part_id','revision','content_hash'}: raise ValueError()
        root=SourceRef(**item['source'])
        ref=AssistantInterpretationRef(1,root,a['message_id'],a['part_id'],a['revision'],a['content_hash'],c['pair_sha256'])
        if _assistant_ref_error(ref) or root.account_id!=account_id or phase['account_id']!=account_id: raise ValueError()
        if type(item['job_id']) is not str or not item['job_id'] or item['job_id']!=item['job_id'].strip(): raise ValueError()
        policy=item['policy_fingerprint']
        if type(policy) is not str or len(policy)!=64 or any(ch not in '0123456789abcdef' for ch in policy): raise ValueError()
        def digest(value):
            return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        root_id='hostpart_'+digest([root.version,root.account_id,root.chat_id,root.actor_id,root.message_id,root.part_id])
        assistant_id='hostassistant_'+digest(['openclank.assistant-source.v1',root.account_id,root.chat_id,a['message_id'],a['part_id']])
        ordered={k:phase[k] for k in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
        expected_root='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',ordered,'root',root_id,None])
        expected_assistant='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',ordered,'assistant',assistant_id,item['job_id']])
        if item['root_fence_id']!=expected_root or item['assistant_fence_id']!=expected_assistant: raise ValueError()
        return ref
    except (KeyError,TypeError,ValueError,OverflowError):
        raise HostSourceFenceUnavailable('paired source receipt changed') from None


def _phase_pending_pair_item(ref, phase, receipt):
    from src.memery_provider import _HostPairedSourceProjection
    if (type(ref) is not _HostPairedSourceProjection or ref.transition_class not in ('pending','terminal','erased')
        or type(receipt) is not dict or set(receipt)!={'version','state','fence_id','root','assistant'}
        or type(receipt['version']) is not int or receipt['version']!=1 or receipt['state']!='fenced'):
        raise HostSourceFenceUnavailable('pending pair receipt unavailable')
    item={'kind':{'pending':'pending_pair','terminal':'terminal_pair','erased':'erased_pair'}[ref.transition_class],'job_id':ref.job_id,'source':asdict(ref.source.root),
        'coverage':_account_pair_coverage(ref.source),'policy_fingerprint':ref.policy_fingerprint,
        'input_sha256':ref.input_sha256,'fence_id':receipt['fence_id'],
        'root':receipt['root'],'assistant':receipt['assistant']}
    _validate_phase_pending_pair_item(item,phase=phase,account_id=ref.source.root.account_id)
    return item


def _validate_phase_pending_pair_item(item, *, phase, account_id):
    from src.memery_external_sources import SourceRef, AssistantInterpretationRef, _assistant_ref_error
    try:
        if (phase is None or type(item) is not dict or set(item)!={'kind','job_id','source','coverage','policy_fingerprint','input_sha256','fence_id','root','assistant'}
            or item['kind'] not in ('pending_pair','terminal_pair','erased_pair')): raise ValueError()
        _account_source_phase_hash(phase); c=item['coverage']; a=c['assistant']
        if (type(c) is not dict or set(c)!={'version','kind','assistant','pair_sha256'} or type(c['version']) is not int
            or c['version']!=1 or c['kind']!='assistant_interpretation_review' or type(a) is not dict
            or set(a)!={'message_id','part_id','revision','content_hash'}): raise ValueError()
        root=SourceRef(**item['source']); ref=AssistantInterpretationRef(1,root,a['message_id'],a['part_id'],a['revision'],a['content_hash'],c['pair_sha256'])
        sha=lambda x:type(x) is str and len(x)==64 and all(ch in '0123456789abcdef' for ch in x)
        if (_assistant_ref_error(ref) or root.account_id!=account_id or phase['account_id']!=account_id
            or type(item['job_id']) is not str or not item['job_id'] or item['job_id']!=item['job_id'].strip()
            or not sha(item['policy_fingerprint']) or not sha(item['input_sha256'])): raise ValueError()
        ordered={k:phase[k] for k in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
        raw=json.dumps(['s04.paired-pending-fence.v1',ordered,item['job_id']],ensure_ascii=False,separators=(',',':')).encode()
        if item['fence_id']!='cs_s04pairpending_'+hashlib.sha256(raw).hexdigest(): raise ValueError()
        def digest(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        root_id='hostpart_'+digest([root.version,root.account_id,root.chat_id,root.actor_id,root.message_id,root.part_id])
        assistant_id='hostassistant_'+digest(['openclank.assistant-source.v1',root.account_id,root.chat_id,a['message_id'],a['part_id']])
        for label,source_id,revision,content_hash in (('root',root_id,root.revision,root.content_hash),('assistant',assistant_id,a['revision'],a['content_hash'])):
            fact=item[label]
            if (type(fact) is not dict or set(fact)!={'source_id','revision','content_hash','prior_state','fence_id'}
                or fact['source_id']!=source_id or type(fact['revision']) is not int or fact['revision']!=revision
                or fact['content_hash']!=content_hash or fact['prior_state'] not in ('active','absent')): raise ValueError()
            expected='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',ordered,label,source_id,item['job_id'] if label=='assistant' else None])
            if label=='root' and fact['prior_state']=='absent':
                expected='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',ordered,'root_absent',source_id,None])
            if fact['fence_id']!=(expected if fact['prior_state']=='active' or label=='root' else None): raise ValueError()
        return ref
    except (KeyError,TypeError,ValueError,OverflowError):
        raise HostSourceFenceUnavailable('pending pair receipt changed') from None


def _phase_root_page_item(ref, phase, receipt):
    from src.memery_provider import _HostSourceProjection
    from src.memery_external_sources import _source_ref_error
    if (type(ref) is not _HostSourceProjection or _source_ref_error(ref.source) or type(receipt) is not dict
        or set(receipt)!={'version','state','fence_id'} or type(receipt['version']) is not int
        or receipt['version']!=1 or receipt['state'] not in ('fenced','absent')):
        raise HostSourceFenceUnavailable('phase root receipt unavailable')
    item={'kind':'phase_root','job_id':ref.job_id,'source':asdict(ref.source),
          'policy_fingerprint':ref.policy_fingerprint,'state':receipt['state'],'fence_id':receipt['fence_id']}
    _validate_phase_root_item(item,phase=phase,account_id=ref.source.account_id)
    return item


def _validate_phase_root_item(item, *, phase, account_id):
    from src.memery_external_sources import SourceRef, _source_ref_error
    fields={'kind','job_id','source','policy_fingerprint','state','fence_id'}
    try:
        if phase is None or type(item) is not dict or set(item)!=fields or item['kind']!='phase_root': raise ValueError()
        _account_source_phase_hash(phase)
        source=SourceRef(**item['source'])
        if _source_ref_error(source) or source.account_id!=account_id or phase['account_id']!=account_id: raise ValueError()
        if type(item['job_id']) is not str or not item['job_id'] or item['job_id']!=item['job_id'].strip(): raise ValueError()
        policy=item['policy_fingerprint']
        if type(policy) is not str or len(policy)!=64 or any(ch not in '0123456789abcdef' for ch in policy): raise ValueError()
        if item['state'] not in ('fenced','absent'): raise ValueError()
        def digest(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        source_id='hostpart_'+digest([source.version,source.account_id,source.chat_id,source.actor_id,source.message_id,source.part_id])
        ordered={k:phase[k] for k in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
        variant='root' if item['state']=='fenced' else 'root_absent'
        if item['fence_id']!='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',ordered,variant,source_id,None]): raise ValueError()
        return source
    except (KeyError,TypeError,ValueError,OverflowError):
        raise HostSourceFenceUnavailable('phase root receipt changed') from None


def _phase_observation_page_item(ref, phase, receipt, *, deleted_chat=False):
    from src.memery_provider import _HostObservationProjection
    if (type(ref) is not _HostObservationProjection or type(receipt) is not dict
        or set(receipt)!={'version','state','fence_id'} or type(receipt['version']) is not int
        or receipt['version']!=1 or receipt['state']!='fenced'):
        raise HostSourceFenceUnavailable('phase observation receipt unavailable')
    authority={'origin_id':ref.origin_id,'origin_hash':ref.origin_hash,'captured_owner':ref.captured_owner,
        'current_fingerprint':ref.current_fingerprint,'rebind_id':ref.rebind_id,'rebind_hash':ref.rebind_hash}
    item={'kind':'phase_observation','source':asdict(ref.source),'authority':authority,'fence_id':receipt['fence_id']}
    if deleted_chat:
        item['deleted_chat'] = True
    _validate_phase_observation_item(item,phase=phase,account_id=ref.source.account_id,workspace_id=ref.workspace_id)
    return item


def _validate_phase_observation_item(item, *, phase, account_id, workspace_id):
    from src.memery_external_sources import SourceRef, _source_ref_error
    try:
        deleted = type(item) is dict and item.get('deleted_chat') is True
        fields = {'kind','source','authority','fence_id'} | ({'deleted_chat'} if deleted else set())
        if (phase is None or type(item) is not dict or set(item)!=fields
            or (deleted and phase['step'] != 'memory_fm_tombstoned')
            or item['kind']!='phase_observation'): raise ValueError()
        _account_source_phase_hash(phase); source=SourceRef(**item['source']); a=item['authority']
        if _source_ref_error(source) or source.account_id!=account_id or phase['account_id']!=account_id: raise ValueError()
        if type(a) is not dict or set(a)!={'origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash'}: raise ValueError()
        sha=lambda x:type(x) is str and len(x)==64 and all(ch in '0123456789abcdef' for ch in x)
        if (type(a['captured_owner']) is not str or not a['captured_owner'] or a['captured_owner']!=a['captured_owner'].strip()
            or not sha(a['origin_hash']) or not sha(a['current_fingerprint'])
            or (a['rebind_id'] is None)!=(a['rebind_hash'] is None)
            or (a['rebind_id'] is not None and (type(a['rebind_id']) is not str or not a['rebind_id'].startswith('cs_s06renew_') or not sha(a['rebind_id'][12:]) or not sha(a['rebind_hash'])))): raise ValueError()
        def digest(value): return hashlib.sha256(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if a['origin_id']!='cs_s06origin_'+digest([a['captured_owner'],workspace_id,asdict(source)]): raise ValueError()
        source_id='hostpart_'+digest([source.version,source.account_id,source.chat_id,source.actor_id,source.message_id,source.part_id])
        ordered={k:phase[k] for k in ('database_id','account_id','operation_id','step','generation','action','owner','new_owner')}
        proof_kind = 'root_deleted_chat' if deleted else 'root'
        if item['fence_id']!='cs_s04pair_'+digest(['s04.paired-rename-fence.v1',ordered,proof_kind,source_id,None]): raise ValueError()
        return source
    except (KeyError,TypeError,ValueError,OverflowError):
        raise HostSourceFenceUnavailable('phase observation receipt changed') from None


def _account_source_renewal(kind, sources, phase=None):
    if kind != 'rename' and not (phase is not None and phase.get('action')=='reverse'):
        return 'not_applicable'
    renewables = [item for item in sources if item.get('deleted_chat') is not True]
    return 'pending' if any(item.get('kind') in ('paired','pending_pair','terminal_pair','erased_pair','phase_observation') or (item.get('kind')=='phase_root' and item['state']=='fenced') or (item.get('kind') not in ('paired','pending_pair','terminal_pair','erased_pair','phase_root','phase_observation') and item['rename_proof']['active_versions']) for item in renewables) else 'not_required'


def _account_pair_read_allowed(source_authority, owner):
    try:
        privileges=source_authority._auth.get_privileges(owner)
        allowed=privileges.get('can_manage_memory',True) if type(privileges) is dict else False
        if type(allowed) is not bool or not allowed: return False
        policy=source_authority._policy.effective_snapshot(owner,read_privilege=allowed,write_privilege=allowed,
            incognito=False,no_memory=False,compare_mode=False)
        return policy.get('eligibility_state')=='available' and policy.get('read_allowed') is True
    except Exception:
        return False


async def _account_pair_verify(ref, *, source_authority, mapping, guard, current, provider_owner):
    """Re-read both retained parts under the actual chat and account guard."""
    from core.database import SessionLocal
    from src.memery_external_sources import _queued_owned_thread, _typed_assistant_component_matches
    from src.openclank.conversation_archive import get_conversation_archive
    def verify():
        if current() is not True or _account_pair_read_allowed(source_authority,provider_owner) is not True:
            raise HostSourceFenceUnavailable('paired source authority changed')
        mapping.validate_chat_guard(ref.root.chat_id,guard)
        with SessionLocal() as db:
            if source_authority._authority(db,ref.root,manual_review=True) is not True:
                raise HostSourceFenceUnavailable('paired source authority unavailable')
        # This worker must take preference authority before the Host writer;
        # the account-current callback may recheck preferences inside it.
        from routes.prefs_routes import PREFERENCE_LOCK
        if not PREFERENCE_LOCK.try_enter():
            raise HostSourceFenceUnavailable('paired source authority unavailable')
        try:
            with SessionLocal() as db:
                component=assistant_component(db,get_conversation_archive(),owner=ref.root.account_id,
                    provider_owner=provider_owner,chat_id=ref.root.chat_id,message_id=ref.root.message_id,
                    current_authority=current)
        finally:
            PREFERENCE_LOCK.__exit__(None, None, None)
        if not _typed_assistant_component_matches(ref,component) or current() is not True:
            raise HostSourceFenceUnavailable('paired source changed')
        return True
    return await _queued_owned_thread(verify)


def _account_pair_coverage(ref):
    return {'version':1,'kind':'assistant_interpretation_review',
            'assistant':{'message_id':ref.assistant_message_id,'part_id':ref.assistant_part_id,
                         'revision':ref.assistant_revision,'content_hash':ref.assistant_content_hash},
            'pair_sha256':ref.pair_sha256}


def _deleted_observation_chat(archive, session_factory, *, account_id, chat):
    """Prove the exact archive tombstone, zero retained rows, and absent core chat."""
    if not archive.chat_erasure_started(owner=account_id, chat_id=chat):
        return False
    inventory = archive.chat_inventory(owner=account_id, chat_id=chat)
    with session_factory() as db:
        core = db.execute(text("SELECT 1 FROM sessions WHERE id=:chat"), {"chat": chat}).first()
    if (inventory.get("owner_account_id") != account_id or inventory.get("erasure_started") is not True
        or type(inventory.get("count")) is not int or inventory["count"] != 0 or core is not None):
        raise HostSourceFenceUnavailable("chat erasure is incomplete")
    return True


async def fence_account_host_sources(*, state, lifecycle, operation_id, claim_token, owner, account_id, kind, phase=None):
    """Fence exact old-owner sources under an already-claimed account operation."""
    import asyncio
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.account_lifecycle import AccountOwnerLifecycle
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.session_map import OwnerSessionMap
    from src.memery_provider import _HostPairedSourceProjection, _HostSourceProjection, _HostObservationProjection
    from src.memery_external_sources import CurrentHostUserSource
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from core.database import SessionLocal
    provider = canonical_source_provider(state)
    if provider is None:
        # No host-source publication is active while cross-provider retirement
        # remains unjoined; this is not proof that an old store was retired.
        return {'state': 'inactive', 'reason': 'publication_lifecycle_unavailable'}
    supervisor, auth = getattr(state, 'mimo_supervisor', None), getattr(state, 'auth_manager', None)
    if (type(lifecycle) is not AccountOwnerLifecycle or not isinstance(supervisor, AcpSupervisorAdapter)
        or auth is None or not operation_id or not claim_token or kind not in ('rename', 'delete')):
        raise HostSourceFenceUnavailable('account source transition unavailable')
    pool = supervisor.owner_lifecycle_pool()
    authority = TargetAuthority(auth_manager=auth, supervisor=pool)
    phase_capability=None
    def validate_operation():
        operation = lifecycle.get_operation(operation_id)
        claim = (operation.get('receipt') or {}).get('claim') or {}
        if (operation.get('kind') != kind or operation.get('source_owner') != owner
            or operation.get('subject_id') != account_id or claim.get('token') != claim_token
            or not lifecycle.owner_has_active_operation(owner)
            or auth.account_id(owner) != account_id or auth.username_for_account_id(account_id) != owner):
            raise HostSourceFenceUnavailable('account source transition changed')
        if phase is not None and _account_source_saved_phase(operation)!=phase:
            raise HostSourceFenceUnavailable('account source phase changed')
        if phase_capability is not None:
            phase_capability.assert_prefence_current(provider=provider,phase=phase,account_id=account_id,sql_owner=phase['owner'])
    validate_operation()
    binding = authority.resolve(account_id)
    phase_capability=(issue_account_source_prefence_transition(state=state,lifecycle=lifecycle,operation_id=operation_id,
        claim_token=claim_token,provider=provider,phase=phase,host_owner=owner) if phase is not None else None)
    try:
        mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
        source_authority=CurrentHostUserSource(owner=owner,auth_manager=auth,session_factory=SessionLocal,
            archive=get_conversation_archive(),workspace_repository=getattr(state,'files_policy_repository',None),policy_store=STORE,
            guarded_preparation=provider._guarded_preparation)
        after, page_count, source_count, renewal = None, 0, 0, ('not_required' if kind == 'rename' or (phase is not None and phase['action']=='reverse') else 'not_applicable')
        while True:
            step = _account_source_page_step(after,phase)
            operation = lifecycle.get_operation(operation_id)
            saved = (operation.get('steps') or {}).get(step) or {}
            if saved.get('state') == 'applied':
                page = saved.get('receipt')
            else:
                lifecycle.start_operation_step(operation_id, step, claim_token=claim_token)
                from src.memery_external_sources import _queued_owned_thread
                from src.memery_provider import _HOST_ERASED_INVENTORY, _HostPairedErasedCandidate
                inventory_args={'account_id':account_id,'target_authority':authority,'after_job_id':after,'limit':128}
                if phase is not None:
                    inventory_args.update(claimed_phase=phase,phase_capability=phase_capability,_erased_token=_HOST_ERASED_INVENTORY)
                result = await _queued_owned_thread(provider._host_account_source_refs, **inventory_args)
                if type(result) is not tuple or len(result) != 2 or type(result[0]) is not tuple or len(result[0]) > 128:
                    raise ValueError('account source inventory unavailable')
                refs, next_after = result
                promoted={}
                for raw in refs:
                    if type(raw) is not _HostPairedErasedCandidate: continue
                    ref=raw.projection;chat=ref.source.root.chat_id
                    async with mapping.async_chat_admission_lock(chat) as guard:
                        def current():
                            try:
                                validate_operation();mapping.validate_chat_guard(chat,guard)
                                return (authority.resolve(account_id)==binding and canonical_source_provider(state) is provider
                                    and _account_pair_read_allowed(source_authority,owner) is True)
                            except Exception:return False
                        promoted[ref.job_id]=await _verified_erased_pair(projection=ref,mapping=mapping,guard=guard,
                            provider=provider,account_id=account_id,current=current)
                if promoted:
                    validate_operation()
                    if await _queued_owned_thread(provider._host_account_source_refs, **inventory_args)!=result:
                        raise HostSourceFenceUnavailable('paired erased inventory changed')
                    validate_operation()
                    refs=tuple(promoted[ref.projection.job_id] if type(ref) is _HostPairedErasedCandidate else ref for ref in refs)
                groups, sources = {}, []
                for ref in refs:
                    chat=ref.source.root.chat_id if type(ref) is _HostPairedSourceProjection else ref.source.chat_id
                    groups.setdefault(chat, []).append(ref)
                for chat in sorted(groups):
                    async with mapping.async_chat_admission_lock(chat) as guard:
                        validate_operation()
                        if authority.resolve(account_id) != binding:
                            raise ValueError('account source target changed')
                        for ref in groups[chat]:
                            if type(ref) is _HostPairedSourceProjection:
                                if phase is None:
                                    raise HostSourceFenceUnavailable('paired phase unavailable')
                                def current():
                                    try:
                                        validate_operation(); mapping.validate_chat_guard(chat,guard)
                                        return (authority.resolve(account_id)==binding and canonical_source_provider(state) is provider
                                            and _account_pair_read_allowed(source_authority,owner) is True)
                                    except Exception: return False
                                await _account_pair_verify(ref.source,source_authority=source_authority,mapping=mapping,
                                    guard=guard,current=current,provider_owner=owner)
                                if ref.transition_class=='erased':
                                    await provider._host_paired_erased_fact(ref,account_id=account_id,current=current)
                                request={'version':1,'phase':phase,'workspace_id':ref.workspace_id,'job_id':ref.job_id,
                                    'source':asdict(ref.source.root),'coverage':_account_pair_coverage(ref.source),
                                    'policy_fingerprint':ref.policy_fingerprint}
                                if ref.transition_class=='pending':
                                    receipt=await provider._host_pending_pair(request,phase='pending_pair_fence',before_issue=current)
                                elif ref.transition_class=='terminal':
                                    receipt=await provider._host_pending_pair(request,phase='terminal_pair_fence',before_issue=current)
                                elif ref.transition_class=='erased':
                                    receipt=await provider._host_pending_pair(request,phase='erased_pair_fence',before_issue=current)
                                elif ref.transition_class=='materialized':
                                    receipt=await provider._host_paired_rename_fence(request,before_issue=current)
                                else: raise HostSourceFenceUnavailable('paired transition classification unavailable')
                                await _account_pair_verify(ref.source,source_authority=source_authority,mapping=mapping,
                                    guard=guard,current=current,provider_owner=owner)
                                sources.append(_phase_pending_pair_item(ref,phase,receipt) if ref.transition_class in ('pending','terminal','erased') else _phase_pair_page_item(ref,phase,receipt))
                                continue
                            if phase is not None and type(ref) is _HostObservationProjection:
                                check_deleted = kind == 'delete' and phase['step'] == 'memory_fm_tombstoned'
                                def deleted_chat():
                                    return check_deleted and _deleted_observation_chat(
                                        get_conversation_archive(), SessionLocal, account_id=account_id, chat=chat)
                                was_deleted = deleted_chat()
                                def current():
                                    try:
                                        validate_operation(); mapping.validate_chat_guard(chat,guard)
                                        return authority.resolve(account_id)==binding and canonical_source_provider(state) is provider and deleted_chat() == was_deleted
                                    except Exception: return False
                                observation={'origin_id':ref.origin_id,'origin_hash':ref.origin_hash,
                                    'captured_owner':ref.captured_owner,'current_fingerprint':ref.current_fingerprint,
                                    'rebind_id':ref.rebind_id,'rebind_hash':ref.rebind_hash}
                                request={'version':1,'phase':phase,'workspace_id':ref.workspace_id,
                                    'source':asdict(ref.source),'authority':observation}
                                if was_deleted:
                                    request['deleted_chat'] = True
                                receipt=await provider._host_phase_root_observation_fence(request,before_issue=current)
                                if current() is not True: raise HostSourceFenceUnavailable('observation phase authority changed')
                                sources.append(_phase_observation_page_item(ref,phase,receipt,deleted_chat=was_deleted))
                                continue
                            if phase is not None and type(ref) is _HostSourceProjection:
                                def current():
                                    try:
                                        validate_operation(); mapping.validate_chat_guard(chat,guard)
                                        return authority.resolve(account_id)==binding and canonical_source_provider(state) is provider
                                    except Exception: return False
                                request={'version':1,'phase':phase,'workspace_id':ref.workspace_id,'job_id':ref.job_id,
                                    'source':asdict(ref.source),'policy_fingerprint':ref.policy_fingerprint}
                                receipt=await provider._host_phase_root_fence(request,before_issue=current)
                                if current() is not True: raise HostSourceFenceUnavailable('phase root authority changed')
                                sources.append(_phase_root_page_item(ref,phase,receipt))
                                continue
                            receipt = await provider._host_source_fence(ref, rename_operation_id=operation_id) if kind == 'rename' else await provider._host_source_fence(ref)
                            mapping.validate_chat_guard(chat, guard)
                            if (not isinstance(receipt, dict) or receipt.get('visibility') != 'closed'
                                or receipt.get('state') not in (('fenced','already_fenced','absent_fenced','rename_absent') if kind == 'rename' else ('fenced','already_fenced','absent_fenced'))
                                or not _source_fence_covered(receipt)):
                                raise ValueError('account source fence unavailable')
                            key = _source_inventory_key(ref)
                            item = {'source':asdict(ref.source), 'source_id':receipt['source_id'], 'state':receipt['state']}
                            if key.startswith('j:'):
                                item.update(job_id=ref.job_id, policy_fingerprint=ref.policy_fingerprint)
                            else:
                                item.update(origin_id=ref.origin_id, origin_hash=ref.origin_hash,
                                    captured_owner=ref.captured_owner, current_fingerprint=ref.current_fingerprint,
                                    rebind_id=ref.rebind_id, rebind_hash=ref.rebind_hash)
                            if kind == 'rename':
                                item['rename_proof'] = receipt.get('rename_proof')
                            sources.append(item)
                sources.sort(key=lambda item:('j:' + item['job_id']) if 'job_id' in item else ('o:' + (item['authority']['origin_id'] if item.get('kind')=='phase_observation' else item['origin_id'])))
                page = {'state':'fenced', 'transition':kind, 'operation_id':operation_id, 'binding':asdict(binding),
                        'source_count':len(refs), 'chat_count':len(groups), 'sources':sources,
                        'renewal':_account_source_renewal(kind,sources,phase), 'after':after, 'next_after':next_after, **({'phase':phase} if phase is not None else {})}
                _validate_account_source_page(page, owner=owner, account_id=account_id, kind=kind, operation_id=operation_id, after=after, phase=phase)
                lifecycle.checkpoint_delete_operation(operation_id, step, page, claim_token=claim_token)
            _validate_account_source_page(page, owner=owner, account_id=account_id, kind=kind, operation_id=operation_id, after=after, phase=phase)
            if page['binding'] != asdict(binding):
                raise ValueError('account source target changed')
            source_count += page['source_count']; page_count += 1
            if page['renewal'] == 'pending': renewal = 'pending'
            if page['next_after'] is None: break
            after = page['next_after']
        validate_operation()
        if authority.resolve(account_id) != binding:
            raise ValueError('account source target changed')
        return {'state':'fenced', 'transition':kind, 'operation_id':operation_id, 'binding':asdict(binding),
                'source_count':source_count, 'page_count':page_count, 'renewal':renewal, **({'phase':phase} if phase is not None else {})}
    except Exception as exc:
        if isinstance(exc, HostSourceFenceUnavailable):
            raise
        raise HostSourceFenceUnavailable('account source transition unavailable') from exc


def _validate_account_source_page(receipt, *, owner, account_id, kind, operation_id, after, phase=None):
    """Validate one bounded saved page without resolving a renamed user."""
    from src.memory_scope import memory_owner, chat_workspace
    if type(receipt) is not dict:
        raise HostSourceFenceUnavailable('account source receipt unavailable')
    binding = receipt.get('binding')
    fields = {'source_account_id','memory_owner','memory_workspace','owner_epoch','durable_epoch','fingerprint'}
    if (set(receipt) != ({'state','transition','operation_id','binding','source_count','chat_count','sources','renewal','after','next_after'} | ({'phase'} if phase is not None else set())) or (phase is not None and receipt.get('phase')!=phase) or receipt['state'] != 'fenced'
        or receipt['transition'] != kind or receipt['operation_id'] != operation_id or kind not in ('rename','delete')
        or receipt['after'] != after
        or type(binding) is not dict or set(binding) != fields or binding['source_account_id'] != account_id
        or binding['memory_owner'] != memory_owner(owner) or binding['memory_workspace'] != chat_workspace()
        or type(binding['owner_epoch']) is not int or binding['owner_epoch'] < 0
        or type(binding['durable_epoch']) is not str or not binding['durable_epoch'].isdigit()
        or any(type(receipt[key]) is not int or not 0 <= receipt[key] <= 128 for key in ('source_count','chat_count'))):
        raise HostSourceFenceUnavailable('account source receipt changed')
    payload = {key:value for key,value in binding.items() if key != 'fingerprint'}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',',':')).encode()).hexdigest()
    if binding['fingerprint'] != expected:
        raise HostSourceFenceUnavailable('account source receipt changed')
    from src.memery_external_sources import SourceRef, _source_ref_error
    sources = receipt['sources']
    if type(sources) is not list or len(sources) != receipt['source_count']:
        raise HostSourceFenceUnavailable('account source receipt changed')
    keys, chats, proofs = set(), set(), {}
    for item in sources:
        if type(item) is dict and item.get('kind')=='phase_observation':
            if item.get('deleted_chat') is True and kind != 'delete':
                raise HostSourceFenceUnavailable('deleted observation is outside account deletion')
            source=_validate_phase_observation_item(item,phase=phase,account_id=account_id,workspace_id=binding['memory_workspace'])
            key='o:'+item['authority']['origin_id']
            if key in keys: raise HostSourceFenceUnavailable('account source receipt changed')
            keys.add(key); chats.add(source.chat_id)
            continue
        if type(item) is dict and item.get('kind')=='phase_root':
            root=_validate_phase_root_item(item,phase=phase,account_id=account_id)
            key='j:'+item['job_id']
            if key in keys: raise HostSourceFenceUnavailable('account source receipt changed')
            keys.add(key); chats.add(root.chat_id)
            continue
        if type(item) is dict and item.get('kind') in ('pending_pair','terminal_pair','erased_pair'):
            pair=_validate_phase_pending_pair_item(item,phase=phase,account_id=account_id)
            key='j:'+item['job_id']
            if key in keys: raise HostSourceFenceUnavailable('account source receipt changed')
            keys.add(key); chats.add(pair.root.chat_id)
            continue
        if type(item) is dict and item.get('kind')=='paired':
            pair=_validate_phase_pair_item(item,phase=phase,account_id=account_id)
            key='j:'+item['job_id']
            if key in keys: raise HostSourceFenceUnavailable('account source receipt changed')
            keys.add(key); chats.add(pair.root.chat_id)
            continue
        is_job = type(item) is dict and 'job_id' in item
        expected_fields = ({'job_id','source','policy_fingerprint','source_id','state'} if is_job else {'origin_id','origin_hash','captured_owner','current_fingerprint','rebind_id','rebind_hash','source','source_id','state'}) | ({'rename_proof'} if kind == 'rename' else set())
        if type(item) is not dict or set(item) != expected_fields:
            raise HostSourceFenceUnavailable('account source receipt changed')
        try:
            source = SourceRef(**item['source'])
        except (TypeError, ValueError):
            raise HostSourceFenceUnavailable('account source receipt changed')
        item_id = item['job_id'] if is_job else item['origin_id']
        key = ('j:' if is_job else 'o:') + item_id if type(item_id) is str else ''
        if (_source_ref_error(source) or source.account_id != account_id or not key or key in keys
            or not item_id or item['state'] not in (('fenced','already_fenced','absent_fenced','rename_absent') if kind == 'rename' else ('fenced','already_fenced','absent_fenced'))
            or (is_job and (type(item['policy_fingerprint']) is not str or len(item['policy_fingerprint']) != 64
                or any(c not in '0123456789abcdef' for c in item['policy_fingerprint'])))):
            raise HostSourceFenceUnavailable('account source receipt changed')
        if not is_job:
            captured = item['captured_owner']
            valid_hash = lambda value: type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)
            if (type(captured) is not str or not captured or captured != captured.strip()
                or not valid_hash(item['origin_hash']) or not valid_hash(item['current_fingerprint'])
                or (item['rebind_id'] is None) != (item['rebind_hash'] is None)
                or (item['rebind_id'] is None and captured != binding['memory_owner'])
                or (item['rebind_id'] is not None and (type(item['rebind_id']) is not str or not item['rebind_id'] or not valid_hash(item['rebind_hash'])))):
                raise HostSourceFenceUnavailable('account observation authority changed')
            material = json.dumps([captured,binding['memory_workspace'],asdict(source)], ensure_ascii=False, separators=(',',':')).encode()
            if item_id != 'cs_s06origin_' + hashlib.sha256(material).hexdigest():
                raise HostSourceFenceUnavailable('account observation origin changed')
        identity = [source.version,source.account_id,source.chat_id,source.actor_id,source.message_id,source.part_id]
        source_id = 'hostpart_' + hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if item['source_id'] != source_id:
            raise HostSourceFenceUnavailable('account source receipt changed')
        if kind == 'rename':
            proof = item['rename_proof']
            identity = [account_id, operation_id, source_id]
            proof_id = 'cs_s04rename_' + hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            if type(proof) is not dict or set(proof) != {'change_set_id','active_versions'} or proof['change_set_id'] != proof_id or type(proof['active_versions']) is not list or len(proof['active_versions']) > 1:
                raise HostSourceFenceUnavailable('account source receipt changed')
            if item['state'] == 'rename_absent' and proof['active_versions']:
                raise HostSourceFenceUnavailable('account source receipt changed')
            prior = 0
            for version in proof['active_versions']:
                if (type(version) is not dict or set(version) != {'revision','content_hash'} or type(version['revision']) is not int
                    or version['revision'] <= prior or type(version['content_hash']) is not str or len(version['content_hash']) != 64
                    or any(c not in '0123456789abcdef' for c in version['content_hash'])):
                    raise HostSourceFenceUnavailable('account source receipt changed')
                prior = version['revision']
            if source_id in proofs and proofs[source_id] != proof:
                raise HostSourceFenceUnavailable('account source receipt changed')
            proofs[source_id] = proof
        keys.add(key); chats.add(source.chat_id)
    renewal = _account_source_renewal(kind, sources, phase)
    if len(chats) != receipt['chat_count'] or receipt['renewal'] != renewal:
        raise HostSourceFenceUnavailable('account source receipt changed')
    ordered = [('j:' + item['job_id']) if 'job_id' in item else ('o:' + (item['authority']['origin_id'] if item.get('kind')=='phase_observation' else item['origin_id'])) for item in sources]
    normalized_after = ('j:' + after) if after and not after.startswith(('j:','o:')) else after
    if ordered != sorted(ordered) or (normalized_after is not None and any(key <= normalized_after for key in ordered)):
        raise HostSourceFenceUnavailable('account source page changed')
    next_after = receipt['next_after']
    normalized_next = ('j:' + next_after) if type(next_after) is str and next_after and not next_after.startswith(('j:','o:')) else next_after
    if next_after is not None and (type(next_after) is not str or
        (normalized_next != 'o:' and (not ordered or normalized_next != ordered[-1])) or
        (normalized_next == 'o:' and normalized_after is not None and normalized_after.startswith('o:'))):
        raise HostSourceFenceUnavailable('account source cursor unavailable')


def validate_account_source_fence_receipt(receipt, *, owner, account_id, kind, operation_id, lifecycle, phase=None):
    """Replay bounded page checkpoints against one exact operation summary."""
    if receipt == {'state':'inactive', 'reason':'publication_lifecycle_unavailable'}:
        return
    if (type(receipt) is not dict or set(receipt) != ({'state','transition','operation_id','binding','source_count','page_count','renewal'} | ({'phase'} if phase is not None else set())) or (phase is not None and receipt.get('phase')!=phase)
        or receipt.get('state') != 'fenced' or receipt.get('transition') != kind or receipt.get('operation_id') != operation_id
        or type(receipt.get('page_count')) is not int or receipt['page_count'] < 1
        or type(receipt.get('source_count')) is not int or receipt['source_count'] < 0):
        raise HostSourceFenceUnavailable('account source receipt unavailable')
    operation = lifecycle.get_operation(operation_id)
    if phase is not None and _account_source_saved_phase(operation)!=phase: raise HostSourceFenceUnavailable('account source phase changed')
    steps = operation.get('steps') or {}
    if receipt['page_count'] > len(steps) or receipt['source_count'] > receipt['page_count'] * 128:
        raise HostSourceFenceUnavailable('account source receipt changed')
    after, total = None, 0
    renewal = 'not_required' if kind == 'rename' or (phase is not None and phase['action']=='reverse') else 'not_applicable'
    for index in range(receipt['page_count']):
        saved = steps.get(_account_source_page_step(after,phase)) or {}
        if saved.get('state') != 'applied':
            raise HostSourceFenceUnavailable('account source page unavailable')
        page = saved.get('receipt')
        _validate_account_source_page(page, owner=owner, account_id=account_id, kind=kind, operation_id=operation_id, after=after, phase=phase)
        if page['binding'] != receipt['binding']:
            raise HostSourceFenceUnavailable('account source target changed')
        total += page['source_count']
        if page['renewal'] == 'pending': renewal = 'pending'
        after = page['next_after']
        if (after is None) != (index == receipt['page_count'] - 1):
            raise HostSourceFenceUnavailable('account source page chain changed')
    if total != receipt['source_count'] or renewal != receipt['renewal']:
        raise HostSourceFenceUnavailable('account source receipt changed')


_ACCOUNT_TRANSITION_SEAL = object()


@dataclass(frozen=True)
class HostAccountSourceTransition:
    lifecycle: object
    operation_id: str
    claim_token: str
    provider: object
    receipt_digest: str
    phase: object
    summary_step: str
    _seal: object
    prefence_state: object = None
    prefence_host_owner: object = None
    prefence_preparation: object = None

    def assert_prefence_current(self, *, provider, phase, account_id, sql_owner):
        from src.openclank.account_lifecycle import AccountOwnerLifecycle
        from src.memery_database import GuardedStorePreparation
        import os
        if (self._seal is not _ACCOUNT_TRANSITION_SEAL or provider is not self.provider
            or type(self.lifecycle) is not AccountOwnerLifecycle or self.prefence_state is None
            or type(self.prefence_preparation) is not GuardedStorePreparation
            or provider._guarded_preparation is not self.prefence_preparation
            or phase!=self.phase or type(phase) is not dict or phase.get('account_id')!=account_id
            or phase.get('owner')!=sql_owner):
            raise HostSourceFenceUnavailable('account source pre-fence unavailable')
        _account_source_phase_hash(phase)
        state=self.prefence_state; auth=getattr(state,'auth_manager',None)
        if (canonical_source_provider(state) is not provider or auth is None
            or getattr(state,'account_owner_lifecycle',None) is not self.lifecycle):
            raise HostSourceFenceUnavailable('account source pre-fence provider changed')
        operation=self.lifecycle.get_operation(self.operation_id)
        claim=(operation.get('receipt') or {}).get('claim') or {}
        old=operation.get('source_owner'); kind=operation.get('kind')
        target=operation.get('target_owner') if kind=='rename' else operation.get('tombstone_owner')
        host=self.prefence_host_owner
        direction=(old,target) if phase['action']=='forward' else (target,old)
        name='memory_fm_source_phase_'+str(phase['generation'])+('_reverse' if phase['action']=='reverse' else '')
        saved=(operation.get('steps') or {}).get(name) or {}
        if (kind not in ('rename','delete') or operation.get('operation_id')!=self.operation_id
            or operation.get('subject_id')!=account_id or claim.get('token')!=self.claim_token
            or not old or not target or not self.lifecycle.owner_has_active_operation(old)
            or (phase['owner'],phase['new_owner'])!=direction
            or phase['operation_id']!=self.operation_id
            or phase['step']!=('memory_fm' if kind=='rename' else 'memory_fm_tombstoned')
            or (operation.get('manifest') or {}).get(name)!=phase
            or saved.get('state')!='applied' or saved.get('receipt')!=phase
            or type(host) is not str or not host or auth.account_id(host)!=account_id
            or auth.username_for_account_id(account_id)!=host):
            raise HostSourceFenceUnavailable('account source pre-fence claim changed')
        preparation=self.prefence_preparation
        if (preparation.database_id!=phase['database_id'] or os.path.realpath(provider._local_db_path() or '')!=preparation.database_path):
            raise HostSourceFenceUnavailable('account source pre-fence database changed')
        preparation.assert_valid()
        if (provider._guarded_preparation is not preparation or canonical_source_provider(state) is not provider
            or getattr(state,'auth_manager',None) is not auth or getattr(state,'account_owner_lifecycle',None) is not self.lifecycle
            or auth.account_id(host)!=account_id or auth.username_for_account_id(account_id)!=host):
            raise HostSourceFenceUnavailable('account source pre-fence provider changed')
        return True

    def assert_current(self, *, provider, action, owner, new_owner=None):
        from src.openclank.account_lifecycle import AccountOwnerLifecycle
        if self._seal is not _ACCOUNT_TRANSITION_SEAL or provider is not self.provider or type(self.lifecycle) is not AccountOwnerLifecycle:
            raise HostSourceFenceUnavailable('account source transition capability unavailable')
        operation = self.lifecycle.get_operation(self.operation_id)
        claim = (operation.get('receipt') or {}).get('claim') or {}
        old, kind, subject = operation.get('source_owner'), operation.get('kind'), operation.get('subject_id')
        target = operation.get('target_owner') if kind == 'rename' else operation.get('tombstone_owner')
        if self.phase is not None and _account_source_saved_phase(operation)!=self.phase:
            raise HostSourceFenceUnavailable('account source phase changed')
        if self.summary_step != account_source_summary_step(self.phase):
            raise HostSourceFenceUnavailable('account source phase changed')
        receipt = ((operation.get('steps') or {}).get(self.summary_step) or {})
        if (kind not in ('rename','delete') or claim.get('token') != self.claim_token or not old or not target or not subject
            or not self.lifecycle.owner_has_active_operation(old) or receipt.get('state') != 'applied'):
            raise HostSourceFenceUnavailable('account source transition changed')
        source_receipt = receipt.get('receipt')
        encoded = json.dumps(source_receipt, sort_keys=True, separators=(',',':'), allow_nan=False)
        if hashlib.sha256(encoded.encode()).hexdigest() != self.receipt_digest:
            raise HostSourceFenceUnavailable('account source receipt changed')
        if source_receipt.get('state') != 'fenced':
            raise HostSourceFenceUnavailable('account source fence unavailable')
        permitted = ((action == 'rename' and (owner, new_owner) in ((old,target),(target,old)))
                     or (kind == 'rename' and action == 'renew' and owner == target and new_owner is None)
                     or (kind == 'delete' and self.phase is not None and self.phase.get('action')=='reverse' and action=='renew' and owner==old and new_owner is None)
                     or (kind == 'delete' and action == 'purge' and owner == target and new_owner is None))
        if not permitted:
            raise HostSourceFenceUnavailable('account source transition scope changed')


def issue_account_source_prefence_transition(*, state, lifecycle, operation_id, claim_token, provider, phase, host_owner):
    """Bind the real claimed phase before its source-summary checkpoint exists."""
    preparation=getattr(provider,'_guarded_preparation',None)
    capability=HostAccountSourceTransition(lifecycle,operation_id,claim_token,provider,'',phase,'',
        _ACCOUNT_TRANSITION_SEAL,state,host_owner,preparation)
    capability.assert_prefence_current(provider=provider,phase=phase,account_id=phase.get('account_id') if type(phase) is dict else None,
                                      sql_owner=phase.get('owner') if type(phase) is dict else None)
    return capability


def issue_account_source_transition(*, lifecycle, operation_id, claim_token, provider, action, owner, new_owner=None):
    operation = lifecycle.get_operation(operation_id)
    phase=_account_source_saved_phase(operation)
    summary_step=account_source_summary_step(phase)
    source_receipt = ((operation.get('steps') or {}).get(summary_step) or {}).get('receipt')
    validate_account_source_fence_receipt(source_receipt, owner=operation.get('source_owner'),
        account_id=operation.get('subject_id'), kind=operation.get('kind'), operation_id=operation_id, lifecycle=lifecycle,phase=phase)
    encoded = json.dumps(source_receipt, sort_keys=True, separators=(',',':'), allow_nan=False)
    capability = HostAccountSourceTransition(lifecycle, operation_id, claim_token, provider, hashlib.sha256(encoded.encode()).hexdigest(), phase, summary_step, _ACCOUNT_TRANSITION_SEAL)
    capability.assert_current(provider=provider, action=action, owner=owner, new_owner=new_owner)
    return capability


def _account_source_renewal_step(source_id, revision, content_hash, origin_id=None):
    value = json.dumps([source_id,revision,content_hash] if origin_id is None else [source_id,revision,content_hash,origin_id], separators=(',',':'))
    return 'host_source_renew_' + hashlib.sha256(value.encode()).hexdigest()


def _account_pair_renewal_step(phase, job_id):
    raw=json.dumps([_account_source_phase_hash(phase),job_id],ensure_ascii=False,separators=(',',':')).encode()
    return 'host_pair_renew_'+hashlib.sha256(raw).hexdigest()


def _account_committed_transition(operation, phase):
    """Recover the exact saved forward or compensation transition and receipt."""
    if phase is None: raise HostSourceFenceUnavailable('paired transition unavailable')
    manifest=operation.get('manifest') or {}; steps=operation.get('steps') or {}
    reverse=phase['action']=='reverse'
    snapshot=manifest.get('memory_fm_reverse_'+str(phase['generation']) if reverse else phase['step'])
    saved=steps.get('memory_fm_reverse_'+str(phase['generation']) if reverse else phase['step']) or {}
    receipt=saved.get('receipt')
    if type(snapshot) is not dict or saved.get('state')!='applied' or type(receipt) is not dict or receipt.get('state')!='applied':
        raise HostSourceFenceUnavailable('paired transition receipt unavailable')
    try:
        if reverse:
            original=manifest[phase['step']]; relocation=original['rag_relocation']
            relocation={'from':relocation['to'],'to':relocation['from']} if relocation is not None else None
            source_hash=snapshot['source_fingerprint']; target_hash=snapshot['target_fingerprint']
        else:
            if snapshot.get('generation')!=phase['generation']: raise ValueError()
            source_hash=snapshot['source']['fingerprint']; target_hash=snapshot['target']['fingerprint']
            relocation=snapshot['rag_relocation']
        transition={'version':1,**phase,'expected_source_fingerprint':source_hash,'expected_target_fingerprint':target_hash,
                    'prior_receipt_id':snapshot['prior_receipt_id'],'prior_receipt_hash':snapshot['prior_receipt_hash'],
                    'rag_relocation':relocation}
        if any(type(receipt[k]) is not str or not receipt[k] for k in ('receipt_id','receipt_hash')): raise ValueError()
        return transition,receipt['receipt_id'],receipt['receipt_hash']
    except (KeyError,TypeError,ValueError):
        raise HostSourceFenceUnavailable('paired transition receipt unavailable') from None


async def renew_account_host_sources(*, state, lifecycle, operation_id, claim_token, old_owner, new_owner, account_id):
    """Revalidate and renew only source versions proven active before rename."""
    import asyncio
    from core.database import SessionLocal
    from src.memery_external_sources import CurrentHostUserSource, SourceRef, SourceRetirementFact
    from src.memery_provider import _HostSourceRenew, _HostObservationRenew
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_job_admission import TargetAuthority
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap

    operation = lifecycle.get_operation(operation_id)
    phase=_account_source_saved_phase(operation)
    summary = ((operation.get('steps') or {}).get(account_source_summary_step(phase)) or {}).get('receipt')
    transition_kind=operation.get('kind')
    validate_account_source_fence_receipt(summary, owner=old_owner, account_id=account_id,
        kind=transition_kind, operation_id=operation_id, lifecycle=lifecycle,phase=phase)
    if summary.get('state') == 'inactive' or summary.get('renewal') == 'not_required':
        return {'state':'complete', 'renewed':0, 'withheld':0}
    provider = canonical_source_provider(state)
    supervisor = getattr(state, 'mimo_supervisor', None)
    auth = getattr(state, 'auth_manager', None)
    if provider is None or not isinstance(supervisor, AcpSupervisorAdapter) or auth is None:
        raise HostSourceFenceUnavailable('rename source authority unavailable')
    pool = supervisor.owner_lifecycle_pool()
    authority = TargetAuthority(auth_manager=auth, supervisor=pool)
    if auth.account_id(new_owner) != account_id or auth.username_for_account_id(account_id) != new_owner:
        raise HostSourceFenceUnavailable('rename account identity changed')
    target = authority.resolve(account_id)
    if target.memory_workspace != summary['binding']['memory_workspace']:
        raise HostSourceFenceUnavailable('rename memory workspace changed')
    capability = issue_account_source_transition(lifecycle=lifecycle, operation_id=operation_id, claim_token=claim_token,
        provider=provider, action='renew', owner=new_owner)
    source_authority = CurrentHostUserSource(owner=new_owner, auth_manager=auth, session_factory=SessionLocal,
        archive=get_conversation_archive(), workspace_repository=getattr(state,'files_policy_repository',None), policy_store=STORE,
        guarded_preparation=provider._guarded_preparation)
    mapping = OwnerSessionMap(pool._runtime_home(new_owner) / 'session-map.json', new_owner)
    after = None
    for _ in range(summary['page_count']):
        operation = lifecycle.get_operation(operation_id)
        page = ((operation.get('steps') or {}).get(_account_source_page_step(after,phase)) or {}).get('receipt')
        _validate_account_source_page(page, owner=old_owner, account_id=account_id, kind=transition_kind, operation_id=operation_id, after=after, phase=phase)
        for item in page['sources']:
            if item.get('kind') in ('pending_pair','terminal_pair','erased_pair'):
                from src.openclank.memory_policy_projection import policy_coordinator
                from src.memery_external_sources import _queued_owned_thread
                pair=_validate_phase_pending_pair_item(item,phase=phase,account_id=account_id)
                root,assistant=item['root'],item['assistant']
                if root['prior_state']=='absent' and assistant['prior_state']=='active':
                    raise HostSourceFenceUnavailable('pending assistant contributor unavailable')
                step=_account_pair_renewal_step(phase,item['kind']+':'+item['job_id'])
                expected={'state':'awaiting_post_transition_rebind' if item['kind']=='pending_pair' else 'terminal_preserved','fence_id':item['fence_id'],
                    'root':'renewed' if root['prior_state']=='active' else 'absent',
                    'assistant':'renewed' if assistant['prior_state']=='active' else 'absent'}
                saved=((lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {})
                if saved.get('state')=='applied':
                    if saved.get('receipt')!=expected: raise HostSourceFenceUnavailable('pending disposition changed')
                    continue
                async with mapping.async_chat_admission_lock(pair.root.chat_id) as guard:
                    capability.assert_current(provider=provider,action='renew',owner=new_owner)
                    coordinator=policy_coordinator()
                    if coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None):
                        raise HostSourceFenceUnavailable('pending pair policy unavailable')
                    refs=await _queued_owned_thread(coordinator.checked_refs,new_owner,account_id,target.memory_workspace)
                    def current():
                        try:
                            mapping.validate_chat_guard(pair.root.chat_id,guard)
                            capability.assert_current(provider=provider,action='renew',owner=new_owner)
                            return (authority.resolve(account_id)==target and canonical_source_provider(state) is provider
                                and policy_coordinator() is coordinator and coordinator.checked_refs(new_owner,account_id,target.memory_workspace)==refs
                                and _account_pair_read_allowed(source_authority,new_owner) is True)
                        except Exception: return False
                    await _account_pair_verify(pair,source_authority=source_authority,mapping=mapping,guard=guard,
                        current=current,provider_owner=new_owner)
                    erased_projection=None
                    if item['kind']=='erased_pair':
                        from src.memery_provider import _HostPairedSourceProjection
                        erased_projection=_HostPairedSourceProjection(new_owner,target.memory_workspace,item['job_id'],pair,
                            item['policy_fingerprint'],'erased',item['input_sha256'])
                        await provider._host_paired_erased_fact(erased_projection,account_id=account_id,current=current)
                    lifecycle.start_operation_step(operation_id,step,claim_token=claim_token)
                    if root['prior_state']=='active':
                        transition,receipt_id,receipt_hash=_account_committed_transition(lifecycle.get_operation(operation_id),phase)
                        common={'version':1,'transition':transition,'receipt_id':receipt_id,'receipt_hash':receipt_hash,
                            'workspace_id':target.memory_workspace,'source':asdict(pair.root),'policy_projection':refs}
                        if assistant['prior_state']=='active':
                            request={**common,'job_id':item['job_id'],'coverage':item['coverage'],
                                'policy_fingerprint':item['policy_fingerprint'],'root_fence_id':root['fence_id'],
                                'assistant_fence_id':assistant['fence_id']}
                            native=await provider._host_paired_rename_renew(request,before_issue=current)
                            if (native.get('state')!='renewed' or native.get('root_fence_id')!=root['fence_id']
                                or native.get('assistant_fence_id')!=assistant['fence_id']):
                                raise HostSourceFenceUnavailable('pending pair renewal unavailable')
                        else:
                            native=await provider._host_phase_root_renew({**common,'fence_id':root['fence_id']},before_issue=current)
                            if native.get('state')!='renewed': raise HostSourceFenceUnavailable('pending root renewal unavailable')
                    await _account_pair_verify(pair,source_authority=source_authority,mapping=mapping,guard=guard,
                        current=current,provider_owner=new_owner)
                    if erased_projection is not None:
                        await provider._host_paired_erased_fact(erased_projection,account_id=account_id,current=current)
                    lifecycle.checkpoint_delete_operation(operation_id,step,expected,claim_token=claim_token)
                continue
            if item.get('kind')=='phase_observation':
                if item.get('deleted_chat') is True:
                    continue
                from src.openclank.memory_policy_projection import policy_coordinator
                from src.memery_external_sources import _queued_owned_thread
                source=_validate_phase_observation_item(item,phase=phase,account_id=account_id,workspace_id=target.memory_workspace)
                step=_account_pair_renewal_step(phase,'observation:'+item['authority']['origin_id'])
                saved=((lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {})
                if saved.get('state')=='applied':
                    prior=saved.get('receipt') or {}
                    if prior.get('state') not in ('renewed','already_applied','restored') or prior.get('fence_id')!=item['fence_id']:
                        raise HostSourceFenceUnavailable('phase observation renewal changed')
                    continue
                async with mapping.async_chat_admission_lock(source.chat_id) as guard:
                    capability.assert_current(provider=provider,action='renew',owner=new_owner)
                    coordinator=policy_coordinator()
                    if coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None):
                        raise HostSourceFenceUnavailable('phase observation policy unavailable')
                    refs=await _queued_owned_thread(coordinator.checked_refs,new_owner,account_id,target.memory_workspace)
                    def current():
                        try:
                            mapping.validate_chat_guard(source.chat_id,guard)
                            capability.assert_current(provider=provider,action='renew',owner=new_owner)
                            return (authority.resolve(account_id)==target and canonical_source_provider(state) is provider
                                and policy_coordinator() is coordinator and coordinator.checked_refs(new_owner,account_id,target.memory_workspace)==refs
                                and _account_pair_read_allowed(source_authority,new_owner) is True)
                        except Exception: return False
                    if current() is not True or (await _queued_owned_thread(source_authority.verify_feedback_origin,source)).get('state')!='available':
                        raise HostSourceFenceUnavailable('phase observation source unavailable')
                    transition,receipt_id,receipt_hash=_account_committed_transition(lifecycle.get_operation(operation_id),phase)
                    lifecycle.start_operation_step(operation_id,step,claim_token=claim_token)
                    request={'version':1,'transition':transition,'receipt_id':receipt_id,'receipt_hash':receipt_hash,
                        'workspace_id':target.memory_workspace,'source':asdict(source),'fence_id':item['fence_id'],
                        'policy_projection':refs,'authority':item['authority'],'current_fingerprint':target.fingerprint}
                    native=await provider._host_phase_root_observation_renew(request,before_issue=current)
                    if (current() is not True or (await _queued_owned_thread(source_authority.verify_feedback_origin,source)).get('state')!='available'
                        or native.get('state') not in ('renewed','already_applied','restored') or native.get('revision')!=source.revision):
                        raise HostSourceFenceUnavailable('phase observation renewal unavailable')
                    lifecycle.checkpoint_delete_operation(operation_id,step,{'state':native['state'],
                        'fence_id':item['fence_id'],'native':native},claim_token=claim_token)
                continue
            if item.get('kind')=='phase_root':
                from src.openclank.memory_policy_projection import policy_coordinator
                from src.memery_external_sources import _queued_owned_thread
                source=_validate_phase_root_item(item,phase=phase,account_id=account_id)
                if item['state']=='absent': continue
                step=_account_pair_renewal_step(phase,'root:'+item['job_id'])
                saved=((lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {})
                if saved.get('state')=='applied':
                    prior=saved.get('receipt') or {}
                    if prior.get('state')!='renewed' or prior.get('fence_id')!=item['fence_id']:
                        raise HostSourceFenceUnavailable('phase root renewal changed')
                    continue
                async with mapping.async_chat_admission_lock(source.chat_id) as guard:
                    capability.assert_current(provider=provider,action='renew',owner=new_owner)
                    coordinator=policy_coordinator()
                    if coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None):
                        raise HostSourceFenceUnavailable('phase root policy unavailable')
                    refs=await _queued_owned_thread(coordinator.checked_refs,new_owner,account_id,target.memory_workspace)
                    def current():
                        try:
                            mapping.validate_chat_guard(source.chat_id,guard)
                            capability.assert_current(provider=provider,action='renew',owner=new_owner)
                            return (authority.resolve(account_id)==target and canonical_source_provider(state) is provider
                                and policy_coordinator() is coordinator and coordinator.checked_refs(new_owner,account_id,target.memory_workspace)==refs
                                and _account_pair_read_allowed(source_authority,new_owner) is True)
                        except Exception: return False
                    if current() is not True or (await _queued_owned_thread(source_authority._hydrate,source,manual_review=True)).get('state')!='available':
                        raise HostSourceFenceUnavailable('phase root source unavailable')
                    transition,receipt_id,receipt_hash=_account_committed_transition(lifecycle.get_operation(operation_id),phase)
                    lifecycle.start_operation_step(operation_id,step,claim_token=claim_token)
                    request={'version':1,'transition':transition,'receipt_id':receipt_id,'receipt_hash':receipt_hash,
                        'workspace_id':target.memory_workspace,'source':asdict(source),'fence_id':item['fence_id'],'policy_projection':refs}
                    native=await provider._host_phase_root_renew(request,before_issue=current)
                    if (current() is not True or (await _queued_owned_thread(source_authority._hydrate,source,manual_review=True)).get('state')!='available'
                        or native.get('state')!='renewed'):
                        raise HostSourceFenceUnavailable('phase root renewal unavailable')
                    lifecycle.checkpoint_delete_operation(operation_id,step,{'state':'renewed','fence_id':item['fence_id'],
                        'receipt_id':native['receipt_id']},claim_token=claim_token)
                continue
            if item.get('kind')=='paired':
                from src.openclank.memory_policy_projection import policy_coordinator
                from src.memery_external_sources import _queued_owned_thread
                pair=_validate_phase_pair_item(item,phase=phase,account_id=account_id)
                step=_account_pair_renewal_step(phase,item['job_id'])
                saved=((lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {})
                if saved.get('state')=='applied':
                    if saved.get('receipt')!={'state':'renewed','root_fence_id':item['root_fence_id'],
                                              'assistant_fence_id':item['assistant_fence_id']}:
                        raise HostSourceFenceUnavailable('paired renewal receipt changed')
                    continue
                async with mapping.async_chat_admission_lock(pair.root.chat_id) as guard:
                    capability.assert_current(provider=provider,action='renew',owner=new_owner)
                    coordinator=policy_coordinator()
                    if coordinator is None or coordinator.projection is not getattr(state,'memory_policy_projection',None):
                        raise HostSourceFenceUnavailable('paired policy unavailable')
                    refs=await _queued_owned_thread(coordinator.checked_refs,new_owner,account_id,target.memory_workspace)
                    def current():
                        try:
                            mapping.validate_chat_guard(pair.root.chat_id,guard)
                            capability.assert_current(provider=provider,action='renew',owner=new_owner)
                            return (authority.resolve(account_id)==target and canonical_source_provider(state) is provider
                                and policy_coordinator() is coordinator and coordinator.checked_refs(new_owner,account_id,target.memory_workspace)==refs
                                and _account_pair_read_allowed(source_authority,new_owner) is True)
                        except Exception: return False
                    await _account_pair_verify(pair,source_authority=source_authority,mapping=mapping,guard=guard,
                        current=current,provider_owner=new_owner)
                    transition,receipt_id,receipt_hash=_account_committed_transition(lifecycle.get_operation(operation_id),phase)
                    lifecycle.start_operation_step(operation_id,step,claim_token=claim_token)
                    request={'version':1,'transition':transition,'receipt_id':receipt_id,'receipt_hash':receipt_hash,
                        'workspace_id':target.memory_workspace,'job_id':item['job_id'],'source':asdict(pair.root),
                        'coverage':item['coverage'],'policy_fingerprint':item['policy_fingerprint'],
                        'root_fence_id':item['root_fence_id'],'assistant_fence_id':item['assistant_fence_id'],
                        'policy_projection':refs}
                    native=await provider._host_paired_rename_renew(request,before_issue=current)
                    await _account_pair_verify(pair,source_authority=source_authority,mapping=mapping,guard=guard,
                        current=current,provider_owner=new_owner)
                    if (native.get('state')!='renewed' or native.get('root_fence_id')!=item['root_fence_id']
                        or native.get('assistant_fence_id')!=item['assistant_fence_id']):
                        raise HostSourceFenceUnavailable('paired renewal unavailable')
                    lifecycle.checkpoint_delete_operation(operation_id,step,{'state':'renewed',
                        'root_fence_id':item['root_fence_id'],'assistant_fence_id':item['assistant_fence_id']},claim_token=claim_token)
                continue
            source = SourceRef(**item['source'])
            if {'revision':source.revision,'content_hash':source.content_hash} not in item['rename_proof']['active_versions']:
                continue
            step = _account_source_renewal_step(item['source_id'], source.revision, source.content_hash, item.get('origin_id'))
            saved = ((lifecycle.get_operation(operation_id).get('steps') or {}).get(step) or {})
            if saved.get('state') == 'applied':
                prior = saved.get('receipt') or {}
                if (prior.get('source_id') != item['source_id'] or prior.get('revision') != source.revision
                    or prior.get('content_hash') != source.content_hash or prior.get('state') not in ('renewed','withheld')):
                    raise HostSourceFenceUnavailable('rename source renewal receipt changed')
                if 'origin_id' in item and prior.get('state') == 'renewed':
                    if (prior.get('origin_id') != item['origin_id'] or type(prior.get('receipt_id')) is not str
                        or not prior['receipt_id'].startswith('cs_s06renew_') or type(prior.get('receipt_sha256')) is not str
                        or len(prior['receipt_sha256']) != 64 or any(c not in '0123456789abcdef' for c in prior['receipt_sha256'])):
                        raise HostSourceFenceUnavailable('rename observation receipt changed')
                continue
            async with mapping.async_chat_admission_lock(source.chat_id) as guard:
                capability.assert_current(provider=provider, action='renew', owner=new_owner)
                if authority.resolve(account_id) != target:
                    raise HostSourceFenceUnavailable('rename source target changed')
                lifecycle.start_operation_step(operation_id, step, claim_token=claim_token)
                retirement = await asyncio.to_thread(source_authority.retirement, source)
                fact = retirement.get('fact') if isinstance(retirement, dict) else None
                if retirement.get('state') == 'retired' and type(fact) is SourceRetirementFact and fact.source == source:
                    result = {'state':'withheld','source_id':item['source_id'],'revision':source.revision,
                              'content_hash':source.content_hash,'reason':fact.reason}
                else:
                    hydrated = await asyncio.to_thread(source_authority.verify_feedback_origin if 'origin_id' in item else source_authority.hydrate, source)
                    if retirement.get('state') != 'current' or hydrated.get('state') != 'available':
                        raise HostSourceFenceUnavailable('rename source revalidation unavailable')
                    if 'origin_id' in item:
                        native = await provider._host_observation_renew(_HostObservationRenew(owner=new_owner, workspace_id=target.memory_workspace,
                            original_owner=old_owner, operation_id=operation_id, origin_id=item['origin_id'], origin_hash=item['origin_hash'],
                            captured_owner=item['captured_owner'], prior_receipt_id=item['rebind_id'], prior_receipt_hash=item['rebind_hash'],
                            previous_fingerprint=item['current_fingerprint'], current_fingerprint=target.fingerprint, source=source,
                            rename_proof_id=item['rename_proof']['change_set_id']), capability=capability, target_authority=authority)
                    else:
                        native = await provider._host_source_renew(_HostSourceRenew(owner=new_owner, workspace_id=target.memory_workspace,
                            original_owner=old_owner, operation_id=operation_id, job_id=item['job_id'], source=source,
                            rename_proof_id=item['rename_proof']['change_set_id']), capability=capability)
                    mapping.validate_chat_guard(source.chat_id, guard)
                    capability.assert_current(provider=provider, action='renew', owner=new_owner)
                    if native.get('source_state') != 'active' or authority.resolve(account_id) != target:
                        # A later fence may never be undone merely by retrying
                        # this operation. Host source edits share this guard.
                        raise HostSourceFenceUnavailable('rename source revalidation changed')
                    result = {'state':'renewed','source_id':item['source_id'],'revision':source.revision,
                              'content_hash':source.content_hash,'native_state':native['state']}
                    if 'origin_id' in item:
                        result.update(origin_id=item['origin_id'], receipt_id=native['receipt_id'], receipt_sha256=native['receipt_sha256'])
                lifecycle.checkpoint_delete_operation(operation_id, step, result, claim_token=claim_token)
        after = page['next_after']
    # A proof may be on a different page from the exact revision's job. Never
    # report completion unless each originally active revision has a receipt.
    after = None
    for _ in range(summary['page_count']):
        operation = lifecycle.get_operation(operation_id)
        page = ((operation.get('steps') or {}).get(_account_source_page_step(after,phase)) or {}).get('receipt')
        for item in page['sources']:
            if item.get('kind') in ('pending_pair','terminal_pair','erased_pair'):
                if ((operation.get('steps') or {}).get(_account_pair_renewal_step(phase,item['kind']+':'+item['job_id'])) or {}).get('state')!='applied':
                    raise HostSourceFenceUnavailable('pending disposition unavailable')
                continue
            if item.get('kind')=='phase_observation':
                if item.get('deleted_chat') is True:
                    continue
                if ((operation.get('steps') or {}).get(_account_pair_renewal_step(phase,'observation:'+item['authority']['origin_id'])) or {}).get('state')!='applied':
                    raise HostSourceFenceUnavailable('phase observation renewal unavailable')
                continue
            if item.get('kind')=='phase_root':
                if item['state']=='fenced' and ((operation.get('steps') or {}).get(_account_pair_renewal_step(phase,'root:'+item['job_id'])) or {}).get('state')!='applied':
                    raise HostSourceFenceUnavailable('phase root renewal unavailable')
                continue
            if item.get('kind')=='paired':
                step=_account_pair_renewal_step(phase,item['job_id'])
                if ((operation.get('steps') or {}).get(step) or {}).get('state')!='applied':
                    raise HostSourceFenceUnavailable('paired renewal unavailable')
                continue
            for version in item['rename_proof']['active_versions']:
                step = _account_source_renewal_step(item['source_id'], version['revision'], version['content_hash'], item.get('origin_id'))
                if ((operation.get('steps') or {}).get(step) or {}).get('state') != 'applied':
                    raise HostSourceFenceUnavailable('rename active source revision unavailable')
        after = page['next_after']
    steps = lifecycle.get_operation(operation_id).get('steps') or {}
    receipts = [value.get('receipt') or {} for key,value in steps.items()
                if (key.startswith('host_source_renew_') or key.startswith('host_pair_renew_')) and isinstance(value,dict) and value.get('state') == 'applied']
    return {'state':'complete','renewed':sum(item.get('state') == 'renewed' for item in receipts),
            'withheld':sum(item.get('state') == 'withheld' for item in receipts)}


@dataclass(frozen=True)
class _HostChatFenceCapability:
    mapping: object
    guard: object
    provider: object
    authority: object
    binding: object
    account: str
    owner: str
    chat_id: str
    whole_chat: bool
    fenced_ids: tuple[str, ...] = ()

    def validate(self, *, provider, owner, chat_id, whole_chat=False):
        if (provider is not self.provider or owner != self.owner or chat_id != self.chat_id
            or (whole_chat and not self.whole_chat)):
            raise HostSourceFenceUnavailable('host source capability scope changed')
        self.mapping.validate_chat_guard(chat_id, self.guard)
        if self.authority.resolve(self.account) != self.binding:
            raise HostSourceFenceUnavailable('host source lifecycle changed')


async def retire_skill_draft_manifests(*, capability, archive, part=None):
    """Retire exact derived originals before a held chat or part erasure commits."""
    import asyncio
    from src.openclank.conversation_archive import ConversationArchive, get_conversation_archive
    if type(capability) is not _HostChatFenceCapability or type(archive) is not ConversationArchive or archive is not get_conversation_archive():
        raise HostSourceFenceUnavailable('skill draft retirement unavailable')
    cap=capability; provider=cap.provider
    def current():
        try:
            cap.validate(provider=provider,owner=cap.owner,chat_id=cap.chat_id,whole_chat=part is None)
            return cap.authority._auth.account_id(cap.owner)==cap.account
        except Exception: return False
    if not current(): raise HostSourceFenceUnavailable('skill draft retirement unavailable')
    selected_identity=None
    if part is not None:
        if (type(part) is not dict or set(part)!={'actor_id','message_id','part_id','revision'}
                or any(type(part[k]) is not str or not part[k] for k in ('actor_id','message_id','part_id'))
                or (part['revision'] is not None and (type(part['revision']) is not int or part['revision']<1))):
            raise HostSourceFenceUnavailable('skill draft part unavailable')
        identity=archive.private_part_retirement_identity(owner=cap.account,chat_id=cap.chat_id,**part)
        if identity is None: raise HostSourceFenceUnavailable('skill draft part preimage unavailable')
        # M5 selects actual main-actor user roots and qualified assistant tool parts.
        root=(identity['role']=='user' and identity['part_type']=='text' and identity['part_id']=='body')
        tool=(identity['role']=='assistant' and identity['part_type']=='tool')
        if identity['actor_id']!='main' or not (root or tool): return identity
        selected_identity=identity
        part={key:identity[key] for key in ('actor_id','message_id','part_id','revision','content_hash')}
    owner=cap.binding.memory_owner; workspace=cap.binding.memory_workspace
    op_raw=json.dumps(['openclank.skill-draft.retirement.v1',owner,cap.account,cap.chat_id,part],
        sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    request={'version':1,'owner':owner,'account_id':cap.account,'chat_id':cap.chat_id,
        'operation_id':'skillretire_'+hashlib.sha256(op_raw).hexdigest(),'part':part}
    request_sha=hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
    def accepted(value):
        if type(value) is not dict or type(value.get('version')) is not int or value.get('version')!=1 or value.get('operation_id')!=request['operation_id']:
            return False
        if value.get('state')=='absent':
            return set(value)=={'version','state','operation_id','retired_count'} and type(value['retired_count']) is int and value['retired_count']==0
        return (set(value)=={'version','state','operation_id','request_sha256','retired_count'}
            and value.get('state')=='retired' and value.get('request_sha256')==request_sha
            and type(value.get('retired_count')) is int and 0<=value['retired_count']<=10000)
    status={}; deadline=float(asyncio.get_running_loop().time()+10.0)
    try:
        result=await provider._host_candidate_request(owner,workspace,{'phase':'skill_draft_retire','request':request},
            require_live=True,write_deadline=deadline,write_status=status,before_issue=current)
    except Exception:
        ticket=status.get('ticket')
        if type(ticket) is not dict: raise HostSourceFenceUnavailable('skill draft retirement unavailable')
        try:
            snapshot=await provider._owned_original_checked_snapshot(ticket,'skill_draft_retire',request)
            read_ticket={}
            result=await provider._s07_owner_rpc(owner,workspace,{'phase':'skill_draft_retire_original','request':request},
                float(asyncio.get_running_loop().time()+5.0),read_ticket=read_ticket)
            await provider._owned_original_checked_current(ticket,'skill_draft_retire',request,snapshot)
            if read_ticket.get('transport') is not snapshot[1] or read_ticket.get('generation') is not snapshot[2]:
                raise ValueError('retirement read custody changed')
        except Exception as exc:
            raise HostSourceFenceUnavailable('skill draft retirement outcome unknown') from exc
    if not current() or not accepted(result):
        raise HostSourceFenceUnavailable('skill draft retirement outcome unknown')
    if selected_identity is not None:
        check=archive.private_part_retirement_identity(owner=cap.account,chat_id=cap.chat_id,
            actor_id=part['actor_id'],message_id=part['message_id'],part_id=part['part_id'],revision=part['revision'])
        if check!=selected_identity or not current():
            raise HostSourceFenceUnavailable('skill draft part changed')
        return selected_identity
    return None


def require_chat_source_capability(*, provider, owner, chat_id, capability):
    """Reject bypass only for the currently configured guarded local owner.

    Cross-provider retirement remains a prerequisite for activation.
    """
    from src.memery_provider import MemeryProvider
    if not isinstance(provider, MemeryProvider) or not provider._guarded_v17:
        return
    if type(capability) is not _HostChatFenceCapability:
        raise HostSourceFenceUnavailable('host source chat fence required')
    capability.validate(provider=provider, owner=owner, chat_id=chat_id, whole_chat=True)


def digest(body: str) -> str:
    if not isinstance(body, str):
        raise ValueError('host source body must be text')
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def available(db) -> bool:
    inspector = inspect(db.get_bind())
    if not inspector.has_table('host_source_schema_marker') or not inspector.has_table('host_source_delivery_intent'):
        return False
    columns = {column['name'] for column in inspector.get_columns('chat_messages')}
    if not {'source_body_revision', 'source_body_hash'}.issubset(columns):
        return False
    return db.execute(text("SELECT version FROM host_source_schema_marker")).scalar() in (1, 2, 3)


@dataclass(frozen=True)
class HostOriginAdmission:
    owner: str
    chat_id: str
    root_operation_id: str
    choices_json: str
    policy_fingerprint: str
    capture_eligible: bool


def origin_available(db):
    return available(db) and db.execute(text("SELECT version FROM host_source_schema_marker")).scalar() in (2, 3)


def _check_owner(db, archive, owner, chat_id):
    row = db.execute(text('SELECT owner FROM sessions WHERE id=:chat'), {'chat': chat_id}).first()
    if not row or archive.resolve_owner(row[0]) != owner:
        raise ValueError('host source authority unavailable')


def enqueue_new(db, *, archive, owner, chat_id, message_id, body, origin=None):
    _check_owner(db, archive, owner, chat_id)
    value = digest(body)
    result = db.execute(text("UPDATE chat_messages SET source_body_revision=1, source_body_hash=:hash WHERE id=:message AND session_id=:chat AND source_body_revision IS NULL"), {'hash': value, 'message': message_id, 'chat': chat_id})
    if result.rowcount != 1:
        raise ValueError('host source baseline unavailable')
    _intent(db, owner, chat_id, message_id, 1, value)
    if origin is not None and origin_available(db):
        if type(origin) is not HostOriginAdmission or (origin.owner, origin.chat_id, origin.root_operation_id) != (owner, chat_id, message_id):
            raise ValueError('host origin identity unavailable')
        choices = json.loads(origin.choices_json)
        if not isinstance(choices, dict) or any(type(choices.get(k)) is not bool for k in ('incognito', 'noMemory', 'compareMode')) or type(origin.capture_eligible) is not bool or len(origin.policy_fingerprint) != 64 or any(c not in '0123456789abcdef' for c in origin.policy_fingerprint):
            raise ValueError('host origin evidence unavailable')
        db.execute(text("UPDATE host_source_delivery_intent SET origin_json=:origin,origin_state='pending' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=1"), {'origin': json.dumps(asdict(origin),sort_keys=True,separators=(',',':')), 'owner': owner, 'chat': chat_id, 'message': message_id})


def _intent(db, owner, chat_id, message_id, revision, value):
    db.execute(text("INSERT INTO host_source_delivery_intent(owner,chat_id,message_id,part_id,revision,content_hash,state) VALUES (:owner,:chat,:message,'body',:revision,:hash,'pending')"), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision, 'hash': value})


def edit(db, *, archive, owner, chat_id, message_id, expected_revision, body):
    _check_owner(db, archive, owner, chat_id)
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError('expected source revision required')
    value = digest(body)
    row = db.execute(text('SELECT content, source_body_revision, source_body_hash FROM chat_messages WHERE id=:message AND session_id=:chat'), {'message': message_id, 'chat': chat_id}).first()
    if not row or row[1] != expected_revision or not row[2] or digest(row[0]) != row[2]:
        raise ValueError('host source changed or baseline unavailable')
    changed = db.execute(text('UPDATE chat_messages SET content=:body, source_body_revision=:next, source_body_hash=:hash WHERE id=:message AND session_id=:chat AND source_body_revision=:expected AND source_body_hash=:previous'), {'body': body, 'next': expected_revision + 1, 'hash': value, 'message': message_id, 'chat': chat_id, 'expected': expected_revision, 'previous': row[2]})
    if changed.rowcount != 1:
        raise ValueError('host source changed or baseline unavailable')
    _intent(db, owner, chat_id, message_id, expected_revision + 1, value)
    db.execute(text("UPDATE sessions SET transcript_revision=COALESCE(transcript_revision,0)+1 WHERE id=:chat"), {"chat": chat_id})
    return expected_revision + 1


async def _verified_erased_pair(*, projection, mapping, guard, provider, account_id, current):
    """Join a private candidate to exact current archive and Native terminal facts."""
    import asyncio
    from dataclasses import replace
    from core.database import SessionLocal
    from src.memery_provider import _HostPairedSourceProjection
    from src.memery_external_sources import _typed_assistant_component_matches, _assistant_ref_error
    from src.openclank.conversation_archive import get_conversation_archive
    if (type(projection) is not _HostPairedSourceProjection or _assistant_ref_error(projection.source)
            or projection.source.root.account_id!=account_id or not callable(current)):
        raise HostSourceFenceUnavailable('paired erased source unavailable')
    chat=projection.source.root.chat_id;archive=get_conversation_archive()
    def verify():
        if current() is not True: raise ValueError('paired erased authority changed')
        mapping.validate_chat_guard(chat,guard)
        with SessionLocal() as db:
            component=assistant_component(db,archive,owner=account_id,provider_owner=mapping.owner,chat_id=chat,
                message_id=projection.source.root.message_id,current_authority=current)
        if not _typed_assistant_component_matches(projection.source,component) or current() is not True:
            raise ValueError('paired erased source changed')
        mapping.validate_chat_guard(chat,guard)
    async def joined_verify():
        task=asyncio.create_task(asyncio.to_thread(verify))
        try: await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result();raise
    try:
        await joined_verify()
        await provider._host_paired_erased_fact(projection,account_id=account_id,current=current)
        await joined_verify()
    except Exception as exc:
        raise HostSourceFenceUnavailable('paired erased source unavailable') from exc
    return replace(projection,transition_class='erased')


async def _fence_current_paired_source(*, projection, mapping, guard, provider, target_authority,
                                       target_binding, account_id, chat_id, message_id):
    """Fence the exact still-current pair before its guarded Host mutation."""
    import asyncio
    from core.database import SessionLocal
    from src.memery_provider import _HostPairedSourceProjection
    from src.memery_external_sources import _typed_assistant_component_matches, _assistant_ref_error
    from src.openclank.conversation_archive import get_conversation_archive
    if (type(projection) is not _HostPairedSourceProjection or _assistant_ref_error(projection.source)
            or projection.source.root.account_id != account_id or projection.source.root.chat_id != chat_id
            or message_id not in (projection.source.root.message_id, projection.source.assistant_message_id)
            or projection.owner != target_binding.memory_owner or projection.workspace_id != target_binding.memory_workspace):
        raise HostSourceFenceUnavailable('paired source identity unavailable')
    archive = get_conversation_archive()
    owner = mapping.owner
    auth = target_authority._auth
    def current():
        try:
            mapping.validate_chat_guard(chat_id, guard)
            return (auth.account_id(owner) == account_id and auth.username_for_account_id(account_id) == owner
                    and target_authority.resolve(account_id) == target_binding)
        except Exception:
            return False
    def verify():
        if current() is not True:
            raise ValueError('paired source authority changed')
        with SessionLocal() as db:
            component = assistant_component(db, archive, owner=account_id, provider_owner=owner, chat_id=chat_id,
                message_id=projection.source.root.message_id, current_authority=current)
        if not _typed_assistant_component_matches(projection.source, component) or current() is not True:
            raise ValueError('paired source changed')
    async def joined_verify():
        task = asyncio.create_task(asyncio.to_thread(verify))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result()
            raise
    await joined_verify()
    if projection.transition_class=='erased':
        await provider._host_paired_erased_fact(projection,account_id=account_id,current=current)
    if message_id == projection.source.root.message_id:
        root_receipt = await provider._host_pair_root_fence(projection)
        if (not isinstance(root_receipt, dict) or root_receipt.get('visibility') != 'closed'
                or root_receipt.get('state') not in ('fenced','already_fenced','absent_fenced')
                or not _source_fence_covered(root_receipt)):
            raise HostSourceFenceUnavailable('paired root source fence unavailable')
        await joined_verify()
        if projection.transition_class=='erased':
            await provider._host_paired_erased_fact(projection,account_id=account_id,current=current)
    receipt = await provider._host_pair_source_fence(projection)
    await joined_verify()
    if (type(receipt) is not dict or set(receipt) != {'version','state'} or type(receipt['version']) is not int
            or receipt['version'] != 1 or receipt['state'] not in ('fenced','already_fenced','absent_fenced')):
        raise HostSourceFenceUnavailable('paired source fence unavailable')
    return receipt


async def fence_retired_paired_source(*, projection, lifecycle_owner, guard):
    """Fence only a positively verified retired pair; no job-settlement claim."""
    import asyncio
    from src.memery_provider import _HostPairedSourceProjection
    from src.memery_external_sources import AssistantInterpretationRetirementFact, _assistant_ref_error
    if (type(projection) is not _HostPairedSourceProjection or type(lifecycle_owner) is not CompletedTurnLifecycleOwner
            or _assistant_ref_error(projection.source) or lifecycle_owner.provider is None):
        raise HostSourceFenceUnavailable('paired retirement unavailable')
    source = lifecycle_owner.source_authority
    ref = projection.source
    def verify():
        result = source.assistant_interpretation_retirement(ref, lifecycle_owner=lifecycle_owner, guard=guard)
        fact = result.get('fact') if type(result) is dict and result.get('state') == 'retired' else None
        if type(fact) is not AssistantInterpretationRetirementFact or fact.source != ref:
            raise HostSourceFenceUnavailable('paired retirement unavailable')
        target = lifecycle_owner.target_authority.resolve(ref.root.account_id)
        if (target != lifecycle_owner.binding or target.memory_owner != projection.owner
                or target.memory_workspace != projection.workspace_id):
            raise HostSourceFenceUnavailable('paired retirement target changed')
        return fact
    async def joined_verify():
        task = asyncio.create_task(asyncio.to_thread(verify))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            task.result()
            raise
    fact = await joined_verify()
    if fact.reason.startswith('root_'):
        root = await lifecycle_owner.provider._host_pair_root_fence(projection)
        if (not isinstance(root, dict) or root.get('visibility') != 'closed'
                or root.get('state') not in ('fenced','already_fenced','absent_fenced')
                or not _source_fence_covered(root)):
            raise HostSourceFenceUnavailable('paired root retirement unavailable')
        if await joined_verify() != fact:
            raise HostSourceFenceUnavailable('paired retirement changed')
    result = await lifecycle_owner.provider._host_pair_source_fence(projection)
    if (type(result) is not dict or set(result) != {'version','state'} or type(result['version']) is not int
            or result['version'] != 1 or result['state'] not in ('fenced','already_fenced','absent_fenced')
            or await joined_verify() != fact):
        raise HostSourceFenceUnavailable('paired retirement unavailable')
    return result


async def _fence_reset_paired_source(*, projection, mapping, guard, provider, target_authority, claim, closure):
    """One manifest entry, with independent no-replay contributor progress."""
    import asyncio, os
    from services.memory.nuke_coordinator import MemoryResetClaim
    from services.memory.skill_lifecycle import locked
    from src.memery_provider import _HostPairedSourceProjection
    if type(projection) is not _HostPairedSourceProjection or type(claim) is not MemoryResetClaim or closure is None or closure.lease.claim is not claim:
        raise HostSourceFenceUnavailable('paired reset authority unavailable')
    coordinator=claim._coordinator; lease=closure.lease; digest=_reset_ref_digest(projection)
    def transition(action, part=None, receipt=None):
        claim.assert_current(owner=claim.owner,account_id=claim.account_id,provider=provider,operation_id=claim.operation_id)
        with locked(os.fspath(coordinator.lock_target)):
            operations=coordinator._load_journal(); row=operations.get(claim.operation_id)
            manifest=row.get('reset_source_manifest') if type(row) is dict else None
            records=row.get('reset_source_receipts') if type(row) is dict else None
            if (type(manifest) is not dict or type(records) is not dict
                    or not any(digest in root.get('refs',[]) for root in manifest.get('roots',[]) if type(root) is dict)):
                raise HostSourceFenceUnavailable('paired reset manifest unavailable')
            previous=records.get(digest)
            if action=='open':
                if type(previous) is dict and previous.get('state')=='complete':
                    if type(previous.get('receipt')) is not dict: raise HostSourceFenceUnavailable('paired reset receipt unavailable')
                    return previous['receipt']
                if previous is None:
                    remaining=getattr(lease,'reset_invocation_remaining',None); attempts=row.get('reset_fence_attempts')
                    if type(remaining) is not int or remaining<1 or type(attempts) is not int or attempts<0:
                        raise HostSourceFenceUnavailable('paired reset invocation deferred')
                    previous={'state':'started','root':'unissued','assistant':'unissued'}
                    records[digest]=previous; row['reset_fence_attempts']=attempts+1
                    coordinator._save_journal(operations); lease.reset_invocation_remaining=remaining-1
                elif previous == {'state':'started'}:
                    # An older ambiguous marker carries no proof that either
                    # contributor was unissued. Preserve read-only recovery.
                    previous={'state':'started','root':'unknown','assistant':'unknown'}
                    records[digest]=previous; coordinator._save_journal(operations)
                if (type(previous) is not dict or set(previous)!={'state','root','assistant'} or previous['state']!='started'
                        or previous['root'] not in ('unissued','unknown','confirmed')
                        or previous['assistant'] not in ('unissued','unknown','confirmed')
                        or (previous['assistant']=='confirmed' and previous['root']!='confirmed')):
                    raise HostSourceFenceUnavailable('paired reset progress unavailable')
                return dict(previous)
            if (type(previous) is not dict or set(previous)!={'state','root','assistant'} or previous['state']!='started'
                    or previous['root'] not in ('unissued','unknown','confirmed')
                    or previous['assistant'] not in ('unissued','unknown','confirmed')):
                raise HostSourceFenceUnavailable('paired reset progress changed')
            if action=='finish':
                if previous['root']!='confirmed' or previous['assistant']!='confirmed' or type(receipt) is not dict:
                    raise HostSourceFenceUnavailable('paired reset postcondition unavailable')
                records[digest]={'state':'complete','receipt':receipt}
            elif part in ('root','assistant') and action=='arm':
                if previous[part]!='unissued' or (part=='assistant' and previous['root']!='confirmed'):
                    raise HostSourceFenceUnavailable('paired reset write unavailable')
                previous[part]='unknown'
            elif part in ('root','assistant') and action=='not_sent':
                if previous[part]!='unknown': raise HostSourceFenceUnavailable('paired reset progress changed')
                previous[part]='unissued'
            elif part in ('root','assistant') and action=='confirm':
                if previous[part] not in ('unknown','confirmed'):
                    raise HostSourceFenceUnavailable('paired reset progress changed')
                previous[part]='confirmed'
            else:
                raise HostSourceFenceUnavailable('paired reset progress unavailable')
            coordinator._save_journal(operations)
            return dict(records[digest])
    async def io(action, write_status=None):
        return await provider._host_pair_reset_io(projection,claim=claim,mapping=mapping,guard=guard,
            target_authority=target_authority,closure=closure,action=action,write_status=write_status)
    async def read(part):
        result=await io(part+'_read')
        if part=='root':
            if (not isinstance(result,dict) or result.get('visibility')!='closed'
                    or result.get('state') not in ('already_fenced','absent_fenced') or not _source_fence_covered(result)):
                raise HostSourceFenceUnavailable('paired root postcondition unavailable')
        elif (type(result) is not dict or set(result)!={'version','state'} or type(result['version']) is not int
                or result['version']!=1 or result['state'] not in ('already_fenced','absent_fenced')):
            raise HostSourceFenceUnavailable('paired assistant postcondition unavailable')
        return result
    async def run():
        binding=await asyncio.to_thread(provider._reset_fence_binding,claim)
        if set(binding.get('components',[])) != {'memories','graph','ingest'}:
            raise HostSourceFenceUnavailable('paired reset scope unavailable')
        progress=await asyncio.to_thread(transition,'open')
        if progress.get('state')!='started':
            root=await read('root'); assistant=await read('assistant')
            if (progress.get('version')!=1 or type(progress.get('root')) is not dict or type(progress.get('assistant')) is not dict
                    or any(progress['root'].get(k)!=root.get(k) for k in ('version','source_id','visibility','covered'))
                    or progress['assistant'].get('version')!=assistant['version']):
                raise HostSourceFenceUnavailable('paired reset postcondition changed')
            return {'version':1,'root':root,'assistant':assistant}
        for part in ('root','assistant'):
            if progress[part]=='unissued':
                progress=await asyncio.to_thread(transition,'arm',part)
                status={'ticket':None}
                try:
                    await io(part+'_write',status)
                    lease.touched=True
                except BaseException:
                    ticket=status.get('ticket')
                    if ticket is not None and ticket.get('issued') is True:
                        lease.touched=True
                    else:
                        await asyncio.to_thread(transition,'not_sent',part)
                    raise
            await read(part)
            progress=await asyncio.to_thread(transition,'confirm',part)
        # Both current native postconditions must hold before this single
        # manifest entry can be completed.
        root=await read('root'); assistant=await read('assistant')
        receipt={'version':1,'root':root,'assistant':assistant}
        await asyncio.to_thread(transition,'finish',receipt=receipt)
        return receipt
    task=asyncio.create_task(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
        task.result()
        raise


async def fence_projected_source(*, mapping, guard, provider, target_authority, account_id, chat_id, message_id, reset_claim=None, reset_closure=None, reset_manifest_refs=None):
    """Fence existing FM projections under the caller's exact live chat guard.

    The caller must keep the same guard through its later host transition.
    Native receipts prove scoped visibility only, not physical erasure.
    """
    import asyncio
    mapping.validate_chat_guard(chat_id, guard)
    def resolve():
        return target_authority.resolve(account_id) if reset_closure is None else target_authority._resolve_stopped_reset(account_id,closure=reset_closure,claim=reset_claim)
    async def validate_closure():
        if reset_closure is not None:
            pool=target_authority._supervisor
            if not pool.queued_reset_closure_current(reset_closure,claim=reset_claim): raise HostSourceFenceUnavailable('stopped reset authority changed')
            await pool.validate_queued_reset_closure(reset_closure,claim=reset_claim)
            if not pool.queued_reset_closure_current(reset_closure,claim=reset_claim): raise HostSourceFenceUnavailable('stopped reset authority changed')
    await validate_closure()
    target_binding=resolve()
    def erased_current():
        try:
            mapping.validate_chat_guard(chat_id,guard)
            if resolve()!=target_binding: return False
            return reset_closure is None or target_authority._supervisor.queued_reset_closure_current(reset_closure,claim=reset_claim)
        except Exception:return False
    async def erased_join(projection):
        return await _verified_erased_pair(projection=projection,mapping=mapping,guard=guard,provider=provider,
            account_id=account_id,current=erased_current)
    paired_profile = None
    if reset_claim is None:
        try:
            paired_profile = await provider._host_paired_profile_state()
        except Exception as exc:
            raise HostSourceFenceUnavailable('paired source profile unavailable') from exc
        if paired_profile not in ('ready', 'absent'):
            raise HostSourceFenceUnavailable('paired source profile unavailable')
        mapping.validate_chat_guard(chat_id, guard)
        if resolve() != target_binding:
            raise HostSourceFenceUnavailable('host source target changed')
    enumerate_refs = getattr(provider, "_host_source_refs", None)
    fence = getattr(provider, "_host_source_fence", None)
    if not callable(enumerate_refs) or not callable(fence):
        raise HostSourceFenceUnavailable('host source projection unavailable')
    count = 0;seen=[]
    extra={'reset_claim':reset_claim,'reset_closure':reset_closure} if reset_closure is not None else {}
    async for refs, _ in _source_inventory_pages(enumerate_refs, account_id=account_id, chat_id=chat_id,
            message_id=message_id, target_authority=target_authority, _erased_join=erased_join,
            _erased_current=erased_current, **extra):
        await validate_closure()
        mapping.validate_chat_guard(chat_id, guard)
        for ref in refs:
            from src.memery_provider import _HostPairedSourceProjection
            if type(ref) is _HostPairedSourceProjection:
                if reset_claim is not None or reset_closure is not None or reset_manifest_refs is not None:
                    if reset_claim is None or reset_closure is None or reset_manifest_refs is None:
                        raise HostSourceFenceUnavailable('paired reset source unavailable')
                    digest=_reset_ref_digest(ref)
                    if digest not in reset_manifest_refs or digest in seen:
                        raise HostSourceFenceUnavailable('paired reset source changed')
                    await _fence_reset_paired_source(projection=ref,mapping=mapping,guard=guard,provider=provider,
                        target_authority=target_authority,claim=reset_claim,closure=reset_closure)
                    seen.append(digest)
                    await validate_closure()
                else:
                    await _fence_current_paired_source(projection=ref, mapping=mapping, guard=guard, provider=provider,
                        target_authority=target_authority, target_binding=target_binding, account_id=account_id,
                        chat_id=chat_id, message_id=message_id)
                count += 1
                continue
            if reset_manifest_refs is not None:
                digest=_reset_ref_digest(ref)
                if digest not in reset_manifest_refs or digest in seen:
                    raise HostSourceFenceUnavailable('reset native source changed')
                seen.append(digest)
            try:
                if reset_claim is None:
                    receipt = await fence(ref)
                else:
                    receipt = await fence(ref, reset_claim=reset_claim, mapping=mapping, guard=guard, target_authority=target_authority, **({'reset_closure':reset_closure} if reset_closure is not None else {}))
                await validate_closure()
            except Exception as exc:
                raise HostSourceFenceUnavailable('host source fence unavailable') from exc
            mapping.validate_chat_guard(chat_id, guard)
            if (not isinstance(receipt, dict) or receipt.get('state') not in ('fenced', 'already_fenced', 'absent_fenced')
                or receipt.get('visibility') != 'closed'
                or not _source_fence_covered(receipt)):
                raise HostSourceFenceUnavailable('host source projection fence unavailable')
            count += 1
    if paired_profile == 'ready':
        from src.memery_provider import _HostPairedSourceProjection
        async for refs, _ in _source_inventory_pages(provider._host_pair_assistant_refs, account_id=account_id,
                chat_id=chat_id, message_id=message_id, target_authority=target_authority,
                _erased_join=erased_join,_erased_current=erased_current):
            mapping.validate_chat_guard(chat_id, guard)
            for ref in refs:
                if type(ref) is not _HostPairedSourceProjection:
                    raise HostSourceFenceUnavailable('paired source inventory unavailable')
                await _fence_current_paired_source(projection=ref, mapping=mapping, guard=guard, provider=provider,
                    target_authority=target_authority, target_binding=target_binding, account_id=account_id,
                    chat_id=chat_id, message_id=message_id)
                count += 1
    if paired_profile is not None:
        try:
            if await provider._host_paired_profile_state() != paired_profile:
                raise HostSourceFenceUnavailable('paired source profile changed')
        except Exception as exc:
            raise HostSourceFenceUnavailable('paired source profile unavailable') from exc
        mapping.validate_chat_guard(chat_id, guard)
        if resolve() != target_binding:
            raise HostSourceFenceUnavailable('host source target changed')
    if reset_manifest_refs is not None and sorted(seen)!=sorted(reset_manifest_refs):
        raise HostSourceFenceUnavailable('reset native source inventory changed')
    # Pending compaction references are not FM jobs yet. Retire these exact
    # source-linked rows under the same live guard before the host mutation.
    from src.openclank.conversation_archive import get_conversation_archive
    archive=get_conversation_archive()
    def current():
        try:
            mapping.validate_chat_guard(chat_id,guard)
            if reset_closure is not None: target_authority._supervisor.assert_queued_reset_closure(reset_closure,claim=reset_claim)
            if reset_closure is not None and (not provider._reset_retirement_ready_local() or provider._reset_retirement_witness is not reset_closure.lease.reset_transport_witness): return False
            return resolve()==target_binding
        except Exception:
            return False
    task=asyncio.create_task(asyncio.to_thread(archive.retire_trusted_maintenance_pending,
        account_id=account_id,chat_id=chat_id,message_id=message_id,current=current))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try: await asyncio.shield(task)
            except asyncio.CancelledError: continue
        task.result()
        raise
    except Exception as exc:
        raise HostSourceFenceUnavailable('maintenance source retirement unavailable') from exc
    mapping.validate_chat_guard(chat_id,guard)
    await validate_closure()
    if resolve()!=target_binding:
        raise HostSourceFenceUnavailable('host source target changed')
    if paired_profile is not None:
        try:
            if await provider._host_paired_profile_state() != paired_profile:
                raise HostSourceFenceUnavailable('paired source profile changed')
        except Exception as exc:
            raise HostSourceFenceUnavailable('paired source profile unavailable') from exc
        mapping.validate_chat_guard(chat_id, guard)
        if resolve() != target_binding:
            raise HostSourceFenceUnavailable('host source target changed')
    # The typed inventory includes S05 refs and qualified observation origins;
    # it does not assert anything about arbitrary direct SQL writes.
    return {'state': 'inventory_empty' if not count else 'fenced',
            'count': count, 'derived_closure': 'closed'}


def _source_inventory_key(ref):
    from src.memery_provider import _HostSourceProjection, _HostPairedSourceProjection
    if type(ref) in (_HostSourceProjection, _HostPairedSourceProjection):
        return 'j:' + ref.job_id
    try:
        from src.memery_provider import _HostObservationProjection
        if type(ref) is _HostObservationProjection:
            return 'o:' + ref.origin_id
    except ImportError:
        pass  # Older S04-only providers have no observation-origin capability.
    raise HostSourceFenceUnavailable('host source inventory type unavailable')


def _source_fence_covered(receipt):
    old = ['candidate','curated','knowledge','graph','digest','scoped_quality','reindex']
    supported=(old,old+['feedback'],old+['feedback','admitted'],old+['feedback','admitted','context_admitted'])
    return isinstance(receipt,dict) and receipt.get('covered') in (*supported,*(item+['maintenance'] for item in supported[1:]))


async def _source_inventory_pages(enumerate_refs, *, _erased_join=None, _erased_current=None, **scope):
    """Yield bounded keyset pages; private candidates require an exact guarded join."""
    import asyncio
    from src.memery_provider import _HOST_ERASED_INVENTORY, _HostPairedErasedCandidate, _HostPairedSourceProjection
    if (_erased_join is None)!=(_erased_current is None) or (_erased_join is not None and (not callable(_erased_join) or not callable(_erased_current))):
        raise HostSourceFenceUnavailable('paired inventory guard unavailable')
    after = None
    while True:
        try:
            kwargs=dict(scope,after_job_id=after,limit=128)
            if _erased_join is not None: kwargs['_erased_token']=_HOST_ERASED_INVENTORY
            page = await asyncio.to_thread(enumerate_refs, **kwargs)
        except Exception as exc:
            raise HostSourceFenceUnavailable('host source inventory unavailable') from exc
        if type(page) is not tuple or len(page) != 2 or type(page[0]) is not tuple or len(page[0]) > 128:
            raise HostSourceFenceUnavailable('host source inventory unavailable')
        refs, next_after = page
        if any(type(ref) is _HostPairedErasedCandidate for ref in refs):
            if _erased_join is None or _erased_current() is not True:
                raise HostSourceFenceUnavailable('paired erased inventory unavailable')
            verified=[]
            for ref in refs:
                if type(ref) is _HostPairedErasedCandidate:
                    result=await _erased_join(ref.projection)
                    if (type(result) is not _HostPairedSourceProjection or result.transition_class!='erased'
                            or result.owner!=ref.projection.owner or result.workspace_id!=ref.projection.workspace_id
                            or result.job_id!=ref.projection.job_id or result.source!=ref.projection.source
                            or result.policy_fingerprint!=ref.projection.policy_fingerprint or result.input_sha256!=ref.projection.input_sha256):
                        raise HostSourceFenceUnavailable('paired erased inventory changed')
                    verified.append(result)
                else: verified.append(ref)
            try: again=await asyncio.to_thread(enumerate_refs, **kwargs)
            except Exception as exc: raise HostSourceFenceUnavailable('paired erased inventory changed') from exc
            if again!=page or _erased_current() is not True:
                raise HostSourceFenceUnavailable('paired erased inventory changed')
            refs=tuple(verified)
        if next_after is not None and type(next_after) is not str:
            raise HostSourceFenceUnavailable('host source inventory cursor unavailable')
        keys = [_source_inventory_key(ref) for ref in refs]
        normalized_after = ('j:' + after) if after and not after.startswith(('j:', 'o:')) else after
        normalized_next = ('j:' + next_after) if next_after and not next_after.startswith(('j:', 'o:')) else next_after
        if (keys != sorted(keys) or len(keys) != len(set(keys))
            or (normalized_after is not None and any(key <= normalized_after for key in keys))
            or (next_after is not None and (type(next_after) is not str
                or (normalized_next != 'o:' and (not keys or normalized_next != keys[-1]))
                or (normalized_next == 'o:' and normalized_after is not None and normalized_after.startswith('o:'))))):
            raise HostSourceFenceUnavailable('host source inventory cursor unavailable')
        yield refs, next_after
        if next_after is None:
            return
        after = next_after


def guarded_source_mutation(request=None, *, state=None, owner, chat_id, message_ids=(), whole_chat=False, pre_fence=None, async_guard=None, _bulk=False, _pre_fence_offloop=False):
    """Guard current local guarded-FM source transitions without startup.

    This adapter is inactive for other providers. Cross-provider retirement
    must be joined before host-source publication can be activated.
    """
    from contextlib import asynccontextmanager, AsyncExitStack

    @asynccontextmanager
    async def guard_context():
        app_state = state if state is not None else request.app.state
        provider = canonical_source_provider(app_state)
        if provider is None:
            async with AsyncExitStack() as stack:
                if async_guard is not None:
                    await stack.enter_async_context(async_guard)
                if pre_fence is not None:
                    if _pre_fence_offloop:
                        import asyncio
                        await asyncio.to_thread(pre_fence)
                    else:
                        pre_fence()
                yield
            return
        from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
        from src.openclank.memory_job_admission import TargetAuthority
        from src.openclank.session_map import OwnerSessionMap
        supervisor = getattr(app_state, 'mimo_supervisor', None)
        auth = getattr(app_state, 'auth_manager', None)
        if (not isinstance(supervisor, AcpSupervisorAdapter) or auth is None or type(owner) is not str or not owner
            or type(chat_id) is not str or not chat_id or type(message_ids) not in (tuple, list)
            or type(_bulk) is not bool or (not _bulk and len(message_ids) > 128)
            or any(type(item) is not str or not item or len(item) > 512 for item in message_ids)):
            raise HostSourceFenceUnavailable('host source authority unavailable')
        try:
            pool = supervisor.owner_lifecycle_pool()
            account = auth.account_id(owner)
            authority = TargetAuthority(auth_manager=auth, supervisor=pool)
            if not account or auth.username_for_account_id(account) != owner:
                raise ValueError('host source owner unavailable')
            binding = authority.resolve(account)
        except Exception as exc:
            raise HostSourceFenceUnavailable('host source authority unavailable') from exc
        mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
        async with mapping.async_chat_admission_lock(chat_id) as acquired:
            async with AsyncExitStack() as stack:
                if async_guard is not None:
                    await stack.enter_async_context(async_guard)
                try:
                    if auth.account_id(owner) != account or authority.resolve(account) != binding:
                        raise ValueError('host source authority changed')
                except Exception as exc:
                    raise HostSourceFenceUnavailable('host source authority changed') from exc
                if pre_fence is not None:
                    if _pre_fence_offloop:
                        import asyncio
                        await asyncio.to_thread(pre_fence)
                    else:
                        pre_fence()
                if whole_chat:
                    import asyncio
                    try:
                        def erased_current():
                            try:
                                mapping.validate_chat_guard(chat_id,acquired)
                                return (auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                                    and authority.resolve(account)==binding and canonical_source_provider(app_state) is provider)
                            except Exception:return False
                        async def erased_join(projection):
                            return await _verified_erased_pair(projection=projection,mapping=mapping,guard=acquired,provider=provider,
                                account_id=account,current=erased_current)
                        async for refs, _ in _source_inventory_pages(provider._host_chat_source_refs,
                                account_id=account, chat_id=chat_id, target_authority=authority,
                                _erased_join=erased_join,_erased_current=erased_current):
                            for ref in refs:
                                from src.memery_provider import _HostPairedSourceProjection
                                if type(ref) is _HostPairedSourceProjection:
                                    await _fence_current_paired_source(projection=ref,mapping=mapping,guard=acquired,
                                        provider=provider,target_authority=authority,target_binding=binding,
                                        account_id=account,chat_id=chat_id,message_id=ref.source.root.message_id)
                                    continue
                                receipt = await provider._host_source_fence(ref)
                                mapping.validate_chat_guard(chat_id, acquired)
                                if (not isinstance(receipt, dict) or receipt.get('visibility') != 'closed'
                                    or receipt.get('state') not in ('fenced','already_fenced','absent_fenced')
                                    or not _source_fence_covered(receipt)):
                                    raise ValueError('host source fence unavailable')
                    except Exception as exc:
                        raise HostSourceFenceUnavailable('host source chat fence unavailable') from exc
                else:
                    for message_id in dict.fromkeys(message_ids):
                        await fence_projected_source(mapping=mapping, guard=acquired, provider=provider,
                            target_authority=authority, account_id=account, chat_id=chat_id, message_id=message_id)
                try:
                    if auth.account_id(owner) != account or authority.resolve(account) != binding:
                        raise ValueError('host source authority changed')
                except Exception as exc:
                    raise HostSourceFenceUnavailable('host source authority changed') from exc
                yield _HostChatFenceCapability(mapping, acquired, provider, authority, binding, account, owner, chat_id, whole_chat, tuple(dict.fromkeys(message_ids)))
    return guard_context()


def require_root_veto_source(db, archive, *, owner, chat_id, root_operation_id):
    """Validate the canonical root intent without mutating it."""
    _check_owner(db, archive, owner, chat_id)
    if not origin_available(db):
        raise ValueError('privacy veto schema unavailable')
    values = {'owner': owner, 'chat': chat_id, 'message': root_operation_id}
    row = db.execute(text("SELECT i.origin_state,i.content_hash,m.content,m.source_body_revision,m.source_body_hash,m.role FROM host_source_delivery_intent i JOIN chat_messages m ON m.id=i.message_id AND m.session_id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.message_id=:message AND i.part_id='body' AND i.revision=1 AND i.origin_json IS NOT NULL"), values).first()
    allowed = ('pending','delivered','superseded','vetoed','projection_user_dropped','projection_assistant_dropped','projection_both_dropped')
    if row:
        if row[0] not in allowed or type(row[3]) is not int or row[3] < 1 or not row[4] or row[5] != 'user' or digest(row[2]) != row[4] or (row[3] == 1 and row[4] != row[1]):
            raise ValueError('privacy veto source unavailable')
    else:
        root = db.execute(text("SELECT content_hash FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_json IS NOT NULL"), values).scalar()
        if not root or source_component_state(db,archive,owner=owner,chat_id=chat_id,message_id=root_operation_id,content_hash=root) != 'projection_dropped':
            raise ValueError('privacy veto source unavailable')
    return values


def veto_root_capture(db, archive, *, owner, chat_id, root_operation_id):
    """Permanently veto capture for an exact current root source receipt."""
    if db.in_transaction():
        raise ValueError('privacy veto requires a fresh transaction')
    if db.get_bind().dialect.name != 'sqlite':
        raise ValueError('privacy veto backend unavailable')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    values = require_root_veto_source(db, archive, owner=owner, chat_id=chat_id, root_operation_id=root_operation_id)
    result = db.execute(text("UPDATE host_source_delivery_intent SET origin_state='vetoed' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_state IN ('pending','delivered','superseded','vetoed','projection_user_dropped','projection_assistant_dropped','projection_both_dropped')"), values)
    if result.rowcount != 1:
        raise ValueError('privacy veto source changed')
    db.commit()
    return True


_PROJECTION_USER = frozenset(('projection_user_dropped', 'projection_both_dropped'))
_PROJECTION_ASSISTANT = frozenset(('projection_assistant_dropped', 'projection_both_dropped'))


def source_component_state(db, archive, *, owner, chat_id, message_id, content_hash, component='user'):
    """Exact committed host proof; archive validation remains a separate check."""
    if not origin_available(db) or component not in ('user', 'assistant'):
        return 'unavailable'
    row = db.execute(text("SELECT i.origin_state,i.origin_completed,i.content_hash,i.origin_assistant_id,i.state,s.owner FROM host_source_delivery_intent i JOIN sessions s ON s.id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.message_id=:message AND i.part_id='body' AND i.revision=1"),
        {'owner':owner,'chat':chat_id,'message':message_id}).first()
    if (not row or archive.resolve_owner(row[5]) != owner or row[2] != content_hash
        or row[0] in ('vetoed','deleted','completion_withdrawn','superseded','reset_retired')):
        return 'unavailable'
    target = message_id if component == 'user' else row[3]
    if not target:
        return 'unavailable'
    actual = db.execute(text('SELECT role,content,source_body_revision,source_body_hash FROM chat_messages WHERE id=:message AND session_id=:chat'), {'message':target,'chat':chat_id}).first()
    drop_states = _PROJECTION_USER if component == 'user' else _PROJECTION_ASSISTANT
    if actual:
        if row[0] in drop_states or actual[0] != component:
            return 'unavailable'
        if component == 'user' and (actual[2] != 1 or actual[3] != content_hash or not isinstance(actual[1],str) or digest(actual[1]) != content_hash):
            return 'unavailable'
        return 'current'
    if row[0] in drop_states and row[1] == 1 and row[4] == 'delivered':
        return 'projection_dropped'
    return 'unavailable'


def verify_current(db, archive, owner, chat_id, actor_id, part):
    if not available(db) or actor_id != 'main' or part.part_id != 'body':
        return False
    # Host source admission exists only for revision one; old unqualified rows
    # retain their existing exact-canonical-row behavior.
    if part.revision == 1 and origin_available(db):
        state = source_component_state(db, archive, owner=owner, chat_id=chat_id,
            message_id=part.message_id, content_hash=part.content_hash)
        if state != 'unavailable':
            return True
        root = db.execute(text("SELECT origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner':owner,'chat':chat_id,'message':part.message_id}).scalar()
        if root is not None:
            return False
    if origin_available(db):
        root = db.execute(text("SELECT origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner':owner,'chat':chat_id,'message':part.message_id}).scalar()
        if root in ('vetoed','deleted','completion_withdrawn','reset_retired'):
            return False
    row = db.execute(text('SELECT m.content,m.source_body_revision,m.source_body_hash,s.owner FROM chat_messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=:message AND m.session_id=:chat'), {'message':part.message_id,'chat':chat_id}).first()
    return bool(row and archive.resolve_owner(row[3]) == owner and row[1] == part.revision and row[2] == part.content_hash and isinstance(row[0],str) and digest(row[0]) == part.content_hash)


def verify_projection_drop(db, archive, owner, chat_id, actor_id, part):
    return (actor_id == 'main' and part.part_id == 'body' and part.revision == 1
        and source_component_state(db, archive, owner=owner, chat_id=chat_id,
            message_id=part.message_id, content_hash=part.content_hash) == 'projection_dropped')


def mark_projection_drops(db, archive, *, owner, chat_id, rows):
    """Record delivered-only component drops in the caller's host transaction.

    Rows are exact canonical rows validated by the guarded writer before this
    call. Unknown/pending origins do not become eligible through projection.
    """
    _check_owner(db, archive, owner, chat_id)
    if not origin_available(db):
        return
    for row in rows:
        if row.role not in ('user', 'assistant'):
            continue
        if row.role == 'user':
            targets = db.execute(text("SELECT message_id,content_hash,origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND state='delivered' AND origin_completed=1 AND origin_state IN ('delivered','projection_user_dropped','projection_assistant_dropped','projection_both_dropped')"), {'owner':owner,'chat':chat_id,'message':row.id}).all()
            version = db.execute(text('SELECT source_body_revision,source_body_hash FROM chat_messages WHERE id=:message AND session_id=:chat'), {'message':row.id,'chat':chat_id}).first() if targets else None
            if targets and (not version or not isinstance(row.content,str) or version[0] != 1 or version[1] != targets[0][1] or digest(row.content) != targets[0][1]):
                raise ValueError('projection source changed')
        else:
            targets = db.execute(text("SELECT message_id,content_hash,origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND origin_assistant_id=:message AND part_id='body' AND revision=1 AND state='delivered' AND origin_completed=1 AND origin_state IN ('delivered','projection_user_dropped','projection_assistant_dropped','projection_both_dropped')"), {'owner':owner,'chat':chat_id,'message':row.id}).all()
        for root, _hash, state in targets:
            if row.role == 'user':
                next_state = 'projection_both_dropped' if state in _PROJECTION_ASSISTANT else 'projection_user_dropped'
            else:
                next_state = 'projection_both_dropped' if state in _PROJECTION_USER else 'projection_assistant_dropped'
            changed = db.execute(text("UPDATE host_source_delivery_intent SET origin_state=:next WHERE owner=:owner AND chat_id=:chat AND message_id=:root AND part_id='body' AND revision=1 AND origin_state=:previous AND state='delivered' AND origin_completed=1"), {'next':next_state,'owner':owner,'chat':chat_id,'root':root,'previous':state})
            if changed.rowcount != 1:
                raise ValueError('projection origin changed')


def _terminal_error_association(value, *, root):
    """Closed, content-free selector; never an assistant-success source."""
    fields={'version','kind','identity','nativeMessageID','hostAssistantID','hostRevision','hostHash','diagnostic'}
    keys={'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
    if type(value) is not dict or set(value)!=fields or type(value.get('version')) is not int or value.get('version')!=2 or value.get('kind')!='terminal-diagnostic-snapshot-v2': raise ValueError('terminal source unavailable')
    from src.openclank.terminal_diagnostic_projection import _NAMES
    marker=value['diagnostic']
    if (type(marker) is not dict or set(marker)!={'version','errorName','stage'} or type(marker.get('version')) is not int or marker['version']!=1 or type(marker.get('errorName')) is not str or marker['errorName'] not in _NAMES or marker.get('stage')!='processor'): raise ValueError('terminal source unavailable')
    ident=value['identity']
    if type(ident) is not dict or set(ident)!=keys or ident.get('rootOperationID')!=root or ident.get('actorID')!='main' or type(ident.get('executionRevision')) is not int or not 0<=ident['executionRevision']<=9007199254740991: raise ValueError('terminal source unavailable')
    if any(type(ident.get(k)) is not str or not ident[k] or len(ident[k].encode('utf-8'))>256 for k in ('sessionID','rootOperationID','actorID','innerTurnID')): raise ValueError('terminal source unavailable')
    if any(type(value.get(k)) is not str or not value[k] or len(value[k].encode('utf-8'))>256 for k in ('nativeMessageID','hostAssistantID')) or type(value.get('hostRevision')) is not int or value['hostRevision']<1 or type(value.get('hostHash')) is not str or not re.fullmatch(r'[0-9a-f]{64}',value['hostHash']): raise ValueError('terminal source unavailable')
    return value


def read_terminal_error_association(db, archive, *, owner, chat_id, root_operation_id, current_authority):
    if not callable(current_authority) or current_authority() is not True or not origin_available(db) or db.execute(text('SELECT version FROM host_source_schema_marker')).scalar()!=3: return None
    _check_owner(db,archive,owner,chat_id)
    row=db.execute(text("SELECT origin_state,origin_completed,origin_assistant_id,content_hash,execution_snapshot_json FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:root AND part_id='body' AND revision=1"),{'owner':owner,'chat':chat_id,'root':root_operation_id}).first()
    if not row or row[0]!='pending' or row[1]!=0 or row[2] is not None or not isinstance(row[4],str) or len(row[4].encode('utf-8'))>4096: return None
    try: value=_terminal_error_association(json.loads(row[4]),root=root_operation_id)
    except (ValueError,TypeError,UnicodeError): return None
    part=type('Part',(),{'part_id':'body','message_id':root_operation_id,'revision':1,'content_hash':row[3]})()
    if not verify_current(db,archive,owner,chat_id,'main',part): return None
    host=db.execute(text('SELECT role,content,source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat AND id=:message'),{'chat':chat_id,'message':value['hostAssistantID']}).first()
    if not host or host[0]!='assistant' or not isinstance(host[1],str) or host[2]!=value['hostRevision'] or host[3]!=value['hostHash'] or digest(host[1])!=value['hostHash'] or current_authority() is not True: return None
    return value


def record_terminal_error_association(db, archive, *, owner, chat_id, root_operation_id, host_assistant_id, identity, native_message_id, diagnostic, current_authority):
    """Attach only an already verified private turn witness; no success promotion."""
    if not callable(current_authority) or current_authority() is not True or db.in_transaction() or db.get_bind().dialect.name!='sqlite': raise ValueError('terminal source unavailable')
    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    try:
        if not origin_available(db) or db.execute(text('SELECT version FROM host_source_schema_marker')).scalar()!=3: raise ValueError('terminal source unavailable')
        _check_owner(db,archive,owner,chat_id)
        row=db.execute(text("SELECT origin_state,origin_completed,origin_assistant_id,content_hash,execution_snapshot_json,origin_json FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:root AND part_id='body' AND revision=1"),{'owner':owner,'chat':chat_id,'root':root_operation_id}).first()
        if not row or row[0]!='pending' or row[1]!=0 or row[2] is not None or not row[5]: raise ValueError('terminal source unavailable')
        origin=json.loads(row[5]); choices=json.loads(origin['choices_json'])
        if origin.get('capture_eligible') is not True or any(choices.get(k) is not False for k in ('incognito','noMemory','compareMode')): raise ValueError('terminal source unavailable')
        host=db.execute(text('SELECT role,content,source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat AND id=:message'),{'chat':chat_id,'message':host_assistant_id}).first()
        if not host or host[0]!='assistant' or not isinstance(host[1],str) or type(host[2]) is not int or host[2]<1 or not isinstance(host[3],str) or digest(host[1])!=host[3]: raise ValueError('terminal source unavailable')
        value=_terminal_error_association({'version':2,'kind':'terminal-diagnostic-snapshot-v2','identity':identity,'nativeMessageID':native_message_id,'hostAssistantID':host_assistant_id,'hostRevision':host[2],'hostHash':host[3],'diagnostic':diagnostic},root=root_operation_id)
        encoded=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        part=type('Part',(),{'part_id':'body','message_id':root_operation_id,'revision':1,'content_hash':row[3]})()
        if len(encoded.encode('utf-8'))>4096 or not verify_current(db,archive,owner,chat_id,'main',part) or current_authority() is not True: raise ValueError('terminal source unavailable')
        if row[4] is not None and row[4]!=encoded: raise ValueError('terminal source conflict')
        changed=db.execute(text("UPDATE host_source_delivery_intent SET execution_snapshot_json=:value WHERE owner=:owner AND chat_id=:chat AND message_id=:root AND part_id='body' AND revision=1 AND origin_state='pending' AND origin_completed=0 AND origin_assistant_id IS NULL AND (execution_snapshot_json IS NULL OR execution_snapshot_json=:value)"),{'value':encoded,'owner':owner,'chat':chat_id,'root':root_operation_id})
        if changed.rowcount!=1 or current_authority() is not True: raise ValueError('terminal source changed')
        db.commit(); return value
    except BaseException:
        db.rollback(); raise


def withdraw_deleted_messages(db, archive, *, owner, chat_id, message_ids):
    """Persist exact root/body or completion withdrawal in the caller's delete transaction."""
    _check_owner(db, archive, owner, chat_id)
    if not origin_available(db):
        return False
    for message_id in dict.fromkeys(message_ids):
        if type(message_id) is not str or not message_id:
            raise ValueError('host source deletion identity unavailable')
        values = {'owner':owner, 'chat':chat_id, 'message':message_id}
        db.execute(text("UPDATE host_source_delivery_intent SET origin_state=CASE WHEN origin_state IN ('vetoed','reset_retired') THEN origin_state ELSE 'deleted' END WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), values)
        db.execute(text("UPDATE host_source_delivery_intent SET origin_state=CASE WHEN origin_state IN ('vetoed','deleted','reset_retired') THEN origin_state ELSE 'completion_withdrawn' END WHERE owner=:owner AND chat_id=:chat AND part_id='body' AND revision=1 AND origin_assistant_id=:message"), values)
        if db.get_bind().dialect.name=='sqlite' and db.execute(text("SELECT version FROM host_source_schema_marker")).scalar()==3:
            db.execute(text("""UPDATE host_source_delivery_intent SET origin_state=CASE WHEN origin_state IN ('vetoed','deleted','reset_retired') THEN origin_state ELSE 'completion_withdrawn' END WHERE owner=:owner AND chat_id=:chat AND part_id='body' AND revision=1 AND origin_completed=0 AND origin_assistant_id IS NULL AND CASE WHEN json_valid(execution_snapshot_json) THEN json_extract(execution_snapshot_json,'$.kind') END IN ('terminal-diagnostic-v1','terminal-diagnostic-snapshot-v2') AND CASE WHEN json_valid(execution_snapshot_json) THEN json_extract(execution_snapshot_json,'$.hostAssistantID') END=:message"""), values)
    return True


def deliver(db, archive, *, owner, chat_id, message_id, revision):
    from src.openclank.conversation_archive import SourcePart
    _check_owner(db, archive, owner, chat_id)
    row = db.execute(text('SELECT i.content_hash,i.state,m.content,m.source_body_revision,m.source_body_hash,m.role,s.owner,s.workspace_id,m.timestamp FROM host_source_delivery_intent i JOIN chat_messages m ON m.id=i.message_id AND m.session_id=i.chat_id JOIN sessions s ON s.id=i.chat_id WHERE i.owner=:owner AND i.chat_id=:chat AND i.message_id=:message AND i.revision=:revision AND i.part_id=\'body\''), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision}).first()
    if not row:
        # The chat owner was verified above; a missing canonical message is a
        # definitive deletion, not an unavailable authority lookup.
        values = {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision}
        db.execute(text("UPDATE host_source_delivery_intent SET state='superseded' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND state='pending'"), values)
        if origin_available(db):
            db.execute(text("UPDATE host_source_delivery_intent SET origin_state='superseded' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND origin_state='pending'"), values)
        db.commit()
        return 'superseded'
    if row[1] == 'delivered':
        if origin_available(db):
            deliver_origin(db, archive, owner=owner, chat_id=chat_id, message_id=message_id, revision=revision)
        return 'delivered'
    exact = archive.resolve_owner(row[6]) == owner and row[3] == revision and row[4] == row[0] and digest(row[2]) == row[0]
    if not exact:
        db.execute(text("UPDATE host_source_delivery_intent SET state='superseded' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND state='pending'"), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision})
        if origin_available(db):
            db.execute(text("UPDATE host_source_delivery_intent SET origin_state='superseded' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND origin_state='pending'"), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision})
        db.commit()
        return 'superseded'
    stamp = row[8] if isinstance(row[8], datetime) else datetime.fromisoformat(str(row[8]))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    created = int(stamp.timestamp() * 1000)
    sequence = db.execute(text('SELECT COUNT(*) FROM chat_messages WHERE session_id=:chat AND (timestamp < :stamp OR (timestamp = :stamp AND id <= :message))'), {'chat': chat_id, 'stamp': row[8], 'message': message_id}).scalar()
    archive.append_parts([SourcePart(owner=owner, chat_id=chat_id, actor_id='main', message_id=message_id, part_id='body', revision=revision, content=row[2], content_hash=row[0], event_workspace=row[7], event_sequence=int(sequence), time_created=created, time_updated=created, role=row[5], aliases=(('host_message', message_id),))])
    db.execute(text("UPDATE host_source_delivery_intent SET state='delivered' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND state='pending'"), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision})
    db.commit()
    if origin_available(db):
        deliver_origin(db, archive, owner=owner, chat_id=chat_id, message_id=message_id, revision=revision)
    return 'delivered'


def recover_chat(db, archive, *, owner, chat_id, limit=16, delivered_ids=None):
    """Bounded retry on an already-authorized history read; no scheduler."""
    _check_owner(db, archive, owner, chat_id)
    if not available(db):
        return 0
    has_origin = origin_available(db)
    pending = "(state='pending' OR (origin_state='pending' AND origin_completed=1))" if has_origin else "state='pending'"
    origin_column = 'origin_state' if has_origin else 'NULL AS origin_state'
    rows = db.execute(text("SELECT message_id,revision,state," + origin_column + " FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND " + pending + " ORDER BY revision,message_id LIMIT :limit"), {'owner': owner, 'chat': chat_id, 'limit': min(max(int(limit), 1), 16)}).all()
    completed = 0
    for message_id, revision, previous_state, previous_origin in rows:
        try:
            outcome = deliver(db, archive, owner=owner, chat_id=chat_id, message_id=message_id, revision=revision)
            if outcome in {'delivered', 'superseded'}:
                completed += 1
                if outcome == 'delivered' and (previous_state == 'pending' or previous_origin == 'pending') and type(delivered_ids) is list:
                    delivered_ids.append(message_id)
        except Exception:
            db.rollback()
    return completed


def intent_inventory(db, account_id):
    if not available(db):
        return {'count': 0, 'state': 'schema_unavailable'}
    if not account_id:
        return {'count': 0, 'state': 'account_unresolved'}
    count = db.execute(text('SELECT COUNT(*) FROM host_source_delivery_intent WHERE owner=:owner'), {'owner': account_id}).scalar()
    return {'count': int(count), 'state': 'available', 'owner_account_id': account_id}


def purge_intents(db, account_id):
    if not available(db):
        return {'erased': 0, 'residual': 0, 'state': 'schema_unavailable'}
    if not account_id:
        raise ValueError('host intent account identity unavailable')
    result = db.execute(text('DELETE FROM host_source_delivery_intent WHERE owner=:owner'), {'owner': account_id})
    residual = db.execute(text('SELECT COUNT(*) FROM host_source_delivery_intent WHERE owner=:owner'), {'owner': account_id}).scalar()
    if residual:
        raise RuntimeError('host intent purge has residual records')
    db.commit()
    return {'owner_account_id': account_id, 'erased': int(result.rowcount), 'residual': 0, 'state': 'available'}


def execution_snapshot_is_current(db, archive, *, owner, provider_owner, chat_id, root_operation_id, snapshot):
    """Recheck exact host binding before attaching bridge-verified evidence."""
    from core.provider_models import ProviderOperationBinding
    _check_owner(db, archive, owner, chat_id)
    principal = str(provider_owner or '').strip().lower()
    if not principal or archive.resolve_owner(principal) != owner or not isinstance(snapshot, dict):
        return False
    if snapshot.get('actorID') != 'main' or snapshot.get('rootOperationID') != root_operation_id:
        return False
    binding = db.query(ProviderOperationBinding).filter_by(id=snapshot.get('bindingID'), owner=principal,
        root_operation_id=root_operation_id, engine_session_id=snapshot.get('sessionID'), actor_id='main',
        execution_revision=snapshot.get('executionRevision')).with_for_update().first()
    return bool(binding is not None and binding.state == 'committed' and binding.revision == snapshot.get('bindingRevision')
        and all(getattr(binding, attr) == snapshot.get(wire) for attr, wire in (
            ('connection_id','connectionID'),('provider_id','providerID'),('billing_lane','billingLane'),
            ('model_id','modelID'),('model_route_id','modelRouteID'),('grant_id','grantID')))
        and (binding.policy_snapshot or {}).get('main_model_options') == snapshot.get('modelRequestOptions'))


def _source_layout_hash(layout, source_position, source_sha256):
    """Hash the bounded content-free ordered Host layout, or refuse it."""
    import re
    if (type(layout) is not list or not 1 <= len(layout) <= 128
            or type(source_position) is not int or not 0 <= source_position < len(layout)):
        return None
    canonical=[]
    for index, item in enumerate(layout):
        if (type(item) is not dict or set(item) != {'position','sha256','synthetic'}
                or type(item['position']) is not int or item['position'] != index
                or type(item['synthetic']) is not bool or type(item['sha256']) is not str
                or re.fullmatch(r'[0-9a-f]{64}',item['sha256']) is None):
            return None
        canonical.append({'position':index,'sha256':item['sha256'],'synthetic':item['synthetic']})
    if (sum(not item['synthetic'] for item in canonical) != 1
            or canonical[source_position]['synthetic']
            or canonical[source_position]['sha256'] != source_sha256):
        return None
    return hashlib.sha256(json.dumps(canonical,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def _closure_host_source_scope(db, *, owner, chat_id, message_id, content_hash, closure):
    """Resolve only the exact current Host source; empty Native scope is not a Files grant."""
    try:
        if closure['wire']['prefix']['environment']['version'] != 2: return None
    except (KeyError,TypeError): return None
    row=db.execute(text("SELECT s.workspace_id,m.source_body_revision,m.source_body_hash FROM sessions s JOIN chat_messages m ON m.session_id=s.id JOIN host_source_delivery_intent i ON i.chat_id=s.id AND i.message_id=m.id AND i.owner=:owner AND i.part_id='body' AND i.revision=1 WHERE s.id=:chat AND m.id=:message AND m.role='user'"),
        {'owner':owner,'chat':chat_id,'message':message_id}).first()
    if not row or row[1]!=1 or row[2]!=content_hash: return None
    workspace=row[0]
    if workspace is None: return (2,None)
    if type(workspace) is not str or not workspace or workspace!=workspace.strip(): return None
    return (1,workspace)


_STATIC_SKILL_GUIDANCE = {'skill-search-first.v1': '<system-reminder>\nSkill search trigger: consider the current request for a specialized skill.\nYou should call skill_search before starting the task.\nFirst rewrite the request as a Skill Query containing action, input, output, and audience when available.\nDescribe the required capability and final deliverable; treat file types such as CSV as inputs, not automatically as the target skill.\nOffice examples:\n- Excel/CSV: analyze or clean data, work with formulas and tables, or create charts and workbooks.\n- PowerPoint/PPT: create, revise, or review a presentation or executive deck.\n- Word/DOCX: draft, edit, review, or format a document or report.\n- PDF: extract or review content, generate a PDF, or preserve and inspect page layout.\nCode examples: code review, debugging, test generation, security or performance analysis, frontend design, and other specialized engineering workflows.\nFor example, "analyze gender_submission.csv" should express data analysis as the capability, the CSV dataset as input, and findings as output.\nThese are examples of likely specialized workflows, not requirements to call a tool.\n</system-reminder>', 'skill-search-refresh.v1': '<system-reminder>\nSkill search trigger: reassess the current request for a specialized skill.\nBy default: call skill_search before acting, unless the user explicitly references the current task or current artifact.\nWhen searching, rewrite the request as a Skill Query containing action, input, output, and audience when available.\nDescribe the required capability and final deliverable; treat file types such as CSV as inputs, not automatically as the target skill.\nOffice examples:\n- Excel/CSV: analyze or clean data, work with formulas and tables, or create charts and workbooks.\n- PowerPoint/PPT: create, revise, or review a presentation or executive deck.\n- Word/DOCX: draft, edit, review, or format a document or report.\n- PDF: extract or review content, generate a PDF, or preserve and inspect page layout.\nCode examples: code review, debugging, test generation, security or performance analysis, frontend design, and other specialized engineering workflows.\nFor example, "analyze gender_submission.csv" should express data analysis as the capability, the CSV dataset as input, and findings as output.\nThese are examples of likely specialized workflows, not requirements to call a tool.\n</system-reminder>'}

_HOST_CONSTRUCTOR_SEAL = object()


class _HostConstructorManifest:
    """In-process Host-issued evidence; engine JSON cannot mint this object."""
    __slots__ = ('_encoded',)
    def __init__(self, seal, payload):
        if seal is not _HOST_CONSTRUCTOR_SEAL: raise ValueError('constructor issuer unavailable')
        self._encoded=json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    def __deepcopy__(self, memo): return self
    def value(self): return json.loads(self._encoded)


def _issue_host_constructor_manifest(*, identity, account, chat, source_hash, holder, session_environment, constructor_ids):
    if (type(source_hash) is not str or re.fullmatch(r'[0-9a-f]{64}',source_hash) is None
            or not isinstance(holder,str) or not holder or type(constructor_ids) is not list
            or not 1<=len(constructor_ids)<=4 or len(set(constructor_ids))!=len(constructor_ids)) : return None
    if sum(name.startswith('skill-search-') for name in constructor_ids)>1:return None
    constructors=[]
    for name in constructor_ids:
        if name in {'host-session-env-reminder.v1','host-session-personal-env.v1'}:
            if not isinstance(session_environment,dict) or session_environment.get('constructor')!=name:return None
            if name=='host-session-personal-env.v1' and (set(session_environment)!={'version','constructor','directory','platform','repositoryStatus','textSHA256'}
                    or type(session_environment.get('version')) is not int or session_environment['version']!=3
                    or session_environment.get('repositoryStatus')!='unobserved'
                    or type(session_environment.get('directory')) is not str or not session_environment['directory']
                    or session_environment.get('platform') not in ('linux','darwin','win32')):return None
            digest=session_environment.get('textSHA256')
        elif name in _STATIC_SKILL_GUIDANCE:
            digest=hashlib.sha256(_STATIC_SKILL_GUIDANCE[name].encode('utf-8')).hexdigest()
        else:return None
        if type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}',digest) is None:return None
        constructors.append({'constructor':name,'sha256':digest})
    return _HostConstructorManifest(_HOST_CONSTRUCTOR_SEAL, {'version':1,'ownerAccountID':account,'chatID':chat,
        'rootOperationID':identity['rootOperationID'],'sourceRevision':1,'sourceSHA256':source_hash,
        'identity':dict(identity),'holderID':holder,'constructors':constructors})


def _constructor_manifest_current(value, *, identity, owner_account, chat_id, source_hash):
    try:
        if (type(value) is not dict or set(value)!={'version','ownerAccountID','chatID','rootOperationID','sourceRevision',
                'sourceSHA256','identity','holderID','constructors'} or value['version']!=1 or value['sourceRevision']!=1
                or value['ownerAccountID']!=owner_account or value['chatID']!=chat_id or value['identity']!=identity
                or value['rootOperationID']!=identity['rootOperationID'] or value['sourceSHA256']!=source_hash
                or not isinstance(value['holderID'],str) or not value['holderID'] or type(value['constructors']) is not list
                or not 1<=len(value['constructors'])<=4):return False
        seen=set()
        for item in value['constructors']:
            if (type(item) is not dict or set(item)!={'constructor','sha256'}
                    or item['constructor'] not in {'host-session-env-reminder.v1','host-session-personal-env.v1',*list(_STATIC_SKILL_GUIDANCE)} or item['constructor'] in seen
                    or type(item['sha256']) is not str or re.fullmatch(r'[0-9a-f]{64}',item['sha256']) is None):return False
            seen.add(item['constructor'])
            if item['constructor'] in _STATIC_SKILL_GUIDANCE and item['sha256']!=hashlib.sha256(_STATIC_SKILL_GUIDANCE[item['constructor']].encode('utf-8')).hexdigest():return False
        return sum(name.startswith('skill-search-') for name in seen)<=1
    except Exception:return False


def _source_closure_matches(closure, *, identity, source, output, owner_account=None, chat_id=None, execution=None, layout=None, source_position=None, source_scope=None, constructor_manifest=None, source_hash=None):
    """Check the bounded closed observation against independently held original IDs."""
    try:
        from src.openclank.managed_protocol import _validated_contract
        contract, validator = _validated_contract()
        schema = {"$defs": contract["$defs"], "$ref": "#/$defs/managedAssistantSourceObservation"}
        if len(json.dumps(closure, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')) > 12000:
            return False
        if not validator(schema).is_valid(closure): return False
        wire, observed = closure['wire'], closure['output']
        if wire.get('version') == 2:
            layout_hash=_source_layout_hash(layout,source_position,source['textSHA256'])
            generated=wire.get('generated')
            if (layout_hash is None or wire.get('profile') != 'compatible-chat-text-layout-v2'
                    or wire.get('layoutSHA256') != layout_hash or wire.get('sourcePosition') != source_position
                    or type(generated) is not list or len(generated) > 4): return False
            full=[{'position':item['position'],'sha256':item['sha256'],'synthetic':item['synthetic']} for item in layout]
            if generated:
                if any(type(item) is dict and item.get('constructor')=='host-session-personal-env.v1' for item in generated) and source_scope!=(2,None): return False
                if not _constructor_manifest_current(constructor_manifest,identity=identity,owner_account=owner_account,
                        chat_id=chat_id,source_hash=source_hash):return False
                expected=constructor_manifest['constructors']
                if len(generated)!=len(expected):return False
                for item, issued in zip(generated,expected):
                    if (type(item) is not dict or set(item)!={'constructor','position','sha256'}
                            or item['constructor']!=issued['constructor'] or item['sha256']!=issued['sha256']
                            or type(item['position']) is not int or item['position']!=len(full)):return False
                    full.append({'position':item['position'],'sha256':item['sha256'],'synthetic':True})
            if hashlib.sha256(json.dumps(full,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest() != wire.get('fullLayoutSHA256'): return False
        elif wire.get('version') != 1 or layout is not None or source_position is not None:
            return False
        env = wire['prefix']['environment']; scope = env['scope']
        if env.get('version') == 2:
            if (type(source_scope) is not tuple or len(source_scope)!=2): return False
            version, workspace = source_scope
            if env.get('scopeKind') == 'account-chat':
                if version != 2 or workspace is not None or scope.get('authorityWorkspaceID') != '': return False
            elif env.get('scopeKind') == 'workspace':
                if version != 1 or type(workspace) is not str or not workspace or scope.get('authorityWorkspaceID') != workspace: return False
            else: return False
        if env['identity'] != identity or wire['dispatchID'] != observed['dispatchID']:
            return False
        if (wire['root'] != {'messageID':source['messageID'],'partID':source['partID'],'digest':source['textSHA256']}
            or any(observed[k] != output[k] for k in ('messageID','partID','textSHA256'))): return False
        if owner_account is not None and scope['ownerAccountID'] != owner_account: return False
        if chat_id is not None and scope['chatID'] != chat_id: return False
        if execution is not None and (scope['providerID'] != execution['connectionID'] or scope['modelID'] != execution['modelID']): return False
        return True
    except Exception:
        return False


def _forward_pair_from_witness(db, *, owner, chat_id, root_operation_id, assistant_message_id, execution, witness):
    """Validate the original C2-issued pair; an invalid optional witness is absent."""
    import hashlib, hmac, re
    from src import secret_storage
    from src.memery_external_sources import SourceRef, _source_ref_error
    try:
        if type(witness) is not dict or set(witness) not in ({'identity', 'input', 'output'}, {'identity', 'input', 'output', 'sourceClosure'}, {'identity', 'input', 'output', 'sourceClosure', 'constructorManifest'}):
            return None
        identity, original, output = witness['identity'], witness['input'], witness['output']
        fields = {'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
        if (type(identity) is not dict or set(identity) != fields or identity['actorID'] != 'main'
                or identity['rootOperationID'] != root_operation_id or identity['sessionID'] != execution['sessionID']
                or identity['executionRevision'] != execution['executionRevision'] or type(identity['innerTurnID']) is not str
                or not 0 < len(identity['innerTurnID'].encode()) <= 256): return None
        base={'identity','attestation','messageID','partID','textSHA256'}
        if type(original) is not dict or set(original) not in (base,base | {'layout','sourcePosition','layoutSHA256'}) or original['identity'] != identity: return None
        attestation = json.loads(original['attestation'])
        if type(attestation) is not dict or set(attestation) != {'payload','mac'} or type(attestation['payload']) is not dict or type(attestation['mac']) is not str or not secret_storage._KEY_PATH.is_file(): return None
        payload = attestation['payload']
        if payload.get('version') == 1:
            if set(original) != base or set(payload) != {'version','source','identity','messageID','partID','textSHA256','position'} or payload['position'] != 0: return None
            domain='openclank.c2.maintenance-source.v1'
        elif payload.get('version') == 2:
            if (set(original) != base | {'layout','sourcePosition','layoutSHA256'} or set(payload) != {'version','source','identity','messageID','partID','textSHA256','sourcePosition','layoutSHA256'}
                    or type(payload['layoutSHA256']) is not str or payload['sourcePosition'] != original['sourcePosition'] or payload['layoutSHA256'] != original['layoutSHA256']
                    or _source_layout_hash(original['layout'],original['sourcePosition'],payload['textSHA256']) != payload['layoutSHA256']): return None
            domain='openclank.c2.maintenance-source.v2'
        else: return None
        if payload['identity'] != identity: return None
        encoded = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if not hmac.compare_digest(attestation['mac'],secret_storage.keyed_digest(encoded,context=domain)): return None
        source = SourceRef(**payload['source'])
        if (_source_ref_error(source) or source.account_id != owner or source.chat_id != chat_id or source.message_id != root_operation_id
                or source.revision != 1 or any(payload[k] != original[k] for k in ('messageID','partID','textSHA256'))): return None
        if type(output) is not dict or set(output) != {'version','messageID','partID','textSHA256'} or output['version'] != 1: return None
        for value in (payload['messageID'],payload['partID'],output['messageID'],output['partID']):
            if type(value) is not str or not value or len(value.encode('utf-8')) > 256: return None
        if any(type(value) is not str or re.fullmatch(r'[0-9a-f]{64}',value) is None for value in (payload['textSHA256'],output['textSHA256'])): return None
        rows = db.execute(text("SELECT id,role,content,source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat AND id IN (:root,:answer)"), {'chat':chat_id,'root':root_operation_id,'answer':assistant_message_id}).all()
        rows = {row[0]:row for row in rows}; user, answer = rows[root_operation_id], rows[assistant_message_id]
        if (user[1] != 'user' or user[3] != source.revision or user[4] != source.content_hash or digest(user[2]) != source.content_hash
                or answer[1] != 'assistant' or type(answer[2]) is not str or type(answer[3]) is not int or answer[3] < 1
                or len(answer[2]) > 8192 or len(answer[2].encode('utf-8')) > 8192
                or digest(answer[2]) != answer[4] or hashlib.sha256(answer[2].encode('utf-8')).hexdigest() != output['textSHA256']): return None
        source_input={'hostMessageID':root_operation_id,'hostPartID':'body','hostRevision':source.revision,'hostHash':source.content_hash,'nativeMessageID':payload['messageID'],'nativePartID':payload['partID'],'nativeTextSHA256':payload['textSHA256']}
        if payload['version'] == 2:
            source_input.update(layout=original['layout'],sourcePosition=original['sourcePosition'],layoutSHA256=payload['layoutSHA256'])
        return {'version':payload['version'],'identity':identity,'input':source_input,'output':{'nativeMessageID':output['messageID'],'nativePartID':output['partID'],'nativeTextSHA256':output['textSHA256'],'hostMessageID':assistant_message_id,'hostRevision':answer[3],'hostHash':answer[4]}}
    except Exception:
        return None


def _valid_execution_snapshot(value, root_operation_id):
    required = {'version','sessionID','actorID','rootOperationID','executionRevision','bindingID','bindingRevision',
                'connectionID','providerID','billingLane','modelID','modelRouteID','modelRequestOptions'}
    if (type(value) is not dict or not required <= set(value) or set(value) - (required | {'grantID'})
            or type(value.get('version')) is not int or value['version'] != 1 or value.get('actorID') != 'main'
            or value.get('rootOperationID') != root_operation_id or type(value.get('modelRequestOptions')) is not dict
            or any(type(value.get(key)) is not int or not 0 <= value[key] <= 9007199254740991 for key in ('executionRevision','bindingRevision'))): return False
    for key in required - {'version','executionRevision','bindingRevision','modelRequestOptions'} | ({'grantID'} if 'grantID' in value else set()):
        item=value.get(key)
        if type(item) is not str or not item: return False
    try: return len(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')) <= 16384
    except (TypeError,ValueError,UnicodeError): return False


def complete_user_origin(db, archive, *, owner, chat_id, root_operation_id, assistant_message_id, execution_snapshot=None, provider_owner=None, forward_witness=None):
    """CAS source and current binding together; replay is historical evidence."""
    if execution_snapshot is not None and db.get_bind().dialect.name == 'sqlite':
        if db.in_transaction():
            raise ValueError('host execution proof requires a fresh transaction')
        # Reserve the SQLite writer before reading the binding. Other backends
        # hold a row lock via with_for_update() through the source update.
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    _check_owner(db, archive, owner, chat_id)
    if not origin_available(db):
        return False
    version = db.execute(text("SELECT version FROM host_source_schema_marker")).scalar()
    encoded = None
    if execution_snapshot is not None and version == 3:
        if not _valid_execution_snapshot(execution_snapshot, root_operation_id):
            raise ValueError('host execution proof unavailable')
        encoded = json.dumps(execution_snapshot, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded) > 16384:
            raise ValueError('host execution proof unavailable')
        if forward_witness is not None and execution_snapshot_is_current(db, archive, owner=owner, provider_owner=provider_owner,
                chat_id=chat_id, root_operation_id=root_operation_id, snapshot=execution_snapshot):
            pair = _forward_pair_from_witness(db, owner=owner, chat_id=chat_id, root_operation_id=root_operation_id,
                assistant_message_id=assistant_message_id, execution=execution_snapshot, witness=forward_witness)
            if pair is not None and _valid_forward_pair(pair):
                closure=forward_witness.get('sourceClosure')
                sealed=forward_witness.get('constructorManifest')
                manifest=sealed.value() if type(sealed) is _HostConstructorManifest else None
                native_source={'messageID':pair['input']['nativeMessageID'],'partID':pair['input']['nativePartID'],'textSHA256':pair['input']['nativeTextSHA256']}
                native_output={'messageID':pair['output']['nativeMessageID'],'partID':pair['output']['nativePartID'],'textSHA256':pair['output']['nativeTextSHA256']}
                qualified = _source_closure_matches(closure,identity=pair['identity'],source=native_source,output=native_output,owner_account=owner,chat_id=chat_id,execution=execution_snapshot, constructor_manifest=manifest, source_hash=pair['input']['hostHash'], source_scope=_closure_host_source_scope(db,owner=owner,chat_id=chat_id,message_id=root_operation_id,content_hash=pair['input']['hostHash'],closure=closure), **({'layout':pair['input']['layout'],'source_position':pair['input']['sourcePosition']} if pair['version']==2 else {}))
                try:
                    logging.getLogger(__name__).debug('S14_HOST_SOURCE_CLOSURE present=%s qualified=%s', closure is not None, qualified is True)
                except Exception:
                    pass
                bundle=({'version':4,'execution':execution_snapshot,'nativePair':pair,'sourceClosure':closure,'constructorManifest':manifest} if manifest is not None else {'version':3,'execution':execution_snapshot,'nativePair':pair,'sourceClosure':closure}) if qualified else {'version':2,'execution':execution_snapshot,'nativePair':pair}
                wrapped = json.dumps(bundle,sort_keys=True,separators=(',', ':'),allow_nan=False)
                if len(wrapped.encode('utf-8')) <= 16384:
                    encoded = wrapped
    column = ',execution_snapshot_json' if version == 3 else ''
    previous = db.execute(text("SELECT origin_completed,origin_assistant_id" + column + " FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner': owner, 'chat': chat_id, 'message': root_operation_id}).first()
    origin_state = db.execute(text("SELECT origin_state FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner': owner, 'chat': chat_id, 'message': root_operation_id}).scalar()
    if origin_state in ('vetoed', 'deleted', 'completion_withdrawn', 'reset_retired'):
        return False
    if previous and previous[0]:
        if previous[1] != assistant_message_id or (version == 3 and previous[2] != encoded):
            raise ValueError('conflicting host origin completion')
        part = type('Part', (), {'part_id': 'body', 'message_id': root_operation_id, 'revision': 1,
            'content_hash': db.execute(text("SELECT content_hash FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner': owner, 'chat': chat_id, 'message': root_operation_id}).scalar()})()
        return verify_current(db, archive, owner, chat_id, 'main', part)
    if encoded is not None and not execution_snapshot_is_current(db, archive, owner=owner, provider_owner=provider_owner,
            chat_id=chat_id, root_operation_id=root_operation_id, snapshot=execution_snapshot):
        # Preserve ordinary source completion, but never attach stale authority.
        execution_snapshot = None
        encoded = None
    if encoded is not None:
        origin = db.execute(text("SELECT origin_json FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner': owner, 'chat': chat_id, 'message': root_operation_id}).scalar()
        evidence = json.loads(origin or '{}'); choices = json.loads(evidence.get('choices_json') or '{}')
        if evidence.get('capture_eligible') is not True or any(choices.get(key) is not False for key in ('incognito','noMemory','compareMode')):
            raise ValueError('host execution proof privacy unavailable')
    source_hash = db.execute(text("SELECT content_hash FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner': owner, 'chat': chat_id, 'message': root_operation_id}).scalar()
    part = type('Part', (), {'part_id':'body','message_id':root_operation_id,'revision':1,'content_hash':source_hash})()
    if not verify_current(db, archive, owner, chat_id, 'main', part):
        return False
    set_snapshot = ', execution_snapshot_json=:snapshot' if version == 3 else ''
    result = db.execute(text("UPDATE host_source_delivery_intent SET origin_completed=1, origin_assistant_id=:assistant" + set_snapshot + " WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_json IS NOT NULL AND origin_state='pending' AND origin_completed=0 AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.id=:message AND m.session_id=:chat AND m.role='user' AND m.source_body_revision=1 AND m.source_body_hash=host_source_delivery_intent.content_hash) AND EXISTS (SELECT 1 FROM chat_messages a WHERE a.id=:assistant AND a.session_id=:chat AND a.role='assistant')"), {'owner': owner, 'chat': chat_id, 'message': root_operation_id, 'assistant': assistant_message_id, 'snapshot': encoded})
    if result.rowcount != 1:
        return False
    db.commit()
    return True


def verify_origin_assistant_component(db, archive, *, owner, chat_id, message_id, content_hash):
    """Verify the exact linked completion component, including a committed drop."""
    assistant = db.execute(text("SELECT origin_assistant_id FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_completed=1"), {'owner':owner,'chat':chat_id,'message':message_id}).scalar()
    if not assistant:
        return False
    state = source_component_state(db,archive,owner=owner,chat_id=chat_id,message_id=message_id,
        content_hash=content_hash,component='assistant')
    if state == 'current':
        return True
    if state != 'projection_dropped':
        return False
    proof = archive.get_part(owner=owner,chat_id=chat_id,actor_id='main',message_id=assistant,part_id='body',length=0)
    part = proof.get('part') if proof.get('ok') else None
    return bool(part and part.get('role') == 'assistant' and part.get('runtime_generation') == '0'
        and part.get('tombstone') is True and part.get('tombstone_reason') == 'projection_drop')


def _valid_forward_pair(pair):
    import re
    if type(pair) is not dict or set(pair) != {'version','identity','input','output'} or type(pair['version']) is not int or pair['version'] not in (1,2): return False
    identity, source, answer = pair['identity'], pair['input'], pair['output']
    if (type(identity) is not dict or set(identity) != {'sessionID','rootOperationID','actorID','executionRevision','innerTurnID'}
            or identity['actorID'] != 'main' or type(identity['executionRevision']) is not int or identity['executionRevision'] < 0): return False
    source_keys={'hostMessageID','hostPartID','hostRevision','hostHash','nativeMessageID','nativePartID','nativeTextSHA256'}
    if pair['version'] == 2: source_keys |= {'layout','sourcePosition','layoutSHA256'}
    if (type(source) is not dict or set(source) != source_keys
            or type(answer) is not dict or set(answer) != {'nativeMessageID','nativePartID','nativeTextSHA256','hostMessageID','hostRevision','hostHash'}
            or source['hostPartID'] != 'body' or source['hostMessageID'] != identity['rootOperationID'] or source['hostRevision'] != 1): return False
    if pair['version'] == 2 and (type(source['layoutSHA256']) is not str or _source_layout_hash(source['layout'],source['sourcePosition'],source['nativeTextSHA256']) != source['layoutSHA256']): return False
    for value in (identity['sessionID'],identity['rootOperationID'],identity['innerTurnID'],source['nativeMessageID'],source['nativePartID'],answer['nativeMessageID'],answer['nativePartID'],answer['hostMessageID']):
        if type(value) is not str or not value or len(value.encode('utf-8')) > 256: return False
    if any(type(value) is not int or value < 1 for value in (source['hostRevision'],answer['hostRevision'])): return False
    return all(type(value) is str and re.fullmatch(r'[0-9a-f]{64}',value) is not None for value in (source['hostHash'],source['nativeTextSHA256'],answer['hostHash'],answer['nativeTextSHA256']))


def read_execution_snapshot(db, archive, *, owner, chat_id, message_id):
    """Read historical execution evidence only for a current eligible source."""
    _check_owner(db, archive, owner, chat_id)
    if not origin_available(db) or db.execute(text("SELECT version FROM host_source_schema_marker")).scalar() != 3:
        return None
    row = db.execute(text("SELECT CASE WHEN length(CAST(execution_snapshot_json AS BLOB))<=16384 THEN execution_snapshot_json END,content_hash,origin_json,origin_assistant_id FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1 AND origin_completed=1 AND origin_state IN ('pending','delivered','projection_user_dropped','projection_assistant_dropped','projection_both_dropped')"), {'owner': owner, 'chat': chat_id, 'message': message_id}).first()
    if not row or not row[0] or not row[2] or not row[3]:
        return None
    if not verify_origin_assistant_component(db,archive,owner=owner,chat_id=chat_id,message_id=message_id,content_hash=row[1]):
        return None
    try:
        evidence = json.loads(row[2]); choices = json.loads(evidence.get('choices_json') or '{}')
        if evidence.get('capture_eligible') is not True or any(choices.get(key) is not False for key in ('incognito','noMemory','compareMode')):
            return None
        snapshot = json.loads(row[0])
        if type(snapshot) is dict and snapshot.get('version') in (2, 3, 4):
            execution=snapshot.get('execution'); pair=snapshot.get('nativePair')
            required={'version','execution','nativePair'} | ({'sourceClosure'} if snapshot['version'] in (3,4) else set()) | ({'constructorManifest'} if snapshot['version']==4 else set())
            if (set(snapshot) != required or not _valid_execution_snapshot(execution,message_id) or not _valid_forward_pair(pair)
                    or pair['output']['hostMessageID'] != row[3] or pair['input']['hostMessageID'] != message_id or pair['input']['hostHash'] != row[1]
                    or any(pair['identity'][key] != execution[key] for key in ('sessionID','rootOperationID','actorID','executionRevision'))):
                return None
            if snapshot['version']==4 and not _constructor_manifest_current(snapshot['constructorManifest'],identity=pair['identity'],owner_account=owner,chat_id=chat_id,source_hash=pair['input']['hostHash']):return None
            snapshot = execution
        elif not _valid_execution_snapshot(snapshot,message_id):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    part = type('Part', (), {'part_id':'body','message_id':message_id,'revision':1,'content_hash':row[1]})()
    if not verify_current(db, archive, owner, chat_id, 'main', part):
        return None
    return snapshot


def read_forward_pair_candidate(db, archive, *, owner, chat_id, message_id):
    """Decode a current host candidate; caller must separately prove native currentness."""
    execution = read_execution_snapshot(db, archive, owner=owner, chat_id=chat_id, message_id=message_id)
    if execution is None:
        return None
    row = db.execute(text("SELECT CASE WHEN length(CAST(execution_snapshot_json AS BLOB))<=16384 THEN execution_snapshot_json END FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner':owner,'chat':chat_id,'message':message_id}).scalar()
    try:
        value = json.loads(row)
        if type(value) is not dict or value.get('version') not in (2,3,4) or set(value) != ({'version','execution','nativePair'} | ({'sourceClosure'} if value['version'] in (3,4) else set()) | ({'constructorManifest'} if value['version']==4 else set())) or value['execution'] != execution or not _valid_forward_pair(value['nativePair']): return None
        pair = value['nativePair']; source, answer = pair['input'], pair['output']
        rows = db.execute(text("SELECT id,role,CASE WHEN id=:answer AND length(CAST(content AS BLOB))>8192 THEN NULL ELSE content END,source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat AND id IN (:root,:answer)"), {'chat':chat_id,'root':message_id,'answer':answer['hostMessageID']}).all()
        rows = {item[0]:item for item in rows}; user, saved = rows[message_id], rows[answer['hostMessageID']]
        if (user[1] != 'user' or user[3] != source['hostRevision'] or user[4] != source['hostHash'] or digest(user[2]) != source['hostHash']
                or saved[1] != 'assistant' or saved[3] != answer['hostRevision'] or saved[4] != answer['hostHash'] or digest(saved[2]) != answer['hostHash']): return None
        return pair
    except (KeyError,TypeError,ValueError,UnicodeError):
        return None


def read_current_forward_closure(db, archive, *, owner, provider_owner, chat_id, message_id):
    """Read an exact current v3 forward closure; this is not a source grant."""
    pair=read_forward_pair_candidate(db,archive,owner=owner,chat_id=chat_id,message_id=message_id)
    execution=read_execution_snapshot(db,archive,owner=owner,chat_id=chat_id,message_id=message_id)
    if pair is None or pair.get('version')!=2 or execution is None:return None
    row=db.execute(text("SELECT CASE WHEN length(CAST(execution_snapshot_json AS BLOB))<=16384 THEN execution_snapshot_json END FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"),{'owner':owner,'chat':chat_id,'message':message_id}).scalar()
    try:
        retained=json.loads(row)
        if (type(retained) is not dict or retained.get('version') not in (3,4) or set(retained)!=({'version','execution','nativePair','sourceClosure'} | ({'constructorManifest'} if retained['version']==4 else set())) or retained['execution']!=execution or retained['nativePair']!=pair):return None
        source=pair['input'];answer=pair['output'];closure=retained['sourceClosure']
        if not _source_closure_matches(closure,identity=pair['identity'],source={'messageID':source['nativeMessageID'],'partID':source['nativePartID'],'textSHA256':source['nativeTextSHA256']},output={'messageID':answer['nativeMessageID'],'partID':answer['nativePartID'],'textSHA256':answer['nativeTextSHA256']},owner_account=owner,chat_id=chat_id,execution=execution,layout=source['layout'],source_position=source['sourcePosition'],constructor_manifest=retained.get('constructorManifest'),source_hash=source['hostHash'],source_scope=_closure_host_source_scope(db,owner=owner,chat_id=chat_id,message_id=message_id,content_hash=source['hostHash'],closure=closure)):return None
        if not execution_snapshot_is_current(db,archive,owner=owner,provider_owner=provider_owner,chat_id=chat_id,root_operation_id=message_id,snapshot=execution):return None
        return {'pair':pair,'closure':closure}
    except (ValueError,TypeError,KeyError,UnicodeError):return None


def assistant_component(db, archive, *, owner, provider_owner, chat_id, message_id, current_authority, create=False, hydrate=False, optional_fast=False):
    """Associate exact delivered parts under a fresh held Host authority and transaction."""
    from src.openclank.conversation_archive import TrustedAssistantComponent, TrustedOriginPart
    if not callable(current_authority) or current_authority() is not True or db.in_transaction():
        raise ValueError('assistant component authority unavailable')
    # The first supported profile holds the Host writer before entering archive.
    # Do not commit, reuse, or silently weaken a caller-owned transaction.
    if db.get_bind().dialect.name != 'sqlite':
        raise ValueError('assistant component transaction unavailable')
    archive_locked = False
    budget_token = None
    previous_timeout = None
    try:
        if optional_fast:
            previous_timeout = db.connection().exec_driver_sql('PRAGMA busy_timeout').scalar()
            db.connection().exec_driver_sql('PRAGMA busy_timeout=50')
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if optional_fast:
            from src.openclank.conversation_archive import _ASSOCIATION_BUSY_BUDGET
            archive_locked = archive._lock.acquire(blocking=False)
            if not archive_locked:
                raise ValueError('assistant component busy')
            budget_token = _ASSOCIATION_BUSY_BUDGET.set(True)
            if not archive.assistant_component_profile_ready():
                raise ValueError('assistant component profile unavailable')
        def current(_component=None):
            if current_authority() is not True:
                return None if _component is None else False
            pair = read_forward_pair_candidate(db, archive, owner=owner, chat_id=chat_id, message_id=message_id)
            execution = read_execution_snapshot(db, archive, owner=owner, chat_id=chat_id, message_id=message_id)
            row = db.execute(text("SELECT execution_snapshot_json FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND part_id='body' AND revision=1"), {'owner':owner,'chat':chat_id,'message':message_id}).scalar()
            try: retained=json.loads(row)
            except (TypeError,ValueError): retained=None
            if pair is None or execution is None or type(retained) is not dict or retained.get('version') not in (3,4) or not _source_closure_matches(retained.get('sourceClosure'),identity=pair['identity'],source={'messageID':pair['input']['nativeMessageID'],'partID':pair['input']['nativePartID'],'textSHA256':pair['input']['nativeTextSHA256']},output={'messageID':pair['output']['nativeMessageID'],'partID':pair['output']['nativePartID'],'textSHA256':pair['output']['nativeTextSHA256']},owner_account=owner,chat_id=chat_id,execution=execution, constructor_manifest=retained.get('constructorManifest'),source_hash=pair['input']['hostHash'], source_scope=_closure_host_source_scope(db,owner=owner,chat_id=chat_id,message_id=message_id,content_hash=pair['input']['hostHash'],closure=retained.get('sourceClosure')), **({'layout':pair['input']['layout'],'source_position':pair['input']['sourcePosition']} if pair['version']==2 else {})):
                return None if _component is None else False
            if not execution_snapshot_is_current(db, archive, owner=owner,
                    provider_owner=provider_owner, chat_id=chat_id, root_operation_id=message_id, snapshot=execution):
                return None if _component is None else False
            scope = db.execute(text('SELECT workspace_id FROM sessions WHERE id=:chat'), {'chat': chat_id}).first()
            if scope is None:
                return None if _component is None else False
            raw = json.dumps(pair, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
            source, answer = pair['input'], pair['output']
            candidate = TrustedAssistantComponent(owner, chat_id,
                TrustedOriginPart(source['hostMessageID'], 'body', source['hostRevision'], source['hostHash']),
                TrustedOriginPart(answer['hostMessageID'], 'body', answer['hostRevision'], answer['hostHash']),
                hashlib.sha256(raw).hexdigest(), scope[0])
            return candidate if _component is None else candidate == _component
        component = current()
        if component is None or current_authority() is not True:
            raise ValueError('assistant component unavailable')
        if create and hydrate:
            raise ValueError('assistant component mode unavailable')
        method = archive.record_assistant_component if create else (archive.hydrate_assistant_component if hydrate else archive.read_assistant_component)
        result = method(component, verify_host=current)
        if current_authority() is not True or current(component) is not True:
            raise ValueError('assistant component authority changed')
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise
    finally:
        if budget_token is not None:
            _ASSOCIATION_BUSY_BUDGET.reset(budget_token)
        if archive_locked:
            archive._lock.release()
        if previous_timeout is not None:
            db.connection().exec_driver_sql(f'PRAGMA busy_timeout={int(previous_timeout)}')



async def associate_original_host_component(*, state, owner, account, chat_id, root_id, admission, barrier, origin):
    """Record only the original guarded completion; this is not extraction admission."""
    from core.database import SessionLocal
    from src.openclank.account_request_barrier import AccountRequestBarrier, _Admission
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap

    supervisor = getattr(state, 'mimo_supervisor', None)
    auth = getattr(state, 'auth_manager', None)
    if (type(admission) is not _Admission or not isinstance(barrier, AccountRequestBarrier)
            or getattr(state, 'account_request_barrier', None) is not barrier
            or not isinstance(supervisor, AcpSupervisorAdapter) or auth is None
            or type(origin) is not HostOriginAdmission or origin.capture_eligible is not True
            or origin.owner != account or origin.chat_id != chat_id or origin.root_operation_id != root_id
            or not await barrier.is_live(owner, admission)):
        return False
    archive = get_conversation_archive()
    pool = supervisor.owner_lifecycle_pool()
    mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
    async with mapping.async_chat_admission_lock(chat_id, blocking=False, existing_only=True) as guard:
        if guard is None or not await barrier.is_live(owner, admission):
            return False

        def current():
            try:
                mapping.validate_chat_guard(chat_id, guard)
                if (admission.released or admission.owner != owner.strip().lower()
                        or getattr(state, 'account_request_barrier', None) is not barrier
                        or getattr(state, 'mimo_supervisor', None) is not supervisor
                        or getattr(state, 'auth_manager', None) is not auth
                        or supervisor.owner_lifecycle_pool() is not pool or barrier._fenced(admission.owner)
                        or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner):
                    return False
                privileges = auth.get_privileges(owner)
                if type(privileges) is not dict:
                    return False
                allowed = bool(privileges.get('can_manage_memory', True))
                snapshot = STORE.effective_snapshot(owner, read_privilege=allowed, write_privilege=allowed,
                    incognito=False, no_memory=False, compare_mode=False)
                return (snapshot.get('eligibility_state') == 'available'
                        and snapshot.get('capture_allowed') is True
                        and snapshot.get('fingerprint') == origin.policy_fingerprint)
            except Exception:
                return False

        if not current():
            return False
        # Keep the shared preference authority ahead of the Host writer, as in
        # provider callbacks. This optional association can be recovered later.
        from routes.prefs_routes import PREFERENCE_LOCK
        if not PREFERENCE_LOCK.try_enter():
            return False
        try:
            # Host transaction still precedes the archive transaction; both
            # delivered parts and the original pair are rechecked inside.
            with SessionLocal() as db:
                assistant_component(db, archive, owner=account, provider_owner=owner, chat_id=chat_id,
                    message_id=root_id, current_authority=current, create=True, optional_fast=True)
        finally:
            PREFERENCE_LOCK.__exit__(None, None, None)
        recorded = bool(current() and await barrier.is_live(owner, admission))
        if recorded:
            try:
                wake = getattr(getattr(state, 'task_scheduler', None), 'request_completed_turn_wake', None)
                if callable(wake):
                    wake()  # Hint only; the reserved association remains the authority.
            except Exception:
                pass
        return recorded


async def reconcile_recovered_assistants(*, state, owner, account, chat_id, admission, barrier, delivered_ids):
    """Reconcile only exact changed deliveries through the existing Host intent owner."""
    from core.database import SessionLocal
    if type(delivered_ids) is not list or len(delivered_ids) > 16:
        return 0
    expected = "CREATE INDEX host_source_assistant_reverse ON host_source_delivery_intent(owner,chat_id,origin_assistant_id,message_id) WHERE part_id='body' AND revision=1 AND origin_completed=1"
    def shape(sql):
        return ''.join(part if i % 2 else ''.join(part.split()).lower() for i,part in enumerate(re.split(r"('(?:''|[^'])*')",sql or '')))
    roots = []
    with SessionLocal() as db:
        if db.get_bind().dialect.name != 'sqlite' or shape(db.execute(text("SELECT sql FROM sqlite_master WHERE type='index' AND name='host_source_assistant_reverse'")).scalar()) != shape(expected):
            return 0
        for assistant in dict.fromkeys(delivered_ids):
            if type(assistant) is not str or not assistant or len(assistant.encode('utf-8')) > 512:
                return 0
            params = {'owner':account,'chat':chat_id,'assistant':assistant}
            rows = db.execute(text("SELECT message_id,CASE WHEN length(CAST(origin_json AS BLOB))<=8192 THEN origin_json END FROM host_source_delivery_intent INDEXED BY host_source_assistant_reverse WHERE owner=:owner AND chat_id=:chat AND origin_assistant_id=:assistant AND part_id='body' AND revision=1 AND origin_completed=1 LIMIT 2"), params).all()
            if len(rows) > 1:
                return 0
            direct = db.execute(text("SELECT message_id,CASE WHEN length(CAST(origin_json AS BLOB))<=8192 THEN origin_json END FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:assistant AND part_id='body' AND revision=1 AND origin_completed=1 AND origin_json IS NOT NULL"), params).first()
            if direct is not None:
                rows.append(direct)
            if len(rows) > 1:
                return 0
            if rows:
                try:
                    evidence = json.loads(rows[0][1])
                    origin = HostOriginAdmission(**evidence)
                    if origin.owner != account or origin.chat_id != chat_id or origin.root_operation_id != rows[0][0]:
                        return 0
                    roots.append((rows[0][0],origin))
                except (TypeError, ValueError):
                    return 0
    count = 0
    for root,origin in roots:
        try:
            if await associate_original_host_component(state=state, owner=owner, account=account, chat_id=chat_id, root_id=root, admission=admission, barrier=barrier, origin=origin):
                count += 1
        except Exception:
            continue
    return count


async def reconcile_history_roots(*, state, owner, account, chat_id, admission, barrier, root_ids):
    """Retry exact user roots returned by this authorized bounded history page."""
    from core.database import SessionLocal
    if type(root_ids) is not list or len(root_ids) > 16:
        return 0
    roots = []
    with SessionLocal() as db:
        if not origin_available(db):
            return 0
        for root in dict.fromkeys(root_ids):
            if type(root) is not str or not root or len(root.encode('utf-8')) > 512:
                return 0
            raw = db.execute(text("SELECT CASE WHEN length(CAST(origin_json AS BLOB))<=8192 THEN origin_json END FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:root AND part_id='body' AND revision=1 AND origin_completed=1 AND origin_json IS NOT NULL"), {'owner':account,'chat':chat_id,'root':root}).scalar()
            if raw is None:
                continue
            try:
                origin = HostOriginAdmission(**json.loads(raw))
                if origin.owner != account or origin.chat_id != chat_id or origin.root_operation_id != root:
                    continue
                roots.append((root,origin))
            except (TypeError, ValueError):
                continue
    count = 0
    for root,origin in roots:
        try:
            if await associate_original_host_component(state=state, owner=owner, account=account, chat_id=chat_id, root_id=root, admission=admission, barrier=barrier, origin=origin):
                count += 1
        except Exception:
            continue
    return count

def deliver_origin(db, archive, *, owner, chat_id, message_id, revision):
    from src.openclank.conversation_archive import TrustedOriginAdmission, TrustedOriginCompletion, TrustedOriginPart
    row = db.execute(text('SELECT origin_json,origin_completed,origin_state,content_hash FROM host_source_delivery_intent WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision'), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision}).first()
    if not row or not row[0] or not row[1] or row[2] != 'pending':
        return
    evidence = json.loads(row[0]); choices = json.loads(evidence['choices_json'])
    part = TrustedOriginPart(message_id, 'body', revision, row[3])
    if not verify_current(db, archive, owner, chat_id, 'main', part):
        db.execute(text("UPDATE host_source_delivery_intent SET origin_state='superseded' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND origin_state='pending'"), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision})
        db.commit()
        return
    admission = TrustedOriginAdmission(message_id, owner, chat_id, 'main', message_id, None, None, None, choices['incognito'], choices['noMemory'], choices['compareMode'], evidence['capture_eligible'], 'trusted_fingerprint', evidence['policy_fingerprint'])
    archive.record_trusted_origin_admission(admission)
    archive.record_trusted_origin_completion(TrustedOriginCompletion(message_id, owner, chat_id, 'main', 'completed', (part,)))
    db.execute(text("UPDATE host_source_delivery_intent SET origin_state='delivered' WHERE owner=:owner AND chat_id=:chat AND message_id=:message AND revision=:revision AND origin_state='pending'"), {'owner': owner, 'chat': chat_id, 'message': message_id, 'revision': revision})
    db.commit()


_REVIEW_CURRENTNESS_SEAL = object()


class _ReviewCurrentness:
    """Change detector around already validated source/policy facts, not an authority."""
    def __init__(self, seal, owner, sources, assignment, coordinator, refs, files, observers):
        if seal is not _REVIEW_CURRENTNESS_SEAL: raise ValueError('review witness unavailable')
        self._seal=seal; self.owner=owner; self.sources=sources; self.assignment=assignment
        self.coordinator=coordinator; self.refs=refs; self.files=files; self.observers=observers; self.closed=False
        from src.openclank.memory_policy import _current_runtime_capability_token
        self.policy_capability_token=_current_runtime_capability_token()
        if self.policy_capability_token is None: raise ValueError('review capability lifecycle unavailable')
        self.source_owner=assignment.source_authority; self.archive=self.source_owner._archive
        self.workspaces=self.source_owner._workspaces; self.auth=owner.target_authority._auth
        self.account_id=owner.binding.source_account_id; self.username=self.source_owner._owner
        # Full canonical account/privilege resolution runs in this worker. The frame
        # only compares its bounded direct record and the unchanged backing file.
        if (self.auth.account_id(self.username)!=self.account_id or self.auth.username_for_account_id(self.account_id)!=self.username
            or self.auth.get_privileges(self.username).get('can_manage_memory') is not True):
            raise ValueError('review account unavailable')
        self.auth_users=self.auth.users
        self.auth_user=self.auth_users.get(self.username)
        if type(self.auth_users) is not dict or type(self.auth_user) is not dict:
            raise ValueError('review account unavailable')
        self.auth_user_count=len(self.auth_users)
        self.auth_admin=self.auth_user.get('is_admin')
        self.auth_privileges=self.auth_user.get('privileges')
        if self.auth_privileges is not None and type(self.auth_privileges) is not dict: raise ValueError('review privilege unavailable')
        self.auth_memory_privilege=self.auth_privileges.get('can_manage_memory',True) if self.auth_privileges is not None else True
        self.supervisor=owner.target_authority._supervisor
        self.local_epoch=self.supervisor._owner_lifecycle_epochs.get(owner.binding.memory_owner,0)
        self.epoch_path=str(self.supervisor._owner_epoch_path(owner.binding.memory_owner))
        self.epoch_stamp=self._file_stamp(self.epoch_path,allow_absent=True)
        if self.epoch_stamp[0]=='absent':
            if owner.binding.durable_epoch!='0': raise ValueError('review target unavailable')
        else:
            import os
            fd=os.open(self.epoch_path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0))
            try:
                st=os.fstat(fd)
                if (st.st_dev,st.st_ino)!=(self.epoch_stamp[0],self.epoch_stamp[1]) or st.st_size>64:
                    raise ValueError('review target unavailable')
                raw=os.read(fd,65)
            finally: os.close(fd)
            if raw.decode('ascii')!=owner.binding.durable_epoch or self._file_stamp(self.epoch_path)!=self.epoch_stamp:
                raise ValueError('review target unavailable')
        self.preparation=coordinator.projection._preparation
        self.provider=owner.provider
        self.owner_task=getattr(self.provider,'_owner_task',None)
        self.generation=getattr(self.provider,'_owned_generation',None)
        self.transport=getattr(self.provider,'_owned_transport',None)
        from core.database import engine
        self.engine=engine; self.engine_url=str(engine.url)
        self.archive_path=self.archive.db_path; self.workspace_path=self.workspaces.db_path; self.auth_path=self.auth.auth_path

    @staticmethod
    def _file_stamp(path, *, allow_absent=False):
        import os, stat
        try: item=os.stat(path,follow_symlinks=False)
        except FileNotFoundError:
            if not allow_absent: raise
            parent=os.stat(os.path.dirname(os.path.abspath(path)),follow_symlinks=False)
            if not stat.S_ISDIR(parent.st_mode): raise ValueError('review witness parent unavailable')
            return ('absent',parent.st_dev,parent.st_ino)
        if not stat.S_ISREG(item.st_mode) or item.st_nlink!=1: raise ValueError('review witness file unavailable')
        return (item.st_dev,item.st_ino,item.st_size,item.st_mtime_ns,item.st_ctime_ns)

    def assert_current(self):
        from src.openclank.memory_policy_projection import policy_coordinator
        import src.openclank.conversation_archive as archive_module
        from core.database import SessionLocal, engine
        from routes.prefs_routes import PREFS_FILE
        from src.settings import SETTINGS_FILE
        from src.openclank.memory_policy import STORE, MemoryPolicyStore, capabilities, _current_runtime_capability_token
        if self._seal is not _REVIEW_CURRENTNESS_SEAL or self.closed: raise ValueError('review witness unavailable')
        owner=self.owner; auth=owner.target_authority._auth
        if (self.assignment.owner!=self.username or self.assignment.source_authority is not self.source_owner
            or owner.mapping.owner!=self.username or auth is not self.auth
            or owner.account_lifecycle._session_factory is not SessionLocal or policy_coordinator() is not self.coordinator
            or (type(owner) is CompletedTurnLifecycleOwner and owner.source_authority is not self.source_owner) or self.source_owner._auth is not self.auth
            or self.source_owner._archive is not self.archive or archive_module._ARCHIVE is not self.archive
            or self.source_owner._workspaces is not self.workspaces or self.source_owner._session_factory is not SessionLocal
            or self.coordinator.projection._preparation is not self.preparation or owner.provider is not self.provider
            or getattr(self.provider,'_guarded_preparation',None) is not self.preparation
            or getattr(self.provider,'_owner_task',None) is not self.owner_task or self.owner_task is None or self.owner_task.done()
            or getattr(self.provider,'_owned_generation',None) is not self.generation or self.generation is None
            or getattr(self.provider,'_owned_transport',None) is not self.transport or self.transport is None or not self.transport.current(self.generation)
            or engine is not self.engine or SessionLocal.kw.get('bind') is not self.engine or str(engine.url)!=self.engine_url
            or self.archive.db_path!=self.archive_path or self.workspaces.db_path!=self.workspace_path or self.auth.auth_path!=self.auth_path
            or self.source_owner._policy is not STORE or self.assignment.policy_store is not STORE
            or STORE.capability_provider is not capabilities or STORE.graph_provider is not MemoryPolicyStore._graph
            or _current_runtime_capability_token() is not self.policy_capability_token
            or (str(__import__('pathlib').Path(PREFS_FILE).absolute()),str(__import__('pathlib').Path(SETTINGS_FILE).absolute()),str(__import__('pathlib').Path(self.auth_path).absolute()))!=tuple(path for path,_,_ in self.files)):
            raise ValueError('review authority changed')
        if not auth._config_lock.acquire(blocking=False): raise ValueError('review account busy')
        try:
            users=auth.users; user=users.get(self.username) if type(users) is dict else None
            if (users is not self.auth_users or len(users)!=self.auth_user_count or user is not self.auth_user
                or user.get('account_id')!=self.account_id or user.get('is_admin')!=self.auth_admin
                or user.get('privileges') is not self.auth_privileges
                or (self.auth_privileges.get('can_manage_memory',True) if self.auth_privileges is not None else True)!=self.auth_memory_privilege):
                raise ValueError('review account changed')
        finally: auth._config_lock.release()
        owner.profile.assert_current()
        supervisor=owner.target_authority._supervisor
        if (supervisor is not self.supervisor or owner.binding.memory_owner in supervisor._owner_lifecycle_blocked
            or supervisor._owner_lifecycle_epochs.get(owner.binding.memory_owner,0)!=self.local_epoch
            or self._file_stamp(self.epoch_path,allow_absent=True)!=self.epoch_stamp):
            raise ValueError('review target changed')
        for source,guard in self.sources:
            if source.account_id!=self.account_id: raise ValueError('review source changed')
            owner.mapping.validate_chat_guard(source.chat_id,guard)
        for path,stamp,allow_absent in self.files:
            if self._file_stamp(path,allow_absent=allow_absent)!=stamp: raise ValueError('review input changed')
        for path,identity,db,version in self.observers:
            import os
            st=os.stat(path,follow_symlinks=False)
            if (st.st_dev,st.st_ino)!=(identity[0],identity[1]) or st.st_nlink!=1 or db.execute('PRAGMA data_version').fetchone()!=(version,):
                raise ValueError('review database changed')
        if self.coordinator.projection.ready_refs_bounded(self.account_id,owner.binding.memory_workspace)!=self.refs:
            raise ValueError('review policy changed')
        for _,_,db,version in self.observers:
            if db.execute('PRAGMA data_version').fetchone()!=(version,): raise ValueError('review database changed')
        return None

    def close(self):
        if not self.closed:
            self.closed=True
            for _,_,db,_ in self.observers: db.close()


def mint_review_currentness(*, lifecycle_owner, sources, assignment, validate):
    """Validate in the caller's drained worker, then retain exact local change observers."""
    import os, sqlite3
    from pathlib import Path
    from core.auth import AuthManager
    from core.database import SessionLocal, engine
    from routes.prefs_routes import PREFS_FILE
    from src.settings import SETTINGS_FILE
    from src.memery_external_sources import CurrentHostUserSource, SourceRef
    from src.openclank.conversation_archive import ConversationArchive, get_conversation_archive
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.memory_job_admission import AssignmentAuthority
    from src.openclank.account_lifecycle import AccountOwnerLifecycle
    from src.openclank.memory_policy import STORE, MemoryPolicyStore, capabilities
    from src.openclank.memory_policy_projection import policy_coordinator
    owner=lifecycle_owner
    if (type(owner) not in (CompletedTurnLifecycleOwner,CompletedTurnLifecycle) or owner._seal is not _PUBLICATION_CAPABILITY_SEAL
        or type(sources) is not tuple or not sources or any(type(item) is not tuple or len(item)!=2 or type(item[0]) is not SourceRef for item in sources)
        or type(owner.account_lifecycle) is not AccountOwnerLifecycle or owner.account_lifecycle._session_factory is not SessionLocal
        or type(assignment) is not AssignmentAuthority or assignment.policy_store is not STORE
        or type(assignment.source_authority) is not CurrentHostUserSource
        or (type(owner) is CompletedTurnLifecycleOwner and owner.source_authority is not assignment.source_authority)
        or (type(owner) is CompletedTurnLifecycle and (len(sources)!=1 or sources[0][0]!=owner.source))
        or assignment.source_authority._session_factory is not SessionLocal or assignment.source_authority._policy is not STORE
        or type(assignment.source_authority._archive) is not ConversationArchive or assignment.source_authority._archive is not get_conversation_archive()
        or type(assignment.source_authority._workspaces) is not FilePolicyRepository or type(owner.target_authority._auth) is not AuthManager
        or STORE.capability_provider is not capabilities or STORE.graph_provider is not MemoryPolicyStore._graph
        or engine.dialect.name!='sqlite' or SessionLocal.kw.get('bind') is not engine or engine.url.query or not engine.url.database or engine.url.database==':memory:'
        or not callable(validate)):
        raise ValueError('review witness profile unavailable')
    raw_paths=(engine.url.database,assignment.source_authority._archive.db_path,assignment.source_authority._workspaces.db_path)
    if any(Path(path).is_symlink() for path in raw_paths): raise ValueError('review database unavailable')
    app,archive,workspace=(Path(path).resolve(strict=True) for path in raw_paths)
    coordinator=policy_coordinator()
    if coordinator is None or coordinator.projection._preparation is not owner.provider._guarded_preparation:
        raise ValueError('review policy projection unavailable')
    paths=((PREFS_FILE,True),(SETTINGS_FILE,True),(owner.target_authority._auth.auth_path,False))
    files=tuple((str(Path(path).absolute()),_ReviewCurrentness._file_stamp(path,allow_absent=allow_absent),allow_absent) for path,allow_absent in paths)
    observers=[]; witness=None
    try:
        seen=set()
        for path in (str(app),str(archive),str(workspace)):
            st=os.stat(path,follow_symlinks=False);identity=(st.st_dev,st.st_ino)
            if st.st_nlink!=1: raise ValueError('review database unavailable')
            if identity in seen: continue
            seen.add(identity)
            db=sqlite3.connect(Path(path).as_uri()+'?mode=ro',uri=True,timeout=0.05,isolation_level=None,check_same_thread=False)
            db.execute('PRAGMA query_only=ON')
            observers.append((path,(st.st_dev,st.st_ino),db,db.execute('PRAGMA data_version').fetchone()[0]))
        refs=coordinator.checked_refs(assignment.owner,owner.binding.source_account_id,owner.binding.memory_workspace)
        witness=_ReviewCurrentness(_REVIEW_CURRENTNESS_SEAL,owner,sources,assignment,coordinator,refs,files,tuple(observers))
        witness.assert_current()
        result=validate()
        if result is False:
            raise ValueError('review validation unavailable')
        witness.assert_current()
        return witness,result
    except BaseException:
        if witness is not None: witness.close()
        else:
            for _,_,db,_ in observers: db.close()
        raise


async def associate_terminal_error(*, state, owner, account, chat_id, root_id, host_assistant_id,
                                   admission, barrier, origin, credential_current, witness):
    """Bind a verified failed Native output without creating success evidence."""
    import asyncio
    from core.database import SessionLocal
    from src.openclank.account_request_barrier import AccountRequestBarrier, _Admission
    from src.openclank.acp_supervisor_adapter import AcpSupervisorAdapter
    from src.openclank.conversation_archive import get_conversation_archive
    from src.openclank.memory_policy import STORE
    from src.openclank.session_map import OwnerSessionMap

    supervisor = getattr(state, 'mimo_supervisor', None)
    auth = getattr(state, 'auth_manager', None)
    if (type(admission) is not _Admission or not isinstance(barrier, AccountRequestBarrier)
            or getattr(state, 'account_request_barrier', None) is not barrier
            or not isinstance(supervisor, AcpSupervisorAdapter) or auth is None
            or type(origin) is not HostOriginAdmission or not origin.capture_eligible
            or origin.owner != account or origin.chat_id != chat_id or origin.root_operation_id != root_id
            or not callable(credential_current) or type(witness) is not dict
            or set(witness) != {'identity', 'messageID', 'messageUpdated', 'diagnostic', 'loggingPolicy'}
            or not await barrier.is_live(owner, admission)):
        return False
    identity = witness['identity']
    if (type(identity) is not dict or identity.get('rootOperationID') != root_id
            or identity.get('actorID') != 'main' or type(identity.get('sessionID')) is not str
            or type(witness['messageID']) is not str or type(witness['messageUpdated']) is not int):
        return False
    pool = supervisor.owner_lifecycle_pool()
    policy = witness['loggingPolicy']
    if (type(policy) is not dict or set(policy) != {'bindingID','bindingRevision','policyRevision','holderID'}
            or type(policy.get('holderID')) is not str or not policy['holderID']): return False
    worker = pool.worker_for_owner(owner)
    bridge = getattr(worker, '_bridge', None) if worker else None
    callbacks = getattr(bridge, '_logging_callbacks', None) if bridge else None
    policy_reader = getattr(getattr(callbacks, '_capture', None), 'actor_diagnostic_policy', None)
    process = getattr(worker, '_proc', None) if worker else None
    actor_identity = {k:identity[k] for k in ('sessionID','rootOperationID','actorID','executionRevision')}
    if not worker or not process or not callable(policy_reader): return False
    mapping = OwnerSessionMap(pool._runtime_home(owner) / 'session-map.json', owner)
    archive = get_conversation_archive()
    async with mapping.async_chat_admission_lock(chat_id, blocking=False, existing_only=True) as guard:
        if guard is None or not await barrier.is_live(owner, admission): return False
        def base_current():
            try:
                mapping.validate_chat_guard(chat_id, guard)
                if (admission.released or admission.owner != owner.strip().lower() or barrier._fenced(admission.owner)
                        or getattr(state, 'account_request_barrier', None) is not barrier
                        or getattr(state, 'mimo_supervisor', None) is not supervisor
                        or getattr(state, 'auth_manager', None) is not auth
                        or supervisor.owner_lifecycle_pool() is not pool or credential_current() is not True
                        or pool.worker_for_owner(owner) is not worker or getattr(worker, '_proc', None) is not process
                        or not worker.is_alive() or getattr(worker, '_bridge', None) is not bridge
                        or getattr(bridge, '_logging_callbacks', None) is not callbacks
                        or getattr(callbacks, 'owner', None) != owner or getattr(callbacks, 'holder_id', None) != policy['holderID']
                        or getattr(bridge, '_managed_provider_generation', None) != policy['holderID']
                        or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner): return False
                privileges = auth.get_privileges(owner)
                if type(privileges) is not dict: return False
                allowed = bool(privileges.get('can_manage_memory', True))
                snapshot = STORE.effective_snapshot(owner, read_privilege=allowed, write_privilege=allowed,
                    incognito=False, no_memory=False, compare_mode=False)
                return (snapshot.get('eligibility_state') == 'available' and snapshot.get('capture_allowed') is True
                        and snapshot.get('fingerprint') == origin.policy_fingerprint)
            except Exception: return False
        def current():
            try:
                return bool(base_current() and policy_reader(owner, policy['holderID'], actor_identity, account, base_current) == policy)
            except Exception: return False
        async def exact_native():
            if not current() or not await barrier.is_live(owner, admission): return False
            try:
                value = await pool.session_terminal_diagnostic_source(chat_id, identity['sessionID'], witness['messageID'], owner=owner, timeout=2.0)
                from src.openclank.terminal_diagnostic_projection import _NAMES
                marker = value.get('diagnostic') if type(value) is dict else None
                return (type(value) is dict and set(value) == {'version','sessionID','messageID','identity','diagnostic','messageUpdated'}
                    and type(value.get('version')) is int and value['version'] == 1 and value.get('identity') == identity
                    and value.get('sessionID') == identity['sessionID'] and value.get('messageID') == witness['messageID']
                    and type(value.get('messageUpdated')) is int and 0 <= value['messageUpdated'] <= 9007199254740991
                    and marker == witness['diagnostic'] and type(marker) is dict and set(marker) == {'version','errorName','stage'}
                    and type(marker.get('version')) is int and marker['version'] == 1
                    and marker.get('errorName') in _NAMES and marker.get('stage') == 'processor' and current()
                    and await barrier.is_live(owner, admission))
            except Exception: return False
        if not await exact_native(): return False
        def write():
            with SessionLocal() as db:
                return record_terminal_error_association(db, archive, owner=account, chat_id=chat_id,
                    root_operation_id=root_id, host_assistant_id=host_assistant_id, identity=identity,
                    native_message_id=witness['messageID'], diagnostic=witness['diagnostic'], current_authority=current)
        task = asyncio.create_task(asyncio.to_thread(write))
        try:
            recorded = await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try: await asyncio.shield(task)
                except asyncio.CancelledError: continue
            try: task.result()
            except Exception: pass
            raise
        return bool(recorded and current() and await barrier.is_live(owner, admission))
