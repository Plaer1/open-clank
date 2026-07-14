import json
import sys
import uuid
from types import SimpleNamespace

import pytest

from src.endpoint_resolver import build_chat_url, resolve_model_target
from src.model_dispatch import call_model_target


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


async def test_nonstream_acp_collects_answer_and_cleans_ephemeral_session():
    supervisor = _FakeSupervisor()
    target = resolve_model_target(
        "mimo://acp", "xiaomi/mimo-v2", lifecycle="ephemeral"
    )

    answer = await call_model_target(
        target,
        [{"role": "user", "content": "hi"}],
        session_id="aux-test",
        owner="alice",
        supervisor=supervisor,
    )

    assert answer == "hello world"
    assert supervisor.deleted == ["aux-test"]


async def test_mimo_url_never_reaches_http_client(monkeypatch):
    import src.llm_core as llm_core
    import src.model_dispatch as dispatch

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(dispatch, "_mimo_supervisor", supervisor)

    class _NoHttp:
        def __getattr__(self, name):
            raise AssertionError(f"HTTP client touched through {name}")

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _NoHttp())
    answer = await llm_core.llm_call_async(
        "mimo://acp",
        "xiaomi/mimo-v2",
        [{"role": "user", "content": "hi"}],
        owner="alice",
    )
    assert answer == "hello world"
    assert len(supervisor.deleted) == 1


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
    assert "call_model_target(" in source


def test_settings_reject_stale_or_ineligible_mimo_capabilities(monkeypatch):
    import src.model_dispatch as dispatch
    from routes.auth_routes import _validate_model_settings_update

    supervisor = _FakeSupervisor()
    monkeypatch.setattr(dispatch, "_mimo_supervisor", supervisor)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(mimo_supervisor=supervisor))
    )

    with pytest.raises(Exception, match="default_model is not available"):
        _validate_model_settings_update(
            {"default_endpoint_id": "mimo", "default_model": "missing/model"},
            {},
            request,
            "admin",
        )
    with pytest.raises(Exception, match="does not advertise vision"):
        _validate_model_settings_update(
            {"vision_model": "xiaomi/mimo-v2"},
            {},
            request,
            "admin",
        )


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
    assert "[odysseus_context role=user id=u1 trust=canonical]" in rendered
    assert "[odysseus_context role=assistant id=a1 trust=canonical]" in rendered
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

    assert _mimo_tool_policy({"mode": "chat"}) == {"*": False}
    assert _mimo_tool_policy({"incognito": True}) == {"*": False}
    policy = _mimo_tool_policy({
        "mode": "agent",
        "disabled_tools": ["write_file", "manage_memory"],
    })
    assert policy["edit"] is False
    assert policy["apply_patch"] is False
    assert policy["memory"] is False
    assert policy["frankenmemory_*"] is False


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
            self.inherit_host_providers = kwargs["inherit_host_providers"]
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
    assert alice_a.inherit_host_providers is True
    assert bob.inherit_host_providers is False
    assert len(created) == 2

    await pool.rename_owner("Alice", "Alice2")
    renamed = await pool.for_owner("Alice2")
    assert renamed.inherit_host_providers is True
    await pool.stop()

    single_pool = module.MimoSupervisorPool(
        auth_enabled=False,
        data_dir=tmp_path,
    )
    single = await single_pool.for_owner(None)
    assert single.inherit_host_providers is True
    await single_pool.stop()


async def test_pool_starts_initial_and_explicit_host_provider_owners(monkeypatch, tmp_path):
    import src.openclank.mimo_supervisor as module

    created = []

    class Worker:
        def __init__(self, **kwargs):
            self.owner = kwargs["owner"]
            self.inherit_host_providers = kwargs["inherit_host_providers"]
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
    assert next(worker for worker in created if worker.owner == "bob").inherit_host_providers is True
    assert next(worker for worker in created if worker.owner == "alice").inherit_host_providers is False
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

    with pytest.raises(RuntimeError, match="boom"):
        await pool.start()

    assert created
    assert all(worker.started is False for worker in created)
    assert pool._workers == {}


def test_openclaw_provider_import_maps_models_without_embedding_keys(tmp_path, caplog):
    import src.openclank.mimo_supervisor as module

    source = tmp_path / "openclaw.json"
    source.write_text(json.dumps({
        "models": {"providers": {
            "xiaomi": {
                "baseUrl": "https://xiaomi.example/v1",
                "api": "openai-completions",
                "apiKey": "xiaomi-sentinel",
                "models": [{
                    "id": "mimo-test",
                    "reasoning": True,
                    "input": ["text", "image"],
                    "contextWindow": 1000,
                    "maxTokens": 100,
                }],
            },
            "deepseek": {
                "baseUrl": "https://deepseek.example/anthropic",
                "api": "anthropic-messages",
                "apiKey": "deepseek-sentinel",
                "models": [{"id": "deepseek-test", "name": "DeepSeek Test"}],
            },
        }},
    }))

    config, credentials = module._load_openclaw_providers(source)

    assert set(config["provider"]) == {"xiaomi", "deepseek"}
    assert set(config["provider"]["xiaomi"]["models"]) == {"mimo-test"}
    assert set(config["provider"]["deepseek"]["models"]) == {"deepseek-test"}
    assert config["provider"]["xiaomi"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["deepseek"]["npm"] == "@ai-sdk/anthropic"
    assert "sentinel" not in json.dumps(config)
    assert credentials == {
        "xiaomi": "xiaomi-sentinel",
        "deepseek": "deepseek-sentinel",
    }
    assert "sentinel" not in caplog.text



def test_host_provider_owner_requires_unique_or_explicit_admin():
    from src.openclank.mimo_supervisor import _select_host_provider_owner

    assert _select_host_provider_owner(["Alice"]) == "alice"
    assert _select_host_provider_owner(["Alice", "Bob"]) == ""
    assert _select_host_provider_owner(["Alice", "Bob"], "BOB") == "bob"
    assert _select_host_provider_owner(["Alice"], "Mallory") == ""


def test_mimo_child_environment_strips_provider_credentials(monkeypatch):
    from src.openclank.mimo_supervisor import _mimo_child_environment

    monkeypatch.setenv("XIAOMI_API_KEY", "xiaomi-sentinel")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-sentinel")
    monkeypatch.setenv("FM_TEST_DEEPSEEK_API_KEY", "duplicate-sentinel")
    monkeypatch.setenv("MIMOCODE_PROVIDER_AUTH_FD", "99")
    monkeypatch.setenv("SAFE_SETTING", "kept")

    child_env = _mimo_child_environment()

    assert "XIAOMI_API_KEY" not in child_env
    assert "DEEPSEEK_API_KEY" not in child_env
    assert "FM_TEST_DEEPSEEK_API_KEY" not in child_env
    assert "MIMOCODE_PROVIDER_AUTH_FD" not in child_env
    assert child_env["SAFE_SETTING"] == "kept"


@pytest.mark.parametrize("content", ["{", "[]", '{"models": null}'])
def test_openclaw_provider_import_fails_closed_for_malformed_config(tmp_path, content):
    import src.openclank.mimo_supervisor as module

    source = tmp_path / "openclaw.json"
    source.write_text(content)

    assert module._load_openclaw_providers(source) == ({}, {})


def test_openclaw_provider_import_rejects_oversized_key(tmp_path):
    import src.openclank.mimo_supervisor as module

    source = tmp_path / "openclaw.json"
    source.write_text(json.dumps({
        "models": {"providers": {"xiaomi": {
            "baseUrl": "https://xiaomi.example/v1",
            "api": "openai-completions",
            "apiKey": "x" * (module._MAX_PROVIDER_KEY_LENGTH + 1),
            "models": [{"id": "mimo-test"}],
        }}},
    }))

    config, credentials = module._load_openclaw_providers(source)

    assert "xiaomi" in config["provider"]
    assert credentials == {}


def test_lifetools_descriptor_uses_running_python():
    from src.openclank.acp_bridge import lifetools_mcp_descriptor

    assert lifetools_mcp_descriptor()["command"] == sys.executable


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
        assert len(names) == 2
        assert any(name.startswith("lifetools_") for name in names)
        assert any(name.startswith("frankenmemory_") for name in names)
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
    restarts2 = []

    async def _record_restart2():
        restarts2.append(True)
        sup2._client = None
        sup2._bridge = None

    sup2._client = None
    sup2._teardown_child = _noop
    sup2._reconcile_sessions = _noop
    sup2._restart_with_backoff = _record_restart2

    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", _instant_sleep)
    await sup2._handle_crash(-_signal.SIGTERM)
    assert restarts2 == [True], "external kill without shutdown should self-heal"


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
