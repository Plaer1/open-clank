"""Revision reads over one record for the memory panel: field diff between two revisions and per-field blame.

`GET /api/memory/{id}/diff?from=<rev>&to=<rev>` -> `{block_id, from_revision, to_revision, changes}`
`GET /api/memory/{id}/blame`                    -> `{block_id, fields}`

Both go through provider.versioned_diff / versioned_blame, which resolve `{id}` with the history route's strict scope
(authenticated owner + server workspace) before any reader runs: a foreign id, an unknown id and the right id in the
wrong workspace are all the same 404. Revisions must be integers 1..MAX_REVISION; a revision that does not exist (or is
hidden) on an owned record is 422 `invalid_revision`. Bounds: each changed field's before/after is replaced by
`{"truncated": true, "bytes": n}` above FIELD_BYTES; blame returns at most one small entry per payload field (no values).

`POST /api/memory/{id}/supersede` and `POST /api/memory/{id}/detach` are thin validated fronts for the EXISTING reviewed
mirror edit (preview -> human approval -> apply). The relation is `activation_rationale.supersedes = {block_id, revision}`
on the superseding record only (selected path `/activation_rationale/supersedes`); the target is pinned to its current
revision at preview and re-checked at apply. Bodies: `{phase:"preview", target_id?, candidate_id, origin_request_key,
prior_receipt_id, expected_revision, request_key, reason}` (the first four come from GET /{id}/mirror-context) and
`{phase:"apply", target_id?, ...same, operation_json, operation_sha256}`. `target_id` is supersede-only. Native stays the
authority at apply. Reverse lookup ("what supersedes X") is not built.
"""
import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from src.openclank.memory_relation_review import KINDS as REVIEW_KINDS, classify_pair, read_declines, retain_decline

logger = logging.getLogger(__name__)
_HEADERS = {'Cache-Control': 'private, no-store'}
MAX_REVISION = 1_000_000
PATH = '/activation_rationale/supersedes'
CHAIN_BOUND = 16
STALE = 'The relation could not be saved: the memory or its target changed. Preview again.'
DIGEST = re.compile(r'[0-9a-f]{64}')
FIELD_BYTES = 16384


def _bound(value):
    size = len(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))
    return value if size <= FIELD_BYTES else {'truncated': True, 'bytes': size}


def register_native_ops_supersede(router, *, require_human, capture_context, owned, provider, mirror_request_data=None):
    def refuse(status, state):
        return JSONResponse({'state': state}, status_code=status, headers=_HEADERS)

    def legacy_context(user):
        from src.memory_scope import memory_owner
        @asynccontextmanager
        async def admitted(_user):
            yield
        return {'owner': memory_owner(user), 'barrier': SimpleNamespace(admitted=admitted), 'current_live': lambda: True}

    def valid_id(memory_id):
        return bool(memory_id) and len(memory_id) <= 512 and memory_id == memory_id.strip()

    def revision(text):
        return int(text) if text is not None and text.isascii() and text.isdigit() and 1 <= int(text) <= MAX_REVISION else None

    async def read(request, memory_id, method, kwargs):
        try:
            user = require_human(request)
            context = capture_context(request, user) if getattr(provider, '_guarded_v17', False) is True else legacy_context(user)
        except Exception:
            return refuse(403, 'unavailable')
        try:
            reader = getattr(provider, method, None)
            if not callable(reader):
                return refuse(503, 'unavailable')
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']): raise ValueError('authority changed')
                found = await reader(memory_id, owner=context['owner'], **kwargs)
                if not await owned(context['current_live']): raise ValueError('authority changed')
            if not isinstance(found, dict):
                return refuse(503, 'unavailable')
            return found
        except Exception as exc:
            from src.memery_v2 import V2OperationError
            from src.memory_versioned import VersionedMemoryNotFound
            if isinstance(exc, VersionedMemoryNotFound):
                return refuse(404, 'not_found')
            if isinstance(exc, V2OperationError) and getattr(exc, 'code', None) == 'not_found':
                return refuse(422, 'invalid_revision')  # the record is already proven owned here
            logger.warning('Memory %s failed: %s: %s', method, type(exc).__name__, str(exc)[:120])
            return refuse(503, 'unavailable')

    async def diff(request: Request, memory_id: str):
        query = request.query_params
        if set(query.keys()) != {'from', 'to'} or any(len(query.getlist(k)) != 1 for k in query.keys()) or not valid_id(memory_id):
            return refuse(422, 'invalid_revision')
        start, end = revision(query['from']), revision(query['to'])
        if start is None or end is None:
            return refuse(422, 'invalid_revision')
        found = await read(request, memory_id, 'versioned_diff', {'from_revision': start, 'to_revision': end})
        if isinstance(found, JSONResponse):
            return found
        changes = found.get('changes')
        if not isinstance(changes, dict):
            return refuse(503, 'unavailable')
        out = {k: {'before': _bound(v.get('before')), 'after': _bound(v.get('after'))} for k, v in changes.items()}
        return JSONResponse({'block_id': found.get('block_id'), 'from_revision': start, 'to_revision': end, 'changes': out}, headers=_HEADERS)

    async def blame(request: Request, memory_id: str):
        if list(request.query_params.keys()) or not valid_id(memory_id):
            return refuse(422, 'invalid_revision')
        found = await read(request, memory_id, 'versioned_blame', {})
        if isinstance(found, JSONResponse):
            return found
        if not isinstance(found.get('fields'), dict):
            return refuse(503, 'unavailable')
        return JSONResponse({'block_id': found.get('block_id'), 'fields': found['fields']}, headers=_HEADERS)

    router.add_api_route('/{memory_id}/diff', diff, methods=['GET'])
    router.add_api_route('/{memory_id}/blame', blame, methods=['GET'])

    async def pair_evidence(context, memory_id, target_id, kind):
        from src.memory_versioned import VersionedMemoryNotFound, _text_value, read_source_reference
        owner, workspace, check = context['owner'], context['workspace'], context['current_live']
        path = getattr(provider, '_fm_db_path', None)
        detail = getattr(provider, 'versioned_detail', None)
        relation = getattr(provider, 'versioned_relation', None)
        if not callable(detail) or not callable(relation) or not isinstance(path, str) or not path:
            raise ValueError('review evidence unavailable')

        async def side(identifier):
            record = await detail(identifier, owner=owner)
            if (not isinstance(record, dict) or record.get('owner') != owner or record.get('workspace_id') != workspace
                    or record.get('project_id') is not None or not isinstance(record.get('current'), dict)):
                raise VersionedMemoryNotFound('review evidence unavailable')
            current = record['current']
            revision = record.get('current_revision')
            claim = _text_value(current.get('value') or {})
            if type(revision) is not int or revision < 1 or current.get('revision') != revision or not isinstance(claim, str):
                raise ValueError('review evidence unavailable')
            allowed = set(current.get('evidence_ids') or [])
            references = []
            for item in record.get('evidence', ()):
                if len(references) >= 4:
                    break
                if not isinstance(item, dict) or item.get('id') not in allowed:
                    continue
                item_id = item['id']; source_revision = item.get('source_revision')
                if isinstance(item_id, str) and 0 < len(item_id) <= 256 and type(source_revision) is int and source_revision > 0:
                    references.append({'kind':'memory_evidence','evidence_id':item_id,'source_revision':source_revision})
            samples = []
            for reference in references:
                row = await owned(read_source_reference, db_path=path, owner=owner, workspace_id=workspace, reference=reference)
                if not await owned(check):
                    raise ValueError('review authority changed')
                if (not row or row.get('forget_state') != 'active' or not isinstance(row.get('source_hash'), str)
                        or not DIGEST.fullmatch(row['source_hash']) or row.get('excerpt_oversize')
                        or not isinstance(row.get('excerpt'), str) or row['source_revision'] != reference['source_revision']):
                    continue
                original = row['excerpt'].encode('utf-8')
                samples.append({**reference,'source_sha256':row['source_hash'],'excerpt_sha256':hashlib.sha256(original).hexdigest(),
                    'excerpt':original[:2048].decode('utf-8', errors='ignore'),'truncated':len(original)>2048})
            published = await relation(identifier, owner=owner)
            if published.get('revision') != revision or published.get('block_id') != record.get('block_id'):
                raise ValueError('review evidence changed')
            original = claim.encode('utf-8')
            validity = {key: current[key] for key in ('valid_from','valid_to')
                        if isinstance(current.get(key), str) and len(current[key]) <= 128}
            return {'block_id':record['block_id'],'revision':revision,'validity':validity,'claim_sha256':hashlib.sha256(original).hexdigest(),
                'claim':original[:2048].decode('utf-8', errors='ignore'),'claim_truncated':len(original)>2048,
                'evidence':samples,'coverage':'partial' if samples else 'source_unavailable'}

        left = await side(memory_id)
        right = await side(target_id)
        if left['block_id'] == right['block_id']:
            raise ValueError('review requires two records')
        material = {'version':1,'kind':kind,'left':left,'right':right,'assessment':'owner_review_required'}
        digest = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        if not await owned(check):
            raise ValueError('review authority changed')
        return {**material,'evidence_sha256':digest}

    def review_authority(request):
        user=require_human(request)
        if getattr(provider,'_guarded_v17',False) is not True:
            raise ValueError('review authority unavailable')
        context=capture_context(request,user)
        path=getattr(provider,'_fm_db_path',None)
        if not isinstance(path,str) or not path:
            raise ValueError('review storage unavailable')
        return user,context,path

    async def suggest_relations(request: Request, memory_id: str):
        from src.memory_versioned import VersionedMemoryNotFound, _text_value
        if list(request.query_params.keys()) or not valid_id(memory_id):
            return refuse(422,'invalid_request')
        try:
            user,context,path=review_authority(request)
        except Exception:
            return refuse(403,'unavailable')
        try:
            async with context['barrier'].admitted(user):
                check=context['current_live']; owner=context['owner']; workspace=context['workspace']
                def scoped(item):
                    return (type(item) is dict and item.get('owner')==owner and item.get('workspace_id')==workspace
                            and item.get('project_id') is None and isinstance(item.get('current'),dict))
                if not await owned(check):
                    return refuse(503,'unavailable')
                selected=await provider.versioned_detail(memory_id,owner=owner)
                if not scoped(selected):
                    return refuse(404,'not_found')
                claim=_text_value(selected['current'].get('value') or {}).strip()
                if not claim:
                    return JSONResponse({'version':1,'coverage':'bounded_native_candidates','items':[]},headers=_HEADERS)
                query=claim.encode('utf-8')[:2048].decode('utf-8',errors='ignore')
                _,refs=await provider._host_qualified_search(owner=owner,workspace_id=workspace,query=query,limit=12)
                pairs=[]; rows=[]; seen={selected['block_id']}
                for ref in refs:
                    if ref is None or ref.block_id in seen:
                        continue
                    if not await owned(check):
                        return refuse(503,'unavailable')
                    seen.add(ref.block_id)
                    try:
                        other=await provider.versioned_detail(ref.block_id,owner=owner)
                    except VersionedMemoryNotFound:
                        continue
                    if not scoped(other) or other['block_id']!=ref.block_id or other['current_revision']!=ref.revision:
                        continue
                    proposal=classify_pair(selected,other)
                    label=_text_value(other['current'].get('value') or {}).strip()
                    if proposal is None or not label:
                        continue
                    kind,basis=proposal
                    pairs.append((kind,{'block_id':selected['block_id'],'revision':selected['current_revision']},
                                       {'block_id':other['block_id'],'revision':other['current_revision']}))
                    rows.append({'target_id':other['block_id'],'revision':other['current_revision'],'kind':kind,
                                 'basis':basis,'label':label[:160]})
                    if len(rows)>=8:
                        break
                decisions=await owned(read_declines,path,owner,context['account'],workspace,pairs) if pairs else []
                live=await provider.versioned_relation(memory_id,owner=owner)
                if live.get('block_id')!=selected['block_id'] or live.get('revision')!=selected['current_revision'] or not await owned(check):
                    return refuse(409,'evidence_changed')
                output=[]
                for row,decision in zip(rows,decisions):
                    try:
                        refreshed=await provider.versioned_relation(row['target_id'],owner=owner)
                    except VersionedMemoryNotFound:
                        continue
                    if refreshed.get('block_id')==row['target_id'] and refreshed.get('revision')==row['revision']:
                        output.append({**row,**({'decision':decision} if decision else {})})
                if not await owned(check):
                    return refuse(503,'unavailable')
                return JSONResponse({'version':1,'coverage':'bounded_native_candidates','items':output},headers=_HEADERS)
        except VersionedMemoryNotFound:
            return refuse(404,'not_found')
        except Exception:
            return refuse(503,'unavailable')

    async def decline_relation(request: Request, memory_id: str):
        from routes.memory.preextracted_candidates import read_json_body
        from src.memory_versioned import VersionedMemoryNotFound
        from src.openclank.memory_policy_projection import policy_coordinator
        try:
            body=await read_json_body(request,4096)
        except Exception:
            return refuse(422,'invalid_request')
        if (type(body) is not dict or set(body)!={'target_id','kind','evidence_sha256'} or not valid_id(memory_id)
                or type(body['target_id']) is not str or not valid_id(body['target_id']) or body['target_id']==memory_id
                or type(body['kind']) is not str or body['kind'] not in REVIEW_KINDS
                or type(body['evidence_sha256']) is not str or not DIGEST.fullmatch(body['evidence_sha256'])):
            return refuse(422,'invalid_request')
        try:
            user,context,path=review_authority(request)
            coordinator=policy_coordinator()
            if coordinator is None or coordinator.projection is not getattr(request.app.state,'memory_policy_projection',None):
                raise ValueError('review policy unavailable')
        except Exception:
            return refuse(403,'unavailable')
        try:
            async with context['barrier'].admitted(user):
                check=context['current_write']; account=context['account']; workspace=context['workspace']
                if not await owned(check):
                    return refuse(403,'unavailable')
                refs=await owned(coordinator.checked_refs,user,account,workspace)
                def current():
                    return (check() is True and coordinator is policy_coordinator()
                            and coordinator.checked_refs(user,account,workspace)==refs and check() is True)
                evidence=await pair_evidence(context,memory_id,body['target_id'],body['kind'])
                if evidence['evidence_sha256']!=body['evidence_sha256']:
                    return refuse(409,'evidence_changed')
                decision=await owned(retain_decline,path,context['owner'],account,workspace,evidence,current)
                if not await owned(current):
                    return refuse(409,'evidence_changed')
                return JSONResponse(decision,headers=_HEADERS)
        except VersionedMemoryNotFound:
            return refuse(404,'not_found')
        except Exception:
            return refuse(409,'evidence_changed')

    async def compare_relation(request: Request, memory_id: str):
        from src.memory_versioned import VersionedMemoryNotFound
        query = request.query_params
        if set(query.keys()) != {'target_id','kind'} or any(len(query.getlist(k)) != 1 for k in query.keys()):
            return refuse(422, 'invalid_request')
        target_id,kind = query['target_id'],query['kind']
        if not valid_id(memory_id) or not valid_id(target_id) or memory_id == target_id or kind not in REVIEW_KINDS:
            return refuse(422, 'invalid_request')
        try:
            user = require_human(request)
            if getattr(provider, '_guarded_v17', False) is not True:
                return refuse(503, 'unavailable')
            context = capture_context(request, user)
        except Exception:
            return refuse(403, 'unavailable')
        try:
            async with context['barrier'].admitted(user):
                if not await owned(context['current_live']):
                    return refuse(503, 'unavailable')
                result = await pair_evidence(context, memory_id, target_id, kind)
                if not await owned(context['current_live']):
                    return refuse(503, 'unavailable')
                decisions=await owned(read_declines,provider._fm_db_path,context['owner'],context['account'],context['workspace'],
                                      [(kind,result['left'],result['right'])])
                if not await owned(context['current_live']):
                    return refuse(409,'evidence_changed')
                if decisions[0] is not None:
                    result['decision']=decisions[0]
                return JSONResponse(result, headers=_HEADERS)
        except VersionedMemoryNotFound:
            return refuse(404, 'not_found')
        except Exception:
            return refuse(409, 'evidence_unavailable')

    async def relation_front(request: Request, memory_id: str, detach: bool):
        """Shared body of supersede / detach. Refusals: 403 no human, 503 unavailable (legacy profile, provider without the
        reader), 404 not_found (own or target id foreign/unknown/wrong workspace: one body), 422 for validation, 409 for the
        stale/changed outcome."""
        try:
            user = require_human(request)
            if getattr(provider, '_guarded_v17', False) is not True:
                return refuse(503, 'unavailable')  # the reviewed update path does not exist on the legacy profile
            context = capture_context(request, user)
        except Exception:
            return refuse(403, 'unavailable')
        try:
            from routes.memory.preextracted_candidates import read_json_body  # bounded, duplicate-key-strict, as the batch routes
            body = await read_json_body(request, 128 * 1024)
        except Exception:
            return refuse(422, 'invalid_request')
        phases = {'preview': {'candidate_id', 'origin_request_key', 'prior_receipt_id', 'expected_revision', 'request_key', 'reason'}}
        phases['apply'] = phases['preview'] | {'operation_json', 'operation_sha256'}
        phase = body.get('phase') if isinstance(body, dict) else None
        if type(phase) is not str or phase not in phases or not valid_id(memory_id):
            return refuse(422, 'invalid_request')
        keys = phases[phase] | {'phase'} | (set() if detach else {'target_id'})
        target_id = body.get('target_id')
        comparison = body.get('comparison')
        if 'comparison' in body:
            if (detach or not isinstance(comparison,dict) or set(comparison) != {'kind','evidence_sha256'}
                    or type(comparison['kind']) is not str or comparison['kind'] not in REVIEW_KINDS
                    or type(comparison['evidence_sha256']) is not str or not DIGEST.fullmatch(comparison['evidence_sha256'])):
                return refuse(422, 'invalid_request')
            keys = keys | {'comparison'}
        if set(body) != keys or (not detach and (not isinstance(target_id, str) or not valid_id(target_id))):
            return refuse(422, 'invalid_request')
        review = {k: v for k, v in body.items() if k not in ('phase', 'target_id', 'comparison')}
        reader = getattr(provider, 'versioned_relation', None)
        if not callable(reader) or mirror_request_data is None:
            return refuse(503, 'unavailable')
        from src.memery_provider import _HostMirrorApply, _HostMirrorPreview
        from src.memory_versioned import VersionedMemoryNotFound
        # A relation write is a reviewed edit: apply needs the explicit-human-review predicate (current_write), preview is a read.
        check = context['current_write'] if phase == 'apply' else context['current_live']
        try:
            from src.openclank.memory_policy_projection import policy_coordinator
            coordinator = policy_coordinator()
            if phase == 'apply' and (coordinator is None or coordinator.projection is not getattr(request.app.state, 'memory_policy_projection', None)):
                raise ValueError('authority changed')
            async with context['barrier'].admitted(user):
                async def current():
                    if not await owned(check): raise ValueError('authority changed')
                    refs = await owned(coordinator.checked_refs, user, context['account'], context['workspace']) if phase == 'apply' else None
                    if (phase == 'apply' and coordinator is not policy_coordinator()) or not await owned(check): raise ValueError('authority changed')
                    return refs
                refs = await current()
                def before_issue():
                    try:
                        return (check() is True and coordinator is policy_coordinator()
                            and coordinator.checked_refs(user, context['account'], context['workspace']) == refs and check() is True)
                    except Exception: return False
                owner, workspace = context['owner'], provider._workspace_id
                try:
                    mine = await reader(memory_id, owner=owner)
                    target = None if detach else await reader(target_id, owner=owner)
                except VersionedMemoryNotFound:
                    return refuse(404, 'not_found')
                expected = review.get('expected_revision')
                replay = bool(comparison is not None and phase == 'apply' and type(expected) is int
                              and mine['revision'] == expected+1
                              and mine['relation'] == {'block_id':target['block_id'],'revision':target['revision']})
                if detach and mine['relation'] is None:
                    return refuse(422, 'no_relation')
                if not detach:
                    if mine['relation'] is not None and not replay:
                        return refuse(422, 'already_supersedes')  # one relation per record: detach first
                    if target['block_id'] == mine['block_id']:
                        return refuse(422, 'self')
                    successor = target
                    for _ in range(CHAIN_BOUND):
                        link = successor['relation']
                        if link is None:
                            break
                        if link['block_id'] == mine['block_id']:
                            return refuse(422, 'cycle')
                        try:
                            successor = await reader(link['block_id'], owner=owner)
                        except VersionedMemoryNotFound:
                            break  # an unresolvable target ends the chain
                    else:
                        return refuse(422, 'chain_too_long')
                if comparison is not None:
                    if not replay:
                        evidence = await pair_evidence(context, memory_id, target_id, comparison['kind'])
                        if (evidence['evidence_sha256'] != comparison['evidence_sha256']
                                or evidence['left']['revision'] != mine['revision'] or evidence['right']['revision'] != target['revision']):
                            return refuse(409, 'evidence_changed')
                    reason = review.get('reason')
                    if type(reason) is not str or not reason.strip() or len(reason.encode('utf-8'))>350:
                        return refuse(422, 'invalid_request')
                    review['reason'] = f"memory_evidence:{comparison['kind']}:{comparison['evidence_sha256']} {reason}"
                relation = None if detach else {'block_id': target['block_id'], 'revision': target['revision']}
                selected = [PATH]
                if phase == 'preview':
                    checked = mirror_request_data({**review, 'selected_paths': selected, 'payload': {'activation_rationale': {'supersedes': relation}}}, apply=False)
                    preview = await provider._host_mirror_preview(_HostMirrorPreview(owner, workspace, memory_id, checked['candidate_id'],
                        checked['origin_request_key'], checked['prior_receipt_id'], checked['expected_revision'], checked['request_key'],
                        checked['reason'], tuple(selected), checked['payload']))
                    result = {**preview, 'target_revision': None if detach else target['revision']}
                else:
                    checked = mirror_request_data({**review, 'selected_paths': selected}, apply=True)
                    apply = _HostMirrorApply(owner, workspace, memory_id, checked['candidate_id'], checked['origin_request_key'],
                        checked['prior_receipt_id'], checked['expected_revision'], checked['request_key'], checked['reason'],
                        tuple(selected), checked['operation_json'], checked['operation_sha256'])
                    operation = provider._validate_host_mirror_operation(apply)
                    payload = operation['payload']
                    sealed = payload.get('activation_rationale') if set(payload) == {'activation_rationale'} else None
                    if not isinstance(sealed, dict) or operation['approved_paths'] != selected:
                        return refuse(422, 'invalid_request')
                    if detach != ('supersedes' not in sealed) or (not detach and sealed['supersedes'] != relation):
                        return refuse(409, 'changed')  # the target moved on since preview, or the sealed operation is not this relation
                    review_result = await provider._host_mirror_apply(apply, before_issue=before_issue)
                    result = {'ok': review_result.get('state') == 'applied', 'review': review_result}
                if await current() != refs: raise ValueError('authority changed')
            return JSONResponse(result, status_code=409 if phase == 'apply' and result.get('ok') is not True else 200, headers=_HEADERS)
        except HTTPException as exc:
            return refuse(422, 'invalid_request') if exc.status_code == 400 else refuse(503, 'unavailable')
        except Exception as exc:
            if str(exc) == 'authority changed':
                return refuse(503, 'unavailable')
            logger.warning('Memory relation %s failed: %s: %s', phase, type(exc).__name__, str(exc)[:120])
            return JSONResponse({'state': 'refused', 'detail': STALE}, status_code=409, headers=_HEADERS)

    async def supersede(request: Request, memory_id: str):
        return await relation_front(request, memory_id, False)

    async def detach(request: Request, memory_id: str):
        return await relation_front(request, memory_id, True)

    router.add_api_route('/{memory_id}/relation-review/suggestions', suggest_relations, methods=['GET'])
    router.add_api_route('/{memory_id}/relation-review', compare_relation, methods=['GET'])
    router.add_api_route('/{memory_id}/relation-review/decline', decline_relation, methods=['POST'])
    router.add_api_route('/{memory_id}/supersede', supersede, methods=['POST'])
    router.add_api_route('/{memory_id}/detach', detach, methods=['POST'])
