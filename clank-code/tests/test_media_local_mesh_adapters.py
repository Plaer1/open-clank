"""B4-P3: Trellis and Hunyuan local-mesh adapters are siblings, not parent and child.

EXPECTED_* were recorded by running table() against the PRE-change code (Hunyuan subclassing
TrellisAdapter) and pasted here verbatim.  The child launcher, job store and artifact store are
fakes; no runner, GPU, weights or provider is touched.  Plain asserts.
"""
import asyncio
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

from src import bg_jobs
from src.openclank.media_adapters import base
from src.openclank.media_adapters.hunyuan_local import HunyuanShapeAdapter
from src.openclank.media_adapters.trellis import TrellisAdapter


def _glb(doc):
    raw = json.dumps(doc, separators=(',', ':')).encode()
    raw += b' ' * (-len(raw) % 4)
    return struct.pack('<4sII', b'glTF', 2, 20 + len(raw)) + struct.pack('<I4s', len(raw), b'JSON') + raw


PLAIN = _glb({'asset': {'version': '2.0'}})
WITH_IMAGES = _glb({'asset': {'version': '2.0'}, 'images': [{'mimeType': 'image/png'}]})
SOURCE = b'source-bytes'
EVIDENCE = {'ref': 'r', 'resourceID': 'x', 'revision': {'kind': 'k', 'value': 'v'}, 'sha256': hashlib.sha256(SOURCE).hexdigest(),
            'mime_type': 'image/png', 'width': 1, 'height': 1, 'name': 'n'}
JOB = {'id': 'job1', 'account_id': 'acct1', 'submission_token': 'tok',
       'identity': {'input_hashes': [EVIDENCE['sha256']], 'resource_binding': {'image_source': EVIDENCE}}}


def _store(tmp):
    db = Path(tmp) / 'a.db'
    db.write_bytes(b'')
    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(url=SimpleNamespace(database=str(db), get_backend_name=lambda: 'sqlite')), close=lambda: None)
    return SimpleNamespace(root=Path(tmp), _session_factory=lambda: session, blob=b'', read_chunks=None)


def _outcome(fn):
    try:
        value = asyncio.run(fn())
        return ('ok', repr(value))
    except BaseException as exc:
        return (type(exc).__name__, str(exc), type(exc.__cause__).__name__, str(exc.__cause__))


def table(cls, tmp):
    from src.openclank.blender_child import artifact_authority
    store = _store(tmp)
    launched = []
    state = {'bytes': PLAIN, 'media': None}
    store.read_chunks = lambda owner, artifact_id: [state['bytes']]

    async def profile_for(identity): return {'artifact_authority': artifact_authority(store), 'tag': 'p'}
    async def capture(identity): return state.get('evidence', EVIDENCE), state.get('source', SOURCE)
    async def launch(child, source, admission): launched.append((child, source))

    adapter = cls(profile_for=profile_for, capture_source=capture, launch_fixed=launch, artifact_store=store)
    reserved = []
    def reserve(job_id, *, account_id, token, profile):
        reserved.append((job_id, account_id, token, sorted(profile), profile['seed']))
        return {'child_id': 'c1'}
    saved = (bg_jobs.reserve_media_local_child, bg_jobs.get_media, bg_jobs.adopt_media_local_artifact)
    bg_jobs.reserve_media_local_child = reserve
    bg_jobs.get_media = lambda job_id, account_id: state['media']
    bg_jobs.adopt_media_local_artifact = lambda job_id, account_id, child_id: True
    noop = lambda: None
    out = {'adapter_id': adapter.adapter_id, 'version': adapter.version, 'route_kind': adapter.route_kind,
           'supports_lookup': adapter.supports_lookup, 'supports_cancel': adapter.supports_cancel,
           'instance_attrs': sorted(vars(adapter))}
    try:
        out['submit_ok'] = _outcome(lambda: adapter.submit({'parameters': {'seed': 7}}, JOB, noop))
        out['submit_effects'] = (reserved[:], len(launched), launched[0][1] == SOURCE)
        for name, request in (('bad_seed', {'parameters': {'seed': -1}}), ('bad_shape', {'parameters': {'seed': 1}, 'x': 1}), ('bool_seed', {'parameters': {'seed': True}})):
            out['submit_' + name] = _outcome(lambda: adapter.submit(request, JOB, noop))
        state['source'] = b'other'
        out['submit_source_changed'] = _outcome(lambda: adapter.submit({'parameters': {'seed': 7}}, JOB, noop))
        del state['source']
        state['evidence'] = {**EVIDENCE, 'name': 'z'}
        out['submit_evidence_changed'] = _outcome(lambda: adapter.submit({'parameters': {'seed': 7}}, JOB, noop))
        del state['evidence']
        out['collect_no_child'] = _outcome(lambda: adapter.collect(JOB, noop))
        for label, data in (('plain', PLAIN), ('images', WITH_IMAGES), ('junk', b'x' * 40), ('empty', b'')):
            state['bytes'] = data
            receipt = {'exit_code': 0, 'artifact_id': 'a1', 'output_size': len(data), 'output_sha256': hashlib.sha256(data).hexdigest()}
            state['media'] = {'local_child': {'state': 'terminal', 'receipt': receipt}}
            out['collect_' + label] = _outcome(lambda: adapter.collect(JOB, noop))
        state['bytes'] = PLAIN
        receipt = {'exit_code': 0, 'artifact_id': 'a1', 'output_size': len(PLAIN) + 1, 'output_sha256': hashlib.sha256(PLAIN).hexdigest()}
        state['media'] = {'local_child': {'state': 'terminal', 'receipt': receipt}}
        out['collect_size_mismatch'] = _outcome(lambda: adapter.collect(JOB, noop))
        state['media'] = {'local_child': {'state': 'terminal', 'receipt': {**receipt, 'exit_code': 1}}}
        out['collect_exit1'] = _outcome(lambda: adapter.collect(JOB, noop))
        out['status_exit1'] = _outcome(lambda: adapter.status(JOB, noop))
        state['media'] = {'local_child': {'state': 'terminal', 'child_id': 'c5', 'receipt': {**receipt, 'exit_code': 0}}}
        out['status_ok'] = _outcome(lambda: adapter.status(JOB, noop))
        state['media'] = {'local_child': {'state': 'running', 'child_id': 'c9'}}
        out['status_running'] = _outcome(lambda: adapter.status(JOB, noop))
        out['cancel'] = _outcome(lambda: adapter.cancel(JOB, noop))
    finally:
        bg_jobs.reserve_media_local_child, bg_jobs.get_media, bg_jobs.adopt_media_local_artifact = saved
    return out


EXPECTED_TRELLIS = {'adapter_id': 'trellis-image3d-v1',
 'version': '1',
 'route_kind': 'local',
 'supports_lookup': True,
 'supports_cancel': False,
 'instance_attrs': ['artifact_store', 'capture_source', 'launch_fixed', 'profile_for'],
 'submit_ok': ('ok', "Submission(state='running', task_id='c1')"),
 'submit_effects': ([('job1', 'acct1', 'tok', ['artifact_authority', 'input_sha256', 'seed', 'tag'], 7)], 1, True),
 'submit_bad_seed': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS seed unavailable'),
 'submit_bad_shape': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS seed unavailable'),
 'submit_bool_seed': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS seed unavailable'),
 'submit_source_changed': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS source changed'),
 'submit_evidence_changed': ('NotDispatched',
                             'TRELLIS preparation unavailable',
                             'ValueError',
                             'TRELLIS source changed'),
 'collect_no_child': ('ValueError', 'TRELLIS receipt unavailable', 'NoneType', 'None'),
 'collect_plain': ('ValueError', 'GLB binary unavailable', 'NoneType', 'None'),
 'collect_images': ('ValueError', 'GLB binary unavailable', 'NoneType', 'None'),
 'collect_junk': ('ValueError', 'invalid GLB header', 'NoneType', 'None'),
 'collect_empty': ('ValueError', 'TRELLIS artifact changed', 'NoneType', 'None'),
 'collect_size_mismatch': ('ValueError', 'TRELLIS artifact changed', 'NoneType', 'None'),
 'collect_exit1': ('ValueError', 'TRELLIS receipt unavailable', 'NoneType', 'None'),
 'status_exit1': ('ok', "Submission(state='failed', task_id=None)"),
 'status_ok': ('ok', "Submission(state='collecting', task_id='c5')"),
 'status_running': ('ok', "Submission(state='running', task_id='c9')"),
 'cancel': ('NotDispatched', 'exact local-child cancellation is unavailable', 'NoneType', 'None')}

EXPECTED_HUNYUAN = {'adapter_id': 'hunyuan3d-v2-shape-v1',
 'version': '1',
 'route_kind': 'local',
 'supports_lookup': True,
 'supports_cancel': False,
 'instance_attrs': ['artifact_store', 'capture_source', 'launch_fixed', 'profile_for'],
 'submit_ok': ('ok', "Submission(state='running', task_id='c1')"),
 'submit_effects': ([('job1', 'acct1', 'tok', ['artifact_authority', 'input_sha256', 'seed', 'tag'], 7)], 1, True),
 'submit_bad_seed': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS seed unavailable'),
 'submit_bad_shape': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS seed unavailable'),
 'submit_bool_seed': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS seed unavailable'),
 'submit_source_changed': ('NotDispatched', 'TRELLIS preparation unavailable', 'ValueError', 'TRELLIS source changed'),
 'submit_evidence_changed': ('NotDispatched',
                             'TRELLIS preparation unavailable',
                             'ValueError',
                             'TRELLIS source changed'),
 'collect_no_child': ('ValueError', 'Hunyuan receipt unavailable', 'NoneType', 'None'),
 'collect_plain': ('ok',
                   "Artifact(artifact_id='a1', "
                   "sha256='d52ea15ab28b6a8dbc5a7623dd23c044eaa0f6460322c22030cc6b976e365637', "
                   "mime_type='model/gltf-binary', size=48)"),
 'collect_images': ('ValueError', 'GLB extensions or images unsupported', 'NoneType', 'None'),
 'collect_junk': ('ValueError', 'invalid GLB header', 'NoneType', 'None'),
 'collect_empty': ('ValueError', 'Hunyuan artifact changed', 'NoneType', 'None'),
 'collect_size_mismatch': ('ValueError', 'Hunyuan artifact changed', 'NoneType', 'None'),
 'collect_exit1': ('ValueError', 'Hunyuan receipt unavailable', 'NoneType', 'None'),
 'status_exit1': ('ok', "Submission(state='failed', task_id=None)"),
 'status_ok': ('ok', "Submission(state='collecting', task_id='c5')"),
 'status_running': ('ok', "Submission(state='running', task_id='c9')"),
 'cancel': ('NotDispatched', 'exact local-child cancellation is unavailable', 'NoneType', 'None')}


def test_siblings_share_only_the_neutral_base():
    from src.openclank.media_adapters.local_mesh import LocalMeshAdapter
    assert not issubclass(HunyuanShapeAdapter, TrellisAdapter)
    assert not issubclass(TrellisAdapter, HunyuanShapeAdapter)
    assert issubclass(TrellisAdapter, LocalMeshAdapter) and issubclass(HunyuanShapeAdapter, LocalMeshAdapter)
    assert TrellisAdapter.__bases__ == (LocalMeshAdapter,) and HunyuanShapeAdapter.__bases__ == (LocalMeshAdapter,)


def test_trellis_visible_facts_unchanged():
    import tempfile
    assert table(TrellisAdapter, tempfile.mkdtemp()) == EXPECTED_TRELLIS


def test_hunyuan_visible_facts_unchanged():
    import tempfile
    assert table(HunyuanShapeAdapter, tempfile.mkdtemp()) == EXPECTED_HUNYUAN


def test_ids_match_declared_catalog():
    assert base.media_operation_facts(TrellisAdapter.adapter_id, '1') is not None
    assert base.media_operation_facts(HunyuanShapeAdapter.adapter_id, '1') is not None
