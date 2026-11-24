"""One explicit pdflatex run in the existing J0 local-child owner."""
import asyncio
import hashlib
import os
from src import bg_jobs
from src.openclank import latex_child
from src.openclank.artifacts import account_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.latex_contract import ADAPTER
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission

class LatexAdapter:
    adapter_id=ADAPTER;version='1';route_kind='local';supports_lookup=False;supports_cancel=True
    def __init__(self,*,scope,artifact_store,barrier,owner_for_account):
        self.scope,self.artifact_store,self.barrier,self.owner_for_account=scope,artifact_store,barrier,owner_for_account

    async def submit(self,request,job,admission):
        identity=job['identity'];compiler=None
        try:
            inputs=await self.scope.source(identity);compiler=await asyncio.to_thread(latex_child.compiler_readiness)
            if not compiler or compiler['executable_sha256']!=identity['resource_binding']['compiler_sha256']: raise ValueError('pdflatex unavailable or changed')
            admission()
            profile={'adapter':ADAPTER,'executable_sha256':compiler['executable_sha256'],'executable_version':'unprobed',
                     'runner_sha256':latex_child.runner_sha256(),'input_hashes':identity['input_hashes'],'artifact_authority':artifact_authority(self.artifact_store)}
            child=await asyncio.to_thread(bg_jobs.reserve_media_local_child,job['id'],account_id=job['account_id'],token=job['submission_token'],profile=profile)
        except Exception as error:
            if compiler: os.close(compiler['fd'])
            raise NotDispatched('pdflatex or captured inputs unavailable') from error
        try: admission()
        except BaseException:
            os.close(compiler['fd']); raise
        async def current():
            return await self.scope.current(identity)
        await latex_child.launch(child,inputs,compiler,store=self.artifact_store,scope_current=current,
                                 barrier=self.barrier,owner=self.owner_for_account(job['account_id']))
        return Submission('running',child['child_id'])

    async def status(self,job,admission):
        admission();row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id']);child=row.get('local_child') if row else None
        if not isinstance(child,dict) or child.get('state')=='launching': return Submission('unknown_outcome')
        if child.get('state')=='running': return Submission('running',child['child_id']) if latex_child.owns(child) else Submission('unknown_outcome')
        if child.get('state')!='terminal': return Submission('unknown_outcome')
        if (child.get('receipt') or {}).get('exit_code')!=0: return Submission('failed')
        if not await asyncio.to_thread(bg_jobs.adopt_media_local_artifact,job['id'],account_id=job['account_id'],child_id=child['child_id']): return Submission('unknown_outcome')
        return Submission('collecting',child['child_id'])

    async def cancel(self,job,admission):
        admission();row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id']);child=row.get('local_child') if row else None
        if not isinstance(child,dict): return 'unknown'
        return await latex_child.cancel(child)

    async def collect(self,job,admission):
        admission();row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id']);child=row.get('local_child') if row else None
        receipt=child.get('receipt') if isinstance(child,dict) and child.get('state')=='terminal' else None
        if not isinstance(receipt,dict) or receipt.get('exit_code')!=0: raise ValueError('PDF receipt unavailable')
        artifact=Artifact(receipt['artifact_id'],receipt['output_sha256'],'application/pdf',receipt['output_size'])
        await self.fetch_artifact(job['identity'],{'artifact':{'artifact_id':artifact.artifact_id,'sha256':artifact.sha256,'size':artifact.size}},lambda: self.scope.current(job['identity']))
        return artifact

    async def fetch_artifact(self,identity,job,current):
        if not await current(): raise ValueError('LaTeX scope changed')
        artifact=job['artifact'];owner=account_artifact_owner(identity['account_id'])
        data=await asyncio.to_thread(lambda:b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=artifact['artifact_id'])))
        if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256'] or not await current(): raise ValueError('LaTeX artifact changed')
        return data
