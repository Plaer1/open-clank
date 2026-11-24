"""Named-human recording through existing J0 and Files authority."""
import asyncio
import os
import uuid
from src import bg_jobs
from src.openclank import recording_child
from src.openclank.recording_contract import ADAPTER, LIMITS, validate_binding
from src.openclank.media_human import HumanMediaService
from src.openclank.media_jobs import request_digest


class RecordingFilesScope:
    def __init__(self, *, facade, context_for, session_current):
        self.facade, self.context_for, self.session_current = facade, context_for, session_current

    async def capture(self, identity, *, destination_ref, destination_name, helper_sha256, recording_protocol=None, audio_intent=None, control_stream=None):
        from src.desktop_capture import capture_enabled
        if not capture_enabled(identity['owner']) or not await self.session_current(identity): raise ValueError('recording scope unavailable')
        context = await self.context_for(identity)
        destination = await self.facade.capture_media_destination(context, resource_ref=destination_ref)
        binding = validate_binding({'interface':ADAPTER, 'ownerAccountID':identity['account_id'],
            'destination_ref':destination['destination_ref'], 'destination_revision':destination['destination_revision'],
            'destination_name':destination_name, 'helper_sha256':helper_sha256, 'limits':dict(LIMITS),
            **({'recording_protocol':recording_protocol} if recording_protocol in ('record-v5','record-v6','record-v7','record-v8') else {}),
            **({'audio_intent':audio_intent} if recording_protocol in ('record-v6','record-v7') else {}),
            **({'control_stream':control_stream} if recording_protocol == 'record-v8' else {})},
            account=identity['account_id'], hashes=[])
        if not await self.session_current(identity): raise ValueError('recording scope changed')
        return binding, destination['destination_label']

    async def current(self, identity):
        try:
            from src.desktop_capture import capture_enabled
            if not capture_enabled(identity['owner']) or not await self.session_current(identity): return False
            context = await self.context_for(identity); binding = identity['resource_binding']
            if binding.get('recording_protocol') == 'record-v8':
                from src.settings import get_user_setting
                from src.openclank.computer_control.hoststate import STATE
                if get_user_setting('desktop_control_recording_enabled',identity['owner'],False) is not True or not STATE.recording_stream_current(identity['owner'],identity['account_id'],identity['session_id'],binding['control_stream']): return False
            destination = await self.facade.capture_media_destination(context, resource_ref=binding['destination_ref'])
            return destination['destination_revision'] == binding['destination_revision'] and capture_enabled(identity['owner']) and await self.session_current(identity)
        except Exception: return False

    async def control(self, identity):
        return (identity.get('resource_binding') or {}).get('ownerAccountID') == identity.get('account_id') and await self.session_current(identity)


class RecordingHumanService(HumanMediaService):
    async def prepare_recording(self, *, account, owner, workspace, generation, session_id, destination_ref, destination_name, audio_intent=None, crop_intent=False, control_session=False):
        async with self.barrier.admitted(owner):
            offered = audio_intent or {'microphone':False,'system_output':False}
            if type(offered) is not dict or set(offered) != {'microphone','system_output'} or any(type(v) is not bool for v in offered.values()): raise ValueError('audio intent unavailable')
            if type(crop_intent) is not bool or type(control_session) is not bool: raise ValueError('recording intent unavailable')
            control_stream = None
            if control_session:
                from src.settings import get_user_setting
                from src.openclank.computer_control.hoststate import STATE
                if crop_intent or any(offered.values()) or get_user_setting('desktop_control_recording_enabled',owner,False) is not True: raise ValueError('recording unavailable')
                control_stream = STATE.recording_stream_binding(owner,account,session_id)
                if control_stream is None: raise ValueError('recording stream unavailable')
                helper = await recording_child.control_recording_readiness(); protocol = 'record-v8'
            elif crop_intent:
                helper = await recording_child.crop_recording_readiness(); protocol = 'record-v7'
            elif any(offered.values()):
                helper = await recording_child.audio_recording_readiness(); protocol = 'record-v6'
            else:
                helper = await recording_child.observed_recording_readiness(); protocol = 'record-v5' if helper else 'record-v3'
                if not helper: helper = await asyncio.to_thread(recording_child.readiness)
            if not helper: raise ValueError('recording helper unavailable')
            try: helper_hash = helper['sha256']
            finally: os.close(helper['fd'])
            operation = uuid.uuid4().hex; request = {'parameters':{}}
            identity = {'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':operation,'root_operation_id':operation,'attempt_id':operation,'request_digest':request_digest(request),
                'adapter_id':ADAPTER,'adapter_version':'1','operation':'screen.record','route_id':ADAPTER,'route_kind':'local',
                'model_revision':'fixed-v1','policy_generation':generation,'input_hashes':[],'resource_binding':{}}
            binding, folder = await self._bounded_read(self.scope.capture(identity, destination_ref=destination_ref,
                destination_name=destination_name, helper_sha256=helper_hash, recording_protocol=protocol, audio_intent=offered, control_stream=control_stream))
            identity['resource_binding'] = binding
            if control_stream is not None and not await self.scope.current(identity): raise ValueError('recording stream changed')
            row = await self.coordinator.prepare(identity, request)
            return {'jobID':row['id'],'revision':row['revision'],'state':row['status'],
                'destination':{'name':destination_name,'folder':folder},'limits':dict(LIMITS),'audio':'selecting' if protocol in ('record-v6','record-v7') else 'unavailable'}

    async def _with_audio_choices(self, result, *, account, owner, job_id):
        async with self.barrier.admitted(owner):
            job=self._job(account,owner,job_id,allow_detached=True)
            child=job.get('local_child')
            if (isinstance(child,dict) and not job.get('chat_detached') and await self.scope.current(job['identity'])):
                choices=recording_child.audio_choices(child)
                if choices is not None and await self.scope.current(job['identity']): result['audioChoices']=choices
            return result

    async def run(self, *, account, owner, job_id, revision, parameters):
        result=await super().run(account=account,owner=owner,job_id=job_id,revision=revision,parameters=parameters)
        return await self._with_audio_choices(result,account=account,owner=owner,job_id=job_id)

    async def action(self, *, account, owner, job_id, action):
        job=self._job(account,owner,job_id,allow_detached=action != 'collect')
        if action == 'status' and (job['identity'].get('resource_binding') or {}).get('recording_protocol') in ('record-v6','record-v7'):
            try: live = await self.coordinator._scope(job['identity'],control=True)
            except Exception: live = False
            if not live:
                async with self.barrier.admitted(owner):
                    if self.coordinator._account(job['identity']) != owner: raise ValueError('recording account changed')
                    row=self._job(account,owner,job_id,allow_detached=True)
                    if self.coordinator._account(row['identity']) != owner: raise ValueError('recording account changed')
                    result=self.project(row)
                    result.pop('result',None)
                    result.update(cancelAvailable=False,stopAvailable=False,partialAvailable=False,partialRecovery=None)
                    return result
        result=await super().action(account=account,owner=owner,job_id=job_id,action=action)
        return await self._with_audio_choices(result,account=account,owner=owner,job_id=job_id)

    async def select_audio(self, *, account, owner, job_id, revision, selection_id, generation, microphone, system_output, request_current, crop=None):
        async with self.barrier.admitted(owner):
            job=self._job(account,owner,job_id);child=job.get('local_child')
            async def current():
                return await self.scope.current(job['identity']) is True and await request_current() is True
            if not isinstance(child,dict) or await current() is not True: raise ValueError('audio selection unavailable')
            await recording_child.select_audio(child,revision=revision,selection_id=selection_id,generation=generation,
                microphone=microphone,system_output=system_output,current=current,crop=crop)
            if await request_current() is not True: raise ValueError('audio authority changed')
            return self.project(self._job(account,owner,job_id))

    async def stop_recording(self, *, account, owner, job_id, revision, stop_id):
        async with self.barrier.admitted(owner):
            job = self._job(account, owner, job_id)
            if not await self.scope.control(job['identity']): raise ValueError('recording control unavailable')
            claim = await asyncio.to_thread(bg_jobs.issue_recording_stop, job_id, account_id=account, revision=revision, stop_id=stop_id)
            if claim['issue']:
                await recording_child.stop(claim['record']['local_child'], stop_id=stop_id, token=claim['token'], current=lambda:self.scope.control(job['identity']))
            return self.project(self._job(account, owner, job_id))

    async def partial_preview(self, *, account, owner, job_id, destination_ref, destination_name):
        async with self.barrier.admitted(owner):
            job = self._job(account, owner, job_id); identity = job['identity']; partial = job.get('recording_partial')
            if (not isinstance(partial,dict) or partial.get('outcome')!='partial' or partial.get('finalized') is not False
                or not isinstance(destination_name,str) or not destination_name.endswith('.partial')
                or len(destination_name.encode('utf-8'))>240 or any(c in destination_name for c in ('/','\\','\x00'))
                or not await self.scope.session_current(identity)): raise ValueError('partial recovery unavailable')
            previous = job.get('partial_recovery') or {}
            if previous.get('state') in {'dispatched','unknown'}: raise ValueError('original partial recovery requires status')
            await self._partial_bytes(account, partial)
            context = await self.recovery_context_for(identity)
            destination = await self.scope.facade.capture_media_destination(context, resource_ref=destination_ref)
            if not await self.scope.session_current(identity): raise ValueError('partial recovery unavailable')
            binding = {'destination_ref':destination['destination_ref'],'destination_revision':destination['destination_revision'],
                'destination_name':destination_name,'generation':context.policy_generation,'artifact_id':partial['artifact_id'],
                'sha256':partial['sha256'],'size':partial['size']}
            job = await asyncio.to_thread(bg_jobs.prepare_recording_partial_recovery,job_id,account_id=account,revision=job['revision'],binding=binding)
            return {'jobID':job_id,'revision':job['revision'],'recoveryID':job['partial_recovery']['id'],
                'destination':{'folder':destination['destination_label'],'name':destination_name},
                'artifact':{'sha256':partial['sha256'],'size':partial['size'],'finalized':False}}

    async def _partial_bytes(self, account, partial):
        import hashlib
        from src.openclank.artifacts import account_artifact_owner
        owner = account_artifact_owner(account)
        row = await asyncio.to_thread(self.artifact_store.get,owner=owner,artifact_id=partial['artifact_id'])
        if row.state != 'acknowledged' or row.content_sha256 != partial['sha256'] or row.size_bytes != partial['size'] or not 0 < row.size_bytes <= LIMITS['max_bytes']:
            raise ValueError('partial artifact unavailable')
        data = await asyncio.to_thread(lambda:b''.join(self.artifact_store.read_chunks(owner=owner,artifact_id=row.id)))
        if len(data)!=partial['size'] or hashlib.sha256(data).hexdigest()!=partial['sha256']: raise ValueError('partial artifact changed')
        return data

    async def partial_save(self, *, account, owner, job_id, revision, recovery_id):
        async with self.barrier.admitted(owner):
            job = self._job(account, owner, job_id); identity=job['identity']; recovery=job.get('partial_recovery') or {}
            if recovery.get('id') != recovery_id or not await self.scope.session_current(identity): raise ValueError('partial recovery unavailable')
            context = await self.recovery_context_for(identity)
            async def settle(receipt, record, token):
                items=receipt.get('items') if isinstance(receipt,dict) else None
                evidence={'media_job_id':job_id,'recovery_id':recovery_id,'artifact_sha256':record['binding']['sha256']}
                self._check_files_binding(context,recovery_id,evidence)
                if (not isinstance(receipt,dict) or receipt.get('state')!='complete' or not isinstance(items,list) or len(items)!=1
                    or items[0].get('item_id')!='output' or items[0].get('outcome')!='committed' or not items[0].get('resource_ref')):
                    return False
                return await asyncio.to_thread(bg_jobs.finish_recording_partial_recovery,job_id,account_id=account,recovery_id=recovery_id,token=token,resource_ref=items[0]['resource_ref'])
            if recovery.get('state')=='complete': return self.project(job)
            if recovery.get('state') in {'dispatched','unknown'}:
                receipt=await self.scope.facade.operation_receipt(context,operation_id=recovery_id)
                await settle(receipt,recovery,recovery['token'])
                return self.project(self._job(account,owner,job_id))
            claim=await asyncio.to_thread(bg_jobs.claim_recording_partial_recovery,job_id,account_id=account,revision=revision,recovery_id=recovery_id)
            job=claim['record']; recovery=job['partial_recovery']; binding=recovery['binding']; token=claim['token']; active={'value':True}
            async def current():
                try:
                    fresh=await asyncio.to_thread(bg_jobs.get_media,job_id,account_id=account); pending=(fresh or {}).get('partial_recovery') or {}
                    return bool(active['value'] and fresh and not fresh.get('chat_detached') and not fresh.get('publication_withdrawn')
                        and pending.get('id')==recovery_id and pending.get('token')==token and pending.get('state')=='dispatched'
                        and await self.scope.session_current(identity) and (await self.recovery_context_for(identity)).policy_generation==binding['generation'])
                except Exception: return False
            try:
                data=await self._partial_bytes(account,job['recording_partial'])
                class Upload:
                    filename=binding['destination_name']; content_type='application/octet-stream'
                    def __init__(self): self.offset=0
                    async def read(self,size):
                        chunk=data[self.offset:self.offset+size]; self.offset+=len(chunk); return chunk
                if not await current(): raise ValueError('partial recovery authority changed')
                async def owned():
                    async with self.barrier.admitted(owner):
                        return await self.scope.facade.import_file(context,upload=Upload(),before_commit=current,
                            expected_destination_revision=binding['destination_revision'],
                            source_evidence={'media_job_id':job_id,'recovery_id':recovery_id,'artifact_sha256':binding['sha256']},
                            metadata={'operation_id':recovery_id,'item_id':'output','generation':binding['generation'],
                                'destination_ref':binding['destination_ref'],'name':binding['destination_name'],'collision':'fail'})
                task=asyncio.create_task(owned())
                async def late(done):
                    try: await settle(done.result(),recovery,token)
                    except BaseException: pass
                try: done,_=await asyncio.wait({task},timeout=self.coordinator.timeout)
                except asyncio.CancelledError:
                    active['value']=False; task.add_done_callback(lambda result:asyncio.create_task(late(result))); raise
                if not done:
                    active['value']=False; task.add_done_callback(lambda result:asyncio.create_task(late(result)))
                    raise TimeoutError('partial recovery outcome unknown')
                active['value']=False; await settle(task.result(),recovery,token)
                return self.project(self._job(account,owner,job_id))
            except BaseException:
                active['value']=False
                await asyncio.to_thread(bg_jobs.finish_recording_partial_recovery,job_id,account_id=account,recovery_id=recovery_id,token=token,resource_ref=None)
                raise

    @staticmethod
    def project(job):
        result = HumanMediaService.project(job)
        control = job.get('stop_control') or {}
        result.update(outputMime='video/webm', stopAvailable=job.get('status') == 'running' and not control,
                      stopState=control.get('state'), partialAvailable=isinstance(job.get('recording_partial'), dict),
                      audio=result.get('audio','unavailable'), partialRecovery=({'recoveryID':job['partial_recovery'].get('id') or job['partial_recovery'].get('recoveryID'), 'state':job['partial_recovery'].get('state')} if isinstance(job.get('partial_recovery'),dict) else None))
        return result
