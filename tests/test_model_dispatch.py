import json
import sys
import uuid
from types import SimpleNamespace

import pytest

from src.endpoint_resolver import ResolvedModelTarget, build_chat_url, resolve_model_target
from src.model_dispatch import _typed_error_sse, call_model_target


def test_resolved_target_uses_transport_not_url_guessing_at_call_sites():
    acp = resolve_model_target("mimo://acp", "xiaomi/mimo-v2")
    http = resolve_model_target("https://models.example/v1/chat/completions", "model-a")

    assert (acp.transport, acp.endpoint_id, acp.provider_id) == ("acp", "mimo", "mimo")
    assert http.transport == "http"
    assert acp.capabilities["tools"] is True


@pytest.mark.parametrize("url", ["mimo://wrong", "ftp://models.example/model", "models.example"])
def test_resolved_target_rejects_unknown_transport(url):
    with pytest.raises(ValueError):
        resolve_model_target(url, "model-a")


def test_resolved_target_rejects_ineligible_owner_before_dispatch():
    with pytest.raises(PermissionError):
        resolve_model_target("mimo://acp", "model-a", owner_eligible=False)


def test_unexpected_agent_error_is_safe_and_terminal():
    first, done = _typed_error_sse(RuntimeError("secret upstream body"))
    payload = json.loads(first.split("data: ", 1)[1])

    assert payload == {
        "code": "AGENT_INTERNAL_ERROR",
        "error": "Agent failed before completion.",
        "phase": "internal",
        "retryable": True,
        "status": 500,
    }
    assert "secret upstream body" not in first
    assert done == "data: [DONE]\n\n"


def test_http_url_builder_rejects_acp_transport():
    with pytest.raises(ValueError, match="http or https"):
        build_chat_url("mimo://acp")


class _FakeBridge:
    async def run_turn(self, session_id, messages, **kwargs):
        assert kwargs["owner"] == "alice"
        yield f'data: {json.dumps({"delta": "hello"})}\n\n'
        yield f'data: {json.dumps({"delta": " hidden", "thinking": True})}\n\n'
        yield f'data: {json.dumps({"delta": " world"})}\n\n'
        yield "data: [DONE]\n\n"


class _FakeSupervisor:
    def __init__(self):
        self.bridge = _FakeBridge()
        self.deleted = []

    def is_alive(self):
        return True

    def available_models(self):
        return [{"modelId": "xiaomi/mimo-v2"}]

    async def delete_session(self, session_id):
        self.deleted.append(session_id)


async def test_nonstream_acp_uses_typed_managed_completion(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        "src.openclank.chat_routing.resolve_chat_route",
        lambda **kwargs: SimpleNamespace(
            model_route_id="route-1",
            provider_grant_id=None,
        ),
    )

    async def complete(**kwargs):
        seen.update(kwargs)
        return "hello world"

    monkeypatch.setattr("src.openclank.modality_facade.complete_text", complete)
    target = ResolvedModelTarget(
        transport="acp",
        endpoint_url="openclank://engine",
        model_id="pcn_xiaomi/mimo-v2",
        endpoint_id="pcn_xiaomi",
        provider_id="pcn_xiaomi",
        capabilities={"chat": True},
        lifecycle="ephemeral",
    )

    answer = await call_model_target(
        target,
        [{"role": "user", "content": "hi"}],
        session_id="aux-test",
        owner="alice",
    )

    assert answer == "hello world"
    assert seen["purpose"] == "utility"
    assert seen["model_route_id"] == "route-1"
    assert seen["owner"] == "alice"


async def test_retired_mimo_url_is_rejected_without_reaching_http_client(monkeypatch):
    import src.llm_core as llm_core
    import src.model_dispatch as dispatch

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(dispatch, "_mimo_supervisor", supervisor)

    class _NoHttp:
        def __getattr__(self, name):
            raise AssertionError(f"HTTP client touched through {name}")

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _NoHttp())
    with pytest.raises(
        llm_core.DirectModelDispatchRetired,
        match="managed operation router",
    ):
        await llm_core.llm_call_async(
            "mimo://acp",
            "xiaomi/mimo-v2",
            [{"role": "user", "content": "hi"}],
            owner="alice",
        )
    assert supervisor.deleted == []


class _RejectingClient:
    def __init__(self):
        self.prompt_calls = 0

    def register_callback(self, _method, _handler):
        pass

    def on_session_update(self, _handler):
        pass

    async def set_session_config_option(self, _session, _config, _value):
        raise RuntimeError("not available")

    async def prompt(self, _session, _parts):
        self.prompt_calls += 1
        return {"stopReason": "end_turn"}


async def test_rejected_mimo_model_aborts_before_prompt(monkeypatch, tmp_path):
    from src.openclank.acp_bridge import ACPBridge

    client = _RejectingClient()
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    bridge = ACPBridge(client, cwd=str(tmp_path))

    async def ensure(*_args, **_kwargs):
        bridge._session_models["mimo-session"] = [{"modelId": "provider/actual"}]
        return "mimo-session"

    monkeypatch.setattr(bridge, "ensure_session", ensure)
    chunks = [
        chunk
        async for chunk in bridge.run_turn(
            "odysseus-session",
            [{"role": "user", "content": "hi"}],
            model="provider/missing",
        )
    ]

    assert client.prompt_calls == 0
    assert any('"type": "config_error"' in chunk for chunk in chunks)
    assert any('"status": 409' in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


def test_chat_routes_dispatch_by_resolved_target_not_drive_flag():
    source = open("routes/chat_routes.py", encoding="utf-8").read()
    assert 'os.environ.get("OPENTHESIUS_DRIVE")' not in source
    assert "stream_chat_target(" in source
    assert "stream_agent_target(" in source
    assert "stream_llm(" not in source


def test_turn_envelope_maps_public_interaction_mode_to_internal_provider_mode(monkeypatch):
    import routes.chat_routes as chat_routes

    monkeypatch.setattr(chat_routes, "_transcript_revision", lambda _session_id: 0)
    for public_mode, provider_mode in (
        ("chat", "build"),
        ("agent", "build"),
        ("plan", "plan"),
    ):
        envelope = chat_routes._turn_envelope(
            session_id="session-1",
            owner="alice",
            workspace="/tmp",
            model="model-a",
            mode=public_mode,
            incognito=False,
        )
        assert envelope["mode"] == public_mode
        assert envelope["provider_mode"] == provider_mode


def test_public_mode_source_does_not_render_or_dispatch_provider_native_modes():
    from pathlib import Path

    slash = Path("static/js/slashCommands.js").read_text(encoding="utf-8")
    autocomplete = Path("static/js/slashAutocomplete.js").read_text(encoding="utf-8")
    index = Path("static/index.html").read_text(encoding="utf-8")
    assert "id=\"mimo-mode-select\"" not in index
    assert "availableModes" not in autocomplete
    assert "_setMimoConfig('mode'" not in slash
    assert "/build" not in autocomplete


def test_settings_accept_only_live_normalized_route_capabilities(monkeypatch):
    from routes.auth_routes import _validate_model_settings_update
    from src.openclank.provider_store import ProviderNotFound

    routes = {
        "pmr-chat": SimpleNamespace(
            id="pmr-chat", connection_id="conn", provider_model_id="chat-model",
            enabled=True, deleted_at=None, visibility="visible",
            operations=["chat.complete", "chat.stream"],
        ),
        "pmr-no-vision": SimpleNamespace(
            id="pmr-no-vision", connection_id="conn", provider_model_id="text-only",
            enabled=True, deleted_at=None, visibility="visible",
            operations=["chat.complete"],
        ),
    }

    class Store:
        def get_model_route(self, *, model_route_id, **_kwargs):
            if model_route_id not in routes:
                raise ProviderNotFound("missing")
            return routes[model_route_id]

        def get_connection(self, **_kwargs):
            return SimpleNamespace(id="conn", enabled=True, deleted_at=None)

        def list_connections(self, **_kwargs):
            return [SimpleNamespace(id="conn", enabled=True, deleted_at=None)]

        def list_model_routes(self, **_kwargs):
            return list(routes.values())

    monkeypatch.setattr("src.openclank.provider_store.ProviderStore", Store)
    request = SimpleNamespace()

    _validate_model_settings_update(
        {"default_endpoint_id": "conn", "default_model": "pmr-chat"},
        {},
        request,
        "admin",
    )

    with pytest.raises(Exception, match="available normalized model route"):
        _validate_model_settings_update(
            {"default_endpoint_id": "conn", "default_model": "missing/model"},
            {},
            request,
            "admin",
        )
    with pytest.raises(Exception, match="available normalized model route"):
        _validate_model_settings_update(
            {"vision_model": "pmr-no-vision"},
            {},
            request,
            "admin",
        )


def test_settings_accept_grant_backed_shared_models_without_source_ids(monkeypatch):
    from routes.auth_routes import _validate_model_settings_update

    seen = []

    class Store:
        pass

    def resolve(**kwargs):
        seen.append(dict(kwargs))
        return SimpleNamespace(operations=("chat.complete", "chat.stream"))

    monkeypatch.setattr("src.openclank.provider_store.ProviderStore", Store)
    monkeypatch.setattr("src.openclank.chat_routing.resolve_chat_route", resolve)

    _validate_model_settings_update(
        {
            "memory_endpoint_id": "share:grant-public",
            "memory_model": "provider-model-public",
        },
        {},
        SimpleNamespace(),
        "alice",
    )

    assert len(seen) == 1
    assert seen[0]["owner"] == "alice"
    assert seen[0]["endpoint_id"] == "share:grant-public"
    assert seen[0]["model_id"] == "provider-model-public"
    assert set(seen[0]) == {
        "owner", "endpoint_id", "model_id", "provider_store"
    }


def test_canonical_revision_bumps_once_per_context_mutation():
    from core import database

    database.Base.metadata.create_all(bind=database.engine)
    session_id = f"projection-{uuid.uuid4().hex}"
    first_id = uuid.uuid4().hex
    second_id = uuid.uuid4().hex
    db = database.SessionLocal()
    try:
        db.add(database.Session(
            id=session_id,
            name="projection",
            endpoint_url="https://models.example/v1/chat/completions",
            model="one",
            owner="alice",
        ))
        db.commit()
        assert db.get(database.Session, session_id).transcript_revision == 0

        db.add_all([
            database.ChatMessage(
                id=first_id, session_id=session_id, role="user", content="one"
            ),
            database.ChatMessage(
                id=second_id, session_id=session_id, role="assistant", content="two"
            ),
        ])
        db.commit()
        db.expire_all()
        assert db.get(database.Session, session_id).transcript_revision == 1

        db.get(database.ChatMessage, first_id).content = "edited"
        db.commit()
        db.expire_all()
        assert db.get(database.Session, session_id).transcript_revision == 2

        db.delete(db.get(database.ChatMessage, second_id))
        db.commit()
        db.expire_all()
        assert db.get(database.Session, session_id).transcript_revision == 3

        row = db.get(database.Session, session_id)
        row.endpoint_url = "mimo://acp"
        row.model = "xiaomi/mimo-v2"
        db.commit()
        db.expire_all()
        assert db.get(database.Session, session_id).transcript_revision == 4
    finally:
        db.query(database.ChatMessage).filter_by(session_id=session_id).delete()
        db.query(database.MimoProjection).filter_by(
            odysseus_session_id=session_id
        ).delete()
        db.query(database.Session).filter_by(id=session_id).delete()
        db.commit()
        db.close()


def test_canonical_prompt_replay_uses_stable_ids_and_drops_deleted_content():
    from src.openclank.acp_bridge import _build_prompt_parts, _turn_source_id

    messages = [
        {"role": "user", "content": "secret-to-delete", "metadata": {"_db_id": "u1"}},
        {"role": "assistant", "content": "answer", "metadata": {"_db_id": "a1"}},
        {"role": "user", "content": "continue", "metadata": {"_db_id": "u2"}},
    ]
    turn_id = _turn_source_id(messages)
    parts = _build_prompt_parts(messages, turn_id=turn_id)
    rendered = json.dumps(parts)

    assert turn_id == "u2"
    assert "[odysseus_context role=user id=u1]" in rendered
    assert "[odysseus_context role=assistant id=a1]" in rendered
    assert rendered.count("continue") == 1

    edited = [messages[1], messages[2]]
    replay = json.dumps(
        _build_prompt_parts(edited, turn_id=_turn_source_id(edited))
    )
    assert "secret-to-delete" not in replay


def test_acp_prompt_compiler_preserves_structured_content_and_path_policy(tmp_path):
    from src.openclank.acp_bridge import _build_prompt_parts

    image = "aGVsbG8="
    messages = [{
        "role": "user",
        "metadata": {"_db_id": "u1", "attachments": [{"name": "pic.png"}]},
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
            {"type": "audio", "audio": {"url": f"data:audio/wav;base64,{image}"}},
            {"type": "resource_link", "uri": "https://example.test/doc", "name": "doc"},
            {"type": "resource_link", "uri": "file:///etc/shadow", "name": "blocked"},
        ],
    }]
    parts = _build_prompt_parts(messages, turn_id="u1", workspace=str(tmp_path))

    assert [part["type"] for part in parts] == [
        "text", "image", "resource", "resource_link"
    ]
    assert parts[1]["mimeType"] == "image/png"
    assert parts[2]["resource"]["mimeType"] == "audio/wav"
    assert "shadow" not in json.dumps(parts)


def test_mimo_tool_policy_applies_chat_incognito_and_aliases():
    from src.openclank.acp_bridge import _mimo_tool_policy

    # Tool-free behavior is structural: auxiliary turns and a user's explicit
    # Tools-off preference both get a full deny.
    ordinary = _mimo_tool_policy({"mode": "chat"})
    incognito = _mimo_tool_policy({"incognito": True})
    for policy in (ordinary, incognito):
        assert policy["frankenmemory_*"] is False
        assert policy["read"] is False
        assert policy["grep"] is False
        assert policy["lifetools_*_read_file"] is False
    assert _mimo_tool_policy({"lane": "auxiliary", "incognito": True}) == {"*": False}
    tools_off = _mimo_tool_policy({"lane": "agent", "allowed_tools": []})
    assert tools_off["*"] is False
    assert tools_off["frankenmemory_*"] is False
    assert tools_off["read"] is False
    assert tools_off["lifetools_*_read_file"] is False
    policy = _mimo_tool_policy({
        "mode": "agent",
        "disabled_tools": ["write_file", "manage_memory"],
    })
    assert policy["edit"] is False
    assert policy["apply_patch"] is False
    assert policy["memory"] is False
    assert policy["frankenmemory_*"] is False

    brokered = _mimo_tool_policy({
        "mode": "agent",
        "brokered_file_tools": ["read_file", "grep"],
    })
    assert brokered["read"] is False
    assert brokered["grep"] is False
    assert brokered["lifetools_*_read_file"] is True
    assert brokered["lifetools_*_grep"] is True
    assert brokered["lifetools_*_write_file"] is False


async def test_owner_supervisor_pool_starts_once_and_partitions_home(monkeypatch, tmp_path):
    import src.openclank.mimo_supervisor as module

    created = []

    class Worker:
        def __init__(self, **kwargs):
            self.runtime_home = kwargs["runtime_home"]
            self.owner = kwargs["owner"]
            self.started = False
            self.bridge = None
            self.permission_handler = None
            self.grant_store = kwargs["grant_store"]
            created.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            self.started = False

        def is_alive(self):
            return self.started

        def available_models(self):
            return []

    monkeypatch.setattr(module, "MimoSupervisor", Worker)
    pool = module.MimoSupervisorPool(
        auth_enabled=True,
        initial_owner="Alice",
        host_provider_owner="Alice",
        data_dir=tmp_path,
    )
    alice_a, alice_b = await __import__("asyncio").gather(
        pool.for_owner("Alice"), pool.for_owner("alice")
    )
    bob = await pool.for_owner("bob")

    assert alice_a is alice_b
    assert bob is not alice_a
    assert alice_a.runtime_home != bob.runtime_home
    assert len(created) == 2

    await pool.rename_owner("Alice", "Alice2")
    renamed = await pool.for_owner("Alice2")
    await pool.stop()

    single_pool = module.MimoSupervisorPool(
        auth_enabled=False,
        data_dir=tmp_path,
    )
    single = await single_pool.for_owner(None)
    await single_pool.stop()


def test_agent_runtime_root_migration_is_idempotent_and_reversible(tmp_path):
    import src.openclank.mimo_supervisor as module

    legacy = tmp_path / "mimocode"
    legacy.mkdir()
    (legacy / "proof").write_text("kept")
    current = module.migrate_agent_runtime_root(tmp_path)
    assert current == tmp_path / "runtime" / "agent-engine"
    assert (current / "proof").read_text() == "kept"
    assert module.migrate_agent_runtime_root(tmp_path) == current
    restored = module.migrate_agent_runtime_root(tmp_path, rollback=True)
    assert restored == legacy
    assert (restored / "proof").read_text() == "kept"


def test_agent_runtime_root_conflict_never_merges(tmp_path):
    import src.openclank.mimo_supervisor as module

    (tmp_path / "mimocode").mkdir()
    current = tmp_path / "runtime" / "agent-engine"
    current.mkdir(parents=True)
    (tmp_path / "mimocode" / "legacy").write_text("legacy")
    (current / "current").write_text("current")
    assert module.migrate_agent_runtime_root(tmp_path) == current
    assert (tmp_path / "mimocode" / "legacy").exists()
    assert (current / "current").exists()


def test_owner_supervisor_pool_catalogue_never_falls_back_across_owners(tmp_path):
    """A real pool lookup for an unmaterialized owner must be empty, not merged."""
    import src.openclank.mimo_supervisor as module

    alice = SimpleNamespace(
        available_models=lambda: [{"modelId": "alice/private-model"}],
        provider_apis=lambda: {"alice": "openai"},
    )
    bob = SimpleNamespace(
        available_models=lambda: [{"modelId": "bob/private-model"}],
        provider_apis=lambda: {"bob": "anthropic"},
    )
    pool = module.MimoSupervisorPool(auth_enabled=True, data_dir=tmp_path)
    pool._workers.update({"alice": alice, "bob": bob})

    assert pool.available_models(owner="alice") == [{"modelId": "alice/private-model"}]
    assert pool.provider_apis(owner="alice") == {"alice": "openai"}
    assert pool.available_models(owner="charlie") == []
    assert pool.provider_apis(owner="charlie") == {}
    assert pool.available_models(owner=None) == []
    assert pool.provider_apis(owner=None) == {}

    single = module.MimoSupervisorPool(auth_enabled=False, data_dir=tmp_path)
    single._workers[""] = alice
    assert single.available_models() == [{"modelId": "alice/private-model"}]
    assert single.provider_apis() == {"alice": "openai"}


async def test_pool_starts_initial_and_explicit_host_provider_owners(monkeypatch, tmp_path):
    import src.openclank.mimo_supervisor as module

    created = []

    class Worker:
        def __init__(self, **kwargs):
            self.owner = kwargs["owner"]
            self.bridge = None
            self.permission_handler = None
            self.started = False
            created.append(self)

        async def start(self):
            self.started = True

        async def stop(self):
            self.started = False

        def is_alive(self):
            return self.started

        def available_models(self):
            return []

    monkeypatch.setattr(module, "MimoSupervisor", Worker)
    pool = module.MimoSupervisorPool(
        auth_enabled=True,
        initial_owner="alice",
        host_provider_owner="bob",
        data_dir=tmp_path,
    )

    await pool.start()

    assert {worker.owner for worker in created} == {"alice", "bob"}
    await pool.stop()


async def test_pool_rolls_back_sibling_when_multi_owner_start_fails(monkeypatch, tmp_path):
    import src.openclank.mimo_supervisor as module

    created = []

    class Worker:
        def __init__(self, **kwargs):
            self.owner = kwargs["owner"]
            self.started = False
            created.append(self)

        async def start(self):
            self.started = True
            if self.owner == "bob":
                raise RuntimeError("boom")

        async def stop(self):
            self.started = False

        def is_alive(self):
            return self.started

    monkeypatch.setattr(module, "MimoSupervisor", Worker)
    pool = module.MimoSupervisorPool(
        auth_enabled=True,
        initial_owner="alice",
        host_provider_owner="bob",
        data_dir=tmp_path,
    )

    with pytest.raises(module.SupervisorAdmissionError) as caught:
        await pool.start()

    assert caught.value.code == "SUPERVISOR_CRASHLOOP"
    assert caught.value.__cause__ is not None
    assert "boom" in str(caught.value.__cause__)

    assert created
    assert all(worker.started is False for worker in created)
    assert pool._workers == {}


def test_host_provider_owner_requires_unique_or_explicit_admin():
    from src.openclank.mimo_supervisor import _select_host_provider_owner

    assert _select_host_provider_owner(["Alice"]) == "alice"
    assert _select_host_provider_owner(["Alice", "Bob"]) == ""
    assert _select_host_provider_owner(["Alice", "Bob"], "BOB") == "bob"
    assert _select_host_provider_owner(["Alice"], "Mallory") == ""


def test_mimo_child_environment_strips_provider_credentials(monkeypatch, tmp_path):
    from src.openclank.mimo_supervisor import _mimo_child_environment

    db_path = str(tmp_path / "frankenmemory.db")
    monkeypatch.setenv("XIAOMI_API_KEY", "xiaomi-sentinel")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-sentinel")
    monkeypatch.setenv("FM_TEST_DEEPSEEK_API_KEY", "duplicate-sentinel")
    monkeypatch.setenv("MIMOCODE_PROVIDER_AUTH_FD", "99")
    monkeypatch.setenv("SAFE_SETTING", "kept")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/host/aws")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/host/google.json")
    monkeypatch.setenv("KUBECONFIG", "/host/kube")
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setenv("FM_DB_PATH", db_path)
    monkeypatch.setenv("FM_DB_ID", "db-test")
    monkeypatch.setenv("FM_WORKSPACE_ID", "goal-workspace")
    monkeypatch.setenv("FM_MCP_COMMAND", "/opt/fm-mcp")
    monkeypatch.setenv("FM_EMBED_API_BASE", "https://embed.example/v1")
    monkeypatch.setenv("FM_EMBED_API_KEY", "embed-secret")
    monkeypatch.setenv("FM_EMBED_MODEL", "embedding-model")
    monkeypatch.setenv("FM_EMBED_DIMENSIONS", "3072")
    monkeypatch.setenv("FM_EMBED_TIMEOUT_MS", "45000")

    child_env = _mimo_child_environment()

    assert "XIAOMI_API_KEY" not in child_env
    assert "DEEPSEEK_API_KEY" not in child_env
    assert "FM_TEST_DEEPSEEK_API_KEY" not in child_env
    assert "MIMOCODE_PROVIDER_AUTH_FD" not in child_env
    assert "AWS_SHARED_CREDENTIALS_FILE" not in child_env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in child_env
    assert "KUBECONFIG" not in child_env
    assert "SAFE_SETTING" not in child_env
    assert child_env["PATH"] == "/safe/bin"
    assert child_env["MIMOCODE_DISABLE_PROVIDER_ENV"] == "1"
    assert child_env["MIMOCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert child_env["MIMOCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    assert child_env["MIMOCODE_DISABLE_REMOTE_SKILLS"] == "1"
    assert child_env["MIMOCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert child_env["FM_WORKSPACE_ID"] == "goal-workspace"
    for name in (
        "FM_EMBED_API_BASE",
        "FM_EMBED_API_KEY",
        "FM_EMBED_MODEL",
        "FM_EMBED_DIMENSIONS",
        "FM_EMBED_TIMEOUT_MS",
    ):
        assert name not in child_env
    assert {
        name: child_env[name]
        for name in (
            "FM_DB_PATH",
            "FM_DB_ID",
            "FM_MCP_COMMAND",
        )
    } == {
        "FM_DB_PATH": db_path,
        "FM_DB_ID": "db-test",
        "FM_MCP_COMMAND": "/opt/fm-mcp",
    }


def test_lifetools_descriptor_uses_running_python():
    from src.openclank.acp_bridge import lifetools_mcp_descriptor

    assert lifetools_mcp_descriptor()["command"] == sys.executable


def test_lifetools_exposes_only_brokered_file_forms():
    from src.openclank.lifetools_server import _BRIDGED_TOOLS

    names = {tool.name for tool in _BRIDGED_TOOLS}
    assert {"read_file", "write_file", "edit_file", "ls", "glob", "grep"}.issubset(names)
    assert {"bash", "python", "apply_patch", "get_workspace"}.isdisjoint(names)


async def test_mimo_question_is_owner_revision_bound_and_single_use():
    import asyncio
    from core import database
    from src.openclank.acp_bridge import QuestionHandler
    from src.openclank.transcript_projection import canonical_snapshot, record_projection

    database.Base.metadata.create_all(bind=database.engine)
    session_id = f"question-{uuid.uuid4().hex}"
    db = database.SessionLocal()
    try:
        db.add(database.Session(
            id=session_id,
            name="question",
            endpoint_url="mimo://acp",
            model="provider/model",
            owner="alice",
        ))
        db.commit()
    finally:
        db.close()

    record_projection(
        canonical_snapshot(session_id, owner="alice"),
        mimo_session_id="ses-question",
        workspace="/tmp",
        endpoint_url="mimo://acp",
        model="provider/model",
        turn_id="turn-1",
    )

    handler = QuestionHandler()
    handler.set_context_resolver(lambda _session: {
        "odysseus_session_id": session_id,
        "owner": "alice",
        "plan_revision": 2,
    })
    surfaced = []

    async def on_request(req):
        surfaced.append(req)

    handler.on_request(on_request)
    task = asyncio.create_task(handler.handle({
        "requestId": "q1",
        "sessionId": "ses-question",
        "questions": [
            {
                "header": "Choice",
                "question": "Pick one",
                "options": [
                    {"label": "A", "description": "first"},
                    {"label": "B", "description": "second"},
                ],
                "custom": False,
            },
        ],
    }))
    while not surfaced:
        await asyncio.sleep(0)
    req = surfaced[0]
    assert not handler.resolve(
        req.request_id,
        owner="bob",
        session_id=session_id,
        answers=[["A"]],
    )
    assert not handler.resolve(
        req.request_id,
        owner="alice",
        session_id=session_id,
        answers=[["custom"]],
    )
    assert handler.resolve(
        req.request_id,
        owner="alice",
        session_id=session_id,
        answers=[["A"]],
    )
    assert await task == {"answers": [["A"]]}
    assert not handler.resolve(
        req.request_id,
        owner="alice",
        session_id=session_id,
        answers=[["B"]],
    )
    from src.openclank.transcript_projection import delete_projection
    delete_projection(session_id)
    db = database.SessionLocal()
    try:
        db.query(database.Session).filter(database.Session.id == session_id).delete()
        db.commit()
    finally:
        db.close()


async def test_plan_clarify_revise_approve_and_reconnect_state(tmp_path):
    import asyncio
    from core import database
    from src.openclank.acp_bridge import ACPBridge, _TurnState
    from src.openclank.transcript_projection import canonical_snapshot, record_projection

    class Client:
        initialize_result = {}

        def register_callback(self, *_args):
            pass

        def on_session_update(self, *_args):
            pass

    database.Base.metadata.create_all(bind=database.engine)
    session_id = f"plan-{uuid.uuid4().hex}"
    db = database.SessionLocal()
    try:
        db.add(database.Session(
            id=session_id,
            name="plan",
            endpoint_url="mimo://acp",
            model="provider/model",
            owner="alice",
        ))
        db.commit()
    finally:
        db.close()

    record_projection(
        canonical_snapshot(session_id, owner="alice"),
        mimo_session_id="ses-plan",
        workspace=str(tmp_path),
        endpoint_url="mimo://acp",
        model="provider/model",
        turn_id="turn-plan",
    )
    bridge = ACPBridge(Client(), str(tmp_path), owner="alice")
    bridge._session_map[session_id] = "ses-plan"
    bridge._session_context["ses-plan"] = {
        "odysseus_session_id": session_id,
        "owner": "alice",
        "workspace": str(tmp_path),
        "incognito": False,
    }
    bridge._session_state["ses-plan"] = {"desired": {}, "current": {"mode": "plan"}}
    bridge._queues["ses-plan"] = asyncio.Queue()

    draft = bridge._process_update(
        "ses-plan",
        {"sessionUpdate": "plan", "entries": [{"content": "Draft", "status": "pending"}]},
        _TurnState(),
    )
    assert "plan_update" in "".join(draft)
    assert bridge._session_state["ses-plan"]["plan_state"]["revision"] == 1

    clarification = asyncio.create_task(bridge.question_handler.handle({
        "requestId": "clarify",
        "sessionId": "ses-plan",
        "questions": [{
            "key": "scope",
            "question": "Which scope?",
            "options": [{"label": "Project"}, {"label": "Global"}],
            "custom": False,
        }],
    }))
    update = await bridge._queues["ses-plan"].get()
    request_id = update["request"].request_id
    assert bridge.question_handler.resolve(
        request_id, owner="alice", session_id=session_id, answers=[["Project"]]
    )
    assert await clarification == {"answers": [["Project"]]}

    bridge._process_update(
        "ses-plan",
        {"sessionUpdate": "plan", "entries": [{"content": "Project plan", "status": "pending"}]},
        _TurnState(),
    )
    assert bridge._session_state["ses-plan"]["plan_state"]["revision"] == 2

    approval = asyncio.create_task(bridge.question_handler.handle({
        "requestId": "approve",
        "sessionId": "ses-plan",
        "questions": [{
            "key": "plan_exit",
            "question": "Approve revision 2?",
            "options": [{"label": "Approve"}, {"label": "Revise"}],
            "custom": False,
        }],
    }))
    pending = await bridge._queues["ses-plan"].get()
    reconnected = ACPBridge(Client(), str(tmp_path), owner="alice").negotiated_state(session_id)
    assert reconnected["plan_state"]["revision"] == 2
    assert reconnected["pending_question"]["plan_revision"] == 2
    assert bridge.question_handler.resolve(
        pending["request"].request_id,
        owner="alice",
        session_id=session_id,
        answers=[["Approve"]],
    )
    assert await approval == {"answers": [["Approve"]]}
    restored = ACPBridge(Client(), str(tmp_path), owner="alice").negotiated_state(session_id)
    assert restored["plan_state"]["approved_revision"] == 2
    assert "pending_question" not in restored
    assert "mode_update" in "".join(bridge._process_update(
        "ses-plan",
        {"sessionUpdate": "current_mode_update", "currentModeId": "build"},
        _TurnState(),
    ))

    from src.openclank.transcript_projection import delete_projection
    delete_projection(session_id)
    db = database.SessionLocal()
    try:
        db.query(database.Session).filter(database.Session.id == session_id).delete()
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "u"}},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a"}},
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "t"}},
        {"sessionUpdate": "tool_call", "toolCallId": "c", "title": "read", "status": "pending"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c", "title": "read", "status": "pending"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c", "title": "read", "status": "in_progress"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c", "title": "read", "status": "completed"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "c", "title": "read", "status": "failed"},
        {"sessionUpdate": "plan", "entries": [{"content": "step", "status": "cancelled"}]},
        {"sessionUpdate": "available_commands_update", "availableCommands": []},
        {"sessionUpdate": "current_mode_update", "currentModeId": "plan"},
        {"sessionUpdate": "config_option_update", "configOptions": []},
        {"sessionUpdate": "session_info_update", "title": "session"},
        {"sessionUpdate": "usage_update", "used": 1, "size": 10, "cost": {"amount": 1, "currency": "USD"}},
    ],
)
def test_acp_update_registry_has_explicit_disposition(tmp_path, update):
    from src.openclank.acp_bridge import ACPBridge, _TurnState

    class Client:
        def register_callback(self, *_args):
            pass

        def on_session_update(self, *_args):
            pass

    bridge = ACPBridge(Client(), str(tmp_path), owner="alice")
    events = bridge._process_update("ses", update, _TurnState())
    assert events
    assert "protocol_error" not in "".join(events)


def test_acp_update_registry_rejects_unknown_and_redacts_secrets(tmp_path):
    from src.openclank.acp_bridge import ACPBridge, _TurnState

    class Client:
        def register_callback(self, *_args):
            pass

        def on_session_update(self, *_args):
            pass

    bridge = ACPBridge(Client(), str(tmp_path), owner="alice")
    unknown = bridge._process_update(
        "ses", {"sessionUpdate": "future_update"}, _TurnState()
    )
    tool = bridge._process_update(
        "ses",
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "secret",
            "title": "tool",
            "rawInput": {"api_key": "do-not-leak"},
        },
        _TurnState(),
    )
    assert "protocol_error" in "".join(unknown)
    assert "do-not-leak" not in "".join(tool)


class _ProjectionBridge:
    def mapped_sessions(self):
        return {}


class _ProjectionSupervisor:
    def __init__(self):
        self.bridge = _ProjectionBridge()
        self.deleted = []

    def is_alive(self):
        return True

    async def delete_session(self, session_id, *, mimo_session_id=None):
        self.deleted.append((session_id, mimo_session_id))
        from src.openclank.transcript_projection import delete_projection

        delete_projection(session_id)


async def test_projection_purge_is_owner_checked_and_uses_recorded_mimo_id():
    from core import database
    from src.openclank.transcript_projection import (
        canonical_snapshot,
        get_projection,
        purge_execution_projection,
        record_projection,
    )

    database.Base.metadata.create_all(bind=database.engine)
    session_id = f"projection-{uuid.uuid4().hex}"
    db = database.SessionLocal()
    try:
        db.add(database.Session(
            id=session_id,
            name="projection",
            endpoint_url="mimo://acp",
            model="xiaomi/mimo-v2",
            owner="alice",
        ))
        db.commit()
    finally:
        db.close()

    record_projection(
        canonical_snapshot(session_id, owner="alice"),
        mimo_session_id="ses_actual_mimo_id",
        workspace="/tmp/work",
        endpoint_url="mimo://acp",
        model="xiaomi/mimo-v2",
        turn_id="turn-1",
    )
    supervisor = _ProjectionSupervisor()
    try:
        with pytest.raises(PermissionError):
            await purge_execution_projection(
                supervisor, session_id, owner="bob"
            )
        assert await purge_execution_projection(
            supervisor, session_id, owner="alice"
        )
        assert supervisor.deleted == [(session_id, "ses_actual_mimo_id")]
        assert get_projection(session_id) is None
    finally:
        db = database.SessionLocal()
        db.query(database.MimoProjection).filter_by(
            odysseus_session_id=session_id
        ).delete()
        db.query(database.Session).filter_by(id=session_id).delete()
        db.commit()
        db.close()


class _HandshakeClient:
    initialize_result = {
        "agentCapabilities": {"promptCapabilities": {"image": True}},
        "authMethods": [{"id": "provider", "name": "Provider login"}],
    }

    def register_callback(self, _method, _handler):
        pass

    def on_session_update(self, handler):
        self.update_handler = handler

    async def new_session(self, _cwd, mcp_servers=None):
        names = {server["name"] for server in mcp_servers}
        assert len(names) == 1
        assert any(name.startswith("lifetools_") for name in names)
        assert not any(name.startswith("frankenmemory_") for name in names)
        return {
            "sessionId": "ses-control-plane",
            "models": {
                "currentModelId": "provider/model",
                "availableModels": [
                    {"modelId": "provider/model", "name": "Model"}
                ],
            },
            "modes": {
                "currentModeId": "build",
                "availableModes": [
                    {"id": "build", "name": "Build"},
                    {"id": "plan", "name": "Plan"},
                ],
            },
            "configOptions": [
                {
                    "id": "model",
                    "type": "select",
                    "currentValue": "provider/model",
                    "options": [{"value": "provider/model", "name": "Model"}],
                },
                {
                    "id": "mode",
                    "type": "select",
                    "currentValue": "build",
                    "options": [
                        {"value": "build", "name": "Build"},
                        {"value": "plan", "name": "Plan"},
                    ],
                },
            ],
            "_meta": {"variant": "default"},
        }

    async def set_session_config_option(self, _session, config_id, value):
        assert config_id == "mode"
        return {
            "configOptions": [
                {
                    "id": "mode",
                    "type": "select",
                    "currentValue": value,
                    "options": [
                        {"value": "build", "name": "Build"},
                        {"value": "plan", "name": "Plan"},
                    ],
                }
            ]
        }


async def test_handshake_modes_config_and_commands_persist_owner_scoped(monkeypatch, tmp_path):
    from core import database
    from src.openclank.acp_bridge import ACPBridge, _build_prompt_parts
    from src.openclank.transcript_projection import get_mimo_state

    database.Base.metadata.create_all(bind=database.engine)
    session_id = f"control-{uuid.uuid4().hex}"
    db = database.SessionLocal()
    try:
        db.add(database.Session(
            id=session_id,
            name="control",
            endpoint_url="mimo://acp",
            model="provider/model",
            owner="alice",
        ))
        db.commit()
    finally:
        db.close()

    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    bridge = ACPBridge(_HandshakeClient(), str(tmp_path), owner="alice")
    try:
        mimo_id = await bridge.ensure_session(
            session_id, cwd=str(tmp_path), owner="alice"
        )
        await bridge._handle_session_update(mimo_id, {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [
                {"name": "review", "description": "Review changes"},
                {"name": "compact", "description": "Compact MiMo"},
            ],
        })
        state = await bridge.set_config_option(
            session_id, "mode", "plan", owner="alice"
        )

        assert state["desired"]["mode"] == "plan"
        assert state["current"]["mode"] == "plan"
        saved = get_mimo_state(session_id, owner="alice")
        assert saved["commands"][0]["name"] == "review"
        assert saved["prompt_capabilities"] == {"image": True}
        assert saved["revision"] > 0

        prompt = _build_prompt_parts([
            {"role": "user", "content": "/mimo:compact", "metadata": {"_db_id": "cmd-1"}}
        ], turn_id="cmd-1")
        assert prompt[-1]["text"].endswith("/compact")
    finally:
        bridge.forget_session(session_id)
        db = database.SessionLocal()
        db.query(database.MimoProjection).filter_by(
            odysseus_session_id=session_id
        ).delete()
        db.query(database.Session).filter_by(id=session_id).delete()
        db.commit()
        db.close()


async def test_signal_death_grace_lets_shutdown_win_over_restart(monkeypatch):
    import signal as _signal
    import src.openclank.mimo_supervisor as module

    sup = module.MimoSupervisor(None)
    restarts = []

    async def _noop():
        return None

    async def _record_restart():
        restarts.append(True)

    sup._client = None
    sup._teardown_child = _noop
    sup._reconcile_sessions = _noop
    sup._restart_with_backoff = _record_restart

    # stop() flips the flag while the grace sleep is in flight
    async def _sleep_then_stopping(_seconds):
        sup._stopping = True

    monkeypatch.setattr(module.asyncio, "sleep", _sleep_then_stopping)
    await sup._handle_crash(-_signal.SIGINT)
    assert restarts == [], "SIGINT death during shutdown must not respawn the child"

    # same signal death but nobody is shutting down: restart proceeds
    sup2 = module.MimoSupervisor(None)
    crashes = []

    async def _record_crash(worker, returncode):
        crashes.append((worker, returncode))

    sup2._client = None
    sup2._teardown_child = _noop
    sup2._reconcile_sessions = _noop
    sup2._crash_callback = _record_crash

    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", _instant_sleep)
    await sup2._handle_crash(-_signal.SIGTERM)
    assert crashes == [(sup2, -_signal.SIGTERM)], (
        "external kill must hand recovery to the generation-scoped pool"
    )


async def test_http_transport_drops_acp_only_turn_envelope(monkeypatch):
    """Regression: chat passes turn_envelope for the ACP leg; the HTTP leg
    (direct endpoints, e.g. deepseek post-dedup) must consume it at the
    transport fork instead of exploding stream_llm with a 500."""
    import src.model_dispatch as module
    from src.endpoint_resolver import ResolvedModelTarget

    captured = {}

    async def fake_stream(candidates, messages, **kwargs):
        captured.update(kwargs)
        yield "data: [DONE]\n\n"

    async def fake_agent_stream(url, model, messages, **kwargs):
        captured.update(kwargs)
        yield "data: [DONE]\n\n"

    import src.llm_core as llm_core
    import src.agent_loop as agent_loop
    monkeypatch.setattr(llm_core, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_agent_stream)

    target = ResolvedModelTarget(
        transport="http",
        endpoint_url="https://api.deepseek.com/v1/chat/completions",
        model_id="deepseek-v4-flash",
    )

    async for _ in module.stream_chat_target(
        target, [{"role": "user", "content": "hi"}],
        session_id="s1", turn_envelope={"owner": "e"},
    ):
        pass
    assert "turn_envelope" not in captured

    captured.clear()
    async for _ in module.stream_agent_target(
        target, [{"role": "user", "content": "hi"}],
        session_id="s1", turn_envelope={"owner": "e"},
    ):
        pass
    assert "turn_envelope" not in captured


def test_managed_supervisor_exposes_no_runtime_provider_auth_cache_api():
    """Provider credentials cross only capability-bound managed callbacks."""
    import inspect
    import src.openclank.mimo_supervisor as module

    for name in (
        "_auth_file",
        "_reconcile_auth_store",
        "sync_auth_to_db",
        "_recover_generation_auth_caches",
    ):
        assert not hasattr(module.MimoSupervisor, name)
        assert not hasattr(module.MimoSupervisorPool, name)
    source = inspect.getsource(module)
    assert "MimoAuthStore" not in source
    assert "MimoProjectionState" not in source
    assert "MIMOCODE_PROVIDER_AUTH_FD" not in source
