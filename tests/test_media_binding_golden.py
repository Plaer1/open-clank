"""Golden tests (B4-P0): pin today's identity / binding / task-id / size-cap
validation of the media section of src/bg_jobs.py, for every adapter id.

Every expected value below is what the code does TODAY (verified by running),
including behaviour that looks wrong; those cases are listed in
.clanker/robonotes/claude-last-lap-2026-11-09/B4-P0-media-binding-golden.md.
Do not "fix" a row to what is right: change product code first, then update the
row deliberately.  Store, lock and clock are stubbed; nothing touches disk,
network or a real provider.  Plain asserts: runs under plain Python or pytest.
"""
import contextlib
import copy
import inspect
import re

import src.bg_jobs as bj
from src.openclank.media_adapters import base

ACCT, ROUTE = 'acct1', 'conn1'
MIB = 1024 * 1024
REV = {'kind': 'k', 'value': 'v'}


def H(c):
    return c * 64


# ==========================================================================
# HAND-WRITTEN TABLES (read from the code, then verified by running)
# ==========================================================================
E_TEXT = 'ValueError: invalid media f'
E_HASH = 'ValueError: invalid media digest'
# _media_text(value, field='f', limit): (value, limit, expected).  Text is rejected if empty, non-str, over `limit`
# BYTES (utf-8), containing a char < 0x20, or containing '://'.  DEL (0x7f) and '..' / '/' / ':/' are NOT rejected.
TEXT_ROWS = [
    ('a', 256, 'a'), ('a' * 256, 256, 'a' * 256), ('a' * 257, 256, E_TEXT),       # byte limit, both sides
    ('\u00e9' * 128, 256, '\u00e9' * 128), ('\u00e9' * 129, 256, E_TEXT),           # limit counts utf-8 bytes
    ('', 256, E_TEXT), (None, 256, E_TEXT), (5, 256, E_TEXT), (b'x', 256, E_TEXT), (['a'], 256, E_TEXT),
    ('a\x1fb', 256, E_TEXT), ('a\x00b', 256, E_TEXT), ('a\tb', 256, E_TEXT), ('a\nb', 256, E_TEXT),
    ('a\x7fb', 256, 'a\x7fb'),                                                      # DEL accepted (finding)
    (' ', 256, ' '), ('\u2028x', 256, '\u2028x'),
    ('http://x.test/a', 256, E_TEXT), ('https://x.test', 256, E_TEXT), ('file:///etc/passwd', 256, E_TEXT), ('a://', 256, E_TEXT),
    ('a:/b', 256, 'a:/b'), ('//evil.test/x', 256, '//evil.test/x'),                 # only the literal '://' is refused
    ('C:\\x', 256, 'C:\\x'), ('../../x', 256, '../../x'), ('x@evil.test:80', 256, 'x@evil.test:80'),
    ('a' * 5, 4, E_TEXT), ('abcd', 4, 'abcd'), ('a', 0, E_TEXT),
]
HASH_ROWS = [
    ('a' * 64, 'a' * 64), ('0123456789abcdef' * 4, '0123456789abcdef' * 4),
    ('A' * 64, E_HASH), ('a' * 63, E_HASH), ('a' * 65, E_HASH), ('g' * 64, E_HASH), ('a' * 64 + '\n', E_HASH),
    (' ' + 'a' * 63, E_HASH), ('', E_HASH), (None, E_HASH), (5, E_HASH), (b'a' * 64, E_HASH), ('sha256:' + 'a' * 64, E_HASH),
]

# (adapter_id, adapter_version, operation) -> operation of the compiled facts row, or None (no facts => legacy fallback)
FACTS_ROWS = [
    ('tripo-text3d-v1', '1', 'mesh.generate', 'mesh.generate'),
    ('tripo-text3d-v1', '2', 'mesh.generate', None), ('tripo-text3d-v1', 1, 'mesh.generate', None),
    ('tripo-text3d-v1', '', 'mesh.generate', None), ('tripo-text3d-v1', None, 'mesh.generate', None),
    ('tripo-text3d-v1', '1', 'mesh.refine', None), ('tripo-text3d-v1', '1', '', None), ('tripo-text3d-v1', '1', None, None),
    ('tripo-text3d-v1', '1', 'MESH.GENERATE', None),
    ('TRIPO-TEXT3D-V1', '1', 'mesh.generate', None), ('tripo-text3d-v1 ', '1', 'mesh.generate', None),
    ('tripo-text3d', '1', 'mesh.generate', None), ('tripo-text3d-v2', '1', 'mesh.generate', None),
    (None, '1', 'mesh.generate', None), (5, '1', 'mesh.generate', None), ('', '1', 'mesh.generate', None),
    ('meshy-preview-v1', '1', 'mesh.refine', None),       # another adapter's operation under this id
    ('meshy-refine-v1', '1', 'mesh.refine', 'mesh.refine'), ('meshy-preview-v1', '1', 'mesh.generate', 'mesh.generate'),
    ('comfyui', '1', 'image.generate', 'image.generate'), ('comfyui', '1', 'audio.generate', None),
    ('unknown-vendor-v1', '1', 'mesh.generate', None),
]
FOREGROUND_MODEL = {'comfyui', 'comfyui-flac-v1', 'comfyui-material-v1', 'sprixen-sprite-v1', 'sprixen-animation-v1', 'meshy-preview-v1',
                    'meshy-refine-v1', 'meshy-rig-v1', 'tripo-text3d-v1', 'fal-flux-schnell-v1', 'voidless-image-v1',
                    'fal-hunyuan-image3d-v1', 'trellis-image3d-v1', 'hunyuan3d-v2-shape-v1'}
FOREGROUND_CPU = {'trim-webm-v1', 'screen-record-v1', 'latex-pdflatex-v1', 'blender-glb-png-v1', 'blender-glb-four-heading-v1',
                  'blender-glb-rig-clip-v1', 'material-pack-v1', 'blender-material-bake-v1', 'sprite-sheet-v1', 'sprite-sheet-crops-v1'}
# owned-fetch lifetime marker written by claim_media('collect'): from compiled facts by adapter id (the legacy tuple was removed by B4-FIX 6)
LEGACY_OWNED_CLAIM = {'meshy-preview-v1', 'meshy-refine-v1', 'tripo-text3d-v1', 'fal-flux-schnell-v1', 'fal-hunyuan-image3d-v1', 'comfyui-flac-v1'}
OWNED_FETCH_FACTS = {'comfyui-flac-v1', 'comfyui-material-v1', 'meshy-preview-v1', 'meshy-refine-v1', 'meshy-rig-v1', 'tripo-text3d-v1',
                     'fal-flux-schnell-v1', 'voidless-image-v1', 'fal-hunyuan-image3d-v1'}

# Remote task id (finish_media remote_task_id / record_media_submission) for EVERY remote adapter except sprixen-animation-v1.
# True = accepted, False = refused by regex [A-Za-z0-9._:@-]+, string = _media_text refusal.
E_TID = 'ValueError: invalid media remote task id'
GENERIC_TASK_IDS = [
    ('a', True), ('task-123', True), ('A.b_c:d@e-f', True), ('018f1c2e-7b3a-7000-8000-0123456789ab', True), ('a' * 512, True),
    ('.', True), ('..', True), ('-', True), ('_', True), ('anim_abc', True), ('a:b', True), ('a@b', True),
    ('a' * 513, E_TID), ('', E_TID), (5, E_TID), (b'x', E_TID),   # None: see test_remote_task_id_syntax
    ('http://x.test/a', E_TID), ('a://b', E_TID), ('a\nb', E_TID), ('a\tb', E_TID), ('a\x00', E_TID),
    ('a b', False), (' a', False), ('a ', False), ('a/b', False), ('a:/b', False), ('a\\b', False), ('a#b', False), ('a?b=1', False),
    ('a%20b', False), ('a;b', False), ('a,b', False), ('a+b', False), ('a=b', False), ("a'b", False), ('\u00e9', False),
    ('a\x7f', False), ('\u0430bc', False),
]
# sprixen-animation-v1 only: ^anim_[A-Za-z0-9_-]{1,124}$ on top of the generic charset
ANIMATION_TASK_IDS = [
    ('anim_a', True), ('anim_' + 'a' * 124, True), ('anim_a-b_c', True), ('anim_A1', True), ('anim__', True), ('anim_-', True),
    ('anim_' + 'a' * 125, False), ('anim_', False), ('anim', False), ('ANIM_a', False), ('Anim_a', False), ('anim-a', False),
    ('anim_a.b', False), ('anim_a:b', False), ('anim_a@b', False), ('xanim_a', False), ('anim_a/b', False), ('_anim_a', False),
    ('task-123', False), ('a', False), ('anim_\u00e9', False),
    ('anim_a\n', E_TID), ('', E_TID), ('http://x.test/anim_a', E_TID),
]
# one illustrative id per remote adapter (shapes are illustrative, not vendor documentation)
VENDOR_TASK_IDS = {
    'comfyui': 'c0ffee00-1111-4222-8333-444455556666', 'comfyui-flac-v1': 'c0ffee00-1111-4222-8333-444455556667',
    'comfyui-material-v1': 'c0ffee00-1111-4222-8333-444455556668', 'sprixen-sprite-v1': 'spr_0123456789',
    'sprixen-animation-v1': 'anim_0123456789', 'meshy-preview-v1': '018f1c2e-7b3a-7000-8000-0123456789ab',
    'meshy-refine-v1': '018f1c2e-7b3a-7000-8000-0123456789ac', 'meshy-rig-v1': '018f1c2e-7b3a-7000-8000-0123456789ad',
    'tripo-text3d-v1': 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', 'fal-flux-schnell-v1': 'f2f0c1a0-0000-4000-8000-0123456789ab',
    'voidless-image-v1': 'vl-0123456789', 'fal-hunyuan-image3d-v1': 'f2f0c1a0-0000-4000-8000-0123456789ac',
}
# every vendor's sample is accepted by every other remote adapter, except under sprixen-animation-v1
TASK_ID_REFUSED = {(a, 'sprixen-animation-v1') for a in VENDOR_TASK_IDS if a != 'sprixen-animation-v1'}

ARTIFACT_SHAPE = {
    'extra-field': 'ValueError: invalid media artifact', 'missing-sha': 'ValueError: invalid media artifact',
    'sha-upper': 'ValueError: invalid media digest', 'sha-short': 'ValueError: invalid media digest',
    'id-url': 'ValueError: invalid media artifact id', 'id-empty': 'ValueError: invalid media artifact id',
    'id-ctl': 'ValueError: invalid media artifact id', 'mime-url': 'ValueError: invalid media MIME',
}
# (adapter, finish-receipt cap in MiB from compiled facts) for adapters whose local receipts use _media_artifact_for
LOCAL_RECEIPT_CAPS = [('trellis-image3d-v1', 64), ('hunyuan3d-v2-shape-v1', 64), ('latex-pdflatex-v1', 32), ('material-pack-v1', 24),
                      ('blender-material-bake-v1', 24), ('blender-glb-png-v1', 256)]

# (adapter, facts mime, facts cap MiB, legacy-fallback mime, legacy cap MiB).  Facts path = adapter_version '1' + recorded operation;
# any other version/operation is refused for catalog adapters since B4-FIX 5 (the hard-coded legacy fallback is kept only for non-catalog ids).  Legacy != facts rows are findings (see note).
ARTIFACT_CAPS = [
    ('comfyui', 'image/png', 256, 'image/png', 256),
    ('comfyui-flac-v1', 'audio/flac', 256, 'audio/flac', 256),
    ('comfyui-material-v1', 'application/zip', 24, 'image/png', 256),  # legacy differs
    ('sprixen-sprite-v1', 'image/png', 256, 'image/png', 256),
    ('sprixen-animation-v1', 'image/png', 256, 'image/png', 256),
    ('meshy-preview-v1', 'model/gltf-binary', 256, 'model/gltf-binary', 256),
    ('meshy-refine-v1', 'model/gltf-binary', 256, 'model/gltf-binary', 256),
    ('meshy-rig-v1', 'model/gltf-binary', 64, 'image/png', 256),  # legacy differs
    ('tripo-text3d-v1', 'model/gltf-binary', 256, 'model/gltf-binary', 256),
    ('fal-flux-schnell-v1', 'image/png', 256, 'image/png', 256),
    ('voidless-image-v1', 'image/png', 32, 'image/png', 256),  # legacy differs
    ('fal-hunyuan-image3d-v1', 'model/gltf-binary', 32, 'model/gltf-binary', 32),
    ('trellis-image3d-v1', 'model/gltf-binary', 64, 'image/png', 256),  # legacy differs
    ('hunyuan3d-v2-shape-v1', 'model/gltf-binary', 64, 'image/png', 256),  # legacy differs
    ('trim-webm-v1', 'video/webm', 64, 'video/webm', 64),
    ('screen-record-v1', 'video/webm', 64, 'video/webm', 64),
    ('latex-pdflatex-v1', 'application/pdf', 32, 'application/pdf', 32),
    ('blender-glb-png-v1', 'image/png', 256, 'image/png', 256),
    ('blender-glb-four-heading-v1', 'image/png', 256, 'image/png', 256),
    ('blender-glb-rig-clip-v1', 'image/png', 256, 'image/png', 256),
    ('material-pack-v1', 'application/zip', 24, 'image/png', 256),  # legacy differs
    ('blender-material-bake-v1', 'application/zip', 24, 'image/png', 256),  # legacy differs
    ('sprite-sheet-v1', 'image/png', 256, 'image/png', 256),
    ('sprite-sheet-crops-v1', 'image/png', 256, 'image/png', 256),
]

# resource_binding absent: adapters whose identity is refused (everything else is accepted without any binding)
NO_BINDING_RESULT = {
    'meshy-rig-v1': 'ValueError: rig source binding required',
    'voidless-image-v1': 'ValueError: Voidless profile binding required',
    'fal-hunyuan-image3d-v1': 'ValueError: image source binding required',
    'trellis-image3d-v1': 'ValueError: TRELLIS source binding required',
    'hunyuan3d-v2-shape-v1': 'ValueError: Hunyuan source binding required',
    'material-pack-v1': 'ValueError: material source binding required',
    'blender-material-bake-v1': 'ValueError: bake source binding required',
}

# (adapter, mutation label, expected reserve_media outcome); mutation functions are in MUTATIONS
BINDING_ROWS = [
    ('comfyui', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('comfyui', 'iface-upper', 'ValueError: invalid media profile binding'),
    ('comfyui', 'iface-empty', 'ValueError: invalid media interface'),
    ('comfyui', 'iface-int', 'ValueError: invalid media interface'),
    ('comfyui', 'binding-none', 'ValueError: invalid media resource binding'),
    ('comfyui', 'binding-list', 'ValueError: invalid media resource binding'),
    ('comfyui', 'binding-str', 'ValueError: invalid media resource binding'),
    ('comfyui', 'binding-absent', 'ok'),
    ('comfyui', 'binding-extra-key', 'ValueError: invalid media resource binding'),
    ('comfyui', 'drop-ownerAccountID', 'ValueError: invalid media resource binding'),
    ('comfyui', 'drop-destination_ref', 'ValueError: invalid media resource binding'),
    ('comfyui', 'drop-project_ref', 'ValueError: invalid media resource binding'),
    ('comfyui', 'conn-mismatch', 'ValueError: invalid media profile binding'),
    ('comfyui', 'owner-mismatch', 'ValueError: invalid media profile binding'),
    ('comfyui', 'rev-0', 'ok'),
    ('comfyui', 'rev-bool', 'ValueError: invalid media profile revision'),
    ('comfyui', 'rev-str', 'ValueError: invalid media profile revision'),
    ('comfyui', 'project-set', 'ValueError: invalid media project binding'),
    ('comfyui', 'name-suffix', 'ValueError: invalid media output name'),
    ('comfyui', 'name-slash', 'ValueError: invalid media output name'),
    ('comfyui', 'name-dotdot', 'ValueError: invalid media output name'),
    ('comfyui', 'name-url', 'ValueError: invalid media destination_ref'),
    ('comfyui', 'dest-rev-extra', 'ValueError: invalid media resource revision'),
    ('comfyui', 'version-2', 'ok'),
    ('comfyui', 'version-int', 'ValueError: invalid media adapter_version'),
    ('comfyui', 'route-flip', 'ok'),
    ('comfyui', 'op-wrong', 'ok'),
    ('comfyui', 'model-wrong', 'ok'),
    ('comfyui', 'digest-upper', 'ValueError: invalid media digest'),
    ('comfyui', 'inputs-extra', 'ok'),
    ('comfyui', 'inputs-empty', 'ok'),
    ('comfyui', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('comfyui', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('comfyui', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('comfyui', 'account-empty', 'ValueError: invalid media account_id'),
    ('comfyui-flac-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('comfyui-flac-v1', 'iface-upper', 'ValueError: invalid media profile binding'),
    ('comfyui-flac-v1', 'iface-empty', 'ValueError: invalid media interface'),
    ('comfyui-flac-v1', 'iface-int', 'ValueError: invalid media interface'),
    ('comfyui-flac-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'binding-absent', 'ok'),
    ('comfyui-flac-v1', 'binding-extra-key', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'drop-ownerAccountID', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'drop-destination_ref', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'drop-project_ref', 'ValueError: invalid media resource binding'),
    ('comfyui-flac-v1', 'conn-mismatch', 'ValueError: invalid media profile binding'),
    ('comfyui-flac-v1', 'owner-mismatch', 'ValueError: invalid media profile binding'),
    ('comfyui-flac-v1', 'rev-0', 'ok'),
    ('comfyui-flac-v1', 'rev-bool', 'ValueError: invalid media profile revision'),
    ('comfyui-flac-v1', 'rev-str', 'ValueError: invalid media profile revision'),
    ('comfyui-flac-v1', 'project-set', 'ValueError: invalid media project binding'),
    ('comfyui-flac-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('comfyui-flac-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('comfyui-flac-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('comfyui-flac-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('comfyui-flac-v1', 'dest-rev-extra', 'ValueError: invalid media resource revision'),
    ('comfyui-flac-v1', 'version-2', 'ok'),
    ('comfyui-flac-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('comfyui-flac-v1', 'route-flip', 'ok'),
    ('comfyui-flac-v1', 'op-wrong', 'ok'),
    ('comfyui-flac-v1', 'model-wrong', 'ok'),
    ('comfyui-flac-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('comfyui-flac-v1', 'inputs-extra', 'ok'),
    ('comfyui-flac-v1', 'inputs-empty', 'ok'),
    ('comfyui-flac-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('comfyui-flac-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('comfyui-flac-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('comfyui-flac-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('comfyui-material-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('comfyui-material-v1', 'iface-upper', 'ValueError: invalid media profile binding'),
    ('comfyui-material-v1', 'iface-empty', 'ValueError: invalid media interface'),
    ('comfyui-material-v1', 'iface-int', 'ValueError: invalid media interface'),
    ('comfyui-material-v1', 'binding-none', 'ValueError: invalid media resource binding'),  # B4-FIX 7
    ('comfyui-material-v1', 'binding-list', 'ValueError: invalid media resource binding'),  # B4-FIX 7
    ('comfyui-material-v1', 'binding-str', 'ValueError: invalid media resource binding'),  # B4-FIX 7
    ('comfyui-material-v1', 'binding-absent', 'ok'),
    ('comfyui-material-v1', 'binding-extra-key', 'ValueError: invalid media resource binding'),
    ('comfyui-material-v1', 'drop-ownerAccountID', 'ValueError: invalid media resource binding'),
    ('comfyui-material-v1', 'drop-destination_ref', 'ValueError: invalid media resource binding'),
    ('comfyui-material-v1', 'drop-project_ref', 'ValueError: invalid media resource binding'),
    ('comfyui-material-v1', 'conn-mismatch', 'ValueError: invalid media profile binding'),
    ('comfyui-material-v1', 'owner-mismatch', 'ValueError: invalid media profile binding'),
    ('comfyui-material-v1', 'rev-0', 'ok'),
    ('comfyui-material-v1', 'rev-bool', 'ValueError: invalid media profile revision'),
    ('comfyui-material-v1', 'rev-str', 'ValueError: invalid media profile revision'),
    ('comfyui-material-v1', 'project-set', 'ValueError: invalid media project binding'),
    ('comfyui-material-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('comfyui-material-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('comfyui-material-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('comfyui-material-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('comfyui-material-v1', 'dest-rev-extra', 'ValueError: invalid media resource revision'),
    ('comfyui-material-v1', 'version-2', 'ValueError: invalid generated material identity'),
    ('comfyui-material-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('comfyui-material-v1', 'route-flip', 'ValueError: invalid generated material identity'),
    ('comfyui-material-v1', 'op-wrong', 'ValueError: invalid generated material identity'),
    ('comfyui-material-v1', 'model-wrong', 'ok'),
    ('comfyui-material-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('comfyui-material-v1', 'inputs-extra', 'ok'),
    ('comfyui-material-v1', 'inputs-empty', 'ok'),
    ('comfyui-material-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('comfyui-material-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('comfyui-material-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('comfyui-material-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('comfyui-material-v1', 'mat-res-bad', 'ValueError: invalid generated material identity'),
    ('sprixen-sprite-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('sprixen-sprite-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('sprixen-sprite-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('sprixen-sprite-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('sprixen-sprite-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('sprixen-sprite-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('sprixen-sprite-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('sprixen-sprite-v1', 'binding-absent', 'ok'),
    ('sprixen-sprite-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('sprixen-sprite-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('sprixen-sprite-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('sprixen-sprite-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('sprixen-sprite-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('sprixen-sprite-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('sprixen-sprite-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('sprixen-sprite-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('sprixen-sprite-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('sprixen-sprite-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('sprixen-sprite-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('sprixen-sprite-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('sprixen-sprite-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('sprixen-sprite-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('sprixen-sprite-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('sprixen-sprite-v1', 'version-2', 'ok'),
    ('sprixen-sprite-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('sprixen-sprite-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('sprixen-sprite-v1', 'op-wrong', 'ok'),
    ('sprixen-sprite-v1', 'model-wrong', 'ok'),
    ('sprixen-sprite-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('sprixen-sprite-v1', 'inputs-extra', 'ok'),
    ('sprixen-sprite-v1', 'inputs-empty', 'ok'),
    ('sprixen-sprite-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('sprixen-sprite-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('sprixen-sprite-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('sprixen-sprite-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('sprixen-animation-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('sprixen-animation-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('sprixen-animation-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('sprixen-animation-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('sprixen-animation-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('sprixen-animation-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('sprixen-animation-v1', 'binding-absent', 'ok'),
    ('sprixen-animation-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('sprixen-animation-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('sprixen-animation-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('sprixen-animation-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('sprixen-animation-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('sprixen-animation-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('sprixen-animation-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('sprixen-animation-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('sprixen-animation-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('sprixen-animation-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('sprixen-animation-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('sprixen-animation-v1', 'version-2', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('sprixen-animation-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'op-wrong', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'model-wrong', 'ValueError: invalid remote media resource binding'),
    ('sprixen-animation-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('sprixen-animation-v1', 'inputs-extra', 'ok'),
    ('sprixen-animation-v1', 'inputs-empty', 'ValueError: animation source witness unavailable'),
    ('sprixen-animation-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('sprixen-animation-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('sprixen-animation-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('sprixen-animation-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('sprixen-animation-v1', 'anim-type-bad', 'ValueError: invalid animation parameters'),
    ('sprixen-animation-v1', 'anim-frames-13', 'ValueError: invalid animation parameters'),
    ('sprixen-animation-v1', 'anim-fps-3', 'ValueError: invalid animation parameters'),
    ('sprixen-animation-v1', 'anim-project-slash', 'ValueError: invalid animation source identity'),
    ('sprixen-animation-v1', 'anim-sprite-129', 'ValueError: invalid animation source identity'),
    ('sprixen-animation-v1', 'source-sha-not-input', 'ValueError: animation source witness unavailable'),
    ('meshy-preview-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('meshy-preview-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('meshy-preview-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('meshy-preview-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('meshy-preview-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('meshy-preview-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('meshy-preview-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('meshy-preview-v1', 'binding-absent', 'ok'),
    ('meshy-preview-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('meshy-preview-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('meshy-preview-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('meshy-preview-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('meshy-preview-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('meshy-preview-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('meshy-preview-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-preview-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-preview-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-preview-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('meshy-preview-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('meshy-preview-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('meshy-preview-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('meshy-preview-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('meshy-preview-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('meshy-preview-v1', 'version-2', 'ok'),
    ('meshy-preview-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('meshy-preview-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('meshy-preview-v1', 'op-wrong', 'ok'),
    ('meshy-preview-v1', 'model-wrong', 'ok'),
    ('meshy-preview-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('meshy-preview-v1', 'inputs-extra', 'ok'),
    ('meshy-preview-v1', 'inputs-empty', 'ok'),
    ('meshy-preview-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('meshy-preview-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('meshy-preview-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('meshy-preview-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('meshy-refine-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('meshy-refine-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('meshy-refine-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('meshy-refine-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('meshy-refine-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('meshy-refine-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('meshy-refine-v1', 'binding-absent', 'ok'),
    ('meshy-refine-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('meshy-refine-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('meshy-refine-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-refine-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-refine-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-refine-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('meshy-refine-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('meshy-refine-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('meshy-refine-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('meshy-refine-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('meshy-refine-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('meshy-refine-v1', 'version-2', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('meshy-refine-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'op-wrong', 'ValueError: invalid remote media resource binding'),
    ('meshy-refine-v1', 'model-wrong', 'ok'),
    ('meshy-refine-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('meshy-refine-v1', 'inputs-extra', 'ok'),
    ('meshy-refine-v1', 'inputs-empty', 'ValueError: refine source unavailable'),
    ('meshy-refine-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('meshy-refine-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('meshy-refine-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('meshy-refine-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('meshy-refine-v1', 'source-sha-not-input', 'ValueError: refine source unavailable'),
    ('meshy-rig-v1', 'iface-other', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'iface-upper', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'iface-empty', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'iface-int', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'binding-none', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'binding-list', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'binding-str', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'binding-absent', 'ValueError: rig source binding required'),
    ('meshy-rig-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('meshy-rig-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('meshy-rig-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-rig-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-rig-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('meshy-rig-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('meshy-rig-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('meshy-rig-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('meshy-rig-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('meshy-rig-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('meshy-rig-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('meshy-rig-v1', 'version-2', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('meshy-rig-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'op-wrong', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'model-wrong', 'ValueError: invalid remote media resource binding'),
    ('meshy-rig-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('meshy-rig-v1', 'inputs-extra', 'ValueError: rig source changed'),
    ('meshy-rig-v1', 'inputs-empty', 'ValueError: rig source changed'),
    ('meshy-rig-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('meshy-rig-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('meshy-rig-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('meshy-rig-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('meshy-rig-v1', 'rig-sha-not-input', 'ValueError: rig source changed'),
    ('tripo-text3d-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('tripo-text3d-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('tripo-text3d-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('tripo-text3d-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('tripo-text3d-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('tripo-text3d-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('tripo-text3d-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('tripo-text3d-v1', 'binding-absent', 'ok'),
    ('tripo-text3d-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('tripo-text3d-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('tripo-text3d-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('tripo-text3d-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('tripo-text3d-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('tripo-text3d-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('tripo-text3d-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('tripo-text3d-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('tripo-text3d-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('tripo-text3d-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('tripo-text3d-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('tripo-text3d-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('tripo-text3d-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('tripo-text3d-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('tripo-text3d-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('tripo-text3d-v1', 'version-2', 'ok'),
    ('tripo-text3d-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('tripo-text3d-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('tripo-text3d-v1', 'op-wrong', 'ok'),
    ('tripo-text3d-v1', 'model-wrong', 'ok'),
    ('tripo-text3d-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('tripo-text3d-v1', 'inputs-extra', 'ok'),
    ('tripo-text3d-v1', 'inputs-empty', 'ok'),
    ('tripo-text3d-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('tripo-text3d-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('tripo-text3d-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('tripo-text3d-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('fal-flux-schnell-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('fal-flux-schnell-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('fal-flux-schnell-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('fal-flux-schnell-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('fal-flux-schnell-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('fal-flux-schnell-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('fal-flux-schnell-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('fal-flux-schnell-v1', 'binding-absent', 'ok'),
    ('fal-flux-schnell-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('fal-flux-schnell-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('fal-flux-schnell-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('fal-flux-schnell-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('fal-flux-schnell-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('fal-flux-schnell-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('fal-flux-schnell-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('fal-flux-schnell-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('fal-flux-schnell-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('fal-flux-schnell-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('fal-flux-schnell-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('fal-flux-schnell-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('fal-flux-schnell-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('fal-flux-schnell-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('fal-flux-schnell-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('fal-flux-schnell-v1', 'version-2', 'ok'),
    ('fal-flux-schnell-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('fal-flux-schnell-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('fal-flux-schnell-v1', 'op-wrong', 'ok'),
    ('fal-flux-schnell-v1', 'model-wrong', 'ok'),
    ('fal-flux-schnell-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('fal-flux-schnell-v1', 'inputs-extra', 'ok'),
    ('fal-flux-schnell-v1', 'inputs-empty', 'ok'),
    ('fal-flux-schnell-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('fal-flux-schnell-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('fal-flux-schnell-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('fal-flux-schnell-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('voidless-image-v1', 'iface-other', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'iface-upper', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'iface-empty', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'iface-int', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'binding-none', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'binding-list', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'binding-str', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'binding-absent', 'ValueError: Voidless profile binding required'),
    ('voidless-image-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('voidless-image-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('voidless-image-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('voidless-image-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('voidless-image-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('voidless-image-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('voidless-image-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('voidless-image-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('voidless-image-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('voidless-image-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('voidless-image-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('voidless-image-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('voidless-image-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('voidless-image-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('voidless-image-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('voidless-image-v1', 'version-2', 'ValueError: invalid Voidless identity'),
    ('voidless-image-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('voidless-image-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('voidless-image-v1', 'op-wrong', 'ValueError: invalid Voidless identity'),
    ('voidless-image-v1', 'model-wrong', 'ValueError: invalid Voidless identity'),
    ('voidless-image-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('voidless-image-v1', 'inputs-extra', 'ValueError: invalid Voidless identity'),
    ('voidless-image-v1', 'inputs-empty', 'ok'),
    ('voidless-image-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('voidless-image-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('voidless-image-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('voidless-image-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('fal-hunyuan-image3d-v1', 'iface-other', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'iface-upper', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'iface-empty', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'iface-int', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'binding-none', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'binding-list', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'binding-str', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'binding-absent', 'ValueError: image source binding required'),
    ('fal-hunyuan-image3d-v1', 'binding-extra-key', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'drop-ownerAccountID', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'drop-destination_ref', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'drop-project_ref', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'conn-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('fal-hunyuan-image3d-v1', 'owner-mismatch', 'ValueError: invalid Sprixen account binding'),
    ('fal-hunyuan-image3d-v1', 'rev-0', 'ValueError: invalid Sprixen profile revision'),
    ('fal-hunyuan-image3d-v1', 'rev-bool', 'ValueError: invalid Sprixen profile revision'),
    ('fal-hunyuan-image3d-v1', 'rev-str', 'ValueError: invalid Sprixen profile revision'),
    ('fal-hunyuan-image3d-v1', 'project-set', 'ValueError: project-aware Sprixen outputs unavailable'),
    ('fal-hunyuan-image3d-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('fal-hunyuan-image3d-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('fal-hunyuan-image3d-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('fal-hunyuan-image3d-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('fal-hunyuan-image3d-v1', 'dest-rev-extra', 'ValueError: invalid Sprixen destination revision'),
    ('fal-hunyuan-image3d-v1', 'version-2', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('fal-hunyuan-image3d-v1', 'route-flip', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'op-wrong', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'model-wrong', 'ValueError: invalid remote media resource binding'),
    ('fal-hunyuan-image3d-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('fal-hunyuan-image3d-v1', 'inputs-extra', 'ValueError: image source hash changed'),
    ('fal-hunyuan-image3d-v1', 'inputs-empty', 'ValueError: image source hash changed'),
    ('fal-hunyuan-image3d-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('fal-hunyuan-image3d-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('fal-hunyuan-image3d-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('fal-hunyuan-image3d-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('fal-hunyuan-image3d-v1', 'source-mime-jpeg', 'ok'),
    ('fal-hunyuan-image3d-v1', 'source-size-0', 'ValueError: invalid image source witness'),
    ('fal-hunyuan-image3d-v1', 'source-size-4097', 'ValueError: invalid image source witness'),
    ('fal-hunyuan-image3d-v1', 'source-hash-changed', 'ValueError: image source hash changed'),
    ('trellis-image3d-v1', 'iface-other', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'iface-upper', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'iface-empty', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'iface-int', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'binding-none', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'binding-list', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'binding-str', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'binding-absent', 'ValueError: TRELLIS source binding required'),
    ('trellis-image3d-v1', 'binding-extra-key', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'drop-ownerAccountID', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'drop-destination_ref', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'drop-project_ref', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'conn-mismatch', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'owner-mismatch', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'rev-0', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'rev-bool', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'rev-str', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'project-set', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('trellis-image3d-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('trellis-image3d-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('trellis-image3d-v1', 'name-url', 'ValueError: invalid media destination'),
    ('trellis-image3d-v1', 'dest-rev-extra', 'ValueError: invalid TRELLIS source'),
    ('trellis-image3d-v1', 'version-2', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('trellis-image3d-v1', 'route-flip', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'op-wrong', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'model-wrong', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('trellis-image3d-v1', 'inputs-extra', 'ValueError: TRELLIS source changed'),
    ('trellis-image3d-v1', 'inputs-empty', 'ValueError: TRELLIS source changed'),
    ('trellis-image3d-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('trellis-image3d-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('trellis-image3d-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('trellis-image3d-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('trellis-image3d-v1', 'seed-neg', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'seed-max+1', 'ValueError: invalid TRELLIS binding'),
    ('trellis-image3d-v1', 'seed-max', 'ok'),
    ('trellis-image3d-v1', 'source-mime-jpeg', 'ValueError: invalid TRELLIS source'),
    ('trellis-image3d-v1', 'source-size-0', 'ValueError: invalid TRELLIS source'),
    ('trellis-image3d-v1', 'source-size-4097', 'ValueError: invalid TRELLIS source'),
    ('trellis-image3d-v1', 'source-hash-changed', 'ValueError: TRELLIS source changed'),
    ('hunyuan3d-v2-shape-v1', 'iface-other', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'iface-upper', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'iface-empty', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'iface-int', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'binding-none', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'binding-list', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'binding-str', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'binding-absent', 'ValueError: Hunyuan source binding required'),
    ('hunyuan3d-v2-shape-v1', 'binding-extra-key', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'drop-ownerAccountID', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'drop-destination_ref', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'drop-project_ref', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'conn-mismatch', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'owner-mismatch', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'rev-0', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'rev-bool', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'rev-str', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'project-set', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('hunyuan3d-v2-shape-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('hunyuan3d-v2-shape-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('hunyuan3d-v2-shape-v1', 'name-url', 'ValueError: invalid media destination'),
    ('hunyuan3d-v2-shape-v1', 'dest-rev-extra', 'ValueError: invalid Hunyuan source'),
    ('hunyuan3d-v2-shape-v1', 'version-2', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('hunyuan3d-v2-shape-v1', 'route-flip', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'op-wrong', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'model-wrong', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('hunyuan3d-v2-shape-v1', 'inputs-extra', 'ValueError: Hunyuan source changed'),
    ('hunyuan3d-v2-shape-v1', 'inputs-empty', 'ValueError: Hunyuan source changed'),
    ('hunyuan3d-v2-shape-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('hunyuan3d-v2-shape-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('hunyuan3d-v2-shape-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('hunyuan3d-v2-shape-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('hunyuan3d-v2-shape-v1', 'seed-neg', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'seed-max+1', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'seed-max', 'ok'),
    ('hunyuan3d-v2-shape-v1', 'device-bad', 'ValueError: invalid Hunyuan binding'),
    ('hunyuan3d-v2-shape-v1', 'device-cuda', 'ok'),
    ('hunyuan3d-v2-shape-v1', 'source-mime-jpeg', 'ValueError: invalid Hunyuan source'),
    ('hunyuan3d-v2-shape-v1', 'source-size-0', 'ValueError: invalid Hunyuan source'),
    ('hunyuan3d-v2-shape-v1', 'source-size-4097', 'ValueError: invalid Hunyuan source'),
    ('hunyuan3d-v2-shape-v1', 'source-hash-changed', 'ValueError: Hunyuan source changed'),
    ('screen-record-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('screen-record-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('screen-record-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('screen-record-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('screen-record-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('screen-record-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('screen-record-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('screen-record-v1', 'binding-absent', 'ok'),
    ('screen-record-v1', 'binding-extra-key', 'ValueError: recording binding unavailable'),
    ('screen-record-v1', 'drop-ownerAccountID', 'ValueError: recording binding unavailable'),
    ('screen-record-v1', 'drop-destination_ref', 'ValueError: recording binding unavailable'),
    ('screen-record-v1', 'owner-mismatch', 'ValueError: recording binding unavailable'),
    ('screen-record-v1', 'name-suffix', 'ValueError: recording name unavailable'),
    ('screen-record-v1', 'name-slash', 'ValueError: recording name unavailable'),
    ('screen-record-v1', 'name-dotdot', 'ValueError: recording name unavailable'),
    ('screen-record-v1', 'name-url', 'ok'),
    ('screen-record-v1', 'dest-rev-extra', 'ValueError: recording revision unavailable'),
    ('screen-record-v1', 'version-2', 'ValueError: invalid recording job identity'),
    ('screen-record-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('screen-record-v1', 'route-flip', 'ValueError: invalid recording job identity'),
    ('screen-record-v1', 'op-wrong', 'ValueError: invalid recording job identity'),
    ('screen-record-v1', 'model-wrong', 'ValueError: invalid recording job identity'),
    ('screen-record-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('screen-record-v1', 'inputs-extra', 'ValueError: recording binding unavailable'),
    ('screen-record-v1', 'inputs-empty', 'ok'),
    ('screen-record-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('screen-record-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('screen-record-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('screen-record-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('latex-pdflatex-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('latex-pdflatex-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('latex-pdflatex-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('latex-pdflatex-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('latex-pdflatex-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('latex-pdflatex-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('latex-pdflatex-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('latex-pdflatex-v1', 'binding-absent', 'ok'),
    ('latex-pdflatex-v1', 'binding-extra-key', 'ValueError: LaTeX binding unavailable'),
    ('latex-pdflatex-v1', 'drop-ownerAccountID', 'ValueError: LaTeX binding unavailable'),
    ('latex-pdflatex-v1', 'drop-destination_ref', 'ValueError: LaTeX binding unavailable'),
    ('latex-pdflatex-v1', 'owner-mismatch', 'ValueError: LaTeX binding unavailable'),
    ('latex-pdflatex-v1', 'name-suffix', 'ValueError: LaTeX destination name unavailable'),
    ('latex-pdflatex-v1', 'name-slash', 'ValueError: LaTeX destination name unavailable'),
    ('latex-pdflatex-v1', 'name-dotdot', 'ValueError: LaTeX destination name unavailable'),
    ('latex-pdflatex-v1', 'name-url', 'ok'),
    ('latex-pdflatex-v1', 'dest-rev-extra', 'ValueError: LaTeX revision unavailable'),
    ('latex-pdflatex-v1', 'version-2', 'ValueError: invalid LaTeX job identity'),
    ('latex-pdflatex-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('latex-pdflatex-v1', 'route-flip', 'ValueError: invalid LaTeX job identity'),
    ('latex-pdflatex-v1', 'op-wrong', 'ValueError: invalid LaTeX job identity'),
    ('latex-pdflatex-v1', 'model-wrong', 'ValueError: invalid LaTeX job identity'),
    ('latex-pdflatex-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('latex-pdflatex-v1', 'inputs-extra', 'ValueError: LaTeX input identity changed'),
    ('latex-pdflatex-v1', 'inputs-empty', 'ValueError: LaTeX input identity changed'),
    ('latex-pdflatex-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('latex-pdflatex-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('latex-pdflatex-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('latex-pdflatex-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('blender-glb-png-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('blender-glb-png-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('blender-glb-png-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('blender-glb-png-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('blender-glb-png-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('blender-glb-png-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('blender-glb-png-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('blender-glb-png-v1', 'binding-absent', 'ok'),
    ('blender-glb-png-v1', 'binding-extra-key', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-png-v1', 'drop-ownerAccountID', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-png-v1', 'drop-destination_ref', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-png-v1', 'drop-project_ref', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-png-v1', 'conn-mismatch', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-png-v1', 'owner-mismatch', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-png-v1', 'rev-0', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-png-v1', 'rev-bool', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-png-v1', 'rev-str', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-png-v1', 'project-set', 'ValueError: project-aware Blender outputs unavailable'),
    ('blender-glb-png-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('blender-glb-png-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('blender-glb-png-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('blender-glb-png-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('blender-glb-png-v1', 'dest-rev-extra', 'ValueError: invalid Blender resource revision'),
    ('blender-glb-png-v1', 'version-2', 'ok'),
    ('blender-glb-png-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('blender-glb-png-v1', 'route-flip', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-png-v1', 'op-wrong', 'ok'),
    ('blender-glb-png-v1', 'model-wrong', 'ok'),
    ('blender-glb-png-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('blender-glb-png-v1', 'inputs-extra', 'ok'),
    ('blender-glb-png-v1', 'inputs-empty', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-png-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('blender-glb-png-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('blender-glb-png-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('blender-glb-png-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('blender-glb-png-v1', 'rig-sha-not-input', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('blender-glb-four-heading-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('blender-glb-four-heading-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('blender-glb-four-heading-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('blender-glb-four-heading-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('blender-glb-four-heading-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('blender-glb-four-heading-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('blender-glb-four-heading-v1', 'binding-absent', 'ok'),
    ('blender-glb-four-heading-v1', 'binding-extra-key', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-four-heading-v1', 'drop-ownerAccountID', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-four-heading-v1', 'drop-destination_ref', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-four-heading-v1', 'drop-project_ref', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-four-heading-v1', 'conn-mismatch', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'owner-mismatch', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'rev-0', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'rev-bool', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'rev-str', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'project-set', 'ValueError: project-aware Blender outputs unavailable'),
    ('blender-glb-four-heading-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('blender-glb-four-heading-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('blender-glb-four-heading-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('blender-glb-four-heading-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('blender-glb-four-heading-v1', 'dest-rev-extra', 'ValueError: invalid Blender resource revision'),
    ('blender-glb-four-heading-v1', 'version-2', 'ok'),
    ('blender-glb-four-heading-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('blender-glb-four-heading-v1', 'route-flip', 'ValueError: invalid Blender resource binding'),
    ('blender-glb-four-heading-v1', 'op-wrong', 'ok'),
    ('blender-glb-four-heading-v1', 'model-wrong', 'ok'),
    ('blender-glb-four-heading-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('blender-glb-four-heading-v1', 'inputs-extra', 'ok'),
    ('blender-glb-four-heading-v1', 'inputs-empty', 'ValueError: invalid Blender profile binding'),
    ('blender-glb-four-heading-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('blender-glb-four-heading-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('blender-glb-four-heading-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('blender-glb-four-heading-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('blender-glb-four-heading-v1', 'rig-sha-not-input', 'ValueError: invalid Blender profile binding'),
    ('material-pack-v1', 'iface-other', 'ValueError: material source binding required'),
    ('material-pack-v1', 'iface-upper', 'ValueError: material source binding required'),
    ('material-pack-v1', 'iface-empty', 'ValueError: material source binding required'),
    ('material-pack-v1', 'iface-int', 'ValueError: material source binding required'),
    ('material-pack-v1', 'binding-none', 'ValueError: material source binding required'),
    ('material-pack-v1', 'binding-list', 'ValueError: material source binding required'),
    ('material-pack-v1', 'binding-str', 'ValueError: material source binding required'),
    ('material-pack-v1', 'binding-absent', 'ValueError: material source binding required'),
    ('material-pack-v1', 'binding-extra-key', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'drop-ownerAccountID', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'drop-destination_ref', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'owner-mismatch', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('material-pack-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('material-pack-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('material-pack-v1', 'name-url', 'ValueError: invalid media destination'),
    ('material-pack-v1', 'dest-rev-extra', 'ValueError: invalid destination'),
    ('material-pack-v1', 'version-2', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('material-pack-v1', 'route-flip', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'op-wrong', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'model-wrong', 'ValueError: invalid material binding'),
    ('material-pack-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('material-pack-v1', 'inputs-extra', 'ValueError: material sources changed'),
    ('material-pack-v1', 'inputs-empty', 'ValueError: material sources changed'),
    ('material-pack-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('material-pack-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('material-pack-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('material-pack-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('blender-material-bake-v1', 'iface-other', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'iface-upper', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'iface-empty', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'iface-int', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'binding-none', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'binding-list', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'binding-str', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'binding-absent', 'ValueError: bake source binding required'),
    ('blender-material-bake-v1', 'binding-extra-key', 'ValueError: invalid Blender resource binding'),
    ('blender-material-bake-v1', 'drop-ownerAccountID', 'ValueError: invalid Blender resource binding'),
    ('blender-material-bake-v1', 'drop-destination_ref', 'ValueError: invalid Blender resource binding'),
    ('blender-material-bake-v1', 'drop-project_ref', 'ValueError: invalid Blender resource binding'),
    ('blender-material-bake-v1', 'conn-mismatch', 'ValueError: invalid Blender profile binding'),
    ('blender-material-bake-v1', 'owner-mismatch', 'ValueError: invalid Blender profile binding'),
    ('blender-material-bake-v1', 'rev-0', 'ValueError: invalid Blender profile binding'),
    ('blender-material-bake-v1', 'rev-bool', 'ValueError: invalid Blender profile binding'),
    ('blender-material-bake-v1', 'rev-str', 'ValueError: invalid Blender profile binding'),
    ('blender-material-bake-v1', 'project-set', 'ValueError: project-aware Blender outputs unavailable'),
    ('blender-material-bake-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('blender-material-bake-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('blender-material-bake-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('blender-material-bake-v1', 'name-url', 'ValueError: invalid media destination_ref'),
    ('blender-material-bake-v1', 'dest-rev-extra', 'ValueError: invalid Blender resource revision'),
    ('blender-material-bake-v1', 'version-2', 'ValueError: invalid bake identity'),
    ('blender-material-bake-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('blender-material-bake-v1', 'route-flip', 'ValueError: invalid Blender resource binding'),
    ('blender-material-bake-v1', 'op-wrong', 'ValueError: invalid bake identity'),
    ('blender-material-bake-v1', 'model-wrong', 'ValueError: invalid bake identity'),
    ('blender-material-bake-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('blender-material-bake-v1', 'inputs-extra', 'ValueError: invalid bake identity'),
    ('blender-material-bake-v1', 'inputs-empty', 'ValueError: invalid Blender profile binding'),
    ('blender-material-bake-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('blender-material-bake-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('blender-material-bake-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('blender-material-bake-v1', 'account-empty', 'ValueError: invalid media account_id'),
    ('blender-material-bake-v1', 'resolution-bad', 'ValueError: invalid bake identity'),
    ('blender-material-bake-v1', 'resolution-2048', 'ok'),
    ('blender-material-bake-v1', 'rig-sha-not-input', 'ValueError: invalid Blender profile binding'),
    ('sprite-sheet-v1', 'iface-other', 'ValueError: invalid remote media resource binding'),
    ('sprite-sheet-v1', 'iface-upper', 'ValueError: invalid media resource binding'),
    ('sprite-sheet-v1', 'iface-empty', 'ValueError: invalid media resource binding'),
    ('sprite-sheet-v1', 'iface-int', 'ValueError: invalid media resource binding'),
    ('sprite-sheet-v1', 'binding-none', 'ValueError: invalid media resource binding'),
    ('sprite-sheet-v1', 'binding-list', 'ValueError: invalid media resource binding'),
    ('sprite-sheet-v1', 'binding-str', 'ValueError: invalid media resource binding'),
    ('sprite-sheet-v1', 'binding-absent', 'ok'),
    ('sprite-sheet-v1', 'binding-extra-key', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'drop-ownerAccountID', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'drop-destination_ref', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'owner-mismatch', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'name-suffix', 'ValueError: invalid media output name'),
    ('sprite-sheet-v1', 'name-slash', 'ValueError: invalid media output name'),
    ('sprite-sheet-v1', 'name-dotdot', 'ValueError: invalid media output name'),
    ('sprite-sheet-v1', 'name-url', 'ValueError: invalid media destination'),
    ('sprite-sheet-v1', 'dest-rev-extra', 'ValueError: invalid destination'),
    ('sprite-sheet-v1', 'version-2', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'version-int', 'ValueError: invalid media adapter_version'),
    ('sprite-sheet-v1', 'route-flip', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'op-wrong', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'model-wrong', 'ValueError: invalid sprite binding'),
    ('sprite-sheet-v1', 'digest-upper', 'ValueError: invalid media digest'),
    ('sprite-sheet-v1', 'inputs-extra', 'ValueError: sprite source hashes changed'),
    ('sprite-sheet-v1', 'inputs-empty', 'ValueError: sprite source hashes changed'),
    ('sprite-sheet-v1', 'adapter-empty', 'ValueError: invalid media adapter_id'),
    ('sprite-sheet-v1', 'adapter-int', 'ValueError: invalid media adapter_id'),
    ('sprite-sheet-v1', 'adapter-url', 'ValueError: invalid media adapter_id'),
    ('sprite-sheet-v1', 'account-empty', 'ValueError: invalid media account_id'),
]

# (A, B): A's valid resource_binding presented under B's otherwise-valid identity is ACCEPTED.  Diagonal = same adapter.
# Off-diagonal pairs are cross-adapter overlaps (findings).
MATRIX_ACCEPTED = {
    ('comfyui', 'comfyui'),
    ('comfyui-flac-v1', 'comfyui-flac-v1'),
    ('comfyui-material-v1', 'comfyui-material-v1'),
    ('sprixen-sprite-v1', 'sprixen-sprite-v1'),
    ('sprixen-animation-v1', 'sprixen-animation-v1'),
    ('meshy-preview-v1', 'meshy-preview-v1'),
    ('meshy-refine-v1', 'meshy-refine-v1'),
    ('meshy-rig-v1', 'meshy-rig-v1'),
    ('tripo-text3d-v1', 'tripo-text3d-v1'),
    ('fal-flux-schnell-v1', 'fal-flux-schnell-v1'),
    ('voidless-image-v1', 'voidless-image-v1'),
    ('fal-hunyuan-image3d-v1', 'fal-hunyuan-image3d-v1'),
    ('trellis-image3d-v1', 'trellis-image3d-v1'),
    ('hunyuan3d-v2-shape-v1', 'hunyuan3d-v2-shape-v1'),
    ('screen-record-v1', 'screen-record-v1'),
    ('latex-pdflatex-v1', 'latex-pdflatex-v1'),
    ('blender-glb-png-v1', 'blender-glb-png-v1'),
    ('blender-glb-png-v1', 'blender-glb-four-heading-v1'),  # overlap
    ('blender-glb-four-heading-v1', 'blender-glb-png-v1'),  # overlap
    ('blender-glb-four-heading-v1', 'blender-glb-four-heading-v1'),
    ('material-pack-v1', 'material-pack-v1'),
    ('blender-material-bake-v1', 'blender-material-bake-v1'),
    ('sprite-sheet-v1', 'sprite-sheet-v1'),
}

CHILD_ROWS = [
    ('trellis-image3d-v1', 'seed-neg', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'seed-max', 'ValueError: local child admission unavailable'),
    ('trellis-image3d-v1', 'seed-max+1', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'seed-bool', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'recipe-wrong', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'rev-0', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'rev-bool', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'hash-upper', 'ValueError: invalid media digest'),
    ('trellis-image3d-v1', 'extra-key', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'drop-artifact_authority', 'ValueError: invalid TRELLIS child'),
    ('trellis-image3d-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('hunyuan3d-v2-shape-v1', 'seed-neg', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'seed-max', 'ValueError: local child admission unavailable'),
    ('hunyuan3d-v2-shape-v1', 'seed-max+1', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'seed-bool', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'recipe-wrong', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'rev-0', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'rev-bool', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'device-bad', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'device-cuda', 'ValueError: local child admission unavailable'),
    ('hunyuan3d-v2-shape-v1', 'hash-upper', 'ValueError: invalid media digest'),
    ('hunyuan3d-v2-shape-v1', 'extra-key', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'drop-artifact_authority', 'ValueError: invalid Hunyuan child'),
    ('hunyuan3d-v2-shape-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('blender-material-bake-v1', 'recipe-wrong', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'hash-upper', 'ValueError: invalid media digest'),
    ('blender-material-bake-v1', 'extra-key', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'drop-artifact_authority', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('blender-material-bake-v1', 'resolution-128', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'resolution-2048', 'ValueError: local child admission unavailable'),
    ('blender-material-bake-v1', 'exe-version-4.6', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'exe-version-4.5.', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'exe-version-4.5.1x', 'ValueError: invalid bake child profile'),
    ('blender-material-bake-v1', 'bake-recipe', 'ValueError: invalid bake child profile'),
    ('material-pack-v1', 'extra-key', 'ValueError: invalid material child profile'),
    ('material-pack-v1', 'drop-artifact_authority', 'ValueError: invalid material child profile'),
    ('material-pack-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('material-pack-v1', 'hashes-2', 'ValueError: invalid material child profile'),
    ('material-pack-v1', 'hashes-4', 'ValueError: local child admission unavailable'),
    ('material-pack-v1', 'hashes-5', 'ValueError: invalid material child profile'),
    ('material-pack-v1', 'hashes-0', 'ValueError: invalid material child profile'),
    ('material-pack-v1', 'hashes-not-list', 'ValueError: invalid material child profile'),
    ('sprite-sheet-v1', 'extra-key', 'ValueError: invalid local child profile'),
    ('sprite-sheet-v1', 'drop-artifact_authority', 'ValueError: invalid local child profile'),
    ('sprite-sheet-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('sprite-sheet-v1', 'hashes-2', 'ValueError: invalid local child profile'),
    ('sprite-sheet-v1', 'hashes-4', 'ValueError: local child admission unavailable'),
    ('sprite-sheet-v1', 'hashes-5', 'ValueError: invalid local child profile'),
    ('sprite-sheet-v1', 'hashes-0', 'ValueError: invalid local child profile'),
    ('sprite-sheet-v1', 'hashes-not-list', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'extra-key', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'drop-artifact_authority', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'hashes-2', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'hashes-4', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'hashes-5', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'hashes-0', 'ValueError: invalid local child profile'),
    ('sprite-sheet-crops-v1', 'hashes-not-list', 'ValueError: invalid local child profile'),
    ('trim-webm-v1', 'hash-upper', 'ValueError: invalid media digest'),
    ('trim-webm-v1', 'extra-key', 'ValueError: invalid trim child profile'),
    ('trim-webm-v1', 'drop-artifact_authority', 'ValueError: invalid trim child profile'),
    ('trim-webm-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('trim-webm-v1', 'trim-end<=start', 'ValueError: invalid trim child profile'),
    ('trim-webm-v1', 'trim-61s', 'ValueError: invalid trim child profile'),
    ('trim-webm-v1', 'trim-60s', 'ValueError: local child admission unavailable'),
    ('trim-webm-v1', 'trim-day', 'ValueError: invalid trim child profile'),
    ('trim-webm-v1', 'trim-neg', 'ValueError: invalid trim child profile'),
    ('trim-webm-v1', 'trim-float', 'ValueError: invalid trim child profile'),
    ('latex-pdflatex-v1', 'extra-key', 'ValueError: invalid LaTeX child profile'),
    ('latex-pdflatex-v1', 'drop-artifact_authority', 'ValueError: invalid LaTeX child profile'),
    ('latex-pdflatex-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('latex-pdflatex-v1', 'exe-version-4.6', 'ValueError: local child admission unavailable'),
    ('latex-pdflatex-v1', 'exe-version-4.5.', 'ValueError: local child admission unavailable'),
    ('latex-pdflatex-v1', 'exe-version-4.5.1x', 'ValueError: local child admission unavailable'),
    ('latex-pdflatex-v1', 'hashes-2', 'ValueError: local child admission unavailable'),
    ('latex-pdflatex-v1', 'hashes-4', 'ValueError: local child admission unavailable'),
    ('latex-pdflatex-v1', 'hashes-5', 'ValueError: local child admission unavailable'),
    ('latex-pdflatex-v1', 'hashes-0', 'ValueError: invalid LaTeX child profile'),
    ('latex-pdflatex-v1', 'hashes-not-list', 'ValueError: invalid LaTeX child profile'),
    ('blender-glb-png-v1', 'hash-upper', 'ValueError: invalid media digest'),
    ('blender-glb-png-v1', 'extra-key', 'ValueError: invalid local child profile'),
    ('blender-glb-png-v1', 'drop-artifact_authority', 'ValueError: invalid local child profile'),
    ('blender-glb-png-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('blender-glb-png-v1', 'exe-version-4.6', 'ValueError: local child admission unavailable'),
    ('blender-glb-png-v1', 'exe-version-4.5.', 'ValueError: local child admission unavailable'),
    ('blender-glb-png-v1', 'exe-version-4.5.1x', 'ValueError: local child admission unavailable'),
    ('blender-glb-four-heading-v1', 'hash-upper', 'ValueError: invalid media digest'),
    ('blender-glb-four-heading-v1', 'extra-key', 'ValueError: invalid local child profile'),
    ('blender-glb-four-heading-v1', 'drop-artifact_authority', 'ValueError: invalid local child profile'),
    ('blender-glb-four-heading-v1', 'adapter-other', 'ValueError: invalid local child profile'),
    ('blender-glb-four-heading-v1', 'exe-version-4.6', 'ValueError: local child admission unavailable'),
    ('blender-glb-four-heading-v1', 'exe-version-4.5.', 'ValueError: local child admission unavailable'),
    ('blender-glb-four-heading-v1', 'exe-version-4.5.1x', 'ValueError: local child admission unavailable'),
]

CHILD_IDENTITY_ROWS = {
    ('trellis-image3d-v1', 'conn-other'): 'ValueError: TRELLIS child identity changed',
    ('trellis-image3d-v1', 'rev-2'): 'ValueError: TRELLIS child identity changed',
    ('trellis-image3d-v1', 'owner-other'): 'ValueError: TRELLIS child identity changed',
    ('trellis-image3d-v1', 'manifest-other'): 'ValueError: TRELLIS child identity changed',
    ('trellis-image3d-v1', 'seed-8'): 'ValueError: TRELLIS child identity changed',
    ('trellis-image3d-v1', 'input-other'): 'ValueError: local child admission unavailable',
    ('hunyuan3d-v2-shape-v1', 'conn-other'): 'ValueError: Hunyuan child identity changed',
    ('hunyuan3d-v2-shape-v1', 'rev-2'): 'ValueError: Hunyuan child identity changed',
    ('hunyuan3d-v2-shape-v1', 'owner-other'): 'ValueError: Hunyuan child identity changed',
    ('hunyuan3d-v2-shape-v1', 'manifest-other'): 'ValueError: Hunyuan child identity changed',
    ('hunyuan3d-v2-shape-v1', 'seed-8'): 'ValueError: Hunyuan child identity changed',
    ('hunyuan3d-v2-shape-v1', 'input-other'): 'ValueError: local child admission unavailable',
    ('hunyuan3d-v2-shape-v1', 'device-cuda'): 'ValueError: Hunyuan child identity changed',
}

# Adapter ids: the catalog (base.py) has 24; the validation code names 23 of them.  'comfyui' is never named in the
# validated functions: it only reaches reserve_media's generic (comfyui-workflow-v1) fall-through branch.
CATALOG_NOT_NAMED_IN_VALIDATION = set()  # B4-FIX 3: reserve_media now names 'comfyui' (was {'comfyui'})
# strings of adapter-id shape in the validated functions that are NOT catalog adapters (interfaces, recipes, protocols, model revisions)
NAMED_NOT_IN_CATALOG = {'blender-material-channels-v1', 'blender-render-v1', 'comfyui-workflow-v1', 'factory-default-gltf-v1',
                        'fixed-material-bake-v1', 'fixed-v1', 'hunyuan3d-v2-image-glb-v1', 'record-v3', 'record-v5',
                        'record-v6', 'record-v7', 'trellis-image-glb-v1'}

# --------------------------------------------------------------------------
# Stubs and runner
# --------------------------------------------------------------------------
@contextlib.contextmanager
def _nolock():
    yield


def with_store(rows, fn):
    """Run fn with the job store replaced by the plain dict `rows`."""
    old = (bj._store_lock, bj._load, bj._save)
    bj._store_lock, bj._load, bj._save = _nolock, (lambda: rows), (lambda jobs: None)
    try:
        return fn()
    finally:
        bj._store_lock, bj._load, bj._save = old


def outcome(fn, *a, **k):
    """'ok' / repr of a returned value, or 'ExcType: message' for a raise."""
    try:
        r = fn(*a, **k)
    except Exception as e:
        return f'{type(e).__name__}: {e}'
    return r


def reserve(identity):
    r = outcome(lambda: with_store({}, lambda: bj.reserve_media(copy.deepcopy(identity))))
    return 'ok' if isinstance(r, dict) else r


ROWS = {'n': 0}


def count(n=1):
    ROWS['n'] += n


# --------------------------------------------------------------------------
# Catalog of adapter ids (neutral catalog, base.py)
# --------------------------------------------------------------------------
CATALOG_IDS = [name for name, _ in base._MEDIA_OPERATION_FACTS]
ALL_IDS = list(CATALOG_IDS)

# Output-name suffix demanded by reserve_media, by adapter id (default .png).
SUFFIX = {a: '.png' for a in ALL_IDS}
SUFFIX.update({'screen-record-v1': '.webm', 'trim-webm-v1': '.webm', 'latex-pdflatex-v1': '.pdf',
               'comfyui-flac-v1': '.flac'})
for _a in ('meshy-preview-v1', 'meshy-refine-v1', 'tripo-text3d-v1', 'fal-hunyuan-image3d-v1',
           'trellis-image3d-v1', 'hunyuan3d-v2-shape-v1', 'meshy-rig-v1'):
    SUFFIX[_a] = '.glb'
for _a in ('material-pack-v1', 'blender-material-bake-v1', 'comfyui-material-v1'):
    SUFFIX[_a] = '.zip'

# --------------------------------------------------------------------------
# Valid identity builders, one per adapter id
# --------------------------------------------------------------------------
def _ident(adapter, op, model, kind, route=ROUTE, hashes=()):
    return dict(account_id=ACCT, owner='o', session_id='s', workspace_id='w', actor_id='a', turn_id='t',
                root_operation_id='root', attempt_id='att', request_digest=H('a'), adapter_id=adapter,
                adapter_version='1', operation=op, route_id=route, route_kind=kind, model_revision=model,
                policy_generation=0, input_hashes=list(hashes))


def _image(sha, mime='image/png'):
    return {'ref': 'r', 'resourceID': 'res', 'revision': dict(REV), 'sha256': sha, 'mime_type': mime,
            'width': 64, 'height': 64, 'name': 'n.png'}


def _remote(iface, adapter, **extra):
    b = {'connectionID': ROUTE, 'connectionRevision': 1, 'interface': iface, 'ownerAccountID': ACCT,
         'providerAccountID': 'prov', 'providerAccountRevision': 1, 'credentialVersion': 1,
         'destination_ref': 'dref', 'destination_revision': dict(REV), 'destination_name': 'out' + SUFFIX[adapter],
         'project_ref': None, 'project_revision': None}
    b.update(extra)
    return b


def _local_image(iface, adapter, **extra):
    b = {'connectionID': ROUTE, 'connectionRevision': 1, 'interface': iface, 'ownerAccountID': ACCT,
         'manifestSHA256': H('b'), 'image_source': _image(H('c')), 'seed': 7, 'destination_ref': 'dref',
         'destination_revision': dict(REV), 'destination_name': 'out' + SUFFIX[adapter],
         'project_ref': None, 'project_revision': None}
    b.update(extra)
    return b


def _blender(adapter, **extra):
    b = {'connectionID': ROUTE, 'connectionRevision': 1, 'interface': 'blender-render-v1', 'ownerAccountID': ACCT,
         'source_ref': 'sref', 'source_revision': dict(REV), 'source_sha256': H('d'), 'destination_ref': 'dref',
         'destination_revision': dict(REV), 'destination_name': 'out' + SUFFIX[adapter],
         'project_ref': None, 'project_revision': None}
    b.update(extra)
    return b


def _comfy(adapter, **extra):
    b = {'connectionID': ROUTE, 'connectionRevision': 0, 'interface': 'comfyui-workflow-v1', 'ownerAccountID': ACCT,
         'recipe_ref': 'rref', 'recipe_revision': dict(REV), 'recipe_sha256': H('e'), 'destination_ref': 'dref',
         'destination_revision': dict(REV), 'destination_name': 'out' + SUFFIX[adapter],
         'project_ref': None, 'project_revision': None}
    b.update(extra)
    return b


def _mat_input(role, i):
    return {'role': role, 'ref': f'ref{i}', 'resourceID': f'res{i}', 'revision': dict(REV), 'sha256': H('123456789abc'[i]),
            'mime_type': 'image/png', 'width': 64, 'height': 64, 'name': role + '.png'}


def _sprite_inputs():
    return [{'ref': f'ref{i}', 'resourceID': f'res{i}', 'revision': dict(REV), 'sha256': H('1234'[i])} for i in range(4)]


def valid(adapter):
    """A valid identity (with binding when the adapter takes one) for adapter, or None if not built here."""
    sfx = SUFFIX[adapter]
    if adapter in ('sprixen-sprite-v1', 'meshy-preview-v1', 'tripo-text3d-v1', 'fal-flux-schnell-v1'):
        op = {'sprixen-sprite-v1': 'image.generate', 'meshy-preview-v1': 'mesh.generate',
              'tripo-text3d-v1': 'mesh.generate', 'fal-flux-schnell-v1': 'image.generate'}[adapter]
        i = _ident(adapter, op, 'm1', 'remote')
        i['resource_binding'] = _remote(adapter, adapter)
        return i
    if adapter == 'voidless-image-v1':
        i = _ident(adapter, 'image.generate', 'flux2-klein', 'remote')
        i['resource_binding'] = _remote(adapter, adapter)
        return i
    if adapter == 'sprixen-animation-v1':
        i = _ident(adapter, 'animation.generate', 'standard', 'remote', hashes=[H('f')])
        i['resource_binding'] = _remote('sprixen-sprite-v1', adapter, projectID='proj', spriteID='spr', sourceSHA256=H('f'),
                                        animationParameters={'type': 'idle', 'frameCount': 4, 'fps': 12})
        return i
    if adapter == 'meshy-refine-v1':
        i = _ident(adapter, 'mesh.refine', 'm1', 'remote', hashes=[H('f')])
        i['resource_binding'] = _remote('meshy-preview-v1', adapter, sourceJobID='j', sourceRevision=1, sourceTaskID='t',
                                        sourceArtifactID='a', sourceSHA256=H('f'))
        return i
    if adapter == 'meshy-rig-v1':
        i = _ident(adapter, 'mesh.rig', 'meshy-rig-v1', 'remote', hashes=[H('f')])
        i['resource_binding'] = _remote('meshy-preview-v1', adapter, source_ref='sref', source_revision=dict(REV), source_sha256=H('f'))
        return i
    if adapter == 'fal-hunyuan-image3d-v1':
        i = _ident(adapter, 'mesh.generate', 'fal-ai/hunyuan3d/v2', 'remote', hashes=[H('f')])
        i['resource_binding'] = _remote('fal-flux-schnell-v1', adapter, image_source=_image(H('f')))
        return i
    if adapter == 'trellis-image3d-v1':
        i = _ident(adapter, 'mesh.generate', 'trellis-image-glb-v1', 'local', hashes=[H('c')])
        i['resource_binding'] = _local_image(adapter, adapter)
        return i
    if adapter == 'hunyuan3d-v2-shape-v1':
        i = _ident(adapter, 'mesh.generate', 'hunyuan3d-v2-image-glb-v1', 'local', hashes=[H('c')])
        i['resource_binding'] = _local_image(adapter, adapter, device='cpu-fp32')
        return i
    if adapter in ('blender-glb-png-v1', 'blender-glb-four-heading-v1'):
        i = _ident(adapter, 'image.render', 'm1', 'local', hashes=[H('d')])
        i['resource_binding'] = _blender(adapter)
        return i
    if adapter == 'blender-material-bake-v1':
        i = _ident(adapter, 'material.bake', 'fixed-material-bake-v1', 'local', hashes=[H('d')])
        i['resource_binding'] = _blender(adapter, resolution=512)
        return i
    if adapter in ('comfyui', 'comfyui-flac-v1'):
        i = _ident(adapter, 'audio.generate' if adapter.endswith('flac-v1') else 'image.generate', 'm1', 'remote')
        i['resource_binding'] = _comfy(adapter)
        return i
    if adapter == 'comfyui-material-v1':
        i = _ident(adapter, 'material.generate', 'm1', 'remote')
        i['resource_binding'] = _comfy(adapter, recipe_resource_id='rid', material_resolution=512, material_outputs={
            'baseColor': {'node_id': 'n1', 'index': 0}, 'roughness': {'node_id': 'n2', 'index': 0},
            'metallic': {'node_id': 'n3', 'index': 0}})
        return i
    if adapter == 'material-pack-v1':
        ins = [_mat_input(r, n) for n, r in enumerate(('baseColor', 'roughness', 'metallic'))]
        i = _ident(adapter, 'material.pack', 'fixed-v1', 'local', route=adapter, hashes=[v['sha256'] for v in ins])
        i['resource_binding'] = {'interface': adapter, 'ownerAccountID': ACCT, 'inputs': ins, 'manifest_sha256': H('9'),
                                 'destination_ref': 'dref', 'destination_revision': dict(REV), 'destination_name': 'out.zip'}
        return i
    if adapter == 'sprite-sheet-v1':
        ins = _sprite_inputs()
        i = _ident(adapter, 'image.pack', 'fixed-v1', 'local', route=adapter, hashes=[v['sha256'] for v in ins])
        i['resource_binding'] = {'interface': adapter, 'ownerAccountID': ACCT, 'inputs': ins, 'cellSize': 32,
                                 'annotations': {'fps': 12, 'facing': None, 'pivots': [{'x': 1, 'y': 1}] * 4, 'palette': None, 'style': None},
                                 'destination_ref': 'dref', 'destination_revision': dict(REV), 'destination_name': 'out.png'}
        return i
    if adapter == 'latex-pdflatex-v1':
        i = _ident(adapter, 'document.latex.compile', 'fixed-v1', 'local', route=adapter, hashes=[H('7')])
        i['resource_binding'] = {'interface': adapter, 'ownerAccountID': ACCT, 'compiler_sha256': H('6'),
                                 'inputs': [{'ref': 'r', 'resourceID': 'res', 'revision': dict(REV), 'sha256': H('7'), 'relative_name': 'main.tex'}],
                                 'destination_ref': 'dref', 'destination_revision': dict(REV), 'destination_name': 'out.pdf'}
        return i
    if adapter == 'screen-record-v1':
        i = _ident(adapter, 'screen.record', 'fixed-v1', 'local', route=adapter)
        i['resource_binding'] = {'interface': adapter, 'ownerAccountID': ACCT, 'destination_ref': 'dref',
                                 'destination_revision': dict(REV), 'destination_name': 'out.webm', 'helper_sha256': H('5'),
                                 'limits': {'duration_ms': 60000, 'fps': 15, 'max_width': 1920, 'max_height': 1080, 'max_bytes': 64 * MIB}}
        return i
    return None  # trim-webm-v1, blender-glb-rig-clip-v1, sprite-sheet-crops-v1: contract-module witnesses; see "not pinned"


# Identity shell (no binding) for adapters `valid` does not build, so they can still be targets.
SHELL = {'trim-webm-v1': ('video.trim', 'fixed-v1', 'local', 'trim-webm-v1'),
         'blender-glb-rig-clip-v1': ('image.render', 'm1', 'local', ROUTE),
         'sprite-sheet-crops-v1': ('image.pack-crops', 'fixed-v1', 'local', 'sprite-sheet-crops-v1')}


def shell(adapter):
    v = valid(adapter)
    if v is not None:
        v = copy.deepcopy(v)
        v.pop('resource_binding', None)
        return v
    op, model, kind, route = SHELL[adapter]
    return _ident(adapter, op, model, kind, route=route)

# --------------------------------------------------------------------------
# Mutations: (label, function(identity) -> True if applicable). Applied to a deep copy of valid(adapter).
# --------------------------------------------------------------------------
def _b(i):
    return i.get('resource_binding')


def _setb(key, value):
    def f(i):
        b = _b(i)
        if type(b) is not dict or key not in b:
            return False
        b[key] = value
        return True
    return f


def _dropb(key):
    def f(i):
        b = _b(i)
        if type(b) is not dict or key not in b:
            return False
        del b[key]
        return True
    return f


def _seti(key, value):
    def f(i):
        i[key] = value
        return True
    return f


def _iface_other(i):
    b = _b(i)
    if type(b) is not dict:
        return False
    b['interface'] = 'sprixen-sprite-v1' if b['interface'] != 'sprixen-sprite-v1' else 'meshy-preview-v1'
    return True


def _iface_upper(i):
    b = _b(i)
    b['interface'] = b['interface'].upper()
    return True


def _extra_key(i):
    _b(i)['zz_extra'] = 1
    return True


def _inputs_extra(i):
    i['input_hashes'] = i['input_hashes'] + [H('0')]
    return True


def _inputs_empty(i):
    i['input_hashes'] = []
    return True


def _route_flip(i):
    i['route_kind'] = 'remote' if i['route_kind'] == 'local' else 'local'
    return True


def _digest_upper(i):
    i['request_digest'] = H('A')
    return True


def _name_suffix(i):
    b = _b(i)
    if type(b) is not dict or 'destination_name' not in b:
        return False
    b['destination_name'] = 'out.exe'
    return True


MUTATIONS = [
    ('iface-other', _iface_other), ('iface-upper', _iface_upper), ('iface-empty', _setb('interface', '')),
    ('iface-int', _setb('interface', 5)),
    ('binding-none', _seti('resource_binding', None)), ('binding-list', _seti('resource_binding', [])),
    ('binding-str', _seti('resource_binding', 'x')),
    ('binding-absent', lambda i: bool(i.pop('resource_binding', None) is not None)),
    ('binding-extra-key', _extra_key),
    ('drop-ownerAccountID', _dropb('ownerAccountID')), ('drop-destination_ref', _dropb('destination_ref')),
    ('drop-project_ref', _dropb('project_ref')),
    ('conn-mismatch', _setb('connectionID', 'other')), ('owner-mismatch', _setb('ownerAccountID', 'acct2')),
    ('rev-0', _setb('connectionRevision', 0)), ('rev-bool', _setb('connectionRevision', True)),
    ('rev-str', _setb('connectionRevision', '1')),
    ('project-set', _setb('project_ref', 'p')),
    ('name-suffix', _name_suffix), ('name-slash', _setb('destination_name', 'a/b' )),
    ('name-dotdot', _setb('destination_name', '..')), ('name-url', _setb('destination_ref', 'http://evil.test/x')),
    ('dest-rev-extra', _setb('destination_revision', {'kind': 'k', 'value': 'v', 'z': 1})),
    ('version-2', _seti('adapter_version', '2')), ('version-int', _seti('adapter_version', 1)),
    ('route-flip', _route_flip), ('op-wrong', _seti('operation', 'bogus.op')),
    ('model-wrong', _seti('model_revision', 'bogus-model')),
    ('digest-upper', _digest_upper), ('inputs-extra', _inputs_extra), ('inputs-empty', _inputs_empty),
    ('adapter-empty', _seti('adapter_id', '')), ('adapter-int', _seti('adapter_id', 5)),
    ('adapter-url', _seti('adapter_id', 'https://x.test/a')),
    ('account-empty', _seti('account_id', '')),
    ('seed-neg', _setb('seed', -1)), ('seed-max+1', _setb('seed', 2147483648)), ('seed-max', _setb('seed', 2147483647)),
    ('device-bad', _setb('device', 'cuda-fp32')), ('device-cuda', _setb('device', 'cuda-fp16')),
    ('resolution-bad', _setb('resolution', 128)), ('resolution-2048', _setb('resolution', 2048)),
    ('mat-res-bad', _setb('material_resolution', 100)),
    ('source-mime-jpeg', lambda i: (_b(i).get('image_source') is not None and _b(i)['image_source'].__setitem__('mime_type', 'image/jpeg')) is not False and 'image_source' in _b(i)),
    ('source-size-0', lambda i: 'image_source' in (_b(i) or {}) and not _b(i)['image_source'].__setitem__('width', 0)),
    ('source-size-4097', lambda i: 'image_source' in (_b(i) or {}) and not _b(i)['image_source'].__setitem__('width', 4097)),
    ('source-hash-changed', lambda i: 'image_source' in (_b(i) or {}) and not _b(i)['image_source'].__setitem__('sha256', H('0'))),
    ('anim-type-bad', lambda i: 'animationParameters' in (_b(i) or {}) and not _b(i)['animationParameters'].__setitem__('type', 'fly')),
    ('anim-frames-13', lambda i: 'animationParameters' in (_b(i) or {}) and not _b(i)['animationParameters'].__setitem__('frameCount', 13)),
    ('anim-fps-3', lambda i: 'animationParameters' in (_b(i) or {}) and not _b(i)['animationParameters'].__setitem__('fps', 3)),
    ('anim-project-slash', _setb('projectID', 'a/b')), ('anim-sprite-129', _setb('spriteID', 'a' * 129)),
    ('source-sha-not-input', _setb('sourceSHA256', H('0'))), ('rig-sha-not-input', _setb('source_sha256', H('0'))),
]

# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------
LAYOUT_KINDS = {'blender-glb-four-heading-v1', 'blender-glb-rig-clip-v1', 'sprite-sheet-v1', 'sprite-sheet-crops-v1'}
MIME_ERR = 'ValueError: invalid media artifact MIME'


def art(adapter, mime, size):
    a = {'artifact_id': 'art', 'sha256': H('a'), 'mime_type': mime, 'size': size}
    if adapter == 'comfyui-flac-v1':
        a['audio'] = {'sampleRate': 44100, 'channels': 2, 'bitsPerSample': 16, 'declaredSamples': 1000, 'payloadVerified': False}
    if adapter == 'sprixen-animation-v1':
        a['animation'] = {'animationID': 'anim_x', 'type': 'idle', 'frameCount': 4, 'fps': 12, 'width': 64, 'height': 64, 'sha256': H('a')}
    if adapter in LAYOUT_KINDS:
        a['layout'] = {}
    return a


def artifact_outcome(adapter, version, mime, size):
    rec = {'identity': {'adapter_id': adapter, 'adapter_version': version, 'operation': base.media_operation_facts(adapter, '1').operation,
                        'resource_binding': {'animationParameters': {'type': 'idle', 'frameCount': 4, 'fps': 12}}},
           'remote_task_id': 'anim_x'}
    r = outcome(bj._media_artifact_for, rec, art(adapter, mime, size))
    return 'ok' if isinstance(r, dict) else r


def run_artifact_caps():
    for adapter, fmime, fmib, lmime, lmib in ARTIFACT_CAPS:
        for version, mime, mib in (('1', fmime, fmib), ('2', lmime, lmib)):  # '2' => no facts => legacy fallback
            cap = mib * MIB
            if adapter not in LAYOUT_KINDS:
                good = 'ok'  # BQ-20: version '2' (no declaration) keeps the snapshot legacy fallback
                assert artifact_outcome(adapter, version, mime, cap) == good, (adapter, version, 'cap accepted')
                assert artifact_outcome(adapter, version, mime, 1) == good, (adapter, version, 'size 1')
                count(2)
            # above the hard 256 MiB shape cap the earlier generic check fires instead of the per-adapter cap
            over = MIME_ERR if cap + 1 <= 268435456 else 'ValueError: invalid media artifact'
            assert artifact_outcome(adapter, version, mime, cap + 1) == over, (adapter, version, 'cap+1')
            assert artifact_outcome(adapter, version, mime + '-x', 1) == MIME_ERR, (adapter, version, 'mime')
            assert artifact_outcome(adapter, version, 'text/html', 1) == MIME_ERR, (adapter, version, 'html')
            assert artifact_outcome(adapter, version, mime.upper(), 1) == MIME_ERR, (adapter, version, 'case')
            count(4)
            for bad in (0, -1, True, '5', None, 268435457):
                r = artifact_outcome(adapter, version, mime, bad)
                assert r == 'ValueError: invalid media artifact', (adapter, version, bad, r)
                count()


def _artifact_shape_rows():
    # field-set / type refusals independent of adapter (adapter: tripo, facts path)
    rec = {'identity': {'adapter_id': 'tripo-text3d-v1', 'adapter_version': '1', 'operation': 'mesh.generate'}}
    good = art('tripo-text3d-v1', 'model/gltf-binary', 10)
    assert outcome(bj._media_artifact_for, rec, good)['size'] == 10
    for label, mut in (('extra-field', lambda a: a.update(x=1)), ('missing-sha', lambda a: a.pop('sha256')),
                       ('sha-upper', lambda a: a.update(sha256=H('A'))), ('sha-short', lambda a: a.update(sha256='a' * 63)),
                       ('id-url', lambda a: a.update(artifact_id='http://x.test/a')), ('id-empty', lambda a: a.update(artifact_id='')),
                       ('id-ctl', lambda a: a.update(artifact_id='a\nb')), ('mime-url', lambda a: a.update(mime_type='x://y'))):
        a = copy.deepcopy(good); mut(a)
        r = outcome(bj._media_artifact_for, rec, a)
        assert r == ARTIFACT_SHAPE[label], (label, r)
        count()
    assert outcome(bj._media_artifact_for, rec, [])  == 'ValueError: invalid media artifact'
    count()


def tid_outcome(fn_name, adapter, tid, local=False):
    """Run finish_media (remote id) or record_media_submission on a stub row; returns True/False/'Exc: msg'."""
    route = 'local' if local else valid(adapter)['route_kind'] if valid(adapter) else shell(adapter)['route_kind']
    rec = {'id': 'j1', 'kind': 'media', 'version': 1, 'account_id': ACCT, 'revision': 1, 'status': 'submitting',
           'submission_token': 'tok', 'claim': {'action': 'status', 'token': 'tok'}, 'remote_task_id': None,
           'identity': {'adapter_id': adapter, 'route_kind': route}}
    rows = {'j1': rec}
    if fn_name == 'finish':
        return outcome(lambda: with_store(rows, lambda: bj.finish_media('j1', account_id=ACCT, token='tok', status='queued', remote_task_id=tid)))
    if fn_name == 'finish-local':
        return outcome(lambda: with_store(rows, lambda: bj.finish_media('j1', account_id=ACCT, token='tok', status='queued', local_process_job_id=tid)))
    return outcome(lambda: with_store(rows, lambda: bj.record_media_submission('j1', account_id=ACCT, token='tok', remote_task_id=tid)))


REMOTE_IDS = [a for a in ALL_IDS if (valid(a) or shell(a))['route_kind'] == 'remote']
LOCAL_IDS = [a for a in ALL_IDS if a not in REMOTE_IDS]


def run_task_ids():
    for fn in ('finish', 'submission'):
        for tid, generic in GENERIC_TASK_IDS:           # (id, expected for every remote adapter except animation)
            for a in REMOTE_IDS:
                if a == 'sprixen-animation-v1':
                    continue
                assert tid_outcome(fn, a, tid) == generic, (fn, a, tid, tid_outcome(fn, a, tid))
                count()
        for tid, expected in ANIMATION_TASK_IDS:         # sprixen-animation-v1 only
            r = tid_outcome(fn, 'sprixen-animation-v1', tid)
            assert r == expected, (fn, tid, r)
            count()
    # remote ids refused on local-route adapters; local ids refused on remote-route adapters
    for a in LOCAL_IDS:
        assert tid_outcome('finish', a, 'task-1') is False and tid_outcome('submission', a, 'task-1') is False, a
        assert tid_outcome('finish-local', a, 'job-1.x:y@z') is True and tid_outcome('finish-local', a, 'a/b') is False, a
        count(4)
    for a in REMOTE_IDS:
        assert tid_outcome('finish-local', a, 'job-1') is False, a
        count()


def run_task_id_matrix():
    refused = set()
    for a_src, tid in VENDOR_TASK_IDS.items():
        for b in REMOTE_IDS:
            for fn in ('finish', 'submission'):
                r = tid_outcome(fn, b, tid)
                assert r in (True, False), (fn, a_src, b, r)
                if r is False:
                    refused.add((a_src, b))
                count()
    assert refused == TASK_ID_REFUSED, sorted(refused ^ TASK_ID_REFUSED)


def run_second_task_id():
    # a second, different id on the same row is refused; the same id is accepted again
    rec = {'id': 'j1', 'kind': 'media', 'version': 1, 'account_id': ACCT, 'revision': 1, 'status': 'submitting',
           'submission_token': 'tok', 'claim': {'action': 'status', 'token': 'tok'}, 'remote_task_id': 'first',
           'identity': {'adapter_id': 'tripo-text3d-v1', 'route_kind': 'remote'}}
    f = lambda tid: with_store({'j1': copy.deepcopy(rec)}, lambda: bj.finish_media('j1', account_id=ACCT, token='tok', status='queued', remote_task_id=tid))
    assert f('first') is True and f('second') is False
    # None means 'no id given': finish_media then refuses (nothing to queue); record_media_submission raises
    assert tid_outcome('finish', 'tripo-text3d-v1', None) is False and tid_outcome('submission', 'tripo-text3d-v1', None) == E_TID
    count(4)


def claim_collect(adapter, version, artifact=None):
    rec = {'id': 'j1', 'kind': 'media', 'version': 1, 'account_id': ACCT, 'revision': 1, 'status': 'collecting', 'claim': None,
           'artifact': artifact, 'identity': {'adapter_id': adapter, 'adapter_version': version, 'operation': base.media_operation_facts(adapter, '1').operation}}
    r = outcome(lambda: with_store({'j1': rec}, lambda: bj.claim_media('j1', account_id=ACCT, revision=1, action='collect')))
    return r['record']['claim'].get('owned_lifetime') if isinstance(r, dict) else r


def run_owned_fetch():
    for a in ALL_IDS:
        assert claim_collect(a, '1') == ('owned-fetch-v1' if a in OWNED_FETCH_FACTS else None), (a, 'facts')
        # BQ-20: a record that is not a known declaration keeps the snapshot's caller-supplied legacy list (6 ids), not the catalog
        assert claim_collect(a, '2') == ('owned-fetch-v1' if a in LEGACY_OWNED_CLAIM else None), (a, 'legacy')
        assert claim_collect(a, '1', artifact={'x': 1}) is None, (a, 'artifact already present')
        count(3)
    assert claim_collect('voidless-image-v1', '1') == 'owned-fetch-v1'
    rec_identity = {'adapter_id': 'voidless-image-v1', 'adapter_version': '1', 'operation': 'bogus'}
    assert bj._media_owned_fetch(rec_identity, ()) is False   # BQ-20: wrong operation = not a known declaration; only the caller's list applies
    assert bj._media_owned_fetch(rec_identity, ('voidless-image-v1',)) is True
    assert bj._media_owned_fetch('voidless-image-v1', ('voidless-image-v1',)) is False   # non-dict identity
    assert bj._media_owned_fetch({'adapter_id': 'unknown-vendor-v1'}, ()) is False
    count(4)


def run_facts_lookup():
    for adapter, version, operation, expected in FACTS_ROWS:
        got = bj._media_facts({'adapter_id': adapter, 'adapter_version': version, 'operation': operation})
        assert (got.operation if got else None) == expected, (adapter, version, operation, got)
        count()
    for bad in (None, [], 'x', 5):
        assert bj._media_facts(bad) is None
        count()
    for a in ALL_IDS:
        kind = bj._media_foreground_kind({'adapter_id': a, 'adapter_version': '2', 'operation': 'x'})
        assert kind == ('model' if a in FOREGROUND_MODEL else 'cpu' if a in FOREGROUND_CPU else None), (a, kind)
        kind = bj._media_foreground_kind({'adapter_id': a, 'adapter_version': '1', 'operation': base.media_operation_facts(a, '1').operation})
        assert kind == ('model' if a in FOREGROUND_MODEL else 'cpu'), (a, kind)
        count(2)
    assert bj._media_foreground_kind({'adapter_id': 'nope-v1'}) is None and bj._media_foreground_kind('x') is None
    count(2)


def run_text_hash():
    for value, limit, expected in TEXT_ROWS:
        assert outcome(bj._media_text, value, 'f', limit) == expected, (value, limit)
        count()
    for value, expected in HASH_ROWS:
        assert outcome(bj._media_hash, value) == expected, (value,)
        count()


def run_reserve_valid():
    for a in ALL_IDS:
        v = valid(a)
        if v is not None:
            assert reserve(v) == 'ok', a
            count()
    # accepted records keep the cleaned identity and are 'prepared'
    r = with_store({}, lambda: bj.reserve_media(valid('tripo-text3d-v1')))
    assert r['status'] == 'prepared' and r['identity']['adapter_id'] == 'tripo-text3d-v1' and r['identity']['resource_binding']['interface'] == 'tripo-text3d-v1'
    count()


def run_reserve_no_binding():
    for a in ALL_IDS:
        assert reserve(shell(a)) == NO_BINDING_RESULT.get(a, 'ok'), (a, reserve(shell(a)))
        count()
    unknown = shell('tripo-text3d-v1'); unknown['adapter_id'] = 'unknown-vendor-v1'
    assert reserve(unknown) == 'ok'  # BQ-20: an id without a declaration is reserved as at the snapshot
    count()


def run_reserve_mutations():
    seen = set()
    for adapter, label, expected in BINDING_ROWS:
        i = copy.deepcopy(valid(adapter))
        assert dict(MUTATIONS)[label](i), (adapter, label, 'mutation not applicable')
        got = reserve(i)
        assert got == expected, (adapter, label, got)
        seen.add((adapter, label))
        count()


def run_reserve_matrix():
    accepted = set()
    for a in ALL_IDS:
        va = valid(a)
        if va is None:
            continue
        for b in ALL_IDS:
            i = shell(b)
            i['resource_binding'] = copy.deepcopy(va['resource_binding'])
            i['input_hashes'] = list(va['input_hashes'])
            if reserve(i) == 'ok':
                accepted.add((a, b))
            count()
    assert accepted == MATRIX_ACCEPTED, sorted(accepted ^ MATRIX_ACCEPTED)


def run_name_suffix():
    # destination_name suffix per adapter (only adapters whose binding reaches the suffix check)
    for a in ALL_IDS:
        v = valid(a)
        if v is None:
            continue
        b = v['resource_binding']
        b['destination_name'] = 'x' + SUFFIX[a].upper()
        assert reserve(v) == 'ok', (a, 'suffix is case-insensitive')
        for bad in ('x', 'x.exe', 'x' + SUFFIX[a] + '.exe', '.', '..', 'a/b' + SUFFIX[a], 'a\\b' + SUFFIX[a]):
            b['destination_name'] = bad
            assert reserve(v) != 'ok', (a, bad)
        count(8)
    # each suffix is wrong for the others: .glb name refused on a .png adapter and vice versa
    v = valid('blender-glb-png-v1'); v['resource_binding']['destination_name'] = 'x.glb'
    assert reserve(v) == 'ValueError: invalid media output name'
    v = valid('trellis-image3d-v1'); v['resource_binding']['destination_name'] = 'x.png'
    assert reserve(v) == 'ValueError: invalid media output name'
    count(2)


def run_ids_vs_catalog():
    src = '\n'.join(inspect.getsource(f) for f in (bj._media_artifact_for, bj._media_result_for, bj.reserve_media,
                    bj.claim_media, bj.reserve_media_local_child, bj.finish_media_local_child, bj.adopt_media_local_artifact,
                    bj.finish_media, bj._media_foreground_kind, bj.record_media_submission))
    named = set(re.findall(r"'([a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]+)'", src)) | ({'comfyui'} if "'comfyui'" in src else set())
    assert set(CATALOG_IDS) - named == CATALOG_NOT_NAMED_IN_VALIDATION, sorted(set(CATALOG_IDS) - named)
    assert named - set(CATALOG_IDS) == NAMED_NOT_IN_CATALOG, sorted(named - set(CATALOG_IDS))
    assert len(CATALOG_IDS) == 24 and len(set(CATALOG_IDS)) == 24
    count(3)

# --------------------------------------------------------------------------
# Local child admission / receipts (reserve_media_local_child, finish/adopt)
# --------------------------------------------------------------------------
ADMISSION_OK = 'ValueError: local child admission unavailable'   # profile accepted, empty store refuses admission


def child_profile(adapter):
    if adapter == 'trellis-image3d-v1':
        return {'adapter': adapter, 'recipe': 'trellis-image-glb-v1', 'connectionID': ROUTE, 'connectionRevision': 1, 'ownerAccountID': ACCT,
                'manifestSHA256': H('b'), 'interpreterSHA256': H('1'), 'interpreterVersion': '3.11', 'runnerSHA256': H('2'),
                'recipeSHA256': H('3'), 'artifact_authority': H('4'), 'input_sha256': H('c'), 'seed': 7}
    if adapter == 'hunyuan3d-v2-shape-v1':
        p = child_profile('trellis-image3d-v1')
        p.update(adapter=adapter, recipe='hunyuan3d-v2-image-glb-v1', helperSHA256=H('5'), device='cpu-fp32')
        return p
    if adapter == 'blender-material-bake-v1':
        return {'adapter': adapter, 'executable_sha256': H('1'), 'executable_version': '4.5.1', 'runner_sha256': H('2'), 'input_sha256': H('d'),
                'artifact_authority': H('4'), 'resolution': 512, 'recipe': 'blender-material-channels-v1'}
    if adapter == 'material-pack-v1':
        return {'adapter': adapter, 'input_hashes': [H('1'), H('2'), H('3')], 'artifact_authority': H('4'), 'manifest_sha256': H('9')}
    if adapter in ('sprite-sheet-v1', 'sprite-sheet-crops-v1'):
        return {'adapter': adapter, 'input_hashes': [H('1')] * (1 if adapter.endswith('crops-v1') else 4), 'artifact_authority': H('4')}
    if adapter == 'trim-webm-v1':
        return {'adapter': adapter, 'helper_sha256': H('1'), 'runner_sha256': H('2'), 'artifact_authority': H('4'), 'input_sha256': H('c'),
                'start_ms': 0, 'end_ms': 1000}
    if adapter == 'latex-pdflatex-v1':
        return {'adapter': adapter, 'executable_sha256': H('1'), 'executable_version': '3', 'runner_sha256': H('2'),
                'input_hashes': [H('7')], 'artifact_authority': H('4')}
    return {'adapter': adapter, 'executable_sha256': H('1'), 'executable_version': '4.5.1', 'runner_sha256': H('2'), 'input_sha256': H('d'),
            'artifact_authority': H('4')}


def child_outcome(profile, rows=None):
    r = outcome(lambda: with_store(rows if rows is not None else {}, lambda: bj.reserve_media_local_child('j1', account_id=ACCT, token='tok', profile=profile)))
    return 'ok' if isinstance(r, dict) else r


def _set(key, value):
    def f(p):
        if key not in p:
            return False
        p[key] = value
        return True
    return f


def _drop(key):
    def f(p):
        return p.pop(key, 'MISSING') != 'MISSING'
    return f


CHILD_MUTATIONS = dict([
    ('seed-neg', _set('seed', -1)), ('seed-max', _set('seed', 2147483647)), ('seed-max+1', _set('seed', 2147483648)), ('seed-bool', _set('seed', True)),
    ('recipe-wrong', _set('recipe', 'other-recipe')), ('rev-0', _set('connectionRevision', 0)), ('rev-bool', _set('connectionRevision', True)),
    ('device-bad', _set('device', 'cuda-fp32')), ('device-cuda', _set('device', 'cuda-fp16')),
    ('hash-upper', lambda p: [p.__setitem__(k, H('A')) for k in ('input_sha256', 'input_hashes') if k in p and k != 'input_hashes'] != []),
    ('extra-key', lambda p: not p.__setitem__('zz', 1)), ('drop-artifact_authority', _drop('artifact_authority')),
    ('adapter-other', _set('adapter', 'sprixen-sprite-v1')),
    ('resolution-128', _set('resolution', 128)), ('resolution-2048', _set('resolution', 2048)),
    ('exe-version-4.6', _set('executable_version', '4.6.0')), ('exe-version-4.5.', _set('executable_version', '4.5.')),
    ('exe-version-4.5.1x', _set('executable_version', '4.5.1x')), ('bake-recipe', _set('recipe', 'x')),
    ('hashes-2', _set('input_hashes', [H('1')] * 2)), ('hashes-4', _set('input_hashes', [H('1')] * 4)), ('hashes-5', _set('input_hashes', [H('1')] * 5)),
    ('hashes-0', _set('input_hashes', [])), ('hashes-not-list', _set('input_hashes', 'x')),
    ('trim-end<=start', lambda p: 'start_ms' in p and not p.update(start_ms=5, end_ms=5)),
    ('trim-61s', lambda p: 'start_ms' in p and not p.update(start_ms=0, end_ms=60001)), ('trim-60s', lambda p: 'start_ms' in p and not p.update(start_ms=0, end_ms=60000)),
    ('trim-day', lambda p: 'start_ms' in p and not p.update(start_ms=86399000, end_ms=86400001)),
    ('trim-neg', _set('start_ms', -1)), ('trim-float', _set('end_ms', 1000.0)),
])
CHILD_ADAPTERS = ['trellis-image3d-v1', 'hunyuan3d-v2-shape-v1', 'blender-material-bake-v1', 'material-pack-v1', 'sprite-sheet-v1',
                  'sprite-sheet-crops-v1', 'trim-webm-v1', 'latex-pdflatex-v1', 'blender-glb-png-v1', 'blender-glb-four-heading-v1']


def run_child_profiles():
    for a in CHILD_ADAPTERS:
        assert child_outcome(child_profile(a)) == ADMISSION_OK, a
        count()
    for a, label, expected in CHILD_ROWS:
        p = child_profile(a)
        assert CHILD_MUTATIONS[label](p), (a, label)
        assert child_outcome(p) == expected, (a, label, child_outcome(p))
        count()
    for bad in (None, [], 'x'):
        assert child_outcome(bad) == 'ValueError: invalid local child profile'
        count()


def run_child_identity():
    # stage two: profile must match the reserved row (trellis / hunyuan)
    for a in ('trellis-image3d-v1', 'hunyuan3d-v2-shape-v1'):
        ident = with_store({}, lambda: bj.reserve_media(valid(a)))['identity']
        row = lambda: {'j1': {'id': 'j1', 'kind': 'media', 'version': 1, 'account_id': ACCT, 'submission_token': 'tok', 'status': 'submitting',
                              'claim': {'token': 'tok'}, 'identity': copy.deepcopy(ident), 'identity_digest': 'dg', 'revision': 1}}
        assert child_outcome(child_profile(a), row()) == 'ok', a
        for label, mut in CHILD_IDENTITY_MUTATIONS:
            p = child_profile(a)
            if not mut(p):
                continue
            got = child_outcome(p, row())
            assert got == CHILD_IDENTITY_ROWS[(a, label)], (a, label, got)
            count()
        count()
        # local row only: a remote-route row refuses admission
        r = row(); r['j1']['identity']['route_kind'] = 'remote'
        assert child_outcome(child_profile(a), r) == ADMISSION_OK
        count()


CHILD_IDENTITY_MUTATIONS = [
    ('conn-other', _set('connectionID', 'other')), ('rev-2', _set('connectionRevision', 2)), ('owner-other', _set('ownerAccountID', 'acct2')),
    ('manifest-other', _set('manifestSHA256', H('0'))), ('seed-8', _set('seed', 8)), ('input-other', _set('input_sha256', H('0'))),
    ('device-cuda', _set('device', 'cuda-fp16')),
]


def receipt_row(adapter, state):
    return {'j1': {'id': 'j1', 'kind': 'media', 'version': 1, 'account_id': ACCT, 'revision': 1, 'submission_token': 'tok',
                   'identity_digest': 'dg', 'identity': {'adapter_id': adapter, 'adapter_version': '1', 'operation': base.media_operation_facts(adapter, '1').operation},
                   'local_child': {'account_id': ACCT, 'job_id': 'j1', 'child_id': 'c1', 'nonce': 'n1', 'state': state, 'claim_token': 'tok',
                                   'identity_digest': 'dg', 'profile': {}}}}


def local_receipt(size):
    return {'exit_code': 0, 'output_sha256': H('a'), 'output_size': size, 'artifact_id': 'art'}


def run_local_receipts():
    for adapter, cap_mib in LOCAL_RECEIPT_CAPS:
        cap = cap_mib * MIB
        for size, finish_expected, adopt_expected in ((cap, True, True), (cap + 1, 'ValueError: invalid local child receipt', False),
                                                      (1, True, True)):
            got = outcome(lambda: with_store(receipt_row(adapter, 'running'), lambda: bj.finish_media_local_child('j1', account_id=ACCT, child_id='c1', nonce='n1', receipt=local_receipt(size))))
            assert got == finish_expected, (adapter, size, got)
            rows = receipt_row(adapter, 'terminal'); rows['j1']['local_child']['receipt'] = local_receipt(size)
            got = outcome(lambda: with_store(rows, lambda: bj.adopt_media_local_artifact('j1', account_id=ACCT, child_id='c1')))
            assert got is adopt_expected, (adapter, size, got)
            count(2)
        # wrong nonce / account / state refuse without raising
        for label, kw in (('nonce', dict(nonce='zz')), ('child', dict(child_id='c2')), ('account', dict(account_id='acct2'))):
            args = dict(account_id=ACCT, child_id='c1', nonce='n1', receipt=local_receipt(1)); args.update(kw)
            assert with_store(receipt_row(adapter, 'running'), lambda: bj.finish_media_local_child('j1', **args)) is False, (adapter, label)
            count()
        assert with_store(receipt_row(adapter, 'terminal'), lambda: bj.finish_media_local_child('j1', account_id=ACCT, child_id='c1', nonce='n1', receipt=local_receipt(1))) is False
        count()
    # receipt shape refusals (adapter independent)
    for label, receipt, expected in RECEIPT_SHAPE_ROWS:
        got = outcome(lambda: with_store(receipt_row('trellis-image3d-v1', 'running'), lambda: bj.finish_media_local_child('j1', account_id=ACCT, child_id='c1', nonce='n1', receipt=receipt)))
        assert got == expected, (label, got)
        count()


E_RCPT = 'ValueError: invalid local child receipt'
RECEIPT_SHAPE_ROWS = [
    ('not-dict', 'x', E_RCPT), ('extra-field', dict(local_receipt(5), zz=1), E_RCPT), ('missing-size', {'exit_code': 0, 'output_sha256': H('a'), 'artifact_id': 'a'}, E_RCPT),
    ('exit-256', dict(local_receipt(5), exit_code=256), E_RCPT), ('exit-bool', dict(local_receipt(5), exit_code=True), E_RCPT),
    ('size-neg', dict(local_receipt(5), output_size=-1), E_RCPT), ('size-over-hard-cap', dict(local_receipt(5), output_size=268435457), E_RCPT),
    ('size-0', dict(local_receipt(0)), 'ValueError: empty render'),
    ('no-artifact-id', {'exit_code': 0, 'output_sha256': H('a'), 'output_size': 5}, 'ValueError: empty render'),
    ('sha-upper', dict(local_receipt(5), output_sha256=H('A')), 'ValueError: invalid media digest'),
    ('artifact-id-url', dict(local_receipt(5), artifact_id='http://x.test/a'), 'ValueError: invalid media artifact id'),
    ('layout-on-non-sheet', dict(local_receipt(5), layout={}), E_RCPT),
    ('failed-clean', {'exit_code': 1, 'output_sha256': None, 'output_size': 0}, True),
    ('failed-min', {'exit_code': -255, 'output_sha256': None, 'output_size': 0}, True),
    ('failed-with-sha', {'exit_code': 1, 'output_sha256': H('a'), 'output_size': 0}, 'ValueError: invalid failed render receipt'),
    ('failed-with-size', {'exit_code': 1, 'output_sha256': None, 'output_size': 5}, 'ValueError: invalid failed render receipt'),
    ('failed-with-artifact', {'exit_code': 1, 'output_sha256': None, 'output_size': 0, 'artifact_id': 'a'}, 'ValueError: invalid failed render receipt'),
    ('outcome-key-non-recording-adapter', {'outcome': 'finalized'}, False),
]


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_text_and_hash_primitives():
    run_text_hash()


def test_facts_lookup_and_foreground_kind():
    run_facts_lookup()


def test_artifact_mime_and_size_caps_facts_and_legacy_fallback():
    run_artifact_caps()


def test_artifact_shape_refusals():
    _artifact_shape_rows()


def test_remote_task_id_syntax():
    run_task_ids()
    run_second_task_id()


def test_task_id_cross_adapter_matrix():
    run_task_id_matrix()


def test_owned_fetch_marker_facts_vs_legacy():
    run_owned_fetch()


def test_reserve_accepts_valid_identity_for_each_buildable_adapter():
    run_reserve_valid()


def test_reserve_without_resource_binding():
    run_reserve_no_binding()


def test_reserve_binding_mutations_table():
    run_reserve_mutations()
    covered = {a for a, _, _ in BINDING_ROWS}
    assert covered == {a for a in ALL_IDS if valid(a) is not None}
    count()


def test_reserve_cross_adapter_binding_matrix():
    run_reserve_matrix()


def test_reserve_output_name_suffix():
    run_name_suffix()


def test_local_child_profiles():
    run_child_profiles()
    run_child_identity()


def test_local_child_receipts_and_size_caps():
    run_local_receipts()


def test_adapter_ids_validation_code_vs_catalog():
    run_ids_vs_catalog()


# --------------------------------------------------------------------------
# B4-FIX: one direct test per deliberately changed finding
# --------------------------------------------------------------------------
def test_b4_fix_1_reverted_unknown_adapter_id_is_reserved_as_at_the_snapshot():
    # BQ-20: older records and fixtures reserve ids without a declaration; no declared interface is granted to them
    unknown = shell('tripo-text3d-v1'); unknown['adapter_id'] = 'unknown-vendor-v1'
    assert reserve(unknown) == 'ok'
    assert reserve(shell('tripo-text3d-v1')) == 'ok'


def test_b4_fix_3_comfy_binding_only_under_comfy_adapters():
    comfy = valid('comfyui')
    assert reserve(comfy) == 'ok'
    for other in ('sprixen-sprite-v1', 'sprixen-animation-v1', 'fal-flux-schnell-v1', 'blender-glb-png-v1',
                  'blender-glb-four-heading-v1', 'blender-glb-rig-clip-v1'):
        i = shell(other); i['resource_binding'] = copy.deepcopy(comfy['resource_binding']); i['input_hashes'] = list(comfy['input_hashes'])
        assert reserve(i) == 'ValueError: invalid media profile binding', other


def test_b4_fix_5_reverted_non_declared_records_keep_the_snapshot_artifact_limits():
    # BQ-20 (CUS:7): a catalog id whose version is not a known declaration keeps the legacy MIME/size fallback
    assert artifact_outcome('comfyui-material-v1', '1', 'application/zip', 1) == 'ok'
    assert artifact_outcome('comfyui-material-v1', '2', 'image/png', 1) == 'ok'
    assert artifact_outcome('comfyui-material-v1', '2', 'application/zip', 1) == MIME_ERR
    rec = {'identity': {'adapter_id': 'unknown-vendor-v1', 'adapter_version': '1', 'operation': 'x'}}
    assert isinstance(outcome(bj._media_artifact_for, rec, art('unknown-vendor-v1', 'image/png', 1)), dict)  # non-catalog ids keep the fallback


def test_b4_fix_6_reverted_owned_fetch_for_non_declared_records_uses_the_snapshot_list():
    for a in ('comfyui-material-v1', 'meshy-rig-v1', 'voidless-image-v1'):
        assert claim_collect(a, '1') == 'owned-fetch-v1', a      # known declaration: facts
        assert claim_collect(a, '2') is None, a                  # snapshot: not in the legacy list
    assert claim_collect('meshy-preview-v1', '2') == 'owned-fetch-v1'
    assert claim_collect('trellis-image3d-v1', '2') is None


def test_b4_fix_7_comfy_material_non_dict_binding_is_value_error():
    for bad in (None, [], 'x'):
        i = valid('comfyui-material-v1'); i['resource_binding'] = bad
        assert reserve(i) == 'ValueError: invalid media resource binding', bad
