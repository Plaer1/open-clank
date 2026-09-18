"""Isolated production-memory server for the real Brain browser acceptance."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.models import Session
from core.operation_models import OperationBase
from core.provider_models import ProviderBase
from routes.memory.memory_routes import setup_memory_routes
from services.memory import MemoryManager
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_scope import CHAT_WORKSPACE
from src.openclank import operation_router
from src.openclank.artifacts import ArtifactStore
from src.openclank.operation_router import ManagedOperationRouter
from src.openclank.provider_store import ProviderStore


class _AuthManager:
    is_configured = True

    @staticmethod
    def get_privileges(_user):
        return {"can_manage_memory": True}


class _SessionLookup:
    def __init__(self, endpoint_url: str):
        self.session = Session(
            id="memory-browser-session",
            name="Real Brain browser acceptance",
            endpoint_url=endpoint_url,
            model="deterministic-memory-extractor",
            headers={"X-Test-Endpoint": "real-memory-browser"},
            owner="alice",
        )
        self.sessions = {self.session.id: self.session}

    def get_session(self, session_id: str):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session


class _Identity:
    """Bind every request on this isolated listener to its sole test tenant."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["current_user"] = "alice"
        await self.app(scope, receive, send)


def _model_server():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            content = json.dumps(
                [
                    {
                        "text": "Alice keeps the launch map in a violet notebook.",
                        "category": "project",
                    }
                ]
            )
            body = json.dumps(
                {
                    "id": "chatcmpl-real-memory-browser",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, thread, f"http://{host}:{port}/v1"


def _build_app(data_dir: Path, fm_binary: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    settings_path = data_dir / "settings.json"
    prefs_path = data_dir / "user_prefs.json"
    settings_path.write_text("{}", encoding="utf-8")
    prefs_path.write_text(
        json.dumps({"_users": {"alice": {"memory_mode": "automatic"}}}),
        encoding="utf-8",
    )

    import routes.prefs_routes as prefs_routes
    import src.settings as settings

    settings.SETTINGS_FILE = str(settings_path)
    prefs_routes.PREFS_FILE = str(prefs_path)
    settings._invalidate_caches()

    model_server, model_thread, endpoint_url = _model_server()
    # Admit one keyless local route through a probe-owned normalized store and
    # bind it independently to Utility and Memory so the production import
    # path exercises the post-S02 purpose split without ambient user state.
    engine = create_engine(f"sqlite:///{data_dir / 'managed-router.db'}")
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider_store = ProviderStore(factory)
    _connection, model_routes = provider_store.create_connection_with_routes(
        owner="alice",
        connection_id="real-browser-utility",
        family_id="local",
        adapter_id="openclank-local-executor",
        kind="local_executor",
        billing_lane="local",
        label="Real browser deterministic utility",
        normalized_url=endpoint_url,
        model_routes=[
            {
                "id": "real-browser-utility-route",
                "provider_model_id": "deterministic-memory-extractor",
                "display_name": "Deterministic memory extractor",
                "operations": ["chat.complete"],
            }
        ],
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="utility",
        model_route_ids=[model_routes[0].id],
        expected_revision=0,
    )
    provider_store.put_route_binding(
        owner="alice",
        purpose="memory",
        model_route_ids=[model_routes[0].id],
        expected_revision=0,
    )
    async def execute(owner, payload):
        if owner != "alice":
            raise RuntimeError("unexpected owner")
        selected = payload["routes"][0]
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{endpoint_url}/chat/completions",
                headers={"X-Test-Endpoint": "real-memory-browser"},
                json={
                    "model": selected["modelID"],
                    "messages": payload["input"]["messages"],
                },
            )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
        return {
            "operationID": "op_real_memory_browser",
            "rootOperationID": payload["rootOperationID"],
            "operation": payload["operation"],
            "state": "complete",
            "committed": True,
            "commitReason": "response_received",
            "modelRouteID": selected["modelRouteID"],
            "connectionID": selected["connectionID"],
            "billingLane": selected["billingLane"],
            "output": {"text": text},
            "artifacts": [],
            "replayed": False,
        }

    operation_router._default_router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=ArtifactStore(
            data_dir / "managed-artifacts", session_factory=factory
        ),
        executor=execute,
    )
    provider = FrankenmemoryProvider(
        command=str(fm_binary),
        env={"FM_DB_PATH": str(data_dir / "frankenmemory.db")},
    )
    sessions = _SessionLookup(endpoint_url)
    legacy_memory_dir = data_dir / "legacy-memory"
    legacy_memory_dir.mkdir()

    @asynccontextmanager
    async def lifespan(_app):
        await provider.initialize()
        await provider._call_tool(
            "graph_upsert",
            {
                "owner": "alice",
                "workspace_id": CHAT_WORKSPACE,
                "nodes": [],
                "edges": [
                    {
                        "src": {"kind": "person", "name": "Alice"},
                        "tag": "works_on",
                        "dst": {"kind": "project", "name": "Project Phoenix"},
                        "fact": "Alice works on Project Phoenix",
                    }
                ],
                "cues": [],
            },
        )
        try:
            yield
        finally:
            await provider.shutdown()
            model_server.shutdown()
            model_server.server_close()
            model_thread.join(timeout=5)
            operation_router.reset_operation_router_for_test()
            engine.dispose()
            settings._invalidate_caches()

    app = FastAPI(lifespan=lifespan)
    app.state.auth_manager = _AuthManager()
    app.include_router(
        setup_memory_routes(
            MemoryManager(str(legacy_memory_dir)),
            sessions,
            memory_provider=provider,
        )
    )

    @app.get("/api/prefs")
    async def prefs():
        return {"memory_mode": "automatic"}

    @app.get("/api/sessions")
    async def list_sessions():
        return [
            {
                "id": sessions.session.id,
                "name": sessions.session.name,
                "model": sessions.session.model,
                "endpoint_url": sessions.session.endpoint_url,
                "message_count": 0,
                "archived": False,
                "owner": "alice",
            }
        ]

    @app.get("/api/default-chat")
    async def default_chat():
        return {}

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def unrelated_api(_request: Request, path: str):
        if path == "notes":
            return []
        if path in {"presets", "presets/templates"}:
            return []
        if path == "copal/status":
            return {"storage_namespace": "real-memory-browser:alice"}
        return {}

    static_dir = REPO / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/{path:path}")
    async def spa(path: str):
        return FileResponse(static_dir / "index.html", media_type="text/html")

    return _Identity(app)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--fm-binary", type=Path, required=True)
    args = parser.parse_args()
    uvicorn.run(
        _build_app(args.data_dir, args.fm_binary),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
