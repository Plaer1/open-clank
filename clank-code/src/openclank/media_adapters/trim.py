"""One fixed local WebM trim using the existing original-child owner."""
import asyncio
import os
from src import bg_jobs
from src.openclank import recording_child
from src.openclank.blender_child import artifact_authority
from src.openclank.media_adapters.base import NotDispatched,Submission
from src.openclank.media_adapters.recording import RecordingAdapter,child_custody_current
from src.openclank.trim_contract import ADAPTER


async def _settled_thread(function,*args,discard=None,**kwargs):
    task=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs));cancelled=False
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True
    value=task.result()
    if cancelled:
        if discard is not None:discard(value)
        raise asyncio.CancelledError
    return value


class TrimAdapter(RecordingAdapter):
    adapter_id=ADAPTER;version='1';route_kind='local';supports_lookup=True;supports_cancel=True

    async def status(self,job,admission):
        admission();row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        child=row.get('local_child') if row else None
        receipt=child.get('receipt') if isinstance(child,dict) and child.get('state')=='terminal' else None
        if isinstance(receipt,dict) and receipt.get('outcome')=='partial' and receipt.get('reason')=='cancelled':
            return Submission('cancelled',child['child_id'])
        return await super().status(job,admission)

    async def submit(self,request,job,admission):
        identity=job['identity'];binding=identity['resource_binding'];helper=None;snapshot=None
        try:
            helper=await recording_child.trim_readiness()
            if not helper or helper['sha256']!=binding['helper_sha256']:raise ValueError('trim helper unavailable')
            snapshot=await self.scope.snapshot(identity)
            if not await self.scope.current(identity):raise ValueError('trim source changed')
            admission()
            profile={'adapter':ADAPTER,'helper_sha256':helper['sha256'],'runner_sha256':recording_child.runner_sha256(),
                'artifact_authority':artifact_authority(self.artifact_store),'input_sha256':binding['source_sha256'],
                'start_ms':binding['start_ms'],'end_ms':binding['end_ms']}
            child=await _settled_thread(bg_jobs.reserve_media_local_child,job['id'],account_id=job['account_id'],token=job['submission_token'],profile=profile)
        except BaseException as exc:
            if helper:os.close(helper['fd'])
            if snapshot:os.close(snapshot['fd'])
            if isinstance(exc,asyncio.CancelledError):raise
            raise NotDispatched('trim source or helper unavailable') from exc
        try:admission()
        except BaseException:
            os.close(helper['fd']);os.close(snapshot['fd']);raise
        async def custody_current():
            return await child_custody_current(identity=identity,job_id=job['id'],child=child,scope=self.scope,
                barrier=self.barrier,owner_for_account=self.owner_for_account)
        launch=asyncio.create_task(recording_child.launch(child,helper,store=self.artifact_store,scope_current=lambda:self.scope.current(identity),
            custody_current=custody_current,barrier=self.barrier,owner=self.owner_for_account(job['account_id']),snapshot=snapshot))
        # A caller cancellation cannot discard the original launch result or
        # close its inherited snapshot while the owned child is still starting.
        while not launch.done():
            try:await asyncio.shield(launch)
            except asyncio.CancelledError:continue
        launch.result()
        return Submission('running',child['child_id'])
