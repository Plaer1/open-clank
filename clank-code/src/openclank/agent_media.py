"""Agent media preparation on the existing ComfyUI job and Files owners.

The caller must supply the original server-resolved C2 authority and currentness;
this module never treats a browser/model selector as an account or actor grant.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping

from src.openclank.media_adapters.comfyui import render_recipe
from src.openclank.media_adapters.base import describe_artifact
from src.openclank.media_jobs import request_digest

_FIELDS=frozenset({'account_id','owner','session_id','workspace_id','actor_id','root_operation_id',
                   'turn_id','message_id','tool_call_id','policy_generation','execution_revision'})


def _authority(value):
    if not isinstance(value,Mapping) or set(value)!=_FIELDS: raise ValueError('media authority unavailable')
    if any(not isinstance(value[key],str) or not value[key] or len(value[key].encode('utf-8'))>512
           for key in _FIELDS-{'policy_generation','execution_revision'}): raise ValueError('media authority unavailable')
    if any(type(value[key]) is not int or value[key]<0 for key in ('policy_generation','execution_revision')):
        raise ValueError('media authority unavailable')
    return dict(value)


def _attempt(authority):
    material=['openclank.agent-media-attempt.v1',authority['session_id'],authority['root_operation_id'],
              authority['actor_id'],authority['execution_revision'],authority['turn_id'],authority['message_id'],authority['tool_call_id']]
    return hashlib.sha256(json.dumps(material,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _project_job(job):
    """Project only a validated local artifact and its exact Files receipt."""
    from src.openclank.media_human import HumanMediaService
    old=HumanMediaService.project(job)
    result={'job_id':old['jobID'],'revision':old['revision'],'state':old['state']}
    if 'reason' in old: result['reason']=old['reason']
    receipt=job.get('result')
    if receipt:
        artifact=job.get('artifact') or {}
        adapter=(job.get('identity') or {}).get('adapter_id')
        if adapter in {'blender-glb-four-heading-v1','blender-glb-rig-clip-v1','comfyui-flac-v1','sprite-sheet-v1','sprite-sheet-crops-v1'}:
            from src.bg_jobs import _media_artifact_for
            artifact=_media_artifact_for(job,artifact)
        descriptor=describe_artifact(artifact)
        if (receipt.get('mime_type')!=descriptor['mime_type'] or receipt.get('sha256')!=descriptor['sha256']
            or not isinstance(receipt.get('resource_ref'),str) or not receipt['resource_ref']):
            raise ValueError('media result unavailable')
        result['result']={'kind':descriptor['kind'],'mime_type':descriptor['mime_type'],'sha256':descriptor['sha256'],
            'size_bytes':descriptor['size_bytes'],'resource_ref':receipt['resource_ref'],'partial':False,
            'follow_up_operations':[]}
        if descriptor['metadata']: result['result']['metadata']=descriptor['metadata']
    return result


class AgentMediaService:
    def __init__(self, *, media_service, provider_store, current, tripo_service=None, fal_service=None, fal_mesh_service=None, material_service=None, blender_services=None, sprite_service=None, trellis_service=None, hunyuan_service=None, meshy_rig_service=None, voidless_service=None):
        if not callable(current): raise ValueError('media authority unavailable')
        self.media=media_service
        self.provider_store=provider_store
        self.current=current
        self.tripo=tripo_service
        self.fal=fal_service
        self.fal_mesh=fal_mesh_service
        self.material=material_service
        self.sprite=sprite_service
        self.trellis=trellis_service
        self.hunyuan=hunyuan_service
        self.meshy_rig=meshy_rig_service
        self.voidless=voidless_service
        self.blender=blender_services or {}

    async def _current(self):
        if await self.current() is not True: raise ValueError('media authority changed')

    async def profiles(self, *, authority, operation=None):
        auth=_authority(authority)
        await self._current()
        if operation in ('material.bake','image.render'):
            service=self.blender.get(operation)
            if service is None: return {'profiles':[],'partial':False}
            rows=await asyncio.to_thread(self.provider_store.list_connections,owner=auth['owner']); await self._current()
            candidates=[row for row in rows if row.enabled and row.deleted_at is None and row.family_id=='blender' and row.adapter_id=='blender-render-v1' and row.kind=='local' and row.billing_lane=='local']
            values=[];partial=len(candidates)>20
            for row in candidates[:20]:
                try:
                    binding=await service._bounded_read(service.profile_selection(auth['owner'],auth['account_id'],row.id)); await self._current()
                    if binding.get('connectionID')!=row.id or binding.get('connectionRevision')!=row.revision or binding.get('ownerAccountID')!=auth['account_id'] or binding.get('interface')!='blender-render-v1': raise ValueError('Blender profile changed')
                    descriptor={'id':operation,'input_kinds':['mesh'],'output_kinds':['archive' if operation=='material.bake' else 'image']}
                    if operation=='image.render':
                        variants=['front']
                        for variant in ('four_heading','rig_clip'):
                            candidate=self.blender.get('image.render:'+variant)
                            if candidate is None: continue
                            try:
                                checked=await candidate._bounded_read(candidate.profile_selection(auth['owner'],auth['account_id'],row.id)); await self._current()
                                if checked != binding: raise ValueError('Blender profile changed')
                                variants.append(variant)
                            except Exception:
                                await self._current();partial=True
                        descriptor['render_variants']=variants
                    values.append({'profile_id':row.id,'revision':row.revision,'label':row.label,'operations':[descriptor]})
                except Exception:
                    await self._current();partial=True
            return {'profiles':values,'partial':partial}
        if operation in ('image.pack','image.pack-crops'):
            profile='sprite-sheet-v1' if operation=='image.pack' else 'sprite-sheet-crops-v1'
            return {'profiles':([{'profile_id':profile,'revision':1,'label':'Sprite sheet',
                'operations':[{'id':operation,'input_kinds':['image'],'output_kinds':['image']}]}] if self.sprite is not None else []),'partial':False}
        if operation in ('audio.generate','material.generate'):
            listed=await asyncio.to_thread(self.provider_store.list_agent_comfy_profiles,owner=auth['owner']);await self._current()
            values=[];partial=listed['partial']
            for row in listed['profiles']:
                try:
                    binding=await self.media._bounded_read(self.media.profile_selection(auth['owner'],auth['account_id'],row['connection_id']));await self._current()
                    if binding.get('connectionRevision')!=row['revision']: raise ValueError('media profile changed')
                    values.append({'profile_id':row['connection_id'],'revision':row['revision'],'label':row['label'],
                        'operations':[{'id':operation,'input_kinds':['workflow_recipe'],'output_kinds':['audio' if operation=='audio.generate' else 'archive']}]})
                except Exception:
                    await self._current();partial=True
            return {'profiles':values[:20],'partial':partial or len(values)>20}
        if operation == 'mesh.rig':
            if self.meshy_rig is None: return {'profiles':[],'partial':False}
            listed=await asyncio.to_thread(self.provider_store.list_human_meshy_profiles,owner=auth['owner']);await self._current()
            values=[];partial=listed['partial']
            for row in listed['profiles']:
                try:
                    binding=await self.meshy_rig._bounded_read(self.meshy_rig.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']));await self._current()
                    if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
                    values.append({'profile_id':row['account_id'],'revision':row['account_revision'],'label':row['label'],
                        'operations':[{'id':'mesh.rig','input_kinds':['mesh'],'output_kinds':['mesh']}]})
                except Exception:
                    await self._current();partial=True
            return {'profiles':values[:20],'partial':partial or len(values)>20}
        if operation == 'material.pack':
            return {'profiles':([{'profile_id':'material-pack-v1','revision':1,'label':'Material bundle',
                'operations':[{'id':'material.pack','input_kinds':['texture_map'],'output_kinds':['archive']}]}] if self.material is not None else []), 'partial':False}
        if operation == 'mesh.generate':
            values=[]; partial=False
            for service, listing, input_kind in ((self.tripo, self.provider_store.list_human_tripo_profiles, 'text'),
                                                  (self.fal_mesh, self.provider_store.list_human_fal_profiles, 'image')):
                if service is None: continue
                listed=await asyncio.to_thread(listing,owner=auth['owner']); await self._current()
                partial=partial or listed['partial']
                for row in listed['profiles']:
                    try:
                        binding=await service._bounded_read(service.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']))
                        await self._current()
                        if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
                        values.append({'profile_id':row['account_id'],'revision':row['account_revision'],'label':row['label'],
                            'operations':[{'id':'mesh.generate','input_kinds':[input_kind],'output_kinds':['mesh']}]})
                    except Exception:
                        await self._current();partial=True
            if self.trellis is not None:
                listed=await asyncio.to_thread(self.provider_store.list_agent_trellis_profiles,owner=auth['owner']);await self._current()
                partial=partial or listed['partial']
                for row in listed['profiles']:
                    try:
                        local,remote=await asyncio.to_thread(self.provider_store.resolve_trellis_media_selector,owner=auth['owner'],selector='trellis:'+row['connection_id']);await self._current()
                        if remote is not None:
                            values=[value for value in values if value['profile_id']!='trellis:'+row['connection_id']]
                            partial=True
                            continue
                        if not local: raise ValueError('media profile changed')
                        binding=await self.trellis._bounded_read(self.trellis.profile_selection(auth['owner'],auth['account_id'],row['connection_id']));await self._current()
                        if (binding.get('connectionID')!=row['connection_id'] or binding.get('connectionRevision')!=row['revision']
                                or binding.get('ownerAccountID')!=auth['account_id'] or binding.get('interface')!='trellis-image3d-v1'): raise ValueError('TRELLIS profile changed')
                        values.append({'profile_id':'trellis:'+row['connection_id'],'revision':row['revision'],'label':row['label'],
                            'operations':[{'id':'mesh.generate','input_kinds':['image'],'output_kinds':['mesh']}]})
                    except Exception:
                        await self._current();partial=True
            if self.hunyuan is not None:
                listed=await asyncio.to_thread(self.provider_store.list_agent_hunyuan_profiles,owner=auth['owner']);await self._current()
                partial=partial or listed['partial']
                for row in listed['profiles']:
                    try:
                        selector='hunyuan:'+row['connection_id']
                        local,remote=await asyncio.to_thread(self.provider_store.resolve_hunyuan_media_selector,owner=auth['owner'],selector=selector);await self._current()
                        if not local or remote is not None: partial=True;continue
                        binding=await self.hunyuan._bounded_read(self.hunyuan.profile_selection(auth['owner'],auth['account_id'],row['connection_id']));await self._current()
                        if (binding.get('connectionID')!=row['connection_id'] or binding.get('connectionRevision')!=row['revision'] or binding.get('ownerAccountID')!=auth['account_id'] or binding.get('interface')!='hunyuan3d-v2-shape-v1'): raise ValueError('Hunyuan profile changed')
                        values.append({'profile_id':selector,'revision':row['revision'],'label':row['label'],
                            'operations':[{'id':'mesh.generate','input_kinds':['image'],'output_kinds':['mesh']}]})
                    except Exception:
                        await self._current();partial=True
            unambiguous=[]
            for value in values:
                if value['profile_id'].startswith('hunyuan:'):
                    try:
                        local,remote=await asyncio.to_thread(self.provider_store.resolve_hunyuan_media_selector,owner=auth['owner'],selector=value['profile_id']);await self._current()
                        if local and remote is not None: partial=True;continue
                    except Exception:
                        await self._current();partial=True;continue
                if value['profile_id'].startswith('trellis:'):
                    try:
                        local,remote=await asyncio.to_thread(self.provider_store.resolve_trellis_media_selector,owner=auth['owner'],selector=value['profile_id']);await self._current()
                        if local and remote is not None: partial=True;continue
                    except Exception:
                        await self._current();partial=True;continue
                unambiguous.append(value)
            values=unambiguous
            return {'profiles':values[:20], 'partial':partial or len(values)>20}
        if operation is not None and operation != 'image.generate':
            return {'profiles':[], 'partial':False}
        result=await asyncio.to_thread(self.provider_store.list_agent_comfy_profiles,owner=auth['owner'])
        await self._current()
        values=[]; partial=result['partial']
        for row in result['profiles']:
            try:
                profile=await self.media._bounded_read(self.media.profile_selection(auth['owner'],auth['account_id'],row['connection_id']))
                await self._current()
                if profile.get('connectionRevision')!=row['revision']: raise ValueError('media profile changed')
                values.append({'profile_id':row['connection_id'],'revision':row['revision'],'label':row['label'],
                    'operations':[{'id':'image.generate','input_kinds':['workflow_recipe'],'output_kinds':['image']}, *([{'id':'audio.generate','input_kinds':['workflow_recipe'],'output_kinds':['audio']}, {'id':'material.generate','input_kinds':['workflow_recipe'],'output_kinds':['archive']}] if operation is None else [])]})
            except Exception:
                await self._current()
                partial=True
        if self.voidless is not None:
            listed=await asyncio.to_thread(self.provider_store.list_human_voidless_profiles,owner=auth['owner']);await self._current()
            partial=partial or listed['partial']
            for row in listed['profiles']:
                try:
                    binding=await self.voidless._bounded_read(self.voidless.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']));await self._current()
                    if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
                    values.append({'profile_id':row['account_id'],'revision':row['account_revision'],'label':row['label'],'operations':[{'id':'image.generate','input_kinds':['text'],'output_kinds':['image']}]})
                except Exception:
                    await self._current();partial=True
        if self.fal is not None:
            listed=await asyncio.to_thread(self.provider_store.list_human_fal_profiles,owner=auth['owner']);await self._current()
            partial=partial or listed['partial']
            for row in listed['profiles']:
                try:
                    binding=await self.fal._bounded_read(self.fal.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']));await self._current()
                    if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
                    values.append({'profile_id':row['account_id'],'revision':row['account_revision'],'label':row['label'],'operations':[{'id':'image.generate','input_kinds':['text'],'output_kinds':['image']}]})
                except Exception:
                    await self._current();partial=True
            partial=partial or len(values)>20;values=values[:20]
        if operation is None:
            rig=await self.profiles(authority=auth,operation='mesh.rig')
            partial=partial or rig['partial'];values.extend(rig['profiles'])
            mesh=await self.profiles(authority=auth,operation='mesh.generate')
            partial=partial or mesh['partial']
            for row in mesh['profiles']:
                matches=[item for item in values if item['profile_id']==row['profile_id']]
                if len(matches)==1 and matches[0]['revision']==row['revision']:
                    matches[0]['operations'].extend(row['operations'])
                elif matches:
                    values=[item for item in values if item['profile_id']!=row['profile_id']];partial=True
                else: values.append(row)
            partial=partial or len(values)>20;values=values[:20]
        if operation is None:
            for supported in ('image.render','material.bake','image.pack','image.pack-crops'):
                listing=await self.profiles(authority=auth,operation=supported);partial=partial or listing['partial']
                for row in listing['profiles']:
                    matches=[item for item in values if item['profile_id']==row['profile_id']]
                    if len(matches)==1 and matches[0]['revision']==row['revision']: matches[0]['operations'].extend(row['operations'])
                    elif matches: values=[item for item in values if item['profile_id']!=row['profile_id']];partial=True
                    else: values.append(row)
                partial=partial or len(values)>20;values=values[:20]
        if operation is None and self.material is not None:
            values.insert(0, {'profile_id':'material-pack-v1','revision':1,'label':'Material bundle',
                'operations':[{'id':'material.pack','input_kinds':['texture_map'],'output_kinds':['archive']}]})
            partial=partial or len(values)>20; values=values[:20]
        return {'profiles':values,'partial':partial}

    @staticmethod
    def project(job):
        return _project_job(job)

    async def _family_of(self, authority, profile_id, families):
        """families: (name, service, lister method). The profile id must be listed by exactly ONE present family -> (name, row)."""
        present=[f for f in families if f[1] is not None]
        if not present: raise ValueError('media operation unavailable')
        auth=_authority(authority);await self._current()
        found=[]
        for name,_service,lister in present:
            listed=await asyncio.to_thread(getattr(self.provider_store,lister),owner=auth['owner']);await self._current()
            matches=[row for row in listed['profiles'] if row['account_id']==profile_id]
            if len(matches)>1: raise ValueError('ambiguous media profile')
            found.extend((name,row) for row in matches)
        if len(found)>1: raise ValueError('ambiguous media profile')
        if not found: raise ValueError('media profile unavailable')
        return found[0]

    async def submit(self, *, authority, operation, profile_id, inputs, parameters, destination, on_reserved=None):
        if operation in ('image.pack','image.pack-crops'):
            return await self._submit_sprite(authority=authority,operation=operation,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
        if operation in ('material.bake','image.render'):
            return await self._submit_blender(authority=authority,operation=operation,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
        if operation == 'mesh.rig':
            if (self.meshy_rig is None or not isinstance(profile_id,str) or not profile_id or not isinstance(inputs,list) or len(inputs)!=1
                    or not isinstance(inputs[0],dict) or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='mesh'
                    or not isinstance(inputs[0]['resource_ref'],str) or not inputs[0]['resource_ref']
                    or not isinstance(destination,dict) or set(destination)!={'resource_ref','name'}
                    or not all(isinstance(destination[k],str) and destination[k] for k in ('resource_ref','name'))): raise ValueError('media operation unavailable')
            auth=_authority(authority);await self._current()
            listed=await asyncio.to_thread(self.provider_store.list_human_meshy_profiles,owner=auth['owner']);await self._current()
            matches=[row for row in listed['profiles'] if row['account_id']==profile_id]
            if len(matches)!=1: raise ValueError('media profile unavailable')
            row=matches[0]; profile=await self.meshy_rig._bounded_read(self.meshy_rig.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']));await self._current()
            if (profile.get('connectionRevision')!=row['connection_revision'] or profile.get('providerAccountRevision')!=row['account_revision'] or profile.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
            identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
                'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),'policy_generation':auth['policy_generation']}
            identity,request,job,created,_=await self.meshy_rig.prepare_scoped(identity=identity,connection_id=row['connection_id'],provider_account_id=row['account_id'],source_ref=inputs[0]['resource_ref'],destination_ref=destination['resource_ref'],destination_name=destination['name'],parameters=parameters,once=True)
            if on_reserved is not None: on_reserved(job['id'])
            await self._current()
            if created: job=await self.meshy_rig.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
            await self._current();return self.project(job)
        if operation == 'material.pack':
            return await self._submit_material(authority=authority,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
        if operation == 'image.generate' and isinstance(inputs,list) and len(inputs)==1 and isinstance(inputs[0],dict) and inputs[0].get('kind')=='text':
            # Family table: the profile must be listed by exactly ONE family, and only that family's submit runs.
            name,row=await self._family_of(authority,profile_id,[('voidless',self.voidless,'list_human_voidless_profiles'),('fal',self.fal,'list_human_fal_profiles')])
            if name=='voidless': return await self._submit_voidless(authority=authority,row=row,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
            return await self._submit_fal(authority=authority,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
        if operation == 'mesh.generate':
            # Input kind only selects among a family's own operations; it never selects the vendor.
            kind=inputs[0].get('kind') if isinstance(inputs,list) and len(inputs)==1 and isinstance(inputs[0],dict) else None
            if kind not in ('text','image'): raise ValueError('media operation unavailable')
            local={'hunyuan:':('hunyuan',self.hunyuan,'resolve_hunyuan_media_selector'),'trellis:':('trellis',self.trellis,'resolve_trellis_media_selector')}
            prefix=next((x for x in local if isinstance(profile_id,str) and profile_id.startswith(x)),None)
            if prefix:
                name,service,resolver=local[prefix]
                auth=_authority(authority);await self._current()
                found,remote=await asyncio.to_thread(getattr(self.provider_store,resolver),owner=auth['owner'],selector=profile_id);await self._current()
                if found and remote is not None: raise ValueError('ambiguous media profile')
                if not found or service is None: raise ValueError('media profile unavailable')    # a remote-only row is refused, never sent to fal-mesh
                if kind!='image': raise ValueError('media operation unavailable')
                if name=='trellis': return await self._submit_trellis(authority=authority,profile_id=profile_id[len(prefix):],inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
                return await self._submit_local_mesh(service=service,validator=name,authority=authority,profile_id=profile_id[len(prefix):],inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
            name,row=await self._family_of(authority,profile_id,[('tripo',self.tripo,'list_human_tripo_profiles'),('fal_mesh',self.fal_mesh,'list_human_fal_profiles')])
            if (name,kind) not in (('tripo','text'),('fal_mesh','image')): raise ValueError('media operation unavailable')
            if name=='tripo': return await self._submit_mesh(authority=authority,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
            return await self._submit_fal_mesh(authority=authority,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)
        if operation == 'material.generate':
            if (not isinstance(profile_id,str) or not profile_id or not isinstance(inputs,list) or len(inputs)!=1
                    or not isinstance(inputs[0],dict) or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='workflow_recipe'
                    or not isinstance(inputs[0]['resource_ref'],str) or not inputs[0]['resource_ref']
                    or not isinstance(destination,dict) or set(destination)!={'resource_ref','name'}
                    or not all(isinstance(destination[k],str) and destination[k] for k in ('resource_ref','name'))):
                raise ValueError('media operation unavailable')
            auth=_authority(authority);await self._current()
            identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
                'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
                'policy_generation':auth['policy_generation']}
            identity,request,job,created,_=await self.media.prepare_scoped(identity=identity,connection_id=profile_id,
                recipe_ref=inputs[0]['resource_ref'],destination_ref=destination['resource_ref'],destination_name=destination['name'],
                parameters=parameters,once=True,operation='material.generate')
            if on_reserved is not None: on_reserved(job['id'])
            await self._current()
            if created: job=await self.media.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
            await self._current();return self.project(job)
        if (operation not in ('image.generate','audio.generate') or not isinstance(profile_id,str) or not profile_id
            or not isinstance(inputs,list) or len(inputs)!=1 or not isinstance(inputs[0],dict)
            or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='workflow_recipe'
            or not isinstance(inputs[0]['resource_ref'],str) or not inputs[0]['resource_ref']
            or not isinstance(destination,dict) or set(destination)!={'resource_ref','name'}
            or not all(isinstance(destination[k],str) and destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        connection_id=profile_id
        recipe_ref=inputs[0]['resource_ref']
        destination_ref=destination['resource_ref']
        destination_name=destination['name']
        auth=_authority(authority)
        await self._current()
        profile=await self.media._bounded_read(self.media.profile_selection(auth['owner'],auth['account_id'],connection_id))
        await self._current()
        request={'parameters':parameters}
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'request_digest':request_digest(request),'adapter_id':'comfyui-flac-v1' if operation=='audio.generate' else 'comfyui','adapter_version':'1','operation':operation,
            'route_id':connection_id,'route_kind':'remote','model_revision':'unknown','policy_generation':auth['policy_generation'],
            'input_hashes':[],'resource_binding':profile}
        binding,(raw,_name,_folder)=await self.media._bounded_read(self.media.scope.capture(identity,recipe_ref=recipe_ref,
            destination_ref=destination_ref,destination_name=destination_name))
        await self._current()
        if operation=='audio.generate':
            from src.openclank.media_adapters.comfy_flac import render_flac_recipe
            render_flac_recipe(raw,parameters)
        else: render_recipe(raw,parameters)
        identity['resource_binding']=binding
        job,created=await self.media.coordinator.prepare_once(identity,request)
        if on_reserved is not None:
            on_reserved(job['id'])
        await self._current()
        if created:
            job=await self.media.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current()
        return self.project(job)

    async def _submit_sprite(self, *, authority, operation, profile_id, inputs, parameters, destination, on_reserved):
        profile='sprite-sheet-v1' if operation=='image.pack' else 'sprite-sheet-crops-v1'
        count=4 if operation=='image.pack' else 1
        if (self.sprite is None or profile_id!=profile or type(inputs) is not list or len(inputs)!=count
                or any(type(v) is not dict or set(v)!={'kind','resource_ref'} or v['kind']!='image'
                       or type(v['resource_ref']) is not str or not v['resource_ref'] for v in inputs)
                or type(destination) is not dict or set(destination)!={'resource_ref','name'}
                or any(type(destination[k]) is not str or not destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        if operation=='image.pack':
            annotations,rectangles=parameters,None
        else:
            if type(parameters) is not dict or set(parameters)!={'annotations','rectangles'}: raise ValueError('media operation unavailable')
            annotations,rectangles=parameters['annotations'],parameters['rectangles']
        auth=_authority(authority);await self._current()
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'policy_generation':auth['policy_generation'],'operation':operation}
        identity,request,job,created,_=await self.sprite.prepare_scoped(identity=identity,source_refs=[v['resource_ref'] for v in inputs],
            destination_ref=destination['resource_ref'],destination_name=destination['name'],parameters=annotations,rectangles=rectangles,once=True)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await self.sprite.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current();return self.project(job)

    async def _submit_blender(self, *, authority, operation, profile_id, inputs, parameters, destination, on_reserved):
        service=self.blender.get(operation)
        if operation=='image.render' and type(parameters) is dict and parameters.get('variant') in ('four_heading','rig_clip'):
            service=self.blender.get('image.render:'+parameters['variant'])
        if (service is None or type(profile_id) is not str or not profile_id or type(inputs) is not list or len(inputs)!=1
                or type(inputs[0]) is not dict or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='mesh'
                or type(inputs[0]['resource_ref']) is not str or not inputs[0]['resource_ref']
                or type(destination) is not dict or set(destination)!={'resource_ref','name'}
                or any(type(destination[k]) is not str or not destination[k] for k in ('resource_ref','name'))): raise ValueError('media operation unavailable')
        if operation=='image.render':
            if type(parameters) is not dict: raise ValueError('media operation unavailable')
            variant=parameters.get('variant')
            valid=(not parameters or parameters=={'variant':'four_heading'} or
                (set(parameters)=={'variant','source_sha256','joint_ordinal'} and variant=='rig_clip'
                 and type(parameters['source_sha256']) is str and len(parameters['source_sha256'])==64
                 and all(c in '0123456789abcdef' for c in parameters['source_sha256'])
                 and type(parameters['joint_ordinal']) is int and 0<=parameters['joint_ordinal']<64))
            if not valid: raise ValueError('media operation unavailable')
        if operation=='material.bake' and (type(parameters) is not dict or set(parameters)!={'resolution'} or type(parameters['resolution']) is not int or parameters['resolution'] not in (256,512,1024,2048)): raise ValueError('media operation unavailable')
        auth=_authority(authority);await self._current()
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'policy_generation':auth['policy_generation'],'operation':operation}
        identity,request,job,created,_=await service.prepare_scoped(identity=identity,connection_id=profile_id,source_ref=inputs[0]['resource_ref'],destination_ref=destination['resource_ref'],destination_name=destination['name'],parameters=parameters,once=True)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await service.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current();return self.project(job)

    async def inspect(self, *, authority, operation, inputs, parameters):
        service=self.blender.get('image.render:rig_clip')
        if (service is None or operation!='image.render' or type(inputs) is not list or len(inputs)!=1
                or type(inputs[0]) is not dict or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='mesh'
                or type(inputs[0]['resource_ref']) is not str or not inputs[0]['resource_ref']
                or type(parameters) is not dict or parameters!={'variant':'rig_clip'}):
            raise ValueError('media inspection unavailable')
        auth=_authority(authority);await self._current()
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],
                  'workspace_id':auth['workspace_id'],'policy_generation':auth['policy_generation']}
        result=await service._bounded_read(service.scope.inspect_rig(identity,source_ref=inputs[0]['resource_ref']))
        await self._current()
        return {'kind':'rig',**result}

    async def _submit_material(self, *, authority, profile_id, inputs, parameters, destination, on_reserved):
        roles=('baseColor','roughness','metallic','normal')
        if (self.material is None or profile_id != 'material-pack-v1' or type(parameters) is not dict or parameters
                or type(inputs) is not list or not 3 <= len(inputs) <= 4
                or type(destination) is not dict or set(destination) != {'resource_ref','name'}
                or any(type(destination[k]) is not str or not destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        sources={}
        for item in inputs:
            if (type(item) is not dict or set(item) != {'kind','role','resource_ref'} or item['kind'] != 'texture_map'
                    or item['role'] not in roles or item['role'] in sources
                    or type(item['resource_ref']) is not str or not item['resource_ref']):
                raise ValueError('media operation unavailable')
            sources[item['role']]=item['resource_ref']
        if not set(roles[:3]) <= set(sources) or len(set(sources.values())) != len(sources):
            raise ValueError('media operation unavailable')
        ordered=[{'role':role,'resource_ref':sources[role]} for role in roles if role in sources]
        auth=_authority(authority); await self._current()
        service=self.material; request={'parameters':{}}
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'request_digest':request_digest(request),'adapter_id':'material-pack-v1','adapter_version':'1','operation':'material.pack',
            'route_id':'material-pack-v1','route_kind':'local','model_revision':'fixed-v1','policy_generation':auth['policy_generation'],
            'input_hashes':[],'resource_binding':{}}
        binding,review=await service._bounded_read(service.scope.capture(identity,sources=ordered,
            destination_ref=destination['resource_ref'],destination_name=destination['name']))
        await self._current()
        if type(review) is not dict or review.get('sources') != binding.get('inputs'):
            raise ValueError('media source changed')
        identity['input_hashes']=[item['sha256'] for item in binding['inputs']]; identity['resource_binding']=binding
        job,created=await service.coordinator.prepare_once(identity,request)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await service.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current(); return self.project(job)

    async def _submit_mesh(self, *, authority, profile_id, inputs, parameters, destination, on_reserved):
        from src.openclank.media_adapters.tripo import validate_request
        if (self.tripo is None or not isinstance(profile_id,str) or not profile_id or not isinstance(inputs,list) or len(inputs)!=1
            or not isinstance(inputs[0],dict) or set(inputs[0])!={'kind','text'} or inputs[0]['kind']!='text'
            or parameters!={} or not isinstance(destination,dict) or set(destination)!={'resource_ref','name'}
            or not all(isinstance(destination[k],str) and destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        request={'parameters':{'prompt':inputs[0]['text']}}; validate_request(request)
        auth=_authority(authority); await self._current()
        listed=await asyncio.to_thread(self.provider_store.list_human_tripo_profiles,owner=auth['owner']); await self._current()
        matches=[row for row in listed['profiles'] if row['account_id']==profile_id]
        if len(matches)!=1: raise ValueError('media profile unavailable')
        row=matches[0]; service=self.tripo
        profile=await service._bounded_read(service.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id'])); await self._current()
        if (profile.get('connectionRevision')!=row['connection_revision'] or profile.get('providerAccountRevision')!=row['account_revision'] or profile.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'request_digest':request_digest(request),'adapter_id':'tripo-text3d-v1','adapter_version':'1','operation':'mesh.generate',
            'route_id':row['connection_id'],'route_kind':'remote','model_revision':'v3.1-20260211','policy_generation':auth['policy_generation'],
            'input_hashes':[],'resource_binding':profile}
        binding,_folder=await service._bounded_read(service.scope.capture(identity,destination_ref=destination['resource_ref'],destination_name=destination['name']))
        await self._current(); identity['resource_binding']=binding
        job,created=await service.coordinator.prepare_once(identity,request)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await service.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current(); return self.project(job)

    async def _submit_voidless(self, *, authority, row, inputs, parameters, destination, on_reserved):
        if (self.voidless is None or type(inputs) is not list or len(inputs)!=1 or type(inputs[0]) is not dict
                or set(inputs[0])!={'kind','text'} or inputs[0]['kind']!='text' or parameters!={}
                or type(destination) is not dict or set(destination)!={'resource_ref','name'}
                or any(type(destination[k]) is not str or not destination[k] for k in ('resource_ref','name'))): raise ValueError('media operation unavailable')
        auth=_authority(authority);await self._current()
        profile=await self.voidless._bounded_read(self.voidless.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']));await self._current()
        if (profile.get('connectionRevision')!=row['connection_revision'] or profile.get('providerAccountRevision')!=row['account_revision'] or profile.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),'policy_generation':auth['policy_generation']}
        identity,request,job,created,_=await self.voidless.prepare_voidless_scoped(identity=identity,connection_id=row['connection_id'],provider_account_id=row['account_id'],destination_ref=destination['resource_ref'],destination_name=destination['name'],parameters={'prompt':inputs[0]['text']},once=True)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await self.voidless.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current();return self.project(job)

    async def _submit_fal(self, *, authority, profile_id, inputs, parameters, destination, on_reserved):
        from src.openclank.media_adapters.fal import validate_request
        if (self.fal is None or not isinstance(profile_id,str) or not profile_id or not isinstance(inputs,list) or len(inputs)!=1
            or not isinstance(inputs[0],dict) or set(inputs[0])!={'kind','text'} or inputs[0]['kind']!='text'
            or parameters!={} or not isinstance(destination,dict) or set(destination)!={'resource_ref','name'}
            or not all(isinstance(destination[k],str) and destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        request={'parameters':{'prompt':inputs[0]['text']}}; validate_request(request)
        auth=_authority(authority); await self._current()
        listed=await asyncio.to_thread(self.provider_store.list_human_fal_profiles,owner=auth['owner']); await self._current()
        matches=[row for row in listed['profiles'] if row['account_id']==profile_id]
        if len(matches)!=1: raise ValueError('media profile unavailable')
        row=matches[0]; service=self.fal
        profile=await service._bounded_read(service.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id'])); await self._current()
        if (profile.get('connectionRevision')!=row['connection_revision'] or profile.get('providerAccountRevision')!=row['account_revision'] or profile.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'request_digest':request_digest(request),'adapter_id':'fal-flux-schnell-v1','adapter_version':'1','operation':'image.generate',
            'route_id':row['connection_id'],'route_kind':'remote','model_revision':'fal-ai/flux/schnell','policy_generation':auth['policy_generation'],
            'input_hashes':[],'resource_binding':profile}
        binding,_folder=await service._bounded_read(service.scope.capture(identity,destination_ref=destination['resource_ref'],destination_name=destination['name']))
        await self._current(); identity['resource_binding']=binding
        job,created=await service.coordinator.prepare_once(identity,request)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await service.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current(); return self.project(job)

    async def _submit_trellis(self, *, authority, profile_id, inputs, parameters, destination, on_reserved):
        return await self._submit_local_mesh(service=self.trellis,validator='trellis',authority=authority,profile_id=profile_id,inputs=inputs,parameters=parameters,destination=destination,on_reserved=on_reserved)

    async def _submit_local_mesh(self, *, service, validator, authority, profile_id, inputs, parameters, destination, on_reserved):
        if (service is None or type(profile_id) is not str or not profile_id or type(inputs) is not list or len(inputs)!=1
                or type(inputs[0]) is not dict or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='image'
                or type(inputs[0]['resource_ref']) is not str or not inputs[0]['resource_ref']
                or type(destination) is not dict or set(destination)!={'resource_ref','name'}
                or any(type(destination[k]) is not str or not destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        if validator == 'trellis':
            from src.openclank.trellis_human import validate_request
        elif validator == 'hunyuan':
            from src.openclank.hunyuan_human import validate_request
        else: raise ValueError('media operation unavailable')
        validate_request({'parameters':parameters})
        auth=_authority(authority);await self._current()
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'policy_generation':auth['policy_generation']}
        identity,request,job,created,_=await service.prepare_scoped(identity=identity,connection_id=profile_id,
            source_ref=inputs[0]['resource_ref'],destination_ref=destination['resource_ref'],destination_name=destination['name'],parameters=parameters,once=True)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await service.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current();return self.project(job)

    async def _submit_fal_mesh(self, *, authority, profile_id, inputs, parameters, destination, on_reserved, selected=None):
        from src.openclank.media_adapters.fal_hunyuan import validate_request
        if (self.fal_mesh is None or not isinstance(profile_id,str) or not profile_id or not isinstance(inputs,list) or len(inputs)!=1
            or not isinstance(inputs[0],dict) or set(inputs[0])!={'kind','resource_ref'} or inputs[0]['kind']!='image'
            or not isinstance(inputs[0]['resource_ref'],str) or not inputs[0]['resource_ref']
            or not isinstance(destination,dict) or set(destination)!={'resource_ref','name'}
            or not all(isinstance(destination[k],str) and destination[k] for k in ('resource_ref','name'))):
            raise ValueError('media operation unavailable')
        request={'parameters':parameters};validate_request(request)
        auth=_authority(authority);await self._current()
        if selected is None:
            listed=await asyncio.to_thread(self.provider_store.list_human_fal_profiles,owner=auth['owner']);await self._current()
            matches=[row for row in listed['profiles'] if row['account_id']==profile_id]
            if len(matches)!=1: raise ValueError('media profile unavailable')
            row=matches[0]
        else:
            if selected.get('account_id')!=profile_id: raise ValueError('media profile unavailable')
            row=selected
        service=self.fal_mesh
        profile=await service._bounded_read(service.profile_selection(auth['owner'],auth['account_id'],row['connection_id'],row['account_id']));await self._current()
        if (profile.get('connectionRevision')!=row['connection_revision'] or profile.get('providerAccountRevision')!=row['account_revision'] or profile.get('credentialVersion')!=row['credential_version']): raise ValueError('media profile changed')
        identity={'account_id':auth['account_id'],'owner':auth['owner'],'session_id':auth['session_id'],'workspace_id':auth['workspace_id'],
            'actor_id':auth['actor_id'],'turn_id':auth['turn_id'],'root_operation_id':auth['root_operation_id'],'attempt_id':_attempt(auth),
            'request_digest':request_digest(request),'adapter_id':'fal-hunyuan-image3d-v1','adapter_version':'1','operation':'mesh.generate',
            'route_id':row['connection_id'],'route_kind':'remote','model_revision':'fal-ai/hunyuan3d/v2','policy_generation':auth['policy_generation'],
            'input_hashes':[],'resource_binding':profile}
        binding,review=await service._bounded_read(service.scope.capture(identity,source_ref=inputs[0]['resource_ref'],destination_ref=destination['resource_ref'],destination_name=destination['name']))
        await self._current();source=binding['image_source']
        if type(review) is not dict or review.get('source')!=source: raise ValueError('media source changed')
        identity['input_hashes']=[source['sha256']];identity['resource_binding']=binding
        job,created=await service.coordinator.prepare_once(identity,request)
        if on_reserved is not None: on_reserved(job['id'])
        await self._current()
        if created: job=await service.coordinator.submit(identity,job['id'],request,expected_revision=job['revision'])
        await self._current();return self.project(job)

    def _job_service(self, job):
        adapter=(job.get('identity') or {}).get('adapter_id')
        if adapter=='blender-material-bake-v1' and self.blender.get('material.bake') is not None: return self.blender['material.bake']
        if adapter=='blender-glb-png-v1' and self.blender.get('image.render') is not None: return self.blender['image.render']
        if adapter=='blender-glb-four-heading-v1' and self.blender.get('image.render:four_heading') is not None: return self.blender['image.render:four_heading']
        if adapter=='blender-glb-rig-clip-v1' and self.blender.get('image.render:rig_clip') is not None: return self.blender['image.render:rig_clip']
        if adapter=='material-pack-v1' and self.material is not None: return self.material
        if adapter=='trellis-image3d-v1' and self.trellis is not None: return self.trellis
        if adapter=='hunyuan3d-v2-shape-v1' and self.hunyuan is not None: return self.hunyuan
        if adapter in {'comfyui','comfyui-flac-v1','comfyui-material-v1'}: return self.media
        if adapter in {'sprite-sheet-v1','sprite-sheet-crops-v1'} and self.sprite is not None: return self.sprite
        if adapter=='tripo-text3d-v1' and self.tripo is not None: return self.tripo
        if adapter=='fal-flux-schnell-v1' and self.fal is not None: return self.fal
        if adapter=='voidless-image-v1' and self.voidless is not None: return self.voidless
        if adapter=='fal-hunyuan-image3d-v1' and self.fal_mesh is not None: return self.fal_mesh
        if adapter=='meshy-rig-v1' and self.meshy_rig is not None: return self.meshy_rig
        raise ValueError('media job unavailable')

    async def snapshot(self, *, authority, job_id):
        """Read the existing local job without issuing provider work."""
        auth=_authority(authority)
        await self._current()
        job=await asyncio.to_thread(self.media._job,auth['account_id'],auth['owner'],job_id)
        identity=job.get('identity') or {}
        if (identity.get('adapter_id') not in {'hunyuan3d-v2-shape-v1','trellis-image3d-v1','meshy-rig-v1','voidless-image-v1','comfyui','comfyui-flac-v1','comfyui-material-v1','sprite-sheet-v1','sprite-sheet-crops-v1','tripo-text3d-v1','fal-flux-schnell-v1','fal-hunyuan-image3d-v1','material-pack-v1','blender-material-bake-v1','blender-glb-png-v1','blender-glb-four-heading-v1','blender-glb-rig-clip-v1'} or job.get('session_id')!=auth['session_id']
            or job.get('workspace_id')!=auth['workspace_id'] or identity.get('account_id')!=auth['account_id']):
            raise ValueError('media job unavailable')
        self._job_service(job)
        await self._current()
        return self.project(job)

    async def action(self, *, authority, job_id, action):
        auth=_authority(authority)
        if action not in {'status','collect'}: raise ValueError('media action unavailable')
        await self._current()
        job=self.media._job(auth['account_id'],auth['owner'],job_id)
        identity=job.get('identity') or {}
        if (identity.get('adapter_id') not in {'hunyuan3d-v2-shape-v1','trellis-image3d-v1','meshy-rig-v1','voidless-image-v1','comfyui','comfyui-flac-v1','comfyui-material-v1','sprite-sheet-v1','sprite-sheet-crops-v1','tripo-text3d-v1','fal-flux-schnell-v1','fal-hunyuan-image3d-v1','material-pack-v1','blender-material-bake-v1','blender-glb-png-v1','blender-glb-four-heading-v1','blender-glb-rig-clip-v1'} or job.get('session_id')!=auth['session_id']
            or job.get('workspace_id')!=auth['workspace_id'] or identity.get('account_id')!=auth['account_id']):
            raise ValueError('media job unavailable')
        await self._current()
        result=await getattr(self._job_service(job).coordinator,action)(identity,job_id)
        await self._current()
        return self.project(result)
