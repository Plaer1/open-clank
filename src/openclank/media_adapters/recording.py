"""Fixed original-child screen recording adapter."""
import asyncio
import hashlib
import os
from src import bg_jobs
from src.openclank import recording_child
from src.openclank.artifacts import account_artifact_owner
from src.openclank.blender_child import artifact_authority
from src.openclank.recording_contract import ADAPTER, LIMITS
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission


async def child_custody_current(*, identity, job_id, child, scope, barrier, owner_for_account):
    try:
        owner = owner_for_account(identity['account_id'])
        if not owner or owner != identity['owner']: return False
        key = str(owner).strip().lower()
        if barrier._fenced(key) or (barrier._absolute_fence and barrier._absolute_fence(key)): return False
        if not await scope.session_current(identity): return False
        row = await asyncio.to_thread(bg_jobs.get_media,job_id,account_id=identity['account_id'])
        original = (row or {}).get('local_child') or {}
        return bool(row and row.get('owner') == owner and row.get('identity') == identity
            and not row.get('chat_detached') and not row.get('publication_withdrawn')
            and original.get('state') == 'running' and original.get('child_id') == child['child_id']
            and original.get('nonce') == child['nonce'] and original.get('identity_digest') == row.get('identity_digest')
            and original.get('claim_token') == row.get('submission_token') and await scope.session_current(identity)
            and not barrier._fenced(key) and not (barrier._absolute_fence and barrier._absolute_fence(key)))
    except Exception: return False


class RecordingAdapter:
    adapter_id=ADAPTER; version='1'; route_kind='local'; supports_lookup=False; supports_cancel=True
    def __init__(self, *, scope, artifact_store, barrier, owner_for_account, before_spawn=None):
        self.scope, self.artifact_store, self.barrier, self.owner_for_account = scope, artifact_store, barrier, owner_for_account
        self.before_spawn = before_spawn

    async def submit(self, request, job, admission):
        identity=job['identity']; helper=None
        try:
            protocol=identity['resource_binding'].get('recording_protocol','record-v3')
            if protocol in ('record-v6','record-v7') and not callable(self.before_spawn): raise ValueError('recording request authority unavailable')
            helper=(await recording_child.control_recording_readiness() if protocol == 'record-v8' else await recording_child.crop_recording_readiness() if protocol == 'record-v7' else await recording_child.audio_recording_readiness() if protocol == 'record-v6' else await recording_child.observed_recording_readiness() if protocol == 'record-v5' else await asyncio.to_thread(recording_child.readiness))
            if not helper or helper['sha256'] != identity['resource_binding']['helper_sha256']: raise ValueError('helper changed')
            admission()
            profile={'adapter':ADAPTER,'helper_sha256':helper['sha256'],'runner_sha256':recording_child.runner_sha256(),
                     'artifact_authority':artifact_authority(self.artifact_store),'limits':dict(LIMITS),
                     **({'recording_protocol':protocol} if protocol in ('record-v5','record-v6','record-v7','record-v8') else {}),
                     **({'audio_intent':dict(identity['resource_binding']['audio_intent'])} if protocol in ('record-v6','record-v7') else {})}
            child=await asyncio.to_thread(bg_jobs.reserve_media_local_child,job['id'],account_id=job['account_id'],token=job['submission_token'],profile=profile)
        except Exception as exc:
            if helper: os.close(helper['fd'])
            raise NotDispatched('recording helper unavailable') from exc
        try: admission()
        except BaseException:
            os.close(helper['fd']); raise
        async def custody_current():
            return await child_custody_current(identity=identity,job_id=job['id'],child=child,scope=self.scope,barrier=self.barrier,owner_for_account=self.owner_for_account)
        def fact_current():
            try:
                owner=self.owner_for_account(identity['account_id']); key=str(owner or '').strip().lower()
                return bool(owner and owner==identity['owner'] and not self.barrier._fenced(key)
                    and not (self.barrier._absolute_fence and self.barrier._absolute_fence(key)))
            except Exception: return False
        try:
            await recording_child.launch(child, helper, store=self.artifact_store, scope_current=lambda:self.scope.current(identity),
                custody_current=custody_current, barrier=self.barrier, owner=self.owner_for_account(job['account_id']), before_spawn=self.before_spawn, fact_current=fact_current, control_identity=identity if protocol == 'record-v8' else None)
        finally:
            self.before_spawn=None
        return Submission('running',child['child_id'])

    async def status(self, job, admission):
        admission(); row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id'])
        child=row.get('local_child') if row else None
        if not isinstance(child,dict) or child.get('state')=='launching': return Submission('unknown_outcome')
        if child.get('state')=='running': return Submission('running',child['child_id']) if recording_child.owns(child) else Submission('unknown_outcome')
        receipt=child.get('receipt') or {}
        if child.get('state')!='terminal': return Submission('unknown_outcome')
        if receipt.get('outcome') != 'finalized': return Submission('failed')
        if not await asyncio.to_thread(bg_jobs.adopt_media_local_artifact,job['id'],account_id=job['account_id'],child_id=child['child_id']): return Submission('unknown_outcome')
        return Submission('collecting',child['child_id'])

    async def cancel(self,job,admission):
        admission();row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id']);child=row.get('local_child') if row else None
        return await recording_child.cancel(child, current=lambda:self.scope.control(job['identity'])) if isinstance(child,dict) else 'unknown'

    async def collect(self,job,admission):
        admission();row=await asyncio.to_thread(bg_jobs.get_media,job['id'],account_id=job['account_id']);child=row.get('local_child') if row else None
        receipt=child.get('receipt') if isinstance(child,dict) and child.get('state')=='terminal' else None
        if not isinstance(receipt,dict) or receipt.get('outcome')!='finalized': raise ValueError('recording receipt unavailable')
        artifact=Artifact(receipt['artifact_id'],receipt['sha256'],'video/webm',receipt['size'])
        await self.fetch_artifact(job['identity'],{'artifact':{'artifact_id':artifact.artifact_id,'sha256':artifact.sha256,'size':artifact.size}},lambda:self.scope.current(job['identity']))
        return artifact

    async def fetch_artifact(self,identity,job,current):
        if not await current(): raise ValueError('recording scope changed')
        artifact=job['artifact'];owner=account_artifact_owner(identity['account_id'])
        data=await asyncio.to_thread(lambda:b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=artifact['artifact_id'])))
        if len(data)!=artifact['size'] or hashlib.sha256(data).hexdigest()!=artifact['sha256'] or not await current(): raise ValueError('recording artifact changed')
        return data
