"""Closed human-analysis project identities and Markdown envelopes.

This module does not authorize paths, perform writes, or attest provider output.
"""
from __future__ import annotations

import hashlib
import base64
import asyncio
import json
import re
from typing import Any, Mapping

from src.openclank.workspace_policy_service import resolve_owned_workspace
from src.openclank.analysis_profiles import profile_for

_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
_ADDRESS = re.compile(r'0x(?:0|[1-9a-f][0-9a-f]*)\Z')
_OPERATION = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')
_FENCE = '\n```\n'
_DOSSIER_LIMIT = 96 * 1024
_INGEST_LIMIT = 2 * 1024 * 1024


def provider_document(identity, dossier_id, receipt, data):
    from src.openclank.analysis_result_receipts import open_historical_completed_receipt, digest, validate_result
    if not isinstance(dossier_id,str) or not _ID.fullmatch(dossier_id): raise ValueError('invalid dossier')
    facts=open_historical_completed_receipt(receipt)
    if facts['account'] != identity['ownerSubjectID'] or facts['workspace'] != identity['workspaceID'] or facts['size'] != len(data) or facts['outputSHA'] != digest(data): raise ValueError('dossier evidence changed')
    result=validate_result(data,operation=facts['operation'],source_sha=facts['sourceSHA'],source_revision=facts['sourceRevision'],provider_sha=facts['providerSHA'])
    if result['provider'] != facts.get('provider','rizin') or result['providerVersion'] != facts['providerVersion']:
        raise ValueError('dossier evidence changed')
    header={'version':1,'evidenceKind':'provider_result',**identity,'dossierID':dossier_id,'completedReceipt':receipt,'outputSHA256':facts['outputSHA'],'outputSize':len(data)}
    encoded=_json(header);_text(encoded,20*1024)
    document='```json\n'+encoded+_FENCE+'\n```base64\n'+base64.b64encode(data).decode('ascii')+_FENCE
    _text(document,_DOSSIER_LIMIT)
    return document


def parse_provider_document(document, identity, dossier_id, *, include_bytes=False):
    _text(document,_DOSSIER_LIMIT)
    prefix='```json\n';separator=_FENCE+'\n```base64\n'
    if not document.startswith(prefix) or not document.endswith(_FENCE): raise ValueError('invalid dossier')
    pos=document.find(separator,len(prefix))
    if pos < 0: raise ValueError('invalid dossier')
    raw=document[len(prefix):pos];_text(raw,20*1024);header=_decode(raw)
    if type(header) is not dict or set(header)!={'version','evidenceKind','ownerSubjectID','workspaceID','projectKey','dossierID','completedReceipt','outputSHA256','outputSize'}: raise ValueError('invalid dossier')
    try:data=base64.b64decode(document[pos+len(separator):-len(_FENCE)],validate=True)
    except Exception as exc:raise ValueError('invalid dossier') from exc
    if document != provider_document(identity,dossier_id,header['completedReceipt'],data): raise ValueError('invalid dossier')
    from src.openclank.analysis_result_receipts import open_historical_completed_receipt, validate_result
    facts=open_historical_completed_receipt(header['completedReceipt'])
    result=validate_result(data,operation=facts['operation'],source_sha=facts['sourceSHA'],source_revision=facts['sourceRevision'],provider_sha=facts['providerSHA'])
    return (facts,result,data) if include_bytes else (facts,result)


def host_elf_document(identity, dossier_id, receipt, data):
    from cryptography.fernet import Fernet, InvalidToken
    from src import secret_storage
    if not isinstance(receipt,str) or not receipt.startswith('ma-elf1.') or len(receipt)>16384:
        raise ValueError('ELF dossier unavailable')
    try:
        facts=_decode(Fernet(secret_storage._KEY_PATH.read_bytes()).decrypt(receipt[8:].encode('ascii')).decode('utf-8'))
    except (InvalidToken,OSError,UnicodeError,ValueError,TypeError) as exc:
        raise ValueError('ELF dossier unavailable') from exc
    keys={'version','kind','account','workspace','dossier','sourceID','sourceSHA256','sourceRevision','resultSHA256'}
    if (type(facts) is not dict or set(facts)!=keys or facts['version']!=1 or facts['kind']!='host_elf_metadata'
        or facts['account']!=identity['ownerSubjectID'] or facts['workspace']!=identity['workspaceID']
        or facts['dossier']!=dossier_id or hashlib.sha256(data).hexdigest()!=facts['resultSHA256']):
        raise ValueError('ELF dossier unavailable')
    result=validate_host_elf_result(data,facts)
    header={'version':1,'evidenceKind':'host_elf_metadata',**identity,'dossierID':dossier_id,'receipt':receipt,'outputSHA256':facts['resultSHA256'],'outputSize':len(data)}
    return '```json\n'+_json(header)+_FENCE+'\n```base64\n'+base64.b64encode(data).decode('ascii')+_FENCE, facts, result


def validate_host_elf_result(data,facts):
    if not 0<len(data)<=50*1024 or not isinstance(facts['sourceID'],str) or not facts['sourceID'] or not isinstance(facts['sourceSHA256'],str) or not re.fullmatch(r'[0-9a-f]{64}',facts['sourceSHA256']):
        raise ValueError('ELF dossier unavailable')
    revision=facts['sourceRevision']
    if (type(revision) is not dict or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint'
        or not isinstance(revision.get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',revision['value'])
        or not revision['value'].startswith('sha256:'+facts['sourceSHA256']+':')):
        raise ValueError('ELF dossier unavailable')
    result=_decode(data.decode('ascii'))
    keys={'version','operation','producer','producerVersion','sourceSHA256','sourceRevision','coverage','payloadIntegrity','info'}
    info_keys={'bits','byteOrder','fileBytes','objectType','machine','osAbi','abiVersion','entryAddress','flags','programCount','sectionCount','sectionNames','omittedPrograms','omittedSections','observedBytes','rangeCalls'}
    if (type(result) is not dict or set(result)!=keys or type(result.get('info')) is not dict or set(result['info'])!=info_keys
        or result['info']['bits'] not in (32,64) or result['info']['byteOrder'] not in ('little','big')
        or result['info']['sectionNames'] not in ('present','absent')
        or any(type(result['info'][k]) is not int or not 0<=result['info'][k]<=2**53-1 for k in info_keys-{'byteOrder','sectionNames','entryAddress'})
        or not isinstance(result['info']['entryAddress'],str) or not re.fullmatch(r'(0|[1-9][0-9]{0,19})',result['info']['entryAddress'])
        or _json(result).encode('ascii')!=data
        or result.get('version')!=1 or result.get('operation')!='info'
        or result.get('producer')!='openclank.elf-metadata' or result.get('producerVersion')!='elf-v1-metadata-1'
        or result.get('sourceSHA256')!=facts['sourceSHA256'] or result.get('sourceRevision')!=facts['sourceRevision']
        or result.get('coverage')!='partial_declared_metadata' or result.get('payloadIntegrity')!='not_checked'):
        raise ValueError('ELF dossier unavailable')
    return result


def parse_host_elf_document(document,identity,dossier_id):
    prefix='```json\n'; separator=_FENCE+'\n```base64\n'
    if not document.startswith(prefix) or not document.endswith(_FENCE): raise ValueError('ELF dossier unavailable')
    pos=document.find(separator,len(prefix))
    if pos<0: raise ValueError('ELF dossier unavailable')
    header=_decode(document[len(prefix):pos])
    if type(header) is not dict or header.get('evidenceKind')!='host_elf_metadata': raise ValueError('ELF dossier unavailable')
    try: data=base64.b64decode(document[pos+len(separator):-len(_FENCE)],validate=True)
    except Exception as exc: raise ValueError('ELF dossier unavailable') from exc
    expected,facts,result=host_elf_document(identity,dossier_id,header.get('receipt'),data)
    if document!=expected: raise ValueError('ELF dossier unavailable')
    return facts,result,data


class AnalysisPreDispatchRefusal(ValueError):
    """A validated MA2 observation was refused before its mutation boundary."""


def _text(value: Any, limit: int, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError('invalid text')
    try:
        size = len(value.encode('utf-8', 'strict'))
    except UnicodeError as exc:
        raise ValueError('invalid Unicode') from exc
    if size > limit or (nonempty and not size):
        raise ValueError('text outside bounds')
    return value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _decode(raw: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError('invalid JSON') from exc


def project_identity(owner_subject_id: str, workspace_id: str) -> dict[str, Any]:
    owner = _text(owner_subject_id, 256)
    workspace = _text(workspace_id, 256)
    encoded = json.dumps(['openclank.analysis-project.v1', owner, workspace], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'version': 1, 'ownerSubjectID': owner, 'workspaceID': workspace,
            'projectKey': 'analysis-' + hashlib.sha256(encoded).hexdigest()}


def initialization_steps(identity: Mapping[str, Any], operation_id: str) -> tuple[tuple[str, str], ...]:
    expected = project_identity(identity['ownerSubjectID'], identity['workspaceID'])
    if dict(identity) != expected or not isinstance(operation_id, str) or not _OPERATION.fullmatch(operation_id):
        raise ValueError('invalid initialization identity')
    components = ('.clanker', 'robonotes', expected['projectKey'], 'analysis')
    result = []
    for index, component in enumerate(components):
        value = ['openclank.analysis-init.v1', expected['ownerSubjectID'], expected['workspaceID'], operation_id, index, component]
        digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
        result.append((component, 'ma2-init-' + digest))
    return tuple(result)


def validate_mutation_request(body: Any) -> dict[str, Any]:
    required = {'workspaceID', 'sourceRef', 'sourceRevision', 'annotationID', 'title', 'text'}
    if type(body) is not dict or not required.issubset(body) or set(body) - required - {'address', 'expectedRevision'}:
        raise ValueError('invalid annotation request')
    _text(body['workspaceID'], 256)
    _text(body['sourceRef'], 16384)
    if not isinstance(body['annotationID'], str) or not _ID.fullmatch(body['annotationID']):
        raise ValueError('invalid annotation identity')
    _text(body['title'], 200)
    _text(body['text'], 16384, nonempty=False)
    for key in ('sourceRevision', 'expectedRevision'):
        if key not in body:
            continue
        revision = body[key]
        if type(revision) is not dict or set(revision) != {'kind', 'value'} or revision['kind'] != 'hostFingerprint':
            raise ValueError('invalid revision')
        if not isinstance(revision['value'], str) or len(revision['value']) > 512 or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+', revision['value']):
            raise ValueError('invalid revision')
    # Source-size-dependent address bounds are checked after trusted capture.
    if 'address' in body and body['address'] is None:
        raise ValueError('invalid address')
    return dict(body)


def resolve_project(repository, *, workspace_id: str, owner_username: str, account_id: str, auth_manager):
    """Resolve existing read authority; caller must separately authorize effects."""
    if auth_manager.account_id(owner_username) != account_id:
        raise ValueError('account unavailable')
    binding = resolve_owned_workspace(repository, workspace_id=_text(workspace_id, 256),
        owner_username=owner_username, auth_manager=auth_manager, purpose='app_folder')
    if binding.workspace.owner_subject_id != account_id or auth_manager.account_id(owner_username) != account_id:
        raise ValueError('account changed')
    return binding, project_identity(account_id, binding.workspace.id)


def read_only_project(*, facade, context, repository, auth_manager, owner, account, workspace_id, extra_current=None):
    """Construct the shared current-account Files read authority; never grants writes."""
    from pathlib import Path
    binding, identity = resolve_project(repository, workspace_id=workspace_id, owner_username=owner,
        account_id=account, auth_manager=auth_manager)
    host = facade._providers.get('host')
    if (host is None or context.owner_subject_id != account or context.owner_username != owner
        or context.workspace_id != binding.workspace.id):
        raise ValueError('analysis workspace unavailable')
    async def current(original, expected, original_context):
        try:
            latest, latest_identity = resolve_project(repository, workspace_id=expected['workspaceID'],
                owner_username=owner, account_id=account, auth_manager=auth_manager)
            if (latest_identity != expected or latest.path != original.path or latest.workspace != original.workspace
                or repository.generation() != original_context.policy_generation
                or auth_manager.account_id(owner) != original_context.owner_subject_id
                or auth_manager.username_for_account_id(original_context.owner_subject_id) != owner
                or (extra_current is not None and not extra_current())):
                return False
            relative = '/'.join(filter(None, (latest.workspace.relative_folder, '.clanker', 'robonotes', expected['projectKey'], 'analysis')))
            decision = repository.resolve(subject_id=account, is_admin=original_context.is_admin, origin='app',
                location_id=latest.workspace.location_id, capability='read', workspace_id=latest.workspace.id,
                relative_resource=relative, observation_only=True)
            if not decision.allowed or decision.policy_generation != original_context.policy_generation:
                return False
            return not any(part.lower() in {'.env', '.secrets', 'auth', 'credentials', 'tokens', 'settings'} for part in Path(latest.path).parts)
        except Exception:
            return False
    return AnalysisProjects(facade=facade, host=host, context=context, binding=binding, identity=identity, current=current, read_only=True)


def manifest(identity: Mapping[str, Any]) -> str:
    expected = project_identity(identity['ownerSubjectID'], identity['workspaceID'])
    if dict(identity) != expected:
        raise ValueError('project identity mismatch')
    result = '# Analysis project\n\n```json\n' + _json(expected) + _FENCE
    _text(result, 4096)
    return result


def verify_manifest(text: str, identity: Mapping[str, Any]) -> None:
    _text(text, 4096)
    prefix = '# Analysis project\n\n```json\n'
    if not text.startswith(prefix) or not text.endswith(_FENCE):
        raise ValueError('invalid manifest')
    parsed = _decode(text[len(prefix):-len(_FENCE)])
    expected = project_identity(identity['ownerSubjectID'], identity['workspaceID'])
    if (type(parsed) is not dict or type(parsed.get('version')) is not int or parsed != expected
            or dict(identity) != expected or text != manifest(expected)):
        raise ValueError('manifest identity mismatch')


def validate_address(value: Any, source_size: int) -> dict[str, str] | None:
    if value is None:
        return None
    if (type(value) is not dict or set(value) not in ({'space', 'start'}, {'space', 'start', 'endExclusive'})
            or not isinstance(value['space'], str) or value['space'] not in {'file_offset', 'virtual', 'rva'}):
        raise ValueError('invalid address')
    numbers = []
    for key in ('start', 'endExclusive'):
        if key not in value:
            continue
        raw = value[key]
        if not isinstance(raw, str) or not _ADDRESS.fullmatch(raw) or len(raw) > 18:
            raise ValueError('invalid address')
        number = int(raw, 16)
        if number > 2**64 - 1:
            raise ValueError('invalid address')
        numbers.append(number)
    if len(numbers) == 2 and numbers[1] <= numbers[0]:
        raise ValueError('invalid address range')
    if value['space'] == 'file_offset' and (type(source_size) is not int or source_size <= 0 or numbers[0] >= source_size or (len(numbers) == 2 and numbers[1] > source_size)):
        raise ValueError('file offset outside source')
    return dict(value)


def annotation(identity: Mapping[str, Any], *, source_id: str, source_sha256: str,
               source_revision: Mapping[str, str], source_size: int, annotation_id: str,
               title: str, text: str, address: Any = None) -> str:
    expected = project_identity(identity['ownerSubjectID'], identity['workspaceID'])
    if dict(identity) != expected or not isinstance(annotation_id, str) or not _ID.fullmatch(annotation_id):
        raise ValueError('invalid annotation identity')
    source_id = _text(source_id, 256)
    if not isinstance(source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha256):
        raise ValueError('invalid source hash')
    if type(source_revision) is not dict or set(source_revision) != {'kind', 'value'} or source_revision['kind'] != 'hostFingerprint':
        raise ValueError('invalid source revision')
    if not isinstance(source_revision['value'], str) or len(source_revision['value']) > 512 or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+', source_revision['value']):
        raise ValueError('invalid source revision')
    title = _text(title, 200)
    text = _text(text, 16384, nonempty=False)
    header = {'version': 1, 'evidenceKind': 'human_observation', **expected, 'annotationID': annotation_id,
              'sourceID': source_id, 'sourceSHA256': source_sha256, 'sourceRevision': dict(source_revision),
              'address': validate_address(address, source_size), 'title': title}
    encoded = _json(header)
    _text(encoded, 8192)
    result = '```json\n' + encoded + _FENCE + '\n' + text
    _text(result, 32768)
    return result


def parse_annotation(document: str, identity: Mapping[str, Any], *, annotation_id: str) -> dict[str, Any]:
    """Read a closed historical envelope without assigning it to a new source."""
    _text(document, 32768)
    prefix = '```json\n'
    if not document.startswith(prefix):
        raise ValueError('invalid annotation')
    end = document.find(_FENCE, len(prefix))
    if end < 0 or document[end + len(_FENCE):end + len(_FENCE) + 1] != '\n':
        raise ValueError('invalid annotation')
    raw = document[len(prefix):end]
    _text(raw, 8192)
    header = _decode(raw)
    if type(header) is not dict or set(header) != {'version', 'evidenceKind', 'ownerSubjectID', 'workspaceID', 'projectKey', 'annotationID', 'sourceID', 'sourceSHA256', 'sourceRevision', 'address', 'title'}:
        raise ValueError('invalid annotation')
    text = document[end + len(_FENCE) + 1:]
    canonical = annotation(identity, source_id=header['sourceID'], source_sha256=header['sourceSHA256'],
        source_revision=header['sourceRevision'], source_size=2**64, annotation_id=annotation_id,
        title=header['title'], text=text, address=header['address'])
    expected_header = _decode(canonical[len(prefix):canonical.find(_FENCE, len(prefix))])
    if type(header.get('version')) is not int or header != expected_header:
        raise ValueError('annotation identity mismatch')
    return {'title': header['title'], 'text': text, 'address': header['address'],
            'sourceID': header['sourceID'], 'sourceSHA256': header['sourceSHA256'], 'sourceRevision': header['sourceRevision']}


def verify_annotation(document: str, identity: Mapping[str, Any], *, source_id: str,
                      source_sha256: str, source_revision: Mapping[str, str],
                      source_size: int, annotation_id: str) -> dict[str, Any]:
    parsed = parse_annotation(document, identity, annotation_id=annotation_id)
    if (parsed['sourceID'] != source_id or parsed['sourceSHA256'] != source_sha256
            or parsed['sourceRevision'] != dict(source_revision)):
        raise ValueError('annotation source mismatch')
    validate_address(parsed['address'], source_size)
    return parsed


async def capture_annotation(facade, context, identity: Mapping[str, Any], body: Any):
    """Capture and bind an observation without granting or writing a destination."""
    from src.openclank.analysis_adapters.base import capture_source
    request = validate_mutation_request(body)
    provider, ref = facade._provider_for_ref(context, request['sourceRef'], capability='download')
    if ref.provider != 'host' or context.owner_subject_id != identity['ownerSubjectID'] or context.workspace_id != identity['workspaceID']:
        raise ValueError('source or account unavailable')
    captured = await capture_source(facade, context, request['sourceRef'], request['sourceRevision'])
    document = annotation(identity, source_id=ref.stable_id, source_sha256=captured.sha256,
        source_revision=captured.revision, source_size=len(captured.data), annotation_id=request['annotationID'],
        title=request['title'], text=request['text'], address=request.get('address'))
    return {'request': request, 'sourceID': ref.stable_id, 'sourceSHA256': captured.sha256,
            'sourceRevision': captured.revision, 'sourceSize': len(captured.data), 'document': document}


class AnalysisProjects:
    """Fixed Files-owned analysis destination; no path is accepted from a caller."""

    def __init__(self, *, facade, host, context, binding, identity, current, read_only=False):
        if type(read_only) is not bool:
            raise ValueError('invalid project mode')
        if not read_only and host.mutation_scope is None:
            raise ValueError('account admission unavailable')
        self.facade, self.host, self.context = facade, host, context
        self.binding, self.identity, self.current = binding, dict(identity), current
        self._read_only = read_only

    def _require_write(self):
        if self._read_only:
            raise ValueError('project is read only')

    async def _check(self):
        if not await self.current(self.binding, self.identity, self.context):
            raise ValueError('workspace or account changed')

    async def _resource(self, path: str, *, kind: str):
        from src.openclank.files_host_provider import _origin
        await self._check()
        resource = await self.host.resource_for_path(self.context, path=path)
        if resource.origin_id != _origin(path) or resource.kind != kind:
            raise ValueError('fixed resource changed')
        required = {'children'} if self._read_only else {'children', 'write'}
        if kind == 'folder' and not required.issubset(resource.capabilities):
            raise ValueError('fixed folder is unavailable')
        return self.facade._resource(self.context, 'host', resource), resource

    async def _read_text(self, path: str, limit: int):
        from src.openclank.files_host_provider import _origin
        await self._check()
        bounded = await self.host.stat_inspection(self.context, origin_id=_origin(path))
        if (bounded.origin_id != _origin(path) or bounded.kind != 'file' or type(bounded.size) is not int
                or not 0 < bounded.size <= limit):
            raise ValueError('fixed document exceeds bounds')
        ref, resource = await self._resource(path, kind='file')
        if (type(resource.size) is not int or resource.size > limit
                or resource.size != bounded.size or not isinstance(resource.revision, Mapping)
                or resource.revision.get('kind') != 'hostFingerprint'):
            raise ValueError('fixed document exceeds bounds')
        opened = await self.facade.open_payload(self.context, resource_ref=ref['ref'], expected_revision=resource.revision)
        payload = opened.get('payload') or {}
        text = payload.get('text')
        _text(text, limit)
        await self._check()
        return ref, text

    async def _listed_child(self, path: str, name: str):
        import os
        from src.openclank.files_host_provider import _origin
        cursor = None
        snapshot = None
        seen = set()
        for _ in range(16):
            await self._check()
            page = await self.host.children(self.context, parent_origin_id=_origin(path), cursor=cursor,
                snapshot=snapshot, limit=200, sort={'key':'name','direction':'asc','directories_first':True}, query='')
            if snapshot is not None and page.snapshot != snapshot:
                raise ValueError('directory listing changed')
            snapshot = page.snapshot
            for entry in page.entries:
                if entry.name == name:
                    if entry.origin_id != _origin(os.path.join(path, name)):
                        raise ValueError('fixed resource changed')
                    return entry
            cursor = page.next_cursor
            if cursor is None:
                return None
            if cursor in seen:
                raise ValueError('directory listing unavailable')
            seen.add(cursor)
        raise ValueError('directory listing exceeds bounds')

    async def preview(self, source_ref: str, annotation_id: str | None = None) -> dict[str, Any]:
        import os
        from src.openclank.analysis_adapters.base import _bounded_identity, capture_source, source_current
        from src.openclank.files_host_provider import _origin
        _text(source_ref, 16384)
        if annotation_id is not None and (not isinstance(annotation_id, str) or not _ID.fullmatch(annotation_id)):
            raise ValueError('invalid annotation identity')
        await self._check()
        provider, ref = self.facade._provider_for_ref(self.context, source_ref, capability='download')
        if ref.provider != 'host':
            raise ValueError('source unavailable')
        size, identity = await _bounded_identity(self.facade, provider, self.context, ref)
        entry = await provider.stat(self.context, origin_id=ref.origin_id)
        revision = self.facade._provider_revision(entry)
        if (entry.origin_id != ref.origin_id or entry.kind != 'file' or entry.size != size
                or not isinstance(revision, Mapping) or revision.get('kind') != 'hostFingerprint'
                or await _bounded_identity(self.facade, provider, self.context, ref) != (size, identity)):
            raise ValueError('source changed')
        captured = await capture_source(self.facade, self.context, source_ref, revision)
        fresh = self.facade._resource(self.context, 'host', entry, parent_stable_id=ref.parent_stable_id)
        if fresh.get('id') != ref.stable_id:
            raise ValueError('source identity changed')
        result = {'workspaceID':self.identity['workspaceID'], 'projectKey':self.identity['projectKey'],
                  'source':{'ref':fresh['ref'], 'resourceID':ref.stable_id, 'size':size,
                            'sha256':captured.sha256, 'revision':dict(revision)},
                  'project':{'state':'uninitialized'}, 'note':None}
        path = self.binding.path
        await self._resource(path, kind='folder')
        for component in ('.clanker', 'robonotes', self.identity['projectKey'], 'analysis'):
            child = await self._listed_child(path, component)
            if child is None:
                break
            if child.kind != 'folder':
                raise ValueError('project directory unavailable')
            path = os.path.join(path, component)
            await self._resource(path, kind='folder')
        else:
            if await self._listed_child(path, '00-index.md') is not None:
                _, contents = await self._read_text(os.path.join(path, '00-index.md'), 4096)
                verify_manifest(contents, self.identity)
                result['project'] = {'state':'ready'}
                if annotation_id is not None:
                    name = annotation_id + '.md'
                    child = await self._listed_child(path, name)
                    if child is None:
                        result['note'] = {'state':'missing'}
                    else:
                        note_ref, document = await self._read_text(os.path.join(path, name), 32768)
                        parsed = parse_annotation(document, self.identity, annotation_id=annotation_id)
                        matches = (parsed['sourceID'] == ref.stable_id and parsed['sourceSHA256'] == captured.sha256
                                   and parsed['sourceRevision'] == dict(revision))
                        if matches:
                            validate_address(parsed['address'], size)
                        result['note'] = {'state':'current' if matches else 'historical', 'annotationID':annotation_id,
                            'title':parsed['title'], 'text':parsed['text'], 'address':parsed['address'],
                            'revision':note_ref['revision'], 'ref':note_ref['ref']}
        if annotation_id is not None and result['project']['state'] == 'uninitialized':
            result['note'] = {'state':'missing'}
        await self._check()
        if not await source_current(self.facade, self.context, source_ref, revision):
            raise ValueError('source changed')
        return result

    async def _existing_directory(self):
        import os
        path = self.binding.path
        parent, _ = await self._resource(path, kind='folder')
        for component in ('.clanker', 'robonotes', self.identity['projectKey'], 'analysis'):
            path = os.path.join(path, component)
            parent, _ = await self._resource(path, kind='folder')
        _, contents = await self._read_text(os.path.join(path, '00-index.md'), 4096)
        verify_manifest(contents, self.identity)
        return path, parent

    async def _prepare_observation(self, body: Any):
        import os
        from src.openclank.analysis_adapters.base import source_current
        await self._check()
        captured = await capture_annotation(self.facade, self.context, self.identity, body)
        request = captured['request']
        path, directory = await self._existing_directory()
        name = request['annotationID'] + '.md'
        if 'expectedRevision' in request:
            ref, old = await self._read_text(os.path.join(path, name), 32768)
            verify_annotation(old, self.identity, source_id=captured['sourceID'], source_sha256=captured['sourceSHA256'],
                source_revision=captured['sourceRevision'], source_size=captured['sourceSize'], annotation_id=request['annotationID'])
            if ref.get('revision') != request['expectedRevision']:
                raise ValueError('annotation revision changed')
        await self._check()
        if not await source_current(self.facade, self.context, request['sourceRef'], request['sourceRevision']):
            raise ValueError('source changed')
        return captured, request, path, directory, name, ref if 'expectedRevision' in request else None

    async def save_observation(self, body: Any) -> dict[str, Any]:
        self._require_write()
        from src.openclank.analysis_adapters.base import source_current
        from src.openclank.files_facade import FilesFacadeError
        try:
            captured, request, path, directory, name, ref = await self._prepare_observation(body)
        except FilesFacadeError:
            raise
        except (ValueError, UnicodeError) as exc:
            raise AnalysisPreDispatchRefusal('Analysis observation was refused before dispatch') from exc
        async def before_dispatch():
            try:
                await self._check()
                latest_path, latest_directory = await self._existing_directory()
                if latest_path != path or latest_directory.get('id') != directory.get('id'):
                    raise ValueError('analysis destination changed')
                if not await source_current(self.facade, self.context, request['sourceRef'], request['sourceRevision']):
                    raise ValueError('source changed')
            except FilesFacadeError:
                raise
            except (ValueError, UnicodeError) as exc:
                raise AnalysisPreDispatchRefusal('Analysis observation was refused before dispatch') from exc
        if 'expectedRevision' in request:
            result = await self.facade.save_resource(self.context, resource_ref=ref['ref'],
                expected_revision=request['expectedRevision'], text=captured['document'], before_dispatch=before_dispatch)
        else:
            result = await self.facade.create(self.context, parent_ref=directory['ref'], name=name,
                text=captured['document'], before_dispatch=before_dispatch)
        try:
            await self._check()
            current = await source_current(self.facade, self.context, request['sourceRef'], request['sourceRevision'])
        except Exception:
            current = False
        return {'receipt': result, 'sourceCurrent': bool(current)}

    async def read_provider_dossier(self, dossier_id, source_ref=None, *, include_result_bytes=False):
        import os
        from src.openclank.analysis_adapters.base import capture_source
        if not isinstance(dossier_id,str) or not _ID.fullmatch(dossier_id): raise ValueError('invalid dossier')
        path,_=await self._existing_directory()
        ref,document=await self._read_text(os.path.join(path,'provider-'+dossier_id+'.md'),_DOSSIER_LIMIT)
        header=_decode(document[len('```json\n'):document.find(_FENCE)]) if document.startswith('```json\n') and _FENCE in document else None
        if type(header) is dict and header.get('evidenceKind')=='host_elf_metadata':
            facts,result,data=parse_host_elf_document(document,self.identity,dossier_id)
            source_state='unchecked'
            if source_ref is not None:
                from src.openclank.analysis_adapters.base import capture_source
                _,source=self.facade._provider_for_ref(self.context,source_ref,capability='download')
                if source.provider!='host' or source.stable_id!=facts['sourceID']: raise ValueError('source unavailable')
                provider,_=self.facade._provider_for_ref(self.context,source_ref,capability='download')
                from src.openclank.analysis_adapters.base import _bounded_identity
                size,tag=await _bounded_identity(self.facade,provider,self.context,source)
                entry=await provider.stat(self.context,origin_id=source.origin_id)
                revision=self.facade._provider_revision(entry)
                if entry.origin_id!=source.origin_id or entry.kind!='file' or entry.size!=size or await _bounded_identity(self.facade,provider,self.context,source)!=(size,tag): raise ValueError('source changed')
                if revision!=facts['sourceRevision']:
                    source_state='historical'
                else:
                    captured=await capture_source(self.facade,self.context,source_ref,revision)
                    if captured.sha256!=facts['sourceSHA256']: raise ValueError('source changed')
                    source_state='current'
            await self._check()
            return {'state':'verified','sourceState':source_state,'result':result,'revision':ref['revision'],'ref':ref['ref'],
                    'resultIdentity':{'sourceID':facts['sourceID'],'sourceSHA256':facts['sourceSHA256'],
                        'sourceRevision':facts['sourceRevision'],'resultSHA256':facts['resultSHA256'],
                        'operation':'info','producer':'openclank.elf-metadata','producerVersion':'elf-v1-metadata-1',
                        'producerEvidence':'host_owned_capture','coverage':'partial_declared_metadata','payloadIntegrity':'not_checked'},
                    **({'_result_bytes':data} if include_result_bytes else {})}
        facts,result,data=parse_provider_document(document,self.identity,dossier_id,include_bytes=True)
        source_state='unchecked'
        if source_ref is not None:
            _text(source_ref,16384)
            _,source=self.facade._provider_for_ref(self.context,source_ref,capability='download')
            if source.provider!='host' or source.stable_id!=facts['sourceID']: raise ValueError('source unavailable')
            provider,_=self.facade._provider_for_ref(self.context,source_ref,capability='download')
            from src.openclank.analysis_adapters.base import _bounded_identity
            size,tag=await _bounded_identity(self.facade,provider,self.context,source)
            entry=await provider.stat(self.context,origin_id=source.origin_id)
            revision=self.facade._provider_revision(entry)
            if entry.origin_id!=source.origin_id or entry.kind!='file' or entry.size!=size or await _bounded_identity(self.facade,provider,self.context,source)!=(size,tag): raise ValueError('source changed')
            captured=await capture_source(self.facade,self.context,source_ref,revision)
            source_state='current' if captured.sha256==facts['sourceSHA'] and revision==facts['sourceRevision'] else 'historical'
        await self._check()
        return {'state':'verified','sourceState':source_state,'result':result,'revision':ref['revision'],'ref':ref['ref'],
                'resultIdentity':{'sourceID':facts['sourceID'],'sourceSHA256':facts['sourceSHA'],'sourceRevision':facts['sourceRevision'],
                    'resultSHA256':facts['outputSHA'],'operation':facts['operation'],'provider':result['provider'],'providerSHA256':facts['providerSHA'],
                    'providerVersion':facts['providerVersion'],'executableEvidence':facts['providerEvidence'],'versionEvidence':facts['versionEvidence']},
                'producerEvidence':{'executableSHA256':facts['providerEvidence'],'version':facts['versionEvidence']},
                **({'_result_bytes':data} if include_result_bytes else {})}

    async def inspect_provider_function(self, dossier_id, expected_revision, selector, source_ref=None):
        from src.openclank.analysis_dossiers import project_function
        if (type(expected_revision) is not dict or set(expected_revision)!={'kind','value'}
            or expected_revision.get('kind')!='hostFingerprint' or not isinstance(expected_revision.get('value'),str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',expected_revision['value']) or len(expected_revision['value'])>512):
            raise ValueError('invalid dossier revision')
        verified=await self.read_provider_dossier(dossier_id,source_ref)
        if verified['revision']!=expected_revision:raise ValueError('dossier revision changed')
        return project_function(verified,selector)

    async def inspect_provider_instruction(self,dossier_id,expected_revision,selector,source_ref=None):
        from src.openclank.analysis_input_digest import instruction_xref_input_sha
        instruction_xref_input_sha('receipt!',{'kind':'receipt','value':'receipt'},
            {'dossier_id':dossier_id,'expected_dossier_revision':expected_revision,'selector':selector,'max_references':1})
        verified=await self.read_provider_dossier(dossier_id,source_ref)
        if verified['revision']!=expected_revision or verified['resultIdentity']['operation']!='disassembly':raise ValueError('instruction dossier changed')
        rows=verified['result'].get('disassembly')
        if type(rows) is not list or sum(row==selector for row in rows)!=1:raise ValueError('instruction selection unavailable')
        return {'instruction':dict(selector),'dossierRevision':verified['revision'],'resultIdentity':verified['resultIdentity'],'sourceState':verified['sourceState']}

    async def static_functions_overlay(self, dossier_id, expected_revision, source_ref=None):
        from src.openclank.analysis_dossiers import static_function_overlay
        verified=await self.read_provider_dossier(dossier_id,source_ref)
        if type(expected_revision) is not dict or verified['revision']!=expected_revision:raise ValueError('dossier revision changed')
        return static_function_overlay(verified)

    async def read_function_export(self, export_id):
        import os
        from src.openclank.analysis_dossiers import verify_function_export
        if not isinstance(export_id,str) or not _ID.fullmatch(export_id):raise ValueError('invalid export')
        path,_=await self._existing_directory()
        ref,document=await self._read_text(os.path.join(path,'functions-'+export_id+'.md'),65536)
        verify_function_export(document,self.identity,export_id)
        return {'state':'available','revision':ref['revision'],'ref':ref['ref']}

    async def export_provider_functions(self, body):
        self._require_write()
        import os
        from src.openclank.analysis_dossiers import project_function,function_export,verify_function_export
        required={'workspaceID','dossierID','expectedDossierRevision','selectors','exportID'}
        if type(body) is not dict or not required.issubset(body) or set(body)-required-{'expectedExportRevision','sourceRef'} or body['workspaceID']!=self.identity['workspaceID']:
            raise ValueError('invalid export request')
        selectors=body['selectors'];export_id=body['exportID'];dossier_id=body['dossierID']
        if type(selectors) is not list or not 1<=len(selectors)<=16 or not isinstance(export_id,str) or not _ID.fullmatch(export_id):raise ValueError('invalid export request')
        if any(selectors[i]==selectors[j] for i in range(len(selectors)) for j in range(i)):raise ValueError('duplicate selector')
        if 'sourceRef' in body:_text(body['sourceRef'],16384)
        if 'expectedExportRevision' in body:
            revision=body['expectedExportRevision']
            if type(revision) is not dict or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint' or not isinstance(revision.get('value'),str) or len(revision['value'])>512 or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',revision['value']):raise ValueError('invalid revision')
        verified=await self.read_provider_dossier(dossier_id,body.get('sourceRef'))
        if verified['revision']!=body['expectedDossierRevision']:raise ValueError('dossier changed')
        projections=[project_function(verified,item) for item in selectors]
        document=function_export(self.identity,export_id,dossier_id,verified['revision'],projections)
        path,directory=await self._existing_directory();name='functions-'+export_id+'.md';ref=None
        if 'expectedExportRevision' in body:
            ref,old=await self._read_text(os.path.join(path,name),65536);verify_function_export(old,self.identity,export_id)
            if ref['revision']!=body['expectedExportRevision']:raise ValueError('export changed')
        async def before_dispatch():
            await self._check();latest_path,latest_directory=await self._existing_directory()
            if latest_path!=path or latest_directory.get('id')!=directory.get('id'):raise ValueError('destination changed')
            latest=await self.read_provider_dossier(dossier_id,body.get('sourceRef'))
            if latest['revision']!=verified['revision'] or [project_function(latest,item) for item in selectors]!=projections:raise ValueError('dossier or source changed')
        if ref is None:
            result=await self.facade.create(self.context,parent_ref=directory['ref'],name=name,text=document,before_dispatch=before_dispatch)
        else:
            result=await self.facade.save_resource(self.context,resource_ref=ref['ref'],expected_revision=body['expectedExportRevision'],text=document,before_dispatch=before_dispatch)
        return {'receipt':result,'sourceStateAtExport':verified['sourceState'],'resultIdentity':verified['resultIdentity'],'functionCount':len(projections)}

    async def save_host_elf_dossier(self, body):
        """Capture and retain one Human-selected ELF declaration through Files."""
        self._require_write()
        import os
        from src.openclank.analysis_adapters.base import capture_source, source_current
        from src.openclank.analysis_elf_metadata import inspect_captured_elf
        from src.secret_storage import encrypt
        required={'workspaceID','sourceRef','sourceRevision','dossierID'}
        if type(body) is not dict or not required.issubset(body) or set(body)-required-{'expectedRevision'} or body['workspaceID']!=self.identity['workspaceID']:
            raise ValueError('invalid ELF dossier request')
        dossier_id=body['dossierID']
        if not isinstance(dossier_id,str) or not _ID.fullmatch(dossier_id): raise ValueError('invalid dossier')
        await self._check()
        _,source=self.facade._provider_for_ref(self.context,body['sourceRef'],capability='download')
        if source.provider!='host': raise ValueError('source unavailable')
        try:
            captured=await capture_source(self.facade,self.context,body['sourceRef'],body['sourceRevision'])
            data=await inspect_captured_elf(captured)
        except (ValueError,UnicodeError) as exc:
            raise AnalysisPreDispatchRefusal('ELF inspection was refused before dispatch') from exc
        facts={'version':1,'kind':'host_elf_metadata','account':self.identity['ownerSubjectID'],
               'workspace':self.identity['workspaceID'],'dossier':dossier_id,'sourceID':source.stable_id,
               'sourceSHA256':captured.sha256,'sourceRevision':captured.revision,'resultSHA256':hashlib.sha256(data).hexdigest()}
        sealed=encrypt(_json(facts))
        if not sealed.startswith('enc:'): raise ValueError('ELF receipt unavailable')
        document,_,_=host_elf_document(self.identity,dossier_id,'ma-elf1.'+sealed[4:],data)
        path,directory=await self._existing_directory();name='provider-'+dossier_id+'.md';ref=None
        if 'expectedRevision' in body:
            ref,old=await self._read_text(os.path.join(path,name),_DOSSIER_LIMIT)
            parse_host_elf_document(old,self.identity,dossier_id)
            if ref.get('revision')!=body['expectedRevision']: raise ValueError('dossier changed')
        async def before_dispatch():
            await self._check()
            latest_path,latest_directory=await self._existing_directory()
            if latest_path!=path or latest_directory.get('id')!=directory.get('id') or not await source_current(self.facade,self.context,body['sourceRef'],captured.revision):
                raise ValueError('source or destination changed')
        if ref is None:
            receipt=await self.facade.create(self.context,parent_ref=directory['ref'],name=name,text=document,before_dispatch=before_dispatch)
        else:
            receipt=await self.facade.save_resource(self.context,resource_ref=ref['ref'],expected_revision=body['expectedRevision'],text=document,before_dispatch=before_dispatch)
        try:
            await self._check();current=await source_current(self.facade,self.context,body['sourceRef'],captured.revision)
        except Exception: current=False
        return {'receipt':receipt,'sourceCurrent':bool(current)}

    async def save_provider_dossier(self, body, store):
        self._require_write()
        import os
        from src.openclank.analysis_result_receipts import open_receipt, digest
        from src.openclank.analysis_adapters.base import capture_source, source_current
        from src.openclank.artifacts import account_artifact_owner
        required={'workspaceID','sourceRef','completedReceipt','dossierID'}
        if type(body) is not dict or not required.issubset(body) or set(body)-required-{'expectedRevision'}: raise ValueError('invalid dossier request')
        if body['workspaceID']!=self.identity['workspaceID']: raise ValueError('workspace changed')
        _text(body['sourceRef'],16384);_text(body['completedReceipt'],16384)
        dossier_id=body['dossierID']
        if not isinstance(dossier_id,str) or not _ID.fullmatch(dossier_id): raise ValueError('invalid dossier')
        if 'expectedRevision' in body:
            revision=body['expectedRevision']
            if type(revision) is not dict or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint' or not isinstance(revision.get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',revision['value']) or len(revision['value'])>512: raise ValueError('invalid revision')
        await self._check()
        facts=open_receipt(body['completedReceipt'],kind='completed')
        if facts['account']!=self.identity['ownerSubjectID'] or facts['workspace']!=self.identity['workspaceID']: raise ValueError('account changed')
        _,source=self.facade._provider_for_ref(self.context,body['sourceRef'],capability='download')
        if source.provider!='host' or source.stable_id!=facts['sourceID']: raise ValueError('source changed')
        captured=await capture_source(self.facade,self.context,body['sourceRef'],facts['sourceRevision'])
        if captured.sha256!=facts['sourceSHA']: raise ValueError('source changed')
        def retained():
            owner=account_artifact_owner(facts['account']);row=store.get(owner=owner,artifact_id=facts['artifactID'])
            if row.state!='acknowledged' or row.media_type!='application/json' or row.content_sha256!=facts['outputSHA'] or row.size_bytes!=facts['size']: raise ValueError('artifact unavailable')
            data=b''.join(store.read_chunks(owner=owner,artifact_id=row.id))
            if len(data)!=facts['size'] or digest(data)!=facts['outputSHA']: raise ValueError('artifact changed')
            return data
        data=await asyncio.to_thread(retained);document=provider_document(self.identity,dossier_id,body['completedReceipt'],data)
        path,directory=await self._existing_directory();name='provider-'+dossier_id+'.md';ref=None
        if 'expectedRevision' in body:
            ref,old=await self._read_text(os.path.join(path,name),_DOSSIER_LIMIT)
            parse_provider_document(old,self.identity,dossier_id)
            if ref.get('revision')!=body['expectedRevision']: raise ValueError('dossier changed')
        async def before_dispatch():
            await self._check()
            latest_path,latest_directory=await self._existing_directory()
            if latest_path!=path or latest_directory.get('id')!=directory.get('id') or open_receipt(body['completedReceipt'],kind='completed')!=facts: raise ValueError('dossier authority changed')
            if not await source_current(self.facade,self.context,body['sourceRef'],facts['sourceRevision']): raise ValueError('source changed')
            if await asyncio.to_thread(retained)!=data: raise ValueError('artifact changed')
        if ref is None:
            result=await self.facade.create(self.context,parent_ref=directory['ref'],name=name,text=document,before_dispatch=before_dispatch)
        else:
            result=await self.facade.save_resource(self.context,resource_ref=ref['ref'],expected_revision=body['expectedRevision'],text=document,before_dispatch=before_dispatch)
        try:
            await self._check();current=await source_current(self.facade,self.context,body['sourceRef'],facts['sourceRevision'])
        except Exception:current=False
        return {'receipt':result,'sourceCurrent':bool(current)}

    async def initialize(self, operation_id: str) -> dict[str, Any]:
        self._require_write()
        import os
        from src.openclank.files_facade import FilesFacadeError
        from src.openclank.files_host_provider import _origin
        from src.openclank.resource_refs import stable_resource_id
        steps = initialization_steps(self.identity, operation_id)
        path = self.binding.path
        parent, _ = await self._resource(path, kind='folder')
        receipts = []
        for component, step_id in steps:
            await self._check()
            target = os.path.join(path, component)
            stored = self.facade._operation_store.get_operation(owner_subject_id=self.context.owner_subject_id, operation_id=step_id)
            if stored is not None:
                receipt = stored.get('receipt') or {}
                if (stored.get('generation') != self.context.policy_generation or receipt.get('_request_kind') != 'create-directory'
                        or receipt.get('_workspace_id') != self.context.workspace_id):
                    raise ValueError('initialization receipt mismatch')
                if receipt.get('state') != 'complete':
                    return {'state': 'pending', 'operationID': step_id, 'completed': receipts}
                items = receipt.get('items') or []
                expected_key = stable_resource_id(owner_subject_id=self.context.owner_subject_id, provider='host', origin_id=_origin(target))
                if (len(items) != 1 or items[0].get('item_id') != step_id or items[0].get('outcome') != 'committed'
                        or items[0].get('resource_key') != expected_key):
                    raise ValueError('initialization receipt mismatch')
                parent, _ = await self._resource(target, kind='folder')
                receipts.append({'operationID': step_id, 'state': 'complete'})
            else:
                result = await self.facade.create_directory(self.context, parent_ref=parent['ref'], name=component,
                    operation_id=step_id, generation=self.context.policy_generation, collision='reuse')
                parent = result.get('resource') or {}
                current, _ = await self._resource(target, kind='folder')
                if parent.get('id') != current.get('id'):
                    raise ValueError('created folder changed')
                parent = current
                # The existing directory operation receipt is not a native
                # History capture receipt; do not project it as one.
                receipts.append({'operationID': step_id, 'state': 'complete'})
            path = target
        expected = manifest(self.identity)
        manifest_path = os.path.join(path, '00-index.md')
        await self._check()
        try:
            result = await self.facade.create(self.context, parent_ref=parent['ref'], name='00-index.md', text=expected)
        except FilesFacadeError as exc:
            if exc.code != 'resource_changed':
                raise
            _, existing = await self._read_text(manifest_path, 4096)
            verify_manifest(existing, self.identity)
            return {'state': 'ready', 'directory': parent, 'steps': receipts, 'manifest': {'state': 'existing'}}
        if result.get('outcome') != 'created':
            raise ValueError('manifest receipt unavailable')
        if result.get('readback') != 'current':
            return {'state': 'partial', 'steps': receipts, 'manifest': result}
        verify_manifest((await self._read_text(manifest_path, 4096))[1], self.identity)
        return {'state': 'ready', 'directory': parent, 'steps': receipts, 'manifest': result}

    # Neutral ingest custody (C5-P2): Markdown `ingest-<id>.md` beside the dossiers; normalized evidence only.
    @staticmethod
    def _ingest_render(header, normalized):
        encoded = _json(normalized);_text(encoded, _INGEST_LIMIT)
        head = _json(header);_text(head, 20 * 1024)
        document = '```json\n' + head + _FENCE + '\n```json\n' + encoded + _FENCE
        return _text(document, _INGEST_LIMIT)

    @classmethod
    def _ingest_document(cls, identity, ingest_id, adapter, source_id, source_revision, raw, normalized):
        encoded = _json(normalized).encode('utf-8')
        header = {'version': 1, 'evidenceKind': 'analysis_ingest', **identity, 'ingestID': ingest_id, 'adapter': adapter,
                  'sourceID': source_id, 'sourceRevision': dict(source_revision), 'rawSHA256': hashlib.sha256(raw).hexdigest(),
                  'rawSize': len(raw), 'normalizedSHA256': hashlib.sha256(encoded).hexdigest(), 'normalizedSize': len(encoded)}
        return cls._ingest_render(header, normalized)

    @staticmethod
    def _ingest_id(normalized):
        body = {k: v for k, v in normalized.items() if k != 'sourceState'}
        return hashlib.sha256(_json(['openclank.analysis-ingest.id.v1', body]).encode('utf-8')).hexdigest()[:32]

    @classmethod
    def _parse_ingest(cls, document, identity, ingest_id, *, _test_adapter=None):
        from src.openclank.analysis_ingest import normalize
        _text(document, _INGEST_LIMIT)
        prefix = '```json\n';separator = _FENCE + '\n```json\n'
        if not document.startswith(prefix) or not document.endswith(_FENCE) or not isinstance(ingest_id, str) or not _ID.fullmatch(ingest_id): raise ValueError('invalid ingest')
        pos = document.find(separator, len(prefix))
        if pos < 0: raise ValueError('invalid ingest')
        header = _decode(document[len(prefix):pos]);normalized = _decode(document[pos + len(separator):-len(_FENCE)])
        keys = {'version', 'evidenceKind', 'ownerSubjectID', 'workspaceID', 'projectKey', 'ingestID', 'adapter', 'sourceID', 'sourceRevision', 'rawSHA256', 'rawSize', 'normalizedSHA256', 'normalizedSize'}
        if type(header) is not dict or set(header) != keys or type(normalized) is not dict or normalized.get('sourceState') not in {'current', 'historical'}: raise ValueError('invalid ingest')
        if header['ingestID'] != ingest_id or cls._ingest_id(normalized) != ingest_id: raise ValueError('invalid ingest')
        # Re-run the C5-P1 validator over the stored rows; the stored form must be exactly its own output.
        try:
            lines = [{'schema': 'openclank.analysis-ingest.v1', **{k: normalized[k] for k in ('kind', 'provider', 'source', 'generation', 'producedAt')}}, *normalized['rows']]
            again = normalize(('\n'.join(_json(x) for x in lines) + '\n').encode('utf-8'), header['adapter'], expected_source_sha256=normalized['source']['sha256'], _test_adapter=_test_adapter)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('invalid ingest') from exc
        again['sourceState'] = normalized['sourceState']
        if (type(header['sourceRevision']) is not dict or set(header['sourceRevision']) != {'kind', 'value'} or header['sourceRevision']['kind'] != 'hostFingerprint'
                or not isinstance(header['sourceRevision']['value'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+', header['sourceRevision']['value'])
                or not isinstance(header['sourceID'], str) or not isinstance(header['rawSHA256'], str) or not re.fullmatch(r'[0-9a-f]{64}', header['rawSHA256'])
                or type(header['rawSize']) is not int or header['version'] != 1 or header['evidenceKind'] != 'analysis_ingest'): raise ValueError('invalid ingest')
        encoded = _json(normalized).encode('utf-8')
        if (again != normalized or any(header[k] != identity[k] for k in identity) or header['normalizedSHA256'] != hashlib.sha256(encoded).hexdigest()
                or header['normalizedSize'] != len(encoded) or document != cls._ingest_render(header, normalized)): raise ValueError('invalid ingest')
        return header, normalized

    async def save_ingest(self, body, *, _test_adapter=None):
        self._require_write()
        import os
        from src.openclank.analysis_adapters.base import capture_source, source_current
        from src.openclank.analysis_ingest import normalize
        required = {'workspaceID', 'sourceRef', 'sourceRevision', 'adapterID', 'data'}
        if type(body) is not dict or not required.issubset(body) or set(body) - required - {'expectedRevision'}: raise ValueError('invalid ingest request')
        if body['workspaceID'] != self.identity['workspaceID']: raise ValueError('workspace changed')
        _text(body['sourceRef'], 16384);_text(body['adapterID'], 64)
        if type(body['data']) is not bytes: raise ValueError('invalid ingest')
        from src.openclank.analysis_adapters.ingest import qualified_adapter_ids
        if _test_adapter is None and body['adapterID'] not in qualified_adapter_ids(): raise ValueError('invalid ingest')   # no producer: refused before the source is read
        for key in ('sourceRevision', 'expectedRevision'):
            if key not in body: continue
            revision = body[key]
            if type(revision) is not dict or set(revision) != {'kind', 'value'} or revision.get('kind') != 'hostFingerprint' or not isinstance(revision.get('value'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+', revision['value']) or len(revision['value']) > 512: raise ValueError('invalid revision')
        await self._check()
        _, source = self.facade._provider_for_ref(self.context, body['sourceRef'], capability='download')
        if source.provider != 'host': raise ValueError('source changed')
        captured = await capture_source(self.facade, self.context, body['sourceRef'], body['sourceRevision'])
        try: normalized = normalize(body['data'], body['adapterID'], expected_source_sha256=captured.sha256, _test_adapter=_test_adapter)
        except ValueError as exc: raise ValueError('invalid ingest') from exc
        ingest_id = self._ingest_id(normalized)
        document = self._ingest_document(self.identity, ingest_id, normalized['adapter'], source.stable_id, captured.revision, body['data'], normalized)
        path, directory = await self._existing_directory();name = 'ingest-' + ingest_id + '.md';ref = None
        if 'expectedRevision' in body:
            ref, old = await self._read_text(os.path.join(path, name), _INGEST_LIMIT)
            self._parse_ingest(old, self.identity, ingest_id, _test_adapter=_test_adapter)
            if ref.get('revision') != body['expectedRevision']: raise ValueError('ingest changed')
        async def before_dispatch():
            await self._check()
            latest_path, latest_directory = await self._existing_directory()
            if latest_path != path or latest_directory.get('id') != directory.get('id'): raise ValueError('ingest authority changed')
            if not await source_current(self.facade, self.context, body['sourceRef'], body['sourceRevision']): raise ValueError('source changed')
        if ref is None:
            result = await self.facade.create(self.context, parent_ref=directory['ref'], name=name, text=document, before_dispatch=before_dispatch)
        else:
            result = await self.facade.save_resource(self.context, resource_ref=ref['ref'], expected_revision=body['expectedRevision'], text=document, before_dispatch=before_dispatch)
        try:
            await self._check();current = await source_current(self.facade, self.context, body['sourceRef'], body['sourceRevision'])
        except Exception: current = False
        return {'receipt': result, 'ingestID': ingest_id, 'sourceCurrent': bool(current)}

    async def read_ingest(self, ingest_id, source_ref=None, *, _test_adapter=None):
        import os
        from src.openclank.analysis_adapters.base import capture_source
        if not isinstance(ingest_id, str) or not _ID.fullmatch(ingest_id): raise ValueError('invalid ingest')
        path, _ = await self._existing_directory()
        ref, document = await self._read_text(os.path.join(path, 'ingest-' + ingest_id + '.md'), _INGEST_LIMIT)
        header, normalized = self._parse_ingest(document, self.identity, ingest_id, _test_adapter=_test_adapter)
        source_state = 'unchecked'
        if source_ref is not None:
            _text(source_ref, 16384)
            provider, source = self.facade._provider_for_ref(self.context, source_ref, capability='download')
            if source.provider != 'host' or source.stable_id != header['sourceID']: raise ValueError('source unavailable')
            entry = await provider.stat(self.context, origin_id=source.origin_id)
            revision = self.facade._provider_revision(entry)
            captured = await capture_source(self.facade, self.context, source_ref, revision)
            source_state = 'current' if captured.sha256 == normalized['source']['sha256'] and revision == header['sourceRevision'] else 'historical'
        await self._check()
        return {'state': 'verified', 'sourceState': source_state, 'result': {**normalized, 'sourceState': source_state},
                'revision': ref['revision'], 'ref': ref['ref'], 'ingestID': ingest_id,
                'resultIdentity': {'sourceID': header['sourceID'], 'sourceSHA256': normalized['source']['sha256'], 'sourceRevision': header['sourceRevision'],
                    'rawSHA256': header['rawSHA256'], 'normalizedSHA256': header['normalizedSHA256'], 'adapter': header['adapter']}}
