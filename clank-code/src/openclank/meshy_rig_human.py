"""Shared human and managed preparation for rigging an owned mesh."""
import math
import uuid

from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


def validate_request(request):
    if type(request) is not dict or set(request) != {'parameters'} or type(request['parameters']) is not dict:
        raise ValueError('invalid rig parameters')
    params = request['parameters']
    if set(params) != {'height_meters'} or type(params['height_meters']) not in (int, float):
        raise ValueError('invalid rig height')
    value = params['height_meters']
    if not 0 < value <= 10 or not math.isfinite(value): raise ValueError('invalid rig height')
    return value


class MeshyRigHumanService(HumanMediaService):
    async def prepare_scoped(self, *, identity, connection_id, provider_account_id, source_ref,
                             destination_ref, destination_name, parameters, once=False):
        if type(parameters) is not dict or set(parameters) - {'height_meters'}:
            raise ValueError('invalid rig parameters')
        request = {'parameters':{'height_meters':parameters.get('height_meters', 1.7)}}
        validate_request(request)
        profile = await self._bounded_read(self.profile_selection(identity['owner'], identity['account_id'], connection_id, provider_account_id))
        identity = {**identity, 'request_digest':request_digest(request), 'adapter_id':'meshy-rig-v1', 'adapter_version':'1',
            'operation':'mesh.rig', 'route_id':connection_id, 'route_kind':'remote', 'model_revision':'meshy-rig-v1',
            'input_hashes':[], 'resource_binding':profile}
        binding, review = await self._bounded_read(self.scope.capture(identity, source_ref=source_ref,
            destination_ref=destination_ref, destination_name=destination_name))
        identity['resource_binding'] = binding
        identity['input_hashes'] = [binding['source_sha256']]
        if not await self._bounded_read(self.scope.current(identity)): raise ValueError('media scope changed')
        if once: job, created = await self.coordinator.prepare_once(identity, request)
        else: job, created = await self.coordinator.prepare(identity, request), True
        return identity, request, job, created, review

    async def prepare_rig(self, *, account, owner, workspace, generation, session_id, connection_id,
                          provider_account_id, source_ref, destination_ref, destination_name, parameters):
        async with self.barrier.admitted(owner):
            turn = uuid.uuid4().hex
            identity = {'account_id':account, 'owner':owner, 'session_id':session_id, 'workspace_id':workspace,
                'actor_id':'human', 'turn_id':turn, 'root_operation_id':turn, 'attempt_id':turn, 'policy_generation':generation}
            identity, request, job, _, review = await self.prepare_scoped(identity=identity, connection_id=connection_id,
                provider_account_id=provider_account_id, source_ref=source_ref, destination_ref=destination_ref,
                destination_name=destination_name, parameters=parameters)
            binding = identity['resource_binding']
            return {'jobID':job['id'], 'revision':job['revision'], 'state':job['status'],
                'profile':{k:binding[k] for k in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
                'source':review['source'], 'destination':{'name':destination_name,'folder':review['folder']},
                'parameters':request['parameters'], 'notice':'Rigging may consume credits. Source pose and rig quality are not verified.'}
