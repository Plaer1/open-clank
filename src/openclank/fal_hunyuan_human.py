"""Prepare one explicitly selected Fal image-to-mesh job."""
import uuid

from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


class FalImageToMeshHumanService(HumanMediaService):
    async def prepare_image_mesh(self, *, account, owner, workspace, generation, session_id,
                                 connection_id, provider_account_id, source_ref,
                                 destination_ref, destination_name, parameters):
        from src.openclank.media_adapters.fal_hunyuan import validate_request
        request = {'parameters': parameters}
        validate_request(request)
        async with self.barrier.admitted(owner):
            profile = await self._bounded_read(self.profile_selection(owner, account, connection_id, provider_account_id))
            operation = uuid.uuid4().hex
            identity = {'account_id': account, 'owner': owner, 'session_id': session_id, 'workspace_id': workspace,
                'actor_id': 'human', 'turn_id': operation, 'root_operation_id': operation, 'attempt_id': operation,
                'request_digest': request_digest(request), 'adapter_id': 'fal-hunyuan-image3d-v1', 'adapter_version': '1',
                'operation': 'mesh.generate', 'route_id': connection_id, 'route_kind': 'remote',
                'model_revision': 'fal-ai/hunyuan3d/v2', 'policy_generation': generation,
                'input_hashes': [], 'resource_binding': profile}
            binding, review = await self._bounded_read(self.scope.capture(identity, source_ref=source_ref,
                destination_ref=destination_ref, destination_name=destination_name))
            source = binding['image_source']
            if type(review) is not dict or review.get('source') != source:
                raise ValueError('image source review changed')
            identity['input_hashes'] = [source['sha256']]
            identity['resource_binding'] = binding
            job = await self.coordinator.prepare(identity, request)
            return {'jobID': job['id'], 'revision': job['revision'], 'state': job['status'],
                'profile': {k: binding[k] for k in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
                'destination': {'name': destination_name, 'folder': review['folder']},
                'source': review['source'], 'parameters': parameters,
                'notice': 'One Fal image-to-mesh task may consume credits. The output is an untextured GLB; cancellation is unavailable.'}
