import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.datastructures import State

from routes.skills_routes import (
    SkillOutcomeRequest,
    SkillPromotionNominateRequest,
    setup_skills_routes,
)
from services.memory.skill_lifecycle import append_usage_event
from services.memory.skills import SkillsManager


def _publish_ready(sm: SkillsManager, name: str, owner: str) -> dict:
    sm.set_necessity(name, True, owner=owner)
    sm.set_audit(
        name,
        "pass",
        worker_model="quality-judge",
        owner=owner,
        results={"verdict": "pass", "confidence": 0.93, "issues": []},
    )
    return sm.publish_readiness(name, owner)


def _published_alice_skill(sm: SkillsManager) -> dict:
    skill = sm.add_skill(
        name="ownership-race",
        description="Alice published head",
        owner="alice",
    )
    skill_dir = Path(sm._find_skill(skill["skill_id"], "alice")[0]).parent
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "private.txt").write_text(
        "alice reference",
        encoding="utf-8",
    )
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    return skill


def _arm_owner_transfer(
    sm: SkillsManager,
    monkeypatch,
    skill_id: str,
):
    original_read = sm._read_skill
    armed = False

    def transfer() -> None:
        assert sm.backfill_owner("bob", {"bob"}) == 1
        bob_path = Path(sm._find_skill(skill_id, "bob")[0])
        (bob_path.parent / "references" / "private.txt").write_text(
            "bob private reference",
            encoding="utf-8",
        )
        ready = _publish_ready(sm, skill_id, "bob")
        assert sm.publish_skill(
            skill_id,
            "bob",
            expected_revision=ready["revision"],
            expected_hash=ready["content_hash"],
            publisher="user:bob",
        )

    def racing_read(path: str):
        nonlocal armed
        skill = original_read(path)
        if (
            armed
            and skill is not None
            and skill.skill_id == skill_id
            and skill.owner == "alice"
        ):
            armed = False
            transfer()
        return skill

    def arm() -> None:
        nonlocal armed
        armed = True

    monkeypatch.setattr(sm, "_read_skill", racing_read)
    return arm


def test_immutable_revision_pointer_cas_rollback_and_usage_survive_rename(tmp_path):
    sm = SkillsManager(str(tmp_path))
    created = sm.add_skill(
        name="safe-flow",
        description="revision one",
        when_to_use="when safe",
        procedure=["do one"],
        owner="alice",
    )
    first_id = created["skill_id"]
    first_dir = Path(sm._find_skill(first_id, "alice")[0]).parent
    (first_dir / "references").mkdir()
    (first_dir / "references" / "guide.txt").write_text(
        "revision one reference",
        encoding="utf-8",
    )
    ready = _publish_ready(sm, "safe-flow", "alice")

    assert sm.publish_skill(
        first_id,
        "alice",
        expected_revision=99,
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    ) is False
    assert sm.publish_skill(
        first_id,
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )

    sm.record_retrieval(first_id, "alice")
    sm.record_use(first_id, "alice")
    assert sm.update_skill(
        first_id,
        {"name": "renamed-flow", "description": "revision two"},
        owner="alice",
    )

    head = sm.load(owner="alice")[0]
    assert head["skill_id"] == first_id
    assert head["revision"] == 2
    assert head["parent_revision"] == 1
    assert head["status"] == "draft"
    assert head["uses"] == 1
    assert head["retrievals"] == 1
    assert head["published_revision"] == 1
    assert sm.index_for(owner="alice")[0]["description"] == "revision one"
    assert sm.read_published_skill_reference(
        "renamed-flow",
        "references/guide.txt",
        owner="alice",
    ) == "revision one reference"
    sm.record_use(first_id, "alice")
    last_event = json.loads(
        (tmp_path / "skills" / "_usage_events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert last_event["revision"] == 1
    assert "the audit is for a different revision" in sm.publish_readiness(
        first_id, "alice"
    )["blockers"]

    second_dir = Path(sm._find_skill(first_id, "alice")[0]).parent
    (second_dir / "references" / "guide.txt").write_text(
        "revision two reference",
        encoding="utf-8",
    )
    ready2 = _publish_ready(sm, "renamed-flow", "alice")
    assert sm.publish_skill(
        first_id,
        "alice",
        expected_revision=ready2["revision"],
        expected_hash=ready2["content_hash"],
        publisher="user:alice",
    )
    assert sm.load(owner="alice")[0]["uses"] == 2
    assert sm.index_for(owner="alice")[0]["description"] == "revision two"
    assert sm.read_published_skill_reference(
        "renamed-flow",
        "references/guide.txt",
        owner="alice",
    ) == "revision two reference"

    assert sm.rollback_skill(first_id, 1, "alice", publisher="user:alice")
    assert sm.index_for(owner="alice")[0]["description"] == "revision one"
    assert sm.read_published_skill_reference(
        "renamed-flow",
        "references/guide.txt",
        owner="alice",
    ) == "revision one reference"


def test_activation_fails_closed_for_cleared_or_tampered_pointer(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="fail-closed", description="safe", owner="alice")
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    skill_dir = Path(sm._find_skill(skill["skill_id"], "alice")[0]).parent
    lifecycle_path = skill_dir / "_lifecycle.json"
    state = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    snapshot = skill_dir / state["published"]["snapshot"]
    snapshot.write_text(
        snapshot.read_text(encoding="utf-8") + "\nTAMPERED\n",
        encoding="utf-8",
    )
    assert sm.load_published(owner="alice") == []
    assert sm.read_published_skill_md("fail-closed", owner="alice") is None
    assert sm.load(owner="alice")[0]["active"] is False

    # Explicit null is a durable demotion even if stale head frontmatter still
    # says published (the state-first crash window).
    state["published"] = None
    lifecycle_path.write_text(json.dumps(state), encoding="utf-8")
    assert "status: published" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert sm.load_published(owner="alice") == []
    assert sm.update_skill(skill["skill_id"], {"confidence": 0.7}, owner="alice")
    assert sm.load_published(owner="alice") == []


def test_legacy_published_frontmatter_is_restaged_before_any_runtime_use(tmp_path):
    skill_dir = tmp_path / "skills" / "legacy" / "legacy-flow"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: legacy-flow
description: Legacy imported procedure
status: published
source: imported
source_revision: upstream-r1
owner: alice
---

## Procedure

1. verify locally
""",
        encoding="utf-8",
    )
    sm = SkillsManager(str(tmp_path))

    # The lookup itself performs the one-time fail-closed restaging. A bare
    # legacy status can never be treated as publication authority.
    assert sm.read_published_skill_md("legacy-flow", owner="alice") is None
    row = sm.load(owner="alice")[0]
    assert row["status"] == "draft"
    assert row["active"] is False
    assert row["source_status"] == "published"
    assert row["source_revision"] == "upstream-r1"
    assert sm.index_for(owner="alice") == []

    state = json.loads((skill_dir / "_lifecycle.json").read_text(encoding="utf-8"))
    assert state["published"] is None
    assert state["migration"] == {
        "kind": "legacy-status-restage",
        "source_status": "published",
        "restaged_at": state["migration"]["restaged_at"],
        "requires_local_publish": True,
    }
    staged = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "status: draft" in staged
    assert "source_status: published" in staged

    ready = _publish_ready(sm, row["skill_id"], "alice")
    assert not sm.publish_skill(
        row["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="audit:quality-judge",
    )
    assert sm.publish_skill(
        row["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    assert sm.index_for(owner="alice")[0]["name"] == "legacy-flow"


def test_legacy_restage_finishes_after_state_first_crash(tmp_path):
    skill_dir = tmp_path / "skills" / "legacy" / "resume-restage"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: resume-restage
description: Resume an interrupted migration
status: published
owner: alice
---
""",
        encoding="utf-8",
    )
    (skill_dir / "_lifecycle.json").write_text(
        json.dumps({
            "published": None,
            "migration": {
                "kind": "legacy-status-restage",
                "source_status": "published",
                "restaged_at": 1,
                "requires_local_publish": True,
            },
        }),
        encoding="utf-8",
    )

    row = SkillsManager(str(tmp_path)).load(owner="alice")[0]
    assert row["status"] == "draft"
    assert row["source_status"] == "published"
    staged = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "status: draft" in staged
    assert "source_status: published" in staged


def test_discovery_prunes_lifecycle_snapshots_and_owner_transfer_restages(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="one-live-row",
        description="canonical head",
        owner="alice",
    )
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    skill_path = Path(sm._find_skill(skill["skill_id"], "alice")[0])
    assert len(list(skill_path.parent.rglob("SKILL.md"))) >= 2
    assert list(sm._iter_skill_files()) == [str(skill_path)]
    assert len(sm.load_all()) == 1

    assert sm.backfill_owner("bob", {"bob"}) == 1
    rows = sm.load(owner="bob")
    assert len(rows) == 1
    assert rows[0]["revision"] == 2
    assert rows[0]["status"] == "draft"
    assert rows[0]["active"] is False
    assert sm.load_published(owner="bob") == []
    assert sm.load(owner="alice") == []
    assert list(sm._iter_skill_files()) == [str(skill_path)]


def test_startup_backfill_preserves_historical_nonempty_owner(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="historical-owner",
        description="Created by an account that may later be removed",
        owner="sam",
    )
    skill_path = Path(sm._find_skill(skill["skill_id"], "sam")[0])
    head_before = skill_path.read_bytes()

    assert sm.backfill_owner("e") == 0
    assert skill_path.read_bytes() == head_before
    assert sm.load(owner="sam")[0]["owner"] == "sam"
    assert sm.load(owner="e") == []


@pytest.mark.parametrize(
    "operation",
    [
        "read_skill_md",
        "read_skill_reference",
        "read_published_skill_md",
        "read_published_skill_reference",
        "rollback_skill",
        "delete_skill",
    ],
)
def test_owner_transfer_interleaving_cannot_read_rollback_or_delete_bob_skill(
    tmp_path,
    monkeypatch,
    operation,
):
    sm = SkillsManager(str(tmp_path))
    skill = _published_alice_skill(sm)
    arm = _arm_owner_transfer(sm, monkeypatch, skill["skill_id"])
    arm()

    if operation == "read_skill_md":
        result = sm.read_skill_md("ownership-race", owner="alice")
    elif operation == "read_skill_reference":
        result = sm.read_skill_reference(
            "ownership-race",
            "references/private.txt",
            owner="alice",
        )
    elif operation == "read_published_skill_md":
        result = sm.read_published_skill_md("ownership-race", owner="alice")
    elif operation == "read_published_skill_reference":
        result = sm.read_published_skill_reference(
            "ownership-race",
            "references/private.txt",
            owner="alice",
        )
    elif operation == "rollback_skill":
        result = sm.rollback_skill(
            skill["skill_id"],
            1,
            owner="alice",
            publisher="user:alice",
        )
    else:
        result = sm.delete_skill("ownership-race", owner="alice")

    assert result is None or result is False
    bob = sm.load_published(owner="bob")
    assert len(bob) == 1
    assert bob[0]["skill_id"] == skill["skill_id"]
    assert sm.read_published_skill_reference(
        "ownership-race",
        "references/private.txt",
        owner="bob",
    ) == "bob private reference"


def test_load_published_rechecks_the_head_under_lock_during_owner_transfer(
    tmp_path,
    monkeypatch,
):
    sm = SkillsManager(str(tmp_path))
    skill = _published_alice_skill(sm)
    arm = _arm_owner_transfer(sm, monkeypatch, skill["skill_id"])
    original_load = sm.load

    def load_then_arm(owner=None):
        rows = original_load(owner=owner)
        arm()
        return rows

    monkeypatch.setattr(sm, "load", load_then_arm)
    assert sm.load_published(owner="alice") == []
    assert sm.load_published(owner="bob")[0]["skill_id"] == skill["skill_id"]


def test_owner_metadata_backfill_never_rewrites_head_or_repairs_mismatch(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="owner-chain",
        description="published revision",
        owner="alice",
    )
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    assert sm.update_skill(
        skill["skill_id"],
        {"description": "staged head"},
        owner="alice",
    )
    skill_path = Path(sm._find_skill(skill["skill_id"], "alice")[0])
    lifecycle_path = skill_path.parent / "_lifecycle.json"
    head_before = skill_path.read_bytes()
    state = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    state.pop("owner")
    state["published"].pop("owner")
    state["published"].pop("skill_id")
    lifecycle_path.write_text(json.dumps(state), encoding="utf-8")

    assert sm.load_published(owner="alice") == []
    assert sm.backfill_owner("alice", {"alice"}) == 0
    assert skill_path.read_bytes() == head_before
    migrated = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    assert migrated["owner"] == "alice"
    assert migrated["published"]["owner"] == "alice"
    assert migrated["published"]["skill_id"] == skill["skill_id"]
    assert sm.load_published(owner="alice")[0]["description"] == "published revision"

    migrated["published"]["owner"] = "bob"
    lifecycle_path.write_text(json.dumps(migrated), encoding="utf-8")
    assert sm.backfill_owner("alice", {"alice"}) == 0
    assert skill_path.read_bytes() == head_before
    assert json.loads(
        lifecycle_path.read_text(encoding="utf-8")
    )["published"]["owner"] == "bob"
    assert sm.load_published(owner="alice") == []


def test_owner_transfer_recovers_when_the_head_write_is_interrupted(
    tmp_path,
    monkeypatch,
):
    import core.atomic_io as atomic_io

    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="resume-before-head", description="safe", owner="alice")
    original_write = atomic_io.atomic_write_text
    failed = False

    def fail_head(path, text, *args, **kwargs):
        nonlocal failed
        if not failed and str(path).endswith("SKILL.md"):
            failed = True
            raise OSError("fault after owner-transfer state write")
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(atomic_io, "atomic_write_text", fail_head)
    assert sm.backfill_owner("bob", {"bob"}) == 0
    path = Path(sm._find_skill(skill["skill_id"], "alice")[0])
    interrupted = json.loads(
        (path.parent / "_lifecycle.json").read_text(encoding="utf-8")
    )
    assert interrupted["owner"] == "bob"
    assert interrupted["owner_transfer"]["to"] == "bob"
    assert sm.load(owner="bob") == []

    monkeypatch.setattr(atomic_io, "atomic_write_text", original_write)
    assert sm.backfill_owner("bob", {"bob"}) == 1
    row = sm.load(owner="bob")[0]
    state = json.loads(
        (path.parent / "_lifecycle.json").read_text(encoding="utf-8")
    )
    assert row["revision"] == 2
    assert state["head_revision"] == row["revision"]
    assert state["head_hash"] == row["content_hash"]
    assert state["published"] is None


def test_owner_transfer_recovers_when_final_state_write_is_interrupted(
    tmp_path,
    monkeypatch,
):
    import services.memory.skills as skills_module

    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="resume-after-head", description="safe", owner="alice")
    original_ensure = skills_module.ensure_revision
    calls = 0

    def fail_final_ensure(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fault after owner-transfer head write")
        return original_ensure(*args, **kwargs)

    monkeypatch.setattr(skills_module, "ensure_revision", fail_final_ensure)
    assert sm.backfill_owner("bob", {"bob"}) == 0
    bob_path = Path(sm._find_skill(skill["skill_id"], "bob")[0])
    interrupted = json.loads(
        (bob_path.parent / "_lifecycle.json").read_text(encoding="utf-8")
    )
    assert interrupted["owner"] == "bob"
    assert interrupted["head_revision"] == 1
    assert sm.load_published(owner="bob") == []

    monkeypatch.setattr(skills_module, "ensure_revision", original_ensure)
    assert sm.backfill_owner("bob", {"bob"}) == 1
    row = sm.load(owner="bob")[0]
    state = json.loads(
        (bob_path.parent / "_lifecycle.json").read_text(encoding="utf-8")
    )
    assert row["revision"] == 2
    assert state["head_revision"] == row["revision"]
    assert state["head_hash"] == row["content_hash"]
    assert state["owner_transfer"]["completed_at"] > 0
    assert state["published"] is None


def test_usage_append_contract_does_not_lose_concurrent_events(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="counter", description="counter", owner="alice")
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: sm.record_use(skill["skill_id"], "alice"), range(100)))
    row = sm.load(owner="alice")[0]
    assert row["uses"] == 100
    assert row["retrievals"] == 0


@pytest.mark.asyncio
async def test_lifetools_usage_uses_session_owner_and_manager_contract(
    tmp_path,
    monkeypatch,
):
    from src.openclank import lifetools_server

    sm = SkillsManager(str(tmp_path))
    alice = sm.add_skill(name="alice-only", description="counter", owner="alice")
    bob = sm.add_skill(name="bob-only", description="counter", owner="bob")
    ready = _publish_ready(sm, alice["skill_id"], "alice")
    assert sm.publish_skill(
        alice["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    monkeypatch.setattr(lifetools_server, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lifetools_server, "_OWNER", "alice")
    monkeypatch.setattr(lifetools_server, "_SKILL_OWNER", "alice")

    input_schema = lifetools_server._RECORD_USAGE_TOOL.input_schema
    assert "owner" not in input_schema["properties"]
    assert set(input_schema["required"]) == {
        "name",
        "skill_id",
        "revision",
        "content_hash",
    }
    accepted = await lifetools_server.call_tool(
        "record_skill_usage",
        {
            "name": alice["skill_id"],
            "skill_id": alice["skill_id"],
            "revision": 1,
            "content_hash": alice["content_hash"],
            "owner": "bob",
        },
    )
    incomplete = await lifetools_server.call_tool(
        "record_skill_usage",
        {"name": alice["skill_id"]},
    )
    refused = await lifetools_server.call_tool(
        "record_skill_usage",
        {"name": bob["skill_id"], "owner": "bob"},
    )
    assert json.loads(accepted[0].text)["ok"] is True
    assert json.loads(incomplete[0].text) == {"ok": False}
    assert json.loads(refused[0].text) == {"ok": False}
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(
            pool.map(
                lambda _: lifetools_server._record_usage(
                    alice["skill_id"],
                    skill_id=alice["skill_id"],
                    revision=1,
                    content_hash=alice["content_hash"],
                ),
                range(99),
            )
        )

    events = [
        json.loads(line)
        for line in (
            tmp_path / "skills" / "_usage_events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert len(events) == 100
    assert {
        (row["owner"], row["skill_id"], row["revision"], row["runtime"])
        for row in events
    } == {("alice", alice["skill_id"], 1, "python")}
    assert sm.load(owner="alice")[0]["uses"] == 100
    assert sm.load(owner="bob")[0]["uses"] == 0
    assert sm.update_skill(
        alice["skill_id"],
        {"description": "new staged revision"},
        owner="alice",
    )
    assert sm.update_skill(
        alice["skill_id"],
        {"status": "draft"},
        owner="alice",
    )
    assert lifetools_server._record_usage(
        alice["skill_id"],
        skill_id=alice["skill_id"],
        revision=1,
        content_hash=alice["content_hash"],
    ) == {"ok": False}


def test_lifetools_binds_anonymous_memory_local_to_ownerless_skills(
    tmp_path,
    monkeypatch,
):
    from src.openclank import lifetools_server

    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="single-user", description="ownerless")
    ready = _publish_ready(sm, skill["skill_id"], None)
    assert sm.publish_skill(
        skill["skill_id"],
        None,
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:local",
    )
    monkeypatch.setattr(lifetools_server, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(lifetools_server, "_OWNER", "local")
    monkeypatch.setattr(lifetools_server, "_SKILL_OWNER", "")

    result = lifetools_server._record_usage(
        skill["skill_id"],
        skill_id=skill["skill_id"],
        revision=1,
        content_hash=skill["content_hash"],
    )

    assert result["ok"] is True
    assert result["owner"] == ""
    assert sm.load()[0]["uses"] == 1
def test_python_reads_the_same_owner_revision_events_written_by_mimo(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="shared-counter", description="counter", owner="alice")
    for owner in ("alice", "alice", "bob"):
        append_usage_event(
            str(tmp_path / "skills"),
            {
                "event": "use",
                "owner": owner,
                "skill_id": skill["skill_id"],
                "revision": 1,
                "timestamp": 1,
                "runtime": "mimo",
            },
        )
    assert sm.load(owner="alice")[0]["uses"] == 2
    assert sm.load(owner="bob") == []


def test_outcome_signals_are_private_metadata_and_correction_demotes(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="outcomes", description="signals", owner="alice")
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    sm.record_success(skill["skill_id"], "alice")
    sm.record_failure(skill["skill_id"], "alice")
    sm.record_mismatch(skill["skill_id"], "alice")
    sm.record_contradiction(skill["skill_id"], "alice")
    sm.record_correction(
        skill["skill_id"],
        "alice",
        replacement_skill_id="replacement-id",
    )
    row = sm.load(owner="alice")[0]
    assert (row["successes"], row["failures"], row["mismatches"]) == (1, 1, 1)
    assert (row["contradictions"], row["corrections"]) == (1, 1)
    assert row["active"] is False
    events = (tmp_path / "skills" / "_usage_events.jsonl").read_text(encoding="utf-8")
    assert "replacement-id" in events
    assert "signals" not in events


@pytest.mark.asyncio
async def test_agent_skill_view_reads_only_the_published_revision(tmp_path, monkeypatch):
    from src import constants
    from src.tools.system import do_manage_skills

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="runtime-view",
        description="Published body only",
        procedure=["published step"],
        owner="alice",
    )
    skill_dir = Path(sm._find_skill(skill["skill_id"], "alice")[0]).parent
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.txt").write_text(
        "published reference",
        encoding="utf-8",
    )
    (skill_dir / "templates").mkdir()
    (skill_dir / "templates" / "reply.txt").write_text(
        "published template",
        encoding="utf-8",
    )

    staged = await do_manage_skills(
        json.dumps({"action": "view", "name": "runtime-view"}),
        owner="alice",
    )
    assert staged["exit_code"] == 1
    staged_ref = await do_manage_skills(
        json.dumps({
            "action": "view_ref",
            "name": "runtime-view",
            "path": "references/guide.txt",
        }),
        owner="alice",
    )
    assert staged_ref["exit_code"] == 1

    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    assert sm.update_skill(
        skill["skill_id"],
        {"procedure": ["unpublished replacement"]},
        owner="alice",
    )
    (skill_dir / "references" / "guide.txt").write_text(
        "swapped live reference",
        encoding="utf-8",
    )
    (skill_dir / "templates" / "reply.txt").write_text(
        "swapped live template",
        encoding="utf-8",
    )

    active = await do_manage_skills(
        json.dumps({"action": "view", "name": "runtime-view"}),
        owner="alice",
    )
    assert "published step" in active["results"]
    assert "unpublished replacement" not in active["results"]
    active_ref = await do_manage_skills(
        json.dumps({
            "action": "view_ref",
            "name": "runtime-view",
            "path": "references/guide.txt",
        }),
        owner="alice",
    )
    assert active_ref == {"results": "published reference"}
    assert sm.read_published_skill_reference(
        "runtime-view",
        "templates/reply.txt",
        owner="alice",
    ) == "published template"
    assert sm.read_published_skill_reference(
        "runtime-view",
        "../SKILL.md",
        owner="alice",
    ) is None
    assert sm.read_published_skill_reference(
        "runtime-view",
        "references/../SKILL.md",
        owner="alice",
    ) is None
    lifecycle = json.loads(
        (skill_dir / "_lifecycle.json").read_text(encoding="utf-8")
    )
    bundle_root = skill_dir / lifecycle["published"]["bundle_root"]
    (bundle_root / "references" / "guide.txt").write_text(
        "tampered snapshot bytes",
        encoding="utf-8",
    )
    assert sm.read_published_skill_reference(
        "runtime-view",
        "references/guide.txt",
        owner="alice",
    ) is None
    assert sm.load_published(owner="alice") == []


def test_reference_changes_after_audit_require_a_new_audit(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="bundle-cas",
        description="Pin supporting files",
        owner="alice",
    )
    skill_dir = Path(sm._find_skill(skill["skill_id"], "alice")[0]).parent
    (skill_dir / "references").mkdir()
    reference = skill_dir / "references" / "guide.txt"
    reference.write_text("audited bytes", encoding="utf-8")

    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert ready["ready"] is True
    reference.write_text("changed after audit", encoding="utf-8")

    readiness = sm.publish_readiness(skill["skill_id"], "alice")
    assert readiness["ready"] is False
    assert "the reference bundle changed after its audit" in readiness["blockers"]
    assert not sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )


def test_quality_gate_requires_exact_audit_and_keeps_waiver_visible(tmp_path):
    current_platform = (
        "windows" if sys.platform.startswith("win")
        else "macos" if sys.platform == "darwin"
        else "linux"
    )
    unsupported_platform = next(
        platform
        for platform in ("linux", "macos", "windows")
        if platform != current_platform
    )
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="waived",
        description="needs review",
        owner="alice",
        platforms=[unsupported_platform],
        requires_toolsets=["not-a-real-open-clank-tool"],
    )
    _publish_ready(sm, skill["skill_id"], "alice")
    readiness = sm.publish_readiness(skill["skill_id"], "alice")
    assert not readiness["ready"]
    assert (
        f"this revision does not support {current_platform}"
        in readiness["blockers"]
    )
    assert any("not-a-real-open-clank-tool" in item for item in readiness["blockers"])
    assert not sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=skill["revision"],
        expected_hash=skill["content_hash"],
    )
    assert not sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=skill["revision"],
        expected_hash=skill["content_hash"],
        publisher="user:alice",
        waiver_reason="not authorized",
    )
    assert not sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=skill["revision"],
        expected_hash=skill["content_hash"],
        publisher="user:alice",
        waiver_reason="still not an administrator",
        allow_waiver=True,
    )
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=skill["revision"],
        expected_hash=skill["content_hash"],
        publisher="admin:alice",
        waiver_reason="incident recovery",
        allow_waiver=True,
    )
    row = sm.load(owner="alice")[0]
    assert row["waiver"]["reason"] == "incident recovery"
    assert row["trust"] != "verified"


def test_rollback_rechecks_exact_results_and_current_compatibility(tmp_path):
    current_platform = (
        "windows" if sys.platform.startswith("win")
        else "macos" if sys.platform == "darwin"
        else "linux"
    )
    incompatible_platform = next(
        platform
        for platform in ("linux", "macos", "windows")
        if platform != current_platform
    )
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(
        name="rollback-gates",
        description=f"{current_platform} revision",
        owner="alice",
        platforms=[current_platform],
    )
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )
    assert sm.update_skill(
        skill["skill_id"],
        {
            "description": f"{incompatible_platform} revision",
            "platforms": [incompatible_platform],
        },
        owner="alice",
    )
    _publish_ready(sm, skill["skill_id"], "alice")
    assert not sm.rollback_skill(
        skill["skill_id"],
        2,
        "alice",
        publisher="user:alice",
    )
    assert not sm.rollback_skill(skill["skill_id"], 1, "alice")
    assert sm.index_for(owner="alice")[0]["description"] == (
        f"{current_platform} revision"
    )


def test_low_retrieval_precision_blocks_publication(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="too-broad", description="broad", owner="alice")
    _publish_ready(sm, skill["skill_id"], "alice")
    sm.set_retrieval_precision(
        skill["skill_id"], False, "matches unrelated requests", owner="alice"
    )
    readiness = sm.publish_readiness(skill["skill_id"], "alice")
    assert readiness["ready"] is False
    assert "retrieval precision must pass" in readiness["blockers"]


def test_memory_promotion_is_literal_staged_and_role_separated(tmp_path):
    sm = SkillsManager(str(tmp_path))
    with pytest.raises(ValueError, match="two curated"):
        sm.nominate_promotion(
            owner="alice",
            recommender="user:alice",
            citations=[{"memory_id": "one", "content_hash": "a"}],
            name="deploy",
            scope="owner",
        )
    with pytest.raises(ValueError, match="independently hashed"):
        sm.nominate_promotion(
            owner="alice",
            recommender="user:alice",
            citations=[
                {"memory_id": "one", "content_hash": "same"},
                {"memory_id": "two", "content_hash": "same"},
            ],
            name="deploy",
            scope="owner",
        )
    with pytest.raises(ValueError, match="contradictory"):
        sm.nominate_promotion(
            owner="alice",
            recommender="user:alice",
            citations=[
                {"memory_id": "one", "content_hash": "a"},
                {"memory_id": "two", "content_hash": "b"},
            ],
            name="deploy",
            scope="owner",
            counterexamples=["this failed"],
        )

    candidate = sm.nominate_promotion(
        owner="alice",
        recommender="user:alice",
        citations=[
            {
                "memory_id": "one",
                "content_hash": "a",
                "source": "chat",
                "text": "PRIVATE BODY MUST NOT PERSIST",
            },
            {"memory_id": "two", "content_hash": "b", "source": "note"},
        ],
        name="deploy",
        scope="owner",
    )
    assert candidate["state"] == "candidate"
    assert "PRIVATE BODY" not in json.dumps(candidate)
    drafted = sm.draft_promotion(
        candidate["id"],
        owner="alice",
        drafter="user:alice",
        fields={
            "description": "Deploy safely",
            "when_to_use": "release time",
            "procedure": ["check", "deploy"],
            "verification": ["health is green"],
        },
    )
    assert drafted["promotion"]["state"] == "drafted"
    assert drafted["skill"]["status"] == "draft"
    _publish_ready(sm, drafted["skill"]["skill_id"], "alice")
    evaluated = sm.evaluate_promotion(
        candidate["id"],
        owner="alice",
        evaluator="audit:quality-judge",
    )
    assert evaluated["state"] == "evaluated"
    assert evaluated["attestation"]["test_results"] == {
        "verdict": "pass",
        "passed": True,
        "confidence": 0.93,
        "issue_count": 0,
    }
    with pytest.raises(ValueError, match="distinct"):
        sm.publish_promotion(
            candidate["id"],
            owner="alice",
            publisher="audit:quality-judge",
        )
    published = sm.publish_promotion(
        candidate["id"],
        owner="alice",
        publisher="user:alice",
    )
    assert published["state"] == "published"
    assert published["metrics"] == {
        "nominations": 1,
        "evaluations": 1,
        "rejections": 0,
        "publishes": 1,
        "rollbacks": 0,
    }
    skill_id = drafted["skill"]["skill_id"]
    assert sm.update_skill(skill_id, {"description": "Deploy more safely"}, owner="alice")
    ready2 = _publish_ready(sm, skill_id, "alice")
    assert sm.publish_skill(
        skill_id,
        "alice",
        expected_revision=ready2["revision"],
        expected_hash=ready2["content_hash"],
        publisher="user:alice",
    )
    rolled_back = sm.rollback_promotion(
        candidate["id"],
        1,
        owner="alice",
        actor="user:alice",
        reason="runtime correction",
    )
    assert rolled_back["state"] == "rolled_back"
    assert rolled_back["metrics"]["rollbacks"] == 1
    assert rolled_back["feedback"][-1]["reason"] == "runtime correction"

    rejected_candidate = sm.nominate_promotion(
        owner="alice",
        recommender="user:alice",
        citations=[
            {"memory_id": "three", "content_hash": "c"},
            {"memory_id": "four", "content_hash": "d"},
        ],
        name="bad-flow",
        scope="owner",
    )
    sm.draft_promotion(
        rejected_candidate["id"],
        owner="alice",
        drafter="user:alice",
        fields={"description": "weak", "procedure": ["guess"]},
    )
    rejected = sm.evaluate_promotion(
        rejected_candidate["id"],
        owner="alice",
        evaluator="audit:quality-judge",
        feedback="not enough retrieval precision",
    )
    assert rejected["state"] == "rejected"
    assert rejected["metrics"]["rejections"] == 1
    assert rejected["feedback"][-1]["reason"] == "not enough retrieval precision"


def _route_handler(router, path: str, method: str):
    return next(
        route.endpoint for route in router.routes
        if route.path == path and method in route.methods
    )


@pytest.mark.asyncio
async def test_feedback_route_links_correction_and_demotes_active_revision(tmp_path):
    sm = SkillsManager(str(tmp_path))
    skill = sm.add_skill(name="bad-guidance", description="bad", owner="alice")
    ready = _publish_ready(sm, skill["skill_id"], "alice")
    assert sm.publish_skill(
        skill["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )

    class App:
        state = State()

    request = Request(
        scope={
            "type": "http",
            "method": "POST",
            "headers": [],
            "app": App(),
            "state": {"current_user": "alice"},
        }
    )
    endpoint = _route_handler(
        setup_skills_routes(sm),
        "/api/skills/{skill_id}/feedback",
        "POST",
    )
    result = await endpoint(
        request,
        skill["skill_id"],
        SkillOutcomeRequest(
            signal="correction",
            replacement_skill_id="replacement-id",
        ),
    )
    assert result == {"ok": True, "signal": "correction"}
    assert sm.load(owner="alice")[0]["active"] is False
    events = [
        json.loads(line)
        for line in (
            tmp_path / "skills" / "_usage_events.jsonl"
        ).read_text().splitlines()
    ]
    assert any(
        event.get("replacement_skill_id") == "replacement-id"
        for event in events
    )
    assert all("description" not in event for event in events)


@pytest.mark.asyncio
async def test_nomination_hashes_owner_scoped_memories_without_copying_bodies(tmp_path):
    class Provider:
        async def get(self, memory_id, *, owner=None):
            assert owner == "alice"
            return SimpleNamespace(
                id=memory_id,
                text=f"PRIVATE BODY {memory_id}",
                source="chat",
                source_type="human",
                kind="fact",
                updated_at="2026-07-28T00:00:00Z",
                archived=False,
                provenance_conflict=memory_id == "conflict",
            )

    class App:
        state = State()

    App.state.memory_provider = Provider()
    request = Request(
        scope={
            "type": "http",
            "method": "POST",
            "headers": [],
            "app": App(),
            "state": {"current_user": "alice"},
        }
    )
    sm = SkillsManager(str(tmp_path))
    endpoint = _route_handler(
        setup_skills_routes(sm),
        "/api/skills/promotions/nominate",
        "POST",
    )
    result = await endpoint(
        request,
        SkillPromotionNominateRequest(
            memory_ids=["one", "two"],
            name="private-flow",
        ),
    )
    assert result["promotion"]["state"] == "candidate"
    stored = (tmp_path / "skills" / "_promotions.json").read_text(encoding="utf-8")
    assert "PRIVATE BODY" not in stored
    assert '"content_hash"' in stored
    with pytest.raises(HTTPException, match="contradictory"):
        await endpoint(
            request,
            SkillPromotionNominateRequest(
                memory_ids=["one", "conflict"],
                name="conflicted-flow",
            ),
        )


@pytest.mark.asyncio
async def test_skill_api_exposes_source_and_exact_last_audit_metadata(tmp_path):
    sm = SkillsManager(str(tmp_path))
    entry = sm.import_bundle_from_files(
        {
            "SKILL.md": """---
name: provenance-flow
description: Imported procedure
status: published
---

## Procedure

1. verify
""",
        },
        owner="alice",
        source_url="https://example.com/skills/provenance-flow",
        source_revision="upstream-r7",
    )
    ready = _publish_ready(sm, entry["skill_id"], "alice")
    assert sm.publish_skill(
        entry["skill_id"],
        "alice",
        expected_revision=ready["revision"],
        expected_hash=ready["content_hash"],
        publisher="user:alice",
    )

    class App:
        state = State()

    request = Request(
        scope={
            "type": "http",
            "method": "GET",
            "headers": [],
            "app": App(),
            "state": {"current_user": "alice"},
        }
    )
    router = setup_skills_routes(sm)
    listed = await _route_handler(router, "/api/skills", "GET")(request)
    row = listed["skills"][0]
    assert row["source_status"] == "published"
    assert row["source_revision"] == "upstream-r7"
    assert row["audited_at"] > 0
    assert row["last_audit"] == {
        "at": row["audited_at"],
        "verdict": "pass",
        "evaluator": "quality-judge",
        "revision": row["revision"],
        "content_hash": row["content_hash"],
    }

    indexed = await _route_handler(router, "/api/skills/index", "GET")(request)
    summary = indexed["index"][0]
    assert summary["source_status"] == "published"
    assert summary["source_revision"] == "upstream-r7"
    assert summary["audited_at"] == row["audited_at"]
    assert summary["audit_verdict"] == "pass"
