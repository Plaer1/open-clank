"""Disposable production-path Copal Redb query qualification.

This intentionally drives the authenticated FastAPI routes and the real
stdio Redb bridge.  The older Base acceptance fixture uses in-memory request
handlers, so it cannot establish query count, persisted index behavior, or
visibility after a restart.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from routes.copal_routes import _prepare_import_tree, setup_copal_routes
from src.openclank.copal_bridge import CopalBridge


CORPUS_SIZE = 5_001
WARM_SAMPLES = 10


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    rank = max(1, math.ceil(len(ordered) * percentile))
    return ordered[rank - 1]


class CountingBridge:
    """Count production bridge operations without changing their behavior."""

    supports_task_index_lookup = True
    supports_keyed_task_index = True

    def __init__(self, delegate: CopalBridge) -> None:
        self.delegate = delegate
        self.calls: list[tuple[str, dict]] = []

    @property
    def data_dir(self):
        return self.delegate.data_dir

    def is_alive(self) -> bool:
        return self.delegate.is_alive()

    async def start(self) -> None:
        await self.delegate.start()

    async def call(self, operation: str, args: dict, timeout: float = 20):
        self.calls.append((operation, dict(args)))
        return await self.delegate.call(operation, args, timeout=timeout)

    async def stop(self) -> None:
        await self.delegate.stop()


def _write_disposable_corpus(root: Path) -> None:
    root.mkdir()
    for index in range(CORPUS_SIZE):
        # Keep the records small while giving the indexed query a property and
        # a deterministic body/name match to exercise both sort and filter.
        (root / f"Source {index:04d}.md").write_text(
            f"---\nscore: {index}\ncohort: {'even' if index % 2 == 0 else 'odd'}\n---\n"
            f"# Source {index}\nBody {index}\n",
            encoding="utf-8",
        )


@pytest.fixture
def production_app(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCLANK_HISTORY_SOCKET", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()

    @app.middleware("http")
    async def authenticated_identity(request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(setup_copal_routes())
    delegate = CopalBridge(data_dir=tmp_path / "copal-redb")
    app.state.copal_bridge = CountingBridge(delegate)
    app.state.auth_manager = SimpleNamespace(account_id=lambda user: f"account-{user}")
    return app, app.state.copal_bridge, tmp_path


@pytest.mark.asyncio
async def test_authenticated_redb_index_query_is_paged_persistent_and_visible_after_mutation(
    production_app,
):
    app, bridge, root = production_app
    vault = root / "vault"
    _write_disposable_corpus(vault)
    prepared = _prepare_import_tree(vault)
    assert prepared["preparedDatabaseNotes"] == CORPUS_SIZE

    import_result = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        headers = {"x-test-user": "alice"}

        # Seed through the production bridge in one Redb transaction.  The
        # route remains the authenticated boundary for every read and write.
        import_result = await bridge.delegate.call(
                "import_vault",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "path": str(vault),
                    "note_kind": "note",
                },
            )
        assert import_result["notes"] == CORPUS_SIZE, import_result

        base = await bridge.delegate.call(
                "create",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "kind": "base",
                    "name": "Catalog.base",
                    "content": json.dumps(
                        {
                            "version": 1,
                            "views": [
                                {
                                    "id": "all",
                                    "name": "All sources",
                                    "type": "table",
                                    "columns": [
                                        {"property": "file.name", "label": "Name"},
                                        {"property": "score", "label": "Score"},
                                    ],
                                    "sorts": [{"property": "score", "direction": "asc"}],
                                    "filters": [],
                                    "summaries": {},
                                    "limit": 10_000,
                                }
                            ],
                        }
                    ),
                },
            )
        base_id = base["doc"]["id"]

        # This call proves route auth owns the scope sent to Redb.  It is the
        # only full-index route call in this workload; later pages use the
        # persisted metadata index directly.
        before = len(bridge.calls)
        started = time.perf_counter()
        response = await http.get("/api/copal/documents?workspace=home&hidden=include", headers=headers)
        first_page_ms = (time.perf_counter() - started) * 1000
        assert response.status_code == 200, response.text
        returned_docs = response.json()["docs"]
        assert len([doc for doc in returned_docs if doc["name"].startswith("Source ")]) == CORPUS_SIZE
        assert any(doc["name"] == "Catalog.base" for doc in returned_docs)
        index_calls_after_first_page = [call for call in bridge.calls[before:] if call[0] == "index"]
        assert len(index_calls_after_first_page) == 1
        assert index_calls_after_first_page[0][1]["owner"] == "alice"
        assert index_calls_after_first_page[0][1]["workspace_id"] == "home"

        # Query the persisted metadata index for the first page, then exercise
        # continuation, sort, filter, and its snapshot/CAS boundary.
        started = time.perf_counter()
        first = await bridge.call(
                "metadata_page",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "corpus": "all",
                    "state": "active",
                    "hidden": "include",
                    "query": "Source",
                    "sort_key": "name",
                    "sort_direction": "asc",
                    "limit": 100,
                },
            )
        indexed_first_page_ms = (time.perf_counter() - started) * 1000
        assert first["total"] == CORPUS_SIZE
        assert len(first["docs"]) == 100
        assert first["next_cursor"] == "100"
        assert all("text" not in row and "content" not in row for row in first["docs"])

        warmed_indexed_page_ms = []
        for _ in range(WARM_SAMPLES):
            started = time.perf_counter()
            warmed = await bridge.call(
                "metadata_page",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "corpus": "all",
                    "state": "active",
                    "hidden": "include",
                    "query": "Source",
                    "sort_key": "name",
                    "sort_direction": "asc",
                    "limit": 100,
                },
            )
            warmed_indexed_page_ms.append((time.perf_counter() - started) * 1000)
            assert warmed["total"] == CORPUS_SIZE
            assert len(warmed["docs"]) == 100

        started = time.perf_counter()
        subsequent = await bridge.call(
                "metadata_page",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "corpus": "all",
                    "state": "active",
                    "hidden": "include",
                    "query": "Source 4999",
                    "sort_key": "modified",
                    "sort_direction": "desc",
                    "limit": 100,
                },
            )
        subsequent_query_ms = (time.perf_counter() - started) * 1000
        assert subsequent["total"] == 1
        assert [row["name"] for row in subsequent["docs"]] == ["Source 4999.md"]

        warmed_sort_filter_ms = []
        for _ in range(WARM_SAMPLES):
            started = time.perf_counter()
            warmed = await bridge.call(
                "metadata_page",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "corpus": "all",
                    "state": "active",
                    "hidden": "include",
                    "query": "Source 4999",
                    "sort_key": "modified",
                    "sort_direction": "desc",
                    "limit": 100,
                },
            )
            warmed_sort_filter_ms.append((time.perf_counter() - started) * 1000)
            assert warmed["total"] == 1
            assert [row["name"] for row in warmed["docs"]] == ["Source 4999.md"]

        indexed_page_stats = {
            "p50": round(_percentile(warmed_indexed_page_ms, 0.50), 2),
            "p95": round(_percentile(warmed_indexed_page_ms, 0.95), 2),
            "max": round(max(warmed_indexed_page_ms), 2),
        }
        sort_filter_stats = {
            "p50": round(_percentile(warmed_sort_filter_ms, 0.50), 2),
            "p95": round(_percentile(warmed_sort_filter_ms, 0.95), 2),
            "max": round(max(warmed_sort_filter_ms), 2),
        }
        assert indexed_page_stats["p95"] <= 100, indexed_page_stats
        assert sort_filter_stats["p95"] <= 100, sort_filter_stats

        # The authenticated route performs a real mutation, and a fresh
        # bridge process sees that new head through the persisted Redb store.
        source = next(doc for doc in returned_docs if doc["name"] == "Source 0000.md")
        source_before = await bridge.delegate.call(
            "get", {"owner": "alice", "workspace_id": "home", "id": source["id"]}
        )
        update = await http.put(
            f"/api/copal/documents/{source['id']}?workspace=home",
            headers=headers,
            json={"content": "# Updated\nBody updated\n", "base": source_before["head"]},
        )
        assert update.status_code == 200, update.text
        updated_head = update.json()["doc"]["head"]
        assert updated_head != source_before["head"]

    # Stop and recreate the actual Redb bridge to distinguish durable index
    # visibility from an in-memory fixture.
    await bridge.delegate.stop()
    reopened = CopalBridge(data_dir=root / "copal-redb")
    await reopened.start()
    try:
        persisted = await reopened.call(
                "metadata_get",
                {
                    "owner": "alice",
                    "workspace_id": "home",
                    "id": source["id"],
                    "state": "active",
                    "hidden": "include",
                },
        )
        assert persisted["head"] == updated_head
        assert persisted["size"] > 0
        fresh = await reopened.call(
            "get", {"owner": "alice", "workspace_id": "home", "id": source["id"]}
        )
        assert "Updated" in fresh["text"]
    finally:
        await reopened.stop()

    print(
        json.dumps(
            {
                "fixture": "authenticated FastAPI -> production Copal Redb bridge",
                "sources": CORPUS_SIZE,
                "base_id": base_id,
                "first_page_ms": round(first_page_ms, 2),
                "indexed_first_page_ms": round(indexed_first_page_ms, 2),
                "subsequent_sort_filter_ms": round(subsequent_query_ms, 2),
                "warmed_indexed_page_ms": indexed_page_stats,
                "warmed_sort_filter_ms": sort_filter_stats,
                "route_index_calls": len(index_calls_after_first_page),
                "query_count": len(
                    [call for call in bridge.calls if call[0] in {"metadata_page", "metadata_get"}]
                ),
                "mutation_visible_after_reopen": True,
            },
            sort_keys=True,
        )
    )
