"""Host-only, content-free ingress proof for one queued plain-text input.

This is not a source currentness or delivery proof. Legacy inputs and missing
proofs remain usable for steering and unavailable for capture.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import re
from typing import Any
from dataclasses import dataclass

@dataclass(frozen=True)
class QueuedSourceRef:
    """Distinct archive/native queued-text identity; not an S04 user SourceRef."""
    version: int
    kind: str
    account_id: str
    chat_id: str
    workspace_id: str
    request_id: str
    native_session_id: str
    message_id: str
    part_id: str
    archive_revision: int
    archive_content_hash: str
    text_sha256: str
    ingress_sha256: str
    root_operation_id: str
    execution_revision: int
    inner_turn_id: str


_DOMAIN = 'openclank.s04.queued-ingress.v1'
_KEYS = {'version','owner','accountID','stableChatID','sessionID','actorID','requestID','bodySHA256',
         'choices','captureEligible','policyFingerprint','workspaceID','mappingRevision','workspaceRevision','executionRevision'}


def _valid(payload: Any) -> bool:
    if type(payload) is not dict or set(payload) != _KEYS or type(payload['version']) is not int or payload['version'] != 1:
        return False
    if any(type(payload[k]) is not str or not payload[k] or payload[k] != payload[k].strip() or len(payload[k]) > 512
           for k in ('owner','accountID','stableChatID','sessionID','actorID','requestID','workspaceID')):
        return False
    if payload['actorID'] != 'main' or any(type(payload[k]) is not int or payload[k] < 0 for k in ('mappingRevision','workspaceRevision','executionRevision')):
        return False
    if any(type(payload[k]) is not str or re.fullmatch(r'[0-9a-f]{64}',payload[k]) is None for k in ('bodySHA256','policyFingerprint')):
        return False
    choices = payload['choices']
    return (type(payload['captureEligible']) is bool and type(choices) is dict and
            set(choices) == {'version','mode','allowBash','allowWebSearch','useWeb','useResearch','compareMode','incognito','noMemory'} and
            type(choices['version']) is int and choices['version'] == 1 and choices['mode'] in ('agent','chat','plan') and
            all(choices[k] is None or type(choices[k]) is bool for k in ('allowBash','allowWebSearch')) and
            all(type(choices[k]) is bool for k in ('useWeb','useResearch','compareMode','incognito','noMemory')))


def make_queued_source_proof(payload: dict[str, Any]) -> str:
    from src.secret_storage import keyed_digest
    if not _valid(payload): raise ValueError('queued source unavailable')
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    envelope=json.dumps({'payload':payload,'mac':keyed_digest(raw,context=_DOMAIN)},ensure_ascii=False,sort_keys=True,separators=(',',':'))
    if len(envelope.encode('utf-8')) > 16384: raise ValueError('queued source unavailable')
    return envelope


def verify_queued_source_proof(value: object) -> dict[str, Any] | None:
    from src import secret_storage
    if type(value) is not str or len(value) > 16384 or not secret_storage._KEY_PATH.is_file(): return None
    try:
        envelope=json.loads(value)
        if type(envelope) is not dict or set(envelope) != {'payload','mac'} or not _valid(envelope['payload']) or type(envelope['mac']) is not str: return None
        raw=json.dumps(envelope['payload'],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        expected=secret_storage.keyed_digest(raw,context=_DOMAIN)
        return envelope['payload'] if hmac.compare_digest(envelope['mac'],expected) else None
    except (ValueError,TypeError,OSError): return None


def match_queued_boundary(value: object, *, owner: str, account_id: str, chat_id: str,
                          session_id: str, actor_id: str, workspace_id: str,
                          receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Match a host-signed ingress to the exact still-pending native receipt."""
    proof=verify_queued_source_proof(value)
    if not proof or type(receipt) is not dict or type(receipt.get('text')) is not str:
        return None
    try: digest=hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest()
    except UnicodeEncodeError: return None
    expected={'owner':owner,'accountID':account_id,'stableChatID':chat_id,'sessionID':session_id,
              'actorID':actor_id,'workspaceID':workspace_id,'requestID':receipt.get('requestID'),
              'bodySHA256':digest,'choices':receipt.get('turnPolicy'),
              'mappingRevision':receipt.get('mappingRevision'),'workspaceRevision':receipt.get('workspaceRevision'),
              'executionRevision':receipt.get('executionRevision')}
    if (any(proof.get(k) != v for k,v in expected.items()) or proof['captureEligible'] is not True
        or any(proof['choices'][k] is not False for k in ('incognito','noMemory','compareMode'))):
        return None
    return proof
