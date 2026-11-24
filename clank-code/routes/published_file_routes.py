"""Owner management and opaque download routes for agent-published files."""

from __future__ import annotations

from typing import Optional, Literal
import hashlib
import os
import tempfile
import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.auth_helpers import effective_user
from src.published_files import PublishedFileError, PublishedFileService
from src.settings import load_settings


class GrantRequest(BaseModel):
    audience: str = "owner"
    expires_in_hours: Optional[int] = None



class ShareResource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resource_ref: str = Field(min_length=8, max_length=16384)
    expected_revision: dict


class ShareDependency(ShareResource):
    name: str = Field(min_length=1, max_length=1024)


class TypedShareRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['snapshot','live'] = 'snapshot'
    audience: Literal['owner','public']
    expires_in_hours: Optional[int] = None
    resource: ShareResource
    dependencies: list[ShareDependency] = Field(default_factory=list, max_length=32)


class ResourceRoleListRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resource: ShareResource


class ResourceRoleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resource: ShareResource
    recipient_account_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    recipient_username: Optional[str] = Field(default=None, min_length=1, max_length=256)
    role: Literal['viewer','commenter','editor']
    expires_unix_ms: Optional[int] = None

    @model_validator(mode='after')
    def one_recipient(self):
        if (self.recipient_account_id is None) == (self.recipient_username is None):
            raise ValueError('Exactly one recipient selector is required')
        return self

def _origin() -> str:
    return str(load_settings().get("app_public_url") or "").strip()


def _owner(request: Request) -> str:
    return str(effective_user(request) or "").strip().lower()


def setup_published_file_routes(service: Optional[PublishedFileService] = None) -> APIRouter:
    service = service or PublishedFileService()
    router = APIRouter(prefix="/api/files", tags=["files"])

    def _named_human(request):
        from src.auth_helpers import require_user, is_internal_tool_identity
        user = require_user(request)
        auth = getattr(request.app.state, 'auth_manager', None)
        if (is_internal_tool_identity(user) or getattr(request.state,'api_token',False)
                or getattr(request.state,'internal_tool_authenticated',False) or _owner(request) != user or not auth):
            raise HTTPException(403, 'A named user is required')
        token = request.cookies.get('open_clank_session')
        if not token or auth.get_username_for_token(token) != user: raise HTTPException(403, 'Session unavailable')
        account = auth.account_id(user)
        if not account or auth.username_for_account_id(account) != user: raise HTTPException(403, 'Account unavailable')
        return user, account, auth

    def _source_context(request, *, owner, account, auth, named_human=False):
        from src.openclank.files_facade import ProviderContext
        state = request.app.state
        token = request.cookies.get('open_clank_session') if named_human else None
        store = getattr(state,'files_policy_repository',None); barrier = getattr(state,'account_request_barrier',None)
        factory = getattr(state,'files_facade_factory',None)
        if not store or not barrier or not callable(factory): raise ValueError('source unavailable')
        generation=store.generation(); admin=auth.is_admin(owner)
        context=ProviderContext(owner_subject_id=account,owner_username=owner,policy_generation=generation,is_admin=admin,workspace_id=None)
        provider=factory(request,context)._providers.get('host')
        if provider is None or getattr(provider,'operation_store',None) is not store: raise ValueError('source unavailable')
        def current():
            try:return (getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                and getattr(state,'files_policy_repository',None) is store and getattr(state,'files_facade_factory',None) is factory
                and not barrier._fenced(owner) and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                and (not named_human or bool(token) and auth.get_username_for_token(token)==owner)
                and auth.is_admin(owner)==admin and store.generation()==generation)
            except Exception:return False
        if not current():raise ValueError('source changed')
        return store,barrier,context,provider,current

    def _capture_chat_credential(request):
        """Capture the original named credential before any request-body await."""
        from src.auth_helpers import require_user, is_internal_tool_identity
        user = require_user(request)
        state = request.app.state
        auth = getattr(state, 'auth_manager', None)
        factory = getattr(state, 'capture_original_credential', None)
        if (is_internal_tool_identity(user) or getattr(request.state, 'api_token', False)
                or getattr(request.state, 'internal_tool_authenticated', False)
                or _owner(request) != user or auth is None or not callable(factory)):
            raise HTTPException(403, 'A named user is required')
        account = auth.account_id(user)
        if not account or auth.username_for_account_id(account) != user:
            raise HTTPException(403, 'Account unavailable')
        credential = factory(request, user, account)
        if not callable(credential) or credential() is not True:
            raise HTTPException(403, 'Session unavailable')
        return user, account, auth, factory, credential

    async def _bounded_json(request, maximum=4096):
        import json
        chunks = []; size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > maximum: raise HTTPException(413, 'Request too large')
            chunks.append(chunk)
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('duplicate key')
                result[key] = value
            return result
        try:
            value = json.loads(b''.join(chunks), object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Invalid request')
        return value

    def _chat_source_authority(request, *, owner, account, auth, chat, workspace, captured=None):
        """Fresh retained-chat authority; no Native worker or transcript revision."""
        from core.database import SessionLocal, Session as DbSession
        from src.openclank.conversation_archive import get_conversation_archive
        state = request.app.state
        barrier = getattr(state, 'account_request_barrier', None)
        store = getattr(state, 'files_policy_repository', None)
        archive = get_conversation_archive()
        if barrier is None or (workspace is not None and store is None):
            raise ValueError('chat source unavailable')
        generation = store.generation() if store is not None else None
        def current():
            try:
                if (getattr(state, 'auth_manager', None) is not auth
                        or getattr(state, 'account_request_barrier', None) is not barrier
                        or auth.account_id(owner) != account or auth.username_for_account_id(account) != owner
                        or barrier._fenced(owner)):
                    return False
                if captured is not None:
                    user, original_account, original_auth, factory, credential = captured
                    if (user != owner or original_account != account or original_auth is not auth
                            or getattr(state, 'capture_original_credential', None) is not factory
                            or credential() is not True): return False
                with SessionLocal() as db:
                    row = db.query(DbSession).filter(DbSession.id == chat, DbSession.owner == owner).first()
                    if row is None or bool(row.archived) or row.workspace_id != workspace: return False
                if archive.chat_erasure_started(owner=owner, chat_id=chat): return False
                if workspace is not None:
                    if getattr(state, 'files_policy_repository', None) is not store or store.generation() != generation: return False
                    selected = store.get_workspace(workspace)
                    location = store.get_location(selected.location_id)
                    if (selected.archived or selected.owner_subject_id != account or not location.enabled
                            or location.availability != 'available'): return False
                    scope = store.resolve(subject_id=account, is_admin=auth.is_admin(owner), origin='app',
                        location_id=location.id, workspace_id=workspace, relative_resource=selected.relative_folder,
                        capability='read', observation_only=True)
                    if not scope.allowed or scope.policy_generation != generation: return False
                return True
            except Exception:
                return False
        return barrier, current

    from src.openclank.shared_resource_authority import location_for as _location_for

    async def _read_host(provider, context, resource, current, *, require_selected_revision=False):
        from dataclasses import replace
        from src.openclank.files_host_provider import _path
        scoped=replace(context,workspace_id=resource['workspace_id'])
        if not current():raise ValueError('source changed')
        before=await provider.stat_inspection(scoped,origin_id=resource['origin_id'])
        content=await provider.content(scoped,origin_id=resource['origin_id'])
        if (before.kind!='file' or before.origin_id!=resource['origin_id'] or type(before.size) is not int
                or before.size < 0 or before.size > 64*1024*1024 or content.origin_id!=resource['origin_id']
                or content.size!=before.size or not content.stream or not isinstance(before.revision,dict)
                or before.revision.get('kind')!='hostObjectIdentity' or before.revision.get('value')!=content.etag):
            raise ValueError('source unavailable')
        data=bytearray()
        async for chunk in content.stream(0,before.size):
            if not current() or len(data)+len(chunk)>before.size:raise ValueError('source changed')
            data.extend(chunk)
        after=await provider.stat_inspection(scoped,origin_id=resource['origin_id'])
        if (not current() or len(data)!=before.size or after.kind!='file' or after.origin_id!=before.origin_id
                or after.size!=before.size or after.revision!=before.revision):raise ValueError('source changed')
        revision=resource['revision']
        if require_selected_revision and revision['kind']=='hostFingerprint':
            observed=f"sha256:{hashlib.sha256(data).hexdigest()}:{len(data)}"
            if observed!=revision['value']:
                raise ValueError('source changed')
        return bytes(data),content.filename,content.media_type

    async def _selected(request, selection, *, owner, account, store, context, provider, current):
        from dataclasses import replace
        from src.openclank.files_host_provider import _path
        from src.openclank.resource_refs import resolve_resource_ref
        ref=resolve_resource_ref(selection.resource_ref,expected_owner_subject_id=account,
            current_policy_generation=context.policy_generation,required_capability='download')
        if ref.provider!='host' or ref.kind!='file' or 'stat' not in ref.capabilities:raise ValueError('source unavailable')
        scoped=replace(context,workspace_id=ref.workspace_id)
        entry=await provider.stat(scoped,origin_id=ref.origin_id)
        path=await provider._authorized_path(scoped,ref.origin_id)
        if (not current() or entry.kind!='file' or entry.origin_id!=ref.origin_id or path!=_path(ref.origin_id)
                or os.path.realpath(path)!=path or type(entry.revision) is not dict or entry.revision!=selection.expected_revision):
            raise ValueError('source changed')
        location=_location_for(store,scoped,path,workspace_id=ref.workspace_id,exact_location=ref.location_id)
        return {'provider':'host','origin_id':ref.origin_id,'resource_id':ref.stable_id,'location_id':location.id,
                'workspace_id':ref.workspace_id,'revision':dict(entry.revision)}

    @router.post('/share')
    async def publish_resource(request: Request, body: TypedShareRequest):
        """Explicit named-human publication of one selected Host resource."""
        from src.openclank.files_host_provider import _path
        user,account,auth=_named_human(request)
        created=[]
        try:
            store,barrier,context,provider,current=_source_context(request,owner=user,account=account,auth=auth,named_human=True)
            async with barrier.admitted(user):
                main=await _selected(request,body.resource,owner=user,account=account,store=store,context=context,provider=provider,current=current)
                selected=[]
                for dependency in body.dependencies:
                    resource=await _selected(request,dependency,owner=user,account=account,store=store,context=context,provider=provider,current=current)
                    selected.append({'name':dependency.name,'resource':resource,'snapshot_file_id':None})
                # Validation runs before any publication or grant is created.
                from src.published_files import _typed_target
                probe={'version':1,'mode':'live','resource':main,'dependencies':selected}
                _typed_target(probe,owner_subject_id=account)
                async def snapshot(resource):
                    data,name,_mime=await _read_host(provider,context,resource,current,require_selected_revision=True)
                    if not data:raise ValueError('empty snapshot unavailable')
                    os.makedirs(service.storage_root,mode=0o700,exist_ok=True)
                    fd,path=tempfile.mkstemp(prefix='.share-',dir=service.storage_root)
                    try:
                        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                        target={'version':1,'mode':'snapshot','resource':resource,'dependencies':[]}
                        result=service.publish(path,owner=user,owner_subject_id=account,typed_target=target,filename_override=name,audience='owner')
                        created.append(result['id']);return result
                    finally:
                        try:os.unlink(path)
                        except FileNotFoundError:pass
                if body.mode=='snapshot':
                    for dependency in selected:
                        child=await snapshot(dependency['resource']);dependency['snapshot_file_id']=child['id']
                    data,name,_mime=await _read_host(provider,context,main,current,require_selected_revision=True)
                    if not data:raise ValueError('empty snapshot unavailable')
                    os.makedirs(service.storage_root,mode=0o700,exist_ok=True)
                    fd,path=tempfile.mkstemp(prefix='.share-',dir=service.storage_root)
                    try:
                        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
                        target={'version':1,'mode':'snapshot','resource':main,'dependencies':selected}
                        result=service.publish(path,owner=user,owner_subject_id=account,typed_target=target,filename_override=name,
                            audience=body.audience,expires_in_hours=body.expires_in_hours,public_origin=_origin())
                        created.append(result['id'])
                    finally:
                        try:os.unlink(path)
                        except FileNotFoundError:pass
                else:
                    data,name,mime=await _read_host(provider,context,main,current)
                    target={'version':1,'mode':'live','resource':main,'dependencies':selected}
                    result=service.create_live(owner=user,owner_subject_id=account,filename=name,mime_type=mime,size=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),typed_target=target,audience=body.audience,
                        expires_in_hours=body.expires_in_hours,public_origin=_origin())
                    created.append(result['id'])
                # Source selection must still match the exact reviewed revisions
                # before exposing the opaque public capability.
                for selection,expected in [(body.resource,main),*[(d,x['resource']) for d,x in zip(body.dependencies,selected)]]:
                    if await _selected(request,selection,owner=user,account=account,store=store,context=context,provider=provider,current=current)!=expected:
                        raise ValueError('source changed')
                if not current():raise ValueError('source changed')
                result['target']={'version':1,'mode':body.mode,'resource_id':main['resource_id'],'revision':main['revision'],
                    'dependencies':[{'name':x['name'],'resource_id':x['resource']['resource_id']} for x in selected]}
                return result
        except BaseException as exc:
            for identifier in reversed(created):
                try:service.delete(identifier,owner=user)
                except Exception:pass
            if isinstance(exc, asyncio.CancelledError): raise
            raise HTTPException(409,'Publication unavailable or changed') from exc

    @router.post('/share/feedback-targets')
    async def owned_feedback_targets(request: Request, response: Response, body: ResourceRoleListRequest):
        """Current owner navigation candidates for the selected resource only."""
        response.headers['Cache-Control']='private, no-store'
        user,account,auth=_named_human(request)
        try:
            store,barrier,context,provider,current=_source_context(request,owner=user,account=account,auth=auth,named_human=True)
            async with barrier.admitted(user):
                selected=await _selected(request,body.resource,owner=user,account=account,store=store,context=context,provider=provider,current=current)
                rows,partial=await asyncio.to_thread(service.owned_live_feedback_targets,owner=user,owner_subject_id=account,resource_id=selected['resource_id'])
                if not current() or await _selected(request,body.resource,owner=user,account=account,store=store,context=context,provider=provider,current=current)!=selected:
                    raise ValueError('source changed')
                return {'version':1,'items':rows,'partial':partial}
        except Exception as exc: raise HTTPException(409,'Feedback targets unavailable or changed') from exc

    @router.post('/share/roles')
    async def create_resource_role(request: Request, body: ResourceRoleRequest):
        """Produce an exact known-user policy role; this does not enable editing."""
        from src.openclank.files_host_provider import _path
        user,account,auth=_named_human(request)
        try:
            if body.recipient_username is not None:
                username = body.recipient_username.strip().lower()
                recipient_account = auth.account_id(username) if username else None
                recipient = auth.username_for_account_id(recipient_account) if recipient_account else None
                if not recipient or recipient.strip().lower() != username: raise ValueError('recipient unavailable')
            else:
                recipient_account = body.recipient_account_id
                recipient = auth.username_for_account_id(recipient_account)
            if not recipient or not recipient_account or auth.account_id(recipient) != recipient_account:
                raise ValueError('recipient unavailable')
            store,barrier,context,provider,current=_source_context(request,owner=user,account=account,auth=auth,named_human=True)
            async with barrier.admitted(user):
                resource=await _selected(request,body.resource,owner=user,account=account,store=store,context=context,provider=provider,current=current)
                if (not current() or auth.account_id(recipient) != recipient_account
                        or auth.username_for_account_id(recipient_account) != recipient): raise ValueError('source changed')
                location=store.get_location(resource['location_id']);path=_path(resource['origin_id'])
                relative='' if path==location.canonical_path else os.path.relpath(path,location.canonical_path)
                binding=store.create_resource_role(owner_subject_id=account,recipient_subject_id=recipient_account,
                    location_id=resource['location_id'],resource_id=resource['resource_id'],role=body.role,
                    owner_is_admin=context.is_admin,expected_generation=context.policy_generation,expires_unix_ms=body.expires_unix_ms,
                    owner_workspace_id=resource['workspace_id'],relative_resource=relative)
                return {'version':1,'binding_id':binding.id,'resource_id':resource['resource_id'],'recipient_account_id':recipient_account,
                        'role':body.role,'expires_unix_ms':binding.expires_unix_ms,'policy_generation':binding.generation}
        except Exception as exc:
            raise HTTPException(409,'Resource role unavailable or changed') from exc

    @router.post('/share/roles/list')
    async def list_resource_roles(request: Request, response: Response, body: ResourceRoleListRequest):
        """List only current owner's roles on the currently selected resource."""
        response.headers['Cache-Control'] = 'private, no-store'
        user,account,auth=_named_human(request)
        try:
            store,barrier,context,provider,current=_source_context(request,owner=user,account=account,auth=auth,named_human=True)
            async with barrier.admitted(user):
                resource=await _selected(request,body.resource,owner=user,account=account,store=store,context=context,provider=provider,current=current)
                if not current(): raise ValueError('source changed')
                rows,partial=store.list_owned_resource_roles(owner_subject_id=account,
                    location_id=resource['location_id'],resource_id=resource['resource_id'])
                items=[]
                for row in rows:
                    recipient=auth.username_for_account_id(row['recipient_subject_id'])
                    if not recipient or auth.account_id(recipient)!=row['recipient_subject_id']:
                        partial=True; continue
                    items.append({'binding_id':row['binding_id'],'recipient_username':recipient,
                                  'role':row['role'],'expires_unix_ms':row['expires_unix_ms']})
                if not current(): raise ValueError('source changed')
                return {'items':items,'partial':partial}
        except Exception as exc:
            raise HTTPException(409,'Resource roles unavailable or changed') from exc

    @router.post('/share/roles/{binding_id}/revoke')
    async def revoke_resource_role(request: Request, binding_id: str):
        user,account,auth=_named_human(request)
        try:
            store,barrier,_context,_provider,current=_source_context(request,owner=user,account=account,auth=auth,named_human=True)
            async with barrier.admitted(user):
                binding=store.get_binding(binding_id)
                if (not current() or not binding or binding.created_by_subject_id!=account or binding.binding_class!='people'
                        or binding.subject_kind!='user' or not binding.resource_ref
                        or binding.operation not in {'share-role:v1:viewer','share-role:v1:commenter','share-role:v1:editor'}):
                    raise ValueError('role unavailable')
                updated=store.update_binding(binding_id,actor_subject_id=account,enabled=False)
                return {'version':1,'binding_id':updated.id,'state':'revoked','policy_generation':updated.generation}
        except Exception as exc:
            raise HTTPException(404,'Resource role unavailable') from exc

    @router.get("/library")
    async def list_files(
        request: Request,
        search: str = Query(""),
        limit: int = Query(200, ge=1, le=500),
    ):
        files = service.list(owner=_owner(request), search=search, limit=limit)
        return {"files": files, "total": len(files)}

    @router.get('/shared-with-me')
    async def shared_with_me(request: Request, response: Response, cursor: Optional[str] = Query(default=None, max_length=4096)):
        """Bounded current recipient discovery; candidates are not authority."""
        response.headers['Cache-Control'] = 'private, no-store'
        import json, re, time
        from contextlib import AsyncExitStack
        from src.secret_storage import encrypt, decrypt, is_encrypted
        from src.openclank.shared_resource_authority import recipient_live_target
        user, account, auth = _named_human(request)
        state = request.app.state
        store = getattr(state,'files_policy_repository',None)
        barrier = getattr(state,'account_request_barrier',None)
        factory = getattr(state,'files_facade_factory',None)
        token = request.cookies.get('open_clank_session')
        if not store or not barrier or not callable(factory): raise HTTPException(409,'Shared files unavailable')
        generation = store.generation()
        async def current():
            try:
                return (getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                    and getattr(state,'files_policy_repository',None) is store and getattr(state,'files_facade_factory',None) is factory
                    and not barrier._fenced(user) and auth.account_id(user)==account and auth.username_for_account_id(account)==user
                    and bool(token) and auth.get_username_for_token(token)==user and store.generation()==generation)
            except Exception: return False
        try:
            if not await current(): raise ValueError('recipient changed')
            owner_ids, overflow = store.recipient_share_role_owners(recipient_subject_id=account)
            owners = []
            owner_pairs = []
            for owner_id in owner_ids:
                name = auth.username_for_account_id(owner_id)
                if name and auth.account_id(name)==owner_id:
                    owners.append(name); owner_pairs.append((owner_id,name))
            fingerprint = hashlib.sha256(json.dumps(owner_pairs,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            after = ''
            if cursor:
                if not cursor.startswith('sw1.') or not is_encrypted('enc:'+cursor[4:]): raise ValueError('cursor invalid')
                data = json.loads(decrypt('enc:'+cursor[4:]))
                if (type(data) is not dict or set(data)!={'version','account','generation','owners','after','expires'}
                        or data['version']!=1 or data['account']!=account or type(data['generation']) is not int
                        or data['generation']!=generation or data['owners']!=fingerprint
                        or type(data['expires']) is not int or data['expires'] < int(time.time())
                        or type(data['after']) is not str or not re.fullmatch(r'[0-9a-f]{32}',data['after'])):
                    raise ValueError('cursor invalid')
                after = data['after']
            if not await current(): raise ValueError('recipient changed')
            if not owners:
                return {'items':[], 'next_cursor':None, 'partial':overflow}
            rows = service.shared_candidates(owners=owners, after_id=after)
            items=[]; held=[]; examined=0; last=after
            async with AsyncExitStack() as stack:
                for row in rows[:64]:
                    examined += 1; last = row['id']
                    try:
                        target = await stack.enter_async_context(recipient_live_target(service=service,
                            publication_id=row['id'], recipient_account=account, auth=auth, barrier=barrier, store=store,
                            provider_for_owner=lambda ctx: factory(request,ctx)._providers.get('host'), original_current=current))
                        if not await target.current(): continue
                        items.append({'publication_id':row['id'], 'resource_id':target.resource['resource_id'],
                                      'filename':row['filename'], 'role':target.role})
                        held.append((row,target))
                    except (ValueError, KeyError):
                        continue
                    if len(items) == 20: break
                for row,target in held:
                    if not await target.current() or service.shared_candidate(file_id=row['id'],owners=owners) != row:
                        raise ValueError('shared file changed')
                if not await current(): raise ValueError('recipient changed')
                more = examined < len(rows)
                next_cursor = None
                if more and last:
                    payload = {'version':1,'account':account,'generation':generation,'owners':fingerprint,
                               'after':last,'expires':int(time.time())+900}
                    sealed = encrypt(json.dumps(payload,ensure_ascii=False,separators=(',',':')))
                    if not sealed.startswith('enc:'): raise ValueError('cursor unavailable')
                    next_cursor = 'sw1.'+sealed[4:]
                return {'items':items,'next_cursor':next_cursor,'partial':bool(overflow or more)}
        except Exception as exc:
            raise HTTPException(409,'Shared files unavailable or changed') from exc

    @router.post("/chat-snapshots/prepare")
    async def prepare_chat_snapshot(request: Request):
        from routes.history.history_routes import _read_complete_chat_snapshot
        from src.published_files import render_chat_snapshot, _chat_snapshot_id
        captured = _capture_chat_credential(request)
        body = await _bounded_json(request)
        if type(body) is not dict or set(body) != {'chat_id'} or not _chat_snapshot_id(body['chat_id']):
            raise HTTPException(400, 'Invalid request')
        snapshot = await _read_complete_chat_snapshot(request, body['chat_id'], request.app.state.session_manager, captured=captured)
        try:
            data, text_sha, review_sha = render_chat_snapshot(snapshot)
            user, account, auth, _, _ = captured
            barrier, current = _chat_source_authority(request, owner=user, account=account, auth=auth,
                chat=snapshot['chat_id'], workspace=snapshot['workspace_id'], captured=captured)
            async with barrier.admitted(user):
                if not current(): raise ValueError('source changed')
                return JSONResponse({'version':1, 'chat_id':snapshot['chat_id'], 'text':data.decode('utf-8'),
                    'source_sha256':snapshot['source_sha256'], 'text_sha256':text_sha, 'review_sha256':review_sha,
                    'notice':'This immutable snapshot includes the displayed conversation text. Access remains subject to the chat and workspace being available.'},
                    headers={'Cache-Control':'private, no-store'})
        except Exception as exc:
            raise HTTPException(409, 'Chat snapshot unavailable or changed') from exc

    @router.post("/chat-snapshots")
    async def create_chat_snapshot(request: Request):
        from routes.history.history_routes import _read_complete_chat_snapshot
        from src.published_files import render_chat_snapshot, _chat_snapshot_id, _chat_snapshot_hash
        captured = _capture_chat_credential(request)
        body = await _bounded_json(request)
        if (type(body) is not dict or not {'chat_id','expected_review_sha256'} <= set(body)
                or set(body) - {'chat_id','expected_review_sha256','audience','expires_in_hours'}
                or not _chat_snapshot_id(body['chat_id']) or not _chat_snapshot_hash(body['expected_review_sha256'])
                or body.get('audience','owner') not in ('owner','public')
                or (body.get('expires_in_hours') is not None and type(body['expires_in_hours']) is not int)):
            raise HTTPException(400, 'Invalid request')
        user, account, auth, _, _ = captured
        created = []
        async def publish_snapshot(snapshot, revalidate):
            data, text_sha, review_sha = render_chat_snapshot(snapshot)
            if review_sha != body['expected_review_sha256']: raise ValueError('review changed')
            _, current = _chat_source_authority(request, owner=user, account=account, auth=auth,
                chat=snapshot['chat_id'], workspace=snapshot['workspace_id'], captured=captured)
            if not current(): raise ValueError('source changed')
            os.makedirs(service.storage_root, mode=0o700, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix='.chat-snapshot-', dir=service.storage_root)
            try:
                with os.fdopen(fd, 'wb') as staged:
                    staged.write(data); staged.flush(); os.fsync(staged.fileno())
                await revalidate()
                if not current(): raise ValueError('source changed')
                target = {'version':2, 'mode':'snapshot', 'chat':{'id':snapshot['chat_id'],
                    'workspace_id':snapshot['workspace_id'], 'source_sha256':snapshot['source_sha256'],
                    'text_sha256':text_sha}, 'dependencies':[]}
                result = service.publish(path, owner=user, owner_subject_id=account, typed_target=target,
                    filename_override='conversation-snapshot.txt', audience=body.get('audience','owner'),
                    expires_in_hours=body.get('expires_in_hours'), public_origin=_origin())
                created.append(result['id'])
                await revalidate()
                if not current(): raise ValueError('source changed')
                return result
            finally:
                try: os.unlink(path)
                except FileNotFoundError: pass
        try:
            result = await _read_complete_chat_snapshot(request, body['chat_id'], request.app.state.session_manager,
                captured=captured, continuation=publish_snapshot)
            return JSONResponse(result, headers={'Cache-Control':'private, no-store'})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if created:
                try: service.delete(created[0], owner=user)
                except Exception: pass
            raise HTTPException(503, 'Publication unavailable, changed, or outcome unknown') from exc

    @router.get("/{file_id}/content")
    async def owner_download(request: Request, file_id: str):
        info = service.get_owned(file_id, owner=_owner(request))
        if not info:
            raise HTTPException(404, "File not found")
        return FileResponse(
            info["path"],
            media_type=info["mime_type"],
            filename=info["filename"],
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @router.get("/download/{token}")
    async def granted_download(request: Request, token: str, asset: Optional[str] = Query(default=None, max_length=1024)):
        info = service.resolve_grant(token)
        if not info: raise HTTPException(404, "File not found")
        if info["audience"] == "owner":
            user = _owner(request)
            auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
            is_admin = bool(auth_manager and user and auth_manager.is_admin(user))
            if user != info["owner"] and not is_admin: raise HTTPException(404, "File not found")
        target=info.get('_target')
        if target is None:
            if asset is not None: raise HTTPException(404, "File not found")
            return FileResponse(info["path"],media_type=info["mime_type"],filename=info["filename"],
                headers={"X-Content-Type-Options":"nosniff","Cache-Control":"private, no-store"})
        try:
            if target.get('version') == 2:
                if asset is not None: raise ValueError('chat snapshot has no dependencies')
                account = info['_owner_subject_id']; auth = getattr(request.app.state, 'auth_manager', None)
                owner = auth.username_for_account_id(account) if auth else None
                if not owner or info['owner'] != owner: raise ValueError('owner unavailable')
                chat = target['chat']
                barrier, current = _chat_source_authority(request, owner=owner, account=account, auth=auth,
                    chat=chat['id'], workspace=chat['workspace_id'])
                async with barrier.admitted(owner):
                    refreshed = service.resolve_grant(token)
                    if (not current() or not refreshed or refreshed.get('grant_id') != info['grant_id']
                            or refreshed.get('_target') != target or refreshed.get('_owner_subject_id') != account):
                        raise ValueError('grant changed')
                    return FileResponse(info['path'], media_type=info['mime_type'], filename=info['filename'],
                        headers={'X-Content-Type-Options':'nosniff', 'Cache-Control':'private, no-store'})
            from dataclasses import replace
            from urllib.parse import quote
            from src.openclank.files_host_provider import _path
            account=info['_owner_subject_id'];auth=getattr(request.app.state,'auth_manager',None)
            owner=auth.username_for_account_id(account) if auth else None
            if not owner or auth.account_id(owner)!=account or info['owner']!=owner:raise ValueError('owner unavailable')
            selected=target['resource']; child=None
            if asset is not None:
                matches=[item for item in target['dependencies'] if item['name']==asset]
                if len(matches)!=1:raise ValueError('asset unavailable')
                child=matches[0];selected=child['resource']
            store,barrier,context,provider,current=_source_context(request,owner=owner,account=account,auth=auth)
            async with barrier.admitted(owner):
                # Both the original source and the selected dependency retain
                # their own current owner/location/workspace policy checks.
                for resource in (target['resource'],selected):
                    path=_path(resource['origin_id'])
                    if os.path.realpath(path)!=path:raise ValueError('source unavailable')
                    _location_for(store,replace(context,workspace_id=resource['workspace_id']),path,
                        workspace_id=resource['workspace_id'],exact_location=resource['location_id'])
                if not current():raise ValueError('source changed')
                if target['mode']=='snapshot':
                    selected_file=info if child is None else service.resolve_snapshot_dependency(
                        file_id=child['snapshot_file_id'],owner=owner,owner_subject_id=account,resource=selected)
                    if not selected_file:raise ValueError('snapshot unavailable')
                    refreshed=service.resolve_grant(token)
                    if (not current() or not refreshed or refreshed.get('grant_id')!=info['grant_id']
                            or refreshed.get('_target')!=target or refreshed.get('_owner_subject_id')!=account):raise ValueError('grant changed')
                    return FileResponse(selected_file['path'],media_type=selected_file['mime_type'],filename=selected_file['filename'],
                        headers={'X-Content-Type-Options':'nosniff','Cache-Control':'private, no-store'})
                data,name,mime=await _read_host(provider,context,selected,current)
                refreshed=service.resolve_grant(token)
                if (not current() or not refreshed or refreshed.get('grant_id')!=info['grant_id']
                        or refreshed.get('_target')!=target or refreshed.get('_owner_subject_id')!=account):raise ValueError('grant changed')
                # Typed publications are downloads. Active content is not run on
                # the application origin merely because a link is available.
                filename=quote(name,safe='')
                return Response(data,media_type=mime,headers={'X-Content-Type-Options':'nosniff',
                    'Cache-Control':'private, no-store','ETag':'"sha256:'+hashlib.sha256(data).hexdigest()+'"',
                    'Content-Disposition':"attachment; filename*=UTF-8''"+filename})
        except Exception:
            raise HTTPException(404, "File not found")

    @router.post("/{file_id}/grants")
    async def create_grant(request: Request, file_id: str):
        candidate = service.typed_owned_target(file_id, owner=_owner(request))
        captured = _capture_chat_credential(request) if candidate and candidate['target'].get('version') == 2 else None
        raw = await _bounded_json(request)
        try:
            body = GrantRequest.model_validate(raw)
        except Exception as exc:
            raise HTTPException(422, 'Invalid request') from exc
        if captured is not None:
            try:
                user, account, auth, _, _ = captured
                if candidate['owner'] != user or candidate['owner_subject_id'] != account: raise ValueError('owner changed')
                chat = candidate['target']['chat']
                barrier, current = _chat_source_authority(request, owner=user, account=account, auth=auth,
                    chat=chat['id'], workspace=chat['workspace_id'], captured=captured)
                async with barrier.admitted(user):
                    if not current() or service.typed_owned_target(file_id, owner=user) != candidate:
                        raise ValueError('publication changed')
                    return service.create_typed_grant(candidate, audience=body.audience,
                        expires_in_hours=body.expires_in_hours, public_origin=_origin(), current=current)
            except Exception as exc:
                raise HTTPException(409, 'Publication unavailable or changed') from exc
        if candidate is None:
            try:
                return service.create_grant(file_id, owner=_owner(request), audience=body.audience,
                    expires_in_hours=body.expires_in_hours, public_origin=_origin())
            except PublishedFileError as exc:
                status = 404 if str(exc) == "File not found." else 400
                raise HTTPException(status, str(exc)) from exc
        try:
            from dataclasses import replace
            from src.openclank.files_host_provider import _path
            user, account, auth = _named_human(request)
            if candidate['owner'] != user or candidate['owner_subject_id'] != account:
                raise ValueError('owner changed')
            store, barrier, context, provider, current = _source_context(request, owner=user, account=account, auth=auth, named_human=True)
            async with barrier.admitted(user):
                target = candidate['target']
                resources = [target['resource']] + [item['resource'] for item in target['dependencies']]
                observations = []
                for resource in resources:
                    path = _path(resource['origin_id'])
                    if os.path.realpath(path) != path: raise ValueError('source unavailable')
                    scoped = replace(context, workspace_id=resource['workspace_id'])
                    _location_for(store, scoped, path, workspace_id=resource['workspace_id'], exact_location=resource['location_id'])
                    if target['mode'] == 'live':
                        before = await provider.stat_inspection(scoped, origin_id=resource['origin_id'])
                        if (not current() or before.kind != 'file' or before.origin_id != resource['origin_id']
                                or not isinstance(before.revision, dict) or before.revision.get('kind') != 'hostObjectIdentity'
                                or not before.revision.get('value')): raise ValueError('source unavailable')
                        observations.append((resource, dict(before.revision)))
                for resource, revision in observations:
                    scoped = replace(context, workspace_id=resource['workspace_id'])
                    after = await provider.stat_inspection(scoped, origin_id=resource['origin_id'])
                    if (not current() or after.kind != 'file' or after.origin_id != resource['origin_id']
                            or after.revision != revision): raise ValueError('source changed')
                if target['mode'] == 'snapshot':
                    if not service.get_owned(file_id, owner=user): raise ValueError('snapshot unavailable')
                    for item in target['dependencies']:
                        if not service.resolve_snapshot_dependency(file_id=item['snapshot_file_id'],owner=user,
                                owner_subject_id=account,resource=item['resource']): raise ValueError('snapshot unavailable')
                if not current() or service.typed_owned_target(file_id,owner=user) != candidate:
                    raise ValueError('publication changed')
                return service.create_typed_grant(candidate, audience=body.audience,
                    expires_in_hours=body.expires_in_hours, public_origin=_origin(), current=current)
        except Exception as exc:
            raise HTTPException(409, 'Publication unavailable or changed') from exc

    @router.post("/{file_id}/revoke")
    async def revoke_links(request: Request, file_id: str):
        try:
            return {"revoked": service.revoke_all(file_id, owner=_owner(request))}
        except PublishedFileError as exc:
            raise HTTPException(404, "File not found") from exc

    @router.delete("/{file_id}")
    async def delete_file(request: Request, file_id: str):
        if not service.delete(file_id, owner=_owner(request)):
            raise HTTPException(404, "File not found")
        return {"deleted": True}

    return router
