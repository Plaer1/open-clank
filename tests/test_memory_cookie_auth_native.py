"""Opt-in cookie-auth HTTP import/review/export/reset against actual fm-mcp.

No builds, live app, bearer/test-user identity injection, extraction model,
browser or all-agent-reset claim. A child process establishes isolated paths
before production imports, independent of pytest's previously imported DB.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import pytest


FM_BIN = os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN", "")
pytestmark = pytest.mark.skipif(
    not FM_BIN or not Path(FM_BIN).is_file(),
    reason="explicit existing OPEN_CLANK_TEST_FM_MCP_BIN required; never builds",
)


def test_cookie_auth_native_import_review_export_and_owner_reset(tmp_path):
    data = tmp_path / "cookie-auth-native"
    data.mkdir(mode=0o700)
    binary = Path(FM_BIN).resolve(strict=True)
    assert binary.is_file() and os.access(binary, os.X_OK)
    # Do not inherit broker/agent/model/store bindings. Keep OS HOME unchanged.
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR",
        "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
    ) if key in os.environ}
    env.update({
        "OPEN_CLANK_TEST_FM_MCP_BIN": str(binary),
        "OPEN_CLANK_DATA_DIR": str(data), "ODYSSEUS_DATA_DIR": str(data),
        "FM_DB_PATH": str(data / "frankenmemory.db"),
        "DATABASE_URL": "sqlite:///" + str(data / "app.db"),
        "AUTH_ENABLED": "true", "PYTHONDONTWRITEBYTECODE": "1",
    })
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--cookie-native", str(data)],
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, (
        "isolated cookie/native regression failed\n"
        + completed.stdout[-8000:] + completed.stderr[-8000:]
    )
    assert "cookie-auth-native: passed" in completed.stdout


async def _exercise(data: Path):
    # These imports occur only inside the isolated, explicitly opted-in child.
    import httpx
    from fastapi import FastAPI
    from starlette.responses import JSONResponse
    from core.auth import AuthManager
    from core.database import ApiToken, Base, SessionLocal, engine
    from core.session_manager import SessionManager
    from routes.auth_routes import SESSION_COOKIE, setup_auth_routes
    from routes.memory.memory_routes import setup_memory_routes
    from services.memory import MemoryManager
    from services.memory.import_batch import MemoryImportBatchStore
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR, FM_DB_PATH, USER_PREFS_FILE
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_scope import chat_workspace
    from src.openclank.account_request_barrier import AccountRequestBarrier, AccountRequestFenced
    from src.openclank.file_policy import FilePolicyRepository
    from src.openclank.request_auth import install_request_auth

    assert Path(DATA_DIR).resolve() == data
    assert Path(FM_DB_PATH).resolve() == data / "frankenmemory.db"
    assert Path(USER_PREFS_FILE).resolve() == data / "user_prefs.json"
    assert str(engine.url.database) == str(data / "app.db")
    Path(USER_PREFS_FILE).write_text(json.dumps({"_users": {
        "alice": {"memory_mode": "automatic"},
        "bob": {"memory_mode": "automatic"},
    }}), encoding="utf-8")
    Base.metadata.create_all(engine)
    auth = AuthManager(str(data / "auth.json"))
    policy = FilePolicyRepository(data / "app.db")
    sessions, memory, skills = SessionManager(), MemoryManager(str(data)), SkillsManager(str(data))
    provider = FrankenmemoryProvider(
        command=os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"], workspace_id=chat_workspace(),
        env={"FM_DB_PATH": str(data / "frankenmemory.db")},
    )
    barrier = AccountRequestBarrier(auth.is_account_lifecycle_fenced)
    app = FastAPI()
    app.state.auth_manager = auth
    app.state.files_policy_repository = policy
    app.state.drain_account_owner_requests = barrier.drain

    async def account_call_next(request, next_call, owner):
        try:
            return await barrier.track_response(owner, lambda: next_call(request))
        except AccountRequestFenced:
            return JSONResponse(status_code=409, content={"error": "Account lifecycle operation in progress"})

    install_request_auth(
        app, auth_manager=auth, session_factory=SessionLocal, api_token_model=ApiToken,
        account_call_next=account_call_next, trusted_loopback=lambda _request: False,
        desktop_ready_path="/cookie-native-ready", session_cookie=SESSION_COOKIE,
    )
    app.include_router(setup_auth_routes(auth))
    app.include_router(setup_memory_routes(memory, sessions, memory_provider=provider, skills_manager=skills))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.19", 54321))
    binary_path = Path(os.environ["OPEN_CLANK_TEST_FM_MCP_BIN"]).resolve(strict=True)
    binary_hash_before = __import__("hashlib").sha256(binary_path.read_bytes()).hexdigest()
    try:
        await provider.initialize()
        async with httpx.AsyncClient(transport=transport, base_url="https://memory-cookie.test") as anon, \
                   httpx.AsyncClient(transport=transport, base_url="https://memory-cookie.test") as alice, \
                   httpx.AsyncClient(transport=transport, base_url="https://memory-cookie.test") as bob:
            uploads = lambda owner: [("files", (f"{owner}-{i}.json", json.dumps([
                {"text": text, "category": "fact"}
            ]).encode(), "application/json")) for i, text in enumerate([
                f"Synthetic {owner} travel checklist uses violet-tulip-211.",
                f"Synthetic {owner} queue-only caution marker is silver-pine-512.",
            ])]
            assert (await anon.post("/api/memory/import-batches", files=uploads("anonymous"))).status_code == 401
            assert (await anon.get("/api/memory/export")).status_code == 401
            assert (await anon.post("/api/memory/nuke", json={"action": "preview", "components": ["ingest"]})).status_code == 401
            passwords = {owner: secrets.token_urlsafe(24) for owner in ("alice", "bob")}
            assert (await alice.post("/api/auth/setup", json={"username": "alice", "password": passwords["alice"]})).status_code == 200
            assert (await alice.post("/api/auth/login", json={"username": "alice", "password": passwords["alice"]})).status_code == 200
            assert alice.cookies.get(SESSION_COOKIE)
            assert (await alice.post("/api/auth/users", json={"username": "bob", "password": passwords["bob"]})).status_code == 200
            assert (await bob.post("/api/auth/login", json={"username": "bob", "password": passwords["bob"]})).status_code == 200
            assert bob.cookies.get(SESSION_COOKIE) != alice.cookies.get(SESSION_COOKIE)
            for owner, client in (("alice", alice), ("bob", bob)):
                status = (await client.get("/api/auth/status")).json()
                assert status["authenticated"] is True and status["username"] == owner
            # Configured-app denial, distinct from pre-setup "Setup required".
            assert (await anon.post("/api/memory/import-batches", files=uploads("anonymous"))).status_code == 401
            assert (await anon.get("/api/memory/export")).status_code == 401
            assert (await anon.post("/api/memory/nuke", json={"action": "preview", "components": ["ingest"]})).status_code == 401

            saved, batches = {}, {}
            for owner, client in (("alice", alice), ("bob", bob)):
                response = await client.post("/api/memory/add", json={
                    "text": f"Synthetic {owner} retained vault marker is amber-copper-410.", "category": "fact",
                })
                assert response.status_code == 200
                saved[owner] = response.json()["memory_id"]
                skills.add_skill(name=f"{owner}-cookie-skill", description="Synthetic HTTP reset only",
                                 when_to_use="Test", procedure=["No action"], owner=owner, source="user")
                response = await client.post("/api/memory/import-batches", files=uploads(owner))
                assert response.status_code == 200
                batch = response.json()
                assert len(batch["items"]) == 2
                assert all(item["state"] == "awaiting_review" for item in batch["items"])
                assert all(len(item["result"]["suggestions"]) == 1 for item in batch["items"])
                batches[owner] = batch

            batch_id = batches["alice"]["batch_id"]
            suggestion = batches["alice"]["items"][0]["result"]["suggestions"][0]
            review_body = {"action": "accept", "suggestion_id": suggestion["suggestion_id"]}
            review_headers = {"Idempotency-Key": "synthetic-alice-cookie-accept-1"}
            foreign_review = await bob.post(f"/api/memory/import-batches/{batch_id}/review", json=review_body,
                                            headers={"Idempotency-Key": "synthetic-bob-foreign-review-1"})
            assert foreign_review.status_code == 404
            accepted = await alice.post(f"/api/memory/import-batches/{batch_id}/review", json=review_body, headers=review_headers)
            assert accepted.status_code == 200 and accepted.json()["ok"] is True
            decision = accepted.json()["review"]["review"]
            assert decision["state"] == "accepted" and decision["publication"]["kind"] == "memory"
            accepted_id = decision["publication"]["id"]
            replay = await alice.post(f"/api/memory/import-batches/{batch_id}/review", json=review_body, headers=review_headers)
            assert replay.status_code == 200 and replay.json()["review"]["replayed"] is True
            assert replay.json()["review"]["review"]["publication"] == decision["publication"]

            alice_before = (await alice.get("/api/memory/export")).json()
            bob_before = (await bob.get("/api/memory/export")).json()
            assert alice_before["owner"] == "alice" and bob_before["owner"] == "bob"
            alice_ids = [row["id"] for row in alice_before["curated"]]
            bob_ids = {row["id"] for row in bob_before["curated"]}
            assert saved["alice"] in alice_ids and alice_ids.count(accepted_id) == 1
            assert saved["bob"] in bob_ids and saved["bob"] not in alice_ids and accepted_id not in bob_ids
            assert (await bob.get(f"/api/memory/import-batches/{batch_id}")).status_code == 404
            assert (await alice.get(f"/api/memory/import-batches/{batches['bob']['batch_id']}")).status_code == 404
            store = MemoryImportBatchStore(str(data / "frankenmemory.db"), str(data))
            bob_batch_before = store.get(owner="bob", batch_id=batches["bob"]["batch_id"])
            bob_pending_before = store.list_pending(owner="bob", states=["queued", "active", "awaiting_review"])
            bob_staging_before, bob_skills_before = store.preview_owner_staging("bob"), skills.load(owner="bob")

            preview_response = await alice.post("/api/memory/nuke", json={"action": "preview", "components": ["ingest", "skills"]})
            assert preview_response.status_code == 200
            preview = preview_response.json()
            assert set(preview["expanded_components"]) == {"ingest", "skills"}
            assert preview["components"]["ingest"]["count"] > 0 and preview["components"]["skills"]["count"] > 0
            commit_body = {"action": "commit", **{key: preview[key] for key in ("operation_id", "preview_token", "confirmation")}}
            committed = await alice.post("/api/memory/nuke", json=commit_body)
            assert committed.status_code == 200 and committed.json()["complete"] is True
            assert set(committed.json()["categories"]) == {"ingest", "skills"}
            assert (await alice.post("/api/memory/nuke", json=commit_body)).json() == committed.json()
            assert store.list_pending(owner="alice", states=["queued", "active", "awaiting_review"]) == []
            assert store.preview_owner_staging("alice")["count"] == 0 and skills.load(owner="alice") == []
            assert (await alice.get(f"/api/memory/import-batches/{batch_id}")).status_code == 404
            assert (await bob.get(f"/api/memory/import-batches/{batch_id}")).status_code == 404
            assert store.get(owner="bob", batch_id=batches["bob"]["batch_id"]) == bob_batch_before
            assert store.list_pending(owner="bob", states=["queued", "active", "awaiting_review"]) == bob_pending_before
            assert store.preview_owner_staging("bob") == bob_staging_before and skills.load(owner="bob") == bob_skills_before
            assert (await bob.get(f"/api/memory/import-batches/{batches['bob']['batch_id']}")).status_code == 200
            alice_after = (await alice.get("/api/memory/export")).json()
            bob_after = (await bob.get("/api/memory/export")).json()
            assert {row["id"] for row in alice_after["curated"]} == set(alice_ids)
            assert bob_after["curated"] == bob_before["curated"]
            assert (await barrier.drain("alice"))["drained"] == 0
            assert (await barrier.drain("bob"))["drained"] == 0
        print("cookie-auth-native: passed; real cookies/multipart/review/export/ingest+skills reset; no model")
    finally:
        await provider.shutdown()
        engine.dispose()
    assert __import__("hashlib").sha256(binary_path.read_bytes()).hexdigest() == binary_hash_before


if __name__ == "__main__":
    assert len(sys.argv) == 3 and sys.argv[1] == "--cookie-native"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    fixture_root = Path(sys.argv[2]).resolve(strict=True)
    assert fixture_root == Path(os.environ["OPEN_CLANK_DATA_DIR"]).resolve(strict=True)
    asyncio.run(_exercise(fixture_root))
