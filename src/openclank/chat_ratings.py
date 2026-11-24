"""Explicit human ratings over exact committed host message revisions.

The optional SQLite schema is prepared offline. This module never creates or
repairs it and never treats a rating as memory or learning evidence.
"""
from __future__ import annotations
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from sqlalchemy import text
from src.openclank.chat_ratings_schema import CREATE, CREATE_V1, TABLE

_HASH=re.compile(r'[0-9a-f]{64}\Z')

class RatingUnavailable(ValueError): pass
class RatingConflict(ValueError): pass


def _shape(value):
    return ' '.join((value or '').rstrip(';').split()).lower()


def schema_state(db):
    if db.get_bind().dialect.name!='sqlite': return 'unavailable'
    row=db.execute(text("SELECT type,sql FROM sqlite_schema WHERE name=:name"),{'name':TABLE}).first()
    if row is None: return 'absent'
    if row[0]=='table' and _shape(row[1])==_shape(CREATE):return 'ready'
    if row[0]=='table' and _shape(row[1])==_shape(CREATE_V1):return 'outdated'
    return 'unavailable'


def _id(value):
    return type(value) is str and bool(value) and value==value.strip() and len(value.encode('utf-8'))<=512


def _current(db, *, owner, account, chat_id, message_id, auth):
    from src.openclank.chat_source_bridge import available, digest
    if not all(_id(x) for x in (owner,account,chat_id,message_id)) or not available(db):
        raise RatingUnavailable('message source unavailable')
    if auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:
        raise RatingUnavailable('account unavailable')
    row=db.execute(text("SELECT s.owner,s.workspace_id,m.role,m.content,m.source_body_revision,m.source_body_hash FROM sessions s JOIN chat_messages m ON m.session_id=s.id WHERE s.id=:chat AND m.id=:message"),{'chat':chat_id,'message':message_id}).first()
    if (not row or row[0]!=owner or row[2] not in ('user','assistant') or type(row[3]) is not str
        or len(row[3].encode('utf-8'))>262144 or type(row[4]) is not int or row[4]<1
        or type(row[5]) is not str or not _HASH.fullmatch(row[5]) or digest(row[3])!=row[5]
        or (row[1] is not None and not _id(row[1]))):
        raise RatingUnavailable('message source unavailable')
    return {'message_id':message_id,'role':row[2],'workspace_id':row[1],'source_revision':row[4],'source_hash':row[5],'content':row[3]}


def _json_descriptor(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)


def _descriptor_hash(value):
    return hashlib.sha256(_json_descriptor(value).encode('utf-8')).hexdigest()


def _message_descriptor(source):
    return {'version':1,'kind':'message','scope':'committed_host_text',
            'message_id':source['message_id'],'role':source['role'],'workspace_id':source['workspace_id'],
            'source_revision':source['source_revision'],'source_hash':source['source_hash']}


def host_text_snapshot(db, *, owner, account, chat_id, auth):
    """Bounded complete host-row sequence, not complete native/visible history."""
    from src.openclank.chat_source_bridge import available, digest
    if not all(_id(v) for v in (owner,account,chat_id)) or not available(db):raise RatingUnavailable('source unavailable')
    if auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise RatingUnavailable('account unavailable')
    chat=db.execute(text('SELECT owner,workspace_id,mimo_state FROM sessions WHERE id=:chat'),{'chat':chat_id}).first()
    if not chat or chat[0]!=owner or (chat[1] is not None and not _id(chat[1])):raise RatingUnavailable('chat unavailable')
    try:
        state=json.loads(chat[2]) if isinstance(chat[2],str) else (chat[2] or {})
        from src.openclank.session_operations import visibility
        hidden=visibility(state,require_settled=True)
    except Exception as exc:raise RatingUnavailable('history boundary unavailable') from exc
    rows=db.execute(text('SELECT id,role,timestamp,length(CAST(content AS BLOB)),length(CAST(metadata AS BLOB)),source_body_revision,source_body_hash FROM chat_messages WHERE session_id=:chat ORDER BY timestamp,id LIMIT 257'),{'chat':chat_id}).all()
    if len(rows)>256:raise RatingUnavailable('host text coverage unavailable')
    total=0; items=[]
    for row in rows:
        mid,role,stamp,size,metadata_size,revision,source_hash=row
        if (not _id(mid) or role not in ('user','assistant') or stamp is None or type(size) is not int or size<0 or size>262144
            or (metadata_size is not None and (type(metadata_size) is not int or metadata_size>16384)) or type(revision) is not int or revision<1 or type(source_hash) is not str or not _HASH.fullmatch(source_hash) or mid in hidden):
            raise RatingUnavailable('host text coverage unavailable')
        total+=size
        if total>1048576:raise RatingUnavailable('host text coverage unavailable')
        metadata=db.execute(text('SELECT metadata FROM chat_messages WHERE session_id=:chat AND id=:id'),{'chat':chat_id,'id':mid}).scalar()
        try: meta=json.loads(metadata) if metadata else {}
        except (ValueError,TypeError) as exc:raise RatingUnavailable('host text coverage unavailable') from exc
        if type(meta) is not dict or meta.get('hidden') or meta.get('managed_native') or meta.get('attachments'):
            raise RatingUnavailable('host text coverage unavailable')
        body=db.execute(text('SELECT content FROM chat_messages WHERE session_id=:chat AND id=:id'),{'chat':chat_id,'id':mid}).scalar()
        if type(body) is not str or len(body.encode('utf-8'))!=size or digest(body)!=source_hash:raise RatingUnavailable('host text changed')
        items.append({'message_id':mid,'role':role,'timestamp':str(stamp),'source_revision':revision,'source_hash':source_hash,'text':body})
    return {'version':1,'scope':'committed_host_text_sequence','workspace_id':chat[1],'items':items}


def target_descriptor(snapshot, kind, segments=None):
    if kind not in ('chat','section'):raise RatingUnavailable('invalid target')
    items=snapshot['items']; heads=[{k:item[k] for k in ('message_id','role','timestamp','source_revision','source_hash')} for item in items]
    base={'version':1,'kind':kind,'scope':'committed_host_text_sequence','workspace_id':snapshot['workspace_id']}
    if kind=='chat':
        if segments is not None:raise RatingUnavailable('invalid target')
        return {**base,'messages':heads}
    if type(segments) is not list or not segments or len(segments)>256:raise RatingUnavailable('invalid segments')
    positions={item['message_id']:i for i,item in enumerate(items)}; result=[]; prior=None
    for segment in segments:
        if type(segment) is not dict or set(segment)!={'message_id','start','end'}:raise RatingUnavailable('invalid segments')
        index=positions.get(segment['message_id']);start=segment['start'];end=segment['end']
        if index is None or type(start) is not int or type(end) is not int:raise RatingUnavailable('invalid segments')
        raw=items[index]['text'].encode('utf-8')
        if start<0 or end<=start or end>len(raw):raise RatingUnavailable('invalid segments')
        try:raw[:start].decode('utf-8');raw[:end].decode('utf-8')
        except UnicodeDecodeError as exc:raise RatingUnavailable('invalid UTF-8 boundary') from exc
        if prior is not None and (index!=prior[0]+1 or prior[2]!=len(items[prior[0]]['text'].encode('utf-8')) or start!=0):
            raise RatingUnavailable('noncontiguous segments')
        result.append({**heads[index],'start':start,'end':end});prior=(index,start,end)
    return {**base,'segments':result}


def _policy(owner):
    from src.openclank.memory_policy import STORE, policy_view
    snap=STORE.snapshot(owner)
    ratings=policy_view(snap['document'])['ratings']
    if (type(ratings) is not dict or ratings.get('visible') is not True or type(ratings.get('collection_enabled')) is not bool or ratings.get('explicit_submission_only') is not True):
        raise RatingUnavailable('rating policy unavailable')
    return ratings


def _latest(db,account,chat_id,message_id):
    return db.execute(text("SELECT rating_revision,action,value,note,source_revision,source_hash,created_at FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind='message' AND target_key=:message AND origin='local' ORDER BY rating_revision DESC LIMIT 1"),{'account':account,'chat':chat_id,'message':message_id}).first()


def _view(source,latest):
    descriptor={k:source[k] for k in ('message_id','role','workspace_id','source_revision','source_hash')}
    if latest is None:return {'state':'unrated','source':descriptor,'rating_revision':0,'value':None,'note':None}
    stale=(latest[4]!=source['source_revision'] or latest[5]!=source['source_hash'])
    return {'state':'stale' if stale else ('rated' if latest[1]=='set' else 'unrated'), 'source':descriptor,
            'rating_revision':latest[0],'value':latest[2] if latest[1]=='set' else None,'note':latest[3]}


def read(db, *, owner, account, chat_id, message_id, auth):
    if schema_state(db)!='ready':raise RatingUnavailable('rating profile unavailable')
    _policy(owner)
    source=_current(db,owner=owner,account=account,chat_id=chat_id,message_id=message_id,auth=auth)
    return _view(source,_latest(db,account,chat_id,message_id))


def write(db, *, owner, account, chat_id, message_id, auth, expected_source_revision, expected_source_hash,
          expected_rating_revision, action, value, note, request_key):
    """Caller owns one SQLite IMMEDIATE transaction and current account admission."""
    if schema_state(db)!='ready':raise RatingUnavailable('rating profile unavailable')
    if (type(expected_source_revision) is not int or expected_source_revision<1 or type(expected_source_hash) is not str or not _HASH.fullmatch(expected_source_hash)
        or type(expected_rating_revision) is not int or expected_rating_revision<0 or action not in ('set','clear')
        or (action=='set' and (type(value) is not int or not 0<=value<=10)) or (action=='clear' and value is not None)
        or (note is not None and (type(note) is not str or len(note.encode('utf-8'))>4096)) or not _id(request_key)):
        raise RatingUnavailable('invalid rating request')
    if (not all(_id(x) for x in (owner,account,chat_id,message_id)) or auth.account_id(owner)!=account
        or auth.username_for_account_id(account)!=owner):
        raise RatingUnavailable('account unavailable')
    chat=db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat_id}).first()
    if not chat or chat[0]!=owner:raise RatingUnavailable('chat unavailable')
    key_hash=hashlib.sha256(('openclank.chat-rating.key.v1\0'+request_key).encode()).hexdigest()
    prior=db.execute(text("SELECT payload_hash,rating_revision,chat_id,message_id,workspace_id,message_role,source_revision,source_hash FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key AND origin='local'"),{'account':account,'key':key_hash}).first()
    if prior:
        payload={'version':1,'account':account,'chat':chat_id,'workspace':prior[4],'message':message_id,'role':prior[5],
            'source_revision':expected_source_revision,'source_hash':expected_source_hash,'expected_rating_revision':expected_rating_revision,
            'action':action,'value':value,'note':note}
        payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        if prior[2]!=chat_id or prior[3]!=message_id or prior[0]!=payload_hash:
            raise RatingConflict('request key conflict')
        current_state='unknown'
        try:
            current_source=_current(db,owner=owner,account=account,chat_id=chat_id,message_id=message_id,auth=auth)
            latest=_latest(db,account,chat_id,message_id)
            if (current_source['source_revision'],current_source['source_hash'])!=(prior[6],prior[7]):current_state='stale'
            elif latest and latest[0]==prior[1]:current_state='current'
            else:current_state='superseded'
        except RatingUnavailable:pass
        return {'state':'replayed','rating_revision':prior[1],'current_state':current_state}
    if _policy(owner).get('collection_enabled') is not True:raise RatingUnavailable('rating collection disabled')
    source=_current(db,owner=owner,account=account,chat_id=chat_id,message_id=message_id,auth=auth)
    if (source['source_revision'],source['source_hash'])!=(expected_source_revision,expected_source_hash):raise RatingConflict('message changed')
    payload={'version':1,'account':account,'chat':chat_id,'workspace':source['workspace_id'],'message':message_id,'role':source['role'],
        'source_revision':expected_source_revision,'source_hash':expected_source_hash,'expected_rating_revision':expected_rating_revision,
        'action':action,'value':value,'note':note}
    payload_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    latest=_latest(db,account,chat_id,message_id);head=latest[0] if latest else 0
    if head!=expected_rating_revision:raise RatingConflict('rating changed')
    db.execute(text("INSERT INTO chat_rating_events(event_id,account_id,rater_account_id,chat_id,workspace_id,target_kind,target_key,descriptor_json,descriptor_hash,message_id,message_role,source_revision,source_hash,rating_revision,action,value,note,origin,asserted_origin,request_key_hash,payload_hash,created_at) VALUES (:event,:account,:account,:chat,:workspace,'message',:message,:descriptor,:descriptor_hash,:message,:role,:revision,:hash,:rating_revision,:action,:value,:note,'local',NULL,:key,:payload,:created)"),
        {'event':secrets.token_hex(32),'account':account,'chat':chat_id,'workspace':source['workspace_id'],'message':message_id,'role':source['role'],
         'revision':source['source_revision'],'hash':source['source_hash'],'rating_revision':head+1,'action':action,'value':value,'note':note,
         'key':key_hash,'payload':payload_hash,'created':datetime.now(timezone.utc).isoformat(),
         'descriptor':_json_descriptor(_message_descriptor(source)), 'descriptor_hash':_descriptor_hash(_message_descriptor(source))})
    return {'state':'applied','rating_revision':head+1}


def read_target(db, *, owner, account, chat_id, auth, kind, segments=None):
    if schema_state(db)!='ready':raise RatingUnavailable('rating profile unavailable')
    _policy(owner)
    snapshot=host_text_snapshot(db,owner=owner,account=account,chat_id=chat_id,auth=auth)
    descriptor=target_descriptor(snapshot,kind,segments)
    digest=_descriptor_hash(descriptor);key='chat' if kind=='chat' else digest
    latest=db.execute(text("SELECT rating_revision,action,value,note,descriptor_hash FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind=:kind AND target_key=:key AND origin='local' ORDER BY rating_revision DESC LIMIT 1"),{'account':account,'chat':chat_id,'kind':kind,'key':key}).first()
    previous=[]
    if kind=='chat':
        # Old exact section identities remain independently visible after edits;
        # they never become ratings on a newly selected revision.
        rows=db.execute(text("SELECT event_id,length(CAST(descriptor_json AS BLOB)),descriptor_hash,rating_revision,action,value,length(CAST(note AS BLOB)) FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind='section' AND target_key NOT LIKE 'native:%' AND origin='local' ORDER BY created_at DESC,event_id DESC LIMIT 257"),{'account':account,'chat':chat_id}).all()
        if len(rows)>256:raise RatingUnavailable('section history unavailable')
        seen=set();current={item['message_id']:item for item in snapshot['items']}; history_bytes=0
        for event_id,size,old_hash,revision,action,value,note_size in rows:
            if type(size) is not int or size>262144 or (note_size is not None and (type(note_size) is not int or note_size>4096)):raise RatingUnavailable('section history unavailable')
            history_bytes+=size+(note_size or 0)
            if history_bytes>1048576:raise RatingUnavailable('section history unavailable')
            data=db.execute(text('SELECT descriptor_json,note FROM chat_rating_events WHERE event_id=:event AND account_id=:account AND chat_id=:chat'),{'event':event_id,'account':account,'chat':chat_id}).first()
            if not data:raise RatingUnavailable('section history unavailable')
            encoded,note=data
            if type(encoded) is not str or len(encoded.encode())!=size or hashlib.sha256(encoded.encode()).hexdigest()!=old_hash:raise RatingUnavailable('section history unavailable')
            if old_hash in seen:continue
            seen.add(old_hash)
            try:old=json.loads(encoded);old_segments=old['segments']
            except (ValueError,KeyError,TypeError) as exc:raise RatingUnavailable('section history unavailable') from exc
            if type(old_segments) is not list or not old_segments:raise RatingUnavailable('section history unavailable')
            if any(type(part) is not dict for part in old_segments):raise RatingUnavailable('section history unavailable')
            positions={item['message_id']:index for index,item in enumerate(snapshot['items'])}
            stale=any((not current.get(part.get('message_id')) or current[part['message_id']]['source_revision']!=part.get('source_revision') or current[part['message_id']]['source_hash']!=part.get('source_hash') or current[part['message_id']]['timestamp']!=part.get('timestamp') or current[part['message_id']]['role']!=part.get('role')) for part in old_segments)
            if not stale:
                indexes=[positions[part['message_id']] for part in old_segments]
                stale=any(right!=left+1 for left,right in zip(indexes,indexes[1:]))
            previous.append({'descriptor_hash':old_hash,'segments':old_segments,'state':'stale' if stale else ('rated' if action=='set' else 'unrated'),
                             'rating_revision':revision,'value':value if action=='set' else None,'note':note})
    return {'state':'unrated' if not latest else ('stale' if latest[4]!=digest else ('rated' if latest[1]=='set' else 'unrated')),
            'target_key':key,'descriptor':descriptor,'descriptor_hash':digest,'rating_revision':latest[0] if latest else 0,
            'value':latest[2] if latest and latest[1]=='set' else None,'note':latest[3] if latest else None,
            'scope_label':'Chat text saved by Open Clank; excludes separately managed history.', 'section_history':previous,
            'raw_messages':[{'message_id':x['message_id'],'role':x['role'],'text':x['text']} for x in snapshot['items']]}


def write_target(db, *, owner, account, chat_id, auth, kind, segments, expected_descriptor_hash, expected_rating_revision, action, value, note, request_key):
    if (type(expected_descriptor_hash) is not str or not _HASH.fullmatch(expected_descriptor_hash) or type(expected_rating_revision) is not int or expected_rating_revision<0
        or action not in ('set','clear') or (action=='set' and (type(value) is not int or not 0<=value<=10)) or (action=='clear' and value is not None)
        or (note is not None and (type(note) is not str or len(note.encode())>4096)) or not _id(request_key)):
        raise RatingUnavailable('invalid rating request')
    if not all(_id(v) for v in (owner,account,chat_id)) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:
        raise RatingUnavailable('account unavailable')
    chat=db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat_id}).first()
    if not chat or chat[0]!=owner:raise RatingUnavailable('chat unavailable')
    key_hash=hashlib.sha256(('openclank.chat-rating.key.v1\0'+request_key).encode()).hexdigest()
    request={'version':2,'account':account,'chat':chat_id,'kind':kind,'segments':segments,'descriptor_hash':expected_descriptor_hash,
             'expected_rating_revision':expected_rating_revision,'action':action,'value':value,'note':note}
    request_hash=hashlib.sha256(_json_descriptor(request).encode()).hexdigest()
    prior=db.execute(text("SELECT payload_hash,rating_revision,target_kind,target_key,descriptor_hash,descriptor_json FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key AND origin='local'"),{'account':account,'key':key_hash}).first()
    if prior:
        if prior[0]!=request_hash:raise RatingConflict('request key conflict')
        current='unknown'
        try:
            _policy(owner)
            snap=host_text_snapshot(db,owner=owner,account=account,chat_id=chat_id,auth=auth)
            if type(prior[5]) is not str or len(prior[5].encode())>262144 or hashlib.sha256(prior[5].encode()).hexdigest()!=prior[4]:raise RatingUnavailable('receipt unavailable')
            old=json.loads(prior[5])
            if kind=='chat':
                stale=_descriptor_hash(target_descriptor(snap,'chat'))!=prior[4]
            else:
                old_parts=old['segments']; by_id={item['message_id']:item for item in snap['items']}; indexes={item['message_id']:i for i,item in enumerate(snap['items'])}
                stale=any(not by_id.get(part['message_id']) or by_id[part['message_id']]['source_revision']!=part['source_revision'] or by_id[part['message_id']]['source_hash']!=part['source_hash'] or by_id[part['message_id']]['timestamp']!=part['timestamp'] or by_id[part['message_id']]['role']!=part['role'] for part in old_parts)
                if not stale:
                    positions=[indexes[part['message_id']] for part in old_parts]
                    stale=any(right!=left+1 for left,right in zip(positions,positions[1:]))
            head=db.execute(text("SELECT max(rating_revision) FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind=:kind AND target_key=:target AND origin='local'"),{'account':account,'chat':chat_id,'kind':kind,'target':prior[3]}).scalar()
            current='stale' if stale else ('current' if head==prior[1] else 'superseded')
        except (RatingUnavailable,ValueError,TypeError,KeyError):pass
        return {'state':'replayed','rating_revision':prior[1],'current_state':current}
    if _policy(owner).get('collection_enabled') is not True:raise RatingUnavailable('rating collection disabled')
    view=read_target(db,owner=owner,account=account,chat_id=chat_id,auth=auth,kind=kind,segments=segments)
    if view['descriptor_hash']!=expected_descriptor_hash:raise RatingConflict('target changed')
    if kind=='chat' and not view['descriptor']['messages']:raise RatingUnavailable('empty host text')
    if view['rating_revision']!=expected_rating_revision:raise RatingConflict('rating changed')
    desc=view['descriptor']
    db.execute(text("INSERT INTO chat_rating_events(event_id,account_id,rater_account_id,chat_id,workspace_id,target_kind,target_key,descriptor_json,descriptor_hash,message_id,message_role,source_revision,source_hash,rating_revision,action,value,note,origin,asserted_origin,request_key_hash,payload_hash,created_at) VALUES (:event,:account,:account,:chat,:workspace,:kind,:target,:descriptor,:digest,NULL,NULL,NULL,NULL,:revision,:action,:value,:note,'local',NULL,:key,:payload,:created)"),
        {'event':secrets.token_hex(32),'account':account,'chat':chat_id,'workspace':desc['workspace_id'],'kind':kind,'target':view['target_key'],
         'descriptor':_json_descriptor(desc),'digest':view['descriptor_hash'],'revision':expected_rating_revision+1,'action':action,'value':value,'note':note,
         'key':key_hash,'payload':request_hash,'created':datetime.now(timezone.utc).isoformat()})
    return {'state':'applied','rating_revision':expected_rating_revision+1}


def native_descriptor(snapshot, kind, segments=None, part_id=None):
    """Describe the authenticated native observation without inventing host revisions."""
    keys={'version','scope','sessionID','snapshotSHA256','items','exclusions'}
    if type(snapshot) is not dict or set(snapshot)!=keys or snapshot['version']!=1 or snapshot['scope']!='native_visible_text' or not _id(snapshot['sessionID']) or type(snapshot['snapshotSHA256']) is not str or not _HASH.fullmatch(snapshot['snapshotSHA256']):raise RatingUnavailable('native source unavailable')
    if type(snapshot['items']) is not list or len(snapshot['items'])>256 or type(snapshot['exclusions']) is not dict or set(snapshot['exclusions'])!={'nonMain','reverted','unfinished','nonText','synthetic'} or any(type(v) is not int or v<0 for v in snapshot['exclusions'].values()):raise RatingUnavailable('native source unavailable')
    clean=[]; total=0
    expected={'messageID','partID','role','messageCreated','messageUpdated','partCreated','partUpdated','messageHash','partHash','textSHA256','text'}
    seen=set()
    for item in snapshot['items']:
        if (type(item) is not dict or set(item)!=expected or not _id(item['messageID']) or not _id(item['partID']) or item['partID'] in seen
            or item['role'] not in ('user','assistant') or any(type(item[k]) is not int or item[k]<0 for k in ('messageCreated','messageUpdated','partCreated','partUpdated'))
            or any(type(item[k]) is not str or not _HASH.fullmatch(item[k]) for k in ('messageHash','partHash','textSHA256')) or type(item['text']) is not str):raise RatingUnavailable('native source unavailable')
        raw=item['text'].encode('utf-8');total+=len(raw)
        if len(raw)>262144 or total>1048576 or hashlib.sha256(raw).hexdigest()!=item['textSHA256']:raise RatingUnavailable('native source unavailable')
        seen.add(item['partID']);clean.append(item)
    if kind=='message':
        picked=[i for i in clean if i['partID']==part_id]
        if len(picked)!=1 or segments is not None:raise RatingUnavailable('native part unavailable')
        values=picked
    elif kind=='chat':
        if part_id is not None or segments is not None:raise RatingUnavailable('invalid native target')
        values=clean
    elif kind=='section':
        if part_id is not None or type(segments) is not list or not segments or len(segments)>256:raise RatingUnavailable('invalid native selection')
        positions={item['partID']:i for i,item in enumerate(clean)};values=[];last=None
        for part in segments:
            if type(part) is not dict or set(part)!={'partID','start','end'}:raise RatingUnavailable('invalid native selection')
            pos=positions.get(part['partID']);start=part['start'];end=part['end']
            if pos is None or type(start) is not int or type(end) is not int:raise RatingUnavailable('invalid native selection')
            raw=clean[pos]['text'].encode('utf-8')
            if start<0 or end<=start or end>len(raw):raise RatingUnavailable('invalid native selection')
            try:raw[:start].decode('utf-8');raw[:end].decode('utf-8')
            except UnicodeDecodeError as exc:raise RatingUnavailable('invalid UTF-8 boundary') from exc
            if last is not None and (pos!=last[0]+1 or last[1]!=len(clean[last[0]]['text'].encode('utf-8')) or start!=0):raise RatingUnavailable('noncontiguous native selection')
            values.append({**clean[pos],'start':start,'end':end});last=(pos,end)
    else:raise RatingUnavailable('invalid native target')
    heads=[{k:v for k,v in item.items() if k!='text'} for item in values]
    descriptor={'version':1,'kind':kind,'scope':'native_visible_text','session_id':snapshot['sessionID'],'items':heads}
    if kind=='chat':descriptor.update(snapshot_hash=snapshot['snapshotSHA256'],exclusions=snapshot['exclusions'])
    return descriptor


def native_structural_snapshot(snapshot):
    """Validate closed v2 coverage; this observation grants no source access."""
    expected={'version','scope','sessionID','snapshotSHA256','items','exclusions','coverage'}
    if (type(snapshot) is not dict or set(snapshot)!=expected or type(snapshot['version']) is not int or snapshot['version']!=2
            or snapshot['scope']!='native_visible_text_with_structural_steps'
            or type(snapshot['items']) is not list or type(snapshot['coverage']) is not list or len(snapshot['coverage'])>256):
        raise RatingUnavailable('native source unavailable')
    text_items=[]
    for item in snapshot['items']:
        if type(item) is not dict or item.get('role') not in ('user','assistant'):
            raise RatingUnavailable('native source unavailable')
        if item['role']=='assistant':
            if not _id(item.get('parentID')):raise RatingUnavailable('native source unavailable')
            value={k:v for k,v in item.items() if k!='parentID'}
        else:
            if 'parentID' in item:raise RatingUnavailable('native source unavailable')
            value=item
        text_items.append(value)
    base={**snapshot,'version':1,'scope':'native_visible_text','items':text_items}
    del base['coverage']
    native_descriptor(base,'chat')
    if any(snapshot['exclusions'].values()):raise RatingUnavailable('native source unavailable')
    fields={'position','kind','role','messageID','partID','messageCreated','messageUpdated','partCreated','partUpdated','messageHash','partHash'}
    seen=set(); text_iter=iter(snapshot['items']); last=None; messages={}
    for index, item in enumerate(snapshot['coverage']):
        if type(item) is not dict or item.get('role') not in ('user','assistant'):
            raise RatingUnavailable('native source unavailable')
        text_kind=item.get('kind') in ('text','context-text','ignored-user')
        keys=fields | ({'parentID'} if item['role']=='assistant' else set()) | ({'textSHA256','synthetic','ignored'} if text_kind else set())
        if (set(item)!=keys or type(item['position']) is not int or item['position']!=index
                or item['kind'] not in ('text','context-text','ignored-user','step-start','step-finish') or not _id(item['messageID']) or not _id(item['partID'])
                or (item['role']=='assistant' and not _id(item['parentID']))
                or (item['kind'] in ('step-start','step-finish') and item['role']!='assistant')
                or (text_kind and (type(item['synthetic']) is not bool or type(item['ignored']) is not bool or type(item['textSHA256']) is not str or not _HASH.fullmatch(item['textSHA256'])))
                or (item['kind']=='text' and (item['synthetic'] or item['ignored']))
                or (item['kind']=='context-text' and (item['role']!='user' or not item['synthetic'] or item['ignored']))
                or (item['kind']=='ignored-user' and (item['role']!='user' or not item['ignored']))
                or item['partID'] in seen or any(type(item[k]) is not int or item[k]<0 for k in ('messageCreated','messageUpdated','partCreated','partUpdated'))
                or any(type(item[k]) is not str or not _HASH.fullmatch(item[k]) for k in ('messageHash','partHash'))):
            raise RatingUnavailable('native source unavailable')
        identity=(item['role'],item.get('parentID'),item['messageCreated'],item['messageUpdated'],item['messageHash'])
        prior=messages.setdefault(item['messageID'],identity)
        if prior!=identity:raise RatingUnavailable('native source unavailable')
        order=(item['messageCreated'],item['messageID'],item['partID'])
        if last is not None and order<=last:raise RatingUnavailable('native source unavailable')
        last=order; seen.add(item['partID'])
        if item['kind']=='text':
            value=next(text_iter,None)
            if value is None or any(value.get(k)!=item[k] for k in keys-{'position','kind','synthetic','ignored'}):
                raise RatingUnavailable('native source unavailable')
    if next(text_iter,None) is not None:raise RatingUnavailable('native source unavailable')
    return base


def complete_paired_text_descriptor(host, snapshot, pairs):
    """Prove complete ordered two-store coverage from independently verified pairs.

    Callers must supply fresh owner-qualified snapshots and pairs, and repeat
    currentness checks around any rating transaction. This function grants no
    access and does not establish rendered-DOM equivalence.
    """
    from src.openclank.chat_source_bridge import _valid_forward_pair
    original_snapshot = snapshot
    snapshot = native_structural_snapshot(snapshot)
    native = native_descriptor(snapshot, 'chat')
    if (type(host) is not dict or set(host) != {'version', 'scope', 'workspace_id', 'items'}
            or host['version'] != 1 or host['scope'] != 'committed_host_text_sequence'
            or type(host['items']) is not list or not host['items']
            or type(pairs) is not list or not pairs or len(pairs) > 128
            or any(snapshot['exclusions'].values())):
        raise RatingUnavailable('complete text coverage unavailable')
    hosts, items = host['items'], snapshot['items']
    if len(hosts) != len(pairs) * 2 or len(items) != len(hosts):
        raise RatingUnavailable('complete text coverage unavailable')
    ordered = []
    seen_host, seen_native = set(), set()
    for index, entry in enumerate(pairs):
        if type(entry) is not dict or set(entry)!={'pair','closure'}:raise RatingUnavailable('complete text coverage unavailable')
        pair,closure=entry['pair'],entry['closure']
        if not _valid_forward_pair(pair) or pair['identity']['sessionID'] != snapshot['sessionID']:
            raise RatingUnavailable('complete text coverage unavailable')
        for offset, (side, role) in enumerate((('input', 'user'), ('output', 'assistant'))):
            at = index * 2 + offset
            source, h, n = pair[side], hosts[at], items[at]
            if (type(h) is not dict or h.get('message_id') != source['hostMessageID']
                    or h.get('role') != role or h.get('source_revision') != source['hostRevision']
                    or h.get('source_hash') != source['hostHash']
                    or n['messageID'] != source['nativeMessageID'] or n['partID'] != source['nativePartID']
                    or n['role'] != role or n['textSHA256'] != source['nativeTextSHA256']
                    or (side=='output' and original_snapshot['items'][at]['parentID'] != pair['input']['nativeMessageID'])
                    or h['message_id'] in seen_host or n['partID'] in seen_native):
                raise RatingUnavailable('complete text coverage unavailable')
            seen_host.add(h['message_id']); seen_native.add(n['partID'])
            ordered.append({'host': {k:h[k] for k in ('message_id','role','timestamp','source_revision','source_hash')},
                            'native': native['items'][at], 'pair':pair, 'closure':closure})
        if pair['version']!=2 or type(closure) is not dict:raise RatingUnavailable('complete text coverage unavailable')
        wire=closure.get('wire')
        if type(wire) is not dict or wire.get('version')!=2:raise RatingUnavailable('complete text coverage unavailable')
        layout=pair['input']['layout']; generated=wire.get('generated')
        if type(generated) is not list or len(generated)>1:raise RatingUnavailable('complete text coverage unavailable')
        visible=[c for c in original_snapshot['coverage'] if c['messageID']==pair['input']['nativeMessageID'] and c['kind'] in ('text','context-text')]
        expected=[*layout,*({'position':g['position'],'sha256':g['sha256'],'synthetic':True} for g in generated)]
        actual=[{'position':i,'sha256':c['textSHA256'],'synthetic':c['synthetic']} for i,c in enumerate(visible)]
        if actual!=expected:raise RatingUnavailable('complete text coverage unavailable')
    user_messages={entry['pair']['input']['nativeMessageID'] for entry in pairs}
    if any(c['kind'] in ('context-text','ignored-user') and c['messageID'] not in user_messages for c in original_snapshot['coverage']):
        raise RatingUnavailable('complete text coverage unavailable')
    return {'version':1, 'kind':'chat', 'scope':'complete_paired_committed_text',
            'workspace_id':host['workspace_id'], 'session_id':snapshot['sessionID'], 'items':ordered,
            'snapshot_sha256':original_snapshot['snapshotSHA256'], 'coverage':original_snapshot['coverage']}


def complete_paired_text_target(db, *, owner, account, chat_id, auth, host, snapshot, pairs, current, payload=None):
    """One complete supported text observation; caller owns both-source guards."""
    if (schema_state(db)!='ready' or not all(_id(v) for v in (owner,account,chat_id))
            or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
            or not callable(current) or current(db) is not True):
        raise RatingUnavailable('complete text unavailable')
    policy=_policy(owner)
    descriptor=complete_paired_text_descriptor(host,snapshot,pairs)
    encoded=_json_descriptor(descriptor)
    if len(encoded.encode('utf-8'))>262144:raise RatingUnavailable('complete text unavailable')
    digest=_descriptor_hash(descriptor); key='complete-paired:chat'
    latest=db.execute(text("SELECT rating_revision,action,value,note,descriptor_hash FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind='chat' AND target_key=:target AND origin='local' ORDER BY rating_revision DESC LIMIT 1"),{'account':account,'chat':chat_id,'target':key}).first()
    view={'state':'unrated' if not latest else ('stale' if latest[4]!=digest else ('rated' if latest[1]=='set' else 'unrated')),
          'target_key':key,'descriptor':descriptor,'descriptor_hash':digest,'rating_revision':latest[0] if latest else 0,
          'value':latest[2] if latest and latest[1]=='set' else None,'note':latest[3] if latest else None,
          'scope_label':'Complete supported committed text; optimistic cross-store observation.'}
    if payload is None:return view
    expected={'expected_descriptor_hash','expected_rating_revision','action','value','note','request_key'}
    if type(payload) is not dict or set(payload)!=expected:raise RatingUnavailable('invalid complete text rating')
    rev,action,value,note,request_key=(payload[k] for k in ('expected_rating_revision','action','value','note','request_key'))
    if (type(rev) is not int or rev<0 or action not in ('set','clear') or (action=='set' and (type(value) is not int or not 0<=value<=10))
            or (action=='clear' and value is not None) or (note is not None and (type(note) is not str or len(note.encode('utf-8'))>4096))
            or not _id(request_key) or type(payload['expected_descriptor_hash']) is not str or not _HASH.fullmatch(payload['expected_descriptor_hash'])):
        raise RatingUnavailable('invalid complete text rating')
    request_hash=_descriptor_hash({'version':5,'account':account,'chat':chat_id,**{k:payload[k] for k in expected if k!='request_key'}})
    key_hash=hashlib.sha256(('openclank.chat-rating.key.v1\0'+request_key).encode()).hexdigest()
    prior=db.execute(text("SELECT payload_hash,rating_revision,descriptor_hash,target_key FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key AND origin='local'"),{'account':account,'key':key_hash}).first()
    if prior:
        if prior[0]!=request_hash or prior[3]!=key:raise RatingConflict('request key conflict')
        return {'state':'replayed','rating_revision':prior[1],'current_state':'current' if latest and latest[0]==prior[1] and prior[2]==digest else 'stale','observation':'optimistic'}
    if policy.get('collection_enabled') is not True:raise RatingUnavailable('rating collection disabled')
    if payload['expected_descriptor_hash']!=digest or rev!=view['rating_revision']:raise RatingConflict('complete text changed')
    if current(db) is not True:raise RatingUnavailable('complete text changed')
    db.execute(text("INSERT INTO chat_rating_events(event_id,account_id,rater_account_id,chat_id,workspace_id,target_kind,target_key,descriptor_json,descriptor_hash,message_id,message_role,source_revision,source_hash,rating_revision,action,value,note,origin,asserted_origin,request_key_hash,payload_hash,created_at) VALUES (:event,:account,:account,:chat,:workspace,'chat',:target,:descriptor,:digest,NULL,NULL,NULL,NULL,:revision,:action,:value,:note,'local',NULL,:key,:payload,:created)"),{'event':secrets.token_hex(32),'account':account,'chat':chat_id,'workspace':host['workspace_id'],'target':key,'descriptor':encoded,'digest':digest,'revision':rev+1,'action':action,'value':value,'note':note,'key':key_hash,'payload':request_hash,'created':datetime.now(timezone.utc).isoformat()})
    return {'state':'applied','rating_revision':rev+1,'descriptor_hash':digest,'observation':'optimistic'}


def _native_descriptor_current(original, snapshot):
    """Only selected preimages and adjacency define a part or section."""
    if type(original) is not dict or original.get('scope')!='native_visible_text' or original.get('session_id')!=snapshot.get('sessionID'):
        return False
    kind=original.get('kind'); items=original.get('items')
    if type(items) is not list or not items:return False
    try:
        if kind=='chat': current=native_descriptor(snapshot,'chat')
        elif kind=='message':
            if len(items)!=1:return False
            current=native_descriptor(snapshot,'message',part_id=items[0]['partID'])
        elif kind=='section':
            segments=[{'partID':item['partID'],'start':item['start'],'end':item['end']} for item in items]
            current=native_descriptor(snapshot,'section',segments=segments)
        else:return False
        return current==original
    except (RatingUnavailable,KeyError,TypeError,ValueError):return False


def paired_status(db, *, owner, account, chat_id, message_id, auth, segments, payload):
    """Read only the exact original request receipt; absence is unknown."""
    expected={'expected_descriptor_hash','expected_rating_revision','action','value','note','request_key'}
    if (schema_state(db)!='ready' or not all(_id(v) for v in (owner,account,chat_id,message_id)) or auth.account_id(owner)!=account
        or auth.username_for_account_id(account)!=owner or type(payload) is not dict or set(payload)!=expected
        or not _id(payload['request_key']) or type(segments) is not list or not 1<=len(segments)<=2):raise RatingUnavailable('paired status unavailable')
    chat=db.execute(text('SELECT owner FROM sessions WHERE id=:chat'),{'chat':chat_id}).first()
    if not chat or chat[0]!=owner:raise RatingUnavailable('paired status unavailable')
    _policy(owner)
    try:
        request_hash=_descriptor_hash({'version':4,'account':account,'chat':chat_id,'segments':segments,**{k:payload[k] for k in expected if k!='request_key'}})
    except (ValueError,TypeError,UnicodeError):raise RatingUnavailable('paired status unavailable')
    key_hash=hashlib.sha256(('openclank.chat-rating.key.v1\0'+payload['request_key']).encode()).hexdigest()
    row=db.execute(text("SELECT payload_hash,rating_revision,descriptor_hash,target_kind,target_key,chat_id,CASE WHEN length(CAST(descriptor_json AS BLOB))<=262144 THEN descriptor_json ELSE NULL END FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key AND origin='local'"),{'account':account,'key':key_hash}).first()
    if not row:return {'state':'unknown'}
    if row[0]!=request_hash or row[3]!='section' or row[4]!='paired:'+row[2] or row[5]!=chat_id:raise RatingConflict('request key conflict')
    try:
        if type(row[6]) is not str or hashlib.sha256(row[6].encode()).hexdigest()!=row[2]:raise ValueError()
        descriptor=json.loads(row[6])
        if not _valid_paired_import(descriptor) or descriptor['pair']['input']['hostMessageID']!=message_id:raise ValueError()
    except (ValueError,TypeError,KeyError,UnicodeError):raise RatingUnavailable('paired status unavailable')
    return {'state':'recorded','rating_revision':row[1],'descriptor_hash':row[2],'current_state':'unknown','observation':'optimistic'}


def paired_target(db, *, owner, account, chat_id, auth, pair, snapshot, segments, payload=None, pair_current):
    """One explicitly witnessed forward turn; caller owns transaction and source lease."""
    from src.openclank.chat_source_bridge import _valid_forward_pair
    if (schema_state(db)!='ready' or not all(_id(v) for v in (owner,account,chat_id)) or auth.account_id(owner)!=account
        or auth.username_for_account_id(account)!=owner or not _valid_forward_pair(pair) or not callable(pair_current)
        or pair_current(db)!=pair): raise RatingUnavailable('paired source unavailable')
    chat=db.execute(text('SELECT owner,workspace_id FROM sessions WHERE id=:chat'),{'chat':chat_id}).first()
    if not chat or chat[0]!=owner:raise RatingUnavailable('paired source unavailable')
    policy=_policy(owner); choices={}; descriptors={}
    for side,role in (('input','user'),('output','assistant')):
        witness=pair[side];host=_current(db,owner=owner,account=account,chat_id=chat_id,message_id=witness['hostMessageID'],auth=auth)
        if host['role']!=role or host['source_revision']!=witness['hostRevision'] or host['source_hash']!=witness['hostHash']:raise RatingUnavailable('paired host changed')
        native=native_descriptor(snapshot,'message',part_id=witness['nativePartID']);item=native['items'][0]
        if snapshot['sessionID']!=pair['identity']['sessionID'] or item['messageID']!=witness['nativeMessageID'] or item['role']!=role or item['textSHA256']!=witness['nativeTextSHA256']:raise RatingUnavailable('paired native changed')
        raw=next((row['text'] for row in snapshot['items'] if row['partID']==witness['nativePartID']),None)
        if type(raw) is not str:raise RatingUnavailable('paired native unavailable')
        choices[side]={'host':host['content'],'native':raw};descriptors[side]={'host':_message_descriptor(host),'native':native}
    if segments is None:
        if payload is not None:raise RatingUnavailable('paired selection required')
        return {'state':'ready','scope_label':'One witnessed forward turn; partial, optimistic observation.','choices':choices}
    if type(segments) is not list or not 1<=len(segments)<=2:raise RatingUnavailable('invalid paired selection')
    selected=[];previous=None
    for segment in segments:
        if (type(segment) is not dict or set(segment)!={'side','source','start','end'} or segment['side'] not in ('input','output')
            or segment['source'] not in ('host','native') or type(segment['start']) is not int or type(segment['end']) is not int):raise RatingUnavailable('invalid paired selection')
        side=segment['side'];raw=choices[side][segment['source']].encode('utf-8');start=segment['start'];end=segment['end']
        if start<0 or end<=start or end>len(raw):raise RatingUnavailable('invalid paired selection')
        try:raw[:start].decode('utf-8');raw[:end].decode('utf-8')
        except UnicodeDecodeError as exc:raise RatingUnavailable('invalid UTF-8 boundary') from exc
        if previous is not None and (previous['side']!='input' or side!='output' or previous['end']!=previous['size'] or start!=0):raise RatingUnavailable('noncontiguous paired selection')
        selected.append({**segment,'descriptor':descriptors[side][segment['source']]});previous={'side':side,'end':end,'size':len(raw)}
    descriptor={'version':1,'kind':'section','scope':'one_forward_turn_partial','pair':pair,'segments':selected}
    digest=_descriptor_hash(descriptor);key='paired:'+digest
    latest=db.execute(text("SELECT rating_revision,action,value,note FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind='section' AND target_key=:target AND origin='local' ORDER BY rating_revision DESC LIMIT 1"),{'account':account,'chat':chat_id,'target':key}).first()
    view={'state':'unrated' if not latest or latest[1]=='clear' else 'rated','target_key':key,'descriptor':descriptor,'descriptor_hash':digest,'rating_revision':latest[0] if latest else 0,'value':latest[2] if latest and latest[1]=='set' else None,'note':latest[3] if latest else None,'scope_label':'One witnessed forward turn; partial, optimistic observation.'}
    if payload is None:return view
    expected={'expected_descriptor_hash','expected_rating_revision','action','value','note','request_key'}
    if type(payload) is not dict or set(payload)!=expected:raise RatingUnavailable('invalid paired rating')
    action,value,note,revision,request_key=(payload[k] for k in ('action','value','note','expected_rating_revision','request_key'))
    if (type(revision) is not int or revision<0 or type(payload['expected_descriptor_hash']) is not str or not _HASH.fullmatch(payload['expected_descriptor_hash'])
        or action not in ('set','clear') or (action=='set' and (type(value) is not int or not 0<=value<=10)) or (action=='clear' and value is not None)
        or (note is not None and (type(note) is not str or len(note.encode('utf-8'))>4096)) or not _id(request_key)):raise RatingUnavailable('invalid paired rating')
    request_hash=_descriptor_hash({'version':4,'account':account,'chat':chat_id,'segments':segments,**{k:payload[k] for k in expected if k!='request_key'}})
    key_hash=hashlib.sha256(('openclank.chat-rating.key.v1\0'+request_key).encode()).hexdigest()
    prior=db.execute(text("SELECT payload_hash,rating_revision,descriptor_hash,target_key FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key AND origin='local'"),{'account':account,'key':key_hash}).first()
    if prior:
        if prior[0]!=request_hash:raise RatingConflict('request key conflict')
        return {'state':'replayed','rating_revision':prior[1],'current_state':'current' if prior[2]==digest and latest and latest[0]==prior[1] else 'stale','observation':'optimistic'}
    if policy.get('collection_enabled') is not True:raise RatingUnavailable('rating collection disabled')
    if digest!=payload['expected_descriptor_hash'] or view['rating_revision']!=revision:raise RatingConflict('paired source or rating changed')
    if pair_current(db)!=pair:raise RatingUnavailable('paired source changed')
    db.execute(text("INSERT INTO chat_rating_events(event_id,account_id,rater_account_id,chat_id,workspace_id,target_kind,target_key,descriptor_json,descriptor_hash,message_id,message_role,source_revision,source_hash,rating_revision,action,value,note,origin,asserted_origin,request_key_hash,payload_hash,created_at) VALUES (:event,:account,:account,:chat,:workspace,'section',:target,:descriptor,:digest,NULL,NULL,NULL,NULL,:revision,:action,:value,:note,'local',NULL,:key,:payload,:created)"),{'event':secrets.token_hex(32),'account':account,'chat':chat_id,'workspace':chat[1],'target':key,'descriptor':_json_descriptor(descriptor),'digest':digest,'revision':revision+1,'action':action,'value':value,'note':note,'key':key_hash,'payload':request_hash,'created':datetime.now(timezone.utc).isoformat()})
    return {'state':'applied','rating_revision':revision+1,'descriptor_hash':digest,'observation':'optimistic'}


def native_target(db, *, owner, account, chat_id, auth, snapshot, kind, segments=None, part_id=None, payload=None):
    if schema_state(db)!='ready' or not all(_id(v) for v in (owner,account,chat_id)) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise RatingUnavailable('native rating unavailable')
    row=db.execute(text('SELECT owner,workspace_id FROM sessions WHERE id=:chat'),{'chat':chat_id}).first()
    if not row or row[0]!=owner:raise RatingUnavailable('native rating unavailable')
    policy=_policy(owner)
    if payload is not None:
        expected={'expected_descriptor_hash','expected_rating_revision','action','value','note','request_key'}
        if type(payload) is not dict or set(payload)!=expected:raise RatingUnavailable('native rating unavailable')
        action=payload['action'];value=payload['value'];note=payload['note'];rev=payload['expected_rating_revision'];request_key=payload['request_key']
        if (type(rev) is not int or rev<0 or action not in ('set','clear') or (action=='set' and (type(value) is not int or not 0<=value<=10)) or (action=='clear' and value is not None) or (note is not None and (type(note) is not str or len(note.encode())>4096)) or not _id(request_key)):raise RatingUnavailable('invalid native rating')
        key_hash=hashlib.sha256(('openclank.chat-rating.key.v1\0'+request_key).encode()).hexdigest()
        request={'version':3,'account':account,'chat':chat_id,'kind':kind,'part_id':part_id,'segments':segments,**{k:payload[k] for k in expected if k!='request_key'}}
        request_hash=hashlib.sha256(_json_descriptor(request).encode()).hexdigest()
        prior=db.execute(text("SELECT payload_hash,rating_revision,descriptor_hash,descriptor_json,target_key FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key AND origin='local'"),{'account':account,'key':key_hash}).first()
        if prior:
            if prior[0]!=request_hash:raise RatingConflict('request key conflict')
            current='unknown'
            try:
                if type(prior[3]) is not str or hashlib.sha256(prior[3].encode()).hexdigest()!=prior[2]:raise RatingUnavailable('receipt unavailable')
                original=json.loads(prior[3]);native_descriptor(snapshot,'chat')
                if not _native_descriptor_current(original,snapshot):current='stale'
                else:
                    head=db.execute(text("SELECT max(rating_revision) FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind=:kind AND target_key=:target AND origin='local'"),{'account':account,'chat':chat_id,'kind':kind,'target':prior[4]}).scalar()
                    current='current' if head==prior[1] else 'superseded'
            except (RatingUnavailable,ValueError,TypeError,KeyError):pass
            return {'state':'replayed','rating_revision':prior[1],'current_state':current,'observation':'optimistic'}
        if policy.get('collection_enabled') is not True:raise RatingUnavailable('native rating unavailable')
    descriptor=native_descriptor(snapshot,kind,segments,part_id);digest=_descriptor_hash(descriptor)
    key='native:chat' if kind=='chat' else ('native:part:'+hashlib.sha256(part_id.encode()).hexdigest() if kind=='message' else 'native:section:'+digest)
    latest=db.execute(text("SELECT rating_revision,action,value,note,descriptor_hash FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_kind=:kind AND target_key=:target AND origin='local' ORDER BY rating_revision DESC LIMIT 1"),{'account':account,'chat':chat_id,'kind':kind,'target':key}).first()
    view={'state':'unrated' if not latest else ('stale' if latest[4]!=digest else ('rated' if latest[1]=='set' else 'unrated')),'target_key':key,'descriptor':descriptor,'descriptor_hash':digest,'rating_revision':latest[0] if latest else 0,'value':latest[2] if latest and latest[1]=='set' else None,'note':latest[3] if latest else None,'scope_label':'Native session text; excludes host-only and non-text history. Currentness is an optimistic observation.','raw_messages':[{'message_id':i['messageID'],'part_id':i['partID'],'role':i['role'],'text':i['text']} for i in snapshot['items']]}
    if payload is None and kind=='chat':
        rows=db.execute(text("SELECT event_id,target_key,target_kind,length(CAST(descriptor_json AS BLOB)),descriptor_hash,rating_revision,action,value,length(CAST(note AS BLOB)) FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat AND target_key LIKE 'native:%' AND origin='local' ORDER BY created_at DESC,event_id DESC LIMIT 257"),{'account':account,'chat':chat_id}).all()
        if len(rows)>256:raise RatingUnavailable('native history unavailable')
        history=[];seen=set();total=0
        for event_id,old_key,old_kind,size,old_hash,revision,action,value,note_size in rows:
            if old_key in seen:continue
            if type(size) is not int or size>262144 or (note_size is not None and (type(note_size) is not int or note_size>4096)):raise RatingUnavailable('native history unavailable')
            total+=size+(note_size or 0)
            if total>1048576:raise RatingUnavailable('native history unavailable')
            saved=db.execute(text('SELECT descriptor_json,note FROM chat_rating_events WHERE event_id=:id AND account_id=:account AND chat_id=:chat'),{'id':event_id,'account':account,'chat':chat_id}).first()
            if not saved or hashlib.sha256(saved[0].encode()).hexdigest()!=old_hash:raise RatingUnavailable('native history unavailable')
            try:old=json.loads(saved[0]);same=_native_descriptor_current(old,snapshot)
            except (ValueError,KeyError,TypeError) as exc:raise RatingUnavailable('native history unavailable') from exc
            seen.add(old_key);history.append({'kind':old_kind,'state':('rated' if action=='set' else 'unrated') if same else 'stale','value':value if action=='set' else None,'note':saved[1],'descriptor_hash':old_hash})
        view['native_history']=history
    if payload is None:return view
    if digest!=payload['expected_descriptor_hash']:raise RatingConflict('native source changed')
    if view['rating_revision']!=rev:raise RatingConflict('rating changed')
    if not descriptor['items']:raise RatingUnavailable('empty native source')
    db.execute(text("INSERT INTO chat_rating_events(event_id,account_id,rater_account_id,chat_id,workspace_id,target_kind,target_key,descriptor_json,descriptor_hash,message_id,message_role,source_revision,source_hash,rating_revision,action,value,note,origin,asserted_origin,request_key_hash,payload_hash,created_at) VALUES (:event,:account,:account,:chat,:workspace,:kind,:target,:descriptor,:digest,NULL,NULL,NULL,NULL,:revision,:action,:value,:note,'local',NULL,:key,:payload,:created)"),{'event':secrets.token_hex(32),'account':account,'chat':chat_id,'workspace':row[1],'kind':kind,'target':key,'descriptor':_json_descriptor(descriptor),'digest':digest,'revision':rev+1,'action':action,'value':value,'note':note,'key':key_hash,'payload':request_hash,'created':datetime.now(timezone.utc).isoformat()})
    return {'state':'applied','rating_revision':rev+1,'observation':'optimistic'}


def execute(session_factory, *, owner, account, chat_id, message_id, auth, current, payload=None, kind=None, segments=None):
    """Keep policy stable while one exact SQLite read or write completes."""
    from routes.prefs_routes import PREFERENCE_LOCK
    if not callable(current):raise RatingUnavailable('rating authority unavailable')
    with PREFERENCE_LOCK:
        db=session_factory()
        try:
            if db.get_bind().dialect.name!='sqlite':raise RatingUnavailable('rating profile unavailable')
            db.connection().exec_driver_sql('BEGIN IMMEDIATE' if payload is not None else 'BEGIN')
            if not current():raise RatingUnavailable('rating authority unavailable')
            if kind is not None:
                if payload is None:result=read_target(db,owner=owner,account=account,chat_id=chat_id,auth=auth,kind=kind,segments=segments)
                else:result=write_target(db,owner=owner,account=account,chat_id=chat_id,auth=auth,kind=kind,segments=segments,**payload)
            elif payload is None:result=read(db,owner=owner,account=account,chat_id=chat_id,message_id=message_id,auth=auth)
            else:result=write(db,owner=owner,account=account,chat_id=chat_id,message_id=message_id,auth=auth,**payload)
            if not current():raise RatingUnavailable('rating authority changed')
            if payload is None:db.rollback()
            else:db.commit()
            return result
        except Exception:
            db.rollback();raise
        finally:db.close()


def purge_chat(db, *, account, chat_id):
    state=schema_state(db)
    if state=='absent':return 0
    if state!='ready' or not _id(account) or not _id(chat_id):raise RatingUnavailable('rating lifecycle unavailable')
    return db.execute(text('DELETE FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat'),{'account':account,'chat':chat_id}).rowcount


def purge_account(db, *, account):
    state=schema_state(db)
    if state=='absent':return 0
    if state!='ready' or not _id(account):raise RatingUnavailable('rating lifecycle unavailable')
    return db.execute(text('DELETE FROM chat_rating_events WHERE account_id=:account'),{'account':account}).rowcount


def inventory_account(db, *, account):
    state=schema_state(db)
    if state=='absent':return {'state':'absent','count':0}
    if state!='ready' or not _id(account):raise RatingUnavailable('rating lifecycle unavailable')
    count=db.execute(text('SELECT count(*) FROM chat_rating_events WHERE account_id=:account'),{'account':account}).scalar()
    return {'state':'available','count':int(count)}


_EXPORT_FIELDS=('event_id','account_id','rater_account_id','chat_id','workspace_id','target_kind','target_key','descriptor_json','descriptor_hash','message_id','message_role','source_revision','source_hash','rating_revision','action','value','note','origin','asserted_origin','created_at')


def export_chat(db, *, account, chat_id):
    """A bounded owner-scoped history; no claim of completeness on overflow."""
    state=schema_state(db)
    if state!='ready' or not _id(account) or not _id(chat_id):return {'version':1,'state':'unavailable','events':[]}
    rows=db.execute(text('SELECT '+','.join(_EXPORT_FIELDS)+' FROM chat_rating_events WHERE account_id=:account AND chat_id=:chat ORDER BY created_at,event_id LIMIT 10001'),{'account':account,'chat':chat_id}).all()
    if len(rows)>10000:return {'version':1,'state':'unavailable','events':[]}
    events=[]
    for row in rows:
        item=dict(zip(_EXPORT_FIELDS,row))
        if item['asserted_origin'] is not None:
            try:item['asserted_origin']=json.loads(item['asserted_origin'])
            except (ValueError,TypeError):return {'version':1,'state':'unavailable','events':[]}
        events.append(item)
    return {'version':1,'state':'ready','events':events}


def _valid_paired_import(value):
    from src.openclank.chat_source_bridge import _valid_forward_pair
    if (type(value) is not dict or set(value)!={'version','kind','scope','pair','segments'} or value['version']!=1
        or value['kind']!='section' or value['scope']!='one_forward_turn_partial' or not _valid_forward_pair(value['pair'])
        or type(value['segments']) is not list or not 1<=len(value['segments'])<=2):return False
    previous=None
    for segment in value['segments']:
        if (type(segment) is not dict or set(segment)!={'side','source','start','end','descriptor'} or segment['side'] not in ('input','output')
            or segment['source'] not in ('host','native') or type(segment['start']) is not int or type(segment['end']) is not int
            or not 0<=segment['start']<segment['end']<=262144 or type(segment['descriptor']) is not dict):return False
        side=segment['side'];desc=segment['descriptor'];witness=value['pair'][side];role='user' if side=='input' else 'assistant'
        if previous is not None and (previous!='input' or side!='output' or segment['start']!=0):return False
        if segment['source']=='host':
            if (set(desc)!={'version','kind','scope','message_id','role','workspace_id','source_revision','source_hash'} or desc['version']!=1
                or desc['kind']!='message' or desc['scope']!='committed_host_text' or desc['message_id']!=witness['hostMessageID']
                or desc['role']!=role or desc['source_revision']!=witness['hostRevision'] or desc['source_hash']!=witness['hostHash']
                or (desc['workspace_id'] is not None and not _id(desc['workspace_id']))):return False
        else:
            if (set(desc)!={'version','kind','scope','session_id','items'} or desc['version']!=1 or desc['kind']!='message'
                or desc['scope']!='native_visible_text' or desc['session_id']!=value['pair']['identity']['sessionID']
                or type(desc['items']) is not list or len(desc['items'])!=1):return False
            item=desc['items'][0]
            if (type(item) is not dict or set(item)!={'messageID','partID','role','messageCreated','messageUpdated','partCreated','partUpdated','messageHash','partHash','textSHA256'}
                or item['messageID']!=witness['nativeMessageID'] or item['partID']!=witness['nativePartID'] or item['role']!=role
                or item['textSHA256']!=witness['nativeTextSHA256'] or any(type(item[k]) is not int or item[k]<0 for k in ('messageCreated','messageUpdated','partCreated','partUpdated'))
                or any(type(item[k]) is not str or not _HASH.fullmatch(item[k]) for k in ('messageHash','partHash','textSHA256'))):return False
        previous=side
    return True


def _valid_complete_paired_import(value):
    from src.openclank.chat_source_bridge import _valid_forward_pair, _source_closure_matches
    fields={'version','kind','scope','workspace_id','session_id','items','snapshot_sha256','coverage'}
    if (type(value) is not dict or set(value)!=fields or type(value['version']) is not int or value['version']!=1
            or value['kind']!='chat' or value['scope']!='complete_paired_committed_text'
            or (value['workspace_id'] is not None and not _id(value['workspace_id'])) or not _id(value['session_id'])
            or type(value['snapshot_sha256']) is not str or not _HASH.fullmatch(value['snapshot_sha256'])
            or type(value['items']) is not list or not 2<=len(value['items'])<=256 or len(value['items'])%2
            or type(value['coverage']) is not list or not value['coverage'] or len(value['coverage'])>256):return False
    hosts=set();parts=set()
    for at,item in enumerate(value['items']):
        if type(item) is not dict or set(item)!={'host','native','pair','closure'} or not _valid_forward_pair(item['pair']):return False
        pair=item['pair']; side='input' if at%2==0 else 'output';role='user' if at%2==0 else 'assistant'; witness=pair[side];h=item['host'];n=item['native']
        if (pair['identity']['sessionID']!=value['session_id'] or (at%2 and (pair!=value['items'][at-1]['pair'] or item['closure']!=value['items'][at-1]['closure']))
                or type(h) is not dict or set(h)!={'message_id','role','timestamp','source_revision','source_hash'}
                or h['role']!=role or h['message_id']!=witness['hostMessageID'] or h['source_revision']!=witness['hostRevision']
                or h['source_hash']!=witness['hostHash'] or type(h['timestamp']) is not str or not h['timestamp']
                or type(n) is not dict or set(n)!={'messageID','partID','role','messageCreated','messageUpdated','partCreated','partUpdated','messageHash','partHash','textSHA256'}
                or n['role']!=role or n['messageID']!=witness['nativeMessageID'] or n['partID']!=witness['nativePartID']
                or n['textSHA256']!=witness['nativeTextSHA256'] or h['message_id'] in hosts or n['partID'] in parts):return False
        if pair['version']!=2 or not _source_closure_matches(item['closure'],identity=pair['identity'],source={'messageID':pair['input']['nativeMessageID'],'partID':pair['input']['nativePartID'],'textSHA256':pair['input']['nativeTextSHA256']},output={'messageID':pair['output']['nativeMessageID'],'partID':pair['output']['nativePartID'],'textSHA256':pair['output']['nativeTextSHA256']},layout=pair['input']['layout'],source_position=pair['input']['sourcePosition'],source_scope=(2,None) if value['workspace_id'] is None else (1,value['workspace_id'])):return False
        hosts.add(h['message_id']);parts.add(n['partID'])
    # Historical imports are never authority; still require coverage shape and order.
    previous=None;covered=[];seen=set();messages={}
    for index,c in enumerate(value['coverage']):
        if type(c) is not dict or c.get('role') not in ('user','assistant'):return False
        is_text=c.get('kind') in ('text','context-text','ignored-user')
        fields={'position','kind','role','messageID','partID','messageCreated','messageUpdated','partCreated','partUpdated','messageHash','partHash'} | ({'parentID'} if c['role']=='assistant' else set()) | ({'textSHA256','synthetic','ignored'} if is_text else set())
        if (set(c)!=fields or type(c['position']) is not int or c['position']!=index or c['kind'] not in ('text','context-text','ignored-user','step-start','step-finish')
                or not _id(c['messageID']) or not _id(c['partID']) or c['partID'] in seen
                or (c['role']=='assistant' and not _id(c['parentID'])) or (c['kind'] in ('step-start','step-finish') and c['role']!='assistant')
                or (is_text and (type(c['synthetic']) is not bool or type(c['ignored']) is not bool or type(c['textSHA256']) is not str or not _HASH.fullmatch(c['textSHA256'])))
                or (c['kind']=='text' and (c['synthetic'] or c['ignored']))
                or (c['kind']=='context-text' and (c['role']!='user' or not c['synthetic'] or c['ignored']))
                or (c['kind']=='ignored-user' and (c['role']!='user' or not c['ignored']))
                or any(type(c[k]) is not int or c[k]<0 for k in ('messageCreated','messageUpdated','partCreated','partUpdated'))
                or any(type(c[k]) is not str or not _HASH.fullmatch(c[k]) for k in ('messageHash','partHash'))):return False
        identity=(c['role'],c.get('parentID'),c['messageCreated'],c['messageUpdated'],c['messageHash'])
        if messages.setdefault(c['messageID'],identity)!=identity:return False
        order=(c['messageCreated'],c['messageID'],c['partID'])
        if previous is not None and order<=previous:return False
        previous=order;seen.add(c['partID'])
        if c['kind']=='text':covered.append(c)
    if len(covered)!=len(value['items']):return False
    for at,(c,item) in enumerate(zip(covered,value['items'])):
        n=item['native'];pair=item['pair']
        if any(c[k]!=n[k] for k in ('role','messageID','partID','messageCreated','messageUpdated','partCreated','partUpdated','messageHash','partHash','textSHA256')):return False
        if at%2 and c['parentID']!=pair['input']['nativeMessageID']:return False
    users={value['items'][i]['pair']['input']['nativeMessageID'] for i in range(0,len(value['items']),2)}
    if any(c['kind'] in ('context-text','ignored-user') and c['messageID'] not in users for c in value['coverage']):return False
    for i in range(0,len(value['items']),2):
        item=value['items'][i];pair=item['pair'];wire=item['closure']['wire'];generated=wire['generated']
        expected=[*pair['input']['layout'],*({'position':g['position'],'sha256':g['sha256'],'synthetic':True} for g in generated)]
        actual=[{'position':j,'sha256':c['textSHA256'],'synthetic':c['synthetic']} for j,c in enumerate(c for c in value['coverage'] if c['messageID']==pair['input']['nativeMessageID'] and c['kind'] in ('text','context-text'))]
        if actual!=expected:return False
    return True


def import_historical(db, *, account, bundle):
    """Retain assertions as external data; never create an active local rating."""
    if schema_state(db)!='ready' or not _id(account):raise RatingUnavailable('rating profile unavailable')
    if (type(bundle) is not dict or set(bundle)!={'version','state','events'} or type(bundle['version']) is not int or bundle['version']!=1
        or bundle['state']!='ready' or type(bundle['events']) is not list or len(bundle['events'])>10000):
        raise RatingUnavailable('rating import unavailable')
    imported=0
    for event in bundle['events']:
        if type(event) is not dict or set(event)!=set(_EXPORT_FIELDS):raise RatingUnavailable('rating import unavailable')
        if (any(not _id(event[k]) for k in ('event_id','account_id','rater_account_id','chat_id','target_key','created_at'))
            or (event['workspace_id'] is not None and not _id(event['workspace_id'])) or event['target_kind'] not in ('message','chat','section')
            or type(event['descriptor_json']) is not str or len(event['descriptor_json'].encode())>262144 or type(event['descriptor_hash']) is not str or not _HASH.fullmatch(event['descriptor_hash'])
            or hashlib.sha256(event['descriptor_json'].encode()).hexdigest()!=event['descriptor_hash']
            or (event['target_kind']=='message' and not event['target_key'].startswith('native:') and (not _id(event['message_id']) or event['message_role'] not in ('user','assistant') or type(event['source_revision']) is not int or event['source_revision']<1 or type(event['source_hash']) is not str or not _HASH.fullmatch(event['source_hash'])))
            or ((event['target_kind']!='message' or event['target_key'].startswith('native:')) and any(event[k] is not None for k in ('message_id','message_role','source_revision','source_hash')))
            or event['action'] not in ('set','clear') or (event['action']=='set' and (type(event['value']) is not int or not 0<=event['value']<=10))
            or (event['action']=='clear' and event['value'] is not None) or (event['note'] is not None and (type(event['note']) is not str or len(event['note'].encode())>4096))
            or event['origin'] not in ('local','external_unverified') or (event['rating_revision'] is not None and (type(event['rating_revision']) is not int or event['rating_revision']<1))):
            raise RatingUnavailable('rating import unavailable')
        try: descriptor=json.loads(event['descriptor_json'])
        except (ValueError,TypeError):raise RatingUnavailable('rating import unavailable')
        if event['target_key']=='complete-paired:chat' or (type(descriptor) is dict and descriptor.get('scope')=='complete_paired_committed_text'):
            if event['target_kind']!='chat' or event['target_key']!='complete-paired:chat' or not _valid_complete_paired_import(descriptor):
                raise RatingUnavailable('rating import unavailable')
        if event['target_key'].startswith('paired:') or (type(descriptor) is dict and descriptor.get('scope')=='one_forward_turn_partial'):
            if (event['target_kind']!='section' or event['target_key']!='paired:'+event['descriptor_hash'] or not _valid_paired_import(descriptor)):
                raise RatingUnavailable('rating import unavailable')
        if event['origin']=='external_unverified':
            asserted=event['asserted_origin']
            if (event['rating_revision'] is not None or type(asserted) is not dict or set(asserted)!={'version','original_event'}
                or type(asserted['version']) is not int or asserted['version']!=1 or type(asserted['original_event']) is not dict):
                raise RatingUnavailable('rating import unavailable')
            original=asserted['original_event']
            preserved=('rater_account_id','chat_id','workspace_id','target_kind','target_key','descriptor_json','descriptor_hash','message_id','message_role','source_revision','source_hash','action','value','note','created_at')
            if (set(original)!=set(_EXPORT_FIELDS) or original.get('origin')!='local' or original.get('asserted_origin') is not None
                or any(original.get(k)!=event[k] for k in preserved)):
                raise RatingUnavailable('rating import unavailable')
        else:
            if event['asserted_origin'] is not None or type(event['rating_revision']) is not int:
                raise RatingUnavailable('rating import unavailable')
            original=event
        if (not _id(original.get('event_id')) or not _id(original.get('account_id')) or
            type(original.get('rating_revision')) is not int or original['rating_revision']<1):
            raise RatingUnavailable('rating import unavailable')
        encoded=json.dumps(original,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        if len(encoded.encode())>524288:raise RatingUnavailable('rating import unavailable')
        identity=hashlib.sha256(json.dumps(['s11.external.v1',account,original['account_id'],original['event_id']],separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        payload=hashlib.sha256(encoded.encode()).hexdigest()
        prior=db.execute(text('SELECT payload_hash,origin FROM chat_rating_events WHERE account_id=:account AND request_key_hash=:key'),{'account':account,'key':identity}).first()
        if prior:
            if prior[0]!=payload or prior[1]!='external_unverified':raise RatingConflict('rating import conflict')
            continue
        asserted=json.dumps({'version':1,'original_event':original},sort_keys=True,separators=(',',':'),ensure_ascii=False)
        db.execute(text("INSERT INTO chat_rating_events(event_id,account_id,rater_account_id,chat_id,workspace_id,target_kind,target_key,descriptor_json,descriptor_hash,message_id,message_role,source_revision,source_hash,rating_revision,action,value,note,origin,asserted_origin,request_key_hash,payload_hash,created_at) VALUES (:event,:account,:rater,:chat,:workspace,:kind,:target,:descriptor,:descriptor_hash,:message,:role,:revision,:hash,NULL,:action,:value,:note,'external_unverified',:asserted,:key,:payload,:created)"),
            {'event':identity,'account':account,'rater':event['rater_account_id'],'chat':event['chat_id'],'workspace':event['workspace_id'],'kind':event['target_kind'],'target':event['target_key'],'descriptor':event['descriptor_json'],'descriptor_hash':event['descriptor_hash'],'message':event['message_id'],
             'role':event['message_role'],'revision':event['source_revision'],'hash':event['source_hash'],'action':event['action'],'value':event['value'],
             'note':event['note'],'asserted':asserted,'key':identity,'payload':payload,'created':event['created_at']})
        imported+=1
    return {'state':'historical_unverified','imported':imported}
