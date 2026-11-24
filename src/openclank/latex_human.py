"""Explicit named-user LaTeX request through the existing J0 job owner."""
import asyncio
import os
import uuid
from src.openclank import latex_child
from src.openclank.latex_contract import ADAPTER
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest

class LatexHumanService(HumanMediaService):
    async def prepare_latex(self,*,account,owner,workspace,generation,session_id,source,dependencies,destination_ref,destination_name):
        async with self.barrier.admitted(owner):
            compiler=await asyncio.to_thread(latex_child.compiler_readiness)
            if not compiler: raise ValueError('pdflatex is not installed or supported')
            try: compiler_hash=compiler['executable_sha256']
            finally: os.close(compiler['fd'])
            operation=uuid.uuid4().hex; request={'parameters':{}}
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':operation,'root_operation_id':operation,'attempt_id':operation,'request_digest':request_digest(request),
                'adapter_id':ADAPTER,'adapter_version':'1','operation':'document.latex.compile','route_id':ADAPTER,'route_kind':'local',
                'model_revision':'fixed-v1','policy_generation':generation,'input_hashes':[],'resource_binding':{}}
            binding,folder=await self._bounded_read(self.scope.capture(identity,source=source,dependencies=dependencies,
                destination_ref=destination_ref,destination_name=destination_name,compiler_sha256=compiler_hash))
            identity['resource_binding']=binding;identity['input_hashes']=[item['sha256'] for item in binding['inputs']]
            row=await self.coordinator.prepare(identity,request)
            return {'jobID':row['id'],'revision':row['revision'],'state':row['status'],
                'inputs':[{'resourceID':item['resourceID'],'revision':item['revision'],'sha256':item['sha256'],'name':item['relative_name']} for item in binding['inputs']],
                'destination':{'name':destination_name,'folder':folder},'compiler':{'identity':'sha256:'+compiler_hash,'version':'unprobed'},
                'cancellationScope':'original_compiler_child_only','descendants':'unverified'}

    @staticmethod
    def project(job):
        result=HumanMediaService.project(job)
        result.update(outputMime='application/pdf',cancelAvailable=job.get('status') in {'running','cancel_requested'},
                      cancellationScope='original_compiler_child_only',descendants='unverified')
        return result
