"""Explicit human preview of one fixed Blender GLB-to-PNG render."""
import asyncio
import re
import uuid
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


class BlenderHumanService(HumanMediaService):
    async def prepare_scoped(self, *, identity, connection_id, source_ref, destination_ref, destination_name, parameters, once=False, human_variant=None, human_selection=None):
        operation=identity.get('operation')
        if operation=='material.bake':
            if type(parameters) is not dict or set(parameters)!={'resolution'} or type(parameters['resolution']) is not int or parameters['resolution'] not in (256,512,1024,2048): raise ValueError('invalid material resolution')
            adapter,model='blender-material-bake-v1','fixed-material-bake-v1'
        elif operation=='image.render' and type(parameters) is dict:
            variant = parameters.get('variant') if parameters else human_variant
            if not parameters and variant is None:
                adapter='blender-glb-png-v1'
            elif (parameters == {'variant':'four_heading'} or (not parameters and variant == 'four_heading')):
                adapter='blender-glb-four-heading-v1'
            elif (set(parameters)=={'variant','source_sha256','joint_ordinal'} and variant=='rig_clip'
                  and type(parameters['source_sha256']) is str and re.fullmatch(r'[0-9a-f]{64}',parameters['source_sha256'])
                  and type(parameters['joint_ordinal']) is int and 0<=parameters['joint_ordinal']<=63):
                adapter='blender-glb-rig-clip-v1'
            elif not parameters and variant=='rig_clip' and type(human_selection) is dict:
                adapter='blender-glb-rig-clip-v1'
            else: raise ValueError('unsupported Blender operation')
            model='fixed-render-v1'
        else: raise ValueError('unsupported Blender operation')
        request={'parameters':dict(parameters)}
        profile=await self._bounded_read(self.profile_selection(identity['owner'],identity['account_id'],connection_id))
        identity={**identity,'request_digest':request_digest(request),'adapter_id':adapter,'adapter_version':'1',
                  'route_id':connection_id,'route_kind':'local','model_revision':model,'input_hashes':[],
                  'resource_binding':{**profile,**({'resolution':parameters['resolution']} if operation=='material.bake' else {})}}
        binding,(raw,name,folder)=await self._bounded_read(self.scope.capture(identity,source_ref=source_ref,destination_ref=destination_ref,destination_name=destination_name))
        if operation=='material.bake':
            from src.openclank.blender_material_inspect import inspect_material_source
            inspected=await self._bounded_read(asyncio.to_thread(inspect_material_source,raw))
            if inspected.get('source_sha256')!=binding['source_sha256']: raise ValueError('material source changed')
        if adapter=='blender-glb-rig-clip-v1':
            from src.openclank.blender_rig_inspect import inspect_rig
            inspected=await self._bounded_read(asyncio.to_thread(inspect_rig,raw))
            if inspected['sourceSHA256'] != binding['source_sha256']: raise ValueError('rig source changed')
            if human_selection is not None:
                matches=[joint for joint in inspected['joints'] if joint == human_selection]
            else:
                if inspected['sourceSHA256'] != parameters['source_sha256']: raise ValueError('rig source changed')
                matches=[joint for joint in inspected['joints'] if joint['jointOrdinal'] == parameters['joint_ordinal']]
            if len(matches)!=1: raise ValueError('rig selection changed')
            binding['rig']={'sourceSHA256':inspected['sourceSHA256'],'skinIndex':0,'hierarchy':inspected['hierarchy'],'selected':matches[0]}
        identity['resource_binding']=binding;identity['input_hashes']=[binding['source_sha256']]
        if await self._bounded_read(self.scope.current(identity)) is not True: raise ValueError('media source changed')
        if once: job,created=await self.coordinator.prepare_once(identity,request)
        else: job=await self.coordinator.prepare(identity,request);created=True
        return identity,request,job,created,{'source':{'name':name,'sha256':binding['source_sha256']},'destination':{'name':destination_name,'folder':folder},'profile':{key:binding[key] for key in ('connectionID','connectionRevision','interface')}}

    async def _human_scoped(self, *, account, owner, workspace, generation, session_id, connection_id, source_ref, destination_ref, destination_name, operation, parameters):
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                      'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation,'operation':operation}
            identity,request,job,_,review=await self.prepare_scoped(identity=identity,connection_id=connection_id,source_ref=source_ref,destination_ref=destination_ref,destination_name=destination_name,parameters=parameters)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],**review,'parameters':request['parameters'],
                    'operation':('Bake existing UV0 material channels to a ZIP; no geometry or UV creation' if operation=='material.bake' else '512x512 CPU front-view PNG')}

    async def prepare_material_bake(self, *, account, owner, workspace, generation, session_id, connection_id, source_ref, destination_ref, destination_name, resolution):
        return await self._human_scoped(account=account,owner=owner,workspace=workspace,generation=generation,session_id=session_id,connection_id=connection_id,source_ref=source_ref,destination_ref=destination_ref,destination_name=destination_name,operation='material.bake',parameters={'resolution':resolution})

    async def prepare_blender(self, *, account, owner, workspace, generation, session_id,
                              connection_id, source_ref, destination_ref, destination_name, four_headings=False, rig_selection=None):
        if not four_headings and rig_selection is None:
            return await self._human_scoped(account=account,owner=owner,workspace=workspace,generation=generation,session_id=session_id,connection_id=connection_id,source_ref=source_ref,destination_ref=destination_ref,destination_name=destination_name,operation='image.render',parameters={})
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                      'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation,'operation':'image.render'}
            variant='rig_clip' if rig_selection is not None else 'four_heading'
            identity,request,job,_,review=await self.prepare_scoped(identity=identity,connection_id=connection_id,source_ref=source_ref,
                destination_ref=destination_ref,destination_name=destination_name,parameters={},human_variant=variant,human_selection=rig_selection)
            description=('Imported bone-local X: 0°, +15°, 0°, −15° at 6 fps; 512×512 CPU cells; no semantic motion implied'
                if rig_selection is not None else 'Four imported-axis headings (0°, 90°, 180°, 270°); 512×512 CPU cells, 1024×1024 PNG; no semantic front implied')
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],**review,'operation':description,'parameters':{},
                    **({'rig':identity['resource_binding']['rig']} if rig_selection is not None else {})}
