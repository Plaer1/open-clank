"""Read an exact completed code-summary result through existing owners."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from src.openclank.code_summary import _generation_material, _encoded
from src.openclank.operation_journal import OperationJournalStore
from src.openclank.operation_router import get_operation_router, _thaw_json

_SEAL = object()


@dataclass(frozen=True)
class VerifiedSummaryResult:
    _seal: object
    owner: str
    account_id: str
    operation_id: str
    root_operation_id: str
    request_id: str
    artifact_id: str
    artifact_sha256: str
    output_sha256: str
    preview_hash: str
    receipt: dict
    sources: list
    omissions: list
    text: bytes


def is_verified_summary(value):
    return type(value) is VerifiedSummaryResult and value._seal is _SEAL


def _lookup_completed(*, router, prepared, operation, owner, strict=False):
    """Shared exact journal lookup; strict checks belong only to the document consumer."""
    payload=_thaw_json(prepared.payload)
    exact={key:payload[key] for key in ('operation','routes','input','artifactInputs','options')}
    journal=OperationJournalStore(router._session_factory)
    identity=journal.discover_exact(owner=owner,root_operation_id=operation.root_operation_id,idempotency_key=operation.idempotency_key,request=exact)
    if identity is None: raise ValueError('Generated result unavailable')
    resolved=router.reconcile_prepared(prepared,authenticated_owner=owner,operation_id=identity[0],revision=identity[1])
    result=resolved.get('result')
    if resolved.get('state')!='terminal' or result is None or result.state!='complete' or not result.committed: raise ValueError('Generated result unavailable')
    text=result.output.get('text')
    if not isinstance(text,str) or not text or len(text.encode('utf-8','strict'))>262144: raise ValueError('Generated text unavailable')
    row=journal.lookup_exact(owner=owner,root_operation_id=operation.root_operation_id,idempotency_key=operation.idempotency_key,operation_id=identity[0],revision=identity[1],request=exact)
    artifact=None
    for artifact_id in reversed(row.artifact_ids or []):
        try:
            candidate=router.artifacts.get(owner=owner,artifact_id=artifact_id)
            if candidate.state!='acknowledged' or candidate.media_type!='application/vnd.openclank.operation-result+json' or candidate.size_bytes>1048576: continue
            if strict:
                expiry=candidate.expires_at
                if expiry is not None:
                    if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=timezone.utc)
                    if expiry<=datetime.now(timezone.utc): continue
            chunks=[]; size=0
            for chunk in router.artifacts.read_chunks(owner=owner,artifact_id=artifact_id):
                size+=len(chunk)
                if strict and size>candidate.size_bytes: raise ValueError('artifact changed')
                chunks.append(chunk)
            data=b''.join(chunks)
            if strict and (size!=candidate.size_bytes or hashlib.sha256(data).hexdigest()!=candidate.content_sha256): continue
            raw=json.loads(data)
            if (raw.get('operationID')!=result.operation_id or raw.get('rootOperationID')!=result.root_operation_id or raw.get('state')!='complete'
                or raw.get('output')!=dict(result.output) or raw.get('bindingID')!=result.binding_id or raw.get('selectedAccountID')!=result.selected_account_id
                or raw.get('modelRouteID')!=result.model_route_id or raw.get('connectionID')!=result.connection_id or raw.get('billingLane')!=result.billing_lane): continue
            if router._validate_result(owner=owner,request=prepared.request,routes=prepared.routes,raw=raw)!=result: continue
            artifact=candidate; break
        except Exception: continue
    if artifact is None: raise ValueError('Acknowledged result artifact unavailable')
    return result,artifact,text.encode('utf-8','strict')


async def _drained(function):
    task=asyncio.create_task(asyncio.to_thread(function)); cancelled=False
    while True:
        try: result=await asyncio.shield(task); break
        except asyncio.CancelledError:
            cancelled=True
            if task.done(): result=task.result(); break
    if cancelled: raise asyncio.CancelledError
    return result


async def read_completed_summary(*,repository,provider,auth_manager,owner,body,current):
    """Read only the same current completed operation; never dispatch a model."""
    if not callable(current) or await current() is not True: raise ValueError('Original request authority changed')
    material,operation=await _generation_material(repository=repository,provider=provider,auth_manager=auth_manager,owner=owner,body=body)
    if await current() is not True: raise ValueError('Original request authority changed')
    router=get_operation_router()
    result,artifact,text=await _drained(lambda:_lookup_completed(router=router,prepared=router.prepare(operation),operation=operation,owner=owner,strict=True))
    if await current() is not True: raise ValueError('Original request authority changed')
    fresh,again=await _generation_material(repository=repository,provider=provider,auth_manager=auth_manager,owner=owner,body=body)
    if (_encoded(fresh)!=_encoded(material) or again.root_operation_id!=operation.root_operation_id
        or again.idempotency_key!=operation.idempotency_key or await current() is not True):
        raise ValueError('Summary source or authority changed')
    last,last_artifact,last_text=await _drained(lambda:_lookup_completed(router=router,prepared=router.prepare(again),operation=again,owner=owner,strict=True))
    if (last!=result or last_artifact.id!=artifact.id or last_artifact.content_sha256!=artifact.content_sha256
        or last_text!=text or await current() is not True): raise ValueError('Generated result changed')
    return VerifiedSummaryResult(_SEAL,owner,material['account_id'],result.operation_id,result.root_operation_id,body['request_id'],
        artifact.id,artifact.content_sha256,hashlib.sha256(text).hexdigest(),body['preview_hash'],
        copy.deepcopy(material['request']['receipt']),
        copy.deepcopy([{'id':item['id'],'source':item['source']} for item in material['sources']]),
        copy.deepcopy(material['omissions']),text)
