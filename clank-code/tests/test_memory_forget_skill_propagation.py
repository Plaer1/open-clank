import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from services.memory.forget_coordinator import (
    MemoryLifecycleCoordinator,
    MemorySkillForgetCoordinator,
)
from services.memory.skills import SkillsManager


def _promotion(sm: SkillsManager, owner: str, name: str) -> tuple[dict, dict]:
    candidate = sm.nominate_promotion(
        owner=owner,
        recommender=f"user:{owner}",
        citations=[
            {"memory_id": "memory-1", "content_hash": f"{owner}-one"},
            {"memory_id": "memory-2", "content_hash": f"{owner}-two"},
        ],
        name=name,
        scope="owner",
    )
    drafted = sm.draft_promotion(
        candidate["id"],
        owner=owner,
        drafter=f"user:{owner}",
        fields={
            "description": f"{owner} procedure",
            "procedure": ["check", "act"],
            "verification": ["verified"],
        },
    )
    skill_id = drafted["skill"]["skill_id"]
    sm.set_necessity(skill_id, True, owner=owner)
    sm.set_audit(
        skill_id,
        "pass",
        worker_model="quality-judge",
        owner=owner,
        results={"verdict": "pass", "confidence": 0.95, "issues": []},
    )
    evaluated = sm.evaluate_promotion(
        candidate["id"],
        owner=owner,
        evaluator="audit:quality-judge",
    )
    assert evaluated["state"] == "evaluated"
    published = sm.publish_promotion(
        candidate["id"],
        owner=owner,
        publisher=f"user:{owner}",
    )
    return published, drafted["skill"]


def test_forget_quarantines_only_owner_artifacts_and_restore_is_bounded(tmp_path):
    sm = SkillsManager(str(tmp_path))
    alice_promotion, alice_skill = _promotion(sm, "alice", "alice-flow")
    bob_promotion, bob_skill = _promotion(sm, "bob", "bob-flow")
    rows = sm._load_promotions()
    for row in rows:
        if row.get("owner") == "bob":
            row["id"] = alice_promotion["id"]
    sm._save_promotions(rows)
    bob_promotion["id"] = alice_promotion["id"]
    coordinator = MemorySkillForgetCoordinator(sm)

    stale = coordinator.preview("alice", ["memory-1"])
    assert stale["promotion_ids"] == [alice_promotion["id"]]
    assert stale["skill_ids"] == [alice_skill["skill_id"]]

    assert sm.update_skill(
        alice_skill["skill_id"],
        {"description": "new head", "source_memory_ids": []},
        owner="alice",
    )
    with pytest.raises(ValueError, match="closure changed"):
        coordinator.prepare("alice", ["memory-1"], stale["token"])

    current = coordinator.preview("alice", ["memory-1"])
    assert current["skill_ids"] == [alice_skill["skill_id"]]
    local_id = coordinator.prepare("alice", ["memory-1"], current["token"])
    assert sm.list_promotions("alice") == []
    assert sm.load_published(owner="alice") == []
    assert [row["id"] for row in sm.list_promotions("bob")] == [
        bob_promotion["id"]
    ]
    assert [row["skill_id"] for row in sm.load_published(owner="bob")] == [
        bob_skill["skill_id"]
    ]
    reserved = sm.add_skill(
        name=alice_skill["name"],
        description="new unrelated draft",
        owner="alice",
        source="user",
    )
    assert reserved["name"] != alice_skill["name"]
    recover_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    coordinator.finalize(
        local_id,
        provider_tombstone_id="forget-provider-1",
        recover_until=recover_until.isoformat(),
    )
    with pytest.raises(ValueError, match="out of scope"):
        coordinator.restore(local_id, owner="bob")

    coordinator.preflight_restore(
        local_id,
        owner="alice",
        provider_tombstone_id="forget-provider-1",
    )
    with pytest.raises(ValueError, match="out of scope"):
        coordinator.preflight_restore(
            local_id,
            owner="alice",
            provider_tombstone_id="forget-provider-other",
        )
    coordinator.mark_provider_restored(
        local_id,
        owner="alice",
        provider_tombstone_id="forget-provider-1",
    )
    restored = coordinator.restore(local_id, owner="alice")
    assert restored["restored_promotion_ids"] == [alice_promotion["id"]]
    assert restored["restored_skill_ids"] == [alice_skill["skill_id"]]
    assert [row["id"] for row in sm.list_promotions("alice")] == [
        alice_promotion["id"]
    ]
    assert [row["skill_id"] for row in sm.load_published(owner="alice")] == [
        alice_skill["skill_id"]
    ]

    expiring = coordinator.preview("alice", ["memory-1"])
    expired_id = coordinator.prepare("alice", ["memory-1"], expiring["token"])
    coordinator.finalize(
        expired_id,
        provider_tombstone_id="forget-provider-2",
        recover_until=(
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat(),
    )
    with pytest.raises(ValueError, match="expired"):
        coordinator.preflight_restore(
            expired_id,
            owner="alice",
            provider_tombstone_id="forget-provider-2",
        )


def test_pending_forget_blocks_new_promotion_uri_lineage(tmp_path):
    sm = SkillsManager(str(tmp_path))
    promotion, _skill = _promotion(sm, "alice", "source-uri-flow")
    unrelated = sm.add_skill(
        name="unrelated-before-forget",
        description="candidate for a later provenance edit",
        owner="alice",
    )
    coordinator = MemorySkillForgetCoordinator(sm)
    preview = coordinator.preview("alice", ["memory-1"])
    coordinator.prepare(
        "alice",
        ["memory-1"],
        preview["token"],
        recovery_id="e" * 32,
    )
    source_uri = f"memory-promotion:{promotion['id']}"

    with pytest.raises(ValueError, match="unfinished memory lifecycle"):
        sm.add_skill(
            name="late-promotion-link",
            description="must not cross the forget boundary",
            source_uri=source_uri,
            owner="alice",
        )
    with pytest.raises(ValueError, match="unfinished memory lifecycle"):
        sm.import_bundle_from_files(
            {
                "SKILL.md": f"""---
name: imported-late-promotion-link
description: Must remain blocked
source_uri: {source_uri}
---

## Procedure

1. stop
""",
            },
            owner="alice",
        )
    with pytest.raises(ValueError, match="unfinished memory lifecycle"):
        sm.update_skill(
            unrelated["skill_id"],
            {"source_uri": source_uri},
            owner="alice",
        )


def test_remote_import_preserves_memory_promotion_lineage(tmp_path):
    sm = SkillsManager(str(tmp_path))
    promotion, _skill = _promotion(sm, "alice", "import-lineage-flow")
    source_uri = f"memory-promotion:{promotion['id']}"
    imported = sm.import_bundle_from_files(
        {
            "SKILL.md": f"""---
name: imported-memory-lineage
description: Imported derived procedure
source_uri: {source_uri}
---

## Procedure

1. verify
""",
        },
        owner="alice",
        source_url="https://example.com/imported-memory-lineage",
        source_revision="revision-1",
    )

    assert imported["source_uri"] == source_uri
    preview = MemorySkillForgetCoordinator(sm).preview(
        "alice",
        ["memory-1"],
    )
    assert imported["skill_id"] in preview["skill_ids"]


def test_startup_repairs_partial_hard_crash_staging(tmp_path):
    sm = SkillsManager(str(tmp_path))
    promotion, skill = _promotion(sm, "alice", "hard-crash-flow")
    coordinator = MemorySkillForgetCoordinator(sm)
    snapshot = coordinator._snapshot("alice", ["memory-1"])
    recovery_id = "c" * 32
    staging = coordinator.recovery_root / ".staging-hard-crash"
    staging.mkdir(parents=True)
    manifest = {
        "version": 1,
        "id": recovery_id,
        "owner": "alice",
        "provider_owner": "alice",
        "workspace_id": "global",
        "operation_kind": "forget",
        "state": "preparing",
        "created_at": 1,
        "memory_ids": snapshot["memory_ids"],
        "preview_token": snapshot["token"],
        "promotions": snapshot["promotions"],
        "skills": snapshot["skills"],
    }
    (staging / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    row = snapshot["_skill_rows"][0]
    active = Path(row["_path"]).parent
    (active / ".memory-forgotten.json").write_text(
        json.dumps({
            "id": recovery_id,
            "owner": "alice",
            "skill_id": row["skill_id"],
        }),
        encoding="utf-8",
    )
    partial = staging / "skills" / Path(*row["relative_dir"].split("/"))
    partial.mkdir(parents=True)
    os.replace(active / "SKILL.md", partial / "SKILL.md")

    restarted = MemorySkillForgetCoordinator(SkillsManager(str(tmp_path)))

    assert list(restarted.recovery_root.glob(".staging-*")) == []
    assert sm.list_promotions("alice")[0]["id"] == promotion["id"]
    assert sm.load_published(owner="alice")[0]["skill_id"] == skill["skill_id"]


def test_owner_bound_lifecycle_reconcile_skips_other_users_journals(tmp_path):
    sm = SkillsManager(str(tmp_path))
    alice = sm.add_skill(
        name="alice-pending",
        description="alice",
        source_memory_ids=["memory-1"],
        owner="alice",
    )
    bob = sm.add_skill(
        name="bob-pending",
        description="bob",
        source_memory_ids=["memory-1"],
        owner="bob",
    )
    local = MemorySkillForgetCoordinator(sm)
    alice_preview = local.preview("alice", ["memory-1"])
    local.prepare(
        "alice",
        ["memory-1"],
        alice_preview["token"],
        recovery_id="a" * 32,
    )
    bob_preview = local.preview("bob", ["memory-1"])
    local.prepare(
        "bob",
        ["memory-1"],
        bob_preview["token"],
        recovery_id="b" * 32,
    )

    class Provider:
        calls = []

        async def forget(self, action, **kwargs):
            self.calls.append((action, kwargs))
            return {"state": "absent"}

    provider = Provider()
    lifecycle = MemoryLifecycleCoordinator(
        provider,
        skill_forget=local,
        skill_owner="alice",
    )
    assert asyncio.run(lifecycle.reconcile()) == {
        "rolled_back": 1,
        "committed": 0,
        "restored": 0,
        "errors": 0,
    }
    assert provider.calls == [
        (
            "status",
            {
                "owner": "alice",
                "workspace_id": "global",
                "operation_id": "a" * 32,
            },
        )
    ]
    assert [row["skill_id"] for row in sm.load(owner="alice")] == [
        alice["skill_id"]
    ]
    assert sm.load(owner="bob") == []
    assert [row["owner"] for row in local.pending_operations()] == ["bob"]
    assert bob["skill_id"] in local.operation_info(
        "b" * 32,
        owner="bob",
    )["skill_ids"]


def test_malformed_promotion_identity_fails_before_any_quarantine(tmp_path):
    sm = SkillsManager(str(tmp_path))
    promotion, skill = _promotion(sm, "alice", "malformed-flow")
    rows = sm._load_promotions()
    rows[0].pop("id")
    sm._save_promotions(rows)
    coordinator = MemorySkillForgetCoordinator(sm)

    with pytest.raises(ValueError, match="stable identity"):
        coordinator.preview("alice", ["memory-1"])
    assert sm.load_published(owner="alice")[0]["skill_id"] == skill["skill_id"]
    assert sm._load_promotions()[0]["name"] == promotion["name"]


def test_nonrecoverable_forget_purges_payload_but_keeps_idempotency_ledger(
    tmp_path,
):
    sm = SkillsManager(str(tmp_path))
    promotion, skill = _promotion(sm, "alice", "permanent-flow")
    coordinator = MemorySkillForgetCoordinator(sm)
    preview = coordinator.preview("alice", ["memory-1"])
    local_id = coordinator.prepare("alice", ["memory-1"], preview["token"])

    assert coordinator.finalize(
        local_id,
        provider_tombstone_id="forget-provider",
        recover_until=None,
    ) is False
    ledger = coordinator.recovery_root / local_id
    assert ledger.is_dir()
    assert not (ledger / "skills").exists()
    assert coordinator.operation_info(
        local_id,
        owner="alice",
    )["state"] == "committed_irrecoverable"
    assert sm.list_promotions("alice") == []
    assert sm.load_published(owner="alice") == []
    recreated = sm.add_skill(
        name=skill["name"],
        description="new unrelated skill",
        owner="alice",
        source="user",
    )
    assert recreated["name"] == skill["name"]
    assert recreated["skill_id"] != skill["skill_id"]
    assert promotion["id"] not in {
        row.get("id") for row in sm._load_promotions()
    }


def test_prepare_failure_restores_runtime_skills_and_promotions(
    tmp_path,
    monkeypatch,
):
    sm = SkillsManager(str(tmp_path))
    promotion, skill = _promotion(sm, "alice", "rollback-flow")
    coordinator = MemorySkillForgetCoordinator(sm)
    preview = coordinator.preview("alice", ["memory-1"])
    original_save = sm._save_promotions
    calls = 0

    def fail_once(rows):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected write failure")
        return original_save(rows)

    monkeypatch.setattr(sm, "_save_promotions", fail_once)
    with pytest.raises(OSError, match="injected"):
        coordinator.prepare("alice", ["memory-1"], preview["token"])

    assert [row["id"] for row in sm.list_promotions("alice")] == [
        promotion["id"]
    ]
    assert [row["skill_id"] for row in sm.load_published(owner="alice")] == [
        skill["skill_id"]
    ]
    recoveries = [
        path
        for path in coordinator.recovery_root.iterdir()
        if path.is_dir()
    ]
    assert recoveries == []


def test_final_quarantine_rename_failure_rolls_back_every_artifact(
    tmp_path,
    monkeypatch,
):
    sm = SkillsManager(str(tmp_path))
    promotion, skill = _promotion(sm, "alice", "rename-failure-flow")
    coordinator = MemorySkillForgetCoordinator(sm)
    preview = coordinator.preview("alice", ["memory-1"])
    original_replace = os.replace

    def fail_final_rename(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            source_path.name.startswith(".staging-")
            and destination_path.parent == coordinator.recovery_root
            and not destination_path.name.startswith(".staging-")
        ):
            raise OSError("injected final rename failure")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_final_rename)
    with pytest.raises(OSError, match="final rename"):
        coordinator.prepare("alice", ["memory-1"], preview["token"])

    assert sm.list_promotions("alice")[0]["id"] == promotion["id"]
    assert sm.load_published(owner="alice")[0]["skill_id"] == skill["skill_id"]
    assert list(coordinator.recovery_root.iterdir()) == []


def test_tampered_recovery_bundle_fails_before_provider_restore(tmp_path):
    sm = SkillsManager(str(tmp_path))
    _promotion_row, skill = _promotion(sm, "alice", "tamper-flow")
    coordinator = MemorySkillForgetCoordinator(sm)
    preview = coordinator.preview("alice", ["memory-1"])
    local_id = coordinator.prepare("alice", ["memory-1"], preview["token"])
    coordinator.finalize(
        local_id,
        provider_tombstone_id="forget-provider",
        recover_until="2099-01-01T00:00:00+00:00",
    )
    manifest_path = coordinator.recovery_root / local_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    relative = Path(*manifest["skills"][0]["relative_dir"].split("/"))
    recovered_skill = (
        coordinator.recovery_root
        / local_id
        / "skills"
        / relative
        / "SKILL.md"
    )
    recovered_skill.write_text(
        recovered_skill.read_text(encoding="utf-8") + "\nTampered.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="identity no longer matches"):
        coordinator.preflight_restore(
            local_id,
            owner="alice",
            provider_tombstone_id="forget-provider",
        )
    assert sm.load(owner="alice") == []
    assert skill["skill_id"] in preview["skill_ids"]


def test_startup_reconciliation_rolls_back_absent_and_finalizes_committed(
    tmp_path,
):
    sm = SkillsManager(str(tmp_path))
    promotion, skill = _promotion(sm, "alice", "reconcile-flow")
    coordinator = MemorySkillForgetCoordinator(sm)

    preview = coordinator.preview("alice", ["memory-1"])
    absent_id = "a" * 32
    coordinator.prepare(
        "alice",
        ["memory-1"],
        preview["token"],
        recovery_id=absent_id,
    )

    class Provider:
        state = "absent"

        async def forget(self, action, **kwargs):
            assert action == "status"
            return (
                {"state": "absent"}
                if self.state == "absent"
                else {
                    "state": "committed",
                    "tombstone_id": "forget-provider",
                    "recover_until": "2099-01-01T00:00:00+00:00",
                    "closure": {
                        "raw_ids": [],
                        "candidate_ids": [],
                        "curated_ids": ["memory-1"],
                        "graph_node_ids": [],
                    },
                }
            )

    provider = Provider()
    result = asyncio.run(coordinator.reconcile(provider))
    assert result == {
        "rolled_back": 1,
        "committed": 0,
        "restored": 0,
        "errors": 0,
    }
    assert sm.list_promotions("alice")[0]["id"] == promotion["id"]
    assert sm.load_published(owner="alice")[0]["skill_id"] == skill["skill_id"]

    current = coordinator.preview("alice", ["memory-1"])
    committed_id = "b" * 32
    coordinator.prepare(
        "alice",
        ["memory-1"],
        current["token"],
        recovery_id=committed_id,
    )
    provider.state = "committed"
    result = asyncio.run(coordinator.reconcile(provider))
    assert result == {
        "rolled_back": 0,
        "committed": 1,
        "restored": 0,
        "errors": 0,
    }
    assert sm.load_published(owner="alice") == []
    assert coordinator.preflight_restore(
        committed_id,
        owner="alice",
        provider_tombstone_id="forget-provider",
    )["provider_restored"] is False
