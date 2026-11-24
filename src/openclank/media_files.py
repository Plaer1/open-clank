"""Private media publication through the existing Files import authority."""
from __future__ import annotations

import asyncio
import hashlib

from src.openclank.media_adapters.base import describe_artifact


def bound_profile_resolver(*, store, auth_manager, account_barrier):
    """Adapt the existing account-admitted profile resolver to a frozen job."""
    from src.openclank.comfy_provider import admitted_comfy_profile
    def resolve(identity):
        binding = identity['resource_binding']
        owner = auth_manager.username_for_account_id(identity['account_id'])
        return admitted_comfy_profile(store=store, auth_manager=auth_manager, account_barrier=account_barrier,
            owner=owner, ownerAccountID=binding['ownerAccountID'], connectionID=binding['connectionID'],
            connectionRevision=binding['connectionRevision'], interface=binding['interface'])
    return resolve


def human_session_authority(*, session_manager, auth_manager):
    """Recheck the current owned chat; this grants no Files or project access."""
    async def current(identity):
        def check():
            account = identity['account_id']
            owner = auth_manager.username_for_account_id(account)
            if not owner or auth_manager.account_id(owner) != account: return False
            try: session = session_manager.get_session(identity['session_id'])
            except (KeyError, RuntimeError): return False
            return (session.owner == owner and str(getattr(session, 'workspace_id', None) or 'default') == identity['workspace_id']
                    and not getattr(session, 'incognito', False)
                    and auth_manager.account_id(owner) == account)
        return await asyncio.to_thread(check)
    return current


async def no_project_capture(_identity):
    return {'project_ref':None, 'project_revision':None}


async def no_project_current(identity):
    binding = identity.get('resource_binding') or {}
    return binding.get('project_ref') is None and binding.get('project_revision') is None


class MediaFilesScope:
    """Join current Files, session, project and profile authorities privately."""
    def __init__(self, *, facade, context_for, session_current, project_current, capture_project, resolve_profile):
        self.facade = facade
        self.context_for = context_for
        self.session_current = session_current
        self.project_current = project_current
        self.capture_project = capture_project
        self.resolve_profile = resolve_profile

    async def capture(self, identity, *, recipe_ref, destination_ref, destination_name):
        """Issue a binding from the caller's current, separately authorized scope."""
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id']
            or context.workspace_id != identity['workspace_id']
            or context.policy_generation != identity['policy_generation']
            or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        async with self.resolve_profile(identity) as (profile, assert_current):
            profile_binding = profile.job_binding()
            if (profile_binding['ownerAccountID'] != identity['account_id']
                or profile_binding['connectionID'] != identity['route_id']):
                raise ValueError('media profile changed')
            generated=identity.get('operation')=='material.generate'
            recipe, data = await self.facade.capture_media_recipe(context, resource_ref=recipe_ref, **({'include_resource_id':True} if generated else {}))
            destination = await self.facade.capture_media_destination(context, resource_ref=destination_ref)
            project = await self.capture_project(identity)
            if not isinstance(project, dict) or set(project) != {'project_ref','project_revision'}:
                raise ValueError('media project authority unavailable')
            await assert_current()
            if not await self.session_current(identity): raise ValueError('media session changed')
            frozen_recipe = {key:recipe[key] for key in ('recipe_ref','recipe_revision','recipe_sha256')}
            if generated: frozen_recipe['recipe_resource_id']=recipe['recipe_resource_id']
            frozen_destination = {key:destination[key] for key in ('destination_ref','destination_revision')}
            return {**profile_binding, **frozen_recipe, **frozen_destination, **project, 'destination_name':destination_name}, (data, recipe['recipe_name'], destination['destination_label'])

    async def recipe(self, identity):
        if not await self.current(identity): raise ValueError('media scope changed')
        binding = identity['resource_binding']
        context = await self.context_for(identity)
        recipe,data = await self.facade.capture_media_recipe(context, resource_ref=binding['recipe_ref'],
            expected_revision=binding['recipe_revision'], **({'include_resource_id':True} if identity.get('operation')=='material.generate' else {}))
        if identity.get('operation')=='material.generate' and recipe.get('recipe_resource_id')!=binding.get('recipe_resource_id'):
            raise ValueError('media recipe changed')
        return recipe,data

    async def control(self, identity):
        try:
            binding = identity['resource_binding']
            async with self.resolve_profile(identity) as (profile, assert_current):
                actual = profile.job_binding()
                if any(actual.get(key) != binding[key] for key in ('connectionID','connectionRevision','interface','ownerAccountID')):
                    return False
                await assert_current()
                return True
        except Exception:
            return False

    async def current(self, identity):
        try:
            binding = identity['resource_binding']
            context = await self.context_for(identity)
            if (context.owner_subject_id != identity['account_id']
                or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation']
                or not await self.session_current(identity)
                or not await self.project_current(identity)):
                return False
            async with self.resolve_profile(identity) as (profile, assert_current):
                actual = profile.job_binding()
                if any(actual.get(key) != binding[key] for key in ('connectionID','connectionRevision','interface','ownerAccountID')):
                    return False
                recipe, _ = await self.facade.capture_media_recipe(context, resource_ref=binding['recipe_ref'],
                    expected_revision=binding['recipe_revision'], **({'include_resource_id':True} if identity.get('operation')=='material.generate' else {}))
                if identity.get('operation')=='material.generate' and recipe.get('recipe_resource_id')!=binding.get('recipe_resource_id'):
                    return False
                destination = await self.facade.capture_media_destination(context, resource_ref=binding['destination_ref'])
                await assert_current()
                return (recipe['recipe_sha256'] == binding['recipe_sha256']
                        and destination['destination_revision'] == binding['destination_revision']
                        and await self.session_current(identity) and await self.project_current(identity))
        except Exception:
            return False


class MediaFilesPublisher:
    def __init__(self, *, facade, context_for, fetch_artifact, source_evidence_for=None):
        self.facade = facade
        self.context_for = context_for
        self.fetch_artifact = fetch_artifact
        self.source_evidence_for = source_evidence_for

    async def publish(self, identity, job, current):
        binding = identity.get('resource_binding')
        artifact = job.get('artifact')
        if not isinstance(binding, dict) or not isinstance(artifact, dict):
            raise ValueError('frozen media publication evidence is unavailable')
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id']
            or context.workspace_id != identity['workspace_id']
            or context.policy_generation != identity['policy_generation']):
            raise ValueError('media publication authority changed')
        data = await self.fetch_artifact(identity, job, current)
        descriptor = describe_artifact(artifact)
        expected_mime = descriptor['mime_type']
        if (not isinstance(data, bytes) or len(data) != artifact['size']
            or hashlib.sha256(data).hexdigest() != artifact['sha256']
            or artifact['mime_type'] != expected_mime):
            raise ValueError('media artifact changed')

        class Upload:
            filename = binding['destination_name']
            content_type = expected_mime
            def __init__(self): self.offset = 0
            async def read(self, size):
                chunk = data[self.offset:self.offset + size]
                self.offset += len(chunk)
                return chunk

        receipt = await self.facade.import_file(
            context, upload=Upload(), before_commit=current,
            expected_destination_revision=binding['destination_revision'],
            source_evidence=(self.source_evidence_for(identity,job) if self.source_evidence_for is not None else
                {'media_job_id':job['id'],'identity_digest':job['identity_digest'],'artifact_sha256':artifact['sha256']}),
            metadata={'operation_id': job['id'], 'item_id': 'output',
                      'generation': context.policy_generation,
                      'destination_ref': binding['destination_ref'],
                      'name': binding['destination_name'], 'collision': 'fail'},
        )
        items = receipt.get('items') if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict) or receipt.get('state') != 'complete' or not isinstance(items, list) or len(items) != 1
            or items[0].get('item_id') != 'output' or items[0].get('outcome') != 'committed'
            or not isinstance(items[0].get('resource_ref'), str)):
            raise ValueError('media Files receipt is not complete')
        result = {'operation_id': job['id'], 'item_id': 'output',
                  'resource_ref': items[0]['resource_ref'], 'sha256': artifact['sha256'],
                  'mime_type': artifact['mime_type']}
        result.update(descriptor['metadata'])
        return result


class SpriteSheetFilesScope:
    def __init__(self, *, facade, context_for, session_current):
        self.facade,self.context_for,self.session_current=facade,context_for,session_current

    async def capture(self, identity, *, source_refs, destination_ref, destination_name, parameters, rectangles=None):
        crops=identity.get('adapter_id')=='sprite-sheet-crops-v1'
        if not isinstance(source_refs,list) or len(source_refs)!=(1 if crops else 4): raise ValueError('sprite sources unavailable')
        context=await self.context_for(identity)
        if context.owner_subject_id!=identity['account_id'] or context.workspace_id!=identity['workspace_id'] or context.policy_generation!=identity['policy_generation'] or not await self.session_current(identity): raise ValueError('sprite scope changed')
        from src.openclank.sprite_sheet import inspect_frames,inspect_sheet
        inputs=[]; frames=[]
        for ref in source_refs:
            evidence,data=await self.facade.capture_sprite_frame(context,resource_ref=ref)
            inputs.append(evidence); frames.append(data)
        task=asyncio.create_task(asyncio.to_thread(inspect_sheet,frames[0],rectangles,parameters) if crops else asyncio.to_thread(inspect_frames,frames,parameters)); cancelled=False
        while True:
            try: dimensions=await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled=True
                if task.done(): dimensions=task.result(); break
        if cancelled: raise asyncio.CancelledError
        destination=await self.facade.capture_media_destination(context,resource_ref=destination_ref)
        if not await self.session_current(identity): raise ValueError('sprite session changed')
        side=dimensions[2] if crops else dimensions
        binding={'interface':'sprite-sheet-crops-v1' if crops else 'sprite-sheet-v1','ownerAccountID':identity['account_id'],'inputs':inputs,'annotations':parameters,'cellSize':side,
            'destination_ref':destination['destination_ref'],'destination_revision':destination['destination_revision'],
            'destination_name':destination_name}
        if crops:binding.update(sourceWidth=dimensions[0],sourceHeight=dimensions[1],rectangles=[dict(r) for r in rectangles])
        return binding, destination['destination_label']

    async def source(self,identity):
        context=await self.context_for(identity); inputs=identity['resource_binding']['inputs']; frames=[]
        if not await self.session_current(identity): raise ValueError('sprite session changed')
        for source in inputs:
            evidence,data=await self.facade.capture_sprite_frame(context,resource_ref=source['ref'],expected_revision=source['revision'])
            if evidence!=source: raise ValueError('sprite source changed')
            frames.append(data)
        if not await self.session_current(identity): raise ValueError('sprite session changed')
        return frames

    async def current(self,identity):
        try:
            await self.source(identity); context=await self.context_for(identity); binding=identity['resource_binding']
            destination=await self.facade.capture_media_destination(context,resource_ref=binding['destination_ref'])
            return destination['destination_revision']==binding['destination_revision'] and await self.session_current(identity)
        except Exception: return False

    async def control(self,identity):
        return (identity.get('resource_binding') or {}).get('ownerAccountID')==identity.get('account_id')


def _blender_bake_identity(identity):
    adapter=identity.get('adapter_id')
    if adapter != 'blender-material-bake-v1':
        if identity.get('operation')=='material.bake': raise ValueError('media operation changed')
        return False
    binding=identity.get('resource_binding') or {}
    if (identity.get('adapter_version')!='1' or identity.get('operation')!='material.bake'
            or identity.get('model_revision')!='fixed-material-bake-v1' or binding.get('interface')!='blender-render-v1'
            or type(binding.get('resolution')) is not int or binding['resolution'] not in (256,512,1024,2048)):
        raise ValueError('media operation changed')
    return True


class BlenderFilesScope:
    """Exact local render source and destination under existing Files authority."""
    def __init__(self, *, facade, context_for, session_current, resolve_profile):
        self.facade, self.context_for = facade, context_for
        self.session_current, self.resolve_profile = session_current, resolve_profile

    async def capture(self, identity, *, source_ref, destination_ref, destination_name):
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        async with self.resolve_profile(identity) as (profile, assert_current):
            source, data = await self.facade.capture_media_glb(context, resource_ref=source_ref, textured=_blender_bake_identity(identity))
            destination = await self.facade.capture_media_destination(context, resource_ref=destination_ref)
            await assert_current()
            if not await self.session_current(identity): raise ValueError('media session changed')
            binding = {**profile.job_binding(), 'source_ref':source['recipe_ref'],
                'source_revision':source['recipe_revision'], 'source_sha256':source['recipe_sha256'],
                'destination_ref':destination['destination_ref'], 'destination_revision':destination['destination_revision'],
                'destination_name':destination_name, 'project_ref':None, 'project_revision':None,
                **({'resolution':identity['resource_binding']['resolution']} if _blender_bake_identity(identity) else {})}
            return binding, (data, source['recipe_name'], destination['destination_label'])

    async def inspect_rig(self, identity, *, source_ref):
        """Observe weighted joints from one current Files revision, without a job."""
        from src.openclank.blender_rig_inspect import inspect_rig
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        source, data = await self.facade.capture_media_glb(context, resource_ref=source_ref)
        found = await asyncio.to_thread(inspect_rig, data)
        if found['sourceSHA256'] != source['recipe_sha256']:
            raise ValueError('rig source changed')
        again_context = await self.context_for(identity)
        if (again_context != context or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        again, _ = await self.facade.capture_media_glb(again_context, resource_ref=source_ref,
            expected_revision=source['recipe_revision'])
        if (again['recipe_ref'] != source['recipe_ref'] or again['recipe_sha256'] != source['recipe_sha256']
                or await self.context_for(identity) != context or not await self.session_current(identity)):
            raise ValueError('rig source changed')
        return {'kind':'rig', 'source_sha256':found['sourceSHA256'], 'joints':found['joints']}

    async def source(self, identity):
        if not await self.current(identity): raise ValueError('media scope changed')
        binding = identity['resource_binding']
        context = await self.context_for(identity)
        evidence, data = await self.facade.capture_media_glb(context, resource_ref=binding['source_ref'],
            expected_revision=binding['source_revision'], textured=_blender_bake_identity(identity))
        if evidence['recipe_sha256'] != binding['source_sha256']: raise ValueError('media source changed')
        return data

    async def control(self, identity):
        # Coordinator already verifies the current immutable account and exact
        # job identity; local receipt reads need no original source or binary.
        binding = identity.get('resource_binding') or {}
        return (binding.get('interface') == 'blender-render-v1'
                and binding.get('ownerAccountID') == identity.get('account_id'))

    async def current(self, identity):
        try:
            binding = identity['resource_binding']
            context = await self.context_for(identity)
            if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                    or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
                return False
            async with self.resolve_profile(identity) as (profile, assert_current):
                actual = profile.job_binding()
                if any(actual.get(key) != binding[key] for key in ('connectionID','connectionRevision','interface','ownerAccountID')):
                    return False
                source, _ = await self.facade.capture_media_glb(context, resource_ref=binding['source_ref'],
                    expected_revision=binding['source_revision'], textured=_blender_bake_identity(identity))
                destination = await self.facade.capture_media_destination(context, resource_ref=binding['destination_ref'])
                await assert_current()
                return (source['recipe_sha256'] == binding['source_sha256']
                        and destination['destination_revision'] == binding['destination_revision']
                        and await self.session_current(identity))
        except Exception:
            return False


class SprixenFilesScope:
    """Destination-only source-free sprite job on existing Files authority."""
    def __init__(self, *, facade, context_for, session_current, resolve_profile):
        self.facade, self.context_for = facade, context_for
        self.session_current, self.resolve_profile = session_current, resolve_profile

    async def capture(self, identity, *, destination_ref, destination_name):
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        async with self.resolve_profile(identity) as (profile, check, _credential):
            destination = await self.facade.capture_media_destination(context, resource_ref=destination_ref)
            await check()
            if not await self.session_current(identity): raise ValueError('media session changed')
            return {**profile.job_binding(), 'destination_ref':destination['destination_ref'],
                    'destination_revision':destination['destination_revision'], 'destination_name':destination_name,
                    'project_ref':None, 'project_revision':None}, destination['destination_label']

    async def control(self, identity):
        # Exact current job/claim and known provider ID are verified by the
        # adapter immediately before a read; this preclaim gate grants nothing.
        binding = identity.get('resource_binding') or {}
        return (binding.get('interface') == 'sprixen-sprite-v1'
                and binding.get('ownerAccountID') == identity.get('account_id'))

    async def current(self, identity):
        try:
            context = await self.context_for(identity); binding = identity['resource_binding']
            if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                    or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
                return False
            async with self.resolve_profile(identity) as (profile, check, _credential):
                if any(profile.job_binding().get(k) != binding.get(k) for k in profile.job_binding()): return False
                destination = await self.facade.capture_media_destination(context, resource_ref=binding['destination_ref'])
                await check()
                return destination['destination_revision'] == binding['destination_revision'] and await self.session_current(identity)
        except Exception:
            return False


class MeshyFilesScope(SprixenFilesScope):
    """Source-free Meshy generation uses the same destination authority."""
    async def control(self, identity):
        binding = identity.get('resource_binding') or {}
        return (binding.get('interface') == 'meshy-preview-v1'
                and binding.get('ownerAccountID') == identity.get('account_id'))


class TripoFilesScope(MeshyFilesScope):
    async def control(self, identity):
        binding=identity.get("resource_binding") or {}
        return binding.get("interface")=="tripo-text3d-v1" and binding.get("ownerAccountID")==identity.get("account_id")


class FalFilesScope(SprixenFilesScope):
    async def control(self, identity):
        binding=identity.get("resource_binding") or {}
        return binding.get("interface")=="fal-flux-schnell-v1" and binding.get("ownerAccountID")==identity.get("account_id")


class VoidlessFilesScope(SprixenFilesScope):
    """Destination custody for one configured Voidless image profile."""
    async def control(self, identity):
        binding=identity.get('resource_binding') or {}
        return binding.get('interface')=='voidless-image-v1' and binding.get('ownerAccountID')==identity.get('account_id')


class ImageToMeshFilesScope:
    """Bind an exact image and destination to the existing Fal transport."""
    def __init__(self, *, facade, context_for, session_current, resolve_profile):
        self.facade, self.context_for = facade, context_for
        self.session_current, self.resolve_profile = session_current, resolve_profile

    async def _context(self, identity):
        context = await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('media scope changed')
        return context

    async def capture(self, identity, *, source_ref, destination_ref, destination_name):
        context = await self._context(identity)
        async with self.resolve_profile(identity) as (profile, check, _credential):
            source, _data = await self.facade.capture_media_image(context, resource_ref=source_ref)
            destination = await self.facade.capture_media_destination(context, resource_ref=destination_ref)
            await check()
            if not await self.session_current(identity): raise ValueError('media session changed')
            binding = {**profile.job_binding(), 'destination_ref':destination['destination_ref'],
                'destination_revision':destination['destination_revision'], 'destination_name':destination_name,
                'project_ref':None, 'project_revision':None, 'image_source':source}
            return binding, {'source':source, 'folder':destination['destination_label']}

    async def source(self, identity):
        context = await self._context(identity)
        binding = identity['resource_binding']; expected = binding['image_source']
        evidence, data = await self.facade.capture_media_image(context, resource_ref=expected['ref'],
            expected_revision=expected['revision'])
        if (evidence != expected or identity['input_hashes'] != [evidence['sha256']]
                or not await self.session_current(identity)):
            raise ValueError('media source changed')
        return evidence, data

    async def control(self, identity):
        # A paid known-ID job remains controllable even if its original file changes.
        binding = identity.get('resource_binding') or {}
        return (binding.get('interface') == 'fal-flux-schnell-v1'
                and binding.get('ownerAccountID') == identity.get('account_id'))

    async def current(self, identity):
        try:
            context = await self._context(identity); binding = identity['resource_binding']
            async with self.resolve_profile(identity) as (profile, check, _credential):
                actual = profile.job_binding()
                if any(actual.get(k) != binding.get(k) for k in actual): return False
                expected = binding['image_source']
                source, _data = await self.facade.capture_media_image(context, resource_ref=expected['ref'],
                    expected_revision=expected['revision'])
                destination = await self.facade.capture_media_destination(context, resource_ref=binding['destination_ref'])
                await check()
                return (source == expected and identity['input_hashes'] == [source['sha256']]
                        and destination['destination_revision'] == binding['destination_revision']
                        and await self.session_current(identity))
        except Exception:
            return False


class MaterialPackFilesScope:
    """Exact role-ordered original maps and destination under Files authority."""
    _roles = ('baseColor', 'roughness', 'metallic', 'normal')

    def __init__(self, *, facade, context_for, session_current):
        self.facade, self.context_for, self.session_current = facade, context_for, session_current

    async def _context(self, identity):
        context=await self.context_for(identity)
        if (context.owner_subject_id != identity['account_id'] or context.workspace_id != identity['workspace_id']
                or context.policy_generation != identity['policy_generation'] or not await self.session_current(identity)):
            raise ValueError('material scope changed')
        return context

    @classmethod
    def _ordered(cls, sources):
        if type(sources) is not list or len(sources) not in (3,4): raise ValueError('material sources unavailable')
        by_role={}
        for item in sources:
            if (type(item) is not dict or set(item)!={'role','resource_ref'} or item['role'] not in cls._roles
                    or item['role'] in by_role or type(item['resource_ref']) is not str or not item['resource_ref']):
                raise ValueError('material sources unavailable')
            by_role[item['role']]=item['resource_ref']
        if any(role not in by_role for role in cls._roles[:3]) or len(set(by_role.values())) != len(by_role):
            raise ValueError('material sources unavailable')
        return [(role,by_role[role]) for role in cls._roles if role in by_role]

    @staticmethod
    async def _manifest(maps):
        from src.openclank.material_bundle import inspect_maps, manifest_sha256
        task=asyncio.create_task(asyncio.to_thread(lambda: manifest_sha256(inspect_maps(maps))))
        cancelled=False
        while True:
            try:
                value=await asyncio.shield(task); break
            except asyncio.CancelledError:
                cancelled=True
                if task.done():
                    try: task.result()
                    except Exception: pass
                    raise
            except Exception:
                if cancelled: raise asyncio.CancelledError from None
                raise
        if cancelled: raise asyncio.CancelledError
        return value

    async def capture(self, identity, *, sources, destination_ref, destination_name):
        ordered=self._ordered(sources); context=await self._context(identity)
        inputs=[]; maps=[]
        for role,ref in ordered:
            evidence,data=await self.facade.capture_media_image(context,resource_ref=ref)
            inputs.append({'role':role,**evidence}); maps.append((role,data))
        if len({item['resourceID'] for item in inputs}) != len(inputs): raise ValueError('duplicate material source')
        manifest=await self._manifest(maps)
        destination=await self.facade.capture_media_destination(context,resource_ref=destination_ref)
        if not await self.session_current(identity): raise ValueError('material session changed')
        binding={'interface':'material-pack-v1','ownerAccountID':identity['account_id'], 'inputs':inputs,
                 'manifest_sha256':manifest,'destination_ref':destination['destination_ref'],
                 'destination_revision':destination['destination_revision'],'destination_name':destination_name}
        return binding, {'sources':inputs,'folder':destination['destination_label']}

    async def source(self, identity):
        context=await self._context(identity); binding=identity['resource_binding']; inputs=binding['inputs']
        ordered=self._ordered([{'role':item['role'],'resource_ref':item['ref']} for item in inputs])
        if [item['role'] for item in inputs] != [role for role,_ in ordered]: raise ValueError('material source order changed')
        if len({item['resourceID'] for item in inputs}) != len(inputs): raise ValueError('duplicate material source')
        maps=[]
        for item in inputs:
            evidence,data=await self.facade.capture_media_image(context,resource_ref=item['ref'],expected_revision=item['revision'])
            if {'role':item['role'],**evidence} != item: raise ValueError('material source changed')
            maps.append((item['role'],data))
        if (identity['input_hashes'] != [item['sha256'] for item in inputs]
                or await self._manifest(maps) != binding['manifest_sha256'] or not await self.session_current(identity)):
            raise ValueError('material source changed')
        return maps

    async def current(self, identity):
        try:
            await self.source(identity); context=await self._context(identity); binding=identity['resource_binding']
            destination=await self.facade.capture_media_destination(context,resource_ref=binding['destination_ref'])
            return destination['destination_revision']==binding['destination_revision'] and await self.session_current(identity)
        except Exception: return False

    async def control(self, identity):
        binding=identity.get('resource_binding') or {}
        return binding.get('interface')=='material-pack-v1' and binding.get('ownerAccountID')==identity.get('account_id')
