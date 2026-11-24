"""GET /api/memory/quality: guarded counts come from the v2 list read; legacy output unchanged (fake providers, no store)."""
import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

NATIVE = {"basis": "scoped_visible_live",
          "counts": {"by_kind": {}, "by_tier": {"raw": 0, "curated": 0, "candidates_pending": 0}, "candidates_pending": 0},
          "graph": {"visible_cues": 3}}
LEGACY = {"basis": "scoped_visible_live",
          "counts": {"by_kind": {"persona": 1}, "by_tier": {"raw": 4, "curated": 2, "candidates_pending": 1}, "candidates_pending": 1},
          "graph": {"visible_cues": 5}}


def _get(provider, owner="alice"):
    from routes.memory.memory_routes import setup_memory_routes
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice": {}, "bob": {}})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = owner
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(setup_memory_routes(SimpleNamespace(), SimpleNamespace(), memory_provider=provider,
                                           memory_lifecycle=SimpleNamespace(skill_forget=None)))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            return await client.get("/api/memory/quality")
    return asyncio.run(run())


class _Guarded:
    _guarded_v17 = True

    def __init__(self, by_owner, pending=None):
        self.by_owner, self.calls, self.pending = by_owner, [], pending or {}

    async def inspect_tier(self, tier, *, owner=None, status=None, limit=500):
        assert (tier, status) == ("candidate", "pending")
        return [{"id": i} for i in self.pending.get(owner, [])]

    async def memory_quality(self, rebuild_graph_fts=False):
        return NATIVE

    async def list_v2_records(self, *, owner=None, limit=None, require_legacy_presence=False, **kw):
        self.calls.append((owner, limit, require_legacy_presence))
        return [SimpleNamespace(category=c) for c in self.by_owner.get(owner, [])]


def test_guarded_counts_visible_records_and_kinds_and_keeps_native_graph():
    provider = _Guarded({"alice": ["fact", "fact", "persona"], "bob": ["fact"] * 7})
    response = _get(provider)
    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["by_tier"] == {"raw": 0, "curated": 3, "candidates_pending": 0}
    assert body["counts"]["by_kind"] == {"fact": 2, "persona": 1}
    assert sum(body["counts"]["by_kind"].values()) == body["counts"]["by_tier"]["curated"]
    assert body["graph"] == {"visible_cues": 3} and body["basis"] == "scoped_visible_live"
    assert provider.calls == [("alice", None, True)]  # owner-scoped, the panel list's read


def test_guarded_owner_never_sees_other_owners_records():
    provider = _Guarded({"alice": ["fact"], "bob": ["fact"] * 7})
    assert _get(provider, owner="bob").json()["counts"]["by_tier"]["curated"] == 7
    assert _get(_Guarded({"bob": ["fact"]}), owner="alice").json()["counts"]["by_tier"]["curated"] == 0


def test_guarded_pending_candidate_counts_then_approval_moves_it_to_curated():
    provider = _Guarded({}, pending={"alice": ["c1"]})
    counts = _get(provider).json()["counts"]
    assert counts["candidates_pending"] == 1 and counts["by_tier"] == {"raw": 0, "curated": 0, "candidates_pending": 1}
    provider.pending, provider.by_owner = {}, {"alice": ["fact"]}  # approved
    counts = _get(provider).json()["counts"]
    assert counts["candidates_pending"] == 0 and counts["by_tier"] == {"raw": 0, "curated": 1, "candidates_pending": 0}
    provider.pending = {"alice": ["c2"]}
    assert _get(provider, owner="bob").json()["counts"]["candidates_pending"] == 0


def test_legacy_output_is_unchanged():
    class Legacy:
        async def memory_quality(self, rebuild_graph_fts=False):
            return LEGACY
    response = _get(Legacy())
    assert response.status_code == 200 and response.json() == LEGACY
