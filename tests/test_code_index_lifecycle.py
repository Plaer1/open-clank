"""Isolated authenticated workspace events; no configured model/provider calls."""

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.openclank.file_policy import FilePolicyRepository
from src.openclank.code_index_lifecycle import CodeIndexLifecycleError, workspace_index_event
from src.openclank.workspace_policy_service import WorkspacePolicyServiceError


class Auth:
    users = {name: {"account_id": "s03-account-" + name, "is_admin": False} for name in ("alice", "bob")}

    def account_id(self, owner):
        return self.users.get(owner, {}).get("account_id")

    def is_admin(self, owner):
        return False


class Provider:
    def __init__(self):
        self.calls = []
        self.hold = None

    async def code_index_lifecycle(self, **arguments):
        self.calls.append(arguments)
        if self.hold is not None:
            await self.hold.wait()
        return {"registration_id": "registration-" + arguments["workspace_id"],
                "checkout_id": "checkout", "admission_epoch": 1, "lifecycle": "queued",
                "freshness": "missing", "root": arguments.get("root"), "error": None}


NATIVE_BINARY = os.environ.get("OPEN_CLANK_TEST_FM_MCP_BIN", "")
needs_native = pytest.mark.skipif(not NATIVE_BINARY or not Path(NATIVE_BINARY).is_file(),
    reason="Explicit current s03-fixtures MCP binary is required")


async def native_provider(scope, tmp_path, monkeypatch):
    from src import constants
    from src.frankenmemory_provider import FrankenmemoryProvider

    repository, _workspaces, _bindings, _provider = scope
    data = tmp_path / "native-data"
    data.mkdir()
    gate = tmp_path / "native-gate"
    gate.mkdir()
    paths = {"OPEN_CLANK_DATA_DIR": str(data), "ODYSSEUS_DATA_DIR": str(data),
        "OPEN_CLANK_AUTHORITY_DB_PATH": repository.db_path,
        "OPEN_CLANK_AUTHORITY_AUTH_PATH": str(data / "auth.json"),
        "FM_DB_PATH": str(data / "fm.db"), "FM_S03_FIXTURE_GATE": str(gate)}
    for name, value in paths.items():
        assert Path(value).resolve().is_relative_to(tmp_path.resolve()), name
        monkeypatch.setenv(name, value)
    Path(paths["OPEN_CLANK_AUTHORITY_AUTH_PATH"]).write_text(json.dumps({"users": Auth.users}), encoding="utf-8")
    monkeypatch.setattr(constants, "DATA_DIR", str(data))
    monkeypatch.setattr(constants, "APP_DB", repository.db_path)
    monkeypatch.setattr(constants, "AUTH_FILE", paths["OPEN_CLANK_AUTHORITY_AUTH_PATH"])
    monkeypatch.setattr(constants, "FM_DB_PATH", paths["FM_DB_PATH"])
    provider = FrankenmemoryProvider(command=NATIVE_BINARY, env=paths)
    assert provider._command == NATIVE_BINARY
    assert provider._local_db_path() == paths["FM_DB_PATH"]
    await provider.initialize()
    health = await provider._call_tool("memory_quality", {"rebuild_graph_fts": False})
    assert health["schema_version"] == 16, "Do not certify S04 using an old installed binary"
    assert provider._env["OPEN_CLANK_AUTHORITY_DB_PATH"] == repository.db_path
    return provider, gate


def native_request(scope, *, owner="alice", subscriber="view:one", action="ensure"):
    repository, workspaces, _bindings, _provider = scope
    workspace = repository.get_workspace(workspaces[owner].id)
    return dict(action=action, owner=owner, owner_subject_id=workspace.owner_subject_id,
        workspace_id=workspace.id, location_id=workspace.location_id,
        workspace_revision=workspace.revision, policy_generation=repository.generation(),
        root=None if action in {"release", "cancel", "remove"} else repository.get_location(workspace.location_id).canonical_path,
        subscriber_id=subscriber, origin="agent")


async def wait_native(predicate, label):
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for " + label)


@needs_native
async def test_native_held_index_serves_memory_coalesces_and_isolates_scopes(scope, tmp_path, monkeypatch):
    provider, gate = await native_provider(scope, tmp_path, monkeypatch)
    try:
        for owner in ("alice", "bob"):
            (Path(native_request(scope, owner=owner)["root"]) / "source.py").write_text("def shared_name():\n    return 1\n", encoding="utf-8")
        opened = await asyncio.wait_for(provider.code_index_lifecycle(**native_request(scope)), 3)
        assert opened["lifecycle"] == "queued"
        token = opened["task_token"]
        await wait_native(lambda: (gate / (token + ".held")).exists(), "queued native hold")
        # This call traverses the same provider owner queue/MCP session while
        # the native task is deliberately held, not a second daemon or fake.
        assert await asyncio.wait_for(provider.recall("nothing stored", owner="alice"), 3) == []
        assert not (gate / "release").exists()
        second = await provider.code_index_lifecycle(**native_request(scope, subscriber="session:second"))
        assert second["task_token"] == token
        assert second["checkout_id"] == opened["checkout_id"]
        assert second["subscribers"] == 2
        other = await provider.code_index_lifecycle(**native_request(scope, owner="bob", subscriber="session:bob"))
        assert other["checkout_id"] != opened["checkout_id"]
        assert other["registration_id"] != opened["registration_id"]
        assert other["task_token"] != token
        released = await provider.code_index_lifecycle(**native_request(scope, action="release"))
        assert released["subscribers"] == 1
        assert released["task_token"] == token
        (gate / "release").touch()

        async def ready(owner):
            status = await provider.code_index_lifecycle(**native_request(scope, owner=owner, action="status"))
            return status if status["lifecycle"] == "ready" else False

        status = await wait_native(lambda: ready("alice"), "Alice ready generation")
        other_status = await wait_native(lambda: ready("bob"), "Bob ready generation")
        assert status["head_generation"] is not None
        assert status["checkout_id"] != other_status["checkout_id"]
        assert status["error"] is None
    finally:
        (gate / "release").touch()
        await provider.shutdown()


@needs_native
async def test_native_rich_evidence_preserves_calls_and_erases_only_selected_owner(scope, tmp_path, monkeypatch):
    import sqlite3

    provider, gate = await native_provider(scope, tmp_path, monkeypatch)
    source = "def target():\n    return 1\n\ndef caller():\n    target()\n    target()\n"
    try:
        for owner in ("alice", "bob"):
            (Path(native_request(scope, owner=owner)["root"]) / "source.py").write_text(source, encoding="utf-8")
            await provider.code_index_lifecycle(**native_request(scope, owner=owner))
        (gate / "release").touch()

        async def ready(owner):
            status = await provider.code_index_lifecycle(**native_request(scope, owner=owner, action="status"))
            return status if status["lifecycle"] == "ready" else False

        heads = {owner: await wait_native(lambda owner=owner: ready(owner), owner + " rich evidence")
                 for owner in ("alice", "bob")}
        database_path = Path(provider._local_db_path()).resolve()
        assert database_path.is_relative_to(tmp_path.resolve())

        def evidence_rows(owner):
            with sqlite3.connect("file:" + str(database_path) + "?mode=ro", uri=True) as database:
                files = database.execute(
                    "SELECT workspace_id,checkout_id,rel_path,source_hash,source_generation,payload_json "
                    "FROM code_file_evidence WHERE owner=? ORDER BY checkout_id,rel_path", (owner,)).fetchall()
                resolutions = database.execute(
                    "SELECT occurrence_id,rel_path,source_hash,source_generation,resolution_generation,payload_json "
                    "FROM code_resolutions WHERE owner=? ORDER BY occurrence_id", (owner,)).fetchall()
                return files, resolutions

        snapshots = {owner: evidence_rows(owner) for owner in heads}
        call_ids = {}
        for owner, (files, resolutions) in snapshots.items():
            assert len(files) == 1
            workspace_id, checkout_id, rel_path, source_hash, source_generation, payload = files[0]
            assert workspace_id == native_request(scope, owner=owner)["workspace_id"]
            assert checkout_id == heads[owner]["checkout_id"]
            assert rel_path == "source.py"
            assert source_generation == heads[owner]["head_generation"]
            file = json.loads(payload)
            assert file["checkout_id"] == checkout_id and file["source_hash"] == source_hash
            assert file["rel_path"] == rel_path and file["source_len"] == len(source.encode("utf-8"))
            assert {item["name"] for item in file["declarations"]} == {"target", "caller"}
            calls = [item for item in file["occurrences"] if item["kind"] == "call"]
            assert len(calls) == 2, "Repeated calls must retain distinct occurrence witnesses"
            assert {item["symbol_text"] for item in calls} == {"target"}
            call_ids[owner] = {item["id"] for item in calls}
            assert len(call_ids[owner]) == 2
            assert len({item["source"]["span"]["start_byte"] for item in calls}) == 2
            assert {row[0] for row in resolutions} == {item["id"] for item in file["occurrences"]}
            target_id = next(item["id"] for item in file["declarations"] if item["name"] == "target")
            for occurrence_id, resolution_path, resolution_hash, generation, resolution_generation, payload in resolutions:
                assert (resolution_path, resolution_hash, generation) == (rel_path, source_hash, source_generation)
                assert resolution_generation == heads[owner]["head_generation"]
                resolution = json.loads(payload)
                assert resolution["occurrence_id"] == occurrence_id
                if occurrence_id in call_ids[owner]:
                    assert resolution["state"] == "exact", "This fixture is a direct lexical binding, not runtime dispatch"
                    assert resolution["target_id"] == target_id
                    assert resolution["witnesses"]
        assert call_ids["alice"].isdisjoint(call_ids["bob"])

        erased = await provider.code_index_lifecycle(**native_request(scope, action="remove"))
        assert erased["lifecycle"] == "erased"
        assert evidence_rows("alice") == ([], [])
        assert evidence_rows("bob") == snapshots["bob"], "Alice erasure must not change Bob's evidence or provenance"
        assert (await ready("bob"))["head_generation"] == heads["bob"]["head_generation"]
    finally:
        (gate / "release").touch()
        await provider.shutdown()


@needs_native
async def test_native_erase_late_queued_claim_and_reopen_require_explicit_admit(scope, tmp_path, monkeypatch):
    from src.memory_provider import MemoryRequestRejectedError

    provider, gate = await native_provider(scope, tmp_path, monkeypatch)
    try:
        (Path(native_request(scope)["root"]) / "source.py").write_text("def erased_name():\n    return 1\n", encoding="utf-8")
        opened = await provider.code_index_lifecycle(**native_request(scope))
        await wait_native(lambda: (gate / (opened["task_token"] + ".held")).exists(), "erase fixture hold")
        erased = await provider.code_index_lifecycle(**native_request(scope, action="remove"))
        assert erased["lifecycle"] == "erased"
        (gate / "release").touch()

        # Erasure deliberately invalidates task metadata, including errors.
        # A feature-only worker completion receipt proves the held old claim
        # actually returned before absence/epoch assertions; no timing sleep.
        receipt = gate / (opened["task_token"] + ".finished")
        completion = await wait_native(lambda: receipt.read_text(encoding="utf-8") if receipt.exists() else False, "old queued worker completion")
        assert completion == "rejected", "old captured admission ticket must not claim after erase"
        tombstone = await provider.code_index_lifecycle(**native_request(scope, action="status"))
        assert tombstone["lifecycle"] == "erased"
        assert tombstone["head_generation"] is None
        assert tombstone["error"] is None
        import sqlite3
        database_path = Path(provider._local_db_path()).resolve()
        assert database_path.is_relative_to(tmp_path.resolve())
        with sqlite3.connect("file:" + str(database_path) + "?mode=ro", uri=True) as database:
            for table in ("code_files", "code_index_runs", "code_index_heads", "code_file_evidence", "code_resolutions"):
                assert database.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
            assert database.execute("SELECT lifecycle,admission_epoch FROM code_registrations").fetchone() == ("erased", tombstone["admission_epoch"])
        with pytest.raises(MemoryRequestRejectedError, match="erased"):
            await provider.code_index_lifecycle(**native_request(scope))
        admitted = await provider.code_index_lifecycle(**native_request(scope, action="admit"))
        assert admitted["admission_epoch"] > opened["admission_epoch"]
        assert admitted["task_token"] != opened["task_token"]

        async def ready():
            status = await provider.code_index_lifecycle(**native_request(scope, action="status"))
            return status if status["lifecycle"] == "ready" else False

        assert (await wait_native(ready, "explicitly re-admitted generation"))["head_generation"] is not None
    finally:
        (gate / "release").touch()
        await provider.shutdown()


@needs_native
@pytest.mark.parametrize("loss", ["revoked", "missing", "archived"])
async def test_native_cleanup_ignores_lost_read_but_rejects_other_account(scope, tmp_path, monkeypatch, loss):
    from src.memory_provider import MemoryRequestRejectedError

    repository, workspaces, bindings, _fake = scope
    provider, gate = await native_provider(scope, tmp_path, monkeypatch)
    try:
        await provider.code_index_lifecycle(**native_request(scope))
        if loss == "revoked":
            repository.revoke_binding(bindings["alice", "agent"].id, actor_subject_id="s03-admin")
        elif loss == "missing":
            Path(repository.get_location(workspaces["alice"].location_id).canonical_path).rmdir()
        else:
            repository.update_workspace(workspaces["alice"].id, actor_subject_id="s03-account-alice", archived=True)
        wrong = native_request(scope, action="remove")
        wrong.update(owner="bob", owner_subject_id="s03-account-bob")
        with pytest.raises(MemoryRequestRejectedError, match="ownership"):
            await provider.code_index_lifecycle(**wrong)
        result = await provider.code_index_lifecycle(**native_request(scope, action="remove"))
        assert result["lifecycle"] == "erased"
        assert "root" not in result
    finally:
        (gate / "release").touch()
        await provider.shutdown()


@needs_native
async def test_native_child_restart_keeps_last_ready_and_revalidates_new_job(scope, tmp_path, monkeypatch):
    from src.frankenmemory_provider import FrankenmemoryProvider

    provider, gate = await native_provider(scope, tmp_path, monkeypatch)
    source = Path(native_request(scope)["root"]) / "restart.py"
    source.write_text("def before_restart():\n    return 1\n", encoding="utf-8")
    try:
        opened = await provider.code_index_lifecycle(**native_request(scope))
        await wait_native(lambda: (gate / (opened["task_token"] + ".held")).exists(), "initial restart hold")
        (gate / "release").touch()

        async def ready():
            status = await provider.code_index_lifecycle(**native_request(scope, action="status"))
            return status if status["lifecycle"] == "ready" else False

        first = await wait_native(ready, "initial ready head")
        environment = dict(provider._env)
        await provider.shutdown()
        (gate / "release").unlink()
        source.write_text("def after_restart():\n    return 2\n", encoding="utf-8")
        provider = FrankenmemoryProvider(command=NATIVE_BINARY, env=environment)
        await provider.initialize()
        persisted = await provider.code_index_lifecycle(**native_request(scope, action="status"))
        assert persisted["head_generation"] == first["head_generation"]
        assert persisted["checkout_id"] == first["checkout_id"]
        queued = await provider.code_index_lifecycle(**native_request(scope))
        await wait_native(lambda: (gate / (queued["task_token"] + ".held")).exists(), "replacement held job")
        assert queued["head_generation"] == first["head_generation"]
        assert queued["freshness"] == "stale"
        assert await asyncio.wait_for(provider.recall("empty memory", owner="alice"), 3) == []
        # Stop this child while its replacement job is held. The following
        # process has no in-memory task and must reuse/fence persisted authority.
        await provider.shutdown()
        provider = FrankenmemoryProvider(command=NATIVE_BINARY, env=environment)
        await provider.initialize()
        resumed = await provider.code_index_lifecycle(**native_request(scope))
        assert resumed["head_generation"] == first["head_generation"]
        assert resumed["task_token"] != queued["task_token"]
        (gate / "release").touch()
        current = await wait_native(ready, "restarted replacement ready head")
        assert current["head_generation"] != first["head_generation"]
    finally:
        (gate / "release").touch()
        await provider.shutdown()


@pytest.fixture
def scope(tmp_path, monkeypatch):
    from src import secret_storage
    from src.openclank.files_service_client import close_all_clients

    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_DB_PATH", str(tmp_path / "authority.db"))
    monkeypatch.setenv("OPEN_CLANK_AUTHORITY_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    repository = FilePolicyRepository(tmp_path / "authority.db")
    repository.sync_subjects(Auth.users)
    workspaces = {}
    bindings = {}
    for owner in ("alice", "bob"):
        root = tmp_path / owner
        root.mkdir()
        location = repository.create_location(actor_subject_id="s03-admin", path=str(root), kind="directory", capabilities=("read",))
        for kind in ("people", "agent"):
            bindings[owner, kind] = repository.create_binding(actor_subject_id="s03-admin", binding_class=kind,
                subject_id=Auth().account_id(owner), location_id=location.id, capabilities=("read",))
        workspaces[owner] = repository.create_workspace(actor_subject_id=Auth().account_id(owner),
            owner_subject_id=Auth().account_id(owner), location_id=location.id, name=owner)
    assert Path(repository.db_path).resolve().is_relative_to(tmp_path.resolve())
    assert secret_storage._KEY_PATH.resolve().is_relative_to(tmp_path.resolve())
    close_all_clients()
    yield repository, workspaces, bindings, Provider()
    close_all_clients()


async def event(scope, **changes):
    repository, workspaces, _bindings, provider = scope
    arguments = dict(repository=repository, provider=provider, auth_manager=Auth(), owner="alice",
        workspace_id=workspaces["alice"].id, subscriber_id="view:one")
    arguments.update(changes)
    return await workspace_index_event(**arguments)


async def test_trusted_dto_has_current_binding_and_private_root(scope):
    repository, workspaces, _bindings, provider = scope
    result = await event(scope)
    arguments = provider.calls[0]
    assert arguments["owner_subject_id"] == "s03-account-alice"
    assert arguments["location_id"] == workspaces["alice"].location_id
    assert arguments["workspace_revision"] == workspaces["alice"].revision
    assert arguments["policy_generation"] == repository.generation()
    assert arguments["origin"] == "agent"
    assert "root" not in result
    assert result["workspace_id"] == workspaces["alice"].id


async def test_public_projection_keeps_completeness_and_redacts_nested_root_text(scope, monkeypatch):
    provider = scope[3]

    async def native(**arguments):
        root = arguments["root"]
        return {"lifecycle": "partial", "coverage_complete": False,
            "coverage": [{"rel_path": "source.py", "status": "failed", "hash": "original-hash", "generation": "original-generation",
                "detail": "failed reading " + root + "/source.py"}],
            "error": {"code": "source_unavailable", "message": "source moved under " + root},
            "progress": {"completed_files": 1, "total_files": 2, "debug": {root: [root]}}, "root": root}

    monkeypatch.setattr(provider, "code_index_lifecycle", native)
    result = await event(scope)
    root = scope[0].get_location(scope[1]["alice"].location_id).canonical_path
    assert root not in json.dumps(result)
    assert result["coverage_complete"] is False
    assert result["coverage"][0]["hash"] == "original-hash"
    assert result["coverage"][0]["generation"] == "original-generation"
    assert result["coverage"][0]["rel_path"] == "source.py"
    assert "debug" not in result["progress"]


@pytest.mark.parametrize("private_root,relative", [("/", "src/nested/name.py"), ("/tmp/literal", "folder/tmp/literal/name.py")])
def test_projection_redacts_only_diagnostics_without_touching_source_identity(private_root, relative):
    from src.openclank.code_index_lifecycle import _public_status

    raw = {"registration_id": "opaque/registration", "checkout_id": "opaque/checkout",
        "config_fingerprint": "fingerprint/with/slash", "coverage_complete": True,
        "coverage": [{"rel_path": relative, "status": "failed", "hash": "hash/with/slash", "generation": "generation/with/slash",
            "source_hash": "source/hash", "source_generation": "source/generation",
            "detail": "failed beneath " + private_root + "/private.py", "root": private_root}],
        "progress": {"completed_files": 1, "total_files": 2, "heartbeat_at": "time/with/slash", "root": private_root},
        "error": {"code": "code/with/slash", "message": "cannot read " + private_root, "debug": private_root}}
    projected = _public_status(raw, private_root)
    for key in ("registration_id", "checkout_id", "config_fingerprint"):
        assert projected[key] == raw[key]
    for key in ("rel_path", "hash", "generation", "source_hash", "source_generation"):
        assert projected["coverage"][0][key] == raw["coverage"][0][key]
    assert projected["progress"]["heartbeat_at"] == "time/with/slash"
    assert projected["error"]["code"] == "code/with/slash"
    assert private_root not in projected["error"]["message"]
    assert private_root not in projected["coverage"][0]["detail"]
    assert "root" not in projected["progress"]
    assert "root" not in projected["coverage"][0]
    assert "debug" not in projected["error"]


async def test_other_owner_and_revoked_read_never_reach_provider(scope):
    repository, workspaces, bindings, provider = scope
    with pytest.raises(WorkspacePolicyServiceError):
        await event(scope, workspace_id=workspaces["bob"].id)
    repository.revoke_binding(bindings["alice", "agent"].id, actor_subject_id="s03-admin")
    with pytest.raises(WorkspacePolicyServiceError):
        await event(scope)
    assert provider.calls == []


async def test_expiring_grant_does_not_create_alternate_native_authority(scope):
    repository, _workspaces, bindings, provider = scope
    expiry = int(time.time() * 1000) + 60_000
    repository.update_binding(bindings["alice", "agent"].id, actor_subject_id="s03-admin", expires_unix_ms=expiry)
    await event(scope)
    assert "authority_expires_unix_ms" not in provider.calls[0]


async def test_expired_short_grant_preserves_permanent_read_alternative(scope, monkeypatch):
    from src.openclank import file_policy
    repository, workspaces, bindings, provider = scope
    expiry = int(time.time() * 1000) + 60_000
    repository.update_binding(bindings["alice", "agent"].id, actor_subject_id="s03-admin", expires_unix_ms=expiry)
    repository.create_binding(actor_subject_id="s03-admin", binding_class="agent", subject_id="s03-account-alice",
        location_id=workspaces["alice"].location_id, capabilities=("read",))
    monkeypatch.setattr(file_policy, "_now_ms", lambda: expiry + 1)
    assert (await event(scope))["lifecycle"] == "queued"
    assert "authority_expires_unix_ms" not in provider.calls[0]


@pytest.mark.parametrize("action", ["release", "cancel", "remove"])
@pytest.mark.parametrize("loss", ["revoked", "missing", "archived"])
async def test_cleanup_requires_ownership_but_no_read_or_live_directory(scope, action, loss):
    repository, workspaces, bindings, provider = scope
    if loss == "revoked":
        repository.revoke_binding(bindings["alice", "agent"].id, actor_subject_id="s03-admin")
    elif loss == "missing":
        Path(repository.get_location(workspaces["alice"].location_id).canonical_path).rmdir()
    else:
        repository.update_workspace(workspaces["alice"].id, actor_subject_id="s03-account-alice", archived=True)
    assert (await event(scope, action=action))["workspace_id"] == workspaces["alice"].id
    assert provider.calls[0]["root"] is None
    with pytest.raises(WorkspacePolicyServiceError):
        await event(scope, action=action, owner="bob")
    assert len(provider.calls) == 1


async def test_incognito_and_unselected_session_never_enqueue(scope):
    repository, workspaces, _bindings, provider = scope
    session = SimpleNamespace(owner="alice", workspace_id=workspaces["alice"].id, incognito=True)
    with pytest.raises(CodeIndexLifecycleError, match="incognito"):
        await event(scope, session=session)
    session.incognito = False
    session.workspace_id = workspaces["bob"].id
    with pytest.raises(CodeIndexLifecycleError, match="changed"):
        await event(scope, session=session)
    assert provider.calls == []


async def test_revocation_during_native_reply_rejects_late_status(scope):
    repository, _workspaces, bindings, provider = scope
    provider.hold = asyncio.Event()
    pending = asyncio.create_task(event(scope))
    await asyncio.sleep(0)
    assert provider.calls
    repository.revoke_binding(bindings["alice", "agent"].id, actor_subject_id="s03-admin")
    provider.hold.set()
    with pytest.raises(WorkspacePolicyServiceError):
        await pending


async def test_rebinding_during_native_reply_rejects_late_status(scope):
    _repository, workspaces, _bindings, provider = scope
    session = SimpleNamespace(owner="alice", workspace_id=workspaces["alice"].id, incognito=False)
    provider.hold = asyncio.Event()
    pending = asyncio.create_task(event(scope, session=session))
    await asyncio.sleep(0)
    session.workspace_id = workspaces["bob"].id
    provider.hold.set()
    with pytest.raises(CodeIndexLifecycleError, match="changed"):
        await pending
    assert [call["action"] for call in provider.calls] == ["ensure", "release"]
    assert provider.calls[1]["workspace_id"] == workspaces["alice"].id
    assert provider.calls[1]["root"] is None


async def test_close_before_open_ack_gets_compensating_session_release(scope):
    _repository, workspaces, _bindings, provider = scope
    session = SimpleNamespace(owner="alice", workspace_id=workspaces["alice"].id, incognito=False, archived=False)
    provider.hold = asyncio.Event()
    pending = asyncio.create_task(event(scope, session=session, subscriber_id="session:chat"))
    await asyncio.sleep(0)
    session.archived = True
    provider.hold.set()
    with pytest.raises(CodeIndexLifecycleError):
        await pending
    assert [(call["action"], call["subscriber_id"]) for call in provider.calls] == [("ensure", "session:chat"), ("release", "session:chat")]


@pytest.mark.parametrize("failure", ["sqlite", "policy"])
async def test_optional_session_hook_tolerates_canonical_authority_failure(scope, monkeypatch, failure):
    import sqlite3
    from src.openclank import code_index_lifecycle
    from src.openclank.file_policy import FilePolicyError

    _repository, workspaces, _bindings, provider = scope
    session = SimpleNamespace(id="chat", owner="alice", workspace_id=workspaces["alice"].id, incognito=False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=Auth(), memory_provider=provider)))
    error = sqlite3.OperationalError("offline") if failure == "sqlite" else FilePolicyError("offline")
    monkeypatch.setattr(code_index_lifecycle, "FilePolicyRepository", lambda: (_ for _ in ()).throw(error))
    result = await code_index_lifecycle.session_workspace_index_event(request, session)
    assert result["lifecycle"] == "unavailable"
    assert provider.calls == []


async def test_optional_session_hook_propagates_cancellation(scope):
    from src.openclank.code_index_lifecycle import session_workspace_index_event
    _repository, workspaces, _bindings, provider = scope
    session = SimpleNamespace(id="chat", owner="alice", workspace_id=workspaces["alice"].id, incognito=False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(auth_manager=Auth(), memory_provider=provider)))
    provider.hold = asyncio.Event()
    pending = asyncio.create_task(session_workspace_index_event(request, session))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.parametrize("action", ["status", "release", "cancel", "remove", "admit"])
async def test_explicit_actions_forward_without_implicit_ensure(scope, action):
    await event(scope, action=action)
    assert scope[3].calls[0]["action"] == action


async def test_mounted_routes_only_explicit_open_indexes(scope):
    from routes.file_policy_routes import setup_file_policy_routes

    repository, workspaces, _bindings, provider = scope
    app = FastAPI()
    app.state.auth_manager = Auth()
    app.state.memory_provider = provider
    app.state.session_manager = SimpleNamespace(get_session=lambda sid: SimpleNamespace(
        id=sid, owner="alice", incognito=sid == "incognito", workspace_id=workspaces["alice"].id))

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.current_user = "alice"
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(setup_file_policy_routes(repository=repository))
    root = "/api/file-policy/workspaces/" + workspaces["alice"].id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        assert (await client.get(root + "/resolve", params={"purpose": "agent_workspace"})).status_code == 200
        assert provider.calls == []
        denied = await client.post(root + "/index/open", json={"subscriber_id": "view-1", "root": "/injected"})
        assert denied.status_code == 422
        assert provider.calls == []
        incognito = await client.post(root + "/index/open", json={"subscriber_id": "view-1", "session_id": "incognito"})
        assert incognito.status_code == 403
        assert provider.calls == []
        opened = await client.post(root + "/index/open", json={"subscriber_id": "view-1", "session_id": "chat"})
        assert opened.status_code == 200, opened.text
        assert opened.json()["index"]["lifecycle"] == "queued"
        assert "root" not in opened.json()["index"]
        assert provider.calls[0]["subscriber_id"] == "view:view-1"


@pytest.mark.parametrize("rejection,expected_status,expected_code", [(True, 409, "index_request_rejected"), (False, 503, "index_unavailable")])
async def test_mounted_routes_distinguish_rejection_from_transport_without_private_text(scope, monkeypatch, rejection, expected_status, expected_code):
    from routes.file_policy_routes import setup_file_policy_routes
    from src.memory_provider import MemoryRequestRejectedError, MemoryTransportError

    repository, workspaces, _bindings, provider = scope
    private_root = repository.get_location(workspaces["alice"].location_id).canonical_path

    async def failed(**_arguments):
        error = MemoryRequestRejectedError if rejection else MemoryTransportError
        raise error(private_root + "/private-data " + "unbounded native detail " * 1000)

    monkeypatch.setattr(provider, "code_index_lifecycle", failed)
    app = FastAPI()
    app.state.auth_manager = Auth()
    app.state.memory_provider = provider

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.current_user = "alice"
        request.state.authenticated = True
        return await call_next(request)

    app.include_router(setup_file_policy_routes(repository=repository))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/api/file-policy/workspaces/" + workspaces["alice"].id + "/index/open", json={"subscriber_id": "view"})
        assert response.status_code == expected_status
        assert response.json()["detail"]["code"] == expected_code
        assert private_root not in response.text
        assert len(response.text) < 512


async def test_provider_lifecycle_is_app_only_and_scope_bound(tmp_path, monkeypatch):
    from src.frankenmemory_provider import BROKER_MEMORY_TOOLS, FrankenmemoryProvider
    from src.memory_provider import MemoryRequestRejectedError

    provider = FrankenmemoryProvider(command="/unused", env={"FM_OWNER": "alice", "FM_DB_PATH": str(tmp_path / "fm.db")})
    calls = []

    async def call(name, arguments):
        calls.append((name, arguments))
        return {"lifecycle": "queued"}

    monkeypatch.setattr(provider, "_call_tool", call)
    arguments = dict(action="ensure", owner="alice", owner_subject_id="s03-account-alice", workspace_id="workspace",
        location_id="location", workspace_revision=1, policy_generation=3, root=str(tmp_path), subscriber_id="one", origin="agent")
    assert await provider.code_index_lifecycle(**arguments) == {"lifecycle": "queued"}
    assert calls[0][0] == "code_index"
    assert "code_index" not in BROKER_MEMORY_TOOLS
    arguments["owner"] = "bob"
    with pytest.raises(MemoryRequestRejectedError):
        await provider.code_index_lifecycle(**arguments)
    assert len(calls) == 1


def test_provider_pins_canonical_child_paths_without_ambient_override(tmp_path, monkeypatch):
    from src import constants
    from src.frankenmemory_provider import FrankenmemoryProvider

    monkeypatch.delenv("OPEN_CLANK_AUTHORITY_DB_PATH", raising=False)
    monkeypatch.delenv("OPEN_CLANK_AUTHORITY_AUTH_PATH", raising=False)
    monkeypatch.setattr(constants, "APP_DB", str(tmp_path / "canonical.db"))
    monkeypatch.setattr(constants, "AUTH_FILE", str(tmp_path / "canonical-auth.json"))
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    assert provider._env["OPEN_CLANK_AUTHORITY_DB_PATH"] == str(tmp_path / "canonical.db")
    assert provider._env["OPEN_CLANK_AUTHORITY_AUTH_PATH"] == str(tmp_path / "canonical-auth.json")
    assert all(Path(provider._env[name]).resolve().is_relative_to(tmp_path.resolve())
        for name in ("OPEN_CLANK_AUTHORITY_DB_PATH", "OPEN_CLANK_AUTHORITY_AUTH_PATH", "FM_DB_PATH"))
    assert not (tmp_path / "canonical.db").exists()


@pytest.mark.parametrize("failure", ["invalid_params", "known_code", "unknown_code", "internal", "timeout", "disconnect", "cancelled"])
async def test_transport_classifies_structured_rejection_and_preserves_ambiguity(tmp_path, failure):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    from src.frankenmemory_provider import FrankenmemoryProvider
    from src.memory_provider import MemoryRequestRejectedError, MemoryTransportError

    failures = {
        "invalid_params": McpError(ErrorData(code=-32602, message="invalid operation")),
        "known_code": McpError(ErrorData(code=-32602, message="explicit workspace-index erasure required", data={"code": "code_node_forget_requires_workspace_erase"})),
        "unknown_code": McpError(ErrorData(code=-32602, message="invalid operation", data={"code": "private_arbitrary_code"})),
        "internal": McpError(ErrorData(code=-32603, message="internal operation failure")),
        "timeout": McpError(ErrorData(code=408, message="request timed out")),
        "disconnect": ConnectionError("connection closed"),
        "cancelled": asyncio.CancelledError(),
    }
    provider = FrankenmemoryProvider(command="/unused", env={"FM_DB_PATH": str(tmp_path / "fm.db")})
    provider._owner_task = SimpleNamespace(done=lambda: False)

    class Queue:
        async def put(self, item):
            item[2].set_exception(failures[failure])

    provider._requests = Queue()
    expected = MemoryRequestRejectedError if failure in {"invalid_params", "known_code", "unknown_code"} else asyncio.CancelledError if failure == "cancelled" else MemoryTransportError
    with pytest.raises(expected) as caught:
        await provider._call_tool_transport("code_index", {})
    if expected is MemoryRequestRejectedError:
        assert caught.value.code == ("code_node_forget_requires_workspace_erase" if failure == "known_code" else None)


async def test_canonical_archive_releases_only_its_shared_session(scope, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, Session as DbSession
    from src.openclank.chat_lifecycle import ChatLifecycleService
    from src.openclank.code_index_lifecycle import session_workspace_index_event
    from src import agent_runs
    from src.openclank import transcript_projection

    repository, workspaces, _bindings, provider = scope
    monkeypatch.setattr(agent_runs, "is_active", lambda _sid: False)
    monkeypatch.setattr(transcript_projection, "purge_execution_projection", AsyncMock())
    engine = create_engine("sqlite:///" + str(tmp_path / "chats.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    sessions = {sid: SimpleNamespace(id=sid, owner="alice", incognito=False,
        workspace_id=workspaces["alice"].id, archived=False) for sid in ("chat-a", "chat-b")}
    context = SimpleNamespace(auth_manager=Auth(), memory_provider=provider)
    manager = SimpleNamespace(sessions=sessions, lifecycle_context=context)
    request = SimpleNamespace(app=SimpleNamespace(state=context))
    with factory() as db:
        for sid in sessions:
            db.add(DbSession(id=sid, owner="alice", name=sid, endpoint_url="", model="", workspace_id=workspaces["alice"].id))
        db.commit()
    try:
        await session_workspace_index_event(request, sessions["chat-a"])
        await session_workspace_index_event(request, sessions["chat-b"])
        service = ChatLifecycleService(session_factory=factory, session_manager=manager)
        await service.set_archived(owner="alice", session_id="chat-a", archived=True)
        assert [(call["action"], call["subscriber_id"]) for call in provider.calls] == [
            ("ensure", "session:chat-a"), ("ensure", "session:chat-b"), ("release", "session:chat-a")]
        assert sessions["chat-a"].archived is True
        assert sessions["chat-b"].archived is False
        with factory() as db:
            assert db.get(DbSession, "chat-a").archived is True
            assert db.get(DbSession, "chat-b").archived is False
        # A native release outage cannot roll back the successful chat archive.
        provider.code_index_lifecycle = AsyncMock(side_effect=RuntimeError("offline"))
        await service.set_archived(owner="alice", session_id="chat-b", archived=True)
        with factory() as db:
            assert db.get(DbSession, "chat-b").archived is True
    finally:
        engine.dispose()
