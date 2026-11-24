"""Joined Host path for a TEST-ONLY second analysis profile (no vendor lock-in proof).

receipt (complete_provisional) -> project save/read (AnalysisProjects) -> dossier export -> snapshot manifest.
Only the Files facade/Host and the artifact store are stubbed. Run as a script, plain asserts, no pytest.
"""
import asyncio, base64, hashlib, json, os, tempfile, time, types

from src.openclank import analysis_dossiers as dd
from src.openclank import analysis_profiles as ap
from src.openclank import analysis_result_receipts as rr
from src.openclank import analysis_snapshot_contract as sc
from src.openclank.analysis_projects import AnalysisProjects, manifest, project_identity, parse_provider_document
from src.openclank.artifacts import account_artifact_owner
from src.openclank.files_host_provider import _origin

WS, OWNER, SOURCE = '/ws', 'owner-1', b'joined-source-bytes' * 4
SRC, PROV = hashlib.sha256(SOURCE).hexdigest(), 'b' * 64
REV = {'kind': 'hostFingerprint', 'value': 'sha256:%s:%d' % (SRC, 1)}
TEST = ap.AnalysisProfile(provider='testprov', version='9.9.9', producer_evidence='tp_producer_x', version_evidence='tp_version_x',
                          result_version_evidence='tp_result_version_x')
RIZIN = ap.PROFILES[0]


def _profile_table(profile): return ap.using_profiles((profile,))


def _no_vendor(*blobs):
    for b in blobs:
        b = b if isinstance(b, bytes) else json.dumps(b, sort_keys=True).encode() if not isinstance(b, str) else b.encode()
        assert b'rizin' not in b.lower() and b'0.8.1' not in b, b[:200]


class Fs:
    """In-memory Files facade + Host stand-in (path -> [bytes, version]); created documents are mirrored under a temp dir."""
    def __init__(self, identity, tmp):
        self.files, self.dirs, self.tmp, self.mutation_scope = {}, {WS}, tmp, object()
        path = WS
        for part in ('.clanker', 'robonotes', identity['projectKey'], 'analysis'):
            path = os.path.join(path, part); self.dirs.add(path)
        self.analysis = path
        self.files[os.path.join(path, '00-index.md')] = [manifest(identity).encode(), 1]
        self.files[WS + '/src.bin'] = [SOURCE, 1]

    def entry(self, path, inspect=False):
        if path in self.dirs:
            return types.SimpleNamespace(origin_id=_origin(path), kind='folder', size=0, capabilities={'children', 'write', 'open'}, revision=REV)
        data, v = self.files[path]
        rev = {'kind': 'hostObjectIdentity', 'value': 'oid:%d' % v} if inspect else {'kind': 'hostFingerprint', 'value': 'sha256:%s:%d' % (hashlib.sha256(data).hexdigest(), v)}
        return types.SimpleNamespace(origin_id=_origin(path), kind='file', size=len(data), capabilities={'open', 'download'}, revision=rev)

    def path(self, origin): return next(p for p in list(self.files) + list(self.dirs) if _origin(p) == origin)
    async def resource_for_path(self, context, path): return self.entry(path)
    async def stat_inspection(self, context, origin_id): return self.entry(self.path(origin_id), inspect=True)
    async def stat(self, context, origin_id): return self.entry(self.path(origin_id))

    async def content(self, context, origin_id):
        path = self.path(origin_id); data = self.files[path][0]
        async def stream(start, end): yield data[start:end]
        return types.SimpleNamespace(origin_id=origin_id, size=len(data), etag='oid:%d' % self.files[path][1], stream=stream, data=None, path=None)

    def _resource(self, context, provider, resource): return {'ref': 'ref:' + resource.origin_id, 'id': resource.origin_id, 'revision': resource.revision}
    def _provider_for_ref(self, context, ref, capability=None):
        origin = _origin(WS + '/src.bin') if ref == 'src-ref' else ref[4:]
        return self, types.SimpleNamespace(provider='host', stable_id='src-1' if ref == 'src-ref' else origin, origin_id=origin)
    @staticmethod
    def _provider_revision(entry): return dict(entry.revision)
    def _generation_current(self, context): return True
    async def open_payload(self, context, resource_ref, expected_revision=None):
        return {'payload': {'text': self.files[self.path(resource_ref[4:])][0].decode('utf-8')}}

    async def create(self, context, parent_ref, name, text, before_dispatch=None):
        path = os.path.join(self.path(parent_ref[4:]), name)
        if before_dispatch: await before_dispatch()
        self.files[path] = [text.encode(), 1]
        with open(os.path.join(self.tmp, name), 'w', encoding='utf-8') as fh: fh.write(text)
        return {'outcome': 'created', 'name': name}


class Store:
    """Artifact store stand-in holding the one result artifact."""
    def __init__(self, data): self.data = data
    def _row(self): return types.SimpleNamespace(id='art1', state='acknowledged', media_type='application/json', content_sha256=hashlib.sha256(self.data).hexdigest(), size_bytes=len(self.data))
    def get(self, owner, artifact_id): return self._row()
    def read_chunks(self, owner, artifact_id): return [self.data]
    def acknowledge(self, owner, artifact_id): return self._row()


def _result(profile, **o):
    r = {'provider': profile.provider, 'providerVersion': profile.version, 'providerVersionEvidence': profile.result_version_evidence,
         'providerSHA256': PROV, 'sourceSHA256': SRC, 'sourceRevision': REV, 'coverage': 'partial_static_analysis', 'architecture': 'unknown',
         'omitted': 0, 'functions': [{'name': 'main', 'address': 4096, 'size': 8, 'addressSpace': 'provider_virtual'}]}
    r.update(o)
    return json.dumps(r, sort_keys=True).encode()


def _provisional(profile, data, now, **o):
    v = {'v': 1, 'kind': 'provisional', 'account': OWNER, 'workspace': 'ws-1', 'chat': 'c', 'nativeSession': 'n', 'message': 'm', 'call': 'k',
         'actor': 'x', 'root': 'r', 'turn': 't', 'executionRevision': 0, 'holder': 'h', 'sourceID': 'src-1', 'sourceSHA': SRC, 'sourceRevision': REV,
         'operation': 'functions', 'providerSHA': PROV, 'providerVersion': profile.version, 'providerEvidence': profile.producer_evidence,
         'versionEvidence': profile.version_evidence, 'artifactID': 'art1', 'outputSHA': hashlib.sha256(data).hexdigest(), 'inputSHA': 'd' * 64,
         'size': len(data), 'issued': now - 10, 'expires': now + 3000}
    v.update(o)
    return v


def _flow(profile):
    """Drive the real path; return every serialized artifact plus the key stage outputs."""
    now, data = int(time.time()), _result(profile)
    tmp = tempfile.mkdtemp(prefix='joined-')
    identity = project_identity(OWNER, 'ws-1'); fs = Fs(identity, tmp)
    context = types.SimpleNamespace(owner_subject_id=OWNER, workspace_id='ws-1', policy_generation=1)
    async def current(*a): return True
    projects = AnalysisProjects(facade=fs, host=fs, context=context, binding=types.SimpleNamespace(path=WS), identity=identity, current=current)

    async def yes(*a): return True
    # 1 receipt: validate_result, seal provisional, complete_provisional -> sealed completed receipt
    rr.validate_result(data, operation='functions', source_sha=SRC, source_revision=REV, provider_sha=PROV)
    provisional = rr.seal(_provisional(profile, data, now), kind='provisional', now=now)
    done = asyncio.run(rr.complete_provisional(token=provisional, account=OWNER, store=Store(data), current=yes, source_current=yes, part_current=yes))
    assert done['state'] == 'complete' and done['providerEvidence'] == {'executableSHA256': profile.producer_evidence, 'version': profile.version_evidence}
    completed = done['receipt']
    facts = rr.open_receipt(completed, kind='completed')
    assert facts['providerVersion'] == profile.version and facts['providerEvidence'] == profile.producer_evidence and facts['versionEvidence'] == profile.version_evidence
    # 2 project save (real save_provider_dossier -> provider_document) and read back (real read_provider_dossier)
    saved = asyncio.run(projects.save_provider_dossier({'workspaceID': 'ws-1', 'sourceRef': 'src-ref', 'completedReceipt': completed, 'dossierID': 'dos1'}, Store(data)))
    assert saved['receipt']['outcome'] == 'created' and saved['sourceCurrent'] is True
    document = open(os.path.join(tmp, 'provider-dos1.md'), encoding='utf-8').read()
    pfacts, result = parse_provider_document(document, identity, 'dos1')
    assert pfacts['providerVersion'] == profile.version and result['provider'] == profile.provider
    verified = asyncio.run(projects.read_provider_dossier('dos1', 'src-ref', include_result_bytes=True))
    assert verified['state'] == 'verified' and verified['sourceState'] == 'current'
    ident = verified['resultIdentity']
    # 3 dossier: projection + function export, verified back from the saved Markdown
    projection = dd.project_function(verified, {'name': 'main', 'address': 4096, 'size': 8, 'addressSpace': 'provider_virtual'})
    export = dd.function_export(identity, 'exp1', 'dos1', verified['revision'], [projection])
    dd.verify_function_export(export, identity, 'exp1')
    overlay = json.dumps(dd.static_function_overlay(verified), sort_keys=True)
    # 4 snapshot contract from the verified dossier facts
    scope = {'mode': 'folder', 'root_resource_id': 'root1', 'selections': [[]], 'filters': [], 'policy_generation': 0}
    digest = sc.directory_digest(scope_sha256=sc.scope_digest(scope), coverage='complete', omitted_count=0, reasons=[], children=[])
    dossier = {'dossier_id': 'dos1', 'dossier_revision': verified['revision'], 'source_id': ident['sourceID'], 'source_revision': ident['sourceRevision'],
               'source_sha256': ident['sourceSHA256'], 'result_sha256': ident['resultSHA256'], 'provider': ident['provider'], 'provider_sha256': ident['providerSHA256'],
               'provider_version': ident['providerVersion'], 'provider_evidence': ident['executableEvidence'], 'version_evidence': ident['versionEvidence'],
               'operation': 'functions', 'result': {'encoding': 'base64', 'size_bytes': len(data), 'sha256': ident['resultSHA256'], 'data': base64.b64encode(verified['_result_bytes']).decode()},
               'interpretations': []}
    snap = {'schema_version': sc.SCHEMA_VERSION, 'account_id': OWNER, 'workspace_id': 'ws-1', 'project_key': identity['projectKey'], 'root_identity': 'root1',
            'capture_interval': {'started_ms': 1, 'finished_ms': 2}, 'scope': scope,
            'limits': {'nodes': 4, 'depth': 4, 'file_bytes': 100, 'total_bytes': 100, 'manifest_bytes': 100000, 'seconds': 5}, 'dossier': dossier,
            'components': [{'path': [], 'kind': 'directory', 'coverage': 'complete', 'child_count': 0, 'omitted_count': 0, 'reasons': [], 'sha256': digest}]}
    raw = sc.encode_manifest(snap)
    reference = sc.reference_for(raw, 'upload1'); sc.validate_reference(reference, raw)
    back = sc.decode_manifest(raw)
    return dict(data=data, provisional=provisional, completed=completed, facts=facts, document=document, ident=ident, export=export,
                overlay=overlay, raw=raw, back=back, reference=reference, saved=saved)


def test_second_profile_flows_through_every_stage_with_no_vendor_literal():
    with _profile_table(TEST):
        r = _flow(TEST)
    assert r['ident']['provider'] == 'testprov' and r['ident']['providerVersion'] == '9.9.9'
    assert r['ident']['executableEvidence'] == 'tp_producer_x' and r['ident']['versionEvidence'] == 'tp_version_x'
    assert r['back']['dossier']['provider'] == 'testprov' and r['back']['dossier']['provider_version'] == '9.9.9'
    assert r['back']['dossier']['provider_evidence'] == 'tp_producer_x' and r['back']['dossier']['version_evidence'] == 'tp_version_x'
    assert json.loads(r['data'])['providerVersionEvidence'] == 'tp_result_version_x'
    assert '"provider":"testprov"' in r['export'] and '"providerVersion":"9.9.9"' in r['export']
    # every artifact on the path, plus the receipts' decrypted facts and the saved file the path read back
    _no_vendor(r['data'], r['provisional'], r['completed'], r['facts'], r['document'], r['export'], r['overlay'], r['raw'], r['back'], r['reference'], r['saved'])
    assert ap.PROFILES == (RIZIN,)


def test_control_shipped_rizin_flow_is_accepted():
    r = _flow(RIZIN)  # no injection: shipped table
    assert (r['ident']['provider'], r['ident']['providerVersion']) == ('rizin', '0.8.1')
    assert r['ident']['executableEvidence'] == RIZIN.producer_evidence and 'rizin' in r['document'].lower() + r['export']


def _first_refusal(fn):
    try: fn()
    except ValueError as exc: return str(exc)


def test_testprov_refused_at_first_stage_with_shipped_table():
    data = _result(TEST)
    assert _first_refusal(lambda: rr.validate_result(data, operation='functions', source_sha=SRC, source_revision=REV, provider_sha=PROV)) == 'analysis result is unavailable'
    assert _first_refusal(lambda: rr.seal(_provisional(TEST, data, int(time.time())), kind='provisional')) == 'analysis receipt unavailable'


def test_undeclared_version_refused_with_test_profile_active():
    with _profile_table(TEST):
        bad = _result(TEST, providerVersion='9.9.8')
        assert _first_refusal(lambda: rr.validate_result(bad, operation='functions', source_sha=SRC, source_revision=REV, provider_sha=PROV)) == 'analysis result is unavailable'
        assert _first_refusal(lambda: rr.seal(_provisional(TEST, bad, int(time.time()), providerVersion='9.9.8'), kind='provisional')) == 'analysis receipt unavailable'
        # acp_bridge.py:4138 `if profile_for_version(request['providerVersion']) is None: return unavailable` sits in a closure (source-read only); its predicate:
        assert ap.profile_for_version('9.9.8') is None and ap.profile_for_version('9.9.9') is TEST
    assert ap.profile_for_version('9.9.9') is None


def test_legacy_document_without_provider_resolves_to_rizin_by_version():
    legacy = {'providerVersion': '0.8.1'}  # stored facts carry a version but no provider field
    assert (ap.profile_for_version(legacy['providerVersion']) or ap.default_legacy_profile()).provider == 'rizin'
    assert (ap.profile_for_version('unknown') or ap.default_legacy_profile()) is RIZIN


if __name__ == '__main__':
    names = [n for n in sorted(globals()) if n.startswith('test_')]
    for n in names: globals()[n]()
    print('%d tests ok' % len(names))
