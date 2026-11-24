"""Compose one qualified recorded file with existing indexed-code impact."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from src.openclank.code_query import query_code, resolve_query_binding, MAX_QUERY_WORK
from src.openclank.history_after_source import capture_recorded_after
from src.openclank.recorded_code_impact_contract import (
    validate_request, validate_read, validate_outline, impact_query,
    new_cost, begin_query, record_cost, success_result, unavailable_result, validate_comparison_read, comparison_result,
)


async def recorded_code_impact(*, repository, provider, auth_manager, owner, account, body,
                               history_client, history_context, files_provider, current, deadline):
    """Read a selected retained after-state and current indexed declarations, fail closed.

    Original caller authority and Files context are supplied by the owning route.
    The two qualified captures and four existing queries are point-in-time checks,
    not a cross-service atomic snapshot.
    """
    cost=new_cost()
    try:
        request = validate_request(body)
        if not callable(current) or current() is not True or not account or auth_manager.account_id(owner) != account:
            raise ValueError('Recorded impact unavailable')
        workspace=request['workspace_id']; receipt=request['expected_receipt']
        native, binding = resolve_query_binding(repository=repository, auth_manager=auth_manager,
            owner=owner, workspace_id=workspace, purpose='app_folder')
        root=os.path.abspath(native['root'])
        if os.path.realpath(root)!=root: raise ValueError('Recorded impact unavailable')
        def check():
            if current() is not True or auth_manager.account_id(owner)!=account:
                raise ValueError('Recorded impact unavailable')
            _, now=resolve_query_binding(repository=repository,auth_manager=auth_manager,
                owner=owner,workspace_id=workspace,purpose='app_folder')
            if now!=binding: raise ValueError('Recorded impact unavailable')
            return True
        async def capture():
            check()
            value=await capture_recorded_after(request['locator'],request['resource_id'],client=history_client,
                account=account,context=history_context,provider=files_provider,current=check)
            check()
            return value
        async def query(value):
            check()
            begin_query(cost)
            result=await query_code(repository=repository,provider=provider,auth_manager=auth_manager,
                owner=owner,workspace_id=workspace,purpose='app_folder',session=None,query=value)
            record_cost(cost,query=result); check()
            return result
        if type(deadline) not in (int, float) or deadline <= time.monotonic():
            raise ValueError('Recorded impact unavailable')
        async with asyncio.timeout(min(30, deadline - time.monotonic())):
            first=await capture(); record_cost(cost,history_bytes=first['size'])
            first['bytes'].decode('utf-8','strict')
            path=first['qualified']['path']
            if type(path) is not str or not os.path.isabs(path) or os.path.realpath(path)!=path:
                raise ValueError('Recorded impact unavailable')
            if os.path.commonpath((root,path))!=root or path==root:
                raise ValueError('Recorded impact unavailable')
            rel=Path(os.path.relpath(path,root)).as_posix()
            read={'operation':'read','read':{'rel_path':rel,'start_byte':0,'length':512},
                  'fields':['source','excerpt','properties'],'channels':[], 'expected_receipt':receipt}
            initial=await query(read)
            observation=validate_comparison_read(initial,receipt=receipt,rel_path=rel)
            after=first['qualified']['item']['after']
            recorded={'action_id':after['action_id'],'version_id':after['version_id'],
                'sha256':first['sha256'],'size_bytes':first['size']}
            retrospective={'state':'unavailable'}
            provenance=None
            if (observation['index']=='current' and first['size']<=32768
                    and (observation['sha256'],observation['size_bytes'])!=(first['sha256'],first['size'])):
                try:
                    current_identity=validate_read(initial,receipt=receipt,rel_path=rel,sha256=observation['sha256'],size=observation['size_bytes'])
                    current_outline={'operation':'outline','seed_ids':[current_identity['file_id']], 'filter':{'rel_path':rel},
                        'fields':['label','source','signature','properties'],'channels':[], 'expected_receipt':receipt,
                        'budgets':{'rows':200,'bytes':262144,'tokens':8192,'hops':0,'work':MAX_QUERY_WORK}}
                    current_result=await query(current_outline)
                    current_ids=validate_outline(current_result,receipt=receipt,rel_path=rel,file_id=current_identity['file_id'],indexed_blake3=current_identity['indexed_blake3'])
                    if any(row.get('properties',{}).get('identity_state')!='stable' for row in current_result['hits']): raise ValueError('Ambiguous declarations')
                    private={'version':1,'receipt':receipt,'file_id':current_identity['file_id'],'rel_path':rel,
                        'source_sha256':first['sha256'],'source_utf8':first['bytes'].decode('utf-8','strict')}
                    check();begin_query(cost)
                    reconstructed=await provider.code_query(binding=native,query=None,_retrospective_request=private)
                    record_cost(cost,query=reconstructed);check()
                    if (type(reconstructed) is not dict or set(reconstructed)!={'version','receipt','file_id','source_sha256','extractor_fingerprint','complete','declarations','cost'}
                            or reconstructed['version']!=1 or reconstructed['receipt']!=receipt or reconstructed['file_id']!=current_identity['file_id']
                            or reconstructed['source_sha256']!=first['sha256'] or reconstructed['complete'] is not True
                            or not isinstance(reconstructed['extractor_fingerprint'],str) or not re.fullmatch(r'[0-9a-f]{64}',reconstructed['extractor_fingerprint'])
                            or reconstructed['extractor_fingerprint']!=receipt['config_fingerprint']): raise ValueError('Retrospective evidence unavailable')
                    entries=reconstructed['declarations']
                    if (type(entries) is not list or not 1<=len(entries)<=200 or any(type(x) is not dict or set(x)!={'id','identity_state'}
                            or x['identity_state']!='stable' or not isinstance(x['id'],str) or not re.fullmatch(r'decl:[0-9a-f]{64}',x['id']) for x in entries)):
                        raise ValueError('Retrospective evidence unavailable')
                    recorded_ids={x['id'] for x in entries}; now_ids=set(current_ids)
                    if len(recorded_ids)!=len(entries): raise ValueError('Retrospective evidence unavailable')
                    retrospective={'state':'complete','basis':'retrospective_current_parser','matched':len(recorded_ids & now_ids),
                        'recorded_only':sorted(recorded_ids-now_ids),'current_only':sorted(now_ids-recorded_ids)}
                    selection=json.dumps([sorted(recorded_ids),sorted(now_ids)],separators=(',', ':')).encode()
                    provenance={
                        'basis':'retrospective_current_parser',
                        'recorded':recorded,
                        'current':{'file_id':current_identity['file_id'],'sha256':observation['sha256'],
                            'size_bytes':observation['size_bytes'],'indexed_blake3':current_identity['indexed_blake3']},
                        'extractor_fingerprint':reconstructed['extractor_fingerprint'],
                        'receipt':receipt,
                        'declaration_sets_sha256':hashlib.sha256(selection).hexdigest(),
                    }
                except Exception:
                    retrospective={'state':'unavailable'}
                    provenance=None
            if (observation['sha256'],observation['size_bytes'])!=(first['sha256'],first['size']) or observation['index']!='current':
                last=await capture(); record_cost(cost,history_bytes=last['size'])
                if last!=first:raise ValueError('Recorded comparison unavailable')
                final_observation=validate_comparison_read(await query(read),receipt=receipt,rel_path=rel)
                if final_observation!=observation:raise ValueError('Recorded comparison unavailable')
                check()
                result=comparison_result(request,recorded_after=recorded,observation=observation,cost=cost)
                result['retrospective_declarations']=retrospective
                if provenance is not None: result['retrospective_provenance']=provenance
                return result
            identity=validate_read(initial,receipt=receipt,rel_path=rel,sha256=first['sha256'],size=first['size'])
            outline={'operation':'outline','seed_ids':[identity['file_id']], 'filter':{'rel_path':rel},
                'fields':['label','source','signature','properties'],'channels':[], 'expected_receipt':receipt,
                'budgets':{'rows':200,'bytes':262144,'tokens':8192,'hops':0,'work':MAX_QUERY_WORK}}
            declarations=validate_outline(await query(outline),receipt=receipt,rel_path=rel,
                file_id=identity['file_id'],indexed_blake3=identity['indexed_blake3'])
            native_query,public_query,budget=impact_query(declarations,receipt)
            impact=await query(native_query)
            if type(impact) is not dict or impact.get('receipt')!=receipt or impact.get('error') is not None:
                raise ValueError('Recorded impact unavailable')
            last=await capture(); record_cost(cost,history_bytes=last['size'])
            if last!=first: raise ValueError('Recorded impact unavailable')
            final=validate_read(await query(read),receipt=receipt,rel_path=rel,sha256=first['sha256'],size=first['size'])
            if final!=identity: raise ValueError('Recorded impact unavailable')
            after=first['qualified']['item']['after']
            recorded={'action_id':after['action_id'],'version_id':after['version_id'],
                'sha256':first['sha256'],'size_bytes':first['size']}
            source={'rel_path':rel,**identity,'sha256':first['sha256'],'size_bytes':first['size']}
            check()
            result=success_result(request,recorded_after=recorded,source=source,query=public_query,
                impact_result=impact,caller_budget=budget,cost=cost)
            result['comparison']=comparison_result(request,recorded_after=recorded,observation=observation,cost=cost)['comparison']
            result['retrospective_declarations']=retrospective
            return result
    except asyncio.CancelledError:
        raise
    except Exception:
        return unavailable_result(cost)
