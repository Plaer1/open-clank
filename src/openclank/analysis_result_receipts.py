"""Closed, authenticated descriptors for native-analysis result custody.

Sealing is not completion proof: the caller must establish the exact persisted
completed tool part and integrity-read the same artifact before completing it.
"""
from __future__ import annotations

import hashlib
import asyncio
import json
import re
import time
from typing import Any, Mapping

from src.secret_storage import encrypt

MAX_RESULT_BYTES = 50 * 1024
_HASH = re.compile(r'[0-9a-f]{64}\Z')
from src.openclank.analysis_profiles import profile_for


_FIELDS = {'v','kind','account','workspace','chat','nativeSession','message','call','actor','root','turn',
           'executionRevision','holder','sourceID','sourceSHA','sourceRevision','operation','providerSHA',
           'providerVersion','providerEvidence','versionEvidence','artifactID','outputSHA','size','inputSHA','issued','expires'}


def legacy_producer_profile(version):
    """The closed native-analysis v1 producer is Rizin 0.8.1."""
    return profile_for('rizin', version) if version == '0.8.1' else None


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate result field')
        result[key] = value
    return result


def _json(raw: str):
    return json.loads(raw, object_pairs_hook=_unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))


def validate_result(data: bytes, *, operation: str, source_sha: str, source_revision: Mapping[str, str], provider_sha: str, expected_target=None, expected_profile=None):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_RESULT_BYTES:
        raise ValueError('analysis result is unavailable')
    try:
        decoded = data.decode('utf-8', 'strict')
        result = _json(decoded)
        # Reject unpaired surrogate escapes in every decoded string.
        def well_formed(value):
            if isinstance(value, str): value.encode('utf-8', 'strict')
            elif isinstance(value, list):
                for item in value: well_formed(item)
            elif isinstance(value, dict):
                for key, item in value.items(): well_formed(key); well_formed(item)
        well_formed(result)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError('analysis result is unavailable') from exc
    profile_facts = profile_for(result.get('provider'), result.get('providerVersion')) if type(result) is dict else None
    if (type(result) is not dict or profile_facts is None or operation not in {'functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'}
        or result.get('providerVersionEvidence') != profile_facts.result_version_evidence
        or result.get('providerSHA256') != provider_sha or result.get('sourceSHA256') != source_sha
        or result.get('sourceRevision') != dict(source_revision) or result.get('coverage') != 'partial_static_analysis'
        or operation not in result or any(key in result for key in {'functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'}-{operation})):
        raise ValueError('analysis result is unavailable')
    rows = result[operation]
    if (operation == 'info' and type(rows) is not dict) or (operation == 'decompile' and (type(rows) is not str or not rows.strip() or len(rows.encode('utf-8','strict'))>48*1024)) or (operation not in {'info','decompile'} and (type(rows) is not list or len(rows) > 256)):
        raise ValueError('analysis result is unavailable')
    if operation == 'decompile':
        target=result.get('functionTarget');profile=result.get('profile')
        if (set(result)!={'provider','providerVersion','providerVersionEvidence','providerSHA256','sourceSHA256','sourceRevision','coverage','architecture','functionTarget','profile','profileEvidence','completion','interpretation','decompile'}
            or type(target) is not dict or set(target)!={'dossierID','dossierRevision','selector','resultSHA256'}
            or expected_target is not None and target!=expected_target or type(profile) is not dict
            or set(profile)!={'providerSHA256','pluginSHA256','rootSelectorSHA256','manifestSHA256'}
            or expected_profile is not None and profile!=expected_profile or profile.get('providerSHA256')!=provider_sha
            or not isinstance(target.get('resultSHA256'),str) or not _HASH.fullmatch(target['resultSHA256'])
            or result.get('profileEvidence')!={'provider':'measured_configured_file','plugin':'measured_configured_file','root':'configured_selector_and_observed_directory','manifest':'measured_declaration_only' if profile.get('manifestSHA256') else 'unavailable','dependencies':'unverified'}
            or result.get('architecture')!='unknown' or result.get('completion')!='unknown'
            or result.get('interpretation')!='partial_provider_interpretation'):
            raise ValueError('analysis result is unavailable')
        from src.openclank.analysis_input_digest import decompile_input_sha
        decompile_input_sha('receipt!',{'kind':'receipt','value':'receipt'},{'dossier_id':target['dossierID'],
            'expected_dossier_revision':target['dossierRevision'],'selector':target['selector']},profile)
    if operation == 'disassembly':
        target=result.get('functionTarget')
        if (set(result)!={'provider','providerVersion','providerVersionEvidence','providerSHA256','sourceSHA256','sourceRevision','coverage','architecture','functionTarget','omitted','disassembly'}
            or type(target) is not dict or set(target)!={'dossierID','dossierRevision','selector','maxInstructions','resultSHA256'}
            or expected_target is not None and target != expected_target
            or type(target.get('maxInstructions')) is not int or not 1<=target['maxInstructions']<=128
            or len(rows)>target['maxInstructions'] or not isinstance(target.get('resultSHA256'),str) or not _HASH.fullmatch(target['resultSHA256'])
            or result.get('architecture')!='unknown' or type(result.get('omitted')) is not int or result['omitted']<0
            or len(rows)+result['omitted']>target['maxInstructions']):
            raise ValueError('analysis result is unavailable')
        from src.openclank.analysis_input_digest import disassembly_input_sha
        selector=target['selector']
        disassembly_input_sha('receipt!',{'kind':'receipt','value':'receipt'}, {'dossier_id':target['dossierID'],
            'expected_dossier_revision':target['dossierRevision'],'selector':selector,'max_instructions':target['maxInstructions']})
        previous=-1
        for row in rows:
            if (type(row) is not dict or set(row)!={'address','size','opcode','addressSpace'}
                or row['addressSpace']!='provider_virtual' or type(row['address']) is not int or type(row['size']) is not int
                or row['address']<=previous or row['size']<=0 or row['address']<selector['address']
                or row['address']+row['size']>selector['address']+selector['size']
                or not isinstance(row['opcode'],str) or len(row['opcode'].encode('utf-8','strict'))>1024):
                raise ValueError('analysis result is unavailable')
            previous=row['address']
    if operation == 'xrefs_to_entry':
        target=result.get('functionTarget')
        if (set(result)!={'provider','providerVersion','providerVersionEvidence','providerSHA256','sourceSHA256','sourceRevision','coverage','architecture','direction','functionTarget','omitted','xrefs_to_entry'}
            or type(target) is not dict or set(target)!={'dossierID','dossierRevision','selector','maxReferences','resultSHA256'}
            or expected_target is not None and target != expected_target or result.get('direction')!='incoming_to_entry'
            or type(target.get('maxReferences')) is not int or not 1<=target['maxReferences']<=128
            or len(rows)>target['maxReferences'] or not isinstance(target.get('resultSHA256'),str) or not _HASH.fullmatch(target['resultSHA256'])
            or result.get('architecture')!='unknown' or type(result.get('omitted')) is not int or result['omitted']<0
            or len(rows)+result['omitted']>16384):
            raise ValueError('analysis result is unavailable')
        from src.openclank.analysis_input_digest import xref_input_sha
        selector=target['selector']
        xref_input_sha('receipt!',{'kind':'receipt','value':'receipt'}, {'dossier_id':target['dossierID'],
            'expected_dossier_revision':target['dossierRevision'],'selector':selector,'max_references':target['maxReferences']})
        for row in rows:
            if (type(row) is not dict or set(row)!={'from','to','type','addressSpace'} or row['addressSpace']!='provider_virtual'
                or type(row['from']) is not int or not 0<=row['from']<=2**53-1 or type(row['to']) is not int or row['to']!=selector['address']
                or not isinstance(row['type'],str) or not row['type'] or len(row['type'].encode('utf-8','strict'))>64
                or re.search(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]',row['type'])):
                raise ValueError('analysis result is unavailable')
    if operation == 'xrefs_from_instruction':
        target=result.get('instructionTarget')
        if (set(result)!={'provider','providerVersion','providerVersionEvidence','providerSHA256','sourceSHA256','sourceRevision','coverage','architecture','direction','instructionTarget','omitted','xrefs_from_instruction'}
            or type(target) is not dict or set(target)!={'dossierID','dossierRevision','instruction','maxReferences','resultSHA256'}
            or expected_target is not None and target!=expected_target or result.get('direction')!='outgoing_from_instruction'
            or type(target.get('maxReferences')) is not int or not 1<=target['maxReferences']<=128 or len(rows)>target['maxReferences']
            or not isinstance(target.get('resultSHA256'),str) or not _HASH.fullmatch(target['resultSHA256'])
            or result.get('architecture')!='unknown' or type(result.get('omitted')) is not int or result['omitted']<0 or len(rows)+result['omitted']>16384):raise ValueError('analysis result is unavailable')
        from src.openclank.analysis_input_digest import instruction_xref_input_sha
        selector=target['instruction']
        instruction_xref_input_sha('receipt!',{'kind':'receipt','value':'receipt'},{'dossier_id':target['dossierID'],'expected_dossier_revision':target['dossierRevision'],'selector':selector,'max_references':target['maxReferences']})
        for row in rows:
            if (type(row) is not dict or set(row)!={'from','to','type','addressSpace'} or row['addressSpace']!='provider_virtual'
                or type(row['from']) is not int or row['from']!=selector['address'] or type(row['to']) is not int or not 0<=row['to']<=2**53-1
                or not isinstance(row['type'],str) or not row['type'] or len(row['type'].encode('utf-8','strict'))>64
                or re.search(r'[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]',row['type'])):raise ValueError('analysis result is unavailable')
    return result


def _validate(value: Any, *, kind: str, now: int):
    if (type(value) is not dict or type(value.get('v')) is not int or value.get('kind') != kind
        or not ((value['v'] == 1 and set(value) == _FIELDS)
                or (value['v'] == 2 and kind == 'completed' and set(value) == _FIELDS | {'provider'}))):
        raise ValueError('analysis receipt unavailable')
    for key in ('account','workspace','chat','nativeSession','message','call','actor','root','turn','holder','sourceID','artifactID'):
        if not isinstance(value[key], str) or not value[key] or len(value[key].encode('utf-8','strict')) > 512 or '\x00' in value[key]:
            raise ValueError('analysis receipt unavailable')
    for key in ('sourceSHA','providerSHA','outputSHA','inputSHA'):
        if not isinstance(value[key],str) or not _HASH.fullmatch(value[key]): raise ValueError('analysis receipt unavailable')
    revision=value['sourceRevision']
    if (type(revision) is not dict or set(revision) != {'kind','value'} or revision.get('kind') != 'hostFingerprint'
        or not isinstance(revision.get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',revision['value'])
        or len(revision['value']) > 512): raise ValueError('analysis receipt unavailable')
    # V1 is the original Rizin-only producer; version alone is not a provider identity.
    provider = value['provider'] if value['v'] == 2 else 'rizin'
    receipt_facts = (legacy_producer_profile(value['providerVersion']) if value['v'] == 1
                     else profile_for(provider, value['providerVersion']))
    if (receipt_facts is None or value['operation'] not in {'functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'}
        or value['providerEvidence'] != receipt_facts.producer_evidence or value['versionEvidence'] != receipt_facts.version_evidence
        or type(value['size']) is not int or not 0 < value['size'] <= MAX_RESULT_BYTES
        or type(value['executionRevision']) is not int or value['executionRevision'] < 0
        or type(value['issued']) is not int or type(value['expires']) is not int
        or value['issued'] > now or value['expires'] <= now or value['expires'] - value['issued'] > 86400):
        raise ValueError('analysis receipt unavailable')
    return value


def seal(value: Mapping[str, Any], *, kind: str, now: int | None = None) -> str:
    timestamp=int(time.time()) if now is None else now
    payload=_validate(dict(value),kind=kind,now=timestamp)
    encoded=encrypt(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    if not encoded.startswith('enc:'): raise ValueError('analysis receipt unavailable')
    token='ma2r1.'+encoded[4:]
    if len(token)>16384: raise ValueError('analysis receipt unavailable')
    return token


def _open_receipt(token: str, *, kind: str, now: int | None = None, historical: bool = False) -> dict[str, Any]:
    if not isinstance(token,str) or not token.startswith('ma2r1.') or len(token)>16384:
        raise ValueError('analysis receipt unavailable')
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from src import secret_storage
        # Verification must never create or rotate the application key.
        raw=Fernet(secret_storage._KEY_PATH.read_bytes()).decrypt(token[6:].encode('ascii')).decode('utf-8')
        value=_json(raw)
        timestamp=int(time.time()) if now is None else now
        if historical:
            if kind != 'completed' or type(value) is not dict or type(value.get('issued')) is not int or value['issued'] > timestamp:
                raise ValueError('analysis receipt unavailable')
            timestamp=value['issued']
        return _validate(value,kind=kind,now=timestamp)
    except (ValueError,TypeError,UnicodeError,RecursionError,OSError,InvalidToken) as exc:
        raise ValueError('analysis receipt unavailable') from exc


def open_receipt(token: str, *, kind: str, now: int | None = None) -> dict[str, Any]:
    return _open_receipt(token,kind=kind,now=now)


def open_historical_completed_receipt(token: str, *, now: int | None = None) -> dict[str, Any]:
    """Authenticate historical facts only; this is never an effect grant."""
    return _open_receipt(token,kind='completed',now=now,historical=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def complete_provisional(*, token: str, account: str, store, current, source_current, part_current) -> dict[str, Any]:
    """Verify an exact persisted completed part and return a bounded sealed fact.

    The caller owns account admission for the entire task, including the
    integrity read and acknowledgment, and supplies only server-side checks.
    """
    from src.openclank.artifacts import account_artifact_owner
    value=open_receipt(token,kind='provisional')
    if value['account'] != account or not await current(value) or not await source_current(value) or not await part_current(value,token):
        raise ValueError('analysis receipt unavailable')
    partition=account_artifact_owner(account)
    def read():
        row=store.get(owner=partition,artifact_id=value['artifactID'])
        if row.content_sha256!=value['outputSHA'] or row.size_bytes!=value['size'] or row.media_type!='application/json':
            raise ValueError('analysis receipt unavailable')
        data=b''.join(store.read_chunks(owner=partition,artifact_id=row.id))
        if len(data)!=value['size'] or digest(data)!=value['outputSHA']: raise ValueError('analysis receipt unavailable')
        return data
    data=await asyncio.to_thread(read)
    result=validate_result(data,operation=value['operation'],source_sha=value['sourceSHA'],
        source_revision=value['sourceRevision'],provider_sha=value['providerSHA'])
    if result['provider'] != 'rizin' or result['providerVersion'] != value['providerVersion']:
        raise ValueError('analysis receipt unavailable')
    if not await current(value) or not await source_current(value) or not await part_current(value,token):
        raise ValueError('analysis receipt unavailable')
    def acknowledge():
        row=store.acknowledge(owner=partition,artifact_id=value['artifactID'])
        if row.content_sha256!=value['outputSHA'] or row.size_bytes!=value['size'] or row.media_type!='application/json':
            raise ValueError('analysis receipt unavailable')
    await asyncio.to_thread(acknowledge)
    if not await current(value) or not await source_current(value) or not await part_current(value,token):
        raise ValueError('analysis receipt unavailable')
    completed={**value,'v':2,'kind':'completed','provider':result['provider']}
    facts=profile_for(result['provider'],result['providerVersion'])
    return {'state':'complete','receipt':seal(completed,kind='completed'),'result':result,
            'providerEvidence':{'executableSHA256':facts.producer_evidence,'version':facts.version_evidence}}
