"""One original managed utility's private, staging-only Git/Files exchange.

No caller can construct authority from these values. The owner supplies the
already admitted immutable identity, currentness predicate and Files custody.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import hmac
import json
import re
import os

_HEX = re.compile(r'^[0-9a-f]{64}$')
_OID = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_ID = re.compile(r'^[A-Za-z0-9_-]{16,80}$')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=False, separators=(',', ':'), allow_nan=False).encode('utf-8', 'strict')


def _path(value):
    if type(value) is not str or not value or len(value.encode('utf-8', 'strict')) > 4096 or value.startswith('/') or '\\' in value or '\0' in value:
        raise ValueError('worktree path unavailable')
    parts = value.split('/')
    if any(not part or part in {'.', '..', '.git', '.clanker', '.clankers', '.references'} for part in parts):
        raise ValueError('worktree path unavailable')
    return value


def _inventory(event):
    if type(event) is not dict or set(event) != {'kind', 'targetOID', 'treeOID', 'entries', 'directories', 'totalBytes'} or event['kind'] != 'begin':
        raise ValueError('worktree inventory unavailable')
    target, tree, entries, directories, total = (event[k] for k in ('targetOID','treeOID','entries','directories','totalBytes'))
    if type(target) is not str or type(tree) is not str or not _OID.fullmatch(target) or not _OID.fullmatch(tree) or len(target) != len(tree):
        raise ValueError('worktree source unavailable')
    if type(entries) is not list or type(directories) is not list or len(entries)+len(directories)+1 > 8192 or len(_encoded(event)) > 3*1024*1024:
        raise ValueError('worktree inventory bound')
    paths=[]; derived=set(); actual=0
    for item in entries:
        if type(item) is not dict or set(item) != {'path','mode','oid','size'}: raise ValueError('worktree inventory unavailable')
        path=_path(item['path']); mode=item['mode']; oid=item['oid']; size=item['size']
        if type(mode) is not int or mode not in (0o100644,0o100755) or type(oid) is not str or not _OID.fullmatch(oid) or len(oid)!=len(target) or type(size) is not int or not 0<=size<=32*1024*1024:
            raise ValueError('worktree inventory unavailable')
        paths.append(path); actual+=size
        parts=path.split('/')
        derived.update('/'.join(parts[:i]) for i in range(1,len(parts)))
    if paths != sorted(set(paths),key=lambda p:p.encode('utf-8')) or any(path in derived for path in paths): raise ValueError('worktree order unavailable')
    expected_dirs=sorted(derived,key=lambda p:p.encode('utf-8'))
    if directories!=expected_dirs or type(total) is not int or total!=actual or actual>511*1024*1024: raise ValueError('worktree totals unavailable')
    for path in directories: _path(path)
    rows=[[e['path'],e['mode'],e['oid'],e['size']] for e in entries]
    plan=hashlib.sha256(_encoded(['openclank.git-worktree.plan.v1',target,tree,rows,directories,total])).hexdigest()
    nodes={path:{'kind':'directory','path':path,'mode':0o755} for path in directories};offset=0
    for item in entries:
        nodes[item['path']]={'kind':'file','path':item['path'],'mode':item['mode']&0o777,'offset':offset,'length':item['size'],'sha256':'0'*64};offset+=item['size']
    manifest={'version':2,'source':{'commit_oid':target,'tree_oid':tree,'target_manifest_sha256':'0'*64},'entries':[nodes[k] for k in sorted(nodes,key=lambda p:p.encode('utf-8'))]+[{'kind':'git_metadata','path':'.git','mode':0o644,'offset':offset,'length':1024*1024,'sha256':'0'*64,'owner':'source_worktree'}]}
    if len(_encoded(manifest))>4*1024*1024: raise ValueError('worktree manifest bound')
    return plan


def _target_hash(entries,directories,hashes):
    nodes={path:(b'd',0o755,None) for path in directories}
    nodes.update({item['path']:(b'f',item['mode']&0o777,(item['size'],hashes[i])) for i,item in enumerate(entries)})
    digest=hashlib.sha256(b'openclank.worktree.target.v2\0')
    for path in sorted(nodes,key=lambda p:p.encode('utf-8')):
        kind,mode,blob=nodes[path];encoded=path.encode('utf-8');digest.update(kind);digest.update(len(encoded).to_bytes(4,'big'));digest.update(encoded);digest.update(mode.to_bytes(4,'big'))
        if blob is not None: digest.update(blob[0].to_bytes(8,'big'));digest.update(blob[1].encode('ascii'))
    return digest.hexdigest()


class WorktreeStagePhase:
    def __init__(self, *, identity, expected_begin, native_binding, custody, verify):
        if type(identity) is not dict or set(identity)!={'operationID','requestDigest','capability','actionSHA256','gitPlanDigest'} or not _ID.fullmatch(identity.get('operationID','')) or any(not _HEX.fullmatch(identity.get(k,'')) for k in ('requestDigest','capability','actionSHA256','gitPlanDigest')):
            raise ValueError('worktree identity unavailable')
        if _inventory(expected_begin)!=identity['gitPlanDigest'] or type(native_binding) is not dict or not callable(verify): raise ValueError('worktree plan unavailable')
        if set(native_binding)!={'operation_id','request_digest','git_plan_digest','root_id','root_path','root_object_tag'} or native_binding['operation_id']!=identity['operationID'] or native_binding['request_digest']!=identity['requestDigest'] or native_binding['git_plan_digest']!=identity['gitPlanDigest'] or type(native_binding['root_id']) is not str or not native_binding['root_id'] or len(native_binding['root_id'].encode('utf-8','strict'))>256 or type(native_binding['root_path']) is not str or not native_binding['root_path'].startswith('/') or len(native_binding['root_path'].encode('utf-8','strict'))>4096 or type(native_binding['root_object_tag']) is not str or not _HEX.fullmatch(native_binding['root_object_tag']): raise ValueError('worktree binding unavailable')
        self.identity=dict(identity);self.begin=json.loads(_encoded(expected_begin));self.native=dict(native_binding);self.custody=custody;self.verify=verify
        self.state='ready';self.invocation=None;self.ordinal=0;self.offset=0;self.blob=hashlib.sha256();self.hashes=[];self.target=None

    def arm_registration(self, *, action_sha256, private_identity, private_path, source_path):
        def canonical(value):
            return type(value) is str and re.fullmatch(r'(?:0|[1-9][0-9]{0,19})',value) and int(value)<=2**64-1
        if (self.state!='sealed' or type(action_sha256) is not str or not _HEX.fullmatch(action_sha256)
                or type(private_identity) is not dict or set(private_identity)!={'device','inode'}
                or not all(canonical(private_identity[k]) for k in ('device','inode')) or private_identity['inode']=='0'
                or any(type(path) is not str or not path.startswith('/') or len(path.encode('utf-8','strict'))>4096 for path in (private_path,source_path))):
            raise ValueError('worktree registration unavailable')
        self.identity['actionSHA256']=action_sha256;self.registration={'privateIdentity':dict(private_identity),'privatePath':private_path,'sourcePath':source_path}
        self.invocation=None;self.state='registration_ready'

    def arm_publication(self, *, action_sha256, destination_path, deadline):
        if self.state!='history_prepared' or not _HEX.fullmatch(action_sha256) or type(destination_path) is not str or not destination_path.startswith('/') or len(destination_path.encode('utf-8','strict'))+6>4096 or any(ord(c)<32 or ord(c)==127 for c in destination_path) or not isinstance(deadline,(int,float)):
            raise ValueError('worktree publication unavailable')
        self.identity['actionSHA256']=action_sha256;self.publication={'destinationPath':destination_path,'candidateSHA256':hashlib.sha256((destination_path+'/.git\n').encode('utf-8','strict')).hexdigest(),'deadline':deadline}
        self.invocation=None;self.state='publication_ready'

    async def dispatch(self, request):
        if type(request) is not dict or set(request)!={'version','operationID','requestDigest','capability','invocationID','actionSHA256','gitPlanDigest','event'} or request.get('version')!=1:
            return {'state':'refused'}
        if any(type(request.get(k)) is not str or not hmac.compare_digest(request[k],v) for k,v in self.identity.items()): return {'state':'refused'}
        invocation=request.get('invocationID');event=request.get('event')
        if type(invocation) is not str or not re.fullmatch(r'[0-9a-f]{32}',invocation) or type(event) is not dict: return {'state':'refused'}
        if self.invocation is not None and not hmac.compare_digest(self.invocation,invocation): return {'state':'refused'}
        if event.get('kind')=='status' and set(event)=={'kind'}:
            try:
                if self.invocation is None or await self.verify() is not True: return {'state':'refused'}
            except Exception: return {'state':'refused'}
            if self.state=='sealed': return {'state':'status','stage':'sealed','targetManifestSHA256':self.target}
            return {'state':'status','stage':'unknown' if self.state=='unknown' else 'pending'}
        if self.state in ('unknown','sealed','registered','completed'): return {'state':'unknown'}
        try:
            if len(_encoded(request))>(4*1024*1024 if event.get('kind')=='begin' else 256*1024) or await self.verify() is not True: raise ValueError('worktree currentness unavailable')
            if self.invocation is None: self.invocation=invocation
            kind=event.get('kind');items=self.begin['entries'];base=dict(self.native);phase_base={k:v for k,v in base.items() if k!='root_path'}
            if kind in ('before_backlink_lock','publication_lock_ready','before_backlink_commit','backlink_committed'):
                remaining=int((self.publication['deadline']-asyncio.get_running_loop().time())*1000)
                if remaining<=0:raise ValueError('worktree publication expired')
                registration=self.registration['witness'];candidate=self.publication['candidateSHA256']
                def valid_identity(value):
                    if type(value) is not dict or set(value)!={'device','inode'}:return False
                    return all(type(value[k]) is str and re.fullmatch(r'(?:0|[1-9][0-9]{0,19})',value[k]) and int(value[k])<=2**64-1 for k in ('device','inode')) and value['inode']!='0'
                def valid_file(value):
                    return type(value) is dict and set(value)=={'device','inode','size','sha256'} and valid_identity({'device':value.get('device'),'inode':value.get('inode')}) and type(value.get('size')) is int and 1<=value['size']<=4096 and type(value.get('sha256')) is str and bool(_HEX.fullmatch(value['sha256']))
                if kind=='before_backlink_lock' and self.state=='publication_ready' and set(event)=={'kind','adminName','adminIdentity','backlink','index'}:
                    if event['adminName']!=registration['adminName'] or event['adminIdentity']!=registration['adminIdentity'] or event['backlink']!=registration['backlink'] or event['index']!=registration['index']:raise ValueError('worktree admin changed')
                    data=(await self.custody.exchange('worktree_private_begin_publication',{**phase_base,'timeout_ms':min(300000,remaining)})).get('data')
                    if data!={'state':'proceed'}:raise ValueError('worktree lock intent unknown')
                    self.state='backlink_lock_intended';return {'state':'proceed'}
                if kind=='publication_lock_ready' and self.state=='backlink_lock_intended' and set(event)=={'kind','adminName','adminIdentity','lockIdentity','candidateSHA256','backlink'}:
                    if event['adminName']!=registration['adminName'] or event['adminIdentity']!=registration['adminIdentity'] or event['backlink']!=registration['backlink'] or not valid_identity(event['lockIdentity']) or event['candidateSHA256']!=candidate:raise ValueError('worktree lock changed')
                    witness={key:value for key,value in event.items() if key!='kind'}
                    data=(await self.custody.exchange('worktree_private_publish',{**phase_base,'timeout_ms':min(300000,remaining),'witness':witness})).get('data')
                    if data!={'state':'published'}:raise ValueError('worktree publication unknown')
                    self.publication['lockIdentity']=dict(event['lockIdentity']);self.state='published';return {'state':'published'}
                if kind=='before_backlink_commit' and self.state=='published' and set(event)=={'kind','adminIdentity','lockIdentity','candidateSHA256','index'}:
                    if event['adminIdentity']!=registration['adminIdentity'] or event['lockIdentity']!=self.publication['lockIdentity'] or event['candidateSHA256']!=candidate or event['index']!=registration['index']:raise ValueError('worktree commit changed')
                    data=(await self.custody.exchange('worktree_private_check_published',{**phase_base,'timeout_ms':min(300000,remaining)})).get('data')
                    if data!={'state':'proceed'}:raise ValueError('worktree published state unknown')
                    self.state='backlink_commit_ready';return {'state':'proceed'}
                if kind=='backlink_committed' and self.state=='backlink_commit_ready' and set(event)=={'kind','adminName','adminIdentity','backlink','head','index'}:
                    backlink=event['backlink'];expected=(self.publication['destinationPath']+'/.git\n').encode('utf-8','strict')
                    if event['adminName']!=registration['adminName'] or event['adminIdentity']!=registration['adminIdentity'] or event['head']!=registration['head'] or event['index']!=registration['index'] or not valid_file(backlink) or {'device':backlink['device'],'inode':backlink['inode']}!=self.publication['lockIdentity'] or backlink['sha256']!=candidate or backlink['size']!=len(expected):raise ValueError('worktree backlink changed')
                    witness={key:value for key,value in event.items() if key not in ('kind','index')}
                    data=(await self.custody.exchange('worktree_private_finish_publication',{**phase_base,'timeout_ms':min(300000,remaining),'witness':witness})).get('data')
                    if data!={'state':'completed'}:raise ValueError('worktree History completion unknown')
                    self.state='completed';return {'state':'completed'}
                raise ValueError('worktree publication phase unavailable')
            if kind=='before_registration' and self.state=='registration_ready' and set(event)=={'kind','privateIdentity'} and event['privateIdentity']==self.registration['privateIdentity']:
                data=(await self.custody.exchange('worktree_private_accept',{**phase_base,'phase':'before_registration'})).get('data')
                if type(data) is not dict or data.get('state')!='proceed':raise ValueError('worktree registration issue unknown')
                self.state='registration_issued';return {'state':'proceed'}
            if kind=='before_worktree_index' and self.state=='registration_issued' and set(event)=={'kind','privateIdentity','marker','adminName','adminIdentity','backlink','head','targetOID'} and event['targetOID']==self.begin['targetOID']:
                def identity(value):
                    if type(value) is not dict or set(value)!={'device','inode'}:return False
                    return all(type(value[k]) is str and re.fullmatch(r'(?:0|[1-9][0-9]{0,19})',value[k]) and int(value[k])<=2**64-1 for k in ('device','inode')) and value['inode']!='0'
                def file(value,marker=False):
                    keys={'device','inode','size','sha256'}|({'bytesBase64'} if marker else set())
                    return type(value) is dict and set(value)==keys and identity({'device':value.get('device'),'inode':value.get('inode')}) and type(value.get('size')) is int and 1<=value['size']<=4096 and type(value.get('sha256')) is str and bool(_HEX.fullmatch(value['sha256']))
                name=event['adminName'];marker=event['marker'];backlink=event['backlink'];head=event['head']
                if event['privateIdentity']!=self.registration['privateIdentity'] or not identity(event['adminIdentity']) or not file(marker,True) or not file(backlink) or not file(head) or type(name) is not str or not name or len(name.encode('utf-8','strict'))>255 or name in ('.','..') or '/' in name or '\\' in name or any(ord(ch)<32 or ord(ch)==127 for ch in name):raise ValueError('registration witness unavailable')
                raw=base64.b64decode(marker['bytesBase64'],validate=True) if type(marker['bytesBase64']) is str else b''
                expected=('gitdir: '+os.path.join(self.registration['sourcePath'],'.git','worktrees',name)+'\n').encode('utf-8','strict')
                back=(self.registration['privatePath']+'/.git\n').encode('utf-8','strict');target=(self.begin['targetOID']+'\n').encode('ascii')
                if raw!=expected or len(raw)!=marker['size'] or base64.b64encode(raw).decode()!=marker['bytesBase64'] or hashlib.sha256(raw).hexdigest()!=marker['sha256'] or backlink['size']!=len(back) or backlink['sha256']!=hashlib.sha256(back).hexdigest() or head['size']!=len(target) or head['sha256']!=hashlib.sha256(target).hexdigest():raise ValueError('registration witness changed')
                witness={key:value for key,value in event.items() if key not in ('kind','targetOID')}
                data=(await self.custody.exchange('worktree_private_accept',{**phase_base,'phase':'registered','witness':witness})).get('data')
                if type(data) is not dict or data.get('state')!='registered':raise ValueError('registration outcome unknown')
                data=(await self.custody.exchange('worktree_private_accept',{**phase_base,'phase':'before_index','target_oid':self.begin['targetOID']})).get('data')
                if data!={'state':'proceed'}:raise ValueError('index issue unknown')
                self.registration['provisional']=witness;self.state='index_issued';return {'state':'proceed'}
            if kind=='registered' and self.state=='index_issued' and set(event)=={'kind','privateIdentity','marker','adminName','adminIdentity','backlink','head','index'}:
                witness={key:value for key,value in event.items() if key not in ('kind','index')};index=event['index']
                if witness!=self.registration['provisional'] or type(index) is not dict or set(index)!={'device','inode','size','sha256'} or any(type(index.get(k)) is not str or not re.fullmatch(r'(?:0|[1-9][0-9]{0,19})',index[k]) or int(index[k])>2**64-1 for k in ('device','inode')) or index['inode']=='0' or type(index['size']) is not int or not 1<=index['size']<=8*1024*1024 or type(index['sha256']) is not str or not _HEX.fullmatch(index['sha256']):raise ValueError('index witness unavailable')
                data=(await self.custody.exchange('worktree_private_accept',{**phase_base,'phase':'index_ready','index':index})).get('data')
                if data!={'state':'registered'}:raise ValueError('index outcome unknown')
                self.registration['witness']={**witness,'index':index};self.state='registered';return {'state':'registered'}
            if kind=='begin' and self.state=='ready' and event==self.begin and _inventory(event)==self.identity['gitPlanDigest']:
                data=(await self.custody.exchange('worktree_stage_begin',{**base,'commit_oid':event['targetOID'],'tree_oid':event['treeOID'],'entries':event['entries'],'directories':event['directories'],'total_bytes':event['totalBytes']})).get('data')
                if type(data) is not dict or data.get('state')!='accepted' or data.get('next_ordinal')!=0 or data.get('next_offset')!=0: raise ValueError('worktree admission unknown')
                self.state='streaming';return {'state':'accepted','nextOrdinal':0,'nextOffset':0}
            if kind=='blob_chunk' and self.state=='streaming' and set(event)=={'kind','ordinal','offset','contentBase64'} and self.ordinal<len(items) and type(event['ordinal']) is int and type(event['offset']) is int and event['ordinal']==self.ordinal and event['offset']==self.offset:
                text=event['contentBase64'];raw=base64.b64decode(text,validate=True) if type(text) is str else b''
                if not raw or len(raw)>128*1024 or base64.b64encode(raw).decode()!=text or self.offset+len(raw)>items[self.ordinal]['size']: raise ValueError('worktree chunk unavailable')
                data=(await self.custody.exchange('worktree_stage_chunk',{**phase_base,'ordinal':self.ordinal,'offset':self.offset,'content_base64':text})).get('data')
                if type(data) is not dict or data.get('state')!='accepted' or data.get('next_ordinal')!=self.ordinal or data.get('next_offset')!=self.offset+len(raw): raise ValueError('worktree write unknown')
                self.blob.update(raw);self.offset+=len(raw);return {'state':'accepted','nextOrdinal':self.ordinal,'nextOffset':self.offset}
            if kind=='blob_end' and self.state=='streaming' and set(event)=={'kind','ordinal','gitOID','sha256'} and self.ordinal<len(items) and type(event['ordinal']) is int and event['ordinal']==self.ordinal:
                item=items[self.ordinal];digest=self.blob.hexdigest()
                if self.offset!=item['size'] or event['gitOID']!=item['oid'] or event['sha256']!=digest: raise ValueError('worktree blob changed')
                data=(await self.custody.exchange('worktree_stage_end',{**phase_base,'ordinal':self.ordinal,'git_oid':item['oid'],'sha256':digest})).get('data')
                if type(data) is not dict or data.get('state')!='accepted' or data.get('next_ordinal')!=self.ordinal+1 or data.get('next_offset')!=0: raise ValueError('worktree blob unknown')
                self.hashes.append(digest);self.ordinal+=1;self.offset=0;self.blob=hashlib.sha256();return {'state':'accepted','nextOrdinal':self.ordinal,'nextOffset':0}
            if kind=='seal_target' and self.state=='streaming' and set(event)=={'kind'} and self.ordinal==len(items) and self.offset==0:
                expected=_target_hash(items,self.begin['directories'],self.hashes);data=(await self.custody.exchange('worktree_stage_seal',phase_base)).get('data')
                if type(data) is not dict or data.get('state')!='sealed' or data.get('target_manifest_sha256')!=expected: raise ValueError('worktree seal unknown')
                self.target=expected;self.state='sealed';return {'state':'sealed','targetManifestSHA256':expected}
        except asyncio.CancelledError:
            self.state='unknown';raise
        except Exception:
            self.state='unknown';return {'state':'unknown'}
        self.state='unknown';return {'state':'unknown'}
