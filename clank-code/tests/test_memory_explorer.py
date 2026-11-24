"""M6(b): explorer figures from a throwaway V2 store; route isolation, bounds and read-only behaviour."""
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from routes.memory.explorer import register_memory_explorer
from src.memery_v2 import V2Repository, explorer_overview
from tests.test_memery_v2_bridge import _db


def store(tmp_path):
    path = str(tmp_path / "x.db")
    _db(path)
    repo = V2Repository(path)
    for owner in ("alice", "bob"):
        repo.create_entity(owner=owner, workspace_id="global", entity_id="e", entity_type="person", canonical_label="A")
    return path, repo


def add(repo, owner, block, kind="fact", value="SECRETCLAIM", tags=()):
    return repo.create_knowledge(owner=owner, workspace_id="global", block_id=block, subject_entity_id="e", predicate="likes-" + block,
                                 claim_slot=block, payload={"value": {"type": "string", "value": value}, "tags": list(tags), "kind": kind})


def test_counts_growth_recent_and_update_after_a_write(tmp_path):
    path, repo = store(tmp_path)
    add(repo, "alice", "a", "fact"); add(repo, "alice", "b", "preference"); add(repo, "bob", "z", "fact")
    out = explorer_overview(owner="alice", workspace_id="global", db_path=path)
    assert out["state"] == "ready" and out["total"] == 2 and out["by_kind"] == {"fact": 1, "preference": 1}
    assert sum(g["created"] for g in out["growth"]) == 2 and {c["event"] for c in out["recent"]} == {"created"}
    assert all(set(c) == {"block_id", "title", "kind", "event", "revision", "at"} for c in out["recent"]), "no content fields"
    add(repo, "alice", "c", "fact")
    assert explorer_overview(owner="alice", workspace_id="global", db_path=path)["total"] == 3
    assert explorer_overview(owner="bob", workspace_id="global", db_path=path)["total"] == 1       # owner isolation
    assert "SECRETCLAIM" not in str(out)                                                        # claim values never returned


def test_links_are_scoped_to_the_owner_and_bounds_hold(tmp_path):
    path, repo = store(tmp_path)
    add(repo, "alice", "a"); add(repo, "bob", "z")
    own = explorer_overview(owner="alice", workspace_id="global", block_id="a", db_path=path)["links"]
    assert own["state"] == "ready" and own["supersedes"] == [] and own["domains"] == {"state": "unavailable"}
    assert explorer_overview(owner="alice", workspace_id="global", block_id="z", db_path=path)["links"] == {"state": "unavailable"}
    out = explorer_overview(owner="alice", workspace_id="global", days=9999, recent=9999, db_path=path)
    assert len(out["recent"]) <= 50 and len(out["growth"]) <= 366
    empty = explorer_overview(owner="nobody", workspace_id="global", db_path=path)
    assert empty["total"] == 0 and empty["recent"] == [] and empty["by_kind"] == {}


def test_read_only_connection_writes_nothing(tmp_path):
    path, repo = store(tmp_path); add(repo, "alice", "a")
    before = sqlite3.connect(path).execute("SELECT count(*) FROM fm_v2_change_sets").fetchone()[0]
    explorer_overview(owner="alice", workspace_id="global", db_path=path)
    assert sqlite3.connect(path).execute("SELECT count(*) FROM fm_v2_change_sets").fetchone()[0] == before


def route(provider, authed=True):
    @asynccontextmanager
    async def admitted(user):
        yield
    ctx = {"owner": "alice", "barrier": SimpleNamespace(admitted=admitted), "current_live": lambda: True}
    def human(request):
        if not authed: raise HTTPException(401, "no")
        return "alice"
    async def owned(fn, *a, **k): return fn(*a, **k)
    router = APIRouter(prefix="/api/memory")
    register_memory_explorer(router, require_human=human, capture_context=lambda r, u: ctx, owned=owned, provider=provider)
    app = FastAPI(); app.include_router(router)
    return TestClient(app)


def test_route_v2_legacy_fallback_auth_and_no_writes(tmp_path):
    path, repo = store(tmp_path); add(repo, "alice", "a")
    calls = []
    class P:
        _guarded_v17 = True
        def _local_db_path(self): return path
        def _scope(self, owner): return SimpleNamespace(owner="alice", workspace_id="global")
        async def list_memories(self, **kw): calls.append("list"); return []
        def __getattr__(self, name):
            if name.startswith(("add", "forget", "update", "delete", "purge", "reset")): raise AssertionError("write " + name)
            raise AttributeError(name)
    client = route(P())
    response = client.get("/api/memory/explorer")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store" and response.json()["total"] == 1
    assert route(P(), authed=False).get("/api/memory/explorer").status_code == 403
    assert client.get("/api/memory/explorer?days=0").status_code == 422
    class Legacy:
        _guarded_v17 = False
        def _local_db_path(self): return None
        async def list_memories(self, **kw):
            return [{"id": "1", "owner": "alice", "text": "secret words", "category": "fact", "timestamp": 1.0},
                    {"id": "2", "owner": "bob", "text": "other", "category": "fact", "timestamp": 2.0}]
    legacy = route(Legacy()).get("/api/memory/explorer").json()
    assert legacy["state"] == "legacy" and legacy["total"] == 1 and "by_status" in legacy["unavailable"] and "secret" not in str(legacy)
    class Off(Legacy):
        async def list_memories(self, **kw): return []
    assert route(Off()).get("/api/memory/explorer").json()["total"] == 0
