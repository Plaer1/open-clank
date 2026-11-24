"""Bounded host-owned whole-plan comments; no delivery authority."""
import hashlib
import json
import uuid
import re
import math
from src.openclank.transcript_projection import mutate_plan_fields

MAX_ENTRIES = 64
MAX_BYTES = 384 * 1024


def _utf8(value, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('feedback text is required')
    try: raw = value.encode('utf-8')
    except UnicodeEncodeError as exc: raise ValueError('feedback must be valid UTF-8') from exc
    if len(raw) > limit: raise ValueError('feedback exceeds its size limit')
    return raw


def _target(plan, binding, owner, session_id):
    if not isinstance(plan, dict) or plan.get('status') == 'cleared' or not plan.get('revision') or not plan.get('digest'):
        raise ValueError('plan target unavailable')
    if not isinstance(binding, dict) or binding.get('owner') != owner or binding.get('stableChatID') != session_id:
        raise ValueError('managed binding unavailable')
    return {'revision': plan['revision'], 'digest': plan['digest'], 'artifact': plan.get('artifact_relpath'),
            'artifact_revision': int(plan.get('artifact_path_revision') or 0), 'binding': binding}


def _base(target):
    return {key:target[key] for key in ('revision','digest','artifact','artifact_revision','binding')}


def _file_target(value):
    fields={'kind','workspace','relativePath','resourceID','revision','sha256','selection'}
    if type(value) is not dict or set(value)!=fields or value['kind'] not in ('file','selection'):
        raise ValueError('feedback file target unavailable')
    if any(not isinstance(value[k],str) or not value[k] or len(value[k].encode('utf-8'))>1024 for k in ('workspace','relativePath','resourceID')) or not re.fullmatch(r'[0-9a-f]{64}',str(value['sha256'])):
        raise ValueError('feedback file target unavailable')
    revision=value['revision']
    if type(revision) is not dict or set(revision)!={'kind','value'} or revision.get('kind')!='hostFingerprint' or not isinstance(revision.get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}:[0-9]+',revision['value']):raise ValueError('feedback file target unavailable')
    selection=value['selection']
    if value['kind']=='file':
        if selection is not None:raise ValueError('feedback file target unavailable')
    elif (type(selection) is not dict or set(selection)!={'start','end','sha256'} or type(selection['start']) is not int or type(selection['end']) is not int or selection['start']<0 or not 0<selection['end']-selection['start']<=8192 or not re.fullmatch(r'[0-9a-f]{64}',str(selection['sha256']))):raise ValueError('feedback selection unavailable')
    return value



def _anchor(value, resource_id):
    if type(value) is not dict or type(value.get('kind')) is not str: raise ValueError('feedback anchor unavailable')
    kind=value['kind']
    shapes={'whole':{'kind'},'text':{'kind','start','end','sha256'},'dom':{'kind','sourceID','nodeID'},
            'image':{'kind','x','y','width','height'},'cell':{'kind','workbookID','sheetID','range'},
            'slide':{'kind','deckID','slideID','objectID'},'frame':{'kind','timeMs'},'time_range':{'kind','startMs','endMs'}}
    if kind not in shapes or set(value)!=shapes[kind]: raise ValueError('feedback anchor unavailable')
    def bounded(x): return type(x) is str and bool(x) and len(x.encode('utf-8'))<=512
    if kind=='text' and (type(value['start']) is not int or type(value['end']) is not int or value['start']<0 or not 0<value['end']-value['start']<=8192 or type(value['sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}',value['sha256'])): raise ValueError('feedback anchor unavailable')
    if kind=='image' and (any(type(value[k]) not in (int,float) or not math.isfinite(value[k]) for k in ('x','y','width','height')) or not 0<=value['x']<1 or not 0<=value['y']<1 or not 0<value['width']<=1-value['x'] or not 0<value['height']<=1-value['y']): raise ValueError('feedback anchor unavailable')
    if kind=='frame' and (type(value['timeMs']) is not int or not 0<=value['timeMs']<=86400000): raise ValueError('feedback anchor unavailable')
    if kind=='time_range' and (type(value['startMs']) is not int or type(value['endMs']) is not int or not 0<=value['startMs']<value['endMs']<=86400000): raise ValueError('feedback anchor unavailable')
    if kind in ('dom','slide') and any(not bounded(value[k]) for k in shapes[kind]-{'kind'}): raise ValueError('feedback anchor unavailable')
    if kind=='cell':
        cell=value['range']
        if value['workbookID']!=resource_id or not bounded(value['sheetID']) or type(cell) is not dict or set(cell)!={'startCellID','endCellID'} or not all(bounded(cell[k]) for k in cell): raise ValueError('feedback anchor unavailable')
    return value



def _scope(value):
    common={'version','actorID','executionRevision','mapRevision','mappingRevision','workerGeneration','holderID'}
    if type(value) is not dict: raise ValueError('feedback scope unavailable')
    version=value.get('version')
    if type(version) is not int or version not in (1,2): raise ValueError('feedback scope unavailable')
    keys=common if version==1 else common|{'actorSessionID','rootSessionID'}
    if (set(value)!=keys or (version==1 and value['actorID']!='main')
        or not isinstance(value['actorID'],str) or not 0<len(value['actorID'].encode('utf-8'))<=200
        or any(type(value[k]) is not int or not 0<=value[k]<=9007199254740991 for k in ('executionRevision','mapRevision','mappingRevision','workerGeneration'))
        or not isinstance(value['holderID'],str) or not 0<len(value['holderID'].encode('utf-8'))<=256
        or (version==2 and any(not isinstance(value[k],str) or not 0<len(value[k].encode('utf-8'))<=256 for k in ('actorSessionID','rootSessionID')))):
        raise ValueError('feedback scope unavailable')
    return value


async def capture_feedback_scope(request,session_id,owner,account,session_manager, *, actor_id=None, actor_session_id=None, current_authority=None):
    """Read one actual ready main execution; expected identifiers confer no authority."""
    from routes.orchestration_routes import _get_observation_binding, _ensure_observation_binding_current
    actual,supervisor,binding,mapping=_get_observation_binding(request,session_id,None,session_manager)
    auth=request.app.state.auth_manager
    if actual!=owner or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner: raise ValueError('feedback scope unavailable')
    selected = actor_id is not None or actor_session_id is not None
    if selected and (not isinstance(actor_id,str) or not actor_id or not isinstance(actor_session_id,str) or not actor_session_id): raise ValueError('feedback actor unavailable')
    current=lambda:auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and (current_authority is None or current_authority() is True)
    if not current(): raise ValueError('feedback scope unavailable')
    original=inspect_resource_binding(session_id,owner,current_authority=current)
    if original.get('engineSessionID')!=binding.engine_session_id or original.get('mapRevision')!=binding.map_revision or not isinstance(original.get('physicalCwd'),str): raise ValueError('feedback scope unavailable')
    pool=supervisor.owner_lifecycle_pool(); lease,state,bridge,holder,generation=await pool.acquire_rating_operation(owner=owner)
    worker=lease.worker; process=getattr(worker,'_proc',None); key=pool._key(owner)
    try:
        if process is None or bridge.mapped_session_id(session_id)!=binding.engine_session_id or bridge.mapped_session_workspace(session_id)!=original['physicalCwd']: raise ValueError('feedback scope unavailable')
        target_session=actor_session_id if selected else binding.engine_session_id
        response=await worker.session_observation_request(session_id,target_session,'actor',100,None,owner=owner,timeout=5)
        if response.status_code!=200: raise ValueError('feedback scope unavailable')
        page=response.json()
        if type(page) is not dict or type(page.get('items')) is not list or len(page['items'])>100: raise ValueError('feedback scope unavailable')
        actors=[item for item in page['items'] if type(item) is dict and item.get('actorID')==(actor_id if selected else 'main') and item.get('sessionID')==target_session]
        if len(actors)!=1 or type(actors[0].get('executionRevision')) is not int or actors[0]['executionRevision']<1: raise ValueError('feedback scope unavailable')
        context=actors[0].get('hostContext')
        if type(context) is not dict or context.get('chatID')!=session_id: raise ValueError('feedback scope unavailable')
        if selected and (actors[0].get('status')!='running' or not isinstance(original.get('authorityWorkspaceID'),str) or not original['authorityWorkspaceID'] or context.get('workspaceID')!=original['authorityWorkspaceID']): raise ValueError('feedback actor unavailable')
        _ensure_observation_binding_current(mapping,owner,session_id,binding)
        pool._assert_owner_lifecycle(key,lease._owner_epoch)
        if inspect_resource_binding(session_id,owner,current_authority=current)!=original or bridge.mapped_session_workspace(session_id)!=original['physicalCwd']: raise ValueError('feedback scope changed')
        if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner or supervisor.owner_lifecycle_pool() is not pool or pool._states.get(key) is not state or state.active is not worker or state.status!='ready' or state.generation!=generation or not worker.is_alive() or worker._proc is not process or worker._bridge is not bridge or getattr(worker,'installed_generation',None)!=generation or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder or getattr(bridge,'_managed_provider_generation',None)!=holder or bridge.mapped_session_id(session_id)!=binding.engine_session_id): raise ValueError('feedback scope changed')
        if not current(): raise ValueError('feedback scope changed')
        result = _scope({'version':2 if selected else 1,'actorID':actor_id if selected else 'main','executionRevision':actors[0]['executionRevision'],'mapRevision':binding.map_revision,'mappingRevision':binding.binding_revision,'workerGeneration':generation,'holderID':holder,**({'actorSessionID':target_session,'rootSessionID':binding.engine_session_id} if selected else {})})
    finally:
        await lease.release()
    if not current(): raise ValueError('feedback scope changed')
    return result

async def recapture_feedback_scope(request,session_id,owner,account,session_manager,scope, *, current_authority=None):
    scope=_scope(scope)
    if scope['version']==1: return await capture_feedback_scope(request,session_id,owner,account,session_manager,current_authority=current_authority)
    return await capture_feedback_scope(request,session_id,owner,account,session_manager,actor_id=scope['actorID'],actor_session_id=scope['actorSessionID'],current_authority=current_authority)


def _design_context(value, resource):
    from src.openclank.deliverable_appearance import validate_deliverable_appearance
    base={'version','standard','appearance','assets','output'}
    if type(value) is not dict or type(value.get('version')) is not int or value['version'] not in (1,2): raise ValueError('design context unavailable')
    allowed=base | ({'standardSource'} if value['version']==2 else set())
    if set(value) not in (allowed,allowed|{'appearanceAssetIndex'}): raise ValueError('design context unavailable')
    appearance=validate_deliverable_appearance(value['appearance'])
    if value['standard']!=appearance['standard']: raise ValueError('design standard changed')
    output=value['output']
    if type(output) is not dict or output.get('format') not in ('html','svg'): raise ValueError('design destination unavailable')
    if output.get('destination')=='source':
        if set(output)!={'format','destination'} or not resource['relativePath'].lower().endswith('.'+output['format']): raise ValueError('design destination unavailable')
    elif output.get('destination')=='new-file':
        if set(output)!={'format','destination','folder','name','collision'} or output['collision']!='fail': raise ValueError('design destination unavailable')
        name=output['name']; folder=output['folder']
        if (type(name) is not str or not name or name!=name.strip() or len(name.encode('utf-8'))>240 or name in ('.','..')
            or any(ch in name for ch in ('/','\\','\x00')) or any(ord(ch)<32 or ord(ch)==127 for ch in name)
            or not name.lower().endswith('.'+output['format'])): raise ValueError('design destination unavailable')
        if (type(folder) is not dict or set(folder)!={'workspace','relativePath','resourceID','revision'}
            or folder['workspace']!=resource['workspace'] or not isinstance(folder['resourceID'],str) or not folder['resourceID']
            or not isinstance(folder['relativePath'],str) or not folder['relativePath'] or len(folder['relativePath'].encode('utf-8'))>1024
            or folder['relativePath'].startswith('/') or '\\' in folder['relativePath'] or any(p in ('','..') for p in folder['relativePath'].split('/'))
            or type(folder['revision']) is not dict or set(folder['revision'])!={'kind','value'} or folder['revision'].get('kind')!='hostDirectoryRevision'
            or not isinstance(folder['revision'].get('value'),str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',folder['revision']['value'])): raise ValueError('design destination unavailable')
    else: raise ValueError('design destination unavailable')
    assets=value['assets']
    if type(assets) is not list or len(assets)>4: raise ValueError('design assets unavailable')
    seen={resource['resourceID']}
    for asset in assets:
        if type(asset) is not dict or set(asset)!={'workspace','relativePath','resourceID','revision','sha256'}: raise ValueError('design asset unavailable')
        _file_target({'kind':'file',**asset,'selection':None})
        if asset['workspace']!=resource['workspace'] or asset['resourceID'] in seen: raise ValueError('design asset scope changed')
        seen.add(asset['resourceID'])
    if 'appearanceAssetIndex' in value and (type(value['appearanceAssetIndex']) is not int or not 0<=value['appearanceAssetIndex']<len(assets)): raise ValueError('appearance source unavailable')
    source=value.get('standardSource')
    if value['version']==2:
        if type(source) is not dict or set(source)!={'resource','id','version','sha256'} or source['id']!='open-clank.deliverable-design' or type(source['version']) is not str or not re.fullmatch(r'1\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})',source['version']): raise ValueError('standard source unavailable')
        row=source['resource']
        if type(row) is not dict or set(row)!={'workspace','relativePath','resourceID','revision','sha256'}: raise ValueError('standard source unavailable')
        _file_target({'kind':'file',**row,'selection':None})
        if row['workspace']!=resource['workspace'] or row['resourceID'] in seen or source['sha256']!=row['sha256']: raise ValueError('standard source changed')
    result={'version':value['version'],'standard':dict(value['standard']),'appearance':appearance,'assets':assets,'output':dict(output),**({'appearanceAssetIndex':value['appearanceAssetIndex']} if 'appearanceAssetIndex' in value else {}),**({'standardSource':source} if value['version']==2 else {})}
    if len(json.dumps(result,ensure_ascii=True,separators=(',',':')).encode())>24576: raise ValueError('design context too large')
    return result


def _design_input(target, verified_standard=None):
    from pathlib import Path
    from src.constants import STATIC_DIR
    design=_design_context(target['designContext'],target['resource'])
    if design['version']==2:
        standard=verified_standard
        if type(standard) is not dict or standard.get('id')!=design['standardSource']['id'] or standard.get('version')!=design['standardSource']['version']: raise ValueError('design standard unavailable')
    else:
        data=(Path(STATIC_DIR)/'brand/open-clank/design/standard.json').read_bytes()
        if len(data)>65536 or hashlib.sha256(data).hexdigest()!=design['standard']['sha256']: raise ValueError('design standard changed')
        standard=json.loads(data.decode('utf-8','strict'))
    return {'standard':standard,**({'standardSource':design['standardSource'],'standardAssets':'declared_only_unverified'} if design['version']==2 else {}),'appearance':design['appearance'],'source':target['resource'],'anchor':target['anchor'],'assets':design['assets'],'output':design['output'],**({'appearanceAssetIndex':design['appearanceAssetIndex']} if 'appearanceAssetIndex' in design else {})}


def _resource_target(value):
    if type(value) is not dict or (set(value)-{'anchorVerification'}) not in ({'kind','binding','resource','anchor'},{'kind','binding','resource','anchor','scope'},{'kind','binding','resource','anchor','scope','designContext'}) or value['kind']!='resource' or type(value['binding']) is not dict: raise ValueError('feedback target unavailable')
    if 'anchorVerification' in value and ((type(value['anchorVerification']) is not dict or set(value['anchorVerification']) != {'version','kind'} or type(value['anchorVerification']['version']) is not int or value['anchorVerification'] != {'version':1,'kind':'source-object'}) or value['anchor'].get('kind') not in ('cell','slide')): raise ValueError('feedback verification unavailable')
    resource=value['resource']
    if type(resource) is not dict: raise ValueError('feedback target unavailable')
    if set(resource)=={'provider','originID','resourceID','revision','sha256'}:
        revision=resource['revision']
        if resource['provider']!='files' or not isinstance(resource['originID'],str) or not re.fullmatch(r'image:[A-Za-z0-9._-]{1,240}',resource['originID']) or not isinstance(resource['resourceID'],str) or not resource['resourceID'] or len(resource['resourceID'])>512 or type(revision) is not dict or set(revision)!={'kind','value'} or revision['kind']!='imageSha256' or revision['value']!='sha256:'+str(resource['sha256']) or not re.fullmatch(r'[0-9a-f]{64}',str(resource['sha256'])) or value['anchor'].get('kind')!='image' or 'designContext' in value: raise ValueError('feedback image unavailable')
    else:
        if set(resource)!={'workspace','relativePath','resourceID','revision','sha256'}: raise ValueError('feedback target unavailable')
        _file_target({'kind':'file',**resource,'selection':None})
    _anchor(value['anchor'],resource['resourceID'])
    if 'scope' in value:_scope(value['scope'])
    if 'designContext' in value:_design_context(value['designContext'],resource)
    return value


def _resource_batch_target(value):
    if (type(value) is not dict or set(value)!={'kind','version','binding','scope','items'}
            or value.get('kind')!='resource_batch' or type(value.get('version')) is not int or value['version']!=1
            or type(value.get('binding')) is not dict or type(value.get('items')) is not list or not 2<=len(value['items'])<=8):
        raise ValueError('feedback batch unavailable')
    _scope(value['scope']); ids=set(); targets=set()
    for item in value['items']:
        base={'feedbackID','commentRevision','text','contentSHA256','resource','anchor'}
        if type(item) is not dict or set(item) not in (base,base|{'anchorVerification'}): raise ValueError('feedback batch unavailable')
        if (not isinstance(item['feedbackID'],str) or not re.fullmatch(r'[0-9a-f]{32}',item['feedbackID'])
                or item['feedbackID'] in ids or type(item['commentRevision']) is not int or item['commentRevision']<1
                or hashlib.sha256(_utf8(item['text'],4096)).hexdigest()!=item['contentSHA256']): raise ValueError('feedback batch unavailable')
        single={'kind':'resource','binding':value['binding'],'scope':value['scope'],'resource':item['resource'],'anchor':item['anchor']}
        if 'anchorVerification' in item:single['anchorVerification']=item['anchorVerification']
        _resource_target(single)
        key=(item['resource']['resourceID'],json.dumps(item['anchor'],ensure_ascii=True,sort_keys=True,separators=(',',':')))
        if key in targets:raise ValueError('feedback batch unavailable')
        ids.add(item['feedbackID']);targets.add(key)
    return value


def _batch_members(entries, account, session_id, members):
    if (type(members) is not list or not 2<=len(members)<=8
            or any(type(m) is not dict or set(m)!={'feedbackID','commentRevision'} or not isinstance(m['feedbackID'],str)
                   or not re.fullmatch(r'[0-9a-f]{32}',m['feedbackID']) or type(m['commentRevision']) is not int or m['commentRevision']<1 for m in members)
            or len({m['feedbackID'] for m in members})!=len(members)):
        raise ValueError('feedback batch unavailable')
    items=[];binding=None;scope=None
    for member in members:
        entry=next((e for e in entries if e['id']==member['feedbackID'] and e['account']==account and e['chat']==session_id),None)
        if (not entry or entry['comment_revision']!=member['commentRevision'] or entry['delivery']!='saved'
                or entry['target'].get('kind')!='resource' or 'scope' not in entry['target'] or 'designContext' in entry['target']):
            raise ValueError('feedback batch member changed')
        target=entry['target'];_resource_target(target)
        if binding is None:binding=target['binding'];scope=target['scope']
        if target['binding']!=binding or target['scope']!=scope:raise ValueError('feedback batch scope changed')
        items.append({'feedbackID':entry['id'],'commentRevision':entry['comment_revision'],'text':entry['text'],
            'contentSHA256':entry['content_sha256'],'resource':target['resource'],'anchor':target['anchor'],
            **({'anchorVerification':target['anchorVerification']} if 'anchorVerification' in target else {})})
    return _resource_batch_target({'kind':'resource_batch','version':1,'binding':binding,'scope':scope,'items':items})


def inspect_batch_members(session_id,owner,account,*,request_key,members,current_authority):
    _utf8(request_key,128)
    def apply(fields,binding):
        binding=_binding(binding,owner,session_id);entries=_entries(fields['plan_feedback'])
        for entry in entries:
            if entry['account']!=account or entry['chat']!=session_id:continue
            if any(req['key']==request_key for req in entry['requests']):
                target=entry['target']
                if target.get('kind')!='resource_batch' or [{'feedbackID':i['feedbackID'],'commentRevision':i['commentRevision']} for i in target['items']]!=members:
                    raise ValueError('feedback request conflict')
                return {'existing':_public(entry,None)}
        target=_batch_members(entries,account,session_id,members)
        if target['binding']!=binding:raise ValueError('feedback batch changed')
        return {'target':target}
    return mutate_plan_fields(session_id,owner,apply,current_authority=current_authority)


def save_resource_batch(session_id,owner,account,*,request_key,members,observed_target,verified_members,current_authority):
    _utf8(request_key,128);label='Feedback batch';digest=hashlib.sha256(label.encode()).hexdigest()
    def apply(fields,binding):
        binding=_binding(binding,owner,session_id);entries=_entries(fields['plan_feedback'])
        for entry in entries:
            if entry['account']==account and entry['chat']==session_id and any(req['key']==request_key for req in entry['requests']):
                target=entry['target']
                if target.get('kind')!='resource_batch' or [{'feedbackID':i['feedbackID'],'commentRevision':i['commentRevision']} for i in target['items']]!=members:
                    raise ValueError('feedback request conflict')
                return _public(entry,None)
        target=_batch_members(entries,account,session_id,members)
        if target!=observed_target or target['binding']!=binding:raise ValueError('feedback batch changed')
        expected=[{k:i[k] for k in ('resource','anchor','anchorVerification') if k in i} for i in target['items']]
        if verified_members!=expected or len(entries)>=MAX_ENTRIES:raise ValueError('feedback batch changed')
        entry={'id':uuid.uuid4().hex,'account':account,'chat':session_id,'text':label,'content_sha256':digest,
            'comment_revision':1,'target':target,'delivery':'saved','requests':[{'key':request_key,'sha256':digest,'text':label,'revision':1}]}
        entries.append(entry);value={'version':1,'entries':entries}
        if len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>MAX_BYTES:raise ValueError('feedback store full')
        fields['plan_feedback']=value
        return _public(entry,None)
    return mutate_plan_fields(session_id,owner,apply,current_authority=current_authority)


def _binding(binding,owner,session_id):
    if not isinstance(binding,dict) or binding.get('owner')!=owner or binding.get('stableChatID')!=session_id: raise ValueError('managed binding unavailable')
    return binding


def inspect_resource_binding(session_id,owner,*,current_authority):
    return mutate_plan_fields(session_id,owner,lambda fields,binding:_binding(binding,owner,session_id),current_authority=current_authority)


def save_resource(session_id,owner,account,*,request_key,text,resource,observed_binding,observed_scope,design_context=None,feedback_id=None,comment_revision=None,retarget=False,current_authority):
    _utf8(request_key,128); digest=hashlib.sha256(_utf8(text,4096)).hexdigest()
    if not account or type(retarget) is not bool: raise ValueError('feedback target unavailable')
    def apply(fields,binding):
        binding=_binding(binding,owner,session_id)
        if binding!=observed_binding: raise ValueError('feedback target changed')
        target=_resource_target({'kind':'resource','binding':binding,'scope':_scope(observed_scope),**resource,**({'designContext':design_context} if design_context is not None else {})}); entries=_entries(fields['plan_feedback'])
        for item in entries:
            if item['account']!=account or item['chat']!=session_id: continue
            for prior in item['requests']:
                if prior['key']==request_key:
                    if prior['sha256']!=digest or item['target']!=target or (feedback_id and item['id']!=feedback_id): raise ValueError('feedback request conflict')
                    return _public(item,None) | {'text':prior['text'],'comment_revision':prior['revision'],'content_sha256':prior['sha256']}
        if feedback_id:
            entry=next((item for item in entries if item['id']==feedback_id and item['account']==account and item['chat']==session_id),None)
            if not entry or entry['target'].get('kind')!='resource' or type(comment_revision) is not int or entry['comment_revision']!=comment_revision or entry['delivery']!='saved': raise ValueError('feedback revision changed')
            # A body-only edit of a saved legacy target retains its original
            # unprofiled identity; the new proof does not silently promote it.
            prior_target=entry['target']
            if (not retarget and 'anchorVerification' not in prior_target and 'anchorVerification' in target
                    and {k:v for k,v in target.items() if k!='anchorVerification'} == prior_target):
                target=prior_target
            if prior_target!=target and not retarget: raise ValueError('explicit retarget required')
            if len(entry['requests'])>=64: raise ValueError('feedback request history full')
            entry.update(target=target,text=text,content_sha256=digest,comment_revision=comment_revision+1)
        else:
            if retarget or len(entries)>=MAX_ENTRIES: raise ValueError('feedback store full')
            entry={'id':uuid.uuid4().hex,'account':account,'chat':session_id,'text':text,'content_sha256':digest,'comment_revision':1,'target':target,'delivery':'saved','requests':[]}; entries.append(entry)
        entry['requests'].append({'key':request_key,'sha256':digest,'text':text,'revision':entry['comment_revision']})
        value={'version':1,'entries':entries}
        if len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>MAX_BYTES: raise ValueError('feedback store full')
        fields['plan_feedback']=value
        return _public(entry,None)
    return mutate_plan_fields(session_id,owner,apply,current_authority=current_authority)

def _queue_delivery_proof(value):
    if (type(value) is not dict or set(value) != {'version', 'kind', 'receipt_id', 'delivery_message_id'}
            or type(value.get('version')) is not int or value['version'] != 1
            or value.get('kind') != 'queue_delivery'
            or any(type(value.get(k)) is not str or not value[k] or len(value[k]) > 256
                   for k in ('receipt_id', 'delivery_message_id'))):
        raise ValueError('feedback queue proof unavailable')
    return dict(value)

def _entries(value):
    if value is None: return []
    if type(value) is not dict or set(value) != {'version', 'entries'} or value['version'] != 1 or type(value['entries']) is not list or len(value['entries']) > MAX_ENTRIES:
        raise ValueError('feedback store unavailable')
    try:
        if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_BYTES:
            raise ValueError('feedback store unavailable')
        ids, keys = set(), set()
        for item in value['entries']:
            required = {'id','account','chat','text','content_sha256','comment_revision','target','delivery','requests'}
            if type(item) is not dict or not required.issubset(item) or set(item) - required - {'intent','delivery_proof'}: raise ValueError('feedback store unavailable')
            if 'delivery_proof' in item:
                _queue_delivery_proof(item['delivery_proof'])
                if item['delivery'] != 'consumed': raise ValueError('feedback store unavailable')
            if (any(type(item[k]) is not str or not item[k] or len(item[k]) > 512 for k in ('id','account','chat'))
                    or item['id'] in ids or type(item['comment_revision']) is not int or item['comment_revision'] < 1
                    or item['delivery'] not in ('saved','unknown','persisted','pending-boundary','consumed','cancelled','failed')
                    or type(item['requests']) is not list or not 1 <= len(item['requests']) <= 64
                    or type(item['target']) is not dict or (set(item['target'])-({'anchorVerification'} if item['target'].get('kind')=='resource' else set())) not in ({'revision','digest','artifact','artifact_revision','binding'},{'revision','digest','artifact','artifact_revision','binding','file'},{'kind','binding','resource','anchor'},{'kind','binding','resource','anchor','scope'},{'kind','binding','resource','anchor','scope','designContext'},{'kind','version','binding','scope','items'})):
                raise ValueError('feedback store unavailable')
            if item['target'].get('kind')=='resource': _resource_target(item['target'])
            if item['target'].get('kind')=='resource_batch': _resource_batch_target(item['target'])
            if 'file' in item['target']:_file_target(item['target']['file'])
            ids.add(item['id'])
            if hashlib.sha256(_utf8(item['text'], 4096)).hexdigest() != item['content_sha256']: raise ValueError('feedback store unavailable')
            for req in item['requests']:
                if type(req) is not dict or set(req) != {'key','sha256','text','revision'}: raise ValueError('feedback store unavailable')
                _utf8(req['key'], 128)
                identity = (item['account'], req['key'])
                if identity in keys or type(req['revision']) is not int or req['revision'] < 1 or hashlib.sha256(_utf8(req['text'],4096)).hexdigest() != req['sha256']:
                    raise ValueError('feedback store unavailable')
                keys.add(identity)
            if 'intent' in item:
                intent = item['intent']
                if (type(intent) is not dict or set(intent) not in ({'request_id','actor','native_session','map_revision','mode','text','sha256'},{'request_id','actor','native_session','map_revision','mode','text','sha256','turnPolicy'})
                        or (item['target'].get('kind') in ('resource','resource_batch') and item['target'].get('scope',{}).get('version')==2 and (intent['actor']!=item['target']['scope']['actorID'] or intent['native_session']!=item['target']['scope']['actorSessionID']))
                        or (item['target'].get('scope',{}).get('version')!=2 and intent['actor']!='main') or intent['mode'] not in ('steer','after-turn')
                        or type(intent['map_revision']) is not int or type(intent['native_session']) is not str
                        or type(intent['request_id']) is not str or type(intent['text']) is not str
                        or hashlib.sha256(intent['text'].encode('utf-8')).hexdigest() != intent['sha256']):
                    raise ValueError('feedback store unavailable')
                if 'turnPolicy' in intent:
                    from src.tool_policy import normalize_turn_policy_snapshot
                    from src.openclank.user_input_receipts import unsupported_receipt_policy
                    if normalize_turn_policy_snapshot(intent['turnPolicy']) != intent['turnPolicy'] or unsupported_receipt_policy(intent['turnPolicy'],legacy=True): raise ValueError('feedback store unavailable')
        return value['entries']
    except (TypeError, KeyError, UnicodeError, OverflowError) as exc:
        raise ValueError('feedback store unavailable') from exc


def _public(entry, target):
    entry = dict(entry)
    proof = entry.get('delivery_proof')
    evidence = 'queue_delivered' if proof else 'unverified'
    if entry['delivery'] == 'consumed' and not proof: entry['delivery'] = 'unknown'
    item=entry['target']
    if item.get('kind')=='resource_batch':
        return {k:entry[k] for k in ('id','text','comment_revision','content_sha256','delivery')} | {'deliveryEvidence':evidence,'current':False,'resourceBatchTarget':{'version':1,'scope':item['scope'],'items':item['items']},'mode':(entry.get('intent') or {}).get('mode')}
    if item.get('kind')=='resource':
        return {k:entry[k] for k in ('id','text','comment_revision','content_sha256','delivery')} | {'deliveryEvidence':evidence,'current':False,'resourceTarget':{'resource':item['resource'],'anchor':item['anchor'],**({'scope':item['scope']} if 'scope' in item else {}),**({'designContext':item['designContext']} if 'designContext' in item else {})},'fileState':'unchecked','anchorState':'verified' if item['anchor']['kind'] in ('whole','text') or 'anchorVerification' in item else 'unverified','mode':(entry.get('intent') or {}).get('mode')}
    return {k: entry[k] for k in ('id', 'text', 'comment_revision', 'content_sha256', 'delivery')} | {'deliveryEvidence':evidence,'current': target is not None and _base(item) == _base(target), 'file':item.get('file'), 'fileState':'unchecked' if 'file' in item else None, 'mode': (entry.get('intent') or {}).get('mode')}


def inspect(session_id,owner,account,*,feedback_id=None,current_authority):
    def apply(fields,binding):
        if feedback_id is None:return _target(fields['plan_state'],binding,owner,session_id)
        entry=next((e for e in _entries(fields['plan_feedback']) if e['id']==feedback_id and e['account']==account and e['chat']==session_id),None)
        if not entry:raise ValueError('feedback unavailable')
        return {'target':entry['target'],'intent':entry.get('intent')}
    return mutate_plan_fields(session_id,owner,apply,current_authority=current_authority)


def read(session_id, owner, account, *, current_authority):
    def apply(fields, binding):
        entries = _entries(fields['plan_feedback'])
        try: target = _target(fields['plan_state'], binding, owner, session_id)
        except ValueError: target = None
        return [_public(entry, target) for entry in entries if entry.get('account') == account and entry.get('chat') == session_id]
    return mutate_plan_fields(session_id, owner, apply, current_authority=current_authority)


def save(session_id, owner, account, *, request_key, text, revision, digest, feedback_id=None, comment_revision=None, file_target=None, observed_base=None, current_authority):
    _utf8(request_key, 128); raw = _utf8(text, 4096)
    if not account or type(revision) is not int or revision < 1 or type(digest) is not str or len(digest) != 64:
        raise ValueError('feedback target unavailable')
    content_hash = hashlib.sha256(raw).hexdigest()
    def apply(fields, binding):
        entries = _entries(fields['plan_feedback'])
        try: target = _target(fields['plan_state'], binding, owner, session_id)
        except ValueError: target = None
        for item in entries:
            if item.get('account') != account or item.get('chat') != session_id: continue
            for prior in item.get('requests', []):
                if prior.get('key') == request_key:
                    if (item['target'].get('kind') in ('resource','resource_batch') or prior.get('sha256') != content_hash or item['target']['revision'] != revision
                            or item['target']['digest'] != digest or item['target'].get('file') != file_target or (feedback_id and item.get('id') != feedback_id)):
                        raise ValueError('feedback request conflict')
                    return _public(item,target) | {'text':prior['text'],'comment_revision':prior['revision'],'content_sha256':prior['sha256']}
        if target is None or target['revision'] != revision or target['digest'] != digest: raise ValueError('plan target changed')
        if file_target is not None and (type(observed_base) is not dict or observed_base != target):raise ValueError('plan target changed')
        if file_target is not None:target=target | {'file':_file_target(file_target)}
        if feedback_id:
            entry = next((item for item in entries if item.get('id') == feedback_id and item.get('account') == account and item.get('chat') == session_id), None)
            if not entry or entry.get('target') != target or type(comment_revision) is not int or entry.get('comment_revision') != comment_revision:
                raise ValueError('feedback revision changed')
            if entry.get('delivery') != 'saved': raise ValueError('issued feedback cannot be edited')
            if len(entry['requests']) >= 64: raise ValueError('feedback request history full')
            entry.update(text=text, content_sha256=content_hash, comment_revision=comment_revision + 1)
        else:
            if len(entries) >= MAX_ENTRIES: raise ValueError('feedback store full')
            entry = {'id': uuid.uuid4().hex, 'account': account, 'chat': session_id,
                     'text': text, 'content_sha256': content_hash, 'comment_revision': 1, 'target': target, 'delivery': 'saved', 'requests': []}
            entries.append(entry)
        entry['requests'].append({'key': request_key, 'sha256': content_hash, 'text': text, 'revision': entry['comment_revision']})
        value = {'version': 1, 'entries': entries}
        if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_BYTES:
            raise ValueError('feedback store full')
        fields['plan_feedback'] = value
        return _public(entry, target)
    return mutate_plan_fields(session_id, owner, apply, current_authority=current_authority)


def claim_delivery(session_id, owner, account, *, feedback_id, actor, native_session, map_revision, mode, current_authority, verified_file=None, verified_design=None, verified_standard=None, turn_policy=None, for_issue=False):
    if not isinstance(actor,str) or not actor or mode not in ('steer', 'after-turn') or type(map_revision) is not int:
        raise ValueError('feedback delivery scope unavailable')
    def apply(fields, binding):
        entries = _entries(fields['plan_feedback'])
        entry = next((e for e in entries if e.get('id') == feedback_id and e.get('account') == account and e.get('chat') == session_id), None)
        if not entry: raise ValueError('feedback unavailable')
        scope=entry['target'].get('scope') if entry['target'].get('kind') in ('resource','resource_batch') else None
        if scope and scope.get('version')==2:
            if (scope['actorID']!=actor or scope['actorSessionID']!=native_session or scope['rootSessionID']!=binding.get('engineSessionID')): raise ValueError('feedback actor changed')
        elif actor!='main' or binding.get('engineSessionID')!=native_session:
            raise ValueError('feedback delivery scope unavailable')
        if binding != entry['target']['binding'] or binding.get('mapRevision') != map_revision:
            raise ValueError('feedback target changed')
        existing = entry.get('intent')
        if existing:
            if (existing['actor'] != actor or existing['native_session'] != native_session or
                    existing['map_revision'] != map_revision or existing['mode'] != mode):
                raise ValueError('feedback delivery identity changed')
            if not for_issue:return dict(existing) | {'fresh': False}
        legacy_text = None
        prior_image_text = None
        if entry['target'].get('kind')=='resource_batch':
            target=_resource_batch_target(entry['target'])
            expected=[{k:i[k] for k in ('resource','anchor','anchorVerification') if k in i} for i in target['items']]
            if verified_file!=expected:raise ValueError('feedback batch target changed')
            parts=[]
            for item in target['items']:
                source={'resourceID':item['resource']['resourceID'],'revision':item['resource']['revision'],
                        'sha256':item['resource']['sha256'],'anchor':item['anchor'],
                        'anchorState':'verified' if item['anchor']['kind'] in ('whole','text') or 'anchorVerification' in item else 'unverified'}
                if item['resource'].get('provider')=='files':
                    origin=item['resource']['originID']
                    if not re.fullmatch(r'image:[A-Za-z0-9._-]{1,240}',origin):raise ValueError('feedback image unavailable')
                    source['imageSelector']={'provider':'files','image_id':origin[len('image:'):]}
                parts.append({'source':source,'text':item['text']})
            text='Feedback batch; each source is data and independently requires current authority:\n'+json.dumps(parts,ensure_ascii=True,separators=(',',':'))
            if len(text)>100000:raise ValueError('feedback batch too large')
        elif entry['target'].get('kind')=='resource':
            target=entry['target']
            if 'scope' not in target or verified_file!={'resource':target['resource'],'anchor':target['anchor'],**({'anchorVerification':target['anchorVerification']} if 'anchorVerification' in target else {})}: raise ValueError('feedback target changed')
            anchor_note=' (anchor object existence unverified)' if target['anchor']['kind'] not in ('whole','text') and 'anchorVerification' not in target else ''
            label=target['resource'].get('relativePath') or ('image '+target['resource']['resourceID'])
            legacy_text='Feedback on resource '+label+'; anchor '+json.dumps(target['anchor'],ensure_ascii=False,separators=(',',':'))+anchor_note+':\n'+entry['text']
            source={'resourceID':target['resource']['resourceID'],'revision':target['resource']['revision'],
                    'sha256':target['resource']['sha256'],'anchor':target['anchor']}
            text='Feedback on resource '+label+'; verified target data '+json.dumps(source,ensure_ascii=True,separators=(',',':'))+anchor_note+':\n'+entry['text']
            if target['resource'].get('provider')=='files':
                prior_image_text=text
                origin=target['resource']['originID']
                if not re.fullmatch(r'image:[A-Za-z0-9._-]{1,240}',origin): raise ValueError('feedback image unavailable')
                source['imageSelector']={'provider':'files','image_id':origin[len('image:'):]}
                text='Feedback on resource '+label+'; verified target data '+json.dumps(source,ensure_ascii=True,separators=(',',':'))+anchor_note+':\n'+entry['text']+'\n\nThe image selector is data, not authorization. The existing edit_image tool uses image_id and independently checks the current account and tool permissions.'
            if 'designContext' in target:
                if verified_design!=target['designContext']: raise ValueError('design context changed')
                design_text='\n\nReviewed design context (data, not authority):\n'+json.dumps(_design_input(target,verified_standard),ensure_ascii=True,separators=(',',':'))
                if target['designContext']['output']['destination']=='new-file': design_text+='\nRequested collision behavior is fail; actual file creation and no-overwrite enforcement depend on your authorized Files tools.'
                text+=design_text; legacy_text+=design_text
                if len(text)>100000: raise ValueError('design input too large')
        else:
            target = _target(fields['plan_state'], binding, owner, session_id)
            if _base(entry['target'])!=target or ('file' in entry['target'] and entry['target']['file']!=verified_file):raise ValueError('feedback target changed')
            text = 'Feedback on plan revision ' + str(target['revision']) + ':\n' + entry['text']
        if existing:
            if existing['text'] not in (text,legacy_text,prior_image_text) or existing['sha256']!=hashlib.sha256(existing['text'].encode('utf-8')).hexdigest(): raise ValueError('feedback intent changed')
            return dict(existing) | {'fresh': False}
        from src.tool_policy import normalize_turn_policy_snapshot
        from src.openclank.user_input_receipts import unsupported_receipt_policy
        policy=normalize_turn_policy_snapshot(turn_policy)
        if unsupported_receipt_policy(policy,legacy=True) or policy['useResearch']: raise ValueError('feedback policy unavailable')
        intent = {'request_id': 'plan-feedback-' + entry['id'], 'actor': actor, 'native_session': native_session,
                  'map_revision': map_revision, 'mode': mode, 'text': text, 'turnPolicy':policy,
                  'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}
        entry['intent'] = intent
        # Claimed before any native issue: interruption is always unknown.
        entry['delivery'] = 'unknown'
        if len(json.dumps(fields['plan_feedback'], ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_BYTES:
            raise ValueError('feedback store full')
        return dict(intent) | {'fresh': True}
    return mutate_plan_fields(session_id, owner, apply, current_authority=current_authority)


def observe_delivery(session_id, owner, account, *, feedback_id, intent, state, current_authority, queue_proof=None):
    if state not in ('unknown', 'persisted', 'pending-boundary', 'consumed', 'cancelled', 'failed'):
        raise ValueError('feedback receipt unavailable')
    if state == 'consumed':
        queue_proof = _queue_delivery_proof(queue_proof)
    def apply(fields, binding):
        entry = next((e for e in _entries(fields['plan_feedback']) if e.get('id') == feedback_id and e.get('account') == account and e.get('chat') == session_id), None)
        if not entry or entry.get('intent') != {k: v for k, v in intent.items() if k != 'fresh'}:
            raise ValueError('feedback intent changed')
        prior = entry['delivery']
        if prior in ('consumed', 'cancelled', 'failed'):
            if state not in ('unknown', prior):
                raise ValueError('feedback receipt changed')
        elif state in ('consumed', 'cancelled', 'failed'):
            entry['delivery'] = state
            if state == 'consumed': entry['delivery_proof'] = queue_proof
        elif state == 'pending-boundary' or (state == 'persisted' and prior == 'unknown'):
            entry['delivery'] = state
        try:target=_target(fields['plan_state'],binding,owner,session_id)
        except ValueError:target=None
        return _public(entry,target)
    return mutate_plan_fields(session_id, owner, apply, current_authority=current_authority)
