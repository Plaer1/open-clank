"""Explicit named-human preview for one Sprixen sprite."""
import uuid
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest
from src.openclank.media_adapters.sprixen import validate_request


class SprixenHumanService(HumanMediaService):
    async def prepare_sprite(self, *, account, owner, workspace, generation, session_id,
                             connection_id, provider_account_id, destination_ref, destination_name, parameters):
        request={'parameters':parameters}
        validate_request(request)
        async with self.barrier.admitted(owner):
            profile=await self._bounded_read(self.profile_selection(owner,account,connection_id,provider_account_id))
            operation=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,
                'actor_id':'human','turn_id':operation,'root_operation_id':operation,'attempt_id':operation,
                'request_digest':request_digest(request),'adapter_id':'sprixen-sprite-v1','adapter_version':'1',
                'operation':'image.generate','route_id':connection_id,'route_kind':'remote','model_revision':'sprite-v1',
                'policy_generation':generation,'input_hashes':[],'resource_binding':profile}
            binding, folder=await self._bounded_read(self.scope.capture(identity,destination_ref=destination_ref,destination_name=destination_name))
            identity['resource_binding']=binding
            job=await self.coordinator.prepare(identity,request)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],
                    'profile':{k:binding[k] for k in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
                    'destination':{'name':destination_name,'folder':folder},'parameters':parameters,
                    'notice':'One provider sprite generation; credits may be charged. Cancellation is unavailable.'}
