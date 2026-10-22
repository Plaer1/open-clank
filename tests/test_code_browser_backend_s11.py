"""Disposable real HTTP/native/Files/Chrome journey over copied first-party code.

No app.py/lifespan, live account, model, source import, or installed binary fallback.
The fixture token authenticates only this ephemeral loopback test service.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
from types import SimpleNamespace

import pytest

if not os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN"):
    pytest.skip("Explicit isolated S11 default-native executable required", allow_module_level=True)


def assert_startup():
    root = Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve()
    assert root.name.startswith("openclank-s03-s06-projection.")
    for key in ("ODYSSEUS_DATA_DIR", "OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH",
                "FM_DB_PATH", "OPENCLANK_CONVERSATION_ARCHIVE_DB"):
        assert Path(os.environ[key]).resolve().is_relative_to(root), key
    assert os.environ["DATABASE_URL"] == "sqlite:///:memory:"
    assert os.environ["ODYSSEUS_INPROCESS_POLLERS"] == "0"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
    for key in ("OPEN_CLANK_AGENT_HOME", "THESIUS_AGENT_HOME"):
        assert key not in os.environ
    for key, digest in (("OPEN_CLANK_TEST_FM_MCP_BIN", "OPEN_CLANK_TEST_FM_MCP_SHA256"),
                        ("ODYSSEUS_FILES_SERVICE_BIN", "OPEN_CLANK_TEST_FILES_SHA256")):
        assert hashlib.sha256(Path(os.environ[key]).read_bytes()).hexdigest() == os.environ[digest]
    assert Path(os.environ["FM_MCP_COMMAND"]).resolve() == Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve()


assert_startup()  # Before application-owner imports (startup env also protects conftest).
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
import uvicorn  # noqa: E402
from routes.memory.memory_routes import setup_memory_routes  # noqa: E402
from routes.file_policy_routes import setup_file_policy_routes  # noqa: E402
from routes.files_facade_routes import setup_files_facade_routes  # noqa: E402
from src.openclank.filesystem_registry import FilesystemRootRegistry  # noqa: E402
from src.openclank.files_service_client import close_all_clients  # noqa: E402
from tests import test_code_index_lifecycle as lifecycle  # noqa: E402

scope = lifecycle.scope


@pytest.mark.parametrize("denial", ["erase", "revoke"])
async def test_real_corpus_browser_graph_source_editor_and_invalidation(scope, tmp_path, monkeypatch, denial):
    from tests.test_code_real_repository_s11 import copy_corpus, manifest_digest, repository_provider

    repository, workspaces, bindings, _ = scope
    # This journey explicitly qualifies the editable CodeMirror surface. The
    # shared lifecycle scope is read-only and correctly uses a static source
    # viewer instead. Grant editing only to a new disposable synthetic folder;
    # never override the real Files capability response or save source bytes.
    browser_root = tmp_path / "browser-alice"
    browser_root.mkdir()
    location = repository.create_location(actor_subject_id="s03-admin", path=str(browser_root), kind="directory", capabilities=("read", "write"))
    for kind in ("people", "agent"):
        bindings["alice", kind] = repository.create_binding(actor_subject_id="s03-admin", binding_class=kind,
            subject_id=lifecycle.Auth().account_id("alice"), location_id=location.id, capabilities=("read", "write"))
    workspaces["alice"] = repository.create_workspace(actor_subject_id=lifecycle.Auth().account_id("alice"),
        owner_subject_id=lifecycle.Auth().account_id("alice"), location_id=location.id, name="S11 disposable Editor")
    root = Path(lifecycle.native_request(scope)["root"])
    manifest = copy_corpus(root)
    selected_path = root / "src/openclank/code_resource_bridge.py"
    original = selected_path.read_bytes()
    provider, gate = await repository_provider(scope, tmp_path, monkeypatch)
    token = secrets.token_urlsafe(32)
    app = FastAPI()
    app.state.auth_manager = lifecycle.Auth()
    app.state.files_policy_repository = repository
    app.state.memory_provider = provider
    app.include_router(setup_file_policy_routes(repository=repository))
    held, release = asyncio.Event(), asyncio.Event()
    control = {"hold": False, "completed": 0}

    @app.middleware("http")
    async def identity(request, call_next):
        if not secrets.compare_digest(request.headers.get("x-s11-token", ""), token):
            return JSONResponse({"error": "fixture_auth_required"}, status_code=401)
        request.state.current_user = "alice"
        request.state.authenticated = True
        if request.url.path == "/api/memory/code/resource" and control["hold"]:
            body = await request.json()
            if body.get("operation") == "resolve":
                held.set()
                await release.wait()
        response = await call_next(request)
        if request.url.path == "/api/memory/code/resource":
            control["completed"] += 1
        return response

    app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
        memory_lifecycle=SimpleNamespace(skill_forget=None)))
    app.include_router(setup_files_facade_routes(policy_repository=repository,
        filesystem_registry=FilesystemRootRegistry(repository=repository)))

    @app.get("/__fixture/info")
    async def info():
        return {"workspace_id": workspaces["alice"].id, "bob_workspace_id": workspaces["bob"].id,
            "account_id": lifecycle.Auth().account_id("alice"), "selected_rel_path": selected_path.relative_to(root).as_posix(),
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "manifest": {"sha256": manifest_digest(manifest), "files": len(manifest), "bytes": sum(r["bytes"] for r in manifest)},
            "held": held.is_set(), "completed": control["completed"]}

    @app.post("/__fixture/control")
    async def action(request: Request):
        name = (await request.json()).get("action")
        if name == "edit": selected_path.write_bytes(original + b"\n# S11 isolated copy edit\n")
        elif name == "restore": selected_path.write_bytes(original)
        elif name == "hold": control["hold"] = True; held.clear(); release.clear()
        elif name == "release": control["hold"] = False; release.set()
        elif name == "erase":
            from src.memory_provider import MemoryTransportError
            try:
                await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
            except MemoryTransportError as error:
                if "cache_cleanup_pending" not in str(error):
                    raise
                status = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
                assert status["lifecycle"] == "erased", status
                print("S11 observed cache_cleanup_pending after committed metadata erasure; verify source denial before owned-worker restart")
                return {"ok": True, "cleanup_pending": True}
        elif name == "cleanup":
            await provider.shutdown()
            await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="remove"))
        elif name == "revoke":
            for kind in ("people", "agent"):
                assert repository.revoke_binding(bindings["alice", kind].id, actor_subject_id="s03-admin")
        else: return JSONResponse({"error": "invalid_fixture_action"}, status_code=400)
        return {"ok": True}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off"))
    task = None
    child = None
    try:
        await provider.code_index_lifecycle(**lifecycle.native_request(scope))
        async with asyncio.timeout(120):
            while True:
                status = await provider.code_index_lifecycle(**lifecycle.native_request(scope, action="status"))
                if status["lifecycle"] == "ready": break
                assert status["lifecycle"] not in {"failed", "erased"}, status
                await asyncio.sleep(.025)
        assert not list(gate.glob("*.held")), "Non-feature executable must ignore fixture gate"
        task = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not server.started: await asyncio.sleep(.01)
        child = await asyncio.create_subprocess_exec("node", "tests/code_graph_real_backend_acceptance.mjs",
            env={**os.environ, "S11_BACKEND_URL": f"http://127.0.0.1:{listener.getsockname()[1]}", "S11_TOKEN": token, "S11_DENIAL": denial},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        async with asyncio.timeout(180):
            output, _ = await child.communicate()
        assert child.returncode == 0, output.decode(errors="replace")
        print(output.decode().strip())
        assert "S11 real backend browser" in output.decode()
    finally:
        release.set()
        if child and child.returncode is None:
            child.terminate(); await child.wait()
        server.should_exit = True
        if task: await task
        listener.close()
        close_all_clients()
        await provider.shutdown()
