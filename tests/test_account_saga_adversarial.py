"""Fault/restart contracts for the durable account route saga."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.operation_models import AccountLifecycleOperation, OperationBase
from core.provider_models import ProviderBase
from routes.auth_routes import DeleteUserRequest, RenameUserRequest, setup_auth_routes
from src import secret_storage
from src.openclank.account_lifecycle import AccountOwnerLifecycle
from src.openclank.artifacts import ArtifactStore
from src.openclank.operation_journal import OperationJournalStore
from src.openclank.provider_store import ProviderStore
from src.preset_manager import PresetManager


def _fingerprint(owner: str, values: set[str]) -> str:
    material = "\n".join(sorted(values))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class _OwnerDomain:
    def __init__(self, *owners: str):
        self.values = {owner: {f"item:{owner}"} for owner in owners}

    def owner_inventory(self, owner: str):
        key = str(owner).lower()
        values = set(self.values.get(key, set()))
        return {
            "count": len(values),
            "fingerprint": _fingerprint(key, values),
        }

    def preview_owner_rename(self, source: str, target: str):
        source_inventory = self.owner_inventory(source)
        target_inventory = self.owner_inventory(target)
        if source_inventory["count"] and target_inventory["count"]:
            raise RuntimeError("target contains state")
        return {
            "schema_version": 1,
            "source": source_inventory,
            "target": target_inventory,
        }

    preview_owner_purge = owner_inventory

    def _move(self, source: str, target: str):
        source = source.lower()
        target = target.lower()
        source_values = set(self.values.get(source, set()))
        target_values = set(self.values.get(target, set()))
        if source_values and target_values:
            raise RuntimeError("split owner state")
        if source_values:
            self.values[target] = source_values
            self.values[source] = set()
        return {
            "state": "staged",
            "source": self.owner_inventory(source),
            "target": self.owner_inventory(target),
        }

    def reconcile_owner_rename(self, source: str, target: str, _manifest):
        return self._move(source, target)

    def compensate_owner_rename(self, source: str, target: str, _manifest):
        if self.owner_inventory(source)["count"]:
            return {"state": "restored"}
        return self._move(target, source)

    def purge_owner(self, owner: str, **_kwargs):
        before = self.owner_inventory(owner)
        self.values[owner.lower()] = set()
        return {"state": "purged", "before": before, "after": self.owner_inventory(owner)}

    def purge_owner_lifecycle(self, owner: str, **kwargs):
        return self.purge_owner(owner, **kwargs)


@dataclass
class _Receipt:
    payload: dict

    def as_dict(self):
        return dict(self.payload)


class _SqlDomain(_OwnerDomain):
    def __init__(self, *owners: str):
        super().__init__(*owners)
        self.fail_reconcile = True

    def reconcile_owner(self, source, target, *, expected_source=None):
        if self.fail_reconcile:
            raise RuntimeError("injected late SQL staging failure")
        return _Receipt(self._move(source, target))

    def compensate_owner(self, source, target, *, expected_source=None):
        return _Receipt(self.compensate_owner_rename(source, target, expected_source))

    def purge_owner(self, owner, *, expected_inventory=None):
        return _Receipt(super().purge_owner(owner, expected=expected_inventory))

    def verify_staged(self, source, target, *, expected_source=None):
        assert self.owner_inventory(source)["count"] == 0
        assert self.owner_inventory(target)["count"] == expected_source["count"]


class _FileDomain(_OwnerDomain):
    preview_rename = _OwnerDomain.preview_owner_rename

    def stage_to_tombstone(self, source, target, manifest):
        return self.reconcile_owner_rename(source, target, manifest)

    def compensate(self, source, target, manifest):
        return self.compensate_owner_rename(source, target, manifest)

    def verify(self, source, target, manifest, *, expected):
        assert expected == "staged"
        assert self.owner_inventory(source)["count"] == 0


class _AncillaryDomain(_FileDomain):
    def __init__(self, *owners):
        super().__init__(*owners)
        self.fail_compensate_once = False
        self.stage_calls = 0

    def stage_owner_to_tombstone(
        self, source, target, manifest, *, operation_token
    ):
        assert len(operation_token) == 32
        self.stage_calls += 1
        return self.reconcile_owner_rename(source, target, manifest)

    def compensate(self, source, target, manifest, *, operation_token):
        assert len(operation_token) == 32
        if self.fail_compensate_once:
            self.fail_compensate_once = False
            raise RuntimeError("injected ancillary compensation failure")
        return self.compensate_owner_rename(source, target, manifest)


class _UploadDomain(_OwnerDomain):
    stage_owner_to_tombstone = _OwnerDomain.reconcile_owner_rename
    compensate_owner_rename = _OwnerDomain.compensate_owner_rename

    def purge_owner_lifecycle(self, owner, **kwargs):
        return self.purge_owner(owner, **kwargs)


class _PersonalDomain(_OwnerDomain):
    def stage_to_tombstone(self, source, target, manifest):
        return self.reconcile_owner_rename(source, target, manifest)

    def compensate(self, source, target, manifest):
        return self.compensate_owner_rename(source, target, manifest)


class _ResearchDomain(_OwnerDomain):
    def __init__(self, *owners):
        super().__init__(*owners)
        self.fences = set()

    def fence_owner(self, owner):
        self.fences.add(owner.lower())
        return {"state": "fenced"}

    def release_owner_fence(self, owner):
        self.fences.discard(owner.lower())

    def compensate_owner_rename(self, source, target):
        receipt = super().compensate_owner_rename(source, target, {})
        self.release_owner_fence(source)
        return receipt


class _MimoDomain(_OwnerDomain):
    async def preview_owner_rename(self, source, target):
        return super().preview_owner_rename(source, target)

    async def reconcile_owner_rename(self, source, target, manifest):
        return super().reconcile_owner_rename(source, target, manifest)

    async def compensate_owner_rename(self, source, target, manifest):
        return super().compensate_owner_rename(source, target, manifest)

    async def purge_owner_lifecycle(self, owner, **kwargs):
        return self.purge_owner(owner, **kwargs)

    def owner_lifecycle_inventory(self, owner):
        return self.owner_inventory(owner)


class _MemoryDomain(_OwnerDomain):
    async def owner_stats(self, *, owner):
        return self.owner_inventory(owner)

    async def rename_owner(self, new_owner, *, owner):
        return self._move(owner, new_owner)

    async def purge_owner(self, *, owner):
        return super().purge_owner(owner)


class _MediaDomain(_OwnerDomain):
    preview_owner_purge = _OwnerDomain.owner_inventory

    def rename_owner(self, source, target):
        return self._move(source, target)


class _StagingDomain(_OwnerDomain):
    preview_owner_staging = _OwnerDomain.owner_inventory

    def rename_owner_staging(
        self,
        source,
        target,
        *,
        expected_source=None,
        expected_target=None,
    ):
        return self._move(source, target)

    def purge_owner_staging(self, owner, **kwargs):
        return self.purge_owner(owner, **kwargs)


class _Auth:
    def __init__(self):
        self.users = {"admin": {"is_admin": True}, "alice": {}}
        self.ids = {"admin": "admin-id", "alice": "alice-id"}
        self.delete_calls = 0
        self.barrier_failure = None

    def get_username_for_token(self, _token):
        return "admin"

    def is_admin(self, username):
        return username == "admin"

    def account_id(self, username):
        return self.ids.get(username)

    def username_for_account_id(self, subject):
        return next((name for name, value in self.ids.items() if value == subject), None)

    def delete_user_auth_barrier(self, username, _actor):
        self.delete_calls += 1
        if self.barrier_failure == "before":
            raise RuntimeError("injected auth barrier failure")
        self.users.pop(username, None)
        self.ids.pop(username, None)
        if self.barrier_failure == "after":
            raise RuntimeError("injected crash after auth effect")
        if self.barrier_failure == "cancel":
            raise asyncio.CancelledError("injected cancellation after auth effect")
        return True


def _coordinator(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(engine)
    OperationBase.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    presets_root = tmp_path / "presets"
    presets_root.mkdir()
    coordinator = AccountOwnerLifecycle(
        provider_store=ProviderStore(factory, clock=lambda: datetime(2026, 8, 31)),
        operation_journal=OperationJournalStore(
            factory, clock=lambda: datetime(2026, 8, 31)
        ),
        artifact_store=ArtifactStore(
            tmp_path / "artifacts",
            session_factory=factory,
            clock=lambda: datetime(2026, 8, 31),
        ),
        preset_manager=PresetManager(str(presets_root)),
        operation_path=tmp_path / "operations.json",
    )
    return coordinator, factory, engine


def _delete_endpoint(router):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/auth/users" and "DELETE" in route.methods
    )


@pytest.mark.parametrize(
    "failure",
    ["late_sql", "compensation_failure", "auth_barrier", "auth_effect_crash", "auth_cancel"],
)
def test_pre_auth_failure_or_crash_resumes_without_residue(
    tmp_path,
    monkeypatch,
    failure,
):
    import routes.auth_routes as auth_routes
    import src.openclank.file_policy as file_policy
    import src.openclank.filesystem_registry as filesystem_registry

    coordinator, factory, engine = _coordinator(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_routes, "_sync_account_skill_runtime", lambda *owners: {})
    from src.task_scheduler import TaskScheduler
    scheduler = TaskScheduler(session_manager=None)
    scheduler._pending_notifications = [
        {"owner": "alice", "kind": "task", "task_id": "alice-task"},
        {"owner": "bob", "kind": "task", "task_id": "bob-task"},
    ]
    bob_runtime = scheduler.owner_runtime_inventory("bob")
    domains = {
        "sql": _SqlDomain("alice", "bob"),
        "file": _FileDomain("alice", "bob"),
        "skills": _OwnerDomain("alice", "bob"),
        "shell_audit": _OwnerDomain("alice", "bob"),
        "ancillary": _AncillaryDomain("alice", "bob"),
        "upload": _UploadDomain("alice", "bob"),
        "personal": _PersonalDomain("alice", "bob"),
        "research": _ResearchDomain("alice", "bob"),
        "mimo": _MimoDomain("alice", "bob"),
        "memory": _MemoryDomain("alice", "bob"),
        "media": _MediaDomain("alice", "bob"),
        "staging": _StagingDomain("alice", "bob"),
        "filesystem": _OwnerDomain("alice", "bob"),
    }
    monkeypatch.setattr(auth_routes, "_account_sql_domain_store", lambda: domains["sql"])
    monkeypatch.setattr(auth_routes, "_account_file_domain_store", lambda: domains["file"])
    monkeypatch.setattr(
        auth_routes,
        "_account_skills_domain_store",
        lambda _request: domains["skills"],
    )
    monkeypatch.setattr(
        auth_routes,
        "_account_shell_audit_store",
        lambda: domains["shell_audit"],
    )
    monkeypatch.setattr(
        auth_routes, "_account_ancillary_domain_store", lambda: domains["ancillary"]
    )
    monkeypatch.setattr(
        auth_routes, "_account_personal_rag_store", lambda _request: domains["personal"]
    )
    monkeypatch.setattr(
        auth_routes,
        "_account_memory_domain_stores",
        lambda _provider: (domains["media"], domains["staging"]),
    )
    monkeypatch.setattr(auth_routes, "_invalidate_account_runtime", lambda *_a, **_k: {})

    async def no_provider_flows(_request, _owner):
        return None

    monkeypatch.setattr(auth_routes, "_drain_owner_provider_flows", no_provider_flows)
    monkeypatch.setattr(auth_routes, "_rename_user_prefs", lambda *_a: None)
    monkeypatch.setattr(auth_routes, "_detach_user_prefs", lambda *_a: None)
    monkeypatch.setattr(
        filesystem_registry,
        "FilesystemRootRegistry",
        lambda: domains["filesystem"],
    )
    monkeypatch.setattr(
        file_policy,
        "FilePolicyRepository",
        lambda: SimpleNamespace(purge_subject=lambda *_a, **_k: {"count": 0}),
    )

    auth = _Auth()
    domains["sql"].fail_reconcile = failure in {"late_sql", "compensation_failure"}
    domains["ancillary"].fail_compensate_once = failure == "compensation_failure"
    if failure == "auth_barrier":
        auth.barrier_failure = "before"
    elif failure == "auth_effect_crash":
        auth.barrier_failure = "after"
    elif failure == "auth_cancel":
        auth.barrier_failure = "cancel"
    router = setup_auth_routes(auth, account_lifecycle=coordinator)
    endpoint = _delete_endpoint(router)
    request = SimpleNamespace(
        cookies={"session": "admin"},
        app=SimpleNamespace(
            state=SimpleNamespace(
                memory_provider=domains["memory"],
                upload_handler=domains["upload"],
                research_handler=domains["research"],
                task_scheduler=scheduler,
                mimo_supervisor=domains["mimo"],
                invalidate_token_cache=lambda: None,
                drain_account_owner_requests=lambda _owner: asyncio.sleep(0),
                quiesce_account_owner_writers=lambda _owner: asyncio.sleep(0),
            )
        ),
    )
    bob_before = {
        name: domain.owner_inventory("bob")
        for name, domain in domains.items()
        if hasattr(domain, "owner_inventory")
    }

    if failure == "late_sql":
        with pytest.raises(HTTPException, match="SQL-owned") as failed:
            asyncio.run(endpoint(DeleteUserRequest(username="alice"), request))
        assert failed.value.status_code == 503
        assert auth.delete_calls == 0
    elif failure == "compensation_failure":
        with pytest.raises(RuntimeError, match="compensation did not restore"):
            asyncio.run(endpoint(DeleteUserRequest(username="alice"), request))
        assert auth.delete_calls == 0
    elif failure == "auth_barrier":
        with pytest.raises(RuntimeError, match="injected auth barrier failure"):
            asyncio.run(endpoint(DeleteUserRequest(username="alice"), request))
        assert auth.delete_calls == 1
    elif failure == "auth_cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(endpoint(DeleteUserRequest(username="alice"), request))
        assert auth.delete_calls == 1
    else:
        with pytest.raises(RuntimeError, match="injected crash after auth effect"):
            asyncio.run(endpoint(DeleteUserRequest(username="alice"), request))
        assert auth.delete_calls == 1

    if failure not in {"auth_effect_crash", "auth_cancel", "compensation_failure"}:
        for name, domain in domains.items():
            inventory = getattr(domain, "owner_inventory", None)
            if not callable(inventory):
                continue
            assert inventory("alice")["count"] == 1, name
            assert inventory("deleted:alice-id")["count"] == 0, name
            assert inventory("bob") == bob_before[name], name
        assert "alice" not in domains["research"].fences

    with factory() as db:
        row = db.query(AccountLifecycleOperation).one()
        operation_id = row.id
        assert row.state == "partial"
        if failure in {"auth_effect_crash", "auth_cancel"}:
            assert row.steps["auth_deleted"]["state"] == "applying"
        else:
            assert row.steps == {}
        assert "claim" not in (row.receipt or {})
        encoded_manifest = str(row.manifest)
        assert "secret" not in encoded_manifest
        assert str(tmp_path) not in encoded_manifest

    domains["sql"].fail_reconcile = False
    auth.barrier_failure = None
    result = asyncio.run(
        endpoint(
            DeleteUserRequest(username="alice", operation_id=operation_id),
            request,
        )
    )
    assert result == {"ok": True, "operation_id": operation_id}
    expected_delete_calls = 1 if failure in {"late_sql", "auth_effect_crash", "auth_cancel"} else 2
    if failure == "compensation_failure":
        expected_delete_calls = 1
    assert auth.delete_calls == expected_delete_calls
    if failure == "compensation_failure":
        assert domains["ancillary"].stage_calls == 2
    for name, domain in domains.items():
        inventory = getattr(domain, "owner_inventory", None)
        if not callable(inventory):
            continue
        assert inventory("alice")["count"] == 0, name
        assert inventory("deleted:alice-id")["count"] == 0, name
        assert inventory("bob") == bob_before[name], name
    completed = coordinator.get_operation(operation_id)
    assert completed["state"] == "complete"
    assert "claim" not in completed["receipt"]
    assert scheduler.owner_runtime_inventory("alice")["count"] == 0
    assert scheduler.owner_runtime_inventory("deleted:alice-id")["count"] == 0
    assert scheduler.owner_runtime_inventory("bob") == bob_runtime
    if failure in {"auth_effect_crash", "auth_cancel"}:
        assert completed["steps"]["auth_deleted"]["receipt"] == {
            "recovered": True,
            "account_absent": True,
        }
    engine.dispose()


@pytest.mark.parametrize("kind", ["rename", "delete"])
def test_cancellation_during_request_drain_releases_fresh_claim(tmp_path, monkeypatch, kind):
    coordinator, factory, engine = _coordinator(tmp_path, monkeypatch)
    auth = _Auth()
    router = setup_auth_routes(auth, account_lifecycle=coordinator)

    async def drive():
        entered = asyncio.Event()

        async def drain(owner):
            assert owner == "alice"
            entered.set()
            await asyncio.Event().wait()

        request = SimpleNamespace(
            cookies={"session": "admin"},
            app=SimpleNamespace(state=SimpleNamespace(drain_account_owner_requests=drain)),
        )
        if kind == "delete":
            task = asyncio.create_task(_delete_endpoint(router)(DeleteUserRequest(username="alice"), request))
        else:
            endpoint = next(r.endpoint for r in router.routes if r.path.endswith("/{username}/rename"))
            task = asyncio.create_task(endpoint("alice", RenameUserRequest(username="carol"), request))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    with factory() as db:
        row = db.query(AccountLifecycleOperation).one()
        assert row.state == "aborted"
        assert "claim" not in (row.receipt or {})
    assert not coordinator.owner_has_active_operation("alice")
    assert "alice" in auth.users
    engine.dispose()


@pytest.mark.parametrize("failure", ["authority", "state_write"])
def test_preflight_failure_releases_claim_and_research_fence(tmp_path, monkeypatch, failure):
    coordinator, factory, engine = _coordinator(tmp_path, monkeypatch)
    research = _ResearchDomain("alice")
    auth = _Auth()

    async def quiesce(owner):
        research.fence_owner(owner)

    if failure == "state_write":
        def fail_state(*args, **kwargs):
            raise RuntimeError("injected state write failure")
        monkeypatch.setattr(coordinator, "set_operation_state", fail_state)

    request = SimpleNamespace(
        cookies={"session": "admin"},
        app=SimpleNamespace(state=SimpleNamespace(
            research_handler=research,
            mimo_supervisor=None,
            drain_account_owner_requests=lambda owner: asyncio.sleep(0),
            quiesce_account_owner_writers=quiesce,
        )),
    )
    endpoint = _delete_endpoint(setup_auth_routes(auth, account_lifecycle=coordinator))
    with pytest.raises(HTTPException):
        asyncio.run(endpoint(DeleteUserRequest(username="alice"), request))
    with factory() as db:
        row = db.query(AccountLifecycleOperation).one()
        assert row.state == "aborted"
        assert "claim" not in (row.receipt or {})
    assert "alice" not in research.fences
    assert not coordinator.owner_has_active_operation("alice")
    assert auth.delete_calls == 0
    engine.dispose()
