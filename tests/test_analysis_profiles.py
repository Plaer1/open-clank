import base64
import hashlib
import json

from src.openclank import analysis_dossiers as dd
from src.openclank import analysis_profiles as ap
from src.openclank import analysis_result_receipts as rr
from src.openclank import analysis_snapshot_contract as sc
from src.openclank.analysis_projects import project_identity

SRC = 'a' * 64
PROV = 'b' * 64
REV = {'kind': 'hostFingerprint', 'value': 'sha256:%s:3' % SRC}
DREV = {'kind': 'hostFingerprint', 'value': 'sha256:%s:1' % ('e' * 64)}
IDN = project_identity('owner', 'ws')
FIXTURE = ap.AnalysisProfile(provider='fixture', version='1.0', producer_evidence='fx_producer',
                             version_evidence='fx_version', result_version_evidence='fx_result_version')
RIZIN = ap.PROFILES[0]


def _refused(fn):
    try:
        fn()
    except ValueError as exc:
        return str(exc)
    return None


def _result(profile, **o):
    r = {'provider': profile.provider, 'providerVersion': profile.version, 'providerVersionEvidence': profile.result_version_evidence,
         'providerSHA256': PROV, 'sourceSHA256': SRC, 'sourceRevision': REV, 'coverage': 'partial_static_analysis',
         'architecture': 'unknown', 'omitted': 0, 'functions': []}
    r.update(o)
    return json.dumps(r, sort_keys=True).encode()


def _validate(data):
    return rr.validate_result(data, operation='functions', source_sha=SRC, source_revision=REV, provider_sha=PROV)


def _receipt(profile, **o):
    v = {'v': 1, 'kind': 'provisional', 'account': 'a', 'workspace': 'w', 'chat': 'c', 'nativeSession': 'n', 'message': 'm', 'call': 'k',
         'actor': 'x', 'root': 'r', 'turn': 't', 'executionRevision': 0, 'holder': 'h', 'sourceID': 's', 'sourceSHA': SRC, 'sourceRevision': REV,
         'operation': 'functions', 'providerSHA': PROV, 'providerVersion': profile.version, 'providerEvidence': profile.producer_evidence,
         'versionEvidence': profile.version_evidence, 'artifactID': 'art', 'outputSHA': 'c' * 64, 'inputSHA': 'd' * 64, 'size': 10,
         'issued': 100, 'expires': 200}
    v.update(o)
    return v


def _export(profile, **o):
    i = {'sourceID': 's', 'sourceSHA256': SRC, 'sourceRevision': REV, 'resultSHA256': 'f' * 64, 'operation': 'functions',
         'provider': profile.provider, 'providerSHA256': PROV, 'providerVersion': profile.version,
         'executableEvidence': profile.producer_evidence, 'versionEvidence': profile.version_evidence}
    i.update(o)
    row = {'name': 'main', 'address': 4096, 'size': 8, 'addressSpace': 'provider_virtual'}
    p = {'state': 'verified', 'sourceState': 'current', 'dossierRevision': DREV, 'resultIdentity': i, 'function': row,
         'coverage': 'partial_static_analysis', 'architecture': 'unknown', 'omitted': 0,
         'unsupported': ['disassembly', 'cross_references', 'traces']}
    return dd.function_export(IDN, 'exp1', 'dos1', DREV, [p])


def _manifest(profile, **d):
    data = _result(profile)
    digest = hashlib.sha256(data).hexdigest()
    dossier = {'dossier_id': 'dos1', 'dossier_revision': DREV, 'source_id': 's', 'source_revision': REV, 'source_sha256': SRC,
               'result_sha256': digest, 'provider': profile.provider, 'provider_sha256': PROV, 'provider_version': profile.version,
               'provider_evidence': profile.producer_evidence, 'version_evidence': profile.version_evidence, 'operation': 'functions',
               'result': {'encoding': 'base64', 'size_bytes': len(data), 'sha256': digest, 'data': base64.b64encode(data).decode()},
               'interpretations': []}
    dossier.update(d)
    scope = {'mode': 'folder', 'root_resource_id': 'root1', 'selections': [[]], 'filters': [], 'policy_generation': 0}
    limits = {'nodes': 4, 'depth': 4, 'file_bytes': 100, 'total_bytes': 100, 'manifest_bytes': 100000, 'seconds': 5}
    dig = sc.directory_digest(scope_sha256=sc.scope_digest(scope), coverage='complete', omitted_count=0, reasons=[], children=[])
    return {'schema_version': sc.SCHEMA_VERSION, 'account_id': 'owner', 'workspace_id': 'ws', 'project_key': IDN['projectKey'],
            'root_identity': 'root1', 'capture_interval': {'started_ms': 1, 'finished_ms': 2}, 'scope': scope, 'limits': limits,
            'dossier': dossier, 'components': [{'path': [], 'kind': 'directory', 'coverage': 'complete', 'child_count': 0,
                                                'omitted_count': 0, 'reasons': [], 'sha256': dig}]}


def test_shipped_table_has_exactly_one_entry():
    assert len(ap.PROFILES) == 1
    assert (RIZIN.provider, RIZIN.version) == ('rizin', '0.8.1')


def test_lookups_use_equality_and_refuse_odd_types():
    assert ap.profile_for('rizin', '0.8.1') is RIZIN
    assert ap.profile_for('rizin', '9') is None and ap.profile_for('other', '0.8.1') is None
    for odd in (['rizin'], {'a': 1}, None, 1, b'rizin'):
        assert ap.profile_for(odd, '0.8.1') is None
        assert ap.profile_for('rizin', odd) is None
        assert ap.profile_for_version(odd) is None
        assert ap.profile_for_provider(odd) is None


def test_legacy_document_without_provider_resolves_by_version():
    assert ap.profile_for_version('0.8.1').provider == 'rizin'
    assert ap.default_legacy_profile() is RIZIN


def test_rizin_still_accepted_everywhere():
    assert _refused(lambda: _validate(_result(RIZIN))) is None
    assert _refused(lambda: rr._validate(_receipt(RIZIN), kind='provisional', now=150)) is None
    assert _refused(lambda: dd.verify_function_export(_export(RIZIN), IDN, 'exp1')) is None
    assert _refused(lambda: sc.validate_manifest(_manifest(RIZIN))) is None


def test_rizin_refusals_unchanged():
    assert _refused(lambda: _validate(_result(RIZIN, provider='other'))) == 'analysis result is unavailable'
    assert _refused(lambda: _validate(_result(RIZIN, provider=['rizin']))) == 'analysis result is unavailable'
    assert _refused(lambda: _validate(_result(RIZIN, providerVersion='9'))) == 'analysis result is unavailable'
    assert _refused(lambda: rr._validate(_receipt(RIZIN, providerVersion=['0.8.1']), kind='provisional', now=150)) == 'analysis receipt unavailable'
    assert _refused(lambda: _export(RIZIN, provider=['rizin'])) == 'invalid function export'
    assert _refused(lambda: sc.validate_manifest(_manifest(RIZIN, provider='other'))) == 'invalid dossier provenance'


def test_synthetic_profile_refused_without_injection_accepted_with_it():
    assert _refused(lambda: _validate(_result(FIXTURE))) == 'analysis result is unavailable'
    assert _refused(lambda: rr._validate(_receipt(FIXTURE), kind='provisional', now=150)) == 'analysis receipt unavailable'
    assert _refused(lambda: _export(FIXTURE)) == 'invalid function export'
    assert _refused(lambda: sc.validate_manifest(_manifest(FIXTURE))) == 'invalid dossier provenance'
    with ap.using_profiles((FIXTURE,)):
        assert _refused(lambda: _validate(_result(FIXTURE))) is None
        assert _refused(lambda: rr._validate(_receipt(FIXTURE), kind='provisional', now=150)) is None
        assert _refused(lambda: dd.verify_function_export(_export(FIXTURE), IDN, 'exp1')) is None
        assert _refused(lambda: sc.validate_manifest(_manifest(FIXTURE))) is None
        # injected table is closed too: Rizin is not in it, and wrong evidence is refused
        assert _refused(lambda: _validate(_result(RIZIN))) == 'analysis result is unavailable'
        assert _refused(lambda: _validate(_result(FIXTURE, providerVersionEvidence='x'))) == 'analysis result is unavailable'
    assert ap.PROFILES == (RIZIN,) and ap.profile_for('rizin', '0.8.1') is RIZIN


def test_explicit_profiles_parameter():
    assert ap.profile_for('fixture', '1.0', (FIXTURE,)) is FIXTURE
    assert ap.profile_for('fixture', '1.0') is None
    assert ap.profile_for_version('1.0', (FIXTURE,)) is FIXTURE
    assert ap.default_legacy_profile((FIXTURE,)) is FIXTURE


if __name__ == '__main__':
    names = [n for n in sorted(globals()) if n.startswith('test_')]
    for n in names:
        globals()[n]()
    print('%d tests ok' % len(names))
