"""Closed data checks for one recorded-file/current-code impact composition.

This module creates no authority and performs no source or History reads.
"""
from __future__ import annotations
import copy
import re
from src.openclank.code_query import SCHEMA as CODE_SCHEMA, validate_query, _relative
from src.openclank.code_outline_witness import verify_outline_completeness
from src.openclank.code_graph_projection import prepare_graph_query, project_code_graph
from src.openclank.history_change_sets import parse_locator

SCHEMA = 'openclank.recorded-code-impact/v1'
COVERAGE = 'indexed_declarations_in_one_recorded_file'
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'(?:file|decl):[0-9a-f]{64}\Z')


def _text(value, limit=4096):
    if type(value) is not str or not value or len(value.encode('utf-8','strict')) > limit or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Recorded impact unavailable')
    return value


def _sha(value):
    if type(value) is not str or not _HEX.fullmatch(value): raise ValueError('Recorded impact unavailable')
    return value


def validate_request(body):
    if type(body) is not dict or set(body) != {'workspace_id','locator','resource_id','expected_receipt'}:
        raise ValueError('Invalid recorded impact request')
    workspace=_text(body['workspace_id'],256); locator=_text(body['locator'],16384)
    resource=_text(body['resource_id'],128)
    if not re.fullmatch(r'resource-[0-9a-f]{64}',resource): raise ValueError('Invalid recorded impact resource')
    parse_locator(locator)
    q=validate_query({'operation':'capabilities','expected_receipt':body['expected_receipt'],'fields':[],'channels':[]})
    if q.get('expected_receipt') is None or q['expected_receipt']['workspace_id'] != workspace:
        raise ValueError('Invalid recorded impact receipt')
    return {'workspace_id':workspace,'locator':locator,'resource_id':resource,'expected_receipt':copy.deepcopy(q['expected_receipt'])}


def validate_read(result, *, receipt, rel_path, sha256, size):
    _relative(rel_path); _sha(sha256)
    if type(size) is not int or not 1 <= size <= 1048576: raise ValueError('Recorded file unavailable')
    if (type(result) is not dict or result.get('schema') != CODE_SCHEMA or result.get('operation') != 'read'
        or result.get('error') is not None or result.get('degraded') is not False or result.get('receipt') != receipt):
        raise ValueError('Current indexed source unavailable')
    hits=result.get('hits')
    if type(hits) is not list or len(hits)!=1: raise ValueError('Current indexed source unavailable')
    hit=hits[0]
    if type(hit) is not dict: raise ValueError('Current indexed source unavailable')
    ident=hit.get('id'); source=hit.get('source'); raw=hit.get('raw_source'); props=hit.get('properties')
    if (type(ident) is not str or not _ID.fullmatch(ident) or not ident.startswith('file:') or hit.get('kind')!='raw_window'
        or hit.get('current_source_verified') is not True or hit.get('evidence_state')!='current'
        or type(source) is not dict or source.get('file_id')!=ident[5:] or source.get('rel_path')!=rel_path
        or type(raw) is not dict or raw.get('rel_path')!=rel_path or raw.get('hash_algorithm')!='sha256'
        or raw.get('hash_scope')!='file' or raw.get('hash')!=sha256 or raw.get('file_size')!=size
        or type(props) is not dict or props.get('rel_path')!=rel_path
        or props.get('indexed_source_hash')!=source.get('source_hash')):
        raise ValueError('Current indexed source unavailable')
    return {'file_id':ident,'indexed_blake3':_sha(source.get('source_hash'))}



def validate_comparison_read(result, *, receipt, rel_path):
    """Validate one full, double-read Files observation; index status is separate."""
    _relative(rel_path)
    if (type(result) is not dict or result.get('schema')!=CODE_SCHEMA or result.get('operation')!='read'
            or result.get('error') is not None or result.get('degraded') is not False or result.get('receipt')!=receipt):
        raise ValueError('Current source unavailable')
    hits=result.get('hits')
    if type(hits) is not list or len(hits)!=1 or type(hits[0]) is not dict:raise ValueError('Current source unavailable')
    hit=hits[0]; ident=hit.get('id'); raw=hit.get('raw_source'); props=hit.get('properties'); source=hit.get('source')
    if (type(ident) is not str or not _ID.fullmatch(ident) or not ident.startswith('file:') or hit.get('kind')!='raw_window'
            or type(raw) is not dict or raw.get('rel_path')!=rel_path or raw.get('hash_algorithm')!='sha256'
            or raw.get('hash_scope')!='file' or type(raw.get('file_size')) is not int or not 0<=raw['file_size']<=1048576
            or type(props) is not dict or props.get('rel_path')!=rel_path or type(hit.get('current_source_verified')) is not bool):
        raise ValueError('Current source unavailable')
    digest=_sha(raw.get('hash')); index='unknown'
    if raw.get('state')!='observed_file':raise ValueError('Current source unavailable')
    if hit['current_source_verified'] is False and (hit.get('evidence_state') not in {'stale','observed_file'} or source is not None):raise ValueError('Current source unavailable')
    if hit['current_source_verified'] is True:
        if (hit.get('evidence_state')!='current' or type(source) is not dict or source.get('file_id')!=ident[5:]
                or source.get('rel_path')!=rel_path or props.get('indexed_source_hash')!=source.get('source_hash')):
            raise ValueError('Current source unavailable')
        _sha(source.get('source_hash'));index='current'
    return {'file_id':ident,'sha256':digest,'size_bytes':raw['file_size'],'index':index}


def comparison_result(request, *, recorded_after, observation, cost):
    request=validate_request(request)
    if (type(recorded_after) is not dict or set(recorded_after)!={'action_id','version_id','sha256','size_bytes'}
            or type(observation) is not dict or set(observation)!={'file_id','sha256','size_bytes','index'}
            or observation['index'] not in {'current','unknown'}):raise ValueError('Comparison unavailable')
    for k in ('action_id','version_id'):_text(recorded_after[k],512)
    for value in (recorded_after['sha256'],observation['sha256']):_sha(value)
    if (type(recorded_after['size_bytes']) is not int or not 1<=recorded_after['size_bytes']<=1048576
            or type(observation['size_bytes']) is not int or not 0<=observation['size_bytes']<=1048576
            or type(observation['file_id']) is not str or not _ID.fullmatch(observation['file_id']) or not observation['file_id'].startswith('file:')):
        raise ValueError('Comparison unavailable')
    state='recorded_matches_current' if (recorded_after['sha256'],recorded_after['size_bytes'])==(observation['sha256'],observation['size_bytes']) else 'current_differs_from_recorded'
    result=unavailable_result(cost)
    result['comparison']={'version':1,'state':state,'index':observation['index'],'workspace_id':request['workspace_id'],
        'resource_id':request['resource_id'],'action_id':recorded_after['action_id'],'version_id':recorded_after['version_id'],
        'file_id':observation['file_id'],'recorded_sha256':recorded_after['sha256'],'recorded_size':recorded_after['size_bytes'],
        'current_sha256':observation['sha256'],'current_size':observation['size_bytes']}
    return result

def validate_outline(result, *, receipt, rel_path, file_id, indexed_blake3):
    _relative(rel_path); _sha(indexed_blake3)
    if type(file_id) is not str or not _ID.fullmatch(file_id) or not file_id.startswith('file:'): raise ValueError('Indexed file unavailable')
    if (type(result) is not dict or result.get('schema')!=CODE_SCHEMA or result.get('operation')!='outline'
        or result.get('receipt')!=receipt or result.get('error') is not None
        or result.get('degraded') is not False or result.get('next_cursor') is not None):
        raise ValueError('Complete outline unavailable')
    if not verify_outline_completeness(result, receipt=receipt, file_id=file_id, rel_path=rel_path, source_hash=indexed_blake3): raise ValueError('Complete outline unavailable')
    hits=result.get('hits')
    if type(hits) is not list or not 1<=len(hits)<=200: raise ValueError('Complete outline unavailable')
    ids=[]
    for hit in hits:
        if type(hit) is not dict: raise ValueError('Complete outline unavailable')
        ident=hit.get('id'); source=hit.get('source'); props=hit.get('properties')
        if (type(ident) is not str or not _ID.fullmatch(ident) or not ident.startswith('decl:') or ident in ids
            or hit.get('kind')!='declaration' or type(source) is not dict or source.get('file_id')!=file_id[5:]
            or source.get('rel_path')!=rel_path or source.get('source_hash')!=indexed_blake3
            or hit.get('evidence_state') in ('ambiguous','stale','unsupported')
            or type(props) is not dict or props.get('identity_state')=='ambiguous_snapshot_local'):
            raise ValueError('Complete outline unavailable')
        ids.append(ident)
    return ids


def impact_query(seeds, receipt):
    if type(seeds) is not list or not 1<=len(seeds)<=200 or len(set(seeds))!=len(seeds) or any(type(x) is not str or not _ID.fullmatch(x) or not x.startswith('decl:') for x in seeds):
        raise ValueError('Impact seeds unavailable')
    query={'schema':CODE_SCHEMA,'operation':'impact','seed_ids':list(seeds),'direction':'incoming',
           'fields':['label','source','properties'],'channels':[], 'expected_receipt':copy.deepcopy(receipt)}
    native, budget=prepare_graph_query(query)
    return native, query, budget


def new_cost():
    return {'query_calls':0,'history_bytes_read':0,'files_bytes_read':0,'native_rpc_calls_observed':0,'composed_work':None}


def begin_query(cost):
    if type(cost) is not dict or type(cost.get('query_calls')) is not int or not 0<=cost['query_calls']<4:
        raise ValueError('Invalid query count')
    cost['query_calls']+=1
    return cost


def record_cost(cost, *, query=None, history_bytes=0):
    if type(history_bytes) is not int or history_bytes<0: raise ValueError('Invalid cost')
    cost['history_bytes_read']+=history_bytes
    if query is not None:
        observed=query.get('cost') if type(query) is dict else None
        for dest, key in (('files_bytes_read','files_bytes_read'),('native_rpc_calls_observed','native_rpc_calls_completed')):
            value=observed.get(key) if type(observed) is dict else None
            if cost[dest] is not None:
                cost[dest]=cost[dest]+value if type(value) is int and value>=0 else None
    return cost


def unavailable_result(cost=None):
    # A failed or cancelled operation may have consumed unreported work.
    count=cost.get('query_calls') if type(cost) is dict else 0
    if type(count) is not int or not 0<=count<=4: count=None
    failure_cost={'query_calls':count,'history_bytes_read':None,'files_bytes_read':None,
                  'native_rpc_calls_observed':None,'composed_work':None}
    return {'schema':SCHEMA,'state':'unavailable','coverage':None,'workspace_id':None,'resource_id':None,
            'recorded_after':None,'source':None,'query':None,'graph':None,'cost':failure_cost}


def success_result(request, *, recorded_after, source, query, impact_result, caller_budget, cost):
    request=validate_request(request)
    if type(recorded_after) is not dict or set(recorded_after)!={'action_id','version_id','sha256','size_bytes'}:
        raise ValueError('Recorded impact unavailable')
    for key in ('action_id','version_id'): _text(recorded_after[key],512)
    _sha(recorded_after['sha256'])
    if type(recorded_after['size_bytes']) is not int or not 1<=recorded_after['size_bytes']<=1048576: raise ValueError('Recorded impact unavailable')
    if type(source) is not dict or set(source)!={'rel_path','file_id','indexed_blake3','sha256','size_bytes'}:
        raise ValueError('Recorded impact unavailable')
    _relative(source['rel_path']); _sha(source['indexed_blake3']); _sha(source['sha256'])
    if (type(source['file_id']) is not str or not _ID.fullmatch(source['file_id']) or not source['file_id'].startswith('file:')
        or source['sha256']!=recorded_after['sha256'] or source['size_bytes']!=recorded_after['size_bytes']): raise ValueError('Recorded impact unavailable')
    if type(impact_result) is not dict or impact_result.get('operation')!='impact' or impact_result.get('receipt')!=request['expected_receipt'] or impact_result.get('error') is not None:
        raise ValueError('Recorded impact unavailable')
    graph=project_code_graph(impact_result,query=query,caller_budget=caller_budget)
    if graph.get('error') is not None or graph.get('receipt')!=request['expected_receipt']: raise ValueError('Recorded impact unavailable')
    if type(cost) is not dict or set(cost)!=set(new_cost()) or cost['query_calls']!=4 or cost['composed_work'] is not None or any(cost[k] is not None and (type(cost[k]) is not int or cost[k]<0) for k in ('history_bytes_read','files_bytes_read','native_rpc_calls_observed')):
        raise ValueError('Recorded impact cost unavailable')
    return {'schema':SCHEMA,'state':'current','coverage':COVERAGE,'workspace_id':request['workspace_id'],
            'resource_id':request['resource_id'],'recorded_after':dict(recorded_after),'source':dict(source),
            'query':copy.deepcopy(query),'graph':graph,'cost':dict(cost)}
