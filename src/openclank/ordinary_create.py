"""Original ordinary-create custody on the existing Files operation repository.

The caller supplies an independently authorized current Files client and scope;
operation keys identify requests and never grant file access.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Mapping

from src.openclank.files_service_client import FilesServiceError


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _identity(account: str, workspace: str, session: str, lane: str, key: str) -> str:
    return 'ordinary_create_' + _digest(['openclank.files.create-key.v1', account, workspace, session, lane, key])


def create_recovery_binding(account: str, workspace: str, session: str, lane: str, key: str,
                            path: str, parent_revision: str, content: bytes) -> dict[str, str]:
    operation = _identity(account, workspace, session, lane, key)
    action = 'create_' + _digest(['openclank.files.create-action.v1', operation])
    content_hash = hashlib.sha256(content).hexdigest()
    native_digest = _digest(['openclank.files.ordinary-create.v1',account,path,parent_revision,
                             content_hash,len(content),operation,action])
    request_digest = _digest(['openclank.files.create-request.v1',account,workspace,session,lane,
                               path,parent_revision,content_hash,len(content),operation,action])
    return {'kind':'ordinary_create','operation_id':operation,'action_id':action,
            'native_digest':native_digest,'request_digest':request_digest}


async def ordinary_create_once(*, client, repository, account: str, workspace: str, session: str,
                               lane: str, key: str, path: str, parent_revision: str, content: bytes,
                               generation: int, before_issue=None, status_only: bool = False, workflow_origin=None) -> Mapping[str, Any]:
    """Issue once or read the original; unknown never grants another issue.

    Native create/status must independently authorize the supplied current client.
    An unsettled or lost original process remains unknown, including after restart.
    """
    if (any(not isinstance(v, str) or not v or v != v.strip() or len(v.encode('utf-8')) > 512
            for v in (account, workspace, lane, key, path, parent_revision))
            or not isinstance(session, str) or len(session.encode('utf-8')) > 512
            or lane not in ('agent', 'human') or not os.path.isabs(path)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', parent_revision)
            or type(content) is not bytes or len(content) > 256 * 1024
            or type(generation) is not int or generation < 0 or type(status_only) is not bool):
        raise ValueError('ordinary create unavailable')
    content.decode('utf-8', 'strict')
    if b'\x00' in content: raise ValueError('ordinary create unavailable')
    recovery = create_recovery_binding(account,workspace,session,lane,key,path,parent_revision,content)
    operation, action, native_digest, request_digest = (recovery[k] for k in ('operation_id','action_id','native_digest','request_digest'))
    content_hash = hashlib.sha256(content).hexdigest()
    # Generation remains captured execution state, not retry identity.
    binding = {'operation_id':operation,'action_id':action,'request_digest':native_digest,
               'timestamp_millis':int(time.time()*1000),'parent_revision':parent_revision,
               'desired_sha256':content_hash,'desired_size':len(content)}
    pending = {'version':1,'kind':'ordinary_create','state':'pending','workspace':workspace,
               'session':session,'lane':lane,'path':path,'generation':generation,
               'binding':binding,'capture_identity':None,'settled':False}
    if workflow_origin is not None:
        from src.openclank.workflow_result_origin import origin_binding
        if lane != 'agent' or workflow_origin.get('account') != account or workflow_origin.get('sessionID') != session or workflow_origin.get('operationID') != key: raise ValueError('workflow result origin unavailable')
        pending['workflow_origin']=origin_binding(request_digest,workflow_origin)
    if status_only:
        old = repository.get_operation(owner_subject_id=account, operation_id=operation)
        if old is None: return {'state':'unknown','operation_id':operation}
        if old.get('digest') != request_digest: raise ValueError('ordinary create identity conflict')
    else:
        old = repository.reserve_operation(owner_subject_id=account, operation_id=operation,
            request_digest=request_digest, generation=generation, receipt=pending, phase='ordinary_create')
    if old is not None:
        receipt = old.get('receipt')
        if (not isinstance(receipt, Mapping) or receipt.get('kind') != 'ordinary_create'
                or receipt.get('workspace') != workspace or receipt.get('session') != session
                or receipt.get('lane') != lane or receipt.get('path') != path
                or not isinstance(receipt.get('binding'), Mapping)):
            raise ValueError('ordinary create original unavailable')
        if receipt.get('state') == 'not_applied' and receipt.get('reason') in ('not_issued','conflict'):
            # Current read authority is still required for the factual receipt.
            try:
                await client.request('stat',os.path.dirname(path),{})
            except Exception:
                return {'state':'unknown','operation_id':operation}
            return {'state':'not_applied','operation_id':operation,'reason':receipt['reason']}
        if receipt.get('settled') is not True or not isinstance(receipt.get('capture_identity'), Mapping):
            return {'state':'unknown','operation_id':operation}
        try:
            response = await client.ordinary_create_status_exact(path, binding=dict(receipt['binding']),
                                                                  capture_identity=dict(receipt['capture_identity']))
            data = response.get('data') if isinstance(response, Mapping) else None
            if (isinstance(data, Mapping) and set(data) == {'state','fingerprint'} and data.get('state') == 'committed'
                    and data.get('fingerprint') == f'sha256:{content_hash}:{len(content)}'):
                return {'state':'committed','operation_id':operation,'fingerprint':data['fingerprint']}
        except Exception:
            pass
        return {'state':'unknown','operation_id':operation}
    def not_applied(reason):
        pending['state'] = 'not_applied'
        pending['reason'] = reason
        repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=request_digest,
            generation=generation,receipt=pending,phase='ordinary_create')
        return {'state':'not_applied','operation_id':operation,'reason':reason}

    try:
        prepared = await client.prepare_ordinary_create(path, binding=binding)
    except FilesServiceError as error:
        if error.code == 'ordinary_create_unsent': return not_applied('not_issued')
        raise
    capture_identity = prepared.capture_identity
    if (capture_identity.get('account_id') != account
            or any(not isinstance(capture_identity.get(k),str) or not capture_identity[k] for k in ('account_id','actor_id','workspace_id'))):
        return not_applied('not_issued')
    pending['capture_identity'] = capture_identity
    repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=request_digest,
        generation=generation,receipt=pending,phase='ordinary_create')
    try:
        allowed = before_issue is None or await before_issue() is True
    except BaseException:
        if prepared.used is False: not_applied('not_issued')
        raise
    if not allowed:
        if prepared.used is False: return not_applied('not_issued')
        return {'state':'unknown','operation_id':operation}
    try:
        await client.ordinary_create(prepared, content=content)
    except FilesServiceError as error:
        # Native ordinary_create emits Conflict only for the parent revision or
        # pre-publication destination check; its History failures are separate.
        if prepared.settled and error.code in ('ordinary_create_unsent','conflict'):
            return not_applied('not_issued' if error.code == 'ordinary_create_unsent' else 'conflict')
        raise
    finally:
        if prepared.settled and pending['state'] == 'pending':
            pending['settled'] = True
            repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=request_digest,
                generation=generation,receipt=pending,phase='ordinary_create')
    response = await client.ordinary_create_status(prepared)
    data = response.get('data') if isinstance(response, Mapping) else None
    if (isinstance(data, Mapping) and set(data) == {'state','fingerprint'} and data.get('state') == 'committed'
            and data.get('fingerprint') == f'sha256:{content_hash}:{len(content)}'):
        if workflow_origin is not None:
            # An exchange being settled does not prove its mutation committed.
            # Persist only this positive original Native status, in the same
            # reservation, before exposing a durable result selector.
            pending['confirmed_commit']={'version':1,'fingerprint':data['fingerprint']}
            repository.record_operation(owner_subject_id=account,operation_id=operation,request_digest=request_digest,
                generation=generation,receipt=pending,phase='ordinary_create')
        return {'state':'committed','operation_id':operation,'fingerprint':data['fingerprint']}
    return {'state':'unknown','operation_id':operation}


def managed_create_binding(*, account, workspace, session, origin, invocation_digest, path, parent_revision, content):
    """Derive one Host-owned original using the existing ordinary-create identity."""
    from src.openclank.ordinary_managed_replace import validate_managed_origin
    validate_managed_origin(origin)
    if (not isinstance(account,str) or not account or not isinstance(workspace,str) or not workspace
            or not isinstance(session,str) or not session or not isinstance(path,str) or not os.path.isabs(path)
            or os.path.realpath(path)!=path or not isinstance(invocation_digest,str)
            or not re.fullmatch(r'[0-9a-f]{64}',invocation_digest) or type(content) is not bytes
            or len(content)>262144 or b'\x00' in content):
        raise ValueError('ordinary create unavailable')
    content.decode('utf-8','strict')
    if not isinstance(parent_revision,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',parent_revision):
        raise ValueError('ordinary create unavailable')
    key='managed_'+_digest(['openclank.model-file.create-key.v1',account,workspace,session,origin,invocation_digest])
    recovery=create_recovery_binding(account,workspace,session,'agent',key,path,parent_revision,content)
    binding={'operation_id':recovery['operation_id'],'action_id':recovery['action_id'],
        'request_digest':recovery['native_digest'],'timestamp_millis':int(time.time()*1000),
        'parent_revision':parent_revision,'desired_sha256':hashlib.sha256(content).hexdigest(),'desired_size':len(content)}
    receipt={'version':1,'kind':'ordinary_create','state':'pending','workspace':workspace,'session':session,
        'lane':'agent','path':path,'binding':binding,'capture_identity':None,'settled':False,
        'managed_origin':dict(origin),'invocation_digest':invocation_digest}
    return recovery,binding,receipt


class PreparedManagedCreate:
    """The original existing create reservation and one Files process."""
    def __init__(self, client, repository, account, generation, recovery, binding, receipt, prepared, content, current):
        self.client=client; self.repository=repository; self.account=account; self.generation=generation
        self.recovery=recovery; self.binding=binding; self.receipt=receipt; self.prepared=prepared
        self.content=content; self.current=current; self.used=False

    def _record(self):
        self.repository.record_operation(owner_subject_id=self.account,operation_id=self.recovery['operation_id'],
            request_digest=self.recovery['request_digest'],generation=self.generation,receipt=self.receipt,phase='ordinary_create')

    def abort_unissued(self):
        if self.used: return False
        self.used=True
        try:
            self.receipt.update(state='not_applied',reason='not_issued')
            self._record(); return True
        finally: self.client.close()

    async def apply(self):
        if self.used: return {'state':'unknown'}
        self.used=True
        try:
            if await self.current() is not True:
                self.receipt.update(state='not_applied',reason='not_issued'); self._record()
                return {'state':'unavailable'}
            await self.client.ordinary_create(self.prepared,content=self.content)
            self.receipt['settled']=True; self._record()
            response=await self.client.ordinary_create_status(self.prepared)
            data=response.get('data') if isinstance(response,Mapping) else None
            fingerprint=f"sha256:{self.binding['desired_sha256']}:{self.binding['desired_size']}"
            if (not isinstance(data,Mapping) or set(data)!={'state','fingerprint'} or data.get('state')!='committed'
                    or data.get('fingerprint')!=fingerprint or await self.current() is not True): return {'state':'unknown'}
            self.receipt['confirmed_commit']={'version':1,'fingerprint':fingerprint}; self._record()
            return {'state':'complete','revision':{'kind':'hostFingerprint','value':fingerprint}}
        except BaseException as error:
            known_unsent=(self.prepared.used is False or (isinstance(error,FilesServiceError)
                and error.code in ('ordinary_create_unsent','conflict') and self.prepared.settled is True))
            if known_unsent:
                self.receipt.update(state='not_applied',reason='conflict' if isinstance(error,FilesServiceError) and error.code=='conflict' else 'not_issued')
                try: self._record()
                except Exception: pass
            elif self.prepared.settled:
                self.receipt['settled']=True
                try: self._record()
                except Exception: pass
            raise
        finally: self.client.close()


async def prepare_managed_create(*, client, repository, account, workspace, session, generation,
                                 path, content, current, recovery, binding, receipt):
    transferred=False; reserved=False
    try:
        if await current() is not True: return None
        old=repository.reserve_operation(owner_subject_id=account,operation_id=recovery['operation_id'],
            request_digest=recovery['request_digest'],generation=generation,receipt=receipt,phase='ordinary_create')
        if old is not None: return None
        reserved=True
        prepared=await client.prepare_ordinary_create(path,binding=binding)
        capture=dict(prepared.capture_identity)
        if capture.get('account_id')!=account or await current() is not True: raise ValueError('ordinary create authority unavailable')
        receipt['capture_identity']=capture
        repository.record_operation(owner_subject_id=account,operation_id=recovery['operation_id'],
            request_digest=recovery['request_digest'],generation=generation,receipt=receipt,phase='ordinary_create')
        owner=PreparedManagedCreate(client,repository,account,generation,recovery,dict(binding),receipt,prepared,content,current)
        transferred=True; return owner
    except BaseException:
        if reserved and not transferred:
            receipt.update(state='not_applied',reason='not_issued')
            try: repository.record_operation(owner_subject_id=account,operation_id=recovery['operation_id'],
                request_digest=recovery['request_digest'],generation=generation,receipt=receipt,phase='ordinary_create')
            except Exception: pass
        raise
    finally:
        if not transferred: client.close()
