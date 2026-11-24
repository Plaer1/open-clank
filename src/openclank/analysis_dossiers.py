"""Read-only projection of an already server-verified analysis dossier."""
from __future__ import annotations

import json
import re
import hashlib
from typing import Any

from src.openclank.analysis_profiles import profile_for

_SAFE_INTEGER = 2**53 - 1
_SELECTOR = {'name', 'address', 'size', 'addressSpace'}


def static_function_overlay(verified: dict[str, Any]) -> dict[str, Any]:
    result=verified.get('result') if type(verified) is dict else None
    if type(result) is not dict or type(result.get('functions')) is not list or len(result['functions'])>256:
        raise ValueError('static function overlay unavailable')
    rows=result['functions'];keys=[]
    for row in rows:
        if type(row) is not dict or set(row)!=_SELECTOR:raise ValueError('static function overlay unavailable')
        project_function({**verified,'result':{**result,'functions':[row]}},row)
        try:keys.append(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False))
        except (TypeError,ValueError,UnicodeError,RecursionError) as exc:raise ValueError('static function overlay unavailable') from exc
    counts={key:keys.count(key) for key in set(keys)};functions=[]
    for row,key in zip(rows,keys):
        if counts[key]!=1:continue
        projection=project_function(verified,row)
        payload=json.dumps(['openclank.ma4.static-function.v1',projection['resultIdentity']['resultSHA256'],row],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')
        functions.append({'id':'overlay:'+hashlib.sha256(payload).hexdigest(),**row})
    identity=verified.get('resultIdentity');revision=verified.get('revision')
    if (type(identity) is not dict or identity.get('operation')!='functions' or result.get('coverage')!='partial_static_analysis'
        or result.get('architecture')!='unknown' or type(result.get('omitted')) is not int or result['omitted']<0
        or verified.get('sourceState') not in {'current','historical','unchecked'} or type(revision) is not dict):raise ValueError('static function overlay unavailable')
    output={'schema':'openclank.static-function-overlay.v1','state':'verified','sourceState':verified['sourceState'],
        'dossierRevision':revision,'resultIdentity':identity,'coverage':'partial_static_analysis','architecture':'unknown',
        'omitted':result['omitted'],'ambiguousRows':sum(count for count in counts.values() if count>1),
        'functions':functions,'edges':[],'unsupported':['disassembly','cross_references','traces','runtime','address_navigation']}
    if len(json.dumps(output,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict'))>262144:raise ValueError('static function overlay unavailable')
    return output


def project_function(verified: dict[str, Any], selector: Any) -> dict[str, Any]:
    if (type(verified) is not dict or verified.get('state') != 'verified'
        or verified.get('sourceState') not in {'current', 'historical', 'unchecked'}
        or type(selector) is not dict or set(selector) != _SELECTOR
        or not isinstance(selector['name'], str) or len(selector['name'].encode('utf-8', 'strict')) > 512
        or selector['addressSpace'] != 'provider_virtual'
        or any(type(selector[key]) is not int or not 0 <= selector[key] <= _SAFE_INTEGER for key in ('address', 'size'))
        or selector['address'] + selector['size'] > _SAFE_INTEGER):
        raise ValueError('function unavailable')
    result, identity = verified.get('result'), verified.get('resultIdentity')
    if (type(result) is not dict or type(identity) is not dict or identity.get('operation') != 'functions'
        or result.get('coverage') != 'partial_static_analysis' or result.get('architecture') != 'unknown'
        or type(result.get('functions')) is not list or len(result['functions']) > 256
        or type(result.get('omitted')) is not int or result['omitted'] < 0):
        raise ValueError('function unavailable')
    matches = [row for row in result['functions'] if type(row) is dict and set(row) == _SELECTOR and row == selector
               and type(row.get('address')) is int and type(row.get('size')) is int]
    if len(matches) != 1:
        raise ValueError('function unavailable or ambiguous')
    projection = {'state':'verified', 'sourceState':verified['sourceState'], 'dossierRevision':verified['revision'],
                  'resultIdentity':identity, 'function':dict(matches[0]), 'coverage':'partial_static_analysis',
                  'architecture':'unknown', 'omitted':result['omitted'],
                  'unsupported':['disassembly','cross_references','traces']}
    try:
        if len(json.dumps(projection,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8','strict')) > 32768:
            raise ValueError('function unavailable')
    except (TypeError,UnicodeError,RecursionError) as exc:
        raise ValueError('function unavailable') from exc
    return projection


def function_export(identity, export_id, dossier_id, dossier_revision, projections):
    from src.openclank.analysis_projects import project_identity
    if (type(identity) is not dict or identity != project_identity(identity['ownerSubjectID'],identity['workspaceID'])
        or any(not isinstance(value,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',value) for value in (export_id,dossier_id))
        or type(dossier_revision) is not dict or set(dossier_revision)!={'kind','value'} or dossier_revision.get('kind')!='hostFingerprint'
        or not isinstance(dossier_revision.get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',dossier_revision['value'])
        or len(dossier_revision['value'])>512 or type(projections) is not list or not 1<=len(projections)<=16):
        raise ValueError('invalid function export')
    first=projections[0]
    if type(first) is not dict:raise ValueError('invalid function export')
    common={key:first.get(key) for key in ('sourceState','dossierRevision','resultIdentity','coverage','architecture','omitted','unsupported')}
    if (common['dossierRevision']!=dossier_revision or common['sourceState'] not in {'current','historical','unchecked'}
        or common['coverage']!='partial_static_analysis' or common['architecture']!='unknown'
        or common['unsupported']!=['disassembly','cross_references','traces'] or type(common['omitted']) is not int or common['omitted']<0):
        raise ValueError('invalid function export')
    result_identity=common['resultIdentity']
    if (type(result_identity) is not dict or set(result_identity)!={'sourceID','sourceSHA256','sourceRevision','resultSHA256','operation','provider','providerSHA256','providerVersion','executableEvidence','versionEvidence'}
        or not isinstance(result_identity['sourceID'],str) or not 0<len(result_identity['sourceID'].encode('utf-8','strict'))<=512
        or any(not isinstance(result_identity[k],str) or not re.fullmatch(r'[0-9a-f]{64}',result_identity[k]) for k in ('sourceSHA256','resultSHA256','providerSHA256'))
        or result_identity['operation']!='functions' or (facts:=profile_for(result_identity['provider'],result_identity['providerVersion'])) is None
        or result_identity['executableEvidence']!=facts.producer_evidence or result_identity['versionEvidence']!=facts.version_evidence):
        raise ValueError('invalid function export')
    source_revision=result_identity['sourceRevision']
    if (type(source_revision) is not dict or set(source_revision)!={'kind','value'} or source_revision.get('kind')!='hostFingerprint'
        or not isinstance(source_revision.get('value'),str) or len(source_revision['value'])>512 or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',source_revision['value'])):
        raise ValueError('invalid function export')
    rows=[]
    for item in projections:
        if type(item) is not dict or item.get('state')!='verified' or any(item.get(key)!=value for key,value in common.items()):raise ValueError('invalid function export')
        row=item.get('function')
        checked=project_function({'state':'verified','sourceState':common['sourceState'],'revision':dossier_revision,'resultIdentity':result_identity,
            'result':{'coverage':'partial_static_analysis','architecture':'unknown','omitted':common['omitted'],'functions':[row]}},row)
        if checked['function'] in rows:raise ValueError('duplicate function')
        rows.append(checked['function'])
    header={'version':1,'evidenceKind':'derived_function_export',**identity,'exportID':export_id,'sourceDossierID':dossier_id,
        'sourceDossierRevision':dossier_revision,'sourceStateAtExport':common['sourceState'],'resultIdentity':result_identity,
        'functions':rows,'coverage':common['coverage'],'architecture':common['architecture'],'omitted':common['omitted'],'unsupported':common['unsupported']}
    document='```json\n'+json.dumps(header,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n```\n\nDerived static inspection; provider virtual addresses are not file offsets or runtime facts.\n'
    if len(document.encode('utf-8','strict'))>65536:raise ValueError('function export too large')
    return document


def verify_function_export(document,identity,export_id):
    from src.openclank.analysis_projects import _decode
    prefix='```json\n';end='\n```\n\nDerived static inspection; provider virtual addresses are not file offsets or runtime facts.\n'
    if not isinstance(document,str) or len(document.encode('utf-8','strict'))>65536 or not document.startswith(prefix) or not document.endswith(end):raise ValueError('invalid function export')
    header=_decode(document[len(prefix):-len(end)])
    if type(header) is not dict or set(header)!={'version','evidenceKind','ownerSubjectID','workspaceID','projectKey','exportID','sourceDossierID','sourceDossierRevision','sourceStateAtExport','resultIdentity','functions','coverage','architecture','omitted','unsupported'} or type(header['version']) is not int or header['version']!=1 or header['evidenceKind']!='derived_function_export' or header['exportID']!=export_id:raise ValueError('invalid function export')
    projections=[{'state':'verified','sourceState':header['sourceStateAtExport'],'dossierRevision':header['sourceDossierRevision'],
        'resultIdentity':header['resultIdentity'],'coverage':header['coverage'],'architecture':header['architecture'],
        'omitted':header['omitted'],'unsupported':header['unsupported'],'function':row} for row in header['functions']] if type(header['functions']) is list else []
    if document!=function_export(identity,export_id,header['sourceDossierID'],header['sourceDossierRevision'],projections):raise ValueError('invalid function export')
