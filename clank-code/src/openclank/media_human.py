"""One explicitly reviewed human media invocation, on the existing job store."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from src import bg_jobs
from src.openclank.media_adapters.comfyui import render_recipe
from src.openclank.media_adapters.base import describe_artifact, media_operation_facts
from src.openclank.media_jobs import request_digest
from src.openclank.artifacts import media_artifact_owner


def _output_profile(artifact):
    descriptor = describe_artifact(artifact)
    return descriptor['extension'], descriptor['mime_type'], descriptor['maximum']


def model_for_job(job):
    """Describe only an exact model retained in a known original job."""
    if type(job) is not dict or type(job.get('identity')) is not dict:
        return None
    identity = job['identity']
    facts = media_operation_facts(identity.get('adapter_id'), identity.get('adapter_version'))
    model = identity.get('model_revision')
    if facts is None or identity.get('operation') != facts.operation or type(model) is not str:
        return None
    match = next(((mid, label) for mid, label in facts.model_labels if mid == model), None)
    return {'id':match[0], 'label':match[1]} if match else None


def model_for_profile(adapter_id, adapter_version):
    """Describe a compiled profile only when it fixes exactly one model."""
    facts = media_operation_facts(adapter_id, adapter_version)
    if facts is None or len(facts.model_labels) != 1:
        return None
    model, label = facts.model_labels[0]
    return {'id':model, 'label':label}


class HumanMediaService:
    def __init__(self, *, coordinator, scope, profile_selection, barrier, recovery_context_for=None, artifact_store=None):
        self.coordinator, self.scope = coordinator, scope
        self.profile_selection, self.barrier = profile_selection, barrier
        self.recovery_context_for, self.artifact_store = recovery_context_for, artifact_store

    async def _bounded_read(self, awaitable):
        task = asyncio.create_task(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.coordinator.timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(lambda result: None if result.cancelled() else result.exception())
            raise
        if not done:
            task.cancel()
            task.add_done_callback(lambda result: None if result.cancelled() else result.exception())
            raise TimeoutError('media preparation deadline exceeded')
        return task.result()

    async def prepare(self, *, account, owner, workspace, generation, session_id, connection_id,
                      recipe_ref, destination_ref, destination_name, parameters, output_profile='png'):
        if output_profile not in ('png', 'flac'):
            raise ValueError('unsupported media output profile')
        async with self.barrier.admitted(owner):
            profile = await self._bounded_read(self.profile_selection(owner, account, connection_id))
            operation = uuid.uuid4().hex
            request = {'parameters':parameters}
            identity = {'account_id':account, 'owner':owner, 'session_id':session_id, 'workspace_id':workspace,
                'actor_id':'human', 'turn_id':operation, 'root_operation_id':operation, 'attempt_id':operation,
                'request_digest':request_digest(request), 'adapter_id':'comfyui-flac-v1' if output_profile == 'flac' else 'comfyui', 'adapter_version':'1',
                'operation':'audio.generate' if output_profile == 'flac' else 'image.generate', 'route_id':connection_id, 'route_kind':'remote',
                'model_revision':'unknown', 'policy_generation':generation, 'input_hashes':[],
                'resource_binding':profile}
            binding, (raw, name, folder) = await self._bounded_read(self.scope.capture(identity, recipe_ref=recipe_ref,
                destination_ref=destination_ref, destination_name=destination_name))
            if output_profile == 'flac':
                from src.openclank.media_adapters.comfy_flac import render_flac_recipe
                _workflow, summary = render_flac_recipe(raw, parameters)
            else:
                _workflow, summary = render_recipe(raw, parameters)
            identity['resource_binding'] = binding
            job = await self.coordinator.prepare(identity, request)
            return {'jobID':job['id'], 'revision':job['revision'], 'state':job['status'],
                'profile':{key:binding[key] for key in ('connectionID','connectionRevision','interface')},
                'destination':{'name':binding['destination_name'], 'folder':folder},
                'recipe':{'name':name, 'sha256':binding['recipe_sha256'], 'nodeClasses':summary['node_classes'], 'slots':summary['slots']},
                'parameters':parameters, **({'outputProfile':'flac'} if output_profile == 'flac' else {})}

    async def prepare_scoped(self, *, identity, connection_id, recipe_ref, destination_ref, destination_name,
                             parameters, once=False, operation='material.generate'):
        if operation != 'material.generate' or type(parameters) is not dict:
            raise ValueError('media operation unavailable')
        from src.openclank.media_adapters.comfyui import parse_material_recipe
        profile = await self._bounded_read(self.profile_selection(identity['owner'], identity['account_id'], connection_id))
        request = {'parameters':parameters}
        identity = {**identity, 'request_digest':request_digest(request), 'adapter_id':'comfyui-material-v1',
            'adapter_version':'1', 'operation':operation, 'route_id':connection_id, 'route_kind':'remote',
            'model_revision':'unknown', 'input_hashes':[], 'resource_binding':profile}
        binding, (raw, name, folder) = await self._bounded_read(self.scope.capture(identity, recipe_ref=recipe_ref,
            destination_ref=destination_ref, destination_name=destination_name))
        _workflow, summary = parse_material_recipe(raw, parameters)
        binding = {**binding, 'material_resolution':summary['resolution'], 'material_outputs':summary['outputs']}
        identity['resource_binding'] = binding
        if await self._bounded_read(self.scope.current(identity)) is not True:
            raise ValueError('media source changed')
        if once: job, created = await self.coordinator.prepare_once(identity, request)
        else: job, created = await self.coordinator.prepare(identity, request), True
        review = {'profile':{key:binding[key] for key in ('connectionID','connectionRevision','interface')},
            'destination':{'name':binding['destination_name'], 'folder':folder},
            'recipe':{'name':name,'sha256':binding['recipe_sha256'],'nodeClasses':summary['node_classes'],
                      'slots':summary['slots'],'resolution':summary['resolution'],'outputs':summary['outputs']},
            'parameters':parameters}
        return identity, request, job, created, review

    async def prepare_generated_material(self, *, account, owner, workspace, generation, session_id, connection_id,
                                         recipe_ref, destination_ref, destination_name, parameters):
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation}
            _,_,job,_,review=await self.prepare_scoped(identity=identity,connection_id=connection_id,recipe_ref=recipe_ref,
                destination_ref=destination_ref,destination_name=destination_name,parameters=parameters)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],**review}

    async def prepare_voidless_scoped(self, *, identity, connection_id, provider_account_id, destination_ref,
                                      destination_name, parameters, once=False):
        from src.openclank.media_adapters.voidless import validate_request
        request={'parameters':parameters}; validate_request(request)
        profile=await self._bounded_read(self.profile_selection(identity['owner'],identity['account_id'],connection_id,provider_account_id))
        identity={**identity,'request_digest':request_digest(request),'adapter_id':'voidless-image-v1','adapter_version':'1',
            'operation':'image.generate','route_id':connection_id,'route_kind':'remote','model_revision':'flux2-klein',
            'input_hashes':[],'resource_binding':profile}
        binding,folder=await self._bounded_read(self.scope.capture(identity,destination_ref=destination_ref,destination_name=destination_name))
        identity['resource_binding']=binding
        if await self._bounded_read(self.scope.current(identity)) is not True: raise ValueError('media scope changed')
        if once: job,created=await self.coordinator.prepare_once(identity,request)
        else: job,created=await self.coordinator.prepare(identity,request),True
        review={'profile':{k:binding[k] for k in ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')},
            'destination':{'name':destination_name,'folder':folder},'parameters':parameters,
            'notice':'One Voidless image may consume credits. The fixed output is a 64-pixel PNG; cancellation is unavailable.'}
        return identity,request,job,created,review

    async def prepare_voidless(self, *, account, owner, workspace, generation, session_id, connection_id,
                               provider_account_id, destination_ref, destination_name, parameters):
        async with self.barrier.admitted(owner):
            turn=uuid.uuid4().hex
            identity={'account_id':account,'owner':owner,'session_id':session_id,'workspace_id':workspace,'actor_id':'human',
                'turn_id':turn,'root_operation_id':turn,'attempt_id':turn,'policy_generation':generation}
            _,_,job,_,review=await self.prepare_voidless_scoped(identity=identity,connection_id=connection_id,
                provider_account_id=provider_account_id,destination_ref=destination_ref,destination_name=destination_name,parameters=parameters)
            return {'jobID':job['id'],'revision':job['revision'],'state':job['status'],**review}

    @staticmethod
    def _job(account, owner, job_id, *, allow_detached=False):
        job = bg_jobs.get_media(job_id, account_id=account)
        if not job or job.get('owner') != owner or (job.get('chat_detached') and not allow_detached):
            raise ValueError('media job unavailable')
        return job

    @staticmethod
    def project(job):
        identity = job.get('identity') or {}
        adapter = job.get('_adapter_id', identity.get('adapter_id'))
        version = job.get('_adapter_version', identity.get('adapter_version'))
        facts = media_operation_facts(adapter, version)
        result = {'jobID':job['id'], 'revision':job['revision'], 'state':job['status'],
                  'cancelAvailable':facts.cancel_available if facts else False}
        if facts and facts.projection_kind == 'recording':
            control = job.get('stop_control') or {}
            choice = job.get('audio_selection')
            if isinstance(choice,dict):
                result['audioSelection']={'state':choice.get('state'),'selectionID':choice.get('selection_id'),
                    'selectionDigest':choice.get('selection_digest'),'generation':choice.get('generation'),'selectedKinds':choice.get('selected_kinds'), **({'crop':choice['crop']} if isinstance(choice.get('crop'),dict) else {})}
            tracks = job.get('audio_tracks') if 'audio_tracks' in job else bg_jobs._media_recording_audio_tracks(job)
            if tracks is not None: result['audioTracks']=tracks
            observations = job.get('recording_observations') or bg_jobs._media_recording_observations(job)
            if observations is not None: result['recordingObservations'] = observations
            result.update(outputMime='video/webm', stopAvailable=job.get('status') == 'running' and not control,
                          stopState=control.get('state'), partialAvailable=bool(job.get('partial_available') or job.get('recording_partial')),
                          cancelAvailable=job.get('status') in {'running','stopping','cancel_requested'}, audio=('selected' if isinstance(choice,dict) and choice.get('state')=='accepted' and choice.get('selected_kinds') else 'off' if isinstance(choice,dict) and choice.get('state')=='accepted' else 'selecting' if ((identity.get('resource_binding') or {}).get('recording_protocol') or job.get('_recording_protocol'))in ('record-v6','record-v7') else 'unavailable'),
                          partialRecovery=job.get('partial_recovery'), recordingPhase=job.get('recording_phase'))
        if facts and facts.projection_kind == 'trim':
            result.update(outputMime='video/webm',cancelAvailable=job.get('status') in {'running','cancel_requested'})
            timing=job.get('trim_timing') or bg_jobs._media_trim_timing(job)
            if timing is not None: result['trimTiming']=timing
        if facts and facts.mime_type != 'image/png':
            result['outputMime'] = facts.mime_type
        if facts and 'mesh.refine' in facts.follow_up_operations and job.get('status')=='complete' and job.get('result') and not job.get('chat_detached'):
            result['canRefine'] = True
        model = model_for_job(job)
        if model is not None:
            result['model'] = model
        if job.get('reason') == 'not_dispatched': result['reason'] = 'not_dispatched'
        if job.get('cancel_outcome') == 'unsupported': result['reason'] = 'unsupported'
        if job.get('reconciliation_required'): result['reason'] = job['reconciliation_required']
        if job.get('result'):
            receipt = job['result']
            result['result'] = {'operationID':receipt['operation_id'], 'itemID':receipt['item_id'],
                'resourceRef':receipt['resource_ref'], 'sha256':receipt['sha256'], 'mimeType':receipt['mime_type']}
            if facts and facts.metadata_kind == 'animation' and isinstance(receipt.get('animation'),dict):
                result['result']['animation'] = dict(receipt['animation'])
            if facts and facts.metadata_kind == 'audio' and isinstance(receipt.get('audio'),dict):
                result['result']['audio'] = dict(receipt['audio'])
            if facts and facts.metadata_kind in {'four_heading','rig_clip','sprite_sheet','sprite_crops'} and isinstance(receipt.get('layout'),dict):
                result['result']['layout'] = dict(receipt['layout'])
        return result

    async def run(self, *, account, owner, job_id, revision, parameters):
        if type(revision) is not int or revision < 1: raise ValueError('invalid media revision')
        job = self._job(account, owner, job_id)
        result = await self.coordinator.submit(job['identity'], job_id, {'parameters':parameters}, expected_revision=revision)
        return self.project(result)

    async def action(self, *, account, owner, job_id, action):
        if action not in {'status','cancel','reconcile','collect'}: raise ValueError('invalid media action')
        job = self._job(account, owner, job_id, allow_detached=action != 'collect')
        if action == 'cancel' and not self.coordinator._adapter(job['identity']).supports_cancel:
            return self.project({**job, 'cancel_outcome':'unsupported'})
        kwargs = {'reconcile_detached':True} if action in {'status','reconcile'} else {}
        result = await getattr(self.coordinator, action)(job['identity'], job_id, **kwargs)
        return self.project(result)

    def _artifact_bytes(self, account, artifact, identity):
        owner = media_artifact_owner(account)
        row = self.artifact_store.get(owner=owner, artifact_id=artifact['artifact_id'])
        descriptor = describe_artifact(artifact)
        mime, maximum = descriptor['mime_type'], descriptor['maximum']
        if (row.state != 'acknowledged' or row.content_sha256 != artifact['sha256']
            or row.size_bytes != artifact['size'] or (row.media_type != mime and (identity.get('adapter_id') not in ('screen-record-v1','trim-webm-v1') or mime != 'video/webm')) or artifact.get('mime_type') != mime
            or not 0 < row.size_bytes <= maximum):
            raise ValueError('retained media artifact unavailable')
        chunks = []; total = 0
        for chunk in self.artifact_store.read_chunks(owner=owner, artifact_id=row.id):
            total += len(chunk)
            if total > row.size_bytes: raise ValueError('retained media artifact changed')
            chunks.append(chunk)
        data = b''.join(chunks)
        if len(data) != row.size_bytes or hashlib.sha256(data).hexdigest() != artifact['sha256']:
            raise ValueError('retained media artifact changed')
        return data

    def _publication_evidence(self, job):
        return {'media_job_id':job['id'], 'identity_digest':job['identity_digest'], 'artifact_sha256':job['artifact']['sha256']}

    def _recovery_evidence(self, job, recovery_id):
        return {'media_job_id':job['id'], 'recovery_id':recovery_id, 'artifact_sha256':job['artifact']['sha256']}

    async def _recovery_current(self, identity, context, destination_ref, destination_name, destination_revision):
        return True

    def _check_files_binding(self, context, operation_id, evidence):
        stored = self.scope.facade._load_operation(context, operation_id)
        actual = ((stored or {}).get('receipt') or {}).get('_import_binding', {}).get('derived_source')
        if actual != evidence: raise ValueError('Files receipt binding unavailable')

    async def recovery_preview(self, *, account, owner, job_id, destination_ref, destination_name):
        async with self.barrier.admitted(owner):
            job = self._job(account, owner, job_id)
            identity = job['identity']
            extension, _mime, _maximum = _output_profile(job.get('artifact'))
            if (not isinstance(destination_name, str) or not destination_name or len(destination_name.encode('utf-8')) > 240
                or destination_name in {'.','..'} or any(c in destination_name for c in ('/','\\','\x00'))
                or not destination_name.lower().endswith(extension)): raise ValueError('invalid recovery name')
            if job.get('publication_withdrawn') or not await self.scope.session_current(identity):
                raise ValueError('media recovery unavailable')
            context = await self.recovery_context_for(identity)
            if job.get('unknown_action') == 'publish':
                # Query the exact old operation; an absent or nonterminal receipt
                # never proves that it did not publish.
                receipt = await self.scope.facade.operation_receipt(context, operation_id=job_id)
                self._check_files_binding(context, job_id, self._publication_evidence(job))
                items = receipt.get('items') if isinstance(receipt, dict) else None
                if receipt.get('state') == 'complete' and isinstance(items, list) and len(items) == 1 and items[0].get('outcome') == 'committed' and items[0].get('resource_ref'):
                    artifact = job['artifact']
                    evidence = {
                        'operation_id':job_id, 'item_id':'output', 'resource_ref':items[0]['resource_ref'],
                        'sha256':artifact['sha256'], 'mime_type':artifact['mime_type']}
                    evidence.update(describe_artifact(artifact)['metadata'])
                    bg_jobs.record_media_publication(job_id, account_id=account, token=job['publication_token'], result=evidence)
                raise ValueError('previous publication requires reconciliation')
            previous = job.get('recovery') or {}
            if previous.get('state') in {'dispatched','unknown'}:
                receipt = await self.scope.facade.operation_receipt(context, operation_id=previous['id'])
                self._check_files_binding(context, previous['id'], self._recovery_evidence(job,previous['id']))
                await self._settle_recovery(account, job, previous, receipt)
                raise ValueError('previous recovery requires reconciliation')
            artifact = job.get('artifact')
            if not isinstance(artifact, dict): raise ValueError('retained media artifact unavailable')
            await self._bounded_read(asyncio.to_thread(self._artifact_bytes, account, artifact, identity))
            destination = await self._bounded_read(self.scope.facade.capture_media_destination(context, resource_ref=destination_ref))
            if not await self._recovery_current(identity,context,destination_ref,destination_name,destination['destination_revision']):
                raise ValueError('media recovery source changed')
            binding = {'destination_ref':destination_ref, 'destination_revision':destination['destination_revision'],
                'destination_name':destination_name, 'generation':context.policy_generation,
                'artifact_id':artifact['artifact_id'], 'sha256':artifact['sha256'], 'size':artifact['size']}
            self.coordinator._window(identity).check()
            job = bg_jobs.prepare_media_recovery(job_id, account_id=account, revision=job['revision'], binding=binding)
            return {'jobID':job_id, 'revision':job['revision'], 'recoveryID':job['recovery']['id'],
                'destination':{'folder':destination['destination_label'], 'name':destination_name},
                'artifact':{'sha256':artifact['sha256'], 'size':artifact['size']}}

    async def recovery_save(self, *, account, owner, job_id, revision, recovery_id):
        async with self.barrier.admitted(owner):
            job = self._job(account, owner, job_id)
            recovery = job.get('recovery') or {}
            if recovery.get('id') != recovery_id or job.get('publication_withdrawn'):
                raise ValueError('media recovery unavailable')
            if recovery.get('state') == 'complete': return self.project(job)
            identity = job['identity']
            if not await self.scope.session_current(identity): raise ValueError('media recovery unavailable')
            context = await self.recovery_context_for(identity)
            if recovery.get('state') in {'dispatched','unknown'}:
                receipt = await self.scope.facade.operation_receipt(context, operation_id=recovery_id)
                self._check_files_binding(context, recovery_id, self._recovery_evidence(job,recovery_id))
                return await self._settle_recovery(account, job, recovery, receipt)
            claim = bg_jobs.claim_media_recovery(job_id, account_id=account, revision=revision, recovery_id=recovery_id)
            job = claim['record']; recovery = job['recovery']; binding = recovery['binding']; token = claim['token']
            window = {'active':True}
            admission = self.coordinator._window(identity)
            async def current():
                try:
                    admission.check()
                    fresh = bg_jobs.get_media(job_id, account_id=account)
                    active = (fresh or {}).get('recovery') or {}
                    valid = bool(window['active'] and fresh and not fresh.get('chat_detached') and not fresh.get('publication_withdrawn')
                        and active.get('id') == recovery_id and active.get('token') == token
                        and active.get('state') == 'dispatched' and await self.scope.session_current(identity)
                        and (await self.recovery_context_for(identity)).policy_generation == binding['generation']
                        and await self._recovery_current(identity,context,binding['destination_ref'],binding['destination_name'],binding['destination_revision']))
                    admission.check()
                    return valid
                except Exception:
                    return False
            try:
                data = await self._bounded_read(asyncio.to_thread(self._artifact_bytes, account, job['artifact'], identity))
                _extension, mime, _maximum = _output_profile(job.get('artifact'))
                class Upload:
                    filename = binding['destination_name']; content_type = mime
                    def __init__(self): self.offset = 0
                    async def read(self, size):
                        chunk = data[self.offset:self.offset+size]; self.offset += len(chunk); return chunk
                if not await current(): raise ValueError('media recovery authority changed')
                async def owned_import():
                    async with self.barrier.admitted(owner):
                        return await self.scope.facade.import_file(context, upload=Upload(), before_commit=current,
                            expected_destination_revision=binding['destination_revision'],
                            source_evidence=self._recovery_evidence(job,recovery_id),
                            metadata={'operation_id':recovery_id, 'item_id':'output', 'generation':binding['generation'],
                                'destination_ref':binding['destination_ref'], 'name':binding['destination_name'], 'collision':'fail'})
                task = asyncio.create_task(owned_import())
                async def retain_late(done):
                    try: await self._settle_recovery(account, job, recovery, done.result())
                    except BaseException: pass
                try:
                    done, _ = await asyncio.wait({task}, timeout=self.coordinator.timeout)
                except asyncio.CancelledError:
                    window['active']=False; task.add_done_callback(lambda result:asyncio.create_task(retain_late(result)))
                    raise
                if not done:
                    window['active']=False; task.add_done_callback(lambda result:asyncio.create_task(retain_late(result)))
                    raise TimeoutError('media recovery outcome unknown')
                receipt = task.result()
                window['active']=False
                return await self._settle_recovery(account, job, recovery, receipt)
            except BaseException:
                window['active']=False
                bg_jobs.finish_media_recovery(job_id, account_id=account, recovery_id=recovery_id, token=token, result=None)
                raise
            finally:
                admission.close()

    async def _settle_recovery(self, account, job, recovery, receipt):
        items = receipt.get('items') if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict) or receipt.get('state') != 'complete' or not isinstance(items, list)
            or len(items) != 1 or items[0].get('item_id') != 'output' or items[0].get('outcome') != 'committed'
            or not items[0].get('resource_ref')):
            return {'jobID':job['id'], 'revision':job['revision'], 'state':'unknown_outcome', 'reason':'files_receipt'}
        _extension, mime, _maximum = _output_profile(job.get('artifact'))
        if job['artifact'].get('mime_type') != mime: raise ValueError('media artifact changed')
        result = {'operation_id':recovery['id'], 'item_id':'output', 'resource_ref':items[0]['resource_ref'],
                  'sha256':job['artifact']['sha256'], 'mime_type':mime}
        result.update(describe_artifact(job['artifact'])['metadata'])
        if not bg_jobs.finish_media_recovery(job['id'], account_id=account, recovery_id=recovery['id'], token=recovery['token'], result=result):
            raise ValueError('media recovery changed')
        return self.project(bg_jobs.get_media(job['id'], account_id=account))
