"""One configured Comfy workflow producing a retained material archive."""
import hashlib
import inspect
import uuid

from src.openclank.artifacts import media_artifact_owner
from src.openclank.material_bundle import pack_generated, validate_bundle
from src.openclank.media_adapters.base import Artifact, CollectionProfile, NotDispatched, Submission
from src.openclank.media_adapters.comfyui import ComfyUIAdapter, inspect_material_recipe, parse_material_recipe

MAX_MAP = 4 * 1024 * 1024
MAX_ARCHIVE = 24 * 1024 * 1024
ROLES = ('baseColor', 'roughness', 'metallic')


class ComfyUIMaterialAdapter(ComfyUIAdapter):
    adapter_id = 'comfyui-material-v1'
    collection_profile = CollectionProfile(owned_fetch=True, artifact_type=Artifact)

    async def _recipe(self, job, parameters=None):
        binding = job['identity']['resource_binding']
        evidence, raw = await self.capture_recipe(job['identity'])
        if any(evidence.get(k) != binding[k] for k in ('recipe_ref','recipe_revision','recipe_sha256','recipe_resource_id')):
            raise ValueError('material workflow changed')
        graph, summary = (inspect_material_recipe(raw) if parameters is None
                          else parse_material_recipe(raw, parameters))
        if (summary['resolution'] != binding['material_resolution']
            or summary['outputs'] != binding['material_outputs']):
            raise ValueError('material workflow changed')
        return graph

    @staticmethod
    def _derivation(job):
        binding = job['identity']['resource_binding']
        return {'job_id':job['id'], 'provider_job_id':str(uuid.UUID(job['remote_task_id'])),
                'provider_id':'comfyui', 'connection_id':binding['connectionID'],
                'connection_revision':binding['connectionRevision'],
                'workflow':{'resource_id':binding['recipe_resource_id'], 'revision':binding['recipe_revision'],
                            'sha256':binding['recipe_sha256']},
                'resolution':binding['material_resolution'], 'outputs':binding['material_outputs']}

    async def submit(self, request, job, admission):
        try:
            graph = await self._recipe(job, request.get('parameters'))
        except Exception as exc:
            raise NotDispatched('material workflow unavailable or changed') from exc
        task = str(uuid.UUID(job['id']))
        answer = await self._json(job, admission, 'POST', '/prompt', body={'prompt':graph,'prompt_id':task})
        if answer.get('prompt_id') != task: raise ValueError('media task identity mismatch')
        return Submission('queued', task)

    async def collect(self, job, admission):
        binding = job['identity']['resource_binding']
        # Recheck the original Files recipe and its selectors before provider reads.
        await self._recipe(job)
        task = str(uuid.UUID(job['remote_task_id']))
        answer = await self._json(job, admission, 'GET', '/api/jobs/' + task)
        if answer.get('id') != task or answer.get('status') != 'completed' or not isinstance(answer.get('outputs'), dict):
            raise ValueError('material output unavailable')
        selected = []
        for role in ROLES:
            spec = binding['material_outputs'][role]
            node = answer['outputs'].get(spec['node_id'])
            images = node.get('images') if isinstance(node, dict) else None
            if not isinstance(images, list) or spec['index'] >= len(images) or not isinstance(images[spec['index']], dict):
                raise ValueError('material output unavailable')
            image = images[spec['index']]
            locator = {key:image.get(key) for key in ('filename','subfolder','type')}
            if (any(not isinstance(v,str) or len(v.encode('utf-8')) > 120 for v in locator.values())
                or locator['type'] != 'output' or locator['subfolder'] != '' or not locator['filename']
                or any(ord(c) < 32 or ord(c) == 127 for c in locator['filename'])
                or '/' in locator['filename'] or '\\' in locator['filename']
                or not locator['filename'].lower().endswith('.png')):
                raise ValueError('unsupported material output')
            if locator in selected: raise ValueError('duplicate material output')
            selected.append(locator)
        maps = []
        for role, locator in zip(ROLES, selected):
            data = await self._request(job, admission, 'GET', '/view', params=locator, limit=MAX_MAP)
            maps.append((role, data))
        derivation = self._derivation(job)
        def compose():
            data, manifest = pack_generated(maps, derivation)
            if not 0 < len(data) <= MAX_ARCHIVE: raise ValueError('material archive exceeds bounds')
            validate_bundle(data, expected_manifest_sha256=manifest, expected_generated=derivation)
            return data
        data = await self._owned_thread(compose)
        digest = hashlib.sha256(data).hexdigest()
        owner = media_artifact_owner(job['account_id'])
        def retain():
            row = self.artifact_store.put(owner=owner, chunks=[data], media_type='application/zip')
            retained = self._read_artifact(owner, row.id, len(data))
            if hashlib.sha256(retained).hexdigest() != digest: raise ValueError('retained material changed')
            validate_bundle(retained, expected_generated=derivation)
            row = self.artifact_store.acknowledge(owner=owner, artifact_id=row.id)
            if row.content_sha256 != digest or row.size_bytes != len(data): raise ValueError('retained material changed')
            return Artifact(row.id, digest, 'application/zip', len(data))
        async with self.resolve_profile(job['identity']) as (profile, assert_current):
            self._profile(job, profile)
            await assert_current()
            await self._recipe(job)
            if self.before_dispatch is not None and await self.before_dispatch() is not True: raise ValueError('media authority changed')
            allowed = admission()
            if inspect.isawaitable(allowed): allowed = await allowed
            if allowed is False: raise ValueError('media admission expired')
            return await self._owned_thread(retain, retain_result_after_cancel=True)

    async def fetch_artifact(self, identity, job, current):
        if not await current(): raise ValueError('media publication authority changed')
        owner = media_artifact_owner(identity['account_id'])
        def read():
            row = self.artifact_store.get(owner=owner, artifact_id=job['artifact']['artifact_id'])
            if (row.content_sha256 != job['artifact']['sha256'] or row.size_bytes != job['artifact']['size']
                or row.media_type != 'application/zip' or row.state != 'acknowledged' or row.size_bytes > MAX_ARCHIVE):
                raise ValueError('retained material changed')
            data = self._read_artifact(owner, row.id, row.size_bytes)
            if hashlib.sha256(data).hexdigest() != row.content_sha256: raise ValueError('retained material changed')
            validate_bundle(data, expected_generated=self._derivation(job))
            return data
        data = await self._owned_thread(read)
        if not await current(): raise ValueError('media publication authority changed')
        return data
