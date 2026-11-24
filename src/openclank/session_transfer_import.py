"""Private, same-row authority for explicitly selected inert history imports."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy.exc import OperationalError
from core.database import Session, ChatMessage, SessionLocal


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def prepare_import(*, account, owner, resource_id, snapshot_sha, workspace_id, workspace_path, thread_id, name, items):
    if (not all(type(x) is str and x and len(x) <= 512 for x in (account,owner,resource_id,workspace_id,workspace_path,thread_id))
            or type(snapshot_sha) is not str or len(snapshot_sha) != 64 or any(c not in '0123456789abcdef' for c in snapshot_sha)
            or type(name) is not str or not name.strip() or len(name.encode('utf-8')) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in name) or type(items) is not list or not 1 <= len(items) <= 100):
        raise ValueError('selected import unavailable')
    selection=[]; total=0; previous=-1; seen=set()
    for item in items:
        if (type(item) is not dict or set(item) != {'turnID','itemID','ordinal','kind','role','text'}
                or item['role'] not in ('user','assistant') or type(item['text']) is not str
                or type(item['ordinal']) is not int or item['ordinal'] <= previous
                or any(type(item[k]) is not str or not item[k] or len(item[k].encode('utf-8')) > 128 for k in ('turnID','itemID'))):
            raise ValueError('selected import unavailable')
        identity=(item['turnID'],item['itemID'])
        if identity in seen: raise ValueError('selected import unavailable')
        seen.add(identity);previous=item['ordinal']; encoded=item['text'].encode('utf-8');total+=len(encoded)
        selection.append({'turnID':item['turnID'],'itemID':item['itemID'],'ordinal':item['ordinal'],'role':item['role'],'sha256':hashlib.sha256(encoded).hexdigest()})
    if items[0]['role'] != 'user' or total > 1024*1024: raise ValueError('selected import unavailable')
    prefix='v1:'+_hash([account,resource_id,thread_id])+':'
    selection_sha=_hash(selection); key=prefix+selection_sha
    root=_hash(['selected-history-v1',key]);host_chat='import-'+root[:40];native_session='ses_'+root[:26]
    host_ids=['import-msg-'+_hash([root,i])[:40] for i in range(len(items))]
    # Deterministic native message IDs sort in the original selected order.
    native_ids=['msg_g'+format(i+1,'016x')+_hash([root,i])[:9] for i in range(len(items))]
    part_ids=['prt_g'+format(i+1,'016x')+_hash(['part',root,i])[:9] for i in range(len(items))]
    provenance={'version':1,'account':account,'owner':owner,'resourceID':resource_id,'snapshotSHA256':snapshot_sha,
        'threadID':thread_id,'selectionSHA256':selection_sha,'sourceKey':key,'hostChatID':host_chat,
        'hostMessageIDs':host_ids,'nativeSessionID':native_session,'nativeMessageIDs':native_ids,'nativePartIDs':part_ids,
        'workspaceID':workspace_id,'workspacePathSHA256':hashlib.sha256(workspace_path.encode('utf-8')).hexdigest(),'destinationName':name.strip(),'sourceTime':'unknown','ordering':'import_order',
        'coverage':'selected_partial','unsupportedCount':None,'selection':selection}
    return {'prefix':prefix,'key':key,'workspacePath':workspace_path,'provenance':provenance,'items':items}


def commit_host(intent, *, fresh, current):
    """Insert exactly once on fresh native claim, or compare the exact existing row."""
    p=intent['provenance']; owner=p['owner']; db=SessionLocal(); conn=None; old_timeout=None; begun=False
    try:
        conn=db.connection()
        if conn.dialect.name!='sqlite': raise RuntimeError('selected import unavailable')
        old_timeout=conn.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
        conn.exec_driver_sql('PRAGMA busy_timeout=0');conn.exec_driver_sql('BEGIN IMMEDIATE');begun=True
        if not current(): raise ValueError('selected import authority changed')
        row=db.query(Session).filter(Session.id==p['hostChatID']).first()
        if row is not None:
            if (row.owner != owner or row.workspace_id != p['workspaceID'] or row.archived
                    or (row.mimo_state or {}).get('selected_history_import') != p): return 'unknown'
            for index,item in enumerate(intent['items']):
                message=db.query(ChatMessage).filter(ChatMessage.id==p['hostMessageIDs'][index]).first()
                if (not message or message.session_id!=p['hostChatID'] or message.role!=item['role']
                        or hashlib.sha256(message.content.encode('utf-8')).hexdigest()!=p['selection'][index]['sha256']): return 'unknown'
            return 'already_present'
        if not fresh: return 'unknown'
        if db.query(ChatMessage.id).filter(ChatMessage.id.in_(p['hostMessageIDs'])).first(): return 'unknown'
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        row=Session(id=p['hostChatID'],owner=owner,name=p['destinationName'],endpoint_url='',model='',workspace_id=p['workspaceID'],
                    headers={},rag=False,message_count=len(intent['items']),mimo_state={'selected_history_import':p,'revision':1})
        db.add(row);db.flush()
        for index,item in enumerate(intent['items']):
            metadata=json.dumps({'source':'selected_codex_history','import_order':index,'source_time':'unknown',
                'source_turn_id':item['turnID'],'source_item_id':item['itemID']},ensure_ascii=False,separators=(',',':'))
            db.add(ChatMessage(id=p['hostMessageIDs'][index],session_id=p['hostChatID'],role=item['role'],content=item['text'],
                               meta_data=metadata,timestamp=now+timedelta(microseconds=index)))
        db.flush()
        if not current(): raise ValueError('selected import authority changed')
        conn.exec_driver_sql('COMMIT');begun=False
        return 'inserted'
    except OperationalError as exc: raise RuntimeError('selected import unavailable') from exc
    finally:
        if begun and conn is not None:
            try:conn.exec_driver_sql('ROLLBACK')
            except Exception:conn.invalidate()
        if conn is not None and old_timeout is not None:
            try:conn.exec_driver_sql('PRAGMA busy_timeout='+str(int(old_timeout)))
            except Exception:conn.invalidate()
        try:db.rollback()
        finally:db.close()


class SelectedImportUnavailable(RuntimeError):
    """The exact original import authority or receipt is unavailable."""


async def execute_selected_import(*, pool, barrier, auth, owner, account, intent, continuation, source_current=None, source_current_sync=None):
    """Hold one original account and worker lifetime for the complete private import.

    ``continuation`` runs inside that lifetime and receives an exact phase sender,
    a shielded host-commit function, and the original utility. It must return only
    after all requested map/receipt work is finished. It cannot reacquire a worker.
    """
    import asyncio
    import secrets
    from src.openclank.account_request_barrier import UtilityLifetime
    from src.openclank.operation_router import ManagedOperationRouter

    if (not owner or not account or auth.account_id(owner) != account
            or auth.username_for_account_id(account) != owner
            or intent['provenance']['owner'] != owner or intent['provenance']['account'] != account):
        raise SelectedImportUnavailable('original account unavailable')
    admission = await barrier.acquire(owner)
    utility = UtilityLifetime(barrier, admission, owner, account, 'selected-history',
                              intent['key'], _hash(intent['provenance']), auth)
    utility.supervisor = pool
    utility._selected_import_result_unknown = False
    try:
        await barrier.bind_utility(admission, utility)
    except BaseException:
        await barrier.release(admission)
        raise

    async def owned():
        lease = None
        successful = False
        try:
            lease = await pool.admit_provider_control(owner)
            worker = lease.worker
            bridge = getattr(worker, '_bridge', None)
            holder = getattr(bridge, '_managed_provider_generation', None)
            process = getattr(worker, '_proc', None)
            callbacks = getattr(worker, '_managed_callbacks', None)
            state = pool._states.get(owner)
            if (state is None or state.active is not worker or pool._workers.get(owner) is not worker
                    or lease._pool is not pool or lease._released or state.in_flight.get(worker, 0) < 1
                    or not utility.account_current() or not await barrier.is_live(owner, admission)
                    or lease.owner != owner or not bridge or not holder or process is None
                    or process.returncode is not None or getattr(worker, '_owner', None) != owner
                    or getattr(bridge, '_owner', None) != owner
                    or getattr(getattr(worker, '_managed_callbacks', None), 'holder_id', None) != holder
                    or type(getattr(worker, 'installed_generation', None)) is not int
                    or worker.installed_generation < 1 or lease.generation != worker.installed_generation
                    or type(lease._owner_epoch) is not int or lease._owner_epoch < 0
                    or getattr(worker, '_client', None) is None or getattr(worker, '_private_runtime', False)
                    or getattr(worker, '_utility_barrier', barrier) is not barrier):
                raise SelectedImportUnavailable('worker authority unavailable')
            utility.worker, utility.bridge, utility.holder = worker, bridge, holder
            utility.generation, utility.epoch, utility.process = lease.generation, lease._owner_epoch, process
            utility.lease, utility.client = lease, worker._client
            worker._utility_barrier = barrier
            runtime_hash = _hash(['selected-history-runtime-v1', owner, account, holder,
                                  lease.generation, lease._owner_epoch, secrets.token_hex(32)])
            issue_nonce = secrets.token_hex(32)
            fresh_reservation = False
            issue_attempted = False
            host_claim = False

            def current():
                try:
                    pool._assert_owner_lifecycle(owner, utility.epoch)
                    return (utility.account_current() and not admission.released
                            and utility.lease is lease and lease.worker is worker
                            and worker._proc is process and process.returncode is None
                            and worker._bridge is bridge and worker.installed_generation == utility.generation
                            and bridge._managed_provider_generation == holder
                            and worker._managed_callbacks is callbacks and getattr(callbacks, 'holder_id', None) == holder
                            and lease._pool is pool and not lease._released
                            and pool._states.get(owner) is state and state.active is worker
                            and pool._workers.get(owner) is worker and state.in_flight.get(worker, 0) >= 1)
                except Exception:
                    return False

            utility._selected_import_current = current

            async def phase(name, *, map_nonce=None, map_binding=None, map_receipt=None):
                nonlocal fresh_reservation, issue_attempted, host_claim
                if not current() or not await barrier.is_live(owner, admission):
                    raise SelectedImportUnavailable('original lifetime changed')
                if name in ('reserve', 'issue') and (source_current is None or not await source_current()):
                    raise SelectedImportUnavailable('source authority changed')
                if name == 'issue':
                    if not fresh_reservation or issue_attempted:
                        raise SelectedImportUnavailable('native issue is not available')
                    issue_attempted = True
                if name not in ('reserve', 'issue', 'read', 'ack_host', 'claim_map', 'ack_map', 'reconcile'):
                    raise SelectedImportUnavailable('unknown import phase')
                payload = {'version': 1, 'phase': name, 'intent': intent}
                if name == 'reconcile':
                    selector = intent['selector']
                    payload = {'version': 1, 'phase': 'reconcile', 'account': account, **selector}
                if name in ('reserve', 'issue'):
                    payload['claim'] = {'nonce': issue_nonce, 'runtimeSHA256': runtime_hash}
                elif name == 'claim_map':
                    if type(map_nonce) is not str or len(map_nonce) != 64:
                        raise SelectedImportUnavailable('map claim unavailable')
                    payload['claim'] = {'nonce': map_nonce, 'runtimeSHA256': runtime_hash, 'binding': map_binding}
                elif name == 'ack_map':
                    payload['claim'] = map_receipt
                utility.issued = True
                utility.response_seen = False
                async with worker.internal_http_client(timeout=20.0) as client:
                    response = await client.post('/global/selected-history', json=payload)
                if response.status_code != 200:
                    raise SelectedImportUnavailable('native receipt unavailable')
                result = response.json()
                if (type(result) is not dict or result.get('state') not in
                        ('reserved', 'host_issuing', 'host_committed', 'map_issuing', 'complete', 'unknown', 'conflict')
                        or not current() or not await barrier.is_live(owner, admission)):
                    raise SelectedImportUnavailable('native receipt unavailable')
                if name != 'reconcile' and result.get('state') not in ('unknown', 'conflict') and result.get('provenance') != intent['provenance']:
                    raise SelectedImportUnavailable('native receipt mismatch')
                utility.response_seen = True
                if name == 'reserve':
                    fresh_reservation = result.get('state') == 'reserved' and result.get('fresh') is True
                if name == 'issue':
                    host_claim = result.get('state') == 'host_issuing' and result.get('fresh') is True
                return result

            async def host_commit():
                nonlocal host_claim
                if (not current() or not await barrier.is_live(owner, admission)
                        or source_current is None or not await source_current()):
                    raise SelectedImportUnavailable('original lifetime changed')
                fresh = host_claim
                host_claim = False
                # The owned task is shielded from caller cancellation, and it
                # awaits this thread before releasing the admission or lease.
                utility.host_cleanup_unknown = True
                task = asyncio.create_task(asyncio.to_thread(commit_host, intent, fresh=fresh, current=lambda: current() and source_current_sync is not None and source_current_sync()))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            continue
                    raise
                finally:
                    if task.done() and not task.cancelled():
                        utility.host_cleanup_unknown = False
                        if task.exception() is not None:
                            utility._selected_import_result_unknown = True
                return result

            result = await continuation(phase, host_commit, utility)
            successful = True
            return result
        finally:
            if utility.lease is None:
                if lease is not None:
                    await lease.release(successful_terminal=False)
                await barrier.release(admission)
            elif not utility.host_cleanup_unknown and not utility._selected_import_result_unknown and (not utility.issued or utility.response_seen):
                await ManagedOperationRouter._settle_utility(utility, successful=successful)

    task = asyncio.create_task(owned())
    utility.task = task
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # This HTTP endpoint is not an ACP utility invocation: do not send an
        # unrelated ACP cancellation. The exact owned task continues draining.
        utility.cancel_requested = True
        raise


def _stage_import_binding(intent, binding, current):
    """CAS the original binding onto the already committed imported host row."""
    p = intent['provenance']; db = SessionLocal(); conn = None; timeout = None; begun = False
    try:
        conn = db.connection()
        if conn.dialect.name != 'sqlite': raise SelectedImportUnavailable('binding unavailable')
        timeout = conn.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
        conn.exec_driver_sql('PRAGMA busy_timeout=0'); conn.exec_driver_sql('BEGIN IMMEDIATE'); begun = True
        row = db.query(Session).filter(Session.id == p['hostChatID'], Session.owner == p['owner']).first()
        if (not current() or row is None or row.archived or row.workspace_id != p['workspaceID']
                or (row.mimo_state or {}).get('selected_history_import') != p
                or (row.mimo_state or {}).get('managed_binding') is not None):
            raise SelectedImportUnavailable('binding authority changed')
        state = dict(row.mimo_state or {})
        state['managed_binding'] = dict(binding)
        state['revision'] = int(state.get('revision') or 0) + 1
        row.mimo_state = state
        db.flush()
        if not current(): raise SelectedImportUnavailable('binding authority changed')
        conn.exec_driver_sql('COMMIT'); begun = False
    finally:
        if begun and conn is not None:
            try: conn.exec_driver_sql('ROLLBACK')
            except Exception: conn.invalidate()
        if conn is not None and timeout is not None:
            try: conn.exec_driver_sql('PRAGMA busy_timeout=' + str(int(timeout)))
            except Exception: conn.invalidate()
        try: db.rollback()
        finally: db.close()


async def publish_import_map(*, intent, utility, phase, host_commit, receipt, current, memory_workspace_id, copal_workspace):
    """Publish one fresh claimed map or only read the original claimed mapping."""
    import asyncio
    import secrets
    p = intent['provenance']; bridge = utility.bridge
    mapping = getattr(bridge, '_durable_session_map', None)
    if (mapping is None or mapping.owner != p['owner'] or not current()
            or not isinstance(memory_workspace_id, str) or not memory_workspace_id.strip()
            or not isinstance(copal_workspace, str) or not copal_workspace.strip()):
        raise SelectedImportUnavailable('session map unavailable')
    async with mapping.async_chat_admission_lock(p['hostChatID'], blocking=False) as guard:
        if guard is None or not current(): raise SelectedImportUnavailable('session map unavailable')
        if await host_commit() != 'already_present' or not current():
            return {'state': 'unknown'}
        entry = await mapping.async_lookup(p['hostChatID'])
        if entry is None:
            map_revision, mapping_revision = await mapping.async_map_revision(), 0
        else:
            map_revision, mapping_revision = await mapping.async_revisions(p['hostChatID'])
        from src.openclank.transcript_projection import get_managed_binding
        binding = await asyncio.to_thread(get_managed_binding, p['hostChatID'], owner=p['owner'])
        if not current(): raise SelectedImportUnavailable('session map authority changed')
        if receipt.get('state') in ('map_issuing', 'complete'):
            original = receipt.get('mapBinding')
            if (type(original) is not dict or binding != original or not entry
                    or entry.get('owner') != p['owner'] or entry.get('current') != p['nativeSessionID']
                    or entry.get('aliases') != [] or mapping_revision != original.get('mappingRevision')
                    or map_revision < original.get('mapRevision', 0)):
                return {'state': 'unknown'}
            return {'state': 'complete' if receipt['state'] == 'complete' else 'mapped',
                    'mapRevision': map_revision, 'mappingRevision': mapping_revision}
        if receipt.get('state') != 'host_committed' or entry is not None or binding or mapping_revision != 0:
            return {'state': 'unknown'}
        # mapRevision is a lower bound captured here. Unrelated chat mutations
        # may advance the owner-wide value before this chat is published.
        planned = {'owner':p['owner'], 'stableChatID':p['hostChatID'], 'engineSessionID':p['nativeSessionID'],
                   'engineAliases':[], 'memoryWorkspaceID':memory_workspace_id, 'authorityWorkspaceID':p['workspaceID'],
                   'copalWorkspace':copal_workspace, 'physicalCwd':intent['workspacePath'], 'workspaceRevision':0,
                   'mapRevision':map_revision + 1, 'mappingRevision':1, 'memoryEnabled':False, 'transition':None}
        claim = await phase('claim_map', map_nonce=secrets.token_hex(32), map_binding=planned)
        if claim.get('state') != 'map_issuing' or claim.get('fresh') is not True or claim.get('mapBinding') != planned:
            return {'state': 'unknown'}
        if not current(): raise SelectedImportUnavailable('session map authority changed')
        utility.host_cleanup_unknown = True
        try:
            await asyncio.to_thread(_stage_import_binding, intent, planned, current)
            if not current(): raise SelectedImportUnavailable('session map authority changed')
            # The guard serializes this chat. The owner-wide revision is not a
            # per-chat CAS and may advance independently.
            item = await mapping.async_bind(p['hostChatID'], p['nativeSessionID'], expected_current=None,
                                            expected_mapping_revision=0, guard=guard)
            if (not current() or item.get('current') != p['nativeSessionID'] or item.get('mappingRevision') != 1
                    or item.get('mapRevision', 0) < planned['mapRevision']):
                raise SelectedImportUnavailable('session map outcome unavailable')
            utility.host_cleanup_unknown = False
        except BaseException:
            # The owned calls have drained, but either durable effect may have
            # committed. Keep that distinct from a still-running host thread.
            utility.host_cleanup_unknown = False
            utility._selected_import_result_unknown = True
            raise
        ack = await phase('ack_map', map_receipt={'mapRevision':item['mapRevision'], 'mappingRevision':item['mappingRevision']})
        if ack.get('state') != 'complete' or ack.get('mapBinding') != planned:
            return {'state':'unknown'}
        return {'state':'complete','mapRevision':item['mapRevision'],'mappingRevision':1}


def read_host_receipt(provenance, current):
    """Read exact content hashes from the committed original host row only."""
    p = provenance
    if (type(p) is not dict or type(p.get('selection')) is not list or not 1 <= len(p['selection']) <= 100
            or type(p.get('hostMessageIDs')) is not list or len(p['hostMessageIDs']) != len(p['selection'])):
        return False
    db = SessionLocal()
    try:
        if not current(): return False
        row = db.query(Session).filter(Session.id == p.get('hostChatID'), Session.owner == p.get('owner')).first()
        if (row is None or row.archived or row.workspace_id != p.get('workspaceID')
                or (row.mimo_state or {}).get('selected_history_import') != p): return False
        for item, mid in zip(p['selection'], p['hostMessageIDs']):
            msg = db.query(ChatMessage).filter(ChatMessage.id == mid, ChatMessage.session_id == row.id).first()
            if (not msg or msg.role != item.get('role') or type(msg.content) is not str
                    or hashlib.sha256(msg.content.encode('utf-8')).hexdigest() != item.get('sha256')): return False
        return current()
    except (TypeError, UnicodeError):
        return False
    finally:
        db.close()


async def reconcile_import_map(*, provenance, receipt, utility):
    """Observe only the originally claimed native/host/map facts."""
    import asyncio
    p = provenance
    current = getattr(utility, '_selected_import_current', None)
    mapping = getattr(utility.bridge, '_durable_session_map', None)
    if (not callable(current) or not current() or mapping is None or mapping.owner != p.get('owner')
            or p.get('account') != utility.account or p.get('owner') != utility.owner
            or receipt.get('state') not in ('map_issuing', 'complete')):
        return {'state':'unknown'}
    try:
        async with mapping.async_chat_admission_lock(p['hostChatID'], blocking=False, existing_only=True) as guard:
            if guard is None or not current(): return {'state':'unknown'}
            if not await asyncio.to_thread(read_host_receipt, p, current): return {'state':'unknown'}
            entry = await mapping.async_lookup(p['hostChatID'])
            if not entry: return {'state':'unknown'}
            map_revision, mapping_revision = await mapping.async_revisions(p['hostChatID'])
            from src.openclank.transcript_projection import get_managed_binding
            binding = await asyncio.to_thread(get_managed_binding, p['hostChatID'], owner=p['owner'])
            original = receipt.get('mapBinding')
            if (not current() or type(original) is not dict or binding != original
                    or entry.get('owner') != p['owner'] or entry.get('current') != p['nativeSessionID']
                    or entry.get('aliases') != [] or mapping_revision != original.get('mappingRevision')
                    or map_revision < original.get('mapRevision', 0)):
                return {'state':'unknown'}
            actual = receipt.get('mapReceipt')
            if (receipt['state'] == 'complete' and (type(actual) is not dict or actual.get('mappingRevision') != mapping_revision
                    or type(actual.get('mapRevision')) is not int or not original['mapRevision'] <= actual['mapRevision'] <= map_revision)):
                return {'state':'unknown'}
            return {'state':'complete' if receipt['state'] == 'complete' else 'unknown'}
    except (OSError, ValueError, KeyError):
        return {'state':'unknown'}


async def settle_reconciled_import(provenance, utility):
    """Release only the exact earlier, drained lifetime proved complete."""
    from src.openclank.operation_router import ManagedOperationRouter
    current = getattr(utility, '_selected_import_current', None)
    if not callable(current) or not current(): return
    for old in await utility.barrier.utility_for_worker(utility.owner, utility.worker):
        if (old is utility or old.settled or old.settling or old.owner != utility.owner or old.account != utility.account
                or old.root != 'selected-history' or old.idempotency_key != provenance.get('sourceKey')
                or old.request_hash != _hash(provenance) or old.worker is not utility.worker
                or old.process is not utility.process or old.bridge is not utility.bridge or old.holder != utility.holder
                or old.generation != utility.generation or old.epoch != utility.epoch
                or old.lease is None or old.lease.worker is not old.worker or old.lease._released
                or old.host_cleanup_unknown or old.task is None or not old.task.done() or old.task.cancelled()
                or not old.account_current() or not await old.barrier.is_live(old.owner, old.admission) or not current()):
            continue
        old.response_seen = True
        old._selected_import_result_unknown = False
        await ManagedOperationRouter._settle_utility(old, successful=True)


async def hydrate_import_visibility(manager, provenance, current):
    """Hydrate only the exact positively completed host row into the existing cache."""
    import asyncio
    def load():
        if not read_host_receipt(provenance, current): return False
        try:
            item = manager.get_session(provenance['hostChatID'])
            return (current() and getattr(item, 'id', None) == provenance['hostChatID']
                    and getattr(item, 'owner', None) == provenance['owner']
                    and getattr(item, 'workspace_id', None) == provenance['workspaceID']
                    and manager.get_sessions_for_user(provenance['owner']).get(provenance['hostChatID']) is item
                    and read_host_receipt(provenance, current))
        except Exception:
            return False
    return await asyncio.to_thread(load)
