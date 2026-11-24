"""WB17 once/status custody on the existing encrypted Files operation ledger.

Private helpers only. Callers must hold both actual account admissions and
recheck the owner target, recipient role and native CAS before any effect.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from collections.abc import Mapping

from src.openclank.collaboration_text import MAX_EDITS, MAX_INSERT_BYTES
from src.openclank.collaboration_workbook import validate_cells, WorkbookError
from src.openclank.collaboration_slides import validate_objects, SlidesError


class CollaborationOperationError(ValueError):
    pass


def _validate_edits(edits: object) -> None:
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise CollaborationOperationError('invalid collaboration edits')
    previous = -1; previous_insertion = False; total = 0
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'start', 'end', 'text'}:
            raise CollaborationOperationError('invalid collaboration edits')
        start, end, text = edit['start'], edit['end'], edit['text']
        if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= 1024*1024
                or start < previous or (start == previous and start == end and previous_insertion)
                or not isinstance(text, str)):
            raise CollaborationOperationError('invalid collaboration edits')
        try: encoded = text.encode('utf-8', 'strict')
        except UnicodeEncodeError as exc: raise CollaborationOperationError('invalid collaboration edits') from exc
        total += len(encoded)
        if total > MAX_INSERT_BYTES or b'\x00' in encoded or b'\r' in encoded or encoded.startswith(b'\xef\xbb\xbf'):
            raise CollaborationOperationError('collaboration edits exceed bounds')
        previous = end; previous_insertion = start == end


def _intent(value: object) -> tuple[dict, str, str]:
    keys = {'owner_account', 'recipient_account', 'publication_id', 'resource_id',
            'operation_id', 'base_sha256', 'edits'}
    workbook = isinstance(value, Mapping) and value.get('kind') == 'collaboration.workbook.v1'
    slides = isinstance(value, Mapping) and value.get('kind') == 'collaboration.slides.v1'
    if workbook: keys = (keys - {'edits'}) | {'kind', 'cells'}
    elif slides: keys = (keys - {'edits'}) | {'kind', 'objects'}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CollaborationOperationError('invalid collaboration operation')
    clean = dict(value)
    for key in ('owner_account', 'recipient_account', 'publication_id', 'operation_id'):
        item = clean[key]
        if not isinstance(item, str) or not item or len(item.encode('utf-8', 'strict')) > 256 or any(ord(c) < 32 for c in item):
            raise CollaborationOperationError('invalid collaboration operation')
    if not isinstance(clean['resource_id'], str) or not re.fullmatch(r'resource-[0-9a-f]{64}', clean['resource_id']):
        raise CollaborationOperationError('invalid collaboration resource')
    if not isinstance(clean['base_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', clean['base_sha256']):
        raise CollaborationOperationError('invalid collaboration base')
    if workbook:
        try: validate_cells(clean['cells'])
        except WorkbookError as exc: raise CollaborationOperationError('invalid workbook cells') from exc
    elif slides:
        try: validate_objects(clean['objects'])
        except SlidesError as exc: raise CollaborationOperationError('invalid slide objects') from exc
    else: _validate_edits(clean['edits'])
    try:
        raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise CollaborationOperationError('invalid collaboration operation') from exc
    digest = hashlib.sha256((b'openclank.collaboration.workbook.v1\0' if workbook else b'openclank.collaboration.slides.v1\0' if slides else b'openclank.collaboration.text.v1\0')+raw).hexdigest()
    operation = 'wb17-' + hashlib.sha256((clean['recipient_account']+'\0'+clean['operation_id']).encode()).hexdigest()
    return clean, digest, operation


def public_selector_digest(*, publication_id: str, operation_id: str, base_sha256: str, edits: list) -> str:
    """Content-free selector; it is not an authorization or resource reference."""
    for item in (publication_id, operation_id):
        if not isinstance(item, str) or not item or len(item.encode('utf-8', 'strict')) > 256 or any(ord(c) < 32 for c in item):
            raise CollaborationOperationError('invalid collaboration selector')
    if not isinstance(base_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', base_sha256):
        raise CollaborationOperationError('invalid collaboration base')
    _validate_edits(edits)
    value = {'version':1, 'publication_id':publication_id, 'operation_id':operation_id,
             'base_sha256':base_sha256, 'edits':edits}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')
    return hashlib.sha256(b'openclank.collaboration.public.v1\0' + raw).hexdigest()


def public_workbook_selector_digest(*, publication_id: str, operation_id: str, base_sha256: str, cells: list) -> str:
    for item in (publication_id, operation_id):
        if not isinstance(item,str) or not item or len(item.encode('utf-8','strict'))>256 or any(ord(c)<32 for c in item): raise CollaborationOperationError('invalid collaboration selector')
    if not isinstance(base_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',base_sha256): raise CollaborationOperationError('invalid collaboration base')
    try: validate_cells(cells)
    except WorkbookError as exc: raise CollaborationOperationError('invalid workbook cells') from exc
    value={'version':1,'publication_id':publication_id,'operation_id':operation_id,'base_sha256':base_sha256,'cells':cells}
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8','strict')
    return hashlib.sha256(b'openclank.collaboration.workbook.public.v1\0'+raw).hexdigest()


def public_slides_selector_digest(*, publication_id: str, operation_id: str, base_sha256: str, objects: list) -> str:
    for item in (publication_id, operation_id):
        if not isinstance(item,str) or not item or len(item.encode('utf-8','strict'))>256 or any(ord(c)<32 for c in item): raise CollaborationOperationError('invalid collaboration selector')
    if not isinstance(base_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',base_sha256): raise CollaborationOperationError('invalid collaboration base')
    try: validate_objects(objects)
    except SlidesError as exc: raise CollaborationOperationError('invalid slide objects') from exc
    value={'version':1,'publication_id':publication_id,'operation_id':operation_id,'base_sha256':base_sha256,'objects':objects}
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8','strict')
    return hashlib.sha256(b'openclank.collaboration.slides.public.v1\0'+raw).hexdigest()


def public_digest(intent: Mapping) -> str:
    clean, _, _ = _intent(intent)
    if clean.get('kind') == 'collaboration.slides.v1':
        return public_slides_selector_digest(publication_id=clean['publication_id'],operation_id=clean['operation_id'],base_sha256=clean['base_sha256'],objects=clean['objects'])
    if clean.get('kind') == 'collaboration.workbook.v1':
        return public_workbook_selector_digest(publication_id=clean['publication_id'],operation_id=clean['operation_id'],base_sha256=clean['base_sha256'],cells=clean['cells'])
    return public_selector_digest(publication_id=clean['publication_id'], operation_id=clean['operation_id'],
                                  base_sha256=clean['base_sha256'], edits=clean['edits'])


def _initiator(value: object) -> dict:
    fields={'version','sessionID','rootOperationID','actorID','executionRevision','innerTurnID','messageID','toolCallID','argsSHA256','stableChatID'}
    if not isinstance(value, Mapping) or set(value)!=fields or type(value['version']) is not int or value['version']!=1:
        raise CollaborationOperationError('invalid collaboration initiator')
    if type(value['executionRevision']) is not int or not 0 <= value['executionRevision'] <= 9007199254740991:
        raise CollaborationOperationError('invalid collaboration initiator')
    for key in fields-{'version','executionRevision','argsSHA256'}:
        item=value[key]
        if not isinstance(item,str) or not item or len(item.encode('utf-8','strict'))>256 or any(ord(c)<32 for c in item):
            raise CollaborationOperationError('invalid collaboration initiator')
    if not isinstance(value['argsSHA256'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['argsSHA256']):
        raise CollaborationOperationError('invalid collaboration initiator')
    return {key:value[key] for key in fields}


@dataclass(frozen=True)
class Reservation:
    """Private handoff to the original admitted producer, never a capability."""
    recipient: str
    operation: str
    digest: str
    generation: int
    owner: str
    publication: str
    resource: str


def reserve(repository, *, intent: Mapping, generation: int, client_digest: str | None = None, initiator: Mapping | None = None) -> tuple[Reservation | None, dict]:
    clean, digest, operation = _intent(intent)
    actor = _initiator(initiator) if initiator is not None else None
    if actor is not None:
        encoded=json.dumps({'intent':clean,'initiator':actor},ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8','strict')
        digest=hashlib.sha256((b'openclank.collaboration.workbook.managed.v1\0' if clean.get('kind') == 'collaboration.workbook.v1' else b'openclank.collaboration.slides.managed.v1\0' if clean.get('kind') == 'collaboration.slides.v1' else b'openclank.collaboration.text.managed.v1\0')+encoded).hexdigest()
    if type(generation) is not int or generation < 0:
        raise CollaborationOperationError('invalid collaboration generation')
    selector_digest = public_digest(clean)
    if client_digest is not None and client_digest != selector_digest:
        raise CollaborationOperationError('collaboration selector mismatch')
    kind = clean.get('kind', 'collaboration.text.v1')
    receipt = {'version':1, 'kind':kind, 'state':'pending',
               'owner_account':clean['owner_account'], 'recipient_account':clean['recipient_account'],
               'publication_id':clean['publication_id'], 'resource_id':clean['resource_id'], 'client_digest':selector_digest}
    if actor is not None: receipt.update(initiator=actor, operation_id=clean['operation_id'])
    existing = repository.reserve_operation(owner_subject_id=clean['owner_account'], operation_id=operation,
        request_digest=digest, generation=generation, receipt=receipt, phase=kind)
    if existing is not None:
        return None, _receipt(existing, clean, digest, actor)
    return Reservation(clean['recipient_account'], operation, digest, generation, clean['owner_account'],
                       clean['publication_id'], clean['resource_id']), dict(receipt)


def _receipt(row: Mapping, clean: Mapping, digest: str, actor: Mapping | None = None) -> dict:
    receipt = row.get('receipt')
    if (row.get('digest') != digest or not isinstance(receipt, Mapping)
            or receipt.get('version') != 1 or receipt.get('kind') != clean.get('kind','collaboration.text.v1')
            or receipt.get('state') not in {'pending','unknown','complete','conflict','unavailable'}
            or any(receipt.get(key) != clean[key] for key in ('owner_account','recipient_account','publication_id','resource_id'))
            or (actor is not None and (receipt.get('initiator') != actor or receipt.get('operation_id') != clean['operation_id']))
            or (actor is None and 'initiator' in receipt)):
        raise CollaborationOperationError('collaboration receipt unavailable')
    if 'initiator' in receipt: _initiator(receipt['initiator'])
    return dict(receipt)


def status(repository, *, intent: Mapping) -> dict | None:
    """Read the original exact operation; absence never authorizes an issue."""
    clean, digest, operation = _intent(intent)
    row = repository.get_operation(owner_subject_id=clean['owner_account'], operation_id=operation)
    return None if row is None else _receipt(row, clean, digest)


def status_selector(repository, *, recipient_account: str, owner_account: str, publication_id: str,
                    resource_id: str, operation_id: str, client_digest: str, kind: str = 'collaboration.text.v1') -> dict | None:
    """Read-only exact selector after the caller reauthorizes the current target."""
    if (kind not in {'collaboration.text.v1','collaboration.workbook.v1','collaboration.slides.v1'} or any(not isinstance(value,str) or not value or len(value.encode('utf-8','strict'))>256 or any(ord(c)<32 for c in value)
            for value in (recipient_account,owner_account,publication_id,operation_id))
            or not isinstance(resource_id,str) or not re.fullmatch(r'resource-[0-9a-f]{64}',resource_id)
            or not isinstance(client_digest,str) or not re.fullmatch(r'[0-9a-f]{64}',client_digest)):
        raise CollaborationOperationError('collaboration selector unavailable')
    operation='wb17-'+hashlib.sha256((recipient_account+'\0'+operation_id).encode('utf-8','strict')).hexdigest()
    row=repository.get_operation(owner_subject_id=owner_account, operation_id=operation)
    if row is None: return None
    receipt=row.get('receipt')
    if (not isinstance(receipt,Mapping) or receipt.get('version')!=1 or receipt.get('kind')!=kind
        or receipt.get('state') not in {'pending','unknown','complete','conflict','unavailable'}
        or receipt.get('owner_account')!=owner_account or receipt.get('recipient_account')!=recipient_account
        or receipt.get('publication_id')!=publication_id or receipt.get('resource_id')!=resource_id
        or receipt.get('client_digest')!=client_digest):
        raise CollaborationOperationError('collaboration selector unavailable')
    if 'initiator' in receipt:
        _initiator(receipt['initiator'])
        if receipt.get('operation_id') != operation_id: raise CollaborationOperationError('collaboration selector unavailable')
    return dict(receipt)



def review(receipt: Mapping | None, operation_id: str, client_digest: str) -> dict:
    """Safe existing result projection; caller has already reauthorized the target."""
    state=receipt.get('state') if isinstance(receipt,Mapping) else 'unknown'
    if state not in {'pending','unknown','complete','conflict','unavailable'}: state='unknown'
    result={'version':1,'state':state,'operation_id':operation_id,'client_digest':client_digest}
    if state=='complete' and isinstance(receipt.get('revision'),Mapping): result['revision']=dict(receipt['revision'])
    if isinstance(receipt,Mapping) and 'initiator' in receipt and state=='complete':
        actor=_initiator(receipt['initiator']); history=receipt.get('history'); binding=receipt.get('native_binding'); revision=receipt.get('revision')
        if (receipt.get('operation_id')!=operation_id or receipt.get('client_digest')!=client_digest
                or not isinstance(history,Mapping) or set(history)!={'state','actionId'} or history.get('state')!='complete'
                or not isinstance(binding,Mapping) or history.get('actionId')!=binding.get('action_id')
                or not isinstance(history.get('actionId'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',history['actionId'])
                or not isinstance(revision,Mapping) or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint'
                or not isinstance(revision.get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:(0|[1-9][0-9]*)',revision['value'])):
            raise CollaborationOperationError('collaboration review unavailable')
        result['review']={'initiator':actor,'history':{'state':'complete','actionId':history['actionId']}}
    return result
