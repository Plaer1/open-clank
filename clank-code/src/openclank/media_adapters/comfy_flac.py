"""Bounded witnesses for the separately reviewed ComfyUI FLAC profile."""
from .comfyui import inspect_recipe, render_recipe

MAX_FLAC = 64 * 1024 * 1024
MAX_METADATA = 1024 * 1024
PREFIX = 'openclank_audio'
OUTPUT_NODE = 'openclank_audio_output'


def render_flac_recipe(raw, parameters):
    recipe, _ = inspect_recipe(raw)
    outputs = [(key, node) for key, node in recipe['workflow'].items()
               if node['class_type'] == 'SaveAudioAdvanced']
    if len(outputs) != 1:
        raise ValueError('one SaveAudioAdvanced node is required')
    key, node = outputs[0]
    if (key != OUTPUT_NODE or node['inputs'].get('filename_prefix') != PREFIX
        or node['inputs'].get('format') != {'format': 'flac'}
        or any(slot['node'] == key and slot['input'] in {'filename_prefix', 'format'}
               for slot in recipe['slots'].values())):
        raise ValueError('the recipe must explicitly fix the FLAC output profile')
    return render_recipe(raw, parameters)


def inspect_flac(data):
    """Return declared STREAMINFO values; audio frames are not decoded or verified."""
    if not isinstance(data, bytes) or not 42 < len(data) <= MAX_FLAC or data[:4] != b'fLaC':
        raise ValueError('unsupported FLAC')
    pos = 4
    first = True
    info = None
    while True:
        if pos + 4 > len(data) or pos + 4 > MAX_METADATA:
            raise ValueError('invalid FLAC metadata')
        flags = data[pos]
        kind = flags & 0x7f
        size = int.from_bytes(data[pos + 1:pos + 4], 'big')
        pos += 4
        end = pos + size
        if kind > 6 or end > len(data) or end > MAX_METADATA:
            raise ValueError('invalid FLAC metadata')
        if first:
            if kind != 0 or size != 34:
                raise ValueError('missing FLAC STREAMINFO')
            block = data[pos:end]
            minimum = int.from_bytes(block[:2], 'big')
            maximum = int.from_bytes(block[2:4], 'big')
            packed = int.from_bytes(block[10:18], 'big')
            rate = packed >> 44
            channels = ((packed >> 41) & 7) + 1
            bits = ((packed >> 36) & 31) + 1
            samples = packed & ((1 << 36) - 1)
            if (minimum < 16 or maximum < minimum or not 8000 <= rate <= 192000
                or channels not in (1, 2) or bits not in (16, 24)
                or not 0 < samples <= rate * 600):
                raise ValueError('unsupported FLAC STREAMINFO')
            info = {'sampleRate': rate, 'channels': channels, 'bitsPerSample': bits,
                    'declaredSamples': samples, 'payloadVerified': False}
        elif kind == 0:
            raise ValueError('duplicate FLAC STREAMINFO')
        first = False
        pos = end
        if flags & 0x80:
            if pos >= len(data):
                raise ValueError('missing FLAC payload')
            return info


from hashlib import sha256
from dataclasses import dataclass
from uuid import UUID
from src.openclank.artifacts import media_artifact_owner
from src.openclank.media_adapters.base import Artifact, NotDispatched, Submission, CollectionProfile
from .comfyui import ComfyUIAdapter


@dataclass(frozen=True)
class FlacArtifact(Artifact):
    audio: dict


class ComfyFlacAdapter(ComfyUIAdapter):
    collection_profile = CollectionProfile(owned_fetch=True, artifact_type=FlacArtifact)
    adapter_id = 'comfyui-flac-v1'

    def _read_artifact(self, owner, artifact_id, expected_size):
        chunks, size = [], 0
        for chunk in self.artifact_store.read_chunks(owner=owner, artifact_id=artifact_id):
            size += len(chunk)
            if size > expected_size or size > MAX_FLAC:
                raise ValueError('retained FLAC exceeds bounds')
            chunks.append(chunk)
        if size != expected_size:
            raise ValueError('retained FLAC changed')
        return b''.join(chunks)

    async def submit(self, request, job, admission):
        binding = job['identity']['resource_binding']
        try:
            evidence, raw = await self.capture_recipe(job['identity'])
            if any(evidence.get(k) != binding[k] for k in ('recipe_ref', 'recipe_revision', 'recipe_sha256')):
                raise ValueError('media recipe changed')
            graph, _ = render_flac_recipe(raw, request.get('parameters'))
        except Exception as exc:
            raise NotDispatched('FLAC recipe unavailable or changed') from exc
        task = str(UUID(job['id']))
        result = await self._json(job, admission, 'POST', '/prompt', body={'prompt': graph, 'prompt_id': task})
        if result.get('prompt_id') != task:
            raise ValueError('media task identity mismatch')
        return Submission('queued', task)

    async def collect(self, job, admission):
        task = str(UUID(job['remote_task_id']))
        result = await self._json(job, admission, 'GET', '/api/jobs/' + task)
        output = result.get('preview_output')
        if (result.get('id') != task or result.get('status') != 'completed'
            or result.get('outputs_count') != 1 or not isinstance(output, dict)
            or output.get('mediaType') != 'audio' or output.get('nodeId') != OUTPUT_NODE):
            raise ValueError('FLAC output unavailable')
        locator = {key: output.get(key) for key in ('filename', 'subfolder', 'type')}
        name = locator['filename']
        if (not isinstance(name, str) or len(name.encode('utf-8')) > 120
            or not name.startswith(PREFIX + '_') or not name.lower().endswith('.flac')
            or any(c in name for c in '/\\') or locator['subfolder'] != '' or locator['type'] != 'output'):
            raise ValueError('unsupported FLAC output')
        data = await self._request(job, admission, 'GET', '/view', params=locator, limit=MAX_FLAC)
        audio = inspect_flac(data)
        digest = sha256(data).hexdigest()
        owner = media_artifact_owner(job['account_id'])
        def retain():
            row = self.artifact_store.put(owner=owner, chunks=[data], media_type='audio/flac')
            if sha256(self._read_artifact(owner, row.id, len(data))).hexdigest() != digest:
                raise ValueError('retained FLAC changed')
            row = self.artifact_store.acknowledge(owner=owner, artifact_id=row.id)
            if row.content_sha256 != digest or row.size_bytes != len(data):
                raise ValueError('retained FLAC changed')
            return FlacArtifact(row.id, digest, 'audio/flac', len(data), audio)
        async with self.resolve_profile(job['identity']) as (profile, assert_current):
            self._profile(job, profile)
            await assert_current()
            admission()
            return await self._owned_thread(retain, retain_result_after_cancel=True)

    async def fetch_artifact(self, identity, job, current):
        if not await current():
            raise ValueError('media publication authority changed')
        owner = media_artifact_owner(identity['account_id'])
        def read():
            row = self.artifact_store.get(owner=owner, artifact_id=job['artifact']['artifact_id'])
            if (row.content_sha256 != job['artifact']['sha256'] or row.size_bytes != job['artifact']['size']
                or row.media_type != 'audio/flac' or row.state != 'acknowledged' or row.size_bytes > MAX_FLAC):
                raise ValueError('retained FLAC changed')
            data = self._read_artifact(owner, row.id, row.size_bytes)
            inspect_flac(data)
            return data
        data = await self._owned_thread(read)
        if not await current():
            raise ValueError('media publication authority changed')
        return data
