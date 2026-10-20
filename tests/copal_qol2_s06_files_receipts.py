"""S06 production FilesFacade receipt and linked-child qualification.

The browser S02 test proves the mounted handler. This service test exercises
the production facade with a bounded provider and the durable FilePolicy
operation ledger, so large selections are sent as explicit <=200 child DTOs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.openclank.file_policy import FilePolicyRepository
from src.openclank.files_facade import (
    FilesFacade,
    ProviderContext,
    ProviderPage,
    ProviderResource,
)


N = 5_001
CHUNK = 200


class QualificationProvider:
    name = "host"

    def __init__(self, *, fail_operations=(), lost_operations=()):
        self.fail_operations = set(fail_operations)
        self.lost_operations = set(lost_operations)
        self.transfer_calls = []
        self.status_calls = []
        self._resources = {
            "root": ProviderResource(
                "root", "Fixture root", "folder", ("children", "stat"),
            ),
            "destination": ProviderResource(
                "destination", "Destination", "folder", ("children", "stat", "write"),
                parent_origin_id="root",
            ),
        }
        for index in range(N):
            self._resources[f"source-{index}"] = ProviderResource(
                f"source-{index}", f"Source {index:04d}.md", "file",
                ("stat", "download", "move", "copy"), parent_origin_id="root",
                mime_type="text/markdown", size=12,
                revision={"kind": "hostFingerprint", "value": f"rev-{index}"},
            )

    async def roots(self, context):
        return [self._resources["root"]]

    async def children(self, context, *, parent_origin_id, cursor, snapshot, limit, sort, query):
        assert parent_origin_id == "root"
        assert limit <= CHUNK
        all_rows = [self._resources["destination"]] + [
            self._resources[f"source-{index}"] for index in range(N)
        ]
        offset = int(cursor or 0)
        rows = tuple(all_rows[offset:offset + limit])
        next_offset = offset + len(rows)
        return ProviderPage(
            rows,
            next_cursor=str(next_offset) if next_offset < len(all_rows) else None,
            total=len(all_rows), snapshot="s06-provider-v1",
        )

    async def stat(self, context, *, origin_id):
        return self._resources[origin_id]

    async def transfer(self, context, *, source_origin_id, destination_origin_id,
                       operation, collision, expected_revision, item_id, operation_id):
        self.transfer_calls.append({
            "operation_id": operation_id, "item_id": item_id,
            "source_origin_id": source_origin_id, "destination_origin_id": destination_origin_id,
        })
        if operation_id in self.fail_operations:
            raise RuntimeError("disposable second-chunk provider failure")
        resource = self._resources[source_origin_id]
        if operation_id in self.lost_operations:
            # The provider completed its side effect but the response was lost.
            raise asyncio.CancelledError("disposable lost response")
        return resource

    async def operation_status(self, context, *, operation_id):
        self.status_calls.append(operation_id)
        if operation_id in self.lost_operations:
            return {
                "state": "complete",
                "items": [{"item_id": "lost-item", "outcome": "committed"}],
            }
        return None


def _context(generation=4):
    return ProviderContext(
        owner_subject_id="account-alice",
        owner_username="alice",
        policy_generation=generation,
        workspace_id="s06-workspace",
    )


async def _all_children(facade, context):
    root = (await facade.roots(context))["entries"][0]
    rows = []
    cursor = None
    while True:
        page = await facade.children(
            context, parent_ref=root["ref"], cursor=cursor, limit=CHUNK,
        )
        rows.extend(page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(rows) == N + 1
    assert len({row["id"] for row in rows}) == N + 1
    return rows


async def _run_batches(facade, context, sources, destination, *, label, fail_operation=None):
    receipts = []
    for chunk_index, start in enumerate(range(0, len(sources), CHUNK)):
        chunk = sources[start:start + CHUNK]
        operation_id = f"s06-{label}-{chunk_index}"
        if fail_operation == chunk_index:
            facade._providers["host"].fail_operations.add(operation_id)
        receipt = await facade.transfer_resources(
            context,
            operation_id=operation_id,
            generation=context.policy_generation,
            kind="copy",
            sources=[{
                "item_id": f"{label}-item-{start + offset}",
                "resource_ref": row["ref"],
                "expected_revision": row["revision"],
            } for offset, row in enumerate(chunk)],
            destination_ref=destination["ref"],
            collision="fail",
        )
        assert receipt["operation_id"] == operation_id
        assert len(receipt["items"]) == len(chunk)
        assert all(item["item_id"].startswith(f"{label}-item-") for item in receipt["items"])
        assert all(
            "source_origin_id" not in item and "destination_origin_id" not in item
            for item in receipt["items"]
        )
        receipts.append(receipt)
    return receipts


@pytest.mark.asyncio
async def test_linked_child_receipts_cover_201_401_5001_and_failure_recovery(tmp_path: Path):
    policy = FilePolicyRepository(tmp_path / "s06-files-policy.db")
    provider = QualificationProvider()
    facade = FilesFacade([provider], operation_store=policy)
    context = _context()
    rows = await _all_children(facade, context)
    destination = rows[0]
    sources = rows[1:]

    for total, label in ((201, "201"), (401, "401"), (N, "5001")):
        receipts = await _run_batches(
            facade, context, sources[:total], destination, label=label,
        )
        assert len(receipts) == (total + CHUNK - 1) // CHUNK
        assert sum(len(receipt["items"]) for receipt in receipts) == total
        assert all(receipt["state"] == "complete" for receipt in receipts)

    # A second-chunk failure preserves the first receipt and explicit failed
    # outcomes, while the bounded child DTO contract remains intact.
    failed = await _run_batches(
        facade, context, sources[:401], destination, label="401-failure", fail_operation=1,
    )
    assert failed[0]["state"] == "complete"
    assert failed[1]["state"] == "partial"
    assert all(item["outcome"] == "failed" for item in failed[1]["items"])
    assert all(item["code"] == "provider_unavailable" for item in failed[1]["items"])

    first_request = {
        "operation_id": "s06-idempotent",
        "generation": context.policy_generation,
        "kind": "copy",
        "sources": [{
            "item_id": "idempotent-item", "resource_ref": sources[0]["ref"],
            "expected_revision": sources[0]["revision"],
        }],
        "destination_ref": destination["ref"],
    }
    original = await facade.transfer_resources(context, **first_request)
    calls_before = len(provider.transfer_calls)
    replay = await facade.transfer_resources(context, **first_request)
    assert replay == original
    assert len(provider.transfer_calls) == calls_before
    with pytest.raises(Exception) as idempotency_conflict:
        await facade.transfer_resources(context, **{**first_request, "kind": "move"})
    assert getattr(idempotency_conflict.value, "code", None) == "idempotency_conflict"
    with pytest.raises(Exception) as wrong_generation:
        await facade.transfer_resources(
            context, **{**first_request, "operation_id": "s06-wrong-generation", "generation": 5}
        )
    assert getattr(wrong_generation.value, "code", None) == "resource_ref_stale"

    # A lost provider response leaves a pending durable ledger row. A new
    # facade/provider instance reconciles it without dispatching a duplicate.
    lost_provider = QualificationProvider(lost_operations={"s06-lost"})
    lost_facade = FilesFacade(
        [lost_provider], operation_store=policy,
    )
    lost_request = {
        "operation_id": "s06-lost", "generation": context.policy_generation, "kind": "copy",
        "sources": [{
            "item_id": "lost-item", "resource_ref": sources[1]["ref"],
            "expected_revision": sources[1]["revision"],
        }],
        "destination_ref": destination["ref"],
    }
    with pytest.raises(asyncio.CancelledError):
        await lost_facade.transfer_resources(context, **lost_request)
    recreated_provider = QualificationProvider(lost_operations={"s06-lost"})
    recreated = FilesFacade([recreated_provider], operation_store=policy)
    recovered = await recreated.operation_receipt(context, operation_id="s06-lost")
    assert recovered["state"] == "complete"
    assert recovered["items"] == [{"item_id": "lost-item", "outcome": "committed"}]
    assert recreated_provider.status_calls == ["s06-lost"]
    assert recreated_provider.transfer_calls == []

    print(
        "FilesFacade production service receipts: 201/401/5001 all-success, "
        "second-chunk partial, idempotency/generation rejection, and provider recreation passed"
    )
