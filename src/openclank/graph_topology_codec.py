"""Closed, bounded Graph topology evidence; never an authority or source reader."""
from __future__ import annotations

import base64
import hashlib
import json
import re

_PROFILE = 'indexed_topology_v1'
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'(?:file|decl|occ):[0-9a-f]{64}\Z')
_RELATIONS = frozenset({'call','import','value_reference','type_reference','inherits','implements',
    'render','route','overrides','manifest_dependency','sql_table_reference','callback_registration','event_publish','event_consume'})
_RESOLUTION = frozenset({'exact','inferred','ambiguous','unresolved','unsupported'})
_UNKNOWN = frozenset({'unresolved','ambiguous','unsupported','boundary','missing_caller','unstable_endpoint'})
_REASONS = frozenset({'not_indexed','partial_extraction','unsupported_language','scan_incomplete'})
_RECEIPT = frozenset({'workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation',
    'admission_epoch','policy_generation','config_fingerprint'})
_PAGE = frozenset({'version','profile','capture_id','sequence','cursor','next_cursor','page_sha256',
    'data_base64','receipt','scope_sha256','producer_fingerprint','terminal'})
_TERMINAL = frozenset({'enumeration_complete','total_bytes','total_pages','file_count','node_count',
    'relation_count','result_sha256','scope_sha256'})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')


def _hex(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _token(value):
    return type(value) is str and 0 < len(value.encode('utf-8', 'strict')) <= 256 and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def _path(value):
    if (type(value) is not str or not value or len(value.encode('utf-8','strict')) > 4096
        or value.startswith('/') or '\\' in value or re.match(r'^[A-Za-z]:', value) is not None
        or any(p in ('','.','..') for p in value.split('/'))
        or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Topology path unavailable')
    return value


def validate_receipt(value):
    if type(value) is not dict or set(value) != _RECEIPT:
        raise ValueError('Topology receipt unavailable')
    for key, item in value.items():
        if key in {'admission_epoch','policy_generation'}:
            if type(item) is not int or item < 0 or item > 9007199254740991:
                raise ValueError('Topology receipt unavailable')
        elif not _token(item):
            raise ValueError('Topology receipt unavailable')
    if not _hex(value['config_fingerprint']):
        raise ValueError('Topology producer unavailable')
    return value


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate topology field')
            result[key] = value
        return result
    return json.loads(raw.decode('utf-8','strict'), object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid topology number')))


def _validate_artifact(raw, *, max_bytes, expected_receipt=None, expected_paths=None):
    if type(raw) is not bytes or type(max_bytes) is not int or not 0 < len(raw) <= max_bytes:
        raise ValueError('Topology size unavailable')
    value = _strict_json(raw)
    if canonical(value) != raw or type(value) is not dict or set(value) != {'version','profile','identity_version','scope','receipt','producer_fingerprint','files','nodes','relations','unknown'}:
        raise ValueError('Topology record unavailable')
    if value['version'] != 1 or type(value['version']) is not int or value['profile'] != _PROFILE or type(value['identity_version']) is not int or value['identity_version'] != 1:
        raise ValueError('Topology version unavailable')
    receipt = validate_receipt(value['receipt'])
    if expected_receipt is not None and receipt != expected_receipt:
        raise ValueError('Topology receipt changed')
    scope = value['scope']
    if type(scope) is not dict or set(scope) != {'workspace_id','registration_id','checkout_id','paths'}:
        raise ValueError('Topology scope unavailable')
    if any(scope[k] != receipt[k] for k in ('workspace_id','registration_id','checkout_id')):
        raise ValueError('Topology scope changed')
    paths = scope['paths']
    if type(paths) is not list or not paths or any(_path(p) != p for p in paths) or paths != sorted(set(paths),key=lambda p:p.encode('utf-8')):
        raise ValueError('Topology paths unavailable')
    if expected_paths is not None and paths != expected_paths:
        raise ValueError('Topology paths changed')
    if value['producer_fingerprint'] != receipt['config_fingerprint'] or not _hex(value['producer_fingerprint']):
        raise ValueError('Topology producer changed')
    files, nodes, relations, unknown = (value[k] for k in ('files','nodes','relations','unknown'))
    if any(type(items) is not list for items in (files,nodes,relations,unknown)) or len(files) != len(paths):
        raise ValueError('Topology enumeration unavailable')
    file_map = {}
    for item, path in zip(files,paths):
        if type(item) is not dict or set(item) != {'file_id','rel_path','source_hash','coverage'} or item['rel_path'] != path:
            raise ValueError('Topology file unavailable')
        fid = item['file_id']
        if type(fid) is not str or not fid.startswith('file:') or not _ID.fullmatch(fid) or fid in file_map:
            raise ValueError('Topology file identity unavailable')
        coverage = item['coverage']
        if type(coverage) is not dict or set(coverage) != {'state','reasons'} or coverage['state'] not in {'indexed','unknown'}:
            raise ValueError('Topology coverage unavailable')
        reasons=coverage['reasons']
        if type(reasons) is not list or any(r not in _REASONS for r in reasons) or reasons != sorted(set(reasons)):
            raise ValueError('Topology coverage unavailable')
        if (coverage['state']=='indexed' and (reasons or not _hex(item['source_hash']))) or (coverage['state']=='unknown' and (not reasons or item['source_hash'] is not None and not _hex(item['source_hash']))):
            raise ValueError('Topology coverage unavailable')
        file_map[fid] = item
    node_map = {}; previous = None
    for item in nodes:
        if type(item) is not dict or set(item) != {'id','file_id','kind','identity_state','source_hash','relation_kind','resolution_state'}:
            raise ValueError('Topology node unavailable')
        ident=item['id']; kind=item['kind']; fid=item['file_id']
        if (type(ident) is not str or not _ID.fullmatch(ident) or kind not in {'file','declaration','occurrence'}
            or not ident.startswith({'file':'file:','declaration':'decl:','occurrence':'occ:'}[kind])
            or previous is not None and ident <= previous or fid not in file_map or item['source_hash'] != file_map[fid]['source_hash']
            or item['identity_state'] not in {'stable','snapshot_local'}):
            raise ValueError('Topology node identity unavailable')
        if kind == 'file' and (ident != fid or item['identity_state'] != 'stable'):
            raise ValueError('Topology file node unavailable')
        if kind == 'declaration' and item['identity_state'] not in {'stable','snapshot_local'}:
            raise ValueError('Topology declaration unavailable')
        if kind != 'occurrence' and (item['relation_kind'] is not None or item['resolution_state'] is not None):
            raise ValueError('Topology node properties unavailable')
        if kind == 'occurrence' and (item['identity_state'] != 'snapshot_local' or item['relation_kind'] not in _RELATIONS or item['resolution_state'] not in _RESOLUTION):
            raise ValueError('Topology occurrence unavailable')
        node_map[ident]=item; previous=ident
    previous = None
    for item in relations:
        if type(item) is not dict or set(item) != {'from','to','kind','state','count'}:
            raise ValueError('Topology relation unavailable')
        key=(item['from'],item['to'],item['kind'],item['state'])
        if (any(type(x) is not str for x in key) or previous is not None and key <= previous
            or item['from'] not in node_map or item['to'] not in node_map
            or item['kind'] not in _RELATIONS or item['state'] not in {'exact','inferred'}
            or type(item['count']) is not int or item['count'] < 1 or item['count'] > 9007199254740991):
            raise ValueError('Topology relation unavailable')
        previous=key
    previous=None
    for item in unknown:
        if type(item) is not dict or set(item) != {'file_id','occurrence_id','reason'}:
            raise ValueError('Topology uncertainty unavailable')
        key=(item['file_id'],item['occurrence_id'],item['reason'])
        if (any(type(x) is not str for x in key) or previous is not None and key <= previous or item['file_id'] not in file_map
            or not item['occurrence_id'].startswith('occ:') or not _ID.fullmatch(item['occurrence_id']) or item['reason'] not in _UNKNOWN):
            raise ValueError('Topology uncertainty unavailable')
        previous=key
    return value


def validate_artifact(raw, *, max_bytes, expected_receipt=None, expected_paths=None):
    try:
        return _validate_artifact(raw, max_bytes=max_bytes, expected_receipt=expected_receipt, expected_paths=expected_paths)
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError('Topology record unavailable') from exc


class PageCollector:
    """Accept one ordered Native-owned stream and verify its complete commitment."""
    def __init__(self, *, receipt, paths, max_bytes):
        self.receipt=validate_receipt(receipt); self.paths=list(paths); self.max_bytes=max_bytes
        if type(max_bytes) is not int or not 0 < max_bytes <= 64 * 1024 * 1024:
            raise ValueError('Topology budget unavailable')
        self.parts=[]; self.length=0; self.next_cursor=None; self.capture_id=None
        self.scope_sha256=None; self.terminal=None

    def accept(self, page):
        try:
            return self._accept(page)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError) as exc:
            raise ValueError('Topology page unavailable') from exc

    def _accept(self, page):
        if self.terminal is not None or type(page) is not dict or set(page) != _PAGE:
            raise ValueError('Topology page unavailable')
        sequence=len(self.parts)
        if (page['version'] != 1 or type(page['version']) is not int or page['profile'] != _PROFILE
            or type(page['sequence']) is not int or page['sequence'] != sequence or page['cursor'] != self.next_cursor
            or page['receipt'] != self.receipt or page['producer_fingerprint'] != self.receipt['config_fingerprint']
            or not _hex(page['capture_id']) or not _hex(page['scope_sha256']) or not _hex(page['page_sha256'])):
            raise ValueError('Topology page changed')
        if self.capture_id is None:
            self.capture_id=page['capture_id']; self.scope_sha256=page['scope_sha256']
        if page['capture_id'] != self.capture_id or page['scope_sha256'] != self.scope_sha256:
            raise ValueError('Topology page changed')
        if type(page['data_base64']) is not str or len(page['data_base64']) > 174764:
            raise ValueError('Topology page unavailable')
        try:
            data=base64.b64decode(page['data_base64'],validate=True)
        except (ValueError,TypeError) as exc:
            raise ValueError('Topology page unavailable') from exc
        if not data or len(data) > 128*1024 or base64.b64encode(data).decode('ascii') != page['data_base64'] or hashlib.sha256(data).hexdigest() != page['page_sha256']:
            raise ValueError('Topology page changed')
        if self.length + len(data) > self.max_bytes:
            raise ValueError('Topology budget exceeded')
        self.parts.append(data); self.length += len(data)
        next_cursor=page['next_cursor']; terminal=page['terminal']
        if next_cursor is None:
            if type(terminal) is not dict or set(terminal) != _TERMINAL or terminal['enumeration_complete'] is not True:
                raise ValueError('Topology completion unavailable')
            if (terminal['total_bytes'] != self.length or type(terminal['total_bytes']) is not int
                or terminal['total_pages'] != len(self.parts) or type(terminal['total_pages']) is not int
                or terminal['result_sha256'] != self.capture_id or terminal['scope_sha256'] != self.scope_sha256
                or any(type(terminal[k]) is not int or terminal[k] < 0 for k in ('file_count','node_count','relation_count'))):
                raise ValueError('Topology completion changed')
            self.terminal=terminal
        elif type(next_cursor) is not str or next_cursor != str(sequence+1) or terminal is not None:
            raise ValueError('Topology continuation unavailable')
        self.next_cursor=next_cursor

    def finish(self):
        if self.terminal is None:
            raise ValueError('Topology incomplete')
        raw=b''.join(self.parts)
        if hashlib.sha256(raw).hexdigest() != self.capture_id:
            raise ValueError('Topology capture changed')
        value=validate_artifact(raw,max_bytes=self.max_bytes,expected_receipt=self.receipt,expected_paths=self.paths)
        if hashlib.sha256(canonical(value['scope'])).hexdigest() != self.scope_sha256:
            raise ValueError('Topology scope changed')
        if any(len(value[k]) != self.terminal[n] for k,n in (('files','file_count'),('nodes','node_count'),('relations','relation_count'))):
            raise ValueError('Topology count changed')
        return raw,value


def compare_artifacts(recorded, current):
    """Compare qualified indexed observations; callers must establish authority first."""
    if (type(recorded) is not dict or type(current) is not dict
        or recorded.get('version') != current.get('version') or recorded.get('identity_version') != current.get('identity_version')
        or recorded.get('profile') != _PROFILE or current.get('profile') != _PROFILE
        or recorded.get('scope') != current.get('scope')
        or recorded.get('producer_fingerprint') != current.get('producer_fingerprint')):
        raise ValueError('Topology scopes are incompatible')
    def stable_nodes(value):
        return {n['id']:n for n in value['nodes'] if n['kind']=='file' or n['kind']=='declaration' and n['identity_state']=='stable'}
    before=stable_nodes(recorded); after=stable_nodes(current)
    stable_both=set(before).intersection(after)
    old_decls={key for key, node in before.items() if node['kind']=='declaration'}
    new_decls={key for key, node in after.items() if node['kind']=='declaration'}
    def relations(value):
        result={}; excluded=0
        for item in value['relations']:
            key=(item['from'],item['to'],item['kind'],item['state'])
            if item['from'] in stable_both and item['to'] in stable_both:
                result[key]=item['count']
            else:
                excluded += 1
        return result, excluded
    old, old_excluded=relations(recorded); new, new_excluded=relations(current)
    changes=[]
    for key in sorted(set(old)|set(new)):
        prior=old.get(key,0); present=new.get(key,0)
        if prior != present:
            changes.append({'from':key[0],'to':key[1],'kind':key[2],'state':key[3],
                'recorded_count':prior,'current_count':present})
    partial=any(item['coverage']['state']!='indexed' for side in (recorded,current) for item in side['files'])
    return {'version':1,'basis':'complete_indexed_induced_scope','partial_extraction':partial,
        'recorded_head':recorded['receipt']['head_generation'],'current_head':current['receipt']['head_generation'],
        'declarations':{'matched':len(old_decls & new_decls), 'recorded_only':sorted(old_decls-new_decls),
            'current_only':sorted(new_decls-old_decls)},
        'indexed_relation_changes':changes,
        'uncomparable_relations':{'recorded_rows':old_excluded,'current_rows':new_excluded,
            'reason':'endpoint_not_stable_on_both_sides'},
        'uncertainty':{'recorded':len(recorded['unknown']),'current':len(current['unknown']),
            'occurrence_continuity':'unknown','outside_scope':'unknown','semantic_or_runtime':'unknown'}}
