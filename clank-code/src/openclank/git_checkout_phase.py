"""Unexposed checkout phase ordering for one already-admitted managed utility.

Only the managed callback owner may construct this with a verifier tied to the
original UtilityLifetime and immutable Native action. This object does not
create authority or verify Git acknowledgements independently.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re


def _same(a, b):
    return type(a) is str and type(b) is str and hmac.compare_digest(a, b)


class CheckoutPhase:
    def __init__(self, *, custody, verify, identity, prepare, phase, count,
                 source_head, source_ref, source_index, target_head, target_ref, target_index):
        if (type(identity) is not dict or set(identity) != {'operationID','requestDigest','capability','actionSHA256','gitPlanDigest','filesRequestDigest'}
                or any(type(v) is not str or not v for v in identity.values())
                or type(prepare) is not dict or type(phase) is not dict or type(count) is not int or not 1 <= count <= 64
                or set(phase) != {'operation_id','request_digest','git_plan_digest','root_id','root_object_tag'}
                or any(prepare.get(k)!=phase.get(k) for k in phase)
                or phase.get('operation_id') != identity.get('operationID')
                or phase.get('request_digest') != identity.get('filesRequestDigest')
                or phase.get('git_plan_digest') != identity.get('gitPlanDigest')
                or type(prepare.get('items')) is not list or len(prepare['items']) != count
                or not callable(verify) or any(type(x) is not str or not re.fullmatch(r'[0-9a-f]{64}',x) for x in (source_index,target_index))
                or any(type(x) is not str or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',x) for x in (source_head,target_head))
                or any(x is not None and (type(x) is not str or not x.startswith('refs/heads/')) for x in (source_ref,target_ref))):
            raise ValueError('checkout phase unavailable')
        self.custody, self.verify = custody, verify
        self.identity, self.prepare, self.phase = (json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
                                                   for value in (identity, prepare, phase))
        self.count = count
        self.source = (source_head,source_ref,source_index)
        self.target = (target_head,target_ref,target_index)
        self.state, self.next, self.invocation = 'new', 0, None
        self.final_outcome, self.cleanup = None, None
        self.lock = asyncio.Lock()

    async def dispatch(self, request):
        unknown = {'state':'unknown'}
        async with self.lock:
            if (type(request) is not dict or set(request) != {'version','invocationID','event'} | set(self.identity)
                    or type(request.get('version')) is not int or request['version'] != 1 or type(request.get('invocationID')) is not str
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',request['invocationID'])
                    or any(not _same(request.get(k),v) for k,v in self.identity.items())
                    or (self.invocation is not None and not _same(self.invocation,request['invocationID']))): return {'state':'refused'}
            if type(request.get('event')) is not dict:
                self.state='unknown';return unknown
            try:
                if await self.verify() is not True: return {'state':'refused'}
                event=request['event'];kind=event.get('kind')
                if self.state == 'new' and event == {'kind':'prepare'}:
                    self.invocation=request['invocationID']
                    data=(await self.custody.exchange('checkout_prepare',self.prepare)).get('data')
                    if type(data) is not dict or data.get('status')!='pending' or data.get('next_ordinal')!=0: raise ValueError('prepare outcome')
                    self.state='applying';return {'state':'ready'}
                if self.state == 'applying' and set(event)=={'kind','ordinal'} and kind=='step' and type(event['ordinal']) is int and event['ordinal']==self.next:
                    payload={**self.phase,'ordinal':self.next}
                    data=(await self.custody.exchange('checkout_step',payload)).get('data')
                    self.next+=1
                    expected='files_applied' if self.next==self.count else 'pending'
                    if type(data) is not dict or data.get('status')!=expected or data.get('next_ordinal')!=self.next: raise ValueError('step outcome')
                    if self.next==self.count:
                        if await self.verify() is not True: raise ValueError('authority changed')
                        data=(await self.custody.exchange('checkout_status',{**self.phase,'verify_applied':True})).get('data')
                        if type(data) is not dict or data.get('status')!='files_applied' or data.get('verification')!='verified': raise ValueError('verification unavailable')
                        self.state='applied'
                    return {'state':'applied'}
                if self.state=='applied' and set(event)=={'kind','indexSHA256'} and kind=='before_ref':
                    if event['indexSHA256']!=self.target[2]:
                        self.state='unknown';return unknown
                    data=(await self.custody.exchange('checkout_status',{**self.phase,'verify_applied':True})).get('data')
                    if (type(data) is not dict or data.get('status')!='files_applied'
                            or data.get('verification')!='verified' or await self.verify() is not True):
                        self.state='unknown';return unknown
                    self.state='ref_ready';return {'state':'accepted'}
                keys={'kind','index','ref','headOID','headRef','indexSHA256'}
                if self.state in ('applying','applied','ref_ready') and set(event)==keys and kind=='failure' and event['index'] in ('unchanged','restored') and event['ref'] in ('unchanged','restored'):
                    if (event['headOID'],event['headRef'],event['indexSHA256']) != self.source:
                        self.state='unknown';return unknown
                    self.state='rollback';return {'state':'accepted'}
                if self.state=='rollback' and set(event)=={'kind','ordinal'} and kind=='rollback' and type(event['ordinal']) is int and event['ordinal']==self.next-1:
                    head,ref,index=self.source
                    observation={'result':'compensated','index_sha256':index,'head_ref':ref,'head_oid':head}
                    data=(await self.custody.exchange('checkout_rollback',{**self.phase,'ordinal':self.next-1,'git_observation':observation})).get('data')
                    self.next-=1
                    if type(data) is not dict or data.get('status')!='rollback_in_progress': raise ValueError('rollback outcome')
                    return {'state':'restored'}
                if set(event)=={'kind','headOID','headRef','indexSHA256'} and kind=='finalize':
                    target=self.target if self.state=='ref_ready' else self.source if self.state=='rollback' and self.next==0 else None
                    if target is None or (event['headOID'],event['headRef'],event['indexSHA256']) != target:
                        self.state='unknown';return unknown
                    head,ref,index=target
                    observation={'result':'committed' if self.state=='ref_ready' else 'compensated','index_sha256':index,'head_ref':ref,'head_oid':head}
                    data=(await self.custody.exchange('checkout_finalize',{**self.phase,'git_observation':observation})).get('data')
                    expected='complete' if self.state=='ref_ready' else 'rolled_back'
                    if type(data) is not dict or data.get('status')!=expected or data.get('cleanup') not in ('complete','pending'): raise ValueError('final outcome')
                    self.final_outcome=observation['result'];self.cleanup=data['cleanup']
                    self.state='complete';return {'state':'complete'}
            except Exception:
                self.state='unknown'
            else:
                self.state='unknown'
            return unknown
