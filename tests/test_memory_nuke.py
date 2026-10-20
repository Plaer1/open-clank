import asyncio
import itertools
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.memory.memory_routes as memory_routes
from core.database import Base, Memory
from services.memory.nuke_coordinator import (
    MemoryNukeConflict,
    MemoryNukeCoordinator,
    MemoryNukeError,
)
from services.memory.import_batch import MemoryImportBatchStore
from services.memory.skills import SkillsManager
from src.memory import MemoryManager
from src.memory_provider import NativeMemoryProvider


class _Provider:
    def __init__(self):
        self.snapshot = {
            "memories": {"count": 2, "fingerprint": "memory-v1"},
            "graph": {"count": 1, "fingerprint": "graph-v1"},
            "ingest": {"count": 3, "fingerprint": "ingest-v1"},
        }
        self.calls = []

    async def reset_owner(
        self,
        action,
        *,
        owner=None,
        components,
        expected_counts=None,
    ):
        self.calls.append((action, owner, list(components), expected_counts))
        expanded = (
            ["memories", "graph", "ingest"]
            if "memories" in components
            else list(components)
        )
        if action == "reset_preview":
            return {
                "components": dict(self.snapshot),
                "expanded_components": expanded,
                "implications": ["provider implication"],
            }
        assert action == "reset_commit"
        for component in expanded:
            if self.snapshot[component]["count"] != 0:
                assert expected_counts[component] == self.snapshot[component]
        categories = {
            component: {
                "state": "complete",
                **self.snapshot[component],
            }
            for component in expanded
        }
        for component in expanded:
            self.snapshot[component] = {
                "count": 0,
                "fingerprint": f"{component}-empty",
            }
        return {"complete": True, "categories": categories}


class _FailOnceProvider(_Provider):
    def __init__(self):
        super().__init__()
        self.commit_attempts = 0

    async def reset_owner(
        self,
        action,
        *,
        owner=None,
        components,
        expected_counts=None,
    ):
        if action == "reset_commit":
            self.commit_attempts += 1
            if self.commit_attempts == 1:
                raise RuntimeError("provider reset boom")
        return await super().reset_owner(
            action,
            owner=owner,
            components=components,
            expected_counts=expected_counts,
        )


class _AgentMemory:
    def __init__(self, expected_owner="alice"):
        self.preview = {"count": 1, "fingerprint": "agent-v1"}
        self.reset_calls = []
        self.expected_owner = expected_owner

    async def preview_owner_memory(self, owner):
        assert owner == self.expected_owner
        return dict(self.preview)

    async def reset_owner_memory(self, owner, *, expected):
        self.reset_calls.append((owner, expected))
        if self.preview["count"] == 0:
            return {"complete": True, "count": 0}
        assert expected == self.preview
        count = self.preview["count"]
        self.preview = {"count": 0, "fingerprint": "agent-empty"}
        return {"complete": True, "count": count}


class _MediaStore:
    def __init__(self):
        self.preview = {"count": 4, "fingerprint": "media-v1"}
        self.purge_calls = []

    def preview_owner_purge(self, owner):
        assert owner == "alice"
        return dict(self.preview)

    def purge_owner(self, owner, *, expected):
        self.purge_calls.append((owner, expected))
        assert expected == self.preview
        count = self.preview["count"]
        self.preview = {"count": 0, "fingerprint": "media-empty"}
        return {"complete": True, "count": count, "blobs_removed": 2}


def _sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_compatibility_memory(tmp_path, sessions):
    manager = MemoryManager(str(tmp_path))
    manager.save([
        {"id": "json-alice", "text": "alice legacy", "owner": "alice"},
        {"id": "json-bob", "text": "bob legacy", "owner": "bob"},
    ])
    (tmp_path / "memory_tidy_state.json").write_text(
        json.dumps({"alice": {"fingerprint": "a"}, "bob": {"fingerprint": "b"}}),
        encoding="utf-8",
    )
    with sessions() as session:
        session.add_all([
            Memory(id="db-alice", text="alice sql", owner="alice", timestamp=1),
            Memory(id="db-bob", text="bob sql", owner="bob", timestamp=1),
        ])
        session.commit()
    return manager


@pytest.mark.asyncio
async def test_nuke_preview_commit_is_owner_bound_expanded_and_idempotent(tmp_path):
    sessions = _sessions(tmp_path)
    manager = _seed_compatibility_memory(tmp_path, sessions)
    skills = SkillsManager(str(tmp_path))
    skills.add_skill(
        name="alice-skill",
        description="alice only",
        when_to_use="test",
        procedure=["one"],
        owner="alice",
        source="user",
    )
    skills.add_skill(
        name="bob-skill",
        description="bob only",
        when_to_use="test",
        procedure=["one"],
        owner="bob",
        source="user",
    )
    provider = _Provider()
    agent_memory = _AgentMemory()
    cancelled = []
    coordinator = MemoryNukeCoordinator(
        provider,
        skills,
        memory_manager=manager,
        session_factory=sessions,
        skill_job_canceller=cancelled.append,
        data_dir=tmp_path,
        preview_ttl_seconds=60,
    )

    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["memories", "skills"],
        agent_supervisor=agent_memory,
    )
    assert preview["status"] == "preview"
    assert preview["complete"] is False
    assert preview["expanded_components"] == ["graph", "ingest", "memories", "skills"]
    # provider 2 + agent 1 + memory.json 1 + tidy 1 + app.db 1
    assert preview["components"]["memories"]["count"] == 6
    assert preview["components"]["skills"]["count"] == 1
    assert {item["id"] for item in preview["retained"]} == {
        "rag_documents",
        "exports_backups_sources",
        "memory_policy",
    }
    assert oct((tmp_path / ".memory-nuke").stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / ".memory-nuke" / "operations.json").stat().st_mode & 0o777) == "0o600"

    with pytest.raises(MemoryNukeConflict):
        await coordinator.commit(
            owner="bob",
            provider_owner="bob",
            operation_id=preview["operation_id"],
            preview_token=preview["preview_token"],
            confirmation=preview["confirmation"],
            agent_supervisor=agent_memory,
        )

    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent_memory,
    )
    assert result["complete"] is True, result
    assert result["status"] == "complete"
    assert result["categories"]["memories"]["count"] == 6
    assert set(result["categories"]) == {"memories", "graph", "ingest", "skills"}
    assert result["receipt"]["attempt"] == 1
    assert result["receipt"]["requested_components"] == ["memories", "skills"]
    assert result["receipt"]["principal_baseline"]["state"] == "cache_invalidated"
    assert result["receipt"]["retained"] == preview["retained"]
    assert cancelled == ["alice"]
    assert agent_memory.reset_calls

    remaining_json = manager.load_all()
    assert [row["id"] for row in remaining_json] == ["json-bob"]
    tidy = json.loads((tmp_path / "memory_tidy_state.json").read_text(encoding="utf-8"))
    assert tidy == {"bob": {"fingerprint": "b"}}
    with sessions() as session:
        assert [row.id for row in session.query(Memory).all()] == ["db-bob"]
    assert [row["name"] for row in skills.load(owner="bob")] == ["bob-skill"]
    assert skills.load(owner="alice") == []

    replay = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent_memory,
    )
    assert replay == result


def test_nuke_rejects_empty_unknown_and_duplicate_components(tmp_path):
    coordinator = MemoryNukeCoordinator(_Provider(), data_dir=tmp_path)
    for components in ([], ["all"], ["memories", "memories"]):
        with pytest.raises(MemoryNukeError):
            coordinator.normalize_components(components)


@pytest.mark.asyncio
async def test_memories_reset_includes_owner_media_preview_and_purge(tmp_path):
    media = _MediaStore()
    agent = _AgentMemory()
    coordinator = MemoryNukeCoordinator(
        _Provider(),
        media_store=media,
        data_dir=tmp_path,
    )

    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["memories"],
        agent_supervisor=agent,
    )
    assert preview["components"]["memories"]["count"] == 7

    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )
    assert result["complete"] is True
    assert result["categories"]["memories"]["count"] == 7
    assert media.purge_calls == [("alice", {"count": 4, "fingerprint": "media-v1"})]


@pytest.mark.asyncio
async def test_memories_reset_invalidates_principal_cache(monkeypatch, tmp_path):
    invalidated = []
    monkeypatch.setattr(
        "services.memory.principal_context.invalidate_principal_cache",
        lambda owner=None: invalidated.append(owner),
    )
    coordinator = MemoryNukeCoordinator(_Provider(), data_dir=tmp_path)
    agent = _AgentMemory()
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["memories"],
        agent_supervisor=agent,
    )

    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )

    assert result["complete"] is True
    assert invalidated == ["alice"]


@pytest.mark.asyncio
async def test_principal_cache_failure_is_partial_and_retry_preserves_receipt_counts(
    monkeypatch, tmp_path
):
    attempts = []

    def fail_once(owner):
        attempts.append(owner)
        if len(attempts) == 1:
            raise RuntimeError("cache reset boom")

    monkeypatch.setattr(
        "services.memory.principal_context.invalidate_principal_cache",
        fail_once,
    )
    provider = _Provider()
    agent = _AgentMemory()
    coordinator = MemoryNukeCoordinator(provider, data_dir=tmp_path)
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["memories"],
        agent_supervisor=agent,
    )

    first = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )
    assert first["complete"] is False
    assert first["categories"]["memories"]["count"] == 3
    assert "principal baseline convergence failed" in first["categories"]["memories"]["error"]

    retry = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )
    assert retry["complete"] is True
    assert retry["categories"]["memories"]["count"] == 3
    assert retry["categories"]["graph"]["count"] == 1
    assert retry["categories"]["ingest"]["count"] == 3
    assert retry["receipt"]["attempt"] == 2
    assert attempts == ["alice", "alice"]


@pytest.mark.asyncio
async def test_provider_failure_preserves_successful_compatibility_deletion_counts(
    tmp_path,
):
    sessions = _sessions(tmp_path)
    manager = _seed_compatibility_memory(tmp_path, sessions)
    provider = _FailOnceProvider()
    agent = _AgentMemory()
    coordinator = MemoryNukeCoordinator(
        provider,
        memory_manager=manager,
        session_factory=sessions,
        data_dir=tmp_path,
    )
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["memories"],
        agent_supervisor=agent,
    )

    first = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )

    assert first["status"] == "partial"
    # Authored agent memory (1) plus the three compatibility rows were really
    # removed even though the provider category failed in this attempt.
    assert first["categories"]["memories"]["count"] == 4

    retry = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )
    assert retry["complete"] is True
    assert retry["categories"]["memories"]["count"] == 6
    assert retry["receipt"]["attempt"] == 2


@pytest.mark.asyncio
async def test_live_foreign_commit_claim_blocks_duplicate_destructive_work(tmp_path):
    provider = _Provider()
    coordinator = MemoryNukeCoordinator(provider, data_dir=tmp_path)
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["graph"],
    )
    journal_path = tmp_path / ".memory-nuke" / "operations.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    row = journal["operations"][preview["operation_id"]]
    row["state"] = "committing"
    row["commit_claim"] = {
        "coordinator_id": "another-coordinator",
        "host": socket.gethostname(),
        "pid": os.getpid(),
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(MemoryNukeConflict, match="already in progress"):
        await coordinator.commit(
            owner="alice",
            provider_owner="alice",
            operation_id=preview["operation_id"],
            preview_token=preview["preview_token"],
            confirmation=preview["confirmation"],
        )

    assert [call[0] for call in provider.calls] == ["reset_preview"]


@pytest.mark.asyncio
async def test_dead_commit_claim_is_recoverable_after_process_failure(tmp_path):
    provider = _Provider()
    coordinator = MemoryNukeCoordinator(provider, data_dir=tmp_path)
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["graph"],
    )
    journal_path = tmp_path / ".memory-nuke" / "operations.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    row = journal["operations"][preview["operation_id"]]
    row["state"] = "committing"
    row["commit_claim"] = {
        "coordinator_id": "dead-coordinator",
        "host": socket.gethostname(),
        "pid": -1,
    }
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
    )
    assert result["complete"] is True
    assert [call[0] for call in provider.calls] == ["reset_preview", "reset_commit"]


@pytest.mark.asyncio
async def test_memories_reset_reseeds_reserved_principal_baseline(
    monkeypatch, tmp_path
):
    invalidated = []
    reseeded = []
    provider = _Provider()
    provider._fm_db_path = str(tmp_path / "fm.db")

    monkeypatch.setattr(
        "services.memory.principal_context.invalidate_principal_cache",
        invalidated.append,
    )

    def ensure(**kwargs):
        reseeded.append(kwargs)
        return {
            "assistant_entity_id": "principal_assistant_" + ("a" * 32),
            "handler_entity_id": "principal_handler_" + ("b" * 32),
        }

    monkeypatch.setattr(
        "services.memory.principal_context.ensure_principal_context_cached",
        ensure,
    )
    coordinator = MemoryNukeCoordinator(provider, data_dir=tmp_path)
    agent = _AgentMemory()
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["memories"],
        agent_supervisor=agent,
    )
    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent,
    )

    assert result["complete"] is True
    assert result["receipt"]["principal_baseline"] == {
        "state": "reseeded",
        "assistant": "self",
        "handler": "Handler",
    }
    assert invalidated == ["alice"]
    assert reseeded == [
        {
            "owner": "alice",
            "workspace_id": "global",
            "db_path": str(tmp_path / "fm.db"),
        }
    ]


@pytest.mark.asyncio
async def test_ingest_reset_clears_only_previewed_owner_staging(tmp_path):
    staging = MemoryImportBatchStore(str(tmp_path / "fm.db"), str(tmp_path))
    staging.stage_bytes(
        "alice",
        "batch_" + ("a" * 32),
        "item_" + ("b" * 32),
        b"alice staged import",
    )
    staging.stage_bytes(
        "bob",
        "batch_" + ("c" * 32),
        "item_" + ("d" * 32),
        b"bob staged import",
    )
    coordinator = MemoryNukeCoordinator(_Provider(), data_dir=tmp_path)

    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["ingest"],
    )
    assert preview["components"]["ingest"]["count"] == 4
    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
    )

    assert result["complete"] is True
    assert result["categories"]["ingest"]["count"] == 4
    assert staging.preview_owner_staging("alice")["count"] == 0
    assert staging.preview_owner_staging("bob")["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "components",
    [
        list(selection)
        for size in range(1, 5)
        for selection in itertools.combinations(
            ["memories", "graph", "ingest", "skills"], size
        )
    ],
)
async def test_every_nonempty_component_selection_previews_exact_closure(
    tmp_path, components
):
    coordinator = MemoryNukeCoordinator(
        _Provider(),
        SkillsManager(str(tmp_path)),
        data_dir=tmp_path,
    )
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=components,
        agent_supervisor=_AgentMemory(),
    )

    expected = set(components)
    if "memories" in expected:
        expected.update({"graph", "ingest"})
    assert set(preview["expanded_components"]) == expected
    assert set(preview["components"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "components",
    [
        list(selection)
        for size in range(1, 5)
        for selection in itertools.combinations(
            ["memories", "graph", "ingest", "skills"], size
        )
    ],
)
async def test_every_nonempty_component_selection_commits_exactly_and_replays(
    tmp_path, components, monkeypatch
):
    """Every UI selection must commit its previewed closure without tenant drift."""
    sessions = _sessions(tmp_path)
    manager = _seed_compatibility_memory(tmp_path, sessions)
    skills = SkillsManager(str(tmp_path))
    skills.add_skill(
        name="alice-skill",
        description="alice only",
        when_to_use="test",
        procedure=["one"],
        owner="alice",
        source="user",
    )
    skills.add_skill(
        name="bob-skill",
        description="bob only",
        when_to_use="test",
        procedure=["one"],
        owner="bob",
        source="user",
    )
    provider = _Provider()
    provider._fm_db_path = str(tmp_path / "fm.db")
    monkeypatch.setattr(
        "services.memory.principal_context.ensure_principal_context_cached",
        lambda **kwargs: {
            "assistant_entity_id": "principal_assistant_" + ("a" * 32),
            "handler_entity_id": "principal_handler_" + ("b" * 32),
        },
    )
    staging = MemoryImportBatchStore(provider._fm_db_path, str(tmp_path))
    staging.stage_bytes(
        "alice", "batch_" + ("a" * 32), "item_" + ("b" * 32), b"alice staged"
    )
    staging.stage_bytes(
        "bob", "batch_" + ("c" * 32), "item_" + ("d" * 32), b"bob staged"
    )
    agent_memory = _AgentMemory()
    coordinator = MemoryNukeCoordinator(
        provider,
        skills,
        memory_manager=manager,
        session_factory=sessions,
        data_dir=tmp_path,
    )

    requested = set(components)
    closure = requested | ({"graph", "ingest"} if "memories" in requested else set())
    provider_before = {
        name: dict(value) for name, value in provider.snapshot.items()
    }
    memory_file = (tmp_path / "memory.json").read_bytes()
    tidy_file = (tmp_path / "memory_tidy_state.json").read_bytes()
    alice_skill = skills.load(owner="alice")
    bob_skill = skills.load(owner="bob")
    alice_staging = staging.preview_owner_staging("alice")
    bob_staging = staging.preview_owner_staging("bob")
    empty_staging = staging.preview_owner_staging("charlie")
    with sessions() as session:
        alice_rows = [(row.id, row.text, row.owner) for row in session.query(Memory).filter(Memory.owner == "alice")]
        bob_rows = [(row.id, row.text, row.owner) for row in session.query(Memory).filter(Memory.owner == "bob")]

    def skill_bytes_by_owner():
        snapshots = {"alice": {}, "bob": {}}
        for path in Path(skills.skills_root).rglob("SKILL.md"):
            skill = skills._read_skill(str(path))
            if skill.owner in snapshots:
                snapshots[skill.owner][str(path.relative_to(skills.skills_root))] = path.read_bytes()
        return snapshots

    skill_bytes_before = skill_bytes_by_owner()

    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=components,
        agent_supervisor=agent_memory,
    )
    assert set(preview["expanded_components"]) == closure
    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent_memory,
    )
    assert result["complete"] is True, result
    assert set(result["categories"]) == closure
    assert all(result["categories"][name]["state"] == "complete" for name in closure)
    assert all(call[1] == "alice" for call in provider.calls)
    for name, before in provider_before.items():
        if name in closure:
            assert provider.snapshot[name]["count"] == 0
            assert provider.snapshot[name]["fingerprint"].endswith("-empty")
        else:
            assert provider.snapshot[name] == before

    # The selected closure is gone for Alice; categories outside it retain data.
    if "memories" in closure:
        assert [row["id"] for row in manager.load_all()] == ["json-bob"]
        assert json.loads((tmp_path / "memory_tidy_state.json").read_text(encoding="utf-8")) == {
            "bob": {"fingerprint": "b"}
        }
        with sessions() as session:
            assert session.query(Memory).filter(Memory.owner == "alice").count() == 0
            assert [(row.id, row.text, row.owner) for row in session.query(Memory).filter(Memory.owner == "bob")] == bob_rows
    else:
        assert (tmp_path / "memory.json").read_bytes() == memory_file
        assert (tmp_path / "memory_tidy_state.json").read_bytes() == tidy_file
        with sessions() as session:
            assert [(row.id, row.text, row.owner) for row in session.query(Memory).filter(Memory.owner == "alice")] == alice_rows
            assert [(row.id, row.text, row.owner) for row in session.query(Memory).filter(Memory.owner == "bob")] == bob_rows

    if "skills" in closure:
        assert skills.load(owner="alice") == []
    else:
        assert skills.load(owner="alice") == alice_skill

    if "ingest" in closure:
        assert staging.preview_owner_staging("alice") == empty_staging
    else:
        assert staging.preview_owner_staging("alice") == alice_staging
    assert staging.preview_owner_staging("bob") == bob_staging
    assert skills.load(owner="bob") == bob_skill
    skill_bytes_after = skill_bytes_by_owner()
    if "skills" in closure:
        assert skill_bytes_after["alice"] == {}
    else:
        assert skill_bytes_after["alice"] == skill_bytes_before["alice"]
    assert skill_bytes_after["bob"] == skill_bytes_before["bob"]

    provider_calls = list(provider.calls)
    agent_reset_calls = list(agent_memory.reset_calls)
    state_after_commit = {
        "memory": (tmp_path / "memory.json").read_bytes(),
        "tidy": (tmp_path / "memory_tidy_state.json").read_bytes(),
        "alice_staging": staging.preview_owner_staging("alice"),
        "bob_staging": staging.preview_owner_staging("bob"),
        "alice_skills": skills.load(owner="alice"),
        "bob_skills": skills.load(owner="bob"),
    }
    with sessions() as session:
        sql_rows_after_commit = [
            (row.id, row.text, row.owner)
            for row in session.query(Memory).order_by(Memory.id)
        ]

    # A terminal replay is a no-op and returns the original receipt exactly.
    replay = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=agent_memory,
    )
    assert replay == result
    assert provider.calls == provider_calls
    assert all(call[1] == "alice" for call in provider.calls)
    assert agent_memory.reset_calls == agent_reset_calls
    assert (tmp_path / "memory.json").read_bytes() == state_after_commit["memory"]
    assert (tmp_path / "memory_tidy_state.json").read_bytes() == state_after_commit["tidy"]
    assert staging.preview_owner_staging("alice") == state_after_commit["alice_staging"]
    assert staging.preview_owner_staging("bob") == state_after_commit["bob_staging"]
    assert skills.load(owner="alice") == state_after_commit["alice_skills"]
    assert skills.load(owner="bob") == state_after_commit["bob_skills"]
    assert skill_bytes_by_owner() == skill_bytes_after
    with sessions() as session:
        assert [
            (row.id, row.text, row.owner)
            for row in session.query(Memory).order_by(Memory.id)
        ] == sql_rows_after_commit


@pytest.mark.asyncio
async def test_native_reset_detects_same_id_content_change(tmp_path):
    manager = MemoryManager(str(tmp_path))
    manager.save([
        {"id": "stable-id", "text": "first", "owner": "alice"},
    ])
    provider = NativeMemoryProvider(manager)
    preview = await provider.reset_owner(
        "reset_preview", owner="alice", components=["memories"]
    )
    manager.save([
        {"id": "stable-id", "text": "changed", "owner": "alice"},
    ])

    with pytest.raises(RuntimeError, match="stale"):
        await provider.reset_owner(
            "reset_commit",
            owner="alice",
            components=["memories"],
            expected_counts=preview["components"],
        )
    assert manager.load_all()[0]["text"] == "changed"


@pytest.mark.asyncio
async def test_auth_disabled_reset_clears_local_and_ownerless_compatibility_rows(
    tmp_path
):
    sessions = _sessions(tmp_path)
    manager = MemoryManager(str(tmp_path))
    manager.save([
        {"id": "local", "text": "durable local", "owner": "local"},
        {"id": "none", "text": "legacy none", "owner": None},
        {"id": "blank", "text": "legacy blank", "owner": ""},
        {"id": "bob", "text": "bob survives", "owner": "bob"},
    ])
    (tmp_path / "memory_tidy_state.json").write_text(
        json.dumps({"": {"fingerprint": "local"}, "bob": {"fingerprint": "b"}}),
        encoding="utf-8",
    )
    with sessions() as session:
        session.add_all([
            Memory(id="db-none", text="legacy SQL", owner=None, timestamp=1),
            Memory(id="db-bob", text="bob SQL", owner="bob", timestamp=1),
        ])
        session.commit()
    coordinator = MemoryNukeCoordinator(
        NativeMemoryProvider(manager),
        memory_manager=manager,
        session_factory=sessions,
        data_dir=tmp_path,
    )

    preview = await coordinator.preview(
        owner="",
        provider_owner="local",
        components=["memories"],
        agent_supervisor=_AgentMemory(expected_owner=""),
    )
    # provider 4 + agent 1
    assert preview["components"]["memories"]["count"] == 6
    result = await coordinator.commit(
        owner="",
        provider_owner="local",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
        agent_supervisor=_AgentMemory(expected_owner=""),
    )

    assert result["complete"] is True, result
    assert [row["id"] for row in manager.load_all()] == ["bob"]
    tidy = json.loads((tmp_path / "memory_tidy_state.json").read_text(encoding="utf-8"))
    assert tidy == {"bob": {"fingerprint": "b"}}
    with sessions() as session:
        assert [row.id for row in session.query(Memory).all()] == ["db-bob"]


def test_skill_owner_purge_filters_every_owner_sidecar(tmp_path):
    skills = SkillsManager(str(tmp_path))
    alice = skills.add_skill(
        name="alice-skill",
        description="alice only",
        when_to_use="test",
        procedure=["one"],
        owner="alice",
        source="user",
    )
    bob = skills.add_skill(
        name="bob-skill",
        description="bob only",
        when_to_use="test",
        procedure=["one"],
        owner="bob",
        source="user",
    )
    (tmp_path / "skills" / "_usage.json").write_text(json.dumps({
        f"alice::{alice['skill_id']}": {"owner": "alice", "uses": 2},
        f"bob::{bob['skill_id']}": {"owner": "bob", "uses": 3},
    }), encoding="utf-8")
    (tmp_path / "skills" / "_usage_events.jsonl").write_text(
        json.dumps({"owner": "alice", "skill_id": alice["skill_id"]}) + "\n"
        + json.dumps({"owner": "bob", "skill_id": bob["skill_id"]}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "_promotions.json").write_text(json.dumps([
        {"owner": "alice", "promotion_id": "pa"},
        {"owner": "bob", "promotion_id": "pb"},
    ]), encoding="utf-8")
    (tmp_path / "skills.json").write_text(json.dumps([
        {"name": "alice-legacy", "owner": "alice"},
        {"name": "bob-legacy", "owner": "bob"},
    ]), encoding="utf-8")
    (tmp_path / "skill-audit-jobs.json").write_text(json.dumps({
        "test": [
            {"key": ["alice", "alice-skill"], "job": {"status": "done"}},
            {"key": ["bob", "bob-skill"], "job": {"status": "done"}},
        ],
        "audit": [],
    }), encoding="utf-8")
    for owner in ("alice", "bob"):
        recovery = tmp_path / ".memory-forget-skills" / f"forget-{owner}"
        recovery.mkdir(parents=True)
        (recovery / "manifest.json").write_text(
            json.dumps({"owner": owner, "state": "prepared"}),
            encoding="utf-8",
        )

    preview = skills.preview_owner_purge("alice")
    assert preview["count"] == 2
    result = skills.purge_owner("alice", expected=preview)

    assert result["complete"] is True
    assert skills.load(owner="alice") == []
    assert "bob-skill" in [row["name"] for row in skills.load(owner="bob")]
    legacy = json.loads((tmp_path / "skills.json").read_text(encoding="utf-8"))
    assert legacy == [{"name": "bob-legacy", "owner": "bob"}]
    usage = json.loads((tmp_path / "skills" / "_usage.json").read_text(encoding="utf-8"))
    assert set(usage) == {f"bob::{bob['skill_id']}"}
    events = (tmp_path / "skills" / "_usage_events.jsonl").read_text(encoding="utf-8")
    assert '"owner": "alice"' not in events
    assert '"owner": "bob"' in events
    jobs = json.loads((tmp_path / "skill-audit-jobs.json").read_text(encoding="utf-8"))
    assert jobs["test"] == [
        {"key": ["bob", "bob-skill"], "job": {"status": "done"}}
    ]
    assert not (tmp_path / ".memory-forget-skills" / "forget-alice").exists()
    assert (tmp_path / ".memory-forget-skills" / "forget-bob").exists()


@pytest.mark.asyncio
async def test_skill_runtime_sync_failure_preserves_successful_purge_count(tmp_path):
    class SkillStore:
        def preview_owner_purge(self, owner):
            assert owner == "alice"
            return {"count": 3, "fingerprint": "skills-v1"}

        def purge_owner(self, owner, *, expected):
            assert owner == "alice"
            assert expected == {"count": 3, "fingerprint": "skills-v1"}
            return {"complete": True, "count": 3}

    def sync(_owner):
        raise RuntimeError("runtime map unavailable")

    coordinator = MemoryNukeCoordinator(
        _Provider(),
        skills_manager=SkillStore(),
        skill_job_runtime_sync=sync,
        data_dir=tmp_path,
    )
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["skills"],
    )
    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
    )

    assert result["complete"] is False
    assert result["categories"]["skills"]["state"] == "failed"
    assert result["categories"]["skills"]["count"] == 3


@pytest.mark.asyncio
async def test_nuke_route_accepts_the_ui_commit_shape(monkeypatch, tmp_path):
    sessions = _sessions(tmp_path)
    manager = _seed_compatibility_memory(tmp_path, sessions)
    monkeypatch.setattr("core.database.SessionLocal", sessions)
    monkeypatch.setattr(memory_routes, "get_current_user", lambda _request: "alice")
    monkeypatch.setattr(
        "src.auth_helpers.require_privilege",
        lambda _request, _privilege: "alice",
    )
    provider = _Provider()
    router = memory_routes.setup_memory_routes(
        manager,
        SimpleNamespace(),
        memory_provider=provider,
        skills_manager=SkillsManager(str(tmp_path)),
    )
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/memory/nuke"
    )

    def request(body, *, authorization="", api_token=False, extra_headers=None):
        async def json_body():
            return body

        headers = dict(extra_headers or {})
        if authorization:
            headers["authorization"] = authorization
        return SimpleNamespace(
            json=json_body,
            headers=headers,
            state=SimpleNamespace(current_user="alice", api_token=api_token),
            app=SimpleNamespace(
                state=SimpleNamespace(auth_manager=None, mimo_supervisor=None)
            ),
        )

    preview = await endpoint(request({"action": "preview", "components": ["graph"]}))
    result = await endpoint(request({
        "action": "commit",
        "operation_id": preview["operation_id"],
        "preview_token": preview["preview_token"],
        "confirmation": preview["confirmation"],
    }))
    assert result["complete"] is True

    with pytest.raises(HTTPException) as exc:
        await endpoint(request(
            {"action": "preview", "components": ["graph"]},
            authorization="Bearer tool-token",
        ))
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await endpoint(request(
            {"action": "preview", "components": ["graph"]},
            api_token=True,
        ))
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        await endpoint(request({
            "action": "preview",
            "components": ["graph"],
            "owner": "bob",
        }))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_memories_preview_fails_closed_without_agent_supervisor(tmp_path):
    """S03: with no agent runtime, a memories reset would silently skip
    authored memory while reporting complete; preview must fail closed."""
    from services.memory.nuke_coordinator import MemoryNukeUnavailable

    coordinator = MemoryNukeCoordinator(_Provider(), data_dir=tmp_path)
    with pytest.raises(MemoryNukeUnavailable, match="agent runtime"):
        await coordinator.preview(
            owner="alice",
            provider_owner="alice",
            components=["memories"],
            agent_supervisor=None,
        )


@pytest.mark.asyncio
async def test_provider_preview_refusal_surfaces_actionable_reason(tmp_path):
    """S03: the provider's bounded refusal (e.g. immutable ingest job-event
    history) must reach the caller instead of a generic 503 body."""
    from services.memory.nuke_coordinator import MemoryNukeUnavailable

    class _RefusingProvider(_Provider):
        async def reset_owner(self, action, *, owner=None, components, expected_counts=None):
            if action == "reset_preview":
                raise RuntimeError(
                    "owner reset cannot safely erase 3 immutable memory-ingest job event(s)"
                )
            return await super().reset_owner(
                action, owner=owner, components=components, expected_counts=expected_counts
            )

    coordinator = MemoryNukeCoordinator(_RefusingProvider(), data_dir=tmp_path)
    with pytest.raises(MemoryNukeUnavailable, match="immutable memory-ingest job event"):
        await coordinator.preview(
            owner="alice",
            provider_owner="alice",
            components=["graph"],
            agent_supervisor=_AgentMemory(),
        )


class _PartialProvider:
    """Provider stub with Rust-like CAS semantics that fails graph once."""

    def __init__(self):
        self.snapshot = {
            "graph": {"count": 1, "fingerprint": "graph-v1"},
            "ingest": {"count": 2, "fingerprint": "ingest-v1"},
        }
        self.commits = 0

    async def reset_owner(self, action, *, owner=None, components, expected_counts=None):
        expanded = list(components)
        if action == "reset_preview":
            return {
                "components": dict(self.snapshot),
                "expanded_components": expanded,
            }
        assert action == "reset_commit"
        self.commits += 1
        categories = {}
        for component in expanded:
            current = self.snapshot[component]
            if current["count"] != 0 and expected_counts.get(component) != current:
                raise RuntimeError(f"owner reset preview is stale for {component}")
            if component == "graph" and self.commits == 1:
                categories[component] = {
                    "state": "failed",
                    "count": 0,
                    "error": "graph reset boom",
                }
                continue
            categories[component] = {"state": "complete", **current}
            self.snapshot[component] = {
                "count": 0,
                "fingerprint": f"{component}-empty",
            }
        return {"complete": True, "categories": categories}


@pytest.mark.asyncio
async def test_partial_commit_stays_retryable_and_is_not_replayed(tmp_path):
    """S03: a partial commit must not be journaled as terminal; the same
    binding retries in truth once the failed category recovers."""
    provider = _PartialProvider()
    now = [1000.0]
    coordinator = MemoryNukeCoordinator(
        provider, data_dir=tmp_path, preview_ttl_seconds=60, clock=lambda: now[0]
    )
    preview = await coordinator.preview(
        owner="alice",
        provider_owner="alice",
        components=["graph", "ingest"],
    )

    first = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
    )
    assert first["status"] == "partial"
    assert first["complete"] is False
    assert first["categories"]["graph"]["state"] == "failed"
    assert first["categories"]["ingest"]["state"] == "complete"
    # The completed category was really cleared during the partial attempt.
    assert provider.snapshot["ingest"]["count"] == 0

    # The journal row is not terminal: the partial result is observability,
    # not a replayable verdict.
    journal = json.loads(
        (tmp_path / ".memory-nuke" / "operations.json").read_text(encoding="utf-8")
    )
    row = journal["operations"][preview["operation_id"]]
    assert row["result"] is None
    assert row["state"] == "partial"
    assert row["last_attempt"]["categories"]["graph"]["error"] == "graph reset boom"

    # A started partial remains resumable after both its preview TTL and a
    # process restart; the TTL gates only first entry into destruction.
    now[0] += 5000.0
    coordinator = MemoryNukeCoordinator(
        provider, data_dir=tmp_path, preview_ttl_seconds=60, clock=lambda: now[0]
    )
    retry = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
    )
    assert provider.commits == 2
    assert retry["complete"] is True, retry
    assert retry["categories"]["graph"]["state"] == "complete"
    assert retry["categories"]["graph"]["count"] == 1
    assert retry["categories"]["ingest"]["count"] == 2
    assert retry["receipt"]["attempt"] == 2

    # Once complete, the terminal result replays without re-running.
    replay = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=preview["operation_id"],
        preview_token=preview["preview_token"],
        confirmation=preview["confirmation"],
    )
    assert provider.commits == 2
    assert replay == retry


@pytest.mark.asyncio
async def test_ttl_gates_commit_start_but_not_finalization(tmp_path):
    """S03: the preview TTL is enforced when a commit starts; a commit whose
    destructive section outlives the TTL must still finalize honestly."""
    now = [1000.0]

    class _SlowProvider(_Provider):
        async def reset_owner(self, action, *, owner=None, components, expected_counts=None):
            result = await super().reset_owner(
                action, owner=owner, components=components, expected_counts=expected_counts
            )
            if action == "reset_commit":
                now[0] += 5000.0
            return result

    provider = _SlowProvider()
    coordinator = MemoryNukeCoordinator(
        provider, data_dir=tmp_path, preview_ttl_seconds=60, clock=lambda: now[0]
    )

    expired = await coordinator.preview(
        owner="alice", provider_owner="alice", components=["graph"]
    )
    now[0] += 5000.0
    with pytest.raises(MemoryNukeConflict, match="expired"):
        await coordinator.commit(
            owner="alice",
            provider_owner="alice",
            operation_id=expired["operation_id"],
            preview_token=expired["preview_token"],
            confirmation=expired["confirmation"],
        )

    fresh = await coordinator.preview(
        owner="alice", provider_owner="alice", components=["graph"]
    )
    # The commit starts inside the TTL; the provider's destructive section
    # then runs the clock far past it.
    result = await coordinator.commit(
        owner="alice",
        provider_owner="alice",
        operation_id=fresh["operation_id"],
        preview_token=fresh["preview_token"],
        confirmation=fresh["confirmation"],
    )
    assert result["complete"] is True, result

    journal = json.loads(
        (tmp_path / ".memory-nuke" / "operations.json").read_text(encoding="utf-8")
    )
    row = journal["operations"][fresh["operation_id"]]
    assert row["state"] == "complete"
    assert row["result"]["complete"] is True


@pytest.mark.asyncio
async def test_expired_non_terminal_journal_rows_are_pruned(tmp_path):
    """S03: abandoned previews must not accumulate in the journal."""
    now = [1000.0]
    coordinator = MemoryNukeCoordinator(
        _Provider(), data_dir=tmp_path, preview_ttl_seconds=60, clock=lambda: now[0]
    )
    abandoned = await coordinator.preview(
        owner="alice", provider_owner="alice", components=["graph"]
    )
    now[0] += 5000.0
    fresh = await coordinator.preview(
        owner="alice", provider_owner="alice", components=["graph"]
    )

    journal = json.loads(
        (tmp_path / ".memory-nuke" / "operations.json").read_text(encoding="utf-8")
    )
    assert set(journal["operations"]) == {fresh["operation_id"]}
    assert abandoned["operation_id"] not in journal["operations"]


def test_journal_bounds_terminal_history_but_keeps_started_partial(tmp_path):
    now = [5000.0]
    coordinator = MemoryNukeCoordinator(
        _Provider(), data_dir=tmp_path, clock=lambda: now[0]
    )
    operations = {
        f"done-{index}": {
            "state": "complete",
            "updated_at": float(index),
            "expires_at_epoch": 1.0,
            "result": {"complete": True},
        }
        for index in range(300)
    }
    operations["partial"] = {
        "state": "partial",
        "updated_at": 1.0,
        "expires_at_epoch": 1.0,
        "result": None,
    }
    operations["expired-preview"] = {
        "state": "preview",
        "updated_at": 1.0,
        "expires_at_epoch": 1.0,
        "result": None,
    }

    coordinator._save_journal(operations)
    kept = coordinator._load_journal()

    assert "partial" in kept
    assert "expired-preview" not in kept
    terminals = [row for row in kept.values() if row.get("result") is not None]
    assert len(terminals) == 256
