"""Bounded same-connection verification of Native owner-custody facts.

The caller owns account authentication and the SQLite transaction; this module
supplies no account, source or mutation authority.
"""
import hashlib
import json
import re
import time

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_REQUEST = ('version','database_id','account_id','operation_id','step','generation','action','owner','new_owner',
            'expected_source_fingerprint','expected_target_fingerprint','prior_receipt_id','prior_receipt_hash','rag_relocation')
_HEAD = {'version','database_id','account_id','anchor_owner','current_owner','link_id','link_sha256'}
_LINK = {'version','request','receipt_id','receipt_sha256','previous_id','previous_sha256'}
_MAX_STEPS = 100_000
_MAX_BYTES = 16 * 1024 * 1024
_MAX_SECONDS = 5.0


def _compact(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _text(value):
    return type(value) is str and bool(value) and value.strip() == value and len(value.encode('utf-8')) <= 512 and not any(ord(c)<32 or ord(c)==127 for c in value)


def _hex(value):
    return type(value) is str and _HEX.fullmatch(value) is not None


def _json(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('duplicate custody field')
            result[key]=value
        return result
    if type(raw) is not str or len(raw.encode('utf-8')) > 65536:
        raise ValueError('custody data unavailable')
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('invalid JSON')))


def _meta(db,key):
    row=db.execute('SELECT length(CAST(value AS BLOB)) FROM fm_meta WHERE key=?',(key,)).fetchone()
    if row is None: return None
    if type(row[0]) is not int or row[0] < 0 or row[0] > 65536: raise ValueError('custody data unavailable')
    raw=db.execute('SELECT value FROM fm_meta WHERE key=?',(key,)).fetchone()
    if raw is None or type(raw[0]) is not str or len(raw[0].encode('utf-8')) != row[0]: raise ValueError('custody data changed')
    return raw[0]


def _receipt(db, request, receipt_id, expected_hash):
    if type(request) is not dict or set(request) != set(_REQUEST): raise ValueError('custody request unavailable')
    request={key:request[key] for key in _REQUEST}
    if (type(request['version']) is not int or request['version'] != 1 or type(request['generation']) is not int or not 1 <= request['generation'] <= (1 << 64) - 1
        or request['action'] not in ('forward','reverse') or request['owner']==request['new_owner']
        or not all(_text(request[k]) for k in ('database_id','account_id','operation_id','step','owner','new_owner'))
        or not all(_hex(request[k]) for k in ('expected_source_fingerprint','expected_target_fingerprint'))):
        raise ValueError('custody request unavailable')
    relocation=request['rag_relocation']
    if relocation is not None:
        if type(relocation) is not dict or set(relocation) != {'from','to'} or not all(_text(relocation[k]) for k in ('from','to')):
            raise ValueError('custody relocation unavailable')
        relocation={'from':relocation['from'],'to':relocation['to']}; request['rag_relocation']=relocation
    key=[request[k] for k in ('database_id','account_id','operation_id','step','generation','action')]
    if receipt_id != 'owner_transition_'+_hash(_compact(key)) or not _hex(expected_hash): raise ValueError('custody receipt unavailable')
    raw=_meta(db,receipt_id)
    if raw is None or len(raw.encode('utf-8')) > 16384 or _hash(raw.encode('utf-8')) != expected_hash: raise ValueError('custody receipt unavailable')
    value=_json(raw)
    original,target=(request['owner'],request['new_owner']) if request['action']=='forward' else (request['new_owner'],request['owner'])
    binding=[request['database_id'],request['account_id'],request['operation_id'],request['step'],original,target]
    moved=None if relocation is None else ([relocation['from'],relocation['to']] if request['action']=='forward' else [relocation['to'],relocation['from']])
    if (type(value) is not dict or set(value) != {'version','receipt_id','request_sha256','binding_sha256','relocation_sha256','action','result'}
        or type(value['version']) is not int or value['version'] != 1 or value['receipt_id'] != receipt_id or value['request_sha256'] != _hash(_compact(request))
        or value['binding_sha256'] != _hash(_compact(binding)) or value['relocation_sha256'] != _hash(_compact(moved))
        or value['action'] != request['action'] or type(value['result']) is not dict): raise ValueError('custody receipt changed')
    return request, len(raw.encode('utf-8'))


def _verified_owner_custody(db, *, database_id, account_id, current_owner):
    """Return (newest-first owner tuple, has_head), using this transaction only.

    An absent head returns only the current label; it does not certify older
    history. The caller must not use it to infer a historical owner.
    """
    deadline=time.monotonic()+_MAX_SECONDS
    if not db.in_transaction or not all(_text(v) for v in (database_id,account_id,current_owner)):
        raise ValueError('custody transaction unavailable')
    row=db.execute("SELECT value FROM fm_meta WHERE key='database_id'").fetchone()
    if row is None or row[0] != database_id: raise ValueError('custody database changed')
    digest=_hash(_compact(['s04.owner-custody-head.v1',database_id,account_id]))
    head_key='owner_custody_head_'+digest; prefix='owner_custody_link_'+digest+'_'
    keys=db.execute('SELECT key FROM fm_meta WHERE key>? AND key<? ORDER BY key LIMIT ?', (prefix,prefix+'~',_MAX_STEPS+1)).fetchall()
    if len(keys)>_MAX_STEPS or time.monotonic()>=deadline: raise ValueError('custody chain exceeds bounds')
    keys={row[0] for row in keys}
    key_bytes=sum(len(k.encode('utf-8')) for k in keys if type(k) is str)
    if key_bytes>_MAX_BYTES or time.monotonic()>=deadline: raise ValueError('custody chain exceeds bounds')
    if any(type(k) is not str or not k.startswith(prefix) or not _hex(k[len(prefix):]) for k in keys): raise ValueError('custody index changed')
    raw=_meta(db,head_key)
    if raw is None:
        if keys: raise ValueError('orphan custody links')
        return (current_owner,),False,(),()
    head=_json(raw)
    if (type(head) is not dict or set(head)!=_HEAD or head['version']!='s04.owner-custody-head.v1'
        or head['database_id']!=database_id or head['account_id']!=account_id or head['current_owner']!=current_owner
        or not _text(head['anchor_owner']) or not _hex(head['link_sha256'])): raise ValueError('custody head unavailable')
    expected_owner=current_owner; cursor=(head['link_id'],head['link_sha256']); seen=set(); links=[]; used=key_bytes+len(raw.encode('utf-8')); steps=len(keys)
    while cursor is not None:
        key, expected_hash=cursor
        if type(key) is not str or key in seen or key not in keys or not _hex(expected_hash): raise ValueError('custody link unavailable')
        seen.add(key);raw=_meta(db,key)
        if raw is None or _hash(raw.encode('utf-8'))!=expected_hash: raise ValueError('custody link changed')
        used+=len(raw.encode('utf-8'))
        steps += 5
        if used>_MAX_BYTES or steps>_MAX_STEPS or time.monotonic()>=deadline: raise ValueError('custody chain exceeds bounds')
        link=_json(raw)
        if (type(link) is not dict or set(link)!=_LINK or link['version']!='s04.owner-custody-link.v1'
            or type(link['receipt_id']) is not str or not link['receipt_id'].startswith('owner_transition_')
            or key != prefix+_hash(link['receipt_id'].encode('utf-8')) or not _hex(link['receipt_sha256'])
            or (link['previous_id'] is None)!=(link['previous_sha256'] is None)):
            raise ValueError('custody link unavailable')
        request,receipt_bytes=_receipt(db,link['request'],link['receipt_id'],link['receipt_sha256'])
        used += receipt_bytes
        if used>_MAX_BYTES or time.monotonic()>=deadline: raise ValueError('custody chain exceeds bounds')
        if request['database_id']!=database_id or request['account_id']!=account_id or request['new_owner']!=expected_owner:
            raise ValueError('custody owner changed')
        links.append((request,link)); expected_owner=request['owner']
        cursor=None if link['previous_id'] is None else (link['previous_id'],link['previous_sha256'])
    if seen != keys or expected_owner != head['anchor_owner'] or not links: raise ValueError('custody chain incomplete')
    for index,(request,_link) in enumerate(links):
        steps += 1
        if steps>_MAX_STEPS or time.monotonic()>=deadline: raise ValueError('custody chain exceeds bounds')
        previous=links[index+1] if index+1<len(links) else None
        if previous is None:
            if request['action']!='forward' or request['generation']!=1 or request['prior_receipt_id'] is not None or request['prior_receipt_hash'] is not None: raise ValueError('custody origin unavailable')
        elif request['action']=='forward' and request['generation']==1:
            if request['prior_receipt_id'] is not None or request['prior_receipt_hash'] is not None: raise ValueError('custody predecessor changed')
        else:
            old,old_link=previous
            wanted=(request['generation'],'forward') if request['action']=='reverse' else (request['generation']-1,'reverse')
            if (request['generation']<1 or old['operation_id']!=request['operation_id'] or old['step']!=request['step']
                or (old['generation'],old['action'])!=wanted or request['prior_receipt_id']!=old_link['receipt_id']
                or request['prior_receipt_hash']!=old_link['receipt_sha256']): raise ValueError('custody predecessor changed')
    descriptors=tuple({**{key:request[key] for key in ('operation_id','step','generation','action','owner','new_owner')},
        'receipt_id':link['receipt_id'],'receipt_sha256':link['receipt_sha256']} for request,link in links)
    return tuple(dict.fromkeys([current_owner]+[entry[0]['owner'] for entry in links])),True,descriptors,tuple((request,link['receipt_id'],link['receipt_sha256']) for request,link in links)


def verified_owner_custody(db, *, database_id, account_id, current_owner):
    owners,has_head,_descriptors,_members=_verified_owner_custody(db,database_id=database_id,account_id=account_id,current_owner=current_owner)
    return owners,has_head


def verified_owner_custody_descriptors(db, *, database_id, account_id, current_owner):
    """Return verified non-path operation references for existing lifecycle owners."""
    _owners,_has_head,descriptors,_members=_verified_owner_custody(db,database_id=database_id,account_id=account_id,current_owner=current_owner)
    return descriptors


def verified_owner_custody_membership(db, *, database_id, account_id, current_owner):
    """Return a transaction-local exact-membership predicate, never receipt paths.

    The synchronous caller must use it within the same open read transaction.
    This is historical fact only and carries no current mutation authority.
    """
    _owners,_has_head,_descriptors,members=_verified_owner_custody(db,database_id=database_id,account_id=account_id,current_owner=current_owner)
    def contains(transition,receipt_id,receipt_sha256):
        if not db.in_transaction:raise ValueError('custody transaction unavailable')
        return any(request==transition and rid==receipt_id and digest==receipt_sha256 for request,rid,digest in members)
    return contains


def verified_owner_custody_phase_context(db, *, database_id, account_id, current_owner):
    """Return current owners and a bounded exact contiguous phase matcher.

    The synchronous caller must use the matcher in this same read transaction.
    """
    owners,_has_head,_descriptors,members=_verified_owner_custody(db,database_id=database_id,account_id=account_id,current_owner=current_owner)
    fields=('database_id','account_id','operation_id','step','generation','action','owner','new_owner')
    phases=tuple({'phase':{key:request[key] for key in fields},'request_sha256':_hash(_compact(request)),
                  'receipt_id':rid,'receipt_hash':digest} for request,rid,digest in members)
    phase_index={};legacy_index={};owner_index={}
    for i,(request,rid,digest) in enumerate(members):
        phase_index.setdefault((rid,digest),[]).append(i)
        legacy_index.setdefault((request['account_id'],request['operation_id'],request['owner'],request['new_owner']),[]).append(i)
        owner_index.setdefault(request['owner'],[]).append(i)
    def locate_segment(segment):
        if not db.in_transaction:raise ValueError('custody transaction unavailable')
        if type(segment) is not list or not segment or type(segment[0]) is not dict:return None
        first=segment[0];matches=phase_index.get((first.get('receipt_id'),first.get('receipt_hash')),())
        if len(matches)!=1:return None
        i=matches[0]
        return i if list(phases[i:i+len(segment)])==segment else None
    def locate_legacy(account,operation,old_owner,new_owner):
        if not db.in_transaction:raise ValueError('custody transaction unavailable')
        matches=legacy_index.get((account,operation,old_owner,new_owner),())
        return matches[0] if len(matches)==1 else None
    def locate_owner(owner):
        if not db.in_transaction:raise ValueError('custody transaction unavailable')
        matches=owner_index.get(owner,())
        return matches[0] if len(matches)==1 else None
    return owners,locate_segment,locate_legacy,locate_owner


def verified_owner_custody_head(db, *, database_id, account_id, current_owner):
    """Return the exact verified head for an internal same-transaction builder.

    No head means the account index was positively empty. This is historical
    evidence only; it is not a current source or write grant.
    """
    _owners,has_head,_descriptors,members=_verified_owner_custody(db,database_id=database_id,account_id=account_id,current_owner=current_owner)
    if not has_head:return None
    if not members:raise ValueError('custody head unavailable')
    request,receipt_id,receipt_sha256=members[0]
    return {'request':json.loads(_compact(request)),'receipt_id':receipt_id,'receipt_hash':receipt_sha256}
