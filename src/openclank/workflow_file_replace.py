"""Strict once-only workflow replacement on the existing Files operation owner.

Private adapter: the caller supplies an independently admitted App-lane Files
client, an already resolved resource and a live authority check. An operation
identifier selects a receipt; it is never file or workflow authorization.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


async def replace_status(*, client, repository, account: str, workspace: str, key: str, path: str, current):
    """Read only the original scoped operation, without caller-supplied content."""
    values = (account, workspace, key, path)
    if (any(not isinstance(v, str) or not v or v != v.strip() or len(v.encode('utf-8')) > 4096 for v in values)
            or not os.path.isabs(path) or not callable(current)):
        raise ValueError('workflow replacement unavailable')
    operation = 'workflow-' + _hash(['openclank.workflow.replace-key.v1', account, workspace, key])
    if await current() is not True: return {'state':'unavailable', 'operation_id':operation}
    row = repository.get_operation(owner_subject_id=account, operation_id=operation)
    if row is None: return {'state':'unknown', 'operation_id':operation}
    receipt = row.get('receipt')
    if (not isinstance(receipt, Mapping) or receipt.get('version') != 1
            or receipt.get('kind') != 'workflow.artifact.v1' or receipt.get('workspace') != workspace
            or receipt.get('target_digest') != _hash(['openclank.workflow.target.v1', account, workspace, path])):
        return {'state':'unavailable', 'operation_id':operation}
    if receipt.get('state') in {'conflict', 'unavailable'}:
        return {'state':receipt['state'], 'operation_id':operation} if await current() is True else {'state':'unavailable', 'operation_id':operation}
    binding = receipt.get('native_binding'); capture = receipt.get('capture_identity'); claim = receipt.get('claim')
    if not all(isinstance(item, Mapping) for item in (binding, capture, claim)):
        return {'state':'unknown', 'operation_id':operation}
    expected_action = 'workflow-' + _hash(['openclank.workflow.replace-action.v1', operation])
    if (binding.get('operation_id') != operation or binding.get('action_id') != expected_action
            or capture.get('account_id') != account
            or binding.get('request_digest') != _hash(['openclank.files.ordinary-replace.v1', account, path,
                binding.get('expected_fingerprint'), binding.get('desired_sha256'), operation, expected_action])):
        return {'state':'unavailable', 'operation_id':operation}
    if receipt.get('state') == 'complete':
        revision = receipt.get('revision')
        expected = f"sha256:{binding.get('desired_sha256')}:{binding.get('desired_size')}"
        if revision != {'kind':'hostFingerprint','value':expected} or await current() is not True:
            return {'state':'unavailable', 'operation_id':operation}
        return {'state':'complete','operation_id':operation,'revision':dict(revision)}
    if receipt.get('state') != 'pending' or receipt.get('retired') is not True:
        return {'state':'unknown', 'operation_id':operation}
    try:
        response = await client.ordinary_replace_status_exact(path, binding=dict(binding), capture_identity=dict(capture))
        evidence = response.get('data') if isinstance(response, Mapping) else None
        result = repository.settle_collaboration_text(owner_subject_id=account, operation_id=operation,
            request_digest=row['digest'], claim=dict(claim), native_status=evidence)
        if await current() is not True: return {'state':'unavailable', 'operation_id':operation}
        answer = {'state':result.get('state','unknown'), 'operation_id':operation}
        if answer['state'] == 'complete': answer['revision'] = result.get('revision')
        return answer
    except Exception:
        return {'state':'unknown', 'operation_id':operation}


async def replace_once(*, client, repository, account: str, workspace: str, generation: int,
                       key: str, path: str, expected_fingerprint: str, content: bytes,
                       current, status_only: bool = False, workflow_origin=None):
    """Issue once or reconcile its original receipt; never replay uncertain I/O."""
    if (any(not isinstance(v, str) or not v or v != v.strip() or len(v.encode('utf-8')) > 512
            for v in (account, workspace, key, expected_fingerprint))
            or not isinstance(path, str) or not os.path.isabs(path) or len(path.encode('utf-8')) > 4096 or not re.fullmatch(r'sha256:[0-9a-f]{64}:(0|[1-9][0-9]*)', expected_fingerprint)
            or type(content) is not bytes or len(content) > 262144 or type(generation) is not int or generation < 0
            or not callable(current) or type(status_only) is not bool):
        raise ValueError('workflow replacement unavailable')
    content.decode('utf-8', 'strict')
    if b'\x00' in content: raise ValueError('workflow replacement unavailable')
    operation = 'workflow-' + _hash(['openclank.workflow.replace-key.v1', account, workspace, key])
    action = 'workflow-' + _hash(['openclank.workflow.replace-action.v1', operation])
    desired = hashlib.sha256(content).hexdigest()
    digest = _hash(['openclank.workflow.replace-request.v1', account, workspace, path,
                    expected_fingerprint, desired, len(content), operation, action])
    native_digest = _hash(['openclank.files.ordinary-replace.v1', account, path, expected_fingerprint,
                           desired, operation, action])
    binding = {'operation_id':operation, 'action_id':action, 'request_digest':native_digest,
               'timestamp_millis':int(time.time()*1000), 'expected_fingerprint':expected_fingerprint,
               'desired_sha256':desired, 'desired_size':len(content)}
    initial = {'version':1, 'kind':'workflow.artifact.v1', 'state':'pending', 'workspace':workspace,
               'target_digest':_hash(['openclank.workflow.target.v1', account, workspace, path])}
    if workflow_origin is not None:
        from src.openclank.workflow_result_origin import origin_binding
        if workflow_origin.get('account') != account or workflow_origin.get('operationID') != key: raise ValueError('workflow result origin unavailable')
        initial['workflow_origin']=origin_binding(digest,workflow_origin)
    if await current() is not True: return {'state':'unavailable', 'operation_id':operation}
    row = (repository.get_operation(owner_subject_id=account, operation_id=operation) if status_only else
           repository.reserve_operation(owner_subject_id=account, operation_id=operation,
               request_digest=digest, generation=generation, receipt=initial, phase='workflow.artifact.v1'))
    if status_only and row is None:
        return {'state':'unknown', 'operation_id':operation}
    if row is not None:
        receipt = row.get('receipt')
        if (row.get('digest') != digest or not isinstance(receipt, Mapping)
                or receipt.get('kind') != 'workflow.artifact.v1' or receipt.get('workspace') != workspace):
            raise ValueError('workflow replacement identity conflict')
        if await current() is not True: return {'state':'unavailable', 'operation_id':operation}
        if receipt.get('state') in {'complete','conflict','unavailable'}:
            answer = {'state':receipt['state'], 'operation_id':operation}
            if receipt['state'] == 'complete': answer['revision'] = receipt.get('revision')
            return answer
        if receipt.get('retired') is not True or not all(isinstance(receipt.get(k), Mapping) for k in ('native_binding','capture_identity','claim')):
            return {'state':'unknown', 'operation_id':operation}
        try:
            response = await client.ordinary_replace_status_exact(path, binding=dict(receipt['native_binding']),
                                                                  capture_identity=dict(receipt['capture_identity']))
            evidence = response.get('data') if isinstance(response, Mapping) else None
            result = repository.settle_collaboration_text(owner_subject_id=account, operation_id=operation,
                request_digest=digest, claim=dict(receipt['claim']), native_status=evidence)
            if await current() is not True: return {'state':'unavailable', 'operation_id':operation}
            answer = {'state':result.get('state','unknown'), 'operation_id':operation}
            if answer['state'] == 'complete': answer['revision'] = result.get('revision')
            return answer
        except Exception:
            return {'state':'unknown', 'operation_id':operation}
    prepared = None
    claim = None
    try:
        prepared = await client.prepare_ordinary_replace(path, binding=binding)
        capture = dict(prepared.capture_identity)
        if capture.get('account_id') != account or await current() is not True:
            repository.refuse_collaboration_unclaimed(owner_subject_id=account,operation_id=operation,request_digest=digest,state='unavailable')
            return {'state':'unavailable','operation_id':operation}
        claim = repository.claim_collaboration_text(owner_subject_id=account,operation_id=operation,request_digest=digest,
            generation=generation,workspace_id=workspace,path=path,binding=binding,capture_identity=capture)
        if await current() is not True:
            result = repository.settle_collaboration_text(owner_subject_id=account,operation_id=operation,request_digest=digest,claim=claim,known_unsent=True)
            return {'state':result['state'],'operation_id':operation}
        repository.mark_collaboration_issued(owner_subject_id=account,operation_id=operation,request_digest=digest,claim=claim)
        await client.ordinary_replace(prepared,content=content)
        repository.retire_collaboration_io(owner_subject_id=account,operation_id=operation,request_digest=digest,claim=claim)
        response = await client.ordinary_replace_status(prepared)
        result = repository.settle_collaboration_text(owner_subject_id=account,operation_id=operation,request_digest=digest,
            claim=claim,native_status=response.get('data') if isinstance(response,Mapping) else None)
        if await current() is not True: return {'state':'unavailable','operation_id':operation}
        answer = {'state':result.get('state','unknown'),'operation_id':operation}
        if answer['state'] == 'complete': answer['revision'] = result.get('revision')
        return answer
    except BaseException:
        # Preserve the original path claim until the original exchange or process
        # is positively retired. A caller may query, never issue the write again.
        if claim is None:
            try: repository.refuse_collaboration_unclaimed(owner_subject_id=account,operation_id=operation,request_digest=digest,state='unavailable')
            except Exception: pass
        elif prepared is not None:
            if prepared.used is False:
                try: repository.settle_collaboration_text(owner_subject_id=account,operation_id=operation,request_digest=digest,claim=claim,known_unsent=True)
                except Exception: pass
            elif prepared.settled:
                try: repository.retire_collaboration_io(owner_subject_id=account,operation_id=operation,request_digest=digest,claim=claim)
                except Exception: pass
        raise


async def agent_replace_once(*, client_factory, repository, account, workspace, generation, key, path, expected_fingerprint, content, current, workflow_origin=None):
    """Run the existing strict owner with a fresh operation-bound Agent client."""
    operation='workflow-'+_hash(['openclank.workflow.replace-key.v1',account,workspace,key])
    action='workflow-'+_hash(['openclank.workflow.replace-action.v1',operation])
    desired=hashlib.sha256(content).hexdigest()
    digest=_hash(['openclank.files.ordinary-replace.v1',account,path,expected_fingerprint,desired,operation,action])
    client=client_factory(path,{'operation_id':operation,'request_digest':digest})
    try:
        return await replace_once(client=client,repository=repository,account=account,workspace=workspace,generation=generation,
            key=key,path=path,expected_fingerprint=expected_fingerprint,content=content,current=current,workflow_origin=workflow_origin)
    finally:
        client.close()


async def agent_replace_status(*, client_factory, repository, account, workspace, key, path, current):
    """Read only an original receipt with a current scoped Agent client."""
    operation='workflow-'+_hash(['openclank.workflow.replace-key.v1',account,workspace,key])
    row=repository.get_operation(owner_subject_id=account,operation_id=operation)
    if not row: return {'state':'unknown','operation_id':operation}
    receipt=row.get('receipt')
    if (not isinstance(receipt,Mapping) or receipt.get('kind')!='workflow.artifact.v1' or receipt.get('workspace')!=workspace
            or receipt.get('target_digest')!=_hash(['openclank.workflow.target.v1',account,workspace,path]) or await current() is not True):
        return {'state':'unavailable','operation_id':operation}
    binding=receipt.get('native_binding')
    if not isinstance(binding,Mapping):
        return {'state':receipt.get('state','unknown') if isinstance(receipt,Mapping) and receipt.get('state')=='unavailable' else 'unknown','operation_id':operation}
    client=client_factory(path,dict(binding))
    try:
        return await replace_status(client=client,repository=repository,account=account,workspace=workspace,key=key,path=path,current=current)
    finally:
        client.close()
