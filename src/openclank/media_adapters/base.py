"""Private, injected media adapter contract; no provider is registered here."""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol

class NotDispatched(ValueError):
    """A local preflight failed before any provider request was attempted."""

@dataclass(frozen=True)
class Submission:
    state: str
    task_id: str | None = None

@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    sha256: str
    mime_type: str
    size: int

class MediaAdapter(Protocol):
    adapter_id: str
    version: str
    route_kind: str
    supports_lookup: bool
    supports_cancel: bool
    # Adapter must call admission immediately before dispatch; after the
    # bounded owner admission expires the callback refuses new side effects.
    async def submit(self, request: Mapping[str, Any], job: Mapping[str, Any], admission: Callable[[], None]) -> Submission: ...
    async def status(self, job: Mapping[str, Any], admission: Callable[[], None]) -> Submission: ...
    async def cancel(self, job: Mapping[str, Any], admission: Callable[[], None]) -> str: ...
    async def collect(self, job: Mapping[str, Any], admission: Callable[[], None]) -> Artifact: ...


def describe_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a retained J0 artifact into provider-neutral output facts."""
    import json
    import re
    if not isinstance(value, Mapping):
        raise ValueError('media artifact unavailable')
    media = {
        'image/png': ('image', '.png', 32 * 1024 * 1024),
        'application/pdf': ('document', '.pdf', 32 * 1024 * 1024),
        'audio/flac': ('audio', '.flac', 64 * 1024 * 1024),
        'video/webm': ('video', '.webm', 64 * 1024 * 1024),
        'model/gltf-binary': ('mesh', '.glb', 64 * 1024 * 1024),
        'application/zip': ('archive', '.zip', 24 * 1024 * 1024),
    }
    mime = value.get('mime_type')
    if mime not in media or not isinstance(value.get('artifact_id'), str) or not value['artifact_id']:
        raise ValueError('media artifact unavailable')
    kind, extension, maximum = media[mime]
    if (not isinstance(value.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])
        or type(value.get('size')) is not int or not 0 < value['size'] <= maximum):
        raise ValueError('media artifact unavailable')
    metadata = {}
    for key in ('animation', 'audio', 'layout'):
        if key in value:
            if not isinstance(value[key], dict):
                raise ValueError('media artifact unavailable')
            try:
                raw = json.dumps(value[key], ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                raise ValueError('media artifact unavailable') from exc
            if len(raw) > 65536:
                raise ValueError('media artifact unavailable')
            metadata[key] = dict(value[key])
    return {'kind': kind, 'mime_type': mime, 'extension': extension, 'maximum': maximum,
            'sha256': value['sha256'], 'size_bytes': value['size'], 'metadata': metadata}



@dataclass(frozen=True)
class MediaOperationFacts:
    """Compiled descriptive facts; never an execution or custody grant."""
    operation: str
    output_kind: str
    mime_type: str
    artifact_max_bytes: int
    metadata_kind: str
    control_read: bool
    owned_fetch: bool
    foreground_kind: str
    projection_kind: str
    cancel_available: bool
    follow_up_operations: tuple[str, ...] = ()
    model_labels: tuple[tuple[str, str], ...] = ()
    # Declared facts read by the shared J0 reservation/finish code in src/bg_jobs.py (reserve_media, _media_task_id_ok): '', () mean "not declared".
    route_kind: str = ''  # 'local' | 'remote'
    binding_interface: str = ''  # resource_binding.interface this adapter accepts
    binding_required: bool = False  # identity must carry that binding
    binding_extra_fields: tuple[str, ...] = ()  # keys required beyond the scope's common set
    fixed_model_revision: str = ''  # identity model_revision, when pinned
    task_id_pattern: str = ''  # regex of the remote task id, when constrained


_MIB = 1024 * 1024
# This immutable catalog describes the existing v1 J0 record contract. Dynamic
# validation and the adapter's CollectionProfile remain their own authorities.
_MEDIA_OPERATION_FACTS = (
    ('comfyui', MediaOperationFacts('image.generate', 'image', 'image/png', 256*_MIB, 'none', False, False, 'model', 'ordinary', True)),
    ('comfyui-flac-v1', MediaOperationFacts('audio.generate', 'audio', 'audio/flac', 256*_MIB, 'audio', False, True, 'model', 'ordinary', True)),
    ('comfyui-material-v1', MediaOperationFacts('material.generate', 'archive', 'application/zip', 24*_MIB, 'none', False, True, 'model', 'ordinary', True)),
    ('sprixen-sprite-v1', MediaOperationFacts('image.generate', 'image', 'image/png', 256*_MIB, 'none', True, False, 'model', 'ordinary', False)),
    ('sprixen-animation-v1', MediaOperationFacts('animation.generate', 'image', 'image/png', 256*_MIB, 'animation', True, False, 'model', 'ordinary', False)),
    ('meshy-preview-v1', MediaOperationFacts('mesh.generate', 'mesh', 'model/gltf-binary', 256*_MIB, 'none', True, True, 'model', 'ordinary', False, ('mesh.refine',), (('meshy-7.1', 'Meshy 7.1'),))),
    ('meshy-refine-v1', MediaOperationFacts('mesh.refine', 'mesh', 'model/gltf-binary', 256*_MIB, 'none', True, True, 'model', 'ordinary', False, (), (('meshy-7.1', 'Meshy 7.1'),))),
    ('meshy-rig-v1', MediaOperationFacts('mesh.rig', 'mesh', 'model/gltf-binary', 64*_MIB, 'none', True, True, 'model', 'ordinary', False)),
    ('tripo-text3d-v1', MediaOperationFacts('mesh.generate', 'mesh', 'model/gltf-binary', 256*_MIB, 'none', True, True, 'model', 'ordinary', False, (), (('v3.1-20260211', 'Tripo v3.1'),))),
    ('fal-flux-schnell-v1', MediaOperationFacts('image.generate', 'image', 'image/png', 256*_MIB, 'none', True, True, 'model', 'ordinary', False, (), (('fal-ai/flux/schnell', 'FLUX.1 schnell'),))),
    ('voidless-image-v1', MediaOperationFacts('image.generate', 'image', 'image/png', 32*_MIB, 'none', True, True, 'model', 'ordinary', False, (), (('flux2-klein', 'Voidless FLUX.2 Klein'),))),
    ('fal-hunyuan-image3d-v1', MediaOperationFacts('mesh.generate', 'mesh', 'model/gltf-binary', 32*_MIB, 'none', True, True, 'model', 'ordinary', False, (), (('fal-ai/hunyuan3d/v2', 'Hunyuan3D v2'),))),
    ('trellis-image3d-v1', MediaOperationFacts('mesh.generate', 'mesh', 'model/gltf-binary', 64*_MIB, 'none', False, False, 'model', 'ordinary', False)),
    ('hunyuan3d-v2-shape-v1', MediaOperationFacts('mesh.generate', 'mesh', 'model/gltf-binary', 64*_MIB, 'none', False, False, 'model', 'ordinary', False)),
    ('trim-webm-v1', MediaOperationFacts('video.trim', 'video', 'video/webm', 64*_MIB, 'none', False, False, 'cpu', 'trim', False)),
    ('screen-record-v1', MediaOperationFacts('screen.record', 'video', 'video/webm', 64*_MIB, 'none', False, False, 'cpu', 'recording', False)),
    ('latex-pdflatex-v1', MediaOperationFacts('document.latex.compile', 'document', 'application/pdf', 32*_MIB, 'none', False, False, 'cpu', 'ordinary', False)),
    ('blender-glb-png-v1', MediaOperationFacts('image.render', 'image', 'image/png', 256*_MIB, 'none', False, False, 'cpu', 'ordinary', False)),
    ('blender-glb-four-heading-v1', MediaOperationFacts('image.render', 'image', 'image/png', 256*_MIB, 'four_heading', False, False, 'cpu', 'ordinary', False)),
    ('blender-glb-rig-clip-v1', MediaOperationFacts('image.render', 'image', 'image/png', 256*_MIB, 'rig_clip', False, False, 'cpu', 'ordinary', False)),
    ('material-pack-v1', MediaOperationFacts('material.pack', 'archive', 'application/zip', 24*_MIB, 'none', False, False, 'cpu', 'ordinary', False)),
    ('blender-material-bake-v1', MediaOperationFacts('material.bake', 'archive', 'application/zip', 24*_MIB, 'none', False, False, 'cpu', 'ordinary', False)),
    ('sprite-sheet-v1', MediaOperationFacts('image.pack', 'image', 'image/png', 256*_MIB, 'sprite_sheet', False, False, 'cpu', 'ordinary', False)),
    ('sprite-sheet-crops-v1', MediaOperationFacts('image.pack-crops', 'image', 'image/png', 256*_MIB, 'sprite_crops', False, False, 'cpu', 'ordinary', False)),
)

_RA = 'remote_account'
# B4-P1: declared facts per adapter; values mirror what shared vendor-name branches implied (see B4-P1 note).
_DECLARED = {
    'comfyui': dict(route_kind='remote', binding_interface='comfyui-workflow-v1'),
    'comfyui-flac-v1': dict(route_kind='remote', binding_interface='comfyui-workflow-v1'),
    'comfyui-material-v1': dict(route_kind='remote', binding_interface='comfyui-workflow-v1', binding_extra_fields=('recipe_resource_id', 'material_resolution', 'material_outputs')),
    'sprixen-sprite-v1': dict(route_kind='remote', binding_interface='sprixen-sprite-v1'),
    'sprixen-animation-v1': dict(route_kind='remote', binding_interface='sprixen-sprite-v1', binding_extra_fields=('projectID', 'spriteID', 'sourceSHA256', 'animationParameters'), fixed_model_revision='standard', task_id_pattern=r'anim_[A-Za-z0-9_-]{1,124}'),
    'meshy-preview-v1': dict(route_kind='remote', binding_interface='meshy-preview-v1'),
    'meshy-refine-v1': dict(route_kind='remote', binding_interface='meshy-preview-v1', binding_extra_fields=('sourceJobID', 'sourceRevision', 'sourceTaskID', 'sourceArtifactID', 'sourceSHA256')),
    'meshy-rig-v1': dict(route_kind='remote', binding_interface='meshy-preview-v1', binding_required=True, binding_extra_fields=('source_ref', 'source_revision', 'source_sha256'), fixed_model_revision='meshy-rig-v1'),
    'tripo-text3d-v1': dict(route_kind='remote', binding_interface='tripo-text3d-v1'),
    'fal-flux-schnell-v1': dict(route_kind='remote', binding_interface='fal-flux-schnell-v1'),
    'voidless-image-v1': dict(route_kind='remote', binding_interface='voidless-image-v1', binding_required=True, fixed_model_revision='flux2-klein'),
    'fal-hunyuan-image3d-v1': dict(route_kind='remote', binding_interface='fal-flux-schnell-v1', binding_required=True, binding_extra_fields=('image_source',), fixed_model_revision='fal-ai/hunyuan3d/v2'),
    'trellis-image3d-v1': dict(route_kind='local', binding_interface='trellis-image3d-v1', binding_required=True, fixed_model_revision='trellis-image-glb-v1'),
    'hunyuan3d-v2-shape-v1': dict(route_kind='local', binding_interface='hunyuan3d-v2-shape-v1', binding_required=True, fixed_model_revision='hunyuan3d-v2-image-glb-v1'),
    'trim-webm-v1': dict(route_kind='local', binding_interface='trim-webm-v1', fixed_model_revision='fixed-v1'),
    'screen-record-v1': dict(route_kind='local', binding_interface='screen-record-v1', fixed_model_revision='fixed-v1'),
    'latex-pdflatex-v1': dict(route_kind='local', binding_interface='latex-pdflatex-v1', fixed_model_revision='fixed-v1'),
    'blender-glb-png-v1': dict(route_kind='local', binding_interface='blender-render-v1'),
    'blender-glb-four-heading-v1': dict(route_kind='local', binding_interface='blender-render-v1'),
    'blender-glb-rig-clip-v1': dict(route_kind='local', binding_interface='blender-render-v1', binding_extra_fields=('rig',)),
    'material-pack-v1': dict(route_kind='local', binding_interface='material-pack-v1', binding_required=True, fixed_model_revision='fixed-v1'),
    'blender-material-bake-v1': dict(route_kind='local', binding_interface='blender-render-v1', binding_required=True, binding_extra_fields=('resolution',)),
    'sprite-sheet-v1': dict(route_kind='local', binding_interface='sprite-sheet-v1', fixed_model_revision='fixed-v1'),
    'sprite-sheet-crops-v1': dict(route_kind='local', binding_interface='sprite-sheet-crops-v1', fixed_model_revision='fixed-v1'),
}
assert set(_DECLARED) == {name for name, _ in _MEDIA_OPERATION_FACTS}, 'media facts declaration drift'
_MEDIA_OPERATION_FACTS = tuple((name, replace(facts, **_DECLARED[name])) for name, facts in _MEDIA_OPERATION_FACTS)


def media_foreground_adapters(kind: str) -> frozenset[str]:
    """Compiled v1 adapters admitted to the existing foreground owner."""
    if kind not in ('model', 'cpu'):
        raise ValueError('invalid media foreground kind')
    return frozenset(adapter for adapter, facts in _MEDIA_OPERATION_FACTS if facts.foreground_kind == kind)


def media_operation_facts(adapter_id: str, adapter_version: str) -> MediaOperationFacts | None:
    """Return closed first-party facts, or None for unknown/legacy profiles."""
    if type(adapter_id) is not str or adapter_version != '1' or type(adapter_version) is not str:
        return None
    return next((facts for name, facts in _MEDIA_OPERATION_FACTS if name == adapter_id), None)


# No declaration: every fact empty, so a branch that needs a declared interface/operation refuses exactly as the snapshot's literal checks did.
NO_DECLARATION = MediaOperationFacts('', '', '', 0, 'none', False, False, '', '', False)


def media_declaration_for_id(adapter_id: str) -> MediaOperationFacts | None:
    """The catalog declaration of a known adapter id, any version: for validating a NEW identity at reservation, where every
    branch states its own version/operation check. Stored records are classified by `media_operation_facts(id, version)` only."""
    if type(adapter_id) is not str:
        return None
    return next((facts for name, facts in _MEDIA_OPERATION_FACTS if name == adapter_id), None)


@dataclass(frozen=True)
class CollectionProfile:
    """Trusted adapter-owned custody and legacy artifact declaration."""
    control_read: bool = False
    owned_fetch: bool = False
    artifact_type: type[Artifact] = Artifact


def collection_profile(adapter: Any) -> CollectionProfile:
    profile = getattr(adapter, 'collection_profile', CollectionProfile())
    if (type(profile) is not CollectionProfile or type(profile.control_read) is not bool
        or type(profile.owned_fetch) is not bool or not isinstance(profile.artifact_type, type)
        or not issubclass(profile.artifact_type, Artifact)):
        raise ValueError('media collection profile unavailable')
    facts = media_operation_facts(getattr(adapter, 'adapter_id', None), getattr(adapter, 'version', None))
    if facts is not None and (facts.control_read != profile.control_read or facts.owned_fetch != profile.owned_fetch):
        raise ValueError('media collection profile unavailable')
    return profile


def artifact_from_record(value: Mapping[str, Any], profile: CollectionProfile) -> Artifact:
    """Decode a validated legacy record using its qualified adapter declaration."""
    describe_artifact(value)
    return profile.artifact_type(**value)
