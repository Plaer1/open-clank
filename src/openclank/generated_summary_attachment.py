"""Host-witnessed generated-summary attachments using existing owners."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
from dataclasses import dataclass

from src.openclank.code_summary import _generation_material, _encoded
from src.openclank.operation_router import get_operation_router, _thaw_json
from src.openclank.operation_journal import OperationJournalStore

_DOMAIN='openclank.generated-summary-attachment.v1'
_SEAL=object()


@dataclass(frozen=True)
class VerifiedGeneratedSummary:
    _seal: object
    owner: str
    origin: dict
    text: bytes


async def verify_result(*,repository,provider,auth_manager,owner,body,chat_id):
    if not isinstance(chat_id,str) or not 1<=len(chat_id)<=256: raise ValueError('Target chat unavailable')
    material,operation=await _generation_material(repository=repository,provider=provider,auth_manager=auth_manager,owner=owner,body=body)
    from src.openclank.generated_summary_result import _lookup_completed
    router=get_operation_router(); prepared=router.prepare(operation)
    result,artifact,value=_lookup_completed(router=router,prepared=prepared,operation=operation,owner=owner)
    origin={'version':1,'account_id':material['account_id'],'chat_id':chat_id,'operation_id':result.operation_id,'root_operation_id':result.root_operation_id,
        'artifact_id':artifact.id,'artifact_sha256':artifact.content_sha256,'output_sha256':hashlib.sha256(value).hexdigest(),
        'preview_hash':body['preview_hash'],'receipt':material['request']['receipt'],
        'sources':[{'id':item['id'],'source':item['source']} for item in material['sources']]}
    return VerifiedGeneratedSummary(_SEAL,owner,origin,value)


def create_upload(handler, value):
    from starlette.datastructures import UploadFile
    if type(value) is not VerifiedGeneratedSummary or value._seal is not _SEAL: raise ValueError('Generated result unavailable')
    upload=UploadFile(file=io.BytesIO(value.text),filename='code-summary.txt')
    return handler.save_upload(upload,'generated-summary',owner=value.owner,_generated_summary=value)


def seal_upload(value, upload_id, file_hash):
    from src import secret_storage
    if type(value) is not VerifiedGeneratedSummary or value._seal is not _SEAL or value.origin['output_sha256']!=file_hash: raise ValueError('Generated result changed')
    payload={**value.origin,'upload_id':upload_id}
    if len(_encoded(payload))>12000: raise ValueError('Generated provenance exceeds its bound')
    return {'payload':payload,'mac':secret_storage.keyed_digest(_encoded(payload).decode(),context=_DOMAIN)}


def verify_upload(info, *, handler, owner, account_id, chat_id):
    """Unsigned, stale or keyless metadata never gains generated authority."""
    try:
        from src import secret_storage
        record=info.get('generated_summary_origin'); payload=record['payload']
        keys={'version','account_id','chat_id','operation_id','root_operation_id','artifact_id','artifact_sha256','output_sha256','preview_hash','receipt','sources','upload_id'}
        if (type(record) is not dict or set(record)!={'payload','mac'} or type(payload) is not dict or set(payload)!=keys or payload['version']!=1
            or info.get('owner')!=owner or payload['account_id']!=account_id or payload['chat_id']!=chat_id or payload['upload_id']!=info.get('id')
            or payload['output_sha256']!=(info.get('checksum_sha256') or info.get('hash')) or type(record['mac']) is not str): return None
        key=secret_storage._KEY_PATH.read_bytes()
        expected=hmac.new(key,(_DOMAIN+'\0'+_encoded(payload).decode()).encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(record['mac'],expected): return None
        path=info.get('path')
        if not isinstance(path,str) or not handler._inside_upload_dir(path): return None
        with open(path,'rb') as source: data=source.read(262145)
        if not data or len(data)>262144 or hashlib.sha256(data).hexdigest()!=payload['output_sha256']: return None
        router=get_operation_router(); artifact=router.artifacts.get(owner=owner,artifact_id=payload['artifact_id'])
        if artifact.state!='acknowledged' or artifact.media_type!='application/vnd.openclank.operation-result+json' or artifact.content_sha256!=payload['artifact_sha256'] or artifact.size_bytes>1048576: return None
        raw=json.loads(b''.join(router.artifacts.read_chunks(owner=owner,artifact_id=artifact.id)))
        if (raw.get('operationID')!=payload['operation_id'] or raw.get('rootOperationID')!=payload['root_operation_id']
            or raw.get('state')!='complete' or not isinstance(raw.get('output'),dict)
            or raw['output'].get('text')!=data.decode('utf-8')): return None
        return payload
    except Exception:
        return None
