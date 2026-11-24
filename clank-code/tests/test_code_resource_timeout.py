"""Exact HTTP timeout contract without starting the app or native services."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def timeout_namespace():
    # Execute the production policy and dispatch method, not a copied matcher.
    tree = ast.parse(Path('app.py').read_text())
    selected = [node for node in tree.body if
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_TIMEOUT_EXEMPT_PREFIXES' for t in node.targets)
        or isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {'_is_timeout_exempt', '_RequestTimeoutMiddleware'}]
    assert len(selected) == 3
    namespace = {'_asyncio': asyncio, '_BaseHTTPMiddleware': BaseHTTPMiddleware,
        '_JSONResponse': JSONResponse, 'REQUEST_HARD_TIMEOUT': 45}
    exec(compile(ast.Module(body=selected, type_ignores=[]), 'app.py', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('method', ['POST', 'post'])
def test_only_exact_resource_post_is_exempt(method):
    assert timeout_namespace()['_is_timeout_exempt']('/api/memory/code/resource', method)


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'PUT', 'DELETE', 'OPTIONS', None])
def test_other_resource_methods_retain_generic_timeout(method):
    assert not timeout_namespace()['_is_timeout_exempt']('/api/memory/code/resource', method)


@pytest.mark.parametrize('path', ['/api/memory/code/graph', '/api/memory/code/resources',
    '/api/memory/code/resource/', '/api/memory/code/resource/resolve', '/api/memory/code/resource-extra',
    '/api/file-policy/workspaces/example/index/status', '/api/files-v1/open-resource'])
def test_neighbors_children_and_status_are_not_exempt(path):
    assert not timeout_namespace()['_is_timeout_exempt'](path, 'POST')


def test_existing_prefixes_and_generic_default_are_unchanged():
    source = ast.parse(Path('app.py').read_text())
    assignment = next(node for node in source.body if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'REQUEST_HARD_TIMEOUT' for t in node.targets))
    assert ast.unparse(assignment.value) == "float(os.getenv('REQUEST_HARD_TIMEOUT', '45'))"
    namespace = timeout_namespace()
    assert namespace['_TIMEOUT_EXEMPT_PREFIXES'] == ('/api/chat','/api/shell/stream','/api/research',
        '/api/model/download','/api/cookbook/setup','/api/upload','/api/image','/api/memory/audit','/api/memory/import-batches')
    assert '/api/memory/code/resource' not in namespace['_TIMEOUT_EXEMPT_PREFIXES']


def request(path, method):
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


def test_compound_post_can_outlast_generic_deadline_without_new_request():
    async def run():
        namespace = timeout_namespace();namespace['REQUEST_HARD_TIMEOUT'] = .005
        middleware = namespace['_RequestTimeoutMiddleware'](None);calls = []
        async def next_handler(req):
            calls.append((req.url.path, req.method));await asyncio.sleep(.02)
            return JSONResponse({'state': 'current'})
        response = await middleware.dispatch(request('/api/memory/code/resource', 'POST'), next_handler)
        assert response.status_code == 200
        assert calls == [('/api/memory/code/resource', 'POST')]
    asyncio.run(run())


@pytest.mark.parametrize('path,method', [('/api/memory/code/graph','POST'),
    ('/api/memory/code/resource','GET'),('/api/memory/code/resource/resolve','POST'),
    ('/api/file-policy/workspaces/example/index/status','POST')])
def test_other_slow_requests_still_return_gateway_timeout(path, method):
    async def run():
        namespace = timeout_namespace();namespace['REQUEST_HARD_TIMEOUT'] = .005
        middleware = namespace['_RequestTimeoutMiddleware'](None)
        async def next_handler(_):
            await asyncio.sleep(.02);return JSONResponse({'unexpected': True})
        assert (await middleware.dispatch(request(path, method), next_handler)).status_code == 504
    asyncio.run(run())


def test_explicit_cancellation_still_propagates_through_exempt_dispatch():
    # This tests an explicit task cancellation, not an immediate browser
    # disconnect guarantee (ASGI/provider disconnect behavior is separate).
    async def run():
        namespace = timeout_namespace();middleware = namespace['_RequestTimeoutMiddleware'](None)
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def next_handler(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        task = asyncio.create_task(middleware.dispatch(request('/api/memory/code/resource','POST'), next_handler))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
    asyncio.run(run())
