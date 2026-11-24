"""Canonical approved-analysis snapshot data; never an access grant."""
from __future__ import annotations
import hashlib
import base64
import binascii
import json
import re
from typing import Any

from src.openclank.analysis_profiles import profile_for

SCHEMA_VERSION = 'openclank.analysis-snapshot.v1'
HOST_SCHEMA_VERSION = 'openclank.analysis-snapshot.v2'
MEDIA_TYPE = 'application/vnd.openclank.analysis-snapshot+json'
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_COMPONENTS = 1024
MAX_DEPTH = 32
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
DEFAULT_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 30
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_COVERAGE = {'complete','selected','partial','unknown'}
_REASONS = {'denied','invalid_name','symlink','special','overflow','changed','unavailable','filtered'}
_REFERENCE = {'version','upload_id','manifest_sha256','size_bytes','media_type','schema_version','capture_scope_sha256','coverage','component_count'}


def _text(value, maximum=256, *, empty=False):
    if (not isinstance(value,str) or (not value and not empty) or len(value.encode('utf-8','strict'))>maximum
        or any(ord(c)<32 or 127<=ord(c)<=159 for c in value)):
        raise ValueError('invalid snapshot text')
    return value


def _hash(value):
    if not isinstance(value,str) or not _HASH.fullmatch(value):raise ValueError('invalid snapshot digest')
    return value


def _opaque(value):
    _text(value,512)
    if value.startswith(('/', '\\', 'file:')) or re.match(r'^[A-Za-z]:[\\/]',value):
        raise ValueError('path is not an opaque identity')
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum<=value<=maximum:raise ValueError('invalid snapshot integer')
    return value


def _path(value):
    if type(value) is not list or len(value)>MAX_DEPTH:raise ValueError('invalid snapshot path')
    for part in value:
        _text(part,255)
        if part in {'.','..'} or '/' in part or '\\' in part:raise ValueError('invalid snapshot component')
    return tuple(value)


def _revision(value):
    if (type(value) is not dict or set(value)!={'kind','value'} or value['kind']!='hostFingerprint'
        or not isinstance(value['value'],str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',value['value'])
        or len(value['value'])>256):raise ValueError('invalid snapshot revision')
    return value


def canonical_json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8','strict')


def scope_digest(scope):
    # Admission generation is historical provenance, not tree structure.
    stable={key:scope[key] for key in ('mode','root_resource_id','selections','filters')}
    return hashlib.sha256(b'openclank.analysis-snapshot.scope.v1\0'+canonical_json(stable)).hexdigest()


def directory_digest(*, scope_sha256, coverage, omitted_count, reasons, children):
    """Hash observed children; omitted_count is an observed lower bound."""
    _hash(scope_sha256)
    if coverage not in _COVERAGE:raise ValueError('invalid directory coverage')
    _integer(omitted_count,0,MAX_COMPONENTS)
    if type(reasons) is not list or reasons!=sorted(set(reasons)) or any(v not in _REASONS for v in reasons):raise ValueError('invalid directory reasons')
    if coverage=='complete' and (omitted_count or reasons):raise ValueError('incomplete directory')
    rows=[]
    for child in children:
        if type(child) is not dict or set(child)!={'name','kind','coverage','size_bytes','sha256'}:raise ValueError('invalid child')
        name=_path([child['name']])[0]
        if child['kind'] not in {'file','directory'} or (child['coverage'] is not None and child['coverage'] not in _COVERAGE):raise ValueError('invalid child kind')
        if child['kind']=='file':
            _integer(child['size_bytes'],0,MAX_FILE_BYTES)
            if child['coverage'] is not None:raise ValueError('invalid file coverage')
        elif child['size_bytes'] is not None or child['coverage'] is None:raise ValueError('invalid directory size')
        _hash(child['sha256']);rows.append((name.encode('utf-8'),child))
    rows.sort(key=lambda item:item[0])
    if len({name for name,_ in rows})!=len(rows):raise ValueError('duplicate child')
    # Each value is length-prefixed UTF-8; scope and coverage are part of the hash.
    h=hashlib.sha256(b'openclank.analysis-snapshot.directory.v1\0')
    for part in [scope_sha256,coverage,str(len(rows)),str(omitted_count),*reasons]:
        b=part.encode('utf-8');h.update(len(b).to_bytes(8,'big'));h.update(b)
    h.update(b'\xff')
    for _,child in rows:
        for part in (child['name'],child['kind'],child['coverage'] or '',str(child['size_bytes']) if child['size_bytes'] is not None else '',child['sha256']):
            b=part.encode('utf-8');h.update(len(b).to_bytes(8,'big'));h.update(b)
    return h.hexdigest()


def validate_manifest(value):
    if type(value) is not dict or set(value)!={'schema_version','account_id','workspace_id','project_key','root_identity','capture_interval','scope','limits','dossier','components'} or value['schema_version'] not in (SCHEMA_VERSION,HOST_SCHEMA_VERSION):raise ValueError('invalid snapshot manifest')
    for key in ('account_id','workspace_id'):_text(value[key],512)
    _opaque(value['root_identity'])
    from src.openclank.analysis_projects import project_identity
    if value['project_key']!=project_identity(value['account_id'],value['workspace_id'])['projectKey']:raise ValueError('invalid project identity')
    interval=value['capture_interval']
    if type(interval) is not dict or set(interval)!={'started_ms','finished_ms'}:raise ValueError('invalid capture interval')
    _integer(interval['started_ms'],0,2**53-1);_integer(interval['finished_ms'],interval['started_ms'],2**53-1)
    scope=value['scope']
    if type(scope) is not dict or set(scope)!={'mode','root_resource_id','selections','filters','policy_generation'} or scope['mode'] not in {'folder','selected'}:raise ValueError('invalid capture scope')
    _opaque(scope['root_resource_id']);_integer(scope['policy_generation'],0,2**53-1)
    if type(scope['selections']) is not list or len(scope['selections'])>MAX_COMPONENTS or type(scope['filters']) is not list or len(scope['filters'])>32:raise ValueError('invalid capture scope')
    paths=[_path(p) for p in scope['selections']]
    if paths!=sorted(set(paths),key=lambda p:tuple(x.encode('utf-8') for x in p)):raise ValueError('invalid scope selection')
    if scope['mode']=='folder' and paths!=[()]:raise ValueError('invalid folder selection')
    if scope['mode']=='selected' and not paths:raise ValueError('empty selection')
    for item in scope['filters']:_text(item,128)
    if scope['filters']!=sorted(set(scope['filters'])):raise ValueError('invalid filters')
    limits=value['limits'];bounds={'nodes':MAX_COMPONENTS,'depth':MAX_DEPTH,'file_bytes':MAX_FILE_BYTES,'total_bytes':MAX_TOTAL_BYTES,'manifest_bytes':MAX_MANIFEST_BYTES,'seconds':MAX_SECONDS}
    if type(limits) is not dict or set(limits)!=set(bounds):raise ValueError('invalid snapshot limits')
    for key,maximum in bounds.items():_integer(limits[key],1,maximum)
    dossier=value['dossier']
    if value['schema_version']==SCHEMA_VERSION:
        keys={'dossier_id','dossier_revision','source_id','source_revision','source_sha256','result_sha256','provider','provider_sha256','provider_version','provider_evidence','version_evidence','operation','result','interpretations'}
        if type(dossier) is not dict or set(dossier)!=keys:raise ValueError('invalid dossier facts')
        for key in ('dossier_id','provider','provider_version','provider_evidence','version_evidence'):_text(dossier[key],512)
        _opaque(dossier['source_id'])
        _revision(dossier['dossier_revision']);_revision(dossier['source_revision'])
        for key in ('source_sha256','result_sha256','provider_sha256'):_hash(dossier[key])
        if (not dossier['source_revision']['value'].startswith('sha256:'+dossier['source_sha256']+':')
            or (facts:=profile_for(dossier['provider'],dossier['provider_version'])) is None or dossier['provider_evidence']!=facts.producer_evidence
            or dossier['version_evidence']!=facts.version_evidence):
            raise ValueError('invalid dossier provenance')
        result=dossier['result']
        if (dossier['operation'] not in {'functions','info','strings','disassembly','xrefs_to_entry','decompile','xrefs_from_instruction'} or type(result) is not dict
            or set(result)!={'encoding','size_bytes','sha256','data'} or result['encoding']!='base64'
            or not isinstance(result['data'],str) or len(result['data'])>68268):raise ValueError('invalid snapshot result')
        try:result_bytes=base64.b64decode(result['data'],validate=True)
        except (ValueError,binascii.Error) as exc:raise ValueError('invalid snapshot result') from exc
        if (base64.b64encode(result_bytes).decode('ascii')!=result['data'] or type(result['size_bytes']) is not int
            or not 0<result['size_bytes']<=50*1024 or len(result_bytes)!=result['size_bytes']
            or result['sha256']!=dossier['result_sha256'] or hashlib.sha256(result_bytes).hexdigest()!=dossier['result_sha256']):
            raise ValueError('snapshot result changed')
        from src.openclank.analysis_result_receipts import validate_result
        validated=validate_result(result_bytes,operation=dossier['operation'],source_sha=dossier['source_sha256'],
            source_revision=dossier['source_revision'],provider_sha=dossier['provider_sha256'])
        if validated['provider']!=dossier['provider'] or validated['providerVersion']!=dossier['provider_version']:
            raise ValueError('snapshot result provenance changed')
    else:
        keys={'dossier_id','dossier_revision','source_id','source_revision','source_sha256','result_sha256','operation','producer','producer_version','producer_evidence','coverage','payload_integrity','result','interpretations'}
        if type(dossier) is not dict or set(dossier)!=keys: raise ValueError('invalid dossier facts')
        _text(dossier['dossier_id'],512);_opaque(dossier['source_id']);_revision(dossier['dossier_revision']);_revision(dossier['source_revision'])
        _hash(dossier['source_sha256']);_hash(dossier['result_sha256'])
        if (not dossier['source_revision']['value'].startswith('sha256:'+dossier['source_sha256']+':')
            or dossier['operation']!='info' or dossier['producer']!='openclank.elf-metadata'
            or dossier['producer_version']!='elf-v1-metadata-1' or dossier['producer_evidence']!='host_owned_capture'
            or dossier['coverage']!='partial_declared_metadata' or dossier['payload_integrity']!='not_checked'):
            raise ValueError('invalid dossier provenance')
        result=dossier['result']
        if (type(result) is not dict or set(result)!={'encoding','size_bytes','sha256','data'} or result['encoding']!='base64'
            or not isinstance(result['data'],str) or len(result['data'])>68268): raise ValueError('invalid snapshot result')
        try: result_bytes=base64.b64decode(result['data'],validate=True)
        except (ValueError,binascii.Error) as exc: raise ValueError('invalid snapshot result') from exc
        if (base64.b64encode(result_bytes).decode('ascii')!=result['data'] or type(result['size_bytes']) is not int
            or not 0<result['size_bytes']<=50*1024 or len(result_bytes)!=result['size_bytes']
            or result['sha256']!=dossier['result_sha256'] or hashlib.sha256(result_bytes).hexdigest()!=dossier['result_sha256']):
            raise ValueError('snapshot result changed')
        from src.openclank.analysis_projects import validate_host_elf_result
        validate_host_elf_result(result_bytes,{'sourceID':dossier['source_id'],'sourceSHA256':dossier['source_sha256'],'sourceRevision':dossier['source_revision']})
    if dossier['interpretations']!=[]:raise ValueError('selected interpretations unsupported')
    seen=set()
    for item in dossier['interpretations']:
        if type(item) is not dict or set(item)!={'kind','sha256'}:raise ValueError('invalid interpretation')
        key=(_text(item['kind'],128),_hash(item['sha256']))
        if key in seen:raise ValueError('duplicate interpretation')
        seen.add(key)
    nodes=value['components']
    if type(nodes) is not list or not 1<=len(nodes)<=limits['nodes']:raise ValueError('invalid components')
    by_path={};ordered=[];total=0
    for node in nodes:
        if type(node) is not dict or node.get('kind') not in {'file','directory'}:raise ValueError('invalid component')
        path=_path(node.get('path'));ordered.append(path)
        if len(path)>limits['depth'] or path in by_path:raise ValueError('duplicate component')
        if node['kind']=='file':
            if set(node)!={'path','kind','resource_id','revision','size_bytes','sha256'} or not path:raise ValueError('invalid file')
            if node['resource_id'] is not None:_opaque(node['resource_id'])
            _revision(node['revision']);_hash(node['sha256']);size=_integer(node['size_bytes'],0,limits['file_bytes']);total+=size
            if node['revision']['value']!=f"sha256:{node['sha256']}:{size}":raise ValueError('file revision mismatch')
        else:
            if set(node)!={'path','kind','coverage','child_count','omitted_count','reasons','sha256'} or node['coverage'] not in _COVERAGE:raise ValueError('invalid directory')
            _integer(node['child_count'],0,MAX_COMPONENTS);_integer(node['omitted_count'],0,MAX_COMPONENTS);_hash(node['sha256'])
        by_path[path]=node
    if total>limits['total_bytes'] or ordered!=sorted(ordered,key=lambda p:tuple(x.encode('utf-8') for x in p)) or by_path.get((),{}).get('kind')!='directory':raise ValueError('invalid component tree')
    children={p:[] for p,n in by_path.items() if n['kind']=='directory'}
    for path,node in by_path.items():
        if not path:continue
        parent=path[:-1]
        if parent not in children:raise ValueError('missing parent')
        children[parent].append({'name':path[-1],'kind':node['kind'],'coverage':node.get('coverage'),'size_bytes':node.get('size_bytes'),'sha256':node['sha256']})
    digest_scope=scope_digest(scope)
    if (scope['mode']!='folder' or scope['filters']) and by_path[()]['coverage']=='complete':raise ValueError('filtered scope is not complete')
    for path,rows in children.items():
        node=by_path[path]
        if node['child_count']!=len(rows) or node['sha256']!=directory_digest(scope_sha256=digest_scope,coverage=node['coverage'],omitted_count=node['omitted_count'],reasons=node['reasons'],children=rows):raise ValueError('directory checksum mismatch')
        if node['coverage']=='complete' and any(child['coverage'] not in (None,'complete') for child in rows):raise ValueError('incomplete descendant')
    return value


def encode_manifest(value):
    raw=canonical_json(validate_manifest(value))
    if len(raw)>value['limits']['manifest_bytes']:raise ValueError('snapshot manifest exceeds bounds')
    return raw


def decode_manifest(raw):
    if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_MANIFEST_BYTES:raise ValueError('invalid snapshot bytes')
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError('duplicate field')
            out[key]=value
        return out
    try:value=json.loads(raw.decode('utf-8','strict'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid JSON')))
    except (UnicodeError,ValueError,TypeError,RecursionError) as exc:raise ValueError('invalid snapshot bytes') from exc
    if encode_manifest(value)!=raw:raise ValueError('noncanonical snapshot bytes')
    return value


def reference_for(raw, upload_id):
    manifest=decode_manifest(raw);_opaque(upload_id)
    return {'version':1,'upload_id':upload_id,'manifest_sha256':hashlib.sha256(raw).hexdigest(),'size_bytes':len(raw),
        'media_type':MEDIA_TYPE,'schema_version':manifest['schema_version'],'capture_scope_sha256':scope_digest(manifest['scope']),
        'coverage':manifest['components'][0]['coverage'],'component_count':len(manifest['components'])}


def validate_reference(reference, raw=None):
    if type(reference) is not dict or set(reference)!=_REFERENCE or type(reference['version']) is not int or reference['version']!=1:raise ValueError('invalid snapshot reference')
    _opaque(reference['upload_id']);_hash(reference['manifest_sha256']);_hash(reference['capture_scope_sha256'])
    _integer(reference['size_bytes'],1,MAX_MANIFEST_BYTES);_integer(reference['component_count'],1,MAX_COMPONENTS)
    if reference['media_type']!=MEDIA_TYPE or reference['schema_version'] not in (SCHEMA_VERSION,HOST_SCHEMA_VERSION) or reference['coverage'] not in _COVERAGE:raise ValueError('invalid snapshot reference')
    if raw is not None and reference_for(raw,reference['upload_id'])!=reference:raise ValueError('snapshot reference mismatch')
    return reference
