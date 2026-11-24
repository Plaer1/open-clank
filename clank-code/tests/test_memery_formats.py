"""Pure tests for the Markdown memory format (no store, no network)."""
import json
import re

from src import memery_formats as f

LABEL, STAMP = 'owner-abc123', '2026-11-10T12:00:00Z'
NEVER = lambda text: False  # noqa: E731


def rec(text, **kw):
    return {'id': 'internal-record-id-' + str(abs(hash(text)) % 10**6), 'owner': 'alice', 'text': text, 'kind': 'fact',
            'status': 'active', 'trust': {'state': 'approved', 'value': 1}, 'trust_score': 1, 'confidence_score': .9,
            'metadata': {'block_id': 'blk', 'revision': 3, 'activation': 'standing'}, 'workspace_id': 'ws', **kw}


def export(records, **kw):
    kw.setdefault('is_secret', NEVER)
    return f.export_markdown(records, owner_label=LABEL, generated_at=STAMP, **kw)


def refused(doc, code, **kw):
    try:
        f.parse_markdown(doc, **kw)
    except f.FormatError as exc:
        assert exc.code == code, (exc.code, code)
        assert exc.code in f.ERROR_CODES
        return
    raise AssertionError('accepted: expected ' + code)


NASTY = [
    '  leading and trailing  \n\n',
    'line1\nline2\r\nline3\n',
    '```python\nprint(1)\n```\n````\nnested\n````',
    '<!-- /openclank:item -->\n### fact `item-0000000000000000`\n<!-- openclank:item item-0000000000000000 5 -->',
    f.FORMAT + '\n<!-- openclank-memory-manifest openclank.memory-markdown/1\n{}\n-->\n',
    'Nicht-ASCII: über 日本語 \U0001f600 ‮',
    '`' * 70,
    '-->\n',
]


def test_round_trip_is_exact_including_adversarial_text():
    records = [rec(t, source_uri='file:///notes/a.md', source_content_sha256='a' * 64,
                   source_message_ids=['m1'], source_revision=2, tags=['x', 'y']) for t in NASTY]
    records.append(rec('plain, no source'))
    items, omitted = f.normalize_items(records, is_secret=NEVER)
    assert not omitted and len(items) == len(records)
    bundle = f.parse_markdown(export(records))
    assert bundle.items == items
    assert [i.text for i in bundle.items] == NASTY + ['plain, no source']
    assert bundle.origin == LABEL and bundle.version == 1 and bundle.format == f.FORMAT
    first = bundle.items[0]
    assert (first.source_uri, first.source_content_sha256, first.source_message_ids, first.source_revision, first.provenance) == (
        'file:///notes/a.md', 'a' * 64, ('m1',), 2, 'resolved')
    assert bundle.items[-1].provenance == 'unresolved' and bundle.items[-1].source_uri is None


def test_bytes_input_and_manifest_tags():
    doc = export([rec('hello')])
    assert f.parse_markdown(doc.encode('utf-8')).items[0].text == 'hello'
    assert doc.startswith(f._TOKEN + ' ' + f.FORMAT + '/1\n')


def test_output_is_deterministic_and_ids_are_content_derived():
    records = [rec('same'), rec('same'), rec('other')]
    a, b = export(records), export(list(records))
    assert a == b
    ids = [i.id for i in f.parse_markdown(a).items]
    assert len(set(ids)) == 3 and ids[1] == ids[0] + '-1'


def test_no_internal_or_approval_state_is_exported():
    doc = export([rec('keep me', source_message_ids=['m9'])])
    manifest = json.loads(doc.split('\n-->\n')[0].split('\n', 1)[1])
    entry = manifest['items'][0]
    assert set(entry) == {'id', 'kind', 'tags', 'bytes', 'sha256', 'source'}
    for word in ('trust', 'approved', 'standing', 'activation', 'internal-record-id', 'blk', 'owner', 'workspace', 'confidence', 'requirement', 'permission'):
        assert word not in doc.lower().replace('owner-abc123', '').replace('not the whole memory', ''), word
    assert not re.search(r'internal', doc)


def test_secrets_are_omitted_not_altered():
    secret = 'password = hunter2hunter2'
    doc = export([rec('fine'), rec(secret), rec('uri', source_uri='https://x/?api_key=abcdefabcdef')], is_secret=None)
    bundle = f.parse_markdown(doc)
    assert [i.text for i in bundle.items] == ['fine']  # text secret and secret-bearing source uri both omitted
    assert {'reason': 'secret_detected', 'count': 2} in bundle.omissions
    assert 'hunter2' not in doc and 'abcdefabcdef' not in doc


def test_bounds_truncate_at_item_boundary_and_say_so():
    records = [rec(f'memory number {i}') for i in range(7)]
    bundle = f.parse_markdown(export(records, max_items=3))
    assert len(bundle.items) == 3 and {'reason': 'export_item_bound', 'count': 4} in bundle.omissions
    big = [rec('x' * 2000 + str(i)) for i in range(30)]
    doc = export(big, max_bytes=12000)
    assert len(doc.encode()) <= 12000
    parsed = f.parse_markdown(doc, max_bytes=12000)
    dropped = 30 - len(parsed.items)
    assert 0 < len(parsed.items) < 30 and {'reason': 'export_size_bound', 'count': dropped} in parsed.omissions
    assert [i.text for i in parsed.items] == [r['text'] for r in big[:len(parsed.items)]]
    too_big = export([rec('y' * (f.MAX_ITEM_BYTES + 1)), rec('ok')])
    assert {'reason': 'item_too_large', 'count': 1} in f.parse_markdown(too_big).omissions
    caller = export([rec('a')], omissions=[{'reason': 'source_read_bound', 'count': 1, 'at_least': True}])
    assert f.parse_markdown(caller).omissions == ({'reason': 'source_read_bound', 'count': 1, 'at_least': True},)


def test_invalid_records_are_omitted():
    bundle = f.parse_markdown(export([rec('ok'), {'text': '   '}, {'text': None}, rec('\ud800 lone')]))
    assert len(bundle.items) == 1 and {'reason': 'invalid_record', 'count': 3} in bundle.omissions


def test_refusals_use_closed_codes():
    good = export([rec('alpha'), rec('beta')])
    refused('# just markdown\n', 'missing_manifest')
    refused('', 'missing_manifest')
    refused(good.replace('/1\n', '/2\n', 1), 'unsupported_version')
    refused(good.replace('"version": 1', '"version": 2', 1), 'unsupported_version')
    refused(good.replace('"bytes": 5', '"bytes": 500', 1), 'malformed_body')
    refused(good + '\n' + good, 'duplicate_manifest')
    refused(good + 'trailing prose\n', 'malformed_body')
    refused(good.replace('alpha', 'ALPHA'), 'item_hash_mismatch')
    refused(good.replace('"kind": "fact"', '"kind": "Fact"', 1), 'bad_manifest')
    refused(good, 'file_too_large', max_bytes=100)
    refused(good.replace('"state": "unresolved"', '"state": "maybe"', 1), 'bad_manifest')
    refused(good.replace('\n-->\n', '\n', 1), 'bad_manifest')
    refused(b'\xff\xfe', 'malformed_body')


def test_duplicate_item_id_and_oversize_item_and_too_many():
    base = export([rec('alpha'), rec('beta')])
    manifest = json.loads(base.split('\n-->\n')[0].split('\n', 1)[1])
    ids = [e['id'] for e in manifest['items']]
    refused(base.replace(ids[1], ids[0]), 'duplicate_item_id')
    big = base.replace('"bytes": 5', '"bytes": %d' % (f.MAX_ITEM_BYTES + 1), 1)
    refused(big, 'item_too_large')
    many = {**manifest, 'items': [{**manifest['items'][0], 'id': 'item-%016x' % n} for n in range(f.MAX_ITEMS + 1)]}
    doc = f._TOKEN + ' ' + f.FORMAT + '/1\n' + f._dumps(many) + '\n-->\n'
    refused(doc, 'too_many_items')


def test_nothing_partial_and_unknown_fields_refused():
    good = export([rec('alpha')])
    refused(good.replace('"bytes"', '"bytez"', 1), 'bad_manifest')
    refused(good.replace('"tags": []', '"tags": [], "trust": "approved"', 1), 'bad_manifest')
    refused(good[:-30], 'malformed_body')
