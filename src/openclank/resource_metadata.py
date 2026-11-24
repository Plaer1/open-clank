"""Bounded shared names/domain-reference envelope; it grants no authority."""
from __future__ import annotations
import base64
import json
import math


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate metadata key')
        result[key]=value
    return result


def validate_metadata(value, *, allow_policy_overrides=False, allow_connection=False):
    allowed={'name','aliases','domain_refs','types','processing_role'} | ({'policy_overrides'} if allow_policy_overrides is True else set()) | ({'connection'} if allow_connection is True else set())
    if type(value) is not dict or set(value)-allowed:raise ValueError('unsupported metadata')
    def item(v):return type(v) is str and bool(v) and v==v.strip() and len(v.encode('utf-8'))<=512
    result={}
    for key in ('name','processing_role'):
        if key in value:
            if not item(value[key]):raise ValueError('invalid metadata value')
            result[key]=value[key]
    for key in ('aliases','types'):
        if key in value:
            values=value[key]
            if type(values) is not list or len(values)>64 or any(not item(x) for x in values) or len(set(values))!=len(values):raise ValueError('invalid metadata list')
            result[key]=list(values)
    if 'domain_refs' in value:
        refs=value['domain_refs'];seen=set()
        if type(refs) is not list or len(refs)>64:raise ValueError('invalid domain references')
        result['domain_refs']=[]
        for ref in refs:
            if type(ref) is not dict or set(ref)!={'block_id','revision'} or not item(ref['block_id']) or type(ref['revision']) is not int or ref['revision']<1:raise ValueError('invalid domain reference')
            key=(ref['block_id'],ref['revision'])
            if key in seen:raise ValueError('duplicate domain reference')
            seen.add(key);result['domain_refs'].append(dict(ref))
    if 'connection' in value:
        c=value['connection']
        if type(c) is not dict or set(c)!={'version','subject','predicate_version','predicate_sha256'} or type(c['version']) is not int or c['version']!=1 or type(c['predicate_version']) is not int or c['predicate_version']<1:raise ValueError('invalid connection metadata')
        subject=c['subject']; digest=c['predicate_sha256']
        if type(subject) is not dict or set(subject)!={'block_id','revision'} or not item(subject['block_id']) or type(subject['revision']) is not int or subject['revision']<1 or type(digest) is not str or len(digest)!=64 or any(x not in '0123456789abcdef' for x in digest):raise ValueError('invalid connection metadata')
        result['connection']={'version':1,'subject':dict(subject),'predicate_version':c['predicate_version'],'predicate_sha256':digest}
    if 'policy_overrides' in value:
        overrides=value['policy_overrides']
        if type(overrides) is not dict or set(overrides)-{'importance','connections','consolidation'}:raise ValueError('invalid policy overrides')
        result['policy_overrides']={}
        for axis,entry in overrides.items():
            if axis=='importance':
                if entry is not None and (type(entry) not in (int,float) or not math.isfinite(entry) or not 0<=entry<=1):raise ValueError('invalid policy override')
            elif entry is not None and type(entry) is not bool:raise ValueError('invalid policy override')
            result['policy_overrides'][axis]=entry
    return result


def validate_envelope(value):
    if type(value) is not dict or set(value)!={'version','metadata','provenance'} or type(value['version']) is not int or value['version']!=1:raise ValueError('invalid metadata envelope')
    claim=value['provenance']
    if (type(claim) is not dict or set(claim)!={'origin','actor'} or claim['origin'] not in ('manual','inferred','inherited','legacy_unknown')
        or type(claim['actor']) is not str or not claim['actor'] or claim['actor']!=claim['actor'].strip() or len(claim['actor'].encode())>512):raise ValueError('invalid metadata provenance')
    # Frontmatter provenance is a claim; only a separate trusted receipt grants authority.
    return {'version':1,'metadata':validate_metadata(value['metadata']),'provenance':dict(claim)}


def encode_envelope(value):
    data=json.dumps(validate_envelope(value),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')
    if len(data)>16384:raise ValueError('metadata envelope too large')
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def decode_envelope(encoded):
    if type(encoded) is not str or not encoded or len(encoded)>21846 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_' for c in encoded):raise ValueError('invalid metadata envelope')
    try:
        raw=base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4))
        if len(raw)>16384:raise ValueError('metadata envelope too large')
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique)
        result=validate_envelope(value)
        if encode_envelope(result)!=encoded:raise ValueError('noncanonical metadata envelope')
        return result
    except (UnicodeError,ValueError,TypeError) as exc:raise ValueError('invalid metadata envelope') from exc
