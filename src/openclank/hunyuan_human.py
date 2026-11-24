"""Shared explicit image preparation for local Hunyuan shape."""
import uuid
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


def validate_request(request):
    if (type(request) is not dict or set(request)!={'parameters'} or type(request['parameters']) is not dict
            or set(request['parameters'])!={'seed'} or type(request['parameters']['seed']) is not int
            or not 0<=request['parameters']['seed']<=2147483647):
        raise ValueError('Hunyuan seed unavailable')
    return request['parameters']['seed']


class HunyuanHumanService(HumanMediaService):
    async def prepare_scoped(self, *, identity, connection_id, source_ref, destination_ref, destination_name, parameters, once=False):
        request={'parameters':parameters};validate_request(request)
        profile=await self._bounded_read(self.profile_selection(identity['owner'],identity['account_id'],connection_id))
        identity={**identity,'request_digest':request_digest(request),'adapter_id':'hunyuan3d-v2-shape-v1','adapter_version':'1',
            'operation':'mesh.generate','route_id':connection_id,'route_kind':'local','model_revision':'hunyuan3d-v2-image-glb-v1',
            'input_hashes':[],'resource_binding':{**profile,'seed':parameters['seed']}}
        binding,review=await self._bounded_read(self.scope.capture(identity,source_ref=source_ref,destination_ref=destination_ref,destination_name=destination_name))
        if type(review) is not dict or review.get('source')!=binding.get('image_source'): raise ValueError('Hunyuan source changed')
        identity['resource_binding']=binding;identity['input_hashes']=[binding['image_source']['sha256']]
        if once: job,created=await self.coordinator.prepare_once(identity,request)
        else: job=await self.coordinator.prepare(identity,request);created=True
        return identity,request,job,created,review

    async def prepare_hunyuan(self, *, account, owner, workspace, generation, session_id, connection_id, source_ref, destination_ref, destination_name, parameters):
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation}
            identity,request,job,_,review=await self.prepare_scoped(identity=identity,connection_id=connection_id,source_ref=source_ref,
                destination_ref=destination_ref,destination_name=destination_name,parameters=parameters)
            binding=identity['resource_binding']
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],
                'profile':{k:binding[k] for k in ('connectionID','connectionRevision','interface')},
                'source':review['source'],'destination':{'name':destination_name,'folder':review['folder']},
                'parameters':request['parameters'],'notice':'Configured local Hunyuan shape job; produces an untextured mesh. Runtime and hardware readiness are not guaranteed.'}
