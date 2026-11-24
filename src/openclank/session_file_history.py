"""Private named-human authority for the existing session history owner.

This value carries no independent grant: each restore still checks the original
account, Files policy, ready History process and the caller's retained session
lease immediately before its existing custody owner can issue an effect.
"""
from __future__ import annotations


def canonical_session_current(manager, *, sid, owner, workspace_id):
    """Check the current canonical chat without requiring cache object identity."""
    from core.database import Session as DbSession, SessionLocal
    try:
        cached = manager.sessions.get(sid)
        if (cached is None or cached.id != sid or cached.owner != owner
                or getattr(cached, 'workspace_id', None) != workspace_id
                or getattr(cached, 'incognito', False) or getattr(cached, 'archived', False)):
            return False
        with SessionLocal() as db:
            row = db.query(DbSession.id, DbSession.owner, DbSession.workspace_id, DbSession.archived).filter(
                DbSession.id == sid, DbSession.owner == owner).first()
            if not (row and row.id == sid and row.owner == owner
                    and row.workspace_id == workspace_id and not row.archived):
                return False
        # Keep the same deletion fence as SessionManager.get_session without
        # hydrating or replacing the cached transcript object.
        from src.openclank.conversation_archive import get_conversation_archive
        return not get_conversation_archive().chat_erasure_started(owner=owner, chat_id=sid)
    except Exception:
        return False


def human_file_authority(request, *, sid, owner, session_manager):
    """Capture a genuine current UI principal; do not retain the Request."""
    from src.auth_helpers import effective_user, is_internal_tool_identity, require_user
    from src.openclank.files_facade import ProviderContext
    from src.openclank.transcript_projection import get_managed_binding

    user = require_user(request)
    if (user != owner or (effective_user(request) or user) != user or is_internal_tool_identity(user)
            or getattr(request.state, 'api_token', False)
            or getattr(request.state, 'internal_tool_authenticated', False)):
        raise ValueError('named human authority unavailable')
    state = request.app.state
    auth = getattr(state, 'auth_manager', None)
    barrier = getattr(state, 'account_request_barrier', None)
    store = getattr(state, 'files_policy_repository', None)
    supervisor = getattr(state, 'history_supervisor', None)
    factory = getattr(state, 'files_facade_factory', None)
    account = auth.account_id(owner) if auth else None
    process = getattr(supervisor, 'process', None)
    session = session_manager.get_session(sid, allow_pending_history=True)
    if (not account or auth.username_for_account_id(account) != owner or not barrier or not store or not supervisor
            or process is None or process.poll() is not None or not callable(factory)
            or session.owner != owner or getattr(session, 'incognito', False) or getattr(session, 'archived', False)):
        raise ValueError('named human authority unavailable')
    binding = get_managed_binding(sid, owner=owner)
    workspace_id = binding.get('authorityWorkspaceID')
    if type(workspace_id) is not str: raise ValueError('managed workspace unavailable')
    generation = store.generation()
    admin = auth.is_admin(owner)
    context = ProviderContext(owner_subject_id=account, owner_username=owner, policy_generation=generation,
                              is_admin=admin, workspace_id=workspace_id or None)
    provider = factory(request, context)._providers.get('host')
    if provider is None or getattr(provider, 'operation_store', None) is not store:
        raise ValueError('Files authority unavailable')

    def current():
        try:
            return (getattr(state, 'auth_manager', None) is auth
                and getattr(state, 'account_request_barrier', None) is barrier
                and getattr(state, 'files_policy_repository', None) is store
                and getattr(state, 'history_supervisor', None) is supervisor
                and getattr(state, 'files_facade_factory', None) is factory
                and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                and auth.is_admin(owner) == admin and not barrier._fenced(owner)
                and store.generation() == generation and supervisor.process is process and process.poll() is None
                and get_managed_binding(sid, owner=owner) == binding
                and canonical_session_current(session_manager, sid=sid, owner=owner, workspace_id=workspace_id))
        except Exception:
            return False

    if not current(): raise ValueError('named human authority changed')
    return {'owner':owner, 'account':account, 'auth':auth, 'barrier':barrier, 'supervisor':supervisor,
            'store':store, 'context':context, 'provider':provider, 'workspace_id':workspace_id, 'original_current':current}


async def read_captured_range(worker, *, chat, native_id, directory, owner, account, workspace, from_id, check_current):
    """Read a bounded exact native head and ordered local observations twice."""
    from src.openclank.history_change_sets import parse_locator, strict_json
    async def read(payload):
        check_current()
        response = await worker.session_observed_step_request(chat, native_id, directory, payload, owner=owner)
        check_current()
        if response.status_code != 200 or len(response.content) > 1024 * 1024: raise ValueError('capture range unavailable')
        return strict_json(response.content.decode('utf-8', 'strict'))
    head = await read({'version':1,'stableChatID':chat,'head':True})
    if (type(head) is not dict or set(head) != {'version','sessionID','hostChatID','head'} or head['version'] != 1
            or head['sessionID'] != native_id or head['hostChatID'] != chat or type(head['head']) is not dict
            or set(head['head']) != {'messageID','timeCreated'} or type(head['head']['messageID']) is not str
            or not head['head']['messageID'] or type(head['head']['timeCreated']) is not int):
        raise ValueError('capture head unavailable')
    async def pages():
        cursor = None; seen = set(); result = []; total = 0; unknown = False
        for _ in range(8):
            body = {'version':1,'stableChatID':chat,'fromMessageID':from_id,'toMessageID':head['head']['messageID']}
            if cursor is not None: body['cursor'] = cursor
            page = await read(body)
            expected = {'version','assurance','coverage','sessionID','hostChatID','fromMessageID','toMessageID','head','unknown','steps'}
            if (type(page) is not dict or set(page) not in (expected, expected|{'nextCursor'}) or page['version'] != 1
                    or page['assurance'] != 'unverified_local_observation' or page['coverage'] not in {'partial','unavailable'}
                    or page['sessionID'] != native_id or page['hostChatID'] != chat or page['fromMessageID'] != from_id
                    or page['toMessageID'] != head['head']['messageID'] or page['head'] != head['head']
                    or type(page['unknown']) is not bool or type(page['steps']) is not list or len(page['steps']) > 20):
                raise ValueError('capture range changed')
            unknown = unknown or page['unknown']; result.append(page)
            for step in page['steps']:
                keys = {'version','assurance','sessionID','messageID','stepID','membershipRevision','accountID','hostChatID','workspaceID','actorID','executionRevision','rootOperationID','innerTurnID','coverage','members'}
                if (type(step) is not dict or set(step) != keys or step['version'] != 1
                        or step['assurance'] != 'unverified_local_observation' or step['sessionID'] != native_id
                        or step['accountID'] != account or step['hostChatID'] != chat or step['workspaceID'] != workspace
                        or type(step['members']) is not list or len(step['members']) > 64): raise ValueError('capture scope changed')
                for index, member in enumerate(step['members']):
                    if type(member) is not dict or member.get('sequence') != index or type(member.get('sequence')) is not int: raise ValueError('capture member unavailable')
                    if member.get('state') == 'pending' and set(member) == {'sequence','state'}:
                        unknown = True; continue
                    if member.get('state') != 'complete' or set(member) != {'sequence','state','actionRef'}: raise ValueError('capture member unavailable')
                    locator = member['actionRef']; parsed = parse_locator(locator); origin = parsed['origin']
                    if (origin['host_chat_id'] != chat or origin['engine_session_id'] != native_id
                            or origin['assistant_message_id'] != step['messageID'] or origin['actor_id'] != step['actorID']
                            or origin['execution_revision'] != step['executionRevision'] or origin['root_operation_id'] != step['rootOperationID']
                            or origin['inner_turn_id'] != step['innerTurnID'] or locator in seen): raise ValueError('capture origin changed')
                    seen.add(locator); total += 1
                    if total > 64: raise ValueError('capture range too large')
            cursor = page.get('nextCursor')
            if cursor is None: return result, unknown
            if type(cursor) is not str or not cursor or len(cursor) > 4096: raise ValueError('capture cursor unavailable')
        raise ValueError('capture range too large')
    first, unknown = await pages()
    if await read({'version':1,'stableChatID':chat,'head':True}) != head: raise ValueError('capture head changed')
    second, again_unknown = await pages()
    if second != first or unknown != again_unknown or await read({'version':1,'stableChatID':chat,'head':True}) != head:
        raise ValueError('capture range changed')
    return {'head':head['head'],'pages':first,'unknown':unknown}


async def plan_undo(worker, *, sid, owner, native_id, directory, from_id, authority, check_current):
    """Prepare independently authorized selected versions; no file effect."""
    from src.openclank.history_change_qualifier import qualify_captured_change
    from src.openclank import regular_file_restore as regular
    def current():
        try: check_current(); return authority['original_current']() is True
        except Exception: return False
    if not current(): raise ValueError('file authority unavailable')
    client = authority['supervisor'].client_for(owner, authority['account'])
    workspace = authority.get('workspace_id')
    if type(workspace) is not str: raise ValueError('capture workspace unavailable')
    observed = await read_captured_range(worker,chat=sid,native_id=native_id,directory=directory,owner=owner,
        account=authority['account'],workspace=workspace,from_id=from_id,check_current=check_current)
    members = []
    for page in observed['pages']:
        for step in page['steps']:
            for part in step['members']:
                if part['state'] != 'complete': continue
                qualified = await qualify_captured_change(part['actionRef'],client=client,account=authority['account'],
                    context=authority['context'],provider=authority['provider'],current=current)
                selected = ([qualified] if qualified['version'] == 1 else qualified['members'])
                for item in selected:
                    if item.get('state','available') != 'available' or not isinstance(item.get('item'),dict):
                        members.append({'state':'unavailable', **({'resource_id':item['current_resource_id']} if isinstance(item.get('current_resource_id'),str) else {})}); continue
                    change = item['item']; before = change.get('before'); after = change.get('after')
                    if (change.get('ambiguous_phase') is not False or not isinstance(before,dict) or not isinstance(after,dict)
                            or not isinstance(before.get('id'),str) or not isinstance(after.get('id'),str)):
                        members.append({'state':'unavailable', **({'resource_id':item['current_resource_id']} if isinstance(item.get('current_resource_id'),str) else {})}); continue
                    members.append({'state':'candidate','resource_id':item['current_resource_id'],'private_resource_id':item['resource_id'],
                        'source_before':{'version_ref':before['id']},'source_after':{'version_ref':after['id']}})
                    if len(members) > 64: raise ValueError('capture range too large')
    if len(members) > 64: raise ValueError('capture range too large')
    groups = {}; order = []
    for item in members:
        resource = item.get('resource_id')
        if resource is None:
            order.append(item); continue
        if resource not in groups:
            groups[resource] = []; order.append(resource)
        groups[resource].append(item)
    common = {k:authority[k] for k in ('owner','account','auth','barrier','supervisor','store','context','provider')}
    common['original_current'] = current
    planned = []
    for entry in order:
        if isinstance(entry,dict): planned.append(entry); continue
        resource = entry; chain = groups[resource]
        item = {'state':'unavailable','resource_id':resource}
        if any(part.get('state') != 'candidate' for part in chain) or len({part.get('private_resource_id') for part in chain}) != 1:
            planned.append(item); continue
        try:
            snapshots = []
            for part in chain:
                before = await regular.preview(resource_id=resource,source=part['source_before'],**common)
                after = await regular.preview(resource_id=resource,source=part['source_after'],**common)
                snapshots.append((before,after))
            current_state = snapshots[-1][1]['review']['current']
            if (snapshots[-1][1]['review']['desired'] != current_state
                    or any(before['review']['current'] != current_state or after['review']['current'] != current_state for before,after in snapshots)
                    or any(snapshots[i][1]['review']['desired'] != snapshots[i+1][0]['review']['desired'] for i in range(len(snapshots)-1))):
                item['state'] = 'conflict'; planned.append(item); continue
            first = snapshots[0][0]
            item.update(state='planned',source_before=chain[0]['source_before'],source_after=chain[-1]['source_after'],
                private_resource_id=chain[0]['private_resource_id'],request_id=first['request_id'],expected_digest=first['expected_digest'],
                expected_current=current_state,desired=first['review']['desired'])
        except Exception:
            pass
        planned.append(item)
    members = planned
    # No hint is proof of whole-turn coverage or of unobserved external effects.
    import hashlib, json
    encoded = json.dumps(observed,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return {'version':1,'coverage':'partial','state':'planned' if any(x['state']=='planned' for x in members) else 'unavailable',
            'from_id':from_id,'head':observed['head'],'range_sha256':hashlib.sha256(encoded).hexdigest(),'members':members}


def _common(authority, check_current):
    def current():
        try: check_current(); return authority['original_current']() is True
        except Exception: return False
    common = {k:authority[k] for k in ('owner','account','auth','barrier','supervisor','store','context','provider')}
    common['original_current'] = current
    return common


async def plan_redo(previous, *, authority, check_current):
    """Prepare inverse of each positively completed original undo only."""
    from src.openclank import regular_file_restore as regular
    common = _common(authority, check_current); members = []
    for old in previous.get('members', []):
        resource = old.get('resource_id')
        if old.get('state') != 'complete' or old.get('verified') is not True or not resource or not old.get('request_id'):
            members.append({'state':'unavailable', **({'resource_id':resource} if resource else {})}); continue
        source = {'inverse_request_id':old['request_id']}
        try:
            preview = await regular.preview(resource_id=resource,source=source,**common)
            if preview['review']['current'] != old.get('desired'):
                members.append({'state':'conflict','resource_id':resource}); continue
            members.append({'state':'planned','resource_id':resource,'source_before':source,
                'request_id':preview['request_id'],'expected_digest':preview['expected_digest'],
                'expected_current':preview['review']['current'],'desired':preview['review']['desired']})
        except Exception:
            members.append({'state':'unavailable','resource_id':resource})
    return {'version':1,'coverage':'partial','state':'planned' if any(x['state']=='planned' for x in members) else 'unavailable','members':members}


async def apply_file_plan(plan, *, authority, check_current, persist, allow_issue):
    """Persist each original ID before effects; reconcile issued IDs by status only."""
    from src.openclank import regular_file_restore as regular
    common = _common(authority, check_current)
    for member in plan.get('members', []):
        state = member.get('state')
        if state not in {'planned','issuing','unknown','pending'}: continue
        source = member.get('source_before'); resource = member.get('resource_id')
        if not resource or not isinstance(source,dict) or not member.get('request_id') or not member.get('expected_digest'):
            member['state'] = 'unavailable'; persist(plan); continue
        if state == 'planned' and not allow_issue:
            member['state'] = 'unavailable'; persist(plan); continue
        try:
            if state == 'planned':
                member['state'] = 'issuing'; persist(plan)
                outcome = await regular.confirm(resource_id=resource,source=source,request_id=member['request_id'],
                    expected_digest=member['expected_digest'],**common)
            else:
                outcome = await regular.status(resource_id=resource,source=source,request_id=member['request_id'],
                    expected_digest=member['expected_digest'],owner=common['owner'],account=common['account'],auth=common['auth'],
                    barrier=common['barrier'],supervisor=common['supervisor'],store=common['store'],original_current=common['original_current'])
            observed = outcome.get('state')
            member['state'] = observed if observed in {'complete','conflict','refused','partial','unknown','pending'} else 'unknown'
            member['verified'] = outcome.get('verified') is True if member['state'] == 'complete' else False
            if member['state'] == 'complete' and not member['verified']: member['state'] = 'unknown'
        except Exception:
            member['state'] = 'unknown'
        persist(plan)
    states = [item.get('state') for item in plan.get('members',[])]
    plan['state'] = ('unknown' if any(x in {'issuing','unknown','pending'} for x in states) else
                     'partial' if any(x == 'complete' for x in states) else 'unavailable')
    persist(plan)
    return plan
