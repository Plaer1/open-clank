"""Explicit human review of one Meshy text-to-3D preview."""
import uuid

from src.openclank.media_adapters.meshy import validate_request
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


class MeshyHumanService(HumanMediaService):
    async def prepare_preview(self, *, account, owner, workspace, generation, session_id,
                              connection_id, provider_account_id, destination_ref, destination_name, parameters):
        request={'parameters':parameters}
        validate_request(request)
        async with self.barrier.admitted(owner):
            profile=await self._bounded_read(self.profile_selection(owner,account,connection_id,provider_account_id))
            operation=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,
                'actor_id':'human','turn_id':operation,'root_operation_id':operation,'attempt_id':operation,
                'request_digest':request_digest(request),'adapter_id':'meshy-preview-v1','adapter_version':'1',
                'operation':'mesh.generate','route_id':connection_id,'route_kind':'remote','model_revision':'meshy-7.1',
                'policy_generation':generation,'input_hashes':[],'resource_binding':profile}
            binding,folder=await self._bounded_read(self.scope.capture(identity,destination_ref=destination_ref,destination_name=destination_name))
            identity['resource_binding']=binding
            job=await self.coordinator.prepare(identity,request)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],
                'profile':{k:binding[k] for k in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
                'destination':{'name':destination_name,'folder':folder},'parameters':parameters,
                'notice':'One Meshy preview may consume credits. The output is untextured; cancellation is unavailable.'}

    async def refine_source_current(self, identity):
        """Revalidate the private original preview witness without reviving it."""
        import asyncio
        binding=identity['resource_binding']
        job=await asyncio.to_thread(self._job,identity['account_id'],identity['owner'],binding['sourceJobID'])
        original=job.get('identity') or {}; profile=original.get('resource_binding') or {}; artifact=job.get('artifact') or {}
        if (job.get('revision')!=binding['sourceRevision'] or job.get('status')!='complete'
            or job.get('publication_withdrawn') or job.get('chat_detached') or original.get('adapter_id')!='meshy-preview-v1'
            or original.get('adapter_version')!='1' or job.get('remote_task_id')!=binding['sourceTaskID']
            or artifact.get('artifact_id')!=binding['sourceArtifactID'] or artifact.get('sha256')!=binding['sourceSHA256']
            or any(profile.get(key)!=binding.get(key) for key in ('ownerAccountID','connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface'))):
            raise ValueError('Meshy preview changed')
        await self._bounded_read(asyncio.to_thread(self._artifact_bytes,identity['account_id'],artifact,original))
        current=await asyncio.to_thread(self._job,identity['account_id'],identity['owner'],binding['sourceJobID'])
        if current!=job: raise ValueError('Meshy preview changed')
        return job

    async def prepare_refine(self, *, account, owner, workspace, generation, session_id,
                             source_job_id, destination_ref, destination_name, parameters):
        import asyncio
        if not isinstance(parameters,dict) or set(parameters)!={'enablePBR'} or type(parameters['enablePBR']) is not bool:
            raise ValueError('invalid Meshy refine')
        async with self.barrier.admitted(owner):
            source=await asyncio.to_thread(self._job,account,owner,source_job_id)
            original=source.get('identity') or {}; profile=original.get('resource_binding') or {}; artifact=source.get('artifact') or {}
            selected=await self._bounded_read(self.profile_selection(owner,account,profile.get('connectionID'),profile.get('providerAccountID')))
            if any(selected.get(key)!=profile.get(key) for key in ('ownerAccountID','connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')):
                raise ValueError('Meshy profile changed')
            request={'parameters':parameters};operation=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,
                'actor_id':'human','turn_id':operation,'root_operation_id':operation,'attempt_id':operation,
                'request_digest':request_digest(request),'adapter_id':'meshy-refine-v1','adapter_version':'1',
                'operation':'mesh.refine','route_id':selected['connectionID'],'route_kind':'remote','model_revision':'meshy-7.1',
                'policy_generation':generation,'input_hashes':[artifact.get('sha256')],'resource_binding':selected}
            binding,folder=await self._bounded_read(self.scope.capture(identity,destination_ref=destination_ref,destination_name=destination_name))
            binding.update(sourceJobID=source_job_id,sourceRevision=source.get('revision'),sourceTaskID=source.get('remote_task_id'),
                sourceArtifactID=artifact.get('artifact_id'),sourceSHA256=artifact.get('sha256'))
            identity['resource_binding']=binding
            await self.refine_source_current(identity)
            await self.preview_read(identity)
            await self.refine_source_current(identity)
            job=await self.coordinator.prepare(identity,request)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],
                'profile':{key:binding[key] for key in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
                'source':{'jobID':source_job_id,'sha256':binding['sourceSHA256']},'destination':{'name':destination_name,'folder':folder},
                'parameters':parameters,'notice':'A separate Meshy refine may consume credits. Cancellation is unavailable; the remote preview may expire.'}
