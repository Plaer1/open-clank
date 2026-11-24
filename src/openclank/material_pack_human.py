"""Prepare one explicit local material bundle using the existing media job owner."""
import uuid

from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


class MaterialPackHumanService(HumanMediaService):
    async def prepare_material(self, *, account, owner, workspace, generation, session_id,
                               sources, destination_ref, destination_name, parameters):
        if type(parameters) is not dict or parameters:
            raise ValueError('invalid material parameters')
        request = {'parameters': {}}
        async with self.barrier.admitted(owner):
            operation = uuid.uuid4().hex
            identity = {'account_id':account, 'owner':owner, 'session_id':session_id, 'workspace_id':workspace,
                'actor_id':'human', 'turn_id':operation, 'root_operation_id':operation, 'attempt_id':operation,
                'request_digest':request_digest(request), 'adapter_id':'material-pack-v1', 'adapter_version':'1',
                'operation':'material.pack', 'route_id':'material-pack-v1', 'route_kind':'local',
                'model_revision':'fixed-v1', 'policy_generation':generation, 'input_hashes':[]}
            binding, review = await self._bounded_read(self.scope.capture(identity, sources=sources,
                destination_ref=destination_ref, destination_name=destination_name))
            if type(review) is not dict or review.get('sources') != binding['inputs']:
                raise ValueError('material source review changed')
            identity['input_hashes'] = [item['sha256'] for item in binding['inputs']]
            identity['resource_binding'] = binding
            job = await self.coordinator.prepare(identity, request)
            return {'jobID':job['id'], 'revision':job['revision'], 'state':job['status'],
                'destination':{'name':destination_name, 'folder':review['folder']},
                'sources':review['sources'], 'manifestSHA256':binding['manifest_sha256'], 'parameters':{},
                'notice':'Packages the selected original maps as a material bundle; no textures or geometry are generated.'}
