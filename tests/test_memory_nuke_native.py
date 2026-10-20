"""Actual native ingest reset + Python batch/skills convergence.

Opt in with OPEN_CLANK_TEST_FM_MCP_BIN; never builds a binary or touches a
running application's stores. This is not browser/auth or LLM qualification.
"""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from services.memory.import_batch import ImportBatchError, MemoryImportBatchStore
from services.memory.nuke_coordinator import MemoryNukeCoordinator
from services.memory.skills import SkillsManager
from src.frankenmemory_provider import FrankenmemoryProvider


FM_BIN = os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN", "")
pytestmark = pytest.mark.skipif(
    not FM_BIN or not Path(FM_BIN).is_file(), reason="explicit native test binary required"
)


@pytest.mark.asyncio
async def test_native_ingest_and_skills_reset_removes_queue_not_saved_memory(tmp_path):
    """A late import result cannot recreate erased jobs; another owner survives."""
    db_path = str(tmp_path / "fm.db")
    workspace = "nuke-queue-native-test"

    def make_provider(owner):
        return FrankenmemoryProvider(
            command=FM_BIN,
            workspace_id=workspace,
            env={"FM_DB_PATH": db_path, "FM_OWNER": owner, "FM_WORKSPACE_ID": workspace},
        )

    alice, bob = make_provider("alice"), make_provider("bob")
    try:
        await asyncio.gather(alice.initialize(), bob.initialize())
        saved = {}
        for owner, provider in (("alice", alice), ("bob", bob)):
            saved[owner] = await provider.remember(
                f"{owner} retains the amber queue boundary marker.", owner=owner
            )
        batches = MemoryImportBatchStore(db_path, str(tmp_path))
        skills = SkillsManager(str(tmp_path))
        created = {}
        for owner in ("alice", "bob"):
            # Two items: a staged queued worker and a result awaiting review.
            # This is the real durable batch substrate, not invented SQL rows.
            contents = [f"{owner} queued bytes".encode(), f"{owner} review bytes".encode()]
            specs = [
                {"item_id": "source-" + str(i), "filename": f"source-{i}.json",
                 "content_hash": hashlib.sha256(content).hexdigest(),
                 "byte_size": len(content), "extension": ".json"}
                for i, content in enumerate(contents)
            ]
            batch = batches.create(owner=owner, workspace_id=workspace, session_id=None, items=specs)
            created[owner] = batch
            for item, content in zip(batch["items"], contents):
                batches.stage_bytes(owner, batch["batch_id"], item["item_id"], content)
            batches.update_item(
                owner=owner, batch_id=batch["batch_id"], item_id=batch["items"][1]["item_id"],
                state="awaiting_review",
                result={"outcome": "suggestions", "suggestions": [
                    {"text": f"{owner} awaits the violet review marker.", "category": "fact"}
                ]},
            )
            skills.add_skill(name=f"{owner}-queue-skill", description="Synthetic only",
                             when_to_use="Test", procedure=["Do nothing"], owner=owner, source="user")

        bob_batch = batches.get(owner="bob", batch_id=created["bob"]["batch_id"])
        bob_pending = batches.list_pending(owner="bob", states=["queued", "active", "awaiting_review"])
        bob_staging = batches.preview_owner_staging("bob")
        bob_skills = skills.load(owner="bob")
        assert batches.list_pending(owner="alice", states=["queued", "active", "awaiting_review"])
        coordinator = MemoryNukeCoordinator(alice, skills, data_dir=tmp_path)
        preview = await coordinator.preview(
            owner="alice", provider_owner="alice", components=["ingest", "skills"]
        )
        assert set(preview["expanded_components"]) == {"ingest", "skills"}
        assert preview["components"]["ingest"]["count"] > 0
        assert preview["components"]["skills"]["count"] > 0
        commit = dict(owner="alice", provider_owner="alice", operation_id=preview["operation_id"],
                      preview_token=preview["preview_token"], confirmation=preview["confirmation"])
        result = await coordinator.commit(**commit)
        assert result["complete"] is True, result
        assert set(result["categories"]) == {"ingest", "skills"}

        for phase in ("after reset", "after process/store restart"):
            if phase == "after process/store restart":
                await asyncio.gather(alice.shutdown(), bob.shutdown())
                alice, bob = make_provider("alice"), make_provider("bob")
                await asyncio.gather(alice.initialize(), bob.initialize())
                batches = MemoryImportBatchStore(db_path, str(tmp_path))
                skills = SkillsManager(str(tmp_path))
                coordinator = MemoryNukeCoordinator(alice, skills, data_dir=tmp_path)
            assert batches.list_pending(owner="alice", states=["queued", "active", "awaiting_review"]) == [], phase
            assert batches.preview_owner_staging("alice")["count"] == 0, phase
            assert skills.load(owner="alice") == [], phase
            with pytest.raises(ImportBatchError) as refused:
                batches.update_item(
                    owner="alice", batch_id=created["alice"]["batch_id"],
                    item_id=created["alice"]["items"][0]["item_id"], state="awaiting_review",
                    result={"outcome": "suggestions", "suggestions": [
                        {"text": "Late result must not resurrect the queue.", "category": "fact"}
                    ]},
                )
            assert refused.value.code == "batch_not_found", phase
            assert batches.list_pending(owner="alice", states=["queued", "active", "awaiting_review"]) == [], phase
            assert batches.get(owner="bob", batch_id=created["bob"]["batch_id"]) == bob_batch, phase
            assert batches.list_pending(owner="bob", states=["queued", "active", "awaiting_review"]) == bob_pending, phase
            assert batches.preview_owner_staging("bob") == bob_staging, phase
            assert skills.load(owner="bob") == bob_skills, phase
            for owner, provider in (("alice", alice), ("bob", bob)):
                record = await provider.get(saved[owner].id, owner=owner)
                assert record is not None and record.text == saved[owner].text, phase
            assert await coordinator.commit(**commit) == result, phase
    finally:
        await asyncio.gather(alice.shutdown(), bob.shutdown())
