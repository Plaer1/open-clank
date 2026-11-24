"""Stubbed-Graph coverage for the structure-only code wiki planner (no model, no network)."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from src.openclank import code_wiki

RECEIPT = {'workspace_id': 'ws', 'registration_id': 'r', 'source_snapshot_id': 'snap-1', 'checkout_id': 'c',
           'head_generation': 'h', 'admission_epoch': 1, 'policy_generation': 2, 'config_fingerprint': 'f'}


def fid(n):
    return 'file:' + hashlib.sha256(str(n).encode()).hexdigest()


def env(operation, hits, receipt=RECEIPT, **extra):
    return {'schema': 'open-clank.code-query/v1', 'operation': operation, 'receipt': receipt, 'hits': hits, 'edges': [],
            'omissions': [], 'partial': False, 'degraded': False, 'next_cursor': None, 'error': None, **extra}


def file_hit(n, path=None):
    path = path or f'src/m{n:03d}.py'
    return {'id': fid(n), 'kind': 'file', 'label': path, 'properties': {'rel_path': path}}


def stub(files, page_size=200, receipt=RECEIPT):
    calls = []

    async def query(q):
        calls.append(q)
        start = int(q.get('cursor') or 0)
        chunk = files[start:start + page_size]
        more = str(start + page_size) if start + page_size < len(files) else None
        return env('catalog', chunk, receipt, next_cursor=more)
    query.calls = calls
    return query


def run(coro):
    return asyncio.run(coro)


def body(**kw):
    return {'workspace_id': 'ws', **kw}


def test_default_cap_and_explicit_omission():
    result = run(code_wiki.plan(body(), query=stub([file_hit(n) for n in range(40)], page_size=15)))
    assert result['cap'] == 25 and len(result['pages']) == 25
    assert any('more than 25 files' in o for o in result['omissions'])
    assert [p['rel_path'] for p in result['pages']] == sorted(p['rel_path'] for p in result['pages'])


def test_max_cap_and_over_max_refused():
    result = run(code_wiki.plan(body(cap=100), query=stub([file_hit(n) for n in range(150)])))
    assert len(result['pages']) == 100 and result['omissions']
    for bad in (101, 0, True, '5'):
        try:
            run(code_wiki.plan(body(cap=bad), query=stub([])))
        except ValueError:
            continue
        raise AssertionError(f'cap {bad!r} accepted')


def test_fewer_files_than_cap_has_no_omissions():
    result = run(code_wiki.plan(body(), query=stub([file_hit(n) for n in range(3)])))
    assert len(result['pages']) == 3 and result['omissions'] == []
    assert 'no omissions were recorded' in code_wiki.render_index(result)


def test_degraded_catalog_records_omission_and_no_completeness_claim():
    async def query(q):
        return env('catalog', [file_hit(1)], partial=True, degraded=True)
    result = run(code_wiki.plan(body(), query=query))
    assert result['omissions']
    index = code_wiki.render_index(result)
    assert 'Coverage: incomplete' in index and 'no omissions were recorded' not in index


def test_graph_error_refused():
    async def query(q):
        return {**env('catalog', []), 'error': {'code': 'unavailable', 'message': 'x'}}
    try:
        run(code_wiki.plan(body(), query=query))
    except ValueError:
        return
    raise AssertionError('error result accepted')


def test_plan_pins_one_receipt_and_refuses_drift():
    other = {**RECEIPT, 'source_snapshot_id': 'snap-2'}
    seen = []

    async def query(q):
        seen.append(q)
        if q.get('cursor'):
            return env('catalog', [file_hit(2)], other)
        return env('catalog', [file_hit(1)], RECEIPT, next_cursor='1')
    try:
        run(code_wiki.plan(body(), query=query))
    except ValueError:
        assert seen[1]['expected_receipt'] == RECEIPT
        return
    raise AssertionError('receipt drift accepted')


def one_page(path='src/a.py'):
    result = run(code_wiki.plan(body(), query=stub([file_hit(1, path)])))
    return result['pages'][0], result


def outline_for(page, names=('f',), receipt=RECEIPT, **extra):
    hits = [{'id': 'decl:' + hashlib.sha256(n.encode()).hexdigest(), 'kind': 'declaration', 'label': n, 'signature': 'def ' + n + '()',
             'properties': {'declaration_kind': 'function'}} for n in names]
    return env('outline', hits, receipt, **extra)


def neighbors_for(page, outline, receipt=RECEIPT, **extra):
    inside = outline['hits'][0]['id']
    other, other2 = 'file:' + 'a' * 64, 'file:' + 'b' * 64
    return env('neighbors', [{'id': other, 'kind': 'file', 'label': 'lib/dep.py'}, {'id': other2, 'kind': 'file', 'label': 'app/use.py'}], receipt,
               edges=[{'from': inside, 'to': other, 'relation_kind': 'imports', 'resolution_state': 'resolved'},
                      {'from': other2, 'to': inside, 'relation_kind': 'calls', 'resolution_state': 'resolved'}], **extra)


def test_render_deterministic_and_content():
    page, _ = one_page()
    out = outline_for(page)
    nb = neighbors_for(page, out)
    first = code_wiki.render_page(page, out, nb, receipt=RECEIPT)
    assert first == code_wiki.render_page(page, out, nb, receipt=RECEIPT)
    assert first.encode() == code_wiki.render_page(page, out, nb, receipt=RECEIPT).encode()
    assert 'function: f' in first and 'imports' in first and 'calls' in first and 'Incoming relations' in first
    assert first.startswith(f"<!-- openclank-wiki-page:v1:{page['page_id']} -->\n")


def test_marker_stable_and_tracks_snapshot():
    page, _ = one_page()
    expected = hashlib.sha256((page['file_id'] + '|snap-1').encode()).hexdigest()
    assert page['page_id'] == expected
    again, _ = one_page()
    assert again['page_id'] == page['page_id']
    other = {**RECEIPT, 'source_snapshot_id': 'snap-2'}
    moved = run(code_wiki.plan(body(), query=stub([file_hit(1, 'src/a.py')], receipt=other)))['pages'][0]
    assert moved['page_id'] != page['page_id']


def test_receipt_mismatch_refused():
    page, _ = one_page()
    out = outline_for(page)
    other = {**RECEIPT, 'source_snapshot_id': 'snap-2'}
    cases = [(page, out, neighbors_for(page, out, receipt=other), RECEIPT),
             (page, outline_for(page, receipt=other), neighbors_for(page, out), RECEIPT),
             (page, out, neighbors_for(page, out), other)]
    for p, o, n, r in cases:
        try:
            code_wiki.render_page(p, o, n, receipt=r)
        except ValueError:
            continue
        raise AssertionError('mismatched receipt rendered')


def test_hostile_names_escaped():
    evil = 'x/<script>alert(1)</script>\n## injected [a](http://e.x) |\r\n`z`.py'
    page, plan_result = one_page(evil)
    out = outline_for(page, names=('<script>\n# boom',))
    out['hits'][0]['signature'] = '<img src=x onerror=1>\n- [l](u)'
    text = code_wiki.render_page(page, out, neighbors_for(page, out), receipt=RECEIPT)
    index = code_wiki.render_index(plan_result)
    for doc in (text, index):
        assert '<script' not in doc and '<img' not in doc and '](' not in doc
        assert not any(line.startswith('## injected') or line.startswith('# boom') for line in doc.splitlines())
    assert sum(1 for line in text.splitlines() if line.startswith('## ')) == 1


def test_very_long_names_truncated():
    page, _ = one_page('d/' + 'n' * 5000 + '.py')
    out = outline_for(page, names=('q' * 9000,))
    text = code_wiki.render_page(page, out, neighbors_for(page, out), receipt=RECEIPT)
    assert 'q' * 600 not in text and len(text.encode()) < code_wiki.PAGE_BYTES


def test_page_byte_cap_enforced_with_omission():
    page, _ = one_page()
    out = outline_for(page, names=tuple(f'n{i}<' * 30 + str(i) for i in range(200)))
    for hit in out['hits']:
        hit['signature'] = '<' * 500
    text = code_wiki.render_page(page, out, neighbors_for(page, out), receipt=RECEIPT)
    assert len(text.encode()) <= code_wiki.PAGE_BYTES
    assert 'omitted by the page byte cap' in text


def test_partial_outline_recorded_on_page():
    page, _ = one_page()
    out = outline_for(page, partial=True, next_cursor='9')
    text = code_wiki.render_page(page, out, neighbors_for(page, out), receipt=RECEIPT)
    assert 'Outline was partial or degraded' in text


def test_fetch_page_queries_pinned_receipt():
    page, _ = one_page()
    out = outline_for(page)
    seen = []

    async def query(q):
        seen.append(q)
        return out if q['operation'] == 'outline' else neighbors_for(page, out)
    text = run(code_wiki.fetch_page(page, query=query, receipt=RECEIPT))
    assert [q['operation'] for q in seen] == ['outline', 'neighbors']
    assert all(q['expected_receipt'] == RECEIPT for q in seen) and 'function: f' in text


def test_index_grouped_by_directory_and_hash_stable():
    files = [file_hit(1, 'a/x.py'), file_hit(2, 'b/y.py'), file_hit(3, 'top.py')]
    one = run(code_wiki.plan(body(), query=stub(files)))
    two = run(code_wiki.plan(body(), query=stub(files)))
    assert one['plan_hash'] == two['plan_hash']
    index = code_wiki.render_index(one)
    assert index == code_wiki.render_index(two)
    assert index.index('### a') < index.index('### b') and '### &#40;root&#41;' in index


def test_no_model_code_in_module():
    source = (Path(__file__).resolve().parents[1] / 'src' / 'openclank' / 'code_wiki.py').read_text(encoding='utf-8')
    for banned in ('chat.complete', 'code_summary.generate', 'execute_model_operation', 'operation_router', 'modality_facade', 'import code_summary\n', 'code_summary import'):
        assert banned not in source, banned
