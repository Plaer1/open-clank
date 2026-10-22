"""Real Linux Files HTTP, Editor save/CAS and disposable native process journey."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import stat

import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
import uvicorn

from src.openclank.files_host_provider import _origin
from src.openclank.files_service_client import OdysseusFilesClient
from src.openclank.resource_refs import issue_resource_ref
from tests.test_files_facade_routes import _app
from tests.test_linux_host_apps import desktop  # reusable isolated XDG recording fixture

pytestmark = pytest.mark.skipif(not os.environ.get('ODYSSEUS_FILES_SERVICE_BIN'), reason='explicit native Files binary required')


async def test_actual_linux_files_editor_bytes_and_conflict(tmp_path):
    clients = {}
    def native(owner, scope):
        key = (owner, json.dumps(scope, sort_keys=True))
        if key not in clients:
            clients[key] = OdysseusFilesClient(owner, app_scope=scope, transport='grpc', binary=os.environ['ODYSSEUS_FILES_SERVICE_BIN'])
        return clients[key]
    app, _, sessions = _app(tmp_path, native)
    root = tmp_path / 'workspace ü space'
    root.mkdir()
    target = root / 'Editor ü.txt'
    original = 'one\ntwo\n'
    target.write_text(original)
    target.chmod(0o640)
    import zlib, struct
    png = root / 'KDE native colors.png'
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
    colors = [b'\xe8\x4f\x63', b'\x4c\xa6\xe8', b'\x76\xc2\x83', b'\xff\xd1\x66']
    pixels = b''.join(b'\0' + b''.join(colors[(x//32+y//24)%4] for x in range(128)) for y in range(96))
    png.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB',128,96,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(pixels)) + chunk(b'IEND',b''))
    ui_folder = None
    utf16 = root / 'BOM CRLF.txt'
    utf16.write_bytes(b'\xff\xfe' + 'first\r\nsecond\r\n'.encode('utf-16-le'))
    utf16.chmod(0o640)
    generation = app.state.file_policy_repository.generation()
    def ref(path, capabilities, kind='file'):
        return issue_resource_ref(owner_subject_id='account-alice', provider='host', origin_id=_origin(str(path)),
            kind=kind, capabilities=capabilities, policy_generation=generation).token
    target_ref = ref(target, ['stat', 'open', 'download', 'preview'])
    unicode_ref = ref(utf16, ['stat', 'open'])
    root_ref = ref(root, ['stat', 'children', 'write'], 'folder')
    token = secrets.token_urlsafe(32)
    @app.middleware('http')
    async def guard(request, call_next):
        if not secrets.compare_digest(request.headers.get('x-linux-fixture', ''), token):
            return JSONResponse({'error': 'fixture_auth_required'}, status_code=401)
        return await call_next(request)
    @app.get('/__fixture/info')
    async def info():
        return {'resource_ref': target_ref, 'original': original, 'folder': ui_folder, 'thumbnail_name': png.name}
    @app.get('/__fixture/disk')
    async def disk():
        return {'text': target.read_text(), 'mode': stat.S_IMODE(target.stat().st_mode)}
    @app.post('/__fixture/external')
    async def external():
        target.write_text('external edit\n')
        return {'ok': True}
    headers = {'x-linux-fixture': token}
    listener = None; server = None; task = None; child = None
    try:
        assert (await native('alice', {'host': True}).request('health', '.'))['ready'] is True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture', headers=headers) as client:
            page = await client.post('/api/files-v1/children', json={'parent_ref': root_ref})
            assert page.status_code == 200, page.text
            assert {r['name'] for r in page.json()['entries']} == {target.name, utf16.name, png.name}
            native_image = next(row for row in page.json()['entries'] if row['name'] == png.name)
            assert native_image['native_thumbnail_available'] is True
            ui_folder = (await client.post('/api/files-v1/stat',json={'resource_ref':root_ref})).json()
            opened = await client.post('/api/files-v1/open-resource', json={'resource_ref': unicode_ref})
            assert opened.status_code == 200, opened.text
            payload = opened.json()['payload']
            saved = await client.post('/api/files-v1/save-resource', json={'resource_ref': unicode_ref,
                'expected_revision': payload['resource']['revision'], 'text': 'changed\nsecond\n'})
            assert saved.status_code == 200, saved.text
            assert saved.json()['outcome'] == 'applied'
            assert utf16.read_bytes() == b'\xff\xfe' + 'changed\r\nsecond\r\n'.encode('utf-16-le')
            assert stat.S_IMODE(utf16.stat().st_mode) == 0o640
            content = await client.get('/api/files-v1/content/' + target_ref, headers={'range': 'bytes=1-5'})
            assert content.status_code == 206, content.text
            assert content.content == original.encode()[1:6]
            assert content.headers['content-range'] == f'bytes 1-5/{len(original.encode())}'
            created = await client.post('/api/files-v1/create', json={'parent_ref': root_ref, 'name': 'created ü.md', 'text': 'created\n'})
            assert created.status_code == 200, created.text
            assert (root / 'created ü.md').read_bytes() == b'created\n'
            fresh_ref = created.json()['resource']['ref']
            renamed = await client.post('/api/files-v1/action', json={'resource_ref': fresh_ref, 'action': 'rename', 'args': {'name': 'renamed ü.md'}})
            assert renamed.status_code == 200, renamed.text
            assert (root / 'renamed ü.md').read_bytes() == b'created\n'
        listener = socket.socket(); listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=0, log_level='error', lifespan='off'))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started: await asyncio.sleep(.01)
        child = await asyncio.create_subprocess_exec('node', 'tests/linux_editor_real_backend_acceptance.mjs',
            env={**os.environ, 'LINUX_BACKEND_URL': f'http://127.0.0.1:{listener.getsockname()[1]}', 'LINUX_FIXTURE_TOKEN': token},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async with asyncio.timeout(120):
            output, _ = await child.communicate()
        assert child.returncode == 0, output.decode(errors='replace')
        print(output.decode())
        assert target.read_bytes() == b'external edit\n'
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
    finally:
        if child and child.returncode is None:
            child.kill(); await child.wait()
        if server: server.should_exit = True
        if task: await task
        if listener: listener.close()
        for client in clients.values():
            process = client.process
            socket_path = client._grpc_socket_path
            client.close()
            if process: assert process.poll() is not None
            if isinstance(socket_path, Path): assert not socket_path.exists()
        sessions.kw['bind'].dispose()


async def test_real_host_and_managed_gio_dispatch(tmp_path, desktop, monkeypatch):
    from core.database import FilesImageResource
    from src.generated_images import resolve_gallery_image_path
    target, entry, record = desktop
    monkeypatch.setenv('WAYLAND_DISPLAY', os.environ.get('WAYLAND_DISPLAY') or 'fixture-session')
    clients = []
    def native(owner, scope):
        client = OdysseusFilesClient(owner, app_scope=scope, transport='grpc', binary=os.environ['ODYSSEUS_FILES_SERVICE_BIN'])
        clients.append(client)
        return client
    app, owner, sessions = _app(tmp_path, native)
    data = Path(os.environ['OPEN_CLANK_DATA_DIR']).resolve()
    assert data.name.startswith('openclank-s03-s06-projection.')
    managed = resolve_gallery_image_path('linux-managed.txt')
    assert managed.is_relative_to(data)
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text('managed file recording fixture\n')
    with sessions() as db:
        db.add(FilesImageResource(id='linux-managed', owner='alice', kind='image', display_name='Managed fixture.txt',
            locator=managed.name, mime_type='text/plain', size=managed.stat().st_size, is_active=True))
        db.commit()
    generation = app.state.file_policy_repository.generation()
    host = issue_resource_ref(owner_subject_id='account-alice', provider='host', origin_id=_origin(str(target)),
        kind='file', capabilities=['stat','open'], policy_generation=generation).token
    managed_ref = issue_resource_ref(owner_subject_id='account-alice', provider='files', origin_id='image:linux-managed',
        kind='file', capabilities=['stat','open'], policy_generation=generation).token
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            for resource_ref, expected in ((host, target), (managed_ref, managed)):
                discovered = await client.post('/api/files-v1/host-apps', json={'resource_ref':resource_ref})
                assert discovered.status_code == 200, discovered.text
                assert discovered.json()['applications'] == [{'id':entry.name,'name':'Open Clank Recorder','default':True}]
                record.unlink(missing_ok=True)
                launched = await client.post('/api/files-v1/open-host', json={'resource_ref':resource_ref,'app_id':entry.name})
                assert launched.status_code == 200, launched.text
                async with asyncio.timeout(5):
                    while not record.exists(): await asyncio.sleep(.01)
                assert json.loads(record.read_text()) == [str(expected)]
            owner['value'] = 'bob'
            denied = await client.post('/api/files-v1/open-host', json={'resource_ref':managed_ref,'app_id':entry.name})
            assert denied.status_code == 404
    finally:
        for client in clients: client.close()
        managed.unlink(missing_ok=True)
        sessions.kw['bind'].dispose()
