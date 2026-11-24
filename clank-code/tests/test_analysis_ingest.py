import hashlib
import json
import pathlib

from src.openclank.analysis_adapters.ingest import REGISTRY, IngestError
from src.openclank.analysis_ingest import normalize, trace_overlay

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / 'tests' / 'fixtures' / 'analysis'
SHA = hashlib.sha256((FIX / 'ingest-source.bin').read_bytes()).hexdigest()
STATIC = (FIX / 'ingest-static-v1.jsonl').read_bytes()
TRACE = (FIX / 'ingest-trace-v1.jsonl').read_bytes()


def _refused(data, sha=SHA):
    try:
        normalize(data, 'fixture_jsonl', expected_source_sha256=sha, _test_adapter=REGISTRY['fixture_jsonl'])
    except IngestError:
        return True
    return False


def _mut(base, line, fn):
    lines = base.decode().split('\n')
    obj = json.loads(lines[line])
    fn(obj)
    lines[line] = json.dumps(obj)
    return '\n'.join(lines).encode()


def test_registry_has_one_entry():
    assert list(REGISTRY) == ['fixture_jsonl']
    assert REGISTRY['fixture_jsonl'].formats == ('openclank.analysis-ingest.v1',)


def test_static_fixture_parses():
    n = normalize(STATIC, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl'])
    assert n['sourceState'] == 'current' and n['label'] == 'static' and n['receiptKind'] == 'ingest'
    assert [r['type'] for r in n['rows']] == ['function', 'function', 'xref', 'xref']
    assert n['rows'][0] == {'type': 'function', 'name': 'entry', 'address': 4096, 'size': 32}
    assert n['rows'][3]['to'] is None


def test_trace_fixture_parses():
    n = normalize(TRACE, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl'])
    assert n['sourceState'] == 'current' and n['label'] == 'runtime_observed'
    assert [r['type'] for r in n['rows']] == ['call', 'call', 'hit']
    assert n['rows'][1]['to'] is None and 'count' not in n['rows'][1]


def test_unknown_adapter():
    try:
        normalize(STATIC, 'nope', expected_source_sha256=SHA)
    except IngestError:
        return
    assert False


def test_unknown_key_rejected():
    assert _refused(_mut(STATIC, 1, lambda o: o.update(extra=1)))
    assert _refused(_mut(STATIC, 0, lambda o: o.update(extra=1)))
    assert _refused(_mut(STATIC, 0, lambda o: o['source'].update(extra=1)))
    assert _refused(_mut(STATIC, 1, lambda o: o.update(type='hit')))  # row type from the wrong kind


def test_oversize_file_rejected():
    assert _refused(STATIC + b' ' * (1024 * 1024))


def test_too_many_rows_rejected():
    head = STATIC.split(b'\n')[0]
    rows = b'\n'.join(json.dumps({'type': 'function', 'name': 'f%d' % i, 'address': i, 'size': 1}).encode() for i in range(4097))
    assert _refused(head + b'\n' + rows)
    ok = b'\n'.join(json.dumps({'type': 'function', 'name': 'f%d' % i, 'address': i, 'size': 1}).encode() for i in range(4096))
    assert not _refused(head + b'\n' + ok)


def test_oversize_row_rejected():
    assert _refused(_mut(STATIC, 1, lambda o: o.update(name='x' * 600)))


def test_duplicate_json_key_rejected():
    lines = STATIC.split(b'\n')
    lines[1] = b'{"type":"function","name":"a","name":"b","address":1,"size":1}'
    assert _refused(b'\n'.join(lines))


def test_duplicate_row_rejected():
    lines = STATIC.split(b'\n')
    lines.insert(2, lines[1])
    assert _refused(b'\n'.join(lines))


def test_nan_infinity_nested_and_unsafe_rejected():
    for bad in (b'NaN', b'Infinity', b'-Infinity', b'[1]', b'{"a":1}', b'true', b'1.5', b'-1', b'9007199254740992'):
        lines = STATIC.split(b'\n')
        lines[1] = b'{"type":"function","name":"a","address":' + bad + b',"size":1}'
        assert _refused(b'\n'.join(lines)), bad
    lines = STATIC.split(b'\n')
    lines[1] = b'{"type":"function","name":["a"],"address":1,"size":1}'
    assert _refused(b'\n'.join(lines))
    lines[1] = b'[' * 400 + b']' * 400
    assert _refused(b'\n'.join(lines))


def test_bad_header_rejected():
    assert _refused(_mut(STATIC, 0, lambda o: o.update(schema='openclank.analysis-ingest.v2')))
    assert _refused(_mut(STATIC, 0, lambda o: o.update(kind='other')))
    assert _refused(_mut(STATIC, 0, lambda o: o['source'].update(sha256='ABC')))
    assert _refused(_mut(STATIC, 0, lambda o: o['source'].update(addressSpace='file')))
    assert _refused(b'')
    assert _refused(b'\xff\xfe')


def test_wrong_source_sha_is_historical():
    n = normalize(STATIC, 'fixture_jsonl', expected_source_sha256='0' * 64, _test_adapter=REGISTRY['fixture_jsonl'])
    assert n['sourceState'] == 'historical'
    assert trace_overlay(n)['sourceState'] == 'historical'


def test_static_and_runtime_never_merged():
    s = trace_overlay(normalize(STATIC, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl']))
    t = trace_overlay(normalize(TRACE, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl']))
    assert s['schema'] == t['schema'] == 'openclank.trace-overlay.v1'
    assert s['label'] == 'static' and {e['origin'] for e in s['edges']} == {'static_xref'} and s['hits'] == []
    assert t['label'] == 'runtime_observed' and {e['origin'] for e in t['edges']} == {'runtime_observed'} and t['functions'] == []
    assert not {x['id'] for x in s['functions'] + s['edges']} & {x['id'] for x in t['edges'] + t['hits']}


def test_unresolved_edges_explicit():
    s = trace_overlay(normalize(STATIC, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl']))
    t = trace_overlay(normalize(TRACE, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl']))
    assert s['unresolvedEdges'] == 1 and t['unresolvedEdges'] == 1
    assert [e['resolved'] for e in s['edges']] == [True, False]


def test_overlay_ids_are_overlay_only():
    for data in (STATIC, TRACE):
        o = trace_overlay(normalize(data, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl']))
        ids = [x['id'] for x in o['functions'] + o['edges'] + o['hits']]
        assert ids and len(set(ids)) == len(ids)
        assert all(i.startswith('overlay:') and len(i) == 8 + 64 for i in ids)
        assert not any(i.startswith(('file:', 'decl:', 'occ:')) for i in ids)


def test_the_fixture_adapter_is_test_only_and_nothing_is_qualified():
    from src.openclank.analysis_adapters.ingest import qualified_adapter_ids
    assert REGISTRY['fixture_jsonl'].qualified is False and qualified_adapter_ids() == ()


def test_production_normalize_refuses_an_unqualified_adapter_but_a_test_may_pass_it_explicitly():
    try:
        normalize(STATIC, 'fixture_jsonl', expected_source_sha256=SHA)
    except IngestError as exc:
        assert str(exc) == 'unknown adapter'
    else:
        raise AssertionError('unqualified adapter accepted')
    assert normalize(STATIC, 'fixture_jsonl', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl'])['adapter'] == 'fixture_jsonl'
    try:   # the explicit object must be the one named by the id
        normalize(STATIC, 'other', expected_source_sha256=SHA, _test_adapter=REGISTRY['fixture_jsonl'])
    except IngestError:
        pass
    else:
        raise AssertionError('mismatched test adapter accepted')
