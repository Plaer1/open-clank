"""B4-P1: declared MediaOperationFacts fields equal what shared vendor-name branches implied."""
import dataclasses
from src.openclank.media_adapters import base
from src.openclank.media_adapters.base import MediaOperationFacts, media_operation_facts

NEW = ('route_kind', 'binding_interface', 'binding_required', 'binding_extra_fields', 'fixed_model_revision', 'task_id_pattern')
# BQ-20: only fields a shared consumer reads remain (all in src/bg_jobs.py); binding_scope, mesh_textured, input_kinds, metering and
# fixed_image_px had no reader and were removed.
# adapter: (route_kind, binding_interface, binding_required, binding_extra_fields, fixed_model_revision, task_id_pattern)
EXPECTED = {
    'comfyui': ('remote', 'comfyui-workflow-v1', False, (), '', ''),
    'comfyui-flac-v1': ('remote', 'comfyui-workflow-v1', False, (), '', ''),
    'comfyui-material-v1': ('remote', 'comfyui-workflow-v1', False, ('recipe_resource_id', 'material_resolution', 'material_outputs'), '', ''),
    'sprixen-sprite-v1': ('remote', 'sprixen-sprite-v1', False, (), '', ''),
    'sprixen-animation-v1': ('remote', 'sprixen-sprite-v1', False, ('projectID', 'spriteID', 'sourceSHA256', 'animationParameters'), 'standard', 'anim_[A-Za-z0-9_-]{1,124}'),
    'meshy-preview-v1': ('remote', 'meshy-preview-v1', False, (), '', ''),
    'meshy-refine-v1': ('remote', 'meshy-preview-v1', False, ('sourceJobID', 'sourceRevision', 'sourceTaskID', 'sourceArtifactID', 'sourceSHA256'), '', ''),
    'meshy-rig-v1': ('remote', 'meshy-preview-v1', True, ('source_ref', 'source_revision', 'source_sha256'), 'meshy-rig-v1', ''),
    'tripo-text3d-v1': ('remote', 'tripo-text3d-v1', False, (), '', ''),
    'fal-flux-schnell-v1': ('remote', 'fal-flux-schnell-v1', False, (), '', ''),
    'voidless-image-v1': ('remote', 'voidless-image-v1', True, (), 'flux2-klein', ''),
    'fal-hunyuan-image3d-v1': ('remote', 'fal-flux-schnell-v1', True, ('image_source',), 'fal-ai/hunyuan3d/v2', ''),
    'trellis-image3d-v1': ('local', 'trellis-image3d-v1', True, (), 'trellis-image-glb-v1', ''),
    'hunyuan3d-v2-shape-v1': ('local', 'hunyuan3d-v2-shape-v1', True, (), 'hunyuan3d-v2-image-glb-v1', ''),
    'trim-webm-v1': ('local', 'trim-webm-v1', False, (), 'fixed-v1', ''),
    'screen-record-v1': ('local', 'screen-record-v1', False, (), 'fixed-v1', ''),
    'latex-pdflatex-v1': ('local', 'latex-pdflatex-v1', False, (), 'fixed-v1', ''),
    'blender-glb-png-v1': ('local', 'blender-render-v1', False, (), '', ''),
    'blender-glb-four-heading-v1': ('local', 'blender-render-v1', False, (), '', ''),
    'blender-glb-rig-clip-v1': ('local', 'blender-render-v1', False, ('rig',), '', ''),
    'material-pack-v1': ('local', 'material-pack-v1', True, (), 'fixed-v1', ''),
    'blender-material-bake-v1': ('local', 'blender-render-v1', True, ('resolution',), '', ''),
    'sprite-sheet-v1': ('local', 'sprite-sheet-v1', False, (), 'fixed-v1', ''),
    'sprite-sheet-crops-v1': ('local', 'sprite-sheet-crops-v1', False, (), 'fixed-v1', ''),
}
# Representative sources: route_kind bg_jobs.py:2662/2680/2722/2506; binding_scope/interface bg_jobs.py:2482-2488,2491,2595,2675,2724;
# binding_required bg_jobs.py:2482-2488; binding_extra_fields bg_jobs.py:2499-2502,2678-2680,2709; fixed_model_revision bg_jobs.py:2503-2509,2578,2662;
# task_id_pattern bg_jobs.py:2953; mesh_textured media_adapters/hunyuan_local.py:24, trellis.py:45; input_kinds files_facade_routes.py:2004-2079,
# agent_media.py:93-141; metering files_facade_routes.py:2005-2079; fixed_image_px files_facade_routes.py:2071, media_human.py:140.


def test_every_catalog_entry_declares_every_new_field():
    assert set(EXPECTED) == {name for name, _ in base._MEDIA_OPERATION_FACTS}
    for name, want in EXPECTED.items():
        facts = media_operation_facts(name, '1')
        assert tuple(getattr(facts, f) for f in NEW) == want, name


def test_facts_immutable_and_hashable():
    facts = media_operation_facts('comfyui', '1')
    hash(facts)
    try: facts.route_kind = 'local'
    except dataclasses.FrozenInstanceError: pass
    else: raise AssertionError('facts must be frozen')
    assert [f.name for f in dataclasses.fields(MediaOperationFacts)][-len(NEW):] == list(NEW)


def test_unknown_adapter_still_none():
    assert media_operation_facts('nope-v1', '1') is None
    assert media_operation_facts('comfyui', '2') is None
    assert media_operation_facts(None, '1') is None
