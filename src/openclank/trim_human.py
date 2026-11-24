"""Private named-human preparation for one existing J0 trim operation."""
import asyncio
import os
import uuid
from src.openclank import recording_child
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest
from src.openclank.trim_contract import ADAPTER


class TrimHumanService(HumanMediaService):
    async def prepare_trim(self,*,account,owner,workspace,generation,session_id,source_ref,source_revision,
                           destination_ref,destination_name,start_ms,end_ms,expected_helper_sha):
        if (type(start_ms) is not int or type(end_ms) is not int
            or not 0<=start_ms<end_ms<=86400000 or end_ms-start_ms>60000):
            raise ValueError('trim range unavailable')
        async with self.barrier.admitted(owner):
            helper=await recording_child.trim_readiness()
            if not helper: raise ValueError('trim helper unavailable')
            try:helper_sha=helper['sha256']
            finally:os.close(helper['fd'])
            if helper_sha!=expected_helper_sha:raise ValueError('trim helper changed')
            operation=uuid.uuid4().hex;request={'parameters':{}}
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':operation,'root_operation_id':operation,'attempt_id':operation,'request_digest':request_digest(request),
                'adapter_id':ADAPTER,'adapter_version':'1','operation':'video.trim','route_id':ADAPTER,'route_kind':'local',
                'model_revision':'fixed-v1','policy_generation':generation,'input_hashes':[],'resource_binding':{}}
            binding,folder=await self._bounded_read(self.scope.capture(identity,source_ref=source_ref,source_revision=source_revision,
                destination_ref=destination_ref,destination_name=destination_name,helper_sha256=helper_sha,start_ms=start_ms,end_ms=end_ms))
            identity['resource_binding']=binding;identity['input_hashes']=[binding['source_sha256']]
            row=await self.coordinator.prepare(identity,request)
            return {'jobID':row['id'],'revision':row['revision'],'state':row['status'],
                'source':{'resourceID':binding['source_resource_id'],'revision':binding['source_revision'],'sha256':binding['source_sha256'],
                          'size':binding['source_size']},'range':{'startMs':start_ms,'endMs':end_ms,'timebase':'requested_hint'},
                'destination':{'name':destination_name,'folder':folder}}

    def _publication_evidence(self,job):
        from src.openclank.trim_contract import source_evidence
        return source_evidence(job['identity'],job)

    def _recovery_evidence(self,job,recovery_id):
        from src.openclank.trim_contract import source_evidence
        return source_evidence(job['identity'],job)

    async def _recovery_current(self,identity,context,destination_ref,destination_name,destination_revision):
        return await self.scope.recovery_current(identity,context,destination_ref,destination_name,destination_revision)
