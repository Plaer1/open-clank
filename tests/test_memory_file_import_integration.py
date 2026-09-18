"""Real HTTP acceptance for file import into the Frankenmemory bank.

The simulated boundaries are a local OpenAI-compatible model server and the
managed engine executor that owns access to it. The application reaches that
executor only through an owner-scoped normalized route; the memory API,
multipart parser, Rust fm-mcp process, and SQLite store are production code.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.operation_models import OperationBase
from core.provider_models import ProviderBase
from core.models import Session
from routes.memory.memory_routes import setup_memory_routes
from services.memory import MemoryManager
from src.frankenmemory_provider import FrankenmemoryProvider
from src.memory_scope import CHAT_WORKSPACE
from src.openclank.artifacts import ArtifactStore
from src.openclank.chat_routing import MANAGED_ENGINE_PUBLIC_URL
from src.openclank.operation_router import ManagedOperationRouter
from src.openclank.provider_store import ProviderStore


REPO = Path(__file__).resolve().parent.parent
FM_BIN = REPO / "mcp_servers/frankenmemory/target/release/fm-mcp"
_PEER = ("203.0.113.7", 54321)
_EMPTY_RESPONSE_SENTINEL = "EMPTY_RESPONSE_SENTINEL"


class _Identity:
    """Mirror the one auth-middleware field these owner-scoped routes consume."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = dict(scope.get("headers") or []).get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


class _AuthManager:
    is_configured = True

    @staticmethod
    def get_privileges(_user):
        return {"can_manage_memory": True}


class _SessionLookup:
    """Use the production Session contract without touching the app's live DB."""

    def __init__(self):
        self.session = Session(
            id="memory-import-session",
            name="Memory import acceptance",
            endpoint_url=MANAGED_ENGINE_PUBLIC_URL,
            model="deterministic-memory-extractor",
            headers={},
            owner="alice",
            provider_model_route_id="memory-import-route",
        )

    def get_session(self, session_id: str):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """Keep endpoint and memory-mode reads away from live user configuration."""

    import routes.prefs_routes as prefs_routes
    import src.settings as settings

    settings_path = tmp_path / "settings.json"
    prefs_path = tmp_path / "user_prefs.json"
    settings_path.write_text("{}", encoding="utf-8")
    prefs_path.write_text(
        json.dumps(
            {
                "_users": {
                    "alice": {"memory_mode": "automatic"},
                    "bob": {"memory_mode": "automatic"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(settings_path))
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_path))
    settings._invalidate_caches()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    yield
    settings._invalidate_caches()


@pytest.fixture(scope="session")
def current_fm_binary():
    """Build the release binary so this cannot pass against stale Rust."""

    result = subprocess.run(
        ["cargo", "build", "--release", "-p", "fm-mcp"],
        cwd=REPO / "mcp_servers/frankenmemory",
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode:
        pytest.fail(
            "current-source fm-mcp release build failed:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    if not FM_BIN.is_file():
        pytest.fail(f"current-source fm-mcp binary was not produced at {FM_BIN}")
    return FM_BIN


@pytest.fixture
def local_llm():
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "payload": payload,
                }
            )
            document = "\n".join(
                str(message.get("content") or "")
                for message in payload.get("messages") or []
            )
            content = (
                ""
                if _EMPTY_RESPONSE_SENTINEL in document
                else json.dumps(
                    [
                        {
                            "text": (
                                "Alice keeps the Project Phoenix launch checklist "
                                "in the violet notebook."
                            ),
                            "category": "project",
                        },
                        {
                            "text": "Alice prefers quiet morning work sessions.",
                            "category": "preference",
                        },
                    ]
                )
            )
            body = json.dumps(
                {
                    "id": "chatcmpl-memory-import",
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
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def managed_llm(local_llm, tmp_path, monkeypatch):
    """Install normalized Utility and Memory routes backed by a simulated engine."""

    from src.openclank import operation_router

    endpoint_url, requests = local_llm
    engine = create_engine(f"sqlite:///{tmp_path / 'managed-router.db'}")
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    store = ProviderStore(factory)
    store.create_connection(
        owner="alice",
        connection_id="memory-import-connection",
        family_id="local",
        adapter_id="openclank-local-executor",
        kind="local_executor",
        billing_lane="local",
        label="Memory import test engine",
    )
    store.create_model_route(
        owner="alice",
        connection_id="memory-import-connection",
        model_route_id="memory-import-route",
        provider_model_id="deterministic-memory-extractor",
        operations=("chat.complete",),
    )
    store.put_route_binding(
        owner="alice",
        purpose="utility",
        model_route_ids=("memory-import-route",),
        expected_revision=0,
    )
    store.put_route_binding(
        owner="alice",
        purpose="memory",
        model_route_ids=("memory-import-route",),
        expected_revision=0,
    )

    async def execute(owner, payload):
        assert owner == "alice"
        assert "url" not in repr(payload).lower()
        selected = payload["routes"][0]
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{endpoint_url}/chat/completions",
                headers={"X-Test-Endpoint": "memory-import"},
                json={
                    "model": selected["modelID"],
                    "messages": payload["input"]["messages"],
                },
            )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
        return {
            "operationID": f"op_memory_import_{len(requests)}",
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

    router = ManagedOperationRouter(
        session_factory=factory,
        artifact_store=ArtifactStore(tmp_path / "artifacts", session_factory=factory),
        executor=execute,
    )
    monkeypatch.setattr(operation_router, "_default_router", router)
    try:
        yield requests, store
    finally:
        engine.dispose()


def _provider(db_path: Path) -> FrankenmemoryProvider:
    return FrankenmemoryProvider(
        command=str(FM_BIN),
        env={"FM_DB_PATH": str(db_path)},
    )


def _app(
    provider: FrankenmemoryProvider,
    tmp_path: Path,
    _legacy_endpoint_url: str | None = None,
):
    app = FastAPI()
    app.state.auth_manager = _AuthManager()
    app.include_router(
        setup_memory_routes(
            MemoryManager(str(tmp_path)),
            _SessionLookup(),
            memory_provider=provider,
        )
    )
    return _Identity(app)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=_PEER),
        base_url="http://memory.test",
    )


def _headers(user: str):
    return {"x-test-user": user}


async def _add_memory(
    client,
    text: str,
    category: str = "fact",
    *,
    owner: str = "alice",
) -> str:
    response = await client.post(
        "/api/memory/add",
        headers=_headers(owner),
        json={"text": text, "category": category},
    )
    assert response.status_code == 200, response.text
    memory_id = response.json()["memory_id"]
    assert memory_id.startswith("m_")
    return memory_id


def _assert_sqlite_integrity(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


async def test_file_import_reaches_persistent_tenant_scoped_memory(
    tmp_path,
    current_fm_binary,
    isolated_settings,
    managed_llm,
):
    llm_requests, _store = managed_llm
    db_path = tmp_path / "frankenmemory.db"
    provider = _provider(db_path)
    await provider.initialize()

    try:
        async with _client(_app(provider, tmp_path)) as client:
            empty_model_free_json = await client.post(
                "/api/memory/import",
                headers=_headers("alice"),
                files={
                    "file": (
                        "empty-memories.json",
                        b"[]",
                        "application/json",
                    )
                },
            )
            assert empty_model_free_json.status_code == 200
            assert empty_model_free_json.json()["suggestions"] == []
            assert llm_requests == []

            model_free_json = await client.post(
                "/api/memory/import",
                headers=_headers("alice"),
                files={
                    "file": (
                        "memories.json",
                        json.dumps(
                            [
                                {
                                    "text": "Alice labels exported boxes in amber.",
                                    "category": "fact",
                                }
                            ]
                        ).encode(),
                        "application/json",
                    )
                },
            )
            assert model_free_json.status_code == 200, model_free_json.text
            assert model_free_json.json()["suggestions"] == [
                {
                    "text": "Alice labels exported boxes in amber.",
                    "category": "fact",
                }
            ]
            assert llm_requests == []

            imported = await client.post(
                "/api/memory/import",
                headers=_headers("alice"),
                data={"session": "memory-import-session"},
                files={
                    "file": (
                        "brain.md",
                        (
                            b"# Brain\nProject Phoenix uses a violet notebook. "
                            b"Alice prefers quiet mornings."
                        ),
                        "text/markdown",
                    )
                },
            )
            assert imported.status_code == 200, imported.text
            suggestions = imported.json()["suggestions"]
            assert [item["category"] for item in suggestions] == [
                "project",
                "preference",
            ]
            assert len(llm_requests) == 1
            assert llm_requests[0]["path"] == "/v1/chat/completions"
            assert llm_requests[0]["payload"]["model"] == "deterministic-memory-extractor"
            assert llm_requests[0]["headers"]["X-Test-Endpoint"] == "memory-import"
            assert "Project Phoenix uses a violet notebook" in json.dumps(
                llm_requests[0]["payload"]
            )

            # A foreign caller cannot borrow the supplied session's managed
            # route. With no memory binding of its own it fails closed before
            # the engine executor receives a request.
            foreign_import = await client.post(
                "/api/memory/import",
                headers=_headers("bob"),
                data={"session": "memory-import-session"},
                files={"file": ("foreign.md", b"private", "text/markdown")},
            )
            assert foreign_import.status_code == 409
            assert foreign_import.json()["detail"] == {
                "code": "MEMORY_ROUTE_UNCONFIGURED",
                "message": (
                    "Memory import needs a Memory-capable model, but neither "
                    "Memory nor its Utility/Chat inheritance resolves to an "
                    "available route. Choose Memory or Utility under AI "
                    "Defaults and retry."
                ),
                "retryable": True,
                "phase": "preflight",
                "required_purpose": "memory",
                "required_operation": "chat.complete",
                "settings_target": "ai",
                "binding_revision": 0,
                "eligible_routes": [],
            }
            assert len(llm_requests) == 1

            # Empty model output is a typed, retryable extraction problem, not
            # a valid empty import or an opaque upstream/proxy failure.
            empty_output = await client.post(
                "/api/memory/import",
                headers=_headers("alice"),
                data={"session": "memory-import-session"},
                files={
                    "file": (
                        "empty.md",
                        _EMPTY_RESPONSE_SENTINEL.encode(),
                        "text/markdown",
                    )
                },
            )
            assert empty_output.status_code == 422
            assert empty_output.json()["detail"] == {
                "code": "MEMORY_EXTRACTION_EMPTY",
                "message": (
                    "The selected Memory model finished without returning "
                    "import suggestions. Retry the import or choose a "
                    "different Memory model."
                ),
                "retryable": True,
                "phase": "extraction",
                "required_purpose": "memory",
                "required_operation": "chat.complete",
                "settings_target": "ai",
            }

            memory_ids = []
            for suggestion in suggestions:
                added = await client.post(
                    "/api/memory/add",
                    headers=_headers("alice"),
                    json=suggestion,
                )
                assert added.status_code == 200, added.text
                memory_ids.append(added.json()["memory_id"])
            assert all(memory_id.startswith("m_") for memory_id in memory_ids)

            listed = await client.get("/api/memory", headers=_headers("alice"))
            assert listed.status_code == 200
            assert {item["id"] for item in listed.json()["memory"]} == set(memory_ids)

            searched = await client.post(
                "/api/memory/search",
                headers=_headers("alice"),
                data={"query": "violet notebook Phoenix"},
            )
            assert searched.status_code == 200
            assert memory_ids[0] in {
                item["id"] for item in searched.json()["memories"]
            }

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
            graph = await client.get(
                "/api/memory/graph",
                headers=_headers("alice"),
                params={"op": "overview"},
            )
            assert graph.status_code == 200
            assert graph.json()["node_total"] >= 2
            assert {"alice", "project phoenix"}.issubset(
                {
                    str(node.get("name") or "").lower()
                    for node in graph.json()["nodes"]
                }
            )
            assert graph.json()["edges"]

            digest = await client.get(
                "/api/memory/digest-preview",
                headers=_headers("alice"),
            )
            assert digest.status_code == 200
            assert digest.json()["digest"]["counts"]["by_tier"]["curated"] == 2

            edited_text = (
                "Alice keeps the Project Phoenix launch checklist "
                "in the cobalt notebook."
            )
            edited = await client.put(
                f"/api/memory/{memory_ids[0]}",
                headers=_headers("alice"),
                data={"text": edited_text, "category": "project"},
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["memory"]["text"] == edited_text

            bob_list = await client.get("/api/memory", headers=_headers("bob"))
            assert bob_list.status_code == 200
            assert bob_list.json()["memory"] == []
            bob_graph = await client.get(
                "/api/memory/graph",
                headers=_headers("bob"),
                params={"op": "overview"},
            )
            assert bob_graph.status_code == 200
            assert bob_graph.json()["nodes"] == []
            bob_digest = await client.get(
                "/api/memory/digest-preview",
                headers=_headers("bob"),
            )
            assert bob_digest.status_code == 200
            assert (
                bob_digest.json()["digest"]["counts"]["by_tier"]["curated"]
                == 0
            )
            bob_search = await client.post(
                "/api/memory/search",
                headers=_headers("bob"),
                data={"query": "Project Phoenix"},
            )
            assert bob_search.json()["memories"] == []
            assert (
                await client.get(
                    f"/api/memory/{memory_ids[0]}",
                    headers=_headers("bob"),
                )
            ).status_code == 404
    finally:
        await provider.shutdown()


    assert db_path.exists()

    reopened = _provider(db_path)
    await reopened.initialize()
    try:
        async with _client(_app(reopened, tmp_path)) as client:
            listed = await client.get("/api/memory", headers=_headers("alice"))
            assert listed.status_code == 200
            persisted = {item["id"]: item for item in listed.json()["memory"]}
            assert set(persisted) == set(memory_ids)
            assert "cobalt notebook" in persisted[memory_ids[0]]["text"]

            searched = await client.post(
                "/api/memory/search",
                headers=_headers("alice"),
                data={"query": "cobalt notebook Phoenix"},
            )
            assert memory_ids[0] in {
                item["id"] for item in searched.json()["memories"]
            }

            graph = await client.get(
                "/api/memory/graph",
                headers=_headers("alice"),
                params={"op": "overview"},
            )
            assert {"alice", "project phoenix"}.issubset(
                {
                    str(node.get("name") or "").lower()
                    for node in graph.json()["nodes"]
                }
            )

            digest = await client.get(
                "/api/memory/digest-preview",
                headers=_headers("alice"),
            )
            assert digest.json()["digest"]["counts"]["by_tier"]["curated"] == 2

            bob_list = await client.get("/api/memory", headers=_headers("bob"))
            assert bob_list.json()["memory"] == []
    finally:
        await reopened.shutdown()
    _assert_sqlite_integrity(db_path)


async def test_file_import_inherits_chat_when_memory_and_utility_are_unset(
    tmp_path,
    current_fm_binary,
    isolated_settings,
    managed_llm,
):
    """Memory follows Utility, which follows Chat, without copying a binding."""

    llm_requests, store = managed_llm
    store.delete_route_binding(
        owner="alice",
        purpose="utility",
        expected_revision=1,
    )
    store.delete_route_binding(
        owner="alice",
        purpose="memory",
        expected_revision=1,
    )
    store.put_route_binding(
        owner="alice",
        purpose="chat",
        model_route_ids=("memory-import-route",),
        expected_revision=0,
    )

    provider = _provider(tmp_path / "frankenmemory.db")
    await provider.initialize()
    try:
        async with _client(_app(provider, tmp_path)) as client:
            response = await client.post(
                "/api/memory/import",
                headers=_headers("alice"),
                data={"session": "memory-import-session"},
                files={
                    "file": (
                        "brain.md",
                        b"Alice keeps the launch checklist in a violet notebook.",
                        "text/markdown",
                    )
                },
            )

        assert response.status_code == 200, response.text
        assert response.json()["suggestions"]
        assert len(llm_requests) == 1
        assert llm_requests[0]["payload"]["model"] == "deterministic-memory-extractor"
    finally:
        await provider.shutdown()


async def test_memory_lifecycle_routes_are_owner_scoped_and_durable(
    tmp_path,
    current_fm_binary,
    isolated_settings,
    local_llm,
):
    endpoint_url, llm_requests = local_llm
    db_path = tmp_path / "lifecycle-frankenmemory.db"
    provider = _provider(db_path)
    await provider.initialize()

    try:
        async with _client(_app(provider, tmp_path, endpoint_url)) as client:
            primary_id = await _add_memory(
                client,
                "Alice keeps the amber archive ledger in cabinet seven.",
            )
            question_id = await _add_memory(
                client,
                "Which cabinet holds the spare archive keys",
                "unknown",
            )
            forget_id = await _add_memory(
                client,
                "Alice temporarily stores the violet manifest in drawer four.",
            )
            delete_id = await _add_memory(
                client,
                "Alice wants this obsolete shipping note permanently removed.",
            )

            default_retention = await client.get(
                "/api/memory/retention",
                headers=_headers("alice"),
            )
            assert default_retention.status_code == 200
            assert default_retention.json()["recovery_seconds"] == 0
            set_recovery = await client.put(
                "/api/memory/retention",
                headers=_headers("alice"),
                json={"recovery_seconds": 300},
            )
            assert set_recovery.status_code == 200, set_recovery.text
            assert set_recovery.json()["recovery_seconds"] == 300
            bob_retention = await client.get(
                "/api/memory/retention",
                headers=_headers("bob"),
            )
            assert bob_retention.status_code == 200
            assert bob_retention.json()["recovery_seconds"] == 0

            pinned = await client.post(
                f"/api/memory/{primary_id}/pin",
                headers=_headers("alice"),
                data={"pinned": "true"},
            )
            assert pinned.status_code == 200
            assert pinned.json()["pinned"] is True
            foreign_pin = await client.post(
                f"/api/memory/{primary_id}/pin",
                headers=_headers("bob"),
                data={"pinned": "false"},
            )
            assert foreign_pin.status_code == 404
            primary = await client.get(
                f"/api/memory/{primary_id}",
                headers=_headers("alice"),
            )
            assert primary.json()["memory"]["pinned"] is True

            explained = await client.get(
                f"/api/memory/{primary_id}/explain",
                headers=_headers("alice"),
            )
            assert explained.status_code == 200
            assert explained.json()["explanation"]["id"] == primary_id
            foreign_explain = await client.get(
                f"/api/memory/{primary_id}/explain",
                headers=_headers("bob"),
            )
            assert foreign_explain.status_code == 404

            candidate = await provider.remember(
                "Alice reviews the cobalt launch checklist every Friday.",
                owner="alice",
                category="fact",
                capture_mode="review_only",
            )
            assert candidate.id.startswith("candidate_")
            pending = await client.get(
                "/api/memory/inspect",
                headers=_headers("alice"),
                params={"tier": "candidate", "status": "pending"},
            )
            assert candidate.id in {item["id"] for item in pending.json()["items"]}
            foreign_pending = await client.get(
                "/api/memory/inspect",
                headers=_headers("bob"),
                params={"tier": "candidate", "status": "pending"},
            )
            assert foreign_pending.json()["items"] == []
            foreign_review = await client.post(
                f"/api/memory/candidate/{candidate.id}/review",
                headers=_headers("bob"),
                json={"accept": True, "reason": "foreign attempt"},
            )
            assert foreign_review.status_code == 400
            reviewed = await client.post(
                f"/api/memory/candidate/{candidate.id}/review",
                headers=_headers("alice"),
                json={"accept": True, "reason": "approved in acceptance"},
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["accepted"] is True
            candidate_memory_id = reviewed.json()["curated_id"]
            assert candidate_memory_id.startswith("m_")

            digest_before_resolve = await client.get(
                "/api/memory/digest-preview",
                headers=_headers("alice"),
            )
            assert question_id in {
                item["id"]
                for item in digest_before_resolve.json()["digest"]["open_questions"]
            }
            foreign_resolve = await client.post(
                f"/api/memory/{question_id}/resolve",
                headers=_headers("bob"),
                data={"answer": "another tenant cannot answer this"},
            )
            assert foreign_resolve.status_code == 404
            resolved = await client.post(
                f"/api/memory/{question_id}/resolve",
                headers=_headers("alice"),
                data={"answer": "Accepted during the real import journey"},
            )
            assert resolved.status_code == 200
            digest_after_resolve = await client.get(
                "/api/memory/digest-preview",
                headers=_headers("alice"),
            )
            assert question_id not in {
                item["id"]
                for item in digest_after_resolve.json()["digest"]["open_questions"]
            }

            exported = await client.get(
                "/api/memory/export",
                headers=_headers("alice"),
            )
            assert exported.status_code == 200
            assert exported.json()["owner"] == "alice"
            assert {
                primary_id,
                question_id,
                forget_id,
                delete_id,
                candidate_memory_id,
            }.issubset(
                {item["id"] for item in exported.json()["curated"]}
            )
            foreign_export = await client.get(
                "/api/memory/export",
                headers=_headers("bob"),
            )
            assert foreign_export.status_code == 200
            assert foreign_export.json()["owner"] == "bob"
            assert foreign_export.json()["curated"] == []

            preview = await client.post(
                "/api/memory/forget",
                headers=_headers("alice"),
                json={
                    "action": "preview",
                    "selector_kind": "record_id",
                    "selector": forget_id,
                },
            )
            assert preview.status_code == 200, preview.text
            assert forget_id in preview.json()["closure"]["curated_ids"]
            foreign_preview = await client.post(
                "/api/memory/forget",
                headers=_headers("bob"),
                json={
                    "action": "preview",
                    "selector_kind": "record_id",
                    "selector": forget_id,
                },
            )
            assert foreign_preview.status_code == 200
            assert forget_id not in foreign_preview.json()["closure"]["curated_ids"]
            foreign_commit = await client.post(
                "/api/memory/forget",
                headers=_headers("bob"),
                json={
                    "action": "commit",
                    "selector_kind": "record_id",
                    "selector": forget_id,
                    "preview_token": preview.json()["token"],
                },
            )
            assert foreign_commit.status_code == 400
            committed = await client.post(
                "/api/memory/forget",
                headers=_headers("alice"),
                json={
                    "action": "commit",
                    "selector_kind": "record_id",
                    "selector": forget_id,
                    "preview_token": preview.json()["token"],
                },
            )
            assert committed.status_code == 200, committed.text
            tombstone_id = committed.json()["tombstone_id"]
            assert (
                await client.get(
                    f"/api/memory/{forget_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 404
            foreign_restore = await client.post(
                "/api/memory/forget",
                headers=_headers("bob"),
                json={"action": "restore", "tombstone_id": tombstone_id},
            )
            assert foreign_restore.status_code == 400
            restored = await client.post(
                "/api/memory/forget",
                headers=_headers("alice"),
                json={"action": "restore", "tombstone_id": tombstone_id},
            )
            assert restored.status_code == 200, restored.text
            assert restored.json()["restored"] is True
            assert (
                await client.get(
                    f"/api/memory/{forget_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 200

            foreign_delete = await client.delete(
                f"/api/memory/{delete_id}",
                headers=_headers("bob"),
            )
            assert foreign_delete.status_code == 404
            assert (
                await client.get(
                    f"/api/memory/{delete_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 200
            deleted = await client.delete(
                f"/api/memory/{delete_id}",
                headers=_headers("alice"),
            )
            assert deleted.status_code == 200, deleted.text
            assert delete_id in deleted.json()["closure"]["curated_ids"]
            assert (
                await client.get(
                    f"/api/memory/{delete_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 404
            bob_id = await _add_memory(
                client,
                "Bob keeps the green tenant ledger in cabinet nine.",
                owner="bob",
            )
            assert (
                await client.get(
                    f"/api/memory/{bob_id}",
                    headers=_headers("bob"),
                )
            ).status_code == 200
            assert (
                await client.get(
                    f"/api/memory/{bob_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 404
            assert llm_requests == []
    finally:
        await provider.shutdown()

    reopened = _provider(db_path)
    await reopened.initialize()
    expired_ids: set[str] = set()
    try:
        async with _client(_app(reopened, tmp_path, endpoint_url)) as client:
            primary = await client.get(
                f"/api/memory/{primary_id}",
                headers=_headers("alice"),
            )
            assert primary.status_code == 200
            assert primary.json()["memory"]["pinned"] is True
            assert (
                await client.get(
                    f"/api/memory/{candidate_memory_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 200
            assert (
                await client.get(
                    f"/api/memory/{forget_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 200
            assert (
                await client.get(
                    f"/api/memory/{delete_id}",
                    headers=_headers("alice"),
                )
            ).status_code == 404
            assert (
                await client.get(
                    f"/api/memory/{bob_id}",
                    headers=_headers("bob"),
                )
            ).status_code == 200

            digest = await client.get(
                "/api/memory/digest-preview",
                headers=_headers("alice"),
            )
            assert question_id not in {
                item["id"] for item in digest.json()["digest"]["open_questions"]
            }
            persisted_retention = await client.get(
                "/api/memory/retention",
                headers=_headers("alice"),
            )
            assert persisted_retention.json()["recovery_seconds"] == 300
            persisted_explain = await client.get(
                f"/api/memory/{primary_id}/explain",
                headers=_headers("alice"),
            )
            assert persisted_explain.json()["explanation"]["id"] == primary_id
            persisted_export = await client.get(
                "/api/memory/export",
                headers=_headers("alice"),
            )
            assert primary_id in {
                item["id"] for item in persisted_export.json()["curated"]
            }
            assert delete_id not in {
                item["id"] for item in persisted_export.json()["curated"]
            }

            bob_policy = await client.put(
                "/api/memory/retention",
                headers=_headers("bob"),
                json={"recovery_seconds": 17},
            )
            assert bob_policy.status_code == 200
            assert bob_policy.json()["recovery_seconds"] == 17
            alice_policy = await client.put(
                "/api/memory/retention",
                headers=_headers("alice"),
                json={
                    "raw_days": 0,
                    "candidate_days": 0,
                    "curated_days": 0,
                    "graph_days": 0,
                    "recovery_seconds": 300,
                },
            )
            assert alice_policy.status_code == 200, alice_policy.text
            assert alice_policy.json()["raw_days"] == 0
            assert alice_policy.json()["candidate_days"] == 0
            assert alice_policy.json()["curated_days"] == 0
            assert alice_policy.json()["graph_days"] == 0
            assert (
                await client.get(
                    "/api/memory/retention",
                    headers=_headers("bob"),
                )
            ).json()["recovery_seconds"] == 17

            await asyncio.sleep(0.02)
            expired = await client.post(
                "/api/memory/retention/expire",
                headers=_headers("alice"),
            )
            assert expired.status_code == 200, expired.text
            expired_ids = set(expired.json()["curated_ids"])
            assert expired_ids == {
                question_id,
                candidate_memory_id,
                forget_id,
            }
            for memory_id in expired_ids:
                missing = await client.get(
                    f"/api/memory/{memory_id}",
                    headers=_headers("alice"),
                )
                assert missing.status_code == 404
            retained_primary = await client.get(
                f"/api/memory/{primary_id}",
                headers=_headers("alice"),
            )
            assert retained_primary.status_code == 200
            assert retained_primary.json()["memory"]["pinned"] is True
            bob_after_alice_expiry = await client.get(
                "/api/memory",
                headers=_headers("bob"),
            )
            assert {
                item["id"] for item in bob_after_alice_expiry.json()["memory"]
            } == {bob_id}
    finally:
        await reopened.shutdown()

    durable = _provider(db_path)
    await durable.initialize()
    try:
        async with _client(_app(durable, tmp_path, endpoint_url)) as client:
            alice_policy = await client.get(
                "/api/memory/retention",
                headers=_headers("alice"),
            )
            assert alice_policy.json()["raw_days"] == 0
            assert alice_policy.json()["candidate_days"] == 0
            assert alice_policy.json()["curated_days"] == 0
            assert alice_policy.json()["graph_days"] == 0
            bob_policy = await client.get(
                "/api/memory/retention",
                headers=_headers("bob"),
            )
            assert bob_policy.json()["recovery_seconds"] == 17
            retained_primary = await client.get(
                f"/api/memory/{primary_id}",
                headers=_headers("alice"),
            )
            assert retained_primary.status_code == 200
            assert retained_primary.json()["memory"]["pinned"] is True
            assert (
                await client.get(
                    f"/api/memory/{bob_id}",
                    headers=_headers("bob"),
                )
            ).status_code == 200
            for memory_id in expired_ids | {delete_id}:
                missing = await client.get(
                    f"/api/memory/{memory_id}",
                    headers=_headers("alice"),
                )
                assert missing.status_code == 404
    finally:
        await durable.shutdown()
    _assert_sqlite_integrity(db_path)
