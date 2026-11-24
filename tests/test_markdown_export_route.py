"""Stubbed route coverage for GET /export/markdown and POST /import/markdown (no app, store or model)."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import ast
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.memory.markdown_export import markdown_import_result, origin_label, register_markdown_export
from src import memery_formats as f

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)


def row(text, owner='owner-1', **kw):
    return {'id': 'rec-' + text, 'owner': owner, 'text': text, 'kind': 'fact', 'workspace_id': 'ws-1',
            'trust': {'state': 'approved'}, **kw}


def make(rows=(), authed=True, live=True, guarded=True, fail=None, **extra):
    seen = {'lists': [], 'lives': 0}
    @asynccontextmanager
    async def admitted(user):
        yield
    def current():
        seen['lives'] += 1
        return live(seen['lives']) if callable(live) else live
    ctx = {'owner': 'owner-1', 'workspace': 'ws-1', 'barrier': SimpleNamespace(admitted=admitted),
           'current_live': current, 'current_write': current}
    def require_human(request):
        if not authed: raise HTTPException(401, 'no')
        return 'alice'
    async def list_memories(**kw):
        seen['lists'].append(kw)
        if fail: raise fail
        return list(rows)
    provider = SimpleNamespace(_guarded_v17=guarded, list_memories=list_memories)
    async def owned(fn, *a, **k): return fn(*a, **k)
    router = APIRouter(prefix='/api/memory')
    register_markdown_export(router, require_human=require_human, capture_context=lambda r, u: ctx, owned=owned,
                             provider=provider, clock=lambda: NOW, **extra)
    app = FastAPI(); app.include_router(router)
    return TestClient(app), seen


def test_auth_refused_reads_nothing():
    client, seen = make([row('a')], authed=False)
    assert client.get('/api/memory/export/markdown').status_code == 403 and not seen['lists']


def test_authority_change_fails_closed():
    client, _ = make([row('zzmemory')], live=lambda n: n < 2)
    r = client.get('/api/memory/export/markdown')
    assert r.status_code == 503 and 'zzmemory' not in r.text


def test_export_download_headers_and_scoping():
    rows = [row('mine one'), row('theirs', owner='owner-2'), row('mine two')]
    client, seen = make(rows)
    r = client.get('/api/memory/export/markdown')
    assert r.status_code == 200
    assert r.headers['content-type'] == 'text/markdown; charset=utf-8'
    assert r.headers['content-disposition'] == 'attachment; filename="openclank-memory-20261110.md"'
    assert r.headers['cache-control'] == 'private, no-store'
    assert seen['lists'] == [{'owner': 'owner-1', 'limit': 5000}]
    bundle = f.parse_markdown(r.content, max_bytes=f.MAX_FILE_BYTES)
    assert sorted(i.text for i in bundle.items) == ['mine one', 'mine two']
    assert 'theirs' not in r.text and 'owner-1' not in r.text
    assert bundle.origin == origin_label('owner-1') and bundle.generated_at == '2026-11-10T12:00:00Z'
    assert 'trust' not in r.text.lower() and 'approved' not in r.text


def test_export_truncation_states_omissions():
    rows = [row(f'memory {i}') for i in range(f.MAX_ITEMS + 5)]
    client, _ = make(rows)
    bundle = f.parse_markdown(client.get('/api/memory/export/markdown').content)
    assert len(bundle.items) == f.MAX_ITEMS and {'reason': 'export_item_bound', 'count': 5} in bundle.omissions
    full = [row(f'm{i}') for i in range(5000)]
    client, _ = make(full)
    parsed = f.parse_markdown(client.get('/api/memory/export/markdown').content)
    assert {'reason': 'source_read_bound', 'count': 1, 'at_least': True} in parsed.omissions


def test_export_omits_secret_text_with_reason():
    client, _ = make([row('fine'), row('api_key = abcdef123456789')])
    bundle = f.parse_markdown(client.get('/api/memory/export/markdown').content)
    assert [i.text for i in bundle.items] == ['fine'] and {'reason': 'secret_detected', 'count': 1} in bundle.omissions


def _doc(owner='owner-1', texts=('alpha\n  indented', 'beta')):
    return f.export_markdown([row(t) for t in texts], owner_label=origin_label(owner), generated_at='2026-11-10T12:00:00Z',
                             is_secret=lambda t: False).encode('utf-8')


def test_hook_returns_exact_drafts_with_no_trust_fields():
    result = markdown_import_result(ext='.md', content=_doc(), owner='owner-1')
    assert result['suggestions'] == [{'text': 'alpha\n  indented', 'category': 'fact'}, {'text': 'beta', 'category': 'fact'}]
    assert result['foreign'] is False and result['origin'] == origin_label('owner-1') and result['item_count'] == 2
    assert all(set(s) == {'text', 'category'} for s in result['suggestions'])


def test_foreign_bundle_is_flagged_but_still_importable():
    result = markdown_import_result(ext='.md', content=_doc(owner='someone-else'), owner='owner-1')
    assert result['foreign'] is True and result['origin'] == origin_label('someone-else') and len(result['suggestions']) == 2


def test_bad_manifest_is_http_400_with_closed_code():
    good = _doc().decode()
    for payload, code in ((good.replace('alpha', 'ALPHA'), 'item_hash_mismatch'), (good.replace('/1\n', '/9\n', 1), 'unsupported_version'),
                          (good + good, 'duplicate_manifest')):
        try:
            markdown_import_result(ext='.md', content=payload.encode(), owner='owner-1')
        except HTTPException as exc:
            assert exc.status_code == 400 and exc.detail['code'] == code
        else:
            raise AssertionError('accepted ' + code)
    try:
        markdown_import_result(ext='.md', content=_doc(), owner='owner-1', max_bytes=50)
    except HTTPException as exc:
        assert exc.detail['code'] == 'file_too_large'
    else:
        raise AssertionError('oversize accepted')


def test_hook_not_entered_without_manifest_token_or_md_extension():
    assert markdown_import_result(ext='.md', content=b'# My notes\n\nI like tea.\n', owner='owner-1') is None
    assert markdown_import_result(ext='.md', content=b' ' + _doc(), owner='owner-1') is None
    assert markdown_import_result(ext='.txt', content=_doc(), owner='owner-1') is None
    assert markdown_import_result(ext='.json', content=b'[]', owner='owner-1') is None


def test_upload_import_calls_hook_before_json_and_extractor_paths():
    # _import_memories_from_file_inner is a closure inside the router factory (no direct test seam exists, as for
    # the .json fast path), so pin its wiring structurally: hook, mirror and return precede the .json branch and extractor.
    src = Path(__file__).resolve().parent.parent.joinpath('routes/memory/memory_routes.py').read_text(encoding='utf-8')
    ast.parse(src)
    start = src.index('async def _import_memories_from_file_inner')
    body = src[start:start + 30000]
    hook, json_branch, extractor = (body.index(x) for x in ('markdown_import_result(ext=ext', 'if ext == ".json":', 'complete_text('))
    assert hook < json_branch < extractor
    block = body[hook:json_branch]
    assert '_mirror_import_result(direct)' in block and '"foreign": markdown_result["foreign"]' in block and 'return {' in block


def test_legacy_profile_exports_without_paired_authority():
    # Legacy provider has no guarded preparation: capture_context/current_live would refuse, so they must not be used.
    client, seen = make([row('legacy one', owner='alice'), row('theirs', owner='owner-2')], guarded=False, live=False)
    # capture_context in make() is irrelevant on legacy; the owner comes from memory_owner(user) = 'alice'.
    r = client.get('/api/memory/export/markdown')
    assert r.status_code == 200 and seen['lists'] == [{'owner': 'alice', 'limit': 5000}] and seen['lives'] == 0
    bundle = f.parse_markdown(r.content)
    assert [i.text for i in bundle.items] == ['legacy one']
    assert bundle.origin == origin_label('alice') and 'theirs' not in r.text


def test_unexpected_failure_is_logged_by_class_without_memory_text_and_returns_503():
    import logging
    records = []
    class Capture(logging.Handler):
        def emit(self, record): records.append(record)
    handler = Capture(); log = logging.getLogger('routes.memory.markdown_export'); log.addHandler(handler)
    failing, _ = make([row('private memory text')], fail=KeyError('detail'))
    try:
        r = failing.get('/api/memory/export/markdown')
    finally:
        log.removeHandler(handler)
    assert r.status_code == 503 and r.json() == {'state': 'unavailable'}
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    text = records[0].getMessage()
    assert 'KeyError' in text and 'private memory text' not in text and 'owner-1' not in text
