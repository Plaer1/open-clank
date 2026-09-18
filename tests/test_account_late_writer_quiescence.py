from __future__ import annotations

import asyncio

import pytest

from routes import chat_helpers
from services.memory.forget_coordinator import MemoryLifecycleCoordinator
from src import agent_runs
from src import memory_maintenance


@pytest.mark.asyncio
async def test_owner_quiesce_joins_replaced_agent_cleanup_and_preserves_other_owner():
    await agent_runs.shutdown()
    first_started = asyncio.Event()
    first_cleanup_started = asyncio.Event()
    release_first_cleanup = asyncio.Event()
    bob_started = asyncio.Event()
    release_bob = asyncio.Event()

    async def first_events():
        try:
            first_started.set()
            yield "data: first\n\n"
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cleanup_started.set()
            # This is ordinary one-shot cancellation cleanup.  A lifecycle
            # join must not re-cancel and interrupt the awaited persistence.
            await release_first_cleanup.wait()
            raise

    async def replacement_events():
        yield "data: replacement\n\n"

    async def bob_events():
        bob_started.set()
        yield "data: bob\n\n"
        await release_bob.wait()

    first = agent_runs.start("alice-replaced", first_events(), owner="Alice")
    await first_started.wait()
    replacement = agent_runs.start(
        "alice-replaced",
        replacement_events(),
        owner="alice",
    )
    await first_cleanup_started.wait()
    bob = agent_runs.start("bob-active", bob_events(), owner="bob")
    await bob_started.wait()

    draining = asyncio.create_task(agent_runs.quiesce_owner("ALICE"))
    await asyncio.sleep(0)
    assert not draining.done()
    assert not bob.task.done()

    release_first_cleanup.set()
    assert await draining == {"owner": "alice", "drained": 2}
    assert first.task.done()
    assert replacement.task.done()
    assert not {
        task
        for task, owner in agent_runs._DRAIN_TASK_OWNERS.items()
        if owner == "alice" and not task.done()
    }
    assert not bob.task.done()

    release_bob.set()
    await bob.task
    await agent_runs.shutdown()


@pytest.mark.asyncio
async def test_chat_background_drain_waits_for_target_only():
    alice_started = asyncio.Event()
    bob_started = asyncio.Event()
    release_alice = asyncio.Event()
    release_bob = asyncio.Event()

    async def writer(started: asyncio.Event, release: asyncio.Event):
        started.set()
        await release.wait()

    alice = chat_helpers._spawn_bg(
        writer(alice_started, release_alice),
        owner="Alice",
    )
    bob = chat_helpers._spawn_bg(
        writer(bob_started, release_bob),
        owner="bob",
    )
    await asyncio.gather(alice_started.wait(), bob_started.wait())

    draining = asyncio.create_task(
        chat_helpers.drain_owner_background_tasks("alice")
    )
    await asyncio.sleep(0)
    assert not draining.done()
    assert not bob.done()

    release_alice.set()
    assert await draining == {"owner": "alice", "drained": 1}
    assert alice.done()
    assert not bob.done()

    release_bob.set()
    await bob


@pytest.mark.asyncio
async def test_cancelled_chat_drain_waiter_does_not_cancel_writer_cleanup():
    started = asyncio.Event()
    release = asyncio.Event()

    async def writer():
        started.set()
        await release.wait()

    task = chat_helpers._spawn_bg(writer(), owner="alice")
    await started.wait()

    first = asyncio.create_task(chat_helpers.drain_owner_background_tasks("alice"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(chat_helpers.drain_owner_background_tasks("alice"))
    await asyncio.sleep(0)
    assert not second.done()
    assert not task.done()
    release.set()
    assert await second == {"owner": "alice", "drained": 1}
    assert task.done()


@pytest.mark.asyncio
async def test_memory_reconcile_drain_is_owner_filtered(monkeypatch):
    monkeypatch.setenv("OPEN_CLANK_MEMORY_RECONCILE_DELAY_SECONDS", "0")
    started = {"alice": asyncio.Event(), "bob": asyncio.Event()}
    release = {"alice": asyncio.Event(), "bob": asyncio.Event()}
    calls: list[tuple[str, bool]] = []

    class SkillForget:
        async def reconcile(self, _provider, *, owner: str, filter_owner: bool):
            calls.append((owner, filter_owner))
            started[owner].set()
            await release[owner].wait()
            return {"rolled_back": 0, "committed": 0, "restored": 0, "errors": 0}

    lifecycle = MemoryLifecycleCoordinator(object(), skill_forget=SkillForget())
    lifecycle._schedule_reconcile("alice")
    lifecycle._schedule_reconcile("bob")
    await asyncio.gather(started["alice"].wait(), started["bob"].wait())

    draining = asyncio.create_task(lifecycle.drain_owner_reconcile_tasks("ALICE"))
    await asyncio.sleep(0)
    assert not draining.done()
    bob_tasks = {
        task
        for task, owner in lifecycle._reconcile_task_owners.items()
        if owner == "bob"
    }
    assert bob_tasks and not all(task.done() for task in bob_tasks)

    release["alice"].set()
    assert await draining == {"owner": "alice", "drained": 1}
    assert ("alice", True) in calls
    assert not all(task.done() for task in bob_tasks)

    release["bob"].set()
    await asyncio.gather(*bob_tasks)


@pytest.mark.asyncio
async def test_groom_drain_waits_for_active_owner_and_fence_stops_next_write():
    alice_started = asyncio.Event()
    release_alice = asyncio.Event()
    fenced: set[str] = set()
    calls: list[tuple[str, str]] = []

    class Provider:
        async def groom(self, op: str, *, owner: str, workspace_id: str):
            del workspace_id
            calls.append((owner, op))
            if owner == "alice" and op == "decay":
                alice_started.set()
                await release_alice.wait()
            return {"op": op}

    def owner_is_fenced(owner: str) -> bool:
        return owner.casefold() in fenced

    alice = asyncio.create_task(
        memory_maintenance.groom_once(
            Provider(),
            owner="alice",
            workspace_id="global",
            owner_is_fenced=owner_is_fenced,
        )
    )
    bob = asyncio.create_task(
        memory_maintenance.groom_once(
            Provider(),
            owner="bob",
            workspace_id="global",
            owner_is_fenced=owner_is_fenced,
        )
    )
    await alice_started.wait()
    fenced.add("alice")

    draining = asyncio.create_task(memory_maintenance.drain_owner_grooms("ALICE"))
    await asyncio.sleep(0)
    assert not draining.done()

    release_alice.set()
    assert await draining == {"owner": "alice", "drained": 1}
    await asyncio.gather(alice, bob)
    assert [op for owner, op in calls if owner == "alice"] == ["decay"]
    assert [op for owner, op in calls if owner == "bob"] == list(
        memory_maintenance.GROOM_OPS
    )


@pytest.mark.asyncio
async def test_app_composite_quiesces_in_write_dependency_order(monkeypatch):
    import app as app_module
    from routes import skills_routes

    events: list[tuple[str, str]] = []

    async def agent(owner):
        events.append(("agent", owner))
        return {"drained": 1}

    async def chat(owner):
        events.append(("chat", owner))
        return {"drained": 1}

    async def groom(owner):
        events.append(("groom", owner))
        return {"drained": 1}

    async def skills(owner):
        events.append(("skills", owner))
        return {"drained": 1}

    class Scheduler:
        async def quiesce_owner_lifecycle(self, owner):
            events.append(("scheduler", owner))
            return {"drained": 1}

    class EmailRouter:
        async def drain_owner_writers(self, owner):
            events.append(("email", owner))
            return {"drained": 1}

    class Research:
        async def quiesce_owner(self, owner):
            events.append(("research", owner))
            return {"drained": 1}

    class Lifecycle:
        async def drain_owner_reconcile_tasks(self, owner):
            events.append(("reconcile", owner))
            return {"drained": 1}

    monkeypatch.setattr(agent_runs, "quiesce_owner", agent)
    monkeypatch.setattr(chat_helpers, "drain_owner_background_tasks", chat)
    monkeypatch.setattr(memory_maintenance, "drain_owner_grooms", groom)
    monkeypatch.setattr(skills_routes, "quiesce_owner_skill_job_handles", skills)
    monkeypatch.setattr(app_module, "memory_lifecycle", Lifecycle())
    monkeypatch.setattr(app_module, "task_scheduler", Scheduler())
    monkeypatch.setattr(app_module, "email_router", EmailRouter())
    monkeypatch.setattr(app_module, "research_handler", Research())

    result = await app_module.app.state.quiesce_account_owner_writers("alice")

    assert events == [
        ("agent", "alice"),
        ("chat", "alice"),
        ("scheduler", "alice"),
        ("skills", "alice"),
        ("email", "alice"),
        ("reconcile", "alice"),
        ("groom", "alice"),
        ("research", "alice"),
    ]
    assert set(result) == {
        "agent_runs",
        "chat_background",
        "task_scheduler",
        "skill_jobs",
        "email_writers",
        "memory_reconcile",
        "memory_groom",
        "research",
    }


def test_auth_settings_write_is_not_bootstrap_exempt():
    import app as app_module

    assert app_module._is_auth_exempt("/api/auth/settings", "GET") is True
    assert app_module._is_auth_exempt("/api/auth/settings", "HEAD") is True
    assert app_module._is_auth_exempt("/api/auth/settings", "POST") is False


def test_durable_account_lifecycle_is_not_cancelled_by_global_timeout():
    import app as app_module

    assert app_module._is_timeout_exempt("/api/auth/users", "DELETE") is True
    assert app_module._is_timeout_exempt(
        "/api/auth/users/alice/rename", "PUT"
    ) is True
    assert app_module._is_timeout_exempt(
        "/api/auth/account-operations/op-1/resume", "POST"
    ) is True
    assert app_module._is_timeout_exempt("/api/auth/users/alice/admin", "PUT") is False
