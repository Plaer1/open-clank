#!/usr/bin/env python3
"""One-shot normal-auth TreeHouse candidate. No live app, providers or restart loop."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def build_candidate(data_root: Path, *, ui_fixture: Path | None = None):
    # Configure before importing modules with default store paths. Explicit
    # AuthManager/Files/repository constructors below additionally bind stores.
    data_root = data_root.resolve()
    os.environ.update(OPEN_CLANK_DATA_DIR=str(data_root), ODYSSEUS_DATA_DIR=str(data_root),
                      AUTH_ENABLED='true', TREEHOUSE_REPOSITORY_PATH=str(data_root / 'treehouse.sqlite3'),
                      DATABASE_URL='sqlite:///' + str(data_root / 'application.sqlite3'),
                      FM_DB_PATH=str(data_root / 'frankenmemory.sqlite3'))
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from core.auth import AuthManager
    from core.database import SessionLocal, ApiToken
    from routes.auth_routes import setup_auth_routes, SESSION_COOKIE
    from routes.copal_treehouse_routes import setup_treehouse_routes
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.copal_treehouse_repository import TreeHouseRepository
    from src.openclank.request_auth import install_request_auth
    from src.openclank.treehouse_global_catalogue import load_global_catalogue
    from src.auth_helpers import copal_owner_for_user

    app = FastAPI()
    manager = AuthManager(str(data_root / 'auth.json'))
    app.state.auth_manager = manager
    app.state.treehouse_repository = TreeHouseRepository(data_root / 'treehouse.sqlite3')
    app.state.files_policy_repository = FilePolicyRepository(data_root / 'file-policy.sqlite3')
    app.state.treehouse_global_catalogue = load_global_catalogue()
    async def scoped_next(request, call_next, owner):
        return await call_next(request)
    install_request_auth(app, auth_manager=manager, session_factory=SessionLocal,
                         api_token_model=ApiToken, account_call_next=scoped_next,
                         trusted_loopback=lambda request: False,
                         desktop_ready_path='/candidate-desktop-ready', session_cookie=SESSION_COOKIE)
    # Never admit internal-tool or forged API-token identities in this fixture.
    @app.middleware('http')
    async def cookie_only(request, call_next):
        from fastapi.responses import JSONResponse
        if request.headers.get('authorization') or request.headers.get('x-odysseus-internal-tool'):
            return JSONResponse(status_code=403, content={'error': 'Candidate supports normal cookie authentication only'})
        return await call_next(request)
    app.include_router(setup_auth_routes(manager))
    def scope_for(request, workspace):
        value = str(workspace or 'default')
        if not value.replace('-', '').replace('_', '').replace('.', '').isalnum() or len(value) > 64:
            from fastapi import HTTPException
            raise HTTPException(400, 'Invalid candidate workspace')
        return {'workspace_id': value, 'owner': copal_owner_for_user(getattr(request.state, 'current_user', None))}
    async def no_bridge(*args, **kwargs):
        from fastapi import HTTPException
        raise HTTPException(503, 'Copal document bridge is outside this bounded TreeHouse candidate')
    app.include_router(setup_treehouse_routes(call=no_bridge, scope_for=scope_for,
                      publish=lambda *_: None, policy_repository=app.state.files_policy_repository), prefix='/api/copal')
    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'candidate': 'treehouse-native-routes-normal-auth',
                'sourceId': app.state.treehouse_global_catalogue.source_id,
                'packageRevision': app.state.treehouse_global_catalogue.revision}
    @app.get('/')
    def page():
        return FileResponse(ui_fixture or PROJECT / 'static/index.html')
    @app.get('/login')
    def login_page():
        return FileResponse(PROJECT / 'static/login.html')
    @app.get('/candidate-ui')
    def candidate_ui():
        from fastapi import HTTPException
        path = data_root / 'ui.html'
        if not path.is_file(): raise HTTPException(503, 'UI owner has not supplied the mounted source fixture yet')
        return FileResponse(path)
    @app.post('/api/candidate/reload-curriculum')
    def reload_curriculum(request: __import__('fastapi').Request):
        from core.middleware import require_admin
        require_admin(request)
        app.state.treehouse_global_catalogue = load_global_catalogue()
        return {'sourceId': app.state.treehouse_global_catalogue.source_id,
                'packageRevision': app.state.treehouse_global_catalogue.revision,
                'packageDigest': app.state.treehouse_global_catalogue.manifest['packageDigest']}
    app.mount('/static', StaticFiles(directory=PROJECT / 'static'), name='static')
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--ui-fixture', type=Path)
    args = parser.parse_args()
    if args.port in {7777, 7796, 7797}:
        parser.error('Production/excluded ports are refused')
    root = Path(tempfile.mkdtemp(prefix='openclank-treehouse-candidate-'))
    if args.port:
        port = args.port
    else:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    app = build_candidate(root, ui_fixture=args.ui_fixture)
    from src.openclank.treehouse_global_catalogue import SOURCE_ID
    ready = {'url': f'http://127.0.0.1:{port}', 'dataRoot': str(root), 'pid': os.getpid(),
             'sourceId': SOURCE_ID, 'packageDigest': app.state.treehouse_global_catalogue.manifest['packageDigest'],
             'authentication': 'Actual AuthManager and installed request/auth route middleware; setup/login via HTTP',
             'coverage': 'Bounded native TreeHouse + auth + first-party static; no Copal bridge/provider/whole-app claim',
             'cleanup': f'Stop this PID once; remove only {root}; no restart automation'}
    (root / 'candidate.json').write_text(json.dumps(ready, indent=2))
    print(json.dumps(ready), flush=True)
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=port, log_level='warning')


if __name__ == '__main__': main()
